import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading_app.exit_policy_validation import (
    COMPARISON_SHADOW_BETTER,
    EXIT_INSUFFICIENT_DATA,
    EXIT_PARTIAL_TAKE_PROFIT,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    EXIT_TIME,
    EXIT_TRAILING_STOP,
    ExitPolicyScenario,
    ExitPolicyShadowSimulator,
    ExitPolicyValidationAnalyzer,
    PricePoint,
    _price_path_after_entry,
)


def _db(tmp_path: Path) -> TradingDatabase:
    return TradingDatabase(str(tmp_path / "trader.sqlite3"))


def _save_decision(db: TradingDatabase, **updates):
    payload = {
        "decision_id": "canary-exit-1",
        "trade_date": "2026-06-17",
        "code": "005930",
        "candidate_id": 1,
        "candidate_instance_id": "ci-exit-1",
        "hybrid_status": "READY",
        "hybrid_score": 82.5,
        "theme_name": "AI",
        "eligible": True,
        "status": "SUBMITTED",
        "limit_price": 10000,
        "quantity": 10,
        "order_intent_id": "live-entry-exit-1",
        "gateway_command_id": "cmd-entry-exit-1",
        "created_at": "2026-06-17T09:00:00+09:00",
    }
    payload.update(updates)
    return db.save_live_sim_canary_decision(payload)


def _save_order(db: TradingDatabase, **updates):
    payload = {
        "order_intent_id": "live-entry-exit-1",
        "command_id": "cmd-entry-exit-1",
        "candidate_id": 1,
        "candidate_instance_id": "ci-exit-1",
        "trade_date": "2026-06-17",
        "code": "005930",
        "name": "Samsung",
        "account_id_masked": "12****90",
        "side": "buy",
        "requested_qty": 10,
        "requested_price": 10000,
        "submitted_qty": 10,
        "submitted_price": 10000,
        "broker_order_id": "ord-entry-exit-1",
        "order_status": "FILLED",
        "submitted_at": "2026-06-17T09:00:01+09:00",
        "accepted_at": "2026-06-17T09:00:01.200000+09:00",
        "first_fill_at": "2026-06-17T09:00:02+09:00",
        "last_fill_at": "2026-06-17T09:00:02+09:00",
        "updated_at": "2026-06-17T09:00:02+09:00",
        "idempotency_key": "idem-entry-exit-1",
    }
    payload.update(updates)
    return db.save_live_sim_order(payload)


def _save_fill(db: TradingDatabase, **updates):
    payload = {
        "order_intent_id": "live-entry-exit-1",
        "broker_order_id": "ord-entry-exit-1",
        "fill_id": "fill-entry-exit-1",
        "event_id": "evt-entry-exit-1",
        "code": "005930",
        "side": "buy",
        "account_id_masked": "12****90",
        "fill_qty": 10,
        "fill_price": 10000,
        "cumulative_fill_qty": 10,
        "remaining_qty": 0,
        "event_time": "2026-06-17T09:00:02+09:00",
        "received_at": "2026-06-17T09:00:02+09:00",
    }
    payload.update(updates)
    return db.save_live_sim_fill_event(payload)


def _seed_closed_live_sim_case(db: TradingDatabase) -> None:
    _save_decision(db)
    _save_order(db)
    _save_fill(db)
    _save_order(
        db,
        order_intent_id="live-exit-exit-1",
        command_id="cmd-exit-exit-1",
        side="sell",
        requested_price=10100,
        submitted_price=10100,
        broker_order_id="ord-exit-exit-1",
        submitted_at="2026-06-17T09:10:00+09:00",
        accepted_at="2026-06-17T09:10:00.100000+09:00",
        first_fill_at="2026-06-17T09:10:01+09:00",
        last_fill_at="2026-06-17T09:10:01+09:00",
        updated_at="2026-06-17T09:10:01+09:00",
        details={"exit_reason": "TIME_EXIT"},
    )
    _save_fill(
        db,
        order_intent_id="live-exit-exit-1",
        broker_order_id="ord-exit-exit-1",
        fill_id="fill-exit-exit-1",
        event_id="evt-exit-exit-1",
        side="sell",
        fill_price=10100,
        event_time="2026-06-17T09:10:01+09:00",
        received_at="2026-06-17T09:10:01+09:00",
    )
    db.save_gateway_price_ticks_batch(
        [
            {
                "event_id": "tick-exit-1",
                "trade_date": "2026-06-17",
                "timestamp": "2026-06-17T09:01:00+09:00",
                "code": "005930",
                "name": "Samsung",
                "price": 10050,
            },
            {
                "event_id": "tick-exit-2",
                "trade_date": "2026-06-17",
                "timestamp": "2026-06-17T09:05:00+09:00",
                "code": "005930",
                "name": "Samsung",
                "price": 10250,
            },
            {
                "event_id": "tick-exit-3",
                "trade_date": "2026-06-17",
                "timestamp": "2026-06-17T09:12:00+09:00",
                "code": "005930",
                "name": "Samsung",
                "price": 9900,
            },
        ]
    )


def test_shadow_exit_simulator_triggers_core_exit_types():
    simulator = ExitPolicyShadowSimulator()
    entry = {"entry_time": "2026-06-17T09:00:00+09:00", "entry_price": 10000, "quantity": 10}

    stop = simulator.simulate(
        ExitPolicyScenario("stop", "stop", stop_loss_pct=-1.0),
        **entry,
        price_path=[PricePoint("2026-06-17T09:01:00+09:00", 9890)],
    )
    take = simulator.simulate(
        ExitPolicyScenario("take", "take", take_profit_pct=2.0),
        **entry,
        price_path=[PricePoint("2026-06-17T09:01:00+09:00", 10210)],
    )
    trailing = simulator.simulate(
        ExitPolicyScenario("trail", "trail", trailing_start_mfe_pct=2.0, trailing_giveback_pct=1.0),
        **entry,
        price_path=[
            PricePoint("2026-06-17T09:01:00+09:00", 10250),
            PricePoint("2026-06-17T09:02:00+09:00", 10100),
        ],
    )
    partial = simulator.simulate(
        ExitPolicyScenario(
            "partial",
            "partial",
            first_take_profit_pct=2.0,
            first_exit_percent=50,
            final_take_profit_pct=5.0,
        ),
        **entry,
        price_path=[
            PricePoint("2026-06-17T09:01:00+09:00", 10250),
            PricePoint("2026-06-17T09:02:00+09:00", 10500),
        ],
    )
    time_exit = simulator.simulate(
        ExitPolicyScenario("time", "time", max_hold_minutes=2),
        **entry,
        price_path=[PricePoint("2026-06-17T09:03:00+09:00", 10020)],
    )
    insufficient = simulator.simulate(
        ExitPolicyScenario("missing", "missing", stop_loss_pct=-1.0),
        **entry,
        price_path=[],
    )

    assert stop.exit_trigger_type == EXIT_STOP_LOSS
    assert take.exit_trigger_type == EXIT_TAKE_PROFIT
    assert trailing.exit_trigger_type == EXIT_TRAILING_STOP
    assert partial.exit_trigger_type == EXIT_PARTIAL_TAKE_PROFIT
    assert time_exit.exit_trigger_type == EXIT_TIME
    assert insufficient.exit_trigger_type == EXIT_INSUFFICIENT_DATA
    assert insufficient.net_return_pct is None


def test_exit_policy_price_path_accepts_mixed_timezone_timestamps():
    points = _price_path_after_entry(
        [
            {"timestamp": "2026-06-16T23:59:59+00:00", "price": 9900},
            {"timestamp": "2026-06-17T00:00:01+00:00", "price": 10050},
        ],
        "2026-06-17T09:00:00",
    )

    assert [point.price for point in points] == [10050]


def test_exit_policy_validation_compares_shadow_to_actual_and_exports(tmp_path):
    db = _db(tmp_path)
    try:
        _seed_closed_live_sim_case(db)
        analyzer = ExitPolicyValidationAnalyzer(db, report_root=tmp_path / "reports")

        report = analyzer.build_report(trade_date="2026-06-17", scenario_id="tight_stop_fast_profit", limit=100)

        assert report["summary"]["analysis_lifecycle_count"] == 1
        assert report["scenario_summary"][0]["scenario_id"] == "tight_stop_fast_profit"
        case = report["items"][0]
        assert case["shadow_exit_type"] == EXIT_TAKE_PROFIT
        assert case["comparison_label"] == COMPARISON_SHADOW_BETTER
        assert case["better_than_actual"] is True
        exports = analyzer.export_report(report, fmt="all")
        assert Path(exports["json"]).exists()
        assert Path(exports["csv"]).exists()
        assert Path(exports["md"]).exists()
    finally:
        db.close()


def test_exit_policy_validation_api_rebuild_is_analysis_only(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")
    import trading_app.api as api

    api = importlib.reload(api)
    db = TradingDatabase(str(db_path))
    try:
        _seed_closed_live_sim_case(db)
    finally:
        db.close()
    client = TestClient(api.app)

    report = client.get("/api/runtime/exit-policy/validation?trade_date=2026-06-17").json()
    assert report["analysis_only"] is True
    assert report["summary"]["analysis_lifecycle_count"] == 1
    assert report["summary"]["shadow_case_count"] >= 1

    rebuild = client.post(
        "/api/runtime/exit-policy/validation/rebuild",
        json={"trade_date": "2026-06-17", "persist": True, "export": "all"},
        headers={"X-Local-Token": "test-token"},
    ).json()
    assert rebuild["analysis_only"] is True
    assert rebuild["gateway_command_created"] is False
    assert rebuild["settings_changed"] is False
    assert Path(rebuild["exported"]["json"]).exists()
    assert Path(rebuild["exported"]["csv"]).exists()
    assert Path(rebuild["exported"]["md"]).exists()

    reports = client.get("/api/runtime/exit-policy/validation/reports").json()
    assert reports["items"][0]["report_id"] == rebuild["report_id"]
    scenarios = client.get("/api/runtime/exit-policy/validation/scenarios?trade_date=2026-06-17").json()
    assert scenarios["items"]
    cases = client.get("/api/runtime/exit-policy/validation/cases?trade_date=2026-06-17&scenario_id=tight_stop_fast_profit").json()
    assert cases["pagination"]["count"] >= 1

    db = TradingDatabase(str(db_path))
    try:
        command_count = db.conn.execute("SELECT COUNT(*) AS count FROM gateway_commands").fetchone()["count"]
        assert command_count == 0
    finally:
        db.close()
