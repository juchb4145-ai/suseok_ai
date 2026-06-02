from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from trading.strategy.candidates import normalize_code
from trading.strategy.models import FillPolicy, OrderMode
from trading.strategy.runtime import StrategyRuntimeConfig


DEFAULT_CONFIG_KEY = "observe_runtime"
CONFIG_VERSION = 1
ALLOWED_INDEX_CODES = {"KOSPI", "KOSDAQ"}
STOCK_LIST_FIELDS = {
    "leader_watch_codes",
    "semiconductor_signal_codes",
    "holding_watch_codes",
}
CONFIG_FIELDS = {
    "order_mode",
    "evaluation_interval_sec",
    "condition_profiles_enabled",
    "index_watch_codes",
    "leader_watch_codes",
    "semiconductor_signal_codes",
    "holding_watch_codes",
    "virtual_fill_policy",
    "review_save_enabled",
    "max_candidates_to_watch",
    "realtime_subscription_limit",
    "theme_engine_mode",
    "theme_lab_dry_run_bridge_enabled",
    "theme_lab_pipeline_interval_sec",
    "theme_lab_condition_names",
    "theme_lab_condition_purposes",
    "exit_context_risk_enabled",
}


@dataclass
class RuntimeConfigLoadResult:
    config: StrategyRuntimeConfig
    warnings: list[str]
    config_key: str = DEFAULT_CONFIG_KEY
    config_version: int = CONFIG_VERSION
    used_default: bool = False


@dataclass
class RuntimeConfigSaveResult:
    config: StrategyRuntimeConfig
    warnings: list[str]
    config_key: str = DEFAULT_CONFIG_KEY
    config_version: int = CONFIG_VERSION


class StrategyRuntimeConfigRepository:
    def __init__(
        self,
        db,
        *,
        config_key: str = DEFAULT_CONFIG_KEY,
        config_version: int = CONFIG_VERSION,
    ) -> None:
        self.db = db
        self.config_key = config_key
        self.config_version = config_version

    def load(self) -> RuntimeConfigLoadResult:
        try:
            row = self.db.load_strategy_runtime_setting(self.config_key)
        except Exception as exc:
            return RuntimeConfigLoadResult(
                config=StrategyRuntimeConfig(),
                warnings=[f"CONFIG_DB_READ_FAILED:{exc}"],
                config_key=self.config_key,
                config_version=self.config_version,
                used_default=True,
            )
        if row is None:
            config = StrategyRuntimeConfig()
            return RuntimeConfigLoadResult(
                config=config,
                warnings=[],
                config_key=self.config_key,
                config_version=self.config_version,
                used_default=True,
            )
        warnings: list[str] = []
        try:
            raw = json.loads(row.get("config_json") or "{}")
            if not isinstance(raw, dict):
                raise ValueError("config_json must be an object")
        except Exception as exc:
            return RuntimeConfigLoadResult(
                config=StrategyRuntimeConfig(),
                warnings=[f"CONFIG_JSON_INVALID:{exc}"],
                config_key=self.config_key,
                config_version=int(row.get("config_version") or self.config_version),
                used_default=True,
            )
        unknown = sorted(set(raw) - CONFIG_FIELDS)
        warnings.extend(f"CONFIG_UNKNOWN_FIELD_IGNORED:{name}" for name in unknown)
        try:
            config = config_from_dict(raw)
            warnings.extend(_validate_and_normalize(config))
        except Exception as exc:
            return RuntimeConfigLoadResult(
                config=StrategyRuntimeConfig(),
                warnings=warnings + [f"CONFIG_INVALID_FALLBACK:{exc}"],
                config_key=self.config_key,
                config_version=int(row.get("config_version") or self.config_version),
                used_default=True,
            )
        return RuntimeConfigLoadResult(
            config=config,
            warnings=warnings,
            config_key=self.config_key,
            config_version=int(row.get("config_version") or self.config_version),
            used_default=False,
        )

    def save(self, config: StrategyRuntimeConfig) -> RuntimeConfigSaveResult:
        warnings = _validate_and_normalize(config)
        payload = config_to_dict(config)
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.db.save_strategy_runtime_setting(
            self.config_key,
            self.config_version,
            serialized,
        )
        loaded = self.load()
        return RuntimeConfigSaveResult(
            config=loaded.config,
            warnings=warnings + loaded.warnings,
            config_key=self.config_key,
            config_version=self.config_version,
        )


def config_to_dict(config: StrategyRuntimeConfig) -> dict[str, Any]:
    return {
        "order_mode": _enum_value(config.order_mode),
        "evaluation_interval_sec": int(config.evaluation_interval_sec),
        "condition_profiles_enabled": bool(config.condition_profiles_enabled),
        "index_watch_codes": dict(config.index_watch_codes),
        "leader_watch_codes": list(config.leader_watch_codes),
        "semiconductor_signal_codes": list(config.semiconductor_signal_codes),
        "holding_watch_codes": list(config.holding_watch_codes),
        "virtual_fill_policy": _enum_value(config.virtual_fill_policy),
        "review_save_enabled": bool(config.review_save_enabled),
        "max_candidates_to_watch": int(config.max_candidates_to_watch),
        "realtime_subscription_limit": int(config.realtime_subscription_limit),
        "theme_engine_mode": str(config.theme_engine_mode),
        "theme_lab_dry_run_bridge_enabled": bool(config.theme_lab_dry_run_bridge_enabled),
        "theme_lab_pipeline_interval_sec": int(config.theme_lab_pipeline_interval_sec),
        "theme_lab_condition_names": dict(config.theme_lab_condition_names),
        "theme_lab_condition_purposes": dict(config.theme_lab_condition_purposes),
        "exit_context_risk_enabled": bool(config.exit_context_risk_enabled),
    }


def config_from_dict(raw: dict[str, Any]) -> StrategyRuntimeConfig:
    defaults = config_to_dict(StrategyRuntimeConfig())
    merged = dict(defaults)
    for key in CONFIG_FIELDS:
        if key in raw:
            merged[key] = raw[key]
    return StrategyRuntimeConfig(
        order_mode=_coerce_order_mode(merged["order_mode"]),
        evaluation_interval_sec=_coerce_int(merged["evaluation_interval_sec"], "evaluation_interval_sec"),
        condition_profiles_enabled=_coerce_bool(merged["condition_profiles_enabled"], "condition_profiles_enabled"),
        index_watch_codes=_coerce_index_watch_codes(merged["index_watch_codes"]),
        leader_watch_codes=_coerce_stock_codes(merged["leader_watch_codes"], "leader_watch_codes"),
        semiconductor_signal_codes=_coerce_stock_codes(
            merged["semiconductor_signal_codes"],
            "semiconductor_signal_codes",
        ),
        holding_watch_codes=_coerce_stock_codes(merged["holding_watch_codes"], "holding_watch_codes"),
        virtual_fill_policy=_coerce_fill_policy(merged["virtual_fill_policy"]),
        review_save_enabled=_coerce_bool(merged["review_save_enabled"], "review_save_enabled"),
        max_candidates_to_watch=_coerce_int(merged["max_candidates_to_watch"], "max_candidates_to_watch"),
        realtime_subscription_limit=_coerce_int(
            merged["realtime_subscription_limit"],
            "realtime_subscription_limit",
        ),
        theme_engine_mode=_coerce_theme_engine_mode(merged["theme_engine_mode"]),
        theme_lab_dry_run_bridge_enabled=_coerce_bool(
            merged["theme_lab_dry_run_bridge_enabled"],
            "theme_lab_dry_run_bridge_enabled",
        ),
        theme_lab_pipeline_interval_sec=_coerce_int(
            merged["theme_lab_pipeline_interval_sec"],
            "theme_lab_pipeline_interval_sec",
        ),
        theme_lab_condition_names=_coerce_theme_lab_condition_names(merged["theme_lab_condition_names"]),
        theme_lab_condition_purposes=_coerce_theme_lab_condition_purposes(merged["theme_lab_condition_purposes"]),
        exit_context_risk_enabled=_coerce_bool(
            merged["exit_context_risk_enabled"],
            "exit_context_risk_enabled",
        ),
    )


def _validate_and_normalize(config: StrategyRuntimeConfig) -> list[str]:
    warnings: list[str] = []
    config.order_mode = _coerce_order_mode(config.order_mode)
    config.virtual_fill_policy = _coerce_fill_policy(config.virtual_fill_policy)
    config.condition_profiles_enabled = _coerce_bool(
        config.condition_profiles_enabled,
        "condition_profiles_enabled",
    )
    config.review_save_enabled = _coerce_bool(config.review_save_enabled, "review_save_enabled")
    config.evaluation_interval_sec = _coerce_int(config.evaluation_interval_sec, "evaluation_interval_sec")
    config.max_candidates_to_watch = _coerce_int(config.max_candidates_to_watch, "max_candidates_to_watch")
    config.realtime_subscription_limit = _coerce_int(
        config.realtime_subscription_limit,
        "realtime_subscription_limit",
    )
    config.theme_engine_mode = _coerce_theme_engine_mode(config.theme_engine_mode)
    config.theme_lab_dry_run_bridge_enabled = _coerce_bool(
        config.theme_lab_dry_run_bridge_enabled,
        "theme_lab_dry_run_bridge_enabled",
    )
    config.theme_lab_pipeline_interval_sec = _coerce_int(
        config.theme_lab_pipeline_interval_sec,
        "theme_lab_pipeline_interval_sec",
    )
    config.theme_lab_condition_names = _coerce_theme_lab_condition_names(config.theme_lab_condition_names)
    config.theme_lab_condition_purposes = _coerce_theme_lab_condition_purposes(config.theme_lab_condition_purposes)
    config.exit_context_risk_enabled = _coerce_bool(
        config.exit_context_risk_enabled,
        "exit_context_risk_enabled",
    )
    if not 1 <= config.evaluation_interval_sec <= 3600:
        raise ValueError("evaluation_interval_sec must be between 1 and 3600")
    if config.max_candidates_to_watch < 0:
        raise ValueError("max_candidates_to_watch must be >= 0")
    if config.realtime_subscription_limit < 1:
        raise ValueError("realtime_subscription_limit must be >= 1")
    if not 1 <= config.theme_lab_pipeline_interval_sec <= 3600:
        raise ValueError("theme_lab_pipeline_interval_sec must be between 1 and 3600")
    config.index_watch_codes = _coerce_index_watch_codes(config.index_watch_codes)
    config.leader_watch_codes = _coerce_stock_codes(config.leader_watch_codes, "leader_watch_codes")
    config.semiconductor_signal_codes = _coerce_stock_codes(
        config.semiconductor_signal_codes,
        "semiconductor_signal_codes",
    )
    config.holding_watch_codes = _coerce_stock_codes(config.holding_watch_codes, "holding_watch_codes")
    warnings.extend(config.validate())
    protected_count = _protected_watch_count(config)
    if protected_count > config.realtime_subscription_limit:
        warnings.append("PROTECTED_WATCH_COUNT_EXCEEDS_REALTIME_LIMIT")
    return _dedupe(warnings)


def _protected_watch_count(config: StrategyRuntimeConfig) -> int:
    protected = set(config.index_watch_codes.values())
    protected.update(config.leader_watch_codes)
    protected.update(config.semiconductor_signal_codes)
    protected.update(config.holding_watch_codes)
    return len({code for code in protected if code})


def _coerce_order_mode(value: Any) -> OrderMode:
    mode = value if isinstance(value, OrderMode) else OrderMode(str(value))
    if mode != OrderMode.OBSERVE:
        raise ValueError("order_mode must be OBSERVE")
    return mode


def _coerce_fill_policy(value: Any) -> FillPolicy:
    policy = value if isinstance(value, FillPolicy) else FillPolicy(str(value))
    if policy not in {FillPolicy.OPTIMISTIC, FillPolicy.NORMAL, FillPolicy.CONSERVATIVE}:
        raise ValueError("virtual_fill_policy must be optimistic, normal, or conservative")
    return policy


def _coerce_bool(value: Any, field_name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be bool")
    return value


def _coerce_int(value: Any, field_name: str) -> int:
    if type(value) is bool:
        raise ValueError(f"{field_name} must be int")
    try:
        return int(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be int") from exc


def _coerce_index_watch_codes(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("index_watch_codes must be a mapping")
    result: dict[str, str] = {}
    for key, raw_code in value.items():
        logical = str(key or "").strip().upper()
        if logical not in ALLOWED_INDEX_CODES:
            raise ValueError("index_watch_codes only supports KOSPI/KOSDAQ")
        code = str(raw_code or "").strip()
        if not code:
            raise ValueError(f"index_watch_codes.{logical} must not be empty")
        result[logical] = code
    for logical, default_code in StrategyRuntimeConfig().index_watch_codes.items():
        result.setdefault(logical, default_code)
    return {logical: result[logical] for logical in sorted(result)}


def _coerce_stock_codes(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values = [part.strip() for part in value.replace("\n", ",").split(",")]
    elif isinstance(value, list):
        raw_values = value
    else:
        raise ValueError(f"{field_name} must be a list or comma-separated string")
    result: list[str] = []
    for raw in raw_values:
        text = str(raw or "").strip().upper()
        if not text:
            continue
        if text in ALLOWED_INDEX_CODES:
            raise ValueError(f"{field_name} cannot contain logical index code {text}")
        code = normalize_code(text)
        if not (len(code) == 6 and code.isdigit()):
            raise ValueError(f"{field_name} contains invalid stock code {text}")
        if code not in result:
            result.append(code)
    return result


def _coerce_theme_engine_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode not in {"legacy", "themelab_flow"}:
        raise ValueError("theme_engine_mode must be legacy or themelab_flow")
    return mode


def _coerce_theme_lab_condition_names(value: Any) -> dict[str, str]:
    return _coerce_theme_lab_mapping(
        value,
        "theme_lab_condition_names",
        {"alive": "테마랩_생존_-1", "strong": "테마랩_강세_3", "leader": "테마랩_주도_5"},
    )


def _coerce_theme_lab_condition_purposes(value: Any) -> dict[str, str]:
    return _coerce_theme_lab_mapping(
        value,
        "theme_lab_condition_purposes",
        {"alive": "theme_lab_alive", "strong": "theme_lab_strong", "leader": "theme_lab_leader"},
    )


def _coerce_theme_lab_mapping(value: Any, field_name: str, defaults: dict[str, str]) -> dict[str, str]:
    if value is None:
        return dict(defaults)
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a mapping")
    result = dict(defaults)
    for key, raw in value.items():
        logical = str(key or "").strip().lower()
        if logical not in defaults:
            raise ValueError(f"{field_name} only supports alive/strong/leader")
        text = str(raw or "").strip()
        if not text:
            raise ValueError(f"{field_name}.{logical} must not be empty")
        result[logical] = text
    return {key: result[key] for key in ("alive", "strong", "leader")}


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
