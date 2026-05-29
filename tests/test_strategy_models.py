import json

from storage.db import TradingDatabase
from trading.strategy.models import (
    BlockType,
    Candidate,
    CandidateEvent,
    CandidateSourceType,
    CandidateState,
    EntryPlan,
    ExitDecision,
    FillPolicy,
    GateDecision,
    IndicatorSnapshot,
    StrategyProfile,
    TradeReview,
    VirtualOrder,
    VirtualOrderStatus,
    VirtualPosition,
)


def test_strategy_dataclasses_round_trip_to_json():
    objects = [
        Candidate(
            id=1,
            trade_date="2026-05-29",
            code="005930",
            name="삼성전자",
            market="KOSPI",
            strategy_profile=StrategyProfile.KOSPI_LEADER_PROFILE,
            sources=[CandidateSourceType.CONDITION, CandidateSourceType.LEADING_STOCK],
            state=CandidateState.WATCHING,
            theme_ids=["semiconductor"],
            condition_names=["대형주"],
            metadata={"score": 72.5},
        ),
        CandidateEvent(
            candidate_id=1,
            event_type="state_changed",
            from_state=CandidateState.DETECTED,
            to_state=CandidateState.WATCHING,
            source=CandidateSourceType.CONDITION,
            payload={"condition_name": "대형주"},
        ),
        IndicatorSnapshot(candidate_id=1, code="005930", price=80_000, vwap=79_500.0, pullback_pct=-1.5),
        GateDecision(
            candidate_id=1,
            gate_name="MarketIndexGate",
            passed=True,
            score=80.0,
            grade="A",
            block_type=BlockType.NONE,
            reason_codes=["INDEX_OK"],
        ),
        EntryPlan(
            candidate_id=1,
            entry_type="vwap_support",
            base_price_source="VWAP",
            limit_price=79_500,
            split_plan=[{"weight": 100}],
            fill_policy=FillPolicy.NORMAL,
        ),
        VirtualOrder(
            candidate_id=1,
            entry_plan_id=1,
            status=VirtualOrderStatus.PLANNED,
            limit_price=79_500,
            fill_policy=FillPolicy.CONSERVATIVE,
        ),
        VirtualPosition(candidate_id=1, virtual_order_id=1, entry_price=79_500, quantity=10),
        ExitDecision(virtual_position_id=1, decision_type="take_profit", trigger_price=82_000, filled=False),
        TradeReview(candidate_id=1, virtual_position_id=1, final_status="observed", max_return_5m=1.2),
    ]

    for obj in objects:
        encoded = obj.to_dict()
        decoded = json.loads(json.dumps(encoded, ensure_ascii=False))
        restored = type(obj).from_dict(decoded)
        assert restored.to_dict() == encoded


def test_strategy_migration_tables_are_created(tmp_path):
    db = TradingDatabase(str(tmp_path / "strategy.sqlite3"))
    tables = {
        row[0]
        for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }

    assert {
        "condition_profiles",
        "candidates",
        "candidate_events",
        "indicator_snapshots",
        "gate_decisions",
        "entry_plans",
        "virtual_orders",
        "virtual_positions",
        "exit_decisions",
        "trade_reviews",
    }.issubset(tables)
    db.close()
