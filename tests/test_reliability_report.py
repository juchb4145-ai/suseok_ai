from trading.reliability.models import (
    QualificationProfile,
    QualificationRecommendation,
    QualificationStatus,
    ReliabilityReport,
)
from trading.reliability.report import ReliabilityReportReader, ReliabilityReportWriter


def test_report_writer_creates_expected_artifacts(tmp_path):
    report = ReliabilityReport(
        run_id="rel_test",
        profile=QualificationProfile.QUICK_CI,
        status=QualificationStatus.HOLD,
        recommendation=QualificationRecommendation.OBSERVE_MORE,
        started_at="2026-06-18T00:00:00+00:00",
        finished_at="2026-06-18T00:00:01+00:00",
        duration_sec=1.0,
        config={"db_path": str(tmp_path / "reliability.sqlite3")},
        metrics={"counters": {"order_command_count": 0}, "series": {}, "gauges": {}},
        report_dir=str(tmp_path / "rel_test"),
    )
    paths = ReliabilityReportWriter(output_dir=tmp_path).write(report)
    assert set(paths) == {"qualification", "summary", "metrics", "scenario_results", "failures"}
    assert (tmp_path / "rel_test" / "qualification.json").exists()
    assert (tmp_path / "rel_test" / "summary.md").exists()


def test_report_reader_lists_latest_run(tmp_path):
    report = ReliabilityReport(
        run_id="rel_test_reader",
        profile=QualificationProfile.FAULT_SUITE,
        status=QualificationStatus.HOLD,
        recommendation=QualificationRecommendation.OBSERVE_MORE,
        started_at="2026-06-18T00:00:00+00:00",
        finished_at="2026-06-18T00:00:02+00:00",
        duration_sec=2.0,
        config={},
        report_dir=str(tmp_path / "rel_test_reader"),
    )
    ReliabilityReportWriter(output_dir=tmp_path).write(report)
    reader = ReliabilityReportReader(output_dir=tmp_path)
    assert reader.latest()["run_id"] == "rel_test_reader"
    assert reader.list_runs()[0]["status"] == "HOLD"
    assert reader.get_run("rel_test_reader")["profile"] == "fault-suite"
