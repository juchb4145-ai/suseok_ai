from fastapi.testclient import TestClient

from trading.reliability.models import (
    QualificationProfile,
    QualificationRecommendation,
    QualificationStatus,
    ReliabilityReport,
)
from trading.reliability.report import ReliabilityReportWriter


def test_runtime_reliability_read_only_api(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RELIABILITY_REPORT_DIR", str(tmp_path))
    report = ReliabilityReport(
        run_id="rel_api",
        profile=QualificationProfile.QUICK_CI,
        status=QualificationStatus.HOLD,
        recommendation=QualificationRecommendation.OBSERVE_MORE,
        started_at="2026-06-18T00:00:00+00:00",
        finished_at="2026-06-18T00:00:01+00:00",
        duration_sec=1.0,
        config={},
        report_dir=str(tmp_path / "rel_api"),
    )
    ReliabilityReportWriter(output_dir=tmp_path).write(report)

    from trading_app import api

    with TestClient(api.app) as client:
        latest = client.get("/api/runtime/reliability/latest").json()
        runs = client.get("/api/runtime/reliability/runs").json()
        item = client.get("/api/runtime/reliability/runs/rel_api").json()

    assert latest["run_id"] == "rel_api"
    assert runs["runs"][0]["run_id"] == "rel_api"
    assert item["status"] == "HOLD"
