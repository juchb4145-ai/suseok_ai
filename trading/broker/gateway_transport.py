from __future__ import annotations

import asyncio
import json
import os
import queue
import random
import re
import threading
import time
from dataclasses import dataclass, field, replace
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
    new_trace_id,
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
DEFAULT_PILOT_CONTROL_EVENT_TYPES = {
    "heartbeat",
    "login_status",
    "orderability",
    "condition_load_result",
    "condition_loaded",
    "condition_event",
    "command_started",
    "command_ack",
    "command_failed",
    "rate_limited",
    "gateway_error",
    "error",
}
CONTROL_WS_MESSAGE_TYPES = {
    "ready_for_commands",
    "heartbeat",
    "command_started",
    "command_ack",
    "command_failed",
    "rate_limited",
}
SEND_COMPLETED_DIAGNOSTIC_MESSAGE_TYPES = {
    "heartbeat",
    "command_started",
    "command_ack",
    "command_failed",
    "rate_limited",
}
DEFAULT_PRIORITY_PRICE_TICK_SOURCES = {
    "holding",
    "theme_lab_watchset",
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
    allowed_event_types: set[str] = field(default_factory=lambda: set(DEFAULT_PILOT_CONTROL_EVENT_TYPES))
    price_tick_sample_rate: float = 0.0
    priority_price_tick_codes: set[str] = field(default_factory=set)
    priority_price_tick_sources: set[str] = field(default_factory=lambda: set(DEFAULT_PRIORITY_PRICE_TICK_SOURCES))
    max_queue_size: int = 2000
    outbound_send_burst_size: int = 100
    condition_event_batch_enabled: bool = True
    condition_event_batch_max_size: int = 100
    condition_event_batch_max_wait_ms: float = 200.0
    send_completed_diagnostics_enabled: bool = True
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
        allowed_events_raw = os.environ.get("TRADING_GATEWAY_WEBSOCKET_PILOT_EVENT_TYPES", "")
        allowed_events = (
            {item.strip() for item in allowed_events_raw.split(",") if item.strip()}
            if allowed_events_raw
            else set(DEFAULT_PILOT_CONTROL_EVENT_TYPES)
        )
        priority_codes = _clean_code_set(os.environ.get("TRADING_GATEWAY_WEBSOCKET_PRIORITY_TICK_CODES", ""))
        priority_sources_raw = os.environ.get("TRADING_GATEWAY_WEBSOCKET_PRIORITY_TICK_SOURCES", "")
        priority_sources = (
            {item.strip() for item in priority_sources_raw.split(",") if item.strip()}
            if priority_sources_raw
            else set(DEFAULT_PRIORITY_PRICE_TICK_SOURCES)
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
            allowed_event_types=allowed_events,
            price_tick_sample_rate=_float_env("TRADING_GATEWAY_WEBSOCKET_PRICE_TICK_SAMPLE_RATE", 0.0),
            priority_price_tick_codes=priority_codes,
            priority_price_tick_sources=priority_sources,
            outbound_send_burst_size=_int_env("TRADING_GATEWAY_WEBSOCKET_OUTBOUND_SEND_BURST_SIZE", 100),
            condition_event_batch_enabled=_bool_env("TRADING_GATEWAY_WEBSOCKET_CONDITION_EVENT_BATCH_ENABLED", True),
            condition_event_batch_max_size=_int_env("TRADING_GATEWAY_WEBSOCKET_CONDITION_EVENT_BATCH_MAX_SIZE", 100),
            condition_event_batch_max_wait_ms=_float_env("TRADING_GATEWAY_WEBSOCKET_CONDITION_EVENT_BATCH_MAX_WAIT_MS", 200.0),
            send_completed_diagnostics_enabled=_bool_env("TRADING_GATEWAY_WEBSOCKET_SEND_COMPLETED_DIAGNOSTICS", True),
        )

    def command_allowed(self, command_type: str) -> bool:
        normalized = str(command_type or "")
        if normalized in ORDER_COMMAND_TYPES:
            return bool(self.allow_order_commands and not self.block_order_commands)
        return normalized in self.allowed_commands

    def order_commands_allowed(self) -> bool:
        return self.command_allowed("send_order")

    def event_allowed(self, event_type: str) -> bool:
        normalized = str(event_type or "")
        if normalized == "price_tick":
            return random.random() < min(1.0, max(0.0, float(self.price_tick_sample_rate or 0.0)))
        return normalized in self.allowed_event_types


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
        self.fallback_detail = ""
        self.fallback_at = ""
        self.last_error = ""
        self.last_error_type = ""
        self.last_error_stage = ""
        self.last_error_at = ""
        self.last_error_reconnect_count = 0
        self.last_close_code = ""
        self.last_close_reason = ""
        self._ws_stage = "idle"
        self.last_send_ms = 0.0
        self.last_send_started_at = ""
        self.last_send_completed_at = ""
        self.last_send_completed_message_id = ""
        self.last_send_completed_message_type = ""
        self.last_send_completed_sequence = 0
        self.last_send_completed_payload_size_bytes = 0
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
        self.ws_price_tick_sampled_count = 0
        self.ws_price_tick_fallback_count = 0
        self.ws_event_fallback_count = 0
        self.ws_priority_price_tick_sampled_count = 0
        self.ws_condition_event_batch_queued_count = 0
        self.ws_condition_event_batch_sent_count = 0
        self.ws_condition_event_batched_count = 0
        self.ws_condition_event_batch_coalesced_count = 0
        self.ws_priority_price_tick_codes: dict[str, set[str]] = {}
        self.last_ws_event_at = ""
        self.last_ws_ack_at = ""
        self._sequence = 0
        self._control_outbound: queue.Queue[GatewayWsMessage] = queue.Queue(maxsize=self.policy.max_queue_size)
        self._outbound: queue.Queue[GatewayWsMessage] = queue.Queue(maxsize=self.policy.max_queue_size)
        self._condition_events: queue.Queue[GatewayEvent] = queue.Queue(maxsize=self.policy.max_queue_size)
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
        allowed, priority_tick = self._event_allowed(event)
        if not allowed:
            if self.fallback_client is not None:
                if event.type == "price_tick":
                    self.ws_price_tick_fallback_count += 1
                else:
                    self.ws_event_fallback_count += 1
                return self.fallback_client.post_event(event)
            return {"accepted": False, "queued": False, "transport_mode": self.transport_mode, "reason": "EVENT_TYPE_NOT_ALLOWED"}
        self.start()
        post_start = monotonic_ms()
        try:
            if event.type == "condition_event" and self.policy.condition_event_batch_enabled:
                self._condition_events.put(self._event_for_ws_queue(event), timeout=0.1)
                self.ws_condition_event_batch_queued_count += 1
            else:
                message = self._message_from_event(event)
                self._queue_outbound_message(message)
            self.last_event_post_ms = monotonic_delta_ms(post_start, monotonic_ms()) or 0.0
            self.post_count += 1
            if event.type == "price_tick":
                self.ws_price_tick_sampled_count += 1
                if priority_tick:
                    self.ws_priority_price_tick_sampled_count += 1
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
            "ws_pilot_live_order_blocked": not self.policy.order_commands_allowed(),
            "ws_pilot_order_commands_allowed": self.policy.order_commands_allowed(),
            "ws_connection_state": self.connection_state,
            "ws_session_id": self.ws_session_id,
            "ws_connection_id": self.ws_connection_id,
            "ws_reconnect_count": self.reconnect_count,
            "ws_last_send_ms": round(self.last_send_ms, 3),
            "ws_last_send_started_at": self.last_send_started_at,
            "ws_last_send_completed_at": self.last_send_completed_at,
            "ws_last_send_completed_message_id": self.last_send_completed_message_id,
            "ws_last_send_completed_message_type": self.last_send_completed_message_type,
            "ws_last_send_completed_sequence": self.last_send_completed_sequence,
            "ws_last_send_completed_duration_ms": round(self.last_send_ms, 3),
            "ws_last_send_completed_payload_size_bytes": self.last_send_completed_payload_size_bytes,
            "ws_last_receive_ms": round(self.last_receive_ms, 3),
            "ws_outbound_queue_size": self._control_outbound.qsize() + self._outbound.qsize() + self._condition_events.qsize(),
            "ws_control_outbound_queue_size": self._control_outbound.qsize(),
            "ws_data_outbound_queue_size": self._outbound.qsize(),
            "ws_condition_event_queue_size": self._condition_events.qsize(),
            "ws_command_queue_size": self._commands.qsize(),
            "ws_fallback_state": self.fallback_state,
            "ws_fallback_reason": self.fallback_reason,
            "ws_fallback_detail": self.fallback_detail,
            "ws_fallback_at": self.fallback_at,
            "ws_last_error": self.last_error,
            "ws_last_error_type": self.last_error_type,
            "ws_last_error_stage": self.last_error_stage,
            "ws_last_error_at": self.last_error_at,
            "ws_last_error_reconnect_count": self.last_error_reconnect_count,
            "ws_last_close_code": self.last_close_code,
            "ws_last_close_reason": self.last_close_reason,
            "ws_error_count": self.error_count,
            "ws_session_loss_count": self.session_loss_count,
            "ws_duplicate_ack_count": self.duplicate_ack_count,
            "ws_unknown_ack_count": self.unknown_ack_count,
            "pilot_blocked_order_command_count": self.blocked_order_command_count,
            "ws_price_tick_sample_rate": min(1.0, max(0.0, float(self.policy.price_tick_sample_rate or 0.0))),
            "ws_price_tick_sampled_count": self.ws_price_tick_sampled_count,
            "ws_price_tick_fallback_count": self.ws_price_tick_fallback_count,
            "ws_priority_price_tick_code_count": len(self._priority_price_tick_code_set()),
            "ws_priority_price_tick_codes": sorted(self._priority_price_tick_code_set())[:200],
            "ws_priority_price_tick_sources": sorted(self.policy.priority_price_tick_sources),
            "ws_priority_price_tick_sampled_count": self.ws_priority_price_tick_sampled_count,
            "ws_condition_event_batch_enabled": bool(self.policy.condition_event_batch_enabled),
            "ws_condition_event_batch_max_size": max(1, int(self.policy.condition_event_batch_max_size or 100)),
            "ws_condition_event_batch_max_wait_ms": max(0.0, float(self.policy.condition_event_batch_max_wait_ms or 0.0)),
            "ws_condition_event_batch_queued_count": self.ws_condition_event_batch_queued_count,
            "ws_condition_event_batch_sent_count": self.ws_condition_event_batch_sent_count,
            "ws_condition_event_batched_count": self.ws_condition_event_batched_count,
            "ws_condition_event_batch_coalesced_count": self.ws_condition_event_batch_coalesced_count,
            "ws_event_fallback_count": self.ws_event_fallback_count,
            "last_ws_event_at": self.last_ws_event_at,
            "last_ws_ack_at": self.last_ws_ack_at,
            "fallback": fallback_snapshot,
        }

    def apply_realtime_subscription_update(self, command_type: str, payload: dict[str, Any]) -> None:
        normalized_type = str(command_type or "")
        payload = dict(payload or {})
        if normalized_type == "remove_all_realtime":
            self.ws_priority_price_tick_codes.clear()
            return
        codes = _clean_code_set(payload.get("codes") or [])
        if not codes:
            return
        if normalized_type == "remove_realtime":
            self._remove_dynamic_priority_price_tick_codes(codes)
            return
        if normalized_type != "register_realtime":
            return
        code_sources = _normalized_code_sources(payload.get("code_sources"))
        priority_sources = {str(source) for source in self.policy.priority_price_tick_sources}
        for code in sorted(codes):
            matched_sources = set(code_sources.get(code) or []) & priority_sources
            if matched_sources:
                self.ws_priority_price_tick_codes.setdefault(code, set()).update(matched_sources)

    def _event_allowed(self, event: GatewayEvent) -> tuple[bool, bool]:
        if event.type != "price_tick":
            return self.policy.event_allowed(event.type), False
        if self._priority_price_tick_event(event):
            return True, True
        return self.policy.event_allowed(event.type), False

    def _priority_price_tick_event(self, event: GatewayEvent) -> bool:
        code = _event_stock_code(event)
        payload = dict(event.payload or {})
        metadata = dict(payload.get("metadata") or {})
        if _truthy(metadata.get("ws_priority_tick") or metadata.get("ws_tick_priority")):
            return True
        sources = _string_set(
            metadata.get("subscription_sources")
            or metadata.get("realtime_sources")
            or metadata.get("realtime_source")
        )
        if sources & {str(source) for source in self.policy.priority_price_tick_sources}:
            return True
        return bool(code and code in self._priority_price_tick_code_set())

    def _priority_price_tick_code_set(self) -> set[str]:
        return set(self.policy.priority_price_tick_codes) | set(self.ws_priority_price_tick_codes)

    def _remove_dynamic_priority_price_tick_codes(self, codes: set[str]) -> None:
        for code in codes:
            self.ws_priority_price_tick_codes.pop(code, None)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run_forever())
        except Exception as exc:
            self._record_ws_error(exc, stage=self._ws_stage or "thread")
            self._maybe_fallback("websocket_thread_failed", str(exc))

    async def _run_forever(self) -> None:
        try:
            self._ws_stage = "import_websockets"
            import websockets
        except Exception as exc:
            self._record_ws_error(exc, stage="import_websockets")
            self._maybe_fallback("websockets_dependency_missing", str(exc))
            return

        while not self._stop.is_set() and not self.fallback_active:
            try:
                self.connection_state = "CONNECTING"
                url = _append_token(self.ws_url, self.token)
                self._ws_stage = "connect"
                async with websockets.connect(url, ping_interval=10, close_timeout=2) as ws:
                    self.ws_connection_id = f"ws_conn_{uuid4().hex}"
                    self.connection_state = "CONNECTED"
                    self._ws_stage = "hello_send"
                    await self._send_ws(ws, self._hello_message())
                    hello_deadline = time.monotonic() + 5.0
                    while time.monotonic() < hello_deadline and self.connection_state != "AUTHENTICATED":
                        self._ws_stage = "hello_recv"
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, hello_deadline - time.monotonic()))
                        self._ws_stage = "hello_handle"
                        self._handle_incoming(GatewayWsMessage.from_dict(json.loads(raw)))
                    if self.connection_state != "AUTHENTICATED":
                        raise RuntimeError("hello_ack timeout")
                    self._ws_stage = "connection_loop"
                    await self._connection_loop(ws)
            except Exception as exc:
                self.error_count += 1
                self._record_ws_error(exc, stage=self._ws_stage or self.connection_state.lower())
                if "hello" in self.last_error.lower() and self.policy.fallback_on_auth_failure:
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
        condition_batch: list[GatewayEvent] = []
        condition_batch_started = 0.0
        receiver_task = asyncio.create_task(self._receive_loop(ws))
        try:
            while not self._stop.is_set() and not self.fallback_active:
                if receiver_task.done():
                    receiver_task.result()
                    return
                sent_count = 0
                send_limit = max(1, int(self.policy.outbound_send_burst_size or 100))
                condition_batch_started = self._drain_condition_events(
                    condition_batch,
                    batch_started=condition_batch_started,
                )
                while sent_count < send_limit:
                    message = self._next_control_message()
                    if message is None:
                        break
                    self._ws_stage = f"send:{message.type}"
                    await self._send_ws(ws, message)
                    sent_count += 1
                if sent_count < send_limit and self._condition_batch_due(condition_batch, condition_batch_started):
                    message = self._condition_batch_message(condition_batch)
                    condition_batch = []
                    condition_batch_started = 0.0
                    self._ws_stage = f"send:{message.type}"
                    await self._send_ws(ws, message)
                    sent_count += 1
                while sent_count < send_limit:
                    message = self._next_data_message()
                    if message is None:
                        break
                    self._ws_stage = f"send:{message.type}"
                    await self._send_ws(ws, message)
                    sent_count += 1
                now = time.monotonic()
                if now >= next_heartbeat:
                    self._ws_stage = "send:transport_heartbeat"
                    await self._send_ws(ws, self._heartbeat_message())
                    next_heartbeat = now + max(1.0, self.policy.heartbeat_interval_sec)
                    sent_count += 1
                await asyncio.sleep(0 if sent_count else 0.01)
        finally:
            receiver_task.cancel()
            try:
                await receiver_task
            except asyncio.CancelledError:
                pass

    async def _receive_loop(self, ws) -> None:
        while not self._stop.is_set() and not self.fallback_active:
            try:
                raw = await ws.recv()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._ws_stage = "recv"
                raise
            try:
                message = GatewayWsMessage.from_dict(json.loads(raw))
            except Exception:
                self._ws_stage = "handle_incoming"
                raise
            self._handle_incoming(message)

    async def _send_ws(self, ws, message: GatewayWsMessage) -> None:
        send_started_at = utc_now_ms()
        send_start = monotonic_ms()
        traced_message = _message_with_ws_send_started_trace(
            message,
            {
                "gateway_ws_send_started_at_utc": send_started_at,
                "gateway_ws_send_started_monotonic_ms": send_start,
                "ws_message_sequence": message.sequence,
            },
        )
        serialized = json.dumps(traced_message.to_dict(), ensure_ascii=False, default=str)
        await ws.send(serialized)
        send_completed_at = utc_now_ms()
        send_completed = monotonic_ms()
        self.last_send_ms = monotonic_delta_ms(send_start, send_completed) or 0.0
        self.last_send_started_at = send_started_at
        self.last_send_completed_at = send_completed_at
        self.last_send_completed_message_id = traced_message.message_id
        self.last_send_completed_message_type = traced_message.type
        self.last_send_completed_sequence = traced_message.sequence
        self.last_send_completed_payload_size_bytes = len(serialized.encode("utf-8"))
        if self._send_completed_diagnostic_enabled(traced_message):
            diagnostic = self._send_completed_diagnostic_message(
                traced_message,
                send_started_at=send_started_at,
                send_completed_at=send_completed_at,
                send_started_monotonic_ms=send_start,
                send_completed_monotonic_ms=send_completed,
                send_duration_ms=self.last_send_ms,
                payload_size_bytes=self.last_send_completed_payload_size_bytes,
            )
            await ws.send(json.dumps(diagnostic.to_dict(), ensure_ascii=False, default=str))

    def _send_completed_diagnostic_enabled(self, message: GatewayWsMessage) -> bool:
        return bool(
            self.policy.send_completed_diagnostics_enabled
            and message.type in SEND_COMPLETED_DIAGNOSTIC_MESSAGE_TYPES
        )

    def _send_completed_diagnostic_message(
        self,
        message: GatewayWsMessage,
        *,
        send_started_at: str,
        send_completed_at: str,
        send_started_monotonic_ms: float,
        send_completed_monotonic_ms: float,
        send_duration_ms: float,
        payload_size_bytes: int,
    ) -> GatewayWsMessage:
        return GatewayWsMessage(
            type="transport_send_completed",
            trace_id=message.trace_id,
            source=self.source,
            payload={
                "original_message_id": message.message_id,
                "original_trace_id": message.trace_id,
                "original_type": message.type,
                "sample_message_type": _sample_message_type_for_ws_message(message),
                "original_event_id": message.event_id,
                "original_command_id": message.command_id,
                "original_sequence": message.sequence,
                "gateway_ws_send_started_at_utc": send_started_at,
                "gateway_ws_send_completed_at_utc": send_completed_at,
                "gateway_ws_send_started_monotonic_ms": send_started_monotonic_ms,
                "gateway_ws_send_completed_monotonic_ms": send_completed_monotonic_ms,
                "gateway_ws_send_duration_ms": send_duration_ms,
                "gateway_ws_payload_size_bytes": payload_size_bytes,
                "transport_mode": self.transport_mode,
                "ws_session_id": self.ws_session_id,
                "ws_connection_id": self.ws_connection_id,
            },
            event_id=message.event_id,
            command_id=message.command_id,
            sequence=self._next_sequence(),
            metadata={
                **self._message_metadata(),
                "original_message_id": message.message_id,
                "original_type": message.type,
                "sample_message_type": _sample_message_type_for_ws_message(message),
                "original_sequence": message.sequence,
            },
        )

    def _next_control_message(self) -> GatewayWsMessage | None:
        try:
            return self._control_outbound.get_nowait()
        except queue.Empty:
            return None

    def _next_data_message(self) -> GatewayWsMessage | None:
        try:
            return self._outbound.get_nowait()
        except queue.Empty:
            return None

    def _drain_condition_events(self, pending: list[GatewayEvent], *, batch_started: float) -> float:
        max_size = max(1, int(self.policy.condition_event_batch_max_size or 100))
        while len(pending) < max_size:
            try:
                event = self._condition_events.get_nowait()
            except queue.Empty:
                break
            batch_started = batch_started or time.monotonic()
            if not self._append_condition_batch_event(pending, event):
                self.ws_condition_event_batch_coalesced_count += 1
        return batch_started if pending else 0.0

    def _append_condition_batch_event(self, pending: list[GatewayEvent], event: GatewayEvent) -> bool:
        key = _condition_event_batch_key(event)
        if key:
            for index in range(len(pending) - 1, -1, -1):
                if _condition_event_batch_key(pending[index]) == key:
                    pending[index] = event
                    return False
        pending.append(event)
        return True

    def _condition_batch_due(self, pending: list[GatewayEvent], batch_started: float) -> bool:
        if not pending:
            return False
        max_size = max(1, int(self.policy.condition_event_batch_max_size or 100))
        if len(pending) >= max_size:
            return True
        wait_ms = max(0.0, float(self.policy.condition_event_batch_max_wait_ms or 0.0))
        if wait_ms <= 0.0:
            return True
        if batch_started <= 0.0:
            return False
        return (time.monotonic() - batch_started) * 1000.0 >= wait_ms

    def _condition_batch_message(self, events: list[GatewayEvent]) -> GatewayWsMessage:
        sent_at = utc_now_ms()
        batch_id = new_trace_id("condition_batch")
        batch_size = len(events)
        payload_events: list[dict[str, Any]] = []
        for index, event in enumerate(events):
            traced_event = _event_with_trace(
                event,
                {
                    "gateway_ws_condition_batch_id": batch_id,
                    "gateway_ws_condition_batch_size": batch_size,
                    "gateway_ws_condition_batch_index": index,
                    "gateway_ws_condition_batch_sent_at_utc": sent_at,
                },
            )
            payload_events.append(traced_event.to_dict())
        self.ws_condition_event_batch_sent_count += 1
        self.ws_condition_event_batched_count += batch_size
        self.last_ws_event_at = sent_at
        return GatewayWsMessage(
            type="condition_event_batch",
            trace_id=batch_id,
            source=self.source,
            payload={
                "events": payload_events,
                "count": batch_size,
                "batch_id": batch_id,
                "sent_at": sent_at,
            },
            event_id=batch_id,
            sequence=self._next_sequence(),
            metadata={
                **self._message_metadata(),
                "condition_event_batch_id": batch_id,
                "condition_event_batch_size": batch_size,
            },
        )

    def _handle_incoming(self, message: GatewayWsMessage) -> None:
        receive_start = monotonic_ms()
        self.last_receive_ms = monotonic_delta_ms(receive_start, monotonic_ms()) or 0.0
        if message.type == "hello_ack":
            ack_mode = str(message.payload.get("transport_mode") or message.metadata.get("transport_mode") or "")
            if ack_mode and ack_mode != self.transport_mode and self.connection_state == "CONNECTED":
                return
            self.ws_session_id = str(message.payload.get("websocket_session_id") or message.metadata.get("websocket_session_id") or self.ws_session_id)
            self.connection_state = "AUTHENTICATED"
            self._clear_ws_error()
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

    def _event_for_ws_queue(self, event: GatewayEvent) -> GatewayEvent:
        return _event_with_trace(
            event,
            {
                "gateway_ws_send_queued_at_utc": utc_now_ms(),
                "transport_mode": self.transport_mode,
                "ws_session_id": self.ws_session_id,
                "ws_connection_id": self.ws_connection_id,
                "ws_reconnect_count": self.reconnect_count,
            },
        )

    def _message_from_event(self, event: GatewayEvent) -> GatewayWsMessage:
        event = self._event_for_ws_queue(event)
        if event.type in {"heartbeat", "command_started", "command_ack", "command_failed", "rate_limited"}:
            payload = dict(event.payload or {})
            if event.type == "heartbeat":
                payload = _compact_ws_heartbeat_payload(payload)
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

    def _queue_outbound_message(self, message: GatewayWsMessage) -> None:
        target = self._control_outbound if message.type in CONTROL_WS_MESSAGE_TYPES else self._outbound
        target.put(message, timeout=0.1)

    def _queue_message(self, message_type: str, payload: dict[str, Any]) -> None:
        self._queue_outbound_message(
            GatewayWsMessage(
                type=message_type,
                source=self.source,
                payload=ensure_transport_trace(payload, process="gateway", extra=self._message_metadata()),
                sequence=self._next_sequence(),
                metadata=self._message_metadata(),
            )
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
            type="transport_heartbeat",
            source=self.source,
            payload={
                "transport_mode": self.transport_mode,
                "transport_keepalive": True,
                **_compact_ws_heartbeat_payload(self.snapshot()),
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
            "ws_fallback_detail": self.fallback_detail,
            "ws_last_error": self.last_error,
            "ws_last_error_type": self.last_error_type,
            "ws_last_error_stage": self.last_error_stage,
            "ws_last_error_at": self.last_error_at,
            "ws_last_close_code": self.last_close_code,
            "ws_last_close_reason": self.last_close_reason,
        }

    def _next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    def _maybe_fallback(self, reason: str, detail: str = "") -> None:
        self.fallback_detail = _redact_sensitive(detail or reason)
        self.fallback_at = utc_now_ms()
        if detail and not self.last_error:
            self.last_error = self.fallback_detail
        if self.policy.fallback_to_rest and self.fallback_client is not None:
            self.connection_state = "FALLBACK_REST"
            self.fallback_state = "FALLBACK_REST"
            self.fallback_reason = reason
            return
        self.connection_state = "DEGRADED"
        self.fallback_state = "STOPPED"
        self.fallback_reason = reason

    def _record_ws_error(self, exc: Exception, *, stage: str) -> None:
        self.last_error = _redact_sensitive(str(exc) or repr(exc))
        self.last_error_type = type(exc).__name__
        self.last_error_stage = str(stage or "unknown")
        self.last_error_at = utc_now_ms()
        self.last_error_reconnect_count = self.reconnect_count
        close_code = getattr(exc, "code", None) or getattr(exc, "close_code", None)
        close_reason = getattr(exc, "reason", None) or getattr(exc, "close_reason", None)
        if close_code is None:
            received = getattr(exc, "rcvd", None)
            close_code = getattr(received, "code", None)
            close_reason = close_reason or getattr(received, "reason", None)
        self.last_close_code = "" if close_code is None else str(close_code)
        self.last_close_reason = _redact_sensitive(close_reason or "")

    def _clear_ws_error(self) -> None:
        self.last_error = ""
        self.last_error_type = ""
        self.last_error_stage = ""
        self.last_error_at = ""
        self.last_error_reconnect_count = 0
        self.last_close_code = ""
        self.last_close_reason = ""

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


def _message_with_ws_send_started_trace(message: GatewayWsMessage, trace_updates: dict[str, Any]) -> GatewayWsMessage:
    payload = dict(message.payload or {})
    if message.type == "gateway_event":
        payload = _gateway_event_payload_with_ws_send_trace(message, payload, trace_updates)
    elif message.type == "condition_event_batch":
        payload = _condition_batch_payload_with_ws_send_trace(message, payload, trace_updates)
    elif message.type in {"heartbeat", "transport_heartbeat", "command_started", "command_ack", "command_failed", "rate_limited"}:
        payload = ensure_transport_trace(
            payload,
            trace_id=trace_from_payload(payload).get("trace_id") or message.trace_id,
            process="gateway",
            extra=trace_updates,
        )
    metadata = {**dict(message.metadata or {}), **trace_updates}
    return replace(message, payload=payload, metadata=metadata)


def _sample_message_type_for_ws_message(message: GatewayWsMessage) -> str:
    if message.type == "gateway_event":
        payload = dict(message.payload or {})
        if isinstance(payload.get("event"), dict):
            return str(payload["event"].get("type") or message.type)
        return str(payload.get("type") or message.type)
    return message.type


WS_HEARTBEAT_PAYLOAD_KEYS = {
    "transport_mode",
    "original_transport",
    "transport_keepalive",
    "kiwoom_logged_in",
    "orderable",
    "mode",
    "account",
    "accounts",
    "broker_name",
    "broker_env",
    "server_mode",
    "account_mode",
    "server_gubun",
    "last_error",
    "reconnect_count",
    "gateway_network_last_error",
    "gateway_reconnect_count",
    "gateway_poll_interval_sec",
    "gateway_last_poll_ms",
    "gateway_last_event_post_ms",
    "gateway_event_queue_size",
    "gateway_command_queue_size",
    "gateway_poll_count",
    "gateway_empty_poll_count",
    "gateway_event_post_count",
    "gateway_event_post_error_count",
    "gateway_last_poll_command_count",
    "gateway_event_drain_limit",
    "gateway_transport_metrics",
    "ws_pilot_enabled",
    "ws_pilot_live_order_blocked",
    "ws_pilot_order_commands_allowed",
    "ws_connection_state",
    "ws_session_id",
    "ws_connection_id",
    "ws_reconnect_count",
    "ws_last_send_ms",
    "ws_last_send_started_at",
    "ws_last_send_completed_at",
    "ws_last_send_completed_message_id",
    "ws_last_send_completed_message_type",
    "ws_last_send_completed_sequence",
    "ws_last_send_completed_duration_ms",
    "ws_last_send_completed_payload_size_bytes",
    "ws_last_receive_ms",
    "ws_outbound_queue_size",
    "ws_control_outbound_queue_size",
    "ws_data_outbound_queue_size",
    "ws_condition_event_queue_size",
    "ws_command_queue_size",
    "ws_fallback_state",
    "ws_fallback_reason",
    "ws_fallback_detail",
    "ws_fallback_at",
    "ws_last_error",
    "ws_last_error_type",
    "ws_last_error_stage",
    "ws_last_error_at",
    "ws_last_error_reconnect_count",
    "ws_last_close_code",
    "ws_last_close_reason",
    "ws_error_count",
    "ws_session_loss_count",
    "ws_duplicate_ack_count",
    "ws_unknown_ack_count",
    "pilot_blocked_order_command_count",
    "ws_price_tick_sample_rate",
    "ws_price_tick_sampled_count",
    "ws_price_tick_fallback_count",
    "ws_priority_price_tick_code_count",
    "ws_priority_price_tick_sources",
    "ws_priority_price_tick_sampled_count",
    "ws_condition_event_batch_enabled",
    "ws_condition_event_batch_max_size",
    "ws_condition_event_batch_max_wait_ms",
    "ws_condition_event_batch_queued_count",
    "ws_condition_event_batch_sent_count",
    "ws_condition_event_batched_count",
    "ws_condition_event_batch_coalesced_count",
    "ws_event_fallback_count",
    "last_ws_event_at",
    "last_ws_ack_at",
    "transport_trace",
}


def _compact_ws_heartbeat_payload(payload: dict[str, Any]) -> dict[str, Any]:
    compact = {key: payload[key] for key in WS_HEARTBEAT_PAYLOAD_KEYS if key in payload}
    if "rate_limit" in payload:
        compact["rate_limit_summary"] = _rate_limit_summary(payload.get("rate_limit"))
    if "gateway_transport_metrics" in compact and isinstance(compact["gateway_transport_metrics"], dict):
        compact["gateway_transport_metrics"] = {
            key: compact["gateway_transport_metrics"].get(key)
            for key in ("last_poll_ms", "last_event_post_ms", "empty_poll_rate")
            if key in compact["gateway_transport_metrics"]
        }
    if "accounts" in compact:
        compact["accounts"] = list(compact.get("accounts") or [])[:5]
    compact["ws_heartbeat_compact"] = True
    compact["ws_heartbeat_omitted_fields"] = [
        key
        for key in ("rate_limit", "fallback", "ws_priority_price_tick_codes")
        if key in payload
    ]
    return compact


def _rate_limit_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    commands = value.get("commands")
    if not isinstance(commands, dict):
        return {}
    allowed_count = 0
    limited_count = 0
    max_wait_time_sec = 0.0
    limited_commands: list[str] = []
    for command_type, raw_stats in commands.items():
        if not isinstance(raw_stats, dict):
            continue
        allowed_count += int(raw_stats.get("allowed_count") or 0)
        current_limited = int(raw_stats.get("limited_count") or 0)
        limited_count += current_limited
        wait_time = _safe_float(raw_stats.get("wait_time_sec"))
        if wait_time > max_wait_time_sec:
            max_wait_time_sec = wait_time
        if current_limited:
            limited_commands.append(str(command_type))
    return {
        "command_count": len(commands),
        "allowed_count": allowed_count,
        "limited_count": limited_count,
        "max_wait_time_sec": round(max_wait_time_sec, 3),
        "limited_commands": sorted(limited_commands)[:10],
    }


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _gateway_event_payload_with_ws_send_trace(
    message: GatewayWsMessage,
    payload: dict[str, Any],
    trace_updates: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(payload.get("event"), dict):
        event = dict(payload["event"])
        event_payload = dict(event.get("payload") or {})
        event["payload"] = ensure_transport_trace(
            event_payload,
            trace_id=trace_from_payload(event_payload).get("trace_id") or message.trace_id,
            process="gateway",
            extra=trace_updates,
        )
        payload["event"] = event
        return payload
    event_payload = dict(payload.get("payload") or {})
    payload["payload"] = ensure_transport_trace(
        event_payload,
        trace_id=trace_from_payload(event_payload).get("trace_id") or message.trace_id,
        process="gateway",
        extra=trace_updates,
    )
    return payload


def _condition_batch_payload_with_ws_send_trace(
    message: GatewayWsMessage,
    payload: dict[str, Any],
    trace_updates: dict[str, Any],
) -> dict[str, Any]:
    traced_events: list[dict[str, Any]] = []
    for index, raw_event in enumerate(list(payload.get("events") or [])):
        if not isinstance(raw_event, dict):
            continue
        event = dict(raw_event)
        event_payload = dict(event.get("payload") or {})
        event["payload"] = ensure_transport_trace(
            event_payload,
            trace_id=trace_from_payload(event_payload).get("trace_id") or f"{message.trace_id}:{index}",
            process="gateway",
            extra={**trace_updates, "gateway_ws_condition_batch_index": index},
        )
        traced_events.append(event)
    payload["events"] = traced_events
    return payload


def _append_token(ws_url: str, token: str) -> str:
    separator = "&" if "?" in ws_url else "?"
    return f"{ws_url}{separator}{urlencode({'token': token})}"


def _redact_sensitive(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    return re.sub(r"(?i)(token=|x-local-token=|authorization:\s*bearer\s+)[^&\s]+", r"\1<redacted>", text)


def _ws_url_from_core_url(core_url: str) -> str:
    base = core_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/ws/gateway/transport"


def _event_stock_code(event: GatewayEvent) -> str:
    payload = dict(event.payload or {})
    return next(iter(_clean_code_set([payload.get("code") or payload.get("stock_code") or ""])), "")


def _condition_event_batch_key(event: GatewayEvent) -> str:
    if event.type != "condition_event":
        return ""
    payload = dict(event.payload or {})
    code = next(iter(_clean_code_set([payload.get("code") or payload.get("stock_code") or payload.get("symbol") or ""])), "")
    if not code:
        return ""
    return "|".join(
        [
            _payload_text(payload, "condition_name", "condition"),
            _payload_text(payload, "condition_index", "index"),
            code,
            _payload_text(payload, "event_type", "action").lower(),
            _payload_text(payload, "source"),
            _payload_text(payload, "strategy_profile", "profile"),
            _payload_text(payload, "purpose"),
        ]
    )


def _payload_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _normalized_code_sources(value: Any) -> dict[str, set[str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, set[str]] = {}
    for raw_code, raw_sources in value.items():
        codes = _clean_code_set([raw_code])
        if not codes:
            continue
        result[next(iter(codes))] = _string_set(raw_sources)
    return result


def _clean_code_set(values: Any) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        raw_values = values.split(",")
    else:
        try:
            raw_values = list(values)
        except TypeError:
            raw_values = [values]
    result: set[str] = set()
    for raw in raw_values:
        text = str(raw or "").strip().upper()
        if text.startswith("A") and len(text) == 7:
            text = text[1:]
        code = "".join(ch for ch in text if ch.isdigit())
        if code:
            result.add(code.zfill(6))
    return result


def _string_set(values: Any) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        raw_values = values.split(",")
    else:
        try:
            raw_values = list(values)
        except TypeError:
            raw_values = [values]
    return {str(value).strip() for value in raw_values if str(value).strip()}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


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


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default
