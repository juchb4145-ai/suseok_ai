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


def test_setup_router_v3_summary_ignores_newer_legacy_run(tmp_path, monkeypatch):
    db_path = tmp_path / "setup-api-version.db"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    db = TradingDatabase(str(db_path))
    db.save_setup_observations([_observation("VALID_OBSERVE", "MATCHED", "fp-current")])
    db.save_setup_router_run(
        {
            "trade_date": TRADE_DATE,
            "calculated_at": "2026-06-22T09:05:00",
            "schema_version": "setup_router_v3.observe.v5.2",
            "feature_schema_version": "setup_router_v3.features.v4.2",
            "router_version": "setup_router_v3.5.2",
            "state_version": "setup_router_v3.state.v3.2",
            "output_mode": "OBSERVE",
            "enabled": True,
            "observe_only": True,
            "candidate_count": 1,
            "evaluated_count": 1,
            "observation_count": 1,
            "valid_observe_count": 1,
            "status": "OK",
        }
    )
    db.save_setup_router_run(
        {
            "trade_date": TRADE_DATE,
            "calculated_at": "2026-06-22T09:30:00",
            "schema_version": "setup_router_v3.observe.v5",
            "feature_schema_version": "setup_router_v3.features.v4",
            "router_version": "setup_router_v3.5",
            "state_version": "setup_router_v3.state.v3.1",
            "output_mode": "OBSERVE",
            "enabled": True,
            "observe_only": True,
            "candidate_count": 999,
            "evaluated_count": 999,
            "observation_count": 999,
            "valid_observe_count": 999,
            "status": "LEGACY_STALE",
        }
    )
    db.conn.close()

    import trading_app.api as api

    api = importlib.reload(api)
    summary = api.setup_router_v3_summary(trade_date=TRADE_DATE)

    assert summary["router_version"] == "setup_router_v3.5.2"
    assert summary["status"] == "OK"
    assert summary["latest_count"] == 1
    assert summary["run"]["router_version"] == "setup_router_v3.5.2"
    assert summary["run"]["candidate_count"] == 1
