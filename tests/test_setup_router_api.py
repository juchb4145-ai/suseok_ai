import importlib

from storage.db import TradingDatabase

from tests.test_setup_router_storage import _observation


TRADE_DATE = "2026-06-22"


def test_setup_router_v3_api_is_read_only_and_filterable(tmp_path, monkeypatch):
    db_path = tmp_path / "setup-api.db"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    db = TradingDatabase(str(db_path))
    db.save_setup_observations([_observation("VALID_OBSERVE", "MATCHED", "fp-api")])
    db.conn.close()

    import trading_app.api as api

    api = importlib.reload(api)
    summary = api.setup_router_v3_summary(trade_date=TRADE_DATE)
    latest = api.setup_router_v3_latest(trade_date=TRADE_DATE)
    transitions = api.setup_router_v3_transitions(trade_date=TRADE_DATE)
    candidate = api.setup_router_v3_candidate("000001", trade_date=TRADE_DATE)

    assert summary["observe_only"] is True
    assert summary["safety"]["order_intent_allowed"] is False
    assert latest["items"][0]["router_status"] == "VALID_OBSERVE"
    assert transitions["transition_count"] == 1
    assert candidate["observations"][0]["setup_type"] == "VWAP_RECLAIM"
