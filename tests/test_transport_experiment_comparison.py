import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading_app.transport_latency import TransportLatencyAnalyzer, TransportLatencyConfig, WebSocketDecisionAdvisor


def _sample(sample_id, transport_mode, command_p95, *, experiment_id="exp-compare", scenario="command-heavy", long_poll=0, rate_limit=0, execute=0):
    return {
        "sample_id": sample_id,
        "trace_id": f"trace-{sample_id}",
        "trade_date": "2026-05-30",
        "direction": "core_to_gateway",
        "message_type": "tr_request",
        "transport_mode": transport_mode,
        "experiment_id": experiment_id,
        "scenario": scenario,
        "success": True,
        "total_wall_ms": command_p95,
        "long_poll_wait_ms": long_poll,
        "rate_limit_wait_ms": rate_limit,
        "gateway_execute_ms": execute,
        "created_at": f"2026-05-30T09:00:0{sample_id[-1]}.000+00:00",
        "metadata": {"experiment_id": experiment_id, "scenario": scenario},
    }


def test_transport_comparison_report_prefers_websocket_when_long_poll_bound(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_gateway_transport_latency_sample(_sample("rest1", "rest_long_poll", 2000, long_poll=1500))
        db.save_gateway_transport_latency_sample(_sample("ws1", "websocket_mock", 300))

        report = TransportLatencyAnalyzer(db, config=TransportLatencyConfig(websocket_recommend_p95_ms=1000)).build_transport_comparison_report(
            experiment_id="exp-compare",
            scenario="command-heavy",
        )

        assert report["delta"]["command_p95_delta_ms"] > 0
        assert report["websocket_recommendation"]["recommendation"] == "WEBSOCKET_PROMISING_BUT_NEEDS_REAL_GATEWAY_TEST"
    finally:
        db.close()


def test_transport_comparison_rate_limit_bound_is_not_websocket_helpful(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_gateway_transport_latency_sample(_sample("rest1", "rest_long_poll", 2000, long_poll=100, rate_limit=1500))
        db.save_gateway_transport_latency_sample(_sample("ws1", "websocket_mock", 300))

        report = TransportLatencyAnalyzer(db, config=TransportLatencyConfig(websocket_recommend_p95_ms=1000)).build_transport_comparison_report(
            experiment_id="exp-compare",
            scenario="command-heavy",
        )

        assert report["websocket_recommendation"]["recommendation"] == "WEBSOCKET_NOT_HELPFUL_RATE_LIMIT_BOUND"
    finally:
        db.close()


def test_websocket_decision_advisor_accepts_comparison_payload():
    advisor = WebSocketDecisionAdvisor(TransportLatencyConfig(websocket_recommend_p95_ms=1000))

    decision = advisor.evaluate(
        {
            "comparison": {
                "rest_summary": {"command_latency_p95_ms": 2000, "long_poll_wait_p95_ms": 1500},
                "websocket_summary": {"command_latency_p95_ms": 300},
                "delta": {"command_p95_delta_ms": 1700},
            }
        }
    )

    assert decision["recommendation"] == "WEBSOCKET_PROMISING_BUT_NEEDS_REAL_GATEWAY_TEST"
    assert decision["real_gateway_switch_ready"] is False


def test_transport_experiment_api_and_dashboard_snapshot(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    import trading_app.api as api

    api = importlib.reload(api)
    client = TestClient(api.app)
    db = TradingDatabase(str(db_path))
    try:
        db.save_gateway_transport_latency_sample(_sample("rest1", "rest_long_poll", 2000, long_poll=1500))
        db.save_gateway_transport_latency_sample(_sample("ws1", "websocket_mock", 300))
    finally:
        db.close()

    experiments = client.get("/api/gateway/transport/experiments").json()
    assert experiments["items"][0]["experiment_id"] == "exp-compare"

    detail = client.get("/api/gateway/transport/experiments/exp-compare?scenario=command-heavy").json()
    assert detail["found"] is True
    assert detail["report"]["sample_counts"]["websocket_mock"] == 1

    decision = client.get("/api/gateway/transport/websocket-decision").json()
    assert decision["real_gateway_switch_ready"] is False
    assert "latest_comparison_report" in decision

    snapshot = client.get("/api/snapshot").json()
    assert snapshot["transport_experiment"]["latest_experiment_id"] == "exp-compare"
