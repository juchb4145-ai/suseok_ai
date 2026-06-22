from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from math import ceil
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
        runtime_by_key = self._candidate_runtime(trade_date, eligible_candidates)
        incremental_codes = self._incremental_codes(latest_entry_by_key)
        context_changed_codes = self._context_changed_codes(eligible_candidates, latest_context_by_code)
        candle_changed_codes = self._candle_boundary_changed_codes(eligible_candidates)
        provider_codes = self._provider_codes()
        ttl_due_codes = self._ttl_due_codes(state_by_candidate, current)
        theme_changed_codes = self._theme_changed_codes(eligible_candidates, latest_context_by_code, runtime_by_key)
        periodic_due = self._periodic_due(current)
        periodic_candidates = self._periodic_candidates(eligible_candidates) if periodic_due else []
        queue = self._evaluation_queue(
            eligible_candidates,
            incremental_codes=set(incremental_codes) | set(provider_codes),
            context_codes=set(context_changed_codes) | set(candle_changed_codes) | set(theme_changed_codes),
            ttl_codes=ttl_due_codes,
            periodic_candidates=periodic_candidates,
        )
        selected_entries, deferred_entries, queue_depth_by_priority = self._select_evaluation_entries(queue, runtime_by_key, current)
        if not selected_entries and eligible_candidates and self.last_run_at is None:
            periodic_due = True
            periodic_candidates = self._periodic_candidates(eligible_candidates)
            queue = self._evaluation_queue(
                eligible_candidates,
                incremental_codes=set(),
                context_codes=set(),
                ttl_codes=ttl_due_codes,
                periodic_candidates=periodic_candidates,
            )
            selected_entries, deferred_entries, queue_depth_by_priority = self._select_evaluation_entries(queue, runtime_by_key, current)
        periodic_selected_count = sum(1 for entry in selected_entries if int(entry.get("priority") or 0) == 3)
        cursor_advanced_count = 0
        if periodic_due and periodic_selected_count > 0:
            cursor_advanced_count = self._advance_periodic_cursor(len(eligible_candidates), periodic_selected_count)
            self.last_periodic_reconcile_at = current
        elif periodic_due and not selected_entries:
            self.last_periodic_reconcile_at = current

        observations: list[dict[str, Any]] = []
        warnings: list[str] = []
        evaluated_count = 0
        fingerprint_skip_count = 0
        runtime_updates: list[dict[str, Any]] = []
        for entry in selected_entries:
            candidate = entry["candidate"]
            code = normalize_code(candidate.code)
            candidate_instance_id = self._candidate_instance_id(candidate)
            try:
                previous = self._previous_for_candidate(previous_by_key, candidate_instance_id)
                context = latest_context_by_code.get(code) or dict(getattr(candidate, "metadata", {}) or {}).get("strategy_context_v3") or {}
                selected_theme_id = self._selected_theme_id(context)
                states = self._states_for_theme(state_by_candidate.get(candidate_instance_id, {}), selected_theme_id)
                lease_info = self._lease_selection(leases_by_code.get(code, ()), context)
                entry_decision = latest_entry_by_key.get((candidate.id, code)) or latest_entry_by_key.get((None, code)) or {}
                feature = self.feature_builder.build(
                    candidate,
                    now=current,
                    contract_snapshot=contracts.get(candidate_instance_id),
                    strategy_context=context,
                    entry_decision=entry_decision,
                    previous_observation=previous,
                    setup_states=states,
                    expansion_lease=dict(lease_info.get("lease") or {}),
                    selected_theme_lease_required=bool(lease_info.get("required")),
                    other_theme_lease_count=int(lease_info.get("other_theme_lease_count") or 0),
                )
                signature = self._feature_signature(feature, states)
                if signature == self._last_feature_signature.get(candidate_instance_id) and not periodic_due:
                    fingerprint_skip_count += 1
                    runtime_updates.append(
                        self._runtime_update(candidate, current, context, entry_decision, feature.latest_completed_candle_at, source=str(entry.get("source") or ""), skipped="FEATURE_SIGNATURE_UNCHANGED")
                    )
                    continue
                self._last_feature_signature[candidate_instance_id] = signature
                result = [item.to_dict() for item in self.router.classify(feature)]
                observations.extend(result)
                evaluated_count += 1
                runtime_updates.append(
                    self._runtime_update(
                        candidate,
                        current,
                        context,
                        entry_decision,
                        feature.latest_completed_candle_at,
                        source=str(entry.get("source") or ""),
                        incremental=int(entry.get("priority") or 0) == 0,
                        periodic=int(entry.get("priority") or 0) == 3,
                        ttl=int(entry.get("priority") or 0) == 2,
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive runtime isolation
                warnings.append(f"SETUP_ROUTER_CANDIDATE_ERROR:{code}:{exc.__class__.__name__}")
                continue
        for entry in deferred_entries:
            candidate = entry["candidate"]
            code = normalize_code(candidate.code)
            context = latest_context_by_code.get(code) or dict(getattr(candidate, "metadata", {}) or {}).get("strategy_context_v3") or {}
            runtime_updates.append(
                self._runtime_update(
                    candidate,
                    current,
                    context,
                    latest_entry_by_key.get((candidate.id, code)) or latest_entry_by_key.get((None, code)) or {},
                    "",
                    source=str(entry.get("source") or ""),
                    deferred=True,
                    skipped=str(entry.get("deferred_reason") or "CAPACITY_DEFERRED"),
                )
            )

        state_counts = {"state_write_count": 0, "transition_write_count": 0}
        saved_count = 0
        if self.config.save_history and observations:
            state_saver = getattr(self.db, "save_setup_router_states", None)
            if callable(state_saver):
                state_counts = dict(state_saver(observations) or state_counts)
            saver = getattr(self.db, "save_setup_observations", None)
            if callable(saver):
                saved_count = int(saver(observations) or 0)
        runtime_saver = getattr(self.db, "save_setup_router_candidate_runtime", None)
        if callable(runtime_saver) and runtime_updates:
            try:
                runtime_saver(runtime_updates)
            except Exception as exc:  # pragma: no cover - diagnostics must not stop runtime
                warnings.append(f"SETUP_ROUTER_RUNTIME_STATE_SAVE_ERROR:{exc.__class__.__name__}")

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
            "ttl_input_count": len(ttl_due_codes),
            "theme_changed_input_count": len(theme_changed_codes),
            "duplicate_code_skip_count": duplicate_code_skip_count,
            "fingerprint_skip_count": fingerprint_skip_count,
            "state_write_count": int(state_counts.get("state_write_count") or 0),
            "transition_write_count": int(state_counts.get("transition_write_count") or 0),
            "no_change_skip_count": int(state_counts.get("no_change_skip_count") or 0),
            "reconcile_cursor": self.reconcile_cursor,
            "reconcile_total_eligible": len(eligible_candidates),
            "deferred_incremental_count": sum(1 for item in deferred_entries if int(item.get("priority") or 0) == 0),
            "deferred_context_count": sum(1 for item in deferred_entries if int(item.get("priority") or 0) == 1),
            "deferred_ttl_count": sum(1 for item in deferred_entries if int(item.get("priority") or 0) == 2),
            "periodic_selected_count": periodic_selected_count,
            "cursor_advanced_count": cursor_advanced_count,
            "actual_starved_candidate_count": self._starved_candidate_count(eligible_candidates, selected_entries, runtime_by_key, current),
            "oldest_unevaluated_age_sec": self._oldest_unevaluated_age_sec(eligible_candidates, runtime_by_key, current),
            "queue_depth_by_priority": queue_depth_by_priority,
            "starved_candidate_count": self._starved_candidate_count(eligible_candidates, selected_entries, runtime_by_key, current),
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

    def _states(self, trade_date: str, candidates: list[Any]) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
        loader = getattr(self.db, "list_setup_router_states", None)
        if not callable(loader):
            return {}
        candidate_ids = [self._candidate_instance_id(candidate) for candidate in candidates]
        rows = list(loader(trade_date=trade_date, candidate_instance_ids=candidate_ids, active_only=False, router_version=SETUP_ROUTER_VERSION) or [])
        result: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        for row in rows:
            payload = dict(row or {})
            candidate_instance_id = str(payload.get("candidate_instance_id") or "")
            theme_id = str(payload.get("theme_id") or payload.get("selected_theme_id") or "")
            setup_type = str(payload.get("setup_type") or "")
            existing = result.setdefault(candidate_instance_id, {}).setdefault(theme_id, {}).get(setup_type)
            if existing is None or int(payload.get("setup_generation") or 0) >= int(existing.get("setup_generation") or 0):
                result.setdefault(candidate_instance_id, {}).setdefault(theme_id, {})[setup_type] = payload
        return result

    def _candidate_runtime(self, trade_date: str, candidates: list[Any]) -> dict[str, dict[str, Any]]:
        loader = getattr(self.db, "list_setup_router_candidate_runtime", None)
        if not callable(loader):
            return {}
        candidate_ids = [self._candidate_instance_id(candidate) for candidate in candidates]
        rows = list(loader(trade_date=trade_date, candidate_instance_ids=candidate_ids, limit=max(1000, len(candidate_ids) + 10)) or [])
        return {str(row.get("candidate_instance_id") or ""): dict(row or {}) for row in rows}

    def _states_for_theme(self, states_by_theme: Mapping[str, Any], theme_id: str) -> dict[str, Any]:
        theme_id = str(theme_id or "")
        states = dict(states_by_theme or {})
        exact = states.get(theme_id)
        if isinstance(exact, Mapping):
            return {str(key): dict(value or {}) for key, value in dict(exact).items()}
        return {}

    def _leases(self, trade_date: str) -> dict[str, tuple[dict[str, Any], ...]]:
        loader = getattr(self.db, "list_theme_expansion_leases", None)
        if not callable(loader):
            return {}
        result: dict[str, list[dict[str, Any]]] = {}
        for lease in list(loader(trade_date=trade_date, active_only=False) or []):
            payload = dict(lease or {})
            result.setdefault(normalize_code(str(payload.get("code") or "")), []).append(payload)
        return {key: tuple(value) for key, value in result.items()}

    def _lease_selection(self, leases: Iterable[Mapping[str, Any]], context: Mapping[str, Any]) -> dict[str, Any]:
        theme = dict(context.get("theme") or {})
        theme_id = str(context.get("selected_theme_id") or theme.get("theme_id") or "")
        payloads = [dict(item or {}) for item in leases or []]
        selected: dict[str, Any] = {}
        for lease in payloads:
            if str(lease.get("theme_id") or "") == theme_id:
                selected = lease
                break
        data = dict(context.get("data") or {})
        required = bool(
            theme_id
            and (
                selected
                or payloads
                or context.get("selected_theme_lease_required")
                or context.get("theme_expansion_lease_required")
                or data.get("selected_theme_lease_required")
                or data.get("theme_expansion_lease_required")
            )
        )
        return {
            "lease": selected,
            "required": required,
            "other_theme_lease_count": sum(1 for item in payloads if str(item.get("theme_id") or "") != theme_id),
        }

    def _selected_theme_id(self, context: Mapping[str, Any]) -> str:
        theme = dict(context.get("theme") or {})
        return str(context.get("selected_theme_id") or theme.get("theme_id") or "")

    def _ttl_due_codes(self, state_by_candidate: Mapping[str, Any], now: datetime) -> set[str]:
        due: set[str] = set()
        now_text = now.isoformat()
        for states_by_theme in dict(state_by_candidate or {}).values():
            for setup_states in dict(states_by_theme or {}).values():
                for state in dict(setup_states or {}).values():
                    payload = dict(state or {})
                    lifecycle = str(payload.get("lifecycle_state") or "").upper()
                    expires_at = str(payload.get("expires_at") or "")
                    code = normalize_code(str(payload.get("code") or ""))
                    if code and lifecycle in {"FORMING", "MATCHED"} and expires_at and expires_at <= now_text:
                        due.add(code)
        return due

    def _theme_changed_codes(self, candidates: list[Any], contexts: Mapping[str, dict[str, Any]], runtime_by_key: Mapping[str, dict[str, Any]]) -> set[str]:
        changed: set[str] = set()
        for candidate in candidates:
            candidate_instance_id = self._candidate_instance_id(candidate)
            code = normalize_code(candidate.code)
            selected_theme_id = self._selected_theme_id(contexts.get(code) or dict(getattr(candidate, "metadata", {}) or {}).get("strategy_context_v3") or {})
            previous_theme_id = str(dict(runtime_by_key.get(candidate_instance_id) or {}).get("selected_theme_id") or "")
            if selected_theme_id and previous_theme_id and selected_theme_id != previous_theme_id:
                changed.add(code)
        return changed

    def _evaluation_queue(
        self,
        candidates: list[Any],
        *,
        incremental_codes: set[str],
        context_codes: set[str],
        ttl_codes: set[str],
        periodic_candidates: list[Any],
    ) -> list[dict[str, Any]]:
        periodic_ids = {self._candidate_instance_id(candidate) for candidate in periodic_candidates}
        queue: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            code = normalize_code(candidate.code)
            candidate_instance_id = self._candidate_instance_id(candidate)
            sources: list[str] = []
            priority = 99
            if code in incremental_codes:
                priority = min(priority, 0)
                sources.append("INCREMENTAL")
            if code in context_codes:
                priority = min(priority, 1)
                sources.append("CONTEXT")
            if code in ttl_codes:
                priority = min(priority, 2)
                sources.append("TTL")
            if candidate_instance_id in periodic_ids:
                priority = min(priority, 3)
                sources.append("PERIODIC")
            if priority <= 3:
                queue[candidate_instance_id] = {
                    "candidate": candidate,
                    "priority": priority,
                    "source": "+".join(sources),
                }
        return list(queue.values())

    def _select_evaluation_entries(
        self,
        queue: list[dict[str, Any]],
        runtime_by_key: Mapping[str, dict[str, Any]],
        now: datetime,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
        limit = max(1, int(self.config.max_candidates_per_cycle))
        selected: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        queue_depth = {str(priority): 0 for priority in range(4)}
        for priority in range(4):
            group = [entry for entry in queue if int(entry.get("priority") or 0) == priority]
            queue_depth[str(priority)] = len(group)
            group.sort(key=lambda entry: self._last_evaluated_sort_key(entry["candidate"], runtime_by_key, now))
            remaining = max(0, limit - len(selected))
            selected.extend(group[:remaining])
            for item in group[remaining:]:
                item["deferred_reason"] = f"P{priority}_CAPACITY_DEFERRED"
                deferred.append(item)
        return selected, deferred, queue_depth

    def _last_evaluated_sort_key(self, candidate: Any, runtime_by_key: Mapping[str, dict[str, Any]], now: datetime) -> tuple[float, str]:
        candidate_instance_id = self._candidate_instance_id(candidate)
        last = str(dict(runtime_by_key.get(candidate_instance_id) or {}).get("last_evaluated_at") or "")
        parsed = _parse_time(last)
        if parsed is None:
            return (0.0, candidate_instance_id)
        return (parsed.timestamp(), candidate_instance_id)

    def _runtime_update(
        self,
        candidate: Any,
        now: datetime,
        context: Mapping[str, Any],
        entry_decision: Mapping[str, Any],
        latest_completed_candle_at: str,
        *,
        source: str,
        incremental: bool = False,
        periodic: bool = False,
        ttl: bool = False,
        deferred: bool = False,
        skipped: str = "",
    ) -> dict[str, Any]:
        candidate_instance_id = self._candidate_instance_id(candidate)
        current = now.isoformat()
        return {
            "trade_date": now.date().isoformat(),
            "candidate_instance_id": candidate_instance_id,
            "code": normalize_code(candidate.code),
            "selected_theme_id": self._selected_theme_id(context),
            "last_evaluated_at": "" if deferred else current,
            "last_evaluation_source": source,
            "last_context_id": str(dict(context or {}).get("context_id") or ""),
            "last_entry_decision_at": str(dict(entry_decision or {}).get("calculated_at") or dict(entry_decision or {}).get("created_at") or ""),
            "last_completed_candle_at": latest_completed_candle_at,
            "evaluation_count": 0 if deferred else 1,
            "incremental_evaluation_count": int(bool(incremental and not deferred)),
            "periodic_evaluation_count": int(bool(periodic and not deferred)),
            "ttl_evaluation_count": int(bool(ttl and not deferred)),
            "capacity_deferred_count": int(bool(deferred)),
            "last_deferred_at": current if deferred else "",
            "last_skip_reason": skipped,
        }

    def _advance_periodic_cursor(self, eligible_count: int, evaluated_periodic_count: int) -> int:
        if eligible_count <= 0 or evaluated_periodic_count <= 0:
            return 0
        advance = min(int(evaluated_periodic_count), int(eligible_count))
        self.reconcile_cursor = (self.reconcile_cursor + advance) % int(eligible_count)
        return advance

    def _starvation_threshold_sec(self, eligible_count: int) -> int:
        if int(self.config.max_starvation_sec or 0) > 0:
            return int(self.config.max_starvation_sec)
        max_per_cycle = max(1, int(self.config.max_candidates_per_cycle))
        return max(120, int(self.config.periodic_reconcile_sec) * ceil(max(1, eligible_count) / max_per_cycle) * 2)

    def _oldest_unevaluated_age_sec(self, candidates: list[Any], runtime_by_key: Mapping[str, dict[str, Any]], now: datetime) -> int:
        ages: list[float] = []
        for candidate in candidates:
            last = str(dict(runtime_by_key.get(self._candidate_instance_id(candidate)) or {}).get("last_evaluated_at") or "")
            parsed = _parse_time(last)
            if parsed is None:
                continue
            ages.append(max(0.0, (now - parsed).total_seconds()))
        return int(max(ages, default=0.0))

    def _starved_candidate_count(
        self,
        candidates: list[Any],
        selected_entries: list[dict[str, Any]],
        runtime_by_key: Mapping[str, dict[str, Any]],
        now: datetime,
    ) -> int:
        selected_ids = {self._candidate_instance_id(entry["candidate"]) for entry in selected_entries}
        threshold = self._starvation_threshold_sec(len(candidates))
        count = 0
        for candidate in candidates:
            candidate_instance_id = self._candidate_instance_id(candidate)
            if candidate_instance_id in selected_ids:
                continue
            last = str(dict(runtime_by_key.get(candidate_instance_id) or {}).get("last_evaluated_at") or "")
            parsed = _parse_time(last)
            if parsed is not None and (now - parsed).total_seconds() > threshold:
                count += 1
        return count

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
        return ordered[:page_size]

    def _feature_signature(self, feature: Any, states: Mapping[str, Any]) -> tuple[Any, ...]:
        state_material = tuple(
            sorted(
                (
                    setup_type,
                    str(dict(state or {}).get("material_state_fingerprint") or dict(state or {}).get("feature_fingerprint") or ""),
                    str(dict(state or {}).get("lifecycle_state") or ""),
                    str(dict(state or {}).get("detector_phase") or ""),
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
            bool(feature.post_subscription_tick_verified),
            _price_bucket(feature.current_price),
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


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _price_bucket(value: Any) -> int:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    if number <= 0:
        return 0
    return int(round(number / 10.0))
