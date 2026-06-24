from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


PRICE_TICK_FIELDS = (
    "price",
    "change_rate",
    "volume",
    "trade_value",
    "execution_strength",
    "best_bid_ask",
    "day_high_low",
    "momentum",
)
INDEX_PRICE_TICK_FIELDS = (
    "price",
    "change_rate",
    "trade_value",
    "volume",
    "day_high_low",
)

FIELD_RELIABILITY_WEIGHTS = {
    "price": 30.0,
    "volume": 15.0,
    "trade_value": 15.0,
    "execution_strength": 10.0,
    "best_bid_ask": 10.0,
    "day_high_low": 8.0,
    "momentum": 7.0,
    "change_rate": 5.0,
}
INDEX_FIELD_RELIABILITY_WEIGHTS = {
    "price": 50.0,
    "change_rate": 25.0,
    "trade_value": 15.0,
    "volume": 5.0,
    "day_high_low": 5.0,
}

REASON_PENALTIES = {
    "MISSING_CURRENT_PRICE": 40.0,
    "PRICE_MISSING": 40.0,
    "STALE_QUOTE": 25.0,
    "REAL_PARSE_FALLBACK": 12.0,
    "TRADE_VALUE_MISSING": 5.0,
    "TURNOVER_ESTIMATED": 4.0,
    "EXECUTION_STRENGTH_MISSING": 6.0,
    "BEST_BID_ASK_MISSING": 6.0,
    "DAY_HIGH_LOW_MISSING": 4.0,
    "MOMENTUM_WARMUP": 2.0,
}
INDEX_IGNORED_REASONS = {
    "BEST_BID_ASK_MISSING",
    "DAY_HIGH_LOW_MISSING",
    "EXECUTION_STRENGTH_MISSING",
    "MOMENTUM_WARMUP",
}


@dataclass(frozen=True)
class RealtimeReliabilityAssessment:
    score: float
    bucket: str
    field_score: float
    penalty: float
    reasons: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    transport_latency_ms: float | None = None
    transport_latency_bucket: str = "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "bucket": self.bucket,
            "field_score": self.field_score,
            "penalty": self.penalty,
            "reasons": list(self.reasons),
            "missing_fields": list(self.missing_fields),
            "transport_latency_ms": self.transport_latency_ms,
            "transport_latency_bucket": self.transport_latency_bucket,
        }


@dataclass
class RealtimeDataQualityTracker:
    total_price_ticks: int = 0
    _field_counts: Counter[str] = field(default_factory=Counter)
    _reason_code_counts: Counter[str] = field(default_factory=Counter)
    _reliability_bucket_counts: Counter[str] = field(default_factory=Counter)
    _reliability_reason_counts: Counter[str] = field(default_factory=Counter)
    _latency_samples: deque[float] = field(default_factory=lambda: deque(maxlen=5000))
    _score_sum: float = 0.0
    _score_min: float = 100.0
    estimated_turnover_count: int = 0
    parse_fallback_count: int = 0

    def assess_price_tick(self, payload: dict[str, Any]) -> RealtimeReliabilityAssessment:
        payload = dict(payload or {})
        metadata = dict(payload.get("metadata") or {})
        if _is_index_payload(payload, metadata):
            return _assess_index_price_tick(payload, metadata)
        present_fields = {field for field in PRICE_TICK_FIELDS if _field_present(field, payload, metadata)}
        missing_fields = tuple(field for field in PRICE_TICK_FIELDS if field not in present_fields)
        field_score = sum(FIELD_RELIABILITY_WEIGHTS.get(field, 0.0) for field in present_fields)
        reason_codes = tuple(_reason_codes(payload, metadata))
        reasons = set(reason_codes)
        if "price" not in present_fields:
            reasons.add("PRICE_MISSING")
        if "volume" not in present_fields:
            reasons.add("VOLUME_MISSING")
        latency_ms = _transport_latency_ms(payload, metadata)
        latency_bucket, latency_penalty = _latency_bucket_penalty(latency_ms)
        penalty = min(60.0, sum(REASON_PENALTIES.get(reason, 0.0) for reason in reasons)) + latency_penalty
        score = round(max(0.0, min(100.0, field_score - penalty)), 2)
        return RealtimeReliabilityAssessment(
            score=score,
            bucket=_reliability_bucket(score),
            field_score=round(field_score, 2),
            penalty=round(penalty, 2),
            reasons=tuple(sorted(reasons)),
            missing_fields=missing_fields,
            transport_latency_ms=round(latency_ms, 3) if latency_ms is not None else None,
            transport_latency_bucket=latency_bucket,
        )

    def observe_price_tick(self, payload: dict[str, Any]) -> RealtimeReliabilityAssessment:
        assessment = self.assess_price_tick(payload)
        self.record_assessment(payload, assessment)
        return assessment

    def record_assessment(
        self,
        payload: dict[str, Any],
        assessment: RealtimeReliabilityAssessment | None = None,
    ) -> RealtimeReliabilityAssessment:
        payload = dict(payload or {})
        metadata = dict(payload.get("metadata") or {})
        assessment = assessment or self.assess_price_tick(payload)
        self.total_price_ticks += 1

        for field in PRICE_TICK_FIELDS:
            if _field_present(field, payload, metadata):
                self._field_counts[field] += 1

        reason_codes = list(assessment.reasons)
        self._reason_code_counts.update(reason_codes)
        self._reliability_bucket_counts.update([assessment.bucket])
        self._reliability_reason_counts.update(assessment.reasons)
        self._score_sum += assessment.score
        self._score_min = min(self._score_min, assessment.score)
        if assessment.transport_latency_ms is not None:
            self._latency_samples.append(float(assessment.transport_latency_ms))
        if "TURNOVER_ESTIMATED" in reason_codes:
            self.estimated_turnover_count += 1
        if "REAL_PARSE_FALLBACK" in reason_codes:
            self.parse_fallback_count += 1
        return assessment

    def snapshot(self) -> dict[str, Any]:
        total = max(1, self.total_price_ticks)
        score_avg = round(self._score_sum / total, 2) if self.total_price_ticks else 0.0
        latency_samples = sorted(self._latency_samples)
        return {
            "total_price_ticks": self.total_price_ticks,
            "field_coverage": {
                field: round(self._field_counts.get(field, 0) / total, 4) if self.total_price_ticks else 0.0
                for field in PRICE_TICK_FIELDS
            },
            "reason_code_counts": dict(sorted(self._reason_code_counts.items())),
            "estimated_turnover_count": self.estimated_turnover_count,
            "parse_fallback_count": self.parse_fallback_count,
            "realtime_reliability_score": score_avg,
            "realtime_reliability_bucket": _reliability_bucket(score_avg) if self.total_price_ticks else "NO_DATA",
            "reliability": {
                "score_avg": score_avg,
                "score_min": round(self._score_min, 2) if self.total_price_ticks else 0.0,
                "bucket": _reliability_bucket(score_avg) if self.total_price_ticks else "NO_DATA",
                "bucket_counts": dict(sorted(self._reliability_bucket_counts.items())),
                "low_reliability_count": self._reliability_bucket_counts.get("LOW", 0) + self._reliability_bucket_counts.get("BROKEN", 0),
                "low_reliability_ratio": round(
                    (self._reliability_bucket_counts.get("LOW", 0) + self._reliability_bucket_counts.get("BROKEN", 0)) / total,
                    4,
                ) if self.total_price_ticks else 0.0,
                "reason_counts": dict(sorted(self._reliability_reason_counts.items())),
                "field_weighted_score": round(
                    sum(
                        FIELD_RELIABILITY_WEIGHTS.get(field, 0.0) * (self._field_counts.get(field, 0) / total)
                        for field in PRICE_TICK_FIELDS
                    ),
                    2,
                ) if self.total_price_ticks else 0.0,
                "transport_latency_ms_avg": round(sum(latency_samples) / len(latency_samples), 3) if latency_samples else None,
                "transport_latency_ms_p95": _percentile(latency_samples, 0.95),
                "transport_latency_sample_count": len(latency_samples),
            },
            "summary": self.summary_line(),
        }

    def summary_line(self) -> str:
        snapshot = {
            field: round(
                self._field_counts.get(field, 0) / max(1, self.total_price_ticks),
                4,
            )
            if self.total_price_ticks
            else 0.0
            for field in PRICE_TICK_FIELDS
        }
        return (
            "REALTIME_DATA_QUALITY "
            f"total_ticks={self.total_price_ticks} "
            f"reliability_score={(self._score_sum / max(1, self.total_price_ticks)):.2f} "
            f"reliability_bucket={_reliability_bucket(self._score_sum / max(1, self.total_price_ticks)) if self.total_price_ticks else 'NO_DATA'} "
            f"coverage_trade_value={snapshot['trade_value']:.2f} "
            f"coverage_execution_strength={snapshot['execution_strength']:.2f} "
            f"coverage_momentum={snapshot['momentum']:.2f} "
            f"estimated_turnover={self.estimated_turnover_count} "
            f"parse_fallback={self.parse_fallback_count}"
        )


def _field_present(field: str, payload: dict[str, Any], metadata: dict[str, Any]) -> bool:
    if field == "price":
        return _number(payload.get("price")) > 0
    if field == "change_rate":
        return "change_rate" in payload and payload.get("change_rate") is not None
    if field == "volume":
        return _number(payload.get("volume", payload.get("cum_volume"))) > 0
    if field == "trade_value":
        return _number(payload.get("trade_value")) > 0
    if field == "execution_strength":
        return _number(payload.get("execution_strength")) > 0
    if field == "best_bid_ask":
        return _number(payload.get("best_bid")) > 0 and _number(payload.get("best_ask")) > 0
    if field == "day_high_low":
        high = payload.get("day_high", payload.get("session_high", metadata.get("session_high")))
        low = payload.get("day_low", payload.get("session_low", metadata.get("session_low")))
        return _number(high) > 0 and _number(low) > 0
    if field == "momentum":
        return all(key in metadata or key in payload for key in ("momentum_1m", "momentum_3m", "momentum_5m"))
    return False


def _assess_index_price_tick(payload: dict[str, Any], metadata: dict[str, Any]) -> RealtimeReliabilityAssessment:
    present_fields = {field for field in INDEX_PRICE_TICK_FIELDS if _field_present(field, payload, metadata)}
    missing_fields = tuple(field for field in INDEX_PRICE_TICK_FIELDS if field not in present_fields)
    field_score = sum(INDEX_FIELD_RELIABILITY_WEIGHTS.get(field, 0.0) for field in present_fields)
    reasons = {reason for reason in _reason_codes(payload, metadata) if reason not in INDEX_IGNORED_REASONS}
    if "price" not in present_fields:
        reasons.add("PRICE_MISSING")
    latency_ms = _transport_latency_ms(payload, metadata)
    latency_bucket, latency_penalty = _latency_bucket_penalty(latency_ms)
    penalty = min(60.0, sum(REASON_PENALTIES.get(reason, 0.0) for reason in reasons)) + latency_penalty
    score = round(max(0.0, min(100.0, field_score - penalty)), 2)
    return RealtimeReliabilityAssessment(
        score=score,
        bucket=_reliability_bucket(score),
        field_score=round(field_score, 2),
        penalty=round(penalty, 2),
        reasons=tuple(sorted(reasons)),
        missing_fields=missing_fields,
        transport_latency_ms=round(latency_ms, 3) if latency_ms is not None else None,
        transport_latency_bucket=latency_bucket,
    )


def _is_index_payload(payload: dict[str, Any], metadata: dict[str, Any]) -> bool:
    instrument_type = str(payload.get("instrument_type") or metadata.get("instrument_type") or "").strip().lower()
    if instrument_type == "index":
        return True
    code = "".join(ch for ch in str(payload.get("code") or "").strip().upper() if ch.isdigit())
    if code in {"001", "101", "000001", "000101"}:
        return True
    real_type = str(metadata.get("real_type") or payload.get("real_type") or "")
    if "업종" in real_type:
        return True
    name = str(payload.get("name") or metadata.get("name") or "").strip().upper()
    return name in {"KOSPI", "KOSDAQ", "코스피", "코스닥"}


def _reason_codes(payload: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    values.extend(metadata.get("reason_codes") or [])
    values.extend(payload.get("reason_codes") or [])
    return sorted({str(value) for value in values if str(value or "").strip()})


def _transport_latency_ms(payload: dict[str, Any], metadata: dict[str, Any]) -> float | None:
    trace = payload.get("transport_trace") or payload.get("trace") or metadata.get("transport_trace") or metadata.get("trace") or {}
    trace = dict(trace) if isinstance(trace, dict) else {}
    start = (
        trace.get("gateway_event_created_at_utc")
        or trace.get("gateway_event_enqueued_at_utc")
        or payload.get("timestamp")
        or metadata.get("broker_tick_timestamp")
    )
    end = (
        trace.get("core_event_received_at_utc")
        or trace.get("core_event_persisted_at_utc")
        or trace.get("gateway_event_post_end_at_utc")
    )
    return _wall_ms(start, end)


def _latency_bucket_penalty(latency_ms: float | None) -> tuple[str, float]:
    if latency_ms is None:
        return "UNKNOWN", 0.0
    if latency_ms <= 1000.0:
        return "STABLE", 0.0
    if latency_ms <= 3000.0:
        return "DELAYED", 4.0
    if latency_ms <= 10000.0:
        return "STALE", 12.0
    return "BROKEN", 25.0


def _reliability_bucket(score: float) -> str:
    if score >= 90.0:
        return "HIGH"
    if score >= 75.0:
        return "MEDIUM"
    if score >= 55.0:
        return "LOW"
    return "BROKEN"


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 3)
    index = min(len(values) - 1, max(0, int(round((len(values) - 1) * ratio))))
    return round(values[index], 3)


def _wall_ms(start: object, end: object) -> float | None:
    start_dt = _parse_datetime(start)
    end_dt = _parse_datetime(end)
    if start_dt is None or end_dt is None:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds() * 1000.0)


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _number(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "").replace("+", "").replace("%", "")
    if not text:
        return 0.0
    try:
        return abs(float(text))
    except (TypeError, ValueError):
        return 0.0
