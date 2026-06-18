from datetime import datetime, timezone

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent, utc_timestamp
from trading.strategy.order_manager import OrderManagerConfig, OrderManagerRuntimePipeline
from trading.strategy.order_models import OrderIntentStatus


NOW = datetime(2026, 6, 18, 9, 10, tzinfo=timezone.utc)
TRADE_DATE = "2026-06-18"


def test_order_manager_default_disabled_creates_no_intent_or_command(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = _sim_gateway()
    _entry_decision(db, "100001")

    summary = OrderManagerRuntimePipeline(db=db, gateway_state=gateway, config=OrderManagerConfig()).run(NOW)

    assert summary["status"] == "DISABLED"
    assert db.list_managed_order_intents(limit=10) == []
    assert gateway.command_snapshot()["queued_count"] == 0


def test_live_sim_entry_decision_persists_intent_and_order_before_gateway_command(tmp_path, monkeypatch):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = _sim_gateway()
    _entry_decision(db, "100002")
    config = _enabled_config()
    observed = {}
    original_enqueue = gateway.enqueue_command

    def spy_enqueue(command, *args, **kwargs):
        observed["order_before_enqueue"] = db.find_managed_order_by_command_id(command.command_id) is None and bool(
            db.list_managed_orders(limit=10)
        )
        return original_enqueue(command, *args, **kwargs)

    monkeypatch.setattr(gateway, "enqueue_command", spy_enqueue)

    summary = OrderManagerRuntimePipeline(db=db, gateway_state=gateway, config=config).run(NOW)

    assert summary["queued_command_count"] == 1
    assert observed["order_before_enqueue"] is True
    intents = db.list_managed_order_intents(limit=10)
    orders = db.list_managed_orders(limit=10)
    assert intents[0]["status"] == OrderIntentStatus.COMMAND_QUEUED.value
    assert orders[0]["status"] == "QUEUED_TO_GATEWAY"
    command = gateway.list_commands(limit=1)[0]["command"]
    assert command["type"] == "send_order"
    assert command["payload"]["strategy"] == "reboot_v2_entry"
    assert command["payload"]["quantity"] == 1


def test_real_broker_is_rejected_even_when_live_sim_flags_are_enabled(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = _sim_gateway(broker_env="REAL", server_gubun="0")
    _entry_decision(db, "100003")

    summary = OrderManagerRuntimePipeline(db=db, gateway_state=gateway, config=_enabled_config()).run(NOW)

    assert summary["queued_command_count"] == 0
    assert gateway.command_snapshot()["queued_count"] == 0
    intent = db.list_managed_order_intents(limit=1)[0]
    risk = db.latest_order_risk_decisions(limit=1)[0]
    assert intent["status"] == OrderIntentStatus.RISK_REJECTED.value
    assert "REAL_BROKER_BLOCKED" in risk["reason_codes"]


def test_risk_off_market_rejects_new_buy_intent_without_gateway_command(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = _sim_gateway()
    _entry_decision(db, "100004", market_status="RISK_OFF")

    summary = OrderManagerRuntimePipeline(db=db, gateway_state=gateway, config=_enabled_config()).run(NOW)

    assert summary["queued_command_count"] == 0
    assert gateway.command_snapshot()["queued_count"] == 0
    risk = db.latest_order_risk_decisions(limit=1)[0]
    assert "MARKET_RISK_OFF_NEW_BUY_BLOCK" in risk["reason_codes"]


def _enabled_config() -> OrderManagerConfig:
    return OrderManagerConfig(
        enabled=True,
        intent_enabled=True,
        create_local_order=True,
        enqueue_gateway_command=True,
        send_order_allowed=True,
        mode="LIVE_SIM",
        allow_live_sim_orders=True,
        observe_only=False,
        live_sim_account_whitelist=("12345678",),
        decision_stale_after_sec=9999,
        quote_stale_after_sec=9999,
    )


def _sim_gateway(*, broker_env: str = "SIMULATION", server_gubun: str = "1") -> GatewayStateStore:
    gateway = GatewayStateStore()
    gateway.record_event(
        GatewayEvent(
            type="heartbeat",
            timestamp=utc_timestamp(),
            payload={
                "kiwoom_logged_in": True,
                "orderable": True,
                "account": "12345678",
                "mode": "LIVE_SIM",
                "broker_env": broker_env,
                "server_gubun": server_gubun,
            },
        )
    )
    return gateway


def _entry_decision(db: TradingDatabase, code: str, *, market_status: str = "EXPANSION") -> None:
    db.save_entry_decisions(
        [
            {
                "trade_date": TRADE_DATE,
                "calculated_at": NOW.isoformat(),
                "code": code,
                "name": code,
                "theme_id": "theme-a",
                "theme_name": "Theme A",
                "theme_status": "LEADING_THEME",
                "stock_role": "LEADER",
                "market_status": market_status,
                "market_action": "ALLOW_NORMAL",
                "price_location": "VWAP_RECLAIM",
                "entry_status": "OBSERVE_READY",
                "current_price": 1000,
                "limit_price_hint": 1000,
                "ready_allowed": True,
                "dry_run_intent_allowed": True,
                "reason_codes": [],
                "details": {},
            }
        ]
    )
