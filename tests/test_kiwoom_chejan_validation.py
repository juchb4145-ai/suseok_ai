from pathlib import Path
import json

from tools.kiwoom_chejan_parser_validation import validate_fixture_dir


def test_synthetic_fixture_validation_is_hold(tmp_path):
    report = validate_fixture_dir("tests/fixtures/kiwoom_chejan", output_dir=tmp_path)
    assert report["status"] == "HOLD"
    assert report["recommendation"] == "READY_FOR_KIWOOM_PARSER_VALIDATION"
    assert report["source"] == "SYNTHETIC"
    assert report["failures"] == []
    assert (tmp_path / "validation.json").exists()
    assert (tmp_path / "classification_matrix.json").exists()


def test_validation_outputs_required_artifacts(tmp_path):
    validate_fixture_dir("tests/fixtures/kiwoom_chejan", output_dir=tmp_path)
    for name in ("validation.json", "summary.md", "field_coverage.json", "unknown_fids.json", "classification_matrix.json", "failures.json"):
        assert Path(tmp_path / name).exists()


def test_captured_simulation_fixture_infers_source_and_covered_cases(tmp_path):
    fixture_dir = tmp_path / "capture"
    fixture_dir.mkdir()
    cases = {
        "accepted.json": {
            "broker_env": "SIMULATION",
            "gubun": "0",
            "item_count": 40,
            "raw_fids": {
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
        },
        "cancel_accepted.json": {
            "broker_env": "SIMULATION",
            "gubun": "0",
            "item_count": 40,
            "raw_fids": {
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
        },
        "cancelled.json": {
            "broker_env": "SIMULATION",
            "gubun": "0",
            "item_count": 40,
            "raw_fids": {
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
        },
        "fill.json": {
            "broker_env": "SIMULATION",
            "gubun": "0",
            "item_count": 40,
            "raw_fids": {
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
        },
        "balance.json": {
            "broker_env": "SIMULATION",
            "gubun": "1",
            "item_count": 37,
            "raw_fids": {
                "9201": "ACC_TOKEN_SIM",
                "9001": "A005930",
                "302": "삼성전자",
                "930": "3",
                "931": "356000",
                "932": "1068000",
                "933": "3",
                "945": "3",
            },
        },
    }
    for name, payload in cases.items():
        (fixture_dir / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = validate_fixture_dir(fixture_dir, output_dir=tmp_path / "out")
    assert report["source"] == "KIWOOM_SIMULATION"
    assert report["status"] == "HOLD"
    assert report["failures"] == []
    assert "order_accepted" not in report["missing_required_cases"]
    assert "cancel_accepted" not in report["missing_required_cases"]
    assert "cancelled" not in report["missing_required_cases"]
    assert "full_fill" not in report["missing_required_cases"]
    assert "balance_increase" not in report["missing_required_cases"]
