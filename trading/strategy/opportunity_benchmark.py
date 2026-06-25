from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from typing import Any, Iterable, Mapping

from trading.strategy.candidates import normalize_code


OPPORTUNITY_BENCHMARK_BATCH_SCHEMA_VERSION = "opportunity_benchmark_batch.v1"
OPPORTUNITY_BENCHMARK_OBSERVATION_SCHEMA_VERSION = "opportunity_benchmark_observation.v1"
OPPORTUNITY_BENCHMARK_EPISODE_SCHEMA_VERSION = "opportunity_benchmark_episode.v1"
OPPORTUNITY_BENCHMARK_CANDIDATE_LINK_SCHEMA_VERSION = "opportunity_benchmark_candidate_link.v1"
OPPORTUNITY_BENCHMARK_PRICE_OBSERVATION_SCHEMA_VERSION = "opportunity_benchmark_price_observation.v1"
OPPORTUNITY_BENCHMARK_OUTCOME_SCHEMA_VERSION = "opportunity_benchmark_outcome.v1"
OPPORTUNITY_BENCHMARK_REPORT_SCHEMA_VERSION = "opportunity_benchmark_report.v1"

SOURCE_OPENING = "OPENING_OPT10032"
SOURCE_INTRADAY = "INTRADAY_OPT10032"
DEFAULT_SOURCES = (SOURCE_OPENING, SOURCE_INTRADAY)
DEFAULT_HORIZONS = (5, 15, 25, 60)
REPORT_ROOT = Path(__file__).resolve().parents[2] / "reports"


@dataclass(frozen=True)
class OpportunityBenchmarkConfig:
    enabled: bool = True
    sources: tuple[str, ...] = DEFAULT_SOURCES
    max_rank: int = 100
    reentry_gap_sec: int = 1200
    horizons_min: tuple[int, ...] = DEFAULT_HORIZONS
    anchor_pre_tick_max_age_sec: int = 5
    anchor_delay_max_sec: int = 30
    realtime_tracking_enabled: bool = False
    new_subscriptions_allowed: bool = False
    tick_coalesce_sec: int = 1
    save_price_observations: bool = True
    strict_qualification: bool = True

    @classmethod
    def from_env(cls) -> "OpportunityBenchmarkConfig":
        return cls(
            enabled=_bool_env("TRADING_OPPORTUNITY_BENCHMARK_ENABLED", True),
            sources=_tuple_env("TRADING_OPPORTUNITY_BENCHMARK_SOURCES", DEFAULT_SOURCES),
            max_rank=max(1, _int_env("TRADING_OPPORTUNITY_BENCHMARK_MAX_RANK", 100)),
            reentry_gap_sec=max(1, _int_env("TRADING_OPPORTUNITY_BENCHMARK_REENTRY_GAP_SEC", 1200)),
            horizons_min=tuple(
                sorted({max(1, int(item)) for item in _tuple_env("TRADING_OPPORTUNITY_BENCHMARK_HORIZONS_MIN", tuple(str(v) for v in DEFAULT_HORIZONS)) if _is_int(item)})
            )
            or DEFAULT_HORIZONS,
            anchor_pre_tick_max_age_sec=max(0, _int_env("TRADING_OPPORTUNITY_BENCHMARK_ANCHOR_PRE_TICK_MAX_AGE_SEC", 5)),
            anchor_delay_max_sec=max(0, _int_env("TRADING_OPPORTUNITY_BENCHMARK_ANCHOR_DELAY_MAX_SEC", 30)),
            realtime_tracking_enabled=_bool_env("TRADING_OPPORTUNITY_BENCHMARK_REALTIME_TRACKING_ENABLED", False),
            new_subscriptions_allowed=_bool_env("TRADING_OPPORTUNITY_BENCHMARK_NEW_SUBSCRIPTIONS_ALLOWED", False),
            tick_coalesce_sec=max(1, _int_env("TRADING_OPPORTUNITY_BENCHMARK_TICK_COALESCE_SEC", 1)),
            save_price_observations=_bool_env("TRADING_OPPORTUNITY_BENCHMARK_SAVE_PRICE_OBSERVATIONS", True),
            strict_qualification=_bool_env("TRADING_OPPORTUNITY_BENCHMARK_STRICT_QUALIFICATION", True),
        )


class OpportunityBenchmarkService:
    def __init__(
        self,
        db: Any,
        *,
        market_data: Any = None,
        candle_builder: Any = None,
        config: OpportunityBenchmarkConfig | None = None,
        clock: Any = datetime.now,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.candle_builder = candle_builder
        self.config = config or OpportunityBenchmarkConfig.from_env()
        self.clock = clock
        self.last_report: dict[str, Any] = _disabled_runtime_section(self.clock().replace(microsecond=0))
        self.last_full_report: dict[str, Any] = {}

    def build_report(
        self,
        *,
        trade_date: str,
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
        trade_date = str(trade_date or current.date().isoformat())
        state = "FINAL" if str(report_state or "").upper() == "FINAL" else "LIVE_PREVIEW"
        if not self.config.enabled:
            report = _disabled_report(trade_date, current, baseline=baseline)
            self.last_full_report = dict(report)
            self.last_report = _runtime_section(report)
            return report

        pr1_status = _pr1_contract_status(self.db)
        baseline_payload = _resolve_baseline(self.db, trade_date=trade_date, runtime_snapshot=runtime_snapshot, baseline=baseline)
        qualification = _latest_qualification(self.db, trade_date=trade_date)
        if pr1_status["status"] != "OK":
            report = _blocked_report(
                trade_date,
                current,
                baseline=baseline_payload,
                pr1_status=pr1_status,
                report_state=state,
                rebuild_reason=rebuild_reason,
            )
            if persist:
                self._persist_report(report)
            self.last_full_report = dict(report)
            self.last_report = _runtime_section(report)
            return report

        source_batches = [
            source
            for source in _load_source_batches(self.db, trade_date=trade_date, sources=self.config.sources, max_rank=self.config.max_rank)
            if (_as_datetime(source.get("observed_at")) or datetime.min) <= cutoff
        ]
        candidate_episodes = _candidate_episodes(self.db, trade_date=trade_date)
        observations_by_batch: dict[str, list[dict[str, Any]]] = {}
        batches: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        previous_rank: dict[str, int] = {}
        invariant_violations: list[dict[str, Any]] = []
        for source in source_batches:
            batch, batch_observations, batch_violations = _materialize_batch(
                source,
                baseline=baseline_payload,
                qualification=qualification,
                candidate_episodes=candidate_episodes,
                previous_rank=previous_rank,
                max_rank=self.config.max_rank,
            )
            batches.append(batch)
            observations.extend(batch_observations)
            observations_by_batch[batch["benchmark_batch_id"]] = batch_observations
            invariant_violations.extend(batch_violations)

        episodes = _build_benchmark_episodes(
            observations,
            market_data=self.market_data,
            config=self.config,
            qualification=qualification,
            cutoff=cutoff,
        )
        links = _build_candidate_links(episodes, candidate_episodes)
        _attach_capture_status(episodes, links)
        price_observations = _build_price_observations(
            episodes,
            observations,
            market_data=self.market_data,
            candle_builder=self.candle_builder,
            cutoff=cutoff,
        )
        outcomes = _build_outcomes(
            episodes,
            price_observations,
            horizons=self.config.horizons_min,
            cutoff=cutoff,
            qualification=qualification,
        )
        report = _build_report_payload(
            trade_date=trade_date,
            as_of=current,
            source_cutoff_at=cutoff,
            report_state=state,
            baseline=baseline_payload,
            qualification=qualification,
            batches=batches,
            observations=observations,
            episodes=episodes,
            links=links,
            outcomes=outcomes,
            invariant_violations=invariant_violations,
            build_ms=round((time.perf_counter() - started) * 1000.0, 3),
            rebuild_reason=rebuild_reason,
        )
        if strict_only:
            report["episodes"] = [item for item in report["episodes"] if item.get("strict_sample_eligible")]
        if persist:
            self._persist_all(batches, observations, episodes, links, price_observations, outcomes, report)
        if export:
            report["exported"] = export_opportunity_benchmark_report(report)
        self.last_full_report = dict(report)
        self.last_report = _runtime_section(report)
        return report

    def runtime_section(
        self,
        *,
        trade_date: str,
        as_of: datetime | str | None = None,
        baseline: Mapping[str, Any] | None = None,
        runtime_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = _as_datetime(as_of) or self.clock().replace(microsecond=0)
        try:
            report = self.build_report(
                trade_date=trade_date,
                as_of=current,
                baseline=baseline,
                runtime_snapshot=runtime_snapshot,
                persist=False,
            )
            return _runtime_section(report)
        except Exception as exc:
            section = _disabled_runtime_section(current)
            section.update({"enabled": True, "status": "ERROR", "error": str(exc), "warning_codes": ["OPPORTUNITY_BENCHMARK_FAILED"]})
            self.last_report = section
            return section

    def _persist_all(
        self,
        batches: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        episodes: list[dict[str, Any]],
        links: list[dict[str, Any]],
        price_observations: list[dict[str, Any]],
        outcomes: list[dict[str, Any]],
        report: dict[str, Any],
    ) -> None:
        calls = [
            ("save_opportunity_benchmark_batches", batches),
            ("save_opportunity_benchmark_observations", observations),
            ("save_opportunity_benchmark_episodes", episodes),
            ("save_opportunity_benchmark_candidate_links", links),
            ("save_opportunity_benchmark_price_observations", price_observations if self.config.save_price_observations else []),
            ("save_opportunity_benchmark_outcomes", outcomes),
        ]
        for name, rows in calls:
            saver = getattr(self.db, name, None)
            if callable(saver):
                saver(rows)
        self._persist_report(report)

    def _persist_report(self, report: dict[str, Any]) -> None:
        saver = getattr(self.db, "save_opportunity_benchmark_report", None)
        if callable(saver):
            saved = dict(saver(dict(report)) or {})
            if saved:
                report["report_id"] = str(saved.get("report_id") or report.get("report_id") or "")
                report["revision"] = int(saved.get("revision") or report.get("revision") or 0)


def export_opportunity_benchmark_report(report: Mapping[str, Any], *, root: Path | None = None) -> dict[str, str]:
    trade_date = str(report.get("trade_date") or "unknown")
    out = (root or REPORT_ROOT) / "opportunity_benchmark" / trade_date
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary_json": out / "summary.json",
        "summary_md": out / "summary.md",
        "batches_csv": out / "batches.csv",
        "observations_csv": out / "observations.csv",
        "episodes_csv": out / "episodes.csv",
        "candidate_links_csv": out / "candidate_links.csv",
        "outcomes_csv": out / "outcomes.csv",
        "uncaptured_csv": out / "uncaptured.csv",
        "data_quality_csv": out / "data_quality.csv",
        "invariant_violations_csv": out / "invariant_violations.csv",
    }
    paths["summary_json"].write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    paths["summary_md"].write_text(_report_markdown(report), encoding="utf-8")
    _write_csv(paths["batches_csv"], list(report.get("batches") or []))
    _write_csv(paths["observations_csv"], list(report.get("observations") or []))
    _write_csv(paths["episodes_csv"], list(report.get("episodes") or []))
    _write_csv(paths["candidate_links_csv"], list(report.get("candidate_links") or []))
    _write_csv(paths["outcomes_csv"], list(report.get("outcomes") or []))
    _write_csv(paths["uncaptured_csv"], list(report.get("uncaptured") or []))
    _write_csv(paths["data_quality_csv"], list(report.get("data_quality") or []))
    _write_csv(paths["invariant_violations_csv"], list(report.get("invariant_violations") or []))
    return {key: str(value) for key, value in paths.items()}


def _load_source_batches(db: Any, *, trade_date: str, sources: Iterable[str], max_rank: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    source_set = {str(item).upper() for item in sources}
    if SOURCE_OPENING in source_set:
        batch_loader = getattr(db, "list_opening_turnover_seed_batches", None)
        row_loader = getattr(db, "list_opening_turnover_seed_rows", None)
        if callable(batch_loader) and callable(row_loader):
            for batch in list(batch_loader(trade_date=trade_date, limit=1000) or []):
                rows = list(row_loader(batch_id=int(batch.get("id") or 0), limit=max_rank * 2) or [])
                result.append(_source_from_opening(batch, rows, max_rank=max_rank))
    if SOURCE_INTRADAY in source_set:
        batch_loader = getattr(db, "list_intraday_theme_discovery_batches", None)
        row_loader = getattr(db, "list_intraday_theme_discovery_rows", None)
        if callable(batch_loader) and callable(row_loader):
            for batch in list(batch_loader(trade_date=trade_date, limit=5000) or []):
                rows = list(row_loader(batch_id=int(batch.get("id") or 0), limit=max_rank * 2) or [])
                result.append(_source_from_intraday(batch, rows, max_rank=max_rank))
    return sorted(result, key=lambda item: (str(item.get("observed_at") or ""), str(item.get("source_type") or ""), str(item.get("source_batch_id") or "")))


def _source_from_opening(batch: Mapping[str, Any], rows: list[Mapping[str, Any]], *, max_rank: int) -> dict[str, Any]:
    raw = dict(batch.get("raw") or {})
    ack = dict(raw.get("ack_payload") or {})
    inputs = dict(ack.get("inputs") or {})
    batch_time = str(batch.get("batch_time") or "")
    observed_at = _opening_observed_at(str(batch.get("trade_date") or ""), batch_time, str(batch.get("created_at") or ""))
    return {
        "source_type": SOURCE_OPENING,
        "source_batch_id": str(batch.get("id") or ""),
        "source_command_id": str(batch.get("command_id") or ""),
        "source_idempotency_key": str(ack.get("idempotency_key") or _opening_idempotency(str(batch.get("trade_date") or ""), batch_time)),
        "observed_at": observed_at,
        "session_phase": "OPENING",
        "parser_status": str(batch.get("parser_status") or ""),
        "source_row_count": _int(raw.get("source_row_count"), _int(batch.get("row_count"), len(rows))),
        "source_top_n": _int(ack.get("top_n"), max_rank),
        "market_code": str(inputs.get("시장구분") or inputs.get("market_code") or ""),
        "exchange_code": str(inputs.get("거래소구분") or inputs.get("exchange_code") or ""),
        "include_management": str(inputs.get("관리종목포함") or inputs.get("include_management") or ""),
        "raw": raw,
        "rows": [dict(row) for row in rows],
    }


def _source_from_intraday(batch: Mapping[str, Any], rows: list[Mapping[str, Any]], *, max_rank: int) -> dict[str, Any]:
    raw = dict(batch.get("raw_summary") or {})
    return {
        "source_type": SOURCE_INTRADAY,
        "source_batch_id": str(batch.get("id") or ""),
        "source_command_id": str(batch.get("command_id") or ""),
        "source_idempotency_key": str(batch.get("idempotency_key") or ""),
        "observed_at": str(batch.get("observed_at") or ""),
        "session_phase": str(batch.get("session_phase") or ""),
        "parser_status": str(batch.get("parser_status") or batch.get("status") or ""),
        "source_row_count": _int(batch.get("row_count"), len(rows)),
        "source_top_n": max_rank,
        "market_code": "",
        "exchange_code": "",
        "include_management": "",
        "raw": raw,
        "rows": [dict(row) for row in rows],
    }


def _materialize_batch(
    source: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any],
    qualification: Mapping[str, Any],
    candidate_episodes: Mapping[str, list[dict[str, Any]]],
    previous_rank: dict[str, int],
    max_rank: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    rows = [dict(row) for row in list(source.get("rows") or [])]
    accepted: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    duplicate_count = 0
    invalid_code_count = 0
    missing_price_count = 0
    violations: list[dict[str, Any]] = []
    source_type = str(source.get("source_type") or "")
    source_batch_id = str(source.get("source_batch_id") or "")
    observed_at = str(source.get("observed_at") or "")
    benchmark_batch_id = _stable_id("obb", source_type, source_batch_id or str(source.get("source_command_id") or ""), observed_at)
    for index, row in enumerate(rows, start=1):
        raw = dict(row.get("raw") or row.get("raw_json") or {})
        code = normalize_code(row.get("stock_code") or row.get("code") or _raw_value(raw, "종목코드", "code", "stock_code"))
        rank = _int(row.get("rank"), index)
        turnover = _float(row.get("turnover_krw") or row.get("current_turnover_krw") or _raw_value(raw, "거래대금", "turnover", "turnover_krw"))
        current_price, price_source = _current_price(row, raw)
        volume = _int(row.get("volume") or _raw_value(raw, "현재거래량", "거래량", "volume", "cum_volume"), 0)
        if code in seen_codes:
            duplicate_count += 1
        seen_codes.add(code)
        if not _valid_stock_code(code):
            invalid_code_count += 1
        if current_price <= 0:
            missing_price_count += 1
        instrument_status = _instrument_filter_status(code, raw, row)
        included = rank <= max_rank and _valid_stock_code(code) and turnover > 0 and instrument_status in {"INCLUDED_COMMON_STOCK", "INCLUDED_UNKNOWN_TYPE"}
        if included:
            accepted.append(row)
        candidate_rows = _candidate_present(candidate_episodes.get(code, []), observed_at)
        prev = previous_rank.get(code)
        observation = {
            "schema_version": OPPORTUNITY_BENCHMARK_OBSERVATION_SCHEMA_VERSION,
            "observation_id": _stable_id("obo", benchmark_batch_id, code or str(index), str(rank), _fingerprint(raw)),
            "benchmark_batch_id": benchmark_batch_id,
            "trade_date": str(row.get("trade_date") or observed_at[:10] or source.get("trade_date") or ""),
            "observed_at": observed_at,
            "session_bucket": _session_bucket(observed_at),
            "source_type": source_type,
            "code": code,
            "name": str(row.get("stock_name") or row.get("name") or _raw_value(raw, "종목명", "name", "stock_name") or ""),
            "market_side": _market_side(raw, row),
            "rank": rank,
            "previous_rank": prev,
            "rank_delta": (prev - rank) if prev is not None and rank else None,
            "current_price": current_price,
            "price_source": price_source,
            "turnover_krw": turnover,
            "turnover_rank": rank,
            "change_rate_pct": _signed_float(row.get("change_rate_pct") or _raw_value(raw, "등락률", "등락율", "change_rate", "change_rate_pct")),
            "volume": volume,
            "best_ask": _float(_raw_value(raw, "매도호가", "best_ask")),
            "best_bid": _float(_raw_value(raw, "매수호가", "best_bid")),
            "spread_pct": _spread_pct(_float(_raw_value(raw, "매수호가", "best_bid")), _float(_raw_value(raw, "매도호가", "best_ask"))),
            "parser_status": str(row.get("parser_status") or source.get("parser_status") or ""),
            "parser_missing_fields": list(row.get("parser_missing_fields") or []),
            "raw_fingerprint": _fingerprint(raw),
            "instrument_type": str(_raw_value(raw, "종목분류", "instrument_type", "security_type") or ""),
            "instrument_filter_status": instrument_status,
            "candidate_present_at_observation": bool(candidate_rows),
            "candidate_instance_ids": [str(item.get("candidate_instance_id") or "") for item in candidate_rows],
            "baseline_id": str(baseline.get("baseline_id") or ""),
            "baseline_version": str(baseline.get("baseline_version") or baseline.get("version") or ""),
            "qualification_status_at_capture": str(qualification.get("qualification_status") or "COLLECTING"),
            "strict_sample_eligible_at_capture": bool(qualification.get("strict_sample_eligible")),
            "created_at": _now(),
            "included_in_universe": bool(included),
            "source_row_index": index,
            "source_row_rank": rank,
            "source_batch_id": source_batch_id,
            "source_command_id": str(source.get("source_command_id") or ""),
            "source_idempotency_key": str(source.get("source_idempotency_key") or ""),
            "raw": raw,
        }
        observations.append(observation)
        if code and rank:
            previous_rank[code] = rank
        if rank > max_rank:
            violations.append({"type": "ROW_OUTSIDE_MAX_RANK", "source_type": source_type, "code": code, "rank": rank})
    completeness = _completeness_status(source, len(rows), len(accepted))
    batch = {
        "schema_version": OPPORTUNITY_BENCHMARK_BATCH_SCHEMA_VERSION,
        "benchmark_batch_id": benchmark_batch_id,
        "trade_date": str(observed_at[:10] or ""),
        "observed_at": observed_at,
        "session_bucket": _session_bucket(observed_at),
        "source_type": source_type,
        "source_batch_id": source_batch_id,
        "source_command_id": str(source.get("source_command_id") or ""),
        "source_idempotency_key": str(source.get("source_idempotency_key") or ""),
        "market_scope": str(source.get("market_code") or ""),
        "exchange_scope": str(source.get("exchange_code") or ""),
        "requested_top_n": _int(source.get("source_top_n"), max_rank),
        "raw_row_count": _int(source.get("source_row_count"), len(rows)),
        "parsed_row_count": len(rows),
        "accepted_row_count": len(accepted),
        "duplicate_row_count": duplicate_count,
        "invalid_code_count": invalid_code_count,
        "missing_price_count": missing_price_count,
        "parser_status": str(source.get("parser_status") or ""),
        "completeness_status": completeness,
        "baseline_id": str(baseline.get("baseline_id") or ""),
        "baseline_version": str(baseline.get("baseline_version") or baseline.get("version") or ""),
        "config_hash": str(baseline.get("config_hash") or ""),
        "git_sha": str(baseline.get("git_sha") or ""),
        "qualification_status_at_capture": str(qualification.get("qualification_status") or "COLLECTING"),
        "source_fingerprint": _fingerprint({"source": dict(source), "rows": rows}),
        "created_at": _now(),
        "source_session_phase": str(source.get("session_phase") or ""),
        "source_parser_status": str(source.get("parser_status") or ""),
        "source_row_count": _int(source.get("source_row_count"), len(rows)),
        "source_top_n": _int(source.get("source_top_n"), max_rank),
        "source_market_code": str(source.get("market_code") or ""),
        "source_exchange_code": str(source.get("exchange_code") or ""),
        "source_include_management": str(source.get("include_management") or ""),
        "raw_source_fingerprint": _fingerprint(source.get("raw") or {}),
        "raw": dict(source.get("raw") or {}),
    }
    return batch, observations, violations


def _build_benchmark_episodes(
    observations: list[dict[str, Any]],
    *,
    market_data: Any,
    config: OpportunityBenchmarkConfig,
    qualification: Mapping[str, Any],
    cutoff: datetime,
) -> list[dict[str, Any]]:
    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        if observation.get("included_in_universe"):
            by_code[str(observation.get("code") or "")].append(observation)
    episodes: list[dict[str, Any]] = []
    for code, rows in by_code.items():
        generation = 0
        current_rows: list[dict[str, Any]] = []
        last_seen: datetime | None = None
        for row in sorted(rows, key=lambda item: str(item.get("observed_at") or "")):
            observed = _as_datetime(row.get("observed_at"))
            if observed is None:
                continue
            if last_seen is None or (observed - last_seen).total_seconds() > config.reentry_gap_sec:
                if current_rows:
                    episodes.append(_episode_from_rows(code, generation, current_rows, market_data=market_data, config=config, qualification=qualification, cutoff=cutoff))
                generation += 1
                current_rows = [row]
            else:
                current_rows.append(row)
            last_seen = observed
        if current_rows:
            episodes.append(_episode_from_rows(code, generation, current_rows, market_data=market_data, config=config, qualification=qualification, cutoff=cutoff))
    return episodes


def _episode_from_rows(
    code: str,
    generation_seq: int,
    rows: list[dict[str, Any]],
    *,
    market_data: Any,
    config: OpportunityBenchmarkConfig,
    qualification: Mapping[str, Any],
    cutoff: datetime,
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda item: str(item.get("observed_at") or ""))
    anchor = next((row for row in ordered if str(row.get("code") or "") == code), ordered[0])
    anchor_at = str(anchor.get("observed_at") or "")
    anchor_dt = _as_datetime(anchor_at) or cutoff
    anchor_price, anchor_source, delay_sec = _anchor_price(anchor, market_data=market_data, config=config)
    ranks = [_int(row.get("rank"), 0) for row in ordered if _int(row.get("rank"), 0) > 0]
    turnovers = [_float(row.get("turnover_krw")) for row in ordered]
    changes = [_float(row.get("change_rate_pct")) for row in ordered]
    missing_batch_count = _missing_batch_count(ordered, config.reentry_gap_sec)
    quality = "HIGH" if anchor_price > 0 and anchor_source in {"OPT10032_PARSED", "OPT10032_RAW_RECOVERED"} else "DELAYED_ANCHOR_PRICE" if anchor_source == "REALTIME_DELAYED" else "INSUFFICIENT_ANCHOR_PRICE"
    payload = {
        "schema_version": OPPORTUNITY_BENCHMARK_EPISODE_SCHEMA_VERSION,
        "benchmark_episode_id": _stable_id("obe", ordered[0].get("trade_date"), code, str(generation_seq), anchor_at),
        "trade_date": str(ordered[0].get("trade_date") or anchor_at[:10] or ""),
        "code": code,
        "name": str(anchor.get("name") or ""),
        "generation_seq": generation_seq,
        "first_seen_at": str(ordered[0].get("observed_at") or ""),
        "last_seen_at": str(ordered[-1].get("observed_at") or ""),
        "exited_at": "",
        "reentered_at": str(ordered[0].get("observed_at") or "") if generation_seq > 1 else "",
        "active": (cutoff - (_as_datetime(ordered[-1].get("observed_at")) or cutoff)).total_seconds() <= config.reentry_gap_sec,
        "first_rank": ranks[0] if ranks else 0,
        "best_rank": min(ranks) if ranks else 0,
        "last_rank": ranks[-1] if ranks else 0,
        "first_turnover_krw": turnovers[0] if turnovers else 0.0,
        "max_turnover_krw": max(turnovers) if turnovers else 0.0,
        "first_change_rate_pct": changes[0] if changes else 0.0,
        "max_change_rate_pct": max(changes) if changes else 0.0,
        "anchor_observation_id": str(anchor.get("observation_id") or ""),
        "anchor_at": anchor_at,
        "anchor_price": anchor_price,
        "anchor_price_source": anchor_source,
        "anchor_delay_sec": delay_sec,
        "session_bucket": str(anchor.get("session_bucket") or _session_bucket(anchor_at)),
        "market_side": str(anchor.get("market_side") or "UNKNOWN"),
        "source_types": sorted({str(row.get("source_type") or "") for row in ordered if row.get("source_type")}),
        "observation_count": len(ordered),
        "missing_batch_count": missing_batch_count,
        "episode_quality": quality,
        "candidate_capture_status": "NOT_CAPTURED",
        "candidate_link_count": 0,
        "qualification_status": str(qualification.get("qualification_status") or anchor.get("qualification_status_at_capture") or "COLLECTING"),
        "strict_sample_eligible": bool(qualification.get("strict_sample_eligible") or anchor.get("strict_sample_eligible_at_capture")),
        "fingerprint": "",
        "created_at": _now(),
        "updated_at": _now(),
        "instrument_filter_status": str(anchor.get("instrument_filter_status") or "UNKNOWN"),
    }
    payload["fingerprint"] = _fingerprint({key: payload.get(key) for key in ("code", "generation_seq", "first_seen_at", "last_seen_at", "best_rank", "anchor_price", "source_types", "observation_count")})
    return payload


def _anchor_price(observation: Mapping[str, Any], *, market_data: Any, config: OpportunityBenchmarkConfig) -> tuple[float, str, int]:
    price = _float(observation.get("current_price"))
    source = str(observation.get("price_source") or "MISSING")
    if price > 0:
        return price, source, 0
    anchor_at = _as_datetime(observation.get("observed_at"))
    tick = market_data.latest_tick(observation.get("code")) if market_data is not None and hasattr(market_data, "latest_tick") else None
    if tick is None or anchor_at is None:
        return 0.0, "MISSING", 0
    tick_at = getattr(tick, "timestamp", None)
    tick_price = _float(getattr(tick, "price", 0))
    if tick_price <= 0 or not isinstance(tick_at, datetime):
        return 0.0, "MISSING", 0
    delta = (tick_at.replace(microsecond=0) - anchor_at).total_seconds()
    if delta <= 0 and abs(delta) <= config.anchor_pre_tick_max_age_sec:
        return tick_price, "REALTIME_NEARBY", int(delta)
    if 0 < delta <= config.anchor_delay_max_sec:
        return tick_price, "REALTIME_DELAYED", int(delta)
    return 0.0, "MISSING", 0


def _build_candidate_links(episodes: list[dict[str, Any]], candidate_episodes: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for episode in episodes:
        candidates = list(candidate_episodes.get(str(episode.get("code") or ""), []))
        episode_links: list[dict[str, Any]] = []
        if not candidates:
            links.append(_not_captured_link(episode))
            continue
        for candidate in candidates:
            link = _candidate_link(episode, candidate)
            links.append(link)
            episode_links.append(link)
        primary_id = _primary_link_id(episode_links)
        for link in episode_links:
            link["primary_link"] = link["link_id"] == primary_id
    return links


def _candidate_link(episode: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    anchor = _as_datetime(episode.get("anchor_at"))
    first_seen = _as_datetime(candidate.get("first_seen_at"))
    delay = int((first_seen - anchor).total_seconds()) if anchor and first_seen else None
    window = _detection_window(delay)
    stage_times = dict(candidate.get("stage_first_reached_at") or {})
    champion_stage = ""
    for stage in ("CHAMPION_VALID_OBSERVE", "CHAMPION_CONTEXT_ELIGIBLE", "CHAMPION_MATCHED", "CHAMPION_FORMING"):
        if stage in set(candidate.get("reached_stages") or []) or stage_times.get(stage):
            champion_stage = stage
            break
    ci = str(candidate.get("candidate_instance_id") or "")
    return {
        "schema_version": OPPORTUNITY_BENCHMARK_CANDIDATE_LINK_SCHEMA_VERSION,
        "link_id": _stable_id("obcl", episode.get("benchmark_episode_id"), ci or "low", str(delay)),
        "trade_date": str(episode.get("trade_date") or ""),
        "benchmark_episode_id": str(episode.get("benchmark_episode_id") or ""),
        "candidate_instance_id": ci,
        "candidate_id": candidate.get("candidate_id"),
        "candidate_generation_seq": _int(candidate.get("candidate_generation_seq"), 0),
        "code": str(episode.get("code") or ""),
        "benchmark_anchor_at": str(episode.get("anchor_at") or ""),
        "candidate_first_seen_at": str(candidate.get("first_seen_at") or ""),
        "detection_delay_sec": delay,
        "candidate_before_benchmark": delay is not None and delay < 0,
        "candidate_after_benchmark": delay is not None and delay >= 0,
        "detection_window": window,
        "max_funnel_stage": str(candidate.get("current_stage") or ""),
        "champion_stage": champion_stage,
        "baseline_role": str(candidate.get("baseline_role") or ""),
        "attribution_confidence": str(candidate.get("attribution_confidence") or ""),
        "link_confidence": "HIGH" if ci and str(candidate.get("attribution_confidence") or "") == "HIGH" else "LOW",
        "link_reason": "candidate_instance_id" if ci else "low_confidence_candidate_episode",
        "primary_link": False,
        "created_at": _now(),
    }


def _not_captured_link(episode: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": OPPORTUNITY_BENCHMARK_CANDIDATE_LINK_SCHEMA_VERSION,
        "link_id": _stable_id("obcl", episode.get("benchmark_episode_id"), "NOT_CAPTURED"),
        "trade_date": str(episode.get("trade_date") or ""),
        "benchmark_episode_id": str(episode.get("benchmark_episode_id") or ""),
        "candidate_instance_id": "",
        "candidate_id": None,
        "candidate_generation_seq": 0,
        "code": str(episode.get("code") or ""),
        "benchmark_anchor_at": str(episode.get("anchor_at") or ""),
        "candidate_first_seen_at": "",
        "detection_delay_sec": None,
        "candidate_before_benchmark": False,
        "candidate_after_benchmark": False,
        "detection_window": "NOT_CAPTURED",
        "max_funnel_stage": "",
        "champion_stage": "",
        "baseline_role": "",
        "attribution_confidence": "",
        "link_confidence": "NONE",
        "link_reason": "not_captured_by_candidate",
        "primary_link": True,
        "created_at": _now(),
    }


def _attach_capture_status(episodes: list[dict[str, Any]], links: list[dict[str, Any]]) -> None:
    links_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for link in links:
        links_by_episode[str(link.get("benchmark_episode_id") or "")].append(link)
    for episode in episodes:
        rows = links_by_episode.get(str(episode.get("benchmark_episode_id") or ""), [])
        captured = [row for row in rows if row.get("candidate_instance_id")]
        episode["candidate_capture_status"] = "CAPTURED" if captured else "NOT_CAPTURED"
        episode["candidate_link_count"] = len(captured)
        episode["updated_at"] = _now()


def _build_price_observations(
    episodes: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    *,
    market_data: Any,
    candle_builder: Any,
    cutoff: datetime,
) -> list[dict[str, Any]]:
    by_code = {str(ep.get("code") or ""): ep for ep in episodes}
    rows: list[dict[str, Any]] = []
    for obs in observations:
        code = str(obs.get("code") or "")
        episode = by_code.get(code)
        if not episode:
            continue
        observed_at = _as_datetime(obs.get("observed_at"))
        anchor_at = _as_datetime(episode.get("anchor_at"))
        price = _float(obs.get("current_price"))
        if observed_at is None or anchor_at is None or observed_at < anchor_at or observed_at > cutoff or price <= 0:
            continue
        rows.append(_price_observation(episode, observed_at, price, price, price, _int(obs.get("volume"), 0), source_type=str(obs.get("source_type") or ""), resolution="FIVE_MINUTE_TR_SNAPSHOT", quality="SAMPLED_TR", event_id=str(obs.get("observation_id") or ""), fingerprint=str(obs.get("raw_fingerprint") or "")))
    for episode in episodes:
        code = str(episode.get("code") or "")
        anchor_at = _as_datetime(episode.get("anchor_at"))
        if anchor_at is None:
            continue
        if candle_builder is not None and hasattr(candle_builder, "completed_candles"):
            for candle in list(candle_builder.completed_candles(code, 1) or []):
                start = getattr(candle, "start_at", None)
                if not isinstance(start, datetime) or start < anchor_at or start > cutoff:
                    continue
                rows.append(
                    _price_observation(
                        episode,
                        start.replace(microsecond=0),
                        _float(getattr(candle, "close", 0)),
                        _float(getattr(candle, "high", 0)),
                        _float(getattr(candle, "low", 0)),
                        _int(getattr(candle, "volume", 0), 0),
                        source_type="EXISTING_1M_CANDLE",
                        resolution="ONE_MINUTE_CANDLE",
                        quality="ONE_MINUTE_OHLC",
                        event_id=f"candle:{code}:{start.isoformat()}",
                        fingerprint=_fingerprint({"code": code, "start_at": start.isoformat(), "close": getattr(candle, "close", 0), "high": getattr(candle, "high", 0), "low": getattr(candle, "low", 0)}),
                    )
                )
        tick = market_data.latest_tick(code) if market_data is not None and hasattr(market_data, "latest_tick") else None
        tick_at = getattr(tick, "timestamp", None) if tick is not None else None
        tick_price = _float(getattr(tick, "price", 0)) if tick is not None else 0.0
        if isinstance(tick_at, datetime) and anchor_at <= tick_at <= cutoff and tick_price > 0:
            rows.append(
                _price_observation(
                    episode,
                    tick_at.replace(microsecond=0),
                    tick_price,
                    tick_price,
                    tick_price,
                    _int(getattr(tick, "cum_volume", 0), 0),
                    source_type="EXISTING_REALTIME_TICK",
                    resolution="TICK",
                    quality="HIGH_RES_REALTIME",
                    event_id=f"tick:{code}:{tick_at.isoformat()}",
                    fingerprint=_fingerprint({"code": code, "timestamp": tick_at.isoformat(), "price": tick_price}),
                )
            )
    dedup: dict[str, dict[str, Any]] = {}
    for row in rows:
        dedup[row["price_observation_id"]] = row
    return sorted(dedup.values(), key=lambda item: (str(item.get("benchmark_episode_id") or ""), str(item.get("observed_at") or ""), str(item.get("source_type") or "")))


def _price_observation(
    episode: Mapping[str, Any],
    observed_at: datetime,
    price: float,
    high: float,
    low: float,
    volume: int,
    *,
    source_type: str,
    resolution: str,
    quality: str,
    event_id: str,
    fingerprint: str,
) -> dict[str, Any]:
    return {
        "schema_version": OPPORTUNITY_BENCHMARK_PRICE_OBSERVATION_SCHEMA_VERSION,
        "price_observation_id": _stable_id("obpo", episode.get("benchmark_episode_id"), observed_at.isoformat(), source_type, event_id),
        "benchmark_episode_id": str(episode.get("benchmark_episode_id") or ""),
        "trade_date": str(episode.get("trade_date") or ""),
        "code": str(episode.get("code") or ""),
        "observed_at": observed_at.isoformat(),
        "price": price,
        "high": high,
        "low": low,
        "volume": volume,
        "source_type": source_type,
        "source_resolution": resolution,
        "source_quality": quality,
        "source_event_id": event_id,
        "source_fingerprint": fingerprint,
        "created_at": _now(),
    }


def _build_outcomes(
    episodes: list[dict[str, Any]],
    price_observations: list[dict[str, Any]],
    *,
    horizons: Iterable[int],
    cutoff: datetime,
    qualification: Mapping[str, Any],
) -> list[dict[str, Any]]:
    by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in price_observations:
        by_episode[str(row.get("benchmark_episode_id") or "")].append(row)
    outcomes: list[dict[str, Any]] = []
    for episode in episodes:
        anchor_at = _as_datetime(episode.get("anchor_at"))
        anchor_price = _float(episode.get("anchor_price"))
        for horizon in horizons:
            horizon = int(horizon)
            target = (anchor_at + timedelta(minutes=horizon)) if anchor_at else None
            tolerance = _horizon_tolerance_min(horizon)
            status = "PENDING"
            quality = "INSUFFICIENT"
            observed_at = ""
            delay = None
            return_pct = None
            mfe_pct = None
            mae_pct = None
            truncated = False
            path = []
            target_row = None
            if anchor_at is None or anchor_price <= 0:
                status = "INSUFFICIENT_ANCHOR_PRICE"
            elif target and target > _market_close(anchor_at):
                status = "TRUNCATED_SESSION"
                truncated = True
            elif target and cutoff < target:
                status = "PENDING"
            else:
                path = [
                    row for row in by_episode.get(str(episode.get("benchmark_episode_id") or ""), [])
                    if anchor_at <= (_as_datetime(row.get("observed_at")) or datetime.min) <= target
                ]
                candidates = [
                    row for row in by_episode.get(str(episode.get("benchmark_episode_id") or ""), [])
                    if target <= (_as_datetime(row.get("observed_at")) or datetime.min) <= target + timedelta(minutes=tolerance)
                ]
                target_row = min(candidates, key=lambda row: abs(((_as_datetime(row.get("observed_at")) or target) - target).total_seconds())) if candidates else None
                if not path and not target_row:
                    status = "INSUFFICIENT_PATH"
                elif not target_row:
                    status = "PARTIAL"
                else:
                    status = "COMPLETE"
                    observed_dt = _as_datetime(target_row.get("observed_at")) or target
                    observed_at = observed_dt.isoformat()
                    delay = int((observed_dt - target).total_seconds())
                    return_pct = _pct(_float(target_row.get("price")) - anchor_price, anchor_price)
                    highs = [_float(row.get("high") or row.get("price")) for row in path + [target_row]]
                    lows = [_float(row.get("low") or row.get("price")) for row in path + [target_row]]
                    mfe_pct = _pct(max(highs) - anchor_price, anchor_price) if highs else None
                    mae_pct = _pct(min(lows) - anchor_price, anchor_price) if lows else None
                quality = _label_quality(path + ([target_row] if target_row else []))
            outcome = {
                "schema_version": OPPORTUNITY_BENCHMARK_OUTCOME_SCHEMA_VERSION,
                "outcome_id": _stable_id("obout", episode.get("benchmark_episode_id"), str(horizon)),
                "benchmark_episode_id": str(episode.get("benchmark_episode_id") or ""),
                "trade_date": str(episode.get("trade_date") or ""),
                "code": str(episode.get("code") or ""),
                "anchor_at": str(episode.get("anchor_at") or ""),
                "anchor_price": anchor_price,
                "anchor_price_source": str(episode.get("anchor_price_source") or ""),
                "horizon_min": horizon,
                "horizon_target_at": target.isoformat() if target else "",
                "horizon_observed_at": observed_at,
                "horizon_delay_sec": delay,
                "return_pct": return_pct,
                "mfe_pct": mfe_pct,
                "mae_pct": mae_pct,
                "path_observation_count": len(path),
                "realtime_observation_count": sum(1 for row in path if row.get("source_resolution") == "TICK"),
                "candle_observation_count": sum(1 for row in path if row.get("source_resolution") == "ONE_MINUTE_CANDLE"),
                "tr_snapshot_observation_count": sum(1 for row in path if "TR" in str(row.get("source_quality") or "")),
                "label_status": status,
                "label_quality": quality,
                "truncated_by_market_close": truncated,
                "qualification_status": str(qualification.get("qualification_status") or episode.get("qualification_status") or "COLLECTING"),
                "strict_sample_eligible": bool(qualification.get("strict_sample_eligible") or episode.get("strict_sample_eligible")),
                "calculated_at": _now(),
                "source_cutoff_at": cutoff.isoformat(),
                "revision": 0,
            }
            outcome["fingerprint"] = _fingerprint(outcome)
            outcomes.append(outcome)
    return outcomes


def _build_report_payload(
    *,
    trade_date: str,
    as_of: datetime,
    source_cutoff_at: datetime,
    report_state: str,
    baseline: Mapping[str, Any],
    qualification: Mapping[str, Any],
    batches: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    links: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    invariant_violations: list[dict[str, Any]],
    build_ms: float,
    rebuild_reason: str,
) -> dict[str, Any]:
    captured = [episode for episode in episodes if episode.get("candidate_capture_status") == "CAPTURED"]
    not_captured = [episode for episode in episodes if episode.get("candidate_capture_status") == "NOT_CAPTURED"]
    exact_labels = [row for row in outcomes if row.get("label_status") == "COMPLETE" and row.get("label_quality") in {"HIGH_RES_REALTIME", "ONE_MINUTE_CANDLE", "MIXED_HIGH_QUALITY"}]
    sampled_labels = [row for row in outcomes if row.get("label_quality") == "SAMPLED_OPT10032"]
    insufficient = [row for row in outcomes if str(row.get("label_status") or "").startswith("INSUFFICIENT")]
    link_windows = Counter(str(row.get("detection_window") or "UNKNOWN") for row in links)
    label_quality_counts = Counter(str(row.get("label_quality") or "UNKNOWN") for row in outcomes)
    horizon_complete = {f"{horizon}m_label_complete_count": sum(1 for row in outcomes if _int(row.get("horizon_min"), 0) == horizon and row.get("label_status") == "COMPLETE") for horizon in DEFAULT_HORIZONS}
    report = {
        "schema_version": OPPORTUNITY_BENCHMARK_REPORT_SCHEMA_VERSION,
        "report_id": _report_id("opportunity_benchmark", trade_date, report_state, as_of, baseline),
        "trade_date": trade_date,
        "report_state": report_state,
        "qualification_status": str(qualification.get("qualification_status") or "COLLECTING"),
        "strict_sample_eligible": bool(qualification.get("strict_sample_eligible")),
        "baseline_id": str(baseline.get("baseline_id") or ""),
        "baseline_version": str(baseline.get("baseline_version") or baseline.get("version") or ""),
        "config_hash": str(baseline.get("config_hash") or ""),
        "git_sha": str(baseline.get("git_sha") or ""),
        "source_batch_count": len(batches),
        "expected_batch_count": len(batches),
        "complete_batch_count": sum(1 for row in batches if row.get("completeness_status") == "COMPLETE"),
        "partial_batch_count": sum(1 for row in batches if row.get("completeness_status") == "PARTIAL"),
        "source_error_batch_count": sum(1 for row in batches if row.get("completeness_status") in {"SOURCE_ERROR", "PARSE_ERROR"}),
        "observation_count": len(observations),
        "unique_code_count": len({row.get("code") for row in observations if row.get("included_in_universe")}),
        "episode_count": len(episodes),
        "active_episode_count": sum(1 for row in episodes if row.get("active")),
        "completed_episode_count": sum(1 for row in episodes if not row.get("active")),
        "candidate_captured_episode_count": len(captured),
        "candidate_not_captured_episode_count": len(not_captured),
        "strict_link_count": sum(1 for row in links if row.get("candidate_instance_id") and row.get("link_confidence") == "HIGH"),
        "low_confidence_link_count": sum(1 for row in links if row.get("candidate_instance_id") and row.get("link_confidence") != "HIGH"),
        "preexisting_candidate_count": link_windows.get("PREEXISTING", 0),
        "captured_within_1m_count": link_windows.get("WITHIN_1M", 0),
        "captured_within_5m_count": link_windows.get("WITHIN_5M", 0),
        "captured_within_15m_count": link_windows.get("WITHIN_15M", 0),
        "captured_after_15m_count": link_windows.get("AFTER_15M", 0),
        "exact_label_count": len(exact_labels),
        "sampled_label_count": len(sampled_labels),
        "insufficient_label_count": len(insufficient),
        "label_coverage_rate": _ratio(len(outcomes) - len(insufficient), len(outcomes)),
        "candidate_capture_rate": _ratio(len(captured), len(episodes)),
        "invariant_violation_count": len(invariant_violations),
        "generated_at": as_of.isoformat(),
        "source_cutoff_at": source_cutoff_at.isoformat(),
        "build_ms": build_ms,
        "revision": 0,
        "rebuild_reason": rebuild_reason,
        "batches": batches,
        "observations": observations,
        "episodes": sorted(episodes, key=lambda row: (_int(row.get("best_rank"), 999999), -_float(row.get("max_turnover_krw")), str(row.get("code") or ""))),
        "candidate_links": links,
        "outcomes": outcomes,
        "uncaptured": _uncaptured_items(not_captured, outcomes),
        "capture_delay": _capture_delay_items(links),
        "data_quality": _data_quality_items(episodes, outcomes, batches),
        "invariant_violations": invariant_violations,
        "breakdowns": _breakdowns(episodes, links, outcomes),
        "behavioral_parity": {
            "opt10032_command_count_before": None,
            "opt10032_command_count_after": None,
            "duplicate_tr_increase_count": 0,
            "new_realtime_registration_count": 0,
            "behavioral_diff_count": 0,
        },
        "order_safety": _order_counts_safe(baseline),
        **horizon_complete,
        "label_quality_counts": dict(label_quality_counts),
    }
    return report


def _uncaptured_items(episodes: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outcomes_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for outcome in outcomes:
        outcomes_by_episode[str(outcome.get("benchmark_episode_id") or "")].append(outcome)
    items = []
    for episode in episodes:
        rows = outcomes_by_episode.get(str(episode.get("benchmark_episode_id") or ""), [])
        item = {
            "benchmark_episode_id": episode.get("benchmark_episode_id"),
            "code": episode.get("code"),
            "name": episode.get("name"),
            "anchor_at": episode.get("anchor_at"),
            "rank": episode.get("first_rank"),
            "turnover_krw": episode.get("first_turnover_krw"),
            "change_rate_pct": episode.get("first_change_rate_pct"),
            "candidate_capture_status": episode.get("candidate_capture_status"),
            "label_quality": _label_quality(rows),
            "qualification": episode.get("qualification_status"),
        }
        for row in rows:
            horizon = _int(row.get("horizon_min"), 0)
            item[f"raw_return_{horizon}m_pct"] = row.get("return_pct")
            item[f"raw_mfe_{horizon}m_pct"] = row.get("mfe_pct")
            item[f"raw_mae_{horizon}m_pct"] = row.get("mae_pct")
        items.append(item)
    return items


def _capture_delay_items(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "benchmark_episode_id": row.get("benchmark_episode_id"),
            "candidate_instance_id": row.get("candidate_instance_id"),
            "code": row.get("code"),
            "detection_window": row.get("detection_window"),
            "detection_delay_sec": row.get("detection_delay_sec"),
            "link_confidence": row.get("link_confidence"),
            "primary_link": row.get("primary_link"),
        }
        for row in links
        if row.get("candidate_instance_id")
    ]


def _data_quality_items(episodes: list[dict[str, Any]], outcomes: list[dict[str, Any]], batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for batch in batches:
        if batch.get("completeness_status") != "COMPLETE":
            items.append({"type": "source_batch", "status": batch.get("completeness_status"), "source_type": batch.get("source_type"), "source_batch_id": batch.get("source_batch_id")})
    for episode in episodes:
        if _float(episode.get("anchor_price")) <= 0:
            items.append({"type": "anchor_price_missing", "benchmark_episode_id": episode.get("benchmark_episode_id"), "code": episode.get("code")})
        if episode.get("market_side") == "UNKNOWN":
            items.append({"type": "market_side_unknown", "benchmark_episode_id": episode.get("benchmark_episode_id"), "code": episode.get("code")})
    for outcome in outcomes:
        if str(outcome.get("label_status") or "").startswith("INSUFFICIENT") or outcome.get("label_quality") in {"SAMPLED_OPT10032", "INSUFFICIENT"}:
            items.append({"type": "label_quality", "benchmark_episode_id": outcome.get("benchmark_episode_id"), "code": outcome.get("code"), "horizon_min": outcome.get("horizon_min"), "status": outcome.get("label_status"), "quality": outcome.get("label_quality")})
    return items


def _breakdowns(episodes: list[dict[str, Any]], links: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "rank_bucket": _counter_rows(_rank_bucket(_int(row.get("best_rank"), 0)) for row in episodes),
        "session_bucket": _counter_rows(str(row.get("session_bucket") or "UNKNOWN") for row in episodes),
        "market_side": _counter_rows(str(row.get("market_side") or "UNKNOWN") for row in episodes),
        "candidate_detection_window": _counter_rows(str(row.get("detection_window") or "UNKNOWN") for row in links),
        "label_quality": _counter_rows(str(row.get("label_quality") or "UNKNOWN") for row in outcomes),
        "instrument_filter_status": _counter_rows(str(row.get("instrument_filter_status") or "UNKNOWN") for row in episodes),
    }


def _candidate_episodes(db: Any, *, trade_date: str) -> dict[str, list[dict[str, Any]]]:
    loader = getattr(db, "list_candidate_funnel_episodes", None)
    if not callable(loader):
        return {}
    rows = [dict(row or {}) for row in list(loader(trade_date=trade_date, limit=100000) or [])]
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        code = normalize_code(row.get("code"))
        if code:
            result[code].append(row)
    for code in result:
        result[code].sort(key=lambda item: str(item.get("first_seen_at") or ""))
    return result


def _candidate_present(candidates: list[dict[str, Any]], observed_at: str) -> list[dict[str, Any]]:
    observed = _as_datetime(observed_at)
    if observed is None:
        return []
    result = []
    for candidate in candidates:
        first = _as_datetime(candidate.get("first_seen_at"))
        if first and first <= observed:
            result.append(candidate)
    return result


def _pr1_contract_status(db: Any) -> dict[str, Any]:
    required_methods = [
        "list_candidate_funnel_episodes",
        "list_trading_day_qualification_reports",
        "save_candidate_funnel_episodes",
        "save_trading_day_qualification_report",
    ]
    missing = [name for name in required_methods if not callable(getattr(db, name, None))]
    missing_tables = []
    for table in ("candidate_funnel_episode_latest", "trading_day_qualification_reports"):
        if not _has_table(db, table):
            missing_tables.append(table)
    status = "OK" if not missing and not missing_tables else "BLOCKED_BY_PR1"
    return {"status": status, "missing_methods": missing, "missing_tables": missing_tables}


def _latest_qualification(db: Any, *, trade_date: str) -> dict[str, Any]:
    loader = getattr(db, "list_trading_day_qualification_reports", None)
    if callable(loader):
        for state in ("FINAL", "LIVE_PREVIEW"):
            rows = list(loader(trade_date=trade_date, report_state=state, limit=1) or [])
            if rows:
                row = dict(rows[0] or {})
                return {
                    "qualification_report_id": row.get("report_id", ""),
                    "qualification_status": row.get("qualification_status", ""),
                    "qualification_revision": _int(row.get("revision"), 0),
                    "strict_sample_eligible": bool(row.get("strict_sample_eligible")),
                    "session_qualification_status": dict(row.get("session_qualifications") or {}),
                    "qualification_reason_codes": list(row.get("reason_codes") or []),
                }
    return {
        "qualification_report_id": "",
        "qualification_status": "COLLECTING",
        "qualification_revision": 0,
        "strict_sample_eligible": False,
        "session_qualification_status": {},
        "qualification_reason_codes": [],
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
            payload = dict(rows[0].get("payload") or rows[0])
            payload.setdefault("version", payload.get("baseline_version"))
            return payload
    return {}


def _disabled_report(trade_date: str, as_of: datetime, *, baseline: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = dict(baseline or {})
    return {
        "schema_version": OPPORTUNITY_BENCHMARK_REPORT_SCHEMA_VERSION,
        "report_id": _report_id("opportunity_benchmark", trade_date, "LIVE_PREVIEW", as_of, payload),
        "trade_date": trade_date,
        "report_state": "LIVE_PREVIEW",
        "enabled": False,
        "status": "DISABLED",
        "baseline_id": str(payload.get("baseline_id") or ""),
        "baseline_version": str(payload.get("baseline_version") or payload.get("version") or ""),
        "source_batch_count": 0,
        "observation_count": 0,
        "episode_count": 0,
        "generated_at": as_of.isoformat(),
        "revision": 0,
    }


def _blocked_report(
    trade_date: str,
    as_of: datetime,
    *,
    baseline: Mapping[str, Any],
    pr1_status: Mapping[str, Any],
    report_state: str,
    rebuild_reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": OPPORTUNITY_BENCHMARK_REPORT_SCHEMA_VERSION,
        "report_id": _report_id("opportunity_benchmark", trade_date, report_state, as_of, baseline),
        "trade_date": trade_date,
        "report_state": report_state,
        "enabled": True,
        "status": "BLOCKED_BY_PR1",
        "baseline_id": str(baseline.get("baseline_id") or ""),
        "baseline_version": str(baseline.get("baseline_version") or baseline.get("version") or ""),
        "source_batch_count": 0,
        "observation_count": 0,
        "episode_count": 0,
        "candidate_capture_rate": None,
        "invariant_violation_count": 1,
        "invariant_violations": [{"type": "BLOCKED_BY_PR1", **dict(pr1_status)}],
        "warning_codes": ["BLOCKED_BY_PR1"],
        "generated_at": as_of.isoformat(),
        "revision": 0,
        "rebuild_reason": rebuild_reason,
    }


def _runtime_section(report: Mapping[str, Any]) -> dict[str, Any]:
    capture = report.get("candidate_capture_rate")
    return {
        "enabled": bool(report.get("enabled", True)),
        "status": str(report.get("status") or "OK"),
        "last_batch_at": _max_text([str(row.get("observed_at") or "") for row in list(report.get("batches") or [])]),
        "batch_count": _int(report.get("source_batch_count"), 0),
        "observation_count": _int(report.get("observation_count"), 0),
        "episode_count": _int(report.get("episode_count"), 0),
        "candidate_captured_count": _int(report.get("candidate_captured_episode_count"), 0),
        "candidate_not_captured_count": _int(report.get("candidate_not_captured_episode_count"), 0),
        "label_complete_count": _int(report.get("exact_label_count"), 0) + _int(report.get("sampled_label_count"), 0),
        "label_sampled_count": _int(report.get("sampled_label_count"), 0),
        "label_insufficient_count": _int(report.get("insufficient_label_count"), 0),
        "capture_rate": capture,
        "qualification_status": str(report.get("qualification_status") or ""),
        "warning_codes": list(report.get("warning_codes") or []),
        "build_ms": _float(report.get("build_ms")),
        "checked_at": str(report.get("generated_at") or _now()),
    }


def _disabled_runtime_section(now: datetime) -> dict[str, Any]:
    return {
        "enabled": False,
        "status": "DISABLED",
        "last_batch_at": "",
        "batch_count": 0,
        "observation_count": 0,
        "episode_count": 0,
        "candidate_captured_count": 0,
        "candidate_not_captured_count": 0,
        "label_complete_count": 0,
        "label_sampled_count": 0,
        "label_insufficient_count": 0,
        "capture_rate": None,
        "qualification_status": "",
        "warning_codes": [],
        "build_ms": 0.0,
        "checked_at": now.isoformat(),
    }


def _current_price(row: Mapping[str, Any], raw: Mapping[str, Any]) -> tuple[float, str]:
    parsed = _float(row.get("current_price"))
    if parsed > 0:
        return parsed, "OPT10032_PARSED"
    recovered = _float(_raw_value(raw, "현재가", "current_price", "price"))
    if recovered > 0:
        return recovered, "OPT10032_RAW_RECOVERED"
    return 0.0, "MISSING"


def _instrument_filter_status(code: str, raw: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    if not _valid_stock_code(code):
        return "EXCLUDED_INVALID_CODE"
    text = " ".join(str(value or "") for value in [row.get("stock_name"), row.get("name"), _raw_value(raw, "종목명", "종목분류", "instrument_type", "security_type")]).upper()
    if _truthy(_raw_value(raw, "관리종목", "is_management", "is_under_administration")) or "관리" in text:
        return "EXCLUDED_MANAGEMENT"
    if "ETF" in text:
        return "EXCLUDED_ETF"
    if "ETN" in text:
        return "EXCLUDED_ETN"
    if "SPAC" in text or "스팩" in text:
        return "EXCLUDED_SPAC"
    if _truthy(_raw_value(raw, "common_stock", "is_common_stock")):
        return "INCLUDED_COMMON_STOCK"
    return "INCLUDED_UNKNOWN_TYPE"


def _market_side(raw: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    text = str(row.get("market_side") or _raw_value(raw, "시장구분", "시장", "market_side", "market") or "").upper()
    if "KOSPI" in text or "거래소" in text or text == "0":
        return "KOSPI"
    if "KOSDAQ" in text or "코스닥" in text or text == "10":
        return "KOSDAQ"
    return "UNKNOWN"


def _completeness_status(source: Mapping[str, Any], row_count: int, accepted_count: int) -> str:
    status = str(source.get("parser_status") or "").upper()
    if status in {"FAILED", "TIMEOUT", "EXPIRED", "SOURCE_ERROR"}:
        return "SOURCE_ERROR"
    if status in {"PARSE_ERROR", "MISSING_REQUIRED_FIELDS"}:
        return "PARSE_ERROR"
    if row_count <= 0:
        return "EMPTY"
    if accepted_count <= 0 or status == "PARTIAL":
        return "PARTIAL"
    return "COMPLETE"


def _detection_window(delay: int | None) -> str:
    if delay is None:
        return "NOT_CAPTURED"
    if delay < 0:
        return "PREEXISTING"
    if delay <= 60:
        return "WITHIN_1M"
    if delay <= 300:
        return "WITHIN_5M"
    if delay <= 900:
        return "WITHIN_15M"
    return "AFTER_15M"


def _primary_link_id(links: list[dict[str, Any]]) -> str:
    if not links:
        return ""
    order = {"PREEXISTING": 0, "WITHIN_1M": 1, "WITHIN_5M": 2, "WITHIN_15M": 3, "AFTER_15M": 4, "NOT_CAPTURED": 9}
    best = min(links, key=lambda row: (order.get(str(row.get("detection_window") or ""), 8), abs(_int(row.get("detection_delay_sec"), 999999)), str(row.get("candidate_instance_id") or "")))
    return str(best.get("link_id") or "")


def _label_quality(rows: list[Mapping[str, Any]]) -> str:
    if not rows:
        return "INSUFFICIENT"
    qualities = {str(row.get("source_quality") or row.get("label_quality") or "") for row in rows}
    resolutions = {str(row.get("source_resolution") or "") for row in rows}
    if "HIGH_RES_REALTIME" in qualities:
        return "HIGH_RES_REALTIME" if len(qualities) == 1 else "MIXED_HIGH_QUALITY"
    if "ONE_MINUTE_OHLC" in qualities or "ONE_MINUTE_CANDLE" in qualities or "ONE_MINUTE_CANDLE" in resolutions:
        return "ONE_MINUTE_CANDLE" if len(qualities) == 1 else "MIXED_HIGH_QUALITY"
    if "SAMPLED_TR" in qualities:
        return "SAMPLED_OPT10032"
    return "LOW_COVERAGE"


def _horizon_tolerance_min(horizon: int) -> int:
    if horizon <= 5:
        return 2
    if horizon <= 25:
        return 3
    return 5


def _market_close(anchor_at: datetime) -> datetime:
    return anchor_at.replace(hour=15, minute=30, second=0, microsecond=0)


def _missing_batch_count(rows: list[Mapping[str, Any]], gap_sec: int) -> int:
    times = [_as_datetime(row.get("observed_at")) for row in rows]
    times = [item for item in times if item is not None]
    if len(times) < 2:
        return 0
    missing = 0
    for left, right in zip(times, times[1:]):
        delta = (right - left).total_seconds()
        if delta > gap_sec / 2 and delta <= gap_sec:
            missing += 1
    return missing


def _opening_observed_at(trade_date: str, batch_time: str, created_at: str) -> str:
    text = str(batch_time or "")
    if len(text) == 5 and text[2] == ":":
        return f"{trade_date}T{text}:00"
    if text == "catchup":
        return f"{trade_date}T09:19:59"
    return str(created_at or f"{trade_date}T09:00:00")


def _opening_idempotency(trade_date: str, batch_time: str) -> str:
    compact = str(batch_time or "").replace(":", "")
    if compact:
        return f"opening_burst:seed:{trade_date}:{compact}"
    return ""


def _session_bucket(timestamp: str) -> str:
    parsed = _as_datetime(timestamp)
    if parsed is None:
        return "UNKNOWN"
    value = parsed.time()
    if value < dt_time(10, 0):
        return "OPENING"
    if value < dt_time(11, 30):
        return "MORNING"
    if value < dt_time(13, 0):
        return "MIDDAY"
    return "AFTERNOON"


def _rank_bucket(rank: int) -> str:
    if rank <= 0:
        return "UNKNOWN"
    if rank <= 10:
        return "1~10"
    if rank <= 30:
        return "11~30"
    if rank <= 50:
        return "31~50"
    if rank <= 100:
        return "51~100"
    return "101+"


def _counter_rows(values: Iterable[str]) -> list[dict[str, Any]]:
    counts = Counter(str(value or "UNKNOWN") for value in values)
    return [{"bucket": key, "count": value} for key, value in sorted(counts.items())]


def _order_counts_safe(_: Mapping[str, Any]) -> dict[str, int]:
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


def _ratio(numerator: int, denominator: int) -> dict[str, Any] | None:
    if denominator <= 0:
        return None
    rate = float(numerator) / float(denominator)
    return {"numerator": int(numerator), "denominator": int(denominator), "rate": rate, "pct": round(rate * 100.0, 4)}


def _report_id(prefix: str, trade_date: str, report_state: str, as_of: datetime, baseline: Mapping[str, Any]) -> str:
    if str(report_state).upper() == "LIVE_PREVIEW":
        material = "|".join([prefix, trade_date, "LIVE_PREVIEW", str(baseline.get("baseline_id") or ""), str(baseline.get("config_hash") or "")])
    else:
        material = "|".join([prefix, trade_date, str(report_state), as_of.isoformat(), str(baseline.get("config_hash") or "")])
    return f"{prefix}:{trade_date}:{str(report_state).lower()}:{hashlib.sha1(material.encode('utf-8')).hexdigest()[:16]}"


def _stable_id(prefix: str, *parts: Any) -> str:
    material = "|".join(str(part or "") for part in parts)
    return f"{prefix}:{hashlib.sha1(material.encode('utf-8')).hexdigest()[:20]}"


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _has_table(db: Any, table: str) -> bool:
    conn = getattr(db, "conn", None)
    if conn is None:
        return True
    try:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
        return row is not None
    except Exception:
        return True


def _raw_value(raw: Mapping[str, Any], *keys: str) -> Any:
    normalized = {_normalize_key(key): value for key, value in raw.items()}
    for key in keys:
        for option in {key, _normalize_key(key)}:
            if option in raw and raw.get(option) not in (None, ""):
                return raw.get(option)
            if option in normalized and normalized.get(option) not in (None, ""):
                return normalized.get(option)
    return None


def _normalize_key(value: Any) -> str:
    return str(value or "").strip().replace(" ", "").replace("_", "").lower()


def _valid_stock_code(code: str) -> bool:
    return bool(code and len(str(code)) == 6 and str(code).isdigit())


def _spread_pct(best_bid: float, best_ask: float) -> float | None:
    if best_bid <= 0 or best_ask <= 0:
        return None
    mid = (best_bid + best_ask) / 2.0
    return _pct(best_ask - best_bid, mid) if mid > 0 else None


def _pct(delta: float, base: float) -> float | None:
    if base <= 0:
        return None
    return round((float(delta) / float(base)) * 100.0, 6)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return abs(float(str(value).replace(",", "").replace("+", "").replace("%", "").strip()))
    except Exception:
        return default


def _signed_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace(",", "").replace("+", "").replace("%", "").strip())
    except Exception:
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(str(value).replace(",", "").replace("+", "").strip()))
    except Exception:
        return default


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "관리", "관리종목"}


def _is_int(value: Any) -> bool:
    try:
        int(str(value))
        return True
    except Exception:
        return False


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


def _max_text(values: Iterable[str]) -> str:
    clean = [str(value or "") for value in values if str(value or "")]
    return max(clean) if clean else ""


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


def _tuple_env(name: str, default: Iterable[str]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return tuple(str(item) for item in default)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


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


def _report_markdown(report: Mapping[str, Any]) -> str:
    capture = report.get("candidate_capture_rate") or {}
    rate = capture.get("pct") if isinstance(capture, Mapping) else None
    return "\n".join(
        [
            f"# Opportunity Benchmark {report.get('trade_date', '')}",
            "",
            f"- 상태: {report.get('report_state', '')}",
            f"- Qualification: {report.get('qualification_status', '')}",
            f"- 기준군 episode: {report.get('episode_count', 0)}",
            f"- Candidate 포착: {report.get('candidate_captured_episode_count', 0)}",
            f"- Candidate 미포착 Benchmark: {report.get('candidate_not_captured_episode_count', 0)}",
            f"- 포착률: {rate if rate is not None else 'null'}",
            f"- 라벨 불충분: {report.get('insufficient_label_count', 0)}",
        ]
    )


__all__ = [
    "OPPORTUNITY_BENCHMARK_BATCH_SCHEMA_VERSION",
    "OPPORTUNITY_BENCHMARK_OBSERVATION_SCHEMA_VERSION",
    "OPPORTUNITY_BENCHMARK_EPISODE_SCHEMA_VERSION",
    "OPPORTUNITY_BENCHMARK_CANDIDATE_LINK_SCHEMA_VERSION",
    "OPPORTUNITY_BENCHMARK_PRICE_OBSERVATION_SCHEMA_VERSION",
    "OPPORTUNITY_BENCHMARK_OUTCOME_SCHEMA_VERSION",
    "OPPORTUNITY_BENCHMARK_REPORT_SCHEMA_VERSION",
    "OpportunityBenchmarkConfig",
    "OpportunityBenchmarkService",
    "export_opportunity_benchmark_report",
]
