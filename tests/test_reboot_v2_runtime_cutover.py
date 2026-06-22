from datetime import datetime, timedelta

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent
from trading.strategy.candidate_hydrator import CandidateHydrator
from trading.strategy.candidate_ingestion import CandidateIngestionService, CandidateSourceEvent
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore
from trading.strategy.market_index import MarketIndexStore
from trading.strategy.models import CandidateState
from trading.strategy.realtime import RealTimeSubscriptionManager
from trading.strategy.reboot_v2 import RebootV2RuntimeProfile
from trading.strategy.reboot_v2_runtime import RebootV2Runtime
from trading.strategy.runtime import StrategyRuntimeConfig
from trading_app.runtime_adapters import GatewayCommandRealtimeClient, GatewayEventMarketDataBridge
from trading.theme_engine.candidate_bridge import CandidateBridge
from trading.theme_engine.roles import RawStockRole, StockRoleDecision, TradeStockRole
from trading.theme_engine.state_machine import ThemeCoreState, ThemeStateSnapshot


class _Pipeline:
    def __init__(self, status="OK"):
        self.config = type("Config", (), {"enabled": True})()
        self.status = status

    def run_if_due(self, now=None, **_):
        return {"enabled": True, "status": self.status, "calculated_at": now.isoformat() if now else ""}


class _CapturePipeline:
    config = type("Config", (), {"enabled": True})()

    def __init__(self):
        self.market_context = {}
        self.last_result = {}

    def run_if_due(self, now=None, **kwargs):
        self.market_context = dict(kwargs.get("market_context") or {})
        self.last_result = {"trade_date": now.date().isoformat() if now else "", "top_themes": [], "stocks": []}
        return {"enabled": True, "status": "OK", "calculated_at": now.isoformat() if now else ""}


class _MarketSnapshot:
    def __init__(self, payload):
        self.payload = payload

    def to_dict(self):
        return dict(self.payload)


class _MarketResult:
    def __init__(self, payload):
        self.snapshot = _MarketSnapshot(payload)


class _DashboardMarketPipeline:
    config = type("Config", (), {"enabled": True})()

    def __init__(self, full_payload):
        self.last_result = _MarketResult(full_payload)
        self.full_payload = full_payload

    def run_if_due(self, now=None):
        self.last_result = _MarketResult(self.full_payload)
        return {
            "enabled": True,
            "status": "OK",
            "calculated_at": now.isoformat() if now else "",
            "global_status": self.full_payload["global_status"],
            "kospi_status": self.full_payload["kospi_status"],
            "kosdaq_status": self.full_payload["kosdaq_status"],
        }


def test_reboot_v2_passes_full_market_snapshot_to_downstream_context(tmp_path):
    db = TradingDatabase(str(tmp_path / "v2_market_context.db"))
    gateway = GatewayStateStore()
    theme_board = _CapturePipeline()
    strategy_context = _CapturePipeline()
    full_market = {
        "trade_date": "2026-06-17",
        "calculated_at": "2026-06-17T09:20:00",
        "global_status": "SELECTIVE",
        "kospi_status": "SELECTIVE",
        "kosdaq_status": "WEAK",
        "kospi_snapshot": {"side": "KOSPI", "status": "SELECTIVE", "index_return_pct": 0.7},
        "kosdaq_snapshot": {"side": "KOSDAQ", "status": "WEAK", "index_return_pct": -1.2},
        "candidate_policy_by_code": {
            "000001": {
                "code": "000001",
                "market_side": "KOSPI",
                "market_status": "SELECTIVE",
                "global_market_status": "SELECTIVE",
                "market_action": "ALLOW_REDUCED",
                "block_new_entry": False,
                "position_size_multiplier_hint": 0.6,
                "reason_codes": ["SIDE_MARKET_SELECTIVE_REDUCED"],
            }
        },
    }
    runtime = RebootV2Runtime(
        db=db,
        subscription_manager=RealTimeSubscriptionManager(GatewayCommandRealtimeClient(gateway), max_codes=20),
        candle_builder=CandleBuilder(),
        market_data=MarketDataStore(),
        market_index_store=MarketIndexStore(),
        config=StrategyRuntimeConfig(max_candidates_to_watch=20, realtime_subscription_limit=20),
        profile=RebootV2RuntimeProfile.V2_OBSERVE,
        theme_board_pipeline=theme_board,
        market_regime_pipeline=_DashboardMarketPipeline(full_market),
        strategy_context_pipeline=strategy_context,
    )

    runtime.cycle(datetime(2026, 6, 17, 9, 20, 0))

    assert theme_board.market_context["candidate_policy_by_code"]["000001"]["market_action"] == "ALLOW_REDUCED"
    assert strategy_context.market_context["candidate_policy_by_code"]["000001"]["market_action"] == "ALLOW_REDUCED"
    assert strategy_context.market_context["kosdaq_snapshot"]["status"] == "WEAK"


def test_reboot_v2_condition_hydration_subscription_cutover_blocks_legacy_orders(tmp_path):
    db = TradingDatabase(str(tmp_path / "v2_cutover.db"))
    gateway = GatewayStateStore()
    market_data = MarketDataStore()
    candle_builder = CandleBuilder()
    market_index_store = MarketIndexStore()
    config = StrategyRuntimeConfig(max_candidates_to_watch=20, realtime_subscription_limit=20)
    runtime = RebootV2Runtime(
        db=db,
        subscription_manager=RealTimeSubscriptionManager(GatewayCommandRealtimeClient(gateway), max_codes=20),
        candle_builder=candle_builder,
        market_data=market_data,
        market_index_store=market_index_store,
        config=config,
        profile=RebootV2RuntimeProfile.V2_OBSERVE,
        candidate_ingestion_service=CandidateIngestionService(db),
        candidate_hydrator=CandidateHydrator(db, gateway, market_data=market_data, clock=lambda: datetime(2026, 6, 17, 9, 0, 20)),
        opening_burst_pipeline=_Pipeline(),
        theme_board_pipeline=_Pipeline(),
        market_regime_pipeline=_Pipeline(),
        entry_engine_pipeline=_Pipeline(status="WAIT"),
    )

    runtime.start(datetime(2026, 6, 17, 9, 0, 0))
    runtime.candidate_ingestion_service.ingest(
        CandidateSourceEvent(
            trade_date="2026-06-17",
            code="005930",
            name="Samsung",
            source_type="condition_search",
            source_id="theme_leader",
            theme_id="semis",
            theme_name="Semiconductors",
            stock_role="LEADER",
            source_score=88.0,
            detected_at="2026-06-17T09:00:05",
        )
    )
    first = runtime.cycle(datetime(2026, 6, 17, 9, 0, 10))

    candidate = db.load_candidate("2026-06-17", "005930")
    assert candidate.state == CandidateState.HYDRATING
    tr_commands = [row for row in gateway.list_commands(include_finished=True, limit=50) if row["command_type"] == "tr_request"]
    assert len(tr_commands) == 1
    tr_payload = tr_commands[0]["command"]["payload"]
    assert tr_payload["purpose"] == "candidate_hydration"
    assert tr_payload["inputs"] == {"종목코드": "005930"}
    assert tr_payload["fields"] == ["종목명", "현재가", "등락율", "거래량", "거래대금", "시가", "고가", "저가", "기준가"]
    assert "005930" in runtime.subscription_manager.records
    register_commands = [
        row
        for row in gateway.list_commands(include_finished=True, limit=50)
        if row["command_type"] == "register_realtime"
    ]
    assert len(register_commands) == 1
    assert {"001", "101", "005930"} <= set(register_commands[0]["command"]["payload"]["codes"])
    assert first["base_realtime_subscription"]["active_count"] == 2
    assert first["candidate_realtime_subscription"]["active_count"] == 1
    assert first["legacy_runtime"]["entry_plan_count"] == 0

    command_id = tr_commands[0]["command_id"]
    runtime.candidate_hydrator.handle_event(
        GatewayEvent(
            type="command_ack",
            command_id=command_id,
            payload={
                "purpose": "candidate_hydration",
                "command_id": command_id,
                "trade_date": "2026-06-17",
                "code": "005930",
                "raw": {
                    "tr_rows": [
                        {
                            "종목명": "Samsung",
                            "현재가": "70000",
                            "등락율": "1.2",
                            "거래량": "1000",
                            "거래대금": "70000000",
                            "시가": "69000",
                            "고가": "70500",
                            "저가": "68800",
                            "기준가": "69100",
                        }
                    ]
                },
            },
        )
    )
    assert db.load_candidate("2026-06-17", "005930").state == CandidateState.WATCHING

    base = datetime(2026, 6, 17, 9, 1, 0)
    tick_times = [base + timedelta(minutes=index) for index in range(4)]
    bridge = GatewayEventMarketDataBridge(market_data, candle_builder, market_index_store, clock=lambda: tick_times.pop(0))
    for index in range(4):
        bridge.handle_event(
            GatewayEvent(
                type="price_tick",
                payload={
                    "code": "005930",
                    "price": 70000 + index * 100,
                    "change_rate": 1.2,
                    "volume": 1000 + index * 100,
                    "trade_time": (base + timedelta(minutes=index)).strftime("%H%M%S"),
                },
            )
        )
    assert market_data.latest_tick("005930") is not None
    assert len(candle_builder.completed_candles("005930", 1)) >= 3

    second = runtime.cycle(datetime(2026, 6, 17, 9, 5, 0))
    send_order_commands = [row for row in gateway.list_commands(include_finished=True, limit=100) if row["command_type"] == "send_order"]
    assert second["runtime_profile"] == "V2_OBSERVE"
    assert second["watching_candidate_count"] == 1
    assert second["entry_plan_count"] == 0
    assert second["virtual_order_count"] == 0
    assert send_order_commands == []


def test_theme_core_v3_candidate_bridge_emits_observe_events_without_ready_or_order_intent(tmp_path):
    db = TradingDatabase(str(tmp_path / "v3_bridge.db"))
    state = ThemeStateSnapshot(
        theme_id="ai",
        theme_name="AI",
        theme_state=ThemeCoreState.LEADING_THEME.value,
        leader_symbol="000001",
    )
    blocked_state = ThemeStateSnapshot(
        theme_id="weak",
        theme_name="Weak",
        theme_state=ThemeCoreState.WATCH_THEME.value,
        leader_symbol="000002",
    )

    result = CandidateBridge().build_events(
        [
            StockRoleDecision(
                code="000001",
                name="AI Leader",
                theme_id="ai",
                theme_name="AI",
                raw_role=RawStockRole.LEADER.value,
                trade_role=TradeStockRole.LEADER_CONFIRMED.value,
                role_score=91.0,
                theme_state=state,
            ),
            StockRoleDecision(
                code="000002",
                name="Weak Watch",
                theme_id="weak",
                theme_name="Weak",
                raw_role=RawStockRole.LEADER.value,
                trade_role=TradeStockRole.LEADER_CONFIRMED.value,
                role_score=88.0,
                theme_state=blocked_state,
            ),
        ],
        trade_date="2026-06-17",
        detected_at="2026-06-17T09:05:00",
    )

    assert len(result.events) == 1
    assert result.excluded[0].code == "000002"
    assert result.events[0].source_type == "theme_board"
    assert result.events[0].raw_payload["ready_allowed"] is False
    assert result.events[0].raw_payload["order_intent_allowed"] is False
    assert result.ready_allowed is False
    assert result.order_intent_allowed is False
    assert db.conn.execute("SELECT COUNT(*) AS count FROM entry_plans").fetchone()["count"] == 0
    assert db.list_runtime_order_intents(limit=10) == []
