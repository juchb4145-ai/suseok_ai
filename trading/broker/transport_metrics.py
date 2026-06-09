from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4


TRANSPORT_MODE_REST_LONG_POLL = "rest_long_poll"
TRANSPORT_MODE_WEBSOCKET_MOCK = "websocket_mock"
TRANSPORT_MODE_WEBSOCKET_REAL_PILOT = "websocket_real_pilot"


def utc_now_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def monotonic_ms() -> float:
    return time.monotonic() * 1000.0


def new_trace_id(prefix: str = "trace") -> str:
    return f"{prefix}_{uuid4().hex}"


def payload_size_bytes(payload: Any) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return 0


def stable_sample(key: str, rate: float) -> bool:
    try:
        normalized_rate = float(rate)
    except (TypeError, ValueError):
        normalized_rate = 0.0
    if normalized_rate >= 1.0:
        return True
    if normalized_rate <= 0.0:
        return False
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return bucket < normalized_rate


def should_sample_transport_message(
    *,
    message_type: str,
    sample_key: str,
    price_tick_rate: float = 0.01,
    heartbeat_rate: float = 0.1,
) -> bool:
    normalized_type = str(message_type or "").lower()
    if normalized_type == "price_tick":
        return stable_sample(sample_key, price_tick_rate)
    if normalized_type == "heartbeat":
        return stable_sample(sample_key, heartbeat_rate)
    return True


def ensure_transport_trace(
    payload: dict[str, Any] | None,
    *,
    trace_id: str | None = None,
    stage: str | None = None,
    process: str | None = None,
    timestamp_key: str | None = None,
    monotonic_key: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = dict(payload or {})
    trace = dict(result.get("transport_trace") or result.get("trace") or {})
    trace.setdefault("trace_id", trace_id or new_trace_id())
    if stage:
        trace["last_stage"] = stage
    if process:
        trace["last_process"] = process
    if timestamp_key:
        trace[timestamp_key] = utc_now_ms()
    if monotonic_key:
        trace[monotonic_key] = monotonic_ms()
    if extra:
        trace.update(extra)
    result["transport_trace"] = trace
    return result


def trace_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    trace = payload.get("transport_trace") or payload.get("trace") or {}
    return dict(trace) if isinstance(trace, dict) else {}


def wall_ms(start: object, end: object) -> Optional[float]:
    start_dt = _parse_datetime(start)
    end_dt = _parse_datetime(end)
    if not start_dt or not end_dt:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds() * 1000.0)


def monotonic_delta_ms(start: object, end: object) -> Optional[float]:
    try:
        start_value = float(start)
        end_value = float(end)
    except (TypeError, ValueError):
        return None
    return max(0.0, end_value - start_value)


def command_or_event_trace_id(message_id: str, message_type: str = "") -> str:
    if message_id:
        return f"trace:{message_id}"
    return new_trace_id(str(message_type or "trace"))


@dataclass(frozen=True)
class TransportTracePoint:
    trace_id: str
    direction: str
    message_type: str
    event_id: str = ""
    command_id: str = ""
    request_id: str = ""
    source: str = ""
    timestamp_utc: str = field(default_factory=utc_now_ms)
    monotonic_ms: float = field(default_factory=monotonic_ms)
    process: str = "core"
    stage: str = ""
    payload_size_bytes: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TransportTracePoint":
        return cls(
            trace_id=str(data.get("trace_id") or new_trace_id()),
            direction=str(data.get("direction") or ""),
            message_type=str(data.get("message_type") or ""),
            event_id=str(data.get("event_id") or ""),
            command_id=str(data.get("command_id") or ""),
            request_id=str(data.get("request_id") or ""),
            source=str(data.get("source") or ""),
            timestamp_utc=str(data.get("timestamp_utc") or utc_now_ms()),
            monotonic_ms=float(data.get("monotonic_ms") or 0.0),
            process=str(data.get("process") or "core"),
            stage=str(data.get("stage") or ""),
            payload_size_bytes=int(data.get("payload_size_bytes") or 0),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class TransportLatencySample:
    sample_id: str
    trace_id: str
    trade_date: str
    direction: str
    message_type: str
    event_id: str = ""
    command_id: str = ""
    request_id: str = ""
    source: str = ""
    success: bool = True
    error: str = ""
    created_at: str = field(default_factory=utc_now_ms)
    completed_at: str = field(default_factory=utc_now_ms)
    payload_size_bytes: int = 0
    stage_ms: dict[str, Any] = field(default_factory=dict)
    total_wall_ms: Optional[float] = None
    gateway_queue_wait_ms: Optional[float] = None
    gateway_post_ms: Optional[float] = None
    core_receive_ms: Optional[float] = None
    core_persist_ms: Optional[float] = None
    core_dispatch_wait_ms: Optional[float] = None
    long_poll_wait_ms: Optional[float] = None
    gateway_receive_wait_ms: Optional[float] = None
    gateway_local_queue_wait_ms: Optional[float] = None
    rate_limit_wait_ms: Optional[float] = None
    gateway_execute_ms: Optional[float] = None
    ack_round_trip_ms: Optional[float] = None
    ws_send_ms: Optional[float] = None
    ws_receive_ms: Optional[float] = None
    ws_reconnect_count: int = 0
    ws_message_sequence: Optional[int] = None
    ws_session_id: str = ""
    ws_connection_id: str = ""
    ws_connection_state: str = ""
    ws_fallback_reason: str = ""
    session_loss_count: int = 0
    duplicate_ack_count: int = 0
    unknown_ack_count: int = 0
    clock_skew_warning: bool = False
    transport_mode: str = TRANSPORT_MODE_REST_LONG_POLL
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.trade_date:
            self.trade_date = str(self.created_at or "")[:10]
        if self.total_wall_ms is not None and self.total_wall_ms < 0:
            self.clock_skew_warning = True
            self.total_wall_ms = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "trace_id": self.trace_id,
            "trade_date": self.trade_date,
            "direction": self.direction,
            "message_type": self.message_type,
            "event_id": self.event_id,
            "command_id": self.command_id,
            "request_id": self.request_id,
            "source": self.source,
            "success": self.success,
            "error": self.error,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "payload_size_bytes": self.payload_size_bytes,
            "stage_ms": dict(self.stage_ms or {}),
            "total_wall_ms": self.total_wall_ms,
            "gateway_queue_wait_ms": self.gateway_queue_wait_ms,
            "gateway_post_ms": self.gateway_post_ms,
            "core_receive_ms": self.core_receive_ms,
            "core_persist_ms": self.core_persist_ms,
            "core_dispatch_wait_ms": self.core_dispatch_wait_ms,
            "long_poll_wait_ms": self.long_poll_wait_ms,
            "gateway_receive_wait_ms": self.gateway_receive_wait_ms,
            "gateway_local_queue_wait_ms": self.gateway_local_queue_wait_ms,
            "rate_limit_wait_ms": self.rate_limit_wait_ms,
            "gateway_execute_ms": self.gateway_execute_ms,
            "ack_round_trip_ms": self.ack_round_trip_ms,
            "ws_send_ms": self.ws_send_ms,
            "ws_receive_ms": self.ws_receive_ms,
            "ws_reconnect_count": self.ws_reconnect_count,
            "ws_message_sequence": self.ws_message_sequence,
            "ws_session_id": self.ws_session_id,
            "ws_connection_id": self.ws_connection_id,
            "ws_connection_state": self.ws_connection_state,
            "ws_fallback_reason": self.ws_fallback_reason,
            "session_loss_count": self.session_loss_count,
            "duplicate_ack_count": self.duplicate_ack_count,
            "unknown_ack_count": self.unknown_ack_count,
            "clock_skew_warning": self.clock_skew_warning,
            "transport_mode": self.transport_mode,
            "metadata": dict(self.metadata or {}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TransportLatencySample":
        metadata_value = _dict_value(data.get("metadata") or data.get("metadata_json") or {})
        return cls(
            sample_id=str(data.get("sample_id") or new_trace_id("lat")),
            trace_id=str(data.get("trace_id") or ""),
            trade_date=str(data.get("trade_date") or ""),
            direction=str(data.get("direction") or ""),
            message_type=str(data.get("message_type") or ""),
            event_id=str(data.get("event_id") or ""),
            command_id=str(data.get("command_id") or ""),
            request_id=str(data.get("request_id") or ""),
            source=str(data.get("source") or ""),
            success=bool(data.get("success", True)),
            error=str(data.get("error") or ""),
            created_at=str(data.get("created_at") or utc_now_ms()),
            completed_at=str(data.get("completed_at") or data.get("created_at") or utc_now_ms()),
            payload_size_bytes=int(data.get("payload_size_bytes") or 0),
            stage_ms=_dict_value(data.get("stage_ms") or data.get("stage_ms_json") or {}),
            total_wall_ms=_optional_float(data.get("total_wall_ms")),
            gateway_queue_wait_ms=_optional_float(data.get("gateway_queue_wait_ms")),
            gateway_post_ms=_optional_float(data.get("gateway_post_ms")),
            core_receive_ms=_optional_float(data.get("core_receive_ms")),
            core_persist_ms=_optional_float(data.get("core_persist_ms")),
            core_dispatch_wait_ms=_optional_float(data.get("core_dispatch_wait_ms")),
            long_poll_wait_ms=_optional_float(data.get("long_poll_wait_ms")),
            gateway_receive_wait_ms=_optional_float(data.get("gateway_receive_wait_ms")),
            gateway_local_queue_wait_ms=_optional_float(data.get("gateway_local_queue_wait_ms")),
            rate_limit_wait_ms=_optional_float(data.get("rate_limit_wait_ms")),
            gateway_execute_ms=_optional_float(data.get("gateway_execute_ms")),
            ack_round_trip_ms=_optional_float(data.get("ack_round_trip_ms")),
            ws_send_ms=_optional_float(data.get("ws_send_ms")),
            ws_receive_ms=_optional_float(data.get("ws_receive_ms")),
            ws_reconnect_count=int(data.get("ws_reconnect_count") or 0),
            ws_message_sequence=(
                int(data.get("ws_message_sequence"))
                if data.get("ws_message_sequence") not in (None, "")
                else None
            ),
            ws_session_id=str(data.get("ws_session_id") or metadata_value.get("ws_session_id") or ""),
            ws_connection_id=str(data.get("ws_connection_id") or metadata_value.get("ws_connection_id") or ""),
            ws_connection_state=str(data.get("ws_connection_state") or metadata_value.get("ws_connection_state") or ""),
            ws_fallback_reason=str(data.get("ws_fallback_reason") or metadata_value.get("ws_fallback_reason") or ""),
            session_loss_count=int(data.get("session_loss_count") or metadata_value.get("session_loss_count") or 0),
            duplicate_ack_count=int(data.get("duplicate_ack_count") or metadata_value.get("duplicate_ack_count") or 0),
            unknown_ack_count=int(data.get("unknown_ack_count") or metadata_value.get("unknown_ack_count") or 0),
            clock_skew_warning=bool(data.get("clock_skew_warning", False)),
            transport_mode=str(data.get("transport_mode") or TRANSPORT_MODE_REST_LONG_POLL),
            metadata=metadata_value,
        )

    @classmethod
    def from_gateway_event_trace(
        cls,
        *,
        event_type: str,
        event_id: str,
        request_id: str = "",
        command_id: str = "",
        source: str = "",
        trace: dict[str, Any] | None = None,
        payload_size: int = 0,
        success: bool = True,
        error: str = "",
        core_receive_ms: Optional[float] = None,
        core_persist_ms: Optional[float] = None,
        metadata: dict[str, Any] | None = None,
    ) -> "TransportLatencySample":
        trace_data = dict(trace or {})
        trace_id = str(trace_data.get("trace_id") or command_or_event_trace_id(event_id, event_type))
        now = utc_now_ms()
        completed_at = str(trace_data.get("core_event_persisted_at_utc") or trace_data.get("core_ack_persisted_at_utc") or now)
        created_at = str(
            trace_data.get("gateway_event_created_at_utc")
            or trace_data.get("gateway_command_ack_created_at_utc")
            or trace_data.get("gateway_command_started_at_utc")
            or completed_at
        )
        total = wall_ms(created_at, completed_at)
        if total is None and core_receive_ms is not None:
            total = core_receive_ms
        ws_send_reference_at = trace_data.get("gateway_ws_condition_batch_sent_at_utc") or trace_data.get("gateway_ws_send_queued_at_utc")
        ws_send_started_at = trace_data.get("gateway_ws_send_started_at_utc")
        ws_send_completed_at = trace_data.get("gateway_ws_send_completed_at_utc")
        stage = {
            "gateway_queue_wait_ms": monotonic_delta_ms(
                trace_data.get("gateway_event_created_monotonic_ms"),
                trace_data.get("gateway_event_enqueued_monotonic_ms"),
            ),
            "gateway_post_ms": monotonic_delta_ms(
                trace_data.get("gateway_event_post_start_monotonic_ms"),
                trace_data.get("gateway_event_post_end_monotonic_ms"),
            ),
            "core_receive_ms": core_receive_ms,
            "core_persist_ms": core_persist_ms,
            "core_condition_event_queue_wait_ms": monotonic_delta_ms(
                trace_data.get("core_condition_event_queued_monotonic_ms"),
                trace_data.get("core_condition_event_worker_started_monotonic_ms"),
            ),
            "core_condition_event_process_ms": _optional_float(trace_data.get("core_condition_event_process_ms")),
            "core_condition_event_stale_include_skip_ms": _optional_float(
                trace_data.get("core_condition_event_stale_include_skip_ms")
            ),
            "core_ws_event_queue_wait_ms": monotonic_delta_ms(
                trace_data.get("core_ws_event_queued_monotonic_ms"),
                trace_data.get("core_ws_event_worker_started_monotonic_ms"),
            ),
            "core_ws_receive_loop_gap_ms": _optional_float(trace_data.get("core_ws_receive_loop_gap_ms")),
            "gateway_ws_queue_to_send_start_ms": wall_ms(ws_send_reference_at, ws_send_started_at),
            "gateway_ws_send_start_to_send_complete_ms": (
                _optional_float(trace_data.get("gateway_ws_send_duration_ms"))
                or wall_ms(ws_send_started_at, ws_send_completed_at)
            ),
            "gateway_ws_send_complete_to_core_receive_ms": wall_ms(ws_send_completed_at, trace_data.get("core_ws_received_at_utc")),
            "gateway_ws_send_start_to_core_receive_ms": wall_ms(ws_send_started_at, trace_data.get("core_ws_received_at_utc")),
            "gateway_ws_to_core_receive_ms": wall_ms(ws_send_reference_at, trace_data.get("core_ws_received_at_utc")),
        }
        command_ack_types = {"command_ack", "command_failed", "command_started", "rate_limited"}
        direction = "gateway_ack_to_core" if event_type in command_ack_types or command_id else "gateway_to_core"
        ack_round_trip = wall_ms(
            trace_data.get("core_command_long_poll_response_at_utc") or trace_data.get("core_command_ws_send_at_utc"),
            completed_at,
        )
        return cls(
            sample_id=new_trace_id("lat"),
            trace_id=trace_id,
            trade_date=str(created_at)[:10],
            direction=direction,
            message_type=event_type,
            event_id=event_id,
            command_id=command_id,
            request_id=request_id,
            source=source,
            success=success,
            error=error,
            created_at=created_at,
            completed_at=completed_at,
            payload_size_bytes=payload_size,
            stage_ms={key: value for key, value in stage.items() if value is not None},
            total_wall_ms=total,
            gateway_queue_wait_ms=stage["gateway_queue_wait_ms"],
            gateway_post_ms=stage["gateway_post_ms"],
            core_receive_ms=core_receive_ms,
            core_persist_ms=core_persist_ms,
            long_poll_wait_ms=_optional_float(trace_data.get("long_poll_wait_ms")),
            gateway_local_queue_wait_ms=_optional_float(trace_data.get("gateway_local_queue_wait_ms")),
            rate_limit_wait_ms=_optional_float(trace_data.get("rate_limit_wait_ms")),
            gateway_execute_ms=_optional_float(trace_data.get("gateway_execute_ms")),
            ack_round_trip_ms=ack_round_trip,
            ws_receive_ms=_optional_float(trace_data.get("ws_receive_ms")),
            ws_send_ms=_optional_float(trace_data.get("ws_send_ms")),
            ws_reconnect_count=int(trace_data.get("ws_reconnect_count") or 0),
            ws_message_sequence=(
                int(trace_data.get("ws_message_sequence"))
                if trace_data.get("ws_message_sequence") not in (None, "")
                else None
            ),
            ws_session_id=str(trace_data.get("ws_session_id") or (metadata or {}).get("ws_session_id") or ""),
            ws_connection_id=str(trace_data.get("ws_connection_id") or (metadata or {}).get("ws_connection_id") or ""),
            ws_connection_state=str(trace_data.get("ws_connection_state") or (metadata or {}).get("ws_connection_state") or ""),
            ws_fallback_reason=str(trace_data.get("ws_fallback_reason") or (metadata or {}).get("ws_fallback_reason") or ""),
            session_loss_count=int(trace_data.get("session_loss_count") or (metadata or {}).get("session_loss_count") or 0),
            duplicate_ack_count=int(trace_data.get("duplicate_ack_count") or (metadata or {}).get("duplicate_ack_count") or 0),
            unknown_ack_count=int(trace_data.get("unknown_ack_count") or (metadata or {}).get("unknown_ack_count") or 0),
            transport_mode=str(trace_data.get("transport_mode") or (metadata or {}).get("transport_mode") or TRANSPORT_MODE_REST_LONG_POLL),
            metadata={**trace_data, **dict(metadata or {})},
        )


@dataclass
class TransportLatencySummary:
    count: int = 0
    success_count: int = 0
    failure_count: int = 0
    p50_ms: float = 0.0
    p90_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0
    avg_ms: float = 0.0
    min_ms: float = 0.0
    timeout_count: int = 0
    error_count: int = 0
    rate_limited_count: int = 0
    sample_window_sec: float = 0.0
    by_direction: dict[str, Any] = field(default_factory=dict)
    by_message_type: dict[str, Any] = field(default_factory=dict)
    by_command_type: dict[str, Any] = field(default_factory=dict)
    by_event_type: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_samples(cls, samples: list[dict[str, Any]], *, value_field: str = "total_wall_ms") -> "TransportLatencySummary":
        values = [_optional_float(sample.get(value_field)) for sample in samples]
        values = [value for value in values if value is not None]
        success_count = sum(1 for sample in samples if bool(sample.get("success", True)))
        failure_count = len(samples) - success_count
        return cls(
            count=len(samples),
            success_count=success_count,
            failure_count=failure_count,
            p50_ms=percentile(values, 50),
            p90_ms=percentile(values, 90),
            p95_ms=percentile(values, 95),
            p99_ms=percentile(values, 99),
            max_ms=max(values) if values else 0.0,
            avg_ms=(sum(values) / len(values)) if values else 0.0,
            min_ms=min(values) if values else 0.0,
            timeout_count=sum(1 for sample in samples if "timeout" in str(sample.get("error") or "").lower()),
            error_count=sum(1 for sample in samples if sample.get("error")),
            rate_limited_count=sum(
                1
                for sample in samples
                if str(sample.get("message_type") or "") == "rate_limited"
                or (_optional_float(sample.get("rate_limit_wait_ms")) or 0.0) > 0
            ),
            sample_window_sec=_sample_window_sec(samples),
        )


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(float(value) for value in values)
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * (float(percentile_value) / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _optional_float(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dict_value(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _parse_datetime(value: object) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value)
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sample_window_sec(samples: list[dict[str, Any]]) -> float:
    timestamps = [_parse_datetime(sample.get("created_at")) for sample in samples]
    timestamps = [item for item in timestamps if item is not None]
    if len(timestamps) < 2:
        return 0.0
    return max(0.0, (max(timestamps) - min(timestamps)).total_seconds())
