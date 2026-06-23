from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Iterator

from trading.strategy.candidates import normalize_code


MARKET_CONTEXT_VIEW_SCHEMA_VERSION = "market_context_view_v1"


@dataclass(frozen=True)
class MarketSideContextView:
    side: str
    status: str = "DATA_WAIT"
    index_return_pct: float = 0.0
    index_slope_1m_pct: float | None = None
    index_slope_3m_pct: float | None = None
    index_slope_5m_pct: float | None = None
    breadth_pct: float = 0.0
    turnover_weighted_return_pct: float = 0.0
    risk_score: float = 0.0
    data_quality_flags: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "status": self.status,
            "index_return_pct": self.index_return_pct,
            "index_slope_1m_pct": self.index_slope_1m_pct,
            "index_slope_3m_pct": self.index_slope_3m_pct,
            "index_slope_5m_pct": self.index_slope_5m_pct,
            "breadth_pct": self.breadth_pct,
            "turnover_weighted_return_pct": self.turnover_weighted_return_pct,
            "risk_score": self.risk_score,
            "data_quality_flags": list(self.data_quality_flags),
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class MarketContextSummary(Mapping[str, Any]):
    market_context_id: str = ""
    market_context_generation: str = ""
    trade_date: str = ""
    calculated_at: str = ""
    global_status: str = "DATA_WAIT"
    kospi_status: str = "DATA_WAIT"
    kosdaq_status: str = "DATA_WAIT"
    composite_market_mode: str = "DATA_DEGRADED"
    systemic_risk_off: bool = False
    market_session_status: str = "closed"
    market_open: bool = False
    market_closed: bool = True
    risk_off_detected: bool = False
    weak_market_detected: bool = False
    reason_codes: tuple[str, ...] = ()
    source: str = "UNAVAILABLE"
    schema_version: str = MARKET_CONTEXT_VIEW_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_context_id": self.market_context_id,
            "market_context_generation": self.market_context_generation,
            "trade_date": self.trade_date,
            "calculated_at": self.calculated_at,
            "global_status": self.global_status,
            "kospi_status": self.kospi_status,
            "kosdaq_status": self.kosdaq_status,
            "composite_market_mode": self.composite_market_mode,
            "systemic_risk_off": self.systemic_risk_off,
            "market_session_status": self.market_session_status,
            "market_open": self.market_open,
            "market_closed": self.market_closed,
            "risk_off_detected": self.risk_off_detected,
            "weak_market_detected": self.weak_market_detected,
            "reason_codes": list(self.reason_codes),
            "source": self.source,
            "schema_version": self.schema_version,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


@dataclass(frozen=True)
class MarketContextView(Mapping[str, Any]):
    summary: MarketContextSummary = field(default_factory=MarketContextSummary)
    kospi: MarketSideContextView = field(default_factory=lambda: MarketSideContextView(side="KOSPI"))
    kosdaq: MarketSideContextView = field(default_factory=lambda: MarketSideContextView(side="KOSDAQ"))
    candidate_policy_by_code: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    source: str = "UNAVAILABLE"
    schema_version: str = MARKET_CONTEXT_VIEW_SCHEMA_VERSION

    @property
    def calculated_at(self) -> str:
        return self.summary.calculated_at

    @property
    def trade_date(self) -> str:
        return self.summary.trade_date

    @property
    def policy_count(self) -> int:
        return len(self.candidate_policy_by_code)

    def policy_for(self, code: str) -> Any | None:
        normalized = normalize_code(code)
        if not normalized:
            return None
        return self.candidate_policy_by_code.get(normalized) or self.candidate_policy_by_code.get(str(code or ""))

    def side_context(self, side: str) -> MarketSideContextView:
        normalized = str(_enum_value(side) or "").upper()
        if normalized == "KOSPI":
            return self.kospi
        if normalized == "KOSDAQ":
            return self.kosdaq
        return MarketSideContextView(side="UNKNOWN")

    def side_status(self, side: str) -> str:
        return self.side_context(side).status

    def side_return_pct(self, side: str) -> float:
        return self.side_context(side).index_return_pct

    def side_breadth_pct(self, side: str) -> float:
        return self.side_context(side).breadth_pct

    def counterpart_side(self, side: str) -> str:
        normalized = str(side or "").upper()
        if normalized == "KOSPI":
            return "KOSDAQ"
        if normalized == "KOSDAQ":
            return "KOSPI"
        return "UNKNOWN"

    def is_fresh(self, now: datetime, max_age_sec: int) -> bool:
        return self.is_transport_fresh(now, max_age_sec)

    def is_transport_fresh(self, now: datetime, max_age_sec: int) -> bool:
        if not self.trade_date or self.trade_date != now.date().isoformat():
            return False
        if not self.calculated_at:
            return False
        if not self.schema_supported():
            return False
        if self.summary.market_closed or self.summary.market_session_status.lower() == "closed" or self.summary.global_status == "MARKET_CLOSED":
            return True
        age = _age_seconds(self.calculated_at, now)
        return age is not None and age <= max(1, int(max_age_sec or 30))

    def schema_supported(self) -> bool:
        version = str(self.schema_version or self.summary.schema_version or "")
        return version in {"", MARKET_CONTEXT_VIEW_SCHEMA_VERSION}

    def decision_data_status(self) -> str:
        statuses = {
            str(self.summary.global_status or "").upper(),
            str(self.summary.kospi_status or "").upper(),
            str(self.summary.kosdaq_status or "").upper(),
        }
        if self.summary.market_closed or self.summary.market_session_status.lower() == "closed" or self.summary.global_status == "MARKET_CLOSED":
            return "MARKET_CLOSED"
        if statuses <= {"", "DATA_WAIT"}:
            return "FULL_DATA_WAIT"
        if "DATA_WAIT" in statuses:
            return "PARTIAL_DATA_WAIT"
        return "READY"

    def is_decision_ready(self) -> bool:
        return self.decision_data_status() == "READY"

    def to_theme_summary(self) -> MarketContextSummary:
        return self.summary

    def to_transport_diagnostics(
        self,
        now: datetime,
        *,
        max_age_sec: int,
        build_ms: int = 0,
        full_snapshot_serialize_count: int = 0,
        db_fallback_count: int = 0,
        summary_fallback_count: int = 0,
        warning_codes: tuple[str, ...] = (),
        fallback_reason: str = "",
        pipeline_view_present: bool | None = None,
        market_section_status: str = "",
        current_snapshot_authoritative: bool | None = None,
    ) -> dict[str, Any]:
        age = _age_seconds(self.calculated_at, now)
        transport_fresh = self.is_transport_fresh(now, max_age_sec)
        decision_status = self.decision_data_status()
        if not self.calculated_at:
            transport_status = "MISSING"
        elif not self.trade_date or self.trade_date != now.date().isoformat():
            transport_status = "TRADE_DATE_MISMATCH"
        elif not self.schema_supported():
            transport_status = "UNSUPPORTED_SCHEMA"
        elif transport_fresh:
            transport_status = "AVAILABLE"
        else:
            transport_status = "STALE"
        return {
            "status": "OK" if transport_fresh else "DATA_WAIT",
            "source": self.source,
            "schema_version": self.schema_version,
            "market_context_id": self.summary.market_context_id,
            "market_context_generation": self.summary.market_context_generation,
            "calculated_at": self.calculated_at,
            "age_sec": round(age, 3) if age is not None else None,
            "freshness_age_sec": round(age, 3) if age is not None else None,
            "trade_date_match": bool(self.trade_date and self.trade_date == now.date().isoformat()),
            "schema_supported": self.schema_supported(),
            "usable": transport_fresh,
            "transport_status": transport_status,
            "transport_fresh": transport_fresh,
            "decision_ready": self.is_decision_ready(),
            "decision_data_status": decision_status,
            "fallback_reason": str(fallback_reason or ""),
            "policy_count": self.policy_count,
            "global_status": self.summary.global_status,
            "kospi_status": self.summary.kospi_status,
            "kosdaq_status": self.summary.kosdaq_status,
            "market_section_status": str(market_section_status or ""),
            "pipeline_view_present": bool(pipeline_view_present) if pipeline_view_present is not None else None,
            "current_snapshot_authoritative": bool(current_snapshot_authoritative) if current_snapshot_authoritative is not None else None,
            "build_ms": int(build_ms or 0),
            "full_snapshot_serialize_count": int(full_snapshot_serialize_count or 0),
            "db_fallback_count": int(db_fallback_count or 0),
            "summary_fallback_count": int(summary_fallback_count or 0),
            "warning_codes": list(warning_codes or ()),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.summary.to_dict()

    def __getitem__(self, key: str) -> Any:
        return self.summary.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.summary.to_dict())

    def __len__(self) -> int:
        return len(self.summary.to_dict())


def market_context_view_from_snapshot(
    snapshot: Any,
    *,
    serialized_snapshot: Mapping[str, Any] | None = None,
    source: str = "PIPELINE_VIEW",
) -> MarketContextView:
    data = serialized_snapshot or {}
    summary = _summary_from_sources(snapshot=snapshot, data=data, source=source)
    kospi = _side_view_from_source(_obj_value(snapshot, "kospi_snapshot", data.get("kospi_snapshot")), side="KOSPI", data=data)
    kosdaq = _side_view_from_source(_obj_value(snapshot, "kosdaq_snapshot", data.get("kosdaq_snapshot")), side="KOSDAQ", data=data)
    policies = _obj_value(snapshot, "candidate_policy_by_code", data.get("candidate_policy_by_code") or {})
    return MarketContextView(
        summary=summary,
        kospi=kospi,
        kosdaq=kosdaq,
        candidate_policy_by_code=MappingProxyType(policies if isinstance(policies, Mapping) else {}),
        source=source,
    )


def market_context_view_from_mapping(payload: Mapping[str, Any] | None, *, source: str) -> MarketContextView:
    data = payload if isinstance(payload, Mapping) else {}
    summary = _summary_from_sources(snapshot=None, data=data, source=source)
    kospi = _side_view_from_source(data.get("kospi_snapshot") or {}, side="KOSPI", data=data)
    kosdaq = _side_view_from_source(data.get("kosdaq_snapshot") or {}, side="KOSDAQ", data=data)
    policies = data.get("candidate_policy_by_code") or {}
    return MarketContextView(
        summary=summary,
        kospi=kospi,
        kosdaq=kosdaq,
        candidate_policy_by_code=MappingProxyType(policies if isinstance(policies, Mapping) else {}),
        source=source,
    )


def unavailable_market_context_view(trade_date: str, *, calculated_at: str = "", source: str = "UNAVAILABLE") -> MarketContextView:
    summary = MarketContextSummary(
        trade_date=str(trade_date or ""),
        calculated_at=str(calculated_at or ""),
        global_status="DATA_WAIT",
        kospi_status="DATA_WAIT",
        kosdaq_status="DATA_WAIT",
        source=source,
        reason_codes=("MARKET_CONTEXT_NOT_READY",),
    )
    return MarketContextView(summary=summary, source=source)


def market_context_identity(
    *,
    trade_date: str,
    calculated_at: str,
    global_status: Any,
    kospi_status: Any,
    kosdaq_status: Any,
    composite_market_mode: Any,
    policy_count: int = 0,
) -> str:
    raw = "|".join(
        [
            MARKET_CONTEXT_VIEW_SCHEMA_VERSION,
            str(trade_date or ""),
            str(calculated_at or ""),
            str(_enum_value(global_status) or ""),
            str(_enum_value(kospi_status) or ""),
            str(_enum_value(kosdaq_status) or ""),
            str(_enum_value(composite_market_mode) or ""),
            str(int(policy_count or 0)),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def market_context_view_max_age_sec(default: int = 30) -> int:
    import os

    try:
        return max(1, int(float(str(os.getenv("TRADING_MARKET_CONTEXT_VIEW_MAX_AGE_SEC", str(default))).strip())))
    except (TypeError, ValueError):
        return default


def _summary_from_sources(*, snapshot: Any | None, data: Mapping[str, Any], source: str) -> MarketContextSummary:
    trade_date = str(_obj_value(snapshot, "trade_date", data.get("trade_date") or "") or "")
    calculated_at = str(_obj_value(snapshot, "calculated_at", data.get("calculated_at") or "") or "")
    kospi_status = _status_value(_obj_value(snapshot, "kospi_status", data.get("kospi_status") or _nested(data, "kospi_snapshot", "status")))
    kosdaq_status = _status_value(_obj_value(snapshot, "kosdaq_status", data.get("kosdaq_status") or _nested(data, "kosdaq_snapshot", "status")))
    global_status = _status_value(_obj_value(snapshot, "global_status", data.get("global_status") or "DATA_WAIT"))
    session_status = str(_obj_value(snapshot, "market_session_status", data.get("market_session_status") or "") or "")
    market_closed = bool(_obj_value(snapshot, "market_closed", data.get("market_closed", session_status.lower() == "closed")))
    market_open = bool(_obj_value(snapshot, "market_open", data.get("market_open", not market_closed and session_status.lower() != "closed")))
    systemic = _obj_value(snapshot, "systemic_risk_off", data.get("systemic_risk_off", None))
    if systemic is None:
        systemic = _infer_systemic(kospi_status, kosdaq_status, market_open=market_open)
    reason_codes = tuple(str(item) for item in list(_obj_value(snapshot, "reason_codes", data.get("reason_codes") or ()) or ()))
    policy_count = len(_obj_value(snapshot, "candidate_policy_by_code", data.get("candidate_policy_by_code") or {}) or {})
    identity = str(
        _obj_value(
            snapshot,
            "market_context_id",
            data.get("market_context_id")
            or market_context_identity(
                trade_date=trade_date,
                calculated_at=calculated_at,
                global_status=global_status,
                kospi_status=kospi_status,
                kosdaq_status=kosdaq_status,
                composite_market_mode=_obj_value(snapshot, "composite_market_mode", data.get("composite_market_mode") or "DATA_DEGRADED"),
                policy_count=policy_count,
            ),
        )
        or ""
    )
    return MarketContextSummary(
        market_context_id=identity,
        market_context_generation=str(_obj_value(snapshot, "market_context_generation", data.get("market_context_generation") or identity) or ""),
        trade_date=trade_date,
        calculated_at=calculated_at,
        global_status=global_status,
        kospi_status=kospi_status,
        kosdaq_status=kosdaq_status,
        composite_market_mode=str(_enum_value(_obj_value(snapshot, "composite_market_mode", data.get("composite_market_mode") or "DATA_DEGRADED"))),
        systemic_risk_off=bool(systemic),
        market_session_status=session_status or ("closed" if market_closed else "open"),
        market_open=market_open,
        market_closed=market_closed,
        risk_off_detected=bool(_obj_value(snapshot, "risk_off_detected", data.get("risk_off_detected", "RISK_OFF" in {kospi_status, kosdaq_status}))),
        weak_market_detected=bool(_obj_value(snapshot, "weak_market_detected", data.get("weak_market_detected", "WEAK" in {kospi_status, kosdaq_status}))),
        reason_codes=reason_codes,
        source=source,
    )


def _side_view_from_source(source: Any, *, side: str, data: Mapping[str, Any]) -> MarketSideContextView:
    prefix = side.lower()
    return MarketSideContextView(
        side=side,
        status=_status_value(_obj_value(source, "status", data.get(f"{prefix}_status") or "DATA_WAIT")),
        index_return_pct=_float(_obj_value(source, "index_return_pct", data.get(f"{prefix}_return_pct"))),
        index_slope_1m_pct=_optional_float(_obj_value(source, "index_slope_1m_pct", None)),
        index_slope_3m_pct=_optional_float(_obj_value(source, "index_slope_3m_pct", None)),
        index_slope_5m_pct=_optional_float(_obj_value(source, "index_slope_5m_pct", None)),
        breadth_pct=_float(_obj_value(source, "breadth_pct", data.get(f"{prefix}_breadth_pct"))),
        turnover_weighted_return_pct=_float(_obj_value(source, "turnover_weighted_return_pct", 0.0)),
        risk_score=_float(_obj_value(source, "risk_score", 0.0)),
        data_quality_flags=tuple(str(item) for item in list(_obj_value(source, "data_quality_flags", ()) or ())),
        reason_codes=tuple(str(item) for item in list(_obj_value(source, "reason_codes", ()) or ())),
    )


def _obj_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    if obj is not None and hasattr(obj, key):
        return getattr(obj, key)
    return default


def _nested(data: Mapping[str, Any], outer: str, inner: str) -> Any:
    value = data.get(outer)
    if isinstance(value, Mapping):
        return value.get(inner)
    return None


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _status_value(value: Any) -> str:
    return str(_enum_value(value) or "DATA_WAIT").upper()


def _infer_systemic(kospi: str, kosdaq: str, *, market_open: bool) -> bool:
    if not market_open:
        return False
    if kospi == "RISK_OFF" and kosdaq in {"RISK_OFF", "WEAK"}:
        return True
    if kosdaq == "RISK_OFF" and kospi in {"RISK_OFF", "WEAK"}:
        return True
    return False


def _age_seconds(calculated_at: str, now: datetime) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(calculated_at or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (now.replace(tzinfo=None) - parsed.replace(tzinfo=None)).total_seconds())


def _float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(str(value).strip().replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return _float(value)
