from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any, Iterable, Mapping

from trading.strategy.candidate_state_contract import CandidateStateContractService
from trading.strategy.candidates import normalize_code
from trading.strategy.setup_features import SETUP_ROUTER_FEATURE_SCHEMA_VERSION, SetupFeatureBuilder
from trading.strategy.setup_router_v3 import (
    SETUP_ROUTER_OUTPUT_MODE,
    SETUP_ROUTER_SCHEMA_VERSION,
    SETUP_ROUTER_STATE_VERSION,
    SETUP_ROUTER_VERSION,
    SetupRouterConfig,
    SetupRouterV3,
)


@dataclass
class SetupRouterV3RuntimePipeline:
    db: Any
    market_data: Any | None = None
    candle_builder: Any | None = None
    config: SetupRouterConfig | None = None
    state_contract: CandidateStateContractService | None = None
    dirty_evaluator_provider: Any | None = None
    latest_entry_decision_provider: Any | None = None
    candidate_code_provider: Any | None = None
    clock: Any = datetime.now

    def __post_init__(self) -> None:
        self.config = self.config or SetupRouterConfig.from_env()
        self.state_contract = self.state_contract or CandidateStateContractService(self.db, clock=self.clock)
        self.feature_builder = SetupFeatureBuilder(
            market_data=self.market_data,
            candle_builder=self.candle_builder,
            min_completed_1m_candles=self.config.min_completed_1m_candles,
            max_tick_age_sec=self.config.max_tick_age_sec,
            entry_decision_max_age_sec=self.config.entry_decision_max_age_sec,
        )
        self.router = SetupRouterV3(self.config)
        self.last_run_at: datetime | None = None
        self.last_run_saved_at: datetime | None = None
        self.last_periodic_reconcile_at: datetime | None = None
        self.reconcile_cursor = 0
        self.last_result: list[dict[str, Any]] = []
        self.last_summary: dict[str, Any] = _base_summary(enabled=bool(self.config.enabled), status="IDLE" if self.config.enabled else "DISABLED")
        self._last_feature_signature: dict[str, tuple[Any, ...]] = {}
        self._last_entry_signature_by_code: dict[str, tuple[Any, ...]] = {}

    def run_if_due(self, now: datetime | None = None, **_: Any) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        if not self.config.enabled:
            self.last_summary = _disabled_summary("CONFIG_DISABLED", current)
            self.last_result = []
            return dict(self.last_summary)
        if self.last_run_at is not None:
            age = (current - self.last_run_at).total_seconds()
            if age < float(self.config.interval_sec):
                summary = dict(self.last_summary)
                summary["status"] = "SKIPPED"
                summary["skip_reason"] = "INTERVAL_NOT_DUE"
                return summary
        return self.run(current)

    def run(self, now: datetime | None = None, **_: Any) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        started = perf_counter()
        if not self.config.enabled:
            self.last_summary = _disabled_summary("CONFIG_DISABLED", current)
            self.last_result = []
            return dict(self.last_summary)
        trade_date = current.date().isoformat()
        candidates = list(self.db.list_candidates(trade_date=trade_date) or [])
        contracts: dict[str, Any] = {}
        candidate_by_key: dict[str, Any] = {}
        eligible_candidates: list[Any] = []
        skipped_reasons: Counter[str] = Counter()
        duplicate_code_skip_count = 0
        seen_codes: set[str] = set()
        for candidate in candidates:
            code = normalize_code(candidate.code)
            contract = self.state_contract.snapshot(candidate)
            contracts[self._candidate_instance_id(candidate)] = contract
            if not contract.evaluation_eligible:
                skipped_reasons[contract.evaluation_eligibility] += 1
                continue
            if code in seen_codes:
                duplicate_code_skip_count += 1
                continue
            seen_codes.add(code)
            eligible_candidates.append(candidate)
            candidate_by_key[self._candidate_instance_id(candidate)] = candidate

        latest_context_by_code = self._latest_contexts(trade_date, eligible_candidates)
        latest_entry_by_key = self._latest_entries(trade_date, eligible_candidates)
        previous_by_key = self._previous_observations(trade_date)
        state_by_candidate = self._states(trade_date, eligible_candidates)
        leases_by_code = self._leases(trade_date)
        incremental_codes = self._incremental_codes(latest_entry_by_key)
        context_changed_codes = self._context_changed_codes(eligible_candidates, latest_context_by_code)
        candle_changed_codes = self._candle_boundary_changed_codes(eligible_candidates)
        provider_codes = self._provider_codes()
        selected_codes = set(incremental_codes) | set(context_changed_codes) | set(candle_changed_codes) | set(provider_codes)
        periodic_due = self._periodic_due(current)
        periodic_candidates: list[Any] = []
        if periodic_due:
            periodic_candidates = self._periodic_candidates(eligible_candidates)
            selected_codes.update(normalize_code(candidate.code) for candidate in periodic_candidates)
            self.last_periodic_reconcile_at = current
        selected_candidates = [
            candidate
            for candidate in eligible_candidates
            if normalize_code(candidate.code) in selected_codes
        ]
        if not selected_candidates and eligible_candidates and self.last_run_at is None:
            selected_candidates = self._periodic_candidates(eligible_candidates)
            periodic_candidates = list(selected_candidates)
            periodic_due = True
            self.last_periodic_reconcile_at = current
        selected_candidates = selected_candidates[: max(1, int(self.config.max_candidates_per_cycle))]

        observations: list[dict[str, Any]] = []
        warnings: list[str] = []
        evaluated_count = 0
        fingerprint_skip_count = 0
        for candidate in selected_candidates:
            code = normalize_code(candidate.code)
            candidate_instance_id = self._candidate_instance_id(candidate)
            try:
                previous = self._previous_for_candidate(previous_by_key, candidate_instance_id)
                states = state_by_candidate.get(candidate_instance_id, {})
                feature = self.feature_builder.build(
                    candidate,
                    now=current,
                    contract_snapshot=contracts.get(candidate_instance_id),
                    strategy_context=latest_context_by_code.get(code) or dict(getattr(candidate, "metadata", {}) or {}).get("strategy_context_v3") or {},
                    entry_decision=latest_entry_by_key.get((candidate.id, code)) or latest_entry_by_key.get((None, code)) or {},
                    previous_observation=previous,
                    setup_states=states,
                    expansion_lease=self._lease_for_theme(leases_by_code.get(code, ()), latest_context_by_code.get(code) or {}),
                )
                signature = self._feature_signature(feature, states)
                if signature == self._last_feature_signature.get(candidate_instance_id) and not periodic_due:
                    fingerprint_skip_count += 1
                    continue
                self._last_feature_signature[candidate_instance_id] = signature
                result = [item.to_dict() for item in self.router.classify(feature)]
                observations.extend(result)
                evaluated_count += 1
            except Exception as exc:  # pragma: no cover - defensive runtime isolation
                warnings.append(f"SETUP_ROUTER_CANDIDATE_ERROR:{code}:{exc.__class__.__name__}")
                continue

        state_counts = {"state_write_count": 0, "transition_write_count": 0}
        saved_count = 0
        if self.config.save_history and observations:
            state_saver = getattr(self.db, "save_setup_router_states", None)
            if callable(state_saver):
                state_counts = dict(state_saver(observations) or state_counts)
            saver = getattr(self.db, "save_setup_observations", None)
            if callable(saver):
                saved_count = int(saver(observations) or 0)

        status_counts = Counter(str(item.get("router_status") or "UNKNOWN") for item in observations)
        shape_counts = Counter(str(item.get("shape_status") or "UNKNOWN") for item in observations)
        context_counts = Counter(str(item.get("context_status") or "UNKNOWN") for item in observations)
        type_counts = Counter(str(item.get("setup_type") or "UNKNOWN") for item in observations)
        reason_counts = Counter()
        for item in observations:
            reason_counts.update([str(reason) for reason in list(item.get("reason_codes") or []) if str(reason)])
        duration_ms = int(round((perf_counter() - started) * 1000))
        summary = {
            **_base_summary(enabled=True, status="OK" if observations else "IDLE"),
            "calculated_at": current.isoformat(),
            "trade_date": trade_date,
            "candidate_count": len(candidates),
            "eligible_candidate_count": len(eligible_candidates),
            "evaluated_count": evaluated_count,
            "skipped_count": sum(skipped_reasons.values()),
            "observation_count": len(observations),
            "saved_count": saved_count,
            "valid_observe_count": status_counts.get("VALID_OBSERVE", 0),
            "pending_count": status_counts.get("PENDING", 0),
            "data_wait_count": status_counts.get("DATA_WAIT", 0),
            "context_blocked_count": status_counts.get("CONTEXT_BLOCKED", 0),
            "avoid_count": status_counts.get("AVOID", 0),
            "unknown_count": status_counts.get("UNKNOWN", 0),
            "invalidated_count": status_counts.get("INVALIDATED", 0),
            "expired_count": status_counts.get("EXPIRED", 0),
            "status_counts": dict(status_counts),
            "shape_counts": dict(shape_counts),
            "context_counts": dict(context_counts),
            "setup_type_counts": dict(type_counts),
            "top_reasons": [{"reason": key, "count": value} for key, value in reason_counts.most_common(10)],
            "skipped_reasons": dict(skipped_reasons),
            "observations": _dashboard_observations(observations),
            "incremental_input_count": len(set(incremental_codes) | set(context_changed_codes) | set(candle_changed_codes) | set(provider_codes)),
            "periodic_input_count": len(periodic_candidates),
            "duplicate_code_skip_count": duplicate_code_skip_count,
            "fingerprint_skip_count": fingerprint_skip_count,
            "state_write_count": int(state_counts.get("state_write_count") or 0),
            "transition_write_count": int(state_counts.get("transition_write_count") or 0),
            "reconcile_cursor": self.reconcile_cursor,
            "reconcile_total_eligible": len(eligible_candidates),
            "starved_candidate_count": max(0, len(eligible_candidates) - len(selected_candidates)),
            "last_periodic_reconcile_at": self.last_periodic_reconcile_at.isoformat() if self.last_periodic_reconcile_at else "",
            "warnings": warnings,
            "duration_ms": duration_ms,
        }
        if self._should_save_run(summary, current, periodic_due=periodic_due):
            run_saver = getattr(self.db, "save_setup_router_run", None)
            if callable(run_saver):
                run_saver(summary)
            self.last_run_saved_at = current
        self.last_run_at = current
        self.last_result = observations
        self.last_summary = summary
        return dict(summary)

    def _candidate_instance_id(self, candidate: Any) -> str:
        code = normalize_code(getattr(candidate, "code", ""))
        metadata = dict(getattr(candidate, "metadata", {}) or {})
        return str(metadata.get("candidate_instance_id") or f"{candidate.trade_date}:{code}:{candidate.id or 0}")

    def _latest_contexts(self, trade_date: str, candidates: list[Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        loader = getattr(self.db, "latest_strategy_context", None)
        for candidate in candidates:
            code = normalize_code(getattr(candidate, "code", ""))
            context = dict(dict(getattr(candidate, "metadata", {}) or {}).get("strategy_context_v3") or {})
            if not context and callable(loader):
                context = dict(loader(trade_date=trade_date, code=code) or {})
            if context:
                result[code] = context
        return result

    def _latest_entries(self, trade_date: str, candidates: list[Any]) -> dict[tuple[int | None, str], dict[str, Any]]:
        result: dict[tuple[int | None, str], dict[str, Any]] = {}
        candidate_ids = [getattr(candidate, "id", None) for candidate in candidates if getattr(candidate, "id", None) is not None]
        codes = [normalize_code(getattr(candidate, "code", "")) for candidate in candidates]
        loader = getattr(self.db, "latest_entry_decisions_per_candidate", None)
        if callable(loader):
            rows = list(loader(trade_date=trade_date, candidate_ids=candidate_ids, codes=codes) or [])
        else:
            rows = list(getattr(self.db, "latest_entry_decisions", lambda **_: [])(trade_date=trade_date) or [])
        provider_rows = self._provider_entry_decisions()
        rows.extend(provider_rows)
        for item in rows:
            payload = _entry_payload(item)
            code = normalize_code(str(payload.get("code") or ""))
            candidate_id = payload.get("candidate_id")
            if candidate_id is not None:
                result[(candidate_id, code)] = payload
            result.setdefault((None, code), payload)
        return result

    def _previous_observations(self, trade_date: str) -> dict[tuple[str, str], dict[str, Any]]:
        loader = getattr(self.db, "list_setup_observations_latest", None)
        if not callable(loader):
            return {}
        result: dict[tuple[str, str], dict[str, Any]] = {}
        rows = list(
            loader(
                trade_date=trade_date,
                router_version=SETUP_ROUTER_VERSION,
                limit=max(1000, self.config.max_candidates_per_cycle * 20),
            )
            or []
        )
        for item in rows:
            payload = dict(item or {})
            result[(str(payload.get("candidate_instance_id") or ""), str(payload.get("setup_type") or ""))] = payload
        return result

    def _previous_for_candidate(self, previous_by_key: Mapping[tuple[str, str], dict[str, Any]], candidate_instance_id: str) -> dict[str, Any]:
        priority = ("VWAP_RECLAIM", "BREAKOUT_RETEST", "LEADER_FIRST_PULLBACK")
        for setup_type in priority:
            item = previous_by_key.get((candidate_instance_id, setup_type))
            if item and bool(item.get("primary_setup")):
                return dict(item)
        for setup_type in priority:
            item = previous_by_key.get((candidate_instance_id, setup_type))
            if item:
                return dict(item)
        return {}

    def _states(self, trade_date: str, candidates: list[Any]) -> dict[str, dict[str, dict[str, Any]]]:
        loader = getattr(self.db, "list_setup_router_states", None)
        if not callable(loader):
            return {}
        candidate_ids = [self._candidate_instance_id(candidate) for candidate in candidates]
        rows = list(loader(trade_date=trade_date, candidate_instance_ids=candidate_ids, active_only=False, router_version=SETUP_ROUTER_VERSION) or [])
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            payload = dict(row or {})
            candidate_instance_id = str(payload.get("candidate_instance_id") or "")
            setup_type = str(payload.get("setup_type") or "")
            existing = result.setdefault(candidate_instance_id, {}).get(setup_type)
            if existing is None or int(payload.get("setup_generation") or 0) >= int(existing.get("setup_generation") or 0):
                result.setdefault(candidate_instance_id, {})[setup_type] = payload
        return result

    def _leases(self, trade_date: str) -> dict[str, tuple[dict[str, Any], ...]]:
        loader = getattr(self.db, "list_theme_expansion_leases", None)
        if not callable(loader):
            return {}
        result: dict[str, list[dict[str, Any]]] = {}
        for lease in list(loader(trade_date=trade_date, active_only=False) or []):
            payload = dict(lease or {})
            result.setdefault(normalize_code(str(payload.get("code") or "")), []).append(payload)
        return {key: tuple(value) for key, value in result.items()}

    def _lease_for_theme(self, leases: Iterable[Mapping[str, Any]], context: Mapping[str, Any]) -> dict[str, Any]:
        theme = dict(context.get("theme") or {})
        theme_id = str(context.get("selected_theme_id") or theme.get("theme_id") or "")
        payloads = [dict(item or {}) for item in leases or []]
        for lease in payloads:
            if str(lease.get("theme_id") or "") == theme_id:
                return lease
        return payloads[0] if payloads else {}

    def _incremental_codes(self, latest_entry_by_key: Mapping[tuple[int | None, str], dict[str, Any]]) -> set[str]:
        codes: set[str] = set()
        for (_candidate_id, code), payload in latest_entry_by_key.items():
            if not code:
                continue
            signature = (payload.get("id"), payload.get("calculated_at"), payload.get("entry_status"), payload.get("price_location"))
            if self._last_entry_signature_by_code.get(code) != signature:
                codes.add(code)
                self._last_entry_signature_by_code[code] = signature
        provider = self.dirty_evaluator_provider
        evaluator = getattr(provider, "evaluator", provider)
        for decision in list(getattr(evaluator, "last_decisions", ()) or ()):
            payload = _entry_payload(decision)
            code = normalize_code(str(payload.get("code") or ""))
            if code:
                codes.add(code)
        dashboard = dict(getattr(evaluator, "last_entry_dashboard_payload", {}) or {})
        for item in list(dashboard.get("items") or []):
            code = normalize_code(str(dict(item or {}).get("code") or ""))
            if code:
                codes.add(code)
        return codes

    def _provider_entry_decisions(self) -> list[dict[str, Any]]:
        provider = self.latest_entry_decision_provider
        if provider is None:
            return []
        try:
            rows = provider() if callable(provider) else provider
        except Exception:
            return []
        return [_entry_payload(item) for item in list(rows or [])]

    def _provider_codes(self) -> set[str]:
        provider = self.candidate_code_provider
        if provider is None:
            return set()
        try:
            rows = provider() if callable(provider) else provider
        except Exception:
            return set()
        return {normalize_code(str(code or "")) for code in list(rows or []) if normalize_code(str(code or ""))}

    def _context_changed_codes(self, candidates: list[Any], contexts: Mapping[str, dict[str, Any]]) -> set[str]:
        changed = set()
        for candidate in candidates:
            code = normalize_code(candidate.code)
            context = dict(contexts.get(code) or {})
            candidate_instance_id = self._candidate_instance_id(candidate)
            current_context_id = str(context.get("context_id") or "")
            previous = self._last_feature_signature.get(candidate_instance_id)
            if previous is None or (current_context_id and current_context_id not in previous):
                changed.add(code)
        return changed

    def _candle_boundary_changed_codes(self, candidates: list[Any]) -> set[str]:
        if self.candle_builder is None:
            return set()
        changed = set()
        loader = getattr(self.candle_builder, "completed_candles", None)
        if not callable(loader):
            return changed
        for candidate in candidates:
            code = normalize_code(candidate.code)
            candles = list(loader(code, 1) or [])
            boundary = ""
            if candles:
                last = candles[-1]
                boundary = str(getattr(last, "start_at", "") if not isinstance(last, Mapping) else last.get("candle_at") or last.get("start_at") or "")
            previous = getattr(self, "_last_candle_boundary_by_code", {})
            if not hasattr(self, "_last_candle_boundary_by_code"):
                self._last_candle_boundary_by_code = {}
            if boundary and self._last_candle_boundary_by_code.get(code) != boundary:
                changed.add(code)
                self._last_candle_boundary_by_code[code] = boundary
        return changed

    def _periodic_due(self, now: datetime) -> bool:
        if self.last_periodic_reconcile_at is None:
            return True
        return (now - self.last_periodic_reconcile_at).total_seconds() >= max(1, int(self.config.periodic_reconcile_sec))

    def _periodic_candidates(self, candidates: list[Any]) -> list[Any]:
        if not candidates:
            return []
        page_size = max(1, int(self.config.max_candidates_per_cycle))
        start = min(self.reconcile_cursor, len(candidates) - 1)
        ordered = candidates[start:] + candidates[:start]
        page = ordered[:page_size]
        self.reconcile_cursor = (start + len(page)) % len(candidates)
        return page

    def _feature_signature(self, feature: Any, states: Mapping[str, Any]) -> tuple[Any, ...]:
        state_material = tuple(
            sorted(
                (
                    setup_type,
                    str(dict(state or {}).get("feature_fingerprint") or ""),
                    str(dict(state or {}).get("lifecycle_state") or ""),
                    int(dict(state or {}).get("setup_generation") or 0),
                )
                for setup_type, state in dict(states or {}).items()
            )
        )
        return (
            feature.context_id,
            feature.latest_completed_candle_at,
            feature.entry_decision_id,
            feature.entry_decision_at,
            feature.tick_at,
            round(float(feature.current_price or 0.0), 2),
            state_material,
        )

    def _should_save_run(self, summary: Mapping[str, Any], now: datetime, *, periodic_due: bool) -> bool:
        if int(summary.get("observation_count") or 0) > 0:
            return True
        if int(summary.get("transition_write_count") or 0) > 0:
            return True
        if periodic_due:
            return True
        if summary.get("warnings"):
            return True
        if self.last_run_saved_at is None:
            return True
        return (now - self.last_run_saved_at).total_seconds() >= max(1, int(self.config.run_heartbeat_sec))


def _dashboard_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority = {"VALID_OBSERVE": 0, "PENDING": 1, "DATA_WAIT": 2, "CONTEXT_BLOCKED": 3, "AVOID": 4, "UNKNOWN": 5}
    rows = sorted(
        observations,
        key=lambda item: (
            priority.get(str(item.get("router_status") or ""), 9),
            0 if item.get("primary_setup") else 1,
            -float(item.get("setup_quality_score") or 0.0),
            str(item.get("code") or ""),
        ),
    )
    return [
        {
            "code": item.get("code", ""),
            "name": item.get("name", ""),
            "theme_name": item.get("theme_name", ""),
            "setup_type": item.get("setup_type", ""),
            "shape_status": item.get("shape_status", ""),
            "lifecycle_state": item.get("lifecycle_state", ""),
            "context_status": item.get("context_status", ""),
            "router_status": item.get("router_status", ""),
            "entry_alignment_status": item.get("entry_alignment_status", ""),
            "setup_quality_score": item.get("setup_quality_score", 0.0),
            "current_price": item.get("current_price", 0.0),
            "price_structure": dict(item.get("price_structure") or {}),
            "reason_codes": list(item.get("reason_codes") or [])[:8],
            "updated_at": item.get("calculated_at", ""),
            "primary_setup": bool(item.get("primary_setup")),
            "setup_generation": item.get("setup_generation", 1),
            "setup_instance_id": item.get("setup_instance_id", ""),
            "post_subscription_tick_verified": bool(item.get("post_subscription_tick_verified", True)),
            "entry_decision_age_sec": item.get("entry_decision_age_sec", 0.0),
            "router_version": item.get("router_version", SETUP_ROUTER_VERSION),
            "last_material_change_at": item.get("last_material_change_at", ""),
            "observe_only": True,
        }
        for item in rows[:50]
    ]


def _disabled_summary(reason: str, now: datetime) -> dict[str, Any]:
    summary = _base_summary(enabled=False, status="DISABLED")
    summary.update(
        {
            "calculated_at": now.isoformat(),
            "blocking_reason": reason,
            "observation_count": 0,
            "valid_observe_count": 0,
            "pending_count": 0,
            "data_wait_count": 0,
            "context_blocked_count": 0,
            "avoid_count": 0,
            "unknown_count": 0,
            "observations": [],
        }
    )
    return summary


def _base_summary(*, enabled: bool, status: str) -> dict[str, Any]:
    return {
        "enabled": bool(enabled),
        "status": status,
        "schema_version": SETUP_ROUTER_SCHEMA_VERSION,
        "feature_schema_version": SETUP_ROUTER_FEATURE_SCHEMA_VERSION,
        "router_version": SETUP_ROUTER_VERSION,
        "state_version": SETUP_ROUTER_STATE_VERSION,
        "output_mode": SETUP_ROUTER_OUTPUT_MODE,
        "observe_only": True,
        "safety": _safety_flags(),
        "ready_allowed": False,
        "candidate_promotion_allowed": False,
        "opportunity_rank_allowed": False,
        "order_intent_allowed": False,
        "live_order_allowed": False,
    }


def _safety_flags() -> dict[str, Any]:
    return {
        "observe_only": True,
        "ready_allowed": False,
        "candidate_promotion_allowed": False,
        "opportunity_rank_allowed": False,
        "order_intent_allowed": False,
        "live_order_allowed": False,
        "recommended_position_size_multiplier": 0,
        "quantity": 0,
    }


def _entry_payload(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict() or {})
    data = getattr(item, "__dict__", {})
    return dict(data or {})
