from trading.theme_engine.models import StockThemeState, ThemeRankItem, ThemeStatus
from trading.theme_engine.ws.schemas import (
    build_heartbeat_payload,
    build_stock_theme_state_payload,
    build_theme_rank_payload,
    parse_subscribe_request,
)


def test_theme_rank_payload_schema():
    payload = build_theme_rank_payload(
        [
            ThemeRankItem(
                rank=1,
                theme_id="furiosa_ai",
                theme_name="퓨리오사AI",
                theme_score=84.5,
                status=ThemeStatus.ACTIVE,
                trade_eligible=True,
                details={
                    "reason_codes": ["LOW_SNAPSHOT_COVERAGE"],
                    "top_stocks": [{"rank": rank, "stock_code": f"{rank:06d}"} for rank in range(1, 7)],
                    "snapshot_quality": {"snapshot_coverage": 0.4},
                },
            )
        ],
        top_n=20,
        ts="2026-05-30T09:10:00+09:00",
    )

    assert payload["type"] == "theme_rank"
    assert payload["themes"][0]["theme_id"] == "furiosa_ai"
    assert payload["themes"][0]["status"] == "ACTIVE"
    assert payload["themes"][0]["reason_codes"] == ["LOW_SNAPSHOT_COVERAGE"]
    assert len(payload["themes"][0]["top_stocks"]) == 5
    assert payload["themes"][0]["snapshot_quality"] == {"snapshot_coverage": 0.4}


def test_stock_theme_state_and_heartbeat_payloads():
    state_payload = build_stock_theme_state_payload(
        StockThemeState(stock_code="000001", primary_theme_id="furiosa_ai", ready=True),
        ts="2026-05-30T09:10:00+09:00",
    )
    heartbeat = build_heartbeat_payload(ts="2026-05-30T09:10:01+09:00")

    assert state_payload["type"] == "stock_theme_state"
    assert heartbeat["type"] == "heartbeat"


def test_subscribe_request_parsing():
    request = parse_subscribe_request(
        {
            "action": "subscribe",
            "channels": ["theme_rank", "stock_theme_state"],
            "top_n": 10,
            "stock_codes": ["000001"],
        }
    )

    assert request["action"] == "subscribe"
    assert request["top_n"] == 10
    assert request["stock_codes"] == ["000001"]


def test_subscribe_request_parsing_normalizes_untrusted_values():
    request = parse_subscribe_request(
        {
            "action": "subscribe",
            "channels": "theme_detail",
            "top_n": "not-a-number",
            "theme_ids": "ai",
            "stock_codes": "000001",
        }
    )

    assert request["channels"] == ["theme_detail"]
    assert request["top_n"] == 20
    assert request["theme_ids"] == ["ai"]
    assert request["stock_codes"] == ["000001"]


def test_ws_server_module_imports_without_fastapi_requirement():
    import trading.theme_engine.ws.server as server

    assert hasattr(server, "create_app")
