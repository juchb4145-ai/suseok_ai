from datetime import datetime, timedelta

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import BrokerConditionEvent, GatewayEvent
from trading.runtime_ports import MarketDataSnapshot
from trading.strategy.candidate_fsm import (
    CandidateBlockingStage,
    CandidateFsmService,
    CandidateReasonCode,
    build_candidate_fsm_summary,
    legacy_to_v2_state,
)
from trading.strategy.candidate_hydrator import CandidateHydrator
from trading.strategy.candidate_ingestion import CandidateIngestionService
from trading.strategy.market_data import MarketDataStore
from trading.strategy.models import CandidateState


def test_condition_include_creates_only_discovered_without_order_side_effects(tmp_path) -> None:
    db = TradingDatabase(str(tmp_path / "fsm.db"))
    service = CandidateIngestionService(db)

    result = service.handle_condition_event(
        BrokerConditionEvent(
            condition_name="theme_alive",
            code="A005930",
            condition_index=3,
            event_type="include",
            timestamp="2026-06-17T09:01:00",
        ),
        trade_date="2026-06-17",
    )

    candidate = result.candidate
    fsm = candidate.metadata["candidate_fsm"]
    assert candidate.state == CandidateState.DETECTED
    assert fsm["v2_state"] == "DISCOVERED"
    assert fsm["blocking_stage"] == "NONE"
    assert db.conn.execute("SELECT COUNT(*) AS count FROM entry_plans").fetchone()["count"] == 0
    assert db.list_runtime_order_intents(limit=10) == []
    transitions = db.list_candidate_state_transitions(candidate_id=candidate.id)
    assert transitions[0]["to_state"] == "DISCOVERED"
    assert transitions[0]["reason_code"] == "CONDITION_INCLUDE"
    db.close()


def test_condition_include_cannot_promote_to_setup_or_timing_ready(tmp_path) -> None:
    db = TradingDatabase(str(tmp_path / "fsm.db"))
    candidate = CandidateIngestionService(db).handle_condition_event(
        BrokerConditionEvent(condition_name="entry", code="005930", condition_index=1, timestamp="2026-06-17T09:01:00"),
        trade_date="2026-06-17",
    ).candidate

    assert candidate.metadata["candidate_fsm"]["v2_state"] == "DISCOVERED"
    assert candidate.metadata["candidate_fsm"]["v2_state"] not in {"SETUP_READY", "TIMING_READY"}
    db.close()


def test_hydration_requested_records_discovered_to_hydrating(tmp_path) -> None:
    db = TradingDatabase(str(tmp_path / "fsm.db"))
    gateway = GatewayStateStore()
    candidate = _candidate(db)

    CandidateHydrator(db, gateway).enqueue_candidate(candidate)
    reloaded = db.load_candidate("2026-06-17", "005930")

    assert reloaded.state == CandidateState.HYDRATING
    fsm = reloaded.metadata["candidate_fsm"]
    assert fsm["v2_state"] == "HYDRATING"
    assert fsm["blocking_stage"] == "DATA"
    assert fsm["primary_reason_code"] == "HYDRATION_REQUESTED"
    transitions = db.list_candidate_state_transitions(candidate_id=reloaded.id)
    assert transitions[0]["to_state"] == "HYDRATING"
    db.close()


def test_hydration_result_without_realtime_tick_keeps_data_blocking(tmp_path) -> None:
    db = TradingDatabase(str(tmp_path / "fsm.db"))
    gateway = GatewayStateStore()
    market_data = MarketDataStore()
    candidate = _candidate(db, theme_id="semis")
    hydrator = CandidateHydrator(db, gateway, market_data=market_data)
    enqueue = hydrator.enqueue_candidate(candidate)

    hydrator.handle_event(
        GatewayEvent(
            type="command_ack",
            event_id="evt-hydration",
            command_id=enqueue.command_id,
            payload={
                "purpose": "candidate_hydration",
                "command_id": enqueue.command_id,
                "trade_date": "2026-06-17",
                "code": "005930",
                "raw": {"tr_rows": [{"종목명": "Samsung", "현재가": "70000", "등락율": "1.2", "거래량": "1000"}]},
            },
        )
    )

    reloaded = db.load_candidate("2026-06-17", "005930")
    fsm = reloaded.metadata["candidate_fsm"]
    assert fsm["v2_state"] in {"HYDRATING", "WATCHING"}
    assert fsm["blocking_stage"] == "DATA"
    assert fsm["primary_reason_code"] in {"TR_BACKFILL_PRICE_ONLY", "LATEST_TICK_MISSING"}
    assert fsm["v2_state"] not in {"SETUP_READY", "TIMING_READY"}
    db.close()


def test_fresh_realtime_tick_promotes_to_watching_only(tmp_path) -> None:
    db = TradingDatabase(str(tmp_path / "fsm.db"))
    candidate = _candidate(db)
    fsm = CandidateFsmService(db)
    snapshot = MarketDataSnapshot(
        code="005930",
        price=70000,
        is_fresh=True,
        freshness_status="FRESH",
        price_source="REALTIME",
        source_event_id="evt-tick",
    )

    fsm.on_realtime_tick(candidate, snapshot)
    db.save_candidate(candidate)

    reloaded = db.load_candidate("2026-06-17", "005930")
    assert reloaded.metadata["candidate_fsm"]["v2_state"] == "WATCHING"
    assert reloaded.metadata["candidate_fsm"]["v2_state"] not in {"SETUP_READY", "TIMING_READY"}
    db.close()


def test_stale_tick_is_data_blocking_not_state_change(tmp_path) -> None:
    db = TradingDatabase(str(tmp_path / "fsm.db"))
    candidate = _candidate(db)
    previous_state = candidate.state
    fsm = CandidateFsmService(db)
    snapshot = MarketDataSnapshot(
        code="005930",
        price=70000,
        is_fresh=False,
        freshness_status="STALE_TICK",
        price_source="REALTIME",
        source_event_id="evt-stale",
    )

    fsm.on_realtime_tick(candidate, snapshot)
    db.save_candidate(candidate)

    reloaded = db.load_candidate("2026-06-17", "005930")
    assert reloaded.state == previous_state
    assert reloaded.metadata["candidate_fsm"]["blocking_stage"] == "DATA"
    assert reloaded.metadata["candidate_fsm"]["primary_reason_code"] == "LATEST_TICK_STALE"
    db.close()


def test_wait_data_legacy_state_maps_to_blocking_stage_not_v2_state(tmp_path) -> None:
    db = TradingDatabase(str(tmp_path / "fsm.db"))
    candidate = _candidate(db)
    candidate.state = CandidateState.WAIT_DATA
    db.save_candidate(candidate)

    assert legacy_to_v2_state(candidate.state).value == "WATCHING"
    transition = CandidateFsmService(db).apply_blocking_reason(
        candidate,
        CandidateBlockingStage.DATA,
        CandidateReasonCode.LATEST_TICK_MISSING,
    )

    assert transition.to_state == "WATCHING" or transition.to_state.value == "WATCHING"
    assert candidate.metadata["candidate_fsm"]["blocking_stage"] == "DATA"
    db.close()


def test_fsm_apply_reconciles_hydration_complete_wait_data_to_watching(tmp_path) -> None:
    db = TradingDatabase(str(tmp_path / "fsm.db"))
    candidate = _candidate(db)
    candidate.state = CandidateState.WAIT_DATA
    candidate.metadata["candidate_hydration"] = _complete_hydration()
    candidate = db.save_candidate(candidate)

    CandidateFsmService(db).apply_blocking_reason(
        candidate,
        CandidateBlockingStage.DATA,
        CandidateReasonCode.LATEST_TICK_MISSING,
    )

    reloaded = db.load_candidate("2026-06-17", "005930")
    assert reloaded.state == CandidateState.WATCHING
    assert reloaded.metadata["candidate_fsm"]["v2_state"] == "WATCHING"
    db.close()


def test_risk_block_does_not_mutate_candidate_to_blocked(tmp_path) -> None:
    db = TradingDatabase(str(tmp_path / "fsm.db"))
    candidate = _candidate(db)
    candidate.state = CandidateState.WATCHING
    fsm = CandidateFsmService(db)

    fsm.apply_blocking_reason(candidate, CandidateBlockingStage.RISK, CandidateReasonCode.ORDER_RISK_BLOCKED)
    db.save_candidate(candidate)

    reloaded = db.load_candidate("2026-06-17", "005930")
    assert reloaded.state == CandidateState.WATCHING
    assert reloaded.metadata["candidate_fsm"]["blocking_stage"] == "RISK"
    assert reloaded.metadata["candidate_fsm"]["primary_reason_code"] == "ORDER_RISK_BLOCKED"
    db.close()


def test_candidate_fsm_summary_counts_states_blocks_and_transitions(tmp_path) -> None:
    db = TradingDatabase(str(tmp_path / "fsm.db"))
    candidate = _candidate(db)
    fsm = CandidateFsmService(db)
    fsm.apply_blocking_reason(candidate, CandidateBlockingStage.DATA, CandidateReasonCode.LATEST_TICK_MISSING)
    db.save_candidate(candidate)

    summary = build_candidate_fsm_summary(db, trade_date="2026-06-17")

    assert summary["status"] == "OK"
    assert summary["state_counts"]["DISCOVERED"] == 1
    assert summary["blocking_stage_counts"]["DATA"] == 1
    assert summary["top_reason_codes"][0]["reason"] == "LATEST_TICK_MISSING"
    assert summary["transition_count"] >= 2
    db.close()


def _candidate(db: TradingDatabase, *, theme_id: str = ""):
    return CandidateIngestionService(db).handle_condition_event(
        BrokerConditionEvent(
            condition_name="theme_alive",
            code="005930",
            condition_index=1,
            timestamp="2026-06-17T09:01:00",
        ),
        trade_date="2026-06-17",
    ).candidate


def _complete_hydration() -> dict:
    return {
        "status": "ACKED",
        "basic_hydration_complete": True,
        "basic_hydration_completed_at": "2026-06-17T09:02:00",
        "parsed": {
            "code": "005930",
            "current_price": 70000,
            "change_rate": 1.2,
            "prev_close": 69170,
        },
    }
