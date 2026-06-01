from storage.db import TradingDatabase
from tests.theme_naver_helpers import repo_with_naver_fixture
from trading.strategy.readiness import build_readiness_report
from trading.strategy.models import Candidate
from trading.theme_engine.context_provider import DynamicThemeContextProvider
from trading.theme_engine.repository import ThemeEngineRepository


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
