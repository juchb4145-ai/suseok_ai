from __future__ import annotations

from dataclasses import dataclass, replace
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
        if not quality or quality == SeedDataQualityStatus.DATA_WAIT.value:
            if realtime_valid:
                quality = SeedDataQualityStatus.REALTIME_VALID.value
            elif tr_backfill_valid:
                quality = SeedDataQualityStatus.TR_BACKFILL_ONLY.value
            else:
                quality = SeedDataQualityStatus.DATA_WAIT.value
        return replace(
            self,
            code=normalize_stock_code(self.code),
            source_types=source_types,
            reason_codes=reason_codes,
            data_quality_status=str(quality),
            market=_normalize_market(self.market),
            metadata=dict(self.metadata or {}),
        )

    @property
    def tradable_realtime(self) -> bool:
        signal = self.normalized()
        return signal.realtime_valid and not signal.vi_active and not signal.upper_limit_near and not signal.overheated


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
    "SeedSourceType",
    "ThemeDataWaitReason",
    "merge_seed_signals",
]
