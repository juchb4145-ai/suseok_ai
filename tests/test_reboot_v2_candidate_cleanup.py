from storage.db import TradingDatabase
from tools.reboot_v2_candidate_cleanup import analyze_cleanup, apply_cleanup
from trading.strategy.models import Candidate, CandidateState
from trading.theme_engine.models import CanonicalTheme, ThemeMembership, ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository


def test_cleanup_moves_hydrating_to_wait_data_and_attaches_theme(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    repo = ThemeEngineRepository(db)
    repo.upsert_canonical_theme(
        CanonicalTheme(
            theme_id="semis",
            canonical_name="Semiconductors",
            display_name="Semiconductors",
            status=ThemeStatus.ACTIVE,
            confidence=0.9,
            trade_eligible=True,
        )
    )
    repo.upsert_current_membership(
        ThemeMembership(
            theme_id="semis",
            stock_code="005930",
            stock_name="Samsung Electronics",
            membership_score=99.0,
            source_count=2,
            active=True,
            trade_eligible=True,
        )
    )
    saved = db.save_candidate(
        Candidate(
            trade_date="2026-06-17",
            code="005930",
            name="",
            state=CandidateState.HYDRATING,
            detected_at="2026-06-17T09:01:00",
            last_seen_at="2026-06-17T09:01:00",
        )
    )

    dry_run = analyze_cleanup(db, trade_date="2026-06-17", enrich_themes=True)
    report = apply_cleanup(
        db,
        trade_date="2026-06-17",
        enrich_themes=True,
        now="2026-06-17T09:10:00",
    )
    second_report = apply_cleanup(
        db,
        trade_date="2026-06-17",
        enrich_themes=True,
        now="2026-06-17T09:11:00",
    )

    reloaded = db.load_candidate_by_id(saved.id)
    assert dry_run["eligible_count"] == 1
    assert report["applied_count"] == 1
    assert second_report["applied_count"] == 0
    assert reloaded.state == CandidateState.WAIT_DATA
    assert reloaded.theme_ids == ["semis"]
    assert reloaded.metadata["primary_theme_id"] == "semis"
    assert reloaded.metadata["theme_name"] == "Semiconductors"
    assert "WAIT_DATA" in reloaded.metadata["reason_codes"]
    assert "HYDRATION_STALE_CLEANUP" in reloaded.metadata["reason_codes"]
    events = db.list_candidate_events(saved.id)
    assert events[-1].event_type == "candidate_hydration_cleanup"
    assert events[-1].to_state == CandidateState.WAIT_DATA
