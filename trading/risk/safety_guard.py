from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trading.broker.command_queue import dedupe_key_for_command
from trading.broker.models import BrokerOrderRequest, GatewayCommand


@dataclass(frozen=True)
class SafetyCheck:
    name: str
    ok: bool
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "reason": self.reason,
            "details": dict(self.details or {}),
        }


@dataclass(frozen=True)
class SafetyCheckResult:
    ok: bool
    reason: str
    checks: list[SafetyCheck] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "checks": [check.to_dict() for check in self.checks],
            "details": dict(self.details or {}),
        }


@dataclass(frozen=True)
class OrderSafetyConfig:
    mode: str = "OBSERVE"
    live_order_enabled: bool = False
    max_order_amount: int = 3_000_000
    max_daily_orders_per_code: int = 5
    allow_zero_price: bool = True


class OrderCommandSafetyGuard:
    def __init__(self, config: OrderSafetyConfig) -> None:
        self.config = config

    def validate(
        self,
        request: BrokerOrderRequest,
        *,
        gateway_status: dict[str, Any],
        existing_order_command_count: int = 0,
        duplicate: bool = False,
    ) -> SafetyCheckResult:
        checks: list[SafetyCheck] = []

        checks.append(
            SafetyCheck(
                "mode_live_enabled",
                self.config.mode == "LIVE" and self.config.live_order_enabled,
                "LIVE_REQUIRES_TRADING_ALLOW_LIVE" if self.config.mode == "LIVE" and not self.config.live_order_enabled else "",
                {"mode": self.config.mode, "live_order_enabled": self.config.live_order_enabled},
            )
        )
        checks.append(SafetyCheck("account_present", bool(request.account), "ACCOUNT_REQUIRED"))
        checks.append(SafetyCheck("code_present", bool(request.code), "CODE_REQUIRED"))
        checks.append(SafetyCheck("side_valid", request.side in {"buy", "sell"}, "SIDE_INVALID"))
        checks.append(SafetyCheck("quantity_positive", request.quantity > 0, "QUANTITY_INVALID"))
        checks.append(
            SafetyCheck(
                "price_valid",
                request.price >= 0 and (self.config.allow_zero_price or request.price > 0),
                "PRICE_INVALID",
                {"allow_zero_price": self.config.allow_zero_price},
            )
        )

        amount = int(request.quantity) * max(0, int(request.price))
        checks.append(
            SafetyCheck(
                "order_amount_limit",
                amount <= self.config.max_order_amount,
                "ORDER_AMOUNT_LIMIT",
                {"amount": amount, "max_order_amount": self.config.max_order_amount},
            )
        )
        checks.append(
            SafetyCheck(
                "daily_code_order_limit",
                existing_order_command_count < self.config.max_daily_orders_per_code,
                "DAILY_CODE_ORDER_LIMIT",
                {
                    "existing_order_command_count": existing_order_command_count,
                    "max_daily_orders_per_code": self.config.max_daily_orders_per_code,
                },
            )
        )
        checks.append(SafetyCheck("dedupe", not duplicate, "DUPLICATE_ORDER_COMMAND"))
        checks.append(SafetyCheck("gateway_connected", bool(gateway_status.get("connected")), "GATEWAY_NOT_CONNECTED"))
        checks.append(SafetyCheck("gateway_heartbeat", bool(gateway_status.get("heartbeat_ok")), "GATEWAY_HEARTBEAT_STALE"))
        checks.append(SafetyCheck("gateway_logged_in", bool(gateway_status.get("kiwoom_logged_in")), "KIWOOM_NOT_LOGGED_IN"))
        checks.append(SafetyCheck("gateway_orderable", bool(gateway_status.get("orderable")), "GATEWAY_NOT_ORDERABLE"))
        checks.append(
            SafetyCheck(
                "gateway_account_match",
                not gateway_status.get("account") or str(gateway_status.get("account")) == request.account,
                "GATEWAY_ACCOUNT_MISMATCH",
                {"gateway_account": gateway_status.get("account"), "request_account": request.account},
            )
        )

        failed = [check for check in checks if not check.ok]
        return SafetyCheckResult(
            ok=not failed,
            reason="OK" if not failed else failed[0].reason,
            checks=checks,
            details={
                "dedupe_key": dedupe_key_for_order_request(request),
                "order_amount": amount,
            },
        )


def dedupe_key_for_order_request(request: BrokerOrderRequest) -> str:
    payload = request.to_dict()
    metadata = dict(request.metadata or {})
    if metadata.get("candidate_id") is not None:
        payload["candidate_id"] = metadata.get("candidate_id")
    if metadata.get("strategy_order_id") is not None:
        payload["strategy_order_id"] = metadata.get("strategy_order_id")
    command = GatewayCommand(type="send_order", payload=payload, idempotency_key=request.idempotency_key)
    return dedupe_key_for_command(command)
