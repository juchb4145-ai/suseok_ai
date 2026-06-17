from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from trading.strategy.order_models import OrderKillSwitchState, OrderManagerConfig


class OrderKillSwitchManager:
    def __init__(self, db: Any, config: OrderManagerConfig | None = None) -> None:
        self.db = db
        self.config = config or OrderManagerConfig.from_env()

    def evaluate(self, *, trade_date: str, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        previous = self.latest_state(trade_date=trade_date)
        previous_state = str(previous.get("state") or OrderKillSwitchState.NORMAL.value)
        previous_manual = bool(previous.get("manual_active"))
        reasons: list[str] = []
        state = OrderKillSwitchState.NORMAL.value

        if not self.config.kill_switch_enabled:
            state = OrderKillSwitchState.NORMAL.value
            reasons.append("KILL_SWITCH_DISABLED")
        elif previous_manual or previous_state == OrderKillSwitchState.KILL_SWITCH_ACTIVE.value and previous.get("manual_active"):
            state = OrderKillSwitchState.KILL_SWITCH_ACTIVE.value
            reasons.append("MANUAL_KILL_SWITCH_ACTIVE")
        else:
            portfolio = self._latest_portfolio(trade_date)
            if bool(portfolio.get("kill_switch_recommended")):
                state = OrderKillSwitchState.KILL_SWITCH_ACTIVE.value
                reasons.append("POSITION_RISK_KILL_SWITCH_RECOMMENDED")
            elif bool(portfolio.get("stop_new_entry_recommended")):
                state = OrderKillSwitchState.STOP_NEW_BUY.value
                reasons.append("POSITION_RISK_STOP_NEW_ENTRY")

            daily_pct = float(portfolio.get("daily_realized_pnl_pct") or 0.0)
            details = dict(portfolio.get("details") or {})
            daily_krw = float(details.get("daily_realized_pnl_krw") or details.get("daily_realized_pnl") or 0.0)
            if daily_pct <= float(self.config.daily_loss_limit_pct):
                state = OrderKillSwitchState.KILL_SWITCH_ACTIVE.value
                reasons.append("DAILY_LOSS_LIMIT_PCT")
            if daily_krw <= -abs(float(self.config.daily_loss_limit_krw)):
                state = OrderKillSwitchState.KILL_SWITCH_ACTIVE.value
                reasons.append("DAILY_LOSS_LIMIT_KRW")
            consecutive_losses = int(previous.get("consecutive_loss_count") or 0)
            if consecutive_losses >= self.config.consecutive_loss_limit:
                state = OrderKillSwitchState.REDUCE_ONLY.value
                reasons.append("CONSECUTIVE_LOSS_LIMIT")

        payload = {
            "trade_date": trade_date,
            "state": state,
            "reason_codes": _dedupe(reasons),
            "manual_active": previous_manual,
            "consecutive_loss_count": int(previous.get("consecutive_loss_count") or 0),
            "daily_realized_pnl_pct": float((self._latest_portfolio(trade_date) or {}).get("daily_realized_pnl_pct") or 0.0),
            "daily_realized_pnl_krw": float(
                dict((self._latest_portfolio(trade_date) or {}).get("details") or {}).get("daily_realized_pnl_krw") or 0.0
            ),
            "updated_at": now.isoformat(),
            "details": {
                "previous_state": previous_state,
                "config": {
                    "daily_loss_limit_pct": self.config.daily_loss_limit_pct,
                    "daily_loss_limit_krw": self.config.daily_loss_limit_krw,
                    "consecutive_loss_limit": self.config.consecutive_loss_limit,
                },
            },
        }
        saver = getattr(self.db, "save_order_kill_switch_state", None)
        if callable(saver):
            saver(payload)
        return payload

    def latest_state(self, *, trade_date: str | None = None) -> dict[str, Any]:
        loader = getattr(self.db, "latest_order_kill_switch_state", None)
        if callable(loader):
            state = loader(trade_date=trade_date)
            if state:
                return state
        return {
            "trade_date": trade_date or "",
            "state": OrderKillSwitchState.NORMAL.value,
            "reason_codes": [],
            "manual_active": False,
            "consecutive_loss_count": 0,
        }

    def _latest_portfolio(self, trade_date: str) -> dict[str, Any]:
        loader = getattr(self.db, "latest_portfolio_risk_snapshot", None)
        if callable(loader):
            return dict(loader(trade_date=trade_date) or {})
        return {}


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
