from trading.reliability.guards import ReliabilitySafetyGuard
from trading.reliability.models import ReliabilityQualificationConfig


def _safe_env(monkeypatch):
    monkeypatch.setenv("TRADING_RELIABILITY_TEST_MODE", "true")
    monkeypatch.setenv("TRADING_SEND_ORDER_ALLOWED", "false")
    monkeypatch.setenv("TRADING_ORDER_MANAGER_OBSERVE_ONLY", "true")
    monkeypatch.setenv("TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND", "false")
    monkeypatch.setenv("TRADING_ORDER_INTENT_ENABLED", "false")
    monkeypatch.setenv("TRADING_ALLOW_LIVE_SIM_ORDERS", "false")
    monkeypatch.setenv("TRADING_BROKER_ENV", "SIMULATION")
    monkeypatch.setenv("TRADING_ACCOUNT_MODE", "SIMULATION")


def test_safety_guard_allows_explicit_qualification_db(tmp_path, monkeypatch):
    _safe_env(monkeypatch)
    config = ReliabilityQualificationConfig(db_path=str(tmp_path / "reliability_qualification.sqlite3"))
    result = ReliabilitySafetyGuard().evaluate(config)
    assert result.allowed is True
    assert result.evidence["send_order_allowed"] is False
    assert result.evidence["observe_only"] is True


def test_safety_guard_rejects_real_broker_and_order_enabled(tmp_path, monkeypatch):
    _safe_env(monkeypatch)
    monkeypatch.setenv("TRADING_BROKER_ENV", "REAL")
    monkeypatch.setenv("TRADING_SEND_ORDER_ALLOWED", "true")
    config = ReliabilityQualificationConfig(db_path=str(tmp_path / "reliability_qualification.sqlite3"))
    result = ReliabilitySafetyGuard().evaluate(config)
    assert result.allowed is False
    assert "REAL_BROKER_OR_ACCOUNT_MODE_DETECTED" in result.failures
    assert "TRADING_SEND_ORDER_ALLOWED_TRUE" in result.failures


def test_safety_guard_requires_test_mode(tmp_path, monkeypatch):
    _safe_env(monkeypatch)
    monkeypatch.setenv("TRADING_RELIABILITY_TEST_MODE", "false")
    config = ReliabilityQualificationConfig(db_path=str(tmp_path / "reliability_qualification.sqlite3"))
    result = ReliabilitySafetyGuard().evaluate(config)
    assert result.allowed is False
    assert "TRADING_RELIABILITY_TEST_MODE_REQUIRED" in result.failures
