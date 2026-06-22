from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from trading.strategy.candidates import normalize_code
from trading.strategy.models import Candidate


SETUP_ROUTER_FEATURE_SCHEMA_VERSION = "setup_router_v3.features.v3"


@dataclass(frozen=True)
class SetupFeatureSnapshot:
    schema_version: str
    trade_date: str
    calculated_at: str
    candidate_id: int | None
    candidate_instance_id: str
    code: str
    name: str
    market: str
    candidate_state: str
    contract: dict[str, Any] = field(default_factory=dict)
    strategy_context: dict[str, Any] = field(default_factory=dict)
    entry_decision: dict[str, Any] = field(default_factory=dict)
    previous_observation: dict[str, Any] = field(default_factory=dict)
    setup_states: dict[str, Any] = field(default_factory=dict)
    expansion_lease: dict[str, Any] = field(default_factory=dict)
    context_id: str = ""
    context_fresh: bool = False
    session_phase: str = ""
    theme_id: str = ""
    theme_name: str = ""
    theme_state: str = ""
    leadership_status: str = ""
    leadership_entry_policy: str = ""
    selected_theme_rank: int = 0
    selected_theme_leadership_score: float = 0.0
    stock_role: str = ""
    stock_data_quality_status: str = ""
    market_side: str = ""
    side_market_regime: str = ""
    market_action: str = ""
    market_session_status: str = ""
    systemic_risk_off: bool = False
    market_block_new_entry: bool = False
    leadership_wait_new_entry: bool = False
    block_new_entry: bool = False
    vi_active: bool = False
    upper_limit_near: bool = False
    overheated: bool = False
    chase_risk: bool = False
    stale_data_block: bool = False
    realtime_tick_available: bool = False
    realtime_tick_fresh: bool = False
    tick_at: str = ""
    tick_age_sec: float = 0.0
    price_source: str = ""
    current_price: float = 0.0
    change_rate_pct: float = 0.0
    turnover_krw: float = 0.0
    cum_volume: int = 0
    execution_strength: float = 0.0
    best_bid: int = 0
    best_ask: int = 0
    spread_ticks: int = 0
    day_high: float = 0.0
    day_low: float = 0.0
    vwap: float = 0.0
    pullback_from_high_pct: float = 0.0
    completed_1m_candles: list[dict[str, Any]] = field(default_factory=list)
    active_1m_candle: dict[str, Any] = field(default_factory=dict)
    completed_1m_count: int = 0
    latest_completed_candle_at: str = ""
    momentum_1m_pct: float = 0.0
    momentum_3m_pct: float = 0.0
    momentum_5m_pct: float = 0.0
    entry_decision_id: int | None = None
    entry_decision_at: str = ""
    entry_decision_age_sec: float = 0.0
    entry_decision_fresh: bool = False
    entry_decision_source: str = ""
    entry_status: str = ""
    entry_price_location: str = ""
    entry_reason_codes: tuple[str, ...] = ()
    context_reason_codes: tuple[str, ...] = ()
    expansion_lease_present: bool = False
    selected_theme_lease_required: bool = False
    other_theme_lease_count: int = 0
    lease_status: str = ""
    lease_selected_at: str = ""
    lease_first_active_at: str = ""
    lease_first_fresh_tick_at: str = ""
    post_subscription_tick_verified: bool = True
    post_subscription_tick_reason: str = ""
    data_wait_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass
class SetupFeatureBuilder:
    market_data: Any | None = None
    candle_builder: Any | None = None
    min_completed_1m_candles: int = 3
    max_tick_age_sec: int = 10
    entry_decision_max_age_sec: int = 60

    def build(
        self,
        candidate: Candidate,
        *,
        now: datetime,
        contract_snapshot: Any | None = None,
        strategy_context: Mapping[str, Any] | None = None,
        entry_decision: Mapping[str, Any] | None = None,
        previous_observation: Mapping[str, Any] | None = None,
        setup_states: Mapping[str, Any] | None = None,
        expansion_lease: Mapping[str, Any] | None = None,
        selected_theme_lease_required: bool = False,
        other_theme_lease_count: int = 0,
    ) -> SetupFeatureSnapshot:
        current = now.replace(microsecond=0)
        code = normalize_code(candidate.code)
        metadata = dict(candidate.metadata or {})
        context = dict(strategy_context or metadata.get("strategy_context_v3") or {})
        entry = dict(entry_decision or {})
        contract = _snapshot_to_dict(contract_snapshot)
        market = dict(context.get("market") or {})
        theme = dict(context.get("theme") or {})
        stock = dict(context.get("stock") or {})
        data = dict(context.get("data") or {})
        risk = dict(context.get("risk") or {})
        tick = self.market_data.latest_tick(code) if self.market_data is not None else None
        tick_at_dt = getattr(tick, "timestamp", None) if tick is not None else None
        tick_metadata = dict(getattr(tick, "metadata", {}) or {}) if tick is not None else {}
        tick_age_sec = _age_sec(tick_at_dt, current)
        price_source = str(tick_metadata.get("price_source") or data.get("price_source") or "")
        realtime_tick_available = tick is not None and float(getattr(tick, "price", 0) or 0) > 0
        realtime_tick_fresh = realtime_tick_available and tick_age_sec <= max(1, int(self.max_tick_age_sec))
        completed = _completed_candles(self.candle_builder, code)
        active = _active_candle(self.candle_builder, code)
        day_high, day_low = _day_high_low(self.market_data, code)
        current_price = _float(getattr(tick, "price", 0) if tick is not None else entry.get("current_price"))
        if day_high <= 0:
            day_high = _float(stock.get("day_high") or tick_metadata.get("session_high"))
        if day_low <= 0:
            day_low = _float(stock.get("day_low") or tick_metadata.get("session_low"))
        vwap = _first_positive(
            tick_metadata.get("vwap"),
            stock.get("vwap"),
            data.get("vwap"),
            completed[-1].get("derived_vwap_at_close") if completed else 0,
            _vwap_from_candles(completed),
        )
        pullback = _first_nonzero(
            stock.get("pullback_from_high_pct"),
            tick_metadata.get("pullback_from_high_pct"),
            _pullback_pct(current_price, day_high),
        )
        source_timestamps = dict(context.get("source_timestamps") or {})
        context_reasons = tuple(_dedupe([*(context.get("reason_codes") or []), *(data.get("blocking_reason_codes") or [])]))
        entry_at = str(entry.get("calculated_at") or entry.get("created_at") or "")
        entry_age_sec = _age_sec(_parse_time(entry_at), current) if entry else 999999.0
        entry_trade_date = str(entry.get("trade_date") or "")
        trade_date = str(candidate.trade_date or context.get("trade_date") or current.date().isoformat())
        entry_fresh = bool(entry) and (not entry_trade_date or entry_trade_date == trade_date) and entry_age_sec <= max(1, int(self.entry_decision_max_age_sec))
        lease = dict(expansion_lease or {})
        lease_check = _post_subscription_tick_check(
            lease,
            theme_id=str(context.get("selected_theme_id") or theme.get("theme_id") or ""),
            tick_at=tick_at_dt,
            price_source=price_source,
            realtime_tick_fresh=realtime_tick_fresh,
            required=bool(selected_theme_lease_required),
        )

        data_wait = []
        if not context:
            data_wait.append("STRATEGY_CONTEXT_V3_MISSING")
        if context and not bool(context.get("context_fresh")):
            data_wait.append("STRATEGY_CONTEXT_V3_NOT_FRESH")
        if not realtime_tick_available:
            data_wait.append("REALTIME_TICK_MISSING")
        if tick is not None and not realtime_tick_fresh:
            data_wait.append("REALTIME_TICK_STALE")
        if price_source.upper() == "TR_BACKFILL":
            data_wait.append("TR_BACKFILL_TICK_NOT_SETUP_ELIGIBLE")
        if len(completed) < int(self.min_completed_1m_candles):
            data_wait.append("COMPLETED_1M_CANDLES_INSUFFICIENT")
        if context and not bool(data.get("theme_context_fresh", True)):
            data_wait.append("THEME_CONTEXT_NOT_FRESH")
        if context and not bool(data.get("market_context_fresh", True)):
            data_wait.append("MARKET_CONTEXT_NOT_FRESH")
        if context and not source_timestamps:
            data_wait.append("SOURCE_TIMESTAMPS_MISSING")
        if str(theme.get("theme_state") or "").upper() == "DATA_WAIT":
            data_wait.append("THEME_DATA_WAIT")
        if any("SIGNAL_STALE" in str(reason).upper() for reason in context_reasons):
            data_wait.append("SIGNAL_STALE")
        if any("REALTIME_COVERAGE_LOW" in str(reason).upper() for reason in context_reasons):
            data_wait.append("REALTIME_COVERAGE_LOW")
        if lease_check["lease_present"] and not lease_check["post_subscription_tick_verified"]:
            data_wait.append(str(lease_check["reason"] or "SETUP_POST_SUBSCRIPTION_FRESH_TICK_MISSING"))
        if lease_check["reason"] == "SETUP_SELECTED_THEME_LEASE_MISSING":
            data_wait.append("SETUP_SELECTED_THEME_LEASE_MISSING")

        return SetupFeatureSnapshot(
            schema_version=SETUP_ROUTER_FEATURE_SCHEMA_VERSION,
            trade_date=trade_date,
            calculated_at=current.isoformat(),
            candidate_id=candidate.id,
            candidate_instance_id=str(metadata.get("candidate_instance_id") or f"{trade_date}:{code}:{candidate.id or 0}"),
            code=code,
            name=str(candidate.name or metadata.get("name") or ""),
            market=str(candidate.market or market.get("market_side") or ""),
            candidate_state=str(getattr(candidate.state, "value", candidate.state) or ""),
            contract=contract,
            strategy_context=context,
            entry_decision=entry,
            previous_observation=dict(previous_observation or {}),
            setup_states=dict(setup_states or {}),
            expansion_lease=lease,
            context_id=str(context.get("context_id") or metadata.get("strategy_context_id") or ""),
            context_fresh=bool(context.get("context_fresh")),
            session_phase=str(context.get("session_phase") or ""),
            theme_id=str(context.get("selected_theme_id") or theme.get("theme_id") or ""),
            theme_name=str(theme.get("theme_name") or ""),
            theme_state=str(theme.get("theme_state") or ""),
            leadership_status=str(theme.get("leadership_status") or context.get("selected_theme_leadership_status") or ""),
            leadership_entry_policy=str(theme.get("leadership_entry_policy") or risk.get("leadership_entry_policy") or ""),
            selected_theme_rank=_int(context.get("selected_theme_rank")),
            selected_theme_leadership_score=_float(context.get("selected_theme_leadership_score")),
            stock_role=str(stock.get("trade_stock_role") or stock.get("stock_role") or ""),
            stock_data_quality_status=str(stock.get("stock_data_quality_status") or ""),
            market_side=str(market.get("market_side") or ""),
            side_market_regime=str(market.get("side_market_regime") or market.get("market_status") or ""),
            market_action=str(market.get("market_action") or ""),
            market_session_status=str(market.get("market_session_status") or ""),
            systemic_risk_off=bool(market.get("systemic_risk_off") or market.get("risk_off_detected")),
            market_block_new_entry=bool(risk.get("market_block_new_entry") or market.get("block_new_entry")),
            leadership_wait_new_entry=bool(risk.get("leadership_wait_new_entry")),
            block_new_entry=bool(market.get("block_new_entry") or risk.get("market_block_new_entry")),
            vi_active=bool(stock.get("vi_active")),
            upper_limit_near=bool(stock.get("upper_limit_near")),
            overheated=bool(stock.get("overheated") or risk.get("overheat_block")),
            chase_risk=bool(risk.get("chase_risk") or stock.get("chase_risk")),
            stale_data_block=bool(risk.get("stale_data_block")),
            realtime_tick_available=realtime_tick_available,
            realtime_tick_fresh=realtime_tick_fresh,
            tick_at=tick_at_dt.replace(microsecond=0).isoformat() if isinstance(tick_at_dt, datetime) else "",
            tick_age_sec=round(tick_age_sec, 3),
            price_source=price_source,
            current_price=current_price,
            change_rate_pct=_float(getattr(tick, "change_rate", 0.0) if tick is not None else stock.get("change_rate_pct")),
            turnover_krw=_float(getattr(tick, "trade_value", 0.0) if tick is not None else stock.get("turnover_krw")),
            cum_volume=_int(getattr(tick, "cum_volume", 0) if tick is not None else 0),
            execution_strength=_float(getattr(tick, "execution_strength", 0.0) if tick is not None else stock.get("execution_strength")),
            best_bid=_int(getattr(tick, "best_bid", 0) if tick is not None else 0),
            best_ask=_int(getattr(tick, "best_ask", 0) if tick is not None else 0),
            spread_ticks=_int(getattr(tick, "spread_ticks", 0) if tick is not None else 0),
            day_high=day_high,
            day_low=day_low,
            vwap=vwap,
            pullback_from_high_pct=pullback,
            completed_1m_candles=completed,
            active_1m_candle=active,
            completed_1m_count=len(completed),
            latest_completed_candle_at=str(completed[-1].get("candle_at") or completed[-1].get("start_at") or "") if completed else "",
            momentum_1m_pct=_momentum(completed, 1),
            momentum_3m_pct=_momentum(completed, 3),
            momentum_5m_pct=_momentum(completed, 5),
            entry_decision_id=_int_or_none(entry.get("id")),
            entry_decision_at=entry_at,
            entry_decision_age_sec=round(entry_age_sec, 3),
            entry_decision_fresh=entry_fresh,
            entry_decision_source=str(entry.get("source") or entry.get("decision_source") or "entry_engine" if entry else ""),
            entry_status=str(entry.get("entry_status") or ""),
            entry_price_location=str(entry.get("price_location") or ""),
            entry_reason_codes=tuple(_dedupe(entry.get("reason_codes") or [])),
            context_reason_codes=context_reasons,
            expansion_lease_present=bool(lease_check["lease_present"]),
            selected_theme_lease_required=bool(selected_theme_lease_required),
            other_theme_lease_count=max(0, int(other_theme_lease_count or 0)),
            lease_status=str(lease_check["lease_status"]),
            lease_selected_at=str(lease_check["selected_at"]),
            lease_first_active_at=str(lease_check["first_active_at"]),
            lease_first_fresh_tick_at=str(lease_check["first_fresh_tick_at"]),
            post_subscription_tick_verified=bool(lease_check["post_subscription_tick_verified"]),
            post_subscription_tick_reason=str(lease_check["reason"]),
            data_wait_reasons=tuple(_dedupe(data_wait)),
        )


def _snapshot_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict() or {})
    if is_dataclass(value):
        return _jsonable(asdict(value))
    return {}


def _completed_candles(candle_builder: Any | None, code: str) -> list[dict[str, Any]]:
    if candle_builder is None:
        return []
    loader = getattr(candle_builder, "completed_candles", None)
    if not callable(loader):
        return []
    return _enrich_completed_candles([_candle_to_dict(candle) for candle in list(loader(code, 1) or [])])


def _active_candle(candle_builder: Any | None, code: str) -> dict[str, Any]:
    if candle_builder is None:
        return {}
    loader = getattr(candle_builder, "active_candle", None)
    if not callable(loader):
        return {}
    data = _candle_to_dict(loader(code, 1))
    if data:
        data.setdefault("completed", False)
        data.setdefault("candle_at", data.get("start_at") or "")
    return data


def _candle_to_dict(candle: Any) -> dict[str, Any]:
    if candle is None:
        return {}
    if isinstance(candle, Mapping):
        data = dict(candle)
    elif is_dataclass(candle):
        data = asdict(candle)
    else:
        data = {
            "code": getattr(candle, "code", ""),
            "interval_min": getattr(candle, "interval_min", 1),
            "start_at": getattr(candle, "start_at", ""),
            "open": getattr(candle, "open", 0),
            "high": getattr(candle, "high", 0),
            "low": getattr(candle, "low", 0),
            "close": getattr(candle, "close", 0),
            "volume": getattr(candle, "volume", 0),
            "trade_value": getattr(candle, "trade_value", 0),
        }
    for key in ("start_at", "candle_at", "ended_at"):
        if isinstance(data.get(key), datetime):
            data[key] = data[key].replace(microsecond=0).isoformat()
    return _jsonable(data)


def _enrich_completed_candles(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    cumulative_volume = 0.0
    cumulative_value = 0.0
    for raw in candles:
        candle = dict(raw or {})
        candle_at = str(candle.get("candle_at") or candle.get("start_at") or "")
        open_price = _float(candle.get("open"))
        high = _float(candle.get("high"))
        low = _float(candle.get("low"))
        close = _float(candle.get("close"))
        volume = _float(candle.get("volume"))
        explicit_cumulative_volume = _float(candle.get("cumulative_volume") or candle.get("cum_volume"))
        explicit_cumulative_value = _float(candle.get("cumulative_value") or candle.get("cumulative_trade_value"))
        trade_value = _float(candle.get("trade_value") or candle.get("value") or candle.get("turnover_krw"))
        typical = (high + low + close) / 3.0 if high > 0 and low > 0 and close > 0 else close
        candle_value = trade_value if trade_value > 0 else typical * volume if volume > 0 else 0.0
        cumulative_volume = explicit_cumulative_volume if explicit_cumulative_volume > 0 else cumulative_volume + max(0.0, volume)
        cumulative_value = explicit_cumulative_value if explicit_cumulative_value > 0 else cumulative_value + max(0.0, candle_value)
        explicit_vwap = _first_positive(candle.get("derived_vwap_at_close"), candle.get("vwap_at_close"), candle.get("vwap"))
        if explicit_vwap > 0:
            derived_vwap = explicit_vwap
            vwap_source = str(candle.get("vwap_source") or "explicit")
        elif cumulative_volume > 0 and cumulative_value > 0:
            derived_vwap = cumulative_value / cumulative_volume
            vwap_source = "actual_cumulative" if explicit_cumulative_value > 0 else "typical_price_volume"
        else:
            derived_vwap = typical
            vwap_source = "typical_price"
        candle.update(
            {
                "candle_at": candle_at,
                "start_at": candle_at or candle.get("start_at") or "",
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": int(volume) if float(volume).is_integer() else volume,
                "trade_value": candle_value,
                "cumulative_volume": cumulative_volume,
                "cumulative_value": cumulative_value,
                "derived_vwap_at_close": derived_vwap,
                "vwap_source": vwap_source,
                "close_vs_vwap_pct": _pct(close - derived_vwap, derived_vwap),
                "completed": True,
            }
        )
        enriched.append(_jsonable(candle))
    return enriched


def _post_subscription_tick_check(
    lease: Mapping[str, Any],
    *,
    theme_id: str,
    tick_at: Any,
    price_source: str,
    realtime_tick_fresh: bool,
    required: bool = False,
) -> dict[str, Any]:
    if not lease:
        return {
            "lease_present": False,
            "lease_status": "",
            "selected_at": "",
            "first_active_at": "",
            "first_fresh_tick_at": "",
            "post_subscription_tick_verified": not required,
            "reason": "SETUP_SELECTED_THEME_LEASE_MISSING" if required else "NO_SELECTED_THEME_EXPANSION_LEASE_REQUIRED",
        }
    lease_theme = str(lease.get("theme_id") or "")
    status = str(lease.get("status") or "")
    active_statuses = {"ACTIVE", "HOLDING", "PROTECTED"}
    if lease_theme and theme_id and lease_theme != theme_id:
        return {
            "lease_present": True,
            "lease_status": status,
            "selected_at": str(lease.get("selected_at") or ""),
            "first_active_at": str(lease.get("first_active_at") or ""),
            "first_fresh_tick_at": str(lease.get("first_fresh_tick_at") or ""),
            "post_subscription_tick_verified": False,
            "reason": "SETUP_SELECTED_THEME_LEASE_MISMATCH",
        }
    if status.upper() not in active_statuses:
        return {
            "lease_present": True,
            "lease_status": status,
            "selected_at": str(lease.get("selected_at") or ""),
            "first_active_at": str(lease.get("first_active_at") or ""),
            "first_fresh_tick_at": str(lease.get("first_fresh_tick_at") or ""),
            "post_subscription_tick_verified": True,
            "reason": "LEASE_NOT_ACTIVE",
        }
    selected_at = str(lease.get("selected_at") or lease.get("selected_tick_baseline_at") or "")
    first_active_at = str(lease.get("first_active_at") or "")
    first_fresh_tick_at = str(lease.get("first_fresh_tick_at") or lease.get("first_post_subscription_tick_at") or "")
    baselines = [_parse_time(selected_at), _parse_time(first_active_at)]
    baseline = max([item for item in baselines if item is not None], default=None)
    tick_dt = tick_at if isinstance(tick_at, datetime) else _parse_time(str(tick_at or ""))
    verified = bool(
        tick_dt is not None
        and realtime_tick_fresh
        and str(price_source or "").upper() == "REALTIME"
        and (baseline is None or tick_dt.replace(tzinfo=None) >= baseline.replace(tzinfo=None))
    )
    if first_fresh_tick_at:
        first_fresh = _parse_time(first_fresh_tick_at)
        verified = verified and (first_fresh is None or tick_dt is None or tick_dt.replace(tzinfo=None) >= first_fresh.replace(tzinfo=None))
    return {
        "lease_present": True,
        "lease_status": status,
        "selected_at": selected_at,
        "first_active_at": first_active_at,
        "first_fresh_tick_at": first_fresh_tick_at,
        "post_subscription_tick_verified": verified,
        "reason": "POST_SUBSCRIPTION_FRESH_TICK_VERIFIED" if verified else "SETUP_POST_SUBSCRIPTION_FRESH_TICK_MISSING",
    }


def _day_high_low(market_data: Any | None, code: str) -> tuple[float, float]:
    if market_data is None:
        return 0.0, 0.0
    loader = getattr(market_data, "day_high_low", None)
    if not callable(loader):
        return 0.0, 0.0
    high, low = loader(code)
    return _float(high), _float(low)


def _age_sec(value: Any, now: datetime) -> float:
    if not isinstance(value, datetime):
        return 999999.0
    timestamp = value.replace(tzinfo=None)
    return max(0.0, (now.replace(tzinfo=None) - timestamp).total_seconds())


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def _vwap_from_candles(candles: list[dict[str, Any]]) -> float:
    total_value = 0.0
    total_volume = 0.0
    for candle in candles:
        volume = _float(candle.get("volume"))
        if volume <= 0:
            continue
        value = _float(candle.get("trade_value"))
        if value <= 0:
            typical = (_float(candle.get("high")) + _float(candle.get("low")) + _float(candle.get("close"))) / 3.0
            value = typical * volume
        total_value += value
        total_volume += volume
    return total_value / total_volume if total_volume > 0 else 0.0


def _pullback_pct(price: float, high: float) -> float:
    if price <= 0 or high <= 0:
        return 0.0
    return max(0.0, (high - price) / high * 100.0)


def _momentum(candles: list[dict[str, Any]], lookback: int) -> float:
    if len(candles) < lookback:
        return 0.0
    window = candles[-lookback:]
    start = _float(window[0].get("open"))
    end = _float(window[-1].get("close"))
    if start <= 0 or end <= 0:
        return 0.0
    return round((end - start) / start * 100.0, 4)


def _first_positive(*values: Any) -> float:
    for value in values:
        number = _float(value)
        if number > 0:
            return number
    return 0.0


def _first_nonzero(*values: Any) -> float:
    for value in values:
        number = _float(value)
        if number != 0.0:
            return number
    return 0.0


def _pct(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator * 100.0, 4)


def _float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(str(value).strip().replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip().replace(",", "")))
    except (TypeError, ValueError):
        return default


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).strip().replace(",", "")))
    except (TypeError, ValueError):
        return None


def _dedupe(values: Any) -> list[str]:
    result: list[str] = []
    for value in list(values or []):
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value
