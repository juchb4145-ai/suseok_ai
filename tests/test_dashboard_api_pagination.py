import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading.strategy.models import ReviewFinalStatus, TradeReview


def _client(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")
    import trading_app.api as api

    api = importlib.reload(api)
    return TestClient(api.app), db_path


def _save_intent(db: TradingDatabase, *, intent_id: str, code: str, side: str, order_phase: str, status: str = "DRY_RUN_ACCEPTED") -> None:
    db.save_runtime_order_intent(
        {
            "intent_id": intent_id,
            "trade_date": "2026-05-30",
            "source": "strategy_runtime",
            "mode": "DRY_RUN",
            "dry_run": True,
            "status": status,
            "reason": "test",
            "account": "dryrun-account",
            "code": code,
            "side": side,
            "quantity": 1,
            "price": 10000,
            "order_amount": 10000,
            "order_type": 1,
            "hoga": "00",
            "tag": "runtime",
            "strategy_name": "KOSDAQ_THEME_PROFILE",
            "candidate_id": 1,
            "virtual_position_id": 3 if side == "sell" else None,
            "exit_decision_id": 8 if side == "sell" else None,
            "order_phase": order_phase,
            "gate_reason": "LOW_BREADTH",
            "gate_status": "BLOCKED",
            "idempotency_key": intent_id,
            "dedupe_key": f"dedupe:{intent_id}",
            "safety": {"ok": True, "reason": ""},
            "live_safety": {"ok": False, "reason": "GATEWAY_NOT_CONNECTED"},
            "request": {"theme_name": "AI", "theme_score": 30, "hybrid_score": 42},
            "metadata": {"session_bucket": "OPEN"},
            "created_at": f"2026-05-30T09:0{1 if side == 'buy' else 2}:00",
            "updated_at": "2026-05-30T09:02:00",
        }
    )


def test_transport_latency_and_experiments_include_pagination(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    db = TradingDatabase(str(db_path))
    try:
        for index, mode in enumerate(["rest_long_poll", "websocket_mock", "rest_long_poll"], start=1):
            db.save_gateway_transport_latency_sample(
                {
                    "sample_id": f"sample-{index}",
                    "trace_id": f"trace-{index}",
                    "trade_date": "2026-05-30",
                    "direction": "core_to_gateway",
                    "message_type": "tr_request",
                    "transport_mode": mode,
                    "experiment_id": "exp-page",
                    "scenario": "command-heavy",
                    "success": True,
                    "total_wall_ms": 100 * index,
                    "long_poll_wait_ms": 20 * index,
                    "created_at": f"2026-05-30T09:00:0{index}.000+00:00",
                }
            )
    finally:
        db.close()

    page = client.get("/api/gateway/transport/latency?trade_date=2026-05-30&limit=2&offset=0").json()
    assert page["pagination"]["count"] == 2
    assert page["pagination"]["has_next"] is True
    assert len(page["items"]) == 2
    assert len(page["samples"]) == 2

    second = client.get("/api/gateway/transport/latency?trade_date=2026-05-30&limit=2&offset=2").json()
    assert second["pagination"]["has_prev"] is True
    assert second["pagination"]["count"] == 1

    experiments = client.get("/api/gateway/transport/experiments?experiment_id=exp-page&limit=1").json()
    assert experiments["pagination"]["count"] == 1
    assert experiments["items"][0]["experiment_id"] == "exp-page"
    assert experiments["items"][0]["sample_counts"]["rest_long_poll"] == 2


def test_dry_run_orders_pagination_and_filters(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    db = TradingDatabase(str(db_path))
    try:
        _save_intent(db, intent_id="entry-page", code="005930", side="buy", order_phase="entry")
        _save_intent(db, intent_id="exit-page", code="000660", side="sell", order_phase="exit", status="DRY_RUN_REJECTED")
    finally:
        db.close()

    sell = client.get("/api/runtime/orders/dry-run?side=sell&order_phase=exit&status=DRY_RUN_REJECTED&limit=1").json()
    assert sell["pagination"]["count"] == 1
    assert sell["items"][0]["intent_id"] == "exit-page"
    assert sell["filters"]["side"] == "sell"


def test_performance_false_signals_and_command_history_pagination(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    db = TradingDatabase(str(db_path))
    try:
        _save_intent(db, intent_id="perf-entry", code="005930", side="buy", order_phase="entry", status="DRY_RUN_REJECTED")
        db.save_trade_review(
            TradeReview(
                candidate_id=1,
                trade_date="2026-05-30",
                code="005930",
                name="Samsung",
                theme_name="AI",
                strategy_profile="KOSDAQ_THEME_PROFILE",
                gate_result_key="g1",
                review_key="r1",
                final_status=ReviewFinalStatus.BLOCKED_FINAL.value,
                entry_price=10000,
                max_return_5m=1.0,
                max_return_10m=2.0,
                max_return_20m=4.0,
                max_drawdown_20m=-0.3,
                blocked_but_later_rallied=True,
                false_negative_flag=True,
                details={"session_bucket": "OPEN"},
                created_at="2026-05-30T09:30:00",
            )
        )
    finally:
        db.close()

    perf = client.get("/api/runtime/performance/dry-run?trade_date=2026-05-30&code=005930&limit=1").json()
    assert perf["pagination"]["count"] == 1
    assert perf["pagination"]["total"] == 1
    assert perf["items"][0]["code"] == "005930"

    signals = client.get("/api/runtime/performance/dry-run/false-signals?trade_date=2026-05-30&type=false_negative&limit=1").json()
    assert signals["pagination"]["count"] == 1
    assert signals["items"][0]["dry_run_false_negative_type"]

    headers = {"X-Local-Token": "test-token"}
    client.post("/api/gateway/commands", json={"type": "login", "command_id": "cmd-page-login"}, headers=headers)
    client.post("/api/gateway/commands", json={"type": "tr_request", "command_id": "cmd-page-tr"}, headers=headers)
    history = client.get("/api/gateway/commands/history?command_type=login&limit=1").json()
    assert history["pagination"]["count"] == 1
    assert history["items"][0]["command_id"] == "cmd-page-login"

    searched = client.get("/api/gateway/commands/history?command_id=cmd-page-tr").json()
    assert searched["pagination"]["total"] == 1
    assert searched["items"][0]["command_id"] == "cmd-page-tr"
