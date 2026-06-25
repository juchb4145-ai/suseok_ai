from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trading.strategy.candidates import normalize_code


SUBSCRIPTION_LIFECYCLE_SCHEMA_VERSION = "realtime_subscription_lifecycle.v1"
try:
    LOCAL_TIMEZONE = ZoneInfo(os.getenv("TRADING_LOCAL_TIMEZONE", "Asia/Seoul"))
except ZoneInfoNotFoundError:  # pragma: no cover - defensive fallback for stripped runtimes
    LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
TARGET_SELECTION_PRESERVE_STATES = {
    "COMMAND_ENQUEUED",
    "COMMAND_DISPATCHED",
    "ACKED_WAIT_FIRST_TICK",
    "ACTIVE_FRESH",
    "ACTIVE_STALE",
}


class RealtimeSubscriptionLifecycleState(str, Enum):
    NOT_REQUESTED = "NOT_REQUESTED"
    REQUESTED = "REQUESTED"
    TARGET_SELECTED = "TARGET_SELECTED"
    BUDGET_DEFERRED = "BUDGET_DEFERRED"
    COMMAND_ENQUEUED = "COMMAND_ENQUEUED"
    COMMAND_DISPATCHED = "COMMAND_DISPATCHED"
    ACKED_WAIT_FIRST_TICK = "ACKED_WAIT_FIRST_TICK"
    ACTIVE_FRESH = "ACTIVE_FRESH"
    ACTIVE_STALE = "ACTIVE_STALE"
    RELEASE_PENDING = "RELEASE_PENDING"
    RELEASED = "RELEASED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class RealtimeCommandReceipt:
    accepted: bool
    command_id: str = ""
    command_type: str = ""
    idempotency_key: str = ""
    duplicate_of: str = ""
    status: str = ""
    reason: str = ""
    enqueued_at_utc: str = ""
    screen_no: str = ""
    codes: tuple[str, ...] = ()
    subscription_session_id: str = ""
    subscription_generation: int = 0
    target_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["codes"] = list(self.codes)
        return payload


@dataclass
class RealtimeSubscriptionLifecycleSnapshot:
    trade_date: str
    code: str
    lifecycle_state: str = RealtimeSubscriptionLifecycleState.NOT_REQUESTED.value
    schema_version: str = SUBSCRIPTION_LIFECYCLE_SCHEMA_VERSION
    requested: bool = False
    target_selected: bool = False
    budget_deferred: bool = False
    command_enqueued: bool = False
    command_dispatched: bool = False
    acked: bool = False
    transport_active: bool = False
    first_tick_verified: bool = False
    decision_fresh: bool = False
    stale: bool = False
    released: bool = False
    failed: bool = False
    screen_no: str = ""
    register_command_id: str = ""
    release_command_id: str = ""
    subscription_session_id: str = ""
    subscription_generation: int = 0
    target_digest: str = ""
    requested_at_utc: str = ""
    target_selected_at_utc: str = ""
    command_enqueued_at_utc: str = ""
    command_dispatched_at_utc: str = ""
    gateway_call_started_at_utc: str = ""
    gateway_call_finished_at_utc: str = ""
    command_acked_at_utc: str = ""
    core_ack_received_at_utc: str = ""
    registration_ack_baseline_at_utc: str = ""
    first_tick_at_utc: str = ""
    first_tick_gateway_at_utc: str = ""
    first_tick_core_at_utc: str = ""
    last_tick_at_utc: str = ""
    stale_since_utc: str = ""
    release_requested_at_utc: str = ""
    released_at_utc: str = ""
    failed_at_utc: str = ""
    failure_reason: str = ""
    latest_tick_age_sec: float = 999999.0
    ack_to_first_tick_ms: float | None = None
    enqueue_to_ack_ms: float | None = None
    dispatch_to_ack_ms: float | None = None
    sources: tuple[str, ...] = ()
    primary_source: str = ""
    updated_at_utc: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["sources"] = list(self.sources)
        payload["metadata"] = dict(self.metadata or {})
        return payload


class RealtimeSubscriptionLifecycleTracker:
    def __init__(self, db: Any | None = None, *, clock: Any = None, max_tick_age_sec: int = 10) -> None:
        self.db = db
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_tick_age_sec = max(1, int(max_tick_age_sec or 10))
        self._snapshots: dict[str, RealtimeSubscriptionLifecycleSnapshot] = {}
        self._latest_tick_evidence: dict[str, dict[str, Any]] = {}
        self._command_codes: dict[str, tuple[str, ...]] = {}

    def on_requested(self, record: Any, now: datetime | None = None) -> None:
        code = normalize_code(getattr(record, "code", ""))
        if not code:
            return
        current = _utc_text(now or self._now())
        sources = tuple(sorted(str(source) for source in getattr(record, "sources", set()) or [] if str(source)))
        self._transition(
            code,
            RealtimeSubscriptionLifecycleState.REQUESTED,
            current,
            requested=True,
            sources=sources,
            primary_source=str(getattr(record, "primary_source", "") or ""),
            requested_at_utc=current,
        )

    def on_target_selected(self, records: Iterable[Any], now: datetime | None = None) -> None:
        current = _utc_text(now or self._now())
        for record in records or []:
            code = normalize_code(getattr(record, "code", ""))
            if not code:
                continue
            previous = self._snapshots.get(code)
            state, preserved_updates = _target_selection_preserved_state(previous)
            updates = {
                "requested": True,
                "target_selected": True,
                "budget_deferred": False,
                "released": False,
                "failed": False,
                "screen_no": str(getattr(record, "screen_no", "") or ""),
                "target_selected_at_utc": current,
                "sources": tuple(sorted(str(source) for source in getattr(record, "sources", set()) or [] if str(source))),
                "primary_source": str(getattr(record, "primary_source", "") or ""),
            }
            updates.update(preserved_updates)
            self._transition(code, state, current, **updates)

    def on_budget_deferred(self, records: Iterable[Any], now: datetime | None = None) -> None:
        current = _utc_text(now or self._now())
        for record in records or []:
            code = normalize_code(getattr(record, "code", ""))
            if not code:
                continue
            self._transition(
                code,
                RealtimeSubscriptionLifecycleState.BUDGET_DEFERRED,
                current,
                requested=True,
                target_selected=False,
                budget_deferred=True,
                command_enqueued=False,
                command_dispatched=False,
                acked=False,
                transport_active=False,
                first_tick_verified=False,
                decision_fresh=False,
                stale=False,
                released=False,
                failed=False,
                screen_no="",
                register_command_id="",
                command_enqueued_at_utc="",
                command_dispatched_at_utc="",
                command_acked_at_utc="",
                core_ack_received_at_utc="",
                registration_ack_baseline_at_utc="",
                first_tick_at_utc="",
                first_tick_gateway_at_utc="",
                first_tick_core_at_utc="",
                last_tick_at_utc="",
                stale_since_utc="",
                release_requested_at_utc="",
                released_at_utc="",
                latest_tick_age_sec=999999.0,
                ack_to_first_tick_ms=None,
                enqueue_to_ack_ms=None,
                dispatch_to_ack_ms=None,
                target_selected_at_utc="",
            )

    def on_command_enqueued(self, receipt: RealtimeCommandReceipt | None, records: Iterable[Any], now: datetime | None = None) -> None:
        if receipt is None:
            return
        current = receipt.enqueued_at_utc or _utc_text(now or self._now())
        codes = tuple(normalize_code(code) for code in receipt.codes if normalize_code(code))
        if receipt.command_id:
            self._command_codes[receipt.command_id] = codes
        if receipt.duplicate_of:
            self._command_codes[receipt.duplicate_of] = codes
        duplicate_status = str(receipt.status or "").upper()
        if not receipt.accepted and receipt.duplicate_of and duplicate_status not in {"QUEUED", "DISPATCHED"}:
            return
        record_by_code = {normalize_code(getattr(record, "code", "")): record for record in records or []}
        for code in codes:
            record = record_by_code.get(code)
            state = RealtimeSubscriptionLifecycleState.COMMAND_DISPATCHED if duplicate_status == "DISPATCHED" else RealtimeSubscriptionLifecycleState.COMMAND_ENQUEUED
            previous = self._snapshots.get(code)
            preserve_active = bool(
                previous
                and not previous.released
                and not previous.failed
                and previous.transport_active
                and previous.first_tick_verified
                and previous.lifecycle_state in {
                    RealtimeSubscriptionLifecycleState.ACTIVE_FRESH.value,
                    RealtimeSubscriptionLifecycleState.ACTIVE_STALE.value,
                }
            )
            if preserve_active:
                state = RealtimeSubscriptionLifecycleState(previous.lifecycle_state)
            self._transition(
                code,
                state,
                current,
                requested=True,
                target_selected=True,
                budget_deferred=False,
                command_enqueued=True,
                command_dispatched=duplicate_status == "DISPATCHED" or (previous.command_dispatched if preserve_active and previous else False),
                acked=previous.acked if preserve_active and previous else False,
                transport_active=previous.transport_active if preserve_active and previous else False,
                first_tick_verified=previous.first_tick_verified if preserve_active and previous else False,
                decision_fresh=previous.decision_fresh if preserve_active and previous else False,
                stale=previous.stale if preserve_active and previous else False,
                released=False,
                failed=False,
                release_requested_at_utc="",
                released_at_utc="",
                first_tick_at_utc=previous.first_tick_at_utc if preserve_active and previous else "",
                first_tick_gateway_at_utc=previous.first_tick_gateway_at_utc if preserve_active and previous else "",
                first_tick_core_at_utc=previous.first_tick_core_at_utc if preserve_active and previous else "",
                last_tick_at_utc=previous.last_tick_at_utc if preserve_active and previous else "",
                stale_since_utc=previous.stale_since_utc if preserve_active and previous else "",
                registration_ack_baseline_at_utc=previous.registration_ack_baseline_at_utc if preserve_active and previous else "",
                latest_tick_age_sec=previous.latest_tick_age_sec if preserve_active and previous else 999999.0,
                ack_to_first_tick_ms=previous.ack_to_first_tick_ms if preserve_active and previous else None,
                screen_no=receipt.screen_no,
                register_command_id=receipt.command_id,
                subscription_session_id=receipt.subscription_session_id,
                subscription_generation=receipt.subscription_generation,
                target_digest=receipt.target_digest,
                command_enqueued_at_utc=current,
                command_dispatched_at_utc=current if duplicate_status == "DISPATCHED" else "",
                sources=tuple(sorted(str(source) for source in getattr(record, "sources", set()) or [] if str(source))) if record is not None else (),
                primary_source=str(getattr(record, "primary_source", "") or "") if record is not None else "",
            )

    def on_command_started(self, payload: Mapping[str, Any], now: datetime | None = None) -> None:
        command_type = str(payload.get("command_type") or "")
        if command_type != "register_realtime":
            return
        current = _first_text(_metadata_text(payload, "gateway_command_started_at_utc"), now or self._now())
        for code in _event_codes(payload, self._command_codes):
            self._transition(
                code,
                RealtimeSubscriptionLifecycleState.COMMAND_DISPATCHED,
                current,
                command_dispatched=True,
                command_dispatched_at_utc=current,
                register_command_id=str(payload.get("command_id") or ""),
                screen_no=str(payload.get("screen_no") or ""),
            )

    def on_command_ack(self, payload: Mapping[str, Any], now: datetime | None = None) -> None:
        command_type = str(payload.get("command_type") or "")
        if command_type not in {"register_realtime", "remove_realtime", "remove_all_realtime"}:
            return
        current = _utc_text(now or self._now())
        if command_type == "register_realtime":
            status = str(payload.get("status") or "ACKED").upper()
            if status != "ACKED":
                self.on_command_failed(payload, now=now)
                return
            ack_time = _first_text(_metadata_text(payload, "gateway_command_ack_created_at_utc"), current)
            baseline = _first_text(
                _metadata_text(payload, "gateway_kiwoom_call_finished_at_utc"),
                _metadata_text(payload, "gateway_command_ack_created_at_utc"),
                current,
            )
            for code in _event_codes(payload, self._command_codes):
                snap = self._transition(
                    code,
                    RealtimeSubscriptionLifecycleState.ACKED_WAIT_FIRST_TICK,
                    current,
                    command_dispatched=True,
                    acked=True,
                    transport_active=True,
                    first_tick_verified=False,
                    decision_fresh=False,
                    stale=False,
                    released=False,
                    failed=False,
                    register_command_id=str(payload.get("command_id") or ""),
                    screen_no=str(payload.get("screen_no") or ""),
                    gateway_call_started_at_utc=_metadata_text(payload, "gateway_kiwoom_call_started_at_utc"),
                    gateway_call_finished_at_utc=_metadata_text(payload, "gateway_kiwoom_call_finished_at_utc"),
                    command_acked_at_utc=ack_time,
                    core_ack_received_at_utc=current,
                    registration_ack_baseline_at_utc=baseline,
                    release_requested_at_utc="",
                    released_at_utc="",
                    first_tick_at_utc="",
                    first_tick_gateway_at_utc="",
                    first_tick_core_at_utc="",
                    last_tick_at_utc="",
                    stale_since_utc="",
                    latest_tick_age_sec=999999.0,
                    ack_to_first_tick_ms=None,
                    enqueue_to_ack_ms=_latency_ms(self.snapshot(code).get("command_enqueued_at_utc"), ack_time),
                    dispatch_to_ack_ms=_latency_ms(self.snapshot(code).get("command_dispatched_at_utc"), ack_time),
                )
                tick = self._latest_tick_evidence.get(code)
                if tick:
                    self._maybe_verify_first_tick(code, tick, snap, current)
        elif command_type == "remove_all_realtime":
            for code in list(self._snapshots):
                self._transition(
                    code,
                    RealtimeSubscriptionLifecycleState.RELEASED,
                    current,
                    transport_active=False,
                    decision_fresh=False,
                    released=True,
                    released_at_utc=current,
                    release_command_id=str(payload.get("command_id") or ""),
                )
        else:
            for code in _event_codes(payload, self._command_codes):
                self._transition(
                    code,
                    RealtimeSubscriptionLifecycleState.RELEASED,
                    current,
                    transport_active=False,
                    decision_fresh=False,
                    released=True,
                    released_at_utc=current,
                    release_command_id=str(payload.get("command_id") or ""),
                )

    def on_command_failed(self, payload: Mapping[str, Any], now: datetime | None = None) -> None:
        command_type = str(payload.get("command_type") or "")
        if command_type not in {"register_realtime", "remove_realtime", "remove_all_realtime"}:
            return
        current = _utc_text(now or self._now())
        for code in _event_codes(payload, self._command_codes):
            self._transition(
                code,
                RealtimeSubscriptionLifecycleState.FAILED,
                current,
                failed=True,
                failed_at_utc=current,
                failure_reason=str(payload.get("error") or payload.get("message") or "COMMAND_FAILED"),
                register_command_id=str(payload.get("command_id") or self.snapshot(code).get("register_command_id") or ""),
            )

    def on_release_requested(self, receipt: RealtimeCommandReceipt | None, codes: Iterable[str], now: datetime | None = None) -> None:
        if receipt is None:
            return
        current = receipt.enqueued_at_utc or _utc_text(now or self._now())
        for code in [normalize_code(item) for item in codes if normalize_code(item)]:
            self._transition(
                code,
                RealtimeSubscriptionLifecycleState.RELEASE_PENDING,
                current,
                release_command_id=receipt.command_id,
                release_requested_at_utc=current,
            )

    def on_price_tick(self, payload: Mapping[str, Any], now: datetime | None = None) -> None:
        code = normalize_code(str(payload.get("code") or payload.get("stock_code") or ""))
        if not code:
            return
        current = _utc_text(now or self._now())
        tick = {
            "code": code,
            "tick_at": _tick_time(payload, current),
            "gateway_at": _metadata_text(payload, "gateway_received_at")
            or _metadata_text(payload, "gateway_event_created_at_utc")
            or _metadata_text(payload, "gateway_event_post_end_at_utc"),
            "core_at": _metadata_text(payload, "core_event_received_at_utc") or current,
            "source": str(payload.get("latest_tick_source") or _metadata_text(payload, "price_source") or "REALTIME").upper(),
        }
        self._latest_tick_evidence[code] = tick
        snap = self._snapshots.get(code)
        if snap is None:
            return
        self._maybe_verify_first_tick(code, tick, snap, current)

    def snapshot(self, code: str) -> dict[str, Any]:
        clean = normalize_code(code)
        snap = self._snapshots.get(clean)
        if snap is None:
            return {}
        return snap.to_dict()

    def refresh_staleness(self, code: str, now: datetime | None = None) -> dict[str, Any]:
        clean = normalize_code(code)
        snap = self._snapshots.get(clean)
        if snap is None or not snap.acked or not snap.first_tick_verified or not snap.transport_active:
            return self.snapshot(clean)
        current = _utc_text(now or self._now())
        tick_at = _parse_time(snap.last_tick_at_utc or snap.first_tick_at_utc)
        current_dt = _parse_time(current) or self._now()
        if tick_at is None:
            return self.snapshot(clean)
        age = max(0.0, (current_dt.replace(tzinfo=None) - tick_at.replace(tzinfo=None)).total_seconds())
        fresh = age <= self.max_tick_age_sec
        state = RealtimeSubscriptionLifecycleState.ACTIVE_FRESH if fresh else RealtimeSubscriptionLifecycleState.ACTIVE_STALE
        if snap.lifecycle_state != state.value or snap.decision_fresh != fresh or snap.stale == fresh:
            snap = self._transition(
                clean,
                state,
                current,
                decision_fresh=fresh,
                stale=not fresh,
                latest_tick_age_sec=round(age, 3),
                stale_since_utc=current if not fresh and not snap.stale_since_utc else snap.stale_since_utc,
            )
            return snap.to_dict()
        snap.latest_tick_age_sec = round(age, 3)
        return snap.to_dict()

    def snapshots(self, codes: Iterable[str]) -> dict[str, dict[str, Any]]:
        return {normalize_code(code): self.snapshot(code) for code in codes if normalize_code(code)}

    def _maybe_verify_first_tick(
        self,
        code: str,
        tick: Mapping[str, Any],
        snapshot: RealtimeSubscriptionLifecycleSnapshot,
        current: str,
    ) -> None:
        if not snapshot.acked:
            return
        if str(tick.get("source") or "").upper() != "REALTIME":
            return
        baseline = _parse_time(snapshot.registration_ack_baseline_at_utc)
        tick_at = _parse_time(tick.get("tick_at"))
        if tick_at is None or (baseline is not None and tick_at < baseline):
            return
        age = max(0.0, (_parse_time(current) or self._now()).replace(tzinfo=None).timestamp() - tick_at.replace(tzinfo=None).timestamp())
        fresh = age <= self.max_tick_age_sec
        state = RealtimeSubscriptionLifecycleState.ACTIVE_FRESH if fresh else RealtimeSubscriptionLifecycleState.ACTIVE_STALE
        first_tick_at = snapshot.first_tick_at_utc or str(tick.get("tick_at") or "")
        self._transition(
            code,
            state,
            current,
            transport_active=True,
            first_tick_verified=True,
            decision_fresh=fresh,
            stale=not fresh,
            first_tick_at_utc=first_tick_at,
            first_tick_gateway_at_utc=snapshot.first_tick_gateway_at_utc or str(tick.get("gateway_at") or ""),
            first_tick_core_at_utc=snapshot.first_tick_core_at_utc or str(tick.get("core_at") or ""),
            last_tick_at_utc=str(tick.get("tick_at") or ""),
            stale_since_utc=current if not fresh and not snapshot.stale_since_utc else snapshot.stale_since_utc,
            latest_tick_age_sec=round(age, 3),
            ack_to_first_tick_ms=snapshot.ack_to_first_tick_ms
            if snapshot.ack_to_first_tick_ms is not None
            else _latency_ms(snapshot.registration_ack_baseline_at_utc, first_tick_at),
        )

    def _transition(
        self,
        code: str,
        state: RealtimeSubscriptionLifecycleState,
        occurred_at: str,
        **updates: Any,
    ) -> RealtimeSubscriptionLifecycleSnapshot:
        clean = normalize_code(code)
        trade_date = occurred_at[:10] if occurred_at else datetime.now().date().isoformat()
        previous = self._snapshots.get(clean) or RealtimeSubscriptionLifecycleSnapshot(trade_date=trade_date, code=clean)
        payload = previous.to_dict()
        payload.update(updates)
        payload["trade_date"] = trade_date
        payload["code"] = clean
        payload["lifecycle_state"] = state.value
        payload["updated_at_utc"] = occurred_at
        payload["requested"] = bool(payload.get("requested") or state != RealtimeSubscriptionLifecycleState.NOT_REQUESTED)
        payload["target_selected"] = bool(payload.get("target_selected") or state in {
            RealtimeSubscriptionLifecycleState.TARGET_SELECTED,
            RealtimeSubscriptionLifecycleState.COMMAND_ENQUEUED,
            RealtimeSubscriptionLifecycleState.COMMAND_DISPATCHED,
            RealtimeSubscriptionLifecycleState.ACKED_WAIT_FIRST_TICK,
            RealtimeSubscriptionLifecycleState.ACTIVE_FRESH,
            RealtimeSubscriptionLifecycleState.ACTIVE_STALE,
        })
        if state == RealtimeSubscriptionLifecycleState.BUDGET_DEFERRED:
            payload["target_selected"] = False
            payload["budget_deferred"] = True
        elif state in {
            RealtimeSubscriptionLifecycleState.TARGET_SELECTED,
            RealtimeSubscriptionLifecycleState.COMMAND_ENQUEUED,
            RealtimeSubscriptionLifecycleState.COMMAND_DISPATCHED,
            RealtimeSubscriptionLifecycleState.ACKED_WAIT_FIRST_TICK,
            RealtimeSubscriptionLifecycleState.ACTIVE_FRESH,
            RealtimeSubscriptionLifecycleState.ACTIVE_STALE,
        }:
            payload["budget_deferred"] = False
        snap = RealtimeSubscriptionLifecycleSnapshot(**{key: payload.get(key) for key in RealtimeSubscriptionLifecycleSnapshot.__dataclass_fields__})
        self._snapshots[clean] = snap
        self._persist(snap, previous, occurred_at)
        return snap

    def _persist(self, snap: RealtimeSubscriptionLifecycleSnapshot, previous: RealtimeSubscriptionLifecycleSnapshot, occurred_at: str) -> None:
        if self.db is None:
            return
        save_latest = getattr(self.db, "save_realtime_subscription_lifecycle_latest", None)
        if callable(save_latest):
            save_latest(snap.to_dict())
        if previous.lifecycle_state == snap.lifecycle_state and previous.updated_at_utc == snap.updated_at_utc:
            return
        save_event = getattr(self.db, "save_realtime_subscription_lifecycle_event", None)
        if callable(save_event):
            save_event(
                {
                    **snap.to_dict(),
                    "previous_state": previous.lifecycle_state,
                    "current_state": snap.lifecycle_state,
                    "occurred_at_utc": occurred_at,
                }
            )

    def _now(self) -> datetime:
        value = self.clock()
        if isinstance(value, datetime):
            if value.tzinfo:
                return value.astimezone(timezone.utc)
            return value.replace(tzinfo=LOCAL_TIMEZONE).astimezone(timezone.utc)
        return datetime.now(timezone.utc)


def _event_codes(payload: Mapping[str, Any], command_codes: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    values = [normalize_code(item) for item in list(payload.get("codes") or []) if normalize_code(item)]
    if values:
        return tuple(values)
    command_id = str(payload.get("command_id") or "")
    if command_id and command_id in command_codes:
        return command_codes[command_id]
    return ()


def _target_selection_preserved_state(
    previous: RealtimeSubscriptionLifecycleSnapshot | None,
) -> tuple[RealtimeSubscriptionLifecycleState, dict[str, Any]]:
    if previous is None:
        return RealtimeSubscriptionLifecycleState.TARGET_SELECTED, {}
    if previous.lifecycle_state in TARGET_SELECTION_PRESERVE_STATES:
        state = RealtimeSubscriptionLifecycleState(previous.lifecycle_state)
    elif previous.transport_active and previous.first_tick_verified:
        state = RealtimeSubscriptionLifecycleState.ACTIVE_FRESH if previous.decision_fresh else RealtimeSubscriptionLifecycleState.ACTIVE_STALE
    elif previous.acked or previous.transport_active:
        state = RealtimeSubscriptionLifecycleState.ACKED_WAIT_FIRST_TICK
    elif previous.command_dispatched:
        state = RealtimeSubscriptionLifecycleState.COMMAND_DISPATCHED
    elif previous.command_enqueued:
        state = RealtimeSubscriptionLifecycleState.COMMAND_ENQUEUED
    else:
        return RealtimeSubscriptionLifecycleState.TARGET_SELECTED, {}

    if state in {RealtimeSubscriptionLifecycleState.ACTIVE_FRESH, RealtimeSubscriptionLifecycleState.ACTIVE_STALE}:
        baseline = _parse_time(previous.registration_ack_baseline_at_utc)
        tick_at = _parse_time(previous.last_tick_at_utc or previous.first_tick_at_utc)
        if baseline is not None and tick_at is not None and tick_at < baseline:
            return (
                RealtimeSubscriptionLifecycleState.ACKED_WAIT_FIRST_TICK,
                {
                    "first_tick_verified": False,
                    "decision_fresh": False,
                    "stale": False,
                    "first_tick_at_utc": "",
                    "first_tick_gateway_at_utc": "",
                    "first_tick_core_at_utc": "",
                    "last_tick_at_utc": "",
                    "stale_since_utc": "",
                    "latest_tick_age_sec": 999999.0,
                    "ack_to_first_tick_ms": None,
                },
            )
    return state, {}


def _tick_time(payload: Mapping[str, Any], fallback: str) -> str:
    for value in (
        _metadata_text(payload, "gateway_received_at_utc"),
        _metadata_text(payload, "core_event_received_at_utc"),
        payload.get("timestamp"),
        payload.get("latest_tick_at"),
    ):
        text = _utc_text_or_empty(value)
        if text:
            return text
    return fallback


def _metadata_text(payload: Mapping[str, Any], key: str) -> str:
    metadata = dict(payload.get("metadata") or {})
    trace = dict(metadata.get("transport_trace") or payload.get("transport_trace") or {})
    return str(metadata.get(key) or trace.get(key) or payload.get(key) or "")


def _first_text(*values: Any) -> str:
    for value in values:
        text = _utc_text_or_empty(value)
        if text:
            return text
    return ""


def _utc_text_or_empty(value: Any) -> str:
    if value in {None, ""}:
        return ""
    return _utc_text(value)


def _utc_text(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            dt = datetime.now(timezone.utc)
        else:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TIMEZONE)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=LOCAL_TIMEZONE).astimezone(timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=LOCAL_TIMEZONE).astimezone(timezone.utc)


def _latency_ms(start: Any, end: Any) -> float | None:
    start_dt = _parse_time(start)
    end_dt = _parse_time(end)
    if start_dt is None or end_dt is None:
        return None
    return round(max(0.0, (end_dt - start_dt).total_seconds() * 1000.0), 3)
