from storage.db import TradingDatabase
from trading_app.transport_latency import TransportLatencyAnalyzer, TransportLatencyConfig, WebSocketDecisionAdvisor


def _db(tmp_path):
    return TradingDatabase(str(tmp_path / "trader.sqlite3"))


def _sample(sample_id, direction, message_type, total, **extra):
    return {
        "sample_id": sample_id,
        "trace_id": f"trace-{sample_id}",
        "trade_date": "2026-05-30",
        "direction": direction,
        "message_type": message_type,
        "success": True,
        "transport_mode": "rest_long_poll",
        "total_wall_ms": total,
        "long_poll_wait_ms": extra.get("long_poll_wait_ms"),
        "rate_limit_wait_ms": extra.get("rate_limit_wait_ms"),
        "gateway_execute_ms": extra.get("gateway_execute_ms"),
        "ack_round_trip_ms": extra.get("ack_round_trip_ms"),
        "created_at": f"2026-05-30T09:00:0{sample_id[-1]}.000+00:00",
        "metadata": extra.get("metadata", {}),
    }


def test_transport_latency_analyzer_builds_percentiles_and_groups(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_gateway_transport_latency_sample(_sample("s1", "gateway_to_core", "condition_event", 10))
        db.save_gateway_transport_latency_sample(_sample("s2", "gateway_to_core", "execution_event", 20))
        db.save_gateway_transport_latency_sample(_sample("s3", "core_to_gateway", "tr_request", 100, long_poll_wait_ms=80))
        db.save_gateway_transport_latency_sample(_sample("s4", "gateway_ack_to_core", "command_ack", 50, ack_round_trip_ms=50))

        report = TransportLatencyAnalyzer(db).build_report(trade_date="2026-05-30")

        assert report["summary"]["count"] == 4
        assert report["summary"]["event_latency_p95_ms"] > 0
        assert report["summary"]["active_command_count"] == 1
        assert report["summary"]["non_heartbeat_event_count"] == 2
        assert "gateway_to_core" in report["summary"]["by_direction"]
        assert "tr_request" in report["summary"]["by_message_type"]
    finally:
        db.close()


def test_websocket_decision_advisor_keeps_rest_when_under_threshold():
    advisor = WebSocketDecisionAdvisor(TransportLatencyConfig(websocket_recommend_p95_ms=1000))

    decision = advisor.evaluate({"command_latency_p95_ms": 100, "long_poll_wait_p95_ms": 20})

    assert decision["recommendation"] == "KEEP_REST_LONG_POLL"
    assert decision["should_switch"] is False


def test_websocket_decision_advisor_prefers_rate_limit_investigation():
    advisor = WebSocketDecisionAdvisor(TransportLatencyConfig(websocket_recommend_p95_ms=1000))

    decision = advisor.evaluate(
        {
            "command_latency_p95_ms": 2000,
            "long_poll_wait_p95_ms": 1500,
            "rate_limit_wait_p95_ms": 1500,
            "gateway_execute_p95_ms": 100,
        }
    )

    assert decision["recommendation"] == "INVESTIGATE_RATE_LIMIT"
    assert "RATE_LIMIT_WAIT_DOMINATES" in decision["blockers"]


def test_websocket_decision_advisor_recommends_experiment_for_long_poll_bottleneck():
    advisor = WebSocketDecisionAdvisor(TransportLatencyConfig(websocket_recommend_p95_ms=1000))

    decision = advisor.evaluate(
        {
            "command_latency_p95_ms": 1800,
            "long_poll_wait_p95_ms": 900,
            "rate_limit_wait_p95_ms": 0,
            "gateway_execute_p95_ms": 0,
        }
    )

    assert decision["recommendation"] == "TRY_WEBSOCKET_EXPERIMENT"
