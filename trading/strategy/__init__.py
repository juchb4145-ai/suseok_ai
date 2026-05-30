"""Strategy engine primitives for the Phase 1 OBSERVE flow."""

from trading.strategy.bridge import StrategyMarketDataBridge
from trading.strategy.candles import Candle, CandleBuilder
from trading.strategy.candidates import CandidateCollector, CandidateLifecycle
from trading.strategy.conditions import ConditionProfile, ConditionProfileRepository, KiwoomConditionAdapter, RegisteredCondition
from trading.strategy.config import (
    CONFIG_VERSION,
    DEFAULT_CONFIG_KEY,
    RuntimeConfigLoadResult,
    RuntimeConfigSaveResult,
    StrategyRuntimeConfigRepository,
)
from trading.strategy.entry import EntryPlanBuilder, TickSizeProvider
from trading.strategy.exit import (
    ExitDecisionEngine,
    PerformanceUpdateResult,
    PositionOpenResult,
    VirtualPositionService,
)
from trading.strategy.export import REVIEW_EXPORT_COLUMNS, ReviewExporter
from trading.strategy.gates import StockLeadershipGate, ThemeStrengthGate
from trading.strategy.hybrid_gate import (
    HybridDynamicThemeGate,
    HybridGateComponent,
    HybridGateConfig,
    HybridGateDecision,
    HybridGateStatus,
    HybridPositionTier,
)
from trading.strategy.holding import HoldingProvider, StaticHoldingProvider
from trading.strategy.indicators import IndicatorCalculator, PreviousDayLevelProvider
from trading.strategy.intraday import IntradayStateTracker
from trading.strategy.market_index import IndexCodeMapper, IndexTick, MarketIndexState, MarketIndexStore
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.models import (
    BlockType,
    Candidate,
    CandidateEvent,
    CandidateSourceType,
    CandidateState,
    EntryPlan,
    ExitDecision,
    FillPolicy,
    GateDecision,
    IndicatorSnapshot,
    OrderMode,
    ReviewFinalStatus,
    StrategyProfile,
    TradeReview,
    VirtualOrder,
    VirtualOrderStatus,
    VirtualPosition,
)
from trading.strategy.pipeline import GatePipeline, GatePipelineResult
from trading.strategy.replay import REPLAY_COLUMNS, TickReplayResult, TickReplayRunner
from trading.strategy.reason_codes import (
    COMPARISON_MODE_LEGACY_ONLY,
    P1_REASON_CODES,
    REASON_DETAILS_FEATURE_VERSION,
    STRATEGY_FEATURE_VERSION,
    ReasonCode,
)
from trading.strategy.review import TradeReviewService
from trading.strategy.realtime import RealTimeSubscriptionManager, SubscriptionRecord
from trading.strategy.runtime import StrategyRuntime, StrategyRuntimeConfig, StrategyRuntimeSnapshot
from trading.strategy.runtime_settings import (
    DEFAULT_PROFILE_NAME,
    DEFAULT_PROFILE_VERSION,
    DEFAULT_STRATEGY_NAME,
    LEGACY_DEFAULT_SETTINGS,
    StrategyRuntimeSettings,
    StrategyRuntimeSettingsRepository,
)
from trading.strategy.safety import ActualOrderGuard, OrderGuardDecision
from trading.strategy.session import session_bucket_at
from trading.theme_engine.models import StockLeadershipResult, ThemeStrengthResult
from trading.strategy.virtual_orders import (
    VirtualOrderEvaluationResult,
    VirtualOrderService,
    VirtualOrderSubmissionResult,
)

__all__ = [
    "ActualOrderGuard",
    "BlockType",
    "Candle",
    "CandleBuilder",
    "Candidate",
    "CandidateCollector",
    "CandidateEvent",
    "CandidateLifecycle",
    "CandidateSourceType",
    "CandidateState",
    "CONFIG_VERSION",
    "COMPARISON_MODE_LEGACY_ONLY",
    "ConditionProfile",
    "ConditionProfileRepository",
    "DEFAULT_CONFIG_KEY",
    "DEFAULT_PROFILE_NAME",
    "DEFAULT_PROFILE_VERSION",
    "DEFAULT_STRATEGY_NAME",
    "EntryPlan",
    "EntryPlanBuilder",
    "ExitDecision",
    "ExitDecisionEngine",
    "FillPolicy",
    "GateDecision",
    "GatePipeline",
    "GatePipelineResult",
    "HoldingProvider",
    "HybridDynamicThemeGate",
    "HybridGateComponent",
    "HybridGateConfig",
    "HybridGateDecision",
    "HybridGateStatus",
    "HybridPositionTier",
    "IndicatorSnapshot",
    "IndicatorCalculator",
    "IndexCodeMapper",
    "IndexTick",
    "IntradayStateTracker",
    "KiwoomConditionAdapter",
    "LEGACY_DEFAULT_SETTINGS",
    "MarketDataStore",
    "MarketIndexState",
    "MarketIndexStore",
    "OrderGuardDecision",
    "OrderMode",
    "PerformanceUpdateResult",
    "PositionOpenResult",
    "P1_REASON_CODES",
    "REPLAY_COLUMNS",
    "REVIEW_EXPORT_COLUMNS",
    "REASON_DETAILS_FEATURE_VERSION",
    "PreviousDayLevelProvider",
    "RealTimeSubscriptionManager",
    "ReasonCode",
    "ReviewExporter",
    "ReviewFinalStatus",
    "RegisteredCondition",
    "RuntimeConfigLoadResult",
    "RuntimeConfigSaveResult",
    "StockLeadershipGate",
    "StockLeadershipResult",
    "StaticHoldingProvider",
    "StrategyProfile",
    "StrategyMarketDataBridge",
    "StrategyRuntime",
    "StrategyRuntimeConfig",
    "StrategyRuntimeConfigRepository",
    "StrategyRuntimeSettings",
    "StrategyRuntimeSettingsRepository",
    "StrategyRuntimeSnapshot",
    "StrategyTick",
    "STRATEGY_FEATURE_VERSION",
    "SubscriptionRecord",
    "ThemeStrengthGate",
    "ThemeStrengthResult",
    "TickSizeProvider",
    "TradeReview",
    "TradeReviewService",
    "TickReplayResult",
    "TickReplayRunner",
    "VirtualOrder",
    "VirtualOrderEvaluationResult",
    "VirtualOrderService",
    "VirtualOrderSubmissionResult",
    "VirtualOrderStatus",
    "VirtualPosition",
    "VirtualPositionService",
    "session_bucket_at",
]
