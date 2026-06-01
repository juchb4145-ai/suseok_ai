from __future__ import annotations

from dataclasses import dataclass, field
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
    return_pct: float = 0.0
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
                return_pct=return_pct,
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
            return_pct=round(return_pct, 4),
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
            change_rate = snapshot.change_rate if snapshot and not current_price else None
            hit = self.classifier.classify(
                symbol=symbol,
                current_price=current_price,
                prev_close=prev_close,
                change_rate_pct=change_rate,
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


class MarketStrengthEngine:
    def __init__(self, config: MarketStatusThresholds | None = None) -> None:
        self.config = config or MarketStatusThresholds()

    def calculate(
        self,
        snapshots: dict[str, StockSnapshot] | list[StockSnapshot],
        *,
        kospi_return_pct: float = 0.0,
        kosdaq_return_pct: float = 0.0,
    ) -> MarketStrengthSnapshot:
        values = list(_snapshot_map(snapshots).values())
        unique = {normalize_stock_code(item.stock_code): item for item in values if normalize_stock_code(item.stock_code)}
        stock_values = list(unique.values())
        advancers = sum(1 for item in stock_values if item.change_rate > 0)
        decliners = sum(1 for item in stock_values if item.change_rate < 0)
        strong_count = sum(1 for item in stock_values if item.change_rate >= 3.0)
        leader_count = sum(1 for item in stock_values if item.change_rate >= 5.0)
        turnover = sum(max(0.0, item.turnover) for item in stock_values)
        status = self._status(kospi_return_pct, kosdaq_return_pct, strong_count, leader_count, advancers, decliners)
        return MarketStrengthSnapshot(
            market_status=status,
            kospi_return_pct=round(kospi_return_pct, 4),
            kosdaq_return_pct=round(kosdaq_return_pct, 4),
            advancers=advancers,
            decliners=decliners,
            market_strong_count=strong_count,
            market_leader_count=leader_count,
            market_turnover_krw=round(turnover, 4),
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


class WatchSetManager:
    def __init__(self, limits: WatchSetLimits | None = None) -> None:
        self.limits = limits or WatchSetLimits()

    def build(
        self,
        ranked_themes: Iterable[ThemeConditionSnapshot],
        snapshots: dict[str, StockSnapshot] | list[StockSnapshot],
        *,
        calculated_at: str = "",
    ) -> list[WatchSetSnapshot]:
        snapshot_by_symbol = _snapshot_map(snapshots)
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
    ) -> None:
        self.risk_filter = risk_filter or TradeabilityRiskFilter()
        self.position_config = position_config or PositionAdjustmentConfig()

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
        if risk.risk_level == TradeabilityRiskLevel.HARD_BLOCK:
            return LabGateDecision(
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
        if market.market_status == MarketStatus.RISK_OFF:
            return LabGateDecision(
                watch.symbol,
                LabGateStatus.BLOCKED,
                ("RISK_OFF",),
                "RISK_OFF",
                risk.risk_level,
                risk.reason_codes,
                0.0,
                0,
                price_location.status,
                price_location.score,
                price_location.reason_codes,
            )
        if theme.theme_status == ThemeLabThemeStatus.LEADER_ONLY_THEME and role not in {StockRole.LEADER, StockRole.CO_LEADER}:
            return LabGateDecision(
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
        if market.market_status == MarketStatus.WEAK:
            return LabGateDecision(
                watch.symbol,
                LabGateStatus.OBSERVE,
                ("WEAK_MARKET_OBSERVE",),
                "",
                risk.risk_level,
                risk.reason_codes,
                risk.position_size_multiplier,
                risk.recheck_after_sec,
                price_location.status,
                price_location.score,
                price_location.reason_codes,
            )
        if risk.risk_level == TradeabilityRiskLevel.SOFT_BLOCK:
            return LabGateDecision(
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
            )
        if _price_location_hard_blocks(role, price_location):
            return LabGateDecision(
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
                return LabGateDecision(
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
            if risk.risk_level == TradeabilityRiskLevel.RISK_ADJUST:
                return LabGateDecision(
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
            return LabGateDecision(
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
        if base_ready and _leader_like(role) and ready_small_location and risk.risk_level == TradeabilityRiskLevel.RISK_ADJUST:
            return LabGateDecision(
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
        if watch.condition_level >= 2:
            return LabGateDecision(
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
        return LabGateDecision(
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


class ThemeLabFlowEngine:
    def __init__(self, config: ThemeLabConfig | None = None) -> None:
        self.config = config or ThemeLabConfig()
        self.market_engine = MarketStrengthEngine(self.config.market_status)
        self.breadth_engine = ThemeBreadthEngine(self.config)
        self.ranker = ThemeLabRanker()
        self.watchset_manager = WatchSetManager(self.config.watchset_limits)
        self.role_detector = StockRoleDetector()
        self.price_location_evaluator = PriceLocationEvaluator(self.config.price_location)
        self.gate = ThemeLabHybridGate(
            TradeabilityRiskFilter(self.config.tradeability_risk),
            self.config.position_adjustment,
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
        market = self.market_engine.calculate(snapshots, kospi_return_pct=kospi_return_pct, kosdaq_return_pct=kosdaq_return_pct)
        themes = self.breadth_engine.calculate(
            theme_inputs,
            snapshots,
            metadata_by_symbol,
            calculated_at=calculated_at,
        )
        ranked_themes = self.ranker.rank(themes, top_n=self.config.watchset_limits.top_theme_count)
        watchset = self.watchset_manager.build(ranked_themes, snapshots, calculated_at=calculated_at)
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
                }
            )
            enriched_watchset.append(enriched)
            decisions.append(decision)
        return ThemeLabFlowResult(
            market=market,
            themes=tuple(ranked_themes),
            watchset=tuple(enriched_watchset),
            gate_decisions=tuple(decisions),
            data_quality=_quality_summary(ranked_themes),
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
