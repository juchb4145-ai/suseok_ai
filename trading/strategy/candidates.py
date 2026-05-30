from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable, Optional

from trading.broker.models import ConditionCandidateEvent
from trading.strategy.models import (
    BlockType,
    Candidate,
    CandidateEvent,
    CandidateSourceType,
    CandidateState,
    StrategyProfile,
)

if TYPE_CHECKING:
    from storage.db import TradingDatabase


FORBIDDEN_ORDER_STATES = {
    CandidateState.ORDER_DECIDED,
    CandidateState.ORDER_SENT,
    CandidateState.FILLED,
    CandidateState.CANCELLED,
}
QUALITY_ACTIONABLE = "actionable"
QUALITY_DISCOVERY_ONLY = "discovery_only"
QUALITY_UNMAPPED = "unmapped"
QUALITY_INVALID_CODE = "invalid_code"
QUALITY_DATA_WAIT = "data_wait"
QUALITY_STATUSES = {
    QUALITY_ACTIONABLE,
    QUALITY_DISCOVERY_ONLY,
    QUALITY_UNMAPPED,
    QUALITY_INVALID_CODE,
    QUALITY_DATA_WAIT,
}


class CandidateLifecycle:
    @classmethod
    def validate_transition(
        cls,
        from_state: Optional[CandidateState],
        to_state: CandidateState,
    ) -> None:
        if to_state in FORBIDDEN_ORDER_STATES:
            raise ValueError(f"{to_state.value} transitions are not allowed for OBSERVE candidate lifecycle")
        if from_state is None:
            if to_state != CandidateState.DETECTED:
                raise ValueError("New candidates must start as DETECTED")
            return
        if from_state == to_state:
            return
        allowed = {
            CandidateState.DETECTED: {
                CandidateState.WATCHING,
                CandidateState.BLOCKED,
                CandidateState.REMOVED,
                CandidateState.EXPIRED,
            },
            CandidateState.WATCHING: {
                CandidateState.DETECTED,
                CandidateState.READY,
                CandidateState.BLOCKED,
                CandidateState.REMOVED,
                CandidateState.EXPIRED,
            },
            CandidateState.READY: {
                CandidateState.WATCHING,
                CandidateState.BLOCKED,
                CandidateState.REMOVED,
                CandidateState.EXPIRED,
            },
            CandidateState.BLOCKED: {
                CandidateState.WATCHING,
                CandidateState.READY,
                CandidateState.REMOVED,
                CandidateState.EXPIRED,
            },
            CandidateState.REMOVED: {CandidateState.DETECTED},
            CandidateState.EXPIRED: {CandidateState.DETECTED},
        }
        if to_state not in allowed.get(from_state, set()):
            raise ValueError(f"{from_state.value} -> {to_state.value} is not allowed for OBSERVE candidate lifecycle")

    @classmethod
    def transition(cls, candidate: Candidate, to_state: CandidateState) -> Candidate:
        cls.validate_transition(candidate.state, to_state)
        candidate.state = to_state
        return candidate


class CandidateCollector:
    def __init__(
        self,
        db: "TradingDatabase",
        client=None,
        clock: Optional[Callable[[], datetime]] = None,
        trade_date_provider: Optional[Callable[[], str]] = None,
        default_ttl_minutes: int = 30,
        condition_event_allowed: Optional[Callable[[datetime], bool]] = None,
    ) -> None:
        self.db = db
        self.client = None
        self.clock = clock or datetime.now
        self.trade_date_provider = trade_date_provider
        self.default_ttl_minutes = default_ttl_minutes
        self.condition_event_allowed = condition_event_allowed
        self.warnings: list[str] = []
        if client is not None:
            self.attach(client)

    def attach(self, client) -> None:
        self.client = client
        client.condition_candidate_included.connect(self.handle_condition_include)
        client.condition_candidate_removed.connect(self.handle_condition_remove)

    def set_condition_event_allowed(self, callback: Optional[Callable[[datetime], bool]]) -> None:
        self.condition_event_allowed = callback

    def handle_condition_include(self, event: ConditionCandidateEvent) -> Optional[Candidate]:
        now = self._now_text()
        trade_date = self._trade_date()
        code = normalize_code(event.code)
        if not is_valid_stock_code(code):
            self._reject_condition_event(event, "include")
            return None
        if not self._condition_event_is_allowed(event, "include"):
            return None
        existing = self.db.load_candidate(trade_date, code)
        if existing is None:
            metadata = {"condition_indices": {event.condition_name: event.condition_index}}
            self._merge_condition_metadata(metadata, event)
            candidate = Candidate(
                trade_date=trade_date,
                code=code,
                name=self._lookup_name(code),
                strategy_profile=self._profile_from_condition_metadata(metadata),
                sources=[CandidateSourceType.CONDITION],
                state=CandidateState.DETECTED,
                detected_at=now,
                last_seen_at=now,
                expires_at=self._expires_at(),
                condition_names=[event.condition_name],
                metadata=metadata,
            )
            self._refresh_condition_entry_metadata(candidate)
            CandidateLifecycle.validate_transition(None, candidate.state)
            return self.db.save_candidate_with_events(
                candidate,
                [
                    self._event(
                        "candidate_detected",
                        candidate,
                        None,
                        CandidateState.DETECTED,
                        CandidateSourceType.CONDITION,
                        "condition include",
                        self._condition_payload(event, candidate),
                    )
                ],
            )

        previous_state = existing.state
        previous_index = dict(existing.metadata.get("condition_indices", {})).get(event.condition_name)
        previous_profile = dict(existing.metadata.get("condition_profiles", {})).get(event.condition_name)
        previous_purpose = dict(existing.metadata.get("condition_purposes", {})).get(event.condition_name)
        source_added = CandidateSourceType.CONDITION not in existing.sources
        condition_added = event.condition_name not in existing.condition_names
        if existing.state in {CandidateState.REMOVED, CandidateState.EXPIRED}:
            CandidateLifecycle.transition(existing, CandidateState.DETECTED)
            existing.block_type = BlockType.NONE
            existing.can_recover = False
            existing.recheck_after_sec = 0

        add_unique(existing.sources, CandidateSourceType.CONDITION)
        add_unique(existing.condition_names, event.condition_name)
        condition_indices = dict(existing.metadata.get("condition_indices", {}))
        condition_indices[event.condition_name] = event.condition_index
        existing.metadata["condition_indices"] = condition_indices
        self._merge_condition_metadata(existing.metadata, event)
        existing.strategy_profile = self._profile_from_condition_metadata(existing.metadata, existing.strategy_profile)
        self._refresh_condition_entry_metadata(existing)
        existing.last_seen_at = now
        existing.expires_at = self._expires_at()

        profile_changed = previous_profile != dict(existing.metadata.get("condition_profiles", {})).get(event.condition_name)
        purpose_changed = previous_purpose != dict(existing.metadata.get("condition_purposes", {})).get(event.condition_name)
        index_changed = previous_index != event.condition_index
        reactivated = previous_state in {CandidateState.REMOVED, CandidateState.EXPIRED}
        if not (reactivated or source_added or condition_added or index_changed or profile_changed or purpose_changed):
            return self.db.save_candidate(existing)

        event_type = "candidate_reactivated" if reactivated else "candidate_merged"
        return self.db.save_candidate_with_events(
            existing,
            [
                self._event(
                    event_type,
                    existing,
                    previous_state,
                    existing.state,
                    CandidateSourceType.CONDITION,
                    "condition include",
                    self._condition_payload(event, existing),
                )
            ],
        )

    def handle_condition_remove(self, event: ConditionCandidateEvent) -> Optional[Candidate]:
        trade_date = self._trade_date()
        code = normalize_code(event.code)
        if not is_valid_stock_code(code):
            self._reject_condition_event(event, "remove")
            return None
        if not self._condition_event_is_allowed(event, "remove"):
            return None
        candidate = self.db.load_candidate(trade_date, code)
        if candidate is None:
            return None
        previous_state = candidate.state
        if event.condition_name in candidate.condition_names:
            candidate.condition_names.remove(event.condition_name)
        condition_indices = dict(candidate.metadata.get("condition_indices", {}))
        condition_indices.pop(event.condition_name, None)
        candidate.metadata["condition_indices"] = condition_indices
        for key in ("condition_profiles", "condition_purposes"):
            values = dict(candidate.metadata.get(key, {}))
            values.pop(event.condition_name, None)
            candidate.metadata[key] = values
        candidate.strategy_profile = self._profile_from_condition_metadata(candidate.metadata, candidate.strategy_profile)
        self._refresh_condition_entry_metadata(candidate)

        events = [
            self._event(
                "condition_removed",
                candidate,
                previous_state,
                candidate.state,
                CandidateSourceType.CONDITION,
                "condition remove",
                self._condition_payload(event, candidate),
            )
        ]

        if not candidate.condition_names and CandidateSourceType.CONDITION in candidate.sources:
            candidate.sources.remove(CandidateSourceType.CONDITION)

        if not candidate.sources and candidate.state in {
            CandidateState.DETECTED,
            CandidateState.WATCHING,
            CandidateState.READY,
            CandidateState.BLOCKED,
        }:
            CandidateLifecycle.transition(candidate, CandidateState.REMOVED)
            events.append(
                self._event(
                    "candidate_removed",
                    candidate,
                    previous_state,
                    CandidateState.REMOVED,
                    CandidateSourceType.CONDITION,
                    "all condition sources removed",
                    self._condition_payload(event, candidate),
                )
            )

        candidate.last_seen_at = self._now_text()
        return self.db.save_candidate_with_events(candidate, events)

    def add_manual_debug_candidate(self, code: str, name: str = "") -> Candidate:
        now = self._now_text()
        trade_date = self._trade_date()
        clean_code = normalize_code(code)
        existing = self.db.load_candidate(trade_date, clean_code)
        if existing is None:
            candidate = Candidate(
                trade_date=trade_date,
                code=clean_code,
                name=name,
                sources=[CandidateSourceType.MANUAL_DEBUG],
                state=CandidateState.DETECTED,
                detected_at=now,
                last_seen_at=now,
                expires_at=self._expires_at(),
                metadata={"manual_debug": True},
            )
            return self.db.save_candidate_with_events(
                candidate,
                [
                    self._event(
                        "candidate_detected",
                        candidate,
                        None,
                        CandidateState.DETECTED,
                        CandidateSourceType.MANUAL_DEBUG,
                        "manual debug candidate",
                        {"code": clean_code, "source": CandidateSourceType.MANUAL_DEBUG.value},
                    )
                ],
            )

        previous_state = existing.state
        if existing.state in {CandidateState.REMOVED, CandidateState.EXPIRED}:
            CandidateLifecycle.transition(existing, CandidateState.DETECTED)
        add_unique(existing.sources, CandidateSourceType.MANUAL_DEBUG)
        existing.name = name or existing.name
        existing.metadata["manual_debug"] = True
        existing.last_seen_at = now
        existing.expires_at = self._expires_at()
        event_type = "candidate_reactivated" if previous_state in {CandidateState.REMOVED, CandidateState.EXPIRED} else "candidate_merged"
        return self.db.save_candidate_with_events(
            existing,
            [
                self._event(
                    event_type,
                    existing,
                    previous_state,
                    existing.state,
                    CandidateSourceType.MANUAL_DEBUG,
                    "manual debug candidate",
                    {"code": clean_code, "source": CandidateSourceType.MANUAL_DEBUG.value},
                )
            ],
        )

    def mark_watching(self, code: str, trade_date: Optional[str] = None, reason: str = "") -> Candidate:
        candidate = self._required_candidate(trade_date or self._trade_date(), normalize_code(code))
        previous_state = candidate.state
        CandidateLifecycle.transition(candidate, CandidateState.WATCHING)
        previous_block_type = candidate.block_type
        previous_can_recover = candidate.can_recover
        previous_recheck_after_sec = candidate.recheck_after_sec
        candidate.block_type = BlockType.NONE
        candidate.can_recover = False
        candidate.recheck_after_sec = 0
        if (
            previous_state == CandidateState.WATCHING
            and previous_block_type == candidate.block_type
            and previous_can_recover == candidate.can_recover
            and previous_recheck_after_sec == candidate.recheck_after_sec
        ):
            return candidate
        return self.db.save_candidate_with_events(
            candidate,
            [
                self._event(
                    "state_changed",
                    candidate,
                    previous_state,
                    CandidateState.WATCHING,
                    None,
                    reason or "watching started",
                    {"code": candidate.code},
                )
            ],
        )

    def expire_stale(
        self,
        now: Optional[datetime] = None,
        keep_alive: Optional[Callable[[Candidate, datetime], bool]] = None,
    ) -> list[Candidate]:
        current = now or self.clock()
        expired: list[Candidate] = []
        for candidate in self.db.list_candidates(trade_date=self._trade_date()):
            if candidate.state == CandidateState.BLOCKED and candidate.block_type != BlockType.TEMPORARY:
                continue
            if candidate.state not in {
                CandidateState.DETECTED,
                CandidateState.WATCHING,
                CandidateState.READY,
                CandidateState.BLOCKED,
            }:
                continue
            if not candidate.expires_at:
                continue
            if _parse_datetime(candidate.expires_at) > current:
                continue
            if keep_alive is not None and keep_alive(candidate, current):
                continue
            previous_state = candidate.state
            CandidateLifecycle.transition(candidate, CandidateState.EXPIRED)
            saved = self.db.save_candidate_with_events(
                candidate,
                [
                    self._event(
                        "candidate_expired",
                        candidate,
                        previous_state,
                        CandidateState.EXPIRED,
                        None,
                        "candidate ttl expired",
                        {"code": candidate.code, "expires_at": candidate.expires_at},
                    )
                ],
            )
            expired.append(saved)
        return expired

    def _required_candidate(self, trade_date: str, code: str) -> Candidate:
        candidate = self.db.load_candidate(trade_date, code)
        if candidate is None:
            raise KeyError(f"candidate not found: {trade_date} {code}")
        return candidate

    def _lookup_name(self, code: str) -> str:
        if self.client is None or not hasattr(self.client, "get_code_name"):
            return ""
        try:
            return str(self.client.get_code_name(code) or "")
        except Exception:
            return ""

    def _trade_date(self) -> str:
        if self.trade_date_provider is not None:
            return self.trade_date_provider()
        return self.clock().date().isoformat()

    def _expires_at(self) -> str:
        return self._format_time(self.clock() + timedelta(minutes=self.default_ttl_minutes))

    def _now_text(self) -> str:
        return self._format_time(self.clock())

    @staticmethod
    def _format_time(value: datetime) -> str:
        return value.replace(microsecond=0).isoformat()

    def _event(
        self,
        event_type: str,
        candidate: Candidate,
        from_state: Optional[CandidateState],
        to_state: Optional[CandidateState],
        source: Optional[CandidateSourceType],
        reason: str,
        payload: dict,
    ) -> CandidateEvent:
        return CandidateEvent(
            candidate_id=candidate.id,
            event_type=event_type,
            from_state=from_state,
            to_state=to_state,
            source=source,
            reason=reason,
            created_at=self._now_text(),
            payload=payload,
        )

    def _condition_event_is_allowed(self, event: ConditionCandidateEvent, event_action: str) -> bool:
        if self.condition_event_allowed is None:
            return True
        try:
            allowed = bool(self.condition_event_allowed(self.clock()))
        except Exception as exc:
            self.warnings.append(f"CONDITION_EVENT_SESSION_CHECK_FAILED:{exc}")
            return True
        if allowed:
            return True
        warning = f"MARKET_SESSION_CLOSED_CONDITION_EVENT:{event.condition_name}:{normalize_code(event.code)}"
        self._reject_condition_event(event, event_action, warning=warning, reason="market session closed")
        return False

    def _reject_condition_event(
        self,
        event: ConditionCandidateEvent,
        event_action: str,
        *,
        warning: Optional[str] = None,
        reason: str = "invalid condition code",
    ) -> None:
        warning = warning or f"INVALID_CONDITION_CODE:{event.condition_name}:{event.code}"
        self.warnings.append(warning)
        payload = {
            "raw_code": str(event.code or ""),
            "normalized_code": normalize_code(event.code),
            "condition_name": event.condition_name,
            "condition_index": event.condition_index,
            "event_action": event_action,
            "strategy_profile": str(getattr(event, "strategy_profile", "") or ""),
            "purpose": str(getattr(event, "purpose", "") or ""),
            "source": CandidateSourceType.CONDITION.value,
            "warning": warning,
            "reject_reason": reason,
        }
        self.db.save_candidate_event(
            CandidateEvent(
                candidate_id=None,
                event_type="candidate_rejected",
                from_state=None,
                to_state=None,
                source=CandidateSourceType.CONDITION,
                reason=reason,
                created_at=self._now_text(),
                payload=payload,
            )
        )

    @staticmethod
    def _condition_payload(event: ConditionCandidateEvent, candidate: Candidate) -> dict:
        return {
            "code": candidate.code,
            "condition_name": event.condition_name,
            "condition_index": event.condition_index,
            "strategy_profile": str(getattr(event, "strategy_profile", "") or ""),
            "purpose": str(getattr(event, "purpose", "") or ""),
            "source": CandidateSourceType.CONDITION.value,
            "sources": [source.value for source in candidate.sources],
            "condition_names": list(candidate.condition_names),
        }

    @staticmethod
    def _merge_condition_metadata(metadata: dict, event: ConditionCandidateEvent) -> None:
        strategy_profile = str(getattr(event, "strategy_profile", "") or "")
        purpose = str(getattr(event, "purpose", "") or "")
        if strategy_profile:
            condition_profiles = dict(metadata.get("condition_profiles", {}))
            condition_profiles[event.condition_name] = strategy_profile
            metadata["condition_profiles"] = condition_profiles
        if purpose:
            condition_purposes = dict(metadata.get("condition_purposes", {}))
            condition_purposes[event.condition_name] = purpose
            metadata["condition_purposes"] = condition_purposes

    @staticmethod
    def _profile_from_condition_metadata(
        metadata: dict,
        fallback: Optional[StrategyProfile] = None,
    ) -> Optional[StrategyProfile]:
        raw_profiles = [str(value) for value in dict(metadata.get("condition_profiles", {})).values() if str(value)]
        for value in raw_profiles:
            if value == StrategyProfile.THEME_DISCOVERY_PROFILE.value:
                continue
            try:
                return StrategyProfile(value)
            except ValueError:
                continue
        if raw_profiles:
            try:
                return StrategyProfile(raw_profiles[0])
            except ValueError:
                return fallback
        return fallback

    @staticmethod
    def _refresh_condition_entry_metadata(candidate: Candidate) -> None:
        profiles = dict(candidate.metadata.get("condition_profiles", {}))
        purposes = dict(candidate.metadata.get("condition_purposes", {}))
        discovery_conditions = {
            name
            for name in set(profiles) | set(purposes)
            if _is_theme_discovery_condition(profiles.get(name, ""), purposes.get(name, ""))
        }
        entry_condition_names = [
            name
            for name in candidate.condition_names
            if name not in discovery_conditions
        ]
        discovery_only = (
            CandidateSourceType.CONDITION in candidate.sources
            and bool(discovery_conditions)
            and not entry_condition_names
        )
        candidate.metadata["theme_discovery_condition_names"] = sorted(discovery_conditions)
        candidate.metadata["entry_condition_names"] = entry_condition_names
        candidate.metadata["entry_excluded"] = discovery_only
        if discovery_only:
            candidate.metadata["entry_excluded_reason"] = "theme_broad_candidate"
        else:
            candidate.metadata.pop("entry_excluded_reason", None)


def normalize_code(code: str) -> str:
    value = str(code or "").strip().upper()
    if value.startswith("A") and value[1:].isdigit():
        return value[1:]
    return value


def is_valid_stock_code(code: str) -> bool:
    value = normalize_code(code)
    return len(value) == 6 and value.isdigit()


def candidate_is_discovery_only(candidate: Candidate) -> bool:
    metadata = _candidate_metadata(candidate)
    profiles = {str(value) for value in dict(metadata.get("condition_profiles", {})).values()}
    purposes = {str(value) for value in dict(metadata.get("condition_purposes", {})).values()}
    entry_conditions = list(metadata.get("entry_condition_names") or [])
    if entry_conditions:
        return False
    if bool(metadata.get("entry_excluded")):
        return True
    if StrategyProfile.THEME_DISCOVERY_PROFILE.value in profiles:
        return True
    if "theme_broad_candidate" in purposes:
        return True
    return candidate.strategy_profile == StrategyProfile.THEME_DISCOVERY_PROFILE


def candidate_quality_status(candidate: Candidate, has_active_theme: Optional[bool] = None) -> str:
    metadata = _candidate_metadata(candidate)
    if not is_valid_stock_code(candidate.code):
        return QUALITY_INVALID_CODE
    if has_active_theme is False:
        return QUALITY_UNMAPPED
    if candidate_is_discovery_only(candidate):
        return QUALITY_DISCOVERY_ONLY
    sub_status = str(metadata.get("sub_status") or "")
    insufficient = {str(reason) for reason in list(metadata.get("insufficient_reason") or [])}
    if sub_status == "DATA_INSUFFICIENT" or "DATA_INSUFFICIENT" in insufficient:
        return QUALITY_DATA_WAIT
    return QUALITY_ACTIONABLE


def add_unique(items: list, value) -> None:
    if value not in items:
        items.append(value)


def _candidate_metadata(candidate: Candidate) -> dict:
    return candidate.metadata if isinstance(candidate.metadata, dict) else {}


def _is_theme_discovery_condition(strategy_profile: str, purpose: str) -> bool:
    return (
        str(strategy_profile) == StrategyProfile.THEME_DISCOVERY_PROFILE.value
        or str(purpose) == "theme_broad_candidate"
    )


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)
