from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.strategy.candidates import CandidateCollector
from trading.strategy.candles import CandleBuilder
from trading.strategy.conditions import ConditionProfileRepository, ensure_default_condition_profiles, ensure_theme_lab_condition_profiles
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
from trading.theme_engine.backfill import ThemeBackfillConfig, ThemeBackfillService
from trading.theme_engine.context_provider import DynamicThemeContextProvider
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.runtime import RealTimeThemeRuntime
from trading.theme_engine.runtime_pipeline import ThemeLabRuntimePipeline, theme_lab_config_from_settings
from trading.theme_engine.lab import ThemeLabFlowEngine
from trading_app.dependencies import CoreSettings
from trading_app.runtime_adapters import (
    GatewayCommandConditionAdapter,
    GatewayCommandRealtimeClient,
    GatewayEventThemeRuntimeBridge,
    GatewayEventMarketDataBridge,
)
from trading_app.order_enqueue_service import OrderEnqueueService
from trading_app.runtime_order_sink import DryRunRuntimeOrderSink, LiveSimRuntimeOrderSink, NoopRuntimeOrderSink


@dataclass
class CoreRuntimeBundle:
    runtime: StrategyRuntime
    market_data_bridge: GatewayEventMarketDataBridge
    db: TradingDatabase
    theme_runtime: Any = None
    theme_runtime_bridge: Any = None
    order_sink: Any = None


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
    theme_lab_condition_seed_result = ensure_theme_lab_condition_profiles(
        db,
        condition_names=config.theme_lab_condition_names,
        condition_purposes=config.theme_lab_condition_purposes,
    )
    config.order_mode = OrderMode.OBSERVE
    if settings.runtime_evaluation_interval_sec > 0:
        config.evaluation_interval_sec = settings.runtime_evaluation_interval_sec
    config.exit_context_risk_enabled = bool(settings.exit_context_risk_enabled)

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
        purpose_filter=set(config.theme_lab_condition_purposes.values()) if config.theme_engine_mode == "themelab_flow" else None,
    )
    candidate_collector = CandidateCollector(db, client=condition_adapter)
    theme_repository = ThemeEngineRepository(db)
    theme_context_provider = DynamicThemeContextProvider(theme_repository)
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
    theme_runtime = RealTimeThemeRuntime(theme_repository)
    theme_runtime_bridge = GatewayEventThemeRuntimeBridge(theme_runtime, warning_sink=warning_sink)
    order_sink = _build_order_sink(settings, gateway_state, warning_sink, runtime_settings=runtime_settings)
    theme_lab_shadow_ab_provider = _theme_lab_shadow_ab_provider(db, runtime_settings)
    shadow_small_entry_promotion_provider = _shadow_small_entry_promotion_provider(db, runtime_settings)
    theme_lab_pipeline = None
    if config.theme_engine_mode == "themelab_flow":
        theme_backfill_service = ThemeBackfillService(
            gateway_state,
            config=ThemeBackfillConfig.from_env(trading_mode=settings.mode),
        )
        theme_lab_pipeline = ThemeLabRuntimePipeline(
            db=db,
            market_data=market_data,
            market_index_store=market_index_store,
            interval_sec=config.theme_lab_pipeline_interval_sec,
            engine=ThemeLabFlowEngine(theme_lab_config_from_settings(runtime_settings)),
            backfill_service=theme_backfill_service,
        )
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
        order_sink=order_sink,
        theme_lab_pipeline=theme_lab_pipeline,
        theme_lab_shadow_ab_provider=theme_lab_shadow_ab_provider,
        shadow_small_entry_promotion_provider=shadow_small_entry_promotion_provider,
    )
    readiness_report = build_readiness_report(
        db,
        subscription_manager=runtime.subscription_manager,
        theme_engine_mode=config.theme_engine_mode,
        theme_lab_flow_wired=theme_lab_pipeline is not None,
        condition_adapter=condition_adapter,
    )
    runtime.readiness_report = readiness_report
    runtime.startup_warnings = dedupe_warnings(
        list(config_result.warnings)
        + runtime_settings.validation_warnings
        + condition_seed_result.warnings
        + theme_lab_condition_seed_result.warnings
        + readiness_report.warnings
    )
    return CoreRuntimeBundle(
        runtime=runtime,
        market_data_bridge=market_data_bridge,
        theme_runtime=theme_runtime,
        theme_runtime_bridge=theme_runtime_bridge,
        db=db,
        order_sink=order_sink,
    )


def _theme_lab_shadow_ab_provider(
    db: TradingDatabase,
    runtime_settings: Any,
) -> Callable[[str], dict[str, Any]] | None:
    policy = dict(runtime_settings.value("theme_lab_shadow_small_entry", {}) or {})
    if str(policy.get("enabled") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None

    def provider(trade_date: str) -> dict[str, Any]:
        from trading_app.theme_lab_gate_reason_outcomes import ThemeLabGateReasonOutcomeAnalyzer

        configured_date = str(policy.get("report_trade_date") or "").strip()
        report_trade_date = configured_date or str(trade_date or "").strip() or None
        return ThemeLabGateReasonOutcomeAnalyzer(db).build_report(
            trade_date=report_trade_date,
            limit=10_000,
        )

    return provider


def _shadow_small_entry_promotion_provider(
    db: TradingDatabase,
    runtime_settings: Any,
) -> Callable[[str], dict[str, Any]] | None:
    policy = dict(runtime_settings.value("shadow_small_entry_promotion", {}) or {})
    if str(policy.get("enabled", True)).strip().lower() not in {"1", "true", "yes", "on"}:
        return None

    def provider(trade_date: str) -> dict[str, Any]:
        from trading_app.shadow_small_entry_promotion import ShadowSmallEntryPromotionAnalyzer

        return ShadowSmallEntryPromotionAnalyzer(db, settings=runtime_settings).load_evidence(
            trade_date=str(trade_date or "").strip() or None,
            limit=50_000,
        )

    return provider


def _build_order_sink(
    settings: CoreSettings,
    gateway_state: GatewayStateStore,
    warning_sink: Callable[[str], None] | None,
    runtime_settings: Any | None = None,
):
    if settings.runtime_allow_live_orders and warning_sink is not None:
        warning_sink("RUNTIME_LIVE_ORDERS_DISABLED_IN_PR5")
    execution = dict(runtime_settings.value("order_execution", {}) or {}) if runtime_settings is not None else {}
    execution_mode = str(execution.get("mode") or "DRY_RUN").upper()
    if execution_mode == "LIVE_REAL" or bool(execution.get("live_real_enabled")):
        if warning_sink is not None:
            warning_sink("LIVE_REAL_ORDER_BLOCKED")
        return NoopRuntimeOrderSink(reason="LIVE_REAL_ORDER_BLOCKED")
    if execution_mode == "LIVE_SIM" and bool(execution.get("live_sim_enabled")):
        if settings.runtime_mode == "DRY_RUN" and settings.runtime_allow_dry_run_orders:
            service = OrderEnqueueService(
                settings=settings,
                gateway_state=gateway_state,
                db_path=settings.db_path,
            )
            return LiveSimRuntimeOrderSink(settings=settings, service=service, warning_sink=warning_sink, runtime_settings=runtime_settings)
        if warning_sink is not None:
            warning_sink("LIVE_SIM_REQUIRES_DRY_RUN_RUNTIME")
        return NoopRuntimeOrderSink(reason="LIVE_SIM_REQUIRES_DRY_RUN_RUNTIME")
    if settings.runtime_mode == "DRY_RUN" and settings.runtime_allow_dry_run_orders:
        service = OrderEnqueueService(
            settings=settings,
            gateway_state=gateway_state,
            db_path=settings.db_path,
        )
        return DryRunRuntimeOrderSink(settings=settings, service=service, warning_sink=warning_sink)
    if settings.runtime_mode == "DRY_RUN" and warning_sink is not None:
        warning_sink("DRY_RUN_ORDER_ENQUEUE_DISABLED")
    return NoopRuntimeOrderSink(
        reason="DRY_RUN_ORDER_ENQUEUE_DISABLED"
        if settings.runtime_mode == "DRY_RUN"
        else "OBSERVE_VIRTUAL_ONLY"
    )
