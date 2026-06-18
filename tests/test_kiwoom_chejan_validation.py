from pathlib import Path

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
