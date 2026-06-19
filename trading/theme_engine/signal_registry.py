from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterable

from trading.theme_engine.normalizer import normalize_stock_code as normalize_code
from trading.theme_engine.signals import LiveSeedSignal, SeedSourceType, merge_seed_signals


class ActiveSeedSource(str, Enum):
    OPENING = "OPENING"
    INTRADAY = "INTRADAY"
    CONDITION = "CONDITION"
    MANUAL = "MANUAL"
    HOLDING = "HOLDING"
    POSITION = "POSITION"


@dataclass(frozen=True)
class ActiveSeedSignal:
    code: str
    source_type: str
    source_id: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    removed_at: str = ""
    active: bool = True
    seed_rank: int = 0
    rank_delta: int = 0
    seen_count: int = 1
    expires_at: str = ""
    latest_turnover_krw: float = 0.0
    latest_change_rate_pct: float = 0.0
    condition_active: bool = False
    reason_codes: tuple[str, ...] = ()

    def to_live_signal(self) -> LiveSeedSignal:
        return LiveSeedSignal(
            code=self.code,
            source_types=(_seed_source_type(self.source_type),),
            seed_rank=self.seed_rank,
            change_rate_pct=self.latest_change_rate_pct,
            turnover_krw=self.latest_turnover_krw,
            observed_at=self.first_seen_at,
            last_seen_at=self.last_seen_at,
            active=self.active,
            expiry_at=self.expires_at,
            reason_codes=self.reason_codes,
            metadata={"source_id": self.source_id, "source_type": self.source_type},
        ).normalized()


@dataclass(frozen=True)
class ActiveSeedSnapshot:
    calculated_at: str
    active_signals: tuple[ActiveSeedSignal, ...]
    expired_signals: tuple[ActiveSeedSignal, ...] = ()
    active_count: int = 0
    expired_count: int = 0
    source_counts: dict[str, int] | None = None
    reason_codes: tuple[str, ...] = ()


class ActiveSeedRegistry:
    def __init__(self, *, ttl_sec: int = 600, clock=None) -> None:
        self.ttl_sec = max(1, int(ttl_sec or 600))
        self.clock = clock or datetime.now
        self._signals: dict[tuple[str, str, str], ActiveSeedSignal] = {}

    def merge(
        self,
        signal: ActiveSeedSignal | LiveSeedSignal | dict[str, Any],
        *,
        now: datetime | None = None,
        ttl_sec: int | None = None,
    ) -> ActiveSeedSignal | None:
        current = (now or self.clock()).replace(microsecond=0)
        item = _active_seed(signal, now=current, ttl_sec=ttl_sec or self.ttl_sec)
        if not item.code:
            return None
        key = (item.code, item.source_type, item.source_id)
        previous = self._signals.get(key)
        merged = _merge_active(previous, item) if previous is not None else item
        self._signals[key] = merged
        return merged

    def remove_source(self, code: str, source_type: str, source_id: str = "", *, now: datetime | None = None, reason: str = "") -> bool:
        current = (now or self.clock()).replace(microsecond=0)
        key = (normalize_code(code), str(source_type or ""), str(source_id or ""))
        item = self._signals.get(key)
        if item is None:
            return False
        reasons = [*item.reason_codes, reason or "SOURCE_REMOVED"]
        self._signals[key] = replace(item, active=False, removed_at=current.isoformat(), reason_codes=tuple(_dedupe(reasons)))
        return True

    def expire(self, *, now: datetime | None = None) -> tuple[ActiveSeedSignal, ...]:
        current = (now or self.clock()).replace(microsecond=0)
        expired: list[ActiveSeedSignal] = []
        for key, item in list(self._signals.items()):
            if not item.active:
                continue
            expires_at = _parse_time(item.expires_at)
            if expires_at is None or expires_at > current:
                continue
            expired_item = replace(item, active=False, removed_at=current.isoformat(), reason_codes=tuple(_dedupe([*item.reason_codes, "SEED_TTL_EXPIRED"])))
            self._signals[key] = expired_item
            expired.append(expired_item)
        return tuple(expired)

    def snapshot(self, *, now: datetime | None = None) -> ActiveSeedSnapshot:
        current = (now or self.clock()).replace(microsecond=0)
        expired = self.expire(now=current)
        active = tuple(sorted((item for item in self._signals.values() if item.active), key=lambda item: (_positive_rank(item.seed_rank), -item.latest_turnover_krw, item.code)))
        counts: dict[str, int] = {}
        for item in active:
            counts[item.source_type] = counts.get(item.source_type, 0) + 1
        return ActiveSeedSnapshot(
            calculated_at=current.isoformat(),
            active_signals=active,
            expired_signals=expired,
            active_count=len(active),
            expired_count=len(expired),
            source_counts=counts,
            reason_codes=("ACTIVE_SEED_REGISTRY_OBSERVE_ONLY",),
        )

    def live_signals(self, *, now: datetime | None = None) -> list[LiveSeedSignal]:
        snapshot = self.snapshot(now=now)
        return merge_seed_signals(item.to_live_signal() for item in snapshot.active_signals)


def _active_seed(value: ActiveSeedSignal | LiveSeedSignal | dict[str, Any], *, now: datetime, ttl_sec: int) -> ActiveSeedSignal:
    if isinstance(value, ActiveSeedSignal):
        expires = value.expires_at or (now + timedelta(seconds=ttl_sec)).isoformat()
        return replace(value, code=normalize_code(value.code), expires_at=expires, active=True)
    if isinstance(value, LiveSeedSignal):
        signal = value.normalized()
        source_type = _active_source_from_signal(signal)
        metadata = dict(signal.metadata or {})
        source_id = str(metadata.get("source_id") or ":".join(signal.source_types) or source_type)
        return ActiveSeedSignal(
            code=signal.code,
            source_type=source_type,
            source_id=source_id,
            first_seen_at=signal.observed_at or now.isoformat(),
            last_seen_at=signal.last_seen_at or signal.observed_at or now.isoformat(),
            seed_rank=signal.seed_rank,
            expires_at=signal.expiry_at or (now + timedelta(seconds=ttl_sec)).isoformat(),
            latest_turnover_krw=signal.turnover_krw,
            latest_change_rate_pct=signal.change_rate_pct,
            condition_active=SeedSourceType.CONDITION_INCLUDE.value in set(signal.source_types),
            reason_codes=signal.reason_codes,
        )
    raw = dict(value or {})
    return ActiveSeedSignal(
        code=normalize_code(raw.get("code") or raw.get("stock_code") or ""),
        source_type=str(raw.get("source_type") or raw.get("source") or ActiveSeedSource.INTRADAY.value),
        source_id=str(raw.get("source_id") or raw.get("batch_id") or ""),
        first_seen_at=str(raw.get("first_seen_at") or raw.get("observed_at") or now.isoformat()),
        last_seen_at=str(raw.get("last_seen_at") or raw.get("observed_at") or now.isoformat()),
        seed_rank=_int(raw.get("seed_rank") or raw.get("rank")),
        expires_at=str(raw.get("expires_at") or (now + timedelta(seconds=ttl_sec)).isoformat()),
        latest_turnover_krw=_float(raw.get("latest_turnover_krw") or raw.get("turnover_krw")),
        latest_change_rate_pct=_float(raw.get("latest_change_rate_pct") or raw.get("change_rate_pct")),
        condition_active=bool(raw.get("condition_active")),
        reason_codes=tuple(_dedupe(raw.get("reason_codes") or ())),
    )


def _merge_active(previous: ActiveSeedSignal, current: ActiveSeedSignal) -> ActiveSeedSignal:
    if not previous.active and current.last_seen_at <= previous.last_seen_at:
        return previous
    if current.last_seen_at <= previous.last_seen_at and current.latest_turnover_krw == previous.latest_turnover_krw:
        return previous
    return replace(
        current,
        first_seen_at=previous.first_seen_at or current.first_seen_at,
        seen_count=previous.seen_count + 1,
        rank_delta=_positive_rank(previous.seed_rank) - _positive_rank(current.seed_rank),
        latest_turnover_krw=current.latest_turnover_krw,
        latest_change_rate_pct=current.latest_change_rate_pct,
        condition_active=previous.condition_active or current.condition_active,
        reason_codes=tuple(_dedupe([*previous.reason_codes, *current.reason_codes])),
    )


def _active_source_from_signal(signal: LiveSeedSignal) -> str:
    sources = set(signal.source_types)
    scope = str(dict(signal.metadata or {}).get("seed_scope") or "").upper()
    if SeedSourceType.CONDITION_INCLUDE.value in sources:
        return ActiveSeedSource.CONDITION.value
    if scope == ActiveSeedSource.OPENING.value:
        return ActiveSeedSource.OPENING.value
    if scope == ActiveSeedSource.INTRADAY.value:
        return ActiveSeedSource.INTRADAY.value
    if SeedSourceType.OPT10032.value in sources:
        return ActiveSeedSource.INTRADAY.value
    if SeedSourceType.HOLDING.value in sources:
        return ActiveSeedSource.HOLDING.value
    if SeedSourceType.PENDING_ORDER.value in sources:
        return ActiveSeedSource.POSITION.value
    return ActiveSeedSource.MANUAL.value


def _seed_source_type(source_type: str) -> str:
    if source_type == ActiveSeedSource.CONDITION.value:
        return SeedSourceType.CONDITION_INCLUDE.value
    if source_type in {ActiveSeedSource.OPENING.value, ActiveSeedSource.INTRADAY.value}:
        return SeedSourceType.OPT10032.value
    if source_type == ActiveSeedSource.HOLDING.value:
        return SeedSourceType.HOLDING.value
    if source_type == ActiveSeedSource.POSITION.value:
        return SeedSourceType.PENDING_ORDER.value
    return SeedSourceType.MANUAL_WATCH.value


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _positive_rank(value: int) -> int:
    parsed = _int(value)
    return parsed if parsed > 0 else 999999


def _int(value: Any) -> int:
    try:
        return int(float(str(value or "0").replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(str(value or "0").replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


__all__ = [
    "ActiveSeedRegistry",
    "ActiveSeedSignal",
    "ActiveSeedSnapshot",
    "ActiveSeedSource",
]
