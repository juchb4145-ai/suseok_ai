import importlib

from storage.db import TradingDatabase


TRADE_DATE = "2026-06-19"


def test_theme_rotation_latest_and_history_api_are_read_only(tmp_path, monkeypatch):
    db_path = tmp_path / "theme-rotation-api.db"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    db = TradingDatabase(str(db_path))
    db.save_theme_board_snapshot(
        {
            "trade_date": TRADE_DATE,
            "calculated_at": "2026-06-19T09:20:00",
            "board_status": "OBSERVE",
            "top_themes": [
                {
                    "theme_id": "ai",
                    "theme_name": "AI",
                    "theme_status": "LEADING_THEME",
                    "state_leadership_consistent": False,
                    "state_leadership_mismatch_code": "LEADING_WITH_LOSING_OR_ROTATED",
                }
            ],
            "stocks": [],
        }
    )
    db.save_theme_leadership_latest(
        {
            "trade_date": TRADE_DATE,
            "theme_id": "ai",
            "theme_name": "AI",
            "leadership_status": "INCUMBENT",
            "leadership_score": 88.0,
            "current_rank": 1,
        }
    )
    db.save_theme_leadership_transition(
        {
            "trade_date": TRADE_DATE,
            "theme_id": "ai",
            "previous_status": "CHALLENGER",
            "current_status": "INCUMBENT",
            "detected_at": "2026-06-19T09:20:00",
        }
    )
    db.save_theme_expansion_lease(
        {
            "code": "000001",
            "theme_id": "ai",
            "source": "reboot_v2_theme_expansion",
            "status": "ACTIVE",
            "selected_at": "2026-06-19T09:20:00",
            "first_fresh_tick_at": "",
        },
        trade_date=TRADE_DATE,
    )
    db.save_strategy_context_snapshot(
        {
            "trade_date": TRADE_DATE,
            "code": "000001",
            "candidate_id": 1,
            "context_id": "ctx-1",
            "calculated_at": "2026-06-19T09:20:00",
            "selected_theme_id": "ai",
            "theme": {"theme_id": "ai"},
        }
    )
    db.save_strategy_context_snapshot(
        {
            "trade_date": TRADE_DATE,
            "code": "000001",
            "candidate_id": 1,
            "context_id": "ctx-2",
            "calculated_at": "2026-06-19T09:21:00",
            "selected_theme_id": "robot",
            "theme": {"theme_id": "robot"},
        }
    )
    db.conn.close()

    import trading_app.api as api

    api = importlib.reload(api)
    latest = api.theme_rotation_latest(trade_date=TRADE_DATE)
    transitions = api.theme_rotation_transitions(trade_date=TRADE_DATE)
    leases = api.theme_rotation_leases(trade_date=TRADE_DATE)
    changes = api.theme_rotation_best_theme_changes(trade_date=TRADE_DATE)

    assert latest["current_incumbent_theme_id"] == "ai"
    assert latest["transition_count"] == 1
    assert latest["mismatch_count"] == 1
    assert latest["active_expansion_lease_count"] == 1
    assert transitions["transition_count"] == 1
    assert leases["lease_count"] == 1
    assert changes["change_count"] == 1
    assert latest["order_intent_allowed"] is False
    assert latest["live_order_allowed"] is False
