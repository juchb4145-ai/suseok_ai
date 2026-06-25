from __future__ import annotations

import os
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Any, Callable

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.strategy.candidates import CandidateCollector
from trading.strategy.candles import CandleBuilder
from trading.strategy.candidate_hydrator import CandidateHydrator
from trading.strategy.candidate_ingestion import CandidateIngestionService
from trading.strategy.conditions import ConditionProfileRepository, ensure_default_condition_profiles, ensure_theme_lab_condition_profiles
from trading.strategy.config import StrategyRuntimeConfigRepository
from trading.strategy.entry import EntryPlanBuilder
from trading.strategy.dirty_strategy_evaluator import (
    DirtyStrategyEvaluator,
    DirtyStrategyEvaluatorConfig,
    DirtyStrategyEvaluatorRuntimePipeline,
)
from trading.strategy.entry_engine import EntryEngineConfig, EntryEngineRuntimePipeline
from trading.strategy.exit import ExitDecisionEngine, VirtualPositionService
from trading.strategy.exit_engine_reboot import ExitEngineConfig, ExitEngineRuntimePipeline
from trading.strategy.holding import StaticHoldingProvider
from trading.strategy.hybrid_validation import HybridValidationRepository
from trading.strategy.indicators import IndicatorCalculator
from trading.strategy.intraday import IntradayStateTracker
from trading.strategy.market_data import MarketDataStore
from trading.strategy.market_index import MarketIndexStore
from trading.strategy.market_regime import MarketRegimeConfig, MarketRegimeRuntimePipeline
from trading.strategy.market_relative_strength_shadow import (
    MarketRelativeStrengthShadowConfig,
    MarketRelativeStrengthShadowRuntimePipeline,
)
from trading.strategy.models import OrderMode
from trading.strategy.order_manager import OrderManagerConfig, OrderManagerRuntimePipeline
from trading.strategy.pipeline import GatePipeline
from trading.strategy.position_risk import PositionRiskConfig, PositionRiskRuntimePipeline
from trading.strategy.readiness import build_readiness_report, dedupe_warnings
from trading.strategy.realtime import RealTimeSubscriptionManager
from trading.strategy.reboot_v2 import RebootV2RuntimeProfile, reboot_v2_runtime_profile
from trading.strategy.reboot_v2_runtime import RebootV2Runtime
from trading.strategy.strategy_baseline import (
    StrategyBaselineRuntimeConfig,
    StrategyBaselineService,
    build_strategy_baseline_snapshot,
)
from trading.strategy.candidate_funnel import (
    CandidateFunnelConfig,
    CandidateFunnelService,
    TradingDayQualificationConfig,
    TradingDayQualificationService,
)
from trading.strategy.opportunity_benchmark import (
    OpportunityBenchmarkConfig,
    OpportunityBenchmarkService,
)
from trading.strategy.champion_outcome_validator import (
    ChampionOutcomeValidatorConfig,
    ChampionOutcomeValidatorService,
)
from trading.strategy.subscription_lifecycle import RealtimeSubscriptionLifecycleTracker
from trading.strategy.subscription_readiness import RealtimeSubscriptionReadinessProvider
from trading.strategy.setup_runtime import SetupRouterV3RuntimePipeline
from trading.strategy.setup_router_v3 import SetupRouterConfig
from trading.strategy.strategy_context import StrategyContextRuntimePipeline
from trading.strategy.review import TradeReviewService
from trading.strategy.runtime import StrategyRuntime
from trading.strategy.runtime_settings import StrategyRuntimeSettingsRepository
from trading.strategy.virtual_orders import VirtualOrderService
from trading.theme_engine.backfill import ThemeBackfillConfig, ThemeBackfillService
from trading.theme_engine.context_provider import DynamicThemeContextProvider
from trading.theme_engine.core_v3_runtime import ThemeCoreV3RuntimeConfig, ThemeCoreV3RuntimePipeline
from trading.theme_engine.expansion_lease import ExpansionLeaseManager
from trading.theme_engine.intraday_discovery import IntradayDiscoveryConfig, IntradayDiscoveryRuntimePipeline
from trading.theme_engine.opening_runtime import OpeningBurstRuntimeConfig, OpeningThemeBurstRuntimePipeline
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.runtime import RealTimeThemeRuntime
from trading.theme_engine.runtime_pipeline import ThemeLabRuntimePipeline, theme_lab_config_from_settings
from trading.theme_engine.lab import ThemeLabFlowEngine
from trading_app.dependencies import CoreSettings
from trading_app.market_relative_strength_outcomes import (
    MarketRelativeStrengthOutcomeRuntimeConfig,
    MarketRelativeStrengthOutcomeRuntimePipeline,
)
from trading_app.runtime_adapters import (
    GatewayCommandConditionAdapter,
    GatewayCommandRealtimeClient,
    GatewayEventThemeRuntimeBridge,
    GatewayEventMarketDataBridge,
)
from trading_app.order_enqueue_service import OrderEnqueueService
from trading_app.runtime_order_sink import DryRunRuntimeOrderSink, LiveSimRuntimeOrderSink, NoopRuntimeOrderSink
from trading_app.runtime_load_guard import runtime_load_guard_from_theme_result


@dataclass
class CoreRuntimeBundle:
    runtime: Any
    market_data_bridge: GatewayEventMarketDataBridge
    db: TradingDatabase
    runtime_profile: str = RebootV2RuntimeProfile.LEGACY.value
    theme_runtime: Any = None
    theme_runtime_bridge: Any = None
    order_sink: Any = None
    candidate_ingestion_service: Any = None
    candidate_hydrator: Any = None
    opening_burst_pipeline: Any = None
    intraday_discovery_pipeline: Any = None
    theme_board_pipeline: Any = None
    market_regime_pipeline: Any = None
    strategy_context_pipeline: Any = None
    entry_engine_pipeline: Any = None
    dirty_strategy_evaluator: Any = None
    setup_router_v3_pipeline: Any = None
    market_relative_strength_shadow_pipeline: Any = None
    market_relative_strength_outcome_pipeline: Any = None
    exit_engine_reboot_pipeline: Any = None
    position_risk_pipeline: Any = None
    order_manager_pipeline: Any = None
    subscription_lifecycle_tracker: Any = None
    strategy_baseline_service: Any = None
    candidate_funnel_service: Any = None
    opportunity_benchmark_service: Any = None
    champion_outcome_service: Any = None
    trading_day_qualification_service: Any = None


def build_core_strategy_runtime(
    db: TradingDatabase,
    gateway_state: GatewayStateStore,
    *,
    settings: CoreSettings,
    warning_sink: Callable[[str], None] | None = None,
) -> CoreRuntimeBundle:
    profile = reboot_v2_runtime_profile()
    if profile == RebootV2RuntimeProfile.LEGACY:
        return build_legacy_runtime_bundle(
            db,
            gateway_state,
            settings=settings,
            warning_sink=warning_sink,
        )
    return build_reboot_v2_runtime_bundle(
        db,
        gateway_state,
        settings=settings,
        warning_sink=warning_sink,
        profile=profile,
    )


def build_legacy_runtime_bundle(
    db: TradingDatabase,
    gateway_state: GatewayStateStore,
    *,
    settings: CoreSettings,
    warning_sink: Callable[[str], None] | None = None,
) -> CoreRuntimeBundle:
    """Frozen legacy trading path.

    This bundle is kept only for explicit LEGACY rollback/debug operation. New
    strategy decisions and runtime wiring should be added to Reboot V2 instead.
    """
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
    candidate_ingestion_service = None
    candidate_hydrator = None
    theme_board_pipeline = None
    market_regime_pipeline = None
    strategy_context_pipeline = None
    entry_engine_pipeline = None
    dirty_strategy_evaluator = None
    market_relative_strength_shadow_pipeline = None
    market_relative_strength_outcome_pipeline = None
    exit_engine_reboot_pipeline = None
    position_risk_pipeline = None
    order_manager_pipeline = None
    theme_lab_pipeline = None
    if config.theme_engine_mode == "themelab_flow":
        theme_backfill_service = ThemeBackfillService(
            gateway_state,
            config=ThemeBackfillConfig.from_env(trading_mode=settings.mode),
            load_guard_provider=lambda gateway, result, summary: runtime_load_guard_from_theme_result(
                gateway,
                result,
                backfill_summary=summary,
            ),
        )
        theme_lab_pipeline = ThemeLabRuntimePipeline(
            db=db,
            market_data=market_data,
            market_index_store=market_index_store,
            interval_sec=config.theme_lab_pipeline_interval_sec,
            engine=ThemeLabFlowEngine(theme_lab_config_from_settings(runtime_settings)),
            backfill_service=theme_backfill_service,
        )
    opening_burst_pipeline = None
    intraday_discovery_pipeline = None
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
        opening_burst_pipeline=opening_burst_pipeline,
        theme_board_pipeline=theme_board_pipeline,
        market_regime_pipeline=market_regime_pipeline,
        entry_engine_pipeline=entry_engine_pipeline,
        exit_engine_reboot_pipeline=exit_engine_reboot_pipeline,
        position_risk_pipeline=position_risk_pipeline,
        order_manager_pipeline=order_manager_pipeline,
        theme_lab_shadow_ab_provider=theme_lab_shadow_ab_provider,
        shadow_small_entry_promotion_provider=shadow_small_entry_promotion_provider,
    )
    runtime.candidate_ingestion_service = candidate_ingestion_service
    runtime.candidate_hydrator = candidate_hydrator
    runtime.theme_board_pipeline = theme_board_pipeline
    runtime.market_regime_pipeline = market_regime_pipeline
    runtime.strategy_context_pipeline = strategy_context_pipeline
    runtime.entry_engine_pipeline = entry_engine_pipeline
    runtime.dirty_strategy_evaluator = dirty_strategy_evaluator
    runtime.market_relative_strength_outcome_pipeline = market_relative_strength_outcome_pipeline
    runtime.exit_engine_reboot_pipeline = exit_engine_reboot_pipeline
    runtime.position_risk_pipeline = position_risk_pipeline
    runtime.order_manager_pipeline = order_manager_pipeline
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
        runtime_profile=RebootV2RuntimeProfile.LEGACY.value,
        theme_runtime=theme_runtime,
        theme_runtime_bridge=theme_runtime_bridge,
        db=db,
        order_sink=order_sink,
        candidate_ingestion_service=candidate_ingestion_service,
        candidate_hydrator=candidate_hydrator,
        opening_burst_pipeline=opening_burst_pipeline,
        intraday_discovery_pipeline=intraday_discovery_pipeline,
        theme_board_pipeline=theme_board_pipeline,
        market_regime_pipeline=market_regime_pipeline,
        strategy_context_pipeline=strategy_context_pipeline,
        entry_engine_pipeline=entry_engine_pipeline,
        dirty_strategy_evaluator=dirty_strategy_evaluator,
        setup_router_v3_pipeline=None,
        market_relative_strength_shadow_pipeline=market_relative_strength_shadow_pipeline,
        market_relative_strength_outcome_pipeline=market_relative_strength_outcome_pipeline,
        exit_engine_reboot_pipeline=exit_engine_reboot_pipeline,
        position_risk_pipeline=position_risk_pipeline,
        order_manager_pipeline=order_manager_pipeline,
    )


def build_reboot_v2_runtime_bundle(
    db: TradingDatabase,
    gateway_state: GatewayStateStore,
    *,
    settings: CoreSettings,
    warning_sink: Callable[[str], None] | None = None,
    profile: RebootV2RuntimeProfile = RebootV2RuntimeProfile.V2_OBSERVE,
) -> CoreRuntimeBundle:
    config_result = StrategyRuntimeConfigRepository(db).load()
    runtime_settings = StrategyRuntimeSettingsRepository(db).load()
    condition_seed_result = ensure_default_condition_profiles(db)
    config = config_result.config
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
    theme_repository = ThemeEngineRepository(db)
    setup_router_config = SetupRouterConfig.from_env()
    subscription_lifecycle_tracker = RealtimeSubscriptionLifecycleTracker(
        db,
        max_tick_age_sec=setup_router_config.max_tick_age_sec,
    )
    realtime_client = GatewayCommandRealtimeClient(gateway_state, warning_sink=warning_sink)
    subscription_manager = RealTimeSubscriptionManager(
        realtime_client,
        max_codes=config.realtime_subscription_limit,
        lifecycle_tracker=subscription_lifecycle_tracker,
    )
    candidate_ingestion_service = CandidateIngestionService(db)
    candidate_hydrator = CandidateHydrator(db, gateway_state, market_data=market_data)
    opening_burst_pipeline = OpeningThemeBurstRuntimePipeline(
        db=db,
        gateway_state=gateway_state,
        market_data=market_data,
        repository=theme_repository,
        config=OpeningBurstRuntimeConfig.from_env(trading_mode=settings.mode),
        candidate_ingestion_service=candidate_ingestion_service,
        candidate_hydrator=candidate_hydrator,
    )
    intraday_discovery_pipeline = IntradayDiscoveryRuntimePipeline(
        db=db,
        gateway_state=gateway_state,
        config=IntradayDiscoveryConfig.from_env(trading_mode=settings.mode),
    )
    dirty_evaluator_config = replace(
        DirtyStrategyEvaluatorConfig.from_env(),
        order_intent_enabled=False,
    )
    theme_core_v3_config = replace(
        ThemeCoreV3RuntimeConfig.from_env(),
        enabled=_v2_component_enabled(
            "TRADING_THEME_CORE_V3_ENABLED",
            default=_v2_component_enabled("TRADING_THEME_BOARD_ENABLED", default=True),
        ),
        interval_sec=dirty_evaluator_config.theme_cadence_sec,
        ingest_candidate_source_events=_v2_component_enabled("TRADING_THEME_CORE_V3_INGEST_CANDIDATES", default=True),
        use_runtime_market_context=_v2_component_enabled("TRADING_THEME_CORE_V3_USE_RUNTIME_MARKET_CONTEXT", default=True),
        theme_expansion_subscriptions_enabled=_v2_component_enabled("TRADING_THEME_EXPANSION_SUBSCRIPTIONS_ENABLED", default=True),
    )
    market_regime_config = replace(
        MarketRegimeConfig.from_env(),
        enabled=_v2_component_enabled("TRADING_MARKET_REGIME_ENABLED", default=True),
        interval_sec=dirty_evaluator_config.market_cadence_sec,
    )
    entry_engine_config = replace(
        EntryEngineConfig.from_env(),
        enabled=_v2_component_enabled("TRADING_ENTRY_ENGINE_ENABLED", default=True),
        use_strategy_context_v3=_v2_component_enabled("TRADING_ENTRY_USE_STRATEGY_CONTEXT_V3", default=True),
        allow_legacy_theme_context_fallback=_v2_component_enabled("TRADING_ENTRY_ALLOW_LEGACY_THEME_CONTEXT_FALLBACK", default=False),
    )
    exit_engine_config = replace(
        ExitEngineConfig.from_env(),
        enabled=_v2_component_enabled("TRADING_EXIT_ENGINE_ENABLED", default=True),
    )
    position_risk_config = replace(
        PositionRiskConfig.from_env(),
        enabled=_v2_component_enabled("TRADING_POSITION_RISK_ENABLED", default=True),
    )
    theme_board_pipeline = ThemeCoreV3RuntimePipeline(
        db=db,
        market_data=market_data,
        repository=theme_repository,
        config=theme_core_v3_config,
        candidate_ingestion_service=candidate_ingestion_service,
    )
    market_regime_pipeline = MarketRegimeRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index_store,
        candle_builder=candle_builder,
        config=market_regime_config,
    )
    strategy_context_pipeline = StrategyContextRuntimePipeline(
        db=db,
        market_data=market_data,
        candle_builder=candle_builder,
        enabled=_v2_component_enabled("TRADING_STRATEGY_CONTEXT_V3_ENABLED", default=True),
    )
    entry_engine_pipeline = EntryEngineRuntimePipeline(
        db=db,
        market_data=market_data,
        candle_builder=candle_builder,
        config=entry_engine_config,
    )
    dirty_strategy_evaluator = DirtyStrategyEvaluatorRuntimePipeline(
        DirtyStrategyEvaluator(
            db=db,
            market_data_service=market_data_bridge.service,
            entry_engine=entry_engine_pipeline.engine,
            config=dirty_evaluator_config,
        )
    )
    market_data_config = getattr(getattr(market_data_bridge, "service", None), "config", None)
    setup_router_tick_age = int(getattr(market_data_config, "max_tick_age_sec", setup_router_config.max_tick_age_sec))
    subscription_lifecycle_tracker.max_tick_age_sec = max(1, setup_router_tick_age)
    subscription_readiness_provider = RealtimeSubscriptionReadinessProvider(
        subscription_manager,
        market_data=market_data,
        max_tick_age_sec=setup_router_tick_age,
        lifecycle_tracker=subscription_lifecycle_tracker,
    )
    setup_router_v3_pipeline = SetupRouterV3RuntimePipeline(
        db=db,
        market_data=market_data,
        candle_builder=candle_builder,
        config=replace(
            setup_router_config,
            observe_only=True,
            max_tick_age_sec=setup_router_tick_age,
        ),
        dirty_evaluator_provider=dirty_strategy_evaluator,
        subscription_readiness_provider=subscription_readiness_provider,
    )
    market_relative_strength_shadow_pipeline = MarketRelativeStrengthShadowRuntimePipeline(
        db=db,
        config=MarketRelativeStrengthShadowConfig.from_env(),
    )
    market_relative_strength_outcome_pipeline = MarketRelativeStrengthOutcomeRuntimePipeline(
        db=db,
        config=MarketRelativeStrengthOutcomeRuntimeConfig.from_env(),
    )
    exit_engine_reboot_pipeline = ExitEngineRuntimePipeline(
        db=db,
        market_data=market_data,
        candle_builder=candle_builder,
        config=exit_engine_config,
    )
    position_risk_pipeline = PositionRiskRuntimePipeline(
        db=db,
        market_data=market_data,
        candle_builder=candle_builder,
        config=position_risk_config,
    )
    order_manager_pipeline = OrderManagerRuntimePipeline(
        db=db,
        gateway_state=gateway_state,
        market_data=market_data,
        config=OrderManagerConfig.from_env(),
    )
    market_data_service_config = getattr(getattr(market_data_bridge, "service", None), "config", None)
    baseline_service = StrategyBaselineService(
        db=db,
        runtime_profile=profile.value,
        config=StrategyBaselineRuntimeConfig.from_env(),
        config_snapshot_provider=lambda: build_strategy_baseline_snapshot(
            runtime_profile=profile.value,
            runtime_config=config,
            runtime_settings=runtime_settings,
            setup_router_config=setup_router_v3_pipeline.config,
            entry_engine_config=entry_engine_config,
            market_regime_config=market_regime_config,
            theme_core_v3_config=theme_core_v3_config,
            market_data_config=market_data_service_config,
            position_risk_config=position_risk_config,
            exit_engine_config=exit_engine_config,
            order_manager_config=order_manager_pipeline.config,
            core_settings=settings,
        ),
    )
    setup_router_v3_pipeline.baseline_section_provider = lambda: baseline_service.last_result
    candidate_funnel_service = CandidateFunnelService(
        db=db,
        config=CandidateFunnelConfig.from_env(),
    )
    opportunity_benchmark_service = OpportunityBenchmarkService(
        db=db,
        market_data=market_data,
        candle_builder=candle_builder,
        config=OpportunityBenchmarkConfig.from_env(),
    )
    champion_outcome_service = ChampionOutcomeValidatorService(
        db=db,
        config=ChampionOutcomeValidatorConfig.from_env(),
    )
    trading_day_qualification_service = TradingDayQualificationService(
        db=db,
        config=TradingDayQualificationConfig.from_env(),
    )
    expansion_lease_manager = ExpansionLeaseManager()
    runtime = RebootV2Runtime(
        db=db,
        subscription_manager=subscription_manager,
        candle_builder=candle_builder,
        market_data=market_data,
        market_index_store=market_index_store,
        config=config,
        profile=profile,
        candidate_ingestion_service=candidate_ingestion_service,
        candidate_hydrator=candidate_hydrator,
        opening_burst_pipeline=opening_burst_pipeline,
        intraday_discovery_pipeline=intraday_discovery_pipeline,
        theme_board_pipeline=theme_board_pipeline,
        market_regime_pipeline=market_regime_pipeline,
        strategy_context_pipeline=strategy_context_pipeline,
        entry_engine_pipeline=entry_engine_pipeline,
        dirty_strategy_evaluator=dirty_strategy_evaluator,
        setup_router_v3_pipeline=setup_router_v3_pipeline,
        market_relative_strength_shadow_pipeline=market_relative_strength_shadow_pipeline,
        market_relative_strength_outcome_pipeline=market_relative_strength_outcome_pipeline,
        exit_engine_reboot_pipeline=exit_engine_reboot_pipeline,
        position_risk_pipeline=position_risk_pipeline,
        order_manager_pipeline=order_manager_pipeline,
        expansion_lease_manager=expansion_lease_manager,
        strategy_baseline_service=baseline_service,
        candidate_funnel_service=candidate_funnel_service,
        opportunity_benchmark_service=opportunity_benchmark_service,
        champion_outcome_service=champion_outcome_service,
        trading_day_qualification_service=trading_day_qualification_service,
    )
    readiness_report = build_readiness_report(
        db,
        subscription_manager=subscription_manager,
        theme_engine_mode="reboot_v2",
        theme_lab_flow_wired=False,
        condition_adapter=None,
    )
    runtime.readiness_report = readiness_report
    runtime.startup_warnings = dedupe_warnings(
        list(config_result.warnings)
        + runtime_settings.validation_warnings
        + condition_seed_result.warnings
        + readiness_report.warnings
    )
    return CoreRuntimeBundle(
        runtime=runtime,
        market_data_bridge=market_data_bridge,
        runtime_profile=profile.value,
        db=db,
        candidate_ingestion_service=candidate_ingestion_service,
        candidate_hydrator=candidate_hydrator,
        opening_burst_pipeline=opening_burst_pipeline,
        intraday_discovery_pipeline=intraday_discovery_pipeline,
        theme_board_pipeline=theme_board_pipeline,
        market_regime_pipeline=market_regime_pipeline,
        strategy_context_pipeline=strategy_context_pipeline,
        entry_engine_pipeline=entry_engine_pipeline,
        dirty_strategy_evaluator=dirty_strategy_evaluator,
        setup_router_v3_pipeline=setup_router_v3_pipeline,
        market_relative_strength_shadow_pipeline=market_relative_strength_shadow_pipeline,
        market_relative_strength_outcome_pipeline=market_relative_strength_outcome_pipeline,
        exit_engine_reboot_pipeline=exit_engine_reboot_pipeline,
        position_risk_pipeline=position_risk_pipeline,
        order_manager_pipeline=order_manager_pipeline,
        subscription_lifecycle_tracker=subscription_lifecycle_tracker,
        strategy_baseline_service=baseline_service,
        candidate_funnel_service=candidate_funnel_service,
        opportunity_benchmark_service=opportunity_benchmark_service,
        champion_outcome_service=champion_outcome_service,
        trading_day_qualification_service=trading_day_qualification_service,
    )


def _theme_lab_shadow_ab_provider(
    db: TradingDatabase,
    runtime_settings: Any,
) -> Callable[[str], dict[str, Any]] | None:
    policy = dict(runtime_settings.value("theme_lab_shadow_small_entry", {}) or {})
    if str(policy.get("enabled") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None

    def load_report(trade_date: str) -> dict[str, Any]:
        from trading_app.theme_lab_gate_reason_outcomes import ThemeLabGateReasonOutcomeAnalyzer

        configured_date = str(policy.get("report_trade_date") or "").strip()
        report_trade_date = configured_date or str(trade_date or "").strip() or None
        return ThemeLabGateReasonOutcomeAnalyzer(db).build_report(
            trade_date=report_trade_date,
            limit=10_000,
        )

    return _cached_report_provider(load_report, ttl_sec=_provider_cache_ttl_sec(policy, default=300))


def _v2_component_enabled(env_name: str, *, default: bool) -> bool:
    raw = os.getenv(env_name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _shadow_small_entry_promotion_provider(
    db: TradingDatabase,
    runtime_settings: Any,
) -> Callable[[str], dict[str, Any]] | None:
    policy = dict(runtime_settings.value("shadow_small_entry_promotion", {}) or {})
    if str(policy.get("enabled", True)).strip().lower() not in {"1", "true", "yes", "on"}:
        return None

    def load_report(trade_date: str) -> dict[str, Any]:
        from trading_app.shadow_small_entry_promotion import ShadowSmallEntryPromotionAnalyzer

        return ShadowSmallEntryPromotionAnalyzer(db, settings=runtime_settings).load_evidence(
            trade_date=str(trade_date or "").strip() or None,
            limit=50_000,
        )

    return _cached_report_provider(load_report, ttl_sec=_provider_cache_ttl_sec(policy, default=300))


def _cached_report_provider(
    loader: Callable[[str], dict[str, Any]],
    *,
    ttl_sec: int,
) -> Callable[[str], dict[str, Any]]:
    cache: dict[str, tuple[float, dict[str, Any]]] = {}
    ttl = max(0, int(ttl_sec or 0))

    def provider(trade_date: str) -> dict[str, Any]:
        key = str(trade_date or "")
        now = perf_counter()
        cached = cache.get(key)
        if ttl > 0 and cached is not None and now - cached[0] <= ttl:
            return dict(cached[1])
        payload = dict(loader(trade_date) or {})
        if ttl > 0:
            cache[key] = (perf_counter(), payload)
        return dict(payload)

    return provider


def _provider_cache_ttl_sec(policy: dict[str, Any], *, default: int) -> int:
    raw = policy.get("cache_ttl_sec", policy.get("report_cache_ttl_sec", policy.get("evidence_cache_ttl_sec", default)))
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return int(default)


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
            if _live_sim_order_sink_blocked_by_preflight(settings, warning_sink):
                return DryRunRuntimeOrderSink(settings=settings, service=service, warning_sink=warning_sink)
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


def _live_sim_order_sink_blocked_by_preflight(
    settings: CoreSettings,
    warning_sink: Callable[[str], None] | None,
) -> bool:
    if not bool(getattr(settings, "runtime_live_sim_require_preflight_go_for_order_sink", False)):
        return False
    status = _latest_live_sim_preflight_status(settings)
    allowed = {"GO"}
    if bool(getattr(settings, "runtime_live_sim_allow_preflight_warnings_for_order_sink", False)):
        allowed.add("GO_WITH_WARNINGS")
    if status in allowed:
        return False
    if warning_sink is not None:
        warning_sink(f"LIVE_SIM_ORDER_SINK_PREFLIGHT_NOT_GO:{status or 'MISSING'}")
    return True


def _latest_live_sim_preflight_status(settings: CoreSettings) -> str:
    try:
        db = TradingDatabase(str(settings.db_path))
        try:
            snapshot = db.latest_live_sim_preflight_snapshot() or {}
        finally:
            db.close()
    except Exception:
        return "ERROR"
    return str(snapshot.get("status") or "").strip().upper()
