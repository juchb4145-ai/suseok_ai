from __future__ import annotations

import os
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping

from trading.strategy.candidates import normalize_code
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.models import Candidate, CandidateState
from trading.theme_engine.leadership import StockLeadershipRole, ThemeLeadershipStatus
from trading.theme_engine.repository import ThemeEngineRepository


THEME_BOARD_OUTPUT_MODE = "OBSERVE"


@dataclass(frozen=True)
class ThemeBoardConfig:
    enabled: bool = False
    observe_only: bool = True
    interval_sec: int = 5
    top_theme_count: int = 5
    max_stocks_per_theme: int = 12
    min_realtime_valid_ratio: float = 0.35
    leading_score_threshold: float = 70.0
    spreading_score_threshold: float = 55.0
    watch_score_threshold: float = 30.0
    leader_min_score: float = 45.0
    co_leader_min_score: float = 52.0
    leader_only_concentration_min: float = 0.65
    leader_only_breadth_max: float = 0.45
    overheat_return_threshold_pct: float = 12.0
    overheat_upper_limit_gap_pct: float = 2.0
    overheat_vwap_gap_pct: float = 8.0
    overheat_pullback_from_high_pct: float = 0.5

    @classmethod
    def from_env(cls) -> "ThemeBoardConfig":
        return cls(
            enabled=_env_bool("TRADING_THEME_BOARD_ENABLED", False),
            observe_only=_env_bool("TRADING_THEME_BOARD_OBSERVE_ONLY", True),
            interval_sec=max(1, _env_int("TRADING_THEME_BOARD_INTERVAL_SEC", 5)),
        )


@dataclass(frozen=True)
class ThemeBoardStockSnapshot:
    code: str
    name: str = ""
    theme_id: str = ""
    theme_name: str = ""
    stock_role: str = StockLeadershipRole.WEAK_MEMBER.value
    stock_score: float = 0.0
    source_types: tuple[str, ...] = ()
    source_score: float = 0.0
    opening_burst_score: float = 0.0
    condition_boost: float = 0.0
    current_price: float = 0.0
    change_rate_pct: float = 0.0
    turnover_krw: float = 0.0
    execution_strength: float = 0.0
    momentum_1m: float = 0.0
    momentum_3m: float = 0.0
    momentum_5m: float = 0.0
    vwap: float = 0.0
    pullback_from_high_pct: float = 100.0
    spread_ticks: int = 0
    vi_active: bool = False
    upper_limit_gap_pct: float = 100.0
    entry_usable: bool = False
    data_quality_flags: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    candidate_id: int | None = None
    realtime_valid: bool = False
    hydration_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = _jsonable(asdict(self))
        data.pop("candidate_id", None)
        data.pop("realtime_valid", None)
        data.pop("hydration_available", None)
        return data


@dataclass(frozen=True)
class ThemeBoardThemeSnapshot:
    theme_id: str
    theme_name: str = ""
    theme_rank: int = 0
    theme_status: str = ThemeLeadershipStatus.DATA_WAIT.value
    theme_score: float = 0.0
    active_candidate_count: int = 0
    watching_candidate_count: int = 0
    data_wait_count: int = 0
    alive_count: int = 0
    strong_count: int = 0
    leader_count: int = 0
    alive_ratio: float = 0.0
    strong_ratio: float = 0.0
    leader_ratio: float = 0.0
    breadth_ratio: float = 0.0
    weighted_return_pct: float = 0.0
    theme_turnover_krw: float = 0.0
    leader_concentration: float = 0.0
    leader_symbol: str = ""
    leader_name: str = ""
    co_leader_symbols: tuple[str, ...] = ()
    opening_burst_score: float = 0.0
    condition_boost_count: int = 0
    realtime_valid_count: int = 0
    realtime_valid_ratio: float = 0.0
    hydration_coverage_ratio: float = 0.0
    data_quality_flags: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    stocks: tuple[ThemeBoardStockSnapshot, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class ThemeBoardSnapshot:
    trade_date: str
    calculated_at: str
    board_status: str = THEME_BOARD_OUTPUT_MODE
    theme_count: int = 0
    active_theme_count: int = 0
    watch_theme_count: int = 0
    data_wait_theme_count: int = 0
    top_themes: tuple[ThemeBoardThemeSnapshot, ...] = ()
    stocks: tuple[ThemeBoardStockSnapshot, ...] = ()
    source_counts: dict[str, int] = field(default_factory=dict)
    data_quality_flags: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    output_mode: str = THEME_BOARD_OUTPUT_MODE
    ready_allowed: bool = False
    order_intent_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class ThemeBoardResult:
    snapshot: ThemeBoardSnapshot
    updated_candidate_count: int = 0
    saved: bool = False
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


class ThemeBoardEngine:
    def __init__(
        self,
        db: Any,
        *,
        market_data: MarketDataStore | None = None,
        repository: ThemeEngineRepository | None = None,
        candle_builder: CandleBuilder | None = None,
        config: ThemeBoardConfig | None = None,
        clock=None,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.repository = repository or ThemeEngineRepository(db)
        self.candle_builder = candle_builder
        self.config = config or ThemeBoardConfig()
        self.clock = clock or datetime.now

    def build(
        self,
        *,
        trade_date: str | None = None,
        now: datetime | None = None,
        save: bool = True,
    ) -> ThemeBoardResult:
        current = (now or self.clock()).replace(microsecond=0)
        trade_date = trade_date or current.date().isoformat()
        candidates = [
            candidate
            for candidate in list(self.db.list_candidates(trade_date=trade_date) or [])
            if candidate.state in {CandidateState.WATCHING, CandidateState.WAIT_DATA}
        ]
        source_counts: Counter[str] = Counter()
        stock_groups: dict[str, list[tuple[Candidate, dict[str, Any]]]] = defaultdict(list)
        board_flags: list[str] = []
        board_reasons: list[str] = []
        for candidate in candidates:
            context = self._candidate_theme_context(candidate)
            source_types = _candidate_source_types(candidate)
            source_counts.update(source_types)
            if not context["theme_id"]:
                board_flags.append("THEME_UNMAPPED")
                board_reasons.append("theme_unmapped")
                continue
            context["source_types"] = source_types
            stock_groups[context["theme_id"]].append((candidate, context))

        stock_snapshots: list[ThemeBoardStockSnapshot] = []
        theme_metrics: list[dict[str, Any]] = []
        for theme_id, items in stock_groups.items():
            prelim = [self._stock_prelim(candidate, context, current) for candidate, context in items]
            scored = self._score_theme_stocks(prelim)
            stock_snapshots.extend(scored)
            theme_metrics.append(self._theme_metrics(theme_id, scored, items))

        turnover_rank_scores = _rank_scores(
            {str(item["theme_id"]): float(item["theme_turnover_krw"] or 0.0) for item in theme_metrics},
            positive_only=True,
        )
        theme_snapshots = [
            self._score_theme(metric, turnover_rank_scores.get(str(metric["theme_id"]), 0.0))
            for metric in theme_metrics
        ]
        theme_snapshots = sorted(
            theme_snapshots,
            key=lambda item: (item.theme_score, item.theme_turnover_krw, item.strong_count),
            reverse=True,
        )
        ranked_themes: list[ThemeBoardThemeSnapshot] = []
        for index, theme in enumerate(theme_snapshots, 1):
            ranked_themes.append(replace(theme, theme_rank=index))
        ranked_theme_ids = {theme.theme_id: theme.theme_rank for theme in ranked_themes}
        stock_snapshots = sorted(
            (
                _with_theme_rank_reason(stock, ranked_theme_ids.get(stock.theme_id, 0))
                for stock in stock_snapshots
            ),
            key=lambda item: (ranked_theme_ids.get(item.theme_id, 9999), -item.stock_score, item.code),
        )
        status_counts = Counter(theme.theme_status for theme in ranked_themes)
        active_statuses = {ThemeLeadershipStatus.LEADING_THEME.value, ThemeLeadershipStatus.SPREADING_THEME.value, ThemeLeadershipStatus.LEADER_ONLY_THEME.value}
        if not ranked_themes:
            board_flags.append("THEME_BOARD_EMPTY")
            board_reasons.append("NO_THEME_CANDIDATES")
        snapshot = ThemeBoardSnapshot(
            trade_date=trade_date,
            calculated_at=current.isoformat(),
            board_status=THEME_BOARD_OUTPUT_MODE if ranked_themes else ThemeLeadershipStatus.DATA_WAIT.value,
            theme_count=len(ranked_themes),
            active_theme_count=sum(status_counts.get(status, 0) for status in active_statuses),
            watch_theme_count=status_counts.get(ThemeLeadershipStatus.WATCH_THEME.value, 0),
            data_wait_theme_count=status_counts.get(ThemeLeadershipStatus.DATA_WAIT.value, 0),
            top_themes=tuple(ranked_themes[: self.config.top_theme_count]),
            stocks=tuple(stock_snapshots),
            source_counts=dict(source_counts),
            data_quality_flags=tuple(_dedupe(board_flags)),
            reason_codes=tuple(_dedupe(board_reasons)),
            output_mode=THEME_BOARD_OUTPUT_MODE,
            ready_allowed=False,
            order_intent_allowed=False,
        )
        updated_count = self._merge_candidate_metadata(snapshot)
        saved = False
        if save:
            saver = getattr(self.db, "save_theme_board_snapshot", None)
            if callable(saver):
                saver(snapshot.to_dict())
                saved = True
        return ThemeBoardResult(snapshot=snapshot, updated_candidate_count=updated_count, saved=saved, warnings=())

    def _candidate_theme_context(self, candidate: Candidate) -> dict[str, Any]:
        metadata = dict(candidate.metadata or {})
        ingestion = dict(metadata.get("candidate_ingestion") or {})
        theme_id = str(
            metadata.get("theme_board_theme_id")
            or ingestion.get("primary_theme_id")
            or metadata.get("primary_theme_id")
            or metadata.get("best_theme_id")
            or (candidate.theme_ids[0] if candidate.theme_ids else "")
            or ""
        )
        theme_name = str(
            metadata.get("theme_board_theme_name")
            or ingestion.get("theme_name")
            or metadata.get("theme_name")
            or ""
        )
        membership_score = 0.0
        if not theme_id:
            memberships = self.repository.get_themes_by_stock(candidate.code)
            if memberships:
                membership = memberships[0]
                theme_id = str(membership.theme_id or "")
                theme_name = theme_name or _theme_display_name(self.repository, theme_id)
                membership_score = float(membership.membership_score or 0.0)
        else:
            membership_score = _membership_score(self.repository, theme_id, candidate.code)
            theme_name = theme_name or _theme_display_name(self.repository, theme_id)
        stock_role = str(ingestion.get("stock_role") or metadata.get("stock_role") or "")
        source_score = _float(metadata.get("source_score") or ingestion.get("score"))
        return {
            "theme_id": theme_id,
            "theme_name": theme_name or theme_id,
            "membership_score": membership_score,
            "stock_role_hint": stock_role,
            "source_score": source_score,
        }

    def _stock_prelim(self, candidate: Candidate, context: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        tick = self.market_data.latest_tick(candidate.code) if self.market_data is not None else None
        metadata = dict(candidate.metadata or {})
        ingestion = dict(metadata.get("candidate_ingestion") or {})
        hydration = dict(metadata.get("candidate_hydration") or {})
        parsed = dict(hydration.get("parsed") or {})
        tick_metadata = dict(getattr(tick, "metadata", {}) or {}) if tick is not None else {}
        source_types = tuple(str(source) for source in context.get("source_types", ()) if str(source))
        opening_burst_score = _opening_burst_score(ingestion)
        condition_boost = 4.0 if "condition_search" in source_types else 0.0
        price_source = str(tick_metadata.get("price_source") or "")
        realtime_valid = bool(tick is not None and getattr(tick, "price", 0) > 0 and price_source != "TR_BACKFILL")
        hydration_available = bool(parsed)
        current_price = _first_number(
            getattr(tick, "price", 0) if tick is not None else 0,
            parsed.get("current_price"),
            parsed.get("price"),
        )
        change_rate_pct = _first_number(
            getattr(tick, "change_rate", 0.0) if tick is not None else 0.0,
            parsed.get("change_rate"),
        )
        turnover_krw = _first_number(
            getattr(tick, "trade_value", 0.0) if tick is not None else 0.0,
            parsed.get("trade_value"),
            parsed.get("turnover"),
        )
        execution_strength = _first_number(getattr(tick, "execution_strength", 0.0) if tick is not None else 0.0)
        momentum_1m = _feature_number(tick_metadata, "momentum_1m", self._candle_momentum(candidate.code, 1))
        momentum_3m = _feature_number(tick_metadata, "momentum_3m", self._candle_momentum(candidate.code, 3))
        momentum_5m = _feature_number(tick_metadata, "momentum_5m", self._candle_momentum(candidate.code, 5))
        vwap = _feature_number(tick_metadata, "vwap", self._candle_vwap(candidate.code))
        day_high = _first_number(
            tick_metadata.get("session_high"),
            tick_metadata.get("day_high"),
            parsed.get("day_high"),
            parsed.get("high"),
            current_price,
        )
        pullback_from_high_pct = _pullback_pct(current_price, day_high)
        spread_ticks = int(_first_number(getattr(tick, "spread_ticks", 0) if tick is not None else 0, _spread_from_tick(tick)))
        vi_active = _bool(tick_metadata.get("vi_active"))
        upper_limit_gap_pct = _upper_limit_gap(current_price, tick_metadata)
        flags: list[str] = []
        reasons: list[str] = []
        if tick is None:
            flags.append("REALTIME_TICK_MISSING")
            reasons.append("WAIT_DATA_REALTIME_TICK")
        if price_source == "TR_BACKFILL" or (tick is not None and not realtime_valid):
            flags.append("TR_BACKFILL_PRICE_ONLY")
            reasons.append("WAIT_REALTIME_TICK")
        if turnover_krw <= 0:
            flags.append("TURNOVER_MISSING")
        if vwap <= 0:
            flags.append("VWAP_MISSING")
        if candidate.state == CandidateState.WAIT_DATA:
            flags.append("CANDIDATE_WAIT_DATA")
            reasons.append("WAIT_DATA")
        if not context.get("theme_id"):
            flags.append("THEME_UNMAPPED")
            reasons.append("theme_unmapped")
        return {
            "candidate": candidate,
            "code": normalize_code(candidate.code),
            "name": candidate.name or str(tick_metadata.get("stock_name") or parsed.get("stock_name") or ""),
            "theme_id": str(context.get("theme_id") or ""),
            "theme_name": str(context.get("theme_name") or ""),
            "source_types": source_types,
            "source_score": _float(context.get("source_score")),
            "opening_burst_score": opening_burst_score,
            "condition_boost": condition_boost,
            "current_price": current_price,
            "change_rate_pct": change_rate_pct,
            "turnover_krw": turnover_krw,
            "execution_strength": execution_strength,
            "momentum_1m": momentum_1m,
            "momentum_3m": momentum_3m,
            "momentum_5m": momentum_5m,
            "vwap": vwap,
            "pullback_from_high_pct": pullback_from_high_pct,
            "spread_ticks": spread_ticks,
            "vi_active": vi_active,
            "upper_limit_gap_pct": upper_limit_gap_pct,
            "realtime_valid": realtime_valid,
            "hydration_available": hydration_available,
            "membership_score": _float(context.get("membership_score")),
            "data_quality_flags": flags,
            "reason_codes": reasons,
        }

    def _score_theme_stocks(self, prelim: list[dict[str, Any]]) -> list[ThemeBoardStockSnapshot]:
        turnover_scores = _rank_scores({item["code"]: item["turnover_krw"] for item in prelim}, positive_only=True)
        scored: list[dict[str, Any]] = []
        for item in prelim:
            return_score = _scale(item["change_rate_pct"], -2.0, 10.0)
            execution_score = _scale(item["execution_strength"], 80.0, 180.0)
            momentum_score = _scale(_avg([item["momentum_1m"], item["momentum_3m"], item["momentum_5m"]]), -1.0, 4.0)
            source_score = _scale(item["source_score"], 0.0, 100.0)
            membership_score = _scale(item["membership_score"], 0.0, 100.0)
            stock_score = (
                0.30 * turnover_scores.get(item["code"], 0.0)
                + 0.25 * return_score
                + 0.15 * execution_score
                + 0.15 * momentum_score
                + 0.10 * source_score
                + 0.05 * membership_score
                + min(10.0, item["opening_burst_score"] / 10.0)
                + item["condition_boost"]
            )
            overheat = self._is_overheated(item)
            if overheat:
                stock_score -= 20.0
            item = dict(item)
            item["stock_score"] = round(max(0.0, stock_score), 4)
            item["overheated"] = overheat
            scored.append(item)
        ranked = sorted(scored, key=lambda item: (item["stock_score"], item["turnover_krw"]), reverse=True)
        valid_ranked = [item for item in ranked if not item["overheated"]]
        leader_code = valid_ranked[0]["code"] if valid_ranked else ""
        second_code = valid_ranked[1]["code"] if len(valid_ranked) > 1 else ""
        leader_score = valid_ranked[0]["stock_score"] if valid_ranked else 0.0
        result: list[ThemeBoardStockSnapshot] = []
        for item in ranked:
            role = StockLeadershipRole.WEAK_MEMBER.value
            if item["overheated"]:
                role = StockLeadershipRole.OVERHEATED.value
                item["data_quality_flags"].append("OVERHEATED")
                item["reason_codes"].append("OVERHEATED")
            elif item["code"] == leader_code and item["stock_score"] >= self.config.leader_min_score:
                role = StockLeadershipRole.LEADER.value
            elif (
                item["code"] == second_code
                and item["stock_score"] >= self.config.co_leader_min_score
                and leader_score - item["stock_score"] <= 12.0
            ):
                role = StockLeadershipRole.CO_LEADER.value
            elif _is_late_laggard(item, leader_score):
                role = StockLeadershipRole.LATE_LAGGARD.value
            elif item["stock_score"] >= 35.0:
                role = StockLeadershipRole.FOLLOWER.value
            result.append(
                ThemeBoardStockSnapshot(
                    code=item["code"],
                    name=item["name"],
                    theme_id=item["theme_id"],
                    theme_name=item["theme_name"],
                    stock_role=role,
                    stock_score=round(item["stock_score"], 4),
                    source_types=tuple(item["source_types"]),
                    source_score=round(float(item["source_score"] or 0.0), 4),
                    opening_burst_score=round(float(item["opening_burst_score"] or 0.0), 4),
                    condition_boost=round(float(item["condition_boost"] or 0.0), 4),
                    current_price=round(float(item["current_price"] or 0.0), 4),
                    change_rate_pct=round(float(item["change_rate_pct"] or 0.0), 4),
                    turnover_krw=round(float(item["turnover_krw"] or 0.0), 4),
                    execution_strength=round(float(item["execution_strength"] or 0.0), 4),
                    momentum_1m=round(float(item["momentum_1m"] or 0.0), 4),
                    momentum_3m=round(float(item["momentum_3m"] or 0.0), 4),
                    momentum_5m=round(float(item["momentum_5m"] or 0.0), 4),
                    vwap=round(float(item["vwap"] or 0.0), 4),
                    pullback_from_high_pct=round(float(item["pullback_from_high_pct"] or 0.0), 4),
                    spread_ticks=int(item["spread_ticks"] or 0),
                    vi_active=bool(item["vi_active"]),
                    upper_limit_gap_pct=round(float(item["upper_limit_gap_pct"] or 0.0), 4),
                    entry_usable=False,
                    data_quality_flags=tuple(_dedupe(item["data_quality_flags"])),
                    reason_codes=tuple(_dedupe(item["reason_codes"])),
                    candidate_id=item["candidate"].id,
                    realtime_valid=bool(item["realtime_valid"]),
                    hydration_available=bool(item["hydration_available"]),
                )
            )
        return result

    def _theme_metrics(
        self,
        theme_id: str,
        stocks: list[ThemeBoardStockSnapshot],
        items: list[tuple[Candidate, Mapping[str, Any]]],
    ) -> dict[str, Any]:
        total = len(stocks)
        total_turnover = sum(stock.turnover_krw for stock in stocks)
        weighted_return = (
            sum(stock.change_rate_pct * max(stock.turnover_krw, 0.0) for stock in stocks) / total_turnover
            if total_turnover > 0
            else _avg([stock.change_rate_pct for stock in stocks])
        )
        leader = next((stock for stock in stocks if stock.stock_role == StockLeadershipRole.LEADER.value), None)
        co_leaders = tuple(stock.code for stock in stocks if stock.stock_role == StockLeadershipRole.CO_LEADER.value)
        data_wait_count = sum(1 for stock in stocks if "REALTIME_TICK_MISSING" in stock.data_quality_flags or "TR_BACKFILL_PRICE_ONLY" in stock.data_quality_flags)
        realtime_valid_count = sum(1 for stock in stocks if stock.realtime_valid)
        hydration_count = sum(1 for stock in stocks if stock.hydration_available)
        condition_boost_count = sum(1 for stock in stocks if stock.condition_boost > 0)
        leader_turnover = sum(stock.turnover_krw for stock in stocks if stock.stock_role in {StockLeadershipRole.LEADER.value, StockLeadershipRole.CO_LEADER.value})
        return {
            "theme_id": theme_id,
            "theme_name": stocks[0].theme_name if stocks else theme_id,
            "stocks": tuple(stocks),
            "active_candidate_count": total,
            "watching_candidate_count": sum(1 for candidate, _ in items if candidate.state == CandidateState.WATCHING),
            "data_wait_count": data_wait_count,
            "alive_count": sum(1 for stock in stocks if stock.change_rate_pct >= -1.0),
            "strong_count": sum(1 for stock in stocks if stock.change_rate_pct >= 3.0),
            "leader_count": sum(1 for stock in stocks if stock.stock_role in {StockLeadershipRole.LEADER.value, StockLeadershipRole.CO_LEADER.value}),
            "breadth_ratio": _ratio(sum(1 for stock in stocks if stock.change_rate_pct > 0), total),
            "weighted_return_pct": weighted_return,
            "theme_turnover_krw": total_turnover,
            "leader_concentration": _ratio(leader_turnover, total_turnover),
            "leader_symbol": leader.code if leader else "",
            "leader_name": leader.name if leader else "",
            "co_leader_symbols": co_leaders,
            "opening_burst_score": max((stock.opening_burst_score for stock in stocks), default=0.0),
            "condition_boost_count": condition_boost_count,
            "realtime_valid_count": realtime_valid_count,
            "realtime_valid_ratio": _ratio(realtime_valid_count, total),
            "hydration_coverage_ratio": _ratio(hydration_count, total),
        }

    def _score_theme(self, metric: Mapping[str, Any], turnover_rank_score: float) -> ThemeBoardThemeSnapshot:
        stocks = tuple(metric["stocks"])
        active_count = int(metric["active_candidate_count"] or 0)
        realtime_ratio = float(metric["realtime_valid_ratio"] or 0.0)
        strong_ratio = _ratio(metric["strong_count"], active_count)
        leader_ratio = _ratio(metric["leader_count"], active_count)
        alive_ratio = _ratio(metric["alive_count"], active_count)
        breadth_score = float(metric["breadth_ratio"] or 0.0) * 100.0
        weighted_return_score = _scale(metric["weighted_return_pct"], -2.0, 8.0)
        leader_strength_score = _avg([stock.stock_score for stock in stocks if stock.stock_role in {StockLeadershipRole.LEADER.value, StockLeadershipRole.CO_LEADER.value}])
        momentum_score = _avg([_scale(_avg([stock.momentum_1m, stock.momentum_3m, stock.momentum_5m]), -1.0, 4.0) for stock in stocks])
        persistence_score = 60.0 if any("opening_burst" in stock.source_types for stock in stocks) else 25.0
        opening_burst_boost = min(10.0, float(metric["opening_burst_score"] or 0.0) / 10.0)
        condition_boost = min(8.0, float(metric["condition_boost_count"] or 0) * 2.0)
        leader_only_penalty = 10.0 if active_count <= 1 or float(metric["leader_concentration"] or 0.0) >= self.config.leader_only_concentration_min else 0.0
        data_quality_penalty = max(0.0, (1.0 - realtime_ratio) * 18.0)
        theme_score = (
            0.25 * turnover_rank_score
            + 0.20 * breadth_score
            + 0.20 * weighted_return_score
            + 0.15 * leader_strength_score
            + 0.10 * momentum_score
            + 0.10 * persistence_score
            + opening_burst_boost
            + condition_boost
            - leader_only_penalty
            - data_quality_penalty
        )
        flags: list[str] = []
        reasons: list[str] = []
        if realtime_ratio < self.config.min_realtime_valid_ratio:
            flags.append("REALTIME_COVERAGE_LOW")
            reasons.append("DATA_WAIT_REALTIME")
        if active_count == 0:
            flags.append("NO_ACTIVE_CANDIDATES")
            reasons.append("DATA_WAIT_CANDIDATE")
        if any("TR_BACKFILL_PRICE_ONLY" in stock.data_quality_flags for stock in stocks):
            flags.append("TR_BACKFILL_INCLUDED")
        status = self._theme_status(
            active_count=active_count,
            realtime_ratio=realtime_ratio,
            theme_score=theme_score,
            strong_count=int(metric["strong_count"] or 0),
            leader_count=int(metric["leader_count"] or 0),
            breadth_ratio=float(metric["breadth_ratio"] or 0.0),
            leader_concentration=float(metric["leader_concentration"] or 0.0),
        )
        return ThemeBoardThemeSnapshot(
            theme_id=str(metric["theme_id"] or ""),
            theme_name=str(metric["theme_name"] or ""),
            theme_status=status,
            theme_score=round(max(0.0, theme_score), 4),
            active_candidate_count=active_count,
            watching_candidate_count=int(metric["watching_candidate_count"] or 0),
            data_wait_count=int(metric["data_wait_count"] or 0),
            alive_count=int(metric["alive_count"] or 0),
            strong_count=int(metric["strong_count"] or 0),
            leader_count=int(metric["leader_count"] or 0),
            alive_ratio=round(alive_ratio, 4),
            strong_ratio=round(strong_ratio, 4),
            leader_ratio=round(leader_ratio, 4),
            breadth_ratio=round(float(metric["breadth_ratio"] or 0.0), 4),
            weighted_return_pct=round(float(metric["weighted_return_pct"] or 0.0), 4),
            theme_turnover_krw=round(float(metric["theme_turnover_krw"] or 0.0), 4),
            leader_concentration=round(float(metric["leader_concentration"] or 0.0), 4),
            leader_symbol=str(metric["leader_symbol"] or ""),
            leader_name=str(metric["leader_name"] or ""),
            co_leader_symbols=tuple(metric["co_leader_symbols"] or ()),
            opening_burst_score=round(float(metric["opening_burst_score"] or 0.0), 4),
            condition_boost_count=int(metric["condition_boost_count"] or 0),
            realtime_valid_count=int(metric["realtime_valid_count"] or 0),
            realtime_valid_ratio=round(realtime_ratio, 4),
            hydration_coverage_ratio=round(float(metric["hydration_coverage_ratio"] or 0.0), 4),
            data_quality_flags=tuple(_dedupe(flags)),
            reason_codes=tuple(_dedupe(reasons)),
            stocks=stocks[: self.config.max_stocks_per_theme],
        )

    def _theme_status(
        self,
        *,
        active_count: int,
        realtime_ratio: float,
        theme_score: float,
        strong_count: int,
        leader_count: int,
        breadth_ratio: float,
        leader_concentration: float,
    ) -> str:
        if active_count <= 0 or realtime_ratio < self.config.min_realtime_valid_ratio:
            return ThemeLeadershipStatus.DATA_WAIT.value
        if active_count <= 1 and leader_count >= 1:
            return ThemeLeadershipStatus.LEADER_ONLY_THEME.value
        if (
            theme_score >= self.config.leading_score_threshold
            and strong_count >= 2
            and leader_count >= 1
            and leader_concentration < 0.75
        ):
            return ThemeLeadershipStatus.LEADING_THEME.value
        if leader_count >= 1 and (breadth_ratio <= self.config.leader_only_breadth_max or leader_concentration >= self.config.leader_only_concentration_min):
            return ThemeLeadershipStatus.LEADER_ONLY_THEME.value
        if theme_score >= self.config.spreading_score_threshold and leader_count >= 1 and strong_count >= 2:
            return ThemeLeadershipStatus.SPREADING_THEME.value
        if theme_score >= self.config.watch_score_threshold or active_count > 0:
            return ThemeLeadershipStatus.WATCH_THEME.value
        return ThemeLeadershipStatus.WEAK_THEME.value

    def _is_overheated(self, item: Mapping[str, Any]) -> bool:
        if bool(item.get("vi_active")):
            return True
        if _float(item.get("upper_limit_gap_pct")) <= self.config.overheat_upper_limit_gap_pct:
            return True
        if _float(item.get("change_rate_pct")) >= self.config.overheat_return_threshold_pct and _float(item.get("pullback_from_high_pct")) <= self.config.overheat_pullback_from_high_pct:
            return True
        vwap = _float(item.get("vwap"))
        price = _float(item.get("current_price"))
        if vwap > 0 and price > 0 and ((price - vwap) / vwap) * 100.0 >= self.config.overheat_vwap_gap_pct:
            return True
        return False

    def _merge_candidate_metadata(self, snapshot: ThemeBoardSnapshot) -> int:
        updated = 0
        for stock in snapshot.stocks:
            if stock.candidate_id is None:
                continue
            candidate = self.db.load_candidate_by_id(stock.candidate_id)
            if candidate is None or candidate.state in {CandidateState.REMOVED, CandidateState.EXPIRED}:
                continue
            theme = next((item for item in snapshot.top_themes if item.theme_id == stock.theme_id), None)
            metadata = dict(candidate.metadata or {})
            metadata.update(
                {
                    "theme_board_theme_id": stock.theme_id,
                    "theme_board_theme_name": stock.theme_name,
                    "theme_board_theme_rank": theme.theme_rank if theme else 0,
                    "theme_board_theme_status": theme.theme_status if theme else "",
                    "theme_board_theme_score": theme.theme_score if theme else 0.0,
                    "theme_board_stock_role": stock.stock_role,
                    "theme_board_stock_score": stock.stock_score,
                    "theme_board_reason_codes": list(stock.reason_codes),
                    "entry_usable": False,
                    "updated_by_theme_board_at": snapshot.calculated_at,
                }
            )
            candidate.metadata = metadata
            self.db.save_candidate(candidate)
            updated += 1
        return updated

    def _candle_momentum(self, code: str, interval_min: int) -> float:
        if self.candle_builder is None:
            return 0.0
        candles = self.candle_builder.completed_candles(code, interval_min)
        if not candles:
            active = self.candle_builder.active_candle(code) if interval_min == 1 else None
            candles = [active] if active is not None else []
        if not candles:
            return 0.0
        candle = candles[-1]
        if candle.open <= 0:
            return 0.0
        return ((candle.close - candle.open) / candle.open) * 100.0

    def _candle_vwap(self, code: str) -> float:
        if self.candle_builder is None:
            return 0.0
        candles = self.candle_builder.completed_candles(code, 1)
        if not candles:
            return 0.0
        total_volume = sum(max(0, candle.volume) for candle in candles)
        if total_volume <= 0:
            return 0.0
        return sum(candle.close * max(0, candle.volume) for candle in candles) / total_volume


class ThemeBoardRuntimePipeline:
    def __init__(
        self,
        *,
        db: Any,
        market_data: MarketDataStore,
        repository: ThemeEngineRepository,
        candle_builder: CandleBuilder | None = None,
        config: ThemeBoardConfig | None = None,
        engine: ThemeBoardEngine | None = None,
        clock=None,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.repository = repository
        self.candle_builder = candle_builder
        self.config = config or ThemeBoardConfig.from_env()
        self.clock = clock or datetime.now
        self.engine = engine or ThemeBoardEngine(
            db,
            market_data=market_data,
            repository=repository,
            candle_builder=candle_builder,
            config=self.config,
            clock=self.clock,
        )
        self.last_result: ThemeBoardResult | None = None
        self.last_summary: dict[str, Any] = {"status": "DISABLED", "enabled": False, "output_mode": THEME_BOARD_OUTPUT_MODE}
        self.last_run_at: datetime | None = None

    def run_if_due(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        if not self.config.enabled:
            self.last_summary = {"status": "DISABLED", "enabled": False, "output_mode": THEME_BOARD_OUTPUT_MODE}
            return self.last_summary
        if self.last_run_at is not None and (current - self.last_run_at).total_seconds() < self.config.interval_sec:
            return dict(self.last_summary)
        result = self.engine.build(trade_date=current.date().isoformat(), now=current, save=True)
        self.last_result = result
        self.last_run_at = current
        self.last_summary = theme_board_dashboard_payload(result.snapshot)
        self.last_summary["enabled"] = True
        self.last_summary["status"] = "OK"
        return dict(self.last_summary)


def theme_board_dashboard_payload(snapshot: ThemeBoardSnapshot | Mapping[str, Any]) -> dict[str, Any]:
    data = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot or {})
    top_themes = list(data.get("top_themes") or [])
    stocks = list(data.get("stocks") or [])
    status_counts = Counter(str(theme.get("theme_status") or "") for theme in top_themes if isinstance(theme, Mapping))
    leaders = [
        stock
        for stock in stocks
        if isinstance(stock, Mapping) and str(stock.get("stock_role") or "") in {StockLeadershipRole.LEADER.value, StockLeadershipRole.CO_LEADER.value}
    ]
    warnings = list(data.get("data_quality_flags") or [])
    return {
        "calculated_at": data.get("calculated_at", ""),
        "trade_date": data.get("trade_date", ""),
        "board_status": data.get("board_status", ""),
        "top_themes": top_themes[:5],
        "theme_status_counts": dict(status_counts),
        "top_leaders": sorted(leaders, key=lambda item: float(item.get("stock_score") or 0.0), reverse=True)[:10],
        "data_wait_count": int(data.get("data_wait_theme_count") or status_counts.get(ThemeLeadershipStatus.DATA_WAIT.value, 0)),
        "weak_theme_count": int(status_counts.get(ThemeLeadershipStatus.WEAK_THEME.value, 0)),
        "leader_only_count": int(status_counts.get(ThemeLeadershipStatus.LEADER_ONLY_THEME.value, 0)),
        "source_counts": dict(data.get("source_counts") or {}),
        "warnings": warnings,
        "output_mode": data.get("output_mode", THEME_BOARD_OUTPUT_MODE),
        "ready_allowed": False,
        "order_intent_allowed": False,
    }


def theme_board_dashboard_section(db: Any, *, trade_date: str | None = None) -> dict[str, Any]:
    loader = getattr(db, "latest_theme_board_snapshot", None)
    if not callable(loader):
        return {"status": "UNAVAILABLE", "output_mode": THEME_BOARD_OUTPUT_MODE, "ready_allowed": False, "order_intent_allowed": False}
    snapshot = loader(trade_date=trade_date)
    if not snapshot:
        return {"status": "EMPTY", "output_mode": THEME_BOARD_OUTPUT_MODE, "ready_allowed": False, "order_intent_allowed": False}
    payload = theme_board_dashboard_payload(snapshot)
    payload["status"] = "OK"
    return payload


def _candidate_source_types(candidate: Candidate) -> tuple[str, ...]:
    metadata = dict(candidate.metadata or {})
    ingestion = dict(metadata.get("candidate_ingestion") or {})
    values = list(ingestion.get("active_source_types") or [])
    if not values:
        values = [getattr(source, "value", str(source)) for source in list(candidate.sources or [])]
    return tuple(_dedupe(values))


def _opening_burst_score(ingestion: Mapping[str, Any]) -> float:
    source_map = dict(ingestion.get("source_map") or {})
    scores = [
        _float(dict(entry).get("source_score"))
        for entry in source_map.values()
        if str(dict(entry).get("source_type") or "") == "opening_burst" and bool(dict(entry).get("active", True))
    ]
    return max(scores, default=0.0)


def _theme_display_name(repository: ThemeEngineRepository, theme_id: str) -> str:
    if not theme_id:
        return ""
    theme = repository.get_canonical_theme(theme_id)
    if theme is None:
        return theme_id
    return theme.display_name or theme.canonical_name or theme.theme_id


def _membership_score(repository: ThemeEngineRepository, theme_id: str, code: str) -> float:
    memberships = repository.get_members_by_theme(theme_id)
    clean = normalize_code(code)
    for membership in memberships:
        if normalize_code(membership.stock_code) == clean:
            return float(membership.membership_score or 0.0)
    return 0.0


def _with_theme_rank_reason(stock: ThemeBoardStockSnapshot, theme_rank: int) -> ThemeBoardStockSnapshot:
    return stock


def _rank_scores(values: Mapping[str, float], *, positive_only: bool = False) -> dict[str, float]:
    items = [(str(key), _float(value)) for key, value in values.items()]
    if positive_only:
        items = [(key, value) for key, value in items if value > 0]
    if not items:
        return {}
    ordered = sorted(items, key=lambda item: item[1], reverse=True)
    if len(ordered) == 1:
        return {ordered[0][0]: 100.0}
    return {key: max(0.0, 100.0 * (len(ordered) - index - 1) / (len(ordered) - 1)) for index, (key, _value) in enumerate(ordered)}


def _scale(value: Any, low: float, high: float) -> float:
    number = _float(value)
    if high <= low:
        return 0.0
    return max(0.0, min(100.0, ((number - low) / (high - low)) * 100.0))


def _avg(values: Iterable[Any]) -> float:
    numbers = [_float(value) for value in values if value not in {None, ""}]
    return sum(numbers) / len(numbers) if numbers else 0.0


def _ratio(numerator: Any, denominator: Any) -> float:
    denom = _float(denominator)
    if denom <= 0:
        return 0.0
    return _float(numerator) / denom


def _first_number(*values: Any) -> float:
    for value in values:
        number = _float(value)
        if number != 0.0:
            return number
    return 0.0


def _feature_number(metadata: Mapping[str, Any], key: str, fallback: float = 0.0) -> float:
    if key in metadata:
        return _float(metadata.get(key))
    return _float(fallback)


def _pullback_pct(price: float, high: float) -> float:
    if high <= 0 or price <= 0:
        return 100.0
    return max(0.0, ((high - price) / high) * 100.0)


def _spread_from_tick(tick: StrategyTick | None) -> float:
    if tick is None:
        return 0.0
    best_ask = _float(getattr(tick, "best_ask", 0))
    best_bid = _float(getattr(tick, "best_bid", 0))
    if best_ask > 0 and best_bid > 0:
        return max(0.0, best_ask - best_bid)
    return 0.0


def _upper_limit_gap(price: float, metadata: Mapping[str, Any]) -> float:
    if "upper_limit_gap_pct" in metadata:
        return _float(metadata.get("upper_limit_gap_pct"))
    upper_limit = _float(metadata.get("upper_limit_price"))
    if upper_limit > 0 and price > 0:
        return max(0.0, ((upper_limit - price) / upper_limit) * 100.0)
    return 100.0


def _is_late_laggard(item: Mapping[str, Any], leader_score: float) -> bool:
    if _float(item.get("change_rate_pct")) < 1.0:
        return False
    if leader_score - _float(item.get("stock_score")) < 20.0:
        return False
    if _avg([item.get("momentum_1m"), item.get("momentum_3m"), item.get("momentum_5m")]) > 0.5:
        return False
    return True


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(str(value).strip().replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        return int(default)


__all__ = [
    "ThemeBoardConfig",
    "ThemeBoardEngine",
    "ThemeBoardResult",
    "ThemeBoardRuntimePipeline",
    "ThemeBoardSnapshot",
    "ThemeBoardStockSnapshot",
    "ThemeBoardThemeSnapshot",
    "theme_board_dashboard_payload",
    "theme_board_dashboard_section",
]
