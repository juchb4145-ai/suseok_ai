from datetime import datetime, timedelta

import pytest

from kiwoom.client import MockKiwoomClient
from storage.db import TradingDatabase
from trading.strategy.candidates import CandidateCollector
from trading.strategy.candles import CandleBuilder
from trading.strategy.entry import EntryPlanBuilder
from trading.strategy.exit import ExitDecisionEngine, VirtualPositionService
from trading.strategy.holding import StaticHoldingProvider
from trading.strategy.market_data import StrategyTick
from trading.strategy.models import (
    BlockType,
    Candidate,
    CandidateSourceType,
    CandidateState,
    EntryPlan,
    FillPolicy,
    GateDecision,
    IndicatorSnapshot,
    OrderMode,
    ReviewFinalStatus,
    StrategyProfile,
    VirtualOrder,
    VirtualOrderStatus,
    VirtualPosition,
)
from trading.strategy.pipeline import GatePipelineResult
from trading.strategy.realtime import RealTimeSubscriptionManager
from trading.strategy.review import TradeReviewService
from trading.strategy.runtime import StrategyRuntime, StrategyRuntimeConfig
from trading.strategy.themes import ThemeMapping
from trading.strategy.virtual_orders import VirtualOrderService


NOW = datetime(2026, 5, 29, 9, 0)


class FakeGatePipeline:
    def __init__(self, *, fail_batch=False, fail_codes=None, results_factory=None):
        self.fail_batch = fail_batch
        self.fail_codes = set(fail_codes or [])
        self.results_factory = results_factory
        self.calls = []

    def evaluate(self, candidates):
        self.calls.append([candidate.code for candidate in candidates])
        if self.fail_batch and len(candidates) > 1:
            raise RuntimeError("batch boom")
        results = []
        for candidate in candidates:
            if candidate.code in self.fail_codes:
                raise RuntimeError(f"{candidate.code} boom")
            if self.results_factory is None:
                results.append(runtime_gate_result(candidate))
            else:
                results.extend(self.results_factory(candidate))
        return results


def runtime_gate_result(candidate, eligible=True, block_type=BlockType.NONE):
    stock_decision = GateDecision(
        candidate_id=candidate.id,
        gate_name="StockPullbackEntryGate",
        passed=eligible,
        score=90,
        grade="A",
        block_type=block_type,
        details={
            "nearest_support": "vwap",
            "nearest_support_price": 10_000,
            "profile": StrategyProfile.KOSDAQ_THEME_PROFILE.value,
        },
        created_at=NOW.isoformat(),
    )
    final_decision = GateDecision(
        candidate_id=candidate.id,
        gate_name="FinalGrade",
        passed=eligible,
        score=90,
        grade="A" if eligible else "C",
        block_type=block_type,
        details={"theme_name": "로봇", "actual_order_allowed": False, "entry_plan_created": False},
        created_at=NOW.isoformat(),
    )
    return GatePipelineResult(
        candidate_id=candidate.id,
        code=candidate.code,
        theme_id="robot",
        final_grade="A" if eligible else "C",
        final_score=90,
        strategy_eligible=eligible,
        block_type=block_type,
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
        details={"theme_id": "robot", "theme_name": "로봇"},
    )


def runtime_gate_result(
    candidate,
    eligible=True,
    block_type=BlockType.NONE,
    theme_id="robot",
    final_grade=None,
    final_score=90,
    can_recover=None,
    recheck_after_sec=60,
    reason_codes=None,
    sub_status="PASS",
):
    final_grade = final_grade or ("A" if eligible else "C")
    reason_codes = list(reason_codes or [])
    can_recover = block_type == BlockType.TEMPORARY if can_recover is None else can_recover
    stock_decision = GateDecision(
        candidate_id=candidate.id,
        gate_name="StockPullbackEntryGate",
        passed=eligible,
        score=final_score,
        grade=final_grade,
        block_type=block_type,
        can_recover=can_recover,
        recheck_after_sec=recheck_after_sec if can_recover else 0,
        reason_codes=reason_codes,
        details={
            "nearest_support": "vwap",
            "nearest_support_price": 10_000,
            "profile": StrategyProfile.KOSDAQ_THEME_PROFILE.value,
            "sub_status": sub_status,
        },
        created_at=NOW.isoformat(),
    )
    final_decision = GateDecision(
        candidate_id=candidate.id,
        gate_name="FinalGrade",
        passed=eligible,
        score=final_score,
        grade=final_grade,
        block_type=block_type,
        can_recover=can_recover,
        recheck_after_sec=recheck_after_sec if can_recover else 0,
        reason_codes=reason_codes,
        details={
            "theme_name": "robot",
            "actual_order_allowed": False,
            "entry_plan_created": False,
            "sub_status": sub_status,
            "cap_rules_applied": reason_codes,
        },
        created_at=NOW.isoformat(),
    )
    return GatePipelineResult(
        candidate_id=candidate.id,
        code=candidate.code,
        theme_id=theme_id,
        final_grade=final_grade,
        final_score=final_score,
        strategy_eligible=eligible,
        block_type=block_type,
        can_recover=can_recover,
        recheck_after_sec=recheck_after_sec if can_recover else 0,
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
            "theme_id": theme_id,
            "theme_name": "robot",
            "sub_status": sub_status,
            "cap_rules_applied": reason_codes,
        },
    )


def build_runtime(tmp_path, *, config=None, gate_pipeline=None, clock=None, condition_adapter=None, holding_provider=None):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    client = MockKiwoomClient()
    current_time = clock or NOW
    collector = CandidateCollector(
        db,
        client=client,
        clock=lambda: current_time,
        trade_date_provider=lambda: "2026-05-29",
        default_ttl_minutes=30,
    )
    builder = CandleBuilder()
    subscription_manager = RealTimeSubscriptionManager(client, max_codes=80)
    runtime = StrategyRuntime(
        db=db,
        candidate_collector=collector,
        subscription_manager=subscription_manager,
        candle_builder=builder,
        gate_pipeline=gate_pipeline or FakeGatePipeline(),
        entry_plan_builder=EntryPlanBuilder(),
        virtual_order_service=VirtualOrderService(db=db),
        virtual_position_service=VirtualPositionService(db=db),
        exit_decision_engine=ExitDecisionEngine(),
        trade_review_service=TradeReviewService(),
        config=config or StrategyRuntimeConfig(),
        clock=lambda: current_time,
        condition_adapter=condition_adapter,
        holding_provider=holding_provider,
    )
    return runtime, db, client, collector, builder


def save_candidate(db, code="111111", state=CandidateState.WATCHING, **kwargs):
    candidate = Candidate(
        trade_date="2026-05-29",
        code=code,
        name=code,
        market="KOSDAQ",
        strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
        state=state,
        detected_at=kwargs.pop("detected_at", NOW.isoformat()),
        last_seen_at=kwargs.pop("last_seen_at", NOW.isoformat()),
        expires_at=kwargs.pop("expires_at", (NOW + timedelta(minutes=30)).isoformat()),
        block_type=kwargs.pop("block_type", BlockType.NONE),
        can_recover=kwargs.pop("can_recover", False),
        metadata=kwargs.pop("metadata", {}),
    )
    for key, value in kwargs.items():
        setattr(candidate, key, value)
    return db.save_candidate(candidate)


def add_completed_candle(builder, start, high=10_100, low=9_990, close=10_000, code="111111"):
    builder.update(StrategyTick.from_realtime(code, 10_010, cum_volume=1_000, timestamp=start + timedelta(seconds=1)))
    builder.update(StrategyTick.from_realtime(code, high, cum_volume=1_100, timestamp=start + timedelta(seconds=15)))
    builder.update(StrategyTick.from_realtime(code, low, cum_volume=1_200, timestamp=start + timedelta(seconds=30)))
    builder.update(StrategyTick.from_realtime(code, close, cum_volume=1_300, timestamp=start + timedelta(seconds=45)))
    builder.flush(code, start + timedelta(minutes=1))


def event_types(db, candidate_id):
    return [event.event_type for event in db.list_candidate_events(candidate_id)]


def test_runtime_config_validation():
    with pytest.raises(ValueError):
        StrategyRuntimeConfig(order_mode=OrderMode.AUTO_A).validate()
    with pytest.raises(ValueError):
        StrategyRuntimeConfig(evaluation_interval_sec=0).validate()
    with pytest.raises(ValueError):
        StrategyRuntimeConfig(virtual_fill_policy="bad").validate()

    warnings = StrategyRuntimeConfig(max_candidates_to_watch=100, realtime_subscription_limit=10).validate()

    assert "REALTIME_LIMIT_BELOW_MAX_CANDIDATES" in warnings


def test_start_recovers_active_candidates_orders_and_positions(tmp_path):
    runtime, db, client, _, _ = build_runtime(tmp_path)
    active = save_candidate(db, "111111", CandidateState.DETECTED)
    watching = save_candidate(db, "222222", CandidateState.WATCHING)
    save_candidate(db, "333333", CandidateState.BLOCKED, block_type=BlockType.TEMPORARY, can_recover=True)
    save_candidate(db, "444444", CandidateState.REMOVED)
    save_candidate(db, "555555", CandidateState.EXPIRED)
    save_candidate(db, "666666", CandidateState.BLOCKED, block_type=BlockType.FINAL, can_recover=False)
    entry_plan = db.save_entry_plan(plan_for(active.id, active.code))
    submitted = db.save_virtual_order(
        VirtualOrder(candidate_id=active.id, entry_plan_id=entry_plan.id, status=VirtualOrderStatus.SUBMITTED, limit_price=10_000)
    )
    filled_plan = db.save_entry_plan(plan_for(watching.id, watching.code))
    db.save_virtual_order(
        VirtualOrder(candidate_id=watching.id, entry_plan_id=filled_plan.id, status=VirtualOrderStatus.FILLED, limit_price=10_000)
    )
    db.save_virtual_order(
        VirtualOrder(candidate_id=watching.id, entry_plan_id=filled_plan.id, status=VirtualOrderStatus.UNFILLED, limit_price=10_000)
    )
    db.save_virtual_position(
        VirtualPosition(candidate_id=active.id, virtual_order_id=submitted.id, entry_price=10_000, quantity=1, opened_at=NOW.isoformat())
    )
    db.save_virtual_position(
        VirtualPosition(
            candidate_id=watching.id,
            virtual_order_id=999,
            entry_price=10_000,
            quantity=1,
            opened_at=NOW.isoformat(),
            closed_at=(NOW + timedelta(minutes=1)).isoformat(),
        )
    )

    snapshot = runtime.start(NOW)

    assert snapshot.active_candidate_count == 3
    assert snapshot.virtual_order_count == 1
    assert snapshot.filled_order_count == 1
    assert snapshot.open_position_count == 1
    assert "001" in client.registered_codes
    assert "101" in client.registered_codes
    assert "005930" in client.registered_codes
    assert any("RECOVERED_ACTIVE_CANDIDATES=3" == warning for warning in snapshot.warnings)
    db.close()


def test_cycle_orchestrates_virtual_flow_and_is_idempotent(tmp_path):
    runtime, db, _, _, builder = build_runtime(tmp_path)
    candidate = save_candidate(db)
    add_completed_candle(builder, NOW + timedelta(minutes=1), high=10_100, low=9_980)
    add_completed_candle(builder, NOW + timedelta(minutes=2), high=10_600, low=10_000)
    runtime.start(NOW)

    first = runtime.cycle(NOW)
    second = runtime.cycle(NOW)

    assert first.entry_plan_count == 1
    assert first.virtual_order_count == 1
    assert first.filled_order_count == 1
    assert first.open_position_count == 1
    assert first.exit_decision_count == 1
    assert first.review_count == 1
    assert first.evaluated_candidate_count == 1
    assert first.virtual_order_status_change_count == 1
    assert first.db_write_count_per_cycle > 0
    assert second.entry_plan_count == 0
    assert second.virtual_order_count == 0
    assert second.open_position_count == 0
    assert second.review_count == 0
    assert len(db.list_entry_plans(candidate.id)) == 1
    assert len(db.list_virtual_orders(candidate.id)) == 1
    assert len(db.list_virtual_positions(candidate.id)) == 1
    assert len(db.list_exit_decisions(db.list_virtual_positions(candidate.id)[0].id)) == 1
    assert len(db.list_trade_reviews(candidate.id)) == 1
    assert db.list_trade_reviews(candidate.id)[0].final_status == ReviewFinalStatus.VIRTUAL_PARTIAL_TAKE_PROFIT.value
    assert db.load_candidate_by_id(candidate.id).state == CandidateState.READY
    db.close()


def test_no_gate_result_repeated_cycle_does_not_resave_candidate(tmp_path, monkeypatch):
    gate = FakeGatePipeline(results_factory=lambda candidate: [])
    runtime, db, _, _, _ = build_runtime(tmp_path, gate_pipeline=gate)
    candidate = save_candidate(
        db,
        "111111",
        CandidateState.WATCHING,
        metadata={},
    )
    original_last_seen = candidate.last_seen_at
    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=15))
    calls = []

    monkeypatch.setattr(db, "save_candidate", lambda changed: calls.append(changed) or changed)

    snapshot = runtime.cycle(NOW + timedelta(seconds=30))

    assert calls == []
    assert snapshot.candidate_save_count == 0
    assert db.load_candidate_by_id(candidate.id).last_seen_at == original_last_seen
    assert "NO_GATE_RESULT" in db.load_candidate_by_id(candidate.id).metadata["insufficient_reason"]
    db.close()


def test_meaningful_lifecycle_change_still_saves_candidate_and_event(tmp_path, monkeypatch):
    runtime, db, _, _, _ = build_runtime(tmp_path)
    candidate = save_candidate(db, "111111", CandidateState.WATCHING)
    original = db.save_candidate_with_events
    saved_event_batches = []

    def wrapped_save(candidate_to_save, events):
        events = list(events)
        saved_event_batches.append(events)
        return original(candidate_to_save, events)

    monkeypatch.setattr(db, "save_candidate_with_events", wrapped_save)
    runtime.start(NOW)

    snapshot = runtime.cycle(NOW)

    assert snapshot.candidate_save_count >= 1
    assert any(event.event_type == "candidate_ready" for batch in saved_event_batches for event in batch)
    assert db.load_candidate_by_id(candidate.id).state == CandidateState.READY
    assert "candidate_ready" in event_types(db, candidate.id)
    db.close()


def test_cycle_does_not_update_last_seen_at_for_evaluation_only(tmp_path):
    gate = FakeGatePipeline(results_factory=lambda candidate: [])
    runtime, db, _, _, _ = build_runtime(tmp_path, gate_pipeline=gate)
    last_seen = (NOW - timedelta(minutes=5)).isoformat()
    candidate = save_candidate(db, "111111", CandidateState.WATCHING, last_seen_at=last_seen)
    runtime.start(NOW)

    runtime.cycle(NOW + timedelta(seconds=15))

    assert db.load_candidate_by_id(candidate.id).last_seen_at == last_seen
    db.close()


def test_expire_stale_keeps_recent_tick_and_open_virtual_activity(tmp_path):
    class RecentTickStore:
        def has_recent_tick(self, code, now, max_age_sec):
            return code == "111111"

    gate = FakeGatePipeline(results_factory=lambda candidate: [])
    gate.market_data = RecentTickStore()
    runtime, db, _, _, _ = build_runtime(tmp_path, gate_pipeline=gate)
    expired_at = (NOW - timedelta(minutes=1)).isoformat()
    tick_candidate = save_candidate(db, "111111", CandidateState.WATCHING, expires_at=expired_at)
    order_candidate = save_candidate(db, "222222", CandidateState.WATCHING, expires_at=expired_at)
    position_candidate = save_candidate(db, "333333", CandidateState.WATCHING, expires_at=expired_at)
    plan = db.save_entry_plan(plan_for(order_candidate.id, order_candidate.code))
    db.save_virtual_order(
        VirtualOrder(
            candidate_id=order_candidate.id,
            entry_plan_id=plan.id,
            status=VirtualOrderStatus.SUBMITTED,
            limit_price=10_000,
            submitted_at=NOW.isoformat(),
        )
    )
    db.save_virtual_position(
        VirtualPosition(
            candidate_id=position_candidate.id,
            virtual_order_id=1,
            entry_price=10_000,
            quantity=1,
            opened_at=NOW.isoformat(),
        )
    )
    runtime.start(NOW)

    snapshot = runtime.cycle(NOW)

    assert snapshot.expired_count == 0
    assert db.load_candidate_by_id(tick_candidate.id).state == CandidateState.WATCHING
    assert db.load_candidate_by_id(order_candidate.id).state == CandidateState.WATCHING
    assert db.load_candidate_by_id(position_candidate.id).state == CandidateState.WATCHING
    db.close()


def test_runtime_readiness_warns_when_theme_mappings_empty(tmp_path):
    runtime, db, _, _, _ = build_runtime(tmp_path, gate_pipeline=FakeGatePipeline(results_factory=lambda candidate: []))
    save_candidate(db, "111111", CandidateState.WATCHING)

    snapshot = runtime.start(NOW)
    cycle_snapshot = runtime.cycle(NOW)
    reloaded = db.load_candidate("2026-05-29", "111111")

    assert snapshot.theme_mappings_count == 0
    assert "THEME_MAPPING_EMPTY" in snapshot.warnings
    assert "NO_THEME_MAPPING_FOR_ACTIVE_CANDIDATES" in snapshot.warnings
    assert "THEME_MAPPING_EMPTY" in cycle_snapshot.warnings
    assert "THEME_MAPPING_EMPTY" in reloaded.metadata["insufficient_reason"]
    db.close()


def test_runtime_readiness_warns_when_active_candidates_mostly_unmapped(tmp_path):
    runtime, db, _, _, _ = build_runtime(tmp_path)
    db.upsert_theme_mapping(
        ThemeMapping(
            code="111111",
            name="Mapped",
            market="KOSDAQ",
            theme_id="robot",
            theme_name="Robot",
            strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
            enabled=True,
        )
    )
    save_candidate(db, "111111", CandidateState.WATCHING)
    save_candidate(db, "222222", CandidateState.WATCHING)
    save_candidate(db, "333333", CandidateState.WATCHING)

    snapshot = runtime.start(NOW)

    assert snapshot.theme_mappings_count == 1
    assert snapshot.enabled_theme_mappings_count == 1
    assert snapshot.active_candidates_with_theme_mapping == 1
    assert snapshot.active_candidates_without_theme_mapping == 2
    assert snapshot.theme_mapping_coverage_pct < 50
    assert "NO_THEME_MAPPING_FOR_ACTIVE_CANDIDATES" in snapshot.warnings
    db.close()


def test_mapped_candidate_enters_gate_without_no_gate_reason(tmp_path):
    def mapped_results(candidate):
        if db.theme_mappings_for_code(candidate.code, enabled=True):
            return [runtime_gate_result(candidate)]
        return []

    gate = FakeGatePipeline(results_factory=mapped_results)
    runtime, db, _, _, _ = build_runtime(tmp_path, gate_pipeline=gate)
    db.upsert_theme_mapping(
        ThemeMapping(
            code="111111",
            name="Mapped",
            market="KOSDAQ",
            theme_id="robot",
            theme_name="Robot",
            strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
            enabled=True,
        )
    )
    candidate = save_candidate(db, "111111", CandidateState.WATCHING)
    runtime.start(NOW)

    snapshot = runtime.cycle(NOW)
    reloaded = db.load_candidate_by_id(candidate.id)

    assert snapshot.gate_result_count == 1
    assert reloaded.metadata["sub_status"] == "PASS"
    assert "insufficient_reason" not in reloaded.metadata
    assert "NO_GATE_RESULT" not in str(reloaded.metadata)
    db.close()


def test_unmapped_candidate_is_blocked_and_excluded_from_candidate_subscription(tmp_path):
    runtime, db, client, _, _ = build_runtime(tmp_path)
    db.upsert_theme_mapping(
        ThemeMapping(
            code="111111",
            name="Mapped",
            market="KOSDAQ",
            theme_id="robot",
            theme_name="Robot",
            strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
            enabled=True,
        )
    )
    mapped = save_candidate(db, "111111", CandidateState.WATCHING)
    unmapped = save_candidate(db, "222222", CandidateState.WATCHING)

    runtime.start(NOW)
    snapshot = runtime.cycle(NOW)
    reloaded = db.load_candidate_by_id(unmapped.id)

    assert runtime.gate_pipeline.calls[-1] == [mapped.code]
    assert reloaded.state == CandidateState.BLOCKED
    assert reloaded.block_type == BlockType.TEMPORARY
    assert reloaded.can_recover is True
    assert reloaded.metadata["quality_status"] == "unmapped"
    assert reloaded.metadata["insufficient_reason"] == ["NO_THEME_MAPPING_FOR_CANDIDATE"]
    assert "222222" not in client.registered_codes
    assert "candidate_quality_blocked" in event_types(db, unmapped.id)
    assert "NO_THEME_MAPPING_FOR_CANDIDATE" in snapshot.warnings
    db.close()


def test_unmapped_candidate_recovers_when_theme_mapping_is_added(tmp_path):
    runtime, db, _, _, _ = build_runtime(tmp_path)
    db.upsert_theme_mapping(
        ThemeMapping(
            code="111111",
            name="Mapped",
            market="KOSDAQ",
            theme_id="robot",
            theme_name="Robot",
            strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
            enabled=True,
        )
    )
    candidate = save_candidate(db, "222222", CandidateState.WATCHING)
    runtime.start(NOW)
    runtime.cycle(NOW)
    assert db.load_candidate_by_id(candidate.id).state == CandidateState.BLOCKED

    db.upsert_theme_mapping(
        ThemeMapping(
            code="222222",
            name="Recovered",
            market="KOSDAQ",
            theme_id="robot",
            theme_name="Robot",
            strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
            enabled=True,
        )
    )
    runtime.cycle(NOW + timedelta(seconds=61))

    reloaded = db.load_candidate_by_id(candidate.id)
    assert reloaded.state == CandidateState.READY
    assert reloaded.metadata["quality_status"] == "actionable"
    db.close()


def test_invalid_active_candidate_is_removed_by_quality_control(tmp_path):
    runtime, db, _, _, _ = build_runtime(tmp_path)
    candidate = save_candidate(db, "0007C0", CandidateState.WATCHING)
    runtime.start(NOW)

    snapshot = runtime.cycle(NOW)
    reloaded = db.load_candidate_by_id(candidate.id)

    assert reloaded.state == CandidateState.REMOVED
    assert reloaded.metadata["quality_status"] == "invalid_code"
    assert "candidate_quality_removed" in event_types(db, candidate.id)
    assert "INVALID_CANDIDATE_CODE:0007C0" in snapshot.warnings
    db.close()


def test_theme_discovery_candidate_feeds_gate_but_never_creates_entry_plan(tmp_path):
    runtime, db, _, _, builder = build_runtime(tmp_path)
    candidate = save_candidate(
        db,
        "412350",
        CandidateState.WATCHING,
        strategy_profile=StrategyProfile.THEME_DISCOVERY_PROFILE,
        metadata={
            "condition_profiles": {"주도테마_넓은후보": StrategyProfile.THEME_DISCOVERY_PROFILE.value},
            "condition_purposes": {"주도테마_넓은후보": "theme_broad_candidate"},
            "theme_discovery_condition_names": ["주도테마_넓은후보"],
            "entry_condition_names": [],
            "entry_excluded": True,
            "entry_excluded_reason": "theme_broad_candidate",
        },
    )
    add_completed_candle(builder, NOW + timedelta(minutes=1), high=10_100, low=9_980, code="412350")
    runtime.start(NOW)

    snapshot = runtime.cycle(NOW)

    assert runtime.gate_pipeline.calls[-1] == ["412350"]
    assert snapshot.gate_result_count == 1
    assert db.load_candidate_by_id(candidate.id).state == CandidateState.READY
    assert db.list_entry_plans(candidate.id) == []
    assert db.list_virtual_orders(candidate.id) == []
    db.close()


def test_detected_candidate_does_not_jump_directly_to_ready(tmp_path):
    config = StrategyRuntimeConfig(max_candidates_to_watch=0)
    runtime, db, _, _, _ = build_runtime(tmp_path, config=config)
    candidate = save_candidate(db, "111111", CandidateState.DETECTED)
    runtime.start(NOW)

    snapshot = runtime.cycle(NOW)
    reloaded = db.load_candidate_by_id(candidate.id)

    assert reloaded.state == CandidateState.WATCHING
    assert db.list_entry_plans(candidate.id) == []
    assert "candidate_ready" not in event_types(db, candidate.id)
    assert not any("CANDIDATE_LIFECYCLE_FAILED" in warning for warning in snapshot.warnings)
    db.close()


def test_multi_theme_candidate_ready_uses_best_eligible_theme(tmp_path):
    def results(candidate):
        return [
            runtime_gate_result(candidate, eligible=False, block_type=BlockType.FINAL, theme_id="weak", final_score=40, reason_codes=["THEME_WEAK"], sub_status="THEME_WEAK"),
            runtime_gate_result(candidate, eligible=True, theme_id="strong", final_score=95, final_grade="A"),
        ]

    runtime, db, _, _, _ = build_runtime(tmp_path, gate_pipeline=FakeGatePipeline(results_factory=results))
    candidate = save_candidate(db, "111111", CandidateState.WATCHING)
    runtime.start(NOW)

    runtime.cycle(NOW)
    reloaded = db.load_candidate_by_id(candidate.id)

    assert reloaded.state == CandidateState.READY
    assert reloaded.metadata["best_theme_id"] == "strong"
    assert reloaded.metadata["best_gate_result_key"] == f"{candidate.id}:111111:strong:A"
    assert set(reloaded.metadata["gate_results_by_theme"]) == {"weak", "strong"}
    assert len(db.list_entry_plans(candidate.id)) == 1
    assert db.list_entry_plans(candidate.id)[0].cancel_condition["theme_id"] == "strong"
    db.close()


def test_partial_theme_final_block_does_not_make_candidate_final_block(tmp_path):
    def results(candidate):
        return [
            runtime_gate_result(candidate, eligible=False, block_type=BlockType.FINAL, theme_id="dead", final_score=20, reason_codes=["THEME_WEAK"], sub_status="THEME_WEAK"),
            runtime_gate_result(candidate, eligible=False, block_type=BlockType.TEMPORARY, theme_id="wait", final_score=65, reason_codes=["WAIT_PULLBACK_CONFIRMATION"], sub_status="WAIT_PULLBACK_CONFIRMATION"),
        ]

    runtime, db, _, _, _ = build_runtime(tmp_path, gate_pipeline=FakeGatePipeline(results_factory=results))
    candidate = save_candidate(db, "111111", CandidateState.WATCHING)
    runtime.start(NOW)

    runtime.cycle(NOW)
    reloaded = db.load_candidate_by_id(candidate.id)

    assert reloaded.state == CandidateState.BLOCKED
    assert reloaded.block_type == BlockType.TEMPORARY
    assert reloaded.can_recover is True
    assert reloaded.metadata["block_reasons_by_theme"]["dead"]["block_type"] == BlockType.FINAL.value
    assert reloaded.metadata["block_reasons_by_theme"]["wait"]["block_type"] == BlockType.TEMPORARY.value
    db.close()


def test_all_theme_final_blocks_candidate_final(tmp_path):
    def results(candidate):
        return [
            runtime_gate_result(candidate, eligible=False, block_type=BlockType.FINAL, theme_id="dead1", final_score=20, reason_codes=["THEME_WEAK"], sub_status="THEME_WEAK"),
            runtime_gate_result(candidate, eligible=False, block_type=BlockType.FINAL, theme_id="dead2", final_score=30, reason_codes=["CHASE_RISK"], sub_status="CHASE_RISK"),
        ]

    runtime, db, _, _, _ = build_runtime(tmp_path, gate_pipeline=FakeGatePipeline(results_factory=results))
    candidate = save_candidate(db, "111111", CandidateState.WATCHING)
    runtime.start(NOW)

    runtime.cycle(NOW)
    reloaded = db.load_candidate_by_id(candidate.id)

    assert reloaded.state == CandidateState.BLOCKED
    assert reloaded.block_type == BlockType.FINAL
    assert reloaded.can_recover is False
    assert event_types(db, candidate.id).count("candidate_blocked_final") == 1
    db.close()


def test_repeated_cycle_does_not_duplicate_ready_event(tmp_path):
    runtime, db, _, _, builder = build_runtime(tmp_path)
    candidate = save_candidate(db, "111111", CandidateState.WATCHING)
    add_completed_candle(builder, NOW + timedelta(minutes=1), low=9_980)
    runtime.start(NOW)

    runtime.cycle(NOW)
    runtime.cycle(NOW)

    assert event_types(db, candidate.id).count("candidate_ready") == 1
    db.close()


def test_review_save_disabled_skips_trade_review(tmp_path):
    config = StrategyRuntimeConfig(review_save_enabled=False)
    runtime, db, _, _, builder = build_runtime(tmp_path, config=config)
    candidate = save_candidate(db)
    add_completed_candle(builder, NOW + timedelta(minutes=1), low=9_980)
    runtime.start(NOW)

    snapshot = runtime.cycle(NOW)

    assert snapshot.review_count == 0
    assert db.list_trade_reviews(candidate.id) == []
    db.close()


def test_pr_2_2_mutates_ready_and_still_allows_expire(tmp_path):
    runtime, db, _, _, _ = build_runtime(tmp_path)
    active = save_candidate(db, "111111", CandidateState.WATCHING)
    stale = save_candidate(db, "222222", CandidateState.WATCHING, expires_at=(NOW - timedelta(minutes=1)).isoformat())
    runtime.start(NOW)

    runtime.cycle(NOW)

    assert db.load_candidate_by_id(active.id).state == CandidateState.READY
    assert db.load_candidate_by_id(stale.id).state == CandidateState.EXPIRED
    db.close()


def test_blocked_temp_not_rechecked_before_next_recheck(tmp_path):
    runtime, db, _, _, _ = build_runtime(tmp_path)
    candidate = save_candidate(
        db,
        "111111",
        CandidateState.BLOCKED,
        block_type=BlockType.TEMPORARY,
        can_recover=True,
        recheck_after_sec=60,
        sources=[CandidateSourceType.MANUAL_DEBUG],
        metadata={
            "blocked_at": NOW.isoformat(),
            "next_recheck_at": (NOW + timedelta(seconds=60)).isoformat(),
        },
    )
    runtime.start(NOW)

    runtime.cycle(NOW + timedelta(seconds=30))

    assert "candidate_block_rechecked" not in event_types(db, candidate.id)
    db.close()


def test_blocked_temp_recheck_updates_metadata(tmp_path):
    gate = FakeGatePipeline(
        results_factory=lambda candidate: [
            runtime_gate_result(
                candidate,
                eligible=False,
                block_type=BlockType.TEMPORARY,
                theme_id="wait",
                can_recover=True,
                recheck_after_sec=90,
                reason_codes=["WAIT_PULLBACK_CONFIRMATION"],
                sub_status="WAIT_PULLBACK_CONFIRMATION",
            )
        ]
    )
    runtime, db, _, _, _ = build_runtime(tmp_path, gate_pipeline=gate)
    candidate = save_candidate(
        db,
        "111111",
        CandidateState.BLOCKED,
        block_type=BlockType.TEMPORARY,
        can_recover=True,
        recheck_after_sec=60,
        sources=[CandidateSourceType.MANUAL_DEBUG],
        metadata={
            "blocked_at": NOW.isoformat(),
            "next_recheck_at": (NOW + timedelta(seconds=60)).isoformat(),
            "block_count": 1,
        },
    )
    runtime.start(NOW)

    runtime.cycle(NOW + timedelta(seconds=60))
    reloaded = db.load_candidate_by_id(candidate.id)

    assert reloaded.state == CandidateState.BLOCKED
    assert reloaded.block_type == BlockType.TEMPORARY
    assert reloaded.metadata["blocked_at"] == NOW.isoformat()
    assert reloaded.metadata["last_rechecked_at"] == (NOW + timedelta(seconds=60)).isoformat()
    assert reloaded.metadata["next_recheck_at"] == (NOW + timedelta(seconds=150)).isoformat()
    assert reloaded.metadata["block_count"] == 2
    assert event_types(db, candidate.id).count("candidate_block_rechecked") == 1
    db.close()


def test_data_insufficient_candidate_stays_watching_not_blocked(tmp_path):
    gate = FakeGatePipeline(
        results_factory=lambda candidate: [
            runtime_gate_result(
                candidate,
                eligible=False,
                block_type=BlockType.TEMPORARY,
                theme_id="robot",
                reason_codes=["DATA_INSUFFICIENT"],
                sub_status="DATA_INSUFFICIENT",
            )
        ]
    )
    runtime, db, _, _, _ = build_runtime(tmp_path, gate_pipeline=gate)
    candidate = save_candidate(db, "111111", CandidateState.WATCHING)
    runtime.start(NOW)

    runtime.cycle(NOW)
    reloaded = db.load_candidate_by_id(candidate.id)

    assert reloaded.state == CandidateState.WATCHING
    assert reloaded.block_type == BlockType.NONE
    assert reloaded.metadata["sub_status"] == "DATA_INSUFFICIENT"
    assert "candidate_blocked_temp" not in event_types(db, candidate.id)
    db.close()


def test_open_virtual_order_keeps_subscription_for_final_blocked_candidate(tmp_path):
    runtime, db, client, _, _ = build_runtime(tmp_path)
    candidate = save_candidate(db, "111111", CandidateState.BLOCKED, block_type=BlockType.FINAL, can_recover=False)
    plan = db.save_entry_plan(plan_for(candidate.id, candidate.code))
    db.save_virtual_order(
        VirtualOrder(
            candidate_id=candidate.id,
            entry_plan_id=plan.id,
            status=VirtualOrderStatus.SUBMITTED,
            limit_price=10_000,
            submitted_at=NOW.isoformat(),
        )
    )

    runtime.start(NOW)

    assert "111111" in client.registered_codes
    assert "virtual_order" in runtime.subscription_manager.records["111111"].sources
    db.close()


def test_open_virtual_position_keeps_subscription_for_expired_candidate(tmp_path):
    runtime, db, client, _, _ = build_runtime(tmp_path)
    candidate = save_candidate(db, "111111", CandidateState.EXPIRED)
    db.save_virtual_position(
        VirtualPosition(
            candidate_id=candidate.id,
            virtual_order_id=1,
            entry_price=10_000,
            quantity=1,
            opened_at=NOW.isoformat(),
        )
    )

    runtime.start(NOW)

    assert "111111" in client.registered_codes
    assert "virtual_position" in runtime.subscription_manager.records["111111"].sources
    db.close()


def test_runtime_uses_mock_subscription_reconcile_without_live_condition_adapter(tmp_path):
    runtime, db, client, _, _ = build_runtime(tmp_path)
    save_candidate(db, "111111", CandidateState.DETECTED)

    snapshot = runtime.start(NOW)

    assert "001" in client.registered_codes
    assert "101" in client.registered_codes
    assert "005930" in client.registered_codes
    assert "000660" in client.registered_codes
    assert "111111" in client.registered_codes
    assert any(warning.startswith("RECONCILED_SUBSCRIPTIONS") for warning in snapshot.warnings)
    db.close()


def test_runtime_start_succeeds_with_empty_holding_provider(tmp_path):
    runtime, db, client, _, _ = build_runtime(tmp_path, holding_provider=StaticHoldingProvider())

    runtime.start(NOW)

    assert "001" in client.registered_codes
    assert "101" in client.registered_codes
    db.close()


def test_runtime_registers_holding_as_protected_subscription(tmp_path):
    runtime, db, client, _, _ = build_runtime(
        tmp_path,
        holding_provider=StaticHoldingProvider({"A035420"}),
    )

    runtime.start(NOW)

    assert "035420" in client.registered_codes
    assert "holding" in runtime.subscription_manager.records["035420"].sources
    assert runtime.subscription_manager.records["035420"].protected is True
    db.close()


def test_runtime_merges_leader_and_signal_sources_for_semiconductor_leaders(tmp_path):
    runtime, db, client, _, _ = build_runtime(tmp_path)

    runtime.start(NOW)

    record = runtime.subscription_manager.records["005930"]
    assert {"leading_stock", "semiconductor_signal"}.issubset(record.sources)
    assert record.protected is True
    assert list(code for code in client.registered_codes if code == "005930") == ["005930"]
    db.close()


def test_runtime_warns_when_protected_subscriptions_exceed_limit(tmp_path):
    config = StrategyRuntimeConfig(realtime_subscription_limit=1, max_candidates_to_watch=1)
    runtime, db, client, _, _ = build_runtime(tmp_path, config=config)

    snapshot = runtime.start(NOW)

    assert "PROTECTED_SUBSCRIPTION_OVER_LIMIT" in snapshot.warnings
    assert {"001", "101", "005930", "000660"}.issubset(client.registered_codes)
    db.close()


def test_protected_subscriptions_are_kept_when_limit_is_near(tmp_path):
    holdings = {f"{code:06d}" for code in range(100000, 100080)}
    config = StrategyRuntimeConfig(realtime_subscription_limit=85, max_candidates_to_watch=80)
    runtime, db, client, _, _ = build_runtime(
        tmp_path,
        config=config,
        holding_provider=StaticHoldingProvider(holdings),
    )

    snapshot = runtime.start(NOW)

    assert holdings.issubset(client.registered_codes)
    assert {"001", "101", "005930", "000660"}.issubset(client.registered_codes)
    assert snapshot.subscription_active_count >= len(holdings) + 4
    assert any(warning.startswith("PROTECTED_SUBSCRIPTION_NEAR_LIMIT") for warning in snapshot.warnings)
    db.close()


def test_runtime_stop_calls_condition_adapter_stop(tmp_path):
    class FakeConditionAdapter:
        def __init__(self):
            self.start_calls = 0
            self.stop_calls = 0

        def start(self, now):
            self.start_calls += 1
            return ["CONDITION_ADAPTER_STARTED"]

        def stop(self):
            self.stop_calls += 1
            return ["CONDITION_ADAPTER_STOPPED"]

    adapter = FakeConditionAdapter()
    runtime, db, _, _, _ = build_runtime(tmp_path, condition_adapter=adapter)

    start_snapshot = runtime.start(NOW)
    stop_snapshot = runtime.stop()
    second_stop = runtime.stop()

    assert adapter.start_calls == 1
    assert adapter.stop_calls == 2
    assert "CONDITION_ADAPTER_STARTED" in start_snapshot.warnings
    assert "CONDITION_ADAPTER_STOPPED" in stop_snapshot.warnings
    assert "CONDITION_ADAPTER_STOPPED" in second_stop.warnings
    db.close()


def test_runtime_continues_after_one_candidate_failure(tmp_path):
    gate = FakeGatePipeline(fail_batch=True, fail_codes={"999999"})
    runtime, db, _, _, builder = build_runtime(tmp_path, gate_pipeline=gate)
    good = save_candidate(db, "111111", CandidateState.WATCHING)
    save_candidate(db, "999999", CandidateState.WATCHING)
    add_completed_candle(builder, NOW + timedelta(minutes=1), low=9_980)
    runtime.start(NOW)

    snapshot = runtime.cycle(NOW)

    assert any("GATE_PIPELINE_BATCH_FAILED" in warning for warning in snapshot.warnings)
    assert any("GATE_PIPELINE_CANDIDATE_FAILED:999999" in warning for warning in snapshot.warnings)
    assert len(db.list_entry_plans(good.id)) == 1
    db.close()


def test_cycle_order_is_stable_for_phase_2_1(tmp_path, monkeypatch):
    runtime, db, _, _, builder = build_runtime(tmp_path)
    save_candidate(db)
    add_completed_candle(builder, NOW + timedelta(minutes=1), low=9_980)
    calls = []

    wrap(runtime.candidate_collector, "expire_stale", calls, "expire")
    wrap(runtime.subscription_manager, "watch_candidates", calls, "subscriptions")
    wrap(runtime.gate_pipeline, "evaluate", calls, "gates")
    wrap(runtime.entry_plan_builder, "build", calls, "entry")
    wrap(runtime.virtual_order_service, "submit_virtual_order", calls, "submit")
    wrap(runtime.virtual_order_service, "evaluate_fill", calls, "fill")
    wrap(runtime.virtual_position_service, "open_from_filled_order", calls, "open")
    wrap(runtime.virtual_position_service, "update_performance", calls, "performance")
    wrap(runtime.exit_decision_engine, "evaluate", calls, "exit")
    wrap(runtime.trade_review_service, "build_review", calls, "review")
    runtime.start(NOW)
    calls.clear()

    runtime.cycle(NOW)

    assert calls[:10] == [
        "expire",
        "subscriptions",
        "gates",
        "entry",
        "submit",
        "fill",
        "open",
        "performance",
        "exit",
        "review",
    ]
    db.close()


def plan_for(candidate_id, code="111111"):
    return EntryPlan(
        candidate_id=candidate_id,
        entry_type="pullback_limit",
        base_price_source="vwap",
        limit_price=10_000,
        split_plan=[{"leg": 1, "weight_pct": 100, "limit_price": 10_000}],
        order_timeout_sec=180,
        cancel_condition={
            "submittable": True,
            "theme_id": "robot",
            "theme_name": "로봇",
            "strategy_profile": StrategyProfile.KOSDAQ_THEME_PROFILE.value,
            "gate_result_key": f"{candidate_id}:{code}:robot:A",
            "code": code,
        },
        fill_policy=FillPolicy.NORMAL,
        created_at=NOW.isoformat(),
    )


def wrap(obj, method_name, calls, label):
    original = getattr(obj, method_name)

    def wrapped(*args, **kwargs):
        calls.append(label)
        return original(*args, **kwargs)

    setattr(obj, method_name, wrapped)
