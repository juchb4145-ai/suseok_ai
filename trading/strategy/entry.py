from __future__ import annotations

from datetime import datetime
from typing import Optional

from trading.rules import tick_size
from trading.strategy.models import EntryPlan, FillPolicy, StrategyProfile
from trading.strategy.pipeline import GatePipelineResult
from trading.strategy.runtime_settings import (
    StrategyRuntimeSettings,
    attach_settings_details,
    legacy_strategy_runtime_settings,
)
from trading.strategy.support_readiness import (
    READY_EARLY_SMALL,
    SUPPORT_NOT_READY,
    WAIT_DATA_SUPPORT_NOT_READY,
    support_source_readiness,
)


class TickSizeProvider:
    def tick_size(self, price: int) -> int:
        return tick_size(price)

    def add_ticks(self, price: int, count: int) -> int:
        value = max(0, int(price))
        for _ in range(max(0, int(count))):
            value += self.tick_size(value)
        return value

    def subtract_ticks(self, price: int, count: int) -> int:
        value = max(0, int(price))
        for _ in range(max(0, int(count))):
            value = max(0, value - self.tick_size(value))
        return value


class EntryPlanBuilder:
    def __init__(
        self,
        tick_provider: Optional[TickSizeProvider] = None,
        settings: Optional[StrategyRuntimeSettings] = None,
    ) -> None:
        self.tick_provider = tick_provider or TickSizeProvider()
        self.settings = settings or legacy_strategy_runtime_settings()

    def build(self, result: GatePipelineResult, now: Optional[datetime] = None) -> Optional[EntryPlan]:
        if not result.strategy_eligible:
            return None
        snapshot = result.snapshot
        if snapshot is None:
            return None

        stock_details = _stock_pullback_details(result)
        profile = _strategy_profile(stock_details)
        max_chase_pct = _max_chase_pct(profile, self.settings)
        order_timeout_sec = _order_timeout_sec(profile, self.settings)
        support_price = _support_price(stock_details)
        support_candidates = _support_candidates(stock_details)
        support_readiness = _support_readiness(stock_details)
        ready_type = str(stock_details.get("ready_type") or result.details.get("ready_type") or "")
        current_price = snapshot.price
        submittable = True
        diagnostic_only = False
        reason = ""
        base_price_source = str(stock_details.get("nearest_support") or "nearest_support")

        if support_price <= 0:
            support_price = current_price
            base_price_source = "current_price_fallback"
            submittable = False
            diagnostic_only = True
            reason = "support_missing"
        elif not support_readiness["ready"]:
            submittable = False
            diagnostic_only = True
            reason = str(support_readiness["reason"] or WAIT_DATA_SUPPORT_NOT_READY)

        split_plan = self._build_split_plan(result.final_grade, stock_details, current_price, ready_type)
        position_size_multiplier = _position_size_multiplier(result)
        if 0 < position_size_multiplier < 1:
            split_plan = _scale_split_plan(split_plan, position_size_multiplier)
        tick_offset = self.settings.integer("entry_plan_thresholds.tick_offset", 1)
        limit_price = int(split_plan[0].get("limit_price") or self.tick_provider.add_ticks(int(support_price), tick_offset))
        limit_vs_current_pct = _pct(current_price - limit_price, limit_price)
        limit_vs_support_pct = _pct(limit_price - support_price, support_price)
        if submittable and current_price > limit_price and limit_vs_current_pct > max_chase_pct:
            submittable = False
            diagnostic_only = True
            reason = "max_chase_exceeded"

        cancel_condition = {
            "submittable": submittable,
            "diagnostic_only": diagnostic_only,
            "reason": reason,
            "theme_id": result.theme_id,
            "theme_name": result.details.get("theme_name", ""),
            "strategy_profile": profile.value if profile else "",
            "final_grade": result.final_grade,
            "ready_type": ready_type,
            "current_price_at_plan": current_price,
            "support_price": support_price,
            "support_candidates": support_candidates,
            "selected_support_source": support_readiness["source"],
            "selected_support_price": support_readiness["price"],
            "selected_support_ready": support_readiness["ready"],
            "selected_support_ready_reason": support_readiness["reason"],
            "support_readiness_reason_codes": list(support_readiness["reason_codes"]),
            "limit_vs_current_pct": limit_vs_current_pct,
            "limit_vs_support_pct": limit_vs_support_pct,
            "max_chase_pct": max_chase_pct,
            "split_policy": {
                "weights": _split_weights(result.final_grade, self.settings),
                "position_size_multiplier": position_size_multiplier,
                "one_new_leg_per_cycle": True,
                "later_legs_require_previous_fill": True,
                "early_small_first_leg_only": ready_type == READY_EARLY_SMALL,
            },
            "dynamic_pullback_policy": dict(stock_details.get("dynamic_pullback_policy") or {}),
            "late_chase_diagnostics": dict(stock_details.get("late_chase_diagnostics") or {}),
            "late_chase_level": stock_details.get("late_chase_level", ""),
            "late_chase_score": stock_details.get("late_chase_score"),
            "comparison_reason_codes": list(stock_details.get("comparison_reason_codes") or []),
            "support_reclaimed": bool(stock_details.get("support_reclaimed")),
            "support_touched": bool(stock_details.get("support_touched")),
            "failed_low_break_rebound": bool(stock_details.get("failed_low_break_rebound")),
            "gate_result_key": f"{result.candidate_id}:{result.code}:{result.theme_id}:{result.final_grade}",
            "code": result.code,
            "order_kind": "virtual",
        }
        cancel_condition = attach_settings_details(cancel_condition, self.settings)
        return EntryPlan(
            candidate_id=result.candidate_id,
            entry_type="pullback_limit",
            base_price_source=base_price_source,
            limit_price=limit_price,
            tick_offset=self.settings.integer("entry_plan_thresholds.tick_offset", 1),
            max_chase_pct=max_chase_pct,
            split_plan=split_plan,
            order_timeout_sec=order_timeout_sec,
            cancel_condition=cancel_condition,
            retry_policy={"max_retries": self.settings.integer("entry_plan_thresholds.max_retries", 0)},
            confirmation_signal=list(result.details.get("cap_rules_applied", [])),
            fill_policy=FillPolicy.NORMAL,
            created_at=(now or datetime.now()).replace(microsecond=0).isoformat(),
        )

    def _build_split_plan(self, final_grade: str, details: dict, current_price: int, ready_type: str = "") -> list[dict]:
        weights = _split_weights(final_grade, self.settings)
        nearest_name = str(details.get("nearest_support") or "")
        nearest_price = _support_price(details)
        supports = _support_candidates(details)
        if nearest_name and nearest_price > 0:
            supports.setdefault(nearest_name, float(nearest_price))
        lower_supports = _lower_supports(supports, nearest_name, nearest_price)
        first_support_readiness = _support_readiness(details)
        plan: list[dict] = []
        for index, weight in enumerate(weights, start=1):
            if index == 1:
                support_name = nearest_name
                support_price = nearest_price
            else:
                support_name = ""
                support_price = 0
                lower_index = index - 2
                if lower_index < len(lower_supports):
                    support_name, support_price = lower_supports[lower_index]
            submittable = support_price > 0
            reason = "" if submittable else "support_missing"
            if index == 1 and submittable and not first_support_readiness["ready"]:
                submittable = False
                reason = str(first_support_readiness["reason"] or WAIT_DATA_SUPPORT_NOT_READY)
            if index > 1 and ready_type == READY_EARLY_SMALL:
                submittable = False
                reason = "early_small_later_leg_pending"
            tick_offset = self.settings.integer("entry_plan_thresholds.tick_offset", 1) if submittable else 0
            limit_price = self.tick_provider.add_ticks(int(support_price), tick_offset) if submittable else 0
            plan.append(
                {
                    "leg": index,
                    "weight_pct": weight,
                    "support_name": support_name or "",
                    "support_price": float(support_price) if support_price else 0,
                    "limit_price": limit_price,
                    "tick_offset": tick_offset,
                    "submittable": submittable,
                    "requires_previous_leg": index > 1,
                    "confirmation_required": index > 1 and not submittable,
                    "pending_after_first_fill": index > 1 and ready_type == READY_EARLY_SMALL,
                    "current_price_at_plan": current_price,
                    "reason": reason,
                }
            )
        return plan


def _stock_pullback_details(result: GatePipelineResult) -> dict:
    for decision in result.decisions:
        if decision.gate_name == "StockPullbackEntryGate":
            return dict(decision.details)
    return {}


def _strategy_profile(details: dict) -> Optional[StrategyProfile]:
    raw = details.get("profile")
    if not raw:
        return None
    try:
        return StrategyProfile(raw)
    except ValueError:
        return None


def _support_price(details: dict) -> int:
    value = details.get("nearest_support_price")
    if value is None:
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _support_candidates(details: dict) -> dict[str, float]:
    raw = details.get("support_candidates") or {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, float] = {}
    for name, value in raw.items():
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price > 0:
            result[str(name)] = price
    return result


def _support_readiness(details: dict) -> dict:
    source = str(details.get("selected_support_source") or details.get("nearest_support") or "")
    price = int(float(details.get("selected_support_price") or details.get("nearest_support_price") or 0))
    readiness_keys = {
        "selected_support_ready",
        "support_ready",
        "selected_support_ready_reason",
        "support_ready_reason",
        "support_readiness_reason_codes",
    }
    if not any(key in details for key in readiness_keys):
        return {
            "source": source,
            "price": price,
            "ready": True,
            "reason": "",
            "reason_codes": [],
        }
    ready = bool(details.get("selected_support_ready", details.get("support_ready", False)))
    reason = str(details.get("selected_support_ready_reason") or details.get("support_ready_reason") or "")
    reason_codes = list(details.get("support_readiness_reason_codes") or [])
    if not ready and not reason:
        reason = WAIT_DATA_SUPPORT_NOT_READY
    if not ready and not reason_codes:
        reason_codes = [reason or SUPPORT_NOT_READY]
    if ready:
        return {
            "source": source,
            "price": price,
            "ready": True,
            "reason": "",
            "reason_codes": [],
        }
    explicit = support_source_readiness(source, details)
    if explicit.reason_codes:
        reason_codes = list(dict.fromkeys(reason_codes + list(explicit.reason_codes)))
    return {
        "source": source,
        "price": price,
        "ready": False,
        "reason": reason or explicit.reason or SUPPORT_NOT_READY,
        "reason_codes": reason_codes,
    }


def _lower_supports(
    supports: dict[str, float],
    nearest_name: str,
    nearest_price: int,
) -> list[tuple[str, float]]:
    if nearest_price <= 0:
        return []
    values = [
        (name, price)
        for name, price in supports.items()
        if name != nearest_name and price > 0 and price < nearest_price
    ]
    return sorted(values, key=lambda item: item[1], reverse=True)


def _split_weights(final_grade: str, settings: Optional[StrategyRuntimeSettings] = None) -> list[int]:
    active_settings = settings or legacy_strategy_runtime_settings()
    grade = str(final_grade or "").upper()
    if grade in {"A", "A_SIGNAL"}:
        return [int(value) for value in active_settings.list_value("entry_plan_thresholds.split_weights.A", [40, 30, 30])]
    if grade in {"B+", "B+_SIGNAL"}:
        return [int(value) for value in active_settings.list_value("entry_plan_thresholds.split_weights.B_PLUS", [50, 30, 20])]
    return [int(value) for value in active_settings.list_value("entry_plan_thresholds.split_weights.default", [60, 25, 15])]


def _position_size_multiplier(result: GatePipelineResult) -> float:
    value = result.details.get("position_size_multiplier")
    if value is None:
        value = result.details.get("theme_lab_bridge", {}).get("position_size_multiplier") if isinstance(result.details.get("theme_lab_bridge"), dict) else None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    if number <= 0:
        return 1.0
    return min(1.0, number)


def _scale_split_plan(split_plan: list[dict], multiplier: float) -> list[dict]:
    scaled: list[dict] = []
    for leg in split_plan:
        item = dict(leg)
        item["original_weight_pct"] = item.get("weight_pct", 0)
        item["weight_pct"] = round(float(item.get("weight_pct") or 0.0) * multiplier, 4)
        item["position_size_multiplier"] = multiplier
        scaled.append(item)
    return scaled


def _profile_key(profile: Optional[StrategyProfile]) -> str:
    if profile in {StrategyProfile.KOSPI_LEADER_PROFILE, StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE}:
        return "semiconductor_signal" if profile == StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE else "kospi"
    return "kosdaq"


def _max_chase_pct(
    profile: Optional[StrategyProfile],
    settings: Optional[StrategyRuntimeSettings] = None,
) -> float:
    active_settings = settings or legacy_strategy_runtime_settings()
    default = 0.4 if profile in {StrategyProfile.KOSPI_LEADER_PROFILE, StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE} else 0.7
    return active_settings.number(f"entry_plan_thresholds.max_chase_pct.{_profile_key(profile)}", default)


def _order_timeout_sec(
    profile: Optional[StrategyProfile],
    settings: Optional[StrategyRuntimeSettings] = None,
) -> int:
    active_settings = settings or legacy_strategy_runtime_settings()
    default = 180 if profile in {StrategyProfile.KOSPI_LEADER_PROFILE, StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE} else 300
    return active_settings.integer(f"entry_plan_thresholds.order_timeout_sec.{_profile_key(profile)}", default)


def _pct(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100.0, 6)
