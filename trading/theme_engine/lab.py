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
    WAIT = "WAIT"
    BLOCKED = "BLOCKED"
    OBSERVE = "OBSERVE"


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
class ThemeLabConfig:
    conditions: ThemeLabConditionConfig = field(default_factory=ThemeLabConditionConfig)
    score_weights: ThemeConditionScoreWeights = field(default_factory=ThemeConditionScoreWeights)
    theme_status: ThemeStatusThresholds = field(default_factory=ThemeStatusThresholds)
    watchset_limits: WatchSetLimits = field(default_factory=WatchSetLimits)
    liquidity_filter: LiquidityFilterConfig = field(default_factory=LiquidityFilterConfig)
    market_status: MarketStatusThresholds = field(default_factory=MarketStatusThresholds)


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


@dataclass(frozen=True)
class LabGateDecision:
    symbol: str
    status: LabGateStatus
    reason_codes: tuple[str, ...] = ()
    blocked_reason: str = ""


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
                    if _turnover(snapshot_by_symbol.get(hit.symbol)) > 0
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
        snapshot = snapshot_by_symbol.get(watch.symbol)
        if watch.condition_level <= 0:
            return StockRole.WEAK_MEMBER
        if snapshot and _pullback_from_high_pct(snapshot) <= 0.3 and watch.return_pct >= 12.0:
            return StockRole.OVERHEATED
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


class ThemeLabHybridGate:
    def evaluate(
        self,
        *,
        market: MarketStrengthSnapshot,
        theme: ThemeConditionSnapshot,
        watch: WatchSetSnapshot,
        data_quality_flags: Iterable[str] = (),
    ) -> LabGateDecision:
        flags = set(data_quality_flags) | set(theme.data_quality_flags)
        role = watch.stock_role
        if "STALE_QUOTE" in flags or "MISSING_PREV_CLOSE" in flags or "MISSING_CURRENT_PRICE" in flags:
            return LabGateDecision(watch.symbol, LabGateStatus.BLOCKED, ("DATA_QUALITY_BLOCK",), "DATA_QUALITY_BLOCK")
        if market.market_status == MarketStatus.RISK_OFF:
            return LabGateDecision(watch.symbol, LabGateStatus.BLOCKED, ("RISK_OFF",), "RISK_OFF")
        if role in {StockRole.LATE_LAGGARD, StockRole.OVERHEATED}:
            return LabGateDecision(watch.symbol, LabGateStatus.BLOCKED, (role.value,), role.value)
        if theme.theme_status == ThemeLabThemeStatus.LEADER_ONLY_THEME and role not in {StockRole.LEADER, StockRole.CO_LEADER}:
            return LabGateDecision(watch.symbol, LabGateStatus.BLOCKED, ("LEADER_ONLY_THEME_LAGGARD_BLOCK",), "LEADER_ONLY_THEME_LAGGARD_BLOCK")
        if market.market_status == MarketStatus.WEAK:
            return LabGateDecision(watch.symbol, LabGateStatus.OBSERVE, ("WEAK_MARKET_OBSERVE",))
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
                return LabGateDecision(watch.symbol, LabGateStatus.READY, ("THEME_LAB_READY",))
        if watch.condition_level >= 2:
            return LabGateDecision(watch.symbol, LabGateStatus.WAIT, ("WATCHSET_WAIT_CONFIRMATION",))
        return LabGateDecision(watch.symbol, LabGateStatus.OBSERVE, ("THEME_LAB_OBSERVE",))


class ThemeLabFlowEngine:
    def __init__(self, config: ThemeLabConfig | None = None) -> None:
        self.config = config or ThemeLabConfig()
        self.market_engine = MarketStrengthEngine(self.config.market_status)
        self.breadth_engine = ThemeBreadthEngine(self.config)
        self.ranker = ThemeLabRanker()
        self.watchset_manager = WatchSetManager(self.config.watchset_limits)
        self.role_detector = StockRoleDetector()
        self.gate = ThemeLabHybridGate()

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
            decision = self.gate.evaluate(market=market, theme=theme, watch=enriched)
            enriched = WatchSetSnapshot(**{**enriched.__dict__, "gate_status": decision.status})
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
