from datetime import datetime, timedelta, timezone

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent, utc_timestamp
from trading.strategy.candidate_ingestion import CandidateIngestionService, CandidateSourceEvent
from trading.strategy.models import CandidateState
from trading.strategy.order_manager import OrderManagerConfig, OrderManagerRuntimePipeline
from trading.strategy.order_models import ManagedOrderIntent, ManagedOrderStatus, OrderIntentStatus, OrderKillSwitchState
from trading.strategy.order_risk import OrderRiskManager


NOW = datetime(2026, 6, 18, 9, 10, tzinfo=timezone.utc)
TRADE_DATE = "2026-06-18"


def test_order_intent_flag_off_blocks_local_intent_even_when_manager_enabled(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = _sim_gateway()
    candidate = _candidate(db, "200001")
    _entry_decision(db, "200001", candidate_id=candidate.id)

    summary = OrderManagerRuntimePipeline(
        db=db,
        gateway_state=gateway,
        config=_observe_config(intent_enabled=False),
    ).run(NOW)

    assert summary["enabled"] is True
    assert summary["intent_enabled"] is False
    assert "ORDER_INTENT_DISABLED" in summary["warnings"]
    assert db.list_managed_order_intents(limit=10) == []
    assert gateway.command_snapshot()["queued_count"] == 0


def test_order_manager_hardening_defaults_are_observe_only_closed():
    config = OrderManagerConfig()

    assert config.enabled is False
    assert config.intent_enabled is False
    assert config.intent_shadow_mode is True
    assert config.observe_only is True
    assert config.create_local_order is False
    assert config.enqueue_gateway_command is False
    assert config.send_order_allowed is False
    assert config.require_simulation_broker is True
    assert config.block_real_broker is True


def test_observe_only_creates_local_order_and_blocks_gateway_command(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = _sim_gateway()
    candidate = _candidate(db, "200002")
    _entry_decision(db, "200002", candidate_id=candidate.id)

    summary = OrderManagerRuntimePipeline(
        db=db,
        gateway_state=gateway,
        config=_observe_config(),
    ).run(NOW)

    intents = db.list_managed_order_intents(limit=10)
    orders = db.list_managed_orders(limit=10)
    reloaded = db.load_candidate(TRADE_DATE, "200002")

    assert summary["created_intent_count"] == 1
    assert summary["risk_approved_count"] == 1
    assert summary["local_order_created_count"] == 1
    assert summary["command_blocked_observe_only_count"] == 1
    assert summary["queued_command_count"] == 0
    assert intents[0]["status"] == OrderIntentStatus.COMMAND_BLOCKED_OBSERVE_ONLY.value
    assert orders[0]["status"] == ManagedOrderStatus.COMMAND_BLOCKED_OBSERVE_ONLY.value
    assert gateway.command_snapshot()["queued_count"] == 0
    assert reloaded.metadata["candidate_fsm"]["blocking_stage"] == "ORDER"
    assert reloaded.metadata["candidate_fsm"]["primary_reason_code"] == "ORDER_MANAGER_OBSERVE_ONLY"


def test_duplicate_idempotency_key_is_not_recreated(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = _sim_gateway()
    candidate = _candidate(db, "200003")
    _entry_decision(db, "200003", candidate_id=candidate.id)
    pipeline = OrderManagerRuntimePipeline(db=db, gateway_state=gateway, config=_observe_config())

    first = pipeline.run(NOW)
    second = pipeline.run(NOW + timedelta(seconds=2))

    assert first["created_intent_count"] == 1
    assert second["created_intent_count"] == 0
    assert len(db.list_managed_order_intents(limit=10)) == 1
    assert len(db.list_managed_orders(limit=10)) == 1


def test_stale_tick_rejects_risk_and_marks_candidate_risk_block(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = _sim_gateway()
    candidate = _candidate(db, "200004")
    _entry_decision(
        db,
        "200004",
        candidate_id=candidate.id,
        details={"last_tick_at": (NOW - timedelta(seconds=30)).isoformat()},
    )

    summary = OrderManagerRuntimePipeline(
        db=db,
        gateway_state=gateway,
        config=_observe_config(stale_tick_sec=10, quote_stale_after_sec=9999),
    ).run(NOW)

    risk = db.latest_order_risk_decisions(limit=1)[0]
    reloaded = db.load_candidate(TRADE_DATE, "200004")

    assert summary["risk_rejected_count"] == 1
    assert "STALE_TICK" in risk["reason_codes"]
    assert db.list_managed_orders(limit=10) == []
    assert reloaded.metadata["candidate_fsm"]["blocking_stage"] == "RISK"
    assert reloaded.metadata["candidate_fsm"]["primary_reason_code"] == "STALE_TICK"


def test_ack_timeout_sets_reconcile_required_and_stop_new_buy(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = _sim_gateway()
    intent = db.save_managed_order_intent(
        {
            "trade_date": TRADE_DATE,
            "source": "TEST_ONLY",
            "side": "BUY",
            "code": "200005",
            "quantity": 1,
            "price": 1000,
            "idempotency_key": "buy:reconcile",
            "status": OrderIntentStatus.COMMAND_QUEUED.value,
        }
    )
    order = db.save_managed_order(
        {
            "intent_id": intent["id"],
            "trade_date": TRADE_DATE,
            "source": "TEST_ONLY",
            "side": "BUY",
            "code": "200005",
            "quantity": 1,
            "price": 1000,
            "status": ManagedOrderStatus.QUEUED_TO_GATEWAY.value,
            "sent_at": (NOW - timedelta(seconds=60)).isoformat(),
            "idempotency_key": "buy:reconcile",
        }
    )

    summary = OrderManagerRuntimePipeline(
        db=db,
        gateway_state=gateway,
        config=_observe_config(ack_timeout_sec=30),
    ).run(NOW)
    updated = db.get_managed_order(order["id"])
    kill = db.latest_order_kill_switch_state(trade_date=TRADE_DATE)

    assert summary["reconcile_required_count"] == 1
    assert updated["status"] == ManagedOrderStatus.RECONCILE_REQUIRED.value
    assert kill["state"] == OrderKillSwitchState.STOP_NEW_BUY.value
    assert "ORDER_ACK_TIMEOUT" in kill["reason_codes"]


def test_market_side_budget_observe_only_records_diagnostics_without_rejecting_buy(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = _sim_gateway()
    _portfolio_budget(db, action="STOP_NEW_ENTRY", reserved=9000, limit=9000)
    config = _observe_config(
        market_side_portfolio_enabled=True,
        market_side_portfolio_enforce_buy_limits=False,
        market_side_budget_max_age_sec=9999,
        max_open_positions=99,
    )
    intent = _intent("BUY", "400001", details={"market_side": "KOSDAQ"})

    decision = OrderRiskManager(db, gateway, config).evaluate(intent, now=NOW)

    assert decision.approved is True
    assert "MARKET_SIDE_STOP_NEW_ENTRY" not in decision.reason_codes
    diagnostic = decision.details["limits"]["market_side_budget"]
    assert "MARKET_SIDE_STOP_NEW_ENTRY" in diagnostic["informational_reason_codes"]
    assert "MARKET_SIDE_BUDGET_OBSERVE_ONLY" in diagnostic["informational_reason_codes"]


def test_market_side_budget_enforce_rejects_buy_over_side_limit(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = _sim_gateway()
    _portfolio_budget(db, action="ALLOW_BUDGET", reserved=9000, limit=9500)
    config = _observe_config(
        market_side_portfolio_enabled=True,
        market_side_portfolio_enforce_buy_limits=True,
        market_side_budget_max_age_sec=9999,
        max_open_positions=99,
    )
    intent = _intent("BUY", "400002", quantity=1, price=1000, details={"market_side": "KOSDAQ"})

    decision = OrderRiskManager(db, gateway, config).evaluate(intent, now=NOW)

    assert decision.approved is False
    assert "MARKET_SIDE_EXPOSURE_LIMIT" in decision.reason_codes


def test_pending_buy_reservation_is_diagnostic_when_projected_exposure_is_within_limit(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = _sim_gateway()
    _portfolio_budget(db, action="ALLOW_BUDGET", reserved=1000, limit=10_000, pending=1000)
    config = _observe_config(
        market_side_portfolio_enabled=True,
        market_side_portfolio_enforce_buy_limits=True,
        market_side_budget_max_age_sec=9999,
        max_open_positions=99,
    )
    intent = _intent("BUY", "400004", quantity=1, price=1000, details={"market_side": "KOSDAQ"})

    decision = OrderRiskManager(db, gateway, config).evaluate(intent, now=NOW)

    assert decision.approved is True
    assert "PENDING_BUY_EXPOSURE_RESERVED" not in decision.reason_codes
    assert "PENDING_BUY_EXPOSURE_RESERVED" in decision.details["limits"]["market_side_budget"]["informational_reason_codes"]


def test_market_side_budget_does_not_block_sell(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = _sim_gateway()
    _portfolio_budget(db, action="STOP_NEW_ENTRY", reserved=9000, limit=9000)
    config = _observe_config(
        market_side_portfolio_enabled=True,
        market_side_portfolio_enforce_buy_limits=True,
        market_side_budget_max_age_sec=9999,
        max_open_positions=99,
    )
    intent = _intent("SELL", "400003", details={"market_side": "KOSDAQ"})

    decision = OrderRiskManager(db, gateway, config).evaluate(intent, now=NOW)

    assert decision.approved is True
    assert not any(str(reason).startswith("MARKET_SIDE_") for reason in decision.reason_codes)


def _observe_config(**overrides) -> OrderManagerConfig:
    payload = {
        "enabled": True,
        "intent_enabled": True,
        "create_local_order": True,
        "enqueue_gateway_command": False,
        "send_order_allowed": False,
        "mode": "LIVE_SIM",
        "allow_live_sim_orders": True,
        "observe_only": True,
        "live_sim_account_whitelist": ("12345678",),
        "decision_stale_after_sec": 9999,
        "quote_stale_after_sec": 9999,
    }
    payload.update(overrides)
    return OrderManagerConfig(**payload)


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


def _candidate(db: TradingDatabase, code: str):
    candidate = CandidateIngestionService(db).ingest(
        CandidateSourceEvent(
            trade_date=TRADE_DATE,
            code=code,
            name=f"Stock {code}",
            source_type="condition_search",
            source_id=f"condition:{code}",
            source_score=50.0,
            theme_id="theme-a",
            theme_name="Theme A",
            detected_at=f"{TRADE_DATE}T09:01:00",
        )
    ).candidate
    candidate.state = CandidateState.WATCHING
    candidate.metadata["candidate_fsm"] = {
        "v2_state": "TIMING_READY",
        "blocking_stage": "NONE",
        "primary_reason_code": "OBSERVE_READY_ORDER_DISABLED",
        "latest_tick_fresh": "true",
        "price_source": "REALTIME",
        "freshness_status": "FRESH",
    }
    return db.save_candidate(candidate)


def _entry_decision(db: TradingDatabase, code: str, *, candidate_id: int | None = None, details: dict | None = None) -> None:
    db.save_entry_decisions(
        [
            {
                "trade_date": TRADE_DATE,
                "calculated_at": NOW.isoformat(),
                "candidate_id": candidate_id,
                "code": code,
                "name": code,
                "theme_id": "theme-a",
                "theme_name": "Theme A",
                "theme_status": "LEADING_THEME",
                "stock_role": "LEADER",
                "market_status": "EXPANSION",
                "market_action": "ALLOW_NORMAL",
                "price_location": "VWAP_RECLAIM",
                "entry_status": "OBSERVE_READY",
                "data_ready_status": "PASS",
                "theme_ready_status": "PASS",
                "market_ready_status": "PASS",
                "role_ready_status": "PASS",
                "price_timing_status": "PASS",
                "current_price": 1000,
                "limit_price_hint": 1000,
                "ready_allowed": True,
                "dry_run_intent_allowed": False,
                "live_order_allowed": False,
                "reason_codes": ["OBSERVE_READY_ORDER_DISABLED"],
                "details": details or {},
            }
        ]
    )


def _intent(side: str, code: str, *, quantity: int = 1, price: int = 1000, details: dict | None = None) -> ManagedOrderIntent:
    return ManagedOrderIntent(
        trade_date=TRADE_DATE,
        source="TEST_ONLY",
        side=side,
        code=code,
        quantity=quantity,
        price=price,
        idempotency_key=f"{side}:{code}",
        created_at=NOW.isoformat(),
        details=details or {},
    )


def _portfolio_budget(db: TradingDatabase, *, action: str, reserved: int, limit: int, pending: int = 0) -> None:
    db.save_portfolio_risk_snapshot(
        {
            "trade_date": TRADE_DATE,
            "calculated_at": NOW.isoformat(),
            "open_position_count": 1,
            "total_exposure": reserved,
            "gross_exposure_limit_krw": 100_000,
            "gross_reserved_exposure_krw": reserved,
            "market_side_budgets": {
                "KOSDAQ": {
                    "market_side": "KOSDAQ",
                    "budget_action": action,
                    "side_market_regime": "EXPANSION",
                    "effective_exposure_limit_krw": limit,
                    "reserved_exposure_krw": reserved,
                    "pending_buy_exposure_krw": pending,
                    "available_position_slots": 5,
                    "max_open_positions": 5,
                    "reason_codes": [],
                }
            },
            "details": {
                "market_side_budgets": {
                    "KOSDAQ": {
                        "market_side": "KOSDAQ",
                        "budget_action": action,
                        "side_market_regime": "EXPANSION",
                        "effective_exposure_limit_krw": limit,
                        "reserved_exposure_krw": reserved,
                        "pending_buy_exposure_krw": pending,
                        "available_position_slots": 5,
                        "max_open_positions": 5,
                        "reason_codes": [],
                    }
                }
            },
        }
    )
