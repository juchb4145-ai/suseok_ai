from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Iterable

from trading.strategy.candidates import normalize_code
from trading.theme_engine.cohort import ThemeCohortSnapshot
from trading.theme_engine.signals import LiveSeedSignal


@dataclass(frozen=True)
class TurnoverObservation:
    code: str
    observed_at: str
    cumulative_turnover_krw: float = 0.0
    theme_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StockTurnoverFlow:
    code: str
    observed_at: str
    cumulative_turnover_krw: float = 0.0
    turnover_delta_1m: float = 0.0
    turnover_delta_3m: float = 0.0
    turnover_delta_5m: float = 0.0
    turnover_speed_1m: float = 0.0
    turnover_speed_3m: float = 0.0
    turnover_acceleration: float = 0.0
    turnover_flow_percentile: float = 0.0
    flow_freshness: str = "FRESH"
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ThemeTurnoverFlow:
    theme_id: str
    observed_at: str
    theme_turnover_delta_1m: float = 0.0
    theme_turnover_delta_3m: float = 0.0
    theme_turnover_delta_5m: float = 0.0
    theme_turnover_acceleration: float = 0.0
    theme_flow_share: float = 0.0
    theme_flow_share_delta: float = 0.0
    theme_flow_percentile: float = 0.0
    fresh_flow_member_count: int = 0
    fresh_flow_coverage_ratio: float = 0.0
    reason_codes: tuple[str, ...] = ()


class TurnoverFlowTracker:
    def __init__(self, *, max_history: int = 20) -> None:
        self.max_history = max(2, int(max_history or 20))
        self._history: dict[str, list[TurnoverObservation]] = {}
        self._latest_stock_flow: dict[str, StockTurnoverFlow] = {}
        self._previous_theme_share: dict[str, float] = {}

    def observe(self, observation: TurnoverObservation | LiveSeedSignal | dict[str, Any]) -> StockTurnoverFlow | None:
        item = _observation(observation)
        if not item.code:
            return None
        history = self._history.setdefault(item.code, [])
        if history and item.observed_at == history[-1].observed_at:
            previous = self._latest_stock_flow.get(item.code)
            if previous is not None:
                self._latest_stock_flow[item.code] = replace(
                    previous,
                    reason_codes=tuple(_dedupe([*previous.reason_codes, "TURNOVER_DUPLICATE_OBSERVATION"])),
                )
                return self._latest_stock_flow[item.code]
        if history and _is_out_of_order(history[-1], item):
            previous = self._latest_stock_flow.get(item.code)
            if previous is not None:
                self._latest_stock_flow[item.code] = replace(
                    previous,
                    reason_codes=tuple(_dedupe([*previous.reason_codes, "TURNOVER_TIMESTAMP_OUT_OF_ORDER"])),
                    flow_freshness="STALE",
                )
                return self._latest_stock_flow[item.code]
        if history and item.cumulative_turnover_krw < history[-1].cumulative_turnover_krw:
            item = replace(item, reason_codes=tuple(_dedupe([*item.reason_codes, "TURNOVER_CUMULATIVE_RESET"])))
        history.append(item)
        del history[:-self.max_history]
        flow = self._stock_flow(item.code, history)
        self._latest_stock_flow[item.code] = flow
        self._recompute_percentiles()
        return self._latest_stock_flow[item.code]

    def observe_signals(self, signals: Iterable[LiveSeedSignal], *, observed_at: str = "") -> dict[str, StockTurnoverFlow]:
        result: dict[str, StockTurnoverFlow] = {}
        for signal in signals:
            if not _signal_flow_eligible(signal):
                continue
            item = TurnoverObservation(
                code=signal.code,
                observed_at=signal.last_seen_at or signal.tick_at or signal.observed_at or observed_at,
                cumulative_turnover_krw=signal.turnover_krw,
            )
            flow = self.observe(item)
            if flow is not None:
                result[flow.code] = flow
        return result

    def latest_stock_flow(self, code: str) -> StockTurnoverFlow | None:
        return self._latest_stock_flow.get(normalize_code(code))

    def theme_flows(self, cohorts: Iterable[ThemeCohortSnapshot], *, observed_at: str = "") -> dict[str, ThemeTurnoverFlow]:
        totals: dict[str, float] = {}
        members: dict[str, int] = {}
        coverage: dict[str, tuple[int, int]] = {}
        for cohort in cohorts:
            theme_id = str(cohort.theme_id or "")
            if not theme_id:
                continue
            total = 0.0
            fresh_count = 0
            signal_count = len(cohort.signals)
            for signal in cohort.signals:
                flow = self.latest_stock_flow(signal.code)
                if flow is None:
                    continue
                total += max(0.0, flow.turnover_delta_1m)
                if flow.flow_freshness == "FRESH":
                    fresh_count += 1
            totals[theme_id] = total
            members[theme_id] = signal_count
            coverage[theme_id] = (fresh_count, signal_count)
        market_total = sum(totals.values())
        percentile_by_theme = _rank_percentiles(totals)
        result: dict[str, ThemeTurnoverFlow] = {}
        for theme_id, delta_1m in totals.items():
            previous_share = self._previous_theme_share.get(theme_id, 0.0)
            share = delta_1m / market_total if market_total > 0 else 0.0
            fresh_count, signal_count = coverage.get(theme_id, (0, 0))
            result[theme_id] = ThemeTurnoverFlow(
                theme_id=theme_id,
                observed_at=observed_at,
                theme_turnover_delta_1m=round(delta_1m, 4),
                theme_turnover_delta_3m=round(sum(max(0.0, self.latest_stock_flow(signal.code).turnover_delta_3m) for cohort in cohorts if cohort.theme_id == theme_id for signal in cohort.signals if self.latest_stock_flow(signal.code)), 4),
                theme_turnover_delta_5m=round(sum(max(0.0, self.latest_stock_flow(signal.code).turnover_delta_5m) for cohort in cohorts if cohort.theme_id == theme_id for signal in cohort.signals if self.latest_stock_flow(signal.code)), 4),
                theme_turnover_acceleration=round(sum(self.latest_stock_flow(signal.code).turnover_acceleration for cohort in cohorts if cohort.theme_id == theme_id for signal in cohort.signals if self.latest_stock_flow(signal.code)), 4),
                theme_flow_share=round(share, 6),
                theme_flow_share_delta=round(share - previous_share, 6),
                theme_flow_percentile=round(percentile_by_theme.get(theme_id, 0.0), 4),
                fresh_flow_member_count=fresh_count,
                fresh_flow_coverage_ratio=round(fresh_count / (signal_count or 1), 4),
                reason_codes=("TURNOVER_FLOW_OBSERVE_ONLY",),
            )
            self._previous_theme_share[theme_id] = share
        return result

    def _stock_flow(self, code: str, history: list[TurnoverObservation]) -> StockTurnoverFlow:
        latest = history[-1]
        delta_1m = _delta_since(history, latest, 60)
        delta_3m = _delta_since(history, latest, 180)
        delta_5m = _delta_since(history, latest, 300)
        speed_1m = delta_1m
        speed_3m = delta_3m / 3.0
        acceleration = speed_1m - speed_3m
        reasons = list(latest.reason_codes)
        if any("TURNOVER_CUMULATIVE_RESET" in item.reason_codes or "TURNOVER_RESET" in item.reason_codes for item in history[-2:]):
            reasons.append("TURNOVER_RESET_DETECTED")
        freshness = "STALE" if any(reason in reasons for reason in {"TURNOVER_TIMESTAMP_OUT_OF_ORDER", "TURNOVER_STALE_OBSERVATION"}) else "FRESH"
        return StockTurnoverFlow(
            code=code,
            observed_at=latest.observed_at,
            cumulative_turnover_krw=latest.cumulative_turnover_krw,
            turnover_delta_1m=round(delta_1m, 4),
            turnover_delta_3m=round(delta_3m, 4),
            turnover_delta_5m=round(delta_5m, 4),
            turnover_speed_1m=round(speed_1m, 4),
            turnover_speed_3m=round(speed_3m, 4),
            turnover_acceleration=round(acceleration, 4),
            flow_freshness=freshness,
            reason_codes=tuple(_dedupe(reasons)),
        )

    def _recompute_percentiles(self) -> None:
        scores = {code: flow.turnover_delta_1m for code, flow in self._latest_stock_flow.items()}
        percentiles = _rank_percentiles(scores)
        for code, flow in list(self._latest_stock_flow.items()):
            self._latest_stock_flow[code] = replace(flow, turnover_flow_percentile=round(percentiles.get(code, 0.0), 4))


def _observation(value: TurnoverObservation | LiveSeedSignal | dict[str, Any]) -> TurnoverObservation:
    if isinstance(value, TurnoverObservation):
        return replace(value, code=normalize_code(value.code))
    if isinstance(value, LiveSeedSignal):
        signal = value.normalized()
        return TurnoverObservation(
            code=signal.code,
            observed_at=signal.last_seen_at or signal.tick_at or signal.observed_at,
            cumulative_turnover_krw=signal.turnover_krw,
            reason_codes=signal.reason_codes,
        )
    raw = dict(value or {})
    return TurnoverObservation(
        code=normalize_code(raw.get("code") or raw.get("stock_code") or ""),
        observed_at=str(raw.get("observed_at") or raw.get("timestamp") or ""),
        cumulative_turnover_krw=_float(raw.get("cumulative_turnover_krw") or raw.get("turnover_krw") or raw.get("trade_value")),
        theme_ids=tuple(str(item) for item in list(raw.get("theme_ids") or []) if str(item)),
        reason_codes=tuple(_dedupe(raw.get("reason_codes") or ())),
    )


def _delta_since(history: list[TurnoverObservation], latest: TurnoverObservation, seconds: int) -> float:
    latest_time = _parse_time(latest.observed_at)
    if latest_time is None:
        previous = history[-2] if len(history) >= 2 else latest
        return max(0.0, latest.cumulative_turnover_krw - previous.cumulative_turnover_krw)
    baseline = history[0]
    for item in reversed(history[:-1]):
        parsed = _parse_time(item.observed_at)
        if parsed is not None and (latest_time - parsed).total_seconds() >= seconds:
            baseline = item
            break
        baseline = item
    delta = latest.cumulative_turnover_krw - baseline.cumulative_turnover_krw
    if delta < 0:
        return 0.0
    return delta


def _signal_flow_eligible(signal: LiveSeedSignal) -> bool:
    normalized = signal.normalized()
    freshness = str(normalized.freshness_status or "").upper()
    if not normalized.active:
        return False
    if freshness in {"STALE", "TR_BACKFILL_ONLY", "MISSING"}:
        return False
    if normalized.tr_backfill_valid and not normalized.realtime_valid:
        return False
    return bool(normalized.realtime_valid)


def _is_out_of_order(previous: TurnoverObservation, current: TurnoverObservation) -> bool:
    previous_time = _parse_time(previous.observed_at)
    current_time = _parse_time(current.observed_at)
    return previous_time is not None and current_time is not None and current_time < previous_time


def _rank_percentiles(values: dict[str, float]) -> dict[str, float]:
    positives = sorted(((key, value) for key, value in values.items() if value > 0), key=lambda item: item[1])
    total = len(positives)
    if total <= 0:
        return {key: 0.0 for key in values}
    return {key: ((index + 1) / total) * 100.0 for index, (key, _value) in enumerate(positives)}


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _float(value: Any) -> float:
    try:
        return float(str(value or "0").replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


__all__ = [
    "StockTurnoverFlow",
    "ThemeTurnoverFlow",
    "TurnoverFlowTracker",
    "TurnoverObservation",
]
