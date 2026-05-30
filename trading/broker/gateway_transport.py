from __future__ import annotations

import asyncio
import json
import os
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode
from uuid import uuid4

from trading.broker.models import GatewayCommand, GatewayEvent
from trading.broker.transport_metrics import (
    TRANSPORT_MODE_REST_LONG_POLL,
    TRANSPORT_MODE_WEBSOCKET_REAL_PILOT,
    ensure_transport_trace,
    monotonic_delta_ms,
    monotonic_ms,
    payload_size_bytes,
    trace_from_payload,
    utc_now_ms,
)
from trading.broker.ws_messages import GatewayWsMessage


ORDER_COMMAND_TYPES = {"send_order", "cancel_order", "modify_order"}
DEFAULT_PILOT_ALLOWED_COMMANDS = {
    "login",
    "load_conditions",
    "send_condition",
    "register_realtime",
    "remove_realtime",
    "remove_all_realtime",
    "stop_condition",
    "tr_request",
}


class CoreTransportClient(Protocol):
    transport_mode: str

    def post_event(self, event: GatewayEvent) -> dict[str, Any]:
        ...

    def poll_commands(self, *, limit: int = 20, wait_sec: float = 1.0) -> list[GatewayCommand]:
        ...

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def snapshot(self) -> dict[str, Any]:
        ...


@dataclass
class WebSocketPilotPolicy:
    enabled: bool = False
    allow_real: bool = False
    fallback_to_rest: bool = True
    fallback_after_errors: int = 3
    fallback_after_reconnects: int = 5
    fallback_on_auth_failure: bool = True
    fallback_on_session_loss: bool = True
    block_order_commands: bool = True
    allow_order_commands: bool = False
    allowed_commands: set[str] = field(default_factory=lambda: set(DEFAULT_PILOT_ALLOWED_COMMANDS))
    max_queue_size: int = 2000
    heartbeat_interval_sec: float = 5.0
    reconnect_base_sec: float = 0.5
    reconnect_max_sec: float = 10.0

    @classmethod
    def from_env(cls) -> "WebSocketPilotPolicy":
        allowed_raw = os.environ.get("TRADING_GATEWAY_WEBSOCKET_PILOT_ALLOWED_COMMANDS", "")
        allowed = (
            {item.strip() for item in allowed_raw.split(",") if item.strip()}
            if allowed_raw
            else set(DEFAULT_PILOT_ALLOWED_COMMANDS)
        )
        return cls(
            enabled=_bool_env("TRADING_GATEWAY_WEBSOCKET_REAL_PILOT", False),
            allow_real=_bool_env("TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL", False),
            fallback_to_rest=_bool_env("TRADING_GATEWAY_WEBSOCKET_FALLBACK_TO_REST", True),
            fallback_after_errors=_int_env("TRADING_GATEWAY_WEBSOCKET_FALLBACK_AFTER_ERRORS", 3),
            fallback_after_reconnects=_int_env("TRADING_GATEWAY_WEBSOCKET_FALLBACK_AFTER_RECONNECTS", 5),
            fallback_on_auth_failure=_bool_env("TRADING_GATEWAY_WEBSOCKET_FALLBACK_ON_AUTH_FAILURE", True),
            fallback_on_session_loss=_bool_env("TRADING_GATEWAY_WEBSOCKET_FALLBACK_ON_SESSION_LOSS", True),
            block_order_commands=_bool_env("TRADING_GATEWAY_WEBSOCKET_PILOT_BLOCK_ORDER_COMMANDS", True),
            allow_order_commands=_bool_env("TRADING_GATEWAY_WEBSOCKET_PILOT_ALLOW_ORDER_COMMANDS", False),
            allowed_commands=allowed,
        )

    def command_allowed(self, command_type: str) -> bool:
        normalized = str(command_type or "")
        if normalized in ORDER_COMMAND_TYPES:
            return bool(self.allow_order_commands and not self.block_order_commands)
        return normalized in self.allowed_commands


class WebSocketRealCoreClient:
    transport_mode = TRANSPORT_MODE_WEBSOCKET_REAL_PILOT

    def __init__(
        self,
        *,
        core_url: str,
        ws_url: str,
        token: str,
        fallback_client: CoreTransportClient | None = None,
        policy: WebSocketPilotPolicy | None = None,
        source: str = "kiwoom_gateway",
    ) -> None:
        self.core_url = core_url
        self.ws_url = ws_url or _ws_url_from_core_url(core_url)
        self.token = token
        self.fallback_client = fallback_client
        self.policy = policy or WebSocketPilotPolicy.from_env()
        self.source = source
        self.gateway_version = "websocket-real-pilot"
        self.process_id = os.getpid()
        self.persistent_session_id = f"gw_pilot_{uuid4().hex}"
        self.ws_session_id = ""
        self.ws_connection_id = ""
        self.connection_state = "DISCONNECTED"
        self.fallback_state = ""
        self.fallback_reason = ""
        self.last_error = ""
        self.last_send_ms = 0.0
        self.last_receive_ms = 0.0
        self.last_event_post_ms = 0.0
        self.last_poll_ms = 0.0
        self.last_poll_error = ""
        self.last_poll_command_count = 0
        self.poll_count = 0
        self.empty_poll_count = 0
        self.post_count = 0
        self.post_error_count = 0
        self.reconnect_count = 0
        self.error_count = 0
        self.session_loss_count = 0
        self.duplicate_ack_count = 0
        self.unknown_ack_count = 0
        self.blocked_order_command_count = 0
        self.last_ws_event_at = ""
        self.last_ws_ack_at = ""
        self._sequence = 0
        self._outbound: queue.Queue[GatewayWsMessage] = queue.Queue(maxsize=self.policy.max_queue_size)
        self._commands: queue.Queue[GatewayCommand] = queue.Queue(maxsize=self.policy.max_queue_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._lock = threading.RLock()

    @property
    def fallback_active(self) -> bool:
        return self.connection_state == "FALLBACK_REST" and self.fallback_client is not None

    def start(self) -> None:
        if self.fallback_active:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="gateway-ws-real-pilot", daemon=True)
        self._thread.start()
        self._started.set()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        if self.fallback_client is not None:
            stop = getattr(self.fallback_client, "stop", None)
            if callable(stop):
                stop()
        with self._lock:
            if self.connection_state != "FALLBACK_REST":
                self.connection_state = "STOPPED"

    def post_event(self, event: GatewayEvent) -> dict[str, Any]:
        if self.fallback_active:
            return self.fallback_client.post_event(event)  # type: ignore[union-attr]
        self.start()
        post_start = monotonic_ms()
        try:
            message = self._message_from_event(event)
            self._outbound.put(message, timeout=0.1)
            self.last_event_post_ms = monotonic_delta_ms(post_start, monotonic_ms()) or 0.0
            self.post_count += 1
            return {"accepted": True, "queued": True, "transport_mode": self.transport_mode}
        except Exception as exc:
            self.last_event_post_ms = monotonic_delta_ms(post_start, monotonic_ms()) or 0.0
            self.post_error_count += 1
            self.last_error = str(exc)
            self._maybe_fallback("post_event_failed", str(exc))
            if self.fallback_active:
                return self.fallback_client.post_event(event)  # type: ignore[union-attr]
            raise

    def poll_commands(self, *, limit: int = 20, wait_sec: float = 1.0) -> list[GatewayCommand]:
        if self.fallback_active:
            return self.fallback_client.poll_commands(limit=limit, wait_sec=wait_sec)  # type: ignore[union-attr]
        self.start()
        self.poll_count += 1
        poll_start = monotonic_ms()
        self._queue_message("ready_for_commands", {"limit": int(limit or 20), "wait_sec": float(wait_sec or 0.0)})
        commands: list[GatewayCommand] = []
        deadline = time.monotonic() + max(0.0, min(float(wait_sec or 0.0), 1.0))
        while len(commands) < max(1, int(limit or 20)):
            timeout = max(0.0, min(0.05, deadline - time.monotonic()))
            try:
                command = self._commands.get(timeout=timeout)
                commands.append(command)
            except queue.Empty:
                if time.monotonic() >= deadline:
                    break
        self.last_poll_ms = monotonic_delta_ms(poll_start, monotonic_ms()) or 0.0
        self.last_poll_command_count = len(commands)
        if not commands:
            self.empty_poll_count += 1
        return commands

    def record_blocked_order_command(self) -> None:
        self.blocked_order_command_count += 1

    def snapshot(self) -> dict[str, Any]:
        fallback_snapshot = {}
        if self.fallback_client is not None:
            snapshot = getattr(self.fallback_client, "snapshot", None)
            if callable(snapshot):
                fallback_snapshot = snapshot()
        return {
            "transport_mode": self.transport_mode if not self.fallback_active else "rest_long_poll_fallback",
            "original_transport": self.transport_mode,
            "ws_pilot_enabled": self.policy.enabled,
            "ws_pilot_live_order_blocked": True,
            "ws_connection_state": self.connection_state,
            "ws_session_id": self.ws_session_id,
            "ws_connection_id": self.ws_connection_id,
            "ws_reconnect_count": self.reconnect_count,
            "ws_last_send_ms": round(self.last_send_ms, 3),
            "ws_last_receive_ms": round(self.last_receive_ms, 3),
            "ws_outbound_queue_size": self._outbound.qsize(),
            "ws_command_queue_size": self._commands.qsize(),
            "ws_fallback_state": self.fallback_state,
            "ws_fallback_reason": self.fallback_reason,
            "ws_error_count": self.error_count,
            "ws_session_loss_count": self.session_loss_count,
            "ws_duplicate_ack_count": self.duplicate_ack_count,
            "ws_unknown_ack_count": self.unknown_ack_count,
            "pilot_blocked_order_command_count": self.blocked_order_command_count,
            "last_ws_event_at": self.last_ws_event_at,
            "last_ws_ack_at": self.last_ws_ack_at,
            "fallback": fallback_snapshot,
        }

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run_forever())
        except Exception as exc:
            self.last_error = str(exc)
            self._maybe_fallback("websocket_thread_failed", str(exc))

    async def _run_forever(self) -> None:
        try:
            import websockets
        except Exception as exc:
            self._maybe_fallback("websockets_dependency_missing", str(exc))
            return

        while not self._stop.is_set() and not self.fallback_active:
            try:
                self.connection_state = "CONNECTING"
                url = _append_token(self.ws_url, self.token)
                async with websockets.connect(url, ping_interval=10, close_timeout=2) as ws:
                    self.ws_connection_id = f"ws_conn_{uuid4().hex}"
                    self.connection_state = "CONNECTED"
                    await self._send_ws(ws, self._hello_message())
                    hello_deadline = time.monotonic() + 5.0
                    while time.monotonic() < hello_deadline and self.connection_state != "AUTHENTICATED":
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, hello_deadline - time.monotonic()))
                        self._handle_incoming(GatewayWsMessage.from_dict(json.loads(raw)))
                    if self.connection_state != "AUTHENTICATED":
                        raise RuntimeError("hello_ack timeout")
                    await self._connection_loop(ws)
            except Exception as exc:
                self.error_count += 1
                self.last_error = str(exc)
                if "hello" in str(exc).lower() and self.policy.fallback_on_auth_failure:
                    self._maybe_fallback("auth_failure", str(exc))
                    return
                self.reconnect_count += 1
                if self.reconnect_count >= self.policy.fallback_after_reconnects:
                    self._maybe_fallback("reconnect_limit", str(exc))
                    return
                self.connection_state = "DEGRADED"
                await asyncio.sleep(self._backoff_seconds())

    async def _connection_loop(self, ws) -> None:
        next_heartbeat = 0.0
        while not self._stop.is_set() and not self.fallback_active:
            while True:
                try:
                    message = self._outbound.get_nowait()
                except queue.Empty:
                    break
                await self._send_ws(ws, message)
            now = time.monotonic()
            if now >= next_heartbeat:
                await self._send_ws(ws, self._heartbeat_message())
                next_heartbeat = now + max(1.0, self.policy.heartbeat_interval_sec)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.05)
            except asyncio.TimeoutError:
                continue
            self._handle_incoming(GatewayWsMessage.from_dict(json.loads(raw)))

    async def _send_ws(self, ws, message: GatewayWsMessage) -> None:
        send_start = monotonic_ms()
        await ws.send(json.dumps(message.to_dict(), ensure_ascii=False, default=str))
        self.last_send_ms = monotonic_delta_ms(send_start, monotonic_ms()) or 0.0

    def _handle_incoming(self, message: GatewayWsMessage) -> None:
        receive_start = monotonic_ms()
        self.last_receive_ms = monotonic_delta_ms(receive_start, monotonic_ms()) or 0.0
        if message.type == "hello_ack":
            ack_mode = str(message.payload.get("transport_mode") or message.metadata.get("transport_mode") or "")
            if ack_mode and ack_mode != self.transport_mode and self.connection_state == "CONNECTED":
                return
            self.ws_session_id = str(message.payload.get("websocket_session_id") or message.metadata.get("websocket_session_id") or self.ws_session_id)
            self.connection_state = "AUTHENTICATED"
            return
        if message.type == "pong":
            return
        if message.type != "core_command_batch":
            return
        received_at = utc_now_ms()
        for item in list(message.payload.get("commands") or []):
            command = GatewayCommand.from_dict(item)
            traced = _command_with_trace(
                command,
                {
                    "gateway_command_polled_at_utc": received_at,
                    "gateway_command_received_at_utc": received_at,
                    "gateway_command_ws_received_at_utc": received_at,
                    "gateway_command_response_payload_size_bytes": payload_size_bytes(message.to_dict()),
                    "transport_mode": self.transport_mode,
                    "ws_session_id": self.ws_session_id,
                    "ws_connection_id": self.ws_connection_id,
                    "ws_reconnect_count": self.reconnect_count,
                    "ws_message_sequence": message.sequence,
                },
            )
            self._commands.put(traced, timeout=0.1)

    def _message_from_event(self, event: GatewayEvent) -> GatewayWsMessage:
        event = _event_with_trace(
            event,
            {
                "gateway_ws_send_queued_at_utc": utc_now_ms(),
                "transport_mode": self.transport_mode,
                "ws_session_id": self.ws_session_id,
                "ws_connection_id": self.ws_connection_id,
                "ws_reconnect_count": self.reconnect_count,
            },
        )
        if event.type in {"heartbeat", "command_started", "command_ack", "command_failed", "rate_limited"}:
            payload = dict(event.payload or {})
            message_type = event.type
        else:
            payload = {
                "type": event.type,
                "payload": dict(event.payload or {}),
                "event_id": event.event_id,
                "request_id": event.request_id,
                "command_id": event.command_id,
                "idempotency_key": event.idempotency_key,
            }
            message_type = "gateway_event"
        if event.type in {"command_ack", "command_failed"}:
            self.last_ws_ack_at = utc_now_ms()
        else:
            self.last_ws_event_at = utc_now_ms()
        return GatewayWsMessage(
            type=message_type,
            trace_id=trace_from_payload(event.payload).get("trace_id") or f"trace:{event.event_id}",
            source=self.source,
            payload=payload,
            event_id=event.event_id,
            command_id=event.command_id,
            sequence=self._next_sequence(),
            metadata=self._message_metadata(),
        )

    def _queue_message(self, message_type: str, payload: dict[str, Any]) -> None:
        self._outbound.put(
            GatewayWsMessage(
                type=message_type,
                source=self.source,
                payload=ensure_transport_trace(payload, process="gateway", extra=self._message_metadata()),
                sequence=self._next_sequence(),
                metadata=self._message_metadata(),
            ),
            timeout=0.1,
        )

    def _hello_message(self) -> GatewayWsMessage:
        return GatewayWsMessage(
            type="hello",
            source=self.source,
            payload={
                "source": self.source,
                "transport_mode": self.transport_mode,
                "gateway_version": self.gateway_version,
                "process_id": self.process_id,
                "session_id": self.persistent_session_id,
                "reconnect_count": self.reconnect_count,
                "pilot_enabled": True,
                "live_order_enabled": False,
            },
            sequence=self._next_sequence(),
            metadata=self._message_metadata(),
        )

    def _heartbeat_message(self) -> GatewayWsMessage:
        return GatewayWsMessage(
            type="heartbeat",
            source=self.source,
            payload={
                "transport_mode": self.transport_mode,
                "mode": "OBSERVE",
                "orderable": False,
                "kiwoom_logged_in": False,
                "account": "",
                **self.snapshot(),
            },
            sequence=self._next_sequence(),
            metadata=self._message_metadata(),
        )

    def _message_metadata(self) -> dict[str, Any]:
        return {
            "transport_mode": self.transport_mode,
            "ws_session_id": self.ws_session_id,
            "websocket_session_id": self.ws_session_id,
            "ws_connection_id": self.ws_connection_id,
            "connection_id": self.ws_connection_id,
            "ws_reconnect_count": self.reconnect_count,
            "ws_connection_state": self.connection_state,
            "ws_fallback_reason": self.fallback_reason,
        }

    def _next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    def _maybe_fallback(self, reason: str, detail: str = "") -> None:
        self.last_error = detail or reason
        if self.policy.fallback_to_rest and self.fallback_client is not None:
            self.connection_state = "FALLBACK_REST"
            self.fallback_state = "FALLBACK_REST"
            self.fallback_reason = reason
            return
        self.connection_state = "DEGRADED"
        self.fallback_state = "STOPPED"
        self.fallback_reason = reason

    def _backoff_seconds(self) -> float:
        base = min(self.policy.reconnect_max_sec, self.policy.reconnect_base_sec * (2 ** max(0, self.reconnect_count - 1)))
        return min(self.policy.reconnect_max_sec, base + random.random() * 0.25)


def _event_with_trace(event: GatewayEvent, trace_updates: dict[str, Any]) -> GatewayEvent:
    payload = ensure_transport_trace(
        event.payload,
        trace_id=trace_from_payload(event.payload).get("trace_id") or f"trace:{event.event_id}",
        process="gateway",
        extra=trace_updates,
    )
    data = event.to_dict()
    data["payload"] = payload
    return GatewayEvent.from_dict(data)


def _command_with_trace(command: GatewayCommand, trace_updates: dict[str, Any]) -> GatewayCommand:
    payload = ensure_transport_trace(
        command.payload,
        trace_id=trace_from_payload(command.payload).get("trace_id") or f"trace:{command.command_id}",
        process="gateway",
        extra=trace_updates,
    )
    data = command.to_dict()
    data["payload"] = payload
    return GatewayCommand.from_dict(data)


def _append_token(ws_url: str, token: str) -> str:
    separator = "&" if "?" in ws_url else "?"
    return f"{ws_url}{separator}{urlencode({'token': token})}"


def _ws_url_from_core_url(core_url: str) -> str:
    base = core_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/ws/gateway/transport"


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default
