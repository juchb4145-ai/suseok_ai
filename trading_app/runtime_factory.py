from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.strategy.candidates import CandidateCollector
from trading.strategy.candles import CandleBuilder
from trading.strategy.conditions import ConditionProfileRepository, ensure_default_condition_profiles
from trading.strategy.config import StrategyRuntimeConfigRepository
from trading.strategy.entry import EntryPlanBuilder
from trading.strategy.exit import ExitDecisionEngine, VirtualPositionService
from trading.strategy.holding import StaticHoldingProvider
from trading.strategy.hybrid_validation import HybridValidationRepository
from trading.strategy.indicators import IndicatorCalculator
from trading.strategy.intraday import IntradayStateTracker
from trading.strategy.market_data import MarketDataStore
from trading.strategy.market_index import MarketIndexStore
from trading.strategy.models import OrderMode
from trading.strategy.pipeline import GatePipeline
from trading.strategy.readiness import build_readiness_report, dedupe_warnings
from trading.strategy.realtime import RealTimeSubscriptionManager
from trading.strategy.review import TradeReviewService
from trading.strategy.runtime import StrategyRuntime
from trading.strategy.runtime_settings import StrategyRuntimeSettingsRepository
from trading.strategy.virtual_orders import VirtualOrderService
from trading.theme_engine.context_provider import DynamicThemeContextProvider
from trading.theme_engine.repository import ThemeEngineRepository
from trading_app.dependencies import CoreSettings
from trading_app.runtime_adapters import (
    GatewayCommandConditionAdapter,
    GatewayCommandRealtimeClient,
    GatewayEventMarketDataBridge,
)


@dataclass
class CoreRuntimeBundle:
    runtime: StrategyRuntime
    market_data_bridge: GatewayEventMarketDataBridge
    db: TradingDatabase


def build_core_strategy_runtime(
    db: TradingDatabase,
    gateway_state: GatewayStateStore,
    *,
    settings: CoreSettings,
    warning_sink: Callable[[str], None] | None = None,
) -> CoreRuntimeBundle:
    config_result = StrategyRuntimeConfigRepository(db).load()
    runtime_settings = StrategyRuntimeSettingsRepository(db).load()
    condition_seed_result = ensure_default_condition_profiles(db)
    config = config_result.config
    config.order_mode = OrderMode.OBSERVE
    if settings.runtime_evaluation_interval_sec > 0:
        config.evaluation_interval_sec = settings.runtime_evaluation_interval_sec

    market_data = MarketDataStore()
    candle_builder = CandleBuilder()
    market_index_store = MarketIndexStore()
    market_data_bridge = GatewayEventMarketDataBridge(
        market_data,
        candle_builder,
        market_index_store,
        warning_sink=warning_sink,
    )

    condition_adapter = GatewayCommandConditionAdapter(
        gateway_state,
        ConditionProfileRepository(db),
        warning_sink=warning_sink,
        require_gateway_heartbeat=settings.runtime_require_gateway_heartbeat,
        require_kiwoom_login=settings.runtime_require_kiwoom_login,
    )
    candidate_collector = CandidateCollector(db, client=condition_adapter)
    theme_context_provider = DynamicThemeContextProvider(ThemeEngineRepository(db))
    indicator_calculator = IndicatorCalculator(market_data, candle_builder)
    gate_pipeline = GatePipeline(
        theme_context_provider,
        market_data,
        candle_builder,
        indicator_calculator,
        IntradayStateTracker(runtime_settings),
        market_index_store,
        runtime_settings,
        hybrid_validation_repository=HybridValidationRepository(db),
    )
    realtime_client = GatewayCommandRealtimeClient(gateway_state, warning_sink=warning_sink)
    runtime = StrategyRuntime(
        db=db,
        candidate_collector=candidate_collector,
        subscription_manager=RealTimeSubscriptionManager(realtime_client, max_codes=config.realtime_subscription_limit),
        candle_builder=candle_builder,
        gate_pipeline=gate_pipeline,
        entry_plan_builder=EntryPlanBuilder(settings=runtime_settings),
        virtual_order_service=VirtualOrderService(db=db, settings=runtime_settings),
        virtual_position_service=VirtualPositionService(db=db),
        exit_decision_engine=ExitDecisionEngine(runtime_settings),
        trade_review_service=TradeReviewService(runtime_settings),
        config=config,
        condition_adapter=condition_adapter,
        holding_provider=StaticHoldingProvider(set(config.holding_watch_codes)),
    )
    readiness_report = build_readiness_report(db, subscription_manager=runtime.subscription_manager)
    runtime.readiness_report = readiness_report
    runtime.startup_warnings = dedupe_warnings(
        list(config_result.warnings)
        + runtime_settings.validation_warnings
        + condition_seed_result.warnings
        + readiness_report.warnings
    )
    return CoreRuntimeBundle(runtime=runtime, market_data_bridge=market_data_bridge, db=db)
