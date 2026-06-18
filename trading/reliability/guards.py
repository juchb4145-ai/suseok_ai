from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from storage.db import TradingDatabase
from trading.broker.command_queue import ORDER_COMMAND_TYPES
from trading.reliability.models import ReliabilityQualificationConfig


@dataclass
class ReliabilitySafetyGuardResult:
    allowed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "evidence": dict(self.evidence),
        }


class ReliabilitySafetyGuard:
    def evaluate(self, config: ReliabilityQualificationConfig) -> ReliabilitySafetyGuardResult:
        failures: list[str] = []
        warnings: list[str] = []
        db_path = Path(config.db_path or config.run_dir(config.resolved_run_id()) / "qualification.sqlite3").expanduser()
        broker_env = str(config.broker_env or os.getenv("TRADING_BROKER_ENV") or os.getenv("KIWOOM_BROKER_ENV") or "").upper()
        account_mode = str(os.getenv("TRADING_ACCOUNT_MODE", "")).upper()
        send_order_allowed = _env_bool("TRADING_SEND_ORDER_ALLOWED", False)
        enqueue_gateway_command = _env_bool("TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND", False)
        observe_only = _env_bool("TRADING_ORDER_MANAGER_OBSERVE_ONLY", True)
        order_manager_enabled = _env_bool("TRADING_ORDER_MANAGER_ENABLED", False)
        intent_enabled = _env_bool("TRADING_ORDER_INTENT_ENABLED", False)
        allow_live_sim_orders = _env_bool("TRADING_ALLOW_LIVE_SIM_ORDERS", False)
        test_mode = _env_bool("TRADING_RELIABILITY_TEST_MODE", False)
        if config.require_test_mode and not test_mode:
            failures.append("TRADING_RELIABILITY_TEST_MODE_REQUIRED")
        if broker_env == "REAL" or account_mode == "REAL":
            failures.append("REAL_BROKER_OR_ACCOUNT_MODE_DETECTED")
        if send_order_allowed:
            failures.append("TRADING_SEND_ORDER_ALLOWED_TRUE")
        if enqueue_gateway_command:
            failures.append("ORDER_GATEWAY_COMMAND_ENQUEUE_ENABLED")
        if not observe_only:
            failures.append("ORDER_MANAGER_OBSERVE_ONLY_FALSE")
        if intent_enabled:
            failures.append("ORDER_INTENT_ENABLED_TRUE")
        if order_manager_enabled:
            warnings.append("ORDER_MANAGER_ENABLED_IN_QUALIFICATION")
        if allow_live_sim_orders:
            failures.append("LIVE_SIM_ORDER_FLAG_ENABLED")
        if not _looks_like_qualification_db(db_path):
            failures.append("DB_PATH_NOT_EXPLICIT_QUALIFICATION_DB")
        order_command_count = 0
        if db_path.exists():
            try:
                db = TradingDatabase(str(db_path))
                try:
                    rows = db.list_commands(limit=1000, include_finished=True) if hasattr(db, "list_commands") else []
                finally:
                    db.close()
                order_command_count = sum(1 for row in rows if str(row.get("command_type") or row.get("type") or "") in ORDER_COMMAND_TYPES)
                if order_command_count:
                    failures.append("ORDER_COMMANDS_ALREADY_PRESENT_IN_DB")
            except Exception as exc:
                warnings.append(f"DB_GUARD_INSPECTION_FAILED:{exc}")
        evidence = {
            "test_mode": test_mode,
            "db_path": str(db_path),
            "broker_env": broker_env,
            "account_mode": account_mode,
            "send_order_allowed": send_order_allowed,
            "enqueue_gateway_command": enqueue_gateway_command,
            "observe_only": observe_only,
            "order_manager_enabled": order_manager_enabled,
            "intent_enabled": intent_enabled,
            "allow_live_sim_orders": allow_live_sim_orders,
            "order_command_count": order_command_count,
        }
        return ReliabilitySafetyGuardResult(not failures, failures, warnings, evidence)


def _looks_like_qualification_db(path: Path) -> bool:
    text = str(path).replace("\\", "/").lower()
    if ":memory:" in text:
        return True
    markers = ("/tmp/", "/temp/", "pytest-", "qualification", "reliability", "/reports/")
    if any(marker in text for marker in markers):
        return True
    return path.name.startswith(("qualification", "reliability", "test_"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


__all__ = ["ReliabilitySafetyGuard", "ReliabilitySafetyGuardResult"]
