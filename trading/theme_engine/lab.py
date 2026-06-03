from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable

from trading.theme_engine.models import StockSnapshot, ThemeMembership
from trading.theme_engine.normalizer import normalize_stock_code


class ThemeLabThemeStatus(str, Enum):
    LEADING_THEME = "LEADING_THEME"
    SPREADING_THEME = "SPREADING_THEME"
    LEADER_ONLY_THEME = "LEADER_ONLY_THEME"
    WATCH_THEME = "WATCH_THEME"
    WEAK_THEME = "WEAK_THEME"


class MarketStatus(str, Enum):
    EXPANSION = "EXPANSION"
    SELECTIVE = "SELECTIVE"
    CHOPPY = "CHOPPY"
    WEAK = "WEAK"
    RISK_OFF = "RISK_OFF"


class MarketSide(str, Enum):
    KOSPI = "KOSPI"
    KOSDAQ = "KOSDAQ"
    UNKNOWN = "UNKNOWN"


class StockRole(str, Enum):
    LEADER = "LEADER"
    CO_LEADER = "CO_LEADER"
    FOLLOWER = "FOLLOWER"
    LATE_LAGGARD = "LATE_LAGGARD"
    WEAK_MEMBER = "WEAK_MEMBER"
    OVERHEATED = "OVERHEATED"


class LabGateStatus(str, Enum):
    READY = "READY"
    READY_SMALL = "READY_SMALL"
    WAIT = "WAIT"
    BLOCKED = "BLOCKED"
    OBSERVE = "OBSERVE"


class TradeabilityRiskLevel(str, Enum):
    PASS = "PASS"
    RISK_ADJUST = "RISK_ADJUST"
    SOFT_BLOCK = "SOFT_BLOCK"
    HARD_BLOCK = "HARD_BLOCK"


class PriceLocationStatus(str, Enum):
    GOOD_PULLBACK = "GOOD_PULLBACK"
    PULLBACK_RECLAIM = "PULLBACK_RECLAIM"
    BREAKOUT_CONTINUATION = "BREAKOUT_CONTINUATION"
    CHASE_HIGH = "CHASE_HIGH"
    FAILED_BREAKOUT = "FAILED_BREAKOUT"
    DEEP_PULLBACK = "DEEP_PULLBACK"
    VWAP_OVEREXTENDED = "VWAP_OVEREXTENDED"
    VWAP_RECLAIM = "VWAP_RECLAIM"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ThemeLabConditionConfig:
    alive_threshold_pct: float = -1.0
    strong_threshold_pct: float = 3.0
    leader_threshold_pct: float = 5.0


@dataclass(frozen=True)
class ThemeConditionScoreWeights:
    alive_ratio: float = 20.0
    strong_ratio: float = 35.0
    leader_ratio: float = 45.0


@dataclass(frozen=True)
class ThemeStatusThresholds:
    min_eligible_members: int = 2
    min_strong_count_for_leading: int = 3
    min_leader_count_for_leading: int = 2
    min_strong_ratio_for_leading: float = 0.3
    min_leader_ratio_for_leading: float = 0.15
    min_alive_ratio_for_spreading: float = 0.6
    min_strong_ratio_for_spreading: float = 0.25
    max_strong_ratio_for_leader_only: float = 0.25
    max_strong_count_for_leader_only: int = 2
    min_theme_turnover_krw_for_leading: float = 0.0


@dataclass(frozen=True)
class WatchSetLimits:
    max_watchset_size: int = 100
    max_watch_per_theme: int = 5
    max_ready_candidates: int = 20
    max_order_candidates: int = 5
    top_theme_count: int = 5


@dataclass(frozen=True)
class LiquidityFilterConfig:
    min_avg_turnover_20d_krw: float = 0.0
    min_today_turnover_krw: float = 0.0
    min_recent_volume: int = 0


@dataclass(frozen=True)
class MarketStatusThresholds:
    expansion_strong_count: int = 200
    expansion_leader_count: int = 80
    selective_strong_count: int = 80
    selective_leader_count: int = 25
    choppy_strong_count: int = 30
    risk_off_kospi_pct: float = -2.0
    risk_off_kosdaq_pct: float = -2.5
    weak_kospi_pct: float = -0.8
    weak_kosdaq_pct: float = -1.0


@dataclass(frozen=True)
class MarketSideGateConfig:
    enabled: bool = True
    unknown_market_action: str = "strict_fallback"
    weak_action: str = "temporary_wait"
    risk_off_action: str = "temporary_wait"
    global_risk_off_action: str = "temporary_wait"
    recheck_after_sec: int = 60
    allow_recover: bool = True
    use_side_breadth_if_available: bool = True
    allow_market_heuristic: bool = False


@dataclass(frozen=True)
class MarketSideBreadthConfig:
    enabled: bool = True
    min_sample_count_kospi: int = 80
    min_sample_count_kosdaq: int = 120
    max_quote_age_sec: int = 60
    advancing_threshold_pct: float = 0.0
    declining_threshold_pct: float = 0.0
    strong_return_threshold_pct: float = 1.0
    weak_return_threshold_pct: float = -1.0
    breadth_weak_pct: float = 0.38
    breadth_risk_off_pct: float = 0.28
    breadth_expansion_pct: float = 0.58
    valid_quote_ratio_min: float = 0.60
    use_turnover_weighted_return: bool = True
    fallback_to_index_return: bool = True
    fallback_to_global_breadth: bool = True
    candidate_universe_fallback_gate_weight: str = "diagnostic_only"


@dataclass(frozen=True)
class TradeabilityRiskConfig:
    vi_cooldown_sec: int = 180
    leader_max_buy_return_pct: float = 15.0
    co_leader_max_buy_return_pct: float = 12.0
    follower_max_buy_return_pct: float = 8.0
    late_laggard_max_buy_return_pct: float = 5.0
    min_upper_limit_gap_pct: float = 3.0
    leader_min_pullback_from_high_pct: float = 0.2
    follower_min_pullback_from_high_pct: float = 0.7
    risk_adjust_multiplier_mild: float = 0.5
    risk_adjust_multiplier_high: float = 0.3
    soft_block_recheck_sec: int = 30


@dataclass(frozen=True)
class PriceLocationConfig:
    good_pullback_min_pct: float = 0.7
    good_pullback_max_pct: float = 3.0
    min_pullback_from_high_pct: float = 0.3
    max_healthy_pullback_pct: float = 5.0
    max_vwap_gap_pct_leader: float = 6.0
    max_vwap_gap_pct_co_leader: float = 5.0
    max_vwap_gap_pct_follower: float = 3.0
    max_breakout_extension_pct: float = 3.0
    min_reclaim_momentum_1m_pct: float = 0.0
    min_reclaim_momentum_3m_pct: float = 0.0
    upper_wick_risk_threshold: float = 0.45
    chase_high_recheck_sec: int = 30
    failed_breakout_recheck_sec: int = 60
    unknown_price_location_recheck_sec: int = 30


@dataclass(frozen=True)
class PositionAdjustmentConfig:
    ready_small_multiplier_leader: float = 0.5
    ready_small_multiplier_co_leader: float = 0.4
    ready_small_multiplier_follower: float = 0.3
    chase_high_leader_multiplier: float = 0.3
    breakout_continuation_leader_multiplier: float = 0.5
    vwap_overextended_leader_multiplier: float = 0.3


@dataclass(frozen=True)
class ThemeLabConfig:
    conditions: ThemeLabConditionConfig = field(default_factory=ThemeLabConditionConfig)
    score_weights: ThemeConditionScoreWeights = field(default_factory=ThemeConditionScoreWeights)
    theme_status: ThemeStatusThresholds = field(default_factory=ThemeStatusThresholds)
    watchset_limits: WatchSetLimits = field(default_factory=WatchSetLimits)
    liquidity_filter: LiquidityFilterConfig = field(default_factory=LiquidityFilterConfig)
    market_status: MarketStatusThresholds = field(default_factory=MarketStatusThresholds)
    tradeability_risk: TradeabilityRiskConfig = field(default_factory=TradeabilityRiskConfig)
    price_location: PriceLocationConfig = field(default_factory=PriceLocationConfig)
    position_adjustment: PositionAdjustmentConfig = field(default_factory=PositionAdjustmentConfig)
    market_side_gate: MarketSideGateConfig = field(default_factory=MarketSideGateConfig)
    market_side_breadth: MarketSideBreadthConfig = field(default_factory=MarketSideBreadthConfig)


@dataclass(frozen=True)
class InstrumentMetadata:
    symbol: str
    name: str = ""
    instrument_type: str = ""
    is_etf: bool = False
    is_etn: bool = False
    is_preferred: bool = False
    is_suspended: bool = False
    is_under_administration: bool = False
    avg_turnover_20d_krw: float = 0.0
    today_turnover_krw: float = 0.0
    recent_volume: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExclusionResult:
    excluded: bool
    reason: str = ""
    fallback_used: bool = False


@dataclass(frozen=True)
class ConditionHitSnapshot:
    calculated_at: str
    symbol: str
    name: str = ""
    current_price: float = 0.0
    return_pct: float = 0.0
    turnover_krw: float = 0.0
    alive_hit: bool = False
    strong_hit: bool = False
    leader_hit: bool = False
    excluded: bool = False
    excluded_reason: str = ""
    data_quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ThemeConditionSnapshot:
    calculated_at: str
    theme_id: str
    theme_name: str = ""
    raw_total_members: int = 0
    eligible_total_members: int = 0
    alive_count: int = 0
    strong_count: int = 0
    leader_count: int = 0
    alive_ratio: float = 0.0
    strong_ratio: float = 0.0
    leader_ratio: float = 0.0
    condition_score: float = 0.0
    theme_turnover_krw: float = 0.0
    theme_status: ThemeLabThemeStatus = ThemeLabThemeStatus.WEAK_THEME
    top_leader_symbol: str = ""
    top_leader_name: str = ""
    top_leader_return_pct: float = 0.0
    data_quality_flags: tuple[str, ...] = ()
    member_hits: tuple[ConditionHitSnapshot, ...] = ()


@dataclass(frozen=True)
class MarketStrengthSnapshot:
    market_status: MarketStatus
    kospi_return_pct: float = 0.0
    kosdaq_return_pct: float = 0.0
    advancers: int = 0
    decliners: int = 0
    market_strong_count: int = 0
    market_leader_count: int = 0
    market_turnover_krw: float = 0.0
    data_quality_flags: tuple[str, ...] = ()
    kospi_status: MarketStatus | None = None
    kosdaq_status: MarketStatus | None = None
    kospi_index_return_pct: float | None = None
    kosdaq_index_return_pct: float | None = None
    kospi_index_ready: bool = True
    kosdaq_index_ready: bool = True
    side_statuses: dict[str, Any] = field(default_factory=dict)
    market_side_data_quality_flags: tuple[str, ...] = ()
    kospi_breadth_pct: float | None = None
    kosdaq_breadth_pct: float | None = None
    kospi_breadth_ready: bool = False
    kosdaq_breadth_ready: bool = False
    kospi_breadth_sample_count: int = 0
    kosdaq_breadth_sample_count: int = 0
    kospi_breadth_source: str = ""
    kosdaq_breadth_source: str = ""
    kospi_advancing_count: int = 0
    kosdaq_advancing_count: int = 0
    kospi_declining_count: int = 0
    kosdaq_declining_count: int = 0
    kospi_flat_count: int = 0
    kosdaq_flat_count: int = 0
    kospi_strong_count: int = 0
    kosdaq_strong_count: int = 0
    kospi_weak_count: int = 0
    kosdaq_weak_count: int = 0
    kospi_stale_count: int = 0
    kosdaq_stale_count: int = 0
    kospi_valid_quote_ratio: float = 0.0
    kosdaq_valid_quote_ratio: float = 0.0
    kospi_turnover_sum: float = 0.0
    kosdaq_turnover_sum: float = 0.0
    kospi_turnover_weighted_return_pct: float | None = None
    kosdaq_turnover_weighted_return_pct: float | None = None
    side_breadth_data_quality_flags: tuple[str, ...] = ()
    side_breadth_reason_codes: tuple[str, ...] = ()

    def status_for_side(self, side: MarketSide | str) -> MarketStatus | None:
        normalized = normalize_market_side(side)
        if normalized == MarketSide.KOSPI:
            return self.kospi_status or self.market_status
        if normalized == MarketSide.KOSDAQ:
            return self.kosdaq_status or self.market_status
        return None

    def index_return_for_side(self, side: MarketSide | str) -> float | None:
        normalized = normalize_market_side(side)
        if normalized == MarketSide.KOSPI:
            return self.kospi_index_return_pct if self.kospi_index_return_pct is not None else self.kospi_return_pct
        if normalized == MarketSide.KOSDAQ:
            return self.kosdaq_index_return_pct if self.kosdaq_index_return_pct is not None else self.kosdaq_return_pct
        return None


@dataclass(frozen=True)
class WatchSetSnapshot:
    calculated_at: str
    symbol: str
    name: str = ""
    themes: tuple[str, ...] = ()
    primary_theme: str = ""
    return_pct: float = 0.0
    turnover_krw: float = 0.0
    condition_level: int = 0
    stock_role: StockRole = StockRole.WEAK_MEMBER
    watch_reason: str = ""
    candidate_market: str = MarketSide.UNKNOWN.value
    candidate_market_source: str = ""
    candidate_market_status: str = ""
    candidate_market_action: str = ""
    candidate_index_return_pct: float | None = None
    global_market_status: str = ""
    kospi_market_status: str = ""
    kosdaq_market_status: str = ""
    kospi_return_pct: float | None = None
    kosdaq_return_pct: float | None = None
    candidate_breadth_pct: float | None = None
    candidate_breadth_ready: bool = False
    candidate_breadth_sample_count: int = 0
    candidate_breadth_source: str = ""
    candidate_valid_quote_ratio: float | None = None
    kospi_breadth_pct: float | None = None
    kosdaq_breadth_pct: float | None = None
    kospi_breadth_ready: bool = False
    kosdaq_breadth_ready: bool = False
    kospi_breadth_sample_count: int = 0
    kosdaq_breadth_sample_count: int = 0
    kospi_valid_quote_ratio: float | None = None
    kosdaq_valid_quote_ratio: float | None = None
    market_side_reason_codes: tuple[str, ...] = ()
    market_side_data_quality_flags: tuple[str, ...] = ()
    gate_status: LabGateStatus = LabGateStatus.OBSERVE
    removal_reason: str = ""
    risk_level: TradeabilityRiskLevel = TradeabilityRiskLevel.PASS
    risk_reason_codes: tuple[str, ...] = ()
    position_size_multiplier: float = 1.0
    recheck_after_sec: int = 0
    vi_active: bool = False
    seconds_since_vi_release: int = 0
    upper_limit_gap_pct: float = 100.0
    pullback_from_high_pct: float = 100.0
    final_gate_status: LabGateStatus = LabGateStatus.OBSERVE
    price_location_status: PriceLocationStatus = PriceLocationStatus.UNKNOWN
    price_location_score: float = 0.0
    price_location_reason_codes: tuple[str, ...] = ()
    distance_to_session_high_pct: float | None = None
    vwap_gap_pct: float | None = None
    breakout_level_gap_pct: float | None = None
    support_gap_pct: float | None = None
    upper_wick_risk: bool | None = None
    failed_breakout: bool | None = None
    pullback_reclaim: bool | None = None
    breakout_continuation: bool | None = None
    price_location_data_quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class LabGateDecision:
    symbol: str
    status: LabGateStatus
    reason_codes: tuple[str, ...] = ()
    blocked_reason: str = ""
    risk_level: TradeabilityRiskLevel = TradeabilityRiskLevel.PASS
    risk_reason_codes: tuple[str, ...] = ()
    position_size_multiplier: float = 1.0
    recheck_after_sec: int = 0
    price_location_status: PriceLocationStatus = PriceLocationStatus.UNKNOWN
    price_location_score: float = 0.0
    price_location_reason_codes: tuple[str, ...] = ()
    candidate_market: str = MarketSide.UNKNOWN.value
    candidate_market_source: str = ""
    candidate_market_status: str = ""
    candidate_market_action: str = ""
    candidate_index_return_pct: float | None = None
    global_market_status: str = ""
    kospi_market_status: str = ""
    kosdaq_market_status: str = ""
    kospi_return_pct: float | None = None
    kosdaq_return_pct: float | None = None
    candidate_breadth_pct: float | None = None
    candidate_breadth_ready: bool = False
    candidate_breadth_sample_count: int = 0
    candidate_breadth_source: str = ""
    candidate_valid_quote_ratio: float | None = None
    kospi_breadth_pct: float | None = None
    kosdaq_breadth_pct: float | None = None
    kospi_breadth_ready: bool = False
    kosdaq_breadth_ready: bool = False
    kospi_breadth_sample_count: int = 0
    kosdaq_breadth_sample_count: int = 0
    kospi_valid_quote_ratio: float | None = None
    kosdaq_valid_quote_ratio: float | None = None
    market_side_reason_codes: tuple[str, ...] = ()
    market_side_data_quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class TradeabilityRiskInput:
    market_status: MarketStatus
    theme_status: ThemeLabThemeStatus
    stock_role: StockRole
    return_pct: float = 0.0
    condition_level: int = 0
    vi_active: bool = False
    seconds_since_vi_release: int = 0
    upper_limit_gap_pct: float = 100.0
    pullback_from_high_pct: float = 100.0
    momentum_1m: float = 0.0
    momentum_3m: float = 0.0
    momentum_5m: float = 0.0
    turnover_krw: float = 0.0
    trade_strength: float = 0.0
    leader_momentum_status: str = ""
    theme_breadth_trend: str = ""
    data_quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class TradeabilityRiskResult:
    risk_level: TradeabilityRiskLevel
    reason_codes: tuple[str, ...] = ()
    position_size_multiplier: float = 1.0
    recheck_after_sec: int = 0


@dataclass(frozen=True)
class PriceLocationInput:
    symbol: str
    current_price: float | None = None
    prev_close: float | None = None
    open_price: float | None = None
    session_high: float | None = None
    session_low: float | None = None
    vwap: float | None = None
    upper_limit_price: float | None = None
    breakout_level: float | None = None
    recent_support_price: float | None = None
    return_pct: float | None = None
    turnover_krw: float | None = None
    trade_strength: float | None = None
    momentum_1m: float | None = None
    momentum_3m: float | None = None
    momentum_5m: float | None = None
    recent_candles_1m: tuple[Any, ...] = ()
    recent_candles_3m: tuple[Any, ...] = ()
    stock_role: StockRole | None = None
    theme_status: ThemeLabThemeStatus | None = None
    market_status: MarketStatus | None = None
    data_quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PriceLocationResult:
    symbol: str
    status: PriceLocationStatus = PriceLocationStatus.UNKNOWN
    score: float = 0.0
    reason_codes: tuple[str, ...] = ()
    pullback_from_high_pct: float | None = None
    distance_to_session_high_pct: float | None = None
    vwap_gap_pct: float | None = None
    upper_limit_gap_pct: float | None = None
    breakout_level_gap_pct: float | None = None
    support_gap_pct: float | None = None
    upper_wick_risk: bool | None = None
    failed_breakout: bool | None = None
    pullback_reclaim: bool | None = None
    breakout_continuation: bool | None = None
    data_quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ThemeLabFlowResult:
    market: MarketStrengthSnapshot
    themes: tuple[ThemeConditionSnapshot, ...]
    watchset: tuple[WatchSetSnapshot, ...]
    gate_decisions: tuple[LabGateDecision, ...]
    data_quality: dict[str, int]


class InstrumentExclusionFilter:
    def __init__(self, config: LiquidityFilterConfig | None = None) -> None:
        self.config = config or LiquidityFilterConfig()

    def evaluate(self, metadata: InstrumentMetadata | None, snapshot: StockSnapshot | None = None) -> ExclusionResult:
        fallback_used = metadata is None or not (metadata.instrument_type or metadata.raw)
        metadata = metadata or _metadata_from_snapshot(snapshot)
        text = f"{metadata.name} {metadata.instrument_type}".upper()
        if metadata.is_etf or metadata.instrument_type.upper() == "ETF" or "ETF" in text:
            return ExclusionResult(True, "ETF", fallback_used)
        if metadata.is_etn or metadata.instrument_type.upper() == "ETN" or "ETN" in text:
            return ExclusionResult(True, "ETN", fallback_used)
        if metadata.is_preferred or _looks_like_preferred(metadata.name):
            return ExclusionResult(True, "PREFERRED_STOCK", fallback_used)
        if metadata.is_suspended:
            return ExclusionResult(True, "TRADING_SUSPENDED", fallback_used)
        if metadata.is_under_administration:
            return ExclusionResult(True, "UNDER_ADMINISTRATION", fallback_used)
        if self.config.min_avg_turnover_20d_krw and metadata.avg_turnover_20d_krw < self.config.min_avg_turnover_20d_krw:
            return ExclusionResult(True, "LOW_AVG_TURNOVER_20D", fallback_used)
        if self.config.min_today_turnover_krw and metadata.today_turnover_krw < self.config.min_today_turnover_krw:
            return ExclusionResult(True, "LOW_TODAY_TURNOVER", fallback_used)
        if self.config.min_recent_volume and metadata.recent_volume < self.config.min_recent_volume:
            return ExclusionResult(True, "LOW_RECENT_VOLUME", fallback_used)
        return ExclusionResult(False, "", fallback_used)


class ThemeLabConditionClassifier:
    def __init__(self, config: ThemeLabConditionConfig | None = None) -> None:
        self.config = config or ThemeLabConditionConfig()

    def classify(
        self,
        *,
        symbol: str,
        current_price: float | None = None,
        prev_close: float | None = None,
        change_rate_pct: float | None = None,
        turnover_krw: float | None = None,
        name: str = "",
        excluded: bool = False,
        excluded_reason: str = "",
        calculated_at: str = "",
    ) -> ConditionHitSnapshot:
        data_quality: list[str] = []
        return_pct = _return_pct(current_price=current_price, prev_close=prev_close, change_rate_pct=change_rate_pct)
        if excluded:
            return ConditionHitSnapshot(
                calculated_at=calculated_at,
                symbol=normalize_stock_code(symbol),
                name=name,
                current_price=float(current_price or 0),
                return_pct=return_pct,
                turnover_krw=float(turnover_krw or 0),
                excluded=True,
                excluded_reason=excluded_reason,
            )
        if return_pct is None:
            if not prev_close:
                data_quality.append("MISSING_PREV_CLOSE")
            if not current_price and change_rate_pct is None:
                data_quality.append("MISSING_CURRENT_PRICE")
            return ConditionHitSnapshot(
                calculated_at=calculated_at,
                symbol=normalize_stock_code(symbol),
                name=name,
                current_price=float(current_price or 0),
                turnover_krw=float(turnover_krw or 0),
                excluded=False,
                data_quality_flags=tuple(data_quality or ["MISSING_RETURN_PCT"]),
            )
        alive = return_pct >= self.config.alive_threshold_pct
        strong = return_pct >= self.config.strong_threshold_pct
        leader = return_pct >= self.config.leader_threshold_pct
        if leader:
            strong = True
            alive = True
        elif strong:
            alive = True
        return ConditionHitSnapshot(
            calculated_at=calculated_at,
            symbol=normalize_stock_code(symbol),
            name=name,
            current_price=float(current_price or 0),
            return_pct=round(return_pct, 4),
            turnover_krw=float(turnover_krw or 0),
            alive_hit=alive,
            strong_hit=strong,
            leader_hit=leader,
            excluded=False,
            data_quality_flags=tuple(data_quality),
        )


class ThemeBreadthEngine:
    def __init__(self, config: ThemeLabConfig | None = None) -> None:
        self.config = config or ThemeLabConfig()
        self.exclusion_filter = InstrumentExclusionFilter(self.config.liquidity_filter)
        self.classifier = ThemeLabConditionClassifier(self.config.conditions)

    def calculate(
        self,
        theme_inputs: Iterable[tuple[str, str, list[ThemeMembership]]],
        snapshots: dict[str, StockSnapshot] | list[StockSnapshot],
        metadata_by_symbol: dict[str, InstrumentMetadata] | None = None,
        *,
        calculated_at: str = "",
    ) -> list[ThemeConditionSnapshot]:
        snapshot_by_symbol = _snapshot_map(snapshots)
        metadata_by_symbol = _metadata_map(metadata_by_symbol or {})
        results = [
            self.calculate_theme(theme_id, theme_name, memberships, snapshot_by_symbol, metadata_by_symbol, calculated_at=calculated_at)
            for theme_id, theme_name, memberships in theme_inputs
        ]
        return results

    def calculate_theme(
        self,
        theme_id: str,
        theme_name: str,
        memberships: list[ThemeMembership],
        snapshots: dict[str, StockSnapshot],
        metadata_by_symbol: dict[str, InstrumentMetadata] | None = None,
        *,
        calculated_at: str = "",
    ) -> ThemeConditionSnapshot:
        metadata_by_symbol = metadata_by_symbol or {}
        active_members = [member for member in memberships if member.active]
        hits: list[ConditionHitSnapshot] = []
        theme_turnover = 0.0
        flags: list[str] = []
        for member in active_members:
            symbol = normalize_stock_code(member.stock_code)
            snapshot = snapshots.get(symbol) or snapshots.get(member.stock_code)
            metadata = metadata_by_symbol.get(symbol) or metadata_by_symbol.get(member.stock_code)
            exclusion = self.exclusion_filter.evaluate(metadata, snapshot)
            if exclusion.fallback_used:
                _add_flag(flags, "EXCLUSION_METADATA_FALLBACK")
            current_price = snapshot.current_price if snapshot else None
            prev_close = _prev_close(snapshot)
            change_rate = snapshot.change_rate if snapshot else None
            hit = self.classifier.classify(
                symbol=symbol,
                current_price=current_price,
                prev_close=prev_close,
                change_rate_pct=change_rate,
                turnover_krw=snapshot.turnover if snapshot else 0,
                name=(snapshot.stock_name if snapshot else "") or member.stock_name,
                excluded=exclusion.excluded,
                excluded_reason=exclusion.reason,
                calculated_at=calculated_at,
            )
            hits.append(hit)
            if not hit.excluded:
                theme_turnover += max(0.0, float(snapshot.turnover if snapshot else 0.0))
            for flag in hit.data_quality_flags:
                _add_flag(flags, flag)
        eligible_hits = [hit for hit in hits if not hit.excluded]
        eligible_total = len(eligible_hits)
        alive_count = sum(1 for hit in eligible_hits if hit.alive_hit)
        strong_count = sum(1 for hit in eligible_hits if hit.strong_hit)
        leader_count = sum(1 for hit in eligible_hits if hit.leader_hit)
        if eligible_total == 0:
            _add_flag(flags, "EMPTY_ELIGIBLE_THEME")
        alive_ratio = alive_count / eligible_total if eligible_total else 0.0
        strong_ratio = strong_count / eligible_total if eligible_total else 0.0
        leader_ratio = leader_count / eligible_total if eligible_total else 0.0
        score = (
            alive_ratio * self.config.score_weights.alive_ratio
            + strong_ratio * self.config.score_weights.strong_ratio
            + leader_ratio * self.config.score_weights.leader_ratio
        )
        top_leader = max((hit for hit in eligible_hits if hit.leader_hit), key=lambda hit: hit.return_pct, default=None)
        status = classify_theme_status(
            eligible_total_members=eligible_total,
            alive_ratio=alive_ratio,
            strong_ratio=strong_ratio,
            leader_ratio=leader_ratio,
            strong_count=strong_count,
            leader_count=leader_count,
            theme_turnover_krw=theme_turnover,
            thresholds=self.config.theme_status,
        )
        return ThemeConditionSnapshot(
            calculated_at=calculated_at,
            theme_id=theme_id,
            theme_name=theme_name,
            raw_total_members=len(active_members),
            eligible_total_members=eligible_total,
            alive_count=alive_count,
            strong_count=strong_count,
            leader_count=leader_count,
            alive_ratio=round(alive_ratio, 4),
            strong_ratio=round(strong_ratio, 4),
            leader_ratio=round(leader_ratio, 4),
            condition_score=round(score, 4),
            theme_turnover_krw=round(theme_turnover, 4),
            theme_status=status,
            top_leader_symbol=top_leader.symbol if top_leader else "",
            top_leader_name=top_leader.name if top_leader else "",
            top_leader_return_pct=top_leader.return_pct if top_leader else 0.0,
            data_quality_flags=tuple(flags),
            member_hits=tuple(hits),
        )


class ThemeLabRanker:
    def rank(self, snapshots: Iterable[ThemeConditionSnapshot], top_n: int | None = None) -> list[ThemeConditionSnapshot]:
        ranked = sorted(
            snapshots,
            key=lambda item: (
                item.theme_status in {ThemeLabThemeStatus.LEADING_THEME, ThemeLabThemeStatus.SPREADING_THEME},
                item.condition_score,
                item.leader_count,
                item.strong_count,
                item.theme_turnover_krw,
            ),
            reverse=True,
        )
        return ranked[:top_n] if top_n is not None else ranked


@dataclass(frozen=True)
class _SideBreadthStats:
    side: MarketSide
    breadth_pct: float | None = None
    ready: bool = False
    sample_count: int = 0
    total_count: int = 0
    source: str = ""
    advancing_count: int = 0
    declining_count: int = 0
    flat_count: int = 0
    strong_count: int = 0
    weak_count: int = 0
    stale_count: int = 0
    valid_quote_ratio: float = 0.0
    turnover_sum: float = 0.0
    turnover_weighted_return_pct: float | None = None
    reason_codes: tuple[str, ...] = ()
    data_quality_flags: tuple[str, ...] = ()


class MarketStrengthEngine:
    def __init__(
        self,
        config: MarketStatusThresholds | None = None,
        side_breadth_config: MarketSideBreadthConfig | None = None,
    ) -> None:
        self.config = config or MarketStatusThresholds()
        self.side_breadth_config = side_breadth_config or MarketSideBreadthConfig()

    def calculate(
        self,
        snapshots: dict[str, StockSnapshot] | list[StockSnapshot],
        *,
        metadata_by_symbol: dict[str, InstrumentMetadata] | None = None,
        kospi_return_pct: float = 0.0,
        kosdaq_return_pct: float = 0.0,
        calculated_at: str = "",
    ) -> MarketStrengthSnapshot:
        snapshot_map = _snapshot_map(snapshots)
        values = list(snapshot_map.values())
        unique = {normalize_stock_code(item.stock_code): item for item in values if normalize_stock_code(item.stock_code)}
        stock_values = list(unique.values())
        metadata_map = _metadata_map(metadata_by_symbol or {})
        side_stats = self._side_breadth_stats(stock_values, metadata_map, calculated_at=calculated_at)
        kospi_stats = side_stats[MarketSide.KOSPI]
        kosdaq_stats = side_stats[MarketSide.KOSDAQ]
        advancers = sum(1 for item in stock_values if item.change_rate > 0)
        decliners = sum(1 for item in stock_values if item.change_rate < 0)
        strong_count = sum(1 for item in stock_values if item.change_rate >= 3.0)
        leader_count = sum(1 for item in stock_values if item.change_rate >= 5.0)
        turnover = sum(max(0.0, item.turnover) for item in stock_values)
        status = self._status(kospi_return_pct, kosdaq_return_pct, strong_count, leader_count, advancers, decliners)
        kospi_status, kospi_reason_codes, kospi_flags = self._side_status(
            MarketSide.KOSPI,
            kospi_return_pct,
            kospi_stats,
            strong_count,
            leader_count,
            advancers,
            decliners,
        )
        kosdaq_status, kosdaq_reason_codes, kosdaq_flags = self._side_status(
            MarketSide.KOSDAQ,
            kosdaq_return_pct,
            kosdaq_stats,
            strong_count,
            leader_count,
            advancers,
            decliners,
        )
        side_flags = _dedupe_tuple(kospi_flags + kosdaq_flags + kospi_stats.data_quality_flags + kosdaq_stats.data_quality_flags)
        side_reason_codes = _dedupe_tuple(kospi_reason_codes + kosdaq_reason_codes + kospi_stats.reason_codes + kosdaq_stats.reason_codes)
        rounded_kospi = round(kospi_return_pct, 4)
        rounded_kosdaq = round(kosdaq_return_pct, 4)
        return MarketStrengthSnapshot(
            market_status=status,
            kospi_return_pct=rounded_kospi,
            kosdaq_return_pct=rounded_kosdaq,
            advancers=advancers,
            decliners=decliners,
            market_strong_count=strong_count,
            market_leader_count=leader_count,
            market_turnover_krw=round(turnover, 4),
            kospi_status=kospi_status,
            kosdaq_status=kosdaq_status,
            kospi_index_return_pct=rounded_kospi,
            kosdaq_index_return_pct=rounded_kosdaq,
            kospi_index_ready=True,
            kosdaq_index_ready=True,
            side_statuses={
                MarketSide.KOSPI.value: {
                    "status": kospi_status.value,
                    "index_return_pct": rounded_kospi,
                    "index_return_ready": True,
                    "breadth_pct": kospi_stats.breadth_pct,
                    "breadth_ready": kospi_stats.ready,
                    "breadth_sample_count": kospi_stats.sample_count,
                    "breadth_source": kospi_stats.source,
                    "valid_quote_ratio": kospi_stats.valid_quote_ratio,
                    "reason_codes": list(_dedupe_tuple(kospi_reason_codes + kospi_stats.reason_codes)),
                    "data_quality_flags": list(_dedupe_tuple(kospi_flags + kospi_stats.data_quality_flags)),
                },
                MarketSide.KOSDAQ.value: {
                    "status": kosdaq_status.value,
                    "index_return_pct": rounded_kosdaq,
                    "index_return_ready": True,
                    "breadth_pct": kosdaq_stats.breadth_pct,
                    "breadth_ready": kosdaq_stats.ready,
                    "breadth_sample_count": kosdaq_stats.sample_count,
                    "breadth_source": kosdaq_stats.source,
                    "valid_quote_ratio": kosdaq_stats.valid_quote_ratio,
                    "reason_codes": list(_dedupe_tuple(kosdaq_reason_codes + kosdaq_stats.reason_codes)),
                    "data_quality_flags": list(_dedupe_tuple(kosdaq_flags + kosdaq_stats.data_quality_flags)),
                },
            },
            market_side_data_quality_flags=side_flags,
            kospi_breadth_pct=kospi_stats.breadth_pct,
            kosdaq_breadth_pct=kosdaq_stats.breadth_pct,
            kospi_breadth_ready=kospi_stats.ready,
            kosdaq_breadth_ready=kosdaq_stats.ready,
            kospi_breadth_sample_count=kospi_stats.sample_count,
            kosdaq_breadth_sample_count=kosdaq_stats.sample_count,
            kospi_breadth_source=kospi_stats.source,
            kosdaq_breadth_source=kosdaq_stats.source,
            kospi_advancing_count=kospi_stats.advancing_count,
            kosdaq_advancing_count=kosdaq_stats.advancing_count,
            kospi_declining_count=kospi_stats.declining_count,
            kosdaq_declining_count=kosdaq_stats.declining_count,
            kospi_flat_count=kospi_stats.flat_count,
            kosdaq_flat_count=kosdaq_stats.flat_count,
            kospi_strong_count=kospi_stats.strong_count,
            kosdaq_strong_count=kosdaq_stats.strong_count,
            kospi_weak_count=kospi_stats.weak_count,
            kosdaq_weak_count=kosdaq_stats.weak_count,
            kospi_stale_count=kospi_stats.stale_count,
            kosdaq_stale_count=kosdaq_stats.stale_count,
            kospi_valid_quote_ratio=kospi_stats.valid_quote_ratio,
            kosdaq_valid_quote_ratio=kosdaq_stats.valid_quote_ratio,
            kospi_turnover_sum=round(kospi_stats.turnover_sum, 4),
            kosdaq_turnover_sum=round(kosdaq_stats.turnover_sum, 4),
            kospi_turnover_weighted_return_pct=kospi_stats.turnover_weighted_return_pct,
            kosdaq_turnover_weighted_return_pct=kosdaq_stats.turnover_weighted_return_pct,
            side_breadth_data_quality_flags=side_flags,
            side_breadth_reason_codes=side_reason_codes,
        )

    def _status(
        self,
        kospi_return_pct: float,
        kosdaq_return_pct: float,
        strong_count: int,
        leader_count: int,
        advancers: int,
        decliners: int,
    ) -> MarketStatus:
        cfg = self.config
        if kospi_return_pct <= cfg.risk_off_kospi_pct and kosdaq_return_pct <= cfg.risk_off_kosdaq_pct:
            return MarketStatus.RISK_OFF
        if kospi_return_pct <= cfg.weak_kospi_pct and kosdaq_return_pct <= cfg.weak_kosdaq_pct:
            return MarketStatus.WEAK
        if strong_count >= cfg.expansion_strong_count and leader_count >= cfg.expansion_leader_count:
            return MarketStatus.EXPANSION
        if strong_count >= cfg.selective_strong_count and leader_count >= cfg.selective_leader_count:
            return MarketStatus.SELECTIVE
        if strong_count >= cfg.choppy_strong_count or advancers >= decliners:
            return MarketStatus.CHOPPY
        return MarketStatus.WEAK

    def _side_status(
        self,
        side: MarketSide,
        index_return_pct: float,
        breadth: _SideBreadthStats,
        strong_count: int,
        leader_count: int,
        advancers: int,
        decliners: int,
    ) -> tuple[MarketStatus, tuple[str, ...], tuple[str, ...]]:
        cfg = self.config
        reason_codes: list[str] = []
        data_quality: list[str] = []
        if side == MarketSide.KOSPI:
            if index_return_pct <= cfg.risk_off_kospi_pct:
                if breadth.ready and breadth.breadth_pct is not None and breadth.breadth_pct > self.side_breadth_config.breadth_weak_pct:
                    reason_codes.append("INDEX_WEAK_BREADTH_OK")
                return MarketStatus.RISK_OFF, tuple(["KOSPI_MARKET_RISK_OFF"] + reason_codes), ()
            if index_return_pct <= cfg.weak_kospi_pct:
                if breadth.ready and breadth.breadth_pct is not None and breadth.breadth_pct > self.side_breadth_config.breadth_weak_pct:
                    reason_codes.append("INDEX_WEAK_BREADTH_OK")
                return MarketStatus.WEAK, tuple(["KOSPI_MARKET_WEAK"] + reason_codes), ()
        elif side == MarketSide.KOSDAQ:
            if index_return_pct <= cfg.risk_off_kosdaq_pct:
                if breadth.ready and breadth.breadth_pct is not None and breadth.breadth_pct > self.side_breadth_config.breadth_weak_pct:
                    reason_codes.append("INDEX_WEAK_BREADTH_OK")
                return MarketStatus.RISK_OFF, tuple(["KOSDAQ_MARKET_RISK_OFF"] + reason_codes), ()
            if index_return_pct <= cfg.weak_kosdaq_pct:
                if breadth.ready and breadth.breadth_pct is not None and breadth.breadth_pct > self.side_breadth_config.breadth_weak_pct:
                    reason_codes.append("INDEX_WEAK_BREADTH_OK")
                return MarketStatus.WEAK, tuple(["KOSDAQ_MARKET_WEAK"] + reason_codes), ()

        if self.side_breadth_config.enabled and breadth.ready and breadth.breadth_pct is not None:
            status, breadth_reasons = self._status_from_side_breadth(side, breadth, index_return_pct)
            return status, breadth_reasons, ()

        if self.side_breadth_config.enabled:
            data_quality.append("SIDE_BREADTH_NOT_READY")
            if str(self.side_breadth_config.fallback_to_index_return).lower() == "true":
                return self._index_return_side_status(side, index_return_pct), ("SIDE_BREADTH_FALLBACK_INDEX_RETURN",), tuple(data_quality)
            if str(self.side_breadth_config.fallback_to_global_breadth).lower() == "true":
                status = self._breadth_status(strong_count, leader_count, advancers, decliners)
                return status, ("SIDE_BREADTH_FALLBACK_GLOBAL",), tuple(data_quality)

        status = self._breadth_status(strong_count, leader_count, advancers, decliners)
        return status, (), ("SIDE_BREADTH_FALLBACK_GLOBAL",)

    def _index_return_side_status(self, side: MarketSide, index_return_pct: float) -> MarketStatus:
        cfg = self.config
        if side == MarketSide.KOSPI:
            if index_return_pct <= cfg.risk_off_kospi_pct:
                return MarketStatus.RISK_OFF
            if index_return_pct <= cfg.weak_kospi_pct:
                return MarketStatus.WEAK
        if side == MarketSide.KOSDAQ:
            if index_return_pct <= cfg.risk_off_kosdaq_pct:
                return MarketStatus.RISK_OFF
            if index_return_pct <= cfg.weak_kosdaq_pct:
                return MarketStatus.WEAK
        return MarketStatus.CHOPPY

    def _status_from_side_breadth(
        self,
        side: MarketSide,
        breadth: _SideBreadthStats,
        index_return_pct: float,
    ) -> tuple[MarketStatus, tuple[str, ...]]:
        cfg = self.side_breadth_config
        side_prefix = side.value
        breadth_pct = float(breadth.breadth_pct or 0.0)
        if breadth_pct <= cfg.breadth_risk_off_pct:
            return MarketStatus.RISK_OFF, (
                f"{side_prefix}_SIDE_BREADTH_RISK_OFF",
                "SIDE_BREADTH_WEAK_INDEX_OK",
            )
        if breadth_pct <= cfg.breadth_weak_pct:
            return MarketStatus.WEAK, (
                f"{side_prefix}_SIDE_BREADTH_WEAK",
                "SIDE_BREADTH_WEAK_INDEX_OK",
            )
        if breadth_pct >= cfg.breadth_expansion_pct and breadth.strong_count > 0:
            return MarketStatus.EXPANSION, (f"{side_prefix}_SIDE_BREADTH_EXPANSION",)
        if breadth.strong_count > 0 or (breadth.turnover_weighted_return_pct or 0.0) > 0:
            return MarketStatus.SELECTIVE, (f"{side_prefix}_SIDE_BREADTH_SELECTIVE",)
        return MarketStatus.CHOPPY, (f"{side_prefix}_SIDE_BREADTH_CHOPPY",)

    def _side_breadth_stats(
        self,
        stock_values: Iterable[StockSnapshot],
        metadata_by_symbol: dict[str, InstrumentMetadata],
        *,
        calculated_at: str = "",
    ) -> dict[MarketSide, _SideBreadthStats]:
        buckets: dict[MarketSide, list[StockSnapshot]] = {MarketSide.KOSPI: [], MarketSide.KOSDAQ: []}
        for snapshot in stock_values:
            side = _market_side_for_breadth_sample(snapshot, metadata_by_symbol)
            if side in buckets:
                buckets[side].append(snapshot)
        return {
            side: self._calculate_side_breadth(side, items, calculated_at=calculated_at)
            for side, items in buckets.items()
        }

    def _calculate_side_breadth(
        self,
        side: MarketSide,
        snapshots: list[StockSnapshot],
        *,
        calculated_at: str = "",
    ) -> _SideBreadthStats:
        cfg = self.side_breadth_config
        source = "SIDE_BREADTH_SOURCE_REALTIME_UNIVERSE" if snapshots else ""
        min_sample = cfg.min_sample_count_kospi if side == MarketSide.KOSPI else cfg.min_sample_count_kosdaq
        valid_items: list[StockSnapshot] = []
        stale_count = 0
        for snapshot in snapshots:
            valid, reason = _quote_valid_for_breadth(snapshot, calculated_at, cfg.max_quote_age_sec)
            if valid:
                valid_items.append(snapshot)
            elif reason == "STALE_QUOTE":
                stale_count += 1
        total_count = len(snapshots)
        sample_count = len(valid_items)
        valid_quote_ratio = round(sample_count / total_count, 4) if total_count else 0.0
        advancing = sum(1 for item in valid_items if item.change_rate > cfg.advancing_threshold_pct)
        declining = sum(1 for item in valid_items if item.change_rate < cfg.declining_threshold_pct)
        flat = max(0, sample_count - advancing - declining)
        strong = sum(1 for item in valid_items if item.change_rate >= cfg.strong_return_threshold_pct)
        weak = sum(1 for item in valid_items if item.change_rate <= cfg.weak_return_threshold_pct)
        turnover_sum = sum(max(0.0, float(item.turnover or 0.0)) for item in valid_items)
        weighted_return = None
        if cfg.use_turnover_weighted_return and turnover_sum > 0:
            weighted_return = round(
                sum(max(0.0, float(item.turnover or 0.0)) * float(item.change_rate or 0.0) for item in valid_items) / turnover_sum,
                4,
            )
        breadth_pct = round(advancing / sample_count, 4) if sample_count else None
        reason_codes: list[str] = [source] if source else []
        data_flags: list[str] = []
        if not snapshots:
            reason_codes.append("SIDE_BREADTH_FALLBACK_GLOBAL")
            data_flags.append("SIDE_BREADTH_NOT_READY")
        if sample_count < min_sample:
            reason_codes.append("SIDE_BREADTH_SAMPLE_TOO_SMALL")
            data_flags.append("SIDE_BREADTH_SAMPLE_TOO_SMALL")
        if total_count and valid_quote_ratio < cfg.valid_quote_ratio_min:
            reason_codes.append("SIDE_BREADTH_VALID_QUOTE_RATIO_LOW")
            data_flags.append("SIDE_BREADTH_VALID_QUOTE_RATIO_LOW")
        if stale_count:
            data_flags.append("STALE_QUOTE")
        ready = (
            cfg.enabled
            and bool(snapshots)
            and sample_count >= min_sample
            and valid_quote_ratio >= cfg.valid_quote_ratio_min
        )
        if not ready:
            data_flags.append("SIDE_BREADTH_NOT_READY")
        return _SideBreadthStats(
            side=side,
            breadth_pct=breadth_pct,
            ready=ready,
            sample_count=sample_count,
            total_count=total_count,
            source=source,
            advancing_count=advancing,
            declining_count=declining,
            flat_count=flat,
            strong_count=strong,
            weak_count=weak,
            stale_count=stale_count,
            valid_quote_ratio=valid_quote_ratio,
            turnover_sum=turnover_sum,
            turnover_weighted_return_pct=weighted_return,
            reason_codes=_dedupe_tuple(reason_codes),
            data_quality_flags=_dedupe_tuple(data_flags),
        )

    def _breadth_status(
        self,
        strong_count: int,
        leader_count: int,
        advancers: int,
        decliners: int,
    ) -> MarketStatus:
        cfg = self.config
        if strong_count >= cfg.expansion_strong_count and leader_count >= cfg.expansion_leader_count:
            return MarketStatus.EXPANSION
        if strong_count >= cfg.selective_strong_count and leader_count >= cfg.selective_leader_count:
            return MarketStatus.SELECTIVE
        if strong_count >= cfg.choppy_strong_count or advancers >= decliners:
            return MarketStatus.CHOPPY
        return MarketStatus.WEAK


class WatchSetManager:
    def __init__(self, limits: WatchSetLimits | None = None) -> None:
        self.limits = limits or WatchSetLimits()

    def build(
        self,
        ranked_themes: Iterable[ThemeConditionSnapshot],
        snapshots: dict[str, StockSnapshot] | list[StockSnapshot],
        *,
        metadata_by_symbol: dict[str, InstrumentMetadata] | None = None,
        calculated_at: str = "",
    ) -> list[WatchSetSnapshot]:
        snapshot_by_symbol = _snapshot_map(snapshots)
        instrument_by_symbol = _instrument_metadata_map(metadata_by_symbol)
        selected: dict[str, WatchSetSnapshot] = {}
        for theme in list(ranked_themes)[: self.limits.top_theme_count]:
            if theme.theme_status not in {
                ThemeLabThemeStatus.LEADING_THEME,
                ThemeLabThemeStatus.SPREADING_THEME,
                ThemeLabThemeStatus.LEADER_ONLY_THEME,
                ThemeLabThemeStatus.WATCH_THEME,
            }:
                continue
            eligible_hits = [hit for hit in theme.member_hits if not hit.excluded]
            promoted = [
                (hit, "CONDITION3_LEADER" if hit.leader_hit else "CONDITION2_STRONG")
                for hit in eligible_hits
                if hit.strong_hit or hit.leader_hit
            ]
            if not promoted:
                promoted = [
                    (hit, "THEME_TOP_TURNOVER")
                    for hit in sorted(eligible_hits, key=lambda hit: _turnover(snapshot_by_symbol.get(hit.symbol)), reverse=True)[:2]
                    if _turnover(snapshot_by_symbol.get(hit.symbol)) > 0 and (hit.strong_hit or hit.leader_hit)
                ]
            per_theme_count = 0
            for hit, reason in promoted:
                if per_theme_count >= self.limits.max_watch_per_theme:
                    break
                if len(selected) >= self.limits.max_watchset_size:
                    break
                snapshot = snapshot_by_symbol.get(hit.symbol)
                existing = selected.get(hit.symbol)
                themes = tuple(dict.fromkeys(((existing.themes if existing else ()) + (theme.theme_id,))))
                condition_level = 3 if hit.leader_hit else 2 if hit.strong_hit else 1 if hit.alive_hit else 0
                market_side, market_source, market_reason_codes = _infer_candidate_market(
                    snapshot=snapshot,
                    instrument_metadata=instrument_by_symbol.get(hit.symbol),
                    theme=theme,
                    existing=existing,
                )
                selected[hit.symbol] = WatchSetSnapshot(
                    calculated_at=calculated_at,
                    symbol=hit.symbol,
                    name=hit.name,
                    themes=themes,
                    primary_theme=existing.primary_theme if existing else theme.theme_id,
                    return_pct=hit.return_pct,
                    turnover_krw=_turnover(snapshot),
                    condition_level=max(condition_level, existing.condition_level if existing else 0),
                    watch_reason=reason if existing is None else existing.watch_reason,
                    candidate_market=market_side.value,
                    candidate_market_source=market_source,
                    market_side_reason_codes=market_reason_codes,
                )
                per_theme_count += 1
        return list(selected.values())


class StockRoleDetector:
    def detect(
        self,
        watch: WatchSetSnapshot,
        theme: ThemeConditionSnapshot,
        snapshots: dict[str, StockSnapshot] | list[StockSnapshot],
    ) -> StockRole:
        snapshot_by_symbol = _snapshot_map(snapshots)
        if watch.condition_level <= 0:
            return StockRole.WEAK_MEMBER
        leaders = sorted(
            [hit for hit in theme.member_hits if not hit.excluded],
            key=lambda hit: (hit.return_pct, _turnover(snapshot_by_symbol.get(hit.symbol))),
            reverse=True,
        )
        rank = next((index for index, hit in enumerate(leaders, start=1) if hit.symbol == watch.symbol), 999)
        if watch.condition_level >= 3 and rank == 1:
            return StockRole.LEADER
        if watch.condition_level >= 3 and rank <= 3:
            return StockRole.CO_LEADER
        if watch.condition_level >= 2 and theme.strong_ratio >= 0.25:
            return StockRole.FOLLOWER
        if rank >= 6 or (watch.return_pct >= 3.0 and theme.theme_status == ThemeLabThemeStatus.LEADER_ONLY_THEME):
            return StockRole.LATE_LAGGARD
        return StockRole.WEAK_MEMBER


class PriceLocationEvaluator:
    def __init__(self, config: PriceLocationConfig | None = None) -> None:
        self.config = config or PriceLocationConfig()

    def evaluate(self, data: PriceLocationInput) -> PriceLocationResult:
        flags = list(data.data_quality_flags)
        reasons: list[str] = []
        pullback_from_high = _pct_gap(data.session_high, data.current_price, "MISSING_SESSION_HIGH", flags, subtract_from_base=True)
        distance_to_high = _pct_gap(data.current_price, data.session_high, "MISSING_SESSION_HIGH", flags, reverse_distance=True)
        vwap_gap = _pct_gap(data.vwap, data.current_price, "MISSING_VWAP", flags)
        upper_limit_gap = _pct_gap(data.current_price, data.upper_limit_price, "MISSING_UPPER_LIMIT_PRICE", flags)
        breakout_gap = _pct_gap(data.breakout_level, data.current_price, "MISSING_BREAKOUT_LEVEL", flags)
        support_gap = _pct_gap(data.recent_support_price, data.current_price, "MISSING_RECENT_SUPPORT_PRICE", flags)
        upper_wick_risk = self._upper_wick_risk(data, flags)
        failed_breakout = self._failed_breakout(data, breakout_gap, upper_wick_risk, flags)
        pullback_reclaim = self._pullback_reclaim(data, pullback_from_high, vwap_gap)
        breakout_continuation = self._breakout_continuation(data, pullback_from_high, upper_wick_risk)

        if data.current_price is None or data.current_price <= 0:
            _add_flag(flags, "MISSING_CURRENT_PRICE")
        if data.return_pct is None:
            _add_flag(flags, "MISSING_RETURN_PCT")
        if data.stock_role is None:
            _add_flag(flags, "MISSING_STOCK_ROLE")
        if data.theme_status is None:
            _add_flag(flags, "MISSING_THEME_STATUS")
        if data.market_status is None:
            _add_flag(flags, "MISSING_MARKET_STATUS")
        if _has_core_price_missing(flags):
            return self._result(
                data,
                PriceLocationStatus.UNKNOWN,
                reasons + ["PRICE_LOCATION_DATA_MISSING"],
                pullback_from_high,
                distance_to_high,
                vwap_gap,
                upper_limit_gap,
                breakout_gap,
                support_gap,
                upper_wick_risk,
                failed_breakout,
                pullback_reclaim,
                breakout_continuation,
                flags,
            )

        status = self._status(
            data,
            pullback_from_high,
            vwap_gap,
            breakout_gap,
            upper_wick_risk,
            failed_breakout,
            pullback_reclaim,
            breakout_continuation,
            reasons,
        )
        return self._result(
            data,
            status,
            reasons,
            pullback_from_high,
            distance_to_high,
            vwap_gap,
            upper_limit_gap,
            breakout_gap,
            support_gap,
            upper_wick_risk,
            failed_breakout,
            pullback_reclaim,
            breakout_continuation,
            flags,
        )

    def _status(
        self,
        data: PriceLocationInput,
        pullback_from_high: float | None,
        vwap_gap: float | None,
        breakout_gap: float | None,
        upper_wick_risk: bool | None,
        failed_breakout: bool | None,
        pullback_reclaim: bool | None,
        breakout_continuation: bool | None,
        reasons: list[str],
    ) -> PriceLocationStatus:
        cfg = self.config
        role = data.stock_role
        momentum_1m = data.momentum_1m
        momentum_3m = data.momentum_3m
        if failed_breakout is True:
            reasons.append("FAILED_BREAKOUT")
            return PriceLocationStatus.FAILED_BREAKOUT
        if pullback_from_high is None:
            reasons.append("PRICE_LOCATION_UNKNOWN")
            return PriceLocationStatus.UNKNOWN
        if (
            pullback_from_high > cfg.max_healthy_pullback_pct
            and _negative_or_missing(vwap_gap)
            and _negative_or_missing(momentum_3m)
        ):
            reasons.append("DEEP_PULLBACK")
            return PriceLocationStatus.DEEP_PULLBACK
        if vwap_gap is not None and vwap_gap > _max_vwap_gap(cfg, role):
            reasons.append("VWAP_OVEREXTENDED")
            return PriceLocationStatus.VWAP_OVEREXTENDED
        if pullback_reclaim is True:
            reasons.append("PULLBACK_RECLAIM")
            return PriceLocationStatus.PULLBACK_RECLAIM
        if self._vwap_reclaim(data, vwap_gap):
            reasons.append("VWAP_RECLAIM")
            return PriceLocationStatus.VWAP_RECLAIM
        if (
            cfg.good_pullback_min_pct <= pullback_from_high <= cfg.good_pullback_max_pct
            and (vwap_gap is None or vwap_gap >= -0.5)
            and _positive(momentum_1m, momentum_3m)
            and failed_breakout is not True
        ):
            reasons.append("GOOD_PULLBACK")
            return PriceLocationStatus.GOOD_PULLBACK
        if breakout_continuation is True:
            reasons.append("BREAKOUT_CONTINUATION")
            return PriceLocationStatus.BREAKOUT_CONTINUATION
        if pullback_from_high < cfg.min_pullback_from_high_pct:
            reasons.append("CHASE_HIGH")
            return PriceLocationStatus.CHASE_HIGH
        if breakout_gap is not None and breakout_gap > cfg.max_breakout_extension_pct:
            reasons.append("BREAKOUT_EXTENSION")
            return PriceLocationStatus.CHASE_HIGH
        reasons.append("PRICE_LOCATION_UNKNOWN")
        return PriceLocationStatus.UNKNOWN

    def _result(
        self,
        data: PriceLocationInput,
        status: PriceLocationStatus,
        reasons: list[str],
        pullback_from_high: float | None,
        distance_to_high: float | None,
        vwap_gap: float | None,
        upper_limit_gap: float | None,
        breakout_gap: float | None,
        support_gap: float | None,
        upper_wick_risk: bool | None,
        failed_breakout: bool | None,
        pullback_reclaim: bool | None,
        breakout_continuation: bool | None,
        flags: list[str],
    ) -> PriceLocationResult:
        return PriceLocationResult(
            symbol=normalize_stock_code(data.symbol),
            status=status,
            score=_price_location_score(status, data.stock_role),
            reason_codes=tuple(dict.fromkeys(reasons)),
            pullback_from_high_pct=_round_optional(pullback_from_high),
            distance_to_session_high_pct=_round_optional(distance_to_high),
            vwap_gap_pct=_round_optional(vwap_gap),
            upper_limit_gap_pct=_round_optional(upper_limit_gap),
            breakout_level_gap_pct=_round_optional(breakout_gap),
            support_gap_pct=_round_optional(support_gap),
            upper_wick_risk=upper_wick_risk,
            failed_breakout=failed_breakout,
            pullback_reclaim=pullback_reclaim,
            breakout_continuation=breakout_continuation,
            data_quality_flags=tuple(dict.fromkeys(flags)),
        )

    def _upper_wick_risk(self, data: PriceLocationInput, flags: list[str]) -> bool | None:
        candles = data.recent_candles_1m or data.recent_candles_3m
        if not candles:
            _add_flag(flags, "MISSING_RECENT_CANDLES")
            return None
        latest = _candle_dict(candles[-1])
        high = _float_or_none(latest.get("high"))
        close = _float_or_none(latest.get("close"))
        low = _float_or_none(latest.get("low"))
        if high is None or close is None or low is None or high <= low:
            _add_flag(flags, "INVALID_RECENT_CANDLE")
            return None
        ratio = (high - close) / (high - low)
        return ratio >= self.config.upper_wick_risk_threshold

    def _failed_breakout(
        self,
        data: PriceLocationInput,
        breakout_gap: float | None,
        upper_wick_risk: bool | None,
        flags: list[str],
    ) -> bool | None:
        if breakout_gap is None or upper_wick_risk is None:
            return None
        return breakout_gap < 0 and upper_wick_risk and (data.momentum_1m is not None and data.momentum_1m < 0)

    def _pullback_reclaim(self, data: PriceLocationInput, pullback_from_high: float | None, vwap_gap: float | None) -> bool | None:
        if pullback_from_high is None or vwap_gap is None:
            return None
        return (
            pullback_from_high >= self.config.good_pullback_min_pct
            and vwap_gap >= 0
            and data.momentum_1m is not None
            and data.momentum_1m > self.config.min_reclaim_momentum_1m_pct
            and data.turnover_krw is not None
            and data.turnover_krw > 0
        )

    def _breakout_continuation(
        self,
        data: PriceLocationInput,
        pullback_from_high: float | None,
        upper_wick_risk: bool | None,
    ) -> bool | None:
        if pullback_from_high is None:
            return None
        if data.stock_role not in {StockRole.LEADER, StockRole.CO_LEADER}:
            return False
        if upper_wick_risk is True:
            return False
        return (
            pullback_from_high <= self.config.min_pullback_from_high_pct
            and data.momentum_1m is not None
            and data.momentum_3m is not None
            and data.momentum_1m > 0
            and data.momentum_3m > self.config.min_reclaim_momentum_3m_pct
            and data.turnover_krw is not None
            and data.turnover_krw > 0
        )

    def _vwap_reclaim(self, data: PriceLocationInput, vwap_gap: float | None) -> bool:
        return (
            vwap_gap is not None
            and vwap_gap >= 0
            and data.momentum_1m is not None
            and data.momentum_1m > self.config.min_reclaim_momentum_1m_pct
            and data.theme_status in {ThemeLabThemeStatus.LEADING_THEME, ThemeLabThemeStatus.SPREADING_THEME}
        )


class TradeabilityRiskFilter:
    def __init__(self, config: TradeabilityRiskConfig | None = None) -> None:
        self.config = config or TradeabilityRiskConfig()

    def evaluate(self, risk_input: TradeabilityRiskInput) -> TradeabilityRiskResult:
        cfg = self.config
        role = risk_input.stock_role
        reasons: list[str] = []
        if _has_hard_data_quality(risk_input.data_quality_flags):
            return TradeabilityRiskResult(TradeabilityRiskLevel.HARD_BLOCK, ("DATA_QUALITY_BLOCK",), 0.0)
        if risk_input.vi_active:
            return TradeabilityRiskResult(TradeabilityRiskLevel.HARD_BLOCK, ("VI_ACTIVE",), 0.0)
        if role == StockRole.LATE_LAGGARD:
            return TradeabilityRiskResult(TradeabilityRiskLevel.HARD_BLOCK, ("LATE_LAGGARD",), 0.0)
        if role == StockRole.FOLLOWER and risk_input.leader_momentum_status.upper() == "PEAKED_OUT":
            return TradeabilityRiskResult(TradeabilityRiskLevel.HARD_BLOCK, ("LEADER_PEAKED_OUT",), 0.0)

        if 0 < risk_input.seconds_since_vi_release < cfg.vi_cooldown_sec:
            if _leader_like(role) and _turnover_maintained(risk_input):
                return TradeabilityRiskResult(
                    TradeabilityRiskLevel.SOFT_BLOCK,
                    ("VI_COOLDOWN",),
                    0.0,
                    cfg.soft_block_recheck_sec,
                )
            return TradeabilityRiskResult(
                TradeabilityRiskLevel.SOFT_BLOCK,
                ("VI_COOLDOWN",),
                0.0,
                cfg.soft_block_recheck_sec,
            )

        if risk_input.upper_limit_gap_pct < cfg.min_upper_limit_gap_pct:
            if role == StockRole.LEADER and _trade_flow_maintained(risk_input):
                return TradeabilityRiskResult(
                    TradeabilityRiskLevel.SOFT_BLOCK,
                    ("UPPER_LIMIT_NEAR",),
                    0.0,
                    cfg.soft_block_recheck_sec,
                )
            return TradeabilityRiskResult(TradeabilityRiskLevel.HARD_BLOCK, ("UPPER_LIMIT_NEAR",), 0.0)

        max_return_result = self._return_risk(risk_input)
        if max_return_result is not None:
            return max_return_result

        pullback_result = self._pullback_risk(risk_input)
        if pullback_result is not None:
            return pullback_result

        if reasons:
            return TradeabilityRiskResult(TradeabilityRiskLevel.RISK_ADJUST, tuple(reasons), cfg.risk_adjust_multiplier_mild)
        return TradeabilityRiskResult(TradeabilityRiskLevel.PASS, (), 1.0)

    def _return_risk(self, risk_input: TradeabilityRiskInput) -> TradeabilityRiskResult | None:
        cfg = self.config
        role = risk_input.stock_role
        if role == StockRole.LEADER and risk_input.return_pct >= cfg.leader_max_buy_return_pct:
            if risk_input.theme_status == ThemeLabThemeStatus.LEADING_THEME and risk_input.momentum_3m > 0:
                return TradeabilityRiskResult(TradeabilityRiskLevel.RISK_ADJUST, ("HIGH_RETURN_LEADER",), cfg.risk_adjust_multiplier_high)
            return TradeabilityRiskResult(TradeabilityRiskLevel.SOFT_BLOCK, ("HIGH_RETURN_LEADER",), 0.0, cfg.soft_block_recheck_sec)
        if role == StockRole.CO_LEADER and risk_input.return_pct >= cfg.co_leader_max_buy_return_pct:
            if risk_input.momentum_3m > 0 and _turnover_maintained(risk_input):
                return TradeabilityRiskResult(TradeabilityRiskLevel.RISK_ADJUST, ("HIGH_RETURN_CO_LEADER",), cfg.risk_adjust_multiplier_high)
            return TradeabilityRiskResult(TradeabilityRiskLevel.SOFT_BLOCK, ("HIGH_RETURN_CO_LEADER",), 0.0, cfg.soft_block_recheck_sec)
        if role == StockRole.FOLLOWER and risk_input.return_pct >= cfg.follower_max_buy_return_pct:
            return TradeabilityRiskResult(TradeabilityRiskLevel.SOFT_BLOCK, ("HIGH_RETURN_FOLLOWER",), 0.0, cfg.soft_block_recheck_sec)
        if role == StockRole.LATE_LAGGARD and risk_input.return_pct >= cfg.late_laggard_max_buy_return_pct:
            return TradeabilityRiskResult(TradeabilityRiskLevel.HARD_BLOCK, ("LATE_LAGGARD",), 0.0)
        return None

    def _pullback_risk(self, risk_input: TradeabilityRiskInput) -> TradeabilityRiskResult | None:
        cfg = self.config
        role = risk_input.stock_role
        threshold = cfg.leader_min_pullback_from_high_pct if _leader_like(role) else cfg.follower_min_pullback_from_high_pct
        if risk_input.pullback_from_high_pct >= threshold:
            return None
        if _leader_like(role) and risk_input.momentum_1m > 0 and _turnover_maintained(risk_input):
            return TradeabilityRiskResult(TradeabilityRiskLevel.RISK_ADJUST, ("HIGH_CHASE_LEADER",), cfg.risk_adjust_multiplier_high)
        return TradeabilityRiskResult(TradeabilityRiskLevel.SOFT_BLOCK, ("HIGH_CHASE_RISK",), 0.0, cfg.soft_block_recheck_sec)


class ThemeLabHybridGate:
    def __init__(
        self,
        risk_filter: TradeabilityRiskFilter | None = None,
        position_config: PositionAdjustmentConfig | None = None,
        market_side_config: MarketSideGateConfig | None = None,
    ) -> None:
        self.risk_filter = risk_filter or TradeabilityRiskFilter()
        self.position_config = position_config or PositionAdjustmentConfig()
        self.market_side_config = market_side_config or MarketSideGateConfig()

    def evaluate(
        self,
        *,
        market: MarketStrengthSnapshot,
        theme: ThemeConditionSnapshot,
        watch: WatchSetSnapshot,
        price_location: PriceLocationResult,
        snapshot: StockSnapshot | None = None,
        data_quality_flags: Iterable[str] = (),
    ) -> LabGateDecision:
        flags = set(data_quality_flags) | set(theme.data_quality_flags) | set(price_location.data_quality_flags)
        role = watch.stock_role
        risk = self.risk_filter.evaluate(_risk_input(market, theme, watch, snapshot, flags))
        risk = _risk_adjusted_for_price_location(risk, role, price_location, self.position_config)
        market_context = _market_side_context(market, watch)

        def finalize(decision: LabGateDecision, *, action: str = "", reason_codes: tuple[str, ...] = ()) -> LabGateDecision:
            context = dict(market_context)
            if action:
                context["candidate_market_action"] = action
            if reason_codes:
                context["market_side_reason_codes"] = _dedupe_tuple(
                    tuple(context.get("market_side_reason_codes") or ()) + tuple(reason_codes)
                )
            return LabGateDecision(**{**decision.__dict__, **context})

        if risk.risk_level == TradeabilityRiskLevel.HARD_BLOCK:
            return finalize(
                LabGateDecision(
                    watch.symbol,
                    LabGateStatus.BLOCKED,
                    risk.reason_codes,
                    risk.reason_codes[0] if risk.reason_codes else "HARD_BLOCK",
                    risk.risk_level,
                    risk.reason_codes,
                    risk.position_size_multiplier,
                    risk.recheck_after_sec,
                    price_location.status,
                    price_location.score,
                    price_location.reason_codes,
                )
            )
        if risk.risk_level == TradeabilityRiskLevel.SOFT_BLOCK:
            return finalize(
                LabGateDecision(
                    watch.symbol,
                    LabGateStatus.WAIT,
                    risk.reason_codes or ("RISK_SOFT_BLOCK",),
                    "",
                    risk.risk_level,
                    risk.reason_codes,
                    0.0,
                    risk.recheck_after_sec,
                    price_location.status,
                    price_location.score,
                    price_location.reason_codes,
                ),
                action="TEMPORARY_WAIT",
            )
        if market.market_status == MarketStatus.RISK_OFF:
            reasons = ("GLOBAL_MARKET_RISK_OFF", "WAIT_MARKET_RECOVERY")
            if not self.market_side_config.enabled or str(self.market_side_config.global_risk_off_action).lower() != "temporary_wait":
                return finalize(
                    LabGateDecision(
                        watch.symbol,
                        LabGateStatus.BLOCKED,
                        reasons,
                        "GLOBAL_MARKET_RISK_OFF",
                        risk.risk_level,
                        risk.reason_codes,
                        0.0,
                        0,
                        price_location.status,
                        price_location.score,
                        price_location.reason_codes,
                    ),
                    action="FINAL_BLOCK",
                    reason_codes=reasons,
                )
            return finalize(
                LabGateDecision(
                    watch.symbol,
                    LabGateStatus.WAIT,
                    reasons,
                    "",
                    risk.risk_level,
                    risk.reason_codes,
                    0.0,
                    self.market_side_config.recheck_after_sec,
                    price_location.status,
                    price_location.score,
                    price_location.reason_codes,
                ),
                action="TEMPORARY_WAIT",
                reason_codes=reasons,
            )
        market_wait_reasons = _candidate_market_wait_reasons(market, watch, self.market_side_config)
        if market_wait_reasons:
            return finalize(
                LabGateDecision(
                    watch.symbol,
                    LabGateStatus.WAIT,
                    market_wait_reasons,
                    "",
                    risk.risk_level,
                    risk.reason_codes,
                    0.0,
                    self.market_side_config.recheck_after_sec,
                    price_location.status,
                    price_location.score,
                    price_location.reason_codes,
                ),
                action="TEMPORARY_WAIT",
                reason_codes=market_wait_reasons,
            )
        if theme.theme_status == ThemeLabThemeStatus.LEADER_ONLY_THEME and role not in {StockRole.LEADER, StockRole.CO_LEADER}:
            return finalize(
                LabGateDecision(
                    watch.symbol,
                    LabGateStatus.BLOCKED,
                    ("LEADER_ONLY_THEME_LAGGARD_BLOCK",),
                    "LEADER_ONLY_THEME_LAGGARD_BLOCK",
                    risk.risk_level,
                    risk.reason_codes,
                    0.0,
                    risk.recheck_after_sec,
                    price_location.status,
                    price_location.score,
                    price_location.reason_codes,
                )
            )
        if _price_location_hard_blocks(role, price_location):
            return finalize(
                LabGateDecision(
                    watch.symbol,
                    LabGateStatus.BLOCKED,
                    price_location.reason_codes or (price_location.status.value,),
                    price_location.status.value,
                    risk.risk_level,
                    risk.reason_codes,
                    0.0,
                    _price_location_recheck_sec(price_location),
                    price_location.status,
                    price_location.score,
                    price_location.reason_codes,
                )
            )
        base_ready = _theme_market_role_ready(market, theme, role)
        ready_location = price_location.status in {
            PriceLocationStatus.GOOD_PULLBACK,
            PriceLocationStatus.PULLBACK_RECLAIM,
            PriceLocationStatus.VWAP_RECLAIM,
        }
        ready_small_location = price_location.status in {
            PriceLocationStatus.BREAKOUT_CONTINUATION,
            PriceLocationStatus.CHASE_HIGH,
            PriceLocationStatus.VWAP_OVEREXTENDED,
        }
        if theme.theme_status in {ThemeLabThemeStatus.LEADING_THEME, ThemeLabThemeStatus.SPREADING_THEME} and role in {
            StockRole.LEADER,
            StockRole.CO_LEADER,
            StockRole.FOLLOWER,
        }:
            if theme.strong_ratio >= 0.25 and theme.leader_ratio >= 0.05 and market.market_status in {
                MarketStatus.EXPANSION,
                MarketStatus.SELECTIVE,
                MarketStatus.CHOPPY,
            }:
                base_ready = True
        if base_ready and ready_location and risk.risk_level in {TradeabilityRiskLevel.PASS, TradeabilityRiskLevel.RISK_ADJUST}:
            if role == StockRole.FOLLOWER:
                return finalize(
                    LabGateDecision(
                        watch.symbol,
                        LabGateStatus.READY_SMALL,
                        ("THEME_LAB_FOLLOWER_READY_SMALL",) + price_location.reason_codes + risk.reason_codes,
                        "",
                        TradeabilityRiskLevel.RISK_ADJUST if risk.risk_level == TradeabilityRiskLevel.PASS else risk.risk_level,
                        risk.reason_codes,
                        min(risk.position_size_multiplier, self.position_config.ready_small_multiplier_follower),
                        risk.recheck_after_sec,
                        price_location.status,
                        price_location.score,
                        price_location.reason_codes,
                    )
                )
            if risk.risk_level == TradeabilityRiskLevel.RISK_ADJUST:
                return finalize(
                    LabGateDecision(
                        watch.symbol,
                        LabGateStatus.READY_SMALL,
                        ("THEME_LAB_READY_SMALL",) + risk.reason_codes,
                        "",
                        risk.risk_level,
                        risk.reason_codes,
                        risk.position_size_multiplier,
                        risk.recheck_after_sec,
                        price_location.status,
                        price_location.score,
                        price_location.reason_codes,
                    )
                )
            return finalize(
                LabGateDecision(
                    watch.symbol,
                    LabGateStatus.READY,
                    ("THEME_LAB_READY",),
                    "",
                    risk.risk_level,
                    risk.reason_codes,
                    risk.position_size_multiplier,
                    risk.recheck_after_sec,
                    price_location.status,
                    price_location.score,
                    price_location.reason_codes,
                )
            )
        if base_ready and _leader_like(role) and ready_small_location and risk.risk_level == TradeabilityRiskLevel.RISK_ADJUST:
            return finalize(
                LabGateDecision(
                    watch.symbol,
                    LabGateStatus.READY_SMALL,
                    ("PRICE_LOCATION_READY_SMALL",) + price_location.reason_codes + risk.reason_codes,
                    "",
                    risk.risk_level,
                    risk.reason_codes,
                    risk.position_size_multiplier,
                    risk.recheck_after_sec,
                    price_location.status,
                    price_location.score,
                    price_location.reason_codes,
                )
            )
        if watch.condition_level >= 2:
            return finalize(
                LabGateDecision(
                    watch.symbol,
                    LabGateStatus.WAIT,
                    ("WATCHSET_WAIT_CONFIRMATION",) + price_location.reason_codes + risk.reason_codes,
                    "",
                    risk.risk_level,
                    risk.reason_codes,
                    risk.position_size_multiplier,
                    risk.recheck_after_sec or _price_location_recheck_sec(price_location),
                    price_location.status,
                    price_location.score,
                    price_location.reason_codes,
                )
            )
        return finalize(
            LabGateDecision(
                watch.symbol,
                LabGateStatus.OBSERVE,
                ("THEME_LAB_OBSERVE",) + price_location.reason_codes,
                "",
                risk.risk_level,
                risk.reason_codes,
                risk.position_size_multiplier,
                risk.recheck_after_sec or _price_location_recheck_sec(price_location),
                price_location.status,
                price_location.score,
                price_location.reason_codes,
            )
        )


class ThemeLabFlowEngine:
    def __init__(self, config: ThemeLabConfig | None = None) -> None:
        self.config = config or ThemeLabConfig()
        self.market_engine = MarketStrengthEngine(self.config.market_status, self.config.market_side_breadth)
        self.breadth_engine = ThemeBreadthEngine(self.config)
        self.ranker = ThemeLabRanker()
        self.watchset_manager = WatchSetManager(self.config.watchset_limits)
        self.role_detector = StockRoleDetector()
        self.price_location_evaluator = PriceLocationEvaluator(self.config.price_location)
        self.gate = ThemeLabHybridGate(
            TradeabilityRiskFilter(self.config.tradeability_risk),
            self.config.position_adjustment,
            self.config.market_side_gate,
        )

    def run_pipeline(
        self,
        *,
        theme_inputs: Iterable[tuple[str, str, list[ThemeMembership]]],
        snapshots: dict[str, StockSnapshot] | list[StockSnapshot],
        metadata_by_symbol: dict[str, InstrumentMetadata] | None = None,
        kospi_return_pct: float = 0.0,
        kosdaq_return_pct: float = 0.0,
        calculated_at: str = "",
    ) -> ThemeLabFlowResult:
        market = self.market_engine.calculate(
            snapshots,
            metadata_by_symbol=metadata_by_symbol,
            kospi_return_pct=kospi_return_pct,
            kosdaq_return_pct=kosdaq_return_pct,
            calculated_at=calculated_at,
        )
        themes = self.breadth_engine.calculate(
            theme_inputs,
            snapshots,
            metadata_by_symbol,
            calculated_at=calculated_at,
        )
        ranked_themes = self.ranker.rank(themes, top_n=self.config.watchset_limits.top_theme_count)
        watchset = self.watchset_manager.build(
            ranked_themes,
            snapshots,
            metadata_by_symbol=metadata_by_symbol,
            calculated_at=calculated_at,
        )
        theme_by_id = {theme.theme_id: theme for theme in ranked_themes}
        enriched_watchset: list[WatchSetSnapshot] = []
        decisions: list[LabGateDecision] = []
        for watch in watchset:
            theme = theme_by_id.get(watch.primary_theme)
            if theme is None:
                continue
            role = self.role_detector.detect(watch, theme, snapshots)
            enriched = WatchSetSnapshot(**{**watch.__dict__, "stock_role": role})
            snapshot = _snapshot_map(snapshots).get(enriched.symbol)
            enriched = WatchSetSnapshot(
                **{
                    **enriched.__dict__,
                    **_market_side_context(market, enriched),
                }
            )
            price_location = self.price_location_evaluator.evaluate(_price_location_input(market, theme, enriched, snapshot))
            decision = self.gate.evaluate(
                market=market,
                theme=theme,
                watch=enriched,
                price_location=price_location,
                snapshot=snapshot,
            )
            risk_input = _risk_input(market, theme, enriched, snapshot, theme.data_quality_flags)
            enriched = WatchSetSnapshot(
                **{
                    **enriched.__dict__,
                    "gate_status": decision.status,
                    "final_gate_status": decision.status,
                    "risk_level": decision.risk_level,
                    "risk_reason_codes": decision.risk_reason_codes,
                    "position_size_multiplier": decision.position_size_multiplier,
                    "recheck_after_sec": decision.recheck_after_sec,
                    "vi_active": risk_input.vi_active,
                    "seconds_since_vi_release": risk_input.seconds_since_vi_release,
                    "upper_limit_gap_pct": risk_input.upper_limit_gap_pct,
                    "pullback_from_high_pct": risk_input.pullback_from_high_pct,
                    "price_location_status": price_location.status,
                    "price_location_score": price_location.score,
                    "price_location_reason_codes": price_location.reason_codes,
                    "distance_to_session_high_pct": price_location.distance_to_session_high_pct,
                    "vwap_gap_pct": price_location.vwap_gap_pct,
                    "breakout_level_gap_pct": price_location.breakout_level_gap_pct,
                    "support_gap_pct": price_location.support_gap_pct,
                    "upper_wick_risk": price_location.upper_wick_risk,
                    "failed_breakout": price_location.failed_breakout,
                    "pullback_reclaim": price_location.pullback_reclaim,
                    "breakout_continuation": price_location.breakout_continuation,
                    "price_location_data_quality_flags": price_location.data_quality_flags,
                    "candidate_market": decision.candidate_market or enriched.candidate_market,
                    "candidate_market_source": decision.candidate_market_source or enriched.candidate_market_source,
                    "candidate_market_status": decision.candidate_market_status or enriched.candidate_market_status,
                    "candidate_market_action": decision.candidate_market_action or enriched.candidate_market_action,
                    "candidate_index_return_pct": decision.candidate_index_return_pct
                    if decision.candidate_index_return_pct is not None
                    else enriched.candidate_index_return_pct,
                    "global_market_status": decision.global_market_status or enriched.global_market_status,
                    "kospi_market_status": decision.kospi_market_status or enriched.kospi_market_status,
                    "kosdaq_market_status": decision.kosdaq_market_status or enriched.kosdaq_market_status,
                    "kospi_return_pct": decision.kospi_return_pct if decision.kospi_return_pct is not None else enriched.kospi_return_pct,
                    "kosdaq_return_pct": decision.kosdaq_return_pct if decision.kosdaq_return_pct is not None else enriched.kosdaq_return_pct,
                    "market_side_reason_codes": decision.market_side_reason_codes or enriched.market_side_reason_codes,
                    "market_side_data_quality_flags": decision.market_side_data_quality_flags or enriched.market_side_data_quality_flags,
                }
            )
            enriched_watchset.append(enriched)
            decisions.append(decision)
        return ThemeLabFlowResult(
            market=market,
            themes=tuple(ranked_themes),
            watchset=tuple(enriched_watchset),
            gate_decisions=tuple(decisions),
            data_quality={
                **_quality_summary(ranked_themes),
                **_market_classification_summary(enriched_watchset, decisions),
            },
        )


def classify_theme_status(
    *,
    eligible_total_members: int,
    alive_ratio: float,
    strong_ratio: float,
    leader_ratio: float,
    strong_count: int,
    leader_count: int,
    theme_turnover_krw: float,
    thresholds: ThemeStatusThresholds | None = None,
) -> ThemeLabThemeStatus:
    cfg = thresholds or ThemeStatusThresholds()
    if eligible_total_members < cfg.min_eligible_members:
        return ThemeLabThemeStatus.WEAK_THEME
    if (
        strong_count >= cfg.min_strong_count_for_leading
        and (leader_count >= cfg.min_leader_count_for_leading or leader_ratio >= cfg.min_leader_ratio_for_leading)
        and strong_ratio >= cfg.min_strong_ratio_for_leading
        and theme_turnover_krw >= cfg.min_theme_turnover_krw_for_leading
    ):
        return ThemeLabThemeStatus.LEADING_THEME
    if leader_count >= 1 and strong_count <= cfg.max_strong_count_for_leader_only and strong_ratio <= cfg.max_strong_ratio_for_leader_only:
        return ThemeLabThemeStatus.LEADER_ONLY_THEME
    if alive_ratio >= cfg.min_alive_ratio_for_spreading and strong_ratio >= cfg.min_strong_ratio_for_spreading:
        return ThemeLabThemeStatus.SPREADING_THEME
    if alive_ratio >= cfg.min_alive_ratio_for_spreading:
        return ThemeLabThemeStatus.WATCH_THEME
    return ThemeLabThemeStatus.WEAK_THEME


def normalize_market_side(value: Any) -> MarketSide:
    if isinstance(value, MarketSide):
        return value
    text = str(value or "").strip().upper()
    if not text:
        return MarketSide.UNKNOWN
    if text in {"KOSPI", "KS", "KSE"} or "코스피" in text or "유가증권" in text:
        return MarketSide.KOSPI
    if text in {"KOSDAQ", "KQ"} or "코스닥" in text:
        return MarketSide.KOSDAQ
    if "KOSDAQ" in text:
        return MarketSide.KOSDAQ
    if "KOSPI" in text:
        return MarketSide.KOSPI
    return MarketSide.UNKNOWN


def _market_side_for_breadth_sample(
    snapshot: StockSnapshot,
    metadata_by_symbol: dict[str, InstrumentMetadata],
) -> MarketSide:
    symbol = normalize_stock_code(snapshot.stock_code)
    metadata = dict(snapshot.metadata or {})
    for key in ("market", "exchange", "market_type"):
        side = normalize_market_side(metadata.get(key))
        if side != MarketSide.UNKNOWN:
            return side
    instrument = metadata_by_symbol.get(symbol) or metadata_by_symbol.get(snapshot.stock_code)
    if instrument is not None:
        raw = dict(instrument.raw or {})
        for key in ("market", "exchange", "market_type"):
            side = normalize_market_side(raw.get(key))
            if side != MarketSide.UNKNOWN:
                return side
    return MarketSide.UNKNOWN


def _quote_valid_for_breadth(snapshot: StockSnapshot, calculated_at: str, max_quote_age_sec: int) -> tuple[bool, str]:
    metadata = dict(snapshot.metadata or {})
    if _metadata_bool(metadata, "stale_quote") or _metadata_bool(metadata, "quote_stale") or _metadata_bool(metadata, "latest_tick_stale"):
        return False, "STALE_QUOTE"
    if _positive_float_or_none(snapshot.current_price) is None and _positive_float_or_none(metadata.get("current_price")) is None:
        return False, "MISSING_CURRENT_PRICE"
    age = _quote_age_sec(snapshot, metadata, calculated_at)
    if age is not None and age > max_quote_age_sec:
        return False, "STALE_QUOTE"
    return True, ""


def _quote_age_sec(snapshot: StockSnapshot, metadata: dict[str, Any], calculated_at: str) -> float | None:
    raw_age = metadata.get("quote_age_sec") or metadata.get("latest_tick_age_sec")
    if raw_age not in (None, ""):
        try:
            return max(0.0, float(raw_age))
        except (TypeError, ValueError):
            return None
    now = _parse_datetime_or_none(calculated_at)
    raw_ts = metadata.get("quote_ts") or metadata.get("tick_ts") or metadata.get("latest_tick_at") or snapshot.updated_at or snapshot.ts
    ts = _parse_datetime_or_none(raw_ts)
    if now is None or ts is None:
        return None
    return max(0.0, (now.replace(tzinfo=None) - ts.replace(tzinfo=None)).total_seconds())


def _parse_datetime_or_none(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _infer_candidate_market(
    *,
    snapshot: StockSnapshot | None = None,
    instrument_metadata: InstrumentMetadata | None = None,
    theme: ThemeConditionSnapshot | None = None,
    existing: WatchSetSnapshot | None = None,
) -> tuple[MarketSide, str, tuple[str, ...]]:
    snapshot_metadata = dict(snapshot.metadata if snapshot else {})
    for key in ("market", "exchange", "market_type"):
        side = normalize_market_side(snapshot_metadata.get(key))
        if side != MarketSide.UNKNOWN:
            return side, f"snapshot.metadata.{key}", ()

    if instrument_metadata is not None:
        raw = dict(instrument_metadata.raw or {})
        for key in ("market", "exchange", "market_type"):
            side = normalize_market_side(raw.get(key))
            if side != MarketSide.UNKNOWN:
                return side, f"metadata_by_symbol.raw.{key}", ()

    for owner, source_prefix in ((theme, "theme"), (existing, "existing_watch")):
        if owner is None:
            continue
        raw = getattr(owner, "market", "")
        side = normalize_market_side(raw)
        if side != MarketSide.UNKNOWN:
            return side, f"{source_prefix}.market", ()

    return MarketSide.UNKNOWN, "", ("MARKET_CLASSIFICATION_MISSING",)


def _instrument_metadata_map(metadata_by_symbol: dict[str, InstrumentMetadata] | None) -> dict[str, InstrumentMetadata]:
    result: dict[str, InstrumentMetadata] = {}
    for key, value in (metadata_by_symbol or {}).items():
        symbol = normalize_stock_code(key or value.symbol)
        if symbol:
            result[symbol] = value
    return result


def _market_status_value(status: MarketStatus | str | None) -> str:
    if isinstance(status, MarketStatus):
        return status.value
    return str(status or "")


def _dedupe_tuple(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _market_side_context(market: MarketStrengthSnapshot, watch: WatchSetSnapshot) -> dict[str, Any]:
    side = normalize_market_side(watch.candidate_market)
    kospi_status = market.status_for_side(MarketSide.KOSPI)
    kosdaq_status = market.status_for_side(MarketSide.KOSDAQ)
    side_status = market.status_for_side(side)
    reason_codes = tuple(watch.market_side_reason_codes or ())
    side_detail = dict(market.side_statuses.get(side.value) or {}) if side != MarketSide.UNKNOWN else {}
    reason_codes = _dedupe_tuple(reason_codes + tuple(side_detail.get("reason_codes") or ()))
    if side == MarketSide.UNKNOWN and "MARKET_CLASSIFICATION_MISSING" not in reason_codes:
        reason_codes = reason_codes + ("MARKET_CLASSIFICATION_MISSING",)
    data_quality_flags = _dedupe_tuple(
        tuple(watch.market_side_data_quality_flags or ())
        + tuple(market.market_side_data_quality_flags or ())
        + tuple(market.side_breadth_data_quality_flags or ())
        + tuple(side_detail.get("data_quality_flags") or ())
    )
    return {
        "candidate_market": side.value,
        "candidate_market_source": watch.candidate_market_source,
        "candidate_market_status": _market_status_value(side_status) if side_status is not None else "UNKNOWN",
        "candidate_market_action": "PASS" if side != MarketSide.UNKNOWN else "UNKNOWN_PASS",
        "candidate_index_return_pct": market.index_return_for_side(side),
        "global_market_status": _market_status_value(market.market_status),
        "kospi_market_status": _market_status_value(kospi_status),
        "kosdaq_market_status": _market_status_value(kosdaq_status),
        "kospi_return_pct": market.index_return_for_side(MarketSide.KOSPI),
        "kosdaq_return_pct": market.index_return_for_side(MarketSide.KOSDAQ),
        "candidate_breadth_pct": side_detail.get("breadth_pct"),
        "candidate_breadth_ready": bool(side_detail.get("breadth_ready")),
        "candidate_breadth_sample_count": int(side_detail.get("breadth_sample_count") or 0),
        "candidate_breadth_source": str(side_detail.get("breadth_source") or ""),
        "candidate_valid_quote_ratio": side_detail.get("valid_quote_ratio"),
        "kospi_breadth_pct": market.kospi_breadth_pct,
        "kosdaq_breadth_pct": market.kosdaq_breadth_pct,
        "kospi_breadth_ready": market.kospi_breadth_ready,
        "kosdaq_breadth_ready": market.kosdaq_breadth_ready,
        "kospi_breadth_sample_count": market.kospi_breadth_sample_count,
        "kosdaq_breadth_sample_count": market.kosdaq_breadth_sample_count,
        "kospi_valid_quote_ratio": market.kospi_valid_quote_ratio,
        "kosdaq_valid_quote_ratio": market.kosdaq_valid_quote_ratio,
        "market_side_reason_codes": reason_codes,
        "market_side_data_quality_flags": data_quality_flags,
    }


def _candidate_market_wait_reasons(
    market: MarketStrengthSnapshot,
    watch: WatchSetSnapshot,
    config: MarketSideGateConfig,
) -> tuple[str, ...]:
    if not config.enabled:
        return ()
    side = normalize_market_side(watch.candidate_market)
    if side in {MarketSide.KOSPI, MarketSide.KOSDAQ}:
        status = market.status_for_side(side)
        side_reasons = tuple((market.side_statuses.get(side.value) or {}).get("reason_codes") or ())
        if status == MarketStatus.RISK_OFF:
            if str(config.risk_off_action).lower() != "temporary_wait":
                return ()
            return _dedupe_tuple(("CANDIDATE_MARKET_RISK_OFF", f"{side.value}_MARKET_RISK_OFF", "WAIT_MARKET_RECOVERY") + side_reasons)
        if status == MarketStatus.WEAK:
            if str(config.weak_action).lower() != "temporary_wait":
                return ()
            return _dedupe_tuple(("CANDIDATE_MARKET_WEAK", f"{side.value}_MARKET_WEAK", "WAIT_MARKET_RECOVERY") + side_reasons)
        return ()

    kospi_status = market.status_for_side(MarketSide.KOSPI)
    kosdaq_status = market.status_for_side(MarketSide.KOSDAQ)
    weak_or_risk = {MarketStatus.WEAK, MarketStatus.RISK_OFF}
    if kospi_status in weak_or_risk or kosdaq_status in weak_or_risk:
        if str(config.unknown_market_action).lower() != "strict_fallback":
            return ()
        return ("MARKET_CLASSIFICATION_MISSING", "MARKET_CLASSIFICATION_FALLBACK_STRICT", "WAIT_MARKET_RECOVERY")
    return ()


def _risk_input(
    market: MarketStrengthSnapshot,
    theme: ThemeConditionSnapshot,
    watch: WatchSetSnapshot,
    snapshot: StockSnapshot | None,
    data_quality_flags: Iterable[str],
) -> TradeabilityRiskInput:
    metadata = dict(snapshot.metadata if snapshot else {})
    return TradeabilityRiskInput(
        market_status=market.market_status,
        theme_status=theme.theme_status,
        stock_role=watch.stock_role,
        return_pct=watch.return_pct,
        condition_level=watch.condition_level,
        vi_active=_metadata_bool(metadata, "vi_active"),
        seconds_since_vi_release=_metadata_int(metadata, "seconds_since_vi_release", default=0),
        upper_limit_gap_pct=_metadata_float(metadata, "upper_limit_gap_pct", default=100.0),
        pullback_from_high_pct=_metadata_float(
            metadata,
            "pullback_from_high_pct",
            default=_pullback_from_high_pct(snapshot) if snapshot else 100.0,
        ),
        momentum_1m=float(snapshot.momentum_1m if snapshot else 0.0),
        momentum_3m=float(snapshot.momentum_3m if snapshot else 0.0),
        momentum_5m=float(snapshot.momentum_5m if snapshot else 0.0),
        turnover_krw=watch.turnover_krw or _turnover(snapshot),
        trade_strength=float(snapshot.execution_strength if snapshot else 0.0),
        leader_momentum_status=str(metadata.get("leader_momentum_status") or ""),
        theme_breadth_trend=str(metadata.get("theme_breadth_trend") or ""),
        data_quality_flags=tuple(data_quality_flags),
    )


def _price_location_input(
    market: MarketStrengthSnapshot,
    theme: ThemeConditionSnapshot,
    watch: WatchSetSnapshot,
    snapshot: StockSnapshot | None,
) -> PriceLocationInput:
    metadata = dict(snapshot.metadata if snapshot else {})
    return PriceLocationInput(
        symbol=watch.symbol,
        current_price=_positive_float_or_none(snapshot.current_price if snapshot else None),
        prev_close=_prev_close(snapshot),
        open_price=_metadata_float_or_none(metadata, "open_price"),
        session_high=_positive_float_or_none(snapshot.session_high if snapshot else None),
        session_low=_positive_float_or_none(snapshot.session_low if snapshot else None),
        vwap=_metadata_float_or_none(metadata, "vwap"),
        upper_limit_price=_metadata_float_or_none(metadata, "upper_limit_price"),
        breakout_level=_metadata_float_or_none(metadata, "breakout_level"),
        recent_support_price=_metadata_float_or_none(metadata, "recent_support_price"),
        return_pct=watch.return_pct,
        turnover_krw=watch.turnover_krw or _turnover(snapshot),
        trade_strength=float(snapshot.execution_strength if snapshot else 0.0) if snapshot else None,
        momentum_1m=float(snapshot.momentum_1m) if snapshot and snapshot.momentum_1m is not None else None,
        momentum_3m=float(snapshot.momentum_3m) if snapshot and snapshot.momentum_3m is not None else None,
        momentum_5m=float(snapshot.momentum_5m) if snapshot and snapshot.momentum_5m is not None else None,
        recent_candles_1m=tuple(metadata.get("recent_candles_1m") or ()),
        recent_candles_3m=tuple(metadata.get("recent_candles_3m") or ()),
        stock_role=watch.stock_role,
        theme_status=theme.theme_status,
        market_status=market.market_status,
        data_quality_flags=theme.data_quality_flags,
    )


def _has_hard_data_quality(flags: Iterable[str]) -> bool:
    return bool({"STALE_QUOTE", "MISSING_PREV_CLOSE", "MISSING_CURRENT_PRICE"} & set(flags))


def _leader_like(role: StockRole) -> bool:
    return role in {StockRole.LEADER, StockRole.CO_LEADER}


def _turnover_maintained(risk_input: TradeabilityRiskInput) -> bool:
    trend = str(risk_input.theme_breadth_trend or "").upper()
    return risk_input.turnover_krw > 0 and trend not in {"COLLAPSING", "WEAKENING"}


def _trade_flow_maintained(risk_input: TradeabilityRiskInput) -> bool:
    return _turnover_maintained(risk_input) and risk_input.trade_strength >= 100.0


def _risk_adjusted_for_price_location(
    risk: TradeabilityRiskResult,
    role: StockRole,
    price_location: PriceLocationResult,
    config: PositionAdjustmentConfig,
) -> TradeabilityRiskResult:
    if risk.risk_level != TradeabilityRiskLevel.PASS:
        return risk
    if role == StockRole.LEADER and price_location.status == PriceLocationStatus.BREAKOUT_CONTINUATION:
        return TradeabilityRiskResult(
            TradeabilityRiskLevel.RISK_ADJUST,
            ("BREAKOUT_CONTINUATION_READY_SMALL",),
            config.breakout_continuation_leader_multiplier,
            0,
        )
    if role == StockRole.CO_LEADER and price_location.status == PriceLocationStatus.BREAKOUT_CONTINUATION:
        return TradeabilityRiskResult(
            TradeabilityRiskLevel.RISK_ADJUST,
            ("BREAKOUT_CONTINUATION_READY_SMALL",),
            config.ready_small_multiplier_co_leader,
            0,
        )
    if role == StockRole.LEADER and price_location.status == PriceLocationStatus.CHASE_HIGH:
        return TradeabilityRiskResult(
            TradeabilityRiskLevel.RISK_ADJUST,
            ("CHASE_HIGH_READY_SMALL",),
            config.chase_high_leader_multiplier,
            PriceLocationConfig().chase_high_recheck_sec,
        )
    if role == StockRole.LEADER and price_location.status == PriceLocationStatus.VWAP_OVEREXTENDED:
        return TradeabilityRiskResult(
            TradeabilityRiskLevel.RISK_ADJUST,
            ("VWAP_OVEREXTENDED_READY_SMALL",),
            config.vwap_overextended_leader_multiplier,
            PriceLocationConfig().chase_high_recheck_sec,
        )
    return risk


def _theme_market_role_ready(market: MarketStrengthSnapshot, theme: ThemeConditionSnapshot, role: StockRole) -> bool:
    return (
        market.market_status in {MarketStatus.EXPANSION, MarketStatus.SELECTIVE, MarketStatus.CHOPPY}
        and theme.theme_status in {ThemeLabThemeStatus.LEADING_THEME, ThemeLabThemeStatus.SPREADING_THEME}
        and role in {StockRole.LEADER, StockRole.CO_LEADER, StockRole.FOLLOWER}
        and theme.strong_ratio >= 0.25
        and theme.leader_ratio >= 0.05
    )


def _price_location_hard_blocks(role: StockRole, price_location: PriceLocationResult) -> bool:
    if role == StockRole.FOLLOWER and price_location.status in {
        PriceLocationStatus.FAILED_BREAKOUT,
        PriceLocationStatus.VWAP_OVEREXTENDED,
    }:
        return True
    if price_location.status == PriceLocationStatus.FAILED_BREAKOUT and "FAILED_BREAKOUT" in price_location.reason_codes:
        return role == StockRole.FOLLOWER
    return False


def _price_location_recheck_sec(price_location: PriceLocationResult) -> int:
    cfg = PriceLocationConfig()
    if price_location.status == PriceLocationStatus.FAILED_BREAKOUT:
        return cfg.failed_breakout_recheck_sec
    if price_location.status in {PriceLocationStatus.CHASE_HIGH, PriceLocationStatus.VWAP_OVEREXTENDED}:
        return cfg.chase_high_recheck_sec
    if price_location.status == PriceLocationStatus.UNKNOWN:
        return cfg.unknown_price_location_recheck_sec
    return 0


def _pct_gap(
    base: float | None,
    value: float | None,
    missing_flag: str,
    flags: list[str],
    *,
    subtract_from_base: bool = False,
    reverse_distance: bool = False,
) -> float | None:
    base_value = _positive_float_or_none(base)
    value_value = _positive_float_or_none(value)
    if base_value is None or value_value is None:
        _add_flag(flags, missing_flag)
        return None
    if subtract_from_base:
        return ((base_value - value_value) / base_value) * 100.0
    if reverse_distance:
        return ((value_value - base_value) / base_value) * 100.0
    return ((value_value - base_value) / base_value) * 100.0


def _max_vwap_gap(config: PriceLocationConfig, role: StockRole | None) -> float:
    if role == StockRole.LEADER:
        return config.max_vwap_gap_pct_leader
    if role == StockRole.CO_LEADER:
        return config.max_vwap_gap_pct_co_leader
    return config.max_vwap_gap_pct_follower


def _price_location_score(status: PriceLocationStatus, role: StockRole | None) -> float:
    leader = role in {StockRole.LEADER, StockRole.CO_LEADER}
    if status == PriceLocationStatus.GOOD_PULLBACK:
        return 90.0
    if status == PriceLocationStatus.PULLBACK_RECLAIM:
        return 85.0
    if status == PriceLocationStatus.VWAP_RECLAIM:
        return 80.0
    if status == PriceLocationStatus.BREAKOUT_CONTINUATION:
        return 75.0 if leader else 50.0
    if status == PriceLocationStatus.CHASE_HIGH:
        return 60.0 if leader else 35.0
    if status == PriceLocationStatus.FAILED_BREAKOUT:
        return 25.0
    if status == PriceLocationStatus.DEEP_PULLBACK:
        return 35.0
    if status == PriceLocationStatus.VWAP_OVEREXTENDED:
        return 55.0 if leader else 25.0
    return 40.0


def _has_core_price_missing(flags: Iterable[str]) -> bool:
    return bool({"MISSING_CURRENT_PRICE", "MISSING_RETURN_PCT", "MISSING_STOCK_ROLE", "MISSING_THEME_STATUS", "MISSING_MARKET_STATUS"} & set(flags))


def _negative_or_missing(value: float | None) -> bool:
    return value is None or value < 0


def _positive(*values: float | None) -> bool:
    return any(value is not None and value > 0 for value in values)


def _round_optional(value: float | None) -> float | None:
    return round(float(value), 4) if value is not None else None


def _candle_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {
        "open": getattr(value, "open", None),
        "high": getattr(value, "high", None),
        "low": getattr(value, "low", None),
        "close": getattr(value, "close", None),
    }


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float_or_none(value: Any) -> float | None:
    number = _float_or_none(value)
    return number if number is not None and number > 0 else None


def _metadata_float_or_none(metadata: dict[str, Any], key: str) -> float | None:
    return _positive_float_or_none(metadata.get(key))


def _metadata_bool(metadata: dict[str, Any], key: str) -> bool:
    value = metadata.get(key)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _metadata_int(metadata: dict[str, Any], key: str, *, default: int = 0) -> int:
    try:
        return int(metadata.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _metadata_float(metadata: dict[str, Any], key: str, *, default: float = 0.0) -> float:
    try:
        return float(metadata.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _return_pct(
    *,
    current_price: float | None,
    prev_close: float | None,
    change_rate_pct: float | None,
) -> float | None:
    if prev_close and current_price:
        return ((float(current_price) - float(prev_close)) / float(prev_close)) * 100.0
    if change_rate_pct is not None:
        return float(change_rate_pct)
    return None


def _prev_close(snapshot: StockSnapshot | None) -> float | None:
    if snapshot is None:
        return None
    for key in ("prev_close", "previous_close", "yesterday_close"):
        value = (snapshot.metadata or {}).get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def _snapshot_map(snapshots: dict[str, StockSnapshot] | list[StockSnapshot]) -> dict[str, StockSnapshot]:
    result: dict[str, StockSnapshot] = {}
    items = snapshots.items() if isinstance(snapshots, dict) else [(snapshot.stock_code, snapshot) for snapshot in snapshots]
    for key, snapshot in items:
        normalized_key = normalize_stock_code(str(key))
        normalized_code = normalize_stock_code(snapshot.stock_code)
        if normalized_key:
            result[normalized_key] = snapshot
        if normalized_code:
            result[normalized_code] = snapshot
    return result


def _metadata_map(values: dict[str, InstrumentMetadata]) -> dict[str, InstrumentMetadata]:
    result = {}
    for key, metadata in values.items():
        normalized_key = normalize_stock_code(str(key))
        normalized_symbol = normalize_stock_code(metadata.symbol)
        if normalized_key:
            result[normalized_key] = metadata
        if normalized_symbol:
            result[normalized_symbol] = metadata
    return result


def _metadata_from_snapshot(snapshot: StockSnapshot | None) -> InstrumentMetadata:
    metadata = dict(snapshot.metadata if snapshot else {})
    return InstrumentMetadata(
        symbol=snapshot.stock_code if snapshot else "",
        name=(snapshot.stock_name if snapshot else "") or str(metadata.get("stock_name") or metadata.get("name") or ""),
        instrument_type=str(metadata.get("instrument_type") or metadata.get("security_type") or ""),
        is_etf=bool(metadata.get("is_etf")),
        is_etn=bool(metadata.get("is_etn")),
        is_preferred=bool(metadata.get("is_preferred")),
        is_suspended=bool(metadata.get("is_suspended") or metadata.get("trading_suspended")),
        is_under_administration=bool(metadata.get("is_under_administration") or metadata.get("liquidation")),
        avg_turnover_20d_krw=float(metadata.get("avg_turnover_20d_krw") or 0.0),
        today_turnover_krw=float(metadata.get("today_turnover_krw") or (snapshot.turnover if snapshot else 0.0) or 0.0),
        recent_volume=int(metadata.get("recent_volume") or (snapshot.volume if snapshot else 0) or 0),
        raw=metadata,
    )


def _looks_like_preferred(name: str) -> bool:
    text = str(name or "").strip()
    return text.endswith("우") or "우선주" in text or text.endswith("우B") or text.endswith("우C")


def _turnover(snapshot: StockSnapshot | None) -> float:
    return max(0.0, float(snapshot.turnover if snapshot else 0.0))


def _pullback_from_high_pct(snapshot: StockSnapshot) -> float:
    high = float(snapshot.session_high or 0.0)
    price = float(snapshot.current_price or 0.0)
    if high <= 0 or price <= 0:
        return 100.0
    return max(0.0, ((high - price) / high) * 100.0)


def _add_flag(flags: list[str], flag: str) -> None:
    if flag and flag not in flags:
        flags.append(flag)


def _quality_summary(themes: Iterable[ThemeConditionSnapshot]) -> dict[str, int]:
    summary = {
        "missing_prev_close_count": 0,
        "missing_current_price_count": 0,
        "stale_quote_count": 0,
        "excluded_count": 0,
        "empty_eligible_theme_count": 0,
    }
    for theme in themes:
        if "EMPTY_ELIGIBLE_THEME" in theme.data_quality_flags:
            summary["empty_eligible_theme_count"] += 1
        for hit in theme.member_hits:
            if hit.excluded:
                summary["excluded_count"] += 1
            if "MISSING_PREV_CLOSE" in hit.data_quality_flags:
                summary["missing_prev_close_count"] += 1
            if "MISSING_CURRENT_PRICE" in hit.data_quality_flags:
                summary["missing_current_price_count"] += 1
            if "STALE_QUOTE" in hit.data_quality_flags:
                summary["stale_quote_count"] += 1
    return summary


def _market_classification_summary(
    watchset: Iterable[WatchSetSnapshot],
    decisions: Iterable[LabGateDecision],
) -> dict[str, int | float]:
    watch_items = list(watchset)
    decision_items = list(decisions)
    total = len(watch_items)
    unknown = sum(1 for item in watch_items if normalize_market_side(item.candidate_market) == MarketSide.UNKNOWN)
    unknown_wait = sum(
        1
        for item in decision_items
        if normalize_market_side(item.candidate_market) == MarketSide.UNKNOWN and item.status == LabGateStatus.WAIT
    )
    ready_possible = sum(
        1
        for item in decision_items
        if normalize_market_side(item.candidate_market) == MarketSide.UNKNOWN
        and item.status in {LabGateStatus.READY, LabGateStatus.READY_SMALL, LabGateStatus.OBSERVE}
    )
    return {
        "market_classification_total_count": total,
        "market_classification_unknown_count": unknown,
        "market_classification_unknown_ratio": round(unknown / total, 4) if total else 0.0,
        "unknown_market_wait_count": unknown_wait,
        "unknown_market_ready_possible_count": ready_possible,
    }
