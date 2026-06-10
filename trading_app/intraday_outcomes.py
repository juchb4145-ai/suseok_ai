from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Optional

from storage.db import TradingDatabase


DEFAULT_HORIZONS_SEC = (60, 180, 300, 600, 1200)
PricePathProvider = Callable[[dict[str, Any], int, datetime], Iterable[dict[str, Any]] | dict[str, Any] | None]


@dataclass(frozen=True)
class IntradayOutcomeConfig:
    enabled: bool = True
    horizons_sec: tuple[int, ...] = DEFAULT_HORIZONS_SEC
    min_price_samples: int = 2
    tp_threshold_pct: float = 2.0
    fn_threshold_pct: float = 2.5
    fp_drawdown_pct: float = -1.5
    fp_return_pct: float = -1.0
    exit_giveback_pct: float = -2.0
    max_batch_size: int = 500


class IntradayOutcomeLabeler:
    def __init__(
        self,
        db: TradingDatabase,
        *,
        config: Optional[IntradayOutcomeConfig] = None,
        price_provider: Optional[PricePathProvider] = None,
    ) -> None:
        self.db = db
        self.config = config or IntradayOutcomeConfig()
        self.price_provider = price_provider

    def find_due_decisions(
        self,
        now: Optional[datetime] = None,
        horizons_sec: Optional[Iterable[int]] = None,
        *,
        trade_date: Optional[str] = None,
        limit: Optional[int] = None,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        evaluated_at = _iso(now or datetime.now())
        horizons = _normalize_horizons(horizons_sec or self.config.horizons_sec)
        return self.db.list_strategy_decision_events_due_for_outcomes(
            evaluated_at=evaluated_at,
            horizons_sec=horizons,
            trade_date=trade_date,
            limit=max(1, int(limit or self.config.max_batch_size)),
            force=force,
        )

    def build_outcome_for_decision(
        self,
        decision: dict[str, Any],
        horizon_sec: int,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        evaluated_at = _iso(now or datetime.now())
        horizon_sec = max(1, int(horizon_sec or decision.get("horizon_sec") or 1))
        metrics = self._build_price_metrics(decision, horizon_sec, now or datetime.now())
        base = _base_outcome(decision, horizon_sec=horizon_sec, evaluated_at=evaluated_at)
        if metrics.get("data_status") == "INSUFFICIENT":
            return {
                **base,
                **_metric_fields(metrics),
                "outcome_label": "INSUFFICIENT_OUTCOME_DATA",
                "outcome_reason": ",".join(metrics.get("data_quality_issues") or ["INSUFFICIENT_PRICE_PATH"]),
                "label_confidence": 0.0,
                "data_status": "INSUFFICIENT",
                "data_quality_issues": metrics.get("data_quality_issues") or [],
                "source": metrics.get("source") or "fallback",
                "details": _outcome_details(decision, metrics, self.config),
            }
        label = self.label_outcome(decision, metrics, horizon_sec)
        return {
            **base,
            **_metric_fields(metrics),
            "outcome_label": label["outcome_label"],
            "outcome_reason": label["outcome_reason"],
            "label_confidence": min(float(metrics.get("label_confidence") or 0.0), float(label["label_confidence"])),
            "data_status": metrics.get("data_status") or "OK",
            "data_quality_issues": metrics.get("data_quality_issues") or [],
            "source": metrics.get("source") or "realtime_tick",
            "details": _outcome_details(decision, metrics, self.config, label=label),
        }

    def label_outcome(self, decision: dict[str, Any], price_path: dict[str, Any] | Iterable[dict[str, Any]], horizon_sec: int) -> dict[str, Any]:
        metrics = price_path if isinstance(price_path, dict) else _metrics_from_samples(
            decision,
            list(price_path or []),
            horizon_sec,
            min_price_samples=self.config.min_price_samples,
            source="provider",
        )
        max_return = float(metrics.get("max_return_pct") or 0.0)
        max_drawdown = float(metrics.get("max_drawdown_pct") or 0.0)
        current_return = float(metrics.get("current_return_pct") or 0.0)
        early_max_return = float(metrics.get("early_max_return_pct") or 0.0)
        late_max_return = float(metrics.get("late_max_return_pct") or max_return)
        giveback = current_return - max_return
        action_type = str(decision.get("action_type") or "").upper()
        gate_status = str(decision.get("gate_status") or "").upper()
        reason_codes = _reason_codes(decision)
        risk_reason = _has_risk_block_reason(reason_codes)
        vi_or_limit = _has_vi_or_limit_reason(reason_codes)
        confidence_cap = 0.6 if vi_or_limit else 0.9

        if action_type == "EXIT_DECISION":
            if max_return >= self.config.tp_threshold_pct:
                return _label("EXIT_TOO_EARLY_CANDIDATE", "exit_followed_by_additional_rally", 0.75)
            return _label("GOOD_EXIT", "exit_followed_by_flat_or_downside", 0.8)
        if action_type == "HOLD":
            if giveback <= self.config.exit_giveback_pct:
                return _label("EXIT_TOO_LATE_CANDIDATE", "hold_gave_back_from_best_return", 0.75)
            if max_return >= self.config.tp_threshold_pct or current_return > 0:
                return _label("GOOD_HOLD", "hold_preserved_or_extended_upside", 0.75)
            return _label("NEUTRAL_OUTCOME", "hold_without_clear_followthrough", 0.55)

        if risk_reason and (gate_status in {"WAIT", "BLOCKED"} or action_type in {"WAIT", "BLOCK"}):
            if max_return >= 3.0:
                return _label("RISK_BLOCK_OPPORTUNITY_LOSS", "risk_block_followed_by_large_rally", confidence_cap)
            if max_return < self.config.fn_threshold_pct and (
                max_drawdown <= self.config.fp_drawdown_pct or current_return <= self.config.fp_return_pct
            ):
                return _label("RISK_BLOCK_EFFECTIVE", "risk_block_avoided_chase_or_downside", confidence_cap)

        if gate_status == "READY" or action_type in {"READY", "ENTRY_PLAN", "ENTRY_ORDER_INTENT"}:
            if max_return >= self.config.tp_threshold_pct and max_drawdown <= self.config.fp_drawdown_pct:
                return _label("ENTRY_TOO_EARLY_CANDIDATE", "ready_rallied_after_large_initial_drawdown", 0.7)
            if max_return >= self.config.tp_threshold_pct and max_drawdown > self.config.fp_drawdown_pct:
                if horizon_sec >= 600 and early_max_return < 1.0 and late_max_return >= self.config.tp_threshold_pct:
                    return _label("ENTRY_TIMING_SLOW_CONFIRMATION", "late_horizon_rally_after_weak_start", 0.65)
                return _label("EARLY_TRUE_POSITIVE", "ready_followed_by_rally_without_large_drawdown", 0.85)
            if max_drawdown <= self.config.fp_drawdown_pct or current_return <= self.config.fp_return_pct:
                return _label("EARLY_FALSE_POSITIVE", "ready_followed_by_drawdown_or_negative_return", 0.8)
            return _label("NEUTRAL_OUTCOME", "ready_without_clear_intraday_edge", 0.55)

        if gate_status in {"WAIT", "BLOCKED"} or action_type in {"WAIT", "BLOCK"}:
            if max_return >= self.config.fn_threshold_pct:
                return _label("EARLY_OPPORTUNITY_LOSS", "wait_or_block_followed_by_rally", 0.8)
            if max_return < 1.0 and max_drawdown <= self.config.fp_drawdown_pct:
                label = "GOOD_BLOCK" if gate_status == "BLOCKED" or action_type == "BLOCK" else "TRUE_NEGATIVE"
                return _label(label, "wait_or_block_avoided_weak_path", 0.75)
            return _label("NEUTRAL_OUTCOME", "wait_or_block_without_clear_edge", 0.55)

        return _label("NEUTRAL_OUTCOME", "decision_without_clear_outcome_rule", 0.5)

    def persist_outcomes(self, outcomes: Iterable[dict[str, Any]], *, force: bool = False) -> int:
        return self.db.save_strategy_decision_outcomes(list(outcomes or []), force=force)

    def build_summary(
        self,
        *,
        trade_date: Optional[str] = None,
        window_sec: Optional[int] = None,
        horizon_sec: Optional[int] = None,
    ) -> dict[str, Any]:
        return self.db.strategy_decision_outcome_summary(
            trade_date=trade_date,
            window_sec=window_sec,
            horizon_sec=horizon_sec,
        )

    def rebuild(
        self,
        *,
        trade_date: Optional[str] = None,
        horizons_sec: Optional[Iterable[int]] = None,
        horizon_sec: Optional[int] = None,
        limit: Optional[int] = None,
        force: bool = False,
        persist: bool = True,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        if not self.config.enabled:
            return {"status": "DISABLED", "persisted_count": 0, "outcome_count": 0, "items": []}
        horizons = [int(horizon_sec)] if horizon_sec else _normalize_horizons(horizons_sec or self.config.horizons_sec)
        decisions = self.find_due_decisions(
            now=now,
            horizons_sec=horizons,
            trade_date=trade_date,
            limit=limit or self.config.max_batch_size,
            force=force,
        )
        outcomes = [
            self.build_outcome_for_decision(decision, int(decision.get("horizon_sec") or horizon_sec or 0), now=now)
            for decision in decisions
        ]
        persisted_count = self.persist_outcomes(outcomes, force=force) if persist else 0
        return {
            "status": "OK",
            "trade_date": trade_date or "",
            "horizons_sec": horizons,
            "force": bool(force),
            "persist": bool(persist),
            "due_count": len(decisions),
            "outcome_count": len(outcomes),
            "persisted_count": persisted_count,
            "items": outcomes[:100],
        }

    def _build_price_metrics(self, decision: dict[str, Any], horizon_sec: int, now: datetime) -> dict[str, Any]:
        samples, source = self._load_price_samples(decision, horizon_sec, now)
        metrics = _metrics_from_samples(
            decision,
            samples,
            horizon_sec,
            min_price_samples=self.config.min_price_samples,
            source=source,
        )
        if metrics.get("data_status") != "INSUFFICIENT":
            return metrics
        fallback = self._fallback_metrics_from_reviews(decision, horizon_sec)
        return fallback or metrics

    def _load_price_samples(self, decision: dict[str, Any], horizon_sec: int, now: datetime) -> tuple[list[dict[str, Any]], str]:
        samples: list[dict[str, Any]] = []
        source = "fallback"
        if self.price_provider is not None:
            provided = self.price_provider(decision, horizon_sec, now)
            if isinstance(provided, dict):
                source = str(provided.get("source") or "provider")
                raw_samples = provided.get("samples") or []
            else:
                source = "provider"
                raw_samples = provided or []
            samples.extend(_normalize_samples(raw_samples, default_source=source))
        if not samples:
            samples.extend(self._postmarket_review_samples(decision, horizon_sec))
            if samples:
                source = "fallback"
        decision_price = _positive_float(decision.get("price"))
        if decision_price:
            decision_at = str(decision.get("decision_at") or "")
            samples.append({"at": decision_at, "price": decision_price, "source": "decision_event"})
        return samples, source

    def _postmarket_review_samples(self, decision: dict[str, Any], horizon_sec: int) -> list[dict[str, Any]]:
        trade_date = str(decision.get("trade_date") or "")
        code = str(decision.get("code") or "")
        decision_at = str(decision.get("decision_at") or "")
        if not trade_date or not code or not hasattr(self.db, "list_postmarket_review_items"):
            return []
        horizon_field = {
            60: "price_1m",
            180: "price_3m",
            300: "price_5m",
            600: "price_10m",
            1200: "price_close_or_last",
        }.get(int(horizon_sec))
        if not horizon_field:
            return []
        try:
            items = self.db.list_postmarket_review_items(trade_date, limit=1000)
        except Exception:
            return []
        for item in items:
            if str(item.get("symbol") or item.get("code") or "") != code:
                continue
            base = _positive_float(item.get("base_price")) or _positive_float(decision.get("price"))
            horizon_price = _positive_float(item.get(horizon_field))
            if not base or not horizon_price:
                continue
            return [
                {"at": decision_at, "price": base, "source": "postmarket_review"},
                {"at": _plus_seconds(decision_at, horizon_sec), "price": horizon_price, "source": "postmarket_review"},
            ]
        return []

    def _fallback_metrics_from_reviews(self, decision: dict[str, Any], horizon_sec: int) -> Optional[dict[str, Any]]:
        if not hasattr(self.db, "list_trade_reviews_for_analysis"):
            return None
        trade_date = str(decision.get("trade_date") or "")
        code = str(decision.get("code") or "")
        candidate_id = decision.get("candidate_id")
        try:
            reviews = self.db.list_trade_reviews_for_analysis()
        except Exception:
            return None
        for review in reviews:
            if trade_date and getattr(review, "trade_date", "") != trade_date:
                continue
            if code and getattr(review, "code", "") != code:
                continue
            if candidate_id is not None and getattr(review, "candidate_id", None) != candidate_id:
                continue
            base = _positive_float(decision.get("price")) or _positive_float(getattr(review, "entry_price", 0))
            if not base:
                continue
            max_return = _review_return_for_horizon(review, horizon_sec)
            if max_return is None:
                continue
            max_drawdown = _positive_or_zero(getattr(review, "max_drawdown_20m", None))
            if max_drawdown > 0:
                max_drawdown = -max_drawdown
            current_return = _positive_or_zero(getattr(review, "realized_return_pct", None))
            return {
                "price_at_decision": base,
                "price_at_horizon": base * (1 + current_return / 100.0),
                "max_price_after_decision": base * (1 + float(max_return) / 100.0),
                "min_price_after_decision": base * (1 + float(max_drawdown) / 100.0),
                "max_return_pct": float(max_return),
                "max_drawdown_pct": float(max_drawdown),
                "current_return_pct": float(current_return),
                "early_max_return_pct": float(max_return),
                "late_max_return_pct": float(max_return),
                "sample_count": 2,
                "source": "fallback",
                "data_status": "SPARSE",
                "data_quality_issues": ["FALLBACK_REVIEW_METRICS"],
                "label_confidence": 0.45,
            }
        return None


class ThemeLabFlowPricePathProvider:
    """Build a decision-time price path from persisted ThemeLab flow snapshots.

    The provider is read-only and only returns samples between decision_at and
    decision_at + horizon_sec, so outcome labeling keeps the look-ahead boundary
    explicit.
    """

    def __init__(self, db: TradingDatabase, *, max_rows: int = 300) -> None:
        self.db = db
        self.max_rows = max(1, int(max_rows or 300))
        self._row_sample_cache: dict[int, list[dict[str, Any]]] = {}

    def __call__(self, decision: dict[str, Any], horizon_sec: int, now: datetime) -> dict[str, Any]:
        code = str(decision.get("code") or "").strip()
        decision_at = _parse_dt(decision.get("decision_at"))
        if not code or decision_at is None:
            return {"source": "theme_lab_flow_snapshot", "samples": []}
        horizon_at = decision_at + timedelta(seconds=max(1, int(horizon_sec or 1)))
        rows = self._load_rows(decision_at, min(horizon_at, now))
        samples: list[dict[str, Any]] = []
        seen: set[tuple[str, float]] = set()
        for row in rows:
            for sample in self._samples_for_row(row):
                if sample.get("code") != code:
                    continue
                sample_dt = _parse_dt(sample.get("at"))
                if sample_dt is None or sample_dt < decision_at or sample_dt > horizon_at:
                    continue
                price = _positive_float(sample.get("price"))
                if not price:
                    continue
                key = (sample_dt.isoformat(timespec="seconds"), float(price))
                if key in seen:
                    continue
                seen.add(key)
                samples.append({"at": key[0], "price": price, "source": sample.get("source") or "theme_lab_flow_snapshot"})
        samples.sort(key=lambda item: item["at"])
        return {"source": "theme_lab_flow_snapshot", "samples": samples}

    def _load_rows(self, start_at: datetime, end_at: datetime) -> list[dict[str, Any]]:
        if not hasattr(self.db, "conn"):
            return []
        try:
            exists = self.db.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='theme_lab_flow_snapshots'"
            ).fetchone()
            if not exists:
                return []
            return [
                dict(row)
                for row in self.db.conn.execute(
                    """
                    SELECT id, calculated_at, watchset_snapshots_json, gate_decisions_json,
                           condition_hit_snapshots_json, theme_condition_snapshots_json,
                           theme_rankings_json
                    FROM theme_lab_flow_snapshots
                    WHERE calculated_at >= ?
                      AND calculated_at <= ?
                    ORDER BY calculated_at ASC, id ASC
                    LIMIT ?
                    """,
                    (start_at.isoformat(timespec="seconds"), end_at.isoformat(timespec="seconds"), self.max_rows),
                ).fetchall()
            ]
        except Exception:
            return []

    def _samples_for_row(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        row_id = int(row.get("id") or 0)
        if row_id in self._row_sample_cache:
            return self._row_sample_cache[row_id]
        calculated_at = str(row.get("calculated_at") or "")
        samples: list[dict[str, Any]] = []
        for column, source in (
            ("watchset_snapshots_json", "theme_lab_flow:watchset"),
            ("gate_decisions_json", "theme_lab_flow:gate_decision"),
            ("condition_hit_snapshots_json", "theme_lab_flow:condition_hit"),
            ("theme_condition_snapshots_json", "theme_lab_flow:theme_condition"),
            ("theme_rankings_json", "theme_lab_flow:theme_ranking"),
        ):
            payload = _safe_json(row.get(column), [])
            samples.extend(_extract_price_samples(payload, at=calculated_at, source=source))
        self._row_sample_cache[row_id] = samples
        return samples


def config_from_settings(settings: Any) -> IntradayOutcomeConfig:
    return IntradayOutcomeConfig(
        enabled=bool(getattr(settings, "intraday_outcome_enabled", True)),
        horizons_sec=_normalize_horizons(getattr(settings, "intraday_outcome_horizons_sec", DEFAULT_HORIZONS_SEC)),
        min_price_samples=max(1, int(getattr(settings, "intraday_outcome_min_price_samples", 2))),
        tp_threshold_pct=float(getattr(settings, "intraday_outcome_tp_threshold_pct", 2.0)),
        fn_threshold_pct=float(getattr(settings, "intraday_outcome_fn_threshold_pct", 2.5)),
        fp_drawdown_pct=float(getattr(settings, "intraday_outcome_fp_drawdown_pct", -1.5)),
        fp_return_pct=float(getattr(settings, "intraday_outcome_fp_return_pct", -1.0)),
        exit_giveback_pct=float(getattr(settings, "intraday_outcome_exit_giveback_pct", -2.0)),
        max_batch_size=max(1, int(getattr(settings, "intraday_outcome_max_batch_size", 500))),
    )


def _base_outcome(decision: dict[str, Any], *, horizon_sec: int, evaluated_at: str) -> dict[str, Any]:
    decision_id = str(decision.get("decision_id") or "")
    return {
        "outcome_id": f"outcome:{decision_id}:{horizon_sec}",
        "decision_id": decision_id,
        "trade_date": str(decision.get("trade_date") or ""),
        "code": str(decision.get("code") or ""),
        "candidate_id": decision.get("candidate_id"),
        "candidate_instance_id": str(decision.get("candidate_instance_id") or ""),
        "candidate_generation_seq": int(decision.get("candidate_generation_seq") or 0),
        "decision_at": str(decision.get("decision_at") or ""),
        "evaluated_at": evaluated_at,
        "horizon_sec": horizon_sec,
    }


def _metric_fields(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "price_at_decision": metrics.get("price_at_decision"),
        "price_at_horizon": metrics.get("price_at_horizon"),
        "max_price_after_decision": metrics.get("max_price_after_decision"),
        "min_price_after_decision": metrics.get("min_price_after_decision"),
        "max_return_pct": metrics.get("max_return_pct"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "current_return_pct": metrics.get("current_return_pct"),
    }


def _metrics_from_samples(
    decision: dict[str, Any],
    samples: list[dict[str, Any]],
    horizon_sec: int,
    *,
    min_price_samples: int,
    source: str,
) -> dict[str, Any]:
    issues: list[str] = []
    decision_at = _parse_dt(decision.get("decision_at"))
    if decision_at is None:
        issues.append("MISSING_DECISION_AT")
    if not str(decision.get("code") or ""):
        issues.append("MISSING_CODE")
    normalized = _normalize_samples(samples, default_source=source)
    if decision_at is not None:
        horizon_at = decision_at + timedelta(seconds=max(1, int(horizon_sec or 1)))
        timed = [sample for sample in normalized if sample.get("dt") is not None]
        if timed:
            normalized = [sample for sample in timed if decision_at <= sample["dt"] <= horizon_at]
    base = _positive_float(decision.get("price")) or (normalized[0]["price"] if normalized else None)
    if not base:
        issues.append("MISSING_DECISION_PRICE")
    prices = [float(sample["price"]) for sample in normalized if _positive_float(sample.get("price"))]
    if base and not prices:
        prices = [base]
    if len(prices) < max(1, int(min_price_samples or 1)):
        issues.append("INSUFFICIENT_PRICE_SAMPLES")
    if issues:
        return {
            "price_at_decision": base,
            "sample_count": len(prices),
            "source": source or "fallback",
            "data_status": "INSUFFICIENT",
            "data_quality_issues": list(dict.fromkeys(issues)),
        }
    max_price = max(prices)
    min_price = min(prices)
    horizon_price = prices[-1]
    max_return = ((max_price - base) / base) * 100.0
    max_drawdown = ((min_price - base) / base) * 100.0
    current_return = ((horizon_price - base) / base) * 100.0
    early_prices = prices
    late_prices = prices
    if decision_at is not None and normalized and any(sample.get("dt") is not None for sample in normalized):
        midpoint = decision_at + timedelta(seconds=max(1, int(horizon_sec or 1)) / 2)
        early_prices = [sample["price"] for sample in normalized if sample.get("dt") is not None and sample["dt"] <= midpoint]
        late_prices = [sample["price"] for sample in normalized if sample.get("dt") is not None and sample["dt"] > midpoint]
        early_prices = early_prices or prices
        late_prices = late_prices or prices
    status = "OK" if len(prices) > max(2, min_price_samples) else "SPARSE"
    confidence = 0.8 if status == "OK" else 0.55
    if source == "fallback":
        confidence = min(confidence, 0.45)
    return {
        "price_at_decision": base,
        "price_at_horizon": horizon_price,
        "max_price_after_decision": max_price,
        "min_price_after_decision": min_price,
        "max_return_pct": max_return,
        "max_drawdown_pct": max_drawdown,
        "current_return_pct": current_return,
        "early_max_return_pct": ((max(early_prices) - base) / base) * 100.0,
        "late_max_return_pct": ((max(late_prices) - base) / base) * 100.0,
        "sample_count": len(prices),
        "source": source or "provider",
        "data_status": status,
        "data_quality_issues": [] if status == "OK" else ["SPARSE_PRICE_SAMPLES"],
        "label_confidence": confidence,
    }


def _normalize_samples(samples: Iterable[dict[str, Any]], *, default_source: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for sample in samples or []:
        if not isinstance(sample, dict):
            continue
        price = _positive_float(sample.get("price") or sample.get("current_price") or sample.get("close"))
        if not price:
            continue
        at = sample.get("at") or sample.get("time") or sample.get("created_at") or sample.get("timestamp")
        normalized.append(
            {
                "at": str(at or ""),
                "dt": _parse_dt(at),
                "price": price,
                "source": str(sample.get("source") or default_source or "provider"),
            }
        )
    normalized.sort(key=lambda item: item.get("dt") or datetime.min)
    return normalized


def _safe_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _extract_price_samples(payload: Any, *, at: str, source: str) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            samples.extend(_extract_price_samples(item, at=at, source=source))
        return samples
    if not isinstance(payload, dict):
        return samples

    code = _snapshot_code(payload)
    price = _positive_float(
        payload.get("current_price")
        or payload.get("price")
        or payload.get("last_price")
        or payload.get("close")
        or payload.get("trade_price")
    )
    sample_at = str(payload.get("calculated_at") or payload.get("timestamp") or payload.get("created_at") or at or "")
    if code and price:
        samples.append({"code": code, "at": sample_at, "price": price, "source": source})

    for key in (
        "member_hits",
        "top_stocks",
        "stocks",
        "items",
        "watchset_snapshots",
        "gate_decisions",
        "condition_hit_snapshots",
        "theme_condition_snapshots",
        "theme_rankings",
    ):
        nested = payload.get(key)
        if nested:
            samples.extend(_extract_price_samples(nested, at=sample_at or at, source=source))
    return samples


def _snapshot_code(payload: dict[str, Any]) -> str:
    for key in ("code", "symbol", "stock_code"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _outcome_details(
    decision: dict[str, Any],
    metrics: dict[str, Any],
    config: IntradayOutcomeConfig,
    *,
    label: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "decision": {
            "gate_status": decision.get("gate_status"),
            "action_type": decision.get("action_type"),
            "action_result": decision.get("action_result"),
            "reason_codes": _reason_codes(decision),
            "reason_family": decision.get("reason_family"),
        },
        "metrics": {
            "sample_count": metrics.get("sample_count"),
            "early_max_return_pct": metrics.get("early_max_return_pct"),
            "late_max_return_pct": metrics.get("late_max_return_pct"),
            "source": metrics.get("source"),
        },
        "thresholds": {
            "tp_threshold_pct": config.tp_threshold_pct,
            "fn_threshold_pct": config.fn_threshold_pct,
            "fp_drawdown_pct": config.fp_drawdown_pct,
            "fp_return_pct": config.fp_return_pct,
            "exit_giveback_pct": config.exit_giveback_pct,
        },
        "label": label or {},
    }


def _label(outcome_label: str, outcome_reason: str, confidence: float) -> dict[str, Any]:
    return {
        "outcome_label": outcome_label,
        "outcome_reason": outcome_reason,
        "label_confidence": float(confidence),
    }


def _reason_codes(decision: dict[str, Any]) -> list[str]:
    raw = decision.get("reason_codes") or []
    if isinstance(raw, str):
        return [part.strip().upper() for part in raw.replace(";", ",").split(",") if part.strip()]
    return [str(item).upper() for item in raw if str(item)]


def _has_risk_block_reason(reason_codes: list[str]) -> bool:
    needles = (
        "LATE_CHASE",
        "LATE_LAGGARD",
        "CHASE_RISK",
        "VI_ACTIVE",
        "VI_COOLDOWN",
        "UPPER_LIMIT_NEAR",
        "HIGH_RETURN",
    )
    return any(any(needle in reason for needle in needles) for reason in reason_codes)


def _has_vi_or_limit_reason(reason_codes: list[str]) -> bool:
    return any("VI_" in reason or "UPPER_LIMIT" in reason for reason in reason_codes)


def _review_return_for_horizon(review: Any, horizon_sec: int) -> Optional[float]:
    if horizon_sec <= 300:
        value = getattr(review, "max_return_5m", None)
    elif horizon_sec <= 600:
        value = getattr(review, "max_return_10m", None)
    else:
        value = getattr(review, "max_return_20m", None)
    return _positive_or_zero(value) if value is not None else None


def _normalize_horizons(value: Any) -> tuple[int, ...]:
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.split(",")]
    else:
        raw_items = list(value or [])
    horizons: list[int] = []
    for item in raw_items:
        try:
            horizon = int(item)
        except (TypeError, ValueError):
            continue
        if horizon > 0 and horizon not in horizons:
            horizons.append(horizon)
    return tuple(horizons or DEFAULT_HORIZONS_SEC)


def _positive_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_or_zero(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _plus_seconds(value: str, seconds: int) -> str:
    parsed = _parse_dt(value)
    if parsed is None:
        return ""
    return _iso(parsed + timedelta(seconds=max(1, int(seconds or 1))))


def _iso(value: datetime) -> str:
    return value.replace(tzinfo=None).isoformat(timespec="seconds")
