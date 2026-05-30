import json
from datetime import datetime, timedelta

from trading.strategy.candles import Candle
from trading.strategy.hybrid_validation import HybridValidationReportExporter, label_event_outcome

from tests.test_hybrid_validation_helpers import event


START = datetime(2026, 5, 30, 9, 0)


def test_hybrid_validation_report_exports_csv_json_markdown_and_recommendations(tmp_path):
    events = [
        label_event_outcome(event(status="READY", theme_score=88, membership_score=0.9), [_candle(1, 104, 99)]),
        label_event_outcome(event(status="WAIT", reason_codes=["LOW_BREADTH"], theme_score=70, membership_score=0.6), [_candle(1, 104, 99)]),
    ]
    exporter = HybridValidationReportExporter()

    csv_path = exporter.export_csv(events, tmp_path / "hybrid_gate_validation_20260530.csv")
    json_path = exporter.export_json(events, tmp_path / "hybrid_gate_validation_20260530.json")
    md_path = exporter.export_markdown(events, tmp_path / "hybrid_gate_validation_20260530.md")
    rec_path = exporter.export_recommendations_json(events, tmp_path / "hybrid_calibration_recommendations_20260530.json")

    assert "hybrid_status" in csv_path.read_text(encoding="utf-8-sig")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    rec_payload = json.loads(rec_path.read_text(encoding="utf-8"))
    markdown = md_path.read_text(encoding="utf-8")

    assert payload["event_count"] == 2
    assert "Hybrid Status Performance" in markdown
    assert "Hybrid Reason Code Performance" in markdown
    assert "Theme Score Band Performance" in markdown
    assert "Membership Score Band Performance" in markdown
    assert "WATCH Theme Small Entry Policy Review" in markdown
    assert "WAIT Quality Review" in markdown
    assert "Calibration Recommendations" in markdown
    assert rec_payload["auto_apply"] is False


def _candle(offset_min: int, high: float, low: float):
    return Candle("000001", 1, START + timedelta(minutes=offset_min), 100, int(high), int(low), int(high))
