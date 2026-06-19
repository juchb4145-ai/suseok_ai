from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterable, Mapping

from trading.strategy.candidates import normalize_code
from trading.theme_engine.expansion import FocusedExpansionTarget


class ExpansionLeaseStatus(str, Enum):
    ACTIVE = "ACTIVE"
    HOLDING = "HOLDING"
    REMOVAL_PENDING = "REMOVAL_PENDING"
    EXPIRED = "EXPIRED"
    PROTECTED = "PROTECTED"


@dataclass(frozen=True)
class ExpansionLease:
    code: str
    theme_id: str = ""
    source: str = "reboot_v2_theme_expansion"
    subscription_generation: int = 1
    selected_at: str = ""
    selected_tick_baseline_at: str = ""
    first_active_at: str = ""
    first_fresh_tick_at: str = ""
    first_post_subscription_tick_at: str = ""
    first_tick_source_event_id: str = ""
    minimum_hold_until: str = ""
    expires_at: str = ""
    last_eligible_at: str = ""
    removal_pending_at: str = ""
    cooldown_until: str = ""
    status: str = ExpansionLeaseStatus.ACTIVE.value
    reason_codes: tuple[str, ...] = ()
    target: FocusedExpansionTarget | None = None


@dataclass(frozen=True)
class ExpansionLeaseSnapshot:
    calculated_at: str
    leases: tuple[ExpansionLease, ...]
    active_lease_count: int = 0
    holding_count: int = 0
    protected_count: int = 0
    pending_removal_count: int = 0
    expired_count: int = 0
    churn_count: int = 0
    first_tick_wait_count: int = 0
    lease_by_theme: dict[str, int] | None = None
    top_removal_reasons: tuple[dict[str, Any], ...] = ()


class ExpansionLeaseManager:
    def __init__(self, *, source: str = "reboot_v2_theme_expansion", cooldown_sec: int = 60, clock=None) -> None:
        self.source = source
        self.cooldown_sec = max(0, int(cooldown_sec or 0))
        self.clock = clock or datetime.now
        self._leases: dict[tuple[str, str], ExpansionLease] = {}
        self._last_snapshot: ExpansionLeaseSnapshot | None = None

    def restore(self, leases: Iterable[ExpansionLease | dict[str, Any]]) -> None:
        restored: dict[tuple[str, str], ExpansionLease] = {}
        for item in leases:
            lease = item if isinstance(item, ExpansionLease) else _lease_from_mapping(item)
            code = normalize_code(lease.code)
            if code:
                restored[(code, lease.theme_id)] = replace(lease, code=code)
        self._leases = restored

    def reconcile(
        self,
        targets: Iterable[FocusedExpansionTarget],
        *,
        now: datetime | None = None,
        active_codes: Iterable[str] = (),
        fresh_tick_codes: Iterable[str] = (),
        fresh_tick_events: Mapping[str, Any] | None = None,
        protected_codes: Iterable[str] = (),
    ) -> ExpansionLeaseSnapshot:
        current = (now or self.clock()).replace(microsecond=0)
        active_code_set = {normalize_code(code) for code in active_codes if normalize_code(code)}
        fresh_events = _fresh_events(fresh_tick_events, fresh_tick_codes=fresh_tick_codes, now=current)
        protected_code_set = {normalize_code(code) for code in protected_codes if normalize_code(code)}
        target_by_key = {
            (normalize_code(target.code), str(target.theme_id or "")): target
            for target in targets
            if normalize_code(target.code)
        }
        churn = 0
        for key, target in target_by_key.items():
            lease = self._leases.get(key)
            if lease is None:
                lease = _new_lease(target, now=current)
                churn += 1
            else:
                lease = _refresh_lease(lease, target, now=current)
            code, _theme_id = key
            if code in active_code_set and not lease.first_active_at:
                lease = replace(lease, first_active_at=current.isoformat())
            event = fresh_events.get(code)
            if event and not lease.first_fresh_tick_at and _post_subscription_fresh_tick(lease, event):
                lease = replace(
                    lease,
                    first_fresh_tick_at=current.isoformat(),
                    first_post_subscription_tick_at=str(event.get("tick_at") or ""),
                    first_tick_source_event_id=str(event.get("source_event_id") or ""),
                    reason_codes=tuple(_dedupe([*lease.reason_codes, "THEME_EXPANSION_TICK_READY"])),
                )
            self._leases[key] = lease
        for key, lease in list(self._leases.items()):
            if key in target_by_key:
                continue
            code, _theme_id = key
            if code in protected_code_set:
                self._leases[key] = replace(lease, status=ExpansionLeaseStatus.PROTECTED.value, reason_codes=tuple(_dedupe([*lease.reason_codes, "PROTECTED_SUBSCRIPTION"])))
                continue
            if _before(current, lease.minimum_hold_until):
                self._leases[key] = replace(lease, status=ExpansionLeaseStatus.HOLDING.value, reason_codes=tuple(_dedupe([*lease.reason_codes, "MINIMUM_HOLD_ACTIVE"])))
                continue
            if lease.status == ExpansionLeaseStatus.REMOVAL_PENDING.value:
                if _before(current, lease.cooldown_until) and _before(current, lease.expires_at):
                    self._leases[key] = lease
                    continue
                self._leases[key] = replace(lease, status=ExpansionLeaseStatus.EXPIRED.value, reason_codes=tuple(_dedupe([*lease.reason_codes, "LEASE_COOLDOWN_EXPIRED"])))
                churn += 1
                continue
            if not _before(current, lease.expires_at):
                self._leases[key] = replace(lease, status=ExpansionLeaseStatus.EXPIRED.value, removal_pending_at=current.isoformat(), reason_codes=tuple(_dedupe([*lease.reason_codes, "LEASE_TTL_EXPIRED"])))
                churn += 1
                continue
            self._leases[key] = replace(
                lease,
                status=ExpansionLeaseStatus.REMOVAL_PENDING.value,
                removal_pending_at=current.isoformat(),
                cooldown_until=(current + timedelta(seconds=self.cooldown_sec)).isoformat(),
                reason_codes=tuple(_dedupe([*lease.reason_codes, "THEME_NO_LONGER_ELIGIBLE"])),
            )
            churn += 1
        snapshot = _snapshot(tuple(self._leases.values()), calculated_at=current.isoformat(), churn=churn)
        self._last_snapshot = snapshot
        return snapshot

    def removable_codes(self) -> list[str]:
        active_status = {ExpansionLeaseStatus.ACTIVE.value, ExpansionLeaseStatus.HOLDING.value, ExpansionLeaseStatus.PROTECTED.value, ExpansionLeaseStatus.REMOVAL_PENDING.value}
        code_statuses: dict[str, set[str]] = {}
        for lease in self._leases.values():
            code_statuses.setdefault(lease.code, set()).add(lease.status)
        return sorted(
            code
            for code, statuses in code_statuses.items()
            if statuses and not statuses.intersection(active_status)
        )

    def active_leases(self) -> tuple[ExpansionLease, ...]:
        return tuple(lease for lease in self._leases.values() if lease.status in {ExpansionLeaseStatus.ACTIVE.value, ExpansionLeaseStatus.HOLDING.value, ExpansionLeaseStatus.PROTECTED.value})

    def retained_leases(self) -> tuple[ExpansionLease, ...]:
        return tuple(
            lease
            for lease in self._leases.values()
            if lease.status in {
                ExpansionLeaseStatus.ACTIVE.value,
                ExpansionLeaseStatus.HOLDING.value,
                ExpansionLeaseStatus.PROTECTED.value,
                ExpansionLeaseStatus.REMOVAL_PENDING.value,
            }
        )


def _new_lease(target: FocusedExpansionTarget, *, now: datetime) -> ExpansionLease:
    hold = max(0, int(getattr(target, "minimum_hold_sec", 0) or 0))
    ttl = max(1, int(getattr(target, "subscription_ttl_sec", 90) or 90))
    reasons = _dedupe([*tuple(getattr(target, "reason_codes", ()) or ()), "EXPANSION_LEASE_CREATED"])
    return ExpansionLease(
        code=normalize_code(target.code),
        theme_id=str(target.theme_id or ""),
        source=str(target.source or "reboot_v2_theme_expansion"),
        subscription_generation=1,
        selected_at=now.isoformat(),
        selected_tick_baseline_at=now.isoformat(),
        minimum_hold_until=(now + timedelta(seconds=hold)).isoformat(),
        expires_at=(now + timedelta(seconds=ttl)).isoformat(),
        last_eligible_at=now.isoformat(),
        cooldown_until="",
        status=ExpansionLeaseStatus.ACTIVE.value,
        reason_codes=tuple(reasons),
        target=target,
    )


def _refresh_lease(lease: ExpansionLease, target: FocusedExpansionTarget, *, now: datetime) -> ExpansionLease:
    ttl = max(1, int(getattr(target, "subscription_ttl_sec", 90) or 90))
    return replace(
        lease,
        source=str(target.source or lease.source),
        selected_tick_baseline_at=lease.selected_tick_baseline_at or lease.selected_at,
        last_eligible_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl)).isoformat(),
        cooldown_until="",
        status=ExpansionLeaseStatus.ACTIVE.value,
        removal_pending_at="",
        reason_codes=tuple(_dedupe([*lease.reason_codes, *tuple(getattr(target, "reason_codes", ()) or ()), "EXPANSION_LEASE_REFRESHED"])),
        target=target,
    )


def _snapshot(leases: tuple[ExpansionLease, ...], *, calculated_at: str, churn: int) -> ExpansionLeaseSnapshot:
    by_theme: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    for lease in leases:
        if lease.status in {ExpansionLeaseStatus.ACTIVE.value, ExpansionLeaseStatus.HOLDING.value, ExpansionLeaseStatus.PROTECTED.value}:
            by_theme[lease.theme_id] = by_theme.get(lease.theme_id, 0) + 1
        if lease.status in {ExpansionLeaseStatus.REMOVAL_PENDING.value, ExpansionLeaseStatus.EXPIRED.value}:
            for reason in lease.reason_codes:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return ExpansionLeaseSnapshot(
        calculated_at=calculated_at,
        leases=leases,
        active_lease_count=sum(1 for lease in leases if lease.status in {ExpansionLeaseStatus.ACTIVE.value, ExpansionLeaseStatus.HOLDING.value, ExpansionLeaseStatus.PROTECTED.value}),
        holding_count=sum(1 for lease in leases if lease.status == ExpansionLeaseStatus.HOLDING.value),
        protected_count=sum(1 for lease in leases if lease.status == ExpansionLeaseStatus.PROTECTED.value),
        pending_removal_count=sum(1 for lease in leases if lease.status == ExpansionLeaseStatus.REMOVAL_PENDING.value),
        expired_count=sum(1 for lease in leases if lease.status == ExpansionLeaseStatus.EXPIRED.value),
        churn_count=churn,
        first_tick_wait_count=sum(1 for lease in leases if lease.status == ExpansionLeaseStatus.ACTIVE.value and not lease.first_fresh_tick_at),
        lease_by_theme=by_theme,
        top_removal_reasons=tuple({"reason": key, "count": count} for key, count in sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)[:10]),
    )


def _lease_from_mapping(value: dict[str, Any]) -> ExpansionLease:
    raw = dict(value or {})
    return ExpansionLease(
        code=normalize_code(raw.get("code") or raw.get("stock_code") or ""),
        theme_id=str(raw.get("theme_id") or ""),
        source=str(raw.get("source") or "reboot_v2_theme_expansion"),
        subscription_generation=int(raw.get("subscription_generation") or 1),
        selected_at=str(raw.get("selected_at") or ""),
        selected_tick_baseline_at=str(raw.get("selected_tick_baseline_at") or raw.get("selected_at") or ""),
        first_active_at=str(raw.get("first_active_at") or ""),
        first_fresh_tick_at=str(raw.get("first_fresh_tick_at") or ""),
        first_post_subscription_tick_at=str(raw.get("first_post_subscription_tick_at") or raw.get("first_fresh_tick_at") or ""),
        first_tick_source_event_id=str(raw.get("first_tick_source_event_id") or ""),
        minimum_hold_until=str(raw.get("minimum_hold_until") or ""),
        expires_at=str(raw.get("expires_at") or ""),
        last_eligible_at=str(raw.get("last_eligible_at") or ""),
        removal_pending_at=str(raw.get("removal_pending_at") or ""),
        cooldown_until=str(raw.get("cooldown_until") or ""),
        status=str(raw.get("status") or ExpansionLeaseStatus.ACTIVE.value),
        reason_codes=tuple(str(item) for item in list(raw.get("reason_codes") or []) if str(item)),
        target=None,
    )


def _before(now: datetime, timestamp: str) -> bool:
    parsed = _parse_time(timestamp)
    return parsed is not None and now < parsed


def _fresh_events(
    fresh_tick_events: Mapping[str, Any] | None,
    *,
    fresh_tick_codes: Iterable[str],
    now: datetime,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if isinstance(fresh_tick_events, Mapping):
        for raw_code, raw_event in fresh_tick_events.items():
            code = normalize_code(str(raw_code or ""))
            if not code:
                continue
            event = dict(raw_event or {}) if isinstance(raw_event, Mapping) else {"tick_at": str(raw_event or "")}
            event.setdefault("tick_at", now.isoformat())
            result[code] = event
    for raw_code in fresh_tick_codes:
        code = normalize_code(raw_code)
        if code and code not in result:
            result[code] = {"tick_at": now.isoformat(), "source_event_id": ""}
    return result


def _post_subscription_fresh_tick(lease: ExpansionLease, event: Mapping[str, Any]) -> bool:
    tick_at = _parse_time(str(event.get("tick_at") or ""))
    if tick_at is None:
        return False
    baselines = [
        _parse_time(lease.selected_tick_baseline_at),
        _parse_time(lease.first_active_at),
        _parse_time(lease.selected_at),
    ]
    baseline = max((item for item in baselines if item is not None), default=None)
    if baseline is None:
        return False
    return tick_at >= baseline


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


__all__ = [
    "ExpansionLease",
    "ExpansionLeaseManager",
    "ExpansionLeaseSnapshot",
    "ExpansionLeaseStatus",
]
