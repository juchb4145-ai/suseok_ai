from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class RebootV2RuntimeProfile(str, Enum):
    LEGACY = "LEGACY"
    THEME_CORE_V3 = "THEME_CORE_V3"
    V2_OBSERVE = "V2_OBSERVE"
    V2_DRY_RUN = "V2_DRY_RUN"
    V2_LIVE_SIM_DISABLED = "V2_LIVE_SIM_DISABLED"


class CandidateV2State(str, Enum):
    DETECTED = "DETECTED"
    HYDRATING = "HYDRATING"
    WATCHING = "WATCHING"
    SETUP_READY = "SETUP_READY"
    TIMING_READY = "TIMING_READY"
    ORDER_PENDING = "ORDER_PENDING"
    OPEN = "OPEN"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    WAIT = "WAIT"
    HARD_BLOCK = "HARD_BLOCK"


class CandidateWaitReason(str, Enum):
    WAIT_DATA = "WAIT_DATA"
    WAIT_TR = "WAIT_TR"
    WAIT_MARKET = "WAIT_MARKET"
    WAIT_THEME = "WAIT_THEME"
    WAIT_TIMING = "WAIT_TIMING"
    WAIT_RISK = "WAIT_RISK"


class CandidateHardBlockReason(str, Enum):
    INVALID_CODE = "HARD_BLOCK_INVALID_CODE"
    UNTRADABLE = "HARD_BLOCK_UNTRADABLE"
    SESSION = "HARD_BLOCK_SESSION"
    MANUAL = "HARD_BLOCK_MANUAL"


class ConditionLevel(str, Enum):
    ALIVE = "ALIVE"
    STRONG = "STRONG"
    LEADER = "LEADER"


class ConditionEventType(str, Enum):
    INCLUDE = "include"
    REMOVE = "remove"


class HydrationPriority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class ThemeStatus(str, Enum):
    LEADING_THEME = "LEADING_THEME"
    SPREADING_THEME = "SPREADING_THEME"
    LEADER_ONLY_THEME = "LEADER_ONLY_THEME"
    WATCH_THEME = "WATCH_THEME"
    WEAK_THEME = "WEAK_THEME"


class MarketRegime(str, Enum):
    EXPANSION = "EXPANSION"
    SELECTIVE = "SELECTIVE"
    CHOPPY = "CHOPPY"
    WEAK = "WEAK"
    RISK_OFF = "RISK_OFF"


class StockRole(str, Enum):
    LEADER = "LEADER"
    STRONG_FOLLOWER = "STRONG_FOLLOWER"
    WATCH_MEMBER = "WATCH_MEMBER"
    WEAK_MEMBER = "WEAK_MEMBER"


class EntryStep(str, Enum):
    DATA_READY = "DATA_READY"
    THEME_READY = "THEME_READY"
    MARKET_ALLOWED = "MARKET_ALLOWED"
    STOCK_ROLE_ALLOWED = "STOCK_ROLE_ALLOWED"
    PRICE_TIMING_READY = "PRICE_TIMING_READY"


class ExitTrigger(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    SUPPORT_LOSS = "SUPPORT_LOSS"
    TIME_EXIT = "TIME_EXIT"
    TRAILING_STOP = "TRAILING_STOP"
    THEME_WEAK_EXIT = "THEME_WEAK_EXIT"
    LEADER_COLLAPSE_EXIT = "LEADER_COLLAPSE_EXIT"
    INDEX_WEAK_EXIT = "INDEX_WEAK_EXIT"
    MARKET_RISK_OFF_EXIT = "MARKET_RISK_OFF_EXIT"
    BREADTH_COLLAPSE_EXIT = "BREADTH_COLLAPSE_EXIT"


@dataclass(frozen=True)
class ConditionHit:
    code: str
    condition_name: str
    condition_level: ConditionLevel
    event_type: ConditionEventType
    first_seen_at: datetime
    last_seen_at: datetime
    hit_count: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        code: str,
        condition_name: str,
        condition_level: ConditionLevel | str,
        event_type: ConditionEventType | str,
        seen_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ConditionHit":
        current = _clean_time(seen_at)
        return cls(
            code=normalize_stock_code(code),
            condition_name=str(condition_name or ""),
            condition_level=ConditionLevel(condition_level),
            event_type=ConditionEventType(event_type),
            first_seen_at=current,
            last_seen_at=current,
            hit_count=1,
            metadata=dict(metadata or {}),
        )

    def seen_again(self, seen_at: datetime | None = None) -> "ConditionHit":
        return ConditionHit(
            code=self.code,
            condition_name=self.condition_name,
            condition_level=self.condition_level,
            event_type=self.event_type,
            first_seen_at=self.first_seen_at,
            last_seen_at=_clean_time(seen_at),
            hit_count=self.hit_count + 1,
            metadata=dict(self.metadata),
        )


@dataclass(frozen=True)
class HydrationRequestKey:
    trade_date: str
    priority: HydrationPriority
    tr_code: str
    rq_name: str
    inputs: dict[str, Any] = field(default_factory=dict)

    def idempotency_key(self) -> str:
        return build_tr_hydration_idempotency_key(
            trade_date=self.trade_date,
            priority=self.priority,
            tr_code=self.tr_code,
            rq_name=self.rq_name,
            inputs=self.inputs,
        )


def build_tr_hydration_idempotency_key(
    *,
    trade_date: str,
    priority: HydrationPriority | str,
    tr_code: str,
    rq_name: str,
    inputs: dict[str, Any],
) -> str:
    normalized_inputs = {str(key): str(value) for key, value in sorted(dict(inputs or {}).items())}
    raw_inputs = json.dumps(normalized_inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw_inputs.encode("utf-8")).hexdigest()[:16]
    return "tr:{trade_date}:{priority}:{tr_code}:{rq_name}:{digest}".format(
        trade_date=str(trade_date or ""),
        priority=HydrationPriority(priority).value,
        tr_code=str(tr_code or ""),
        rq_name=str(rq_name or ""),
        digest=digest,
    )


def normalize_stock_code(code: str) -> str:
    value = str(code or "").strip().upper()
    if value.startswith("A") and value[1:].isdigit():
        return value[1:]
    return value


def strategy_reboot_v2_enabled() -> bool:
    return reboot_v2_runtime_profile() != RebootV2RuntimeProfile.LEGACY


def reboot_v2_runtime_profile() -> RebootV2RuntimeProfile:
    raw = _runtime_profile_env()
    if raw:
        if raw in {"THEME_CORE_V3", "V3", "RT_TLS", "OPENING_THEME_BURST"}:
            return RebootV2RuntimeProfile.THEME_CORE_V3
        try:
            return RebootV2RuntimeProfile(raw)
        except ValueError:
            return RebootV2RuntimeProfile.V2_OBSERVE
    legacy_flag = os.getenv("STRATEGY_REBOOT_V2_ENABLED")
    if legacy_flag is not None and not _bool_text(legacy_flag):
        return RebootV2RuntimeProfile.LEGACY
    return RebootV2RuntimeProfile.V2_OBSERVE


def _runtime_profile_env() -> str:
    for name in ("STRATEGY_RUNTIME_PROFILE", "STRATEGY_REBOOT_V2_PROFILE"):
        value = str(os.getenv(name, "") or "").strip().upper()
        if value:
            return value
    return ""


def _clean_time(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).replace(microsecond=0)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return _bool_text(raw)


def _bool_text(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


__all__ = [
    "CandidateHardBlockReason",
    "CandidateV2State",
    "CandidateWaitReason",
    "ConditionEventType",
    "ConditionHit",
    "ConditionLevel",
    "EntryStep",
    "ExitTrigger",
    "HydrationPriority",
    "HydrationRequestKey",
    "MarketRegime",
    "RebootV2RuntimeProfile",
    "StockRole",
    "ThemeStatus",
    "build_tr_hydration_idempotency_key",
    "normalize_stock_code",
    "reboot_v2_runtime_profile",
    "strategy_reboot_v2_enabled",
]
