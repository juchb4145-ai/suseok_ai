from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from trading.strategy.candidate_state_contract import CandidateStateContractService
from trading.strategy.candidates import normalize_code


CANDIDATE_FUNNEL_SCHEMA_VERSION = "candidate_funnel.reboot_v2.v1"
CANDIDATE_FUNNEL_EPISODE_SCHEMA_VERSION = "candidate_funnel_episode.v1"
TRADING_DAY_QUALIFICATION_SCHEMA_VERSION = "trading_day_qualification.v1"
NO_TRADE_CLASSIFICATION_SCHEMA_VERSION = "no_trade_classification.v1"

RUNTIME_PROFILE = "THEME_CORE_V3"
CHAMPION_SETUP = "LEADER_FIRST_PULLBACK"
CHALLENGER_SETUPS = {"VWAP_RECLAIM", "BREAKOUT_RETEST"}
ROUTER_VERSION = "setup_router_v3.5.2"
TIMEZONE = "Asia/Seoul"
REPORT_ROOT = Path(__file__).resolve().parents[2] / "reports"

STAGE_ORDER = [
    "SOURCE_DETECTED",
    "CANDIDATE_CREATED",
    "ACTIVE_SOURCE_PRESENT",
    "HYDRATION_COMPLETE",
    "EVALUATION_ELIGIBLE",
    "REALTIME_SUBSCRIPTION_ACTIVE",
    "FRESH_REALTIME_READY",
    "STRATEGY_CONTEXT_READY",
    "ENTRY_EVALUATED",
    "CHAMPION_FORMING",
    "CHAMPION_MATCHED",
    "CHAMPION_CONTEXT_ELIGIBLE",
    "CHAMPION_VALID_OBSERVE",
    "ORDER_INTENT_CREATED",
    "GATEWAY_COMMAND_QUEUED",
    "BROKER_ACCEPTED",
    "PARTIAL_FILLED",
    "FILLED",
]
STAGE_ORDINAL = {stage: index for index, stage in enumerate(STAGE_ORDER)}
ORDER_STAGES = set(STAGE_ORDER[13:])
STAGE_REQUIRED_PREDECESSORS = {
    "ACTIVE_SOURCE_PRESENT": ("CANDIDATE_CREATED",),
    "HYDRATION_COMPLETE": ("CANDIDATE_CREATED",),
    "EVALUATION_ELIGIBLE": ("ACTIVE_SOURCE_PRESENT", "HYDRATION_COMPLETE"),
    "REALTIME_SUBSCRIPTION_ACTIVE": ("CANDIDATE_CREATED",),
    "FRESH_REALTIME_READY": ("REALTIME_SUBSCRIPTION_ACTIVE",),
    "STRATEGY_CONTEXT_READY": ("CANDIDATE_CREATED",),
    "ENTRY_EVALUATED": ("CANDIDATE_CREATED",),
    "CHAMPION_FORMING": ("ENTRY_EVALUATED", "FRESH_REALTIME_READY"),
    "CHAMPION_MATCHED": ("CHAMPION_FORMING",),
    "CHAMPION_CONTEXT_ELIGIBLE": ("CHAMPION_MATCHED", "STRATEGY_CONTEXT_READY"),
    "CHAMPION_VALID_OBSERVE": ("CHAMPION_CONTEXT_ELIGIBLE", "FRESH_REALTIME_READY"),
    "ORDER_INTENT_CREATED": ("CHAMPION_VALID_OBSERVE",),
    "GATEWAY_COMMAND_QUEUED": ("ORDER_INTENT_CREATED",),
    "BROKER_ACCEPTED": ("GATEWAY_COMMAND_QUEUED",),
    "PARTIAL_FILLED": ("BROKER_ACCEPTED",),
    "FILLED": ("BROKER_ACCEPTED",),
}
STRICT_LATENCY_STAGES = {
    "EVALUATION_ELIGIBLE",
    "FRESH_REALTIME_READY",
    "CHAMPION_FORMING",
    "CHAMPION_MATCHED",
    "CHAMPION_CONTEXT_ELIGIBLE",
    "CHAMPION_VALID_OBSERVE",
    "ORDER_INTENT_CREATED",
    "GATEWAY_COMMAND_QUEUED",
    "BROKER_ACCEPTED",
    "PARTIAL_FILLED",
    "FILLED",
}


@dataclass(frozen=True)
class CandidateFunnelConfig:
    enabled: bool = True
    save_episodes: bool = True
    strict_identity: bool = True

    @classmethod
    def from_env(cls) -> "CandidateFunnelConfig":
        return cls(
            enabled=_bool_env("TRADING_CANDIDATE_FUNNEL_ENABLED", True),
            save_episodes=_bool_env("TRADING_CANDIDATE_FUNNEL_SAVE_EPISODES", True),
            strict_identity=_bool_env("TRADING_CANDIDATE_FUNNEL_STRICT_IDENTITY", True),
        )


@dataclass(frozen=True)
class TradingDayQualificationConfig:
    enabled: bool = True
    sample_interval_sec: int = 30
    warmup_grace_sec: int = 180
    market_context_unavailable_max_rate: float = 0.05
    market_context_unavailable_max_consecutive: int = 3
    fallback_warn_rate: float = 0.10
    fallback_invalid_rate: float = 0.30
    subscription_warn_coverage: float = 0.90
    subscription_invalid_coverage: float = 0.70
    fresh_data_warn_coverage: float = 0.90
    fresh_data_invalid_coverage: float = 0.70

    @classmethod
    def from_env(cls) -> "TradingDayQualificationConfig":
        return cls(
            enabled=_bool_env("TRADING_DAY_QUALIFICATION_ENABLED", True),
            sample_interval_sec=max(1, _int_env("TRADING_DAY_QUALIFICATION_SAMPLE_INTERVAL_SEC", 30)),
            warmup_grace_sec=max(0, _int_env("TRADING_DAY_QUALIFICATION_WARMUP_GRACE_SEC", 180)),
            market_context_unavailable_max_rate=_float_env(
                "TRADING_DAY_QUALIFICATION_MARKET_CONTEXT_UNAVAILABLE_MAX_RATE",
                0.05,
            ),
            market_context_unavailable_max_consecutive=max(
                1,
                _int_env("TRADING_DAY_QUALIFICATION_MARKET_CONTEXT_UNAVAILABLE_MAX_CONSECUTIVE", 3),
            ),
            fallback_warn_rate=_float_env("TRADING_DAY_QUALIFICATION_FALLBACK_WARN_RATE", 0.10),
            fallback_invalid_rate=_float_env("TRADING_DAY_QUALIFICATION_FALLBACK_INVALID_RATE", 0.30),
            subscription_warn_coverage=_float_env("TRADING_DAY_QUALIFICATION_SUBSCRIPTION_WARN_COVERAGE", 0.90),
            subscription_invalid_coverage=_float_env("TRADING_DAY_QUALIFICATION_SUBSCRIPTION_INVALID_COVERAGE", 0.70),
            fresh_data_warn_coverage=_float_env("TRADING_DAY_QUALIFICATION_FRESH_DATA_WARN_COVERAGE", 0.90),
            fresh_data_invalid_coverage=_float_env("TRADING_DAY_QUALIFICATION_FRESH_DATA_INVALID_COVERAGE", 0.70),
        )


class CandidateFunnelService:
    def __init__(
        self,
        db: Any,
        *,
        config: CandidateFunnelConfig | None = None,
        clock: Any = datetime.now,
    ) -> None:
        self.db = db
        self.config = config or CandidateFunnelConfig.from_env()
        self.clock = clock
        self.last_report: dict[str, Any] = _disabled_candidate_funnel_section(self.clock())
        self.last_full_report: dict[str, Any] = {}

    def build_report(
        self,
        *,
        trade_date: str,
        as_of: datetime | str | None = None,
        report_state: str = "LIVE_PREVIEW",
        baseline: Mapping[str, Any] | None = None,
        persist: bool = True,
        export: bool = False,
        strict_only: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        current = _as_datetime(as_of) or self.clock().replace(microsecond=0)
        trade_date = str(trade_date or current.date().isoformat())
        if not self.config.enabled:
            self.last_report = _disabled_candidate_funnel_section(current)
            return dict(self.last_report)
        baseline_payload = dict(baseline or {})
        episodes = _build_episodes(self.db, trade_date=trade_date, as_of=current, persist_readiness_reconcile=persist)
        order_policy = _order_policy(baseline_payload)
        order_counts = _order_counts(self.db, trade_date)
        for episode in episodes.values():
            _finalize_episode(episode, order_policy=order_policy)
        invariant_violations = _invariant_violations(episodes.values(), as_of=current)
        stages = _stage_summary(episodes.values(), order_policy=order_policy)
        if strict_only:
            detail_episodes = [episode for episode in episodes.values() if episode["attribution_confidence"] == "HIGH"]
        else:
            detail_episodes = list(episodes.values())
        no_trade = _no_trade_classification(
            episodes.values(),
            order_counts=order_counts,
            qualification_status="",
        )
        report_id = _report_id("candidate_funnel", trade_date, report_state, current, baseline_payload)
        report = {
            "schema_version": CANDIDATE_FUNNEL_SCHEMA_VERSION,
            "report_id": report_id,
            "trade_date": trade_date,
            "as_of": current.isoformat(),
            "report_state": _report_state(report_state),
            "baseline_id": str(baseline_payload.get("baseline_id") or ""),
            "baseline_version": str(baseline_payload.get("baseline_version") or baseline_payload.get("version") or ""),
            "config_hash": str(baseline_payload.get("config_hash") or ""),
            "git_sha": str(baseline_payload.get("git_sha") or ""),
            "runtime_profile": str(baseline_payload.get("runtime_profile") or RUNTIME_PROFILE),
            "candidate_episode_count": len(episodes),
            "strict_attribution_count": sum(1 for item in episodes.values() if item["attribution_confidence"] == "HIGH"),
            "low_confidence_attribution_count": sum(1 for item in episodes.values() if item["attribution_confidence"] == "LOW"),
            "stages": stages,
            "drop_offs": _drop_offs(stages),
            "top_stop_reasons": _top_counts(episode["stop_reason_family"] for episode in episodes.values()),
            "invariant_violations": invariant_violations,
            "invariant_violation_count": len(invariant_violations),
            "critical_invariant_violation_count": sum(1 for item in invariant_violations if item.get("severity") == "CRITICAL"),
            "warning_invariant_violation_count": sum(1 for item in invariant_violations if item.get("severity") != "CRITICAL"),
            "no_trade_classification": no_trade,
            "episodes": sorted(detail_episodes, key=lambda item: (item["max_stage_ordinal"], item["candidate_instance_id"]), reverse=True),
            "generated_at": current.isoformat(),
            "build_duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }
        if persist:
            self._persist(report)
        if export:
            report["exported"] = export_candidate_funnel_report(report)
        self.last_full_report = dict(report)
        self.last_report = _candidate_funnel_runtime_section(report)
        return report

    def runtime_section(
        self,
        *,
        trade_date: str,
        as_of: datetime | str | None = None,
        baseline: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            report = self.build_report(trade_date=trade_date, as_of=as_of, baseline=baseline, persist=True)
            return _candidate_funnel_runtime_section(report)
        except Exception as exc:
            section = _disabled_candidate_funnel_section(_as_datetime(as_of) or self.clock())
            section.update({"enabled": True, "status": "ERROR", "error": str(exc), "warning_codes": ["CANDIDATE_FUNNEL_FAILED"]})
            self.last_report = section
            return section

    def _persist(self, report: Mapping[str, Any]) -> None:
        if self.config.save_episodes:
            saver = getattr(self.db, "save_candidate_funnel_episodes", None)
            if callable(saver):
                saver(list(report.get("episodes") or []))
        report_saver = getattr(self.db, "save_candidate_funnel_report", None)
        if callable(report_saver):
            report_saver(dict(report))


class TradingDayQualificationService:
    def __init__(
        self,
        db: Any,
        *,
        config: TradingDayQualificationConfig | None = None,
        clock: Any = datetime.now,
    ) -> None:
        self.db = db
        self.config = config or TradingDayQualificationConfig.from_env()
        self.clock = clock
        self.last_report: dict[str, Any] = _disabled_qualification_section(self.clock())

    def build_report(
        self,
        *,
        trade_date: str,
        as_of: datetime | str | None = None,
        report_state: str = "LIVE_PREVIEW",
        runtime_snapshot: Mapping[str, Any] | None = None,
        funnel_report: Mapping[str, Any] | None = None,
        finalize: bool = False,
        persist: bool = True,
        export: bool = False,
        rebuild_reason: str = "",
    ) -> dict[str, Any]:
        started = time.perf_counter()
        current = _as_datetime(as_of) or self.clock().replace(microsecond=0)
        trade_date = str(trade_date or current.date().isoformat())
        if not self.config.enabled:
            self.last_report = _disabled_qualification_section(current)
            return dict(self.last_report)
        runtime = dict(runtime_snapshot or {})
        state = "FINAL" if finalize or str(report_state).upper() == "FINAL" else "LIVE_PREVIEW"
        baseline = _resolve_baseline(self.db, trade_date=trade_date, runtime_snapshot=runtime)
        funnel = dict(funnel_report or {})
        if not funnel or funnel.get("schema_version") != CANDIDATE_FUNNEL_SCHEMA_VERSION:
            funnel = CandidateFunnelService(self.db).build_report(
                trade_date=trade_date,
                as_of=current,
                report_state=state,
                baseline=baseline,
                persist=persist,
            )
        health_sample = save_ops_runtime_health_sample(
            self.db,
            trade_date=trade_date,
            runtime_snapshot=runtime,
            funnel_report=funnel,
            as_of=current,
            sample_interval_sec=self.config.sample_interval_sec,
        )
        evidence = _qualification_evidence(self.db, trade_date=trade_date, runtime_snapshot=runtime, funnel_report=funnel)
        checks = [
            _baseline_check(baseline),
            _runtime_check(runtime, evidence),
            _market_context_check(evidence, self.config),
            _realtime_check(evidence, self.config),
            _candidate_attribution_check(funnel),
            _snapshot_check(evidence),
            _order_safety_check(evidence),
            _funnel_integrity_check(funnel),
        ]
        status = _qualification_status(checks, report_state=state, evidence=evidence)
        score = _qualification_score(checks)
        strict_sample_eligible = bool(state == "FINAL" and status == "VALID")
        no_trade = _no_trade_classification(
            list(funnel.get("episodes") or []),
            order_counts=dict(evidence.get("order_counts") or {}),
            qualification_status=status,
        )
        reason_codes = _dedupe(reason for check in checks for reason in check.get("reason_codes", []))
        warning_codes = _dedupe(reason for check in checks if check.get("status") == "WARN" for reason in check.get("reason_codes", []))
        critical_issue_count = sum(1 for check in checks if check.get("status") == "FAIL" and check.get("critical"))
        degraded_issue_count = sum(1 for check in checks if check.get("status") == "WARN")
        report_id = _report_id("trading_day_qualification", trade_date, state, current, baseline)
        report = {
            "schema_version": TRADING_DAY_QUALIFICATION_SCHEMA_VERSION,
            "report_id": report_id,
            "trade_date": trade_date,
            "timezone": TIMEZONE,
            "report_state": state,
            "qualification_status": status,
            "qualification_score": score,
            "strict_sample_eligible": strict_sample_eligible,
            "baseline_id": str(baseline.get("baseline_id") or ""),
            "baseline_version": str(baseline.get("baseline_version") or baseline.get("version") or ""),
            "config_hash": str(baseline.get("config_hash") or ""),
            "git_sha": str(baseline.get("git_sha") or ""),
            "generated_at": current.isoformat(),
            "as_of": current.isoformat(),
            "finalized_at": current.isoformat() if state == "FINAL" else "",
            "evaluated_window": _evaluated_window(trade_date, evidence, current),
            "market_session_status": str(evidence.get("market_session_status") or ""),
            "checks": checks,
            "reason_codes": reason_codes,
            "warning_codes": warning_codes,
            "critical_issue_count": critical_issue_count,
            "degraded_issue_count": degraded_issue_count,
            "evidence_summary": evidence,
            "recommended_operator_action_ko": _recommended_action(status, no_trade),
            "funnel_report_id": str(funnel.get("report_id") or ""),
            "no_trade_classification": no_trade,
            "session_qualifications": _session_qualifications(funnel),
            "champion_sample_eligible_by_session": _champion_session_eligibility(funnel, strict_sample_eligible),
            "revision": 0,
            "supersedes_report_id": "",
            "rebuild_reason": str(rebuild_reason or ""),
            "source_cutoff_at": current.isoformat(),
            "health_sample": health_sample,
            "build_duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }
        if persist:
            saver = getattr(self.db, "save_trading_day_qualification_report", None)
            if callable(saver):
                saved = dict(saver(report) or {})
                report["revision"] = int(saved.get("revision") or report.get("revision") or 0)
                report["report_id"] = str(saved.get("report_id") or report["report_id"])
        if export:
            report["exported"] = export_trading_day_qualification_report(report)
        self.last_report = _qualification_runtime_section(report)
        return report

    def runtime_section(
        self,
        *,
        trade_date: str,
        as_of: datetime | str | None = None,
        runtime_snapshot: Mapping[str, Any] | None = None,
        funnel_report: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            report = self.build_report(
                trade_date=trade_date,
                as_of=as_of,
                runtime_snapshot=runtime_snapshot,
                funnel_report=funnel_report,
                report_state="LIVE_PREVIEW",
                persist=True,
            )
            return _qualification_runtime_section(report)
        except Exception as exc:
            section = _disabled_qualification_section(_as_datetime(as_of) or self.clock())
            section.update({"enabled": True, "status": "ERROR", "error": str(exc), "warning_codes": ["TRADING_DAY_QUALIFICATION_FAILED"]})
            self.last_report = section
            return section


def save_ops_runtime_health_sample(
    db: Any,
    *,
    trade_date: str,
    runtime_snapshot: Mapping[str, Any] | None,
    funnel_report: Mapping[str, Any] | None,
    as_of: datetime,
    sample_interval_sec: int = 30,
) -> dict[str, Any]:
    runtime = dict(runtime_snapshot or {})
    funnel = dict(funnel_report or {})
    market_transport = dict(runtime.get("market_context_transport") or {})
    read_model = dict(runtime.get("read_model") or {})
    section = _candidate_funnel_runtime_section(funnel) if funnel.get("schema_version") == CANDIDATE_FUNNEL_SCHEMA_VERSION else dict(funnel)
    payload = {
        "trade_date": trade_date,
        "bucket_at": _bucket_at(as_of, sample_interval_sec),
        "sampled_at": as_of.isoformat(),
        "runtime_cycle_count": _int(runtime.get("cycle_count") or runtime.get("runtime_cycle_count"), 0),
        "runtime_status": str(runtime.get("status") or ""),
        "runtime_profile": str(runtime.get("runtime_profile") or RUNTIME_PROFILE),
        "market_context_source": str(market_transport.get("source") or runtime.get("market_context_source") or "UNAVAILABLE"),
        "market_context_available": str(market_transport.get("source") or "").upper() != "UNAVAILABLE",
        "dashboard_generation": _int(read_model.get("generation"), 0),
        "dashboard_checksum": str(read_model.get("checksum") or ""),
        "dashboard_namespace": str(read_model.get("snapshot_namespace") or runtime.get("snapshot_namespace") or ""),
        "candidate_episode_count": _int(section.get("candidate_episode_count"), 0),
        "evaluation_eligible_count": _int(section.get("evaluation_eligible_count"), 0),
        "active_subscription_count": _int(section.get("active_subscription_count"), 0),
        "fresh_realtime_ready_count": _int(section.get("fresh_realtime_ready_count"), 0),
        "champion_forming_count": _int(section.get("champion_forming_count"), 0),
        "champion_matched_count": _int(section.get("champion_matched_count"), 0),
        "champion_valid_observe_count": _int(section.get("champion_valid_observe_count"), 0),
        "payload": {
            "market_context_transport": market_transport,
            "candidate_funnel": section,
            "read_model": read_model,
        },
    }
    saver = getattr(db, "save_ops_runtime_health_sample", None)
    if callable(saver):
        return dict(saver(payload) or payload)
    return payload


def export_candidate_funnel_report(report: Mapping[str, Any], *, root: Path | None = None) -> dict[str, str]:
    trade_date = str(report.get("trade_date") or "unknown")
    out = (root or REPORT_ROOT) / "candidate_funnel" / trade_date
    out.mkdir(parents=True, exist_ok=True)
    summary_json = out / "summary.json"
    summary_md = out / "summary.md"
    stages_csv = out / "stages.csv"
    episodes_csv = out / "episodes.csv"
    violations_csv = out / "invariant_violations.csv"
    summary_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    summary_md.write_text(_candidate_funnel_markdown(report), encoding="utf-8")
    _write_csv(stages_csv, list(report.get("stages") or []))
    _write_csv(episodes_csv, list(report.get("episodes") or []))
    _write_csv(violations_csv, list(report.get("invariant_violations") or []))
    return {"json": str(summary_json), "md": str(summary_md), "stages_csv": str(stages_csv), "episodes_csv": str(episodes_csv), "invariant_violations_csv": str(violations_csv)}


def export_trading_day_qualification_report(report: Mapping[str, Any], *, root: Path | None = None) -> dict[str, str]:
    trade_date = str(report.get("trade_date") or "unknown")
    out = (root or REPORT_ROOT) / "trading_day_qualification" / trade_date
    out.mkdir(parents=True, exist_ok=True)
    report_json = out / "report.json"
    report_md = out / "report.md"
    checks_csv = out / "checks.csv"
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    report_md.write_text(_qualification_markdown(report), encoding="utf-8")
    _write_csv(checks_csv, list(report.get("checks") or []))
    return {"json": str(report_json), "md": str(report_md), "checks_csv": str(checks_csv)}


def _build_episodes(db: Any, *, trade_date: str, as_of: datetime, persist_readiness_reconcile: bool = False) -> dict[str, dict[str, Any]]:
    contract_service = CandidateStateContractService(db=None)
    candidates = list(db.list_candidates(trade_date=trade_date) or [])
    source_events = _safe_call(getattr(db, "list_candidate_source_events", None), trade_date=trade_date, limit=100000)
    readiness_rows = _safe_call(getattr(db, "list_setup_router_readiness_latest", None), trade_date=trade_date, router_version=ROUTER_VERSION, limit=100000)
    lifecycle_rows = _safe_call(
        getattr(db, "list_realtime_subscription_lifecycle_latest", None),
        trade_date=trade_date,
        lifecycle_state="ACTIVE_FRESH",
        limit=100000,
    )
    observations = _safe_call(getattr(db, "list_setup_observations_latest", None), trade_date=trade_date, router_version=ROUTER_VERSION, limit=100000)
    runtime_rows = _safe_call(getattr(db, "list_setup_router_candidate_runtime", None), trade_date=trade_date, router_version=ROUTER_VERSION, limit=100000)
    contexts = _rows(db, "SELECT * FROM strategy_context_latest WHERE trade_date = ?", (trade_date,))
    entries = _rows(db, "SELECT * FROM entry_decisions WHERE trade_date = ? ORDER BY calculated_at ASC, id ASC", (trade_date,))
    order_intents = _rows(db, "SELECT * FROM runtime_order_intents WHERE trade_date = ?", (trade_date,))
    managed_intents = _rows(db, "SELECT * FROM managed_order_intents WHERE trade_date = ?", (trade_date,))
    managed_orders = _rows(db, "SELECT * FROM managed_orders WHERE trade_date = ?", (trade_date,))
    live_orders = _rows(db, "SELECT * FROM live_sim_orders WHERE trade_date = ?", (trade_date,))
    by_candidate_id: dict[int, dict[str, Any]] = {}
    by_code: dict[str, dict[str, Any]] = {}
    episodes: dict[str, dict[str, Any]] = {}

    for candidate in candidates:
        metadata = dict(getattr(candidate, "metadata", {}) or {})
        candidate_id = getattr(candidate, "id", None)
        ci = _candidate_instance_id(metadata)
        confidence = "HIGH" if ci else "LOW"
        if not ci:
            ci = f"LOW:{trade_date}:{normalize_code(getattr(candidate, 'code', ''))}:{candidate_id or 0}"
        episode = _episode(
            trade_date=trade_date,
            candidate_instance_id=ci,
            candidate_id=candidate_id,
            candidate_generation_seq=_generation_seq(metadata),
            code=normalize_code(getattr(candidate, "code", "")),
            name=str(getattr(candidate, "name", "") or ""),
            attribution_confidence=confidence,
        )
        episode["first_seen_at"] = str(getattr(candidate, "detected_at", "") or "")
        episode["last_seen_at"] = str(getattr(candidate, "last_seen_at", "") or episode["first_seen_at"])
        episode["terminal"] = str(getattr(getattr(candidate, "state", ""), "value", getattr(candidate, "state", ""))) in {"removed", "expired", "cancelled", "REMOVED", "EXPIRED", "CANCELLED"}
        episode["source_types"] = [str(getattr(source, "value", source)) for source in list(getattr(candidate, "sources", []) or [])]
        _reach(episode, "CANDIDATE_CREATED", episode["first_seen_at"])
        if episode["source_types"]:
            _reach(episode, "SOURCE_DETECTED", episode["first_seen_at"])
        try:
            snapshot = dict(metadata.get("candidate_state_contract") or contract_service.snapshot(candidate).to_dict())
        except Exception:
            snapshot = dict(metadata.get("candidate_state_contract") or {})
        if snapshot.get("active_source_exists"):
            _reach(episode, "ACTIVE_SOURCE_PRESENT", episode["last_seen_at"])
        if snapshot.get("hydration_complete"):
            _reach(episode, "HYDRATION_COMPLETE", episode["last_seen_at"])
        if snapshot.get("evaluation_eligible"):
            _reach(episode, "EVALUATION_ELIGIBLE", episode["last_seen_at"])
        for reason in [snapshot.get("primary_reason_code"), snapshot.get("evaluation_eligibility"), snapshot.get("lifecycle_readiness")]:
            if reason:
                episode["reason_codes"].append(str(reason))
        episodes[ci] = episode
        if candidate_id is not None:
            by_candidate_id[int(candidate_id)] = episode
        by_code[normalize_code(getattr(candidate, "code", ""))] = episode

    for row in [*readiness_rows, *observations, *runtime_rows]:
        ci = str(row.get("candidate_instance_id") or "")
        if not ci or ci in episodes:
            continue
        candidate_id = _maybe_int(row.get("candidate_id"))
        code = normalize_code(row.get("code"))
        at = str(
            row.get("first_seen_at")
            or row.get("calculated_at")
            or row.get("last_success_at")
            or row.get("updated_at")
            or as_of.isoformat()
        )
        episode = _episode(
            trade_date=trade_date,
            candidate_instance_id=ci,
            candidate_id=candidate_id,
            candidate_generation_seq=_int(row.get("candidate_generation_seq") or row.get("setup_generation"), 0),
            code=code,
            name=str(row.get("name") or ""),
            attribution_confidence="HIGH",
        )
        episode["first_seen_at"] = at
        episode["last_seen_at"] = str(row.get("last_seen_at") or row.get("updated_at") or row.get("calculated_at") or at)
        episode["source_types"] = ["candidate_instance_fallback"]
        _reach(episode, "SOURCE_DETECTED", at)
        _reach(episode, "CANDIDATE_CREATED", at)
        episodes[ci] = episode
        if candidate_id is not None:
            by_candidate_id[int(candidate_id)] = episode
        if code and code not in by_code:
            by_code[code] = episode

    readiness_rows = _reconcile_readiness_rows_with_active_lifecycle(
        readiness_rows,
        lifecycle_rows,
        episodes.values(),
        as_of=as_of,
    )
    if persist_readiness_reconcile:
        _persist_reconciled_readiness_rows(db, readiness_rows)

    for event in source_events:
        candidate_id = _maybe_int(event.get("candidate_id"))
        code = normalize_code(event.get("code"))
        episode = by_candidate_id.get(candidate_id) if candidate_id is not None else by_code.get(code)
        if episode is None:
            ci = f"LOW:{trade_date}:{code}:source:{event.get('id')}"
            episode = _episode(
                trade_date=trade_date,
                candidate_instance_id=ci,
                candidate_id=candidate_id,
                candidate_generation_seq=0,
                code=code,
                name=str(event.get("name") or ""),
                attribution_confidence="LOW",
            )
            episodes[ci] = episode
        source_type = str(event.get("source_type") or "")
        if source_type and source_type not in episode["source_types"]:
            episode["source_types"].append(source_type)
        episode["reason_codes"].extend(str(item) for item in list(event.get("reason_codes") or []) if str(item))
        _reach(episode, "SOURCE_DETECTED", event.get("detected_at") or event.get("created_at"))

    for row in readiness_rows:
        episode = episodes.get(str(row.get("candidate_instance_id") or ""))
        if episode is None:
            continue
        at = str(row.get("calculated_at") or row.get("updated_at") or "")
        episode["reason_codes"].extend(str(item) for item in list(row.get("reason_codes") or []) if str(item))
        if row.get("subscription_active"):
            _reach(episode, "REALTIME_SUBSCRIPTION_ACTIVE", at)
        if _fresh_readiness(row):
            _reach(episode, "FRESH_REALTIME_READY", at)

    for row in contexts:
        payload = _json(row.get("payload_json"), {})
        candidate_id = _maybe_int(row.get("candidate_id"))
        code = normalize_code(row.get("code"))
        episode = by_candidate_id.get(candidate_id) if candidate_id is not None else by_code.get(code)
        if episode is None:
            continue
        episode["reason_codes"].extend(_json(row.get("reason_codes_json"), []))
        episode["reason_codes"].extend(str(item) for item in list(payload.get("reason_codes") or []) if str(item))
        if bool(row.get("context_fresh")):
            _reach(episode, "STRATEGY_CONTEXT_READY", row.get("calculated_at"))

    for row in entries:
        payload = _json(row.get("payload_json"), {})
        candidate_id = _maybe_int(row.get("candidate_id"))
        code = normalize_code(row.get("code"))
        episode = by_candidate_id.get(candidate_id) if candidate_id is not None else by_code.get(code)
        if episode is None:
            continue
        episode["reason_codes"].extend(_json(row.get("reason_codes_json"), []))
        episode["reason_codes"].extend(str(item) for item in list(payload.get("reason_codes") or []) if str(item))
        _reach(episode, "ENTRY_EVALUATED", row.get("calculated_at"), attempt=1)

    runtime_by_ci = {str(row.get("candidate_instance_id") or ""): row for row in runtime_rows}
    for row in observations:
        ci = str(row.get("candidate_instance_id") or "")
        episode = episodes.get(ci)
        if episode is None:
            continue
        readiness_at = str(row.get("input_readiness_calculated_at") or row.get("calculated_at") or row.get("updated_at") or "")
        if _observation_fresh_readiness(row):
            _reach(episode, "REALTIME_SUBSCRIPTION_ACTIVE", readiness_at)
            _reach(episode, "FRESH_REALTIME_READY", readiness_at)
        setup_type = str(row.get("setup_type") or "")
        role = str(row.get("baseline_role") or _baseline_role(setup_type))
        if role != "OUT_OF_SCOPE":
            episode["baseline_role"] = role
        episode["reason_codes"].extend(str(item) for item in list(row.get("reason_codes") or []) if str(item))
        if setup_type != CHAMPION_SETUP:
            continue
        at = str(row.get("calculated_at") or row.get("updated_at") or "")
        runtime = runtime_by_ci.get(ci) or {}
        attempt = max(1, _int(runtime.get("shape_evaluation_count"), 0))
        shape = str(row.get("shape_status") or "")
        if shape == "FORMING":
            _reach(episode, "CHAMPION_FORMING", at, attempt=attempt)
        if shape == "MATCHED":
            _reach(episode, "CHAMPION_FORMING", at)
            _reach(episode, "CHAMPION_MATCHED", at, attempt=attempt)
        if shape == "MATCHED" and str(row.get("context_status") or "") == "ELIGIBLE":
            _reach(episode, "CHAMPION_CONTEXT_ELIGIBLE", at)
        if str(row.get("router_status") or "") == "VALID_OBSERVE" and role == "CHAMPION":
            _reach(episode, "CHAMPION_VALID_OBSERVE", at)

    _mark_order_stages(episodes, order_intents, "ORDER_INTENT_CREATED", "created_at")
    _mark_order_stages(episodes, managed_intents, "GATEWAY_COMMAND_QUEUED", "created_at")
    _mark_order_stages(episodes, managed_orders, "BROKER_ACCEPTED", "acked_at")
    _mark_order_stages(episodes, managed_orders, "PARTIAL_FILLED", "acked_at", status_values={"PARTIALLY_FILLED"})
    _mark_order_stages(episodes, managed_orders, "FILLED", "updated_at", status_values={"FILLED"})
    _mark_order_stages(episodes, live_orders, "BROKER_ACCEPTED", "accepted_at")
    _mark_order_stages(episodes, live_orders, "PARTIAL_FILLED", "first_fill_at")
    _mark_order_stages(episodes, live_orders, "FILLED", "last_fill_at")

    for episode in episodes.values():
        episode["reason_codes"] = _dedupe(episode["reason_codes"])
        episode["updated_at"] = as_of.isoformat()
    return episodes


def _episode(
    *,
    trade_date: str,
    candidate_instance_id: str,
    candidate_id: int | None,
    candidate_generation_seq: int,
    code: str,
    name: str,
    attribution_confidence: str,
) -> dict[str, Any]:
    return {
        "schema_version": CANDIDATE_FUNNEL_EPISODE_SCHEMA_VERSION,
        "trade_date": trade_date,
        "candidate_instance_id": candidate_instance_id,
        "candidate_id": candidate_id,
        "candidate_generation_seq": candidate_generation_seq,
        "code": code,
        "name": name,
        "first_seen_at": "",
        "last_seen_at": "",
        "baseline_role": "OUT_OF_SCOPE",
        "champion_setup": CHAMPION_SETUP,
        "current_stage": "",
        "max_stage_ordinal": -1,
        "reached_stages": [],
        "stage_first_reached_at": {},
        "stage_last_seen_at": {},
        "stage_attempt_counts": {},
        "stop_stage": "",
        "stop_reason_family": "UNKNOWN",
        "primary_reason_code": "",
        "reason_codes": [],
        "attribution_confidence": attribution_confidence,
        "source_types": [],
        "terminal": False,
        "fingerprint": "",
        "updated_at": "",
    }


def _reach(episode: dict[str, Any], stage: str, reached_at: Any = "", *, attempt: int = 1) -> None:
    if stage not in STAGE_ORDINAL:
        return
    at = str(reached_at or "")
    stages = list(episode.get("reached_stages") or [])
    if stage not in stages:
        stages.append(stage)
    first = dict(episode.get("stage_first_reached_at") or {})
    last = dict(episode.get("stage_last_seen_at") or {})
    if at and (not first.get(stage) or at < str(first.get(stage))):
        first[stage] = at
    elif stage not in first:
        first[stage] = at
    if at and at > str(last.get(stage) or ""):
        last[stage] = at
    elif stage not in last:
        last[stage] = at
    attempts = dict(episode.get("stage_attempt_counts") or {})
    attempts[stage] = int(attempts.get(stage) or 0) + max(1, int(attempt or 1))
    episode["reached_stages"] = sorted(stages, key=lambda item: STAGE_ORDINAL[item])
    episode["stage_first_reached_at"] = first
    episode["stage_last_seen_at"] = last
    episode["stage_attempt_counts"] = attempts
    max_stage = max(STAGE_ORDINAL[item] for item in episode["reached_stages"])
    if max_stage > int(episode.get("max_stage_ordinal") or -1):
        episode["max_stage_ordinal"] = max_stage
        episode["current_stage"] = STAGE_ORDER[max_stage]


def _finalize_episode(episode: dict[str, Any], *, order_policy: Mapping[str, Any]) -> None:
    reached = set(episode.get("reached_stages") or [])
    applicable = _applicable_stages(order_policy)
    applicable_reached = [stage for stage in reached if stage in applicable]
    if applicable_reached:
        max_stage = max(STAGE_ORDINAL[stage] for stage in applicable_reached)
        episode["max_stage_ordinal"] = max_stage
        episode["current_stage"] = STAGE_ORDER[max_stage]
    current = str(episode.get("current_stage") or "SOURCE_DETECTED")
    episode["stop_stage"] = current
    episode["stop_reason_family"] = _stop_reason(episode, order_policy=order_policy)
    if not episode.get("primary_reason_code"):
        episode["primary_reason_code"] = _primary_reason(episode)
    material = {
        "candidate_instance_id": episode.get("candidate_instance_id"),
        "max_stage_ordinal": episode.get("max_stage_ordinal"),
        "reached_stages": episode.get("reached_stages"),
        "stop_reason_family": episode.get("stop_reason_family"),
        "reason_codes": episode.get("reason_codes"),
        "stage_first_reached_at": episode.get("stage_first_reached_at"),
    }
    episode["fingerprint"] = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _stage_summary(episodes: Iterable[Mapping[str, Any]], *, order_policy: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    episode_list = list(episodes)
    applicable = _applicable_stages(order_policy)
    for stage in STAGE_ORDER:
        ordinal = STAGE_ORDINAL[stage]
        reached = [item for item in episode_list if stage in set(item.get("reached_stages") or [])]
        strict = [item for item in reached if item.get("attribution_confidence") == "HIGH"]
        low = [item for item in reached if item.get("attribution_confidence") == "LOW"]
        stopped = [item for item in episode_list if item.get("stop_stage") == stage]
        attempts = sum(_int(dict(item.get("stage_attempt_counts") or {}).get(stage), 0) for item in episode_list)
        previous_stage = STAGE_ORDER[ordinal - 1] if ordinal > 0 else ""
        previous_reached = [item for item in episode_list if previous_stage in set(item.get("reached_stages") or [])] if previous_stage else []
        conversion = _ratio(len(reached), len(previous_reached)) if previous_stage else None
        cumulative = _ratio(len(reached), len(episode_list))
        first_times = [str(dict(item.get("stage_first_reached_at") or {}).get(stage) or "") for item in reached]
        first_times = [item for item in first_times if item]
        latencies = _latencies_ms(reached, stage, previous_stage)
        reason_counts = Counter()
        for item in stopped:
            reason_counts.update(list(item.get("reason_codes") or [])[:5])
            if item.get("stop_reason_family"):
                reason_counts.update([str(item.get("stop_reason_family"))])
        rows.append(
            {
                "stage": stage,
                "ordinal": ordinal,
                "applicable": stage in applicable,
                "expected_disabled": stage in ORDER_STAGES and stage not in applicable,
                "reached_count": len(reached),
                "strict_reached_count": len(strict),
                "low_confidence_reached_count": len(low),
                "stopped_here_count": len(stopped),
                "transition_count": max(len(reached), attempts),
                "evaluation_attempt_count": attempts,
                "conversion_from_previous_pct": conversion["pct"] if conversion else None,
                "conversion_from_previous_numerator": conversion["numerator"] if conversion else None,
                "conversion_from_previous_denominator": conversion["denominator"] if conversion else None,
                "cumulative_conversion_pct": cumulative["pct"] if cumulative else None,
                "cumulative_conversion_numerator": cumulative["numerator"] if cumulative else None,
                "cumulative_conversion_denominator": cumulative["denominator"] if cumulative else None,
                "first_reached_at": min(first_times) if first_times else "",
                "last_reached_at": max(first_times) if first_times else "",
                "p50_latency_from_previous_ms": _percentile(latencies, 0.50),
                "p95_latency_from_previous_ms": _percentile(latencies, 0.95),
                "top_reason_codes": [{"reason": key, "count": value} for key, value in reason_counts.most_common(5)],
            }
        )
    return rows


def _invariant_violations(episodes: Iterable[Mapping[str, Any]], *, as_of: datetime) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    episode_rows = list(episodes)
    as_of_text = as_of.isoformat()
    for episode in episode_rows:
        ci = str(episode.get("candidate_instance_id") or "")
        reached = set(episode.get("reached_stages") or [])
        times = dict(episode.get("stage_first_reached_at") or {})
        emitted_missing: dict[tuple[str, str], dict[str, Any]] = {}
        for stage in reached:
            for required in STAGE_REQUIRED_PREDECESSORS.get(stage, ()):
                if required in reached:
                    continue
                key = (ci, required)
                blocked = sorted(
                    item
                    for item in reached
                    if required in STAGE_REQUIRED_PREDECESSORS.get(item, ())
                )
                severity = _missing_predecessor_severity(stage, required)
                existing = emitted_missing.get(key)
                if existing is not None:
                    existing["blocked_stages"] = sorted(set(existing.get("blocked_stages") or []) | set(blocked))
                    if severity == "CRITICAL" and existing.get("severity") != "CRITICAL":
                        existing["severity"] = "CRITICAL"
                        existing["stage"] = stage
                    continue
                violation = {
                    "candidate_instance_id": ci,
                    "type": "DOWNSTREAM_WITHOUT_UPSTREAM",
                    "severity": severity,
                    "stage": stage,
                    "missing_stage": required,
                    "blocked_stages": blocked,
                }
                emitted_missing[key] = violation
                violations.append(violation)
            at = str(times.get(stage) or "")
            if at and at > as_of_text:
                violations.append({"candidate_instance_id": ci, "type": "FUTURE_TIMESTAMP", "severity": "CRITICAL", "stage": stage, "timestamp": at, "as_of": as_of_text})
            if stage in STRICT_LATENCY_STAGES:
                for prev in STAGE_REQUIRED_PREDECESSORS.get(stage, ()):
                    prev_at = str(times.get(prev) or "")
                    if at and prev_at and at < prev_at:
                        violations.append(
                            {
                                "candidate_instance_id": ci,
                                "type": "NEGATIVE_STAGE_LATENCY",
                                "severity": "CRITICAL",
                                "stage": stage,
                                "previous_stage": prev,
                                "stage_at": at,
                                "previous_at": prev_at,
                            }
                        )
        if int(episode.get("max_stage_ordinal") or -1) < max([STAGE_ORDINAL[item] for item in reached], default=-1):
            violations.append({"candidate_instance_id": ci, "type": "MAX_STAGE_REGRESSION", "severity": "CRITICAL", "stage": episode.get("current_stage") or ""})
    code_by_instance: dict[str, str] = {}
    for episode in episode_rows:
        ci = str(episode.get("candidate_instance_id") or "")
        code = str(episode.get("code") or "")
        if ci in code_by_instance and code_by_instance[ci] != code:
            violations.append({"candidate_instance_id": ci, "type": "IDENTITY_CONFLICT", "severity": "CRITICAL", "code": code, "previous_code": code_by_instance[ci]})
        code_by_instance[ci] = code
    return violations


def _missing_predecessor_severity(stage: str, missing_stage: str) -> str:
    if stage in ORDER_STAGES:
        return "CRITICAL"
    if stage.startswith("CHAMPION_"):
        return "CRITICAL"
    if stage in {"EVALUATION_ELIGIBLE", "FRESH_REALTIME_READY"}:
        return "CRITICAL"
    if missing_stage in {"FRESH_REALTIME_READY", "REALTIME_SUBSCRIPTION_ACTIVE"} and stage in {"STRATEGY_CONTEXT_READY", "ENTRY_EVALUATED"}:
        return "WARNING"
    return "WARNING"


def _qualification_evidence(
    db: Any,
    *,
    trade_date: str,
    runtime_snapshot: Mapping[str, Any],
    funnel_report: Mapping[str, Any],
) -> dict[str, Any]:
    samples = _safe_call(getattr(db, "list_ops_runtime_health_samples", None), trade_date=trade_date, limit=100000)
    readiness = _safe_call(getattr(db, "list_setup_router_readiness_latest", None), trade_date=trade_date, router_version=ROUTER_VERSION, limit=100000)
    lifecycle_rows = _safe_call(
        getattr(db, "list_realtime_subscription_lifecycle_latest", None),
        trade_date=trade_date,
        lifecycle_state="ACTIVE_FRESH",
        limit=100000,
    )
    readiness = _reconcile_readiness_rows_with_active_lifecycle(
        readiness,
        lifecycle_rows,
        list(funnel_report.get("episodes") or []),
        as_of=_as_datetime(funnel_report.get("as_of")) or datetime.now().replace(microsecond=0),
    )
    order_counts = _order_counts(db, trade_date)
    current_eval_instances = _current_eval_instances(funnel_report)
    if current_eval_instances:
        readiness = [item for item in readiness if str(item.get("candidate_instance_id") or "") in current_eval_instances]
    market_sources = Counter(str(item.get("market_context_source") or "UNAVAILABLE") for item in samples)
    if not market_sources:
        source = str(dict(runtime_snapshot.get("market_context_transport") or {}).get("source") or "UNAVAILABLE")
        market_sources[source] += 1
    source_total = sum(market_sources.values())
    fallback_count = sum(count for source, count in market_sources.items() if source in {"DB_FALLBACK", "DASHBOARD_SUMMARY_FALLBACK"})
    unavailable_count = market_sources.get("UNAVAILABLE", 0)
    selected = sum(1 for item in readiness if item.get("subscription_selected") or item.get("subscription_target_selected"))
    active = sum(1 for item in readiness if item.get("subscription_active"))
    eval_count = len(current_eval_instances) if current_eval_instances else _stage_count(funnel_report, "EVALUATION_ELIGIBLE")
    fresh_count = sum(1 for item in readiness if item.get("post_subscription_tick_verified")) if current_eval_instances else _stage_count(funnel_report, "FRESH_REALTIME_READY")
    snapshot_integrity = _snapshot_integrity(samples, db)
    return {
        "runtime_cycle_count": _int(runtime_snapshot.get("cycle_count") or runtime_snapshot.get("runtime_cycle_count"), 0),
        "candidate_episode_count": _int(funnel_report.get("candidate_episode_count"), 0),
        "runtime_status": str(runtime_snapshot.get("status") or ""),
        "pipeline_status": dict(runtime_snapshot.get("pipeline_status") or {}),
        "market_session_status": str(dict(runtime_snapshot.get("market_regime") or {}).get("market_session_status") or ""),
        "market_context_source_counts": _counts_with_rates(market_sources),
        "market_context_unavailable_rate": _safe_rate(unavailable_count, source_total),
        "market_context_unavailable_max_consecutive": _max_consecutive([str(item.get("market_context_source") or "UNAVAILABLE") == "UNAVAILABLE" for item in samples]),
        "market_context_fallback_rate": _safe_rate(fallback_count, source_total),
        "subscription_coverage": _ratio(active, selected),
        "fresh_realtime_coverage": _ratio(fresh_count, eval_count),
        "selected_subscription_count": selected,
        "active_subscription_count": active,
        "evaluation_eligible_count": eval_count,
        "fresh_realtime_ready_count": fresh_count,
        "readiness_wait_count": sum(1 for item in readiness if not item.get("readiness_ready")),
        "tr_backfill_only_count": sum(1 for item in readiness if str(item.get("latest_tick_source") or "").upper() == "TR_BACKFILL"),
        "coverage_denominator_candidate_instance_count": len(current_eval_instances),
        "order_counts": order_counts,
        "snapshot_integrity": snapshot_integrity,
        "health_sample_count": len(samples),
    }


def _current_eval_instances(funnel_report: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for episode in list(funnel_report.get("episodes") or []):
        reached = set(episode.get("reached_stages") or [])
        ci = str(episode.get("candidate_instance_id") or "")
        if ci and "EVALUATION_ELIGIBLE" in reached:
            result.add(ci)
    return result


def _baseline_check(baseline: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    status = "PASS"
    critical = False
    required = {
        "enabled": True,
        "status": "FROZEN",
        "baseline_id": "leader_first_pullback_v1",
        "baseline_version": "1.0.0",
        "drift_status": "CLEAN",
        "config_snapshot_completeness": "COMPLETE",
        "order_intent_allowed": False,
        "live_order_allowed": False,
        "strategy_mutation_allowed": False,
    }
    for key, expected in required.items():
        value = baseline.get(key)
        if key == "baseline_version":
            value = baseline.get("baseline_version") or baseline.get("version")
        if value != expected:
            status = "FAIL"
            critical = True
            reasons.append(f"BASELINE_{key.upper()}_MISMATCH")
    if not baseline.get("config_hash"):
        status = "FAIL"
        critical = True
        reasons.append("BASELINE_CONFIG_HASH_MISSING")
    if not baseline.get("git_sha") or str(baseline.get("git_sha")) == "UNKNOWN":
        if status != "FAIL":
            status = "WARN"
        reasons.append("BASELINE_GIT_SHA_UNKNOWN")
    if baseline.get("git_dirty_or_unknown"):
        if status != "FAIL":
            status = "WARN"
        reasons.append("BASELINE_GIT_DIRTY_OR_UNKNOWN")
    return _check("BASELINE_INTEGRITY", status, critical=critical, reason_codes=reasons, score_weight=20)


def _runtime_check(runtime: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    status = "PASS"
    critical = False
    if str(runtime.get("runtime_profile") or RUNTIME_PROFILE) != RUNTIME_PROFILE:
        status = "FAIL"
        critical = True
        reasons.append("RUNTIME_PROFILE_MISMATCH")
    pipelines = dict(runtime.get("pipeline_status") or {})
    for name in ("market_regime", "theme_board", "strategy_context", "setup_router_v3"):
        if name in pipelines and not pipelines.get(name):
            status = "WARN" if status != "FAIL" else status
            reasons.append(f"PIPELINE_{name.upper()}_DISABLED")
    if any(str(item.get("status") or "").upper() == "ERROR" for item in dict(runtime).values() if isinstance(item, Mapping)):
        status = "FAIL"
        critical = True
        reasons.append("RUNTIME_PIPELINE_ERROR")
    return _check("RUNTIME_INTEGRITY", status, critical=critical, reason_codes=reasons, score_weight=15)


def _market_context_check(evidence: Mapping[str, Any], config: TradingDayQualificationConfig) -> dict[str, Any]:
    reasons: list[str] = []
    status = "PASS"
    critical = False
    unavailable_rate = evidence.get("market_context_unavailable_rate")
    fallback_rate = evidence.get("market_context_fallback_rate")
    consecutive = _int(evidence.get("market_context_unavailable_max_consecutive"), 0)
    if unavailable_rate is not None and unavailable_rate > config.market_context_unavailable_max_rate:
        status = "FAIL"
        critical = True
        reasons.append("MARKET_CONTEXT_UNAVAILABLE_RATE_INVALID")
    elif unavailable_rate and unavailable_rate > 0:
        status = "WARN"
        reasons.append("MARKET_CONTEXT_UNAVAILABLE")
    if consecutive >= config.market_context_unavailable_max_consecutive:
        status = "FAIL"
        critical = True
        reasons.append("MARKET_CONTEXT_UNAVAILABLE_CONSECUTIVE_INVALID")
    if fallback_rate is not None and fallback_rate > config.fallback_invalid_rate:
        status = "FAIL"
        critical = True
        reasons.append("MARKET_CONTEXT_FALLBACK_RATE_INVALID")
    elif fallback_rate is not None and fallback_rate > config.fallback_warn_rate and status != "FAIL":
        status = "WARN"
        reasons.append("MARKET_CONTEXT_FALLBACK_RATE_WARN")
    return _check("MARKET_CONTEXT_INTEGRITY", status, critical=critical, reason_codes=reasons, score_weight=15)


def _realtime_check(evidence: Mapping[str, Any], config: TradingDayQualificationConfig) -> dict[str, Any]:
    reasons: list[str] = []
    status = "PASS"
    critical = False
    subscription = evidence.get("subscription_coverage")
    fresh = evidence.get("fresh_realtime_coverage")
    for label, coverage, warn, invalid in (
        ("SUBSCRIPTION", subscription, config.subscription_warn_coverage, config.subscription_invalid_coverage),
        ("FRESH_DATA", fresh, config.fresh_data_warn_coverage, config.fresh_data_invalid_coverage),
    ):
        if coverage is None:
            continue
        pct = float(coverage.get("ratio") if isinstance(coverage, Mapping) else coverage)
        if pct < invalid:
            status = "FAIL"
            critical = True
            reasons.append(f"{label}_COVERAGE_INVALID")
        elif pct < warn and status != "FAIL":
            status = "WARN"
            reasons.append(f"{label}_COVERAGE_WARN")
    return _check("REALTIME_DATA_INTEGRITY", status, critical=critical, reason_codes=reasons, score_weight=15)


def _candidate_attribution_check(funnel: Mapping[str, Any]) -> dict[str, Any]:
    total = _int(funnel.get("candidate_episode_count"), 0)
    low = _int(funnel.get("low_confidence_attribution_count"), 0)
    status = "PASS"
    critical = False
    reasons: list[str] = []
    if low:
        status = "WARN"
        reasons.append("LOW_CONFIDENCE_ATTRIBUTION_PRESENT")
    if total and low / total > 0.10:
        status = "FAIL"
        critical = True
        reasons.append("LOW_CONFIDENCE_ATTRIBUTION_RATE_INVALID")
    if any(item.get("type") == "IDENTITY_CONFLICT" for item in list(funnel.get("invariant_violations") or [])):
        status = "FAIL"
        critical = True
        reasons.append("CANDIDATE_IDENTITY_CONFLICT")
    return _check("CANDIDATE_ATTRIBUTION_INTEGRITY", status, critical=critical, reason_codes=reasons, score_weight=10)


def _snapshot_check(evidence: Mapping[str, Any]) -> dict[str, Any]:
    integrity = dict(evidence.get("snapshot_integrity") or {})
    reasons = list(integrity.get("reason_codes") or [])
    status = "FAIL" if integrity.get("critical") else "WARN" if reasons else "PASS"
    return _check("SNAPSHOT_INTEGRITY", status, critical=bool(integrity.get("critical")), reason_codes=reasons, score_weight=10)


def _order_safety_check(evidence: Mapping[str, Any]) -> dict[str, Any]:
    counts = dict(evidence.get("order_counts") or {})
    non_zero = [key for key, value in counts.items() if int(value or 0) > 0]
    return _check(
        "ORDER_SAFETY_INTEGRITY",
        "FAIL" if non_zero else "PASS",
        critical=bool(non_zero),
        reason_codes=[f"OBSERVE_ORDER_ACTIVITY_{key.upper()}" for key in non_zero],
        score_weight=10,
        evidence={"counts": counts},
    )


def _funnel_integrity_check(funnel: Mapping[str, Any]) -> dict[str, Any]:
    total = _int(funnel.get("invariant_violation_count"), 0)
    explicit_critical = "critical_invariant_violation_count" in funnel
    count = _int(funnel.get("critical_invariant_violation_count"), 0) if explicit_critical else total
    warning_count = _int(funnel.get("warning_invariant_violation_count"), 0)
    return _check(
        "FUNNEL_INTEGRITY",
        "FAIL" if count else "PASS",
        critical=bool(count),
        reason_codes=["FUNNEL_INVARIANT_VIOLATION"] if count else [],
        score_weight=5,
        evidence={
            "invariant_violation_count": total,
            "critical_invariant_violation_count": count,
            "warning_invariant_violation_count": warning_count,
        },
    )


def _qualification_status(checks: Iterable[Mapping[str, Any]], *, report_state: str, evidence: Mapping[str, Any]) -> str:
    checks = list(checks)
    if not evidence.get("runtime_cycle_count") and not evidence.get("candidate_episode_count") and not evidence.get("health_sample_count"):
        return "NO_SESSION" if report_state == "FINAL" else "COLLECTING"
    if any(item.get("status") == "FAIL" and item.get("critical") for item in checks):
        return "INVALID"
    if str(report_state).upper() != "FINAL":
        return "COLLECTING"
    if any(item.get("status") in {"WARN", "FAIL"} for item in checks):
        return "DEGRADED"
    return "VALID"


def _qualification_score(checks: Iterable[Mapping[str, Any]]) -> int:
    score = 0.0
    for check in checks:
        weight = float(check.get("score_weight") or 0)
        status = str(check.get("status") or "")
        if status == "PASS":
            score += weight
        elif status == "WARN":
            score += weight * 0.5
    return max(0, min(100, int(round(score))))


def _resolve_baseline(db: Any, *, trade_date: str, runtime_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    baseline = dict(runtime_snapshot.get("strategy_baseline") or {})
    if baseline:
        return baseline
    loader = getattr(db, "list_strategy_baseline_sessions", None)
    if callable(loader):
        sessions = list(loader(trade_date=trade_date, limit=1) or [])
        if sessions:
            payload = dict(sessions[0].get("payload") or sessions[0])
            payload.setdefault("enabled", True)
            payload.setdefault("status", payload.get("baseline_status") or "FROZEN")
            payload.setdefault("version", payload.get("baseline_version"))
            return payload
    return {}


def _order_counts(db: Any, trade_date: str) -> dict[str, int]:
    return {
        "runtime_order_intents": _count(db, "runtime_order_intents", trade_date=trade_date),
        "managed_order_intents": _count(db, "managed_order_intents", trade_date=trade_date),
        "managed_orders": _count(db, "managed_orders", trade_date=trade_date),
        "live_sim_orders": _count(db, "live_sim_orders", trade_date=trade_date),
        "send_order": _gateway_count(db, "send_order", trade_date=trade_date),
        "cancel_order": _gateway_count(db, "cancel_order", trade_date=trade_date),
        "modify_order": _gateway_count(db, "modify_order", trade_date=trade_date),
        "broker_accepted": _count_where(db, "live_sim_orders", "accepted_at != ''", trade_date=trade_date)
        + _count_where(db, "managed_orders", "acked_at != ''", trade_date=trade_date),
        "partial_filled": _count_where(db, "live_sim_orders", "first_fill_at != ''", trade_date=trade_date)
        + _count_where(db, "managed_orders", "status = 'PARTIALLY_FILLED'", trade_date=trade_date),
        "filled": _count_where(db, "live_sim_orders", "last_fill_at != ''", trade_date=trade_date)
        + _count_where(db, "managed_orders", "status = 'FILLED'", trade_date=trade_date),
    }


def _no_trade_classification(
    episodes: Iterable[Mapping[str, Any]],
    *,
    order_counts: Mapping[str, Any],
    qualification_status: str,
) -> dict[str, Any]:
    episodes = list(episodes)
    order_activity = {key: int(value or 0) for key, value in dict(order_counts or {}).items()}
    order_nonzero = sum(order_activity.values())
    champion_valid = sum(1 for item in episodes if "CHAMPION_VALID_OBSERVE" in set(item.get("reached_stages") or []))
    champion_matched = sum(1 for item in episodes if "CHAMPION_MATCHED" in set(item.get("reached_stages") or []))
    context_eligible = sum(1 for item in episodes if "CHAMPION_CONTEXT_ELIGIBLE" in set(item.get("reached_stages") or []))
    eval_eligible = sum(1 for item in episodes if "EVALUATION_ELIGIBLE" in set(item.get("reached_stages") or []))
    highest = max([_int(item.get("max_stage_ordinal"), -1) for item in episodes], default=-1)
    if qualification_status == "INVALID":
        classification = "DATA_INVALID_NO_TRADE"
        primary = "qualification invalid"
    elif order_nonzero:
        classification = "NOT_A_NO_TRADE_DAY"
        primary = "order activity observed"
    elif champion_valid:
        classification = "EXPECTED_OBSERVE_ONLY"
        primary = "Champion VALID_OBSERVE reached under OBSERVE_ONLY baseline"
    elif not episodes:
        classification = "DISCOVERY_COVERAGE_UNKNOWN"
        primary = "no candidate episode"
    elif not eval_eligible:
        classification = "CANDIDATE_PIPELINE_STALL"
        primary = "candidate did not reach evaluation eligibility"
    elif champion_matched and not context_eligible:
        classification = "CHAMPION_CONTEXT_BLOCKED"
        primary = "Champion matched but context was not eligible"
    else:
        classification = "HEALTHY_NO_CHAMPION_SIGNAL"
        primary = "Champion pattern not matched"
    return {
        "schema_version": NO_TRADE_CLASSIFICATION_SCHEMA_VERSION,
        "classification": classification,
        "confidence": "HIGH" if classification in {"EXPECTED_OBSERVE_ONLY", "NOT_A_NO_TRADE_DAY", "DATA_INVALID_NO_TRADE"} else "MEDIUM",
        "provisional": classification in {"HEALTHY_NO_CHAMPION_SIGNAL", "DISCOVERY_COVERAGE_UNKNOWN", "CHAMPION_CONTEXT_BLOCKED"},
        "order_expected": False,
        "actual_order_activity": order_activity,
        "highest_funnel_stage": STAGE_ORDER[highest] if highest >= 0 else "",
        "primary_reason": primary,
        "reason_codes": [classification],
        "requires_opportunity_benchmark": classification in {"HEALTHY_NO_CHAMPION_SIGNAL", "DISCOVERY_COVERAGE_UNKNOWN"},
        "requires_outcome_labels": classification == "CHAMPION_CONTEXT_BLOCKED",
        "operator_message_ko": _no_trade_message(classification),
    }


def _candidate_funnel_runtime_section(report: Mapping[str, Any]) -> dict[str, Any]:
    if not report:
        return _disabled_candidate_funnel_section(datetime.now())
    stages = {item.get("stage"): item for item in list(report.get("stages") or [])}
    return {
        "enabled": True,
        "status": "OK",
        "report_id": report.get("report_id", ""),
        "trade_date": report.get("trade_date", ""),
        "report_state": report.get("report_state", ""),
        "candidate_episode_count": _int(report.get("candidate_episode_count"), 0),
        "strict_attribution_count": _int(report.get("strict_attribution_count"), 0),
        "low_confidence_attribution_count": _int(report.get("low_confidence_attribution_count"), 0),
        "evaluation_eligible_count": _int(dict(stages.get("EVALUATION_ELIGIBLE") or {}).get("strict_reached_count"), 0),
        "active_subscription_count": _int(dict(stages.get("REALTIME_SUBSCRIPTION_ACTIVE") or {}).get("strict_reached_count"), 0),
        "fresh_realtime_ready_count": _int(dict(stages.get("FRESH_REALTIME_READY") or {}).get("strict_reached_count"), 0),
        "champion_forming_count": _int(dict(stages.get("CHAMPION_FORMING") or {}).get("strict_reached_count"), 0),
        "champion_matched_count": _int(dict(stages.get("CHAMPION_MATCHED") or {}).get("strict_reached_count"), 0),
        "champion_context_eligible_count": _int(dict(stages.get("CHAMPION_CONTEXT_ELIGIBLE") or {}).get("strict_reached_count"), 0),
        "champion_valid_observe_count": _int(dict(stages.get("CHAMPION_VALID_OBSERVE") or {}).get("strict_reached_count"), 0),
        "highest_applicable_stage": _highest_applicable_stage(report),
        "top_stop_reasons": list(report.get("top_stop_reasons") or [])[:5],
        "invariant_violation_count": _int(report.get("invariant_violation_count"), 0),
        "critical_invariant_violation_count": _int(report.get("critical_invariant_violation_count"), 0),
        "warning_invariant_violation_count": _int(report.get("warning_invariant_violation_count"), 0),
        "no_trade_classification": dict(report.get("no_trade_classification") or {}),
        "checked_at": report.get("generated_at", ""),
        "build_duration_ms": report.get("build_duration_ms", 0),
    }


def _qualification_runtime_section(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "enabled": True,
        "status": "OK",
        "report_id": report.get("report_id", ""),
        "trade_date": report.get("trade_date", ""),
        "report_state": report.get("report_state", ""),
        "qualification_status": report.get("qualification_status", "COLLECTING"),
        "qualification_score": _int(report.get("qualification_score"), 0),
        "strict_sample_eligible": bool(report.get("strict_sample_eligible")),
        "top_reason_codes": list(report.get("reason_codes") or [])[:3],
        "no_trade_classification": dict(report.get("no_trade_classification") or {}),
        "baseline_id": report.get("baseline_id", ""),
        "baseline_version": report.get("baseline_version", ""),
        "config_hash": report.get("config_hash", ""),
        "git_sha": report.get("git_sha", ""),
        "checked_at": report.get("generated_at", ""),
        "build_duration_ms": report.get("build_duration_ms", 0),
    }


def _disabled_candidate_funnel_section(now: datetime) -> dict[str, Any]:
    return {
        "enabled": False,
        "status": "DISABLED",
        "candidate_episode_count": 0,
        "evaluation_eligible_count": 0,
        "champion_forming_count": 0,
        "champion_matched_count": 0,
        "champion_context_eligible_count": 0,
        "champion_valid_observe_count": 0,
        "highest_applicable_stage": "",
        "top_stop_reasons": [],
        "invariant_violation_count": 0,
        "checked_at": now.replace(microsecond=0).isoformat(),
    }


def _disabled_qualification_section(now: datetime) -> dict[str, Any]:
    return {
        "enabled": False,
        "status": "DISABLED",
        "report_state": "LIVE_PREVIEW",
        "qualification_status": "COLLECTING",
        "qualification_score": 0,
        "strict_sample_eligible": False,
        "top_reason_codes": [],
        "checked_at": now.replace(microsecond=0).isoformat(),
    }


def _mark_order_stages(
    episodes: Mapping[str, dict[str, Any]],
    rows: Iterable[Mapping[str, Any]],
    stage: str,
    timestamp_key: str,
    *,
    status_values: set[str] | None = None,
) -> None:
    for row in rows:
        status = str(row.get("status") or row.get("order_status") or "")
        if status_values and status not in status_values:
            continue
        ci = str(row.get("candidate_instance_id") or "")
        if not ci:
            metadata = _json(row.get("metadata_json"), {})
            ci = str(metadata.get("candidate_instance_id") or "")
        episode = episodes.get(ci)
        if episode is not None:
            _reach(episode, stage, row.get(timestamp_key) or row.get("created_at") or row.get("updated_at"))


def _fresh_readiness(row: Mapping[str, Any]) -> bool:
    if not row.get("subscription_active"):
        return False
    if str(row.get("latest_tick_source") or "").upper() == "TR_BACKFILL":
        return False
    if not row.get("post_subscription_tick_verified"):
        return False
    if row.get("candle_ready") is False:
        return False
    if row.get("readiness_ready") is False:
        return False
    return bool(row.get("latest_tick_at") or row.get("core_tick_at"))


def _observation_fresh_readiness(row: Mapping[str, Any]) -> bool:
    if not row.get("post_subscription_tick_verified"):
        return False
    payload = _json(row.get("payload_json"), {})
    evidence = dict(row.get("evidence") or payload.get("evidence") or {})
    data = dict(evidence.get("data") or {})
    source = str(data.get("price_source") or row.get("latest_tick_source") or "").upper()
    if source == "TR_BACKFILL":
        return False
    if data.get("realtime_tick_fresh") is False:
        return False
    return bool(data.get("tick_at") or _float(row.get("current_price")) > 0)


_REALTIME_WAIT_STATUSES = {
    "WAIT_SUBSCRIPTION_NOT_ACTIVE",
    "WAIT_SUBSCRIPTION_BUDGET",
    "WAIT_REGISTER_COMMAND",
    "WAIT_REGISTER_ACK",
    "WAIT_FIRST_TICK",
    "WAIT_POST_SUBSCRIPTION_TICK",
    "WAIT_REALTIME_TICK_STALE",
}

_REALTIME_WAIT_REASONS = {
    "SUBSCRIPTION_NOT_REQUESTED",
    "SUBSCRIPTION_BUDGET_DEFERRED",
    "REGISTER_COMMAND_ENQUEUED",
    "REGISTER_COMMAND_DISPATCHED",
    "SUBSCRIPTION_NOT_ACTIVE",
    "ACKED_WAIT_FIRST_TICK",
    "ACTIVE_SUBSCRIPTION_NO_POST_ACTIVE_TICK",
    "LATEST_TICK_STALE",
}


def _reconcile_readiness_rows_with_active_lifecycle(
    readiness_rows: Iterable[Mapping[str, Any]],
    lifecycle_rows: Iterable[Mapping[str, Any]],
    episodes: Iterable[Mapping[str, Any]],
    *,
    as_of: datetime,
) -> list[dict[str, Any]]:
    rows_by_ci = {str(row.get("candidate_instance_id") or ""): dict(row or {}) for row in readiness_rows if str(row.get("candidate_instance_id") or "")}
    current_eval_by_code: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for episode in episodes:
        ci = str(episode.get("candidate_instance_id") or "")
        code = normalize_code(episode.get("code"))
        if ci and code and "EVALUATION_ELIGIBLE" in set(episode.get("reached_stages") or []):
            current_eval_by_code[code].append(episode)

    for lifecycle in lifecycle_rows:
        if not _lifecycle_active_fresh(lifecycle):
            continue
        code = normalize_code(lifecycle.get("code"))
        current = current_eval_by_code.get(code) or []
        if len(current) != 1:
            continue
        tick_at = str(lifecycle.get("last_tick_at_utc") or "")
        active_since = _lifecycle_active_since(lifecycle)
        if not _timestamp_at_or_after(tick_at, active_since):
            continue
        episode = current[0]
        ci = str(episode.get("candidate_instance_id") or "")
        existing = dict(rows_by_ci.get(ci) or {})
        rows_by_ci[ci] = _reconciled_active_fresh_readiness(existing, lifecycle, episode, as_of=as_of)
    return list(rows_by_ci.values())


def _lifecycle_active_fresh(row: Mapping[str, Any]) -> bool:
    if str(row.get("lifecycle_state") or "").upper() != "ACTIVE_FRESH":
        return False
    if row.get("released") or row.get("stale") or row.get("failed"):
        return False
    return bool(row.get("transport_active") and row.get("first_tick_verified") and row.get("decision_fresh"))


def _lifecycle_active_since(row: Mapping[str, Any]) -> str:
    return str(
        row.get("registration_ack_baseline_at_utc")
        or row.get("command_acked_at_utc")
        or row.get("target_selected_at_utc")
        or row.get("requested_at_utc")
        or ""
    )


def _timestamp_at_or_after(left: Any, right: Any) -> bool:
    left_dt = _as_datetime(left)
    if left_dt is None:
        return False
    right_dt = _as_datetime(right)
    return right_dt is None or left_dt >= right_dt


def _reconciled_active_fresh_readiness(
    existing: Mapping[str, Any],
    lifecycle: Mapping[str, Any],
    episode: Mapping[str, Any],
    *,
    as_of: datetime,
) -> dict[str, Any]:
    row = dict(existing or {})
    had_existing = bool(existing)
    active_since = _lifecycle_active_since(lifecycle)
    tick_at = str(lifecycle.get("last_tick_at_utc") or "")
    reason_codes = [
        str(reason)
        for reason in list(row.get("reason_codes") or [])
        if str(reason) and str(reason) not in _REALTIME_WAIT_REASONS
    ]
    previous_status = str(row.get("readiness_status") or "")
    realtime_blocked = previous_status in _REALTIME_WAIT_STATUSES or not previous_status
    readiness_ready = bool(row.get("readiness_ready"))
    non_realtime_status, non_realtime_reason = _non_realtime_readiness_blocker(row)
    if non_realtime_status:
        readiness_ready = False
        readiness_status = non_realtime_status
        reason_codes = _dedupe([*reason_codes, non_realtime_reason])
    elif realtime_blocked and not reason_codes:
        readiness_ready = True
        readiness_status = "READY"
    else:
        readiness_status = previous_status or ("READY" if readiness_ready else "WAIT_POST_SUBSCRIPTION_TICK")
    informational = _dedupe(
        [
            *[str(reason) for reason in list(row.get("informational_reason_codes") or []) if str(reason)],
            "LIFECYCLE_ACTIVE_FRESH_CURRENT_CANDIDATE_BIND",
        ]
    )
    row.update(
        {
            "trade_date": row.get("trade_date") or episode.get("trade_date") or lifecycle.get("trade_date") or "",
            "router_version": row.get("router_version") or ROUTER_VERSION,
            "candidate_instance_id": episode.get("candidate_instance_id") or row.get("candidate_instance_id") or "",
            "candidate_id": row.get("candidate_id", episode.get("candidate_id")),
            "candidate_generation_seq": row.get("candidate_generation_seq", episode.get("candidate_generation_seq")),
            "code": normalize_code(episode.get("code") or lifecycle.get("code")),
            "name": row.get("name") or episode.get("name") or "",
            "readiness_status": readiness_status,
            "readiness_ready": readiness_ready,
            "readiness_schema_version": row.get("readiness_schema_version") or "setup_router_readiness.v3",
            "readiness_fingerprint": _reconciled_readiness_fingerprint(
                episode=episode,
                lifecycle=lifecycle,
                readiness_status=readiness_status,
                readiness_ready=readiness_ready,
            ),
            "coverage_type": row.get("coverage_type") or "CANDIDATE",
            "subscription_requested": True,
            "subscription_target_selected": True,
            "subscription_selected": True,
            "subscription_active": True,
            "subscription_sources": list(row.get("subscription_sources") or ["realtime_subscription_lifecycle"]),
            "subscription_primary_source": row.get("subscription_primary_source") or "realtime_subscription_lifecycle",
            "subscription_budget_deferred": False,
            "subscription_screen_no": row.get("subscription_screen_no") or lifecycle.get("screen_no") or "",
            "subscription_generation": _int(lifecycle.get("subscription_generation"), _int(row.get("subscription_generation"), 0)),
            "subscription_active_since": row.get("subscription_active_since") or active_since,
            "latest_tick_at": tick_at,
            "latest_tick_age_sec": _float(lifecycle.get("latest_tick_age_sec"), _float(row.get("latest_tick_age_sec"), 999999.0)),
            "latest_tick_source": "REALTIME",
            "post_subscription_tick_verified": True,
            "gateway_tick_at": row.get("gateway_tick_at") or tick_at,
            "core_tick_at": tick_at,
            "setup_feature_tick_at": tick_at,
            "market_context_fresh": bool(row.get("market_context_fresh", True)),
            "theme_context_fresh": bool(row.get("theme_context_fresh", True)),
            "candle_ready": bool(row.get("candle_ready", True)),
            "calculated_at": max(str(row.get("calculated_at") or ""), as_of.replace(microsecond=0).isoformat()),
            "updated_at": max(str(row.get("updated_at") or ""), str(lifecycle.get("updated_at_utc") or lifecycle.get("updated_at") or "")),
            "reason_codes": _dedupe(reason_codes),
            "informational_reason_codes": informational,
            "readiness_reconcile_source": "realtime_subscription_lifecycle_latest",
            "readiness_reconcile_status": "ACTIVE_FRESH_BOUND_TO_CURRENT_CANDIDATE",
            "readiness_reconcile_existing_row": had_existing,
        }
    )
    return row


def _non_realtime_readiness_blocker(row: Mapping[str, Any]) -> tuple[str, str]:
    if row.get("candle_ready") is False:
        return "WAIT_CANDLE_WARMUP", "COMPLETED_1M_CANDLES_INSUFFICIENT"
    if row.get("market_context_fresh") is False:
        return "WAIT_MARKET_CONTEXT", "MARKET_CONTEXT_NOT_FRESH"
    if row.get("theme_context_fresh") is False:
        return "WAIT_THEME_SIGNAL_STALE", "SIGNAL_STALE"
    return "", ""


def _reconciled_readiness_fingerprint(
    *,
    episode: Mapping[str, Any],
    lifecycle: Mapping[str, Any],
    readiness_status: str,
    readiness_ready: bool,
) -> str:
    material = {
        "candidate_instance_id": episode.get("candidate_instance_id") or "",
        "code": normalize_code(episode.get("code") or lifecycle.get("code")),
        "readiness_status": readiness_status,
        "readiness_ready": bool(readiness_ready),
        "subscription_generation": _int(lifecycle.get("subscription_generation"), 0),
        "last_tick_at_utc": lifecycle.get("last_tick_at_utc") or "",
        "updated_at_utc": lifecycle.get("updated_at_utc") or lifecycle.get("updated_at") or "",
    }
    return hashlib.sha1(json.dumps(material, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _persist_reconciled_readiness_rows(db: Any, readiness_rows: Iterable[Mapping[str, Any]]) -> int:
    saver = getattr(db, "save_setup_router_readiness_snapshots", None)
    if not callable(saver):
        return 0
    rows = [
        dict(row)
        for row in readiness_rows
        if str(row.get("readiness_reconcile_status") or "") == "ACTIVE_FRESH_BOUND_TO_CURRENT_CANDIDATE"
    ]
    if not rows:
        return 0
    try:
        return int(saver(rows) or 0)
    except Exception:
        return 0


def _order_policy(baseline: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "order_intent_allowed": bool(baseline.get("order_intent_allowed")),
        "live_order_allowed": bool(baseline.get("live_order_allowed")),
    }


def _applicable_stages(order_policy: Mapping[str, Any]) -> set[str]:
    stages = set(STAGE_ORDER)
    if not order_policy.get("order_intent_allowed") and not order_policy.get("live_order_allowed"):
        stages -= ORDER_STAGES
    return stages


def _stop_reason(episode: Mapping[str, Any], *, order_policy: Mapping[str, Any]) -> str:
    reached = set(episode.get("reached_stages") or [])
    current = str(episode.get("current_stage") or "")
    if current == "CHAMPION_VALID_OBSERVE" and not (order_policy.get("order_intent_allowed") or order_policy.get("live_order_allowed")):
        return "OBSERVE_ONLY_EXPECTED_STOP"
    if "SOURCE_DETECTED" not in reached:
        return "SOURCE_REMOVED"
    if "ACTIVE_SOURCE_PRESENT" not in reached:
        return "NO_ACTIVE_SOURCE"
    if "HYDRATION_COMPLETE" not in reached:
        return _family_from_reasons(episode, default="HYDRATION_PENDING")
    if "REALTIME_SUBSCRIPTION_ACTIVE" not in reached:
        return "SUBSCRIPTION_NOT_ACTIVE"
    if "FRESH_REALTIME_READY" not in reached:
        return _family_from_reasons(episode, default="REALTIME_TICK_MISSING")
    if "STRATEGY_CONTEXT_READY" not in reached:
        return "STRATEGY_CONTEXT_MISSING"
    if "ENTRY_EVALUATED" not in reached:
        return "ENTRY_DATA_WAIT"
    if "CHAMPION_FORMING" in reached and "CHAMPION_MATCHED" not in reached:
        return "CHAMPION_FORMING"
    if "CHAMPION_MATCHED" not in reached:
        return "CHAMPION_NOT_SEEN"
    if "CHAMPION_CONTEXT_ELIGIBLE" not in reached:
        return "CHAMPION_CONTEXT_BLOCKED"
    return "UNKNOWN"


def _family_from_reasons(episode: Mapping[str, Any], *, default: str) -> str:
    reasons = {str(item).upper() for item in list(episode.get("reason_codes") or [])}
    if "HYDRATION_RETRY_WAIT" in reasons:
        return "HYDRATION_RETRY_WAIT"
    if "HYDRATION_FAILED" in reasons:
        return "HYDRATION_FAILED"
    if "TR_BACKFILL_ONLY" in reasons:
        return "TR_BACKFILL_ONLY"
    if any("CANDLE" in reason for reason in reasons):
        return "CANDLE_NOT_READY"
    if any("STALE" in reason for reason in reasons):
        return "REALTIME_TICK_STALE"
    return default


def _primary_reason(episode: Mapping[str, Any]) -> str:
    reasons = [str(item) for item in list(episode.get("reason_codes") or []) if str(item)]
    return reasons[0] if reasons else str(episode.get("stop_reason_family") or "UNKNOWN")


def _drop_offs(stages: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in stages:
        if item.get("applicable") and int(item.get("stopped_here_count") or 0) > 0:
            rows.append({"stage": item.get("stage"), "count": item.get("stopped_here_count"), "top_reason_codes": item.get("top_reason_codes")})
    return sorted(rows, key=lambda item: int(item.get("count") or 0), reverse=True)[:10]


def _highest_applicable_stage(report: Mapping[str, Any]) -> str:
    stages = [item for item in list(report.get("stages") or []) if item.get("applicable") and int(item.get("strict_reached_count") or 0) > 0]
    if not stages:
        return ""
    return str(max(stages, key=lambda item: int(item.get("ordinal") or 0)).get("stage") or "")


def _stage_count(report: Mapping[str, Any], stage: str) -> int:
    for item in list(report.get("stages") or []):
        if item.get("stage") == stage:
            return _int(item.get("strict_reached_count"), 0)
    return 0


def _snapshot_integrity(samples: Iterable[Mapping[str, Any]], db: Any) -> dict[str, Any]:
    reasons: list[str] = []
    critical = False
    previous_generation: int | None = None
    checksum_by_generation: dict[int, str] = {}
    for sample in sorted(samples, key=lambda item: str(item.get("bucket_at") or "")):
        namespace = str(sample.get("dashboard_namespace") or "")
        generation = _int(sample.get("dashboard_generation"), 0)
        checksum = str(sample.get("dashboard_checksum") or "")
        if namespace and namespace != "reboot_v2.main":
            critical = True
            reasons.append("SNAPSHOT_NAMESPACE_MISMATCH")
        if previous_generation is not None and generation and generation < previous_generation:
            critical = True
            reasons.append("SNAPSHOT_GENERATION_REGRESSION")
        previous_generation = generation or previous_generation
        if generation and checksum:
            previous = checksum_by_generation.get(generation)
            if previous and previous != checksum:
                critical = True
                reasons.append("SNAPSHOT_CHECKSUM_CONFLICT")
            checksum_by_generation[generation] = checksum
    try:
        row = db.conn.execute("SELECT * FROM dashboard_read_models WHERE view_name = ?", ("reboot_v2.main",)).fetchone()
    except Exception:
        row = None
    if row is not None:
        payload = _json(row["snapshot_json"], {})
        namespace = str(payload.get("snapshot_namespace") or dict(payload.get("read_model") or {}).get("snapshot_namespace") or "")
        if namespace and namespace != "reboot_v2.main":
            critical = True
            reasons.append("SNAPSHOT_NAMESPACE_MISMATCH")
    return {"critical": critical, "reason_codes": _dedupe(reasons)}


def _check(
    name: str,
    status: str,
    *,
    critical: bool,
    reason_codes: Iterable[str],
    score_weight: int,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "check_name": name,
        "status": status,
        "critical": bool(critical),
        "reason_codes": _dedupe(reason_codes),
        "score_weight": score_weight,
        "evidence": dict(evidence or {}),
    }


def _evaluated_window(trade_date: str, evidence: Mapping[str, Any], as_of: datetime) -> dict[str, Any]:
    return {
        "trade_date": trade_date,
        "start_at": f"{trade_date}T09:00:00",
        "end_at": as_of.isoformat(),
        "source_cutoff_at": as_of.isoformat(),
        "health_sample_count": _int(evidence.get("health_sample_count"), 0),
    }


def _session_qualifications(funnel: Mapping[str, Any]) -> dict[str, Any]:
    episodes = list(funnel.get("episodes") or [])
    buckets: dict[str, dict[str, Any]] = {}
    for name in ("OPENING", "MORNING", "MIDDAY", "AFTERNOON"):
        buckets[name] = {
            "qualification_status": "COLLECTING",
            "candidate_count": 0,
            "evaluation_eligible_count": 0,
            "champion_forming_count": 0,
            "champion_matched_count": 0,
            "champion_valid_observe_count": 0,
        }
    for episode in episodes:
        bucket = _session_bucket(str(episode.get("first_seen_at") or ""))
        row = buckets.setdefault(bucket, {"qualification_status": "COLLECTING"})
        row["candidate_count"] = _int(row.get("candidate_count"), 0) + 1
        reached = set(episode.get("reached_stages") or [])
        if "EVALUATION_ELIGIBLE" in reached:
            row["evaluation_eligible_count"] = _int(row.get("evaluation_eligible_count"), 0) + 1
        if "CHAMPION_FORMING" in reached:
            row["champion_forming_count"] = _int(row.get("champion_forming_count"), 0) + 1
        if "CHAMPION_MATCHED" in reached:
            row["champion_matched_count"] = _int(row.get("champion_matched_count"), 0) + 1
        if "CHAMPION_VALID_OBSERVE" in reached:
            row["champion_valid_observe_count"] = _int(row.get("champion_valid_observe_count"), 0) + 1
    return buckets


def _champion_session_eligibility(funnel: Mapping[str, Any], strict_sample_eligible: bool) -> dict[str, bool]:
    sessions = _session_qualifications(funnel)
    return {
        name: bool(strict_sample_eligible and _int(row.get("champion_valid_observe_count"), 0) > 0)
        for name, row in sessions.items()
    }


def _recommended_action(status: str, no_trade: Mapping[str, Any]) -> str:
    if status == "VALID":
        return "성과 표본으로 사용할 수 있습니다."
    if status == "INVALID":
        return "성과 표본에서 제외하고 원인 체크를 확인하세요."
    if status == "DEGRADED":
        return "주의 표본입니다. 주요 경고와 세션별 품질을 확인하세요."
    if status == "NO_SESSION":
        return "거래 세션 데이터가 없습니다."
    classification = str(no_trade.get("classification") or "")
    if classification == "EXPECTED_OBSERVE_ONLY":
        return "주문 미실행은 OBSERVE 전용 기준선에서 정상입니다."
    return "장중 수집 중입니다."


def _no_trade_message(classification: str) -> str:
    return {
        "EXPECTED_OBSERVE_ONLY": "주문 미실행 정상: OBSERVE 전용",
        "HEALTHY_NO_CHAMPION_SIGNAL": "Champion 패턴 미형성",
        "DATA_INVALID_NO_TRADE": "데이터 품질 문제로 분석 제외",
        "CANDIDATE_PIPELINE_STALL": "Candidate 파이프라인 병목",
        "CHAMPION_CONTEXT_BLOCKED": "Champion 일치 후 Context 차단",
        "VALID_OBSERVE_REACHED": "Champion VALID_OBSERVE 도달",
        "ORDER_PIPELINE_NO_TRADE": "주문 파이프라인 미진행",
        "DISCOVERY_COVERAGE_UNKNOWN": "외부 기회 포착 범위 미확인",
        "UNRESOLVED": "무매매 원인 미해결",
        "NOT_A_NO_TRADE_DAY": "주문 활동 존재",
    }.get(classification, "무매매 원인 확인 필요")


def _session_bucket(timestamp: str) -> str:
    try:
        value = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except Exception:
        return "UNKNOWN"
    hhmm = value.hour * 100 + value.minute
    if hhmm < 1000:
        return "OPENING"
    if hhmm < 1130:
        return "MORNING"
    if hhmm < 1300:
        return "MIDDAY"
    return "AFTERNOON"


def _report_id(prefix: str, trade_date: str, report_state: str, as_of: datetime, baseline: Mapping[str, Any]) -> str:
    if str(report_state).upper() == "LIVE_PREVIEW":
        material = "|".join([prefix, trade_date, "LIVE_PREVIEW", str(baseline.get("baseline_id") or ""), str(baseline.get("config_hash") or "")])
    else:
        material = "|".join([prefix, trade_date, str(report_state), as_of.isoformat(), str(baseline.get("config_hash") or "")])
    digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{trade_date}:{str(report_state).lower()}:{digest}"


def _candidate_instance_id(metadata: Mapping[str, Any]) -> str:
    return str(
        metadata.get("candidate_instance_id")
        or dict(metadata.get("candidate_ingestion") or {}).get("candidate_instance_id")
        or dict(metadata.get("candidate_generation") or {}).get("candidate_instance_id")
        or ""
    )


def _generation_seq(metadata: Mapping[str, Any]) -> int:
    return _int(
        metadata.get("candidate_generation_seq")
        or dict(metadata.get("candidate_generation") or {}).get("candidate_generation_seq")
        or dict(metadata.get("candidate_ingestion") or {}).get("candidate_generation_seq"),
        0,
    )


def _baseline_role(setup_type: str) -> str:
    text = str(setup_type or "").upper()
    if text == CHAMPION_SETUP:
        return "CHAMPION"
    if text in CHALLENGER_SETUPS:
        return "CHALLENGER"
    return "OUT_OF_SCOPE"


def _latencies_ms(episodes: Iterable[Mapping[str, Any]], stage: str, previous_stage: str) -> list[float]:
    values: list[float] = []
    if not previous_stage:
        return values
    for item in episodes:
        times = dict(item.get("stage_first_reached_at") or {})
        left = _as_datetime(times.get(previous_stage))
        right = _as_datetime(times.get(stage))
        if left and right and right >= left:
            values.append((right - left).total_seconds() * 1000.0)
    return values


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct))))
    return round(ordered[index], 3)


def _ratio(numerator: int, denominator: int) -> dict[str, Any] | None:
    if denominator <= 0:
        return None
    return {
        "numerator": int(numerator),
        "denominator": int(denominator),
        "ratio": float(numerator) / float(denominator),
        "pct": round(float(numerator) / float(denominator) * 100.0, 4),
    }


def _safe_rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return float(numerator) / float(denominator)


def _counts_with_rates(counts: Mapping[str, int]) -> list[dict[str, Any]]:
    total = sum(int(value or 0) for value in counts.values())
    return [
        {"source": key, "count": int(value or 0), "rate": _safe_rate(int(value or 0), total)}
        for key, value in sorted(counts.items())
    ]


def _top_counts(values: Iterable[str]) -> list[dict[str, Any]]:
    return [{"reason": key, "count": value} for key, value in Counter(str(item or "UNKNOWN") for item in values).most_common(10)]


def _max_consecutive(values: Iterable[bool]) -> int:
    best = 0
    current = 0
    for value in values:
        if value:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _bucket_at(value: datetime, interval_sec: int) -> str:
    interval = max(1, int(interval_sec or 30))
    epoch = int(value.timestamp())
    bucket = epoch - (epoch % interval)
    return datetime.fromtimestamp(bucket).replace(microsecond=0).isoformat()


def _rows(db: Any, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in db.conn.execute(query, params).fetchall()]
    except Exception:
        return []


def _count(db: Any, table: str, *, trade_date: str) -> int:
    return _count_where(db, table, "1=1", trade_date=trade_date)


def _count_where(db: Any, table: str, where: str, *, trade_date: str) -> int:
    try:
        row = db.conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE trade_date = ? AND {where}", (trade_date,)).fetchone()
        return int(row["count"] or 0) if row else 0
    except Exception:
        return 0


def _gateway_count(db: Any, command_type: str, *, trade_date: str) -> int:
    try:
        row = db.conn.execute(
            "SELECT COUNT(*) AS count FROM gateway_commands WHERE trade_date = ? AND command_type = ?",
            (trade_date, command_type),
        ).fetchone()
        return int(row["count"] or 0) if row else 0
    except Exception:
        return 0


def _safe_call(func: Any, **kwargs: Any) -> list[dict[str, Any]]:
    if not callable(func):
        return []
    try:
        return [dict(item or {}) for item in list(func(**kwargs) or [])]
    except Exception:
        return []


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except Exception:
        return default


def _maybe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip().replace(",", ""))
    except Exception:
        return default


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(microsecond=0)
    except Exception:
        return None


def _report_state(value: str) -> str:
    return "FINAL" if str(value or "").upper() == "FINAL" else "LIVE_PREVIEW"


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "")
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


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


def _write_csv(path: Path, rows: list[Mapping[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in keys})


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value


def _candidate_funnel_markdown(report: Mapping[str, Any]) -> str:
    no_trade = dict(report.get("no_trade_classification") or {})
    return "\n".join(
        [
            f"# Candidate Funnel {report.get('trade_date', '')}",
            "",
            f"- 상태: {report.get('report_state', '')}",
            f"- Candidate episode: {report.get('candidate_episode_count', 0)}",
            f"- Strict attribution: {report.get('strict_attribution_count', 0)}",
            f"- Low confidence: {report.get('low_confidence_attribution_count', 0)}",
            f"- 무매매 해석: {no_trade.get('operator_message_ko', '')}",
        ]
    )


def _qualification_markdown(report: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            f"# Trading Day Qualification {report.get('trade_date', '')}",
            "",
            f"- 판정: {report.get('qualification_status', '')}",
            f"- 점수: {report.get('qualification_score', 0)}",
            f"- 엄격 표본 사용 가능: {report.get('strict_sample_eligible', False)}",
            f"- 권장 조치: {report.get('recommended_operator_action_ko', '')}",
        ]
    )


__all__ = [
    "CANDIDATE_FUNNEL_SCHEMA_VERSION",
    "CANDIDATE_FUNNEL_EPISODE_SCHEMA_VERSION",
    "TRADING_DAY_QUALIFICATION_SCHEMA_VERSION",
    "NO_TRADE_CLASSIFICATION_SCHEMA_VERSION",
    "CandidateFunnelConfig",
    "CandidateFunnelService",
    "TradingDayQualificationConfig",
    "TradingDayQualificationService",
    "export_candidate_funnel_report",
    "export_trading_day_qualification_report",
    "save_ops_runtime_health_sample",
]
