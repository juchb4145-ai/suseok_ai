from pathlib import Path

from storage.db import TradingDatabase
from trading.strategy.models import Candidate
from trading.theme_engine.context_provider import DynamicThemeContextProvider
from trading.theme_engine.evidence import ThemeEvidenceService
from trading.theme_engine.membership import ThemeMembershipBuilder
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.resolver import ThemeCanonicalResolver
from trading.theme_engine.sources.fixture import FixtureThemeSource


FIXTURE = Path("tests/fixtures/theme_engine/furiosa_ai.json")


def test_context_provider_reports_not_ready_without_membership(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    provider = DynamicThemeContextProvider(ThemeEngineRepository(db))

    enriched = provider.enrich_candidate(Candidate(code="000001"))

    assert enriched.metadata["reason_code"] == "THEME_CONTEXT_NOT_READY"
    db.close()


def test_context_provider_enriches_candidate_from_current_membership(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    repo = ThemeEngineRepository(db)
    ThemeEvidenceService(repo, ThemeCanonicalResolver(repo)).sync_source(FixtureThemeSource(FIXTURE))
    ThemeMembershipBuilder(repo).build_all_current_memberships()
    provider = DynamicThemeContextProvider(repo)

    enriched = provider.enrich_candidate(Candidate(code="000001"))
    missing = provider.enrich_candidate(Candidate(code="009999"))

    assert "furiosa_ai" in enriched.theme_ids
    assert enriched.metadata["theme_context_status"] == "ready"
    assert missing.metadata["reason_code"] == "NO_ACTIVE_THEME"
    assert provider.get_stock_theme_state("000001").primary_theme_id == "furiosa_ai"
    db.close()
