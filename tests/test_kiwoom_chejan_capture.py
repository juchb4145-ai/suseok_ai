import json

import pytest

from trading.broker.chejan_capture import ChejanCaptureConfig, KiwoomChejanCaptureWriter, redact_chejan_payload, validate_redaction


def test_capture_redacts_account_and_drops_secrets(tmp_path):
    writer = KiwoomChejanCaptureWriter(
        ChejanCaptureConfig(enabled=True, simulation_only=True, capture_dir=str(tmp_path), max_rows=10)
    )
    result = writer.write(
        broker_env="SIMULATION",
        gubun="0",
        item_count=2,
        fid_list="9201;9203",
        raw_fids={"9201": "1234567890", "9203": "OID-1", "password": "secret"},
        parser_result={"parser_version": "kiwoom_chejan_v2", "event_kind": "order_accepted", "account": "1234567890"},
    )
    assert result["written"] is True
    payload = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert payload["raw_fids"]["9201"].startswith("ACC_TOKEN_")
    assert "1234567890" not in json.dumps(payload, ensure_ascii=False)
    assert "password" not in json.dumps(payload, ensure_ascii=False).lower()


def test_capture_rejects_real_broker(tmp_path):
    writer = KiwoomChejanCaptureWriter(ChejanCaptureConfig(enabled=True, simulation_only=True, capture_dir=str(tmp_path)))
    with pytest.raises(RuntimeError):
        writer.write(
            broker_env="REAL",
            gubun="0",
            item_count=1,
            fid_list="9201",
            raw_fids={"9201": "1234567890"},
            parser_result={},
        )


def test_redaction_validator_flags_sensitive_keys():
    payload = redact_chejan_payload({"account": "123", "user_id": "alice", "safe": "ok"})
    assert payload["account"].startswith("ACC_TOKEN_")
    assert "user_id" not in payload
    assert validate_redaction(payload)["ok"] is True


def test_redaction_validator_flags_unredacted_account_fields():
    result = validate_redaction({"9201": "1234567890", "account": "ACC_TOKEN_SAFE", "safe": "ok"})
    assert result["ok"] is False
    assert "9201" in result["leaks"]
