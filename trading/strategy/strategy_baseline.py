from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


STRATEGY_BASELINE_SCHEMA_VERSION = "strategy_baseline.freeze.v1"
STRATEGY_BASELINE_ID = "leader_first_pullback_v1"
STRATEGY_BASELINE_VERSION = "1.0.0"
STRATEGY_BASELINE_STATUS = "FROZEN"
STRATEGY_BASELINE_RUNTIME_PROFILE = "THEME_CORE_V3"
STRATEGY_BASELINE_CHAMPION_SETUP = "LEADER_FIRST_PULLBACK"
STRATEGY_BASELINE_CHALLENGER_SETUPS = ("VWAP_RECLAIM", "BREAKOUT_RETEST")
STRATEGY_BASELINE_OPERATION_MODE = "OBSERVE_ONLY"
STRATEGY_BASELINE_CONFIG_COMPLETE = "COMPLETE"
STRATEGY_BASELINE_CONFIG_PARTIAL = "PARTIAL"
STRATEGY_BASELINE_DRIFT_CLEAN = "CLEAN"
STRATEGY_BASELINE_DRIFT_DETECTED = "DRIFT_DETECTED"
STRATEGY_BASELINE_DRIFT_PARTIAL = "PARTIAL"
STRATEGY_BASELINE_DRIFT_UNKNOWN = "UNKNOWN"

SENSITIVE_KEY_PARTS = (
    "account",
    "token",
    "password",
    "secret",
    "absolute_path",
    "path",
    "pid",
    "request_id",
    "db_connection",
    "user",
)

RUNTIME_SETTINGS_ALLOWLIST = (
    "data_readiness",
    "entry_risk_gate",
    "market_side_gate",
    "market_side_breadth",
    "market_side_gate_confirmation",
    "risk_off_entry",
    "data_quality_early_small",
    "theme_lab_realtime_reliability_gate",
    "shadow_small_entry_promotion",
    "shadow_small_entry_ops",
    "live_sim_hybrid_ready_canary",
    "live_sim_exit_guard",
    "live_sim_order_lifecycle",
    "live_sim_reconcile",
    "order_execution",
    "entry_plan_thresholds",
    "exit_policy_thresholds",
    "session_profile_thresholds",
    "fill_model_thresholds",
)

CORE_SETTINGS_ALLOWLIST = (
    "mode",
    "allow_live",
    "max_order_amount",
    "max_daily_orders_per_code",
    "runtime_enabled",
    "runtime_auto_start",
    "runtime_mode",
    "runtime_evaluation_interval_sec",
    "runtime_cycle_timeout_sec",
    "runtime_allow_dry_run_orders",
    "runtime_allow_live_orders",
    "runtime_require_gateway_heartbeat",
    "runtime_require_kiwoom_login",
    "runtime_require_orderable_for_order",
    "exit_context_risk_enabled",
    "threshold_ab_enable_apply",
    "shadow_strategy_observe_only",
    "shadow_strategy_allow_apply",
    "change_proposal_allow_auto_apply",
)

STRATEGY_FEATURE_FLAG_ALLOWLIST = (
    "STRATEGY_RUNTIME_PROFILE",
    "STRATEGY_REBOOT_V2_PROFILE",
    "STRATEGY_REBOOT_V2_ENABLED",
    "TRADING_OPENING_BURST_ENABLED",
    "TRADING_THEME_BOARD_ENABLED",
    "TRADING_THEME_CORE_V3_ENABLED",
    "TRADING_THEME_CORE_V3_USE_RUNTIME_MARKET_CONTEXT",
    "TRADING_THEME_CORE_V3_INGEST_CANDIDATES",
    "TRADING_THEME_EXPANSION_SUBSCRIPTIONS_ENABLED",
    "TRADING_STRATEGY_CONTEXT_V3_ENABLED",
    "TRADING_MARKET_REGIME_ENABLED",
    "TRADING_ENTRY_ENGINE_ENABLED",
    "TRADING_ENTRY_USE_STRATEGY_CONTEXT_V3",
    "TRADING_ENTRY_ALLOW_LEGACY_THEME_CONTEXT_FALLBACK",
    "TRADING_SETUP_ROUTER_V3_ENABLED",
    "TRADING_SETUP_ROUTER_V3_OBSERVE_ONLY",
    "TRADING_SETUP_ROUTER_V3_INTERVAL_SEC",
    "TRADING_SETUP_ROUTER_V3_MAX_CANDIDATES_PER_CYCLE",
    "TRADING_SETUP_ROUTER_V3_PERIODIC_RECONCILE_SEC",
    "TRADING_SETUP_ROUTER_V3_MIN_COMPLETED_1M_CANDLES",
    "TRADING_SETUP_ROUTER_V3_SAVE_HISTORY",
    "TRADING_SETUP_ROUTER_READINESS_P01_ENABLED",
    "TRADING_SETUP_ROUTER_ATOMIC_READINESS_COMPLETION_ENABLED",
    "TRADING_SETUP_ROUTER_CANONICAL_MARKET_ACTION_ENABLED",
    "TRADING_MARKET_DATA_SERVICE_ENABLED",
    "TRADING_MARKET_DATA_DIRTY_QUEUE_ENABLED",
    "TRADING_MARKET_DATA_BATCH_FLUSH_ENABLED",
    "TRADING_MARKET_DATA_MAX_TICK_AGE_SEC",
    "TRADING_MARKET_RS_SHADOW_ENABLED",
    "TRADING_MARKET_RS_OUTCOME_ENABLED",
    "TRADING_POSITION_RISK_ENABLED",
    "TRADING_EXIT_ENGINE_ENABLED",
    "TRADING_MARKET_SIDE_PORTFOLIO_ENABLED",
    "TRADING_MARKET_SIDE_PORTFOLIO_OBSERVE_ONLY",
    "TRADING_MARKET_SIDE_PORTFOLIO_ENFORCE_BUY_LIMITS",
    "TRADING_ORDER_MANAGER_ENABLED",
    "TRADING_ORDER_MANAGER_OBSERVE_ONLY",
    "TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND",
    "TRADING_ORDER_INTENT_ENABLED",
    "TRADING_SEND_ORDER_ALLOWED",
    "TRADING_ALLOW_LIVE_SIM_ORDERS",
    "TRADING_ENTRY_ALLOW_DRY_RUN_INTENTS",
    "TRADING_EXIT_ALLOW_DRY_RUN_SELL_INTENTS",
    "TRADING_CHANGE_PROPOSAL_ALLOW_AUTO_APPLY",
    "TRADING_SHADOW_STRATEGY_ALLOW_APPLY",
)


@dataclass(frozen=True)
class GitInfo:
    git_sha: str = "UNKNOWN"
    git_dirty_or_unknown: bool = True


@dataclass(frozen=True)
class StrategyBaselineRuntimeConfig:
    enabled: bool = False
    baseline_id: str = STRATEGY_BASELINE_ID
    baseline_version: str = STRATEGY_BASELINE_VERSION
    freeze_enabled: bool = True
    allow_mutation: bool = False

    @classmethod
    def from_env(cls) -> "StrategyBaselineRuntimeConfig":
        return cls(
            enabled=_bool_env("TRADING_STRATEGY_BASELINE_ENABLED", False),
            baseline_id=str(os.getenv("TRADING_STRATEGY_BASELINE_ID", STRATEGY_BASELINE_ID) or STRATEGY_BASELINE_ID),
            baseline_version=str(
                os.getenv("TRADING_STRATEGY_BASELINE_VERSION", STRATEGY_BASELINE_VERSION)
                or STRATEGY_BASELINE_VERSION
            ),
            freeze_enabled=_bool_env("TRADING_STRATEGY_BASELINE_FREEZE_ENABLED", True),
            allow_mutation=_bool_env("TRADING_STRATEGY_BASELINE_ALLOW_MUTATION", False),
        )


class StrategyBaselineService:
    def __init__(
        self,
        *,
        db: Any,
        runtime_profile: str,
        config_snapshot_provider: Callable[[], tuple[dict[str, Any], list[str]]],
        config: StrategyBaselineRuntimeConfig | None = None,
        git_info_provider: Callable[[], GitInfo] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        self.runtime_profile = str(runtime_profile or "")
        self.config_snapshot_provider = config_snapshot_provider
        self.config = config or StrategyBaselineRuntimeConfig.from_env()
        self.git_info_provider = git_info_provider or current_git_info
        self.clock = clock or datetime.now
        self.session_id = ""
        self.runtime_started_at = ""
        self.last_result: dict[str, Any] = self.disabled_snapshot(self.clock().replace(microsecond=0))

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled and self.config.freeze_enabled)

    def disabled_snapshot(self, now: datetime) -> dict[str, Any]:
        return {
            "enabled": False,
            "status": "DISABLED",
            "baseline_id": self.config.baseline_id,
            "version": self.config.baseline_version,
            "baseline_version": self.config.baseline_version,
            "champion_setup": STRATEGY_BASELINE_CHAMPION_SETUP,
            "challenger_setups": list(STRATEGY_BASELINE_CHALLENGER_SETUPS),
            "challenger_count": len(STRATEGY_BASELINE_CHALLENGER_SETUPS),
            "config_hash_short": "",
            "git_sha_short": "",
            "drift_status": STRATEGY_BASELINE_DRIFT_UNKNOWN,
            "config_snapshot_completeness": STRATEGY_BASELINE_CONFIG_PARTIAL,
            "order_intent_allowed": False,
            "live_order_allowed": False,
            "checked_at": now.isoformat(),
        }

    def check(
        self,
        *,
        now: datetime | None = None,
        runtime_started_at: str = "",
        runtime_cycle_count: int = 0,
    ) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        if not self.enabled:
            self.last_result = self.disabled_snapshot(current)
            return dict(self.last_result)
        if runtime_started_at:
            self.runtime_started_at = runtime_started_at
        elif not self.runtime_started_at:
            self.runtime_started_at = current.isoformat()
        try:
            config_snapshot, missing_paths = self.config_snapshot_provider()
            config_snapshot = sanitize_config(config_snapshot)
            missing_paths = _dedupe(missing_paths)
            completeness = (
                STRATEGY_BASELINE_CONFIG_PARTIAL
                if missing_paths
                else STRATEGY_BASELINE_CONFIG_COMPLETE
            )
            config_hash_value = config_hash(config_snapshot)
            git_info = self.git_info_provider()
            contract = self._contract(
                now=current,
                git_info=git_info,
                config_snapshot=config_snapshot,
                config_hash_value=config_hash_value,
                completeness=completeness,
                missing_paths=missing_paths,
            )
            definition = self._ensure_definition(contract)
            drift_status, drift_paths, warning_codes = self._drift(
                definition,
                config_snapshot=config_snapshot,
                config_hash_value=config_hash_value,
                completeness=completeness,
            )
            payload = {
                **contract,
                "drift_status": drift_status,
                "drift_paths": drift_paths,
                "warning_codes": _dedupe([*contract["warning_codes"], *warning_codes]),
                "checked_at": current.isoformat(),
                "session_id": self._session_id(
                    trade_date=current.date().isoformat(),
                    runtime_started_at=self.runtime_started_at,
                    git_sha=git_info.git_sha,
                    config_hash_value=config_hash_value,
                ),
            }
            self.session_id = payload["session_id"]
            self._save_session(payload, runtime_cycle_count=runtime_cycle_count)
            self.last_result = self._runtime_section(payload)
            return dict(self.last_result)
        except Exception as exc:
            self.last_result = {
                **self.disabled_snapshot(current),
                "enabled": True,
                "status": STRATEGY_BASELINE_STATUS,
                "drift_status": STRATEGY_BASELINE_DRIFT_UNKNOWN,
                "warning_codes": [f"STRATEGY_BASELINE_CHECK_FAILED:{exc.__class__.__name__}"],
                "blocking_reason": str(exc),
            }
            return dict(self.last_result)

    def _contract(
        self,
        *,
        now: datetime,
        git_info: GitInfo,
        config_snapshot: dict[str, Any],
        config_hash_value: str,
        completeness: str,
        missing_paths: list[str],
    ) -> dict[str, Any]:
        warnings: list[str] = []
        if self.config.allow_mutation:
            warnings.append("STRATEGY_BASELINE_MUTATION_FLAG_TRUE")
        if self.runtime_profile and self.runtime_profile != STRATEGY_BASELINE_RUNTIME_PROFILE:
            warnings.append("STRATEGY_BASELINE_RUNTIME_PROFILE_MISMATCH")
        if missing_paths:
            warnings.append("STRATEGY_BASELINE_CONFIG_SNAPSHOT_PARTIAL")
        return {
            "schema_version": STRATEGY_BASELINE_SCHEMA_VERSION,
            "baseline_id": self.config.baseline_id,
            "baseline_version": self.config.baseline_version,
            "baseline_status": STRATEGY_BASELINE_STATUS,
            "status": STRATEGY_BASELINE_STATUS,
            "runtime_profile": self.runtime_profile or STRATEGY_BASELINE_RUNTIME_PROFILE,
            "champion_setup": STRATEGY_BASELINE_CHAMPION_SETUP,
            "challenger_setups": list(STRATEGY_BASELINE_CHALLENGER_SETUPS),
            "champion_operation_mode": STRATEGY_BASELINE_OPERATION_MODE,
            "challenger_operation_mode": STRATEGY_BASELINE_OPERATION_MODE,
            "strategy_mutation_allowed": False,
            "legacy_decision_usage_allowed": False,
            "order_intent_allowed": False,
            "live_order_allowed": False,
            "effective_trade_date": now.date().isoformat(),
            "created_at": now.isoformat(),
            "activated_at": now.isoformat(),
            "git_sha": git_info.git_sha,
            "git_dirty_or_unknown": bool(git_info.git_dirty_or_unknown),
            "config_hash": config_hash_value,
            "config_snapshot": config_snapshot,
            "config_snapshot_completeness": completeness,
            "missing_config_paths": missing_paths,
            "drift_status": STRATEGY_BASELINE_DRIFT_UNKNOWN,
            "drift_paths": [],
            "warning_codes": _dedupe(warnings),
        }

    def _ensure_definition(self, contract: Mapping[str, Any]) -> dict[str, Any]:
        loader = getattr(self.db, "load_strategy_baseline_definition", None)
        saver = getattr(self.db, "save_strategy_baseline_definition", None)
        if callable(saver):
            result = dict(saver(dict(contract)) or {})
            existing = dict(result.get("definition") or {})
            if existing:
                return existing
        if callable(loader):
            existing = loader(self.config.baseline_id, self.config.baseline_version)
            if existing:
                return dict(existing)
        return dict(contract)

    def _drift(
        self,
        definition: Mapping[str, Any],
        *,
        config_snapshot: dict[str, Any],
        config_hash_value: str,
        completeness: str,
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        warnings: list[str] = []
        if completeness == STRATEGY_BASELINE_CONFIG_PARTIAL:
            warnings.append("STRATEGY_BASELINE_CONFIG_SNAPSHOT_PARTIAL")
            return STRATEGY_BASELINE_DRIFT_PARTIAL, [], warnings
        baseline_hash = str(definition.get("config_hash") or "")
        baseline_config = definition.get("config_snapshot")
        if baseline_config is None:
            baseline_config = definition.get("canonical_config")
        if baseline_config is None:
            baseline_config = definition.get("canonical_config_json")
        if isinstance(baseline_config, str):
            try:
                baseline_config = json.loads(baseline_config)
            except json.JSONDecodeError:
                baseline_config = {}
        if not baseline_hash and baseline_config:
            baseline_hash = config_hash(baseline_config)
        if not baseline_hash:
            warnings.append("STRATEGY_BASELINE_DEFINITION_HASH_MISSING")
            return STRATEGY_BASELINE_DRIFT_UNKNOWN, [], warnings
        if baseline_hash == config_hash_value:
            return STRATEGY_BASELINE_DRIFT_CLEAN, [], warnings
        paths = diff_config_paths(baseline_config if isinstance(baseline_config, Mapping) else {}, config_snapshot)
        return STRATEGY_BASELINE_DRIFT_DETECTED, paths, warnings

    def _session_id(
        self,
        *,
        trade_date: str,
        runtime_started_at: str,
        git_sha: str,
        config_hash_value: str,
    ) -> str:
        material = "|".join(
            [
                self.config.baseline_id,
                self.config.baseline_version,
                trade_date,
                runtime_started_at,
                git_sha,
                config_hash_value,
            ]
        )
        return hashlib.sha1(material.encode("utf-8")).hexdigest()[:24]

    def _save_session(self, payload: Mapping[str, Any], *, runtime_cycle_count: int) -> None:
        saver = getattr(self.db, "upsert_strategy_baseline_session", None)
        if not callable(saver):
            return
        saver(
            {
                "session_id": payload.get("session_id"),
                "trade_date": payload.get("effective_trade_date"),
                "runtime_started_at": self.runtime_started_at,
                "runtime_profile": payload.get("runtime_profile"),
                "baseline_id": payload.get("baseline_id"),
                "baseline_version": payload.get("baseline_version"),
                "config_hash": payload.get("config_hash"),
                "git_sha": payload.get("git_sha"),
                "operation_mode": payload.get("champion_operation_mode"),
                "drift_status": payload.get("drift_status"),
                "drift_paths": payload.get("drift_paths") or [],
                "last_checked_at": payload.get("checked_at"),
                "runtime_cycle_count": int(runtime_cycle_count or 0),
                "payload": dict(payload),
            }
        )

    def _runtime_section(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        config_hash_value = str(payload.get("config_hash") or "")
        git_sha = str(payload.get("git_sha") or "")
        return {
            "enabled": True,
            "status": payload.get("baseline_status") or STRATEGY_BASELINE_STATUS,
            "baseline_id": payload.get("baseline_id") or self.config.baseline_id,
            "version": payload.get("baseline_version") or self.config.baseline_version,
            "baseline_version": payload.get("baseline_version") or self.config.baseline_version,
            "champion_setup": payload.get("champion_setup") or STRATEGY_BASELINE_CHAMPION_SETUP,
            "challenger_setups": list(payload.get("challenger_setups") or STRATEGY_BASELINE_CHALLENGER_SETUPS),
            "challenger_count": len(list(payload.get("challenger_setups") or STRATEGY_BASELINE_CHALLENGER_SETUPS)),
            "config_hash": config_hash_value,
            "config_hash_short": config_hash_value[:12],
            "git_sha": git_sha,
            "git_sha_short": git_sha[:8] if git_sha and git_sha != "UNKNOWN" else git_sha,
            "git_dirty_or_unknown": bool(payload.get("git_dirty_or_unknown")),
            "drift_status": payload.get("drift_status") or STRATEGY_BASELINE_DRIFT_UNKNOWN,
            "drift_paths": list(payload.get("drift_paths") or []),
            "config_snapshot_completeness": payload.get("config_snapshot_completeness")
            or STRATEGY_BASELINE_CONFIG_PARTIAL,
            "missing_config_paths": list(payload.get("missing_config_paths") or []),
            "order_intent_allowed": False,
            "live_order_allowed": False,
            "strategy_mutation_allowed": False,
            "legacy_decision_usage_allowed": False,
            "champion_operation_mode": STRATEGY_BASELINE_OPERATION_MODE,
            "challenger_operation_mode": STRATEGY_BASELINE_OPERATION_MODE,
            "baseline_frozen": True,
            "checked_at": payload.get("checked_at") or "",
            "session_id": payload.get("session_id") or "",
            "warning_codes": list(payload.get("warning_codes") or []),
        }


def build_strategy_baseline_snapshot(
    *,
    runtime_profile: str,
    runtime_config: Any = None,
    runtime_settings: Any = None,
    setup_router_config: Any = None,
    entry_engine_config: Any = None,
    market_regime_config: Any = None,
    theme_core_v3_config: Any = None,
    market_data_config: Any = None,
    position_risk_config: Any = None,
    exit_engine_config: Any = None,
    order_manager_config: Any = None,
    core_settings: Any = None,
    feature_flags: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    missing: list[str] = []
    snapshot = {
        "runtime_profile": str(runtime_profile or ""),
        "setup_router_v3": _required_config("setup_router_v3", setup_router_config, missing),
        "entry_engine": _required_config("entry_engine", entry_engine_config, missing),
        "market_regime": _required_config("market_regime", market_regime_config, missing),
        "theme_core_v3": _required_config("theme_core_v3", theme_core_v3_config, missing),
        "market_data": _required_config("market_data", market_data_config, missing),
        "position_risk": _required_config("position_risk", position_risk_config, missing),
        "exit_engine": _required_config("exit_engine", exit_engine_config, missing),
        "order_manager": _required_config("order_manager", order_manager_config, missing),
        "strategy_runtime": _required_config("strategy_runtime", runtime_config, missing),
        "runtime_settings": _runtime_settings_snapshot(runtime_settings, missing),
        "core_settings": _allowlisted_object(core_settings, CORE_SETTINGS_ALLOWLIST),
        "feature_flags": dict(feature_flags if feature_flags is not None else strategy_feature_flags()),
        "baseline_contract": {
            "champion_setup": STRATEGY_BASELINE_CHAMPION_SETUP,
            "challenger_setups": list(STRATEGY_BASELINE_CHALLENGER_SETUPS),
            "champion_operation_mode": STRATEGY_BASELINE_OPERATION_MODE,
            "challenger_operation_mode": STRATEGY_BASELINE_OPERATION_MODE,
            "strategy_mutation_allowed": False,
            "legacy_decision_usage_allowed": False,
            "order_intent_allowed": False,
            "live_order_allowed": False,
        },
    }
    return sanitize_config(snapshot), _dedupe(missing)


def strategy_feature_flags() -> dict[str, str]:
    return {name: str(os.getenv(name, "")) for name in STRATEGY_FEATURE_FLAG_ALLOWLIST}


def strategy_baseline_role(setup_type: Any) -> str:
    text = _enum_value(setup_type).upper()
    if text == STRATEGY_BASELINE_CHAMPION_SETUP:
        return "CHAMPION"
    if text in STRATEGY_BASELINE_CHALLENGER_SETUPS:
        return "CHALLENGER"
    return "OUT_OF_SCOPE"


def baseline_observation_metadata(
    setup_type: Any,
    *,
    baseline_id: str = STRATEGY_BASELINE_ID,
    baseline_version: str = STRATEGY_BASELINE_VERSION,
    baseline_config_hash: str = "",
) -> dict[str, Any]:
    return {
        "baseline_role": strategy_baseline_role(setup_type),
        "baseline_id": baseline_id,
        "baseline_version": baseline_version,
        "baseline_config_hash": str(baseline_config_hash or ""),
        "baseline_frozen": True,
    }


def apply_baseline_metadata_to_observations(
    observations: Iterable[Mapping[str, Any]],
    baseline_section: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    section = dict(baseline_section or {})
    if not section or section.get("enabled") is False:
        return [dict(item or {}) for item in observations or []]
    baseline_id = str(section.get("baseline_id") or STRATEGY_BASELINE_ID)
    baseline_version = str(section.get("baseline_version") or section.get("version") or STRATEGY_BASELINE_VERSION)
    baseline_config_hash = str(section.get("config_hash") or "")
    result = []
    for item in observations or []:
        payload = dict(item or {})
        payload.update(
            baseline_observation_metadata(
                payload.get("setup_type"),
                baseline_id=baseline_id,
                baseline_version=baseline_version,
                baseline_config_hash=baseline_config_hash,
            )
        )
        result.append(payload)
    return result


def sanitize_config(value: Any) -> Any:
    return _sanitize(value)


def canonical_config_json(value: Any) -> str:
    return json.dumps(sanitize_config(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def config_hash(value: Any) -> str:
    return hashlib.sha256(canonical_config_json(value).encode("utf-8")).hexdigest()


def diff_config_paths(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    detected_at: str = "",
    runtime_cycle: int | None = None,
    current_git_sha: str = "",
    baseline_git_sha: str = "",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def visit(path: str, left: Any, right: Any) -> None:
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            for key in sorted(set(left) | set(right)):
                visit(f"{path}.{key}" if path else str(key), left.get(key), right.get(key))
            return
        if isinstance(left, list) and isinstance(right, list):
            if left == right:
                return
        if left != right:
            item = {
                "path": path,
                "baseline_value": _jsonable(left),
                "current_value": _jsonable(right),
            }
            if detected_at:
                item["detected_at"] = detected_at
            if runtime_cycle is not None:
                item["runtime_cycle"] = int(runtime_cycle)
            if current_git_sha:
                item["current_git_sha"] = current_git_sha
            if baseline_git_sha:
                item["baseline_git_sha"] = baseline_git_sha
            rows.append(item)

    visit("", baseline, current)
    return rows


def current_git_info() -> GitInfo:
    root = Path(__file__).resolve().parents[2]
    try:
        sha = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return GitInfo(git_sha=sha or "UNKNOWN", git_dirty_or_unknown=bool(status.strip()))
    except Exception:
        return GitInfo()


def _required_config(name: str, value: Any, missing: list[str]) -> Any:
    if value is None:
        missing.append(name)
        return None
    return _jsonable(value)


def _runtime_settings_snapshot(runtime_settings: Any, missing: list[str]) -> dict[str, Any] | None:
    if runtime_settings is None:
        missing.append("runtime_settings")
        return None
    result = {
        "profile_name": str(getattr(runtime_settings, "profile_name", "") or ""),
        "profile_version": str(getattr(runtime_settings, "profile_version", "") or ""),
        "mode": str(getattr(runtime_settings, "mode", "") or ""),
        "loaded_from": str(getattr(runtime_settings, "loaded_from", "") or ""),
        "fallback_used": bool(getattr(runtime_settings, "fallback_used", False)),
        "validation_warnings": list(getattr(runtime_settings, "validation_warnings", []) or []),
        "settings": {},
    }
    getter = getattr(runtime_settings, "value", None)
    settings_json = getattr(runtime_settings, "settings_json", None)
    for path in RUNTIME_SETTINGS_ALLOWLIST:
        if callable(getter):
            value = getter(path, None)
        elif isinstance(settings_json, Mapping):
            value = settings_json.get(path)
        else:
            value = None
        if value is None:
            missing.append(f"runtime_settings.{path}")
            continue
        result["settings"][path] = _jsonable(value)
    return result


def _allowlisted_object(value: Any, keys: Iterable[str]) -> dict[str, Any]:
    if value is None:
        return {}
    data = _jsonable(value)
    if not isinstance(data, Mapping):
        return {}
    return {key: data.get(key) for key in keys if key in data}


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if _sensitive_key(text_key):
                continue
            result[text_key] = _sanitize(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return _jsonable(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return ""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dict__") and not isinstance(value, (str, bytes)):
        try:
            names = [field.name for field in fields(value)]
        except TypeError:
            names = []
        if names:
            return {name: _jsonable(getattr(value, name)) for name in names}
    return value


def _enum_value(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value or "")


def _sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}
