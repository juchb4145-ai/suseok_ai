from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Optional

from trading.strategy.candidates import normalize_code
from trading.strategy.models import BlockType, Candidate, CandidateState
from trading.strategy.subscription_lifecycle import RealtimeSubscriptionLifecycleState


SOURCE_PRIORITIES = {
    "reboot_v2_index": 110,
    "reboot_v2_position": 105,
    "reboot_v2_theme_expansion": 82,
    "reboot_v2_opening_seed": 70,
    "reboot_v2_candidate": 65,
    "reboot_v2_theme_board": 60,
    "index": 100,
    "leading_stock": 90,
    "semiconductor_signal": 90,
    "virtual_position": 85,
    "virtual_order": 84,
    "holding": 80,
    "candidate_watch": 50,
    "theme_board_watch": 56,
    "theme_lab_watchset": 55,
    "theme_lab_bootstrap": 54,
    "theme_lab_outcome_tracking": 53,
    "theme_universe": 45,
}
PROTECTED_SOURCES = {
    "reboot_v2_index",
    "reboot_v2_position",
    "index",
    "leading_stock",
    "semiconductor_signal",
    "virtual_order",
    "virtual_position",
    "holding",
}
FALLBACK_SOURCE_ORDER = {
    "reboot_v2_index": 0,
    "reboot_v2_position": 1,
    "reboot_v2_theme_expansion": 2,
    "reboot_v2_opening_seed": 3,
    "reboot_v2_candidate": 4,
    "reboot_v2_theme_board": 5,
    "index": 0,
    "leading_stock": 1,
    "semiconductor_signal": 1,
    "holding": 2,
    "virtual_position": 3,
    "virtual_order": 4,
    "theme_board_watch": 5,
    "candidate_watch": 5,
    "theme_lab_watchset": 5,
    "theme_lab_bootstrap": 5,
    "theme_lab_outcome_tracking": 5,
}


@dataclass
class SubscriptionRecord:
    code: str
    sources: set[str] = field(default_factory=set)
    source_priorities: dict[str, int] = field(default_factory=dict)
    source_protected: dict[str, bool] = field(default_factory=dict)
    priority: int = 0
    screen_no: str = ""
    protected: bool = False
    active: bool = False
    created_at: str = ""
    active_since: str = ""
    last_sync_at: str = ""
    subscription_generation: int = 0
    source_added_at_by_source: dict[str, str] = field(default_factory=dict)
    source_removed_at_by_source: dict[str, str] = field(default_factory=dict)
    source_generation_by_source: dict[str, int] = field(default_factory=dict)
    last_registered_at: str = ""
    last_deactivated_at: str = ""

    def add_source(self, source: str, priority: int, protected: bool, now: Optional[datetime] = None) -> None:
        timestamp = _timestamp(now)
        if not self.created_at:
            self.created_at = timestamp
        already_active = source in self.sources
        self.sources.add(source)
        self.source_priorities[source] = priority
        self.source_protected[source] = protected
        if not already_active:
            self.source_added_at_by_source[source] = timestamp
            self.source_generation_by_source[source] = int(self.source_generation_by_source.get(source, 0) or 0) + 1
        self.refresh()

    def remove_source(self, source: str, now: Optional[datetime] = None) -> None:
        timestamp = _timestamp(now)
        self.sources.discard(source)
        self.source_priorities.pop(source, None)
        self.source_protected.pop(source, None)
        self.source_added_at_by_source.pop(source, None)
        self.source_removed_at_by_source[source] = timestamp
        self.refresh()

    def refresh(self) -> None:
        self.priority = max(self.source_priorities.values(), default=0)
        self.protected = any(self.source_protected.values())

    @property
    def primary_source(self) -> str:
        if not self.sources:
            return ""
        return sorted(self.sources, key=lambda source: (SOURCE_PRIORITIES.get(source, 0), source), reverse=True)[0]


class RealTimeSubscriptionManager:
    def __init__(
        self,
        client,
        max_codes: int = 100,
        screen_base: int = 7000,
        screen_size: int = 100,
        clock: Callable[[], datetime] | None = None,
        lifecycle_tracker: object | None = None,
    ) -> None:
        self.client = client
        self.max_codes = max_codes
        self.screen_base = screen_base
        self.screen_size = screen_size
        self.clock = clock or datetime.now
        self.lifecycle_tracker = lifecycle_tracker
        self.records: dict[str, SubscriptionRecord] = {}
        self.code_to_screen: dict[str, str] = {}
        self.screen_to_codes: dict[str, set[str]] = {}
        self.target_screen_by_code: dict[str, str] = {}
        self.pending_register_by_code: dict[str, str] = {}
        self.acked_screen_by_code: dict[str, str] = {}
        self.pending_release_by_code: dict[str, str] = {}
        self.warnings: list[str] = []
        self._source_generation_by_code_source: dict[tuple[str, str], int] = {}
        self._source_removed_at_by_code_source: dict[tuple[str, str], str] = {}

    def ensure_subscription(
        self,
        code: str,
        source: str,
        priority: Optional[int] = None,
        protected: Optional[bool] = None,
    ) -> SubscriptionRecord:
        now = self._now()
        clean_code = normalize_code(code)
        resolved_priority = SOURCE_PRIORITIES.get(source, 0) if priority is None else priority
        resolved_protected = source in PROTECTED_SOURCES if protected is None else protected
        record = self.records.get(clean_code)
        if record is None:
            record = SubscriptionRecord(code=clean_code, created_at=_timestamp(now))
            self.records[clean_code] = record
        already_active = source in record.sources
        record.add_source(source, resolved_priority, resolved_protected, now=now)
        if not already_active:
            generation_key = (clean_code, source)
            generation = int(self._source_generation_by_code_source.get(generation_key, 0) or 0) + 1
            record.source_generation_by_source[source] = generation
            self._source_generation_by_code_source[generation_key] = generation
        self._lifecycle_call("on_requested", record, now=now)
        return record

    def remove_subscription(self, code: str, source: str) -> None:
        now = self._now()
        clean_code = normalize_code(code)
        record = self.records.get(clean_code)
        if record is None:
            return
        record.remove_source(source, now=now)
        removed_at = str(record.source_removed_at_by_source.get(source) or _timestamp(now))
        self._source_removed_at_by_code_source[(clean_code, source)] = removed_at
        self._source_generation_by_code_source[(clean_code, source)] = int(record.source_generation_by_source.get(source, 0) or self._source_generation_by_code_source.get((clean_code, source), 0) or 0)
        if not record.sources:
            self.records.pop(clean_code, None)

    def watch_candidates(self, candidates: Iterable[Candidate]) -> list[str]:
        candidate_codes: list[str] = []
        for candidate in candidates:
            if candidate.state in {CandidateState.REMOVED, CandidateState.EXPIRED}:
                self.remove_subscription(candidate.code, "candidate_watch")
                continue
            if not _candidate_watchable(candidate):
                continue
            candidate_codes.append(normalize_code(candidate.code))
            self.ensure_subscription(candidate.code, "candidate_watch")
        active_codes = self.sync()
        registered: list[str] = []
        for code in candidate_codes:
            if code in active_codes and code not in registered:
                registered.append(code)
        return registered

    def sync(self) -> set[str]:
        now = self._now()
        self.warnings = []
        target_records = self._target_records()
        target_codes = {record.code for record in target_records}
        self._lifecycle_call("on_target_selected", target_records, now=now)
        deferred_records = [record for record in self.records.values() if record.code not in target_codes and record.sources]
        self._lifecycle_call("on_budget_deferred", deferred_records, now=now)
        active_codes = set(self.code_to_screen)

        self._remove_codes(active_codes - target_codes, now=now)
        pending_codes = set(self.pending_register_by_code)
        self._register_records(
            [record for record in target_records if record.code not in self.code_to_screen and record.code not in pending_codes],
            now=now,
        )

        for record in self.records.values():
            record.active = record.code in self.code_to_screen
            record.screen_no = self.code_to_screen.get(record.code, "")
            record.last_sync_at = _timestamp(now)
        return set(self.code_to_screen)

    def mark_all_stale(self, reason: str = "") -> None:
        now = self._now()
        had_active_realtime = bool(self.code_to_screen or self.screen_to_codes)
        generation = None
        if had_active_realtime:
            generation = self._advance_client_generation(reason)
            self._remove_all_client_realtime(reason)
        self.code_to_screen.clear()
        self.screen_to_codes.clear()
        self.target_screen_by_code.clear()
        self.pending_register_by_code.clear()
        self.acked_screen_by_code.clear()
        self.pending_release_by_code.clear()
        for record in self.records.values():
            record.active = False
            record.screen_no = ""
            record.active_since = ""
            record.last_deactivated_at = _timestamp(now)
            record.subscription_generation += 1
        suffix = f":{reason}" if reason else ""
        generation_suffix = f":g{generation}" if generation is not None else ""
        self.warnings.append(f"REALTIME_SUBSCRIPTIONS_MARKED_STALE{suffix}{generation_suffix}")

    def remove_realtime(self, codes: Iterable[str]) -> None:
        now = self._now()
        self.warnings = []
        self._remove_codes([normalize_code(code) for code in codes], now=now)

    def _target_records(self) -> list[SubscriptionRecord]:
        protected_records = [record for record in self.records.values() if record.protected]
        regular_records = [record for record in self.records.values() if not record.protected]
        protected_records = sorted(protected_records, key=self._record_sort_key, reverse=True)
        if len(protected_records) > self.max_codes:
            self.warnings.append("PROTECTED_SUBSCRIPTION_OVER_LIMIT")
            return protected_records
        remaining = max(0, self.max_codes - len(protected_records))
        regular_records = sorted(regular_records, key=self._record_sort_key, reverse=True)[:remaining]
        return protected_records + regular_records

    @staticmethod
    def _record_sort_key(record: SubscriptionRecord) -> tuple[int, int, str]:
        return (1 if record.protected else 0, record.priority, record.code)

    @staticmethod
    def _registration_sort_key(record: SubscriptionRecord) -> tuple[int, int, int, str]:
        source_order = min((FALLBACK_SOURCE_ORDER.get(source, 99) for source in record.sources), default=99)
        return (source_order, 0 if record.protected else 1, -record.priority, record.code)

    def _register_records(self, records: list[SubscriptionRecord], now: Optional[datetime] = None) -> None:
        current = now or self._now()
        timestamp = _timestamp(current)
        screen_to_codes = {screen_no: set(codes) for screen_no, codes in self.screen_to_codes.items()}
        for code, screen_no in self.pending_register_by_code.items():
            screen_to_codes.setdefault(screen_no, set()).add(code)
        batches: dict[str, list[SubscriptionRecord]] = {}
        for record in sorted(records, key=self._registration_sort_key):
            screen_no = self._screen_for_new_code(screen_to_codes)
            screen_to_codes.setdefault(screen_no, set()).add(record.code)
            batches.setdefault(screen_no, []).append(record)

        for screen_no, records_for_screen in batches.items():
            codes = [record.code for record in records_for_screen]
            register_records = getattr(self.client, "register_realtime_records", None)
            if callable(register_records):
                receipt = register_records(records_for_screen, screen_no=screen_no)
            else:
                receipt = self.client.register_realtime(codes, screen_no=screen_no)
            self._lifecycle_call("on_command_enqueued", receipt, records_for_screen, now=current)
            for code in codes:
                self.target_screen_by_code[code] = screen_no
                if self.lifecycle_tracker is not None:
                    self.pending_register_by_code[code] = screen_no
                else:
                    self.code_to_screen[code] = screen_no
                    self.screen_to_codes.setdefault(screen_no, set()).update(codes)

        for screen_no, records_for_screen in batches.items():
            for record_for_screen in records_for_screen:
                code = record_for_screen.code
                record = self.records.get(code)
                if record is not None:
                    record.screen_no = screen_no
                    if self.lifecycle_tracker is None and not record.active:
                        record.active_since = timestamp
                        record.subscription_generation += 1
                    record.active = self.lifecycle_tracker is None
                    record.last_registered_at = timestamp
                    record.last_sync_at = timestamp

    def _remove_codes(self, codes: Iterable[str], now: Optional[datetime] = None) -> None:
        current = now or self._now()
        timestamp = _timestamp(current)
        codes = [code for code in codes if code]
        if not codes:
            return
        missing_mapping = [code for code in codes if code not in self.code_to_screen]
        if missing_mapping:
            self.warnings.append("REALTIME_REMOVE_ALL_FALLBACK")
            self._remove_all_client_realtime("REALTIME_REMOVE_ALL_FALLBACK")
            self.code_to_screen.clear()
            self.screen_to_codes.clear()
            for record in self.records.values():
                if record.active:
                    record.subscription_generation += 1
                record.active = False
                record.screen_no = ""
                record.active_since = ""
                record.last_deactivated_at = timestamp
            try:
                self._register_records(self._target_records(), now=current)
            except Exception as exc:
                self.warnings.append(f"PROTECTED_REREGISTER_FAILED:{exc}")
                raise
            return
        for code in codes:
            screen_no = self.code_to_screen.pop(code)
            receipt = self.client.remove_realtime([code], screen_no=screen_no)
            self._lifecycle_call("on_release_requested", receipt, [code], now=current)
            self.pending_release_by_code[code] = screen_no
            record = self.records.get(code)
            if record is not None:
                record.active = False
                record.screen_no = ""
                record.active_since = ""
                record.last_deactivated_at = timestamp
                record.last_sync_at = timestamp
                record.subscription_generation += 1
            codes_for_screen = self.screen_to_codes.get(screen_no)
            if codes_for_screen is not None:
                codes_for_screen.discard(code)
                if not codes_for_screen:
                    self.screen_to_codes.pop(screen_no, None)

    def _screen_for_new_code(self, screen_to_codes: Optional[dict[str, set[str]]] = None) -> str:
        screen_to_codes = screen_to_codes if screen_to_codes is not None else self.screen_to_codes
        screen_number = self.screen_base
        while True:
            screen_no = f"{screen_number:04d}"
            if len(screen_to_codes.get(screen_no, set())) < self.screen_size:
                return screen_no
            screen_number += 1

    def _advance_client_generation(self, reason: str) -> int | None:
        advance = getattr(self.client, "advance_subscription_generation", None)
        if not callable(advance):
            return None
        try:
            return int(advance(reason) or 0)
        except Exception as exc:
            self.warnings.append(f"REALTIME_SUBSCRIPTION_GENERATION_ADVANCE_FAILED:{exc}")
            return None

    def _remove_all_client_realtime(self, reason: str) -> None:
        remove_all = getattr(self.client, "remove_all_realtime", None)
        if not callable(remove_all):
            return
        try:
            try:
                remove_all(reason=reason)
            except TypeError:
                remove_all()
        except Exception as exc:
            self.warnings.append(f"REALTIME_REMOVE_ALL_ON_STALE_FAILED:{reason}:{exc}")

    def handle_realtime_command_event(self, event) -> bool:
        payload = dict(getattr(event, "payload", {}) or {})
        command_type = str(payload.get("command_type") or "")
        if command_type not in {"register_realtime", "remove_realtime", "remove_all_realtime"}:
            return False
        event_type = str(getattr(event, "type", "") or "")
        if event_type == "command_started":
            self._lifecycle_call("on_command_started", payload, now=self._now())
            return True
        if event_type == "command_ack":
            status = str(payload.get("status") or "ACKED").upper()
            if status == "ACKED":
                self._apply_realtime_command_ack(payload)
                self._lifecycle_call("on_command_ack", payload, now=self._now())
            else:
                self._apply_realtime_command_failed(payload)
                self._lifecycle_call("on_command_failed", payload, now=self._now())
            return True
        if event_type in {"command_failed", "command_timeout", "command_expired"}:
            self._apply_realtime_command_failed(payload)
            self._lifecycle_call("on_command_failed", payload, now=self._now())
            return True
        return False

    def handle_price_tick(self, payload: dict) -> None:
        self._lifecycle_call("on_price_tick", dict(payload or {}), now=self._now())

    def _apply_realtime_command_ack(self, payload: dict) -> None:
        command_type = str(payload.get("command_type") or "")
        codes = [normalize_code(code) for code in list(payload.get("codes") or []) if normalize_code(code)]
        screen_no = str(payload.get("screen_no") or "")
        timestamp = _timestamp(self._now())
        if command_type == "register_realtime":
            for code in codes:
                resolved_screen = screen_no or self.pending_register_by_code.get(code) or self.target_screen_by_code.get(code) or self.code_to_screen.get(code) or self._screen_for_new_code()
                self.pending_register_by_code.pop(code, None)
                self.code_to_screen[code] = resolved_screen
                self.acked_screen_by_code[code] = resolved_screen
                self.screen_to_codes.setdefault(resolved_screen, set()).add(code)
                record = self.records.get(code)
                if record is not None:
                    record.active = True
                    record.screen_no = resolved_screen
                    record.active_since = _payload_text(
                        payload,
                        "gateway_kiwoom_call_finished_at_utc",
                        "gateway_command_ack_created_at_utc",
                    ) or timestamp
                    record.last_sync_at = timestamp
                    record.subscription_generation = int(payload.get("subscription_generation") or record.subscription_generation)
            return
        if command_type == "remove_all_realtime":
            codes = list(self.code_to_screen)
        for code in codes:
            screen = self.code_to_screen.pop(code, "")
            self.pending_release_by_code.pop(code, None)
            self.pending_register_by_code.pop(code, None)
            if screen and screen in self.screen_to_codes:
                self.screen_to_codes[screen].discard(code)
                if not self.screen_to_codes[screen]:
                    self.screen_to_codes.pop(screen, None)
            record = self.records.get(code)
            if record is not None:
                record.active = False
                record.screen_no = ""
                record.active_since = ""
                record.last_deactivated_at = timestamp

    def _apply_realtime_command_failed(self, payload: dict) -> None:
        codes = [normalize_code(code) for code in list(payload.get("codes") or []) if normalize_code(code)]
        command_type = str(payload.get("command_type") or "")
        if command_type == "remove_all_realtime":
            codes = list(self.pending_register_by_code) + list(self.code_to_screen)
        for code in codes:
            if command_type == "register_realtime":
                self.pending_register_by_code.pop(code, None)
            if command_type.startswith("remove"):
                self.pending_release_by_code.pop(code, None)

    def _lifecycle_call(self, method_name: str, *args, **kwargs):
        tracker = self.lifecycle_tracker
        if tracker is None:
            return None
        method = getattr(tracker, method_name, None)
        if not callable(method):
            return None
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            self.warnings.append(f"REALTIME_LIFECYCLE_{method_name.upper()}_FAILED:{exc}")
            return None

    def _now(self) -> datetime:
        return self.clock().replace(microsecond=0)


def _candidate_watchable(candidate: Candidate) -> bool:
    if candidate.state in {
        CandidateState.DETECTED,
        CandidateState.HYDRATING,
        CandidateState.WATCHING,
        CandidateState.WAIT_DATA,
        CandidateState.READY,
    }:
        return True
    return (
        candidate.state == CandidateState.BLOCKED
        and candidate.block_type == BlockType.TEMPORARY
        and candidate.can_recover
    )


def _timestamp(value: Optional[datetime] = None) -> str:
    return (value or datetime.now()).replace(microsecond=0).isoformat()


def _payload_text(payload: dict, *keys: str) -> str:
    trace = dict(payload.get("transport_trace") or {})
    metadata = dict(payload.get("metadata") or {})
    metadata_trace = dict(metadata.get("transport_trace") or {})
    for key in keys:
        value = payload.get(key) or trace.get(key) or metadata.get(key) or metadata_trace.get(key)
        if value:
            return str(value)
    return ""
