from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, time
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Optional

from storage.db import TradingDatabase
from trading.broker.models import new_message_id, utc_timestamp
from trading_app.dry_run_performance import DryRunPerformanceAnalyzer, DryRunPerformanceConfig
from trading_app.live_sim_canary_performance import LiveSimCanaryPerformanceAnalyzer


REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports" / "exit_policy_validation"

EXIT_STOP_LOSS = "STOP_LOSS"
EXIT_TAKE_PROFIT = "TAKE_PROFIT"
EXIT_TRAILING_STOP = "TRAILING_STOP"
EXIT_PARTIAL_TAKE_PROFIT = "PARTIAL_TAKE_PROFIT"
EXIT_TIME = "TIME_EXIT"
EXIT_THEME_WEAK = "THEME_WEAK_EXIT"
EXIT_LEADER_COLLAPSE = "LEADER_COLLAPSE_EXIT"
EXIT_INDEX_WEAK = "INDEX_WEAK_EXIT"
EXIT_BREADTH_COLLAPSE = "BREADTH_COLLAPSE_EXIT"
EXIT_MARKET_CLOSE = "MARKET_CLOSE_EXIT"
EXIT_NO_EXIT = "NO_EXIT"
EXIT_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

CONTEXT_EXIT_TYPES = {
    EXIT_THEME_WEAK,
    EXIT_LEADER_COLLAPSE,
    EXIT_INDEX_WEAK,
    EXIT_BREADTH_COLLAPSE,
}

COMPARISON_SHADOW_BETTER = "SHADOW_BETTER"
COMPARISON_SHADOW_WORSE = "SHADOW_WORSE"
COMPARISON_SAME = "SAME_AS_ACTUAL"
COMPARISON_INCOMPARABLE = "INCOMPARABLE"
COMPARISON_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True)
class ExitPolicyValidationConfig:
    baseline_stop_loss_pct: float = -2.0
    baseline_take_profit_pct: float = 5.0
    baseline_max_hold_minutes: int = 60
    commission_bp_per_side: float = 1.5
    sell_tax_bp: float = 15.0
    primary_slippage_bp: float = 10.0
    comparison_tolerance_pct: float = 0.05
    min_candidate_samples: int = 3
    large_giveback_pct: float = 1.0
    stale_tick_gap_sec: float = 60.0
    market_close_hhmm: str = "15:20"


@dataclass(frozen=True)
class ExitPolicyScenario:
    scenario_id: str
    description_ko: str
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    max_hold_minutes: Optional[int] = None
    trailing_start_mfe_pct: Optional[float] = None
    trailing_giveback_pct: Optional[float] = None
    first_take_profit_pct: Optional[float] = None
    first_exit_percent: float = 0.0
    trailing_after_first_tp: bool = False
    final_take_profit_pct: Optional[float] = None
    theme_weak_exit: bool = False
    leader_collapse_exit: bool = False
    index_weak_exit: bool = False
    breadth_collapse_exit: bool = False
    confirmation_cycles: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PricePoint:
    at: str
    price: float
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class ContextPoint:
    at: str
    theme_status: str = ""
    breadth_status: str = ""
    leader_return_pct: Optional[float] = None
    leader_support_broken: bool = False
    index_status: str = ""
    index_return_pct: Optional[float] = None
    market_status: str = ""
    market_risk_status: str = ""
    risk_reason_codes: tuple[str, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass
class ShadowExitResult:
    scenario_id: str
    exit_trigger_type: str
    exit_time: str = ""
    exit_price: Optional[float] = None
    hold_minutes: Optional[float] = None
    gross_return_pct: Optional[float] = None
    net_return_pct: Optional[float] = None
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None
    giveback_pct: Optional[float] = None
    risk_avoided_pct: Optional[float] = None
    missed_upside_pct: Optional[float] = None
    realized_pnl_krw: Optional[float] = None
    estimated_cost_krw: Optional[float] = None
    data_quality: str = "OK"
    reason_codes: list[str] | None = None
    details_json: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes or [])
        payload["details_json"] = dict(self.details_json or {})
        return payload


def default_exit_policy_scenarios(config: ExitPolicyValidationConfig | None = None) -> list[ExitPolicyScenario]:
    cfg = config or ExitPolicyValidationConfig()
    return [
        ExitPolicyScenario(
            scenario_id="baseline_current",
            description_ko="현재 live_sim_exit_guard 기준값을 분석 전용 baseline으로 사용",
            stop_loss_pct=cfg.baseline_stop_loss_pct,
            take_profit_pct=cfg.baseline_take_profit_pct,
            max_hold_minutes=cfg.baseline_max_hold_minutes,
        ),
        ExitPolicyScenario(
            scenario_id="tight_stop_fast_profit",
            description_ko="짧은 손절과 빠른 익절",
            stop_loss_pct=-1.2,
            take_profit_pct=2.0,
            max_hold_minutes=20,
        ),
        ExitPolicyScenario(
            scenario_id="balanced_intraday",
            description_ko="장중 균형형 손익비와 보유시간",
            stop_loss_pct=-1.5,
            take_profit_pct=3.0,
            max_hold_minutes=30,
        ),
        ExitPolicyScenario(
            scenario_id="wide_stop_theme_runner",
            description_ko="강한 테마 runner를 더 넓게 보유하되 테마/대장주 붕괴 시 조기청산",
            stop_loss_pct=-2.5,
            take_profit_pct=5.0,
            max_hold_minutes=60,
            theme_weak_exit=True,
            leader_collapse_exit=True,
            confirmation_cycles=1,
        ),
        ExitPolicyScenario(
            scenario_id="trailing_after_mfe",
            description_ko="MFE 2% 이후 1% 되돌림 트레일링",
            stop_loss_pct=-1.5,
            take_profit_pct=None,
            trailing_start_mfe_pct=2.0,
            trailing_giveback_pct=1.0,
            max_hold_minutes=45,
        ),
        ExitPolicyScenario(
            scenario_id="partial_take_profit",
            description_ko="2%에서 50% 부분익절 후 잔여분 트레일링",
            stop_loss_pct=-1.5,
            first_take_profit_pct=2.0,
            first_exit_percent=50.0,
            trailing_after_first_tp=True,
            trailing_giveback_pct=1.0,
            final_take_profit_pct=5.0,
            max_hold_minutes=60,
        ),
        ExitPolicyScenario(
            scenario_id="context_risk_fast_exit_aggressive",
            description_ko="테마/대장주/지수/breadth 약화 1 cycle 확인 후 조기청산",
            stop_loss_pct=cfg.baseline_stop_loss_pct,
            take_profit_pct=cfg.baseline_take_profit_pct,
            max_hold_minutes=cfg.baseline_max_hold_minutes,
            theme_weak_exit=True,
            leader_collapse_exit=True,
            index_weak_exit=True,
            breadth_collapse_exit=True,
            confirmation_cycles=1,
        ),
        ExitPolicyScenario(
            scenario_id="context_risk_fast_exit_balanced",
            description_ko="테마/대장주/지수/breadth 약화 2 cycle 확인 후 조기청산",
            stop_loss_pct=cfg.baseline_stop_loss_pct,
            take_profit_pct=cfg.baseline_take_profit_pct,
            max_hold_minutes=cfg.baseline_max_hold_minutes,
            theme_weak_exit=True,
            leader_collapse_exit=True,
            index_weak_exit=True,
            breadth_collapse_exit=True,
            confirmation_cycles=2,
        ),
        ExitPolicyScenario(
            scenario_id="context_risk_fast_exit_conservative",
            description_ko="테마/대장주/지수/breadth 약화 3 cycle 확인 후 조기청산",
            stop_loss_pct=cfg.baseline_stop_loss_pct,
            take_profit_pct=cfg.baseline_take_profit_pct,
            max_hold_minutes=cfg.baseline_max_hold_minutes,
            theme_weak_exit=True,
            leader_collapse_exit=True,
            index_weak_exit=True,
            breadth_collapse_exit=True,
            confirmation_cycles=3,
        ),
    ]


class ExitPolicyShadowSimulator:
    def __init__(self, config: ExitPolicyValidationConfig | None = None) -> None:
        self.config = config or ExitPolicyValidationConfig()

    def simulate(
        self,
        scenario: ExitPolicyScenario,
        *,
        entry_time: str,
        entry_price: Any,
        quantity: Any,
        price_path: Iterable[PricePoint | dict[str, Any]],
        context_path: Iterable[ContextPoint | dict[str, Any]] | None = None,
    ) -> ShadowExitResult:
        entry_dt = _parse_time(entry_time)
        entry = _optional_float(entry_price)
        qty = _first_int(quantity) or 0
        if entry_dt is None or entry is None or entry <= 0 or qty <= 0:
            return _insufficient_result(
                scenario.scenario_id,
                "ENTRY_DATA_MISSING",
                {"entry_time": entry_time, "entry_price": entry_price, "quantity": quantity},
            )

        raw_points = [_coerce_price_point(point) for point in price_path]
        points = [
            point
            for point in raw_points
            if point is not None and _parse_time(point.at) is not None and _parse_time(point.at) >= entry_dt and point.price > 0
        ]
        points.sort(key=lambda point: _parse_time(point.at) or entry_dt)
        if not points:
            return _insufficient_result(
                scenario.scenario_id,
                "PRICE_PATH_MISSING_AFTER_ENTRY",
                {"entry_time": entry_time, "entry_price": entry, "raw_point_count": len(raw_points)},
            )

        contexts = [_coerce_context_point(point) for point in (context_path or [])]
        contexts = [point for point in contexts if point is not None and _parse_time(point.at) is not None]
        contexts.sort(key=lambda point: _parse_time(point.at) or entry_dt)

        returns: list[tuple[PricePoint, float]] = []
        mfe = 0.0
        mae = 0.0
        partial_exit: dict[str, Any] | None = None
        confirmation_counts = {EXIT_THEME_WEAK: 0, EXIT_LEADER_COLLAPSE: 0, EXIT_INDEX_WEAK: 0, EXIT_BREADTH_COLLAPSE: 0}
        context_index = 0
        latest_context: ContextPoint | None = None
        triggered: tuple[str, PricePoint, list[str]] | None = None
        market_close = _market_close_time(self.config.market_close_hhmm)

        for point in points:
            point_dt = _parse_time(point.at) or entry_dt
            while context_index < len(contexts):
                context_dt = _parse_time(contexts[context_index].at)
                if context_dt is None or context_dt > point_dt:
                    break
                latest_context = contexts[context_index]
                context_index += 1

            current_return = _return_pct(point.price, entry)
            returns.append((point, current_return))
            mfe = max(mfe, current_return)
            mae = min(mae, current_return)

            if scenario.stop_loss_pct is not None and current_return <= scenario.stop_loss_pct:
                triggered = (EXIT_STOP_LOSS, point, ["STOP_LOSS_TOUCH"])
                break

            context_trigger = self._context_trigger(scenario, latest_context, confirmation_counts)
            if context_trigger:
                triggered = (context_trigger[0], point, context_trigger[1])
                break

            if scenario.first_take_profit_pct is not None and partial_exit is None and current_return >= scenario.first_take_profit_pct:
                partial_exit = {
                    "exit_time": point.at,
                    "exit_price": point.price,
                    "exit_return_pct": current_return,
                    "exit_percent": max(0.0, min(100.0, scenario.first_exit_percent)),
                }

            if partial_exit is None and scenario.take_profit_pct is not None and current_return >= scenario.take_profit_pct:
                triggered = (EXIT_TAKE_PROFIT, point, ["TAKE_PROFIT_TOUCH"])
                break

            if partial_exit is not None:
                final_tp = scenario.final_take_profit_pct
                if final_tp is not None and current_return >= final_tp:
                    triggered = (EXIT_PARTIAL_TAKE_PROFIT, point, ["FINAL_TAKE_PROFIT_AFTER_PARTIAL"])
                    break
                if scenario.trailing_after_first_tp and scenario.trailing_giveback_pct is not None:
                    if current_return <= mfe - scenario.trailing_giveback_pct:
                        triggered = (EXIT_PARTIAL_TAKE_PROFIT, point, ["TRAILING_AFTER_PARTIAL_TP"])
                        break
            elif scenario.trailing_start_mfe_pct is not None and scenario.trailing_giveback_pct is not None:
                if mfe >= scenario.trailing_start_mfe_pct and current_return <= mfe - scenario.trailing_giveback_pct:
                    triggered = (EXIT_TRAILING_STOP, point, ["TRAILING_GIVEBACK"])
                    break

            if scenario.max_hold_minutes is not None and _hold_minutes(entry_dt, point_dt) >= scenario.max_hold_minutes:
                triggered = (EXIT_TIME, point, ["MAX_HOLD_MINUTES"])
                break

            if market_close is not None and point_dt.time() >= market_close:
                triggered = (EXIT_MARKET_CLOSE, point, ["MARKET_CLOSE_ASSUMPTION"])
                break

        if triggered is None:
            point = points[-1]
            reason_codes = ["NO_EXIT_CONDITION_MET"]
            return self._result(
                scenario,
                trigger_type=EXIT_NO_EXIT,
                point=point,
                entry_dt=entry_dt,
                entry_price=entry,
                quantity=qty,
                returns=returns,
                all_points=points,
                reason_codes=reason_codes,
                partial_exit=partial_exit,
                net_is_realized=False,
            )

        trigger_type, trigger_point, reason_codes = triggered
        return self._result(
            scenario,
            trigger_type=trigger_type,
            point=trigger_point,
            entry_dt=entry_dt,
            entry_price=entry,
            quantity=qty,
            returns=returns,
            all_points=points,
            reason_codes=reason_codes,
            partial_exit=partial_exit,
            net_is_realized=True,
        )

    def _context_trigger(
        self,
        scenario: ExitPolicyScenario,
        context: ContextPoint | None,
        confirmation_counts: dict[str, int],
    ) -> tuple[str, list[str]] | None:
        signal_flags = {
            EXIT_THEME_WEAK: scenario.theme_weak_exit and _theme_weak(context),
            EXIT_LEADER_COLLAPSE: scenario.leader_collapse_exit and _leader_collapsed(context),
            EXIT_INDEX_WEAK: scenario.index_weak_exit and _index_weak(context),
            EXIT_BREADTH_COLLAPSE: scenario.breadth_collapse_exit and _breadth_collapsed(context),
        }
        for trigger_type, active in signal_flags.items():
            confirmation_counts[trigger_type] = confirmation_counts.get(trigger_type, 0) + 1 if active else 0
            if active and confirmation_counts[trigger_type] >= max(1, int(scenario.confirmation_cycles or 1)):
                return trigger_type, [f"{trigger_type}_CONFIRMED_{scenario.confirmation_cycles}"]
        return None

    def _result(
        self,
        scenario: ExitPolicyScenario,
        *,
        trigger_type: str,
        point: PricePoint,
        entry_dt: datetime,
        entry_price: float,
        quantity: int,
        returns: list[tuple[PricePoint, float]],
        all_points: list[PricePoint],
        reason_codes: list[str],
        partial_exit: dict[str, Any] | None,
        net_is_realized: bool,
    ) -> ShadowExitResult:
        point_dt = _parse_time(point.at) or entry_dt
        if partial_exit and trigger_type == EXIT_PARTIAL_TAKE_PROFIT:
            first_percent = float(partial_exit.get("exit_percent") or 0.0) / 100.0
            remaining = max(0.0, 1.0 - first_percent)
            weighted_exit = (float(partial_exit["exit_price"]) * first_percent) + (float(point.price) * remaining)
            exit_price = round(weighted_exit, 6)
        else:
            exit_price = float(point.price)

        gross = _return_pct(exit_price, entry_price) if net_is_realized else None
        cost_pct = self._round_trip_cost_pct()
        basis = entry_price * quantity
        estimated_cost = round(basis * (cost_pct / 100.0), 4) if net_is_realized else None
        net = round((gross or 0.0) - cost_pct, 6) if gross is not None else None
        realized = round((exit_price - entry_price) * quantity - (estimated_cost or 0.0), 4) if net_is_realized else None
        holding_returns = [item[1] for item in returns] or [0.0]
        mfe = max(holding_returns)
        mae = min(holding_returns)
        final_return = _return_pct(exit_price, entry_price)
        giveback = max(0.0, mfe - final_return)
        post_returns = [_return_pct(candidate.price, entry_price) for candidate in all_points if (_parse_time(candidate.at) or entry_dt) >= point_dt]
        risk_avoided = max(0.0, final_return - min(post_returns)) if post_returns else 0.0
        missed_upside = max(0.0, max(post_returns) - final_return) if post_returns else 0.0
        stale_gap_count = _stale_gap_count(all_points, self.config.stale_tick_gap_sec)
        data_quality = "STALE_TICK_GAPS" if stale_gap_count else "OK"
        if trigger_type == EXIT_NO_EXIT:
            data_quality = "NO_EXIT_MARK_TO_MARK"
        details = {
            "scenario": scenario.to_dict(),
            "entry_price": entry_price,
            "quantity": quantity,
            "point_count": len(all_points),
            "partial_exit": partial_exit or {},
            "stale_gap_count": stale_gap_count,
            "first_tick_at": all_points[0].at if all_points else "",
            "last_tick_at": all_points[-1].at if all_points else "",
        }
        return ShadowExitResult(
            scenario_id=scenario.scenario_id,
            exit_trigger_type=trigger_type,
            exit_time=point.at if net_is_realized or trigger_type == EXIT_NO_EXIT else "",
            exit_price=exit_price,
            hold_minutes=round(_hold_minutes(entry_dt, point_dt), 3),
            gross_return_pct=round(gross, 6) if gross is not None else None,
            net_return_pct=net,
            mfe_pct=round(mfe, 6),
            mae_pct=round(mae, 6),
            giveback_pct=round(giveback, 6),
            risk_avoided_pct=round(risk_avoided, 6),
            missed_upside_pct=round(missed_upside, 6),
            realized_pnl_krw=realized,
            estimated_cost_krw=estimated_cost,
            data_quality=data_quality,
            reason_codes=reason_codes,
            details_json=details,
        )

    def _round_trip_cost_pct(self) -> float:
        total_bp = (
            float(self.config.commission_bp_per_side) * 2.0
            + float(self.config.sell_tax_bp)
            + float(self.config.primary_slippage_bp) * 2.0
        )
        return round(total_bp / 100.0, 6)


class ExitPolicyValidationAnalyzer:
    def __init__(
        self,
        db: TradingDatabase,
        *,
        config: ExitPolicyValidationConfig | None = None,
        report_root: Path | None = None,
        canary_analyzer: LiveSimCanaryPerformanceAnalyzer | None = None,
    ) -> None:
        self.db = db
        self.config = config or ExitPolicyValidationConfig()
        self.report_root = report_root or REPORT_ROOT
        self.canary_analyzer = canary_analyzer or LiveSimCanaryPerformanceAnalyzer(
            db,
            dry_run_analyzer=DryRunPerformanceAnalyzer(db, config=_dry_config_from_exit_config(self.config)),
        )
        self.simulator = ExitPolicyShadowSimulator(self.config)

    def build_report(
        self,
        *,
        trade_date: Optional[str] = None,
        code: Optional[str] = None,
        scenario_id: Optional[str] = None,
        comparison_label: Optional[str] = None,
        exit_trigger_type: Optional[str] = None,
        segment: Optional[str] = None,
        recommendation_grade: Optional[str] = None,
        issue_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        resolved_trade_date = self.resolve_trade_date(trade_date)
        scenarios = default_exit_policy_scenarios(self.config)
        lifecycle_cases = self.canary_analyzer.build_lifecycles(trade_date=resolved_trade_date, code=code)
        ticks_by_code = self._price_ticks_by_code(trade_date=resolved_trade_date, code=code)
        contexts_by_key = self._contexts_by_key(trade_date=resolved_trade_date, code=code)
        all_items = self._build_cases(
            lifecycle_cases,
            scenarios=scenarios,
            ticks_by_code=ticks_by_code,
            contexts_by_key=contexts_by_key,
        )
        filtered = _filter_cases(
            all_items,
            scenario_id=scenario_id,
            comparison_label=comparison_label,
            exit_trigger_type=exit_trigger_type,
            segment=segment,
            recommendation_grade=recommendation_grade,
            issue_type=issue_type,
        )
        scenario_summary = self.aggregate_scenarios(filtered)
        scenario_grades = {row["scenario_id"]: row.get("recommendation_grade", "") for row in scenario_summary}
        for item in filtered:
            item["recommendation_grade"] = scenario_grades.get(str(item.get("scenario_id") or ""), "")
        if recommendation_grade:
            filtered = [item for item in filtered if item.get("recommendation_grade") == recommendation_grade]
            scenario_summary = self.aggregate_scenarios(filtered)
        summary = self.aggregate_summary(filtered, scenario_summary, lifecycle_cases, trade_date=resolved_trade_date)
        segment_analysis = self.aggregate_segments(filtered)
        recommendations = self.recommendations(scenario_summary)
        proposals = self.change_proposals(scenario_summary)
        start = max(0, int(offset or 0))
        end = start + max(1, int(limit or 100))
        return {
            "report_id": new_message_id("exit_policy_validation"),
            "status": "READY",
            "review_only": True,
            "analysis_only": True,
            "trade_date": resolved_trade_date,
            "generated_at": utc_timestamp(),
            "filters": {
                "trade_date": resolved_trade_date,
                "code": code or "",
                "scenario_id": scenario_id or "",
                "comparison_label": comparison_label or "",
                "exit_trigger_type": exit_trigger_type or "",
                "segment": segment or "",
                "recommendation_grade": recommendation_grade or "",
                "issue_type": issue_type or "",
                "limit": int(limit or 100),
                "offset": int(offset or 0),
            },
            "safety_scope": {
                "analysis_only": True,
                "live_real_order_activation": False,
                "gateway_send_order_created": False,
                "gateway_cancel_order_created": False,
                "gateway_modify_order_created": False,
                "strategy_settings_auto_change": False,
                "exit_threshold_auto_apply": False,
                "hybrid_threshold_auto_change": False,
            },
            "disclaimer_ko": "Exit 정책 검증은 분석 전용입니다. 청산 주문 생성, 설정 자동 변경, LIVE_REAL 활성화를 수행하지 않습니다.",
            "scenarios": [scenario.to_dict() for scenario in scenarios],
            "summary": summary,
            "actual_exit_quality": self.actual_exit_quality(lifecycle_cases),
            "scenario_summary": scenario_summary,
            "segment_analysis": segment_analysis,
            "recommendations": recommendations,
            "change_proposals": proposals,
            "items": filtered[start:end],
            "total_items": len(filtered),
        }

    def resolve_trade_date(self, trade_date: Optional[str]) -> str:
        if trade_date:
            return trade_date
        for table in ("live_sim_canary_decisions", "live_sim_orders", "gateway_price_ticks", "runtime_order_intents"):
            try:
                row = self.db.conn.execute(f"SELECT MAX(trade_date) AS trade_date FROM {table} WHERE trade_date != ''").fetchone()
            except Exception:
                row = None
            if row and row["trade_date"]:
                return str(row["trade_date"])
        return datetime.now().date().isoformat()

    def _build_cases(
        self,
        lifecycle_cases: list[dict[str, Any]],
        *,
        scenarios: list[ExitPolicyScenario],
        ticks_by_code: dict[str, list[dict[str, Any]]],
        contexts_by_key: dict[tuple[str, str], list[ContextPoint]],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for lifecycle in lifecycle_cases:
            entry = _entry_source(lifecycle)
            code = str(lifecycle.get("code") or "")
            context_path = _context_for_lifecycle(lifecycle, contexts_by_key)
            price_path = _price_path_after_entry(ticks_by_code.get(code, []), entry.get("entry_time"))
            segments = _segments_for_case(lifecycle, context_path)
            for scenario in scenarios:
                shadow = self.simulator.simulate(
                    scenario,
                    entry_time=str(entry.get("entry_time") or ""),
                    entry_price=entry.get("entry_price"),
                    quantity=entry.get("quantity"),
                    price_path=price_path,
                    context_path=context_path,
                )
                comparison = self.compare_actual_shadow(lifecycle, shadow)
                issue_types = _case_issue_types(shadow, comparison, self.config.large_giveback_pct)
                operator_message = _operator_message_ko(shadow, comparison, issue_types)
                item = {
                    "case_id": f"{lifecycle.get('case_id') or lifecycle.get('lifecycle_id')}:{scenario.scenario_id}",
                    "lifecycle_id": lifecycle.get("lifecycle_id") or lifecycle.get("case_id") or "",
                    "source_case_id": lifecycle.get("case_id") or "",
                    "trade_date": lifecycle.get("trade_date") or "",
                    "code": lifecycle.get("code") or "",
                    "name": lifecycle.get("name") or "",
                    "theme": lifecycle.get("theme") or "",
                    "scenario_id": scenario.scenario_id,
                    "scenario": scenario.to_dict(),
                    "entry_time": entry.get("entry_time") or "",
                    "entry_price": entry.get("entry_price"),
                    "entry_price_source": entry.get("entry_price_source"),
                    "quantity": entry.get("quantity"),
                    "actual_exit_type": comparison["actual_exit_type"],
                    "actual_exit_time": comparison["actual_exit_time"],
                    "actual_net_return_pct": comparison["actual_net_return_pct"],
                    "shadow_exit_type": shadow.exit_trigger_type,
                    "shadow_exit_time": shadow.exit_time,
                    "shadow_exit_price": shadow.exit_price,
                    "shadow_net_return_pct": shadow.net_return_pct,
                    "net_return_delta_pct": comparison["net_return_delta_pct"],
                    "hold_time_delta_min": comparison["hold_time_delta_min"],
                    "comparison_label": comparison["comparison_label"],
                    "better_than_actual": comparison["better_than_actual"],
                    "worse_than_actual": comparison["worse_than_actual"],
                    "avoided_loss": comparison["avoided_loss"],
                    "cut_winner_too_early": comparison["cut_winner_too_early"],
                    "held_loser_too_long": comparison["held_loser_too_long"],
                    "max_drawdown_reduction_pct": comparison["max_drawdown_reduction_pct"],
                    "giveback_reduction_pct": comparison["giveback_reduction_pct"],
                    "missed_upside_delta_pct": comparison["missed_upside_delta_pct"],
                    "shadow_result": shadow.to_dict(),
                    "actual": _actual_payload(lifecycle),
                    "segments": segments,
                    "issue_types": issue_types,
                    "issue_type": issue_types[0] if issue_types else "",
                    "operator_message_ko": operator_message,
                    "price_path_summary": _price_path_summary(price_path, entry.get("entry_time"), self.config.stale_tick_gap_sec),
                    "context_risk_signals": _context_summary(context_path),
                    "raw_details_json": {
                        "linked_ids": lifecycle.get("linked_ids") or {},
                        "entry_fills": lifecycle.get("entry_fills") or [],
                        "exit_fills": lifecycle.get("exit_fills") or [],
                        "order_timeline": lifecycle.get("order_timeline") or [],
                        "fill_timeline": lifecycle.get("fill_timeline") or [],
                        "exit_timeline": lifecycle.get("exit_timeline") or [],
                        "raw_metadata": lifecycle.get("raw_metadata") or {},
                    },
                }
                items.append(item)
        return items

    def compare_actual_shadow(self, lifecycle: dict[str, Any], shadow: ShadowExitResult) -> dict[str, Any]:
        actual_net = _optional_float(lifecycle.get("net_return_pct"), lifecycle.get("live_sim_net_return_pct"))
        actual_exit_time = _first_text(lifecycle.get("final_exit_fill_at"), lifecycle.get("first_exit_fill_at"))
        actual_hold = _optional_float(lifecycle.get("hold_minutes"))
        actual_type = _actual_exit_type(lifecycle)
        if shadow.exit_trigger_type == EXIT_INSUFFICIENT_DATA:
            label = COMPARISON_INSUFFICIENT_DATA
        elif actual_net is None or not actual_exit_time or shadow.net_return_pct is None:
            label = COMPARISON_INCOMPARABLE
        else:
            delta = shadow.net_return_pct - actual_net
            if abs(delta) <= self.config.comparison_tolerance_pct:
                label = COMPARISON_SAME
            elif delta > 0:
                label = COMPARISON_SHADOW_BETTER
            else:
                label = COMPARISON_SHADOW_WORSE
        delta = _diff(shadow.net_return_pct, actual_net)
        actual_mae = _optional_float(lifecycle.get("max_mae_pct_after_entry"))
        actual_mfe = _optional_float(lifecycle.get("max_mfe_pct_after_entry"))
        actual_giveback = None
        if actual_mfe is not None and actual_net is not None:
            actual_giveback = max(0.0, actual_mfe - actual_net)
        max_drawdown_reduction = None
        if actual_mae is not None and shadow.mae_pct is not None:
            max_drawdown_reduction = round(abs(actual_mae) - abs(shadow.mae_pct), 6)
        giveback_reduction = _diff(actual_giveback, shadow.giveback_pct)
        actual_missed = max(0.0, (actual_mfe or 0.0) - (actual_net or 0.0)) if actual_net is not None else None
        missed_upside_delta = _diff(shadow.missed_upside_pct, actual_missed)
        hold_delta = _diff(shadow.hold_minutes, actual_hold)
        return {
            "actual_exit_type": actual_type,
            "actual_exit_time": actual_exit_time,
            "actual_net_return_pct": actual_net,
            "shadow_exit_type": shadow.exit_trigger_type,
            "shadow_net_return_pct": shadow.net_return_pct,
            "net_return_delta_pct": round(delta, 6) if delta is not None else None,
            "hold_time_delta_min": round(hold_delta, 6) if hold_delta is not None else None,
            "max_drawdown_reduction_pct": max_drawdown_reduction,
            "giveback_reduction_pct": round(giveback_reduction, 6) if giveback_reduction is not None else None,
            "missed_upside_delta_pct": round(missed_upside_delta, 6) if missed_upside_delta is not None else None,
            "better_than_actual": label == COMPARISON_SHADOW_BETTER,
            "worse_than_actual": label == COMPARISON_SHADOW_WORSE,
            "avoided_loss": bool(label == COMPARISON_SHADOW_BETTER and actual_net is not None and actual_net < 0),
            "cut_winner_too_early": bool(label == COMPARISON_SHADOW_WORSE and actual_net is not None and actual_net > 0 and (hold_delta or 0) < 0),
            "held_loser_too_long": bool(label == COMPARISON_SHADOW_WORSE and actual_net is not None and actual_net < 0 and (hold_delta or 0) > 0),
            "comparison_label": label,
        }

    def aggregate_summary(
        self,
        items: list[dict[str, Any]],
        scenario_summary: list[dict[str, Any]],
        lifecycle_cases: list[dict[str, Any]],
        *,
        trade_date: str,
    ) -> dict[str, Any]:
        best = _best_scenario(scenario_summary)
        actual_returns = [_optional_float(item.get("net_return_pct")) for item in lifecycle_cases]
        actual_returns = [value for value in actual_returns if value is not None]
        return {
            "trade_date": trade_date,
            "analysis_lifecycle_count": len(lifecycle_cases),
            "shadow_case_count": len(items),
            "scenario_count": len({item.get("scenario_id") for item in items}),
            "actual_live_sim_avg_net_return_pct": _avg(actual_returns),
            "best_shadow_scenario": best.get("scenario_id", ""),
            "best_shadow_expectancy_pct": best.get("expectancy_pct"),
            "best_shadow_avg_net_return_pct": best.get("avg_net_return_pct"),
            "stop_loss_hit_count": sum(1 for item in items if item.get("shadow_exit_type") == EXIT_STOP_LOSS),
            "take_profit_capture_count": sum(1 for item in items if item.get("shadow_exit_type") in {EXIT_TAKE_PROFIT, EXIT_PARTIAL_TAKE_PROFIT}),
            "time_exit_weak_count": sum(
                1
                for item in items
                if item.get("shadow_exit_type") == EXIT_TIME and (item.get("shadow_net_return_pct") or 0.0) <= 0
            ),
            "trailing_would_improve_count": sum(
                1
                for item in items
                if item.get("shadow_exit_type") == EXIT_TRAILING_STOP and item.get("better_than_actual")
            ),
            "giveback_large_count": sum(
                1 for item in items if ((item.get("shadow_result") or {}).get("giveback_pct") or 0.0) >= self.config.large_giveback_pct
            ),
            "insufficient_data_count": sum(1 for item in items if item.get("comparison_label") == COMPARISON_INSUFFICIENT_DATA),
            "comparison_counts": dict(Counter(str(item.get("comparison_label") or "") for item in items)),
            "exit_trigger_counts": dict(Counter(str(item.get("shadow_exit_type") or "") for item in items)),
            "review_only": True,
            "analysis_only": True,
        }

    def actual_exit_quality(self, lifecycle_cases: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "sample_count": len(lifecycle_cases),
            "closed_count": sum(1 for item in lifecycle_cases if item.get("final_status") == "CLOSED"),
            "avg_net_return_pct": _avg(item.get("net_return_pct") for item in lifecycle_cases),
            "avg_mfe_pct": _avg(item.get("max_mfe_pct_after_entry") for item in lifecycle_cases),
            "avg_mae_pct": _avg(item.get("max_mae_pct_after_entry") for item in lifecycle_cases),
            "avg_hold_minutes": _avg(item.get("hold_minutes") for item in lifecycle_cases),
            "exit_type_counts": dict(Counter(_actual_exit_type(item) for item in lifecycle_cases)),
            "quality_grade_counts": dict(Counter(str(item.get("exit_quality_grade") or "UNKNOWN") for item in lifecycle_cases)),
            "issue_counts": dict(Counter(issue for item in lifecycle_cases for issue in item.get("issue_types", []))),
        }

    def aggregate_scenarios(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            grouped[str(item.get("scenario_id") or "")].append(item)
        rows = [self._scenario_row(scenario_id, rows) for scenario_id, rows in sorted(grouped.items())]
        return sorted(rows, key=lambda row: (row.get("recommendation_rank", 99), -(row.get("expectancy_pct") or -999)))

    def _scenario_row(self, scenario_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        returns = [_optional_float(row.get("shadow_net_return_pct")) for row in rows]
        returns = [value for value in returns if value is not None]
        wins = [value for value in returns if value > 0]
        losses = [value for value in returns if value < 0]
        comparable = [row for row in rows if row.get("comparison_label") not in {COMPARISON_INCOMPARABLE, COMPARISON_INSUFFICIENT_DATA}]
        trigger_counts = Counter(str(row.get("shadow_exit_type") or "") for row in rows)
        insufficient = sum(1 for row in rows if row.get("comparison_label") == COMPARISON_INSUFFICIENT_DATA)
        better = sum(1 for row in rows if row.get("comparison_label") == COMPARISON_SHADOW_BETTER)
        worse = sum(1 for row in rows if row.get("comparison_label") == COMPARISON_SHADOW_WORSE)
        stale = sum(1 for row in rows if ((row.get("price_path_summary") or {}).get("stale_gap_count") or 0) > 0)
        no_context = sum(1 for row in rows if not ((row.get("context_risk_signals") or {}).get("sample_count") or 0))
        profit_factor = round(sum(wins) / abs(sum(losses)), 6) if losses else (None if not wins else 999.0)
        row = {
            "scenario_id": scenario_id,
            "sample_count": len(rows),
            "comparable_count": len(comparable),
            "insufficient_data_count": insufficient,
            "win_rate": _ratio(len(wins), len(returns)),
            "avg_net_return_pct": _avg(returns),
            "median_net_return_pct": round(median(returns), 6) if returns else None,
            "total_net_return_pct": round(sum(returns), 6) if returns else None,
            "avg_mfe_pct": _avg((row.get("shadow_result") or {}).get("mfe_pct") for row in rows),
            "avg_mae_pct": _avg((row.get("shadow_result") or {}).get("mae_pct") for row in rows),
            "avg_hold_minutes": _avg((row.get("shadow_result") or {}).get("hold_minutes") for row in rows),
            "profit_factor": profit_factor,
            "expectancy_pct": _avg(returns),
            "max_loss_pct": min(returns) if returns else None,
            "avg_loss_pct": _avg(losses),
            "bad_trade_rate": _ratio(len(losses), len(returns)),
            "stop_loss_hit_rate": _ratio(trigger_counts.get(EXIT_STOP_LOSS, 0), len(rows)),
            "time_exit_rate": _ratio(trigger_counts.get(EXIT_TIME, 0), len(rows)),
            "trailing_stop_rate": _ratio(trigger_counts.get(EXIT_TRAILING_STOP, 0), len(rows)),
            "take_profit_rate": _ratio(trigger_counts.get(EXIT_TAKE_PROFIT, 0) + trigger_counts.get(EXIT_PARTIAL_TAKE_PROFIT, 0), len(rows)),
            "context_exit_rate": _ratio(sum(trigger_counts.get(kind, 0) for kind in CONTEXT_EXIT_TYPES), len(rows)),
            "no_exit_rate": _ratio(trigger_counts.get(EXIT_NO_EXIT, 0), len(rows)),
            "giveback_avg_pct": _avg((row.get("shadow_result") or {}).get("giveback_pct") for row in rows),
            "large_giveback_count": sum(
                1 for row in rows if ((row.get("shadow_result") or {}).get("giveback_pct") or 0.0) >= self.config.large_giveback_pct
            ),
            "exit_signal_latency_avg_sec": 0.0,
            "exit_fill_assumption_risk": "TICK_OR_MINUTE_PRICE_ASSUMPTION",
            "missing_tick_rate": _ratio(insufficient, len(rows)),
            "stale_tick_rate": _ratio(stale, len(rows)),
            "insufficient_context_rate": _ratio(no_context, len(rows)),
            "actual_vs_shadow_match_rate": _ratio(
                sum(1 for row in rows if row.get("comparison_label") == COMPARISON_SAME),
                len(comparable),
            ),
            "better_than_actual_count": better,
            "worse_than_actual_count": worse,
            "comparison_counts": dict(Counter(str(row.get("comparison_label") or "") for row in rows)),
            "exit_trigger_counts": dict(trigger_counts),
        }
        grade, reason, warning, rank = _recommendation(row, self.config)
        row["recommendation_grade"] = grade
        row["recommendation_reason_ko"] = reason
        row["risk_warning_ko"] = warning
        row["recommendation_rank"] = rank
        return row

    def aggregate_segments(self, items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        dimensions = [
            "session_bucket",
            "hybrid_score_bucket",
            "theme_score_bucket",
            "stock_role",
            "theme_status",
            "price_location_status",
            "price_location_readiness",
            "entry_reason_code",
            "exit_reason_code",
            "liquidity_bucket",
            "entry_slippage_bucket",
            "volatility_bucket",
            "market_side",
            "actual_outcome_match",
        ]
        output: dict[str, list[dict[str, Any]]] = {}
        for dimension in dimensions:
            grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            for item in items:
                value = str((item.get("segments") or {}).get(dimension) or "UNKNOWN")
                grouped[(str(item.get("scenario_id") or ""), value)].append(item)
            rows = []
            for (scenario_id, value), group in grouped.items():
                rows.append(
                    {
                        "scenario_id": scenario_id,
                        "segment": dimension,
                        "value": value,
                        "sample_count": len(group),
                        "comparable_count": sum(
                            1 for item in group if item.get("comparison_label") not in {COMPARISON_INCOMPARABLE, COMPARISON_INSUFFICIENT_DATA}
                        ),
                        "avg_net_return_pct": _avg(item.get("shadow_net_return_pct") for item in group),
                        "expectancy_pct": _avg(item.get("shadow_net_return_pct") for item in group),
                        "better_than_actual_count": sum(1 for item in group if item.get("better_than_actual")),
                        "worse_than_actual_count": sum(1 for item in group if item.get("worse_than_actual")),
                        "win_rate": _ratio(
                            sum(1 for item in group if (item.get("shadow_net_return_pct") is not None and item.get("shadow_net_return_pct") > 0)),
                            sum(1 for item in group if item.get("shadow_net_return_pct") is not None),
                        ),
                    }
                )
            output[dimension] = sorted(rows, key=lambda row: (-row["sample_count"], row["scenario_id"], row["value"]))[:100]
        return output

    def recommendations(self, scenario_summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for row in scenario_summary[:5]:
            rows.append(
                {
                    "scenario_id": row.get("scenario_id"),
                    "recommendation_grade": row.get("recommendation_grade"),
                    "recommendation_reason_ko": row.get("recommendation_reason_ko"),
                    "risk_warning_ko": row.get("risk_warning_ko"),
                    "expectancy_pct": row.get("expectancy_pct"),
                    "sample_count": row.get("sample_count"),
                    "review_only": True,
                    "auto_apply": False,
                }
            )
        return rows

    def change_proposals(self, scenario_summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
        best = _best_scenario(scenario_summary)
        if best.get("recommendation_grade") not in {"STRONG_CANDIDATE", "WATCH_CANDIDATE"}:
            return []
        scenario = next((item for item in default_exit_policy_scenarios(self.config) if item.scenario_id == best.get("scenario_id")), None)
        if scenario is None:
            return []
        proposal_fields = [
            ("live_sim_exit_guard.stop_loss_pct", self.config.baseline_stop_loss_pct, scenario.stop_loss_pct),
            ("live_sim_exit_guard.take_profit_pct", self.config.baseline_take_profit_pct, scenario.take_profit_pct),
            ("live_sim_exit_guard.max_hold_minutes", self.config.baseline_max_hold_minutes, scenario.max_hold_minutes),
            ("live_sim_exit_guard.trailing_start_mfe_pct", None, scenario.trailing_start_mfe_pct),
            ("live_sim_exit_guard.trailing_giveback_pct", None, scenario.trailing_giveback_pct),
            ("context_risk.confirmation_cycles", None, scenario.confirmation_cycles if any([scenario.theme_weak_exit, scenario.leader_collapse_exit, scenario.index_weak_exit, scenario.breadth_collapse_exit]) else None),
        ]
        proposals = []
        for field, current, candidate in proposal_fields:
            if candidate is None or candidate == current:
                continue
            proposals.append(
                {
                    "proposal_id": f"exit_policy_validation:{best.get('scenario_id')}:{field}",
                    "field": field,
                    "current_value": current,
                    "candidate_value": candidate,
                    "evidence_summary": best.get("recommendation_reason_ko", ""),
                    "affected_sample_count": best.get("sample_count", 0),
                    "expected_net_benefit": best.get("expectancy_pct"),
                    "risk_warning": best.get("risk_warning_ko", ""),
                    "confidence": "MEDIUM" if best.get("recommendation_grade") == "WATCH_CANDIDATE" else "HIGH",
                    "auto_apply": False,
                    "requires_operator_approval": True,
                    "review_only": True,
                }
            )
        return proposals

    def persist_report(self, report: dict[str, Any]) -> dict[str, Any]:
        path = self.export_json(report, self._report_dir(report) / f"exit_policy_validation_{report.get('trade_date')}.json")
        return {"report_id": report.get("report_id"), "path": str(path), "persisted": True}

    def export_json(self, report: dict[str, Any], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return path

    def export_csv(self, report: dict[str, Any], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = [
            "case_id",
            "trade_date",
            "code",
            "name",
            "scenario_id",
            "actual_exit_type",
            "actual_net_return_pct",
            "shadow_exit_type",
            "shadow_net_return_pct",
            "net_return_delta_pct",
            "comparison_label",
            "issue_type",
            "recommendation_grade",
            "operator_message_ko",
        ]
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for item in report.get("items") or []:
                writer.writerow({column: item.get(column, "") for column in columns})
        return path

    def export_markdown(self, report: dict[str, Any], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        summary = dict(report.get("summary") or {})
        actual = dict(report.get("actual_exit_quality") or {})
        scenarios = list(report.get("scenario_summary") or [])
        bad_cases = [item for item in report.get("items") or [] if item.get("worse_than_actual") or item.get("issue_type") == "LARGE_GIVEBACK"]
        better_cases = [item for item in report.get("items") or [] if item.get("better_than_actual")]
        proposal_lines = [
            f"- {item.get('field')}: {item.get('current_value')} -> {item.get('candidate_value')} "
            f"(auto_apply={item.get('auto_apply')}, requires_operator_approval={item.get('requires_operator_approval')})"
            for item in report.get("change_proposals", [])
        ] or ["- No review-only setting proposal from the current sample."]
        lines = [
            f"# Exit Policy Validation Report {report.get('trade_date') or ''}".strip(),
            "",
            "## 1. Exit Policy Validation Summary",
            f"- Generated at: {report.get('generated_at', '')}",
            f"- Report ID: {report.get('report_id', '')}",
            f"- Analysis-only: {bool(report.get('analysis_only', True))}",
            f"- Lifecycle count: {summary.get('analysis_lifecycle_count', 0)}",
            f"- Actual LIVE_SIM avg net return: {_fmt(summary.get('actual_live_sim_avg_net_return_pct'))}",
            f"- Best shadow scenario: {summary.get('best_shadow_scenario') or '-'}",
            "",
            "## 2. Actual LIVE_SIM Exit Quality",
            f"- Closed count: {actual.get('closed_count', 0)}",
            f"- Avg net return: {_fmt(actual.get('avg_net_return_pct'))}",
            f"- Avg MFE / MAE: {_fmt(actual.get('avg_mfe_pct'))} / {_fmt(actual.get('avg_mae_pct'))}",
            f"- Exit type counts: {json.dumps(actual.get('exit_type_counts', {}), ensure_ascii=False, sort_keys=True)}",
            "",
            "## 3. Shadow Scenario Performance",
            *_scenario_markdown_lines(scenarios),
            "",
            "## 4. Actual vs Shadow Comparison",
            f"- Comparison counts: {json.dumps(summary.get('comparison_counts', {}), ensure_ascii=False, sort_keys=True)}",
            "",
            "## 5. Segment Analysis",
            *_segment_markdown_lines(report.get("segment_analysis") or {}),
            "",
            "## 6. Stop Loss Review",
            *_trigger_review_lines(scenarios, EXIT_STOP_LOSS, "stop_loss_hit_rate"),
            "",
            "## 7. Take Profit Review",
            *_trigger_review_lines(scenarios, EXIT_TAKE_PROFIT, "take_profit_rate"),
            "",
            "## 8. Trailing Stop Review",
            *_trigger_review_lines(scenarios, EXIT_TRAILING_STOP, "trailing_stop_rate"),
            "",
            "## 9. Time Exit Review",
            *_trigger_review_lines(scenarios, EXIT_TIME, "time_exit_rate"),
            "",
            "## 10. Context Risk Exit Review",
            *_trigger_review_lines(scenarios, "CONTEXT", "context_exit_rate"),
            "",
            "## 11. Bad Exit Cases",
            *_case_markdown_lines(bad_cases[:20]),
            "",
            "## 12. Missed Profit / Giveback Cases",
            *_case_markdown_lines(better_cases[:20]),
            "",
            "## 13. Recommendations for Review Only",
            *[f"- {item.get('scenario_id')}: {item.get('recommendation_grade')} - {item.get('recommendation_reason_ko')}" for item in report.get("recommendations", [])],
            "",
            "## 14. Next PR Candidate Settings",
            *proposal_lines,
        ]
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return path

    def export_report(self, report: dict[str, Any], *, fmt: str = "json") -> dict[str, str]:
        report_dir = self._report_dir(report)
        trade_date = str(report.get("trade_date") or datetime.now().date().isoformat())
        stem = f"exit_policy_validation_{trade_date}"
        exports: dict[str, str] = {}
        for item in (["json", "csv", "md"] if fmt == "all" else [fmt]):
            if item == "json":
                exports["json"] = str(self.export_json(report, report_dir / f"{stem}.json"))
            elif item == "csv":
                exports["csv"] = str(self.export_csv(report, report_dir / f"{stem}.csv"))
            elif item in {"md", "markdown"}:
                exports["md"] = str(self.export_markdown(report, report_dir / f"{stem}.md"))
        return exports

    def list_reports(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        rows = []
        for path in sorted(self.report_root.glob("*/exit_policy_validation_*.json"), reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            rows.append(
                {
                    "report_id": payload.get("report_id") or path.stem,
                    "trade_date": payload.get("trade_date") or path.parent.name,
                    "generated_at": payload.get("generated_at", ""),
                    "status": payload.get("status", "READY"),
                    "summary": payload.get("summary", {}),
                    "path": str(path),
                }
            )
        return rows[max(0, offset) : max(0, offset) + max(1, limit)]

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        for path in self.report_root.glob("*/exit_policy_validation_*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if report_id in {str(payload.get("report_id") or ""), path.stem, path.name}:
                payload["path"] = str(path)
                return payload
        return None

    def _report_dir(self, report: dict[str, Any]) -> Path:
        trade_date = str(report.get("trade_date") or datetime.now().date().isoformat())
        return self.report_root / trade_date

    def _price_ticks_by_code(self, *, trade_date: str, code: Optional[str]) -> dict[str, list[dict[str, Any]]]:
        clauses = ["trade_date = ?"]
        params: list[Any] = [trade_date]
        if code:
            clauses.append("code = ?")
            params.append(code)
        rows = _select_dicts(
            self.db,
            f"""
            SELECT *
            FROM gateway_price_ticks
            WHERE {' AND '.join(clauses)}
            ORDER BY timestamp ASC, id ASC
            LIMIT 200000
            """,
            tuple(params),
        )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row.get("code") or "")].append(row)
        return grouped

    def _contexts_by_key(self, *, trade_date: str, code: Optional[str]) -> dict[tuple[str, str], list[ContextPoint]]:
        clauses = ["trade_date = ?"]
        params: list[Any] = [trade_date]
        if code:
            clauses.append("code = ?")
            params.append(code)
        rows = _select_dicts(
            self.db,
            f"""
            SELECT *
            FROM position_context_history
            WHERE {' AND '.join(clauses)}
            ORDER BY captured_at ASC, id ASC
            LIMIT 100000
            """,
            tuple(params),
        )
        grouped: dict[tuple[str, str], list[ContextPoint]] = defaultdict(list)
        for row in rows:
            point = ContextPoint(
                at=str(row.get("captured_at") or ""),
                theme_status=str(row.get("theme_status") or ""),
                breadth_status=str(row.get("breadth_status") or ""),
                leader_return_pct=_optional_float(row.get("leader_return_pct")),
                leader_support_broken=bool(row.get("leader_support_broken")),
                index_status=str(row.get("index_status") or ""),
                index_return_pct=_optional_float(row.get("index_return_pct")),
                market_status=str(row.get("market_status") or ""),
                market_risk_status=str(row.get("market_risk_status") or ""),
                risk_reason_codes=tuple(_safe_json_loads(row.get("risk_reason_codes_json"), [])),
                metadata=_safe_json_loads(row.get("metadata_json"), {}),
            )
            key = (str(row.get("candidate_instance_id") or ""), str(row.get("code") or ""))
            grouped[key].append(point)
            grouped[("", str(row.get("code") or ""))].append(point)
        return grouped


def config_from_dry_run_config(config: DryRunPerformanceConfig) -> ExitPolicyValidationConfig:
    return ExitPolicyValidationConfig(
        commission_bp_per_side=float(config.commission_bp_per_side),
        sell_tax_bp=float(config.sell_tax_bp),
        primary_slippage_bp=float(config.primary_slippage_bp),
    )


def _dry_config_from_exit_config(config: ExitPolicyValidationConfig) -> DryRunPerformanceConfig:
    return DryRunPerformanceConfig(
        commission_bp_per_side=config.commission_bp_per_side,
        sell_tax_bp=config.sell_tax_bp,
        primary_slippage_bp=config.primary_slippage_bp,
    )


def _insufficient_result(scenario_id: str, reason: str, details: dict[str, Any]) -> ShadowExitResult:
    return ShadowExitResult(
        scenario_id=scenario_id,
        exit_trigger_type=EXIT_INSUFFICIENT_DATA,
        data_quality="INSUFFICIENT_DATA",
        reason_codes=[reason],
        details_json=details,
    )


def _filter_cases(
    items: list[dict[str, Any]],
    *,
    scenario_id: Optional[str],
    comparison_label: Optional[str],
    exit_trigger_type: Optional[str],
    segment: Optional[str],
    recommendation_grade: Optional[str],
    issue_type: Optional[str],
) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        segments = item.get("segments") or {}
        if scenario_id and item.get("scenario_id") != scenario_id:
            continue
        if comparison_label and item.get("comparison_label") != comparison_label:
            continue
        if exit_trigger_type and item.get("shadow_exit_type") != exit_trigger_type:
            continue
        if recommendation_grade and item.get("recommendation_grade") not in {"", recommendation_grade}:
            continue
        if issue_type and issue_type not in (item.get("issue_types") or []):
            continue
        if segment and segment not in {str(value) for value in segments.values()} and segment not in segments:
            continue
        rows.append(item)
    return rows


def _entry_source(lifecycle: dict[str, Any]) -> dict[str, Any]:
    raw = dict(lifecycle.get("raw_metadata") or {})
    dry = dict(raw.get("dry_run_item") or {})
    entry_time = _first_text(
        lifecycle.get("first_fill_at"),
        lifecycle.get("full_fill_at"),
        lifecycle.get("submitted_at"),
        dry.get("opened_at"),
    )
    entry_price = _optional_float(
        lifecycle.get("avg_fill_price"),
        lifecycle.get("live_sim_avg_entry_price"),
        dry.get("entry_price"),
        dry.get("position_entry_price"),
        lifecycle.get("requested_price"),
    )
    source = "LIVE_SIM_FILL" if lifecycle.get("avg_fill_price") else "DRY_RUN_OR_REQUESTED"
    quantity = _first_int(lifecycle.get("filled_quantity"), dry.get("entry_quantity"), lifecycle.get("requested_quantity"))
    return {"entry_time": entry_time, "entry_price": entry_price, "quantity": quantity, "entry_price_source": source}


def _price_path_after_entry(ticks: list[dict[str, Any]], entry_time: Any) -> list[PricePoint]:
    entry_dt = _parse_time(entry_time)
    points: list[PricePoint] = []
    for tick in ticks:
        at = str(tick.get("timestamp") or tick.get("received_at") or "")
        dt = _parse_time(at)
        price = _optional_float(tick.get("price"))
        if dt is None or price is None or price <= 0:
            continue
        if entry_dt is not None and dt < entry_dt:
            continue
        points.append(PricePoint(at=at, price=price, raw=tick))
    return points


def _context_for_lifecycle(
    lifecycle: dict[str, Any],
    contexts_by_key: dict[tuple[str, str], list[ContextPoint]],
) -> list[ContextPoint]:
    code = str(lifecycle.get("code") or "")
    candidate_instance_id = str(lifecycle.get("candidate_instance_id") or "")
    rows = list(contexts_by_key.get((candidate_instance_id, code), []))
    if not rows:
        rows = list(contexts_by_key.get(("", code), []))
    return rows


def _segments_for_case(lifecycle: dict[str, Any], context_path: list[ContextPoint]) -> dict[str, str]:
    raw = dict(lifecycle.get("raw_metadata") or {})
    dry = dict(raw.get("dry_run_item") or {})
    details = dict(dry.get("details") or {})
    latest_context = context_path[-1] if context_path else None
    return {
        "session_bucket": _session_bucket(_first_text(lifecycle.get("first_fill_at"), lifecycle.get("submitted_at"))),
        "hybrid_score_bucket": _score_bucket(_optional_float(lifecycle.get("hybrid_score"), dry.get("hybrid_score"))),
        "theme_score_bucket": _score_bucket(_optional_float(dry.get("theme_score"), details.get("theme_score"))),
        "stock_role": _first_text(dry.get("stock_role"), details.get("stock_role"), "UNKNOWN"),
        "theme_status": _first_text((latest_context.theme_status if latest_context else ""), dry.get("theme_status"), "UNKNOWN"),
        "price_location_status": _first_text(details.get("price_location_status"), dry.get("price_location_status"), "UNKNOWN"),
        "price_location_readiness": _first_text(details.get("price_location_readiness"), dry.get("price_location_readiness"), "UNKNOWN"),
        "entry_reason_code": _first_reason_code(lifecycle, dry),
        "exit_reason_code": _first_text(lifecycle.get("exit_reason"), lifecycle.get("live_sim_exit_reason"), "UNKNOWN"),
        "liquidity_bucket": _first_text(lifecycle.get("liquidity_bucket"), dry.get("liquidity_bucket"), "UNKNOWN"),
        "entry_slippage_bucket": _slippage_bucket(lifecycle.get("entry_slippage_bp")),
        "volatility_bucket": _volatility_bucket(lifecycle.get("max_mfe_pct_after_entry"), lifecycle.get("max_mae_pct_after_entry")),
        "market_side": _market_side(latest_context),
        "actual_outcome_match": _first_text(lifecycle.get("outcome_match"), "INCOMPARABLE"),
    }


def _actual_payload(lifecycle: dict[str, Any]) -> dict[str, Any]:
    return {
        "actual_exit_type": _actual_exit_type(lifecycle),
        "actual_exit_time": _first_text(lifecycle.get("final_exit_fill_at"), lifecycle.get("first_exit_fill_at")),
        "actual_net_return_pct": _optional_float(lifecycle.get("net_return_pct"), lifecycle.get("live_sim_net_return_pct")),
        "actual_gross_return_pct": _optional_float(lifecycle.get("gross_return_pct")),
        "actual_mfe_pct": _optional_float(lifecycle.get("max_mfe_pct_after_entry")),
        "actual_mae_pct": _optional_float(lifecycle.get("max_mae_pct_after_entry")),
        "actual_hold_minutes": _optional_float(lifecycle.get("hold_minutes")),
        "exit_reason": lifecycle.get("exit_reason") or lifecycle.get("live_sim_exit_reason") or "",
        "final_status": lifecycle.get("final_status") or "",
    }


def _actual_exit_type(lifecycle: dict[str, Any]) -> str:
    reason = str(lifecycle.get("exit_reason") or lifecycle.get("live_sim_exit_reason") or "").upper()
    if lifecycle.get("stop_loss_triggered") or "STOP" in reason or "LOSS" in reason:
        return EXIT_STOP_LOSS
    if lifecycle.get("take_profit_triggered") or "TAKE" in reason or "PROFIT" in reason:
        return EXIT_TAKE_PROFIT
    if "TRAIL" in reason:
        return EXIT_TRAILING_STOP
    if lifecycle.get("time_exit_triggered") or "TIME" in reason:
        return EXIT_TIME
    if "THEME" in reason:
        return EXIT_THEME_WEAK
    if "LEADER" in reason:
        return EXIT_LEADER_COLLAPSE
    if "INDEX" in reason or "MARKET" in reason:
        return EXIT_INDEX_WEAK
    if "BREADTH" in reason:
        return EXIT_BREADTH_COLLAPSE
    if lifecycle.get("market_close_exit_triggered") or "CLOSE" in reason:
        return EXIT_MARKET_CLOSE
    if lifecycle.get("final_status") == "OPEN":
        return EXIT_NO_EXIT
    return "UNKNOWN"


def _case_issue_types(shadow: ShadowExitResult, comparison: dict[str, Any], large_giveback_pct: float) -> list[str]:
    issues: list[str] = []
    if shadow.exit_trigger_type == EXIT_INSUFFICIENT_DATA:
        issues.append("INSUFFICIENT_DATA")
    if comparison.get("better_than_actual"):
        issues.append("SHADOW_BETTER")
    if comparison.get("worse_than_actual"):
        issues.append("SHADOW_WORSE")
    if comparison.get("avoided_loss"):
        issues.append("AVOIDED_LOSS")
    if comparison.get("cut_winner_too_early"):
        issues.append("CUT_WINNER_TOO_EARLY")
    if comparison.get("held_loser_too_long"):
        issues.append("HELD_LOSER_TOO_LONG")
    if (shadow.giveback_pct or 0.0) >= large_giveback_pct:
        issues.append("LARGE_GIVEBACK")
    if shadow.exit_trigger_type in CONTEXT_EXIT_TYPES:
        issues.append("CONTEXT_EXIT_CANDIDATE")
    return issues


def _operator_message_ko(shadow: ShadowExitResult, comparison: dict[str, Any], issues: list[str]) -> str:
    if "INSUFFICIENT_DATA" in issues:
        return "진입 이후 가격 경로가 부족해 0수익으로 처리하지 않고 데이터 부족으로 분류했습니다."
    if comparison.get("better_than_actual"):
        return f"{shadow.scenario_id} 기준 shadow exit가 실제 LIVE_SIM보다 유리했습니다. 자동 적용 없이 후보로만 검토하세요."
    if comparison.get("cut_winner_too_early"):
        return "shadow exit가 수익 구간을 너무 빨리 잘랐을 가능성이 있습니다."
    if comparison.get("worse_than_actual"):
        return "shadow exit가 실제 LIVE_SIM보다 불리했습니다. 해당 조건은 보수적으로 해석하세요."
    if "LARGE_GIVEBACK" in issues:
        return "MFE 대비 되돌림이 커서 trailing 또는 time exit 후보 검토가 필요합니다."
    return "실제 결과와 큰 차이가 없습니다. 표본 누적 후 재검토하세요."


def _price_path_summary(points: list[PricePoint], entry_time: Any, stale_gap_sec: float) -> dict[str, Any]:
    entry_dt = _parse_time(entry_time)
    prices = [point.price for point in points]
    return {
        "entry_time": str(entry_time or ""),
        "point_count": len(points),
        "first_tick_at": points[0].at if points else "",
        "last_tick_at": points[-1].at if points else "",
        "min_price": min(prices) if prices else None,
        "max_price": max(prices) if prices else None,
        "duration_minutes": _hold_minutes(entry_dt, _parse_time(points[-1].at)) if entry_dt and points else None,
        "stale_gap_count": _stale_gap_count(points, stale_gap_sec),
        "data_quality": "INSUFFICIENT_DATA" if not points else ("STALE_TICK_GAPS" if _stale_gap_count(points, stale_gap_sec) else "OK"),
    }


def _context_summary(points: list[ContextPoint]) -> dict[str, Any]:
    return {
        "sample_count": len(points),
        "theme_weak_count": sum(1 for point in points if _theme_weak(point)),
        "leader_collapse_count": sum(1 for point in points if _leader_collapsed(point)),
        "index_weak_count": sum(1 for point in points if _index_weak(point)),
        "breadth_collapse_count": sum(1 for point in points if _breadth_collapsed(point)),
        "latest": asdict(points[-1]) if points else {},
    }


def _recommendation(row: dict[str, Any], config: ExitPolicyValidationConfig) -> tuple[str, str, str, int]:
    sample = int(row.get("sample_count") or 0)
    comparable = int(row.get("comparable_count") or 0)
    expectancy = _optional_float(row.get("expectancy_pct")) or 0.0
    better = int(row.get("better_than_actual_count") or 0)
    worse = int(row.get("worse_than_actual_count") or 0)
    max_loss = _optional_float(row.get("max_loss_pct")) or 0.0
    missing_tick_rate = _optional_float(row.get("missing_tick_rate")) or 0.0
    if sample < config.min_candidate_samples or comparable < config.min_candidate_samples:
        return (
            "INSUFFICIENT_SAMPLE",
            f"비교 가능한 표본이 {comparable}건으로 최소 {config.min_candidate_samples}건보다 적습니다.",
            "표본 부족 상태에서는 exit threshold를 변경하지 마세요.",
            3,
        )
    if missing_tick_rate >= 0.3:
        return (
            "RISKY",
            "가격 경로 부족 비율이 높아 결과 왜곡 가능성이 큽니다.",
            "tick/minute 수집 품질을 먼저 보강하세요.",
            2,
        )
    if expectancy > 0 and better > worse and max_loss > -3.0:
        return (
            "STRONG_CANDIDATE",
            f"expectancy {expectancy:.3f}%로 양수이며 실제보다 나은 케이스가 더 많습니다.",
            "자동 적용 금지. 다음 PR에서 별도 승인/캔들 재검증이 필요합니다.",
            0,
        )
    if expectancy >= 0 and better >= worse:
        return (
            "WATCH_CANDIDATE",
            "기대값은 나쁘지 않지만 표본/리스크 확인이 더 필요합니다.",
            "후보로만 관찰하고 장세/세그먼트별 편차를 확인하세요.",
            1,
        )
    if max_loss <= -3.0 or worse > better:
        return (
            "NOT_RECOMMENDED",
            "실제보다 나쁜 케이스가 많거나 손실 꼬리가 큽니다.",
            "해당 scenario를 현재 운영 설정으로 승격하지 마세요.",
            4,
        )
    return (
        "RISKY",
        "성과와 리스크 신호가 혼재되어 있습니다.",
        "자동 적용 금지. 추가 표본과 세그먼트 분석이 필요합니다.",
        2,
    )


def _best_scenario(rows: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [row for row in rows if row.get("recommendation_grade") in {"STRONG_CANDIDATE", "WATCH_CANDIDATE"}]
    if not candidates:
        candidates = list(rows)
    if not candidates:
        return {}
    return sorted(candidates, key=lambda row: (row.get("recommendation_rank", 99), -(row.get("expectancy_pct") or -999)))[0]


def _theme_weak(context: ContextPoint | None) -> bool:
    if context is None:
        return False
    text = " ".join([context.theme_status, " ".join(context.risk_reason_codes)]).upper()
    return any(token in text for token in ("WEAK", "INACTIVE", "COLLAPSE", "RISK_OFF", "THEME_WEAK"))


def _leader_collapsed(context: ContextPoint | None) -> bool:
    if context is None:
        return False
    text = " ".join([context.leader_vwap_status if hasattr(context, "leader_vwap_status") else "", " ".join(context.risk_reason_codes)]).upper()
    return bool(context.leader_support_broken) or (context.leader_return_pct is not None and context.leader_return_pct <= -2.0) or "LEADER" in text


def _index_weak(context: ContextPoint | None) -> bool:
    if context is None:
        return False
    text = " ".join([context.index_status, context.market_status, context.market_risk_status, " ".join(context.risk_reason_codes)]).upper()
    return any(token in text for token in ("WEAK", "RISK_OFF", "INDEX_WEAK", "MARKET_RISK")) or (
        context.index_return_pct is not None and context.index_return_pct <= -0.5
    )


def _breadth_collapsed(context: ContextPoint | None) -> bool:
    if context is None:
        return False
    text = " ".join([context.breadth_status, " ".join(context.risk_reason_codes)]).upper()
    return any(token in text for token in ("WEAK", "COLLAPSE", "LOW_BREADTH", "BREADTH"))


def _market_side(context: ContextPoint | None) -> str:
    if context is None:
        return "UNKNOWN"
    if _index_weak(context) or "RISK_OFF" in f"{context.market_status} {context.market_risk_status}".upper():
        return "RISK_OFF"
    if "WEAK" in f"{context.market_status} {context.index_status} {context.breadth_status}".upper():
        return "WEAK"
    return "HEALTHY"


def _coerce_price_point(value: PricePoint | dict[str, Any]) -> PricePoint | None:
    if isinstance(value, PricePoint):
        return value
    if not isinstance(value, dict):
        return None
    price = _optional_float(value.get("price"), value.get("close"), value.get("last_price"))
    if price is None:
        return None
    return PricePoint(at=str(value.get("at") or value.get("timestamp") or value.get("received_at") or ""), price=price, raw=value)


def _coerce_context_point(value: ContextPoint | dict[str, Any]) -> ContextPoint | None:
    if isinstance(value, ContextPoint):
        return value
    if not isinstance(value, dict):
        return None
    return ContextPoint(
        at=str(value.get("at") or value.get("captured_at") or value.get("timestamp") or ""),
        theme_status=str(value.get("theme_status") or ""),
        breadth_status=str(value.get("breadth_status") or ""),
        leader_return_pct=_optional_float(value.get("leader_return_pct")),
        leader_support_broken=bool(value.get("leader_support_broken")),
        index_status=str(value.get("index_status") or ""),
        index_return_pct=_optional_float(value.get("index_return_pct")),
        market_status=str(value.get("market_status") or ""),
        market_risk_status=str(value.get("market_risk_status") or ""),
        risk_reason_codes=tuple(str(item) for item in value.get("risk_reason_codes") or []),
        metadata=dict(value.get("metadata") or {}),
    )


def _select_dicts(db: TradingDatabase, query: str, params: tuple = ()) -> list[dict[str, Any]]:
    try:
        rows = db.conn.execute(query, params).fetchall()
    except Exception:
        return []
    return [dict(row) for row in rows]


def _safe_json_loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _market_close_time(value: str) -> time | None:
    try:
        hour, minute = [int(part) for part in str(value or "").split(":", 1)]
        return time(hour=hour, minute=minute)
    except Exception:
        return None


def _hold_minutes(start: datetime | None, end: datetime | None) -> float:
    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start).total_seconds() / 60.0)


def _return_pct(price: float, entry: float) -> float:
    if entry <= 0:
        return 0.0
    return round((price / entry - 1.0) * 100.0, 6)


def _stale_gap_count(points: list[PricePoint], stale_gap_sec: float) -> int:
    count = 0
    parsed = [_parse_time(point.at) for point in points]
    parsed = [value for value in parsed if value is not None]
    for left, right in zip(parsed, parsed[1:]):
        if (right - left).total_seconds() > stale_gap_sec:
            count += 1
    return count


def _optional_float(*values: Any) -> Optional[float]:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return None


def _first_int(*values: Any) -> Optional[int]:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return int(float(str(value).replace(",", "")))
        except (TypeError, ValueError):
            continue
    return None


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "")
        if text:
            return text
    return ""


def _diff(left: Any, right: Any) -> Optional[float]:
    left_value = _optional_float(left)
    right_value = _optional_float(right)
    if left_value is None or right_value is None:
        return None
    return left_value - right_value


def _avg(values: Iterable[Any]) -> Optional[float]:
    parsed = [_optional_float(value) for value in values]
    parsed = [value for value in parsed if value is not None]
    if not parsed:
        return None
    return round(sum(parsed) / len(parsed), 6)


def _ratio(numerator: int | float, denominator: int | float) -> Optional[float]:
    if denominator is None or float(denominator) <= 0:
        return None
    return round(float(numerator) / float(denominator), 6)


def _session_bucket(value: Any) -> str:
    dt = _parse_time(value)
    if dt is None:
        return "UNKNOWN"
    minute = dt.hour * 60 + dt.minute
    open_minute = 9 * 60
    if open_minute <= minute < open_minute + 10:
        return "open_0_10"
    if open_minute + 10 <= minute < open_minute + 30:
        return "open_10_30"
    if minute >= 14 * 60:
        return "late_day"
    return "mid_day"


def _score_bucket(value: Any) -> str:
    parsed = _optional_float(value)
    if parsed is None:
        return "UNKNOWN"
    start = int(parsed // 20) * 20
    end = min(100, start + 20)
    return f"{start}-{end}"


def _first_reason_code(lifecycle: dict[str, Any], dry: dict[str, Any]) -> str:
    for values in (lifecycle.get("reason_codes"), dry.get("reason_codes"), dry.get("hybrid_reason_codes")):
        if isinstance(values, list) and values:
            return str(values[0])
    return _first_text(dry.get("gate_reason"), lifecycle.get("matched_by"), "UNKNOWN")


def _slippage_bucket(value: Any) -> str:
    parsed = _optional_float(value)
    if parsed is None:
        return "UNKNOWN"
    if parsed <= 10:
        return "<=10bp"
    if parsed <= 30:
        return "10-30bp"
    if parsed <= 50:
        return "30-50bp"
    return ">50bp"


def _volatility_bucket(mfe: Any, mae: Any) -> str:
    hi = abs(_optional_float(mfe) or 0.0)
    lo = abs(_optional_float(mae) or 0.0)
    width = hi + lo
    if width >= 5:
        return "HIGH"
    if width >= 2:
        return "MEDIUM"
    return "LOW"


def _fmt(value: Any) -> str:
    parsed = _optional_float(value)
    return "-" if parsed is None else f"{parsed:.4f}%"


def _scenario_markdown_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- no scenario rows"]
    return [
        f"- {row.get('scenario_id')}: samples={row.get('sample_count')}, comparable={row.get('comparable_count')}, "
        f"expectancy={_fmt(row.get('expectancy_pct'))}, win_rate={row.get('win_rate')}, grade={row.get('recommendation_grade')}"
        for row in rows
    ]


def _segment_markdown_lines(segments: dict[str, list[dict[str, Any]]]) -> list[str]:
    if not segments:
        return ["- no segment data"]
    lines: list[str] = []
    for key, rows in segments.items():
        top = rows[:3]
        if top:
            lines.append(f"- {key}: " + "; ".join(f"{row.get('value')} {row.get('scenario_id')} n={row.get('sample_count')} exp={_fmt(row.get('expectancy_pct'))}" for row in top))
    return lines or ["- no segment data"]


def _trigger_review_lines(rows: list[dict[str, Any]], trigger: str, rate_key: str) -> list[str]:
    if not rows:
        return ["- no scenario data"]
    return [f"- {row.get('scenario_id')}: {rate_key}={row.get(rate_key)}, expectancy={_fmt(row.get('expectancy_pct'))}" for row in rows]


def _case_markdown_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- no cases"]
    return [
        f"- {row.get('code')} {row.get('scenario_id')}: actual={_fmt(row.get('actual_net_return_pct'))}, "
        f"shadow={_fmt(row.get('shadow_net_return_pct'))}, delta={_fmt(row.get('net_return_delta_pct'))}, {row.get('comparison_label')}"
        for row in rows
    ]
