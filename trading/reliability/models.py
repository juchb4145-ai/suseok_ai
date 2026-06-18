from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4


class QualificationProfile(str, Enum):
    QUICK_CI = "quick-ci"
    REPLAY = "replay"
    FAULT_SUITE = "fault-suite"
    OBSERVE_SOAK = "observe-soak"
    FULL_QUALIFICATION = "full"


class QualificationStatus(str, Enum):
    PASS = "PASS"
    HOLD = "HOLD"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"
    ERROR = "ERROR"


class QualificationRecommendation(str, Enum):
    NOT_READY = "NOT_READY"
    OBSERVE_MORE = "OBSERVE_MORE"
    READY_FOR_KIWOOM_PARSER_VALIDATION = "READY_FOR_KIWOOM_PARSER_VALIDATION"
    READY_FOR_RECONCILE_TR_PILOT = "READY_FOR_RECONCILE_TR_PILOT"
    READY_FOR_LIVE_SIM_CANARY_REVIEW = "READY_FOR_LIVE_SIM_CANARY_REVIEW"


class ScenarioId(str, Enum):
    F01_DUPLICATE_PRICE_TICKS = "F01_DUPLICATE_PRICE_TICKS"
    F02_DUPLICATE_EXECUTION = "F02_DUPLICATE_EXECUTION"
    F03_FILL_BEFORE_ACK = "F03_FILL_BEFORE_ACK"
    F04_OUT_OF_ORDER_PARTIAL_FILLS = "F04_OUT_OF_ORDER_PARTIAL_FILLS"
    F05_CRASH_AFTER_RECEIPT = "F05_CRASH_AFTER_RECEIPT"
    F06_STALE_EVENT_CLAIM = "F06_STALE_EVENT_CLAIM"
    F07_SQLITE_BUSY = "F07_SQLITE_BUSY"
    F08_EVENT_LOG_APPEND_FAILURE = "F08_EVENT_LOG_APPEND_FAILURE"
    F09_CONSUMER_EXCEPTION = "F09_CONSUMER_EXCEPTION"
    F10_MALFORMED_ORDER_EVENT = "F10_MALFORMED_ORDER_EVENT"
    F11_GATEWAY_DISCONNECT_RECONNECT = "F11_GATEWAY_DISCONNECT_RECONNECT"
    F12_RUNTIME_CYCLE_SLOW = "F12_RUNTIME_CYCLE_SLOW"
    F13_TICK_QUEUE_SATURATION = "F13_TICK_QUEUE_SATURATION"
    F14_DASHBOARD_WRITER_FAILURE = "F14_DASHBOARD_WRITER_FAILURE"
    F15_MULTI_CLIENT_DASHBOARD = "F15_MULTI_CLIENT_DASHBOARD"
    F16_CORE_RESTART_WITH_BACKLOG = "F16_CORE_RESTART_WITH_BACKLOG"
    F17_BALANCE_MISMATCH = "F17_BALANCE_MISMATCH"
    F18_DEAD_LETTER_PRESENT = "F18_DEAD_LETTER_PRESENT"


@dataclass(frozen=True)
class ReliabilityQualificationConfig:
    profile: QualificationProfile = QualificationProfile.QUICK_CI
    output_dir: str = "reports/reliability"
    run_id: str = ""
    db_path: str = ""
    core_url: str = ""
    bundle_path: str = ""
    duration_sec: float = 60.0
    sample_interval_sec: float = 1.0
    repeat: int = 2
    seed: int = 20260618
    code_count: int = 30
    ticks_per_sec: float = 20.0
    event_burst_size: int = 10
    duplicate_rate: float = 0.05
    out_of_order_rate: float = 0.05
    malformed_event_rate: float = 0.0
    reconnect_rate: float = 0.0
    order_event_rate: float = 0.05
    require_test_mode: bool = True
    broker_env: str = ""
    account: str = ""
    git_sha: str = ""

    @classmethod
    def from_env(
        cls,
        *,
        profile: str = "quick-ci",
        output_dir: str = "reports/reliability",
        db_path: str = "",
        core_url: str = "",
        bundle_path: str = "",
        duration_sec: float | None = None,
        sample_interval_sec: float | None = None,
        repeat: int | None = None,
        seed: int | None = None,
        code_count: int | None = None,
    ) -> "ReliabilityQualificationConfig":
        resolved_profile = QualificationProfile(str(profile or "quick-ci"))
        return cls(
            profile=resolved_profile,
            output_dir=output_dir,
            db_path=db_path,
            core_url=core_url,
            bundle_path=bundle_path,
            duration_sec=float(duration_sec if duration_sec is not None else _env_float("TRADING_RELIABILITY_DURATION_SEC", 60.0)),
            sample_interval_sec=float(sample_interval_sec if sample_interval_sec is not None else _env_float("TRADING_RELIABILITY_SAMPLE_INTERVAL_SEC", 1.0)),
            repeat=int(repeat if repeat is not None else _env_int("TRADING_RELIABILITY_REPLAY_REPEAT", 2)),
            seed=int(seed if seed is not None else _env_int("TRADING_RELIABILITY_SEED", 20260618)),
            code_count=int(code_count if code_count is not None else _env_int("TRADING_RELIABILITY_CODE_COUNT", 30)),
            ticks_per_sec=_env_float("TRADING_RELIABILITY_TICKS_PER_SEC", 20.0),
            event_burst_size=_env_int("TRADING_RELIABILITY_EVENT_BURST_SIZE", 10),
            duplicate_rate=_env_float("TRADING_RELIABILITY_DUPLICATE_RATE", 0.05),
            out_of_order_rate=_env_float("TRADING_RELIABILITY_OUT_OF_ORDER_RATE", 0.05),
            malformed_event_rate=_env_float("TRADING_RELIABILITY_MALFORMED_EVENT_RATE", 0.0),
            reconnect_rate=_env_float("TRADING_RELIABILITY_RECONNECT_RATE", 0.0),
            order_event_rate=_env_float("TRADING_RELIABILITY_ORDER_EVENT_RATE", 0.05),
            broker_env=os.getenv("TRADING_BROKER_ENV", os.getenv("KIWOOM_BROKER_ENV", "")),
            account=os.getenv("TRADING_ACCOUNT", ""),
            git_sha=os.getenv("GIT_COMMIT", ""),
        )

    def resolved_run_id(self) -> str:
        return self.run_id or f"rel_{self.profile.value}_{uuid4().hex[:10]}"

    def run_dir(self, run_id: str) -> Path:
        return Path(self.output_dir).expanduser() / run_id

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["profile"] = self.profile.value
        return payload


@dataclass(frozen=True)
class QualificationScenario:
    scenario_id: str
    name: str
    mandatory: bool = True
    expected: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QualificationScenarioResult:
    scenario_id: str
    status: QualificationStatus | str
    started_at: str = ""
    finished_at: str = ""
    duration_ms: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recommendation: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value if isinstance(self.status, QualificationStatus) else str(self.status)
        return payload


@dataclass
class SLOThresholds:
    runtime_cycle_p95_ms: float = 1500.0
    dirty_evaluator_p95_ms: float = 500.0
    order_event_p95_ms: float = 250.0
    dashboard_read_p95_ms: float = 100.0
    dashboard_build_p95_ms: float = 500.0
    read_model_max_age_sec: float = 5.0
    backlog_max_age_sec: float = 5.0
    replay_drain_max_sec: float = 10.0
    max_capacity_drops: int = 0
    max_rss_growth_mb: float = 200.0
    min_soak_duration_sec: float = 3600.0

    @classmethod
    def from_env(cls) -> "SLOThresholds":
        return cls(
            runtime_cycle_p95_ms=_env_float("TRADING_RELIABILITY_RUNTIME_CYCLE_P95_MS", 1500.0),
            dirty_evaluator_p95_ms=_env_float("TRADING_RELIABILITY_DIRTY_EVALUATOR_P95_MS", 500.0),
            order_event_p95_ms=_env_float("TRADING_RELIABILITY_ORDER_EVENT_P95_MS", 250.0),
            dashboard_read_p95_ms=_env_float("TRADING_RELIABILITY_DASHBOARD_READ_P95_MS", 100.0),
            dashboard_build_p95_ms=_env_float("TRADING_RELIABILITY_DASHBOARD_BUILD_P95_MS", 500.0),
            read_model_max_age_sec=_env_float("TRADING_RELIABILITY_READ_MODEL_MAX_AGE_SEC", 5.0),
            backlog_max_age_sec=_env_float("TRADING_RELIABILITY_BACKLOG_MAX_AGE_SEC", 5.0),
            replay_drain_max_sec=_env_float("TRADING_RELIABILITY_REPLAY_DRAIN_MAX_SEC", 10.0),
            max_capacity_drops=_env_int("TRADING_RELIABILITY_MAX_CAPACITY_DROPS", 0),
            max_rss_growth_mb=_env_float("TRADING_RELIABILITY_MAX_RSS_GROWTH_MB", 200.0),
            min_soak_duration_sec=_env_float("TRADING_RELIABILITY_MIN_SOAK_DURATION_SEC", 3600.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SLOEvaluationResult:
    status: QualificationStatus | str
    hard_gate_failures: list[dict[str, Any]] = field(default_factory=list)
    operational_failures: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sample_status: str = "OK"
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value if isinstance(self.status, QualificationStatus) else str(self.status)
        return payload


@dataclass
class ReliabilityReport:
    run_id: str
    profile: QualificationProfile | str
    status: QualificationStatus | str
    recommendation: QualificationRecommendation | str
    started_at: str
    finished_at: str
    duration_sec: float
    config: dict[str, Any]
    scenarios: list[QualificationScenarioResult] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    slo_result: dict[str, Any] = field(default_factory=dict)
    hard_gate_failures: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    not_run: list[str] = field(default_factory=list)
    report_dir: str = ""
    transport: dict[str, Any] = field(default_factory=dict)
    deterministic_replay: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "profile": self.profile.value if isinstance(self.profile, QualificationProfile) else str(self.profile),
            "status": self.status.value if isinstance(self.status, QualificationStatus) else str(self.status),
            "recommendation": self.recommendation.value if isinstance(self.recommendation, QualificationRecommendation) else str(self.recommendation),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_sec": self.duration_sec,
            "config": dict(self.config),
            "scenarios": [item.to_dict() for item in self.scenarios],
            "metrics": dict(self.metrics),
            "slo_result": dict(self.slo_result),
            "hard_gate_failures": list(self.hard_gate_failures),
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "not_run": list(self.not_run),
            "report_dir": self.report_dir,
            "transport": dict(self.transport),
            "deterministic_replay": dict(self.deterministic_replay),
        }


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        return float(default)
