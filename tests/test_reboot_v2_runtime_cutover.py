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


class _Pipeline:
    def __init__(self, status="OK"):
        self.config = type("Config", (), {"enabled": True})()
        self.status = status

    def run_if_due(self, now=None):
        return {"enabled": True, "status": self.status, "calculated_at": now.isoformat() if now else ""}


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
