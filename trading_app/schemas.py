from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from trading.broker.models import GatewayCommand, GatewayEvent, new_message_id, utc_timestamp


class GatewayEventIn(BaseModel):
    type: str
    event_id: Optional[str] = None
    request_id: str = ""
    timestamp: Optional[str] = None
    source: str = "kiwoom_gateway"
    command_id: str = ""
    idempotency_key: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_gateway_event(self) -> GatewayEvent:
        return GatewayEvent(
            type=self.type,
            event_id=self.event_id or new_message_id("evt"),
            request_id=self.request_id,
            timestamp=self.timestamp or utc_timestamp(),
            source=self.source,
            command_id=self.command_id,
            idempotency_key=self.idempotency_key,
            payload=dict(self.payload or {}),
        )


class GatewayCommandIn(BaseModel):
    type: str
    command_id: Optional[str] = None
    request_id: str = ""
    idempotency_key: str = ""
    priority: Optional[str] = None
    ttl_sec: Optional[int] = None
    max_attempts: Optional[int] = None
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_gateway_command(self) -> GatewayCommand:
        return GatewayCommand(
            type=self.type,
            command_id=self.command_id or new_message_id("cmd"),
            request_id=self.request_id,
            idempotency_key=self.idempotency_key,
            payload=dict(self.payload or {}),
        )


class HealthResponse(BaseModel):
    ok: bool
    service: str
    mode: str
    timestamp: str


class GatewayCommandBatch(BaseModel):
    commands: list[dict[str, Any]]
    count: int
    timestamp: str


class OrderEnqueueRequest(BaseModel):
    account: str
    code: str
    side: str
    quantity: int
    price: int
    order_type: int
    hoga: str = "00"
    tag: str = ""
    strategy_name: str = ""
    candidate_id: Optional[int] = None
    reason: str = ""
    idempotency_key: Optional[str] = None
    dry_run: Optional[bool] = None
