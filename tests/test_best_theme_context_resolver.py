from trading.theme_engine.context_resolver import BestThemeContextResolver


def test_best_theme_context_resolver_selects_stronger_theme_for_multi_theme_stock():
    board = {
        "top_themes": [
            {"theme_id": "ai", "theme_state": "WATCH_THEME", "theme_score": 50, "leadership_score": 40, "persistence_count": 1},
            {"theme_id": "battery", "theme_state": "LEADING_THEME", "theme_score": 70, "leadership_score": 80, "persistence_count": 3},
        ],
        "stocks": [
            {"code": "000001", "theme_id": "ai", "trade_role": "LEADER_CONFIRMED", "role_score": 95},
            {"code": "000001", "theme_id": "battery", "trade_role": "CO_LEADER_CONFIRMED", "role_score": 88},
        ],
    }

    result = BestThemeContextResolver().resolve("000001", theme_board=board, previous_selected_theme_id="ai")

    assert result.selected_theme_id == "battery"
    assert result.alternative_theme_ids == ("ai",)
    assert result.theme_selection_changed is True
