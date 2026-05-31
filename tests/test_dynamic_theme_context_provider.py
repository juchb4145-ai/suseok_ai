from storage.db import TradingDatabase
from tests.theme_naver_helpers import repo_with_naver_fixture
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
