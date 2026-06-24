from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from trading.strategy.candidates import normalize_code
from trading.strategy.setup_data_readiness import RealtimeCoverageType


@dataclass(frozen=True)
class RealtimeSubscriptionReadinessSnapshot:
    code: str
    calculated_at: str
    subscription_selected: bool = False
    subscription_active: bool = False
    subscription_budget_deferred: bool = False
    subscription_sources: tuple[str, ...] = ()
    subscription_primary_source: str = ""
    subscription_screen_no: str = ""
    subscription_generation: int = 0
    subscription_active_since: str = ""
    relevant_source_added_at: str = ""
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
        payload["source_priorities"] = dict(self.source_priorities or {})
        return payload


class RealtimeSubscriptionReadinessProvider:
    def __init__(self, subscription_manager: Any, market_data: Any | None = None, *, clock: Any = datetime.now, max_tick_age_sec: int = 10) -> None:
        self.subscription_manager = subscription_manager
        self.market_data = market_data
        self.clock = clock
        self.max_tick_age_sec = max(1, int(max_tick_age_sec or 10))

    def snapshot(self, code: str, selected_theme_id: str = "", candidate: Any | None = None, now: datetime | None = None) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        clean_code = normalize_code(code)
        manager = self.subscription_manager
        records = getattr(manager, "records", {}) or {}
        record = records.get(clean_code)
        target_codes = self._target_codes()
        subscription_selected = record is not None
        subscription_active = bool(record and getattr(record, "active", False) and clean_code in getattr(manager, "code_to_screen", {}))
        budget_deferred = bool(record and clean_code not in target_codes and not subscription_active)
        sources = tuple(sorted(str(source) for source in list(getattr(record, "sources", set()) or []) if str(source))) if record else ()
        primary_source = str(getattr(record, "primary_source", "") if record else "")
        active_since = str(getattr(record, "active_since", "") if record else "")
        source_added = dict(getattr(record, "source_added_at_by_source", {}) or {}) if record else {}
        relevant_source = self._relevant_source(sources, candidate)
        relevant_source_added_at = str(source_added.get(relevant_source) or max(source_added.values(), default=""))
        tick_payload = self._tick_payload(clean_code, current)
        post_subscription_tick_verified = self._post_subscription_tick_verified(
            tick_payload,
            active_since=active_since,
            relevant_source_added_at=relevant_source_added_at,
            subscription_active=subscription_active,
        )
        return RealtimeSubscriptionReadinessSnapshot(
            code=clean_code,
            calculated_at=current.isoformat(),
            subscription_selected=subscription_selected,
            subscription_active=subscription_active,
            subscription_budget_deferred=budget_deferred,
            subscription_sources=sources,
            subscription_primary_source=primary_source,
            subscription_screen_no=str(getattr(record, "screen_no", "") if record else ""),
            subscription_generation=int(getattr(record, "subscription_generation", 0) if record else 0),
            subscription_active_since=active_since,
            relevant_source_added_at=relevant_source_added_at,
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
    def _relevant_source(sources: Iterable[str], candidate: Any | None = None) -> str:
        values = set(str(source or "") for source in list(sources or []) if str(source or ""))
        metadata = dict(getattr(candidate, "metadata", {}) or {}) if candidate is not None else {}
        requested = str(metadata.get("realtime_subscription_source") or "")
        if requested and requested in values:
            return requested
        for preferred in ("reboot_v2_theme_expansion", "reboot_v2_candidate", "reboot_v2_theme_board", "reboot_v2_opening_seed"):
            if preferred in values:
                return preferred
        return sorted(values)[0] if values else ""


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None, microsecond=0)
    except ValueError:
        return None
