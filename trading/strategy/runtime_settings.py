from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from trading.strategy.reason_codes import ReasonCode, normalize_reason_codes


DEFAULT_STRATEGY_NAME = "OBSERVE_DREAMROAD"
DEFAULT_PROFILE_NAME = "legacy_default"
DEFAULT_PROFILE_VERSION = "v1"
DEFAULT_SETTINGS_MODE = "legacy"
DEFAULT_PROFILE_DESCRIPTION = "Legacy OBSERVE thresholds externalized without behavior change."
SETTINGS_CONFIG_KEY_PREFIX = "strategy_settings"
ALLOWED_SETTINGS_MODES = {"legacy", "candidate", "ab_test", "observe_only"}


def strategy_settings_config_key(
    strategy_name: str = DEFAULT_STRATEGY_NAME,
    profile_name: str = DEFAULT_PROFILE_NAME,
    profile_version: str = DEFAULT_PROFILE_VERSION,
) -> str:
    return f"{SETTINGS_CONFIG_KEY_PREFIX}:{strategy_name}:{profile_name}:{profile_version}"


LEGACY_DEFAULT_SETTINGS: dict[str, Any] = {
    "gate_weights": {
        "market": 0.15,
        "theme_strength": 0.30,
        "theme_pullback": 0.15,
        "stock_leadership": 0.20,
        "stock_pullback": 0.20,
    },
    "market_thresholds": {
        "data_insufficient_score": 0.0,
        "index_weak_score": 20.0,
        "pass_score_above_mid": 100.0,
        "pass_score_below_mid": 75.0,
        "recheck_after_sec": 60,
        "weak_directions": ["DOWN"],
        "pass_mid_positions": ["ABOVE_MID", "AT_MID"],
    },
    "theme_thresholds": {
        "grade_a_score": 75.0,
        "grade_b_plus_score": 70.0,
        "grade_b_score": 55.0,
        "valid_tick_ratio_c_min": 0.5,
        "valid_tick_ratio_full_min": 0.67,
        "min_active_count_for_a": 3,
        "min_valid_turnover_count_for_a": 2,
        "candidate_count_score_max": 20.0,
        "candidate_count_full_count": 3.0,
        "average_change_rate_score_max": 20.0,
        "average_change_rate_full_pct": 5.0,
        "rising_ratio_score_max": 20.0,
        "theme_turnover_score_max": 20.0,
        "leader_turnover_score_max": 20.0,
        "significant_rise_pct": 1.0,
        "theme_sync_weak_score": 50.0,
        "change_rate_cap_pct": 10.0,
        "leader_follower_gap_threshold_pct": 8.0,
    },
    "leadership_thresholds": {
        "turnover_rank_score_max": 35.0,
        "change_rate_rank_score_max": 25.0,
        "leader_candidate_points": 15.0,
        "base_priority_score_max": 15.0,
        "signal_or_large_cap_points": 10.0,
        "base_priority_max": 100.0,
        "leader_rank": 1,
        "second_leader_rank": 2,
        "follower_min_rank": 3,
        "leader_follower_gap_threshold_pct": 8.0,
        "persistence_rank_slot_points": 25.0,
        "persistence_rank_step_penalty": 10.0,
        "persistence_high_update_bonus": 10.0,
        "leader_replaced_rank_limit": 2,
        "leader_gap_trade_value_divisor": 10.0,
        "leader_gap_delay_min": 5.0,
        "leader_gap_delay_divisor": 3.0,
        "leader_gap_component_cap": 10.0,
    },
    "pullback_thresholds": {
        "kosdaq_range": [-5.0, -2.0],
        "kosdaq_shallow_range": [-2.0, -1.5],
        "kospi_range": [-3.0, -0.8],
        "kosdaq_support_near_pct": 2.5,
        "kospi_support_near_pct": 1.5,
        "support_dedupe_pct": 0.25,
        "theme_leader_collapse_pct": -7.0,
        "theme_pullback_data_insufficient_score": 30.0,
        "theme_pullback_wait_score": 55.0,
        "stock_support_missing_score": 35.0,
        "stock_pullback_wait_score": 55.0,
        "kosdaq_strong_leader_shallow_range": [-4.0, -1.2],
        "kospi_strong_leader_shallow_range": [-2.5, -0.5],
        "kosdaq_weak_or_late_deep_range": [-6.5, -3.0],
        "kospi_weak_or_late_deep_range": [-4.0, -1.5],
        "high_volatility_pct": 2.5,
        "low_volatility_pct": 1.0,
        "high_volatility_low_adjust": -1.0,
        "high_volatility_high_adjust": -0.5,
        "low_volatility_low_adjust": 0.5,
        "low_volatility_high_adjust": 0.3,
        "volume_reaccel_ratio": 1.2,
        "volume_deceleration_ratio": 0.8,
        "chase_risk_within_high_pct": 0.5,
        "low_break_lookback": 3,
        "large_candle_body_pct": 2.0,
        "large_candle_close_position": 0.75,
    },
    "late_chase_thresholds": {
        "near_session_high_pct": 0.5,
        "warning_score": 25.0,
        "soft_block_score": 80.0,
        "score_near_session_high": 20.0,
        "score_support_distance_excessive": 25.0,
        "score_volume_deceleration": 20.0,
        "score_after_large_candle": 20.0,
        "score_no_volume_reacceleration": 15.0,
        "reaccel_near_high_cap_score": 20.0,
    },
    "entry_plan_thresholds": {
        "max_chase_pct": {
            "kosdaq": 0.7,
            "kospi": 0.4,
            "semiconductor_signal": 0.4,
        },
        "order_timeout_sec": {
            "kosdaq": 300,
            "kospi": 180,
            "semiconductor_signal": 180,
        },
        "split_weights": {
            "A": [40, 30, 30],
            "A_SIGNAL": [40, 30, 30],
            "B_PLUS": [50, 30, 20],
            "B_PLUS_SIGNAL": [50, 30, 20],
            "default": [60, 25, 15],
        },
        "tick_offset": 1,
        "max_retries": 0,
    },
    "exit_policy_thresholds": {
        "kosdaq": {
            "take_profit_pct": 5.0,
            "take_profit_exit_percent": 70,
            "max_hold_minutes": 40,
            "min_expected_return_pct": 1.0,
        },
        "kospi": {
            "take_profit_pct": 3.0,
            "take_profit_exit_percent": 70,
            "max_hold_minutes": 60,
            "min_expected_return_pct": 0.6,
        },
        "semiconductor_signal": {
            "take_profit_pct": 3.0,
            "take_profit_exit_percent": 70,
            "max_hold_minutes": 60,
            "min_expected_return_pct": 0.6,
        },
        "support_loss_consecutive_closes_below": 2,
        "trailing_recent_low_window": 3,
        "support_dedupe_pct": 0.25,
        "recent_high_failure_window": 3,
    },
    "session_profile_thresholds": {
        "buckets": {
            "OPEN_0_10": {"start": "09:00", "end": "09:10", "entry_allowed": True},
            "OPEN_10_90": {"start": "09:10", "end": "10:30", "entry_allowed": True},
            "MIDDAY": {"start": "10:30", "end": "13:30", "entry_allowed": True},
            "LATE": {"start": "13:30", "end": "", "entry_allowed": True},
        },
        "restricted_reason_code": "SESSION_PROFILE_RESTRICTED",
    },
    "fill_model_thresholds": {
        "confidence_high": 75.0,
        "confidence_medium": 50.0,
        "min_candle_volume": 100,
        "strong_candle_volume": 300,
        "min_trade_value": 1_000_000.0,
        "strong_trade_value": 3_000_000.0,
        "spread_risk_ticks": 3,
        "execution_strength_weak": 80.0,
        "execution_strength_healthy": 90.0,
        "execution_strength_strong": 120.0,
        "close_position_risk": 0.35,
        "close_position_healthy": 0.55,
        "score_touched": 55.0,
        "score_not_touched": 10.0,
        "score_missing_penalty": -5.0,
        "score_strong_volume_bonus": 15.0,
        "score_min_volume_bonus": 8.0,
        "score_weak_volume_penalty": -20.0,
        "score_strong_trade_value_bonus": 10.0,
        "score_min_trade_value_bonus": 5.0,
        "score_weak_trade_value_penalty": -10.0,
        "score_tight_spread_bonus": 10.0,
        "score_wide_spread_penalty": -20.0,
        "score_normal_spread_penalty": -8.0,
        "score_strong_execution_bonus": 10.0,
        "score_healthy_execution_bonus": 5.0,
        "score_weak_execution_penalty": -15.0,
        "score_soft_execution_penalty": -5.0,
        "score_healthy_close_bonus": 10.0,
        "score_weak_close_penalty": -10.0,
        "score_neutral_close_bonus": 3.0,
        "score_pending_missing_penalty": -5.0,
        "liquidity_risk_confidence_cap": 45.0,
    },
    "review_label_thresholds": {
        "false_negative_rally_threshold_pct": 3.0,
        "false_positive_drawdown_threshold_pct": -3.0,
        "partial_take_profit_default_exit_percent": 70.0,
        "horizon_minutes": [5, 10, 20],
    },
}


@dataclass
class StrategyRuntimeSettings:
    strategy_name: str = DEFAULT_STRATEGY_NAME
    profile_name: str = DEFAULT_PROFILE_NAME
    profile_version: str = DEFAULT_PROFILE_VERSION
    mode: str = DEFAULT_SETTINGS_MODE
    enabled: bool = True
    effective_from: str = ""
    effective_to: str = ""
    settings_json: dict[str, Any] = field(default_factory=lambda: deepcopy(LEGACY_DEFAULT_SETTINGS))
    created_at: str = ""
    updated_at: str = ""
    description: str = DEFAULT_PROFILE_DESCRIPTION
    loaded_from: str = "legacy_default"
    fallback_used: bool = False
    missing_keys: list[str] = field(default_factory=list)
    invalid_keys: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)

    @classmethod
    def legacy_default(
        cls,
        *,
        loaded_from: str = "legacy_default",
        fallback_used: bool = False,
        missing_keys: Optional[list[str]] = None,
        invalid_keys: Optional[list[str]] = None,
        validation_warnings: Optional[list[str]] = None,
    ) -> "StrategyRuntimeSettings":
        return cls(
            settings_json=deepcopy(LEGACY_DEFAULT_SETTINGS),
            loaded_from=loaded_from,
            fallback_used=fallback_used,
            missing_keys=list(missing_keys or []),
            invalid_keys=list(invalid_keys or []),
            validation_warnings=list(validation_warnings or []),
        )

    @classmethod
    def from_row(cls, row: dict[str, Any] | None, *, now: Optional[datetime] = None) -> "StrategyRuntimeSettings":
        if not row:
            return cls.legacy_default(
                fallback_used=True,
                missing_keys=["strategy_runtime_settings"],
            )
        try:
            raw_settings = row.get("settings_json") or row.get("config_json") or "{}"
            payload = json.loads(raw_settings) if isinstance(raw_settings, str) else raw_settings
            if not isinstance(payload, dict):
                raise ValueError("settings_json must be an object")
        except Exception as exc:
            return cls.legacy_default(
                loaded_from="strategy_runtime_settings",
                fallback_used=True,
                invalid_keys=[f"settings_json:{exc}"],
            )

        merged, missing, invalid = merge_with_legacy_defaults(payload)
        mode = str(row.get("mode") or DEFAULT_SETTINGS_MODE)
        warnings = validate_settings(merged)
        if mode not in ALLOWED_SETTINGS_MODES:
            invalid.append("mode")
            mode = DEFAULT_SETTINGS_MODE
        enabled = _bool_from_db(row.get("enabled"), default=True)
        effective_from = str(row.get("effective_from") or "")
        effective_to = str(row.get("effective_to") or "")
        if not _is_effective(effective_from, effective_to, now):
            warnings.append("SETTINGS_PROFILE_NOT_IN_EFFECTIVE_WINDOW")
        fallback_used = bool(missing or invalid or any(item.endswith("_FALLBACK_TO_LEGACY") for item in warnings))
        return cls(
            strategy_name=str(row.get("strategy_name") or DEFAULT_STRATEGY_NAME),
            profile_name=str(row.get("profile_name") or DEFAULT_PROFILE_NAME),
            profile_version=str(row.get("profile_version") or row.get("config_version") or DEFAULT_PROFILE_VERSION),
            mode=mode,
            enabled=enabled,
            effective_from=effective_from,
            effective_to=effective_to,
            settings_json=merged,
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
            description=str(row.get("description") or ""),
            loaded_from="strategy_runtime_settings",
            fallback_used=fallback_used,
            missing_keys=missing,
            invalid_keys=invalid,
            validation_warnings=warnings,
        )

    @classmethod
    def from_settings_json(
        cls,
        settings_json: dict[str, Any],
        *,
        strategy_name: str = DEFAULT_STRATEGY_NAME,
        profile_name: str = DEFAULT_PROFILE_NAME,
        profile_version: str = DEFAULT_PROFILE_VERSION,
        mode: str = DEFAULT_SETTINGS_MODE,
        loaded_from: str = "provided",
    ) -> "StrategyRuntimeSettings":
        merged, missing, invalid = merge_with_legacy_defaults(settings_json)
        warnings = validate_settings(merged)
        return cls(
            strategy_name=strategy_name,
            profile_name=profile_name,
            profile_version=profile_version,
            mode=mode if mode in ALLOWED_SETTINGS_MODES else DEFAULT_SETTINGS_MODE,
            settings_json=merged,
            loaded_from=loaded_from,
            fallback_used=bool(missing or invalid or any(item.endswith("_FALLBACK_TO_LEGACY") for item in warnings)),
            missing_keys=missing,
            invalid_keys=invalid,
            validation_warnings=warnings,
        )

    def value(self, path: str, default: Any = None) -> Any:
        current: Any = self.settings_json
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def number(self, path: str, default: float) -> float:
        value = self.value(path, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def integer(self, path: str, default: int) -> int:
        value = self.value(path, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    def range_pair(self, path: str, default: tuple[float, float]) -> tuple[float, float]:
        value = self.value(path)
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return default
        try:
            return (float(value[0]), float(value[1]))
        except (TypeError, ValueError):
            return default

    def list_value(self, path: str, default: list[Any]) -> list[Any]:
        value = self.value(path)
        return list(value) if isinstance(value, list) else list(default)

    def settings_details(self) -> dict[str, Any]:
        return {
            "strategy_settings_profile": self.profile_name,
            "strategy_settings_version": self.profile_version,
            "strategy_settings_mode": self.mode,
            "settings_loaded_from": self.loaded_from,
            "settings_fallback_used": bool(self.fallback_used),
            "settings_missing_keys": list(self.missing_keys),
            "settings_invalid_keys": list(self.invalid_keys),
            "settings_validation_warnings": list(self.validation_warnings),
        }

    def to_db_payload(self) -> dict[str, Any]:
        settings_text = json.dumps(self.settings_json, ensure_ascii=False, sort_keys=True)
        return {
            "config_key": strategy_settings_config_key(self.strategy_name, self.profile_name, self.profile_version),
            "config_version": 1,
            "config_json": settings_text,
            "strategy_name": self.strategy_name,
            "profile_name": self.profile_name,
            "profile_version": self.profile_version,
            "mode": self.mode,
            "enabled": int(self.enabled),
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "settings_json": settings_text,
            "description": self.description,
        }


class StrategyRuntimeSettingsRepository:
    def __init__(
        self,
        db,
        *,
        strategy_name: str = DEFAULT_STRATEGY_NAME,
        profile_name: str = DEFAULT_PROFILE_NAME,
        profile_version: str = DEFAULT_PROFILE_VERSION,
    ) -> None:
        self.db = db
        self.strategy_name = strategy_name
        self.profile_name = profile_name
        self.profile_version = profile_version

    def load(self, *, now: Optional[datetime] = None) -> StrategyRuntimeSettings:
        try:
            row = self.db.load_strategy_runtime_settings_profile(
                self.strategy_name,
                self.profile_name,
                self.profile_version,
                now=(now or datetime.now()).replace(microsecond=0).isoformat(),
            )
        except Exception as exc:
            return StrategyRuntimeSettings.legacy_default(
                fallback_used=True,
                invalid_keys=[f"strategy_runtime_settings:{exc}"],
            )
        return StrategyRuntimeSettings.from_row(row, now=now)

    def save(self, settings: StrategyRuntimeSettings) -> StrategyRuntimeSettings:
        self.db.save_strategy_runtime_settings_profile(settings.to_db_payload())
        loaded = StrategyRuntimeSettingsRepository(
            self.db,
            strategy_name=settings.strategy_name,
            profile_name=settings.profile_name,
            profile_version=settings.profile_version,
        ).load()
        return loaded


def legacy_strategy_runtime_settings() -> StrategyRuntimeSettings:
    return StrategyRuntimeSettings.legacy_default()


def legacy_profile_payload() -> dict[str, Any]:
    return StrategyRuntimeSettings.legacy_default().to_db_payload()


def attach_settings_details(details: dict[str, Any], settings: Optional[StrategyRuntimeSettings]) -> dict[str, Any]:
    active = settings or legacy_strategy_runtime_settings()
    details.update(active.settings_details())
    if active.missing_keys or active.invalid_keys:
        details["comparison_reason_codes"] = normalize_reason_codes(
            list(details.get("comparison_reason_codes") or []) + [ReasonCode.SETTINGS_KEY_MISSING.value]
        )
        details["input_missing_fields"] = normalize_reason_codes(
            list(details.get("input_missing_fields") or [])
            + list(active.missing_keys)
            + list(active.invalid_keys)
        )
    return details


def merge_with_legacy_defaults(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    missing: list[str] = []
    invalid: list[str] = []
    merged = _merge_value(LEGACY_DEFAULT_SETTINGS, raw, "", missing, invalid)
    assert isinstance(merged, dict)
    return merged, missing, invalid


def validate_settings(settings_json: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    gate_weights = settings_json.get("gate_weights")
    if isinstance(gate_weights, dict):
        total = sum(float(gate_weights.get(key, 0.0) or 0.0) for key in LEGACY_DEFAULT_SETTINGS["gate_weights"])
        if abs(total - 1.0) > 0.0001:
            warnings.append("GATE_WEIGHTS_SUM_INVALID_FALLBACK_TO_LEGACY")
            settings_json["gate_weights"] = deepcopy(LEGACY_DEFAULT_SETTINGS["gate_weights"])
    return warnings


def _merge_value(default: Any, raw: Any, path: str, missing: list[str], invalid: list[str]) -> Any:
    if isinstance(default, dict):
        if raw is None:
            if path:
                missing.append(path)
            return deepcopy(default)
        if not isinstance(raw, dict):
            invalid.append(path or "settings_json")
            return deepcopy(default)
        result: dict[str, Any] = {}
        for key, default_value in default.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key not in raw:
                missing.append(child_path)
                result[key] = deepcopy(default_value)
                continue
            result[key] = _merge_value(default_value, raw.get(key), child_path, missing, invalid)
        return result
    if isinstance(default, list):
        if not isinstance(raw, list):
            invalid.append(path)
            return deepcopy(default)
        if len(raw) != len(default):
            invalid.append(path)
            return deepcopy(default)
        coerced = []
        for index, default_item in enumerate(default):
            item_path = f"{path}.{index}"
            coerced.append(_merge_value(default_item, raw[index], item_path, missing, invalid))
        return coerced
    if isinstance(default, bool):
        if type(raw) is not bool:
            invalid.append(path)
            return default
        return raw
    if isinstance(default, int) and not isinstance(default, bool):
        if type(raw) is bool:
            invalid.append(path)
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            invalid.append(path)
            return default
    if isinstance(default, float):
        if type(raw) is bool:
            invalid.append(path)
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            invalid.append(path)
            return default
    if isinstance(default, str):
        if not isinstance(raw, str):
            invalid.append(path)
            return default
        return raw
    return deepcopy(raw)


def _bool_from_db(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if type(value) is bool:
        return value
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return default


def _is_effective(effective_from: str, effective_to: str, now: Optional[datetime]) -> bool:
    if now is None:
        now = datetime.now().replace(microsecond=0)
    if effective_from:
        try:
            if now < datetime.fromisoformat(effective_from):
                return False
        except ValueError:
            return False
    if effective_to:
        try:
            if now >= datetime.fromisoformat(effective_to):
                return False
        except ValueError:
            return False
    return True
