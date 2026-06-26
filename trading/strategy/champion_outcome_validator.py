from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import random
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

from trading.strategy.costs import RoundTripCostConfig, cost_adjusted_signal_return_pct, cost_assumption_payload
from trading.strategy.candidates import normalize_code


CHAMPION_SIGNAL_EPISODE_SCHEMA_VERSION = "champion_signal_episode.v1"
CHAMPION_SIGNAL_ANCHOR_SCHEMA_VERSION = "champion_signal_anchor.v1"
CHAMPION_SIGNAL_OUTCOME_SCHEMA_VERSION = "champion_signal_outcome.v1"
CHAMPION_CONTEXT_REASON_OUTCOME_SCHEMA_VERSION = "champion_context_reason_outcome.v1"
CHAMPION_OPPORTUNITY_LOSS_LABEL_SCHEMA_VERSION = "champion_opportunity_loss_label.v1"
CHAMPION_OUTCOME_REPORT_SCHEMA_VERSION = "champion_outcome_report.v1"
CHAMPION_OUTCOME_RECOMMENDATION_SCHEMA_VERSION = "champion_outcome_recommendation.v1"

ANALYSIS_VERSION = "champion_outcome_validator.v1"
PRIMARY_SETUP = "LEADER_FIRST_PULLBACK"
PRIMARY_ANCHOR_TYPE = "CHAMPION_FIRST_VALID_OBSERVE"
GATE_ANCHOR_TYPE = "CHAMPION_FIRST_MATCHED"
DISCOVERY_ANCHOR_TYPE = "BENCHMARK_FIRST_SEEN"
REPORT_ROOT = Path(__file__).resolve().parents[2] / "reports" / "champion_outcomes"


@dataclass(frozen=True)
class ChampionOutcomeValidatorConfig:
    enabled: bool = True
    primary_setup: str = PRIMARY_SETUP
    primary_horizon_min: int = 15
    horizons_min: tuple[int, ...] = (5, 15, 25, 60)
    primary_target_pct: float = 1.5
    primary_stop_pct: float = 1.0
    update_interval_sec: int = 60
    report_interval_sec: int = 300
    min_early_count: int = 10
    min_review_count: int = 30
    min_review_days: int = 3
    min_decision_count: int = 50
    min_decision_days: int = 5
    bootstrap_repetitions: int = 2000
    bootstrap_seed: int = 4145
    commission_bp_per_side: float = 1.5
    sell_tax_bp: float = 15.0
    primary_entry_slippage_bp: float = 10.0
    primary_exit_slippage_bp: float = 10.0
    cost_sensitivity_bp: tuple[float, ...] = (0.0, 10.0, 20.0, 30.0)
    barrier_sensitivity: tuple[tuple[float, float], ...] = ((1.0, 0.8), (1.5, 1.0), (2.0, 1.2))
    anchor_pre_price_max_age_sec: int = 5
    anchor_delay_max_sec: int = 30
    auto_apply: bool = False
    auto_enable_dry_run: bool = False

    @classmethod
    def from_env(cls) -> "ChampionOutcomeValidatorConfig":
        return cls(
            enabled=_bool_env("TRADING_CHAMPION_OUTCOME_VALIDATOR_ENABLED", True),
            primary_setup=os.getenv("TRADING_CHAMPION_OUTCOME_PRIMARY_SETUP", PRIMARY_SETUP) or PRIMARY_SETUP,
            primary_horizon_min=max(1, _int_env("TRADING_CHAMPION_OUTCOME_PRIMARY_HORIZON_MIN", 15)),
            horizons_min=_int_tuple_env("TRADING_CHAMPION_OUTCOME_HORIZONS_MIN", (5, 15, 25, 60)),
            primary_target_pct=_float_env("TRADING_CHAMPION_OUTCOME_PRIMARY_TARGET_PCT", 1.5),
            primary_stop_pct=_float_env("TRADING_CHAMPION_OUTCOME_PRIMARY_STOP_PCT", 1.0),
            update_interval_sec=max(1, _int_env("TRADING_CHAMPION_OUTCOME_UPDATE_INTERVAL_SEC", 60)),
            report_interval_sec=max(1, _int_env("TRADING_CHAMPION_OUTCOME_REPORT_INTERVAL_SEC", 300)),
            min_early_count=max(1, _int_env("TRADING_CHAMPION_OUTCOME_MIN_EARLY_COUNT", 10)),
            min_review_count=max(1, _int_env("TRADING_CHAMPION_OUTCOME_MIN_REVIEW_COUNT", 30)),
            min_review_days=max(1, _int_env("TRADING_CHAMPION_OUTCOME_MIN_REVIEW_DAYS", 3)),
            min_decision_count=max(1, _int_env("TRADING_CHAMPION_OUTCOME_MIN_DECISION_COUNT", 50)),
            min_decision_days=max(1, _int_env("TRADING_CHAMPION_OUTCOME_MIN_DECISION_DAYS", 5)),
            bootstrap_repetitions=max(0, _int_env("TRADING_CHAMPION_OUTCOME_BOOTSTRAP_REPETITIONS", 2000)),
            bootstrap_seed=_int_env("TRADING_CHAMPION_OUTCOME_BOOTSTRAP_SEED", 4145),
            primary_entry_slippage_bp=_float_env("TRADING_CHAMPION_OUTCOME_PRIMARY_ENTRY_SLIPPAGE_BP", 10.0),
            primary_exit_slippage_bp=_float_env("TRADING_CHAMPION_OUTCOME_PRIMARY_EXIT_SLIPPAGE_BP", 10.0),
            auto_apply=_bool_env("TRADING_CHAMPION_OUTCOME_AUTO_APPLY", False),
            auto_enable_dry_run=_bool_env("TRADING_CHAMPION_OUTCOME_AUTO_ENABLE_DRY_RUN", False),
        )

    @property
    def primary_cost(self) -> RoundTripCostConfig:
        return RoundTripCostConfig(
            commission_bp_per_side=self.commission_bp_per_side,
            sell_tax_bp=self.sell_tax_bp,
            entry_slippage_bp=self.primary_entry_slippage_bp,
            exit_slippage_bp=self.primary_exit_slippage_bp,
        )


class ChampionOutcomeValidatorService:
    def __init__(
        self,
        db: Any,
        *,
        config: ChampionOutcomeValidatorConfig | None = None,
        clock: Any = datetime.now,
    ) -> None:
        self.db = db
        self.config = config or ChampionOutcomeValidatorConfig.from_env()
        self.clock = clock
        self.last_report: dict[str, Any] = _disabled_runtime_section(self.clock().replace(microsecond=0))
        self.last_full_report: dict[str, Any] = {}

    def runtime_section(
        self,
        *,
        trade_date: str,
        as_of: datetime | str | None = None,
        baseline: Mapping[str, Any] | None = None,
        runtime_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = _as_datetime(as_of) or self.clock().replace(microsecond=0)
        if not self.config.enabled:
            self.last_report = _disabled_runtime_section(current)
            return self.last_report
        try:
            report = self._latest_persisted_report(trade_date_from=trade_date, trade_date_to=trade_date)
            if not report:
                report = self.build_report(
                    trade_date_from=trade_date,
                    trade_date_to=trade_date,
                    report_state="LIVE_PREVIEW",
                    as_of=current,
                    baseline=baseline,
                    runtime_snapshot=runtime_snapshot,
                    persist=False,
                )
        except Exception as exc:
            section = _empty_runtime_section(current)
            section.update(
                {
                    "status": "ERROR",
                    "error": str(exc),
                    "warning_codes": ["CHAMPION_OUTCOME_FAILED"],
                    "baseline_id": (baseline or {}).get("baseline_id", ""),
                    "checked_at": current.isoformat(),
                }
            )
            self.last_report = section
            return section
        if not report:
            section = _empty_runtime_section(current)
            section.update({"baseline_id": (baseline or {}).get("baseline_id", ""), "checked_at": current.isoformat()})
            self.last_report = section
            return section
        section = _runtime_section(report)
        section["checked_at"] = current.isoformat()
        self.last_report = section
        return section

    def build_report(
        self,
        *,
        trade_date_from: str,
        trade_date_to: str | None = None,
        as_of: datetime | str | None = None,
        report_state: str = "LIVE_PREVIEW",
        baseline: Mapping[str, Any] | None = None,
        runtime_snapshot: Mapping[str, Any] | None = None,
        persist: bool = True,
        export: bool = False,
        strict_only: bool = False,
        rebuild_reason: str = "",
        source_cutoff_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        current = _as_datetime(as_of) or self.clock().replace(microsecond=0)
        cutoff = _as_datetime(source_cutoff_at) or current
        start_date = str(trade_date_from or current.date().isoformat())
        end_date = str(trade_date_to or start_date)
        state = "FINAL" if str(report_state or "").upper() == "FINAL" else "LIVE_PREVIEW"
        baseline_payload = _resolve_baseline(self.db, trade_date=end_date, runtime_snapshot=runtime_snapshot, baseline=baseline)
        analysis_hash = _fingerprint({"config": asdict(self.config), "analysis_version": ANALYSIS_VERSION})

        contract = self.audit_contracts()
        if not self.config.enabled:
            report = _disabled_report(start_date, end_date, current, baseline_payload, analysis_hash)
            self.last_full_report = dict(report)
            self.last_report = _runtime_section(report)
            return report
        if contract["status"] != "OK":
            report = _blocked_report(
                start_date,
                end_date,
                current,
                baseline_payload,
                analysis_hash,
                contract=contract,
                report_state=state,
                rebuild_reason=rebuild_reason,
            )
            self.last_full_report = dict(report)
            self.last_report = _runtime_section(report)
            return report

        days = _date_range(start_date, end_date)
        source = self._load_source(days, cutoff=cutoff)
        invariant_violations = _identity_invariant_violations(source["setup_observations"], source["setup_states"])
        signal_episodes = _build_signal_episodes(source, baseline_payload, self.config, invariant_violations)
        anchors = _build_signal_anchors(signal_episodes, source, self.config)
        outcomes = _build_signal_outcomes(
            signal_episodes,
            anchors,
            source,
            config=self.config,
            cutoff=cutoff,
            analysis_config_hash=analysis_hash,
        )
        context_reason_outcomes = _build_context_reason_outcomes(signal_episodes, outcomes, self.config)
        opportunity_loss = _build_opportunity_loss_labels(signal_episodes, outcomes, source, self.config)
        discovery_metrics = _discovery_metrics(source, self.config)
        funnel_metrics = _funnel_metrics(signal_episodes)
        matched_signal_metrics = _signal_metrics(outcomes, anchor_type=GATE_ANCHOR_TYPE, horizon_min=self.config.primary_horizon_min, strict_only=strict_only)
        valid_observe_metrics = _signal_metrics(outcomes, anchor_type=PRIMARY_ANCHOR_TYPE, horizon_min=self.config.primary_horizon_min, strict_only=strict_only)
        timing_metrics = _timing_metrics(signal_episodes, source)
        context_gate_metrics = _context_gate_metrics(signal_episodes, outcomes, self.config)
        concentration = _concentration_analysis(outcomes, signal_episodes, self.config)
        confidence = _confidence_intervals(outcomes, self.config)
        qualification_summary = _qualification_summary(source["qualifications"])
        warning_codes = _warning_codes(
            baseline_payload,
            qualification_summary,
            invariant_violations,
            valid_observe_metrics,
            concentration,
        )
        evidence_tier = _evidence_tier(valid_observe_metrics, self.config)
        recommendation = _recommendation(
            evidence_tier=evidence_tier,
            baseline=baseline_payload,
            qualification_summary=qualification_summary,
            discovery_metrics=discovery_metrics,
            valid_observe_metrics=valid_observe_metrics,
            matched_signal_metrics=matched_signal_metrics,
            context_gate_metrics=context_gate_metrics,
            concentration=concentration,
            warning_codes=warning_codes,
            invariant_violations=invariant_violations,
            config=self.config,
        )
        report = {
            "schema_version": CHAMPION_OUTCOME_REPORT_SCHEMA_VERSION,
            "report_id": _report_id("champion_outcome", start_date, end_date, state, current, baseline_payload),
            "report_state": state,
            "revision": 0,
            "trade_date_from": start_date,
            "trade_date_to": end_date,
            "source_cutoff_at": cutoff.isoformat(),
            "baseline_id": str(baseline_payload.get("baseline_id") or ""),
            "baseline_version": str(baseline_payload.get("baseline_version") or baseline_payload.get("version") or ""),
            "config_hash": str(baseline_payload.get("config_hash") or ""),
            "git_sha": str(baseline_payload.get("git_sha") or ""),
            "baseline_drift_status": str(baseline_payload.get("drift_status") or "UNKNOWN"),
            "config_snapshot_completeness": str(baseline_payload.get("config_snapshot_completeness") or "UNKNOWN"),
            "analysis_version": ANALYSIS_VERSION,
            "analysis_config_hash": analysis_hash,
            "analysis_only": True,
            "provisional": state != "FINAL",
            "decision_allowed": False,
            "auto_apply_allowed": False,
            "dry_run_auto_enable_allowed": False,
            "strategy_change_proposal_created": False,
            "evidence_tier": evidence_tier,
            "qualification_summary": qualification_summary,
            "source_coverage": _source_coverage(source, contract),
            "discovery_metrics": discovery_metrics,
            "funnel_metrics": funnel_metrics,
            "matched_signal_metrics": matched_signal_metrics,
            "valid_observe_metrics": valid_observe_metrics,
            "timing_metrics": timing_metrics,
            "context_gate_metrics": context_gate_metrics,
            "reason_outcomes": context_reason_outcomes,
            "opportunity_loss_taxonomy": _opportunity_loss_summary(opportunity_loss),
            "cost_sensitivity": _cost_sensitivity(outcomes, self.config),
            "barrier_sensitivity": _barrier_sensitivity(anchors, source, self.config, cutoff=cutoff),
            "session_breakdown": _breakdown(outcomes, signal_episodes, "session_phase", self.config),
            "market_side_breakdown": _breakdown(outcomes, signal_episodes, "market_side", self.config),
            "market_regime_breakdown": _breakdown(outcomes, signal_episodes, "market_action", self.config),
            "theme_state_breakdown": _breakdown(outcomes, signal_episodes, "theme_state", self.config),
            "stock_role_breakdown": _breakdown(outcomes, signal_episodes, "stock_role", self.config),
            "rank_bucket_breakdown": _rank_breakdown(outcomes, signal_episodes, source, self.config),
            "concentration_analysis": concentration,
            "confidence_intervals": confidence,
            "recommendation": recommendation,
            "warning_codes": warning_codes,
            "invariant_violations": invariant_violations,
            "behavioral_parity": _behavioral_parity_payload(),
            "external_load": _external_load_payload(),
            "order_safety": _order_safety_payload(),
            "build_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "generated_at": current.isoformat(),
            "finalized_at": current.isoformat() if state == "FINAL" else "",
            "rebuild_reason": rebuild_reason,
            "signals": signal_episodes,
            "anchors": anchors,
            "signal_outcomes": outcomes,
            "opportunity_loss_labels": opportunity_loss,
            "audit_matrix": _audit_matrix(),
        }
        if strict_only:
            report["signals"] = [item for item in signal_episodes if item.get("strict_sample_eligible")]
            report["signal_outcomes"] = [item for item in outcomes if item.get("strict_sample_eligible")]
        if persist:
            persisted_report = self._persist_all(report, signal_episodes, anchors, outcomes, context_reason_outcomes, opportunity_loss, recommendation)
            if persisted_report:
                report.update(persisted_report)
        if export:
            report["exported"] = export_champion_outcome_report(report)
        self.last_full_report = dict(report)
        self.last_report = _runtime_section(report)
        return report

    def audit_contracts(self) -> dict[str, Any]:
        required_tables = [
            "strategy_baseline_definitions",
            "strategy_baseline_sessions",
            "candidate_funnel_episode_latest",
            "candidate_funnel_reports",
            "setup_observations_latest_v2",
            "setup_router_state_v3",
            "setup_router_state_transitions_v3",
            "opportunity_benchmark_batches",
            "opportunity_benchmark_observations",
            "opportunity_benchmark_episodes",
            "opportunity_benchmark_candidate_links",
            "opportunity_benchmark_price_observations",
            "opportunity_benchmark_outcomes",
            "opportunity_benchmark_reports",
        ]
        required_methods = [
            "list_strategy_baseline_sessions",
            "list_candidate_funnel_episodes",
            "list_setup_observations_latest",
            "list_setup_router_states",
            "list_opportunity_benchmark_episodes",
            "list_opportunity_benchmark_candidate_links",
            "list_opportunity_benchmark_price_observations",
            "list_opportunity_benchmark_outcomes",
            "list_trading_day_qualification_reports",
        ]
        missing_tables = [table for table in required_tables if not _has_table(self.db, table)]
        missing_methods = [name for name in required_methods if not callable(getattr(self.db, name, None))]
        status = "OK" if not missing_tables and not missing_methods else "BLOCKED_BY_PR2"
        return {
            "status": status,
            "missing_tables": missing_tables,
            "missing_methods": missing_methods,
            "required_tables": required_tables,
            "required_methods": required_methods,
        }

    def _load_source(self, days: list[str], *, cutoff: datetime) -> dict[str, Any]:
        source: dict[str, Any] = {
            "benchmark_episodes": [],
            "benchmark_links": [],
            "benchmark_price_observations": [],
            "benchmark_outcomes": [],
            "candidate_episodes": [],
            "setup_observations": [],
            "setup_states": [],
            "qualifications": [],
        }
        for trade_date in days:
            source["benchmark_episodes"].extend(_list_call(self.db, "list_opportunity_benchmark_episodes", trade_date=trade_date, limit=100000))
            source["benchmark_links"].extend(_list_call(self.db, "list_opportunity_benchmark_candidate_links", trade_date=trade_date, limit=100000))
            source["benchmark_price_observations"].extend(_list_call(self.db, "list_opportunity_benchmark_price_observations", trade_date=trade_date, limit=100000))
            source["benchmark_outcomes"].extend(_list_call(self.db, "list_opportunity_benchmark_outcomes", trade_date=trade_date, limit=100000))
            source["candidate_episodes"].extend(_list_call(self.db, "list_candidate_funnel_episodes", trade_date=trade_date, limit=100000))
            source["setup_observations"].extend(
                _list_call(
                    self.db,
                    "list_setup_observations_latest",
                    trade_date=trade_date,
                    setup_type=self.config.primary_setup,
                    router_version="setup_router_v3.5.2",
                    limit=100000,
                )
            )
            source["setup_states"].extend(
                [
                    row
                    for row in _list_call(self.db, "list_setup_router_states", trade_date=trade_date, router_version="setup_router_v3.5.2", limit=100000)
                    if str(row.get("setup_type") or "") == self.config.primary_setup
                ]
            )
            source["qualifications"].extend(_list_call(self.db, "list_trading_day_qualification_reports", trade_date=trade_date, report_state="LIVE_PREVIEW", limit=5))
        for key in ("benchmark_price_observations", "benchmark_outcomes", "setup_observations", "setup_states"):
            source[key] = [row for row in source[key] if (_as_datetime(_row_time(row)) or datetime.min) <= cutoff]
        return source

    def _latest_persisted_report(self, *, trade_date_from: str, trade_date_to: str) -> dict[str, Any]:
        loader = getattr(self.db, "list_champion_outcome_reports", None)
        if not callable(loader):
            return {}
        rows = list(loader(trade_date_from=trade_date_from, trade_date_to=trade_date_to, report_state="LIVE_PREVIEW", limit=1) or [])
        return dict(rows[0]) if rows else {}

    def _persist_all(
        self,
        report: dict[str, Any],
        signal_episodes: list[dict[str, Any]],
        anchors: list[dict[str, Any]],
        outcomes: list[dict[str, Any]],
        context_reason_outcomes: list[dict[str, Any]],
        opportunity_loss: list[dict[str, Any]],
        recommendation: dict[str, Any],
    ) -> dict[str, Any]:
        calls = [
            ("save_champion_signal_episodes", signal_episodes),
            ("save_champion_signal_anchors", anchors),
            ("save_champion_signal_outcomes", outcomes),
            ("save_champion_context_reason_outcomes", context_reason_outcomes),
            ("save_champion_opportunity_loss_labels", opportunity_loss),
        ]
        for name, rows in calls:
            saver = getattr(self.db, name, None)
            if callable(saver):
                saver(rows if isinstance(rows, list) else dict(rows))
        persisted_report = report
        report_saver = getattr(self.db, "save_champion_outcome_report", None)
        if callable(report_saver):
            persisted_report = dict(report_saver(report) or report)
        rec = dict(recommendation or {})
        rec.update(
            {
                "report_id": persisted_report.get("report_id") or report.get("report_id", ""),
                "trade_date_from": persisted_report.get("trade_date_from") or report.get("trade_date_from", ""),
                "trade_date_to": persisted_report.get("trade_date_to") or report.get("trade_date_to", ""),
                "evidence_tier": persisted_report.get("evidence_tier") or report.get("evidence_tier", ""),
            }
        )
        rec_saver = getattr(self.db, "save_champion_outcome_recommendation", None)
        if callable(rec_saver):
            rec_saver(rec)
        return persisted_report


def export_champion_outcome_report(report: Mapping[str, Any], *, root: Path | None = None) -> dict[str, str]:
    period = _period_label(str(report.get("trade_date_from") or ""), str(report.get("trade_date_to") or ""))
    out = (root or REPORT_ROOT) / period
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary_json": out / "summary.json",
        "summary_md": out / "summary.md",
        "signals_csv": out / "signals.csv",
        "signal_outcomes_csv": out / "signal_outcomes.csv",
        "discovery_csv": out / "discovery.csv",
        "context_gates_csv": out / "context_gates.csv",
        "reason_outcomes_csv": out / "reason_outcomes.csv",
        "timing_csv": out / "timing.csv",
        "opportunity_loss_csv": out / "opportunity_loss.csv",
        "cost_sensitivity_csv": out / "cost_sensitivity.csv",
        "barrier_sensitivity_csv": out / "barrier_sensitivity.csv",
        "concentration_csv": out / "concentration.csv",
        "confidence_intervals_csv": out / "confidence_intervals.csv",
        "invariant_violations_csv": out / "invariant_violations.csv",
    }
    paths["summary_json"].write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    paths["summary_md"].write_text(_report_markdown(report), encoding="utf-8")
    _write_csv(paths["signals_csv"], list(report.get("signals") or []))
    _write_csv(paths["signal_outcomes_csv"], list(report.get("signal_outcomes") or []))
    _write_csv(paths["discovery_csv"], _flatten_metric_rows(report.get("discovery_metrics") or {}))
    _write_csv(paths["context_gates_csv"], _flatten_metric_rows(report.get("context_gate_metrics") or {}))
    _write_csv(paths["reason_outcomes_csv"], list(report.get("reason_outcomes") or []))
    _write_csv(paths["timing_csv"], _flatten_metric_rows(report.get("timing_metrics") or {}))
    _write_csv(paths["opportunity_loss_csv"], list(report.get("opportunity_loss_labels") or []))
    _write_csv(paths["cost_sensitivity_csv"], list(report.get("cost_sensitivity") or []))
    _write_csv(paths["barrier_sensitivity_csv"], list(report.get("barrier_sensitivity") or []))
    _write_csv(paths["concentration_csv"], _flatten_metric_rows(report.get("concentration_analysis") or {}))
    _write_csv(paths["confidence_intervals_csv"], _flatten_metric_rows(report.get("confidence_intervals") or {}))
    _write_csv(paths["invariant_violations_csv"], list(report.get("invariant_violations") or []))
    return {key: str(value) for key, value in paths.items()}


def _build_signal_episodes(source: Mapping[str, Any], baseline: Mapping[str, Any], config: ChampionOutcomeValidatorConfig, violations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates_by_instance = {str(row.get("candidate_instance_id") or ""): dict(row) for row in source.get("candidate_episodes") or []}
    links_by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for link in source.get("benchmark_links") or []:
        ci = str(link.get("candidate_instance_id") or "")
        if ci:
            links_by_candidate[ci].append(dict(link))
    benchmarks_by_id = {str(row.get("benchmark_episode_id") or ""): dict(row) for row in source.get("benchmark_episodes") or []}
    episodes: dict[str, dict[str, Any]] = {}
    for row in list(source.get("setup_states") or []) + list(source.get("setup_observations") or []):
        if str(row.get("setup_type") or "") != config.primary_setup:
            continue
        key = _signal_identity(row)
        if not key:
            continue
        candidate = candidates_by_instance.get(str(row.get("candidate_instance_id") or ""), {})
        link = _best_candidate_link(links_by_candidate.get(str(row.get("candidate_instance_id") or ""), []))
        benchmark = benchmarks_by_id.get(str(link.get("benchmark_episode_id") or ""), {}) if link else {}
        episode = episodes.setdefault(key, _empty_signal_episode(row, candidate, link, benchmark, baseline, config))
        _merge_signal_row(episode, row, candidate)
    for episode in episodes.values():
        _finalize_signal_episode(episode, config)
    return sorted(episodes.values(), key=lambda item: (str(item.get("trade_date") or ""), str(item.get("terminal_at") or ""), str(item.get("code") or "")))


def _empty_signal_episode(
    row: Mapping[str, Any],
    candidate: Mapping[str, Any],
    link: Mapping[str, Any],
    benchmark: Mapping[str, Any],
    baseline: Mapping[str, Any],
    config: ChampionOutcomeValidatorConfig,
) -> dict[str, Any]:
    setup_instance_id = str(row.get("setup_instance_id") or "")
    candidate_instance_id = str(row.get("candidate_instance_id") or "")
    trade_date = str(row.get("trade_date") or candidate.get("trade_date") or benchmark.get("trade_date") or "")
    identity_parts = [trade_date, setup_instance_id or candidate_instance_id, str(row.get("setup_type") or config.primary_setup), str(row.get("setup_generation") or 1)]
    link_status = _benchmark_link_status(link, benchmark)
    attribution = "HIGH" if setup_instance_id and str(candidate.get("attribution_confidence") or "") == "HIGH" else "LOW"
    if str(link.get("link_confidence") or "") == "LOW":
        attribution = "LOW"
    return {
        "schema_version": CHAMPION_SIGNAL_EPISODE_SCHEMA_VERSION,
        "champion_signal_episode_id": _stable_id("cse", *identity_parts),
        "trade_date": trade_date,
        "setup_instance_id": setup_instance_id,
        "setup_generation": _int(row.get("setup_generation"), 1),
        "candidate_instance_id": candidate_instance_id,
        "candidate_id": row.get("candidate_id") if row.get("candidate_id") is not None else candidate.get("candidate_id"),
        "candidate_generation_seq": _int(candidate.get("candidate_generation_seq"), _int(row.get("candidate_generation_seq"), 0)),
        "benchmark_episode_id": str(link.get("benchmark_episode_id") or ""),
        "code": normalize_code(row.get("code") or candidate.get("code") or benchmark.get("code") or ""),
        "name": str(row.get("name") or candidate.get("name") or benchmark.get("name") or ""),
        "theme_id": str(row.get("theme_id") or ""),
        "theme_name": str(row.get("theme_name") or ""),
        "theme_state": str(row.get("theme_state") or ""),
        "stock_role": str(row.get("stock_role") or ""),
        "market_side": str(row.get("market_side") or benchmark.get("market_side") or "UNKNOWN"),
        "market_action": str(row.get("market_action") or ""),
        "session_phase": str(row.get("session_phase") or benchmark.get("session_bucket") or ""),
        "baseline_id": str(baseline.get("baseline_id") or ""),
        "baseline_version": str(baseline.get("baseline_version") or baseline.get("version") or ""),
        "config_hash": str(baseline.get("config_hash") or ""),
        "git_sha": str(baseline.get("git_sha") or ""),
        "first_forming_at": "",
        "first_matched_at": "",
        "first_context_eligible_at": "",
        "first_valid_observe_at": "",
        "invalidated_at": "",
        "expired_at": "",
        "terminal_at": "",
        "final_shape_status": "",
        "final_context_status": "",
        "final_router_status": "",
        "first_match_context_status": "",
        "first_match_entry_alignment_status": "",
        "first_match_reason_codes": [],
        "first_match_setup_quality_score": None,
        "first_match_price_structure": {},
        "first_match_evidence": {},
        "first_forming_price": 0.0,
        "first_match_price": 0.0,
        "valid_observe_price": 0.0,
        "attribution_confidence": attribution,
        "benchmark_link_status": link_status,
        "qualification_status": str(benchmark.get("qualification_status") or "COLLECTING"),
        "strict_sample_eligible": bool(benchmark.get("strict_sample_eligible")) and link_status == "STRICT_LINKED" and attribution == "HIGH",
        "source_rows": [],
        "created_at": _now(),
        "updated_at": _now(),
    }


def _merge_signal_row(episode: dict[str, Any], row: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
    at = _row_time(row)
    lifecycle = str(row.get("lifecycle_state") or "").upper()
    shape = str(row.get("shape_status") or lifecycle or "").upper()
    context = str(row.get("context_status") or "").upper()
    router = str(row.get("router_status") or "").upper()
    if shape == "FORMING" or lifecycle == "FORMING":
        if _set_min_time(episode, "first_forming_at", _first_text(row.get("state_entered_at"), row.get("first_seen_at"), at)):
            episode["first_forming_price"] = _float(row.get("current_price"))
        elif _float(episode.get("first_forming_price")) <= 0:
            episode["first_forming_price"] = _float(row.get("current_price"))
    if shape == "MATCHED" or lifecycle == "MATCHED" or router == "VALID_OBSERVE":
        matched_at = _first_text(row.get("state_entered_at"), row.get("last_material_change_at"), at)
        if _set_min_time(episode, "first_matched_at", matched_at):
            episode["first_match_context_status"] = context
            episode["first_match_entry_alignment_status"] = str(row.get("entry_alignment_status") or "")
            episode["first_match_reason_codes"] = list(row.get("reason_codes") or [])
            episode["first_match_setup_quality_score"] = _optional_float(row.get("setup_quality_score"))
            episode["first_match_price_structure"] = dict(row.get("price_structure") or {})
            episode["first_match_evidence"] = dict(row.get("state_payload") or row.get("evidence") or {})
            episode["first_match_price"] = _float(row.get("current_price"))
    if context == "ELIGIBLE" and (shape == "MATCHED" or router == "VALID_OBSERVE"):
        _set_min_time(episode, "first_context_eligible_at", at)
    if router == "VALID_OBSERVE":
        if _set_min_time(episode, "first_valid_observe_at", at):
            episode["valid_observe_price"] = _float(row.get("current_price"))
    if lifecycle == "INVALIDATED" or shape == "INVALIDATED":
        _set_min_time(episode, "invalidated_at", _first_text(row.get("terminal_at"), at))
    if lifecycle == "EXPIRED" or shape == "EXPIRED":
        _set_min_time(episode, "expired_at", _first_text(row.get("expired_at"), row.get("terminal_at"), at))
    if at >= str(episode.get("_latest_at") or ""):
        episode["_latest_at"] = at
        episode["final_shape_status"] = shape or episode.get("final_shape_status", "")
        episode["final_context_status"] = context or episode.get("final_context_status", "")
        episode["final_router_status"] = router or episode.get("final_router_status", "")
        episode["theme_id"] = str(row.get("theme_id") or episode.get("theme_id") or "")
        episode["theme_name"] = str(row.get("theme_name") or episode.get("theme_name") or "")
        episode["theme_state"] = str(row.get("theme_state") or episode.get("theme_state") or "")
        episode["stock_role"] = str(row.get("stock_role") or episode.get("stock_role") or "")
        episode["market_side"] = str(row.get("market_side") or episode.get("market_side") or "UNKNOWN")
        episode["market_action"] = str(row.get("market_action") or episode.get("market_action") or "")
        episode["session_phase"] = str(row.get("session_phase") or episode.get("session_phase") or "")
    stage_first = dict(candidate.get("stage_first_reached_at") or {})
    _set_min_time(episode, "first_forming_at", stage_first.get("CHAMPION_FORMING"))
    _set_min_time(episode, "first_matched_at", stage_first.get("CHAMPION_MATCHED"))
    _set_min_time(episode, "first_context_eligible_at", stage_first.get("CHAMPION_CONTEXT_ELIGIBLE"))
    _set_min_time(episode, "first_valid_observe_at", stage_first.get("CHAMPION_VALID_OBSERVE"))
    episode.setdefault("source_rows", []).append(
        {
            "calculated_at": at,
            "shape_status": shape,
            "context_status": context,
            "router_status": router,
            "lifecycle_state": lifecycle,
            "fingerprint": row.get("fingerprint") or row.get("material_state_fingerprint") or "",
        }
    )


def _finalize_signal_episode(episode: dict[str, Any], config: ChampionOutcomeValidatorConfig) -> None:
    terminal = _first_text(episode.get("invalidated_at"), episode.get("expired_at"), episode.get("first_valid_observe_at"), episode.get("first_matched_at"), episode.get("first_forming_at"))
    episode["terminal_at"] = terminal
    episode.pop("_latest_at", None)
    episode["fingerprint"] = _fingerprint({key: episode.get(key) for key in sorted(episode) if key not in {"created_at", "updated_at", "fingerprint"}})


def _build_signal_anchors(signal_episodes: list[dict[str, Any]], source: Mapping[str, Any], config: ChampionOutcomeValidatorConfig) -> list[dict[str, Any]]:
    benchmarks = {str(row.get("benchmark_episode_id") or ""): dict(row) for row in source.get("benchmark_episodes") or []}
    candidates = {str(row.get("candidate_instance_id") or ""): dict(row) for row in source.get("candidate_episodes") or []}
    prices_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for price in source.get("benchmark_price_observations") or []:
        prices_by_episode[str(price.get("benchmark_episode_id") or "")].append(dict(price))
    anchors: list[dict[str, Any]] = []
    for signal in signal_episodes:
        benchmark = benchmarks.get(str(signal.get("benchmark_episode_id") or ""), {})
        candidate = candidates.get(str(signal.get("candidate_instance_id") or ""), {})
        price_rows = prices_by_episode.get(str(signal.get("benchmark_episode_id") or ""), [])
        anchor_specs = [
            (DISCOVERY_ANCHOR_TYPE, benchmark.get("anchor_at"), benchmark.get("anchor_price"), "BENCHMARK_ANCHOR", 0, benchmark.get("benchmark_episode_id"), benchmark.get("fingerprint")),
            ("CANDIDATE_FIRST_SEEN", candidate.get("first_seen_at"), None, "", 0, signal.get("candidate_instance_id"), candidate.get("fingerprint")),
            ("CHAMPION_FIRST_FORMING", signal.get("first_forming_at"), signal.get("first_forming_price"), "SETUP_OBSERVATION_CURRENT_PRICE", 0, signal.get("champion_signal_episode_id"), signal.get("fingerprint")),
            (GATE_ANCHOR_TYPE, signal.get("first_matched_at"), signal.get("first_match_price"), "SETUP_OBSERVATION_CURRENT_PRICE", 0, signal.get("champion_signal_episode_id"), signal.get("fingerprint")),
            ("CHAMPION_FIRST_CONTEXT_ELIGIBLE", signal.get("first_context_eligible_at"), None, "", 0, signal.get("champion_signal_episode_id"), signal.get("fingerprint")),
            (PRIMARY_ANCHOR_TYPE, signal.get("first_valid_observe_at"), signal.get("valid_observe_price"), "SETUP_OBSERVATION_CURRENT_PRICE", 0, signal.get("champion_signal_episode_id"), signal.get("fingerprint")),
        ]
        for anchor_type, anchor_at, price, source_name, delay, source_id, fingerprint in anchor_specs:
            anchor_at = str(anchor_at or "")
            if not anchor_at:
                continue
            anchor_price = _float(price)
            price_source = str(source_name or "")
            anchor_delay = int(delay or 0)
            quality = "HIGH"
            if anchor_price <= 0:
                anchor_price, price_source, anchor_delay, quality = _anchor_price_from_path(anchor_at, price_rows, config)
            if anchor_type == "CHAMPION_FIRST_FORMING" and anchor_price <= 0:
                continue
            if anchor_price <= 0:
                quality = "INSUFFICIENT"
                price_source = "MISSING"
            anchor = {
                "schema_version": CHAMPION_SIGNAL_ANCHOR_SCHEMA_VERSION,
                "anchor_id": _stable_id("csa", signal.get("champion_signal_episode_id"), anchor_type),
                "champion_signal_episode_id": signal.get("champion_signal_episode_id"),
                "trade_date": signal.get("trade_date"),
                "candidate_instance_id": signal.get("candidate_instance_id"),
                "setup_instance_id": signal.get("setup_instance_id"),
                "benchmark_episode_id": signal.get("benchmark_episode_id"),
                "anchor_type": anchor_type,
                "anchor_at": anchor_at,
                "anchor_price": anchor_price,
                "anchor_price_source": price_source,
                "anchor_delay_sec": anchor_delay,
                "source_observation_id": str(source_id or ""),
                "source_fingerprint": str(fingerprint or ""),
                "anchor_quality": quality,
                "created_at": _now(),
                "updated_at": _now(),
            }
            anchor["fingerprint"] = _stable_payload_fingerprint(anchor)
            anchors.append(anchor)
    return anchors


def _build_signal_outcomes(
    signal_episodes: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    source: Mapping[str, Any],
    *,
    config: ChampionOutcomeValidatorConfig,
    cutoff: datetime,
    analysis_config_hash: str,
) -> list[dict[str, Any]]:
    signals = {str(row.get("champion_signal_episode_id") or ""): dict(row) for row in signal_episodes}
    prices_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for price in source.get("benchmark_price_observations") or []:
        prices_by_episode[str(price.get("benchmark_episode_id") or "")].append(dict(price))
    outcomes: list[dict[str, Any]] = []
    for anchor in anchors:
        if str(anchor.get("anchor_type") or "") not in {GATE_ANCHOR_TYPE, PRIMARY_ANCHOR_TYPE, "CHAMPION_FIRST_CONTEXT_ELIGIBLE", "CHAMPION_FIRST_FORMING"}:
            continue
        signal = signals.get(str(anchor.get("champion_signal_episode_id") or ""), {})
        path = sorted(prices_by_episode.get(str(anchor.get("benchmark_episode_id") or ""), []), key=lambda item: str(item.get("observed_at") or ""))
        for horizon in config.horizons_min:
            outcome = _outcome_for_anchor(signal, anchor, path, horizon_min=int(horizon), config=config, cutoff=cutoff, analysis_config_hash=analysis_config_hash)
            outcomes.append(outcome)
    return outcomes


def _outcome_for_anchor(
    signal: Mapping[str, Any],
    anchor: Mapping[str, Any],
    path: list[dict[str, Any]],
    *,
    horizon_min: int,
    config: ChampionOutcomeValidatorConfig,
    cutoff: datetime,
    analysis_config_hash: str,
) -> dict[str, Any]:
    anchor_at = _as_datetime(anchor.get("anchor_at"))
    anchor_price = _float(anchor.get("anchor_price"))
    target_at = anchor_at + timedelta(minutes=int(horizon_min)) if anchor_at else None
    cutoff = cutoff.replace(microsecond=0)
    usable = []
    endpoint = None
    if anchor_at and anchor_price > 0 and target_at:
        for row in path:
            observed = _as_datetime(row.get("observed_at"))
            if observed is None or observed < anchor_at or observed > cutoff:
                continue
            if observed <= target_at:
                usable.append(row)
            if endpoint is None and observed >= target_at:
                endpoint = row
        if endpoint is not None and endpoint not in usable:
            usable.append(endpoint)
    label_status = "PENDING"
    label_quality = "INSUFFICIENT"
    raw_return = None
    mfe = None
    mae = None
    time_to_mfe = None
    time_to_mae = None
    horizon_observed_at = ""
    if not anchor_at or anchor_price <= 0:
        label_status = "INSUFFICIENT"
        label_quality = "INSUFFICIENT_ANCHOR"
    elif target_at and cutoff < target_at:
        label_status = "PENDING"
        label_quality = "INSUFFICIENT"
    elif endpoint is None:
        label_status = "INSUFFICIENT"
        label_quality = "INSUFFICIENT_PATH"
    else:
        label_status = "COMPLETE"
        label_quality = _path_quality(usable)
        horizon_observed_at = str(endpoint.get("observed_at") or "")
        end_price = _float(endpoint.get("price"))
        raw_return = _pct(end_price - anchor_price, anchor_price)
        high_row = max(usable, key=lambda item: _float(item.get("high") or item.get("price")), default={})
        low_row = min(usable, key=lambda item: _float(item.get("low") or item.get("price"), default=anchor_price), default={})
        mfe = _pct(_float(high_row.get("high") or high_row.get("price")) - anchor_price, anchor_price)
        mae = _pct(_float(low_row.get("low") or low_row.get("price"), default=anchor_price) - anchor_price, anchor_price)
        high_at = _as_datetime(high_row.get("observed_at"))
        low_at = _as_datetime(low_row.get("observed_at"))
        time_to_mfe = int((high_at - anchor_at).total_seconds()) if high_at else None
        time_to_mae = int((low_at - anchor_at).total_seconds()) if low_at else None
    barrier = _barrier_outcome(usable, anchor_at=anchor_at, anchor_price=anchor_price, target_pct=config.primary_target_pct, stop_pct=config.primary_stop_pct)
    cost_adjusted = cost_adjusted_signal_return_pct(raw_return, config.primary_cost)
    outcome = {
        "schema_version": CHAMPION_SIGNAL_OUTCOME_SCHEMA_VERSION,
        "outcome_id": _stable_id("cso", anchor.get("anchor_id"), horizon_min, "primary_10bp", ANALYSIS_VERSION),
        "champion_signal_episode_id": signal.get("champion_signal_episode_id"),
        "candidate_instance_id": signal.get("candidate_instance_id"),
        "setup_instance_id": signal.get("setup_instance_id"),
        "benchmark_episode_id": signal.get("benchmark_episode_id"),
        "trade_date": signal.get("trade_date"),
        "code": signal.get("code"),
        "theme_id": signal.get("theme_id"),
        "anchor_type": anchor.get("anchor_type"),
        "anchor_at": anchor.get("anchor_at"),
        "anchor_price": anchor_price,
        "anchor_price_source": anchor.get("anchor_price_source"),
        "horizon_min": int(horizon_min),
        "horizon_target_at": target_at.isoformat() if target_at else "",
        "horizon_observed_at": horizon_observed_at,
        "raw_return_pct": raw_return,
        "cost_adjusted_return_pct": cost_adjusted,
        "signal_proxy_return": raw_return,
        "cost_adjusted_signal_proxy": cost_adjusted,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "time_to_mfe_sec": time_to_mfe,
        "time_to_mae_sec": time_to_mae,
        "target_threshold_pct": config.primary_target_pct,
        "stop_threshold_pct": -abs(config.primary_stop_pct),
        "barrier_outcome": barrier["barrier_outcome"],
        "time_to_target_sec": barrier["time_to_target_sec"],
        "time_to_stop_sec": barrier["time_to_stop_sec"],
        "path_observation_count": len(usable),
        "label_status": label_status,
        "label_quality": label_quality,
        "qualification_status": signal.get("qualification_status") or "COLLECTING",
        "strict_sample_eligible": bool(signal.get("strict_sample_eligible")) and label_status == "COMPLETE" and label_quality not in {"INSUFFICIENT", "INSUFFICIENT_PATH", "INSUFFICIENT_ANCHOR"},
        "cost_scenario_id": "primary_10bp",
        "cost_assumptions": cost_assumption_payload(config.primary_cost, cost_scenario_id="primary_10bp"),
        "analysis_config_hash": analysis_config_hash,
        "calculated_at": _now(),
        "source_cutoff_at": cutoff.isoformat(),
        "revision": 0,
    }
    outcome["fingerprint"] = _stable_payload_fingerprint(outcome)
    return outcome


def _discovery_metrics(source: Mapping[str, Any], config: ChampionOutcomeValidatorConfig) -> dict[str, Any]:
    episodes = [dict(row) for row in source.get("benchmark_episodes") or []]
    links = [dict(row) for row in source.get("benchmark_links") or []]
    outcomes = [dict(row) for row in source.get("benchmark_outcomes") or []]
    primary_outcomes = [row for row in outcomes if _int(row.get("horizon_min")) == config.primary_horizon_min and str(row.get("label_status") or "") == "COMPLETE"]
    outcome_by_episode = {str(row.get("benchmark_episode_id") or ""): row for row in primary_outcomes}
    controlled = []
    for ep in episodes:
        episode_id = str(ep.get("benchmark_episode_id") or "")
        outcome = outcome_by_episode.get(episode_id, {})
        outcome_return = _optional_float(outcome.get("return_pct"))
        if (
            bool(ep.get("strict_sample_eligible"))
            and str(outcome.get("label_status") or "") == "COMPLETE"
            and outcome_return is not None
            and outcome_return >= config.primary_target_pct
        ):
            controlled.append(ep)
    primary_links = _primary_links_by_episode(links)
    captured_episode_ids = {eid for eid, link in primary_links.items() if str(link.get("candidate_instance_id") or "")}
    controlled_ids = {str(row.get("benchmark_episode_id") or "") for row in controlled}
    controlled_captured_5m = sum(1 for eid in controlled_ids if _within_window(primary_links.get(eid), {"PREEXISTING", "WITHIN_1M", "WITHIN_5M"}))
    controlled_captured_15m = sum(1 for eid in controlled_ids if _within_window(primary_links.get(eid), {"PREEXISTING", "WITHIN_1M", "WITHIN_5M", "WITHIN_15M"}))
    windows = Counter(str((link or {}).get("detection_window") or "NOT_CAPTURED") for link in primary_links.values())
    not_captured = len(episodes) - len(captured_episode_ids)
    return {
        "benchmark_episode_count": len(episodes),
        "strict_benchmark_episode_count": sum(1 for row in episodes if bool(row.get("strict_sample_eligible"))),
        "controlled_opportunity_count": len(controlled),
        "candidate_capture_count": len(captured_episode_ids),
        "candidate_not_captured_count": not_captured,
        "preexisting_count": windows.get("PREEXISTING", 0),
        "within_1m_count": windows.get("WITHIN_1M", 0),
        "within_5m_count": windows.get("WITHIN_5M", 0),
        "within_15m_count": windows.get("WITHIN_15M", 0),
        "after_15m_count": windows.get("AFTER_15M", 0),
        "not_captured_count": not_captured,
        "candidate_capture_rate_all": _ratio(len(captured_episode_ids), len(episodes)),
        "controlled_opportunity_recall_5m": _ratio(controlled_captured_5m, len(controlled)),
        "controlled_opportunity_recall_15m": _ratio(controlled_captured_15m, len(controlled)),
    }


def _funnel_metrics(signals: list[dict[str, Any]]) -> dict[str, Any]:
    captured = [row for row in signals if str(row.get("benchmark_link_status") or "") == "STRICT_LINKED"]
    forming = [row for row in signals if row.get("first_forming_at")]
    matched = [row for row in signals if row.get("first_matched_at")]
    eligible = [row for row in signals if row.get("first_context_eligible_at")]
    valid = [row for row in signals if row.get("first_valid_observe_at")]
    wait = [row for row in matched if str(row.get("first_match_context_status") or "").upper() == "WAIT"]
    blocked = [row for row in matched if str(row.get("first_match_context_status") or "").upper() == "BLOCKED"]
    data_wait = [row for row in signals if str(row.get("final_router_status") or "").upper() == "DATA_WAIT" or str(row.get("final_context_status") or "").upper() == "DATA_WAIT"]
    return {
        "candidate_captured_count": len(captured),
        "champion_forming_count": len(forming),
        "champion_matched_count": len(matched),
        "champion_context_eligible_count": len(eligible),
        "champion_valid_observe_count": len(valid),
        "champion_context_wait_count": len(wait),
        "champion_context_blocked_count": len(blocked),
        "champion_data_wait_count": len(data_wait),
        "champion_invalidated_count": sum(1 for row in signals if row.get("invalidated_at")),
        "champion_expired_count": sum(1 for row in signals if row.get("expired_at")),
        "candidate_to_forming": _ratio(len(forming), len(captured)),
        "forming_to_matched": _ratio(len(matched), len(forming)),
        "matched_to_context_eligible": _ratio(len(eligible), len(matched)),
        "context_eligible_to_valid_observe": _ratio(len(valid), len(eligible)),
        "matched_to_valid_observe": _ratio(len(valid), len(matched)),
    }


def _signal_metrics(outcomes: list[dict[str, Any]], *, anchor_type: str, horizon_min: int, strict_only: bool) -> dict[str, Any]:
    rows = [
        row
        for row in outcomes
        if str(row.get("anchor_type") or "") == anchor_type
        and _int(row.get("horizon_min")) == int(horizon_min)
        and str(row.get("label_status") or "") == "COMPLETE"
        and (not strict_only or bool(row.get("strict_sample_eligible")))
    ]
    raw = [_optional_float(row.get("raw_return_pct")) for row in rows]
    raw_values = [float(v) for v in raw if v is not None]
    net = [_optional_float(row.get("cost_adjusted_return_pct")) for row in rows]
    net_values = [float(v) for v in net if v is not None]
    mfe_values = [float(v) for v in (_optional_float(row.get("mfe_pct")) for row in rows) if v is not None]
    mae_values = [float(v) for v in (_optional_float(row.get("mae_pct")) for row in rows) if v is not None]
    barrier = Counter(str(row.get("barrier_outcome") or "INSUFFICIENT") for row in rows)
    valid_days = {str(row.get("trade_date") or "") for row in rows if row.get("trade_date")}
    codes = {str(row.get("code") or "") for row in rows if row.get("code")}
    themes = {str(row.get("theme_id") or "") for row in rows if row.get("theme_id")}
    positives = [v for v in net_values if v > 0]
    negatives = [v for v in net_values if v < 0]
    return {
        "anchor_type": anchor_type,
        "horizon_min": horizon_min,
        "strict_labeled_count": len(rows),
        "low_confidence_labeled_count": sum(1 for row in rows if not bool(row.get("strict_sample_eligible"))),
        "valid_trade_days": len(valid_days),
        "unique_codes": len(codes),
        "unique_themes": len(themes),
        "avg_raw_return": _avg(raw_values),
        "median_raw_return": _median(raw_values),
        "avg_cost_adjusted_return": _avg(net_values),
        "median_cost_adjusted_return": _median(net_values),
        "positive_return_rate": _ratio(sum(1 for v in net_values if v > 0), len(net_values)),
        "target_first_count": barrier.get("TARGET_FIRST", 0),
        "target_first_rate": _ratio(barrier.get("TARGET_FIRST", 0), len(rows)),
        "stop_first_count": barrier.get("STOP_FIRST", 0),
        "stop_first_rate": _ratio(barrier.get("STOP_FIRST", 0), len(rows)),
        "ambiguous_count": barrier.get("AMBIGUOUS_SAME_BAR", 0),
        "ambiguous_rate": _ratio(barrier.get("AMBIGUOUS_SAME_BAR", 0), len(rows)),
        "neither_count": barrier.get("NEITHER", 0),
        "neither_rate": _ratio(barrier.get("NEITHER", 0), len(rows)),
        "avg_mfe": _avg(mfe_values),
        "median_mfe": _median(mfe_values),
        "avg_mae": _avg(mae_values),
        "median_mae": _median(mae_values),
        "p10_return": _quantile(net_values, 0.10),
        "p90_return": _quantile(net_values, 0.90),
        "worst_return": min(net_values) if net_values else None,
        "best_return": max(net_values) if net_values else None,
        "profit_factor_proxy": (round(sum(positives) / abs(sum(negatives)), 6) if negatives else None),
        "average_time_to_target": _avg([_optional_float(row.get("time_to_target_sec")) for row in rows if row.get("time_to_target_sec") is not None]),
        "average_time_to_stop": _avg([_optional_float(row.get("time_to_stop_sec")) for row in rows if row.get("time_to_stop_sec") is not None]),
        "label_quality_distribution": _counter_rows(str(row.get("label_quality") or "UNKNOWN") for row in rows),
    }


def _timing_metrics(signals: list[dict[str, Any]], source: Mapping[str, Any]) -> dict[str, Any]:
    benchmarks = {str(row.get("benchmark_episode_id") or ""): dict(row) for row in source.get("benchmark_episodes") or []}
    candidates = {str(row.get("candidate_instance_id") or ""): dict(row) for row in source.get("candidate_episodes") or []}
    b_to_c: list[float] = []
    c_to_match: list[float] = []
    match_to_valid: list[float] = []
    forming_to_match: list[float] = []
    match_to_eligible: list[float] = []
    b_to_valid: list[float] = []
    remaining: list[float] = []
    for signal in signals:
        benchmark = benchmarks.get(str(signal.get("benchmark_episode_id") or ""), {})
        candidate = candidates.get(str(signal.get("candidate_instance_id") or ""), {})
        b = _as_datetime(benchmark.get("anchor_at"))
        c = _as_datetime(candidate.get("first_seen_at"))
        forming = _as_datetime(signal.get("first_forming_at"))
        match = _as_datetime(signal.get("first_matched_at"))
        eligible = _as_datetime(signal.get("first_context_eligible_at"))
        valid = _as_datetime(signal.get("first_valid_observe_at"))
        _append_delta(b_to_c, b, c)
        _append_delta(c_to_match, c, match)
        _append_delta(forming_to_match, forming, match)
        _append_delta(match_to_eligible, match, eligible)
        _append_delta(match_to_valid, match, valid)
        _append_delta(b_to_valid, b, valid)
        if b and valid:
            total = max((valid - b).total_seconds(), 1.0)
            remaining.append(max(0.0, 1.0 - min(total / 900.0, 1.0)))
    return {
        "benchmark_to_candidate_median_delay_sec": _median(b_to_c),
        "candidate_to_match_median_delay_sec": _median(c_to_match),
        "forming_to_match_median_delay_sec": _median(forming_to_match),
        "match_to_context_eligible_median_delay_sec": _median(match_to_eligible),
        "match_to_valid_median_delay_sec": _median(match_to_valid),
        "benchmark_to_valid_median_delay_sec": _median(b_to_valid),
        "median_remaining_opportunity_ratio": _median(remaining),
        "valid_observe_late_candidate_count": sum(1 for value in b_to_valid if value > 300),
    }


def _context_gate_metrics(signals: list[dict[str, Any]], outcomes: list[dict[str, Any]], config: ChampionOutcomeValidatorConfig) -> dict[str, Any]:
    primary = {
        str(row.get("champion_signal_episode_id") or ""): row
        for row in outcomes
        if str(row.get("anchor_type") or "") == GATE_ANCHOR_TYPE
        and _int(row.get("horizon_min")) == config.primary_horizon_min
        and str(row.get("label_status") or "") == "COMPLETE"
    }
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        status = str(signal.get("first_match_context_status") or signal.get("final_context_status") or "UNKNOWN").upper()
        outcome = primary.get(str(signal.get("champion_signal_episode_id") or ""))
        groups[status].append({"signal": signal, "outcome": outcome or {}})
    rows = {}
    for status, items in groups.items():
        labeled = [item for item in items if item.get("outcome")]
        target = sum(1 for item in labeled if item["outcome"].get("barrier_outcome") == "TARGET_FIRST")
        stop = sum(1 for item in labeled if item["outcome"].get("barrier_outcome") == "STOP_FIRST")
        rows[status.lower()] = {
            "signal_count": len(items),
            "strict_labeled_count": sum(1 for item in labeled if item["outcome"].get("strict_sample_eligible")),
            "target_first_count": target,
            "target_first_rate": _ratio(target, len(labeled)),
            "stop_first_count": stop,
            "stop_first_rate": _ratio(stop, len(labeled)),
            "avg_cost_adjusted_return": _avg([_optional_float(item["outcome"].get("cost_adjusted_return_pct")) for item in labeled]),
        }
    wait_block = [item for key in ("WAIT", "BLOCKED") for item in groups.get(key, []) if item.get("outcome")]
    false_blocks = sum(1 for item in wait_block if item["outcome"].get("barrier_outcome") == "TARGET_FIRST")
    good_blocks = sum(1 for item in wait_block if item["outcome"].get("barrier_outcome") == "STOP_FIRST")
    return {
        **rows,
        "context_false_block_candidate_count": false_blocks,
        "context_false_block_candidate_rate": _ratio(false_blocks, len(wait_block)),
        "good_context_block_candidate_count": good_blocks,
        "good_context_block_candidate_rate": _ratio(good_blocks, len(wait_block)),
    }


def _build_context_reason_outcomes(signals: list[dict[str, Any]], outcomes: list[dict[str, Any]], config: ChampionOutcomeValidatorConfig) -> list[dict[str, Any]]:
    primary = {
        str(row.get("champion_signal_episode_id") or ""): row
        for row in outcomes
        if str(row.get("anchor_type") or "") == GATE_ANCHOR_TYPE and _int(row.get("horizon_min")) == config.primary_horizon_min
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        for reason in list(signal.get("first_match_reason_codes") or ["NO_REASON"]):
            grouped[str(reason or "NO_REASON")].append(primary.get(str(signal.get("champion_signal_episode_id") or ""), {}))
    rows: list[dict[str, Any]] = []
    for reason, items in sorted(grouped.items()):
        labeled = [item for item in items if item]
        target = sum(1 for item in labeled if item.get("barrier_outcome") == "TARGET_FIRST")
        stop = sum(1 for item in labeled if item.get("barrier_outcome") == "STOP_FIRST")
        payload = {
            "schema_version": CHAMPION_CONTEXT_REASON_OUTCOME_SCHEMA_VERSION,
            "reason_outcome_id": _stable_id("ccro", reason, len(items), target, stop),
            "reason_code": reason,
            "signal_count": len(items),
            "labeled_count": len(labeled),
            "target_first_count": target,
            "stop_first_count": stop,
            "avg_cost_adjusted_return": _avg([_optional_float(item.get("cost_adjusted_return_pct")) for item in labeled]),
            "created_at": _now(),
            "updated_at": _now(),
        }
        payload["fingerprint"] = _stable_payload_fingerprint(payload)
        rows.append(payload)
    return rows


def _build_opportunity_loss_labels(signals: list[dict[str, Any]], outcomes: list[dict[str, Any]], source: Mapping[str, Any], config: ChampionOutcomeValidatorConfig) -> list[dict[str, Any]]:
    primary = {
        str(row.get("champion_signal_episode_id") or ""): row
        for row in outcomes
        if str(row.get("anchor_type") or "") == PRIMARY_ANCHOR_TYPE and _int(row.get("horizon_min")) == config.primary_horizon_min
    }
    labels: list[dict[str, Any]] = []
    for signal in signals:
        outcome = primary.get(str(signal.get("champion_signal_episode_id") or ""), {})
        label = "NO_STRICT_LABEL"
        if str(signal.get("benchmark_link_status") or "") == "CANDIDATE_NOT_IN_BENCHMARK":
            label = "DISCOVERY_NOT_BENCHMARKED"
        elif signal.get("first_valid_observe_at") and _delay_seconds(signal.get("first_matched_at"), signal.get("first_valid_observe_at")) > 300:
            label = "VALID_OBSERVE_LATE_CANDIDATE"
        elif str(signal.get("first_match_context_status") or "").upper() in {"WAIT", "BLOCKED"} and outcome.get("barrier_outcome") == "TARGET_FIRST":
            label = "CHAMPION_CONTEXT_FALSE_BLOCK_CANDIDATE"
        elif str(signal.get("first_match_context_status") or "").upper() in {"WAIT", "BLOCKED"} and outcome.get("barrier_outcome") == "STOP_FIRST":
            label = "GOOD_CONTEXT_BLOCK_CANDIDATE"
        elif outcome.get("barrier_outcome") == "TARGET_FIRST":
            label = "VALID_SIGNAL_CAPTURED_OPPORTUNITY"
        payload = {
            "schema_version": CHAMPION_OPPORTUNITY_LOSS_LABEL_SCHEMA_VERSION,
            "loss_label_id": _stable_id("coll", signal.get("champion_signal_episode_id"), label),
            "champion_signal_episode_id": signal.get("champion_signal_episode_id"),
            "trade_date": signal.get("trade_date"),
            "code": signal.get("code"),
            "opportunity_loss_label": label,
            "benchmark_link_status": signal.get("benchmark_link_status"),
            "barrier_outcome": outcome.get("barrier_outcome", ""),
            "created_at": _now(),
            "updated_at": _now(),
        }
        payload["fingerprint"] = _stable_payload_fingerprint(payload)
        labels.append(payload)
    return labels


def _recommendation(
    *,
    evidence_tier: str,
    baseline: Mapping[str, Any],
    qualification_summary: Mapping[str, Any],
    discovery_metrics: Mapping[str, Any],
    valid_observe_metrics: Mapping[str, Any],
    matched_signal_metrics: Mapping[str, Any],
    context_gate_metrics: Mapping[str, Any],
    concentration: Mapping[str, Any],
    warning_codes: list[str],
    invariant_violations: list[dict[str, Any]],
    config: ChampionOutcomeValidatorConfig,
) -> dict[str, Any]:
    primary = "KEEP_CHAMPION_OBSERVE"
    secondary: list[str] = []
    if warning_codes and any(code in warning_codes for code in ("BASELINE_DRIFT", "QUALIFICATION_INVALID", "INVARIANT_VIOLATION", "ANCHOR_LOOKAHEAD_SUSPECT")):
        primary = "DATA_QUALITY_BLOCKED"
    elif evidence_tier in {"EMPTY", "COLLECTING", "EARLY"}:
        primary = "CONTINUE_COLLECTING"
    else:
        avg_net = _optional_float(valid_observe_metrics.get("avg_cost_adjusted_return"))
        med_net = _optional_float(valid_observe_metrics.get("median_cost_adjusted_return"))
        target_rate = _rate_value(valid_observe_metrics.get("target_first_rate"))
        stop_rate = _rate_value(valid_observe_metrics.get("stop_first_rate"))
        if avg_net is not None and med_net is not None and avg_net <= 0 and med_net <= 0 and (target_rate is not None and target_rate < 0.40 or stop_rate is not None and stop_rate > 0.40):
            primary = "REVIEW_CHAMPION_RULES"
        elif (discovery_metrics.get("controlled_opportunity_count") or 0) >= 10 and (_rate_value(discovery_metrics.get("controlled_opportunity_recall_5m")) or 1.0) < 0.60:
            primary = "REVIEW_DISCOVERY"
        elif (_rate_value(context_gate_metrics.get("context_false_block_candidate_rate")) or 0.0) >= 0.30:
            primary = "REVIEW_CONTEXT_GATE"
        elif (valid_observe_metrics.get("strict_labeled_count") or 0) >= 10 and avg_net is not None and med_net is not None and avg_net > 0 and med_net >= 0 and (target_rate or 0) >= 0.50 and (stop_rate or 1) <= 0.35 and not concentration.get("severe_concentration_warning"):
            primary = "READY_FOR_DRY_RUN_REVIEW"
        elif (valid_observe_metrics.get("strict_labeled_count") or 0) >= 10:
            primary = "REVIEW_SIGNAL_TIMING"
    if (_rate_value(discovery_metrics.get("controlled_opportunity_recall_15m")) or 1.0) < 0.70:
        secondary.append("DISCOVERY_RECALL_WEAK")
    if concentration.get("warning_codes"):
        secondary.extend(list(concentration.get("warning_codes") or []))
    payload = {
        "schema_version": CHAMPION_OUTCOME_RECOMMENDATION_SCHEMA_VERSION,
        "recommendation_id": _stable_id("cor", evidence_tier, primary, baseline.get("config_hash"), _now()[:10]),
        "primary_recommendation": primary,
        "secondary_findings": _dedupe(secondary),
        "evidence_tier": evidence_tier,
        "auto_apply_allowed": False,
        "dry_run_auto_enable_allowed": False,
        "operator_review_only": True,
        "created_at": _now(),
        "updated_at": _now(),
    }
    payload["fingerprint"] = _stable_payload_fingerprint(payload)
    return payload


def _barrier_outcome(path: list[dict[str, Any]], *, anchor_at: datetime | None, anchor_price: float, target_pct: float, stop_pct: float) -> dict[str, Any]:
    if not anchor_at or anchor_price <= 0 or not path:
        return {"barrier_outcome": "INSUFFICIENT", "time_to_target_sec": None, "time_to_stop_sec": None}
    target_price = anchor_price * (1.0 + float(target_pct) / 100.0)
    stop_price = anchor_price * (1.0 - abs(float(stop_pct)) / 100.0)
    for row in path:
        observed = _as_datetime(row.get("observed_at"))
        high = _float(row.get("high") or row.get("price"))
        low = _float(row.get("low") or row.get("price"), default=anchor_price)
        target_hit = high >= target_price
        stop_hit = low <= stop_price
        if target_hit and stop_hit:
            return {
                "barrier_outcome": "AMBIGUOUS_SAME_BAR",
                "time_to_target_sec": int((observed - anchor_at).total_seconds()) if observed else None,
                "time_to_stop_sec": int((observed - anchor_at).total_seconds()) if observed else None,
            }
        if target_hit:
            return {
                "barrier_outcome": "TARGET_FIRST",
                "time_to_target_sec": int((observed - anchor_at).total_seconds()) if observed else None,
                "time_to_stop_sec": None,
            }
        if stop_hit:
            return {
                "barrier_outcome": "STOP_FIRST",
                "time_to_target_sec": None,
                "time_to_stop_sec": int((observed - anchor_at).total_seconds()) if observed else None,
            }
    return {"barrier_outcome": "NEITHER", "time_to_target_sec": None, "time_to_stop_sec": None}


def _contract_status_summary(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": contract.get("status", "UNKNOWN"),
        "missing_tables": list(contract.get("missing_tables") or []),
        "missing_methods": list(contract.get("missing_methods") or []),
    }


def _runtime_section(report: Mapping[str, Any]) -> dict[str, Any]:
    discovery = dict(report.get("discovery_metrics") or {})
    funnel = dict(report.get("funnel_metrics") or {})
    valid = dict(report.get("valid_observe_metrics") or {})
    context = dict(report.get("context_gate_metrics") or {})
    timing = dict(report.get("timing_metrics") or {})
    recommendation = dict(report.get("recommendation") or {})
    return {
        "enabled": True,
        "status": "OK",
        "report_id": report.get("report_id", ""),
        "report_state": report.get("report_state", "LIVE_PREVIEW"),
        "trade_date_from": report.get("trade_date_from", ""),
        "trade_date_to": report.get("trade_date_to", ""),
        "evidence_tier": report.get("evidence_tier", "EMPTY"),
        "valid_trade_days": valid.get("valid_trade_days", 0),
        "strict_labeled_signal_count": valid.get("strict_labeled_count", 0),
        "controlled_recall_5m": discovery.get("controlled_opportunity_recall_5m"),
        "champion_matched_count": funnel.get("champion_matched_count", 0),
        "champion_valid_observe_count": funnel.get("champion_valid_observe_count", 0),
        "primary_avg_cost_adjusted_return": valid.get("avg_cost_adjusted_return"),
        "primary_median_cost_adjusted_return": valid.get("median_cost_adjusted_return"),
        "target_first_rate": valid.get("target_first_rate"),
        "stop_first_rate": valid.get("stop_first_rate"),
        "context_false_block_candidate_rate": context.get("context_false_block_candidate_rate"),
        "benchmark_to_valid_median_delay_sec": timing.get("benchmark_to_valid_median_delay_sec"),
        "primary_recommendation": recommendation.get("primary_recommendation", "CONTINUE_COLLECTING"),
        "data_quality_status": "BLOCKED" if "DATA_QUALITY_BLOCKED" == recommendation.get("primary_recommendation") else "OK",
        "warning_codes": list(report.get("warning_codes") or [])[:5],
        "build_ms": float(report.get("build_ms") or 0.0),
        "generated_at": report.get("generated_at", ""),
    }


def _disabled_runtime_section(now: datetime) -> dict[str, Any]:
    return {"enabled": False, "status": "DISABLED", "evidence_tier": "EMPTY", "primary_recommendation": "DISABLED", "checked_at": now.isoformat()}


def _empty_runtime_section(now: datetime) -> dict[str, Any]:
    return {"enabled": True, "status": "EMPTY", "evidence_tier": "EMPTY", "primary_recommendation": "CONTINUE_COLLECTING", "checked_at": now.isoformat()}


def _disabled_report(start_date: str, end_date: str, current: datetime, baseline: Mapping[str, Any], analysis_hash: str) -> dict[str, Any]:
    return {
        "schema_version": CHAMPION_OUTCOME_REPORT_SCHEMA_VERSION,
        "report_id": _report_id("champion_outcome", start_date, end_date, "LIVE_PREVIEW", current, baseline),
        "report_state": "LIVE_PREVIEW",
        "trade_date_from": start_date,
        "trade_date_to": end_date,
        "analysis_config_hash": analysis_hash,
        "enabled": False,
        "status": "DISABLED",
        "evidence_tier": "EMPTY",
        "recommendation": {"primary_recommendation": "DISABLED", "auto_apply_allowed": False, "dry_run_auto_enable_allowed": False},
        "generated_at": current.isoformat(),
    }


def _blocked_report(
    start_date: str,
    end_date: str,
    current: datetime,
    baseline: Mapping[str, Any],
    analysis_hash: str,
    *,
    contract: Mapping[str, Any],
    report_state: str,
    rebuild_reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": CHAMPION_OUTCOME_REPORT_SCHEMA_VERSION,
        "report_id": _report_id("champion_outcome", start_date, end_date, report_state, current, baseline),
        "report_state": report_state,
        "trade_date_from": start_date,
        "trade_date_to": end_date,
        "analysis_config_hash": analysis_hash,
        "status": "BLOCKED_BY_PR2",
        "evidence_tier": "EMPTY",
        "contract_audit": _contract_status_summary(contract),
        "warning_codes": ["BLOCKED_BY_PR2"],
        "recommendation": {"primary_recommendation": "DATA_QUALITY_BLOCKED", "auto_apply_allowed": False, "dry_run_auto_enable_allowed": False},
        "analysis_only": True,
        "generated_at": current.isoformat(),
        "rebuild_reason": rebuild_reason,
    }


def _audit_matrix() -> list[dict[str, str]]:
    return [
        {"analysis_item": "Benchmark anchor", "canonical_source": "opportunity_benchmark_episodes", "identity": "benchmark_episode_id", "timestamp": "anchor_at", "quality_field": "episode_quality", "fallback": "none"},
        {"analysis_item": "Candidate first seen", "canonical_source": "candidate_funnel_episode_latest", "identity": "candidate_instance_id", "timestamp": "first_seen_at", "quality_field": "attribution_confidence", "fallback": "none"},
        {"analysis_item": "Champion first forming", "canonical_source": "candidate_funnel_episode_latest + setup_router_state_v3", "identity": "setup_instance_id", "timestamp": "CHAMPION_FORMING/state_entered_at", "quality_field": "attribution_confidence", "fallback": "candidate_instance_id+setup_type+generation"},
        {"analysis_item": "Champion first matched", "canonical_source": "setup_router_state_v3/setup_observations_latest_v2", "identity": "setup_instance_id", "timestamp": "state_entered_at/calculated_at", "quality_field": "shape_status", "fallback": "candidate_instance_id+setup_type+generation"},
        {"analysis_item": "Context eligible", "canonical_source": "setup_observations_latest_v2", "identity": "setup_instance_id", "timestamp": "calculated_at", "quality_field": "context_status", "fallback": "none"},
        {"analysis_item": "Valid observe", "canonical_source": "setup_observations_latest_v2", "identity": "setup_instance_id", "timestamp": "calculated_at", "quality_field": "router_status", "fallback": "none"},
        {"analysis_item": "Signal price", "canonical_source": "setup_observations_latest_v2", "identity": "setup_instance_id", "timestamp": "calculated_at", "quality_field": "anchor_quality", "fallback": "PR-2 price observation within tolerance"},
        {"analysis_item": "Future price path", "canonical_source": "opportunity_benchmark_price_observations", "identity": "benchmark_episode_id", "timestamp": "observed_at", "quality_field": "source_quality", "fallback": "none"},
        {"analysis_item": "Context reason", "canonical_source": "setup_observations_latest_v2", "identity": "setup_instance_id", "timestamp": "calculated_at", "quality_field": "reason_codes", "fallback": "none"},
        {"analysis_item": "Qualification", "canonical_source": "trading_day_qualification_reports", "identity": "trade_date", "timestamp": "source_cutoff_at", "quality_field": "qualification_status", "fallback": "COLLECTING"},
    ]


# Utility helpers


def _source_coverage(source: Mapping[str, Any], contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "contract_status": contract.get("status", "UNKNOWN"),
        "benchmark_episode_count": len(source.get("benchmark_episodes") or []),
        "candidate_episode_count": len(source.get("candidate_episodes") or []),
        "setup_observation_count": len(source.get("setup_observations") or []),
        "setup_state_count": len(source.get("setup_states") or []),
        "price_observation_count": len(source.get("benchmark_price_observations") or []),
        "benchmark_outcome_count": len(source.get("benchmark_outcomes") or []),
    }


def _identity_invariant_violations(setup_observations: Iterable[Mapping[str, Any]], setup_states: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_setup: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in list(setup_observations or []) + list(setup_states or []):
        setup_id = str(row.get("setup_instance_id") or "")
        if setup_id:
            by_setup[setup_id].add((str(row.get("code") or ""), str(row.get("candidate_instance_id") or "")))
    violations = []
    for setup_id, identities in sorted(by_setup.items()):
        if len(identities) > 1:
            violations.append({"severity": "CRITICAL", "type": "SETUP_INSTANCE_IDENTITY_CONFLICT", "setup_instance_id": setup_id, "identity_count": len(identities)})
    return violations


def _signal_identity(row: Mapping[str, Any]) -> str:
    setup_instance_id = str(row.get("setup_instance_id") or "")
    trade_date = str(row.get("trade_date") or "")
    if setup_instance_id:
        return f"setup:{trade_date}:{setup_instance_id}"
    ci = str(row.get("candidate_instance_id") or "")
    setup_type = str(row.get("setup_type") or "")
    generation = _int(row.get("setup_generation"), 1)
    if trade_date and ci and setup_type:
        return f"fallback:{trade_date}:{ci}:{setup_type}:{generation}"
    return ""


def _benchmark_link_status(link: Mapping[str, Any], benchmark: Mapping[str, Any]) -> str:
    if not link:
        return "CANDIDATE_NOT_IN_BENCHMARK"
    if not benchmark:
        return "BENCHMARK_LINK_MISSING"
    if str(link.get("link_confidence") or "") == "HIGH" and str(link.get("candidate_instance_id") or ""):
        return "STRICT_LINKED"
    if str(link.get("candidate_instance_id") or ""):
        return "LOW_CONFIDENCE_LINKED"
    return "BENCHMARK_LINK_MISSING"


def _best_candidate_link(links: list[dict[str, Any]]) -> dict[str, Any]:
    captured = [row for row in links if str(row.get("candidate_instance_id") or "")]
    if captured:
        return sorted(captured, key=lambda row: (str(row.get("link_confidence") or "") != "HIGH", abs(_int(row.get("detection_delay_sec"), 999999))))[0]
    return dict(links[0]) if links else {}


def _primary_links_by_episode(links: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for link in links:
        grouped[str(link.get("benchmark_episode_id") or "")].append(dict(link))
    return {episode_id: _best_candidate_link(rows) for episode_id, rows in grouped.items()}


def _within_window(link: Mapping[str, Any] | None, windows: set[str]) -> bool:
    if not link:
        return False
    return str(link.get("detection_window") or "") in windows


def _anchor_price_from_path(anchor_at: str, rows: list[dict[str, Any]], config: ChampionOutcomeValidatorConfig) -> tuple[float, str, int, str]:
    anchor_dt = _as_datetime(anchor_at)
    if anchor_dt is None:
        return 0.0, "MISSING", 0, "INSUFFICIENT"
    candidates: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        observed = _as_datetime(row.get("observed_at"))
        if observed is None:
            continue
        delta = int((observed - anchor_dt).total_seconds())
        if delta <= 0 and abs(delta) <= config.anchor_pre_price_max_age_sec:
            candidates.append((abs(delta), row))
        elif 0 < delta <= config.anchor_delay_max_sec:
            candidates.append((delta + 100000, row))
    if not candidates:
        return 0.0, "MISSING", 0, "INSUFFICIENT"
    _, row = sorted(candidates, key=lambda item: item[0])[0]
    observed = _as_datetime(row.get("observed_at")) or anchor_dt
    delay = int((observed - anchor_dt).total_seconds())
    source = "PR2_PRICE_OBSERVATION_FRESH" if delay <= 0 else "DELAYED_PRICE"
    quality = "HIGH" if delay <= 0 else "DELAYED"
    return _float(row.get("price")), source, delay, quality


def _path_quality(rows: list[dict[str, Any]]) -> str:
    qualities = {str(row.get("source_quality") or "") for row in rows}
    if not rows:
        return "INSUFFICIENT"
    if any("REALTIME" in q or "CANDLE" in q or "HIGH" in q for q in qualities):
        return "HIGH"
    if any("SAMPLED" in q or "TR" in q for q in qualities):
        return "MIXED_HIGH_QUALITY"
    return "MEDIUM"


def _qualification_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_status = Counter(str(row.get("qualification_status") or "COLLECTING") for row in rows)
    valid = sum(1 for row in rows if str(row.get("qualification_status") or "") == "VALID" and bool(row.get("strict_sample_eligible")))
    return {
        "valid_trade_days": valid,
        "status_distribution": _counter_to_rows(by_status),
        "has_invalid": any(str(row.get("qualification_status") or "") == "INVALID" for row in rows),
    }


def _warning_codes(baseline: Mapping[str, Any], qualification: Mapping[str, Any], violations: list[dict[str, Any]], metrics: Mapping[str, Any], concentration: Mapping[str, Any]) -> list[str]:
    codes: list[str] = []
    if str(baseline.get("drift_status") or "") == "DRIFT_DETECTED":
        codes.append("BASELINE_DRIFT")
    if str(baseline.get("config_snapshot_completeness") or "") == "PARTIAL":
        codes.append("BASELINE_PARTIAL")
    if qualification.get("has_invalid") and not qualification.get("valid_trade_days"):
        codes.append("QUALIFICATION_INVALID")
    if violations:
        codes.append("INVARIANT_VIOLATION")
    if int(metrics.get("strict_labeled_count") or 0) == 0:
        codes.append("STRICT_LABEL_INSUFFICIENT")
    codes.extend(list(concentration.get("warning_codes") or []))
    return _dedupe(codes)


def _evidence_tier(valid_metrics: Mapping[str, Any], config: ChampionOutcomeValidatorConfig) -> str:
    count = int(valid_metrics.get("strict_labeled_count") or 0)
    days = int(valid_metrics.get("valid_trade_days") or 0)
    if count <= 0:
        return "EMPTY"
    if count < config.min_early_count:
        return "COLLECTING"
    if count < config.min_review_count or days < config.min_review_days:
        return "EARLY"
    if count < config.min_decision_count or days < config.min_decision_days:
        return "REVIEW_READY"
    return "DECISION_SUPPORT"


def _concentration_analysis(outcomes: list[dict[str, Any]], signals: list[dict[str, Any]], config: ChampionOutcomeValidatorConfig) -> dict[str, Any]:
    primary = [row for row in outcomes if str(row.get("anchor_type") or "") == PRIMARY_ANCHOR_TYPE and _int(row.get("horizon_min")) == config.primary_horizon_min and str(row.get("label_status") or "") == "COMPLETE"]
    total = len(primary)
    if total <= 0:
        return {"sample_count": 0, "warning_codes": []}
    by_day = Counter(str(row.get("trade_date") or "") for row in primary)
    by_code = Counter(str(row.get("code") or "") for row in primary)
    signal_by_id = {str(row.get("champion_signal_episode_id") or ""): row for row in signals}
    by_theme = Counter(str(signal_by_id.get(str(row.get("champion_signal_episode_id") or ""), {}).get("theme_id") or "") for row in primary)
    top_day = max(by_day.values()) / total if by_day else 0.0
    top_code = max(by_code.values()) / total if by_code else 0.0
    top_theme = max(by_theme.values()) / total if by_theme else 0.0
    warnings = []
    if top_day >= 0.5:
        warnings.append("CONCENTRATION_WARNING_TOP_DAY")
    if top_code >= 0.5:
        warnings.append("CONCENTRATION_WARNING_TOP_CODE")
    if top_theme >= 0.5:
        warnings.append("CONCENTRATION_WARNING_TOP_THEME")
    return {
        "sample_count": total,
        "top_day_contribution": round(top_day, 6),
        "top_code_contribution": round(top_code, 6),
        "top_theme_contribution": round(top_theme, 6),
        "severe_concentration_warning": bool(warnings),
        "warning_codes": warnings,
        "trade_day_sample_counts": _counter_to_rows(by_day),
        "code_sample_counts": _counter_to_rows(by_code),
        "theme_sample_counts": _counter_to_rows(by_theme),
    }


def _confidence_intervals(outcomes: list[dict[str, Any]], config: ChampionOutcomeValidatorConfig) -> dict[str, Any]:
    rows = [row for row in outcomes if str(row.get("anchor_type") or "") == PRIMARY_ANCHOR_TYPE and _int(row.get("horizon_min")) == config.primary_horizon_min and str(row.get("label_status") or "") == "COMPLETE"]
    n = len(rows)
    target = sum(1 for row in rows if row.get("barrier_outcome") == "TARGET_FIRST")
    returns = [float(row.get("cost_adjusted_return_pct") or 0.0) for row in rows if row.get("cost_adjusted_return_pct") is not None]
    return {
        "target_first_wilson_95": _wilson_interval(target, n),
        "cost_adjusted_return_bootstrap_95": _bootstrap_mean_ci(returns, config.bootstrap_repetitions, config.bootstrap_seed) if len(returns) >= config.min_early_count else None,
    }


def _cost_sensitivity(outcomes: list[dict[str, Any]], config: ChampionOutcomeValidatorConfig) -> list[dict[str, Any]]:
    primary = [row for row in outcomes if str(row.get("anchor_type") or "") == PRIMARY_ANCHOR_TYPE and _int(row.get("horizon_min")) == config.primary_horizon_min and str(row.get("label_status") or "") == "COMPLETE"]
    rows = []
    for slip in config.cost_sensitivity_bp:
        cost = RoundTripCostConfig(config.commission_bp_per_side, config.sell_tax_bp, slip, slip)
        values = [cost_adjusted_signal_return_pct(_optional_float(row.get("raw_return_pct")), cost) for row in primary]
        clean = [float(value) for value in values if value is not None]
        rows.append({"slippage_bp": float(slip), "sample_count": len(clean), "avg_cost_adjusted_return": _avg(clean), "median_cost_adjusted_return": _median(clean)})
    return rows


def _barrier_sensitivity(anchors: list[dict[str, Any]], source: Mapping[str, Any], config: ChampionOutcomeValidatorConfig, *, cutoff: datetime) -> list[dict[str, Any]]:
    prices_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for price in source.get("benchmark_price_observations") or []:
        prices_by_episode[str(price.get("benchmark_episode_id") or "")].append(dict(price))
    rows = []
    for target, stop in config.barrier_sensitivity:
        counts = Counter()
        sample = 0
        for anchor in anchors:
            if str(anchor.get("anchor_type") or "") != PRIMARY_ANCHOR_TYPE:
                continue
            anchor_at = _as_datetime(anchor.get("anchor_at"))
            if not anchor_at:
                continue
            target_at = anchor_at + timedelta(minutes=config.primary_horizon_min)
            path = [
                row
                for row in prices_by_episode.get(str(anchor.get("benchmark_episode_id") or ""), [])
                if anchor_at <= (_as_datetime(row.get("observed_at")) or datetime.min) <= min(target_at, cutoff)
            ]
            result = _barrier_outcome(path, anchor_at=anchor_at, anchor_price=_float(anchor.get("anchor_price")), target_pct=target, stop_pct=stop)
            counts[result["barrier_outcome"]] += 1
            sample += 1
        rows.append({"target_pct": target, "stop_pct": -abs(stop), "sample_count": sample, **dict(counts)})
    return rows


def _breakdown(outcomes: list[dict[str, Any]], signals: list[dict[str, Any]], field: str, config: ChampionOutcomeValidatorConfig) -> list[dict[str, Any]]:
    signal_by_id = {str(row.get("champion_signal_episode_id") or ""): row for row in signals}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for outcome in outcomes:
        if str(outcome.get("anchor_type") or "") != PRIMARY_ANCHOR_TYPE or _int(outcome.get("horizon_min")) != config.primary_horizon_min:
            continue
        signal = signal_by_id.get(str(outcome.get("champion_signal_episode_id") or ""), {})
        grouped[str(signal.get(field) or "UNKNOWN")].append(outcome)
    return [_breakdown_row(bucket, rows) for bucket, rows in sorted(grouped.items())]


def _rank_breakdown(outcomes: list[dict[str, Any]], signals: list[dict[str, Any]], source: Mapping[str, Any], config: ChampionOutcomeValidatorConfig) -> list[dict[str, Any]]:
    benchmark_rank = {str(row.get("benchmark_episode_id") or ""): _rank_bucket(_int(row.get("best_rank"))) for row in source.get("benchmark_episodes") or []}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for outcome in outcomes:
        if str(outcome.get("anchor_type") or "") != PRIMARY_ANCHOR_TYPE or _int(outcome.get("horizon_min")) != config.primary_horizon_min:
            continue
        grouped[benchmark_rank.get(str(outcome.get("benchmark_episode_id") or ""), "UNKNOWN")].append(outcome)
    return [_breakdown_row(bucket, rows) for bucket, rows in sorted(grouped.items())]


def _breakdown_row(bucket: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    clean = [float(row.get("cost_adjusted_return_pct") or 0.0) for row in rows if row.get("cost_adjusted_return_pct") is not None]
    target = sum(1 for row in rows if row.get("barrier_outcome") == "TARGET_FIRST")
    return {
        "bucket": bucket,
        "sample_count": len(rows),
        "descriptive_only": len(rows) < 10,
        "avg_cost_adjusted_return": _avg(clean),
        "target_first_rate": _ratio(target, len(rows)),
    }


def _opportunity_loss_summary(labels: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(row.get("opportunity_loss_label") or "UNKNOWN") for row in labels)
    return {
        "labels": _counter_to_rows(counts),
        "discovery_loss_candidate_count": counts.get("DISCOVERY_NOT_BENCHMARKED", 0),
        "context_false_block_candidate_count": counts.get("CHAMPION_CONTEXT_FALSE_BLOCK_CANDIDATE", 0),
        "good_context_block_candidate_count": counts.get("GOOD_CONTEXT_BLOCK_CANDIDATE", 0),
        "valid_observe_late_candidate_count": counts.get("VALID_OBSERVE_LATE_CANDIDATE", 0),
    }


def _behavioral_parity_payload() -> dict[str, int]:
    return {
        "behavioral_diff_count": 0,
        "candidate_fsm_diff_count": 0,
        "market_regime_diff_count": 0,
        "theme_core_v3_diff_count": 0,
        "strategy_context_diff_count": 0,
        "entry_decision_diff_count": 0,
        "setup_router_diff_count": 0,
        "candidate_funnel_diff_count": 0,
        "opportunity_benchmark_diff_count": 0,
        "order_manager_diff_count": 0,
    }


def _external_load_payload() -> dict[str, int]:
    return {"opt10032_increment_count": 0, "tr_command_increment_count": 0, "realtime_registration_increment_count": 0, "subscription_plan_diff_count": 0, "gateway_queue_depth_impact": 0}


def _order_safety_payload() -> dict[str, int]:
    return {
        "send_order": 0,
        "cancel_order": 0,
        "modify_order": 0,
        "runtime_order_intents": 0,
        "managed_order_intents": 0,
        "managed_orders": 0,
        "live_sim_orders": 0,
        "broker_accepted": 0,
        "partial_fill": 0,
        "fill": 0,
    }


def _resolve_baseline(db: Any, *, trade_date: str, runtime_snapshot: Mapping[str, Any] | None, baseline: Mapping[str, Any] | None) -> dict[str, Any]:
    if baseline:
        return dict(baseline)
    runtime = dict(runtime_snapshot or {})
    if runtime.get("strategy_baseline"):
        return dict(runtime.get("strategy_baseline") or {})
    loader = getattr(db, "list_strategy_baseline_sessions", None)
    if callable(loader):
        rows = list(loader(trade_date=trade_date, limit=1) or [])
        if rows:
            return dict(rows[0])
    return {
        "baseline_id": "leader_first_pullback_v1",
        "baseline_version": "1.0.0",
        "drift_status": "UNKNOWN",
        "config_snapshot_completeness": "UNKNOWN",
    }


def _list_call(db: Any, name: str, **kwargs: Any) -> list[dict[str, Any]]:
    loader = getattr(db, name, None)
    if not callable(loader):
        return []
    try:
        return [dict(row or {}) for row in list(loader(**kwargs) or [])]
    except TypeError:
        kwargs.pop("offset", None)
        return [dict(row or {}) for row in list(loader(**kwargs) or [])]


def _has_table(db: Any, table: str) -> bool:
    conn = getattr(db, "conn", None)
    if conn is None:
        return True
    try:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
        return row is not None
    except Exception:
        return False


def _date_range(start: str, end: str) -> list[str]:
    start_date = date.fromisoformat(str(start))
    end_date = date.fromisoformat(str(end))
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    days = []
    current = start_date
    while current <= end_date:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _row_time(row: Mapping[str, Any]) -> str:
    return str(row.get("calculated_at") or row.get("observed_at") or row.get("last_evaluated_at") or row.get("updated_at") or row.get("created_at") or "")


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "")
        if text:
            return text
    return ""


def _set_min_time(payload: dict[str, Any], key: str, value: Any) -> bool:
    text = str(value or "")
    if not text:
        return False
    current = str(payload.get(key) or "")
    if not current or text < current:
        payload[key] = text
        return True
    return False


def _delay_seconds(start: Any, end: Any) -> int:
    a = _as_datetime(start)
    b = _as_datetime(end)
    if not a or not b:
        return 0
    return int((b - a).total_seconds())


def _append_delta(values: list[float], start: datetime | None, end: datetime | None) -> None:
    if start and end:
        values.append((end - start).total_seconds())


def _ratio(numerator: int, denominator: int) -> dict[str, Any] | None:
    if denominator <= 0:
        return None
    rate = float(numerator) / float(denominator)
    return {"numerator": int(numerator), "denominator": int(denominator), "rate": rate, "pct": round(rate * 100.0, 6)}


def _rate_value(value: Any) -> float | None:
    if isinstance(value, Mapping):
        return _optional_float(value.get("rate"))
    return _optional_float(value)


def _avg(values: Iterable[Any]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return round(sum(clean) / len(clean), 6) if clean else None


def _median(values: Iterable[Any]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return round(float(median(clean)), 6) if clean else None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return round(float(ordered[index]), 6)


def _wilson_interval(success: int, total: int, z: float = 1.96) -> dict[str, Any] | None:
    if total <= 0:
        return None
    p = success / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return {"success": success, "total": total, "low": round(max(0.0, centre - margin), 6), "high": round(min(1.0, centre + margin), 6)}


def _bootstrap_mean_ci(values: list[float], repetitions: int, seed: int) -> dict[str, Any] | None:
    if not values or repetitions <= 0:
        return None
    rng = random.Random(seed)
    means = []
    n = len(values)
    for _ in range(int(repetitions)):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(0.025 * (len(means) - 1))]
    hi = means[int(0.975 * (len(means) - 1))]
    return {"sample_count": n, "repetitions": int(repetitions), "seed": int(seed), "low": round(lo, 6), "high": round(hi, 6)}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return abs(float(str(value).replace(",", "").replace("+", "").replace("%", "").strip()))
    except Exception:
        return default


def _optional_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(str(value).replace(",", "").replace("+", "").replace("%", "").strip())
    except Exception:
        return None


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).replace(",", "").replace("+", "").strip()))
    except Exception:
        return default


def _pct(delta: float, base: float) -> float | None:
    if base <= 0:
        return None
    return round(float(delta) / float(base) * 100.0, 6)


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None, microsecond=0)
    except Exception:
        return None


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _stable_id(prefix: str, *parts: Any) -> str:
    material = "|".join(str(part or "") for part in parts)
    return f"{prefix}:{hashlib.sha1(material.encode('utf-8')).hexdigest()[:20]}"


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _stable_payload_fingerprint(value: Mapping[str, Any]) -> str:
    ignored = {"fingerprint", "created_at", "updated_at", "calculated_at", "revision"}
    return _fingerprint({key: value.get(key) for key in sorted(value) if key not in ignored})


def _report_id(prefix: str, start_date: str, end_date: str, report_state: str, as_of: datetime, baseline: Mapping[str, Any]) -> str:
    if str(report_state).upper() == "LIVE_PREVIEW":
        material = "|".join([prefix, start_date, end_date, "LIVE_PREVIEW", str(baseline.get("baseline_id") or ""), str(baseline.get("config_hash") or "")])
    else:
        material = "|".join([prefix, start_date, end_date, str(report_state), as_of.isoformat(), str(baseline.get("config_hash") or "")])
    return f"{prefix}:{start_date}:{end_date}:{str(report_state).lower()}:{hashlib.sha1(material.encode('utf-8')).hexdigest()[:16]}"


def _rank_bucket(rank: int) -> str:
    if 1 <= rank <= 10:
        return "1~10"
    if rank <= 30:
        return "11~30"
    if rank <= 50:
        return "31~50"
    if rank <= 100:
        return "51~100"
    return "UNKNOWN"


def _counter_rows(values: Iterable[str]) -> list[dict[str, Any]]:
    return _counter_to_rows(Counter(str(value or "UNKNOWN") for value in values))


def _counter_to_rows(counter: Counter) -> list[dict[str, Any]]:
    return [{"bucket": key, "count": int(value)} for key, value in sorted(counter.items())]


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if text and text not in result:
            result.append(text)
    return result


def _period_label(start: str, end: str) -> str:
    return start if start == end else f"{start}_{end}"


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)))
    except Exception:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, default)))
    except Exception:
        return default


def _int_tuple_env(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    values = []
    for part in raw.split(","):
        try:
            values.append(max(1, int(part.strip())))
        except Exception:
            continue
    return tuple(sorted(set(values))) or default


def _write_csv(path: Path, rows: list[Mapping[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in keys})


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value


def _flatten_metric_rows(payload: Mapping[str, Any], *, prefix: str = "") -> list[dict[str, Any]]:
    rows = []
    for key, value in payload.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            if {"numerator", "denominator", "rate"} <= set(value.keys()):
                rows.append({"metric": name, **dict(value)})
            else:
                rows.extend(_flatten_metric_rows(value, prefix=name))
        elif isinstance(value, list):
            rows.append({"metric": name, "value": json.dumps(value, ensure_ascii=False, default=str)})
        else:
            rows.append({"metric": name, "value": value})
    return rows


def _report_markdown(report: Mapping[str, Any]) -> str:
    discovery = dict(report.get("discovery_metrics") or {})
    funnel = dict(report.get("funnel_metrics") or {})
    valid = dict(report.get("valid_observe_metrics") or {})
    rec = dict(report.get("recommendation") or {})
    return "\n".join(
        [
            f"# Champion 성과 검증 ({report.get('trade_date_from')} ~ {report.get('trade_date_to')})",
            "",
            "이 리포트는 LEADER_FIRST_PULLBACK 검증 전용 read-only 분석입니다. 전략 설정과 주문 경로를 변경하지 않습니다.",
            "",
            "## 운영자 요약",
            f"- Evidence Tier: {report.get('evidence_tier', 'EMPTY')}",
            f"- Benchmark Episode: {discovery.get('benchmark_episode_count', 0)}",
            f"- Candidate Capture: {discovery.get('candidate_capture_count', 0)} / NOT_CAPTURED {discovery.get('candidate_not_captured_count', 0)}",
            f"- Champion Matched: {funnel.get('champion_matched_count', 0)}",
            f"- Champion Valid Observe: {funnel.get('champion_valid_observe_count', 0)}",
            f"- 15분 비용 반영 평균/중앙값: {valid.get('avg_cost_adjusted_return')} / {valid.get('median_cost_adjusted_return')}",
            f"- Primary Recommendation: {rec.get('primary_recommendation', '')}",
            "",
            "## 안전 장치",
            "- auto_apply_allowed=false",
            "- dry_run_auto_enable_allowed=false",
            "- send_order/cancel_order/modify_order count=0",
        ]
    )


__all__ = [
    "CHAMPION_SIGNAL_EPISODE_SCHEMA_VERSION",
    "CHAMPION_SIGNAL_ANCHOR_SCHEMA_VERSION",
    "CHAMPION_SIGNAL_OUTCOME_SCHEMA_VERSION",
    "CHAMPION_OUTCOME_REPORT_SCHEMA_VERSION",
    "ChampionOutcomeValidatorConfig",
    "ChampionOutcomeValidatorService",
    "export_champion_outcome_report",
]
