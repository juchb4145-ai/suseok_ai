from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RoundTripCostConfig:
    commission_bp_per_side: float = 1.5
    sell_tax_bp: float = 15.0
    entry_slippage_bp: float = 10.0
    exit_slippage_bp: float = 10.0


def round_trip_cost_pct(config: RoundTripCostConfig) -> float:
    total_bp = (
        float(config.commission_bp_per_side) * 2.0
        + float(config.sell_tax_bp)
        + float(config.entry_slippage_bp)
        + float(config.exit_slippage_bp)
    )
    return total_bp / 100.0


def cost_adjusted_signal_return_pct(raw_return_pct: float | None, config: RoundTripCostConfig) -> float | None:
    if raw_return_pct is None:
        return None
    return round(float(raw_return_pct) - round_trip_cost_pct(config), 6)


def cost_assumption_payload(config: RoundTripCostConfig, *, cost_scenario_id: str = "primary_10bp") -> dict[str, Any]:
    return {
        "cost_scenario_id": cost_scenario_id,
        "commission_bp_per_side": float(config.commission_bp_per_side),
        "sell_tax_bp": float(config.sell_tax_bp),
        "entry_slippage_bp": float(config.entry_slippage_bp),
        "exit_slippage_bp": float(config.exit_slippage_bp),
        "round_trip_cost_pct": round(round_trip_cost_pct(config), 6),
    }
