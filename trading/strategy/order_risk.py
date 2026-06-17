from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from trading.strategy.order_models import (
    ManagedOrderIntent,
    OrderKillSwitchState,
    OrderManagerConfig,
    OrderRiskDecision,
    OrderRiskResult,
    OrderSide,
)


SIMULATION_ENV_VALUES = {"SIM", "SIMULATION", "MOCK", "PAPER", "DEMO", "LIVE_SIM", "1"}
REAL_ENV_VALUES = {"REAL", "PROD", "PRODUCTION", "LIVE", "LIVE_REAL", "REAL_BROKER", "0"}


class OrderRiskManager:
    def __init__(self, db: Any, gateway_state: Any, config: OrderManagerConfig | None = None) -> None:
        self.db = db
        self.gateway_state = gateway_state
        self.config = config or OrderManagerConfig.from_env()

    def evaluate(
        self,
        intent: ManagedOrderIntent | dict[str, Any],
        *,
        now: datetime | None = None,
        kill_switch_state: str = OrderKillSwitchState.NORMAL.value,
    ) -> OrderRiskDecision:
        payload = intent.to_dict() if hasattr(intent, "to_dict") else dict(intent or {})
        side = str(payload.get("side") or "").upper()
        code = str(payload.get("code") or "")
        details = dict(payload.get("details") or {})
        now = now or datetime.now(timezone.utc)
        reason_codes: list[str] = []

        broker = self._broker_snapshot()
        broker_guard = self._broker_guard(broker)
        reason_codes.extend(broker_guard)

        if not self.config.enabled:
            reason_codes.append("ORDER_MANAGER_DISABLED")
        if self.config.mode != "LIVE_SIM":
            reason_codes.append("ORDER_MANAGER_NOT_LIVE_SIM_MODE")
        if not self.config.allow_live_sim_orders:
            reason_codes.append("LIVE_SIM_FLAG_DISABLED")
        if self.config.observe_only:
            reason_codes.append("ORDER_MANAGER_OBSERVE_ONLY")

        if side == OrderSide.BUY.value and kill_switch_state in {
            OrderKillSwitchState.STOP_NEW_BUY.value,
            OrderKillSwitchState.REDUCE_ONLY.value,
            OrderKillSwitchState.KILL_SWITCH_ACTIVE.value,
        }:
            reason_codes.append("KILL_SWITCH_BLOCKS_BUY")

        if side == OrderSide.BUY.value:
            reason_codes.extend(self._buy_limits(payload))

        reason_codes.extend(self._common_order_checks(payload, now=now))

        if self._is_kill_switch_reason(reason_codes):
            decision = OrderRiskResult.KILL_SWITCH.value
        elif any(code in {"STALE_ENTRY_DECISION", "STALE_EXIT_DECISION", "STALE_QUOTE"} for code in reason_codes):
            decision = OrderRiskResult.WAIT.value
        elif reason_codes:
            decision = OrderRiskResult.REJECT.value
        else:
            decision = OrderRiskResult.PASS.value

        return OrderRiskDecision(
            decision=decision,
            side=side,
            code=code,
            idempotency_key=str(payload.get("idempotency_key") or ""),
            reason_codes=tuple(_dedupe(reason_codes)),
            operator_message_ko=self._operator_message(decision, reason_codes),
            details={
                "broker": broker,
                "intent": payload,
                "kill_switch_state": kill_switch_state,
                "limits": self._limit_details(payload),
            },
        )

    def _broker_guard(self, snapshot: dict[str, Any]) -> list[str]:
        reasons: list[str] = []
        if not bool(snapshot.get("kiwoom_logged_in")):
            reasons.append("BROKER_NOT_LOGGED_IN")
        if not bool(snapshot.get("orderable")):
            reasons.append("BROKER_NOT_ORDERABLE")
        account = str(snapshot.get("account") or "")
        if not account:
            reasons.append("ACCOUNT_NOT_CONFIGURED")
        whitelist = tuple(self.config.live_sim_account_whitelist or ())
        if whitelist and account not in whitelist:
            reasons.append("ACCOUNT_NOT_WHITELISTED")
        if not bool(snapshot.get("heartbeat_ok")):
            reasons.append("GATEWAY_HEARTBEAT_STALE")
        env_state = broker_environment_state(snapshot)
        if self.config.block_real_broker and env_state == "REAL":
            reasons.append("REAL_BROKER_BLOCKED")
        if self.config.require_simulation_broker and env_state == "UNKNOWN":
            reasons.append("BROKER_ENV_UNKNOWN")
        if self.config.require_simulation_broker and env_state == "REAL":
            reasons.append("REAL_BROKER_BLOCKED")
        queued = int(snapshot.get("pending_command_count") or 0)
        if queued >= 1000:
            reasons.append("COMMAND_QUEUE_UNHEALTHY")
        return reasons

    def _buy_limits(self, payload: dict[str, Any]) -> list[str]:
        reasons: list[str] = []
        trade_date = str(payload.get("trade_date") or "")
        code = str(payload.get("code") or "")
        theme_id = str(payload.get("theme_id") or "")
        details = dict(payload.get("details") or {})
        summary = _call(self.db, "managed_order_summary", trade_date=trade_date) or {}
        if int(summary.get("today_buy_order_count") or 0) >= self.config.max_daily_buy_orders:
            reasons.append("DAILY_BUY_ORDER_LIMIT")
        if self._daily_code_order_count(trade_date, code) >= self.config.max_daily_orders_per_code:
            reasons.append("DAILY_CODE_ORDER_LIMIT")
        if self._open_position_count() >= self.config.max_open_positions:
            reasons.append("MAX_OPEN_POSITIONS")
        if self.config.block_pyramiding and self._has_open_position(code):
            reasons.append("DUPLICATE_OPEN_POSITION")
        if self._has_pending_order(code):
            reasons.append("DUPLICATE_PENDING_ORDER")
        if theme_id and self._theme_exposure_count(theme_id) >= self.config.max_theme_exposure_count:
            reasons.append("MAX_THEME_EXPOSURE")

        market_status = str(details.get("market_status") or payload.get("market_status") or "").upper()
        market_action = str(details.get("market_action") or payload.get("market_action") or "").upper()
        if market_status == "RISK_OFF" or market_action == "BLOCK_NEW_ENTRY":
            reasons.append("MARKET_RISK_OFF_NEW_BUY_BLOCK")

        portfolio = _call(self.db, "latest_portfolio_risk_snapshot", trade_date=trade_date) or {}
        if bool(portfolio.get("stop_new_entry_recommended")):
            reasons.append("POSITION_RISK_STOP_NEW_ENTRY")
        if bool(portfolio.get("kill_switch_recommended")):
            reasons.append("POSITION_RISK_KILL_SWITCH_RECOMMENDED")
        return reasons

    def _common_order_checks(self, payload: dict[str, Any], *, now: datetime) -> list[str]:
        reasons: list[str] = []
        quantity = int(payload.get("quantity") or 0)
        price = int(payload.get("price") or 0)
        side = str(payload.get("side") or "").upper()
        details = dict(payload.get("details") or {})
        if quantity <= 0:
            reasons.append("QUANTITY_INVALID")
        if quantity > self.config.max_order_quantity:
            reasons.append("MAX_ORDER_QUANTITY")
        if price <= 0 and self.config.use_limit_price:
            reasons.append("PRICE_INVALID")
        if price <= 0 and not self.config.allow_market_order:
            reasons.append("MARKET_ORDER_FORBIDDEN")
        if price > 0 and quantity > 0 and price * quantity > self.config.max_order_amount:
            reasons.append("MAX_ORDER_AMOUNT")
        if int(details.get("spread_ticks") or 0) > self.config.max_spread_ticks:
            reasons.append("SPREAD_TOO_WIDE")
        if side == OrderSide.BUY.value and bool(details.get("vi_active")):
            reasons.append("VI_ACTIVE_BUY_BLOCK")
        if side == OrderSide.BUY.value and bool(details.get("upper_limit_near")):
            reasons.append("UPPER_LIMIT_NEAR_BUY_BLOCK")

        calculated_at = str(details.get("calculated_at") or payload.get("calculated_at") or "")
        if calculated_at and self.config.decision_stale_after_sec > 0:
            age = _age_sec(calculated_at, now)
            if age is not None and age > self.config.decision_stale_after_sec:
                reasons.append("STALE_ENTRY_DECISION" if side == OrderSide.BUY.value else "STALE_EXIT_DECISION")
        quote_at = str(details.get("last_tick_at") or details.get("tick_timestamp") or "")
        if quote_at and self.config.quote_stale_after_sec > 0:
            age = _age_sec(quote_at, now)
            if age is not None and age > self.config.quote_stale_after_sec:
                reasons.append("STALE_QUOTE")
        return reasons

    def _daily_code_order_count(self, trade_date: str, code: str) -> int:
        rows = _call(self.db, "list_managed_orders", trade_date=trade_date, code=code, limit=500) or []
        return sum(1 for row in rows if str(row.get("side") or "").upper() in {OrderSide.BUY.value, OrderSide.SELL.value})

    def _open_position_count(self) -> int:
        rows = _call(self.db, "list_live_sim_positions", status="OPEN", limit=1000) or []
        return len(rows)

    def _has_open_position(self, code: str) -> bool:
        rows = _call(self.db, "list_live_sim_positions", status="OPEN", code=code, limit=10) or []
        return bool(rows)

    def _has_pending_order(self, code: str) -> bool:
        finder = getattr(self.db, "find_active_managed_order_by_code", None)
        return bool(finder(code)) if callable(finder) else False

    def _theme_exposure_count(self, theme_id: str) -> int:
        rows = _call(self.db, "list_live_sim_positions", status="OPEN", limit=1000) or []
        count = 0
        for row in rows:
            details = dict(row.get("details") or {})
            if str(details.get("theme_id") or "") == theme_id:
                count += 1
        return count

    def _broker_snapshot(self) -> dict[str, Any]:
        try:
            snapshot = self.gateway_state.snapshot()
            payload = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot or {})
        except Exception:
            payload = {}
        payload["broker_env"] = broker_environment_state(payload)
        payload["account_whitelisted"] = self._account_whitelisted(str(payload.get("account") or ""))
        return payload

    def _account_whitelisted(self, account: str) -> bool:
        whitelist = tuple(self.config.live_sim_account_whitelist or ())
        return bool(account) and (not whitelist or account in whitelist)

    def _limit_details(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "max_order_quantity": self.config.max_order_quantity,
            "max_order_amount": self.config.max_order_amount,
            "quantity": int(payload.get("quantity") or 0),
            "price": int(payload.get("price") or 0),
        }

    def _operator_message(self, decision: str, reason_codes: list[str]) -> str:
        if decision == OrderRiskResult.PASS.value:
            return "Order risk passed for LIVE_SIM."
        if decision == OrderRiskResult.WAIT.value:
            return "Order waits for fresh data."
        if decision == OrderRiskResult.KILL_SWITCH.value:
            return "Kill switch blocks new buy orders."
        return "Order rejected by LIVE_SIM risk guard."

    def _is_kill_switch_reason(self, reason_codes: list[str]) -> bool:
        return any(code in {"KILL_SWITCH_BLOCKS_BUY", "POSITION_RISK_KILL_SWITCH_RECOMMENDED"} for code in reason_codes)


def broker_environment_state(snapshot: dict[str, Any]) -> str:
    heartbeat = dict(snapshot.get("last_heartbeat_payload") or {})
    raw_values = [
        snapshot.get("broker_env"),
        snapshot.get("account_mode"),
        snapshot.get("server_mode"),
        snapshot.get("server_gubun"),
        snapshot.get("mode"),
        heartbeat.get("broker_env"),
        heartbeat.get("account_mode"),
        heartbeat.get("server_mode"),
        heartbeat.get("server_gubun"),
        heartbeat.get("mode"),
    ]
    normalized = {str(value or "").strip().upper() for value in raw_values if str(value or "").strip()}
    if normalized & REAL_ENV_VALUES:
        return "REAL"
    if normalized & SIMULATION_ENV_VALUES:
        return "SIMULATION"
    return "UNKNOWN"


def _call(target: Any, name: str, **kwargs):
    fn = getattr(target, name, None)
    if not callable(fn):
        return None
    return fn(**kwargs)


def _age_sec(value: str, now: datetime) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    if now.tzinfo is None:
        now = now.replace(tzinfo=parsed.tzinfo)
    return max(0.0, (now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())


def _dedupe(values: list[str]) -> list[str]:
    return list(Counter(str(item) for item in values if item).keys())
