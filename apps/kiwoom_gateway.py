from __future__ import annotations

import argparse
import json
import os
import queue
from pathlib import Path
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trading.broker.gateway_client import GatewayEventQueue
from trading.broker.models import (
    BrokerExecutionEvent,
    BrokerOrderRequest,
    BrokerPriceTick,
    GatewayCommand,
    GatewayEvent,
)
from trading.broker.rate_limit import RateLimiter
from trading.broker.transport_metrics import (
    ensure_transport_trace,
    monotonic_ms,
    monotonic_delta_ms,
    payload_size_bytes,
    trace_from_payload,
    utc_now_ms,
)


@dataclass
class RestCoreClient:
    core_url: str
    token: str
    timeout_sec: float = 5.0
    transport_mode: str = "rest_long_poll"
    metrics_enabled: bool = True
    last_event_post_ms: float = 0.0
    last_poll_ms: float = 0.0
    last_poll_command_count: int = 0
    last_poll_error: str = ""
    poll_count: int = 0
    empty_poll_count: int = 0
    post_count: int = 0
    post_error_count: int = 0
    _session: Any = field(default=None, init=False, repr=False)

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Local-Token": self.token}

    def post_event(self, event: GatewayEvent) -> dict[str, Any]:
        post_start = time.perf_counter()
        event = _event_with_gateway_trace(
            event,
            {
                "gateway_event_post_start_at_utc": utc_now_ms(),
                "gateway_event_post_start_monotonic_ms": monotonic_ms(),
                "gateway_event_payload_size_bytes": payload_size_bytes(event.to_dict()),
                "transport_mode": self.transport_mode,
            },
        )
        payload = event.to_dict()
        try:
            response = self.session.post(
                f"{self.core_url.rstrip('/')}/api/gateway/events",
                json=payload,
                headers=self.headers,
                timeout=self.timeout_sec,
            )
            self.last_event_post_ms = (time.perf_counter() - post_start) * 1000.0
            self.post_count += 1
            response.raise_for_status()
            return dict(response.json())
        except Exception as exc:
            self.last_event_post_ms = (time.perf_counter() - post_start) * 1000.0
            self.post_error_count += 1
            self.last_poll_error = str(exc)
            raise

    def poll_commands(self, *, limit: int = 20, wait_sec: float = 1.0) -> list[GatewayCommand]:
        poll_start = time.perf_counter()
        self.poll_count += 1
        try:
            response = self.session.get(
                f"{self.core_url.rstrip('/')}/api/gateway/commands",
                params={"limit": limit, "wait_sec": wait_sec},
                headers=self.headers,
                timeout=max(self.timeout_sec, wait_sec + 2.0),
            )
            self.last_poll_ms = (time.perf_counter() - poll_start) * 1000.0
            response.raise_for_status()
            payload = response.json()
            items = list(payload.get("commands", []) or [])
            self.last_poll_command_count = len(items)
            if not items:
                self.empty_poll_count += 1
            received_at = utc_now_ms()
            commands = []
            for item in items:
                command = GatewayCommand.from_dict(item)
                commands.append(
                    _command_with_gateway_trace(
                        command,
                        {
                            "gateway_command_polled_at_utc": received_at,
                            "gateway_command_received_at_utc": received_at,
                            "gateway_command_poll_duration_ms": self.last_poll_ms,
                            "gateway_command_response_payload_size_bytes": payload_size_bytes(payload),
                            "transport_mode": self.transport_mode,
                        },
                    )
                )
            self.last_poll_error = ""
            return commands
        except Exception as exc:
            self.last_poll_ms = (time.perf_counter() - poll_start) * 1000.0
            self.last_poll_error = str(exc)
            raise


class GatewayRuntime:
    def __init__(self, core_client: RestCoreClient, *, source: str = "kiwoom_gateway") -> None:
        self.core_client = core_client
        self.source = source
        self.events = GatewayEventQueue(max_size=2000, coalesce_price_ticks=True)
        self.commands: queue.Queue[GatewayCommand] = queue.Queue()
        self.rate_limiter = RateLimiter.from_env()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.last_error = ""
        self.reconnect_count = 0
        self.network_interval_sec = 0.5
        self.drained_event_count = 0
        self.coalesced_tick_count = 0

    def emit(self, event_type: str, payload: dict[str, Any] | None = None, **kwargs) -> None:
        created_at = utc_now_ms()
        traced_payload = ensure_transport_trace(
            dict(payload or {}),
            trace_id=f"trace:{kwargs.get('command_id') or event_type}:{time.time_ns()}",
            process="gateway",
            extra={
                "gateway_event_created_at_utc": created_at,
                "gateway_event_created_monotonic_ms": monotonic_ms(),
                "gateway_event_type": event_type,
                "transport_mode": self.core_client.transport_mode,
            },
        )
        event = GatewayEvent(type=event_type, payload=traced_payload, source=self.source, **kwargs)
        event = _event_with_gateway_trace(
            event,
            {
                "gateway_event_enqueued_at_utc": utc_now_ms(),
                "gateway_event_enqueued_monotonic_ms": monotonic_ms(),
                "gateway_event_queue_size": len(self.events),
            },
        )
        self.events.put(event)

    def start_network_worker(self, *, interval_sec: float = 0.5) -> None:
        if self._worker and self._worker.is_alive():
            return
        self.network_interval_sec = interval_sec
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._network_loop,
            kwargs={"interval_sec": interval_sec},
            name="kiwoom-gateway-network",
            daemon=True,
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=3)

    def _network_loop(self, *, interval_sec: float) -> None:
        while not self._stop.is_set():
            try:
                drained = self.events.drain(limit=100)
                self.drained_event_count += len(drained)
                for event in drained:
                    self.core_client.post_event(event)
                for command in self.core_client.poll_commands(wait_sec=interval_sec):
                    self.commands.put(
                        _command_with_gateway_trace(
                            command,
                            {
                                "gateway_command_local_queued_at_utc": utc_now_ms(),
                                "gateway_command_local_queued_monotonic_ms": monotonic_ms(),
                                "gateway_command_queue_size": self.commands.qsize(),
                            },
                        )
                    )
                self.last_error = ""
            except Exception as exc:
                self.last_error = str(exc)
                self.reconnect_count += 1
                time.sleep(min(5.0, max(1.0, interval_sec)))

    def transport_snapshot(self) -> dict[str, Any]:
        return {
            "transport_mode": self.core_client.transport_mode,
            "gateway_network_last_error": self.last_error or self.core_client.last_poll_error,
            "gateway_reconnect_count": self.reconnect_count,
            "gateway_poll_interval_sec": self.network_interval_sec,
            "gateway_last_poll_ms": round(self.core_client.last_poll_ms, 3),
            "gateway_last_event_post_ms": round(self.core_client.last_event_post_ms, 3),
            "gateway_event_queue_size": len(self.events),
            "gateway_command_queue_size": self.commands.qsize(),
            "gateway_poll_count": self.core_client.poll_count,
            "gateway_empty_poll_count": self.core_client.empty_poll_count,
            "gateway_event_post_count": self.core_client.post_count,
            "gateway_event_post_error_count": self.core_client.post_error_count,
            "gateway_last_poll_command_count": self.core_client.last_poll_command_count,
            "gateway_transport_metrics": {
                "last_poll_ms": round(self.core_client.last_poll_ms, 3),
                "last_event_post_ms": round(self.core_client.last_event_post_ms, 3),
                "empty_poll_rate": (
                    self.core_client.empty_poll_count / self.core_client.poll_count
                    if self.core_client.poll_count
                    else 0.0
                ),
            },
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="32bit Kiwoom Gateway")
    parser.add_argument("--core-url", default="http://127.0.0.1:8000", help="64bit Core/API base URL.")
    parser.add_argument("--token", default="local-dev-token", help="Local gateway token shared with Core.")
    parser.add_argument("--mock", action="store_true", help="Send mock events instead of loading Kiwoom ActiveX.")
    parser.add_argument("--once", action="store_true", help="Send one mock batch and exit.")
    parser.add_argument("--interval-sec", type=float, default=1.0, help="Heartbeat/mock interval.")
    parser.add_argument("--transport", choices=["rest", "websocket-experimental"], default=os.environ.get("TRADING_GATEWAY_TRANSPORT", "rest"))
    parser.add_argument("--poll-wait-sec", type=float, default=float(os.environ.get("TRADING_GATEWAY_POLL_WAIT_SEC", "1.0")))
    parser.add_argument("--network-interval-sec", type=float, default=float(os.environ.get("TRADING_GATEWAY_NETWORK_INTERVAL_SEC", "0.5")))
    parser.add_argument("--ws-url", default=os.environ.get("TRADING_GATEWAY_WS_URL", ""))
    parser.add_argument("--metrics-enabled", action="store_true", default=os.environ.get("TRADING_TRANSPORT_METRICS_ENABLED", "1") != "0")
    parser.add_argument("--metrics-sample-price-tick-rate", type=float, default=float(os.environ.get("TRADING_TRANSPORT_METRICS_SAMPLE_PRICE_TICK_RATE", "0.01")))
    parser.add_argument("--metrics-sample-heartbeat-rate", type=float, default=float(os.environ.get("TRADING_TRANSPORT_METRICS_SAMPLE_HEARTBEAT_RATE", "0.1")))
    return parser.parse_args()


def _build_core_client(args: argparse.Namespace) -> RestCoreClient:
    if args.transport == "websocket-experimental":
        print("websocket-experimental is mock-only in apps/mock_websocket_gateway.py; falling back to REST long-poll.")
    transport_mode = "rest_long_poll"
    return RestCoreClient(
        core_url=args.core_url,
        token=args.token,
        transport_mode=transport_mode,
        metrics_enabled=bool(args.metrics_enabled),
    )


def send_mock_events(core_url: str, token: str) -> list[dict[str, Any]]:
    client = RestCoreClient(core_url=core_url, token=token)
    events = [
        GatewayEvent(
            type="heartbeat",
            source="mock_kiwoom_gateway",
            payload={
                "kiwoom_logged_in": True,
                "orderable": False,
                "mode": "OBSERVE",
                "account": "MOCK-ACCOUNT",
            },
        ),
        GatewayEvent(
            type="price_tick",
            source="mock_kiwoom_gateway",
            payload=BrokerPriceTick(code="005930", price=73200, change_rate=1.25, volume=120000).to_dict(),
        ),
        GatewayEvent(
            type="condition_event",
            source="mock_kiwoom_gateway",
            payload={
                "condition_name": "mock_theme_pullback",
                "condition_index": 1,
                "code": "005930",
                "event_type": "include",
                "source": "condition",
                "purpose": "mock_gateway_flow",
            },
        ),
        GatewayEvent(
            type="execution_event",
            source="mock_kiwoom_gateway",
            payload=BrokerExecutionEvent(
                code="005930",
                order_no="M000001",
                side="buy",
                quantity=1,
                price=73200,
                filled_quantity=1,
                remaining_quantity=0,
                tag="MOCK_EXEC",
            ).to_dict(),
        ),
    ]
    return [client.post_event(event) for event in events]


def run_mock_gateway(args: argparse.Namespace) -> int:
    if args.once:
        results = send_mock_events(args.core_url, args.token)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    runtime = GatewayRuntime(_build_core_client(args), source="mock_kiwoom_gateway")
    runtime.start_network_worker(interval_sec=args.poll_wait_sec or args.interval_sec)
    try:
        while True:
            runtime.emit(
                "heartbeat",
                {
                    "kiwoom_logged_in": True,
                    "orderable": False,
                    "mode": "OBSERVE",
                    "account": "MOCK-ACCOUNT",
                    "last_error": runtime.last_error,
                    "reconnect_count": runtime.reconnect_count,
                    "rate_limit": runtime.rate_limiter.snapshot(),
                    **runtime.transport_snapshot(),
                },
            )
            runtime.emit(
                "price_tick",
                BrokerPriceTick(code="005930", price=73200, change_rate=1.25, volume=120000).to_dict(),
            )
            _drain_mock_commands(runtime)
            time.sleep(max(0.5, args.interval_sec))
    except KeyboardInterrupt:
        runtime.stop()
        return 0


def run_real_gateway(args: argparse.Namespace) -> int:
    from PyQt5.QtCore import QTimer
    from PyQt5.QtWidgets import QApplication

    from kiwoom.client import KiwoomClient

    app = QApplication(sys.argv[:1])
    client = KiwoomClient()
    runtime = GatewayRuntime(_build_core_client(args), source="kiwoom_gateway")
    _wire_kiwoom_signals(client, runtime)
    runtime.start_network_worker(interval_sec=args.poll_wait_sec or args.network_interval_sec or args.interval_sec)

    heartbeat_timer = QTimer()
    heartbeat_timer.timeout.connect(
        lambda: runtime.emit(
            "heartbeat",
            {
                "kiwoom_logged_in": False,
                "orderable": False,
                "mode": "OBSERVE",
                "account": "",
                "last_error": runtime.last_error,
                "reconnect_count": runtime.reconnect_count,
                "rate_limit": runtime.rate_limiter.snapshot(),
                **runtime.transport_snapshot(),
            },
        )
    )
    heartbeat_timer.start(int(max(1.0, args.interval_sec) * 1000))

    command_timer = QTimer()
    command_timer.timeout.connect(lambda: _drain_real_commands(client, runtime))
    command_timer.start(200)

    client.login()
    try:
        return int(app.exec_())
    finally:
        runtime.stop()


def _wire_kiwoom_signals(client, runtime: GatewayRuntime) -> None:
    client.connected.connect(
        lambda ok, code, message: runtime.emit(
            "login_status",
            {"logged_in": bool(ok), "code": int(code), "message": str(message or "")},
        )
    )
    client.price_received.connect(
        lambda code, price, change_rate=0.0, volume=0, best_ask=0, best_bid=0, **kwargs: runtime.emit(
            "price_tick",
            BrokerPriceTick(
                code=str(code),
                price=int(price or 0),
                change_rate=float(change_rate or 0.0),
                volume=int(volume or 0),
                best_ask=int(best_ask or 0),
                best_bid=int(best_bid or 0),
                instrument_type=str(kwargs.get("instrument_type") or "stock"),
                name=str(kwargs.get("name") or ""),
                day_high=int(kwargs.get("day_high") or 0),
                day_low=int(kwargs.get("day_low") or 0),
            ).to_dict(),
        )
    )
    client.order_result.connect(lambda result: runtime.emit("order_result", result.to_dict()))
    client.execution_received.connect(lambda event: runtime.emit("execution_event", event.to_dict()))
    client.message_received.connect(lambda message: runtime.emit("gateway_log", {"message": str(message or "")}))
    client.condition_real_received.connect(
        lambda code, event_type, condition_name, condition_index: runtime.emit(
            "condition_event",
            {
                "code": str(code or ""),
                "condition_name": str(condition_name or ""),
                "condition_index": int(condition_index or -1),
                "event_type": "include" if str(event_type).upper() == "I" else "remove",
                "source": "condition",
            },
        )
    )
    client.condition_tr_received.connect(
        lambda screen_no, code_list, condition_name, condition_index, next_flag: [
            runtime.emit(
                "condition_event",
                {
                    "code": code.strip().replace("A", ""),
                    "condition_name": str(condition_name or ""),
                    "condition_index": int(condition_index or -1),
                    "event_type": "include",
                    "source": "condition",
                },
            )
            for code in str(code_list or "").split(";")
            if code.strip()
        ]
    )


def _drain_real_commands(client, runtime: GatewayRuntime) -> None:
    while True:
        try:
            command = runtime.commands.get_nowait()
        except queue.Empty:
            return
        wait_time = runtime.rate_limiter.wait_time(command.type)
        if wait_time > 0:
            runtime.emit(
                "rate_limited",
                _command_event_payload(
                    command,
                    {
                        "wait_time_sec": round(wait_time, 3),
                        "rate_limit_wait_ms": round(wait_time * 1000.0, 3),
                    },
                ),
                command_id=command.command_id,
            )
            runtime.commands.put(command)
            return
        try:
            start_monotonic = monotonic_ms()
            runtime.emit(
                "command_started",
                _command_event_payload(
                    command,
                    {
                        "gateway_command_started_at_utc": utc_now_ms(),
                        "gateway_command_started_monotonic_ms": start_monotonic,
                        "gateway_local_queue_wait_ms": monotonic_delta_ms(
                            trace_from_payload(command.payload).get("gateway_command_local_queued_monotonic_ms"),
                            start_monotonic,
                        ),
                    },
                ),
                command_id=command.command_id,
            )
            execute_start = monotonic_ms()
            execute_started_at = utc_now_ms()
            result_payload = _execute_command(client, command)
            execute_finished_at = utc_now_ms()
            execute_ms = monotonic_delta_ms(execute_start, monotonic_ms())
            runtime.rate_limiter.record(command.type)
            runtime.emit(
                "command_ack",
                {
                    **_command_event_payload(
                        command,
                        {
                            "gateway_kiwoom_call_started_at_utc": execute_started_at,
                            "gateway_kiwoom_call_finished_at_utc": execute_finished_at,
                            "gateway_execute_ms": execute_ms,
                            "gateway_command_ack_created_at_utc": utc_now_ms(),
                            "gateway_command_ack_created_monotonic_ms": monotonic_ms(),
                        },
                    ),
                    **result_payload,
                    "status": result_payload.get("status", "ACKED"),
                },
                command_id=command.command_id,
            )
        except Exception as exc:
            runtime.emit(
                "command_failed",
                {
                    **_command_event_payload(
                        command,
                        {
                            "gateway_command_failed_at_utc": utc_now_ms(),
                            "gateway_command_ack_created_at_utc": utc_now_ms(),
                        },
                    ),
                    "error": str(exc),
                    "retryable": command.type not in {"send_order", "cancel_order", "modify_order"},
                },
                command_id=command.command_id,
            )


def _execute_command(client, command: GatewayCommand) -> dict[str, Any]:
    payload = dict(command.payload or {})
    if command.type == "login":
        result_code = int(client.login() or 0)
        return _result_payload(result_code=result_code, message="login requested")
    elif command.type == "load_conditions":
        result_code = int(client.load_conditions() or 0)
        return _result_payload(result_code=result_code, message="condition load requested", success_code=1)
    elif command.type == "send_condition":
        result_code = int(client.send_condition(
            str(payload.get("screen_no") or "7600"),
            str(payload.get("condition_name") or ""),
            int(payload.get("condition_index") or 0),
            realtime=bool(payload.get("realtime", True)),
            search_type=payload.get("search_type"),
        ) or 0)
        return _result_payload(result_code=result_code, message="condition sent", success_code=1)
    elif command.type == "register_realtime":
        client.register_realtime(list(payload.get("codes") or []), screen_no=payload.get("screen_no"))
        return _result_payload(result_code=0, message="realtime registered")
    elif command.type == "remove_realtime":
        client.remove_realtime(list(payload.get("codes") or []), screen_no=payload.get("screen_no"))
        return _result_payload(result_code=0, message="realtime removed")
    elif command.type == "remove_all_realtime":
        if hasattr(client, "remove_all_realtime"):
            client.remove_all_realtime()
        else:
            client.remove_realtime([], screen_no=payload.get("screen_no"))
        return _result_payload(result_code=0, message="all realtime removed")
    elif command.type == "stop_condition":
        if not hasattr(client, "stop_condition"):
            return _result_payload(result_code=-1, message="stop_condition unsupported")
        client.stop_condition(
            str(payload.get("screen_no") or "7600"),
            str(payload.get("condition_name") or ""),
            int(payload.get("condition_index") or 0),
        )
        return _result_payload(result_code=0, message="condition stopped")
    elif command.type == "tr_request":
        for key, value in dict(payload.get("inputs") or {}).items():
            client.set_input_value(str(key), str(value))
        result_code = int(client.comm_rq_data(
            str(payload.get("rq_name") or ""),
            str(payload.get("tr_code") or ""),
            int(payload.get("prev_next") or 0),
            str(payload.get("screen_no") or "9000"),
        ) or 0)
        return _result_payload(result_code=result_code, message="tr requested")
    elif command.type == "send_order":
        request = BrokerOrderRequest.from_dict({**payload, "command_id": command.command_id})
        result = client.send_order(request)
        return _order_result_payload(result)
    elif command.type == "cancel_order":
        result = client.cancel_order(
            str(payload.get("account") or ""),
            str(payload.get("code") or ""),
            int(payload.get("quantity") or 0),
            str(payload.get("original_order_no") or ""),
            str(payload.get("tag") or f"CANCEL_{command.command_id}"),
        )
        return _order_result_payload(result)
    elif command.type == "modify_order":
        result = client.modify_buy_order(
            str(payload.get("account") or ""),
            str(payload.get("code") or ""),
            int(payload.get("quantity") or 0),
            int(payload.get("price") or 0),
            str(payload.get("original_order_no") or ""),
            str(payload.get("tag") or f"MODIFY_{command.command_id}"),
        )
        return _order_result_payload(result)
    return {
        "status": "REJECTED",
        "message": f"unsupported command type: {command.type}",
        "result_code": -1,
        "raw": {},
    }


def _drain_mock_commands(runtime: GatewayRuntime) -> None:
    while True:
        try:
            command = runtime.commands.get_nowait()
        except queue.Empty:
            return
        wait_time = runtime.rate_limiter.wait_time(command.type)
        if wait_time > 0:
            runtime.emit(
                "rate_limited",
                _command_event_payload(
                    command,
                    {
                        "wait_time_sec": round(wait_time, 3),
                        "rate_limit_wait_ms": round(wait_time * 1000.0, 3),
                    },
                ),
                command_id=command.command_id,
            )
            runtime.commands.put(command)
            return
        start_monotonic = monotonic_ms()
        runtime.emit(
            "command_started",
            _command_event_payload(
                command,
                {
                    "gateway_command_started_at_utc": utc_now_ms(),
                    "gateway_command_started_monotonic_ms": start_monotonic,
                    "gateway_local_queue_wait_ms": monotonic_delta_ms(
                        trace_from_payload(command.payload).get("gateway_command_local_queued_monotonic_ms"),
                        start_monotonic,
                    ),
                },
            ),
            command_id=command.command_id,
        )
        runtime.rate_limiter.record(command.type)
        execute_start = monotonic_ms()
        execute_started_at = utc_now_ms()
        if command.type == "send_order":
            request = BrokerOrderRequest.from_dict({**dict(command.payload or {}), "command_id": command.command_id})
            result = {
                "status": "ACKED",
                "result_code": 0,
                "message": "mock send_order accepted",
                "order_no": f"MOCK-{command.command_id[-8:]}",
                "order_result": {
                    "ok": True,
                    "code": 0,
                    "message": "mock send_order accepted",
                    "request": request.to_dict(),
                    "order_no": f"MOCK-{command.command_id[-8:]}",
                    "command_id": command.command_id,
                    "idempotency_key": command.idempotency_key,
                    "raw": {"mock": True},
                },
                "raw": {"mock": True},
            }
        else:
            result = {"status": "ACKED", "result_code": 0, "message": f"mock {command.type} accepted", "raw": {"mock": True}}
        execute_finished_at = utc_now_ms()
        execute_ms = monotonic_delta_ms(execute_start, monotonic_ms())
        runtime.emit(
            "command_ack",
            {
                **_command_event_payload(
                    command,
                    {
                        "gateway_kiwoom_call_started_at_utc": execute_started_at,
                        "gateway_kiwoom_call_finished_at_utc": execute_finished_at,
                        "gateway_execute_ms": execute_ms,
                        "gateway_command_ack_created_at_utc": utc_now_ms(),
                        "gateway_command_ack_created_monotonic_ms": monotonic_ms(),
                    },
                ),
                **result,
            },
            command_id=command.command_id,
        )
        runtime.emit(
            "gateway_log",
            {"message": f"mock gateway received command {command.type}", "command": command.to_dict()},
            command_id=command.command_id,
        )


def _event_with_gateway_trace(event: GatewayEvent, trace_updates: dict[str, Any]) -> GatewayEvent:
    payload = ensure_transport_trace(
        event.payload,
        trace_id=trace_from_payload(event.payload).get("trace_id") or f"trace:{event.event_id}",
        process="gateway",
        extra=trace_updates,
    )
    data = event.to_dict()
    data["payload"] = payload
    return GatewayEvent.from_dict(data)


def _command_with_gateway_trace(command: GatewayCommand, trace_updates: dict[str, Any]) -> GatewayCommand:
    payload = ensure_transport_trace(
        command.payload,
        trace_id=trace_from_payload(command.payload).get("trace_id") or f"trace:{command.command_id}",
        process="gateway",
        extra=trace_updates,
    )
    data = command.to_dict()
    data["payload"] = payload
    return GatewayCommand.from_dict(data)


def _command_event_payload(command: GatewayCommand, trace_updates: dict[str, Any] | None = None) -> dict[str, Any]:
    trace = trace_from_payload(command.payload)
    if trace_updates:
        trace.update(trace_updates)
    return {
        "command_id": command.command_id,
        "command_type": command.type,
        "idempotency_key": command.idempotency_key,
        "request_id": command.request_id,
        "transport_trace": trace,
    }


def _result_payload(*, result_code: int, message: str, success_code: int = 0, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    ok = int(result_code) == int(success_code)
    return {
        "status": "ACKED" if ok else "FAILED",
        "result_code": int(result_code),
        "message": message,
        "raw": dict(raw or {}),
    }


def _order_result_payload(result) -> dict[str, Any]:
    payload = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    return {
        "status": "ACKED" if bool(payload.get("ok")) else "FAILED",
        "result_code": int(payload.get("code") or 0),
        "message": str(payload.get("message") or ""),
        "order_no": str(payload.get("order_no") or ""),
        "order_result": payload,
        "raw": dict(payload.get("raw") or {}),
    }


def main() -> int:
    args = parse_args()
    if args.mock:
        return run_mock_gateway(args)
    return run_real_gateway(args)


if __name__ == "__main__":
    raise SystemExit(main())
