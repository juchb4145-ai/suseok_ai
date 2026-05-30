from storage.db import TradingDatabase
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.resolver import ThemeCanonicalResolver


def test_external_theme_not_in_kiwoom_is_created(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    resolver = ThemeCanonicalResolver(ThemeEngineRepository(db))

    theme = resolver.match_or_create_theme("external_fixture", "퓨리오사AI")

    assert theme.theme_id == "furiosa_ai"
    assert theme.display_name == "퓨리오사AI"
    db.close()


def test_aliases_resolve_to_same_canonical_theme(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    repo = ThemeEngineRepository(db)
    resolver = ThemeCanonicalResolver(repo)

    theme = resolver.match_or_create_theme("themelab_fixture", "퓨리오사AI")
    resolver.add_alias(theme.theme_id, "FuriosaAI")
    resolver.add_alias(theme.theme_id, "퓨리오사/창투사")

    matched = resolver.match_or_create_theme("infostock_fixture", "퓨리오사AI/창투사")

    assert matched.theme_id == theme.theme_id
    assert resolver.resolve_alias("FuriosaAI") == theme.theme_id
    assert resolver.resolve_alias("퓨리오사/창투사") == theme.theme_id
    db.close()
