from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from trading.broker.models import new_message_id
from trading.broker.transport_metrics import new_trace_id, utc_now_ms


@dataclass(frozen=True)
class GatewayWsMessage:
    type: str
    message_id: str = field(default_factory=lambda: new_message_id("ws"))
    trace_id: str = field(default_factory=lambda: new_trace_id("ws_trace"))
    timestamp: str = field(default_factory=utc_now_ms)
    source: str = "mock_websocket_gateway"
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    command_id: str = ""
    event_id: str = ""
    sequence: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GatewayWsMessage":
        return cls(
            type=str(data.get("type") or ""),
            message_id=str(data.get("message_id") or new_message_id("ws")),
            trace_id=str(data.get("trace_id") or new_trace_id("ws_trace")),
            timestamp=str(data.get("timestamp") or utc_now_ms()),
            source=str(data.get("source") or "mock_websocket_gateway"),
            payload=dict(data.get("payload") or {}),
            metadata=dict(data.get("metadata") or {}),
            command_id=str(data.get("command_id") or ""),
            event_id=str(data.get("event_id") or ""),
            sequence=int(data.get("sequence") or 0),
        )
