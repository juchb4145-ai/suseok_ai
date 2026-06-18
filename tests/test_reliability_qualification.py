from trading.reliability.models import QualificationProfile, QualificationStatus, ReliabilityQualificationConfig, SLOThresholds
from trading.reliability.qualification import ReliabilityQualificationRunner, qualification_exit_code


def _safe_env(monkeypatch):
    monkeypatch.setenv("TRADING_RELIABILITY_TEST_MODE", "true")
    monkeypatch.setenv("TRADING_SEND_ORDER_ALLOWED", "false")
    monkeypatch.setenv("TRADING_ORDER_MANAGER_OBSERVE_ONLY", "true")
    monkeypatch.setenv("TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND", "false")
    monkeypatch.setenv("TRADING_ORDER_INTENT_ENABLED", "false")
    monkeypatch.setenv("TRADING_ALLOW_LIVE_SIM_ORDERS", "false")
    monkeypatch.setenv("TRADING_BROKER_ENV", "SIMULATION")
    monkeypatch.setenv("TRADING_ACCOUNT_MODE", "SIMULATION")


def test_quick_ci_qualification_generates_hold_report(tmp_path, monkeypatch):
    _safe_env(monkeypatch)
    config = ReliabilityQualificationConfig(
        profile=QualificationProfile.QUICK_CI,
        output_dir=str(tmp_path),
        db_path=str(tmp_path / "reliability_qualification.sqlite3"),
        duration_sec=1,
        code_count=3,
        ticks_per_sec=2,
        order_event_rate=0,
        duplicate_rate=0,
        out_of_order_rate=0,
    )
    report = ReliabilityQualificationRunner(config, thresholds=SLOThresholds(min_soak_duration_sec=3600)).run()
    payload = report.to_dict()
    assert payload["status"] == QualificationStatus.HOLD.value
    assert payload["metrics"]["counters"]["order_command_count"] == 0
    assert "OBSERVE_SOAK_1H" in payload["not_run"]
    assert (tmp_path / payload["run_id"] / "qualification.json").exists()
    assert all(scenario["status"] == "PASS" for scenario in payload["scenarios"])


def test_qualification_guard_failure_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RELIABILITY_TEST_MODE", "false")
    monkeypatch.setenv("TRADING_SEND_ORDER_ALLOWED", "false")
    config = ReliabilityQualificationConfig(output_dir=str(tmp_path), db_path=str(tmp_path / "reliability_qualification.sqlite3"))
    report = ReliabilityQualificationRunner(config).run()
    assert report.to_dict()["status"] == QualificationStatus.ERROR.value
    assert qualification_exit_code(report.status) == 3
