from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from trading.broker.models import ConditionCandidateEvent, ConditionInfo, Signal
from trading.strategy.candidates import is_valid_stock_code, normalize_code
from trading.strategy.models import StrategyProfile

if TYPE_CHECKING:
    from storage.db import TradingDatabase


@dataclass
class ConditionProfile:
    id: Optional[int] = None
    condition_name: str = ""
    strategy_profile: StrategyProfile = StrategyProfile.KOSDAQ_THEME_PROFILE
    enabled: bool = True
    priority: int = 0
    purpose: str = ""
    last_resolved_index: Optional[int] = None


@dataclass
class RegisteredCondition:
    condition_name: str
    condition_index: int
    screen_no: str
    strategy_profile: StrategyProfile
    purpose: str = ""
    registered_at: str = ""


@dataclass
class ConditionProfileSeedResult:
    inserted: int = 0
    existing: int = 0
    warnings: list[str] = field(default_factory=list)


DEFAULT_CONDITION_PROFILES = [
    ConditionProfile(
        condition_name="코스닥_테마주_눌림",
        strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
        enabled=True,
        priority=90,
        purpose="kosdaq_pullback_candidate",
    ),
    ConditionProfile(
        condition_name="코스피_대형주_주도",
        strategy_profile=StrategyProfile.KOSPI_LEADER_PROFILE,
        enabled=True,
        priority=85,
        purpose="kospi_leader_candidate",
    ),
    ConditionProfile(
        condition_name="주도테마_넓은후보",
        strategy_profile=StrategyProfile.THEME_DISCOVERY_PROFILE,
        enabled=True,
        priority=70,
        purpose="theme_broad_candidate",
    ),
]
KNOWN_CONDITION_PURPOSES = {
    "kosdaq_pullback_candidate",
    "kospi_leader_candidate",
    "theme_broad_candidate",
}


class ConditionProfileRepository:
    def __init__(self, db: "TradingDatabase") -> None:
        self.db = db

    def upsert_profile(self, profile: ConditionProfile) -> ConditionProfile:
        return self.db.upsert_condition_profile(profile)

    def enabled_profiles(self) -> list[ConditionProfile]:
        return self.db.list_condition_profiles(enabled=True)

    def update_last_resolved_index(self, condition_name: str, condition_index: int) -> None:
        self.db.update_condition_last_resolved_index(condition_name, condition_index)


def ensure_default_condition_profiles(db: "TradingDatabase") -> ConditionProfileSeedResult:
    repository = ConditionProfileRepository(db)
    result = ConditionProfileSeedResult()
    existing_profiles = {profile.condition_name: profile for profile in db.list_condition_profiles(enabled=None)}

    for profile in db.list_condition_profiles(enabled=None):
        if not str(profile.purpose or "").strip():
            result.warnings.append(f"CONDITION_PROFILE_PURPOSE_MISSING:{profile.condition_name}")
        elif profile.purpose not in KNOWN_CONDITION_PURPOSES:
            result.warnings.append(f"CONDITION_PROFILE_PURPOSE_UNKNOWN:{profile.condition_name}:{profile.purpose}")
        if _looks_mojibake(profile.condition_name):
            result.warnings.append(f"CONDITION_PROFILE_NAME_MOJIBAKE_SUSPECTED:{profile.condition_name}")

    for default in DEFAULT_CONDITION_PROFILES:
        existing = existing_profiles.get(default.condition_name)
        if existing is not None:
            if (
                existing.strategy_profile != default.strategy_profile
                or str(existing.purpose or "") != str(default.purpose or "")
            ):
                repository.upsert_profile(
                    ConditionProfile(
                        id=existing.id,
                        condition_name=existing.condition_name,
                        strategy_profile=default.strategy_profile,
                        enabled=existing.enabled,
                        priority=existing.priority,
                        purpose=default.purpose,
                        last_resolved_index=existing.last_resolved_index,
                    )
                )
                result.warnings.append(f"CONDITION_PROFILE_DEFAULT_UPDATED:{existing.condition_name}")
            result.existing += 1
            continue
        repository.upsert_profile(default)
        result.inserted += 1
    result.warnings = _dedupe(result.warnings)
    return result


class KiwoomConditionAdapter:
    def __init__(
        self,
        client,
        repository: ConditionProfileRepository,
        *,
        clock=None,
        max_realtime_conditions: int = 10,
        condition_screen_base: int = 7600,
        load_timeout_sec: int = 5,
        dedupe_window_sec: int = 3,
    ) -> None:
        self.client = client
        self.repository = repository
        self.clock = clock or datetime.now
        self.max_realtime_conditions = max(0, int(max_realtime_conditions))
        self.condition_screen_base = int(condition_screen_base)
        self.load_timeout_sec = max(1, int(load_timeout_sec))
        self.dedupe_window_sec = max(0, int(dedupe_window_sec))
        self.condition_candidate_included = Signal()
        self.condition_candidate_removed = Signal()
        self.warnings: list[str] = []
        self.registered_conditions: dict[tuple[str, int], RegisteredCondition] = {}
        self.screen_to_condition: dict[str, tuple[str, int]] = {}
        self._recent_events: dict[tuple[str, str, int, str, str], datetime] = {}
        self._load_requested_at: Optional[datetime] = None
        self._load_succeeded = False
        self._connect_client_signals()

    def start(self, now: Optional[datetime] = None) -> list[str]:
        self.warnings.clear()
        self._load_requested_at = _clean_time(now or self.clock())
        self._load_succeeded = False
        result = self.client.load_conditions()
        if int(result or 0) <= 0:
            self._warn("CONDITION_LOAD_REQUEST_FAILED")
        return list(self.warnings)

    def stop(self) -> list[str]:
        for condition in list(self.registered_conditions.values()):
            self.client.stop_condition(condition.screen_no, condition.condition_name, condition.condition_index)
        self.registered_conditions.clear()
        self.screen_to_condition.clear()
        return list(self.warnings)

    def check_load_timeout(self, now: Optional[datetime] = None) -> list[str]:
        current = _clean_time(now or self.clock())
        if self._load_requested_at is None or self._load_succeeded:
            return list(self.warnings)
        if current >= self._load_requested_at + timedelta(seconds=self.load_timeout_sec):
            self._warn("CONDITION_LOAD_TIMEOUT")
        return list(self.warnings)

    def handle_condition_load_result(self, success: bool, message: str = "") -> None:
        if not success:
            self._load_succeeded = False
            self._warn(f"CONDITION_LOAD_FAILED:{message}")
            return
        self._load_succeeded = True

    def handle_condition_loaded(self, conditions: list[ConditionInfo]) -> None:
        if not self._load_succeeded and self._load_requested_at is not None:
            self._warn("CONDITION_LOADED_BEFORE_SUCCESS")
            return
        self._load_succeeded = True
        self._register_resolved_conditions(conditions)

    def handle_tr_condition(
        self,
        screen_no: str,
        code_list: str,
        condition_name: str,
        condition_index: int,
        next_flag: str,
    ) -> None:
        registered = self._registered_condition(condition_name, condition_index)
        if registered is None:
            return
        for code in _parse_code_list(code_list):
            self._emit_candidate_event("include", registered, code)
        if str(next_flag) == "2":
            result = self.client.send_condition(
                screen_no,
                condition_name,
                int(condition_index),
                realtime=True,
                search_type=2,
            )
            if int(result or 0) <= 0:
                self._warn(f"CONDITION_CONTINUATION_FAILED:{condition_name}:{condition_index}")

    def handle_real_condition(
        self,
        code: str,
        event_type: str,
        condition_name: str,
        condition_index: int,
    ) -> None:
        registered = self._registered_condition(condition_name, condition_index)
        if registered is None:
            return
        normalized_event = "include" if str(event_type).upper() == "I" else "remove" if str(event_type).upper() == "D" else ""
        if not normalized_event:
            self._warn(f"UNKNOWN_REAL_CONDITION_EVENT:{event_type}:{condition_name}:{condition_index}")
            return
        self._emit_candidate_event(normalized_event, registered, code)

    def get_code_name(self, code: str) -> str:
        if hasattr(self.client, "get_code_name"):
            return str(self.client.get_code_name(code) or "")
        return ""

    def _connect_client_signals(self) -> None:
        if hasattr(self.client, "condition_load_result"):
            self.client.condition_load_result.connect(self.handle_condition_load_result)
        if hasattr(self.client, "condition_loaded"):
            self.client.condition_loaded.connect(self.handle_condition_loaded)
        if hasattr(self.client, "condition_tr_received"):
            self.client.condition_tr_received.connect(self.handle_tr_condition)
        if hasattr(self.client, "condition_real_received"):
            self.client.condition_real_received.connect(self.handle_real_condition)

    def _register_resolved_conditions(self, conditions: list[ConditionInfo]) -> None:
        by_name: dict[str, list[ConditionInfo]] = {}
        for condition in conditions:
            by_name.setdefault(condition.name, []).append(condition)

        profiles = sorted(self.repository.enabled_profiles(), key=lambda profile: profile.priority, reverse=True)
        selected = profiles[: self.max_realtime_conditions]
        for skipped in profiles[self.max_realtime_conditions :]:
            self._warn(f"CONDITION_PROFILE_SKIPPED_LIMIT:{skipped.condition_name}")

        for index, profile in enumerate(selected):
            matches = by_name.get(profile.condition_name, [])
            if not matches:
                self._warn(f"CONDITION_PROFILE_UNRESOLVED:{profile.condition_name}")
                continue
            unique_indices = sorted({match.index for match in matches})
            if len(unique_indices) != 1:
                self._warn(f"CONDITION_PROFILE_AMBIGUOUS:{profile.condition_name}")
                continue
            condition_index = unique_indices[0]
            screen_no = f"{self.condition_screen_base + index:04d}"
            result = self.client.send_condition(screen_no, profile.condition_name, condition_index, realtime=True)
            if int(result or 0) <= 0:
                self._warn(f"CONDITION_REGISTER_FAILED:{profile.condition_name}:{condition_index}")
                continue
            self.repository.update_last_resolved_index(profile.condition_name, condition_index)
            registered = RegisteredCondition(
                condition_name=profile.condition_name,
                condition_index=condition_index,
                screen_no=screen_no,
                strategy_profile=profile.strategy_profile,
                purpose=profile.purpose,
                registered_at=_clean_time(self.clock()).isoformat(),
            )
            key = (registered.condition_name, registered.condition_index)
            self.registered_conditions[key] = registered
            self.screen_to_condition[screen_no] = key

    def _registered_condition(self, condition_name: str, condition_index: int) -> Optional[RegisteredCondition]:
        key = (str(condition_name), int(condition_index))
        registered = self.registered_conditions.get(key)
        if registered is not None:
            return registered
        name_match = any(condition.condition_name == condition_name for condition in self.registered_conditions.values())
        index_match = any(condition.condition_index == int(condition_index) for condition in self.registered_conditions.values())
        if name_match or index_match:
            self._warn(f"CONDITION_EVENT_MISMATCH:{condition_name}:{condition_index}")
        else:
            self._warn(f"UNREGISTERED_CONDITION_EVENT:{condition_name}:{condition_index}")
        return None

    def _emit_candidate_event(self, event_type: str, condition: RegisteredCondition, code: str) -> bool:
        raw_code = str(code or "").strip().upper()
        clean_code = normalize_code(code)
        if not is_valid_stock_code(clean_code):
            self._warn(f"INVALID_CONDITION_CODE:{condition.condition_name}:{raw_code}")
        now = _clean_time(self.clock())
        key = (now.date().isoformat(), condition.condition_name, condition.condition_index, clean_code, event_type)
        previous = self._recent_events.get(key)
        if previous is not None and now <= previous + timedelta(seconds=self.dedupe_window_sec):
            return False
        self._recent_events[key] = now
        event = ConditionCandidateEvent(
            condition_name=condition.condition_name,
            code=clean_code,
            condition_index=condition.condition_index,
            event_type=event_type,
            source="condition",
            strategy_profile=condition.strategy_profile.value,
            purpose=condition.purpose,
        )
        if event_type == "include":
            self.condition_candidate_included.emit(event)
        else:
            self.condition_candidate_removed.emit(event)
        return True

    def _warn(self, warning: str) -> None:
        self.warnings.append(warning)


def _parse_code_list(code_list: str) -> list[str]:
    return [str(code or "").strip() for code in str(code_list or "").split(";") if normalize_code(code)]


def _clean_time(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def _looks_mojibake(value: str) -> bool:
    text = str(value or "")
    return any(marker in text for marker in ["�", "Ã", "Â", "ì", "ë", "í", "ê"])


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
