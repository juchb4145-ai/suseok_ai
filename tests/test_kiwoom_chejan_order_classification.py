import json
from pathlib import Path

from kiwoom.chejan import KiwoomChejanParser
from trading.broker.models import GatewayEvent
from trading_app.gateway_event_consumer import GatewayEventCodec


FIXTURE_DIR = Path("tests/fixtures/kiwoom_chejan")


def _event(name: str) -> GatewayEvent:
    payload = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    result = KiwoomChejanParser().parse(
        gubun=payload["gubun"],
        item_count=payload["item_count"],
        fid_list=payload.get("fid_list", ""),
        raw_fids=payload["raw_fids"],
    )
    return GatewayEvent(type=result.gateway_event_type, payload=result.to_event_payload(), event_id=f"fixture-{name}")


def test_order_accept_with_remaining_zero_but_no_execution_is_not_filled():
    payload = json.loads((FIXTURE_DIR / "order_accepted.json").read_text(encoding="utf-8"))
    payload["raw_fids"]["902"] = "0"
    result = KiwoomChejanParser().parse(gubun="0", item_count=payload["item_count"], raw_fids=payload["raw_fids"])
    decoded = GatewayEventCodec().decode(GatewayEvent(type=result.gateway_event_type, payload=result.to_event_payload()))
    assert decoded.canonical_type == "ORDER_ACCEPTED"


def test_partial_full_reject_cancel_and_balance_classification():
    codec = GatewayEventCodec()
    assert codec.decode(_event("partial_fill")).canonical_type == "ORDER_PARTIALLY_FILLED"
    assert codec.decode(_event("full_fill")).canonical_type == "ORDER_FILLED"
    assert codec.decode(_event("order_rejected")).canonical_type == "ORDER_REJECTED"
    assert codec.decode(_event("cancel_accepted")).canonical_type == "ORDER_CANCEL_ACCEPTED"
    assert codec.decode(_event("cancelled")).canonical_type == "ORDER_CANCELLED"
    assert codec.decode(_event("balance_increase")).canonical_type == "POSITION_SNAPSHOT"


def test_unknown_gubun_is_safe_ignored():
    decoded = GatewayEventCodec().decode(_event("unknown_gubun"))
    assert decoded.ignored is True
    assert decoded.canonical_type == "IGNORED"


def test_invalid_order_chejan_fails_closed_as_order_rejected():
    result = KiwoomChejanParser().parse(gubun="0", item_count=1, raw_fids={"9001": "A005930"})
    decoded = GatewayEventCodec().decode(GatewayEvent(type=result.gateway_event_type, payload=result.to_event_payload()))
    assert decoded.canonical_type == "ORDER_REJECTED"
    assert decoded.payload["reason"] == "KIWOOM_CHEJAN_PARSER_INVALID"
