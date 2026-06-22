from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from time import perf_counter
from typing import Any, Mapping

from trading.strategy.candidate_state_contract import CandidateStateContractService
from trading.strategy.candidates import normalize_code
from trading.strategy.setup_features import SetupFeatureBuilder
from trading.strategy.setup_router_v3 import (
    SETUP_ROUTER_OUTPUT_MODE,
    SETUP_ROUTER_SCHEMA_VERSION,
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
    clock: Any = datetime.now

    def __post_init__(self) -> None:
        self.config = self.config or SetupRouterConfig.from_env()
        self.state_contract = self.state_contract or CandidateStateContractService(self.db, clock=self.clock)
        self.feature_builder = SetupFeatureBuilder(
            market_data=self.market_data,
            candle_builder=self.candle_builder,
            min_completed_1m_candles=self.config.min_completed_1m_candles,
            max_tick_age_sec=self.config.max_tick_age_sec,
        )
        self.router = SetupRouterV3(self.config)
        self.last_run_at: datetime | None = None
        self.last_result: list[dict[str, Any]] = []
        self.last_summary: dict[str, Any] = {
            "enabled": bool(self.config.enabled),
            "status": "IDLE" if self.config.enabled else "DISABLED",
            "schema_version": SETUP_ROUTER_SCHEMA_VERSION,
            "output_mode": SETUP_ROUTER_OUTPUT_MODE,
            "observe_only": True,
        }

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
        candidates = candidates[: max(1, int(self.config.max_candidates_per_cycle))]
        latest_context_by_code = self._latest_contexts(trade_date, candidates)
        latest_entry_by_key = self._latest_entries(trade_date)
        previous_by_key = self._previous_observations(trade_date)
        leases_by_code = self._leases(trade_date)
        observations: list[dict[str, Any]] = []
        skipped_reasons: Counter[str] = Counter()
        evaluated_count = 0
        for candidate in candidates:
            contract = self.state_contract.snapshot(candidate)
            if not contract.evaluation_eligible:
                skipped_reasons[contract.evaluation_eligibility] += 1
                continue
            code = normalize_code(candidate.code)
            metadata = dict(candidate.metadata or {})
            candidate_instance_id = str(metadata.get("candidate_instance_id") or f"{candidate.trade_date}:{code}:{candidate.id or 0}")
            previous = self._previous_for_candidate(previous_by_key, candidate_instance_id)
            feature = self.feature_builder.build(
                candidate,
                now=current,
                contract_snapshot=contract,
                strategy_context=latest_context_by_code.get(code) or metadata.get("strategy_context_v3") or {},
                entry_decision=latest_entry_by_key.get((candidate.id, code)) or latest_entry_by_key.get((None, code)) or {},
                previous_observation=previous,
                expansion_lease=leases_by_code.get(code) or {},
            )
            result = [item.to_dict() for item in self.router.classify(feature)]
            observations.extend(result)
            evaluated_count += 1
        saved_count = 0
        if self.config.save_history:
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
            "enabled": True,
            "status": "OK" if observations else "EMPTY",
            "schema_version": SETUP_ROUTER_SCHEMA_VERSION,
            "output_mode": SETUP_ROUTER_OUTPUT_MODE,
            "calculated_at": current.isoformat(),
            "trade_date": trade_date,
            "observe_only": True,
            "candidate_count": len(candidates),
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
            "safety": _safety_flags(),
            "ready_allowed": False,
            "candidate_promotion_allowed": False,
            "opportunity_rank_allowed": False,
            "order_intent_allowed": False,
            "live_order_allowed": False,
            "duration_ms": duration_ms,
        }
        run_saver = getattr(self.db, "save_setup_router_run", None)
        if callable(run_saver):
            run_saver(summary)
        self.last_run_at = current
        self.last_result = observations
        self.last_summary = summary
        return dict(summary)

    def _latest_contexts(self, trade_date: str, candidates: list[Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            code = normalize_code(getattr(candidate, "code", ""))
            context = dict(dict(getattr(candidate, "metadata", {}) or {}).get("strategy_context_v3") or {})
            if context:
                result[code] = context
                continue
            loader = getattr(self.db, "latest_strategy_context", None)
            if callable(loader):
                loaded = dict(loader(trade_date=trade_date, code=code) or {})
                if loaded:
                    result[code] = loaded
        return result

    def _latest_entries(self, trade_date: str) -> dict[tuple[int | None, str], dict[str, Any]]:
        loader = getattr(self.db, "latest_entry_decisions", None)
        if not callable(loader):
            return {}
        result: dict[tuple[int | None, str], dict[str, Any]] = {}
        for item in list(loader(trade_date=trade_date) or []):
            payload = dict(item or {})
            code = normalize_code(str(payload.get("code") or ""))
            candidate_id = payload.get("candidate_id")
            result[(candidate_id, code)] = payload
            result.setdefault((None, code), payload)
        return result

    def _previous_observations(self, trade_date: str) -> dict[tuple[str, str], dict[str, Any]]:
        loader = getattr(self.db, "list_setup_observations_latest", None)
        if not callable(loader):
            return {}
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for item in list(loader(trade_date=trade_date, limit=max(1000, self.config.max_candidates_per_cycle * 5)) or []):
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

    def _leases(self, trade_date: str) -> dict[str, dict[str, Any]]:
        loader = getattr(self.db, "list_theme_expansion_leases", None)
        if not callable(loader):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for lease in list(loader(trade_date=trade_date, active_only=False) or []):
            payload = dict(lease or {})
            result.setdefault(normalize_code(str(payload.get("code") or "")), payload)
        return result


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
            "context_status": item.get("context_status", ""),
            "router_status": item.get("router_status", ""),
            "entry_alignment_status": item.get("entry_alignment_status", ""),
            "setup_quality_score": item.get("setup_quality_score", 0.0),
            "current_price": item.get("current_price", 0.0),
            "price_structure": dict(item.get("price_structure") or {}),
            "reason_codes": list(item.get("reason_codes") or [])[:8],
            "updated_at": item.get("calculated_at", ""),
            "primary_setup": bool(item.get("primary_setup")),
            "observe_only": True,
        }
        for item in rows[:50]
    ]


def _disabled_summary(reason: str, now: datetime) -> dict[str, Any]:
    return {
        "enabled": False,
        "status": "DISABLED",
        "schema_version": SETUP_ROUTER_SCHEMA_VERSION,
        "output_mode": SETUP_ROUTER_OUTPUT_MODE,
        "observe_only": True,
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
    }
