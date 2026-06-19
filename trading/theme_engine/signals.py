from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any, Iterable

from trading.theme_engine.normalizer import normalize_stock_code


class SeedSourceType(str, Enum):
    OPT10032 = "opt10032"
    CONDITION_INCLUDE = "condition_include"
    MANUAL_WATCH = "manual_watch"
    HOLDING = "holding"
    PENDING_ORDER = "pending_order"
    REALTIME_TICK = "realtime_tick"
    HYDRATION = "hydration"


class SeedDataQualityStatus(str, Enum):
    REALTIME_VALID = "REALTIME_VALID"
    REALTIME_WARMUP = "REALTIME_WARMUP"
    TR_BACKFILL_ONLY = "TR_BACKFILL_ONLY"
    SIGNAL_STALE = "SIGNAL_STALE"
    DATA_WAIT = "DATA_WAIT"


class SeedFreshnessStatus(str, Enum):
    FRESH = "FRESH"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    TR_BACKFILL_ONLY = "TR_BACKFILL_ONLY"
    MISSING = "MISSING"


class ThemeDataWaitReason(str, Enum):
    SEED_WAIT = "SEED_WAIT"
    NO_THEME_SIGNALS = "NO_THEME_SIGNALS"
    THEME_MEMBERSHIP_MISSING = "THEME_MEMBERSHIP_MISSING"
    REALTIME_WARMUP = "REALTIME_WARMUP"
    REALTIME_COVERAGE_LOW = "REALTIME_COVERAGE_LOW"
    TR_BACKFILL_ONLY = "TR_BACKFILL_ONLY"
    SIGNAL_STALE = "SIGNAL_STALE"
    UNMAPPED_SEED = "UNMAPPED_SEED"
    CANDLE_WARMUP = "CANDLE_WARMUP"


@dataclass(frozen=True)
class LiveSeedSignal:
    code: str
    name: str = ""
    source_types: tuple[str, ...] = ()
    seed_rank: int = 0
    change_rate_pct: float = 0.0
    turnover_krw: float = 0.0
    turnover_speed: float = 0.0
    execution_strength: float = 0.0
    realtime_valid: bool = False
    tr_backfill_valid: bool = False
    data_quality_status: str = SeedDataQualityStatus.DATA_WAIT.value
    reason_codes: tuple[str, ...] = ()
    observed_at: str = ""
    last_seen_at: str = ""
    tick_at: str = ""
    tick_age_sec: float = 0.0
    freshness_status: str = ""
    source_confirmation_count: int = 0
    active: bool = True
    expiry_at: str = ""
    market: str = ""
    momentum_1m: float = 0.0
    momentum_3m: float = 0.0
    momentum_5m: float = 0.0
    vi_active: bool = False
    upper_limit_near: bool = False
    overheated: bool = False
    metadata: dict[str, Any] | None = None

    def normalized(self) -> "LiveSeedSignal":
        source_types = tuple(_dedupe(self.source_types))
        reason_codes = tuple(_dedupe(self.reason_codes))
        realtime_valid = bool(self.realtime_valid)
        tr_backfill_valid = bool(self.tr_backfill_valid)
        quality = self.data_quality_status
        freshness = self.freshness_status
        if not quality or quality == SeedDataQualityStatus.DATA_WAIT.value:
            if realtime_valid:
                quality = SeedDataQualityStatus.REALTIME_VALID.value
            elif tr_backfill_valid:
                quality = SeedDataQualityStatus.TR_BACKFILL_ONLY.value
            else:
                quality = SeedDataQualityStatus.DATA_WAIT.value
        if not freshness:
            if realtime_valid:
                freshness = SeedFreshnessStatus.FRESH.value
            elif tr_backfill_valid:
                freshness = SeedFreshnessStatus.TR_BACKFILL_ONLY.value
            else:
                freshness = SeedFreshnessStatus.MISSING.value
        return replace(
            self,
            code=normalize_stock_code(self.code),
            source_types=source_types,
            reason_codes=reason_codes,
            data_quality_status=str(quality),
            freshness_status=str(freshness),
            source_confirmation_count=self.source_confirmation_count or len(source_types),
            market=_normalize_market(self.market),
            metadata=dict(self.metadata or {}),
        )

    @property
    def tradable_realtime(self) -> bool:
        signal = self.normalized()
        if signal.freshness_status in {SeedFreshnessStatus.STALE.value, SeedFreshnessStatus.TR_BACKFILL_ONLY.value, SeedFreshnessStatus.MISSING.value}:
            return False
        return signal.realtime_valid and signal.active and not signal.vi_active and not signal.upper_limit_near and not signal.overheated


def apply_signal_freshness(
    signal: LiveSeedSignal,
    *,
    now: datetime,
    max_tick_age_sec: int,
    stale_multiplier: float = 3.0,
) -> LiveSeedSignal:
    normalized = signal.normalized()
    tick_at = normalized.tick_at or normalized.last_seen_at or normalized.observed_at
    if not tick_at and normalized.realtime_valid:
        freshness = SeedFreshnessStatus.FRESH.value
        age = 0.0
    elif not tick_at:
        freshness = SeedFreshnessStatus.TR_BACKFILL_ONLY.value if normalized.tr_backfill_valid else SeedFreshnessStatus.MISSING.value
        age = 0.0
    else:
        parsed = _parse_time(tick_at)
        age = max(0.0, (now - parsed).total_seconds()) if parsed is not None else 0.0
        if normalized.tr_backfill_valid and not normalized.realtime_valid:
            freshness = SeedFreshnessStatus.TR_BACKFILL_ONLY.value
        elif age <= max_tick_age_sec:
            freshness = SeedFreshnessStatus.FRESH.value
        elif age <= max_tick_age_sec * max(1.0, float(stale_multiplier)):
            freshness = SeedFreshnessStatus.DEGRADED.value
        else:
            freshness = SeedFreshnessStatus.STALE.value
    reasons = list(normalized.reason_codes)
    if freshness == SeedFreshnessStatus.STALE.value:
        reasons.append("SIGNAL_STALE")
    elif freshness == SeedFreshnessStatus.DEGRADED.value:
        reasons.append("SIGNAL_DEGRADED")
    elif freshness == SeedFreshnessStatus.TR_BACKFILL_ONLY.value:
        reasons.append("TR_BACKFILL_ONLY")
    return replace(
        normalized,
        tick_age_sec=round(age, 3),
        freshness_status=freshness,
        data_quality_status=SeedDataQualityStatus.SIGNAL_STALE.value
        if freshness == SeedFreshnessStatus.STALE.value
        else normalized.data_quality_status,
        reason_codes=tuple(_dedupe(reasons)),
    )


def merge_seed_signals(signals: Iterable[LiveSeedSignal]) -> list[LiveSeedSignal]:
    by_code: dict[str, LiveSeedSignal] = {}
    for signal in signals:
        normalized = signal.normalized()
        if not normalized.code:
            continue
        previous = by_code.get(normalized.code)
        by_code[normalized.code] = _merge(previous, normalized) if previous else normalized
    return sorted(by_code.values(), key=lambda item: (item.turnover_krw, -item.seed_rank), reverse=True)


def _merge(previous: LiveSeedSignal | None, current: LiveSeedSignal) -> LiveSeedSignal:
    if previous is None:
        return current
    best = current if current.turnover_krw >= previous.turnover_krw else previous
    return replace(
        best,
        source_types=tuple(_dedupe([*previous.source_types, *current.source_types])),
        seed_rank=min(_positive_rank(previous.seed_rank), _positive_rank(current.seed_rank)),
        change_rate_pct=max(previous.change_rate_pct, current.change_rate_pct),
        turnover_krw=max(previous.turnover_krw, current.turnover_krw),
        turnover_speed=max(previous.turnover_speed, current.turnover_speed),
        execution_strength=max(previous.execution_strength, current.execution_strength),
        realtime_valid=previous.realtime_valid or current.realtime_valid,
        tr_backfill_valid=previous.tr_backfill_valid or current.tr_backfill_valid,
        reason_codes=tuple(_dedupe([*previous.reason_codes, *current.reason_codes])),
    )


def _normalize_market(value: str) -> str:
    raw = str(value or "").strip().upper()
    if raw in {"KOSPI", "KS", "P"}:
        return "KOSPI"
    if raw in {"KOSDAQ", "KQ", "Q"}:
        return "KOSDAQ"
    return raw


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _positive_rank(value: int) -> int:
    return int(value) if int(value or 0) > 0 else 9999


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


__all__ = [
    "LiveSeedSignal",
    "SeedDataQualityStatus",
    "SeedFreshnessStatus",
    "SeedSourceType",
    "ThemeDataWaitReason",
    "apply_signal_freshness",
    "merge_seed_signals",
]
