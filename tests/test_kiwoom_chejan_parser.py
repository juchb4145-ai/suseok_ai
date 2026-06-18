import json
from pathlib import Path

from kiwoom.chejan import ChejanParseStatus, KiwoomChejanParser


FIXTURE_DIR = Path("tests/fixtures/kiwoom_chejan")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _parse(name: str):
    payload = _fixture(name)
    return KiwoomChejanParser().parse(
        gubun=payload["gubun"],
        item_count=payload["item_count"],
        fid_list=payload.get("fid_list", ""),
        raw_fids=payload["raw_fids"],
    )


def test_order_accepted_preserves_account_order_original_and_screen_no():
    result = _parse("order_accepted")
    payload = result.canonical_payload
    assert result.gubun == "0"
    assert result.event_kind == "order_accepted"
    assert payload["account"] == "ACC_TOKEN_SYNTHETIC"
    assert payload["order_no"] == "OID-1001"
    assert payload["original_order_no"] == ""
    assert payload["screen_no"] == "7000"
    assert payload["legacy_tag"] == ""
    assert payload["tag_source"] == "UNAVAILABLE_FROM_CHEJAN"
    assert payload["remaining_quantity"] == 3
    assert payload["cumulative_filled_quantity"] == 0


def test_order_rejected_preserves_reject_reason():
    result = _parse("order_rejected")
    assert result.event_kind == "order_rejected"
    assert result.canonical_payload["reject_reason"] == "증거금 부족"


def test_partial_and_full_fill_quantity_semantics_are_separate():
    partial = _parse("partial_fill")
    full = _parse("full_fill")
    assert partial.event_kind == "order_fill"
    assert partial.canonical_payload["execution_id"] == "EXEC-1003-1"
    assert partial.canonical_payload["incremental_execution_quantity"] == 1
    assert partial.canonical_payload["cumulative_filled_quantity"] == 1
    assert partial.canonical_payload["remaining_quantity"] == 2
    assert full.canonical_payload["incremental_execution_quantity"] == 1
    assert full.canonical_payload["cumulative_filled_quantity"] == 3
    assert full.canonical_payload["remaining_quantity"] == 0
    assert full.details["quantity_semantics"]["fid_911"].startswith("execution_quantity_raw")


def test_balance_zero_is_single_code_delta_not_full_snapshot():
    result = _parse("balance_zero")
    payload = result.canonical_payload
    assert result.gubun == "1"
    assert result.event_kind == "position_delta"
    assert payload["position_quantity"] == 0
    assert payload["snapshot_scope"] == "SINGLE_CODE_DELTA"
    assert payload["full_account_snapshot"] is False
    assert payload["positions"][0]["quantity"] == 0


def test_unknown_gubun_is_unsupported_not_invalid_order():
    result = _parse("unknown_gubun")
    assert result.parse_status == ChejanParseStatus.UNSUPPORTED
    assert result.event_kind == "unsupported_gubun"
    assert result.gateway_event_type == "kiwoom_special_chejan"


def test_missing_execution_id_with_fill_quantity_is_degraded_low_confidence():
    raw = dict(_fixture("partial_fill")["raw_fids"])
    raw["909"] = ""
    result = KiwoomChejanParser().parse(gubun="0", item_count=len(raw), raw_fids=raw)
    assert result.parse_status == ChejanParseStatus.DEGRADED
    assert "EXECUTION_ID_MISSING" in result.warning_codes
    assert result.canonical_payload["dedupe_confidence"] == "LOW"


def test_empty_value_and_actual_zero_are_distinguished():
    raw = dict(_fixture("balance_zero")["raw_fids"])
    raw["930"] = "0"
    raw["933"] = ""
    result = KiwoomChejanParser().parse(gubun="1", item_count=len(raw), raw_fids=raw)
    fields = result.details["fields"]
    assert fields["position_quantity"]["field_present"] is True
    assert fields["position_quantity"]["raw_value"] == "0"
    assert result.canonical_payload["position_quantity"] == 0
    assert fields["orderable_quantity"]["field_present"] is True
    assert fields["orderable_quantity"]["raw_value"] == ""
    assert result.canonical_payload["orderable_quantity"] is None
