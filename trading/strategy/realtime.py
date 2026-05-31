from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from trading.strategy.candidates import normalize_code
from trading.strategy.models import BlockType, Candidate, CandidateState


SOURCE_PRIORITIES = {
    "index": 100,
    "leading_stock": 90,
    "semiconductor_signal": 90,
    "virtual_position": 85,
    "virtual_order": 84,
    "holding": 80,
    "candidate_watch": 50,
    "theme_universe": 45,
}
PROTECTED_SOURCES = {
    "index",
    "leading_stock",
    "semiconductor_signal",
    "virtual_order",
    "virtual_position",
    "holding",
}
FALLBACK_SOURCE_ORDER = {
    "index": 0,
    "leading_stock": 1,
    "semiconductor_signal": 1,
    "holding": 2,
    "virtual_position": 3,
    "virtual_order": 4,
    "candidate_watch": 5,
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

    def add_source(self, source: str, priority: int, protected: bool) -> None:
        self.sources.add(source)
        self.source_priorities[source] = priority
        self.source_protected[source] = protected
        self.refresh()

    def remove_source(self, source: str) -> None:
        self.sources.discard(source)
        self.source_priorities.pop(source, None)
        self.source_protected.pop(source, None)
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
    ) -> None:
        self.client = client
        self.max_codes = max_codes
        self.screen_base = screen_base
        self.screen_size = screen_size
        self.records: dict[str, SubscriptionRecord] = {}
        self.code_to_screen: dict[str, str] = {}
        self.screen_to_codes: dict[str, set[str]] = {}
        self.warnings: list[str] = []

    def ensure_subscription(
        self,
        code: str,
        source: str,
        priority: Optional[int] = None,
        protected: Optional[bool] = None,
    ) -> SubscriptionRecord:
        clean_code = normalize_code(code)
        resolved_priority = SOURCE_PRIORITIES.get(source, 0) if priority is None else priority
        resolved_protected = source in PROTECTED_SOURCES if protected is None else protected
        record = self.records.get(clean_code)
        if record is None:
            record = SubscriptionRecord(code=clean_code)
            self.records[clean_code] = record
        record.add_source(source, resolved_priority, resolved_protected)
        return record

    def remove_subscription(self, code: str, source: str) -> None:
        clean_code = normalize_code(code)
        record = self.records.get(clean_code)
        if record is None:
            return
        record.remove_source(source)
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
        self.warnings = []
        target_records = self._target_records()
        target_codes = {record.code for record in target_records}
        active_codes = set(self.code_to_screen)

        self._remove_codes(active_codes - target_codes)
        self._register_records([record for record in target_records if record.code not in self.code_to_screen])

        for record in self.records.values():
            record.active = record.code in self.code_to_screen
            record.screen_no = self.code_to_screen.get(record.code, "")
        return set(self.code_to_screen)

    def remove_realtime(self, codes: Iterable[str]) -> None:
        self.warnings = []
        self._remove_codes([normalize_code(code) for code in codes])

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

    def _register_records(self, records: list[SubscriptionRecord]) -> None:
        for record in sorted(records, key=self._registration_sort_key):
            screen_no = self._screen_for_new_code()
            self.client.register_realtime([record.code], screen_no=screen_no)
            self.code_to_screen[record.code] = screen_no
            self.screen_to_codes.setdefault(screen_no, set()).add(record.code)

    def _remove_codes(self, codes: Iterable[str]) -> None:
        codes = [code for code in codes if code]
        if not codes:
            return
        missing_mapping = [code for code in codes if code not in self.code_to_screen]
        if missing_mapping:
            self.warnings.append("REALTIME_REMOVE_ALL_FALLBACK")
            self.client.remove_all_realtime()
            self.code_to_screen.clear()
            self.screen_to_codes.clear()
            try:
                self._register_records(self._target_records())
            except Exception as exc:
                self.warnings.append(f"PROTECTED_REREGISTER_FAILED:{exc}")
                raise
            return
        for code in codes:
            screen_no = self.code_to_screen.pop(code)
            self.client.remove_realtime([code], screen_no=screen_no)
            codes_for_screen = self.screen_to_codes.get(screen_no)
            if codes_for_screen is not None:
                codes_for_screen.discard(code)
                if not codes_for_screen:
                    self.screen_to_codes.pop(screen_no, None)

    def _screen_for_new_code(self) -> str:
        screen_number = self.screen_base
        while True:
            screen_no = f"{screen_number:04d}"
            if len(self.screen_to_codes.get(screen_no, set())) < self.screen_size:
                return screen_no
            screen_number += 1


def _candidate_watchable(candidate: Candidate) -> bool:
    if candidate.state in {CandidateState.DETECTED, CandidateState.WATCHING, CandidateState.READY}:
        return True
    return (
        candidate.state == CandidateState.BLOCKED
        and candidate.block_type == BlockType.TEMPORARY
        and candidate.can_recover
    )
