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


def test_actual_simulation_accept_cancel_and_confirm_are_not_rejected():
    parser = KiwoomChejanParser()
    codec = GatewayEventCodec()

    accepted = parser.parse(
        gubun="0",
        item_count=40,
        raw_fids={
            "9201": "ACC_TOKEN_SIM",
            "9203": "0110280",
            "904": "0000000",
            "9001": "A005930",
            "302": "삼성전자",
            "913": "접수",
            "905": "+매수",
            "900": "1",
            "901": "351500",
            "902": "1",
            "903": "0",
            "907": "2",
            "908": "131637",
            "919": "0",
        },
    )
    assert accepted.event_kind == "order_accepted"
    assert codec.decode(GatewayEvent(type=accepted.gateway_event_type, payload=accepted.to_event_payload())).canonical_type == "ORDER_ACCEPTED"

    cancel_accepted = parser.parse(
        gubun="0",
        item_count=40,
        raw_fids={
            "9201": "ACC_TOKEN_SIM",
            "9203": "0110324",
            "904": "0110280",
            "9001": "A005930",
            "302": "삼성전자",
            "913": "접수",
            "905": "매수취소",
            "900": "1",
            "901": "0",
            "902": "1",
            "903": "0",
            "907": "2",
            "908": "131647",
            "919": "0",
        },
    )
    assert cancel_accepted.event_kind == "order_cancel_accepted"
    assert codec.decode(GatewayEvent(type=cancel_accepted.gateway_event_type, payload=cancel_accepted.to_event_payload())).canonical_type == "ORDER_CANCEL_ACCEPTED"

    cancelled = parser.parse(
        gubun="0",
        item_count=40,
        raw_fids={
            "9201": "ACC_TOKEN_SIM",
            "9203": "0110324",
            "904": "0110280",
            "9001": "A005930",
            "302": "삼성전자",
            "913": "확인",
            "905": "매수취소",
            "900": "1",
            "901": "0",
            "902": "0",
            "903": "0",
            "907": "2",
            "908": "131647",
            "919": "0",
        },
    )
    assert cancelled.event_kind == "order_cancelled"
    assert codec.decode(GatewayEvent(type=cancelled.gateway_event_type, payload=cancelled.to_event_payload())).canonical_type == "ORDER_CANCELLED"


def test_actual_simulation_fill_keeps_execution_identity():
    result = KiwoomChejanParser().parse(
        gubun="0",
        item_count=40,
        raw_fids={
            "9201": "ACC_TOKEN_SIM",
            "9203": "0110357",
            "904": "0000000",
            "9001": "A005930",
            "302": "삼성전자",
            "913": "체결",
            "905": "+매수",
            "900": "3",
            "901": "356500",
            "902": "0",
            "903": "1068000",
            "907": "2",
            "908": "131656",
            "909": "839528",
            "910": "356000",
            "911": "3",
            "914": "356000",
            "915": "3",
            "919": "0",
        },
    )
    decoded = GatewayEventCodec().decode(GatewayEvent(type=result.gateway_event_type, payload=result.to_event_payload()))
    assert result.event_kind == "order_fill"
    assert result.canonical_payload["execution_id"] == "839528"
    assert decoded.canonical_type == "ORDER_FILLED"
