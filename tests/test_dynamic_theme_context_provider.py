from storage.db import TradingDatabase
from tests.theme_naver_helpers import repo_with_naver_fixture
from trading.strategy.readiness import _active_candidates, _theme_active_stock_count, build_readiness_report
from trading.strategy.models import (
    BlockType,
    Candidate,
    CandidateState,
    VirtualOrder,
    VirtualOrderStatus,
    VirtualPosition,
)
from trading.theme_engine.context_provider import DynamicThemeContextProvider
from trading.theme_engine.models import CanonicalTheme, ThemeMembership, ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.universe import ThemeUniverseBuilder, ThemeUniverseConfig


def test_context_provider_reports_not_ready_without_membership(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    provider = DynamicThemeContextProvider(ThemeEngineRepository(db))

    enriched = provider.enrich_candidate(Candidate(code="000001"))

    assert enriched.metadata["reason_code"] == "THEME_CONTEXT_NOT_READY"
    db.close()


def test_context_provider_enriches_candidate_from_current_membership(tmp_path):
    db, repo = repo_with_naver_fixture(tmp_path)
    provider = DynamicThemeContextProvider(repo)

    enriched = provider.enrich_candidate(Candidate(code="000001"))
    missing = provider.enrich_candidate(Candidate(code="009999"))

    assert "furiosa_ai" in enriched.theme_ids
    assert enriched.metadata["theme_context_status"] == "ready"
    assert missing.metadata["reason_code"] == "NO_ACTIVE_THEME"
    assert provider.get_stock_theme_state("000001").primary_theme_id == "furiosa_ai"
    db.close()


def test_readiness_theme_coverage_uses_bulk_membership_lookup(tmp_path, monkeypatch):
    db, _repo = repo_with_naver_fixture(tmp_path)
    db.save_candidate(Candidate(trade_date="2026-05-30", code="000001"))
    db.save_candidate(Candidate(trade_date="2026-05-30", code="009999"))

    def fail_latest_activity(*args, **kwargs):
        raise AssertionError("readiness should not scan latest activity per candidate")

    monkeypatch.setattr(ThemeEngineRepository, "latest_activity_snapshots", fail_latest_activity)
    report = build_readiness_report(db, trade_date="2026-05-30")

    assert report.active_candidates_count == 2
    assert report.active_candidates_with_active_theme == 1
    assert report.active_candidates_without_active_theme == 1
    db.close()


def test_readiness_active_candidates_use_bulk_open_activity_lookup(tmp_path, monkeypatch):
    db = TradingDatabase(str(tmp_path / "readiness.sqlite3"))
    trade_date = "2026-05-30"
    active = db.save_candidate(Candidate(trade_date=trade_date, code="000001", state=CandidateState.WATCHING))
    recoverable = db.save_candidate(
        Candidate(
            trade_date=trade_date,
            code="000002",
            state=CandidateState.BLOCKED,
            block_type=BlockType.TEMPORARY,
            can_recover=True,
        )
    )
    submitted = db.save_candidate(Candidate(trade_date=trade_date, code="000003", state=CandidateState.EXPIRED))
    positioned = db.save_candidate(Candidate(trade_date=trade_date, code="000004", state=CandidateState.REMOVED))
    db.save_candidate(Candidate(trade_date=trade_date, code="000005", state=CandidateState.EXPIRED))
    db.save_candidate(Candidate(trade_date="2026-05-31", code="000006", state=CandidateState.WATCHING))
    db.save_virtual_order(VirtualOrder(candidate_id=submitted.id, status=VirtualOrderStatus.SUBMITTED))
    db.save_virtual_position(VirtualPosition(candidate_id=positioned.id, entry_price=10_000, quantity=1))

    def fail_per_candidate_activity(*args, **kwargs):
        raise AssertionError("readiness should not query open activity per candidate")

    monkeypatch.setattr(db, "load_open_virtual_position", fail_per_candidate_activity)
    monkeypatch.setattr(db, "list_virtual_orders", fail_per_candidate_activity)

    candidates = _active_candidates(db, trade_date)

    assert [candidate.id for candidate in candidates] == [active.id, recoverable.id, submitted.id, positioned.id]
    db.close()


def test_readiness_theme_active_stock_count_uses_capped_sql_count(tmp_path):
    db = TradingDatabase(str(tmp_path / "theme-count.sqlite3"))
    repo = ThemeEngineRepository(db)
    repo.upsert_canonical_theme(CanonicalTheme("active_theme", "A", "A", status=ThemeStatus.ACTIVE, trade_eligible=True))
    repo.upsert_canonical_theme(CanonicalTheme("watch_theme", "W", "W", status=ThemeStatus.WATCH, trade_eligible=True))
    repo.upsert_canonical_theme(CanonicalTheme("candidate_theme", "C", "C", status=ThemeStatus.CANDIDATE))
    repo.upsert_current_membership(ThemeMembership("active_theme", "000001", membership_score=0.9, source_count=3, active=True, trade_eligible=True))
    repo.upsert_current_membership(ThemeMembership("watch_theme", "000002", membership_score=0.8, source_count=2, active=True, trade_eligible=True))
    repo.upsert_current_membership(ThemeMembership("active_theme", "000002", membership_score=0.7, source_count=1, active=True, trade_eligible=True))
    repo.upsert_current_membership(ThemeMembership("active_theme", "000003", membership_score=0.4, source_count=1, active=True, trade_eligible=True))
    repo.upsert_current_membership(ThemeMembership("candidate_theme", "000004", membership_score=0.9, active=True, trade_eligible=True))
    builder = ThemeUniverseBuilder(repo, ThemeUniverseConfig(max_size=1, min_membership_score=0.55))

    assert _theme_active_stock_count(builder) == len(builder.build_active_universe()) == 1
    db.close()


def test_context_provider_caches_activity_lookup_per_refresh(tmp_path, monkeypatch):
    db, repo = repo_with_naver_fixture(tmp_path)
    calls = 0
    original = ThemeEngineRepository.latest_activity_snapshots

    def count_latest_activity(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ThemeEngineRepository, "latest_activity_snapshots", count_latest_activity)
    provider = DynamicThemeContextProvider(repo)

    assert provider.themes_for_code("000001")
    assert provider.themes_for_code("000001")
    assert provider.get_stock_theme_state("000001").ready is True

    assert calls == 1
    provider.refresh_cache()
    assert calls == 2
    db.close()
