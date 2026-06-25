from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from trading.strategy.candidates import normalize_code
from trading.strategy.setup_data_readiness import RealtimeCoverageType


@dataclass(frozen=True)
class RealtimeSubscriptionReadinessSnapshot:
    code: str
    calculated_at: str
    subscription_requested: bool = False
    subscription_target_selected: bool = False
    subscription_selected: bool = False
    subscription_active: bool = False
    subscription_budget_deferred: bool = False
    subscription_sources: tuple[str, ...] = ()
    subscription_primary_source: str = ""
    subscription_screen_no: str = ""
    subscription_generation: int = 0
    subscription_active_since: str = ""
    relevant_source_added_at: str = ""
    readiness_relevant_source: str = ""
    readiness_relevant_source_reason: str = ""
    readiness_relevant_source_generation: int = 0
    baseline_source_type: str = ""
    candidate_active_source_types: tuple[str, ...] = ()
    candidate_primary_source: str = ""
    coverage_type: str = RealtimeCoverageType.NONE.value
    latest_tick_at: str = ""
    latest_tick_age_sec: float = 999999.0
    latest_tick_source: str = ""
    core_tick_at: str = ""
    gateway_tick_at: str = ""
    post_subscription_tick_verified: bool = False
    price: float = 0.0
    source_priorities: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["subscription_sources"] = list(self.subscription_sources)
        payload["candidate_active_source_types"] = list(self.candidate_active_source_types)
        payload["source_priorities"] = dict(self.source_priorities or {})
        return payload


class RealtimeSubscriptionReadinessProvider:
    def __init__(
        self,
        subscription_manager: Any,
        market_data: Any | None = None,
        *,
        clock: Any = datetime.now,
        max_tick_age_sec: int = 10,
        lifecycle_tracker: Any | None = None,
    ) -> None:
        self.subscription_manager = subscription_manager
        self.market_data = market_data
        self.clock = clock
        self.max_tick_age_sec = max(1, int(max_tick_age_sec or 10))
        self.lifecycle_tracker = lifecycle_tracker

    def snapshot(self, code: str, selected_theme_id: str = "", candidate: Any | None = None, now: datetime | None = None) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        clean_code = normalize_code(code)
        manager = self.subscription_manager
        records = getattr(manager, "records", {}) or {}
        record = records.get(clean_code)
        target_codes = self._target_codes()
        subscription_requested = bool(record and getattr(record, "sources", None))
        subscription_target_selected = clean_code in target_codes
        subscription_selected = subscription_target_selected
        subscription_active = bool(record and getattr(record, "active", False) and clean_code in getattr(manager, "code_to_screen", {}))
        budget_deferred = bool(subscription_requested and not subscription_target_selected and not subscription_active)
        sources = tuple(sorted(str(source) for source in list(getattr(record, "sources", set()) or []) if str(source))) if record else ()
        primary_source = str(getattr(record, "primary_source", "") if record else "")
        active_since = str(getattr(record, "active_since", "") if record else "")
        source_added = dict(getattr(record, "source_added_at_by_source", {}) or {}) if record else {}
        source_generations = dict(getattr(record, "source_generation_by_source", {}) or {}) if record else {}
        active_source_map = self._candidate_active_source_map(candidate)
        expansion_required = self._expansion_required(candidate, active_source_map, selected_theme_id)
        relevant_source, relevant_reason = self._relevant_source(sources, candidate, expansion_required=expansion_required)
        relevant_source_added_at = str(source_added.get(relevant_source) or max(source_added.values(), default=""))
        relevant_generation = int(source_generations.get(relevant_source, 0) or 0)
        active_source_types = tuple(sorted({str(item.get("source_type") or "") for item in active_source_map if str(item.get("source_type") or "")}))
        candidate_primary_source = str(dict(getattr(candidate, "metadata", {}) or {}).get("primary_source") or "")
        tick_payload = self._tick_payload(clean_code, current)
        post_subscription_tick_verified = self._post_subscription_tick_verified(
            tick_payload,
            active_since=active_since,
            relevant_source_added_at=relevant_source_added_at,
            subscription_active=subscription_active,
        )
        lifecycle = self._lifecycle_snapshot(clean_code, now=current)
        if lifecycle:
            lifecycle_state = str(lifecycle.get("lifecycle_state") or "")
            subscription_active = bool(lifecycle.get("transport_active"))
            active_since = str(lifecycle.get("registration_ack_baseline_at_utc") or active_since)
            lifecycle_tick_payload = {
                "latest_tick_at": lifecycle.get("last_tick_at_utc") or tick_payload.get("latest_tick_at") or "",
                "latest_tick_age_sec": lifecycle.get("latest_tick_age_sec", tick_payload.get("latest_tick_age_sec")),
                "latest_tick_source": "REALTIME" if lifecycle.get("last_tick_at_utc") else tick_payload.get("latest_tick_source") or "",
                "core_tick_at": lifecycle.get("last_tick_at_utc") or tick_payload.get("core_tick_at") or "",
                "gateway_tick_at": lifecycle.get("first_tick_gateway_at_utc") or tick_payload.get("gateway_tick_at") or "",
            }
            tick_payload = _newer_tick_payload(tick_payload, lifecycle_tick_payload)
            post_subscription_tick_verified = self._post_subscription_tick_verified(
                tick_payload,
                active_since=active_since,
                relevant_source_added_at=relevant_source_added_at,
                subscription_active=subscription_active,
            )
            if lifecycle_state in {"COMMAND_ENQUEUED", "COMMAND_DISPATCHED"}:
                subscription_active = False
                post_subscription_tick_verified = False
            elif lifecycle_state == "ACKED_WAIT_FIRST_TICK":
                subscription_active = True
                post_subscription_tick_verified = self._post_subscription_tick_verified(
                    tick_payload,
                    active_since=active_since,
                    relevant_source_added_at=relevant_source_added_at,
                    subscription_active=subscription_active,
                )
        payload = RealtimeSubscriptionReadinessSnapshot(
            code=clean_code,
            calculated_at=current.isoformat(),
            subscription_requested=subscription_requested,
            subscription_target_selected=subscription_target_selected,
            subscription_selected=subscription_selected,
            subscription_active=subscription_active,
            subscription_budget_deferred=budget_deferred,
            subscription_sources=sources,
            subscription_primary_source=primary_source,
            subscription_screen_no=str(getattr(record, "screen_no", "") if record else ""),
            subscription_generation=int(getattr(record, "subscription_generation", 0) if record else 0),
            subscription_active_since=active_since,
            relevant_source_added_at=relevant_source_added_at,
            readiness_relevant_source=relevant_source,
            readiness_relevant_source_reason=relevant_reason,
            readiness_relevant_source_generation=relevant_generation,
            baseline_source_type=relevant_source,
            candidate_active_source_types=active_source_types,
            candidate_primary_source=candidate_primary_source,
            coverage_type=self._coverage_type(sources),
            latest_tick_at=str(tick_payload.get("latest_tick_at") or ""),
            latest_tick_age_sec=float(tick_payload.get("latest_tick_age_sec") or 999999.0),
            latest_tick_source=str(tick_payload.get("latest_tick_source") or ""),
            core_tick_at=str(tick_payload.get("core_tick_at") or ""),
            gateway_tick_at=str(tick_payload.get("gateway_tick_at") or ""),
            post_subscription_tick_verified=post_subscription_tick_verified,
            price=float(tick_payload.get("price") or 0.0),
            source_priorities=dict(getattr(record, "source_priorities", {}) or {}) if record else {},
        ).to_dict()
        if lifecycle:
            payload.update(
                {
                    "subscription_lifecycle_schema_version": lifecycle.get("schema_version") or "",
                    "subscription_lifecycle_state": lifecycle.get("lifecycle_state") or "",
                    "budget_deferred": bool(lifecycle.get("budget_deferred")),
                    "command_enqueued": bool(lifecycle.get("command_enqueued")),
                    "command_dispatched": bool(lifecycle.get("command_dispatched")),
                    "acked": bool(lifecycle.get("acked")),
                    "transport_active": bool(lifecycle.get("transport_active")),
                    "first_tick_verified": bool(lifecycle.get("first_tick_verified")),
                    "decision_fresh": bool(lifecycle.get("decision_fresh")),
                    "stale": bool(lifecycle.get("stale")),
                    "released": bool(lifecycle.get("released")),
                    "failed": bool(lifecycle.get("failed")),
                    "register_command_id": lifecycle.get("register_command_id") or "",
                    "registration_ack_baseline_at_utc": lifecycle.get("registration_ack_baseline_at_utc") or "",
                    "first_tick_at_utc": lifecycle.get("first_tick_at_utc") or "",
                    "last_tick_at_utc": lifecycle.get("last_tick_at_utc") or "",
                    "ack_to_first_tick_ms": lifecycle.get("ack_to_first_tick_ms"),
                }
            )
        return payload

    def snapshots(
        self,
        codes: Iterable[str],
        context_by_code: Mapping[str, Mapping[str, Any]] | None = None,
        candidate_by_code: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, dict[str, Any]]:
        current = (now or self.clock()).replace(microsecond=0)
        result: dict[str, dict[str, Any]] = {}
        contexts = dict(context_by_code or {})
        candidates = dict(candidate_by_code or {})
        for raw_code in codes:
            code = normalize_code(raw_code)
            context = dict(contexts.get(code) or {})
            theme = dict(context.get("theme") or {})
            selected_theme_id = str(context.get("selected_theme_id") or theme.get("theme_id") or "")
            result[code] = self.snapshot(code, selected_theme_id=selected_theme_id, candidate=candidates.get(code), now=current)
        return result

    def _target_codes(self) -> set[str]:
        target_loader = getattr(self.subscription_manager, "_target_records", None)
        if not callable(target_loader):
            return set(getattr(self.subscription_manager, "code_to_screen", {}) or {})
        try:
            return {normalize_code(getattr(record, "code", "")) for record in list(target_loader() or []) if normalize_code(getattr(record, "code", ""))}
        except Exception:
            return set(getattr(self.subscription_manager, "code_to_screen", {}) or {})

    def _lifecycle_snapshot(self, code: str, now: datetime | None = None) -> dict[str, Any]:
        tracker = self.lifecycle_tracker or getattr(self.subscription_manager, "lifecycle_tracker", None)
        refresher = getattr(tracker, "refresh_staleness", None)
        if callable(refresher):
            try:
                return dict(refresher(code, now=now) or {})
            except Exception:
                pass
        snapshot = getattr(tracker, "snapshot", None)
        if not callable(snapshot):
            return {}
        try:
            return dict(snapshot(code) or {})
        except Exception:
            return {}

    def _tick_payload(self, code: str, now: datetime) -> dict[str, Any]:
        if self.market_data is None:
            return {}
        loader = getattr(self.market_data, "latest_tick", None)
        if not callable(loader):
            return {}
        tick = loader(code)
        if tick is None:
            return {}
        tick_at = getattr(tick, "timestamp", None)
        age = 999999.0
        if isinstance(tick_at, datetime):
            age = max(0.0, (now.replace(tzinfo=None) - tick_at.replace(tzinfo=None)).total_seconds())
            tick_text = tick_at.replace(microsecond=0).isoformat()
        else:
            tick_text = ""
        metadata = dict(getattr(tick, "metadata", {}) or {})
        return {
            "latest_tick_at": tick_text,
            "latest_tick_age_sec": round(age, 3),
            "latest_tick_source": str(metadata.get("price_source") or "REALTIME"),
            "core_tick_at": tick_text,
            "gateway_tick_at": str(metadata.get("gateway_received_at") or metadata.get("received_at") or ""),
            "price": float(getattr(tick, "price", 0) or 0.0),
        }

    def _post_subscription_tick_verified(
        self,
        tick_payload: Mapping[str, Any],
        *,
        active_since: str,
        relevant_source_added_at: str,
        subscription_active: bool,
    ) -> bool:
        if not subscription_active:
            return False
        tick_at = _parse_time(tick_payload.get("latest_tick_at"))
        if tick_at is None:
            return False
        latest_source = str(tick_payload.get("latest_tick_source") or "").upper()
        if latest_source != "REALTIME":
            return False
        try:
            age = float(tick_payload.get("latest_tick_age_sec") or 999999.0)
        except (TypeError, ValueError):
            age = 999999.0
        if age > self.max_tick_age_sec:
            return False
        baselines = [item for item in (_parse_time(active_since), _parse_time(relevant_source_added_at)) if item is not None]
        return not baselines or tick_at >= max(baselines)

    @staticmethod
    def _coverage_type(sources: Iterable[str]) -> str:
        values = {str(source or "") for source in list(sources or []) if str(source or "")}
        if not values:
            return RealtimeCoverageType.NONE.value
        if len(values) > 1:
            return RealtimeCoverageType.MULTI_SOURCE.value
        source = next(iter(values))
        if source in {"reboot_v2_theme_expansion"}:
            return RealtimeCoverageType.THEME_EXPANSION.value
        if source in {"reboot_v2_theme_board", "theme_board_watch"}:
            return RealtimeCoverageType.THEME_BOARD.value
        if source in {"reboot_v2_opening_seed"}:
            return RealtimeCoverageType.OPENING_SEED.value
        if source in {"reboot_v2_position", "virtual_position", "holding"}:
            return RealtimeCoverageType.POSITION_PROTECTED.value
        if source in {"reboot_v2_candidate", "candidate_watch"}:
            return RealtimeCoverageType.CANDIDATE.value
        return RealtimeCoverageType.CANDIDATE.value

    @staticmethod
    def _relevant_source(sources: Iterable[str], candidate: Any | None = None, *, expansion_required: bool = False) -> tuple[str, str]:
        values = set(str(source or "") for source in list(sources or []) if str(source or ""))
        metadata = dict(getattr(candidate, "metadata", {}) or {}) if candidate is not None else {}
        requested = str(metadata.get("realtime_subscription_source") or "")
        if requested and requested in values:
            return requested, "CANDIDATE_REQUESTED_SOURCE"
        if expansion_required:
            for preferred in ("reboot_v2_theme_expansion", "theme_expansion"):
                if preferred in values:
                    return preferred, "EXPANSION_REQUIRED_SOURCE"
        for preferred in ("reboot_v2_candidate", "candidate_watch", "reboot_v2_theme_board", "theme_board_watch", "reboot_v2_opening_seed"):
            if preferred in values:
                return preferred, "GENERAL_CANDIDATE_SOURCE"
        for fallback in ("reboot_v2_theme_expansion", "theme_expansion"):
            if fallback in values:
                return fallback, "GENERAL_ONLY_EXPANSION_COVERAGE_SOURCE"
        selected = sorted(values)[0] if values else ""
        return selected, "FALLBACK_ACTIVE_SOURCE" if selected else "NO_ACTIVE_SOURCE"

    @staticmethod
    def _candidate_active_source_map(candidate: Any | None) -> list[dict[str, Any]]:
        metadata = dict(getattr(candidate, "metadata", {}) or {}) if candidate is not None else {}
        ingestion = dict(metadata.get("candidate_ingestion") or {})
        source_map = dict(ingestion.get("source_map") or {})
        result: list[dict[str, Any]] = []
        for item in source_map.values():
            entry = dict(item or {}) if isinstance(item, Mapping) else {}
            if entry and bool(entry.get("active", True)):
                result.append(entry)
        return result

    @staticmethod
    def _expansion_required(candidate: Any | None, active_source_map: list[dict[str, Any]], selected_theme_id: str) -> bool:
        metadata = dict(getattr(candidate, "metadata", {}) or {}) if candidate is not None else {}
        selected = str(selected_theme_id or "")
        for entry in active_source_map:
            source_type = str(entry.get("source_type") or entry.get("source") or "").lower()
            theme_id = str(entry.get("theme_id") or entry.get("selected_theme_id") or "")
            if source_type in {"reboot_v2_theme_expansion", "theme_expansion"} and selected and theme_id == selected:
                return True
        if active_source_map:
            return bool(metadata.get("expansion_only") or metadata.get("theme_expansion_only"))
        if bool(metadata.get("expansion_only") or metadata.get("theme_expansion_only")):
            return True
        source_type = str(metadata.get("source_type") or metadata.get("source") or "").lower()
        source_theme = str(metadata.get("theme_id") or metadata.get("selected_theme_id") or metadata.get("primary_theme_id") or "")
        return source_type in {"reboot_v2_theme_expansion", "theme_expansion"} and bool(selected and source_theme == selected)


LOCAL_TIMEZONE = timezone(timedelta(hours=9))


def _newer_tick_payload(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    left_payload = dict(left or {})
    right_payload = dict(right or {})
    left_at = _parse_time(left_payload.get("latest_tick_at"))
    right_at = _parse_time(right_payload.get("latest_tick_at"))
    if left_at is None:
        return right_payload
    if right_at is None:
        return left_payload
    return left_payload if left_at >= right_at else right_payload


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=LOCAL_TIMEZONE)
        return dt.astimezone(timezone.utc).replace(microsecond=0)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TIMEZONE)
    return dt.astimezone(timezone.utc).replace(microsecond=0)
