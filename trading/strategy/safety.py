from __future__ import annotations

from dataclasses import dataclass, field

from trading.strategy.models import OrderMode


@dataclass(frozen=True)
class OrderGuardDecision:
    allowed: bool
    reason: str
    reason_codes: list[str] = field(default_factory=list)


class ActualOrderGuard:
    """Phase 1 guard: strategy-generated real orders are always blocked."""

    def __init__(self, phase: str = "PHASE_1_OBSERVE") -> None:
        self.phase = phase

    def allow_real_order(
        self,
        order_mode: OrderMode = OrderMode.OBSERVE,
        *,
        config_enabled: bool = False,
        ui_enabled: bool = False,
        account: str = "",
        ordering_enabled: bool = False,
    ) -> OrderGuardDecision:
        if order_mode == OrderMode.OBSERVE:
            return OrderGuardDecision(
                allowed=False,
                reason="OBSERVE mode records virtual orders only.",
                reason_codes=["OBSERVE_MODE", "REAL_ORDER_DISABLED"],
            )
        return OrderGuardDecision(
            allowed=False,
            reason="Real strategy orders are disabled during Phase 1.",
            reason_codes=["PHASE_1_GUARD", "REAL_ORDER_DISABLED"],
        )
