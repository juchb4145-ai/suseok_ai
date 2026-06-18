from types import SimpleNamespace

from storage.db import TradingDatabase
from trading.broker.models import BrokerConditionEvent
from trading.strategy.candidate_ingestion import (
    CandidateIngestionService,
    CandidateSourceEventType,
    source_events_from_opening_burst_result,
)
from trading.strategy.models import CandidateState
from trading.theme_engine.models import CanonicalTheme, ThemeMembership, ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository


def test_condition_include_becomes_candidate_source_event(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
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
    assert candidate is not None
    assert candidate.code == "005930"
    assert candidate.state == CandidateState.DETECTED
    assert db.list_candidate_source_events(trade_date="2026-06-17")[0]["source_type"] == "condition_search"
    assert db.conn.execute("SELECT COUNT(*) AS count FROM entry_plans").fetchone()["count"] == 0
    assert db.list_runtime_order_intents(limit=10) == []


def test_condition_include_attaches_current_theme_context_without_order_intent(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    repo = ThemeEngineRepository(db)
    _theme(repo, "semis", "Semiconductors", ["005930"])
    service = CandidateIngestionService(db, theme_repository=repo)

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
    assert candidate is not None
    assert candidate.state == CandidateState.DETECTED
    assert candidate.theme_ids == ["semis"]
    assert candidate.metadata["candidate_ingestion"]["primary_theme_id"] == "semis"
    assert candidate.metadata["candidate_ingestion"]["theme_name"] == "Semiconductors"
    source_event = db.list_candidate_source_events(trade_date="2026-06-17")[0]
    assert source_event["theme_id"] == "semis"
    assert source_event["theme_name"] == "Semiconductors"
    assert source_event["status"] == "INGESTED"
    assert db.conn.execute("SELECT COUNT(*) AS count FROM entry_plans").fetchone()["count"] == 0
    assert db.list_runtime_order_intents(limit=10) == []


def test_condition_and_opening_burst_merge_into_one_active_candidate(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    service = CandidateIngestionService(db)
    service.handle_condition_event(
        BrokerConditionEvent(
            condition_name="theme_alive",
            code="005930",
            condition_index=1,
            event_type="include",
            timestamp="2026-06-17T09:01:00",
        ),
        trade_date="2026-06-17",
    )

    opening_result = _opening_result("005930")
    results = service.ingest_opening_burst_result(opening_result, trade_date="2026-06-17")

    assert len(results) == 1
    candidates = db.list_candidates(trade_date="2026-06-17")
    assert len(candidates) == 1
    candidate = candidates[0]
    metadata = candidate.metadata["candidate_ingestion"]
    assert metadata["primary_source"] == CandidateSourceEventType.OPENING_BURST.value
    assert metadata["primary_theme_id"] == "semis"
    assert metadata["stock_role"] == "LEADER"
    assert set(metadata["active_source_types"]) == {"condition_search", "opening_burst"}


def test_opening_burst_selected_maps_to_candidate_source_event():
    events = source_events_from_opening_burst_result(_opening_result("000660"), trade_date="2026-06-17")

    assert len(events) == 1
    assert events[0].source_type == "opening_burst"
    assert events[0].code == "000660"
    assert events[0].theme_id == "semis"
    assert events[0].stock_role == "LEADER"


def test_condition_remove_records_source_removal_without_delete(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    service = CandidateIngestionService(db)
    service.handle_condition_event(
        BrokerConditionEvent(
            condition_name="theme_alive",
            code="005930",
            condition_index=1,
            event_type="include",
            timestamp="2026-06-17T09:01:00",
        ),
        trade_date="2026-06-17",
    )

    result = service.handle_condition_event(
        BrokerConditionEvent(
            condition_name="theme_alive",
            code="005930",
            condition_index=1,
            event_type="remove",
            timestamp="2026-06-17T09:02:00",
        ),
        trade_date="2026-06-17",
    )

    assert result.removed is True
    assert db.load_candidate("2026-06-17", "005930").state == CandidateState.REMOVED
    assert len(db.list_candidate_source_events(trade_date="2026-06-17")) == 2


def _opening_result(code: str):
    stock = SimpleNamespace(
        stock_code=code,
        stock_name="Test Stock",
        role="LEADER",
        rank_in_theme=1,
        seed_rank=2,
        stock_burst_score=88.0,
        reason_codes=("OPENING_LEADER_SCORE_TOP",),
    )
    theme_snapshot = SimpleNamespace(stocks=(stock,))
    rank = SimpleNamespace(theme_id="semis", theme_name="Semiconductors", snapshot=theme_snapshot)
    return SimpleNamespace(
        calculated_at="2026-06-17T09:05:00",
        selected=(stock,),
        selected_symbols=(code,),
        ranked_themes=(rank,),
    )


def _theme(repo: ThemeEngineRepository, theme_id: str, name: str, codes: list[str]) -> None:
    repo.upsert_canonical_theme(
        CanonicalTheme(
            theme_id=theme_id,
            canonical_name=name,
            display_name=name,
            status=ThemeStatus.ACTIVE,
            confidence=0.9,
            trade_eligible=True,
        )
    )
    for index, code in enumerate(codes, 1):
        repo.upsert_current_membership(
            ThemeMembership(
                theme_id=theme_id,
                stock_code=code,
                stock_name=f"Stock {code}",
                membership_score=max(1.0, 100.0 - index),
                source_count=1,
                active=True,
                trade_eligible=True,
            )
        )
