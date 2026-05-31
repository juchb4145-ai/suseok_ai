from trading.theme_engine.normalizer import normalize_stock_code, normalize_theme_name, suggest_theme_id


def test_normalize_stock_code_handles_kiwoom_and_excel_forms():
    assert normalize_stock_code("A005930") == "005930"
    assert normalize_stock_code("5930") == "005930"
    assert normalize_stock_code("") == ""
    assert normalize_stock_code("not-a-code") == ""


def test_normalize_theme_name_keeps_alias_variants_stable():
    assert normalize_theme_name("퓨리오사AI") == normalize_theme_name("퓨리오사 AI")
    assert normalize_theme_name("AI 반도체") == normalize_theme_name("에이아이-반도체")
    assert normalize_theme_name("퓨리오사/창투사") == "퓨리오사창투사"
    assert normalize_theme_name("FuriosaAI") == "furiosaai"


def test_suggest_theme_id_has_furiosa_special_case():
    assert suggest_theme_id("퓨리오사AI") == "furiosa_ai"
    assert suggest_theme_id("FuriosaAI") == "furiosa_ai"
