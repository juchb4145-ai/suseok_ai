"""
Phase 2 OBSERVE acceptance criteria.

- Mock condition include creates strategy candidates.
- Tick replay builds deterministic market/candle data for runtime evaluation.
- Gate evaluation drives READY/BLOCKED strategy state without real orders.
- EntryPlan, VirtualOrder, VirtualPosition, ExitDecision, and TradeReview flow is verified.
- Repeated cycles are idempotent by business key, not only by row count.
- Config, protected subscriptions, UI read-only behavior, export read-only behavior, and
  no OrderRequest/send_order safety are covered before Phase 2 is considered complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from kiwoom.client import MockKiwoomClient
from main import build_observe_runtime
from storage.db import TradingDatabase
from trading.strategy.candidates import CandidateCollector
from trading.strategy.candles import CandleBuilder
from trading.strategy.config import StrategyRuntimeConfigRepository
from trading.strategy.conditions import ConditionProfile, ConditionProfileRepository
from trading.strategy.entry import EntryPlanBuilder
from trading.strategy.exit import ExitDecisionEngine, VirtualPositionService
from trading.strategy.export import REVIEW_EXPORT_COLUMNS, ReviewExporter
from trading.strategy.holding import StaticHoldingProvider
from trading.strategy.models import (
    BlockType,
    Candidate,
    CandidateState,
    FillPolicy,
    GateDecision,
    IndicatorSnapshot,
    ReviewFinalStatus,
    StrategyProfile,
    VirtualOrderStatus,
)
from trading.strategy.pipeline import GatePipelineResult
from trading.strategy.realtime import RealTimeSubscriptionManager
from trading.strategy.replay import TickReplayRunner
from trading.strategy.review import TradeReviewService
from trading.strategy.runtime import StrategyRuntime, StrategyRuntimeConfig, _candidate_generation_summary
from trading.strategy.virtual_orders import VirtualOrderService


NOW = datetime(2026, 5, 29, 9, 0)
CODE = "111111"
THEME_ID = "robot"


@dataclass
class FixedClock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value

    def set(self, value: datetime) -> None:
        self.value = value


class AcceptanceGatePipeline:
    def __init__(
        self,
        *,
        eligible: bool = True,
        block_type: BlockType = BlockType.NONE,
        reason_codes: list[str] | None = None,
        sub_status: str = "PASS",
        final_score: float = 92.0,
    ) -> None:
        self.eligible = eligible
        self.block_type = block_type
        self.reason_codes = list(reason_codes or [])
        self.sub_status = sub_status
        self.final_score = final_score
        self.calls: list[list[str]] = []

    def evaluate(self, candidates):
        self.calls.append([candidate.code for candidate in candidates])
        return [self._result(candidate) for candidate in candidates if candidate.code == CODE]

    def _result(self, candidate):
        grade = "A" if self.eligible else "C"
        stock_decision = GateDecision(
            candidate_id=candidate.id,
            gate_name="StockPullbackEntryGate",
            passed=self.eligible,
            score=self.final_score,
            grade=grade,
            block_type=self.block_type,
            can_recover=self.block_type == BlockType.TEMPORARY,
            recheck_after_sec=60 if self.block_type == BlockType.TEMPORARY else 0,
            reason_codes=list(self.reason_codes),
            details={
                "nearest_support": "vwap",
                "nearest_support_price": 10_000,
                "profile": StrategyProfile.KOSDAQ_THEME_PROFILE.value,
                "sub_status": self.sub_status,
            },
            created_at=NOW.isoformat(),
        )
        final_decision = GateDecision(
            candidate_id=candidate.id,
            gate_name="FinalGrade",
            passed=self.eligible,
            score=self.final_score,
            grade=grade,
            block_type=self.block_type,
            can_recover=self.block_type == BlockType.TEMPORARY,
            recheck_after_sec=60 if self.block_type == BlockType.TEMPORARY else 0,
            reason_codes=list(self.reason_codes),
            details={
                "theme_name": "Robot",
                "actual_order_allowed": False,
                "entry_plan_created": False,
                "sub_status": self.sub_status,
                "cap_rules_applied": list(self.reason_codes),
            },
            created_at=NOW.isoformat(),
        )
        return GatePipelineResult(
            candidate_id=candidate.id,
            code=candidate.code,
            theme_id=THEME_ID,
            final_grade=grade,
            final_score=self.final_score,
            strategy_eligible=self.eligible,
            block_type=self.block_type,
            can_recover=self.block_type == BlockType.TEMPORARY,
            recheck_after_sec=60 if self.block_type == BlockType.TEMPORARY else 0,
            decisions=[stock_decision, final_decision],
            snapshot=IndicatorSnapshot(
                candidate_id=candidate.id,
                code=candidate.code,
                created_at=NOW.isoformat(),
                price=10_000,
                vwap=10_000,
                day_mid=10_000,
                metadata={"strategy_profile": StrategyProfile.KOSDAQ_THEME_PROFILE.value},
            ),
            details={
                "theme_id": THEME_ID,
                "theme_name": "Robot",
                "sub_status": self.sub_status,
                "cap_rules_applied": list(self.reason_codes),
            },
        )


def build_acceptance_runtime(tmp_path, *, config=None, gate=None):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    client = MockKiwoomClient()
    clock = FixedClock()
    collector = CandidateCollector(
        db,
        client=client,
        clock=clock,
        trade_date_provider=lambda: "2026-05-29",
        default_ttl_minutes=30,
    )
    builder = CandleBuilder()
    runtime = StrategyRuntime(
        db=db,
        candidate_collector=collector,
        subscription_manager=RealTimeSubscriptionManager(client, max_codes=(config or StrategyRuntimeConfig()).realtime_subscription_limit),
        candle_builder=builder,
        gate_pipeline=gate or AcceptanceGatePipeline(),
        entry_plan_builder=EntryPlanBuilder(),
        virtual_order_service=VirtualOrderService(db=db),
        virtual_position_service=VirtualPositionService(db=db),
        exit_decision_engine=ExitDecisionEngine(),
        trade_review_service=TradeReviewService(),
        config=config or StrategyRuntimeConfig(),
        clock=clock,
        condition_adapter=None,
        holding_provider=StaticHoldingProvider(),
    )
    return runtime, db, client, clock


def include_candidate(client: MockKiwoomClient) -> None:
    client.set_conditions([(1, "phase2")])
    client.emit_condition_include("phase2", CODE)


def replay(runtime: StrategyRuntime, rows: list[dict]):
    return TickReplayRunner(candle_builder=runtime.candle_builder).replay_rows(rows)


def fill_rows():
    return [
        {"timestamp": "2026-05-29T09:00:01", "code": CODE, "price": 10_050, "cum_volume": 1_000},
        {"timestamp": "2026-05-29T09:00:45", "code": CODE, "price": 10_020, "cum_volume": 1_100},
        {"timestamp": "2026-05-29T09:01:01", "code": CODE, "price": 10_020, "cum_volume": 1_200},
        {"timestamp": "2026-05-29T09:01:20", "code": CODE, "price": 9_990, "cum_volume": 1_300},
        {"timestamp": "2026-05-29T09:01:45", "code": CODE, "price": 10_040, "cum_volume": 1_400},
        {"timestamp": "2026-05-29T09:02:01", "code": CODE, "price": 10_200, "cum_volume": 1_500},
        {"timestamp": "2026-05-29T09:02:30", "code": CODE, "price": 10_650, "cum_volume": 1_700},
        {"timestamp": "2026-05-29T09:02:45", "code": CODE, "price": 10_550, "cum_volume": 1_800},
    ]


def unfilled_rows():
    return [
        {"timestamp": "2026-05-29T09:01:01", "code": CODE, "price": 10_050, "cum_volume": 1_000},
        {"timestamp": "2026-05-29T09:01:20", "code": CODE, "price": 10_020, "cum_volume": 1_100},
        {"timestamp": "2026-05-29T09:01:45", "code": CODE, "price": 10_400, "cum_volume": 1_300},
        {"timestamp": "2026-05-29T09:02:01", "code": CODE, "price": 10_350, "cum_volume": 1_400},
        {"timestamp": "2026-05-29T09:02:45", "code": CODE, "price": 10_300, "cum_volume": 1_500},
        {"timestamp": "2026-05-29T09:06:01", "code": CODE, "price": 10_450, "cum_volume": 1_700},
        {"timestamp": "2026-05-29T09:06:45", "code": CODE, "price": 10_360, "cum_volume": 1_800},
    ]


def entry_keys(db, candidate_id: int):
    return {
        (
            plan.candidate_id,
            plan.cancel_condition.get("theme_id"),
            plan.cancel_condition.get("gate_result_key"),
            plan.entry_type,
        )
        for plan in db.list_entry_plans(candidate_id)
    }


def virtual_order_keys(db, candidate_id: int):
    return {
        (
            order.candidate_id,
            (db.load_entry_plan(order.entry_plan_id).cancel_condition.get("theme_id") if order.entry_plan_id else ""),
            (db.load_entry_plan(order.entry_plan_id).entry_type if order.entry_plan_id else ""),
            order.status.value,
        )
        for order in db.list_virtual_orders(candidate_id)
    }


def position_keys(db, candidate_id: int):
    return {position.virtual_order_id for position in db.list_virtual_positions(candidate_id)}


def exit_decision_keys(db, candidate_id: int):
    keys = set()
    for position in db.list_virtual_positions(candidate_id):
        if position.id is None:
            continue
        for decision in db.list_exit_decisions(position.id):
            keys.add((decision.virtual_position_id, decision.decision_type, decision.trigger_price, position.close_reason))
    return keys


def review_keys(db, candidate_id: int):
    return {(review.trade_date, review.candidate_id, review.theme_id, review.review_key) for review in db.list_trade_reviews(candidate_id)}


def assert_unique(values: set, expected_count: int) -> None:
    assert len(values) == expected_count


def test_candidate_generation_summary_snapshot_reuses_recent_cache(tmp_path, monkeypatch):
    runtime, db, _client, _clock = build_acceptance_runtime(tmp_path)
    monkeypatch.setenv("TRADING_RUNTIME_CANDIDATE_GENERATION_SUMMARY_TTL_SEC", "30")
    candidates = [
        Candidate(
            trade_date="2026-05-29",
            code=CODE,
            metadata={"candidate_generation_seq": 1, "candidate_generation_reason": "stale_re_detected"},
        ),
        Candidate(
            trade_date="2026-05-29",
            code=CODE,
            metadata={"candidate_generation_seq": 2, "candidate_generation_reason": "theme_changed"},
        ),
    ]
    list_calls = 0

    def fake_list_candidates(trade_date=None, *args, **kwargs):
        nonlocal list_calls
        assert trade_date == "2026-05-29"
        list_calls += 1
        return list(candidates)

    def fake_count_candidates(trade_date=None, *args, **kwargs):
        assert trade_date == "2026-05-29"
        return len(candidates)

    monkeypatch.setattr(db, "list_candidates", fake_list_candidates)
    monkeypatch.setattr(db, "count_candidates", fake_count_candidates)
    monkeypatch.setattr(runtime, "_candidate_generation_summary_from_sql", lambda trade_date: None)

    first = runtime._snapshot(NOW)
    second = runtime._snapshot(NOW + timedelta(seconds=1))

    assert list_calls == 1
    assert second.candidate_generation_summary == first.candidate_generation_summary
    assert second.candidate_generation_summary["multi_generation_code_count"] == 1
    assert second.candidate_generation_summary["theme_change_generation_count"] == 1

    candidates.append(
        Candidate(
            trade_date="2026-05-29",
            code="222222",
            metadata={"candidate_generation_seq": 1, "candidate_generation_reason": "source_changed"},
        )
    )
    third = runtime._snapshot(NOW + timedelta(seconds=2))

    assert list_calls == 2
    assert third.candidate_generation_summary["source_change_generation_count"] == 1


def test_candidate_generation_summary_sql_matches_python_summary(tmp_path):
    runtime, db, _client, _clock = build_acceptance_runtime(tmp_path)
    db.save_candidate(
        Candidate(
            trade_date="2026-05-29",
            code=CODE,
            metadata={"candidate_generation_seq": 1, "candidate_generation_reason": "stale_re_detected"},
        )
    )
    db.save_candidate(
        Candidate(
            trade_date="2026-05-29",
            code="222222",
            metadata={"candidate_generation_seq": 3, "generation_reason": "theme_changed"},
        )
    )
    db.save_candidate(
        Candidate(
            trade_date="2026-05-29",
            code="333333",
            metadata={
                "candidate_generation_seq": 0,
                "candidate_generation_reason": "same_generation_min_gap_guardrail",
                "excessive_generation_blocked": True,
            },
        )
    )

    expected = _candidate_generation_summary(db.list_candidates("2026-05-29"))

    assert runtime._candidate_generation_summary_from_sql("2026-05-29") == expected


def test_active_candidate_count_uses_fast_sql_for_simple_states(tmp_path, monkeypatch):
    runtime, db, _client, _clock = build_acceptance_runtime(tmp_path)
    db.save_candidate(Candidate(trade_date="2026-05-29", code=CODE, state=CandidateState.WATCHING))
    db.save_candidate(Candidate(trade_date="2026-05-29", code="BAD", state=CandidateState.WATCHING))
    db.save_candidate(Candidate(trade_date="2026-05-29", code="222222", state=CandidateState.EXPIRED))

    def fail_active_candidates(*args, **kwargs):
        raise AssertionError("simple active count should use SQL")

    monkeypatch.setattr(runtime, "_active_candidates", fail_active_candidates)

    assert runtime._active_candidate_count("2026-05-29", NOW) == 1


def test_active_candidate_count_falls_back_for_recoverable_blocked(tmp_path, monkeypatch):
    runtime, db, _client, _clock = build_acceptance_runtime(tmp_path)
    db.save_candidate(
        Candidate(
            trade_date="2026-05-29",
            code=CODE,
            state=CandidateState.BLOCKED,
            block_type=BlockType.TEMPORARY,
            can_recover=True,
        )
    )
    calls = 0

    def fake_active_candidates(trade_date, now=None):
        nonlocal calls
        calls += 1
        return [object(), object()]

    monkeypatch.setattr(runtime, "_active_candidates", fake_active_candidates)

    assert runtime._active_candidate_count("2026-05-29", NOW) == 2
    assert calls == 1


def test_phase2_eligible_filled_path_acceptance_and_idempotency(tmp_path, monkeypatch):
    runtime, db, client, clock = build_acceptance_runtime(tmp_path)
    calls = []
    monkeypatch.setattr(client, "send_order", lambda request: calls.append(request))
    include_candidate(client)
    replay_result = replay(runtime, fill_rows())

    start_snapshot = runtime.start(NOW)
    first = runtime.cycle(NOW)
    second = runtime.cycle(NOW + timedelta(minutes=3))
    third = runtime.cycle(NOW + timedelta(minutes=3))

    candidate = db.list_candidates()[0]
    reviews = db.list_trade_reviews(candidate.id)
    csv_path = ReviewExporter().export_csv(db.list_trade_reviews(), tmp_path / "phase2.csv")
    md_path = ReviewExporter().export_markdown(db.list_trade_reviews(), tmp_path / "phase2.md")

    assert replay_result.completed_1m_count >= 3
    assert candidate.state == CandidateState.READY
    assert len(db.list_entry_plans(candidate.id)) == 1
    assert [order.status for order in db.list_virtual_orders(candidate.id)] == [VirtualOrderStatus.FILLED]
    assert len(db.list_virtual_positions(candidate.id)) == 1
    assert len(db.list_exit_decisions(db.list_virtual_positions(candidate.id)[0].id)) == 1
    assert reviews[0].final_status == ReviewFinalStatus.VIRTUAL_PARTIAL_TAKE_PROFIT.value
    assert reviews[0].details["horizon_start_reason"] == "position_opened_at"
    assert reviews[0].created_at == NOW.isoformat()
    assert first.entry_plan_count == 1
    assert second.entry_plan_count == 0
    assert third.entry_plan_count == 0
    assert_unique(entry_keys(db, candidate.id), 1)
    assert_unique(virtual_order_keys(db, candidate.id), 1)
    assert_unique(position_keys(db, candidate.id), 1)
    assert_unique(exit_decision_keys(db, candidate.id), 1)
    assert_unique(review_keys(db, candidate.id), 1)
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "## Summary" in md_path.read_text(encoding="utf-8")
    assert "## False Negative" in md_path.read_text(encoding="utf-8")
    assert calls == []
    assert client.orders == []
    assert any(warning.startswith("RECONCILED_SUBSCRIPTIONS") for warning in start_snapshot.warnings)
    db.close()


def test_phase2_blocked_path_acceptance_records_review_without_virtual_order(tmp_path):
    gate = AcceptanceGatePipeline(
        eligible=False,
        block_type=BlockType.TEMPORARY,
        reason_codes=["MARKET_WAIT"],
        sub_status="WAIT_PULLBACK_CONFIRMATION",
        final_score=62,
    )
    runtime, db, client, _clock = build_acceptance_runtime(tmp_path, gate=gate)
    include_candidate(client)
    replay(runtime, unfilled_rows())

    runtime.start(NOW)
    runtime.cycle(NOW)
    runtime.cycle(NOW + timedelta(minutes=1))

    candidate = db.list_candidates()[0]
    reviews = db.list_trade_reviews(candidate.id)

    assert candidate.state == CandidateState.BLOCKED
    assert candidate.block_type == BlockType.TEMPORARY
    assert db.list_entry_plans(candidate.id) == []
    assert db.list_virtual_orders(candidate.id) == []
    assert len(reviews) == 1
    assert reviews[0].final_status == ReviewFinalStatus.BLOCKED_TEMP.value
    assert reviews[0].details["horizon_start_reason"] == "gate_evaluated_at"
    assert reviews[0].details["gate_decisions_snapshot"][-1]["reason_codes"] == ["MARKET_WAIT"]
    assert reviews[0].max_return_20m is not None
    assert_unique(review_keys(db, candidate.id), 1)
    db.close()


def test_phase2_unfilled_path_acceptance_records_missed_metrics(tmp_path):
    runtime, db, client, _clock = build_acceptance_runtime(tmp_path)
    include_candidate(client)
    replay(runtime, unfilled_rows())

    runtime.start(NOW)
    runtime.cycle(NOW)
    runtime.cycle(NOW + timedelta(minutes=6))
    runtime.cycle(NOW + timedelta(minutes=6))

    candidate = db.list_candidates()[0]
    orders = db.list_virtual_orders(candidate.id)
    reviews = db.list_trade_reviews(candidate.id)

    assert candidate.state == CandidateState.READY
    assert len(db.list_entry_plans(candidate.id)) == 1
    assert len(orders) == 1
    assert orders[0].status == VirtualOrderStatus.UNFILLED
    assert orders[0].unfilled_reason == "TIMEOUT"
    assert reviews[0].final_status == ReviewFinalStatus.VIRTUAL_UNFILLED.value
    assert reviews[0].details["horizon_start_reason"] == "virtual_order_submitted_at"
    assert reviews[0].details["false_negative_type"] == "UNFILLED_LATER_RALLIED"
    assert reviews[0].details["timeout_at_metrics"]["max_return_20m"] is not None
    assert_unique(entry_keys(db, candidate.id), 1)
    assert_unique(virtual_order_keys(db, candidate.id), 1)
    assert_unique(review_keys(db, candidate.id), 1)
    db.close()


def test_phase2_review_save_disabled_keeps_trade_flow_but_no_review(tmp_path):
    runtime, db, client, _clock = build_acceptance_runtime(
        tmp_path,
        config=StrategyRuntimeConfig(review_save_enabled=False),
    )
    include_candidate(client)
    replay(runtime, fill_rows())

    runtime.start(NOW)
    runtime.cycle(NOW)

    candidate = db.list_candidates()[0]

    assert len(db.list_entry_plans(candidate.id)) == 1
    assert len(db.list_virtual_orders(candidate.id)) == 1
    assert len(db.list_virtual_positions(candidate.id)) == 1
    assert db.list_trade_reviews(candidate.id) == []
    db.close()


def test_phase2_export_is_read_only_and_has_expected_sections(tmp_path):
    runtime, db, client, _clock = build_acceptance_runtime(tmp_path)
    include_candidate(client)
    replay(runtime, fill_rows())
    runtime.start(NOW)
    runtime.cycle(NOW)
    before = len(db.list_trade_reviews())

    csv_path = ReviewExporter().export_csv(db.list_trade_reviews(), tmp_path / "reviews.csv")
    md_path = ReviewExporter().export_markdown(db.list_trade_reviews(), tmp_path / "reviews.md")
    after = len(db.list_trade_reviews())
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    md_text = md_path.read_text(encoding="utf-8")

    assert before == after == 1
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert csv_text.splitlines()[0] == ",".join(REVIEW_EXPORT_COLUMNS)
    assert "## Summary" in md_text
    assert "## False Negative" in md_text
    assert "## False Positive" in md_text
    assert "## Details" in md_text
    db.close()


def test_phase2_main_build_runtime_uses_saved_config_and_adapter_standard_event(tmp_path, monkeypatch):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    StrategyRuntimeConfigRepository(db).save(
        StrategyRuntimeConfig(
            theme_engine_mode="legacy",
            leader_watch_codes=["035420"],
            holding_watch_codes=["000270"],
            realtime_subscription_limit=1,
            max_candidates_to_watch=1,
        )
    )
    ConditionProfileRepository(db).upsert_profile(
        ConditionProfile(
            condition_name="phase2",
            strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
            enabled=True,
            priority=10,
            purpose="acceptance",
        )
    )
    client = MockKiwoomClient()
    client.set_conditions([(1, "phase2")])
    calls = []
    monkeypatch.setattr(client, "send_order", lambda request: calls.append(request))
    runtime = build_observe_runtime(client, db)

    snapshot = runtime.start(NOW)
    client.emit_condition_load_result(True, "ok")
    client.emit_tr_condition("7600", "A111111;", "phase2", 1, "")

    assert runtime.config.leader_watch_codes == ["035420"]
    assert runtime.config.holding_watch_codes == ["000270"]
    assert {"001", "101", "005930", "000660", "035420", "000270"}.issubset(client.registered_codes)
    assert "PROTECTED_SUBSCRIPTION_OVER_LIMIT" in snapshot.warnings
    assert db.list_candidates()[0].code == CODE
    assert calls == []
    assert client.orders == []
    db.close()


def test_phase2_condition_adapter_disabled_failure_and_missing_paths_are_safe(tmp_path):
    class SpyAdapter:
        def __init__(self, warnings=None):
            self.start_calls = 0
            self.warnings = list(warnings or ["CONDITION_REGISTER_FAILED:phase2:1"])

        def start(self, now):
            self.start_calls += 1
            return list(self.warnings)

        def stop(self):
            return []

    disabled_adapter = SpyAdapter()
    runtime, db, _client, _clock = build_acceptance_runtime(
        tmp_path / "disabled",
        config=StrategyRuntimeConfig(condition_profiles_enabled=False),
    )
    runtime.condition_adapter = disabled_adapter
    snapshot = runtime.start(NOW)
    runtime.cycle(NOW)

    assert disabled_adapter.start_calls == 0
    assert not any("CONDITION_REGISTER_FAILED" in warning for warning in snapshot.warnings)
    db.close()

    failing_adapter = SpyAdapter()
    runtime2, db2, _client2, _clock2 = build_acceptance_runtime(tmp_path / "failing")
    runtime2.condition_adapter = failing_adapter
    start_snapshot = runtime2.start(NOW)
    first_cycle = runtime2.cycle(NOW)
    second_cycle = runtime2.cycle(NOW + timedelta(seconds=1))

    assert failing_adapter.start_calls == 1
    assert "CONDITION_REGISTER_FAILED:phase2:1" in start_snapshot.warnings
    assert first_cycle.warnings.count("CONDITION_REGISTER_FAILED:phase2:1") <= 1
    assert second_cycle.warnings.count("CONDITION_REGISTER_FAILED:phase2:1") <= 1
    db2.close()

    runtime3, db3, _client3, _clock3 = build_acceptance_runtime(tmp_path / "missing")
    runtime3.condition_adapter = None
    runtime3.start(NOW)
    first = runtime3.cycle(NOW)
    second = runtime3.cycle(NOW + timedelta(seconds=1))

    assert not any("CONDITION_ADAPTER" in warning for warning in first.warnings + second.warnings)
    db3.close()


def test_runtime_retries_condition_adapter_when_startup_gateway_was_not_ready(tmp_path):
    class RetryAdapter:
        def __init__(self):
            self.start_calls = 0
            self.registered_conditions = {}

        def start(self, now):
            self.start_calls += 1
            if self.start_calls == 1:
                return ["GATEWAY_HEARTBEAT_REQUIRED_FOR_CONDITIONS"]
            self.registered_conditions[("phase2", 1)] = object()
            return []

        def stop(self):
            self.registered_conditions.clear()
            return []

    adapter = RetryAdapter()
    runtime, db, _client, _clock = build_acceptance_runtime(tmp_path)
    runtime.condition_adapter = adapter

    start_snapshot = runtime.start(NOW)
    cycle_snapshot = runtime.cycle(NOW + timedelta(seconds=5))
    next_cycle = runtime.cycle(NOW + timedelta(seconds=10))

    assert adapter.start_calls == 2
    assert "GATEWAY_HEARTBEAT_REQUIRED_FOR_CONDITIONS" in start_snapshot.warnings
    assert not any("GATEWAY_HEARTBEAT_REQUIRED_FOR_CONDITIONS" in warning for warning in cycle_snapshot.warnings)
    assert adapter.start_calls == 2
    assert next_cycle.started is True
    db.close()


def test_phase2_no_order_static_safety_for_acceptance_paths(monkeypatch, tmp_path):
    runtime, db, client, _clock = build_acceptance_runtime(tmp_path)
    calls = []
    monkeypatch.setattr(client, "send_order", lambda request: calls.append(request))
    include_candidate(client)
    replay(runtime, fill_rows())

    runtime.start(NOW)
    runtime.cycle(NOW)

    assert calls == []
    assert client.orders == []
    for path in [
        "trading/strategy/runtime.py",
        "trading/strategy/config.py",
        "trading/strategy/replay.py",
        "trading/strategy/export.py",
        "ui/main_window.py",
        "main.py",
    ]:
        source = open(path, encoding="utf-8").read()
        assert "OrderRequest" not in source
        assert "send_order" not in source
    db.close()
