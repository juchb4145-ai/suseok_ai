from __future__ import annotations

import argparse
import json
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


@dataclass
class RestCoreClient:
    core_url: str
    token: str
    timeout_sec: float = 5.0
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
        response = self.session.post(
            f"{self.core_url.rstrip('/')}/api/gateway/events",
            json=event.to_dict(),
            headers=self.headers,
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        return dict(response.json())

    def poll_commands(self, *, limit: int = 20, wait_sec: float = 1.0) -> list[GatewayCommand]:
        response = self.session.get(
            f"{self.core_url.rstrip('/')}/api/gateway/commands",
            params={"limit": limit, "wait_sec": wait_sec},
            headers=self.headers,
            timeout=max(self.timeout_sec, wait_sec + 2.0),
        )
        response.raise_for_status()
        payload = response.json()
        return [GatewayCommand.from_dict(item) for item in payload.get("commands", [])]


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

    def emit(self, event_type: str, payload: dict[str, Any] | None = None, **kwargs) -> None:
        self.events.put(GatewayEvent(type=event_type, payload=dict(payload or {}), source=self.source, **kwargs))

    def start_network_worker(self, *, interval_sec: float = 0.5) -> None:
        if self._worker and self._worker.is_alive():
            return
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
                for event in self.events.drain(limit=100):
                    self.core_client.post_event(event)
                for command in self.core_client.poll_commands(wait_sec=interval_sec):
                    self.commands.put(command)
                self.last_error = ""
            except Exception as exc:
                self.last_error = str(exc)
                self.reconnect_count += 1
                time.sleep(min(5.0, max(1.0, interval_sec)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="32bit Kiwoom Gateway")
    parser.add_argument("--core-url", default="http://127.0.0.1:8000", help="64bit Core/API base URL.")
    parser.add_argument("--token", default="local-dev-token", help="Local gateway token shared with Core.")
    parser.add_argument("--mock", action="store_true", help="Send mock events instead of loading Kiwoom ActiveX.")
    parser.add_argument("--once", action="store_true", help="Send one mock batch and exit.")
    parser.add_argument("--interval-sec", type=float, default=1.0, help="Heartbeat/mock interval.")
    return parser.parse_args()


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

    runtime = GatewayRuntime(RestCoreClient(args.core_url, args.token), source="mock_kiwoom_gateway")
    runtime.start_network_worker(interval_sec=args.interval_sec)
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
    runtime = GatewayRuntime(RestCoreClient(args.core_url, args.token), source="kiwoom_gateway")
    _wire_kiwoom_signals(client, runtime)
    runtime.start_network_worker(interval_sec=args.interval_sec)

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
                {
                    "command_id": command.command_id,
                    "command_type": command.type,
                    "wait_time_sec": round(wait_time, 3),
                },
                command_id=command.command_id,
            )
            runtime.commands.put(command)
            return
        try:
            runtime.emit("command_started", _command_event_payload(command), command_id=command.command_id)
            result_payload = _execute_command(client, command)
            runtime.rate_limiter.record(command.type)
            runtime.emit(
                "command_ack",
                {
                    **_command_event_payload(command),
                    **result_payload,
                    "status": result_payload.get("status", "ACKED"),
                },
                command_id=command.command_id,
            )
        except Exception as exc:
            runtime.emit(
                "command_failed",
                {
                    **_command_event_payload(command),
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
                {
                    "command_id": command.command_id,
                    "command_type": command.type,
                    "wait_time_sec": round(wait_time, 3),
                },
                command_id=command.command_id,
            )
            runtime.commands.put(command)
            return
        runtime.emit("command_started", _command_event_payload(command), command_id=command.command_id)
        runtime.rate_limiter.record(command.type)
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
        runtime.emit(
            "command_ack",
            {**_command_event_payload(command), **result},
            command_id=command.command_id,
        )
        runtime.emit(
            "gateway_log",
            {"message": f"mock gateway received command {command.type}", "command": command.to_dict()},
            command_id=command.command_id,
        )


def _command_event_payload(command: GatewayCommand) -> dict[str, Any]:
    return {
        "command_id": command.command_id,
        "command_type": command.type,
        "idempotency_key": command.idempotency_key,
        "request_id": command.request_id,
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
