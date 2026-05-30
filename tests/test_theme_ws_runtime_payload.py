from trading.theme_engine.ws.schemas import build_runtime_health_payload, parse_subscribe_request


def test_runtime_health_payload_schema():
    payload = build_runtime_health_payload(
        {
            "running": True,
            "last_sync_at": "2026-05-30T09:05:00",
            "active_theme_count": 3,
            "active_stock_count": 20,
            "data_ready": True,
        },
        ts="2026-05-30T09:10:00+09:00",
    )

    assert payload["type"] == "runtime_health"
    assert payload["running"] is True
    assert payload["data_ready"] is True


def test_runtime_health_subscribe_parsing():
    request = parse_subscribe_request({"action": "subscribe", "channels": ["theme_rank", "runtime_health"]})

    assert "runtime_health" in request["channels"]
