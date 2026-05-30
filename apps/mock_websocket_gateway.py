from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trading.broker.models import BrokerExecutionEvent, BrokerOrderRequest, BrokerPriceTick, GatewayCommand
from trading.broker.transport_metrics import TRANSPORT_MODE_WEBSOCKET_MOCK, ensure_transport_trace, monotonic_ms, utc_now_ms
from trading.broker.ws_messages import GatewayWsMessage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mock Gateway WebSocket transport experiment")
    parser.add_argument("--core-url", default="http://127.0.0.1:8000")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8000/ws/gateway/transport")
    parser.add_argument("--token", default="local-dev-token")
    parser.add_argument("--scenario", choices=["basic", "burst", "command-heavy", "event-heavy", "reconnect", "ack-failure"], default="basic")
    parser.add_argument("--duration-sec", type=float, default=30.0)
    parser.add_argument("--event-rate-per-sec", type=float, default=1.0)
    parser.add_argument("--command-delay-ms", type=float, default=20.0)
    parser.add_argument("--ack-failure-rate", type=float, default=0.0)
    parser.add_argument("--reconnect-every-sec", type=float, default=0.0)
    parser.add_argument("--experiment-id", default="")
    parser.add_argument("--emit-price-ticks", action="store_true", default=True)
    parser.add_argument("--emit-condition-events", action="store_true", default=True)
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("websockets package is required for mock websocket gateway experiments") from exc

    experiment_id = args.experiment_id or f"exp_ws_{int(time.time())}"
    deadline = time.monotonic() + max(1.0, args.duration_sec)
    reconnect_count = 0
    while time.monotonic() < deadline:
        ws_url = f"{args.ws_url}?token={args.token}"
        async with websockets.connect(ws_url, ping_interval=10) as ws:
            sequence = 1
            await _send(ws, "hello", {}, args, experiment_id, sequence, reconnect_count)
            await ws.recv()
            next_event_at = 0.0
            next_ready_at = 0.0
            connected_at = time.monotonic()
            while time.monotonic() < deadline:
                if args.reconnect_every_sec > 0 and time.monotonic() - connected_at >= args.reconnect_every_sec:
                    reconnect_count += 1
                    break
                now = time.monotonic()
                if now >= next_event_at:
                    sequence += 1
                    await _emit_mock_events(ws, args, experiment_id, sequence, reconnect_count)
                    interval = 1.0 / max(0.1, args.event_rate_per_sec)
                    next_event_at = now + interval
                if now >= next_ready_at:
                    sequence += 1
                    await _send(ws, "ready_for_commands", {"limit": 20}, args, experiment_id, sequence, reconnect_count)
                    next_ready_at = now + (0.1 if args.scenario == "command-heavy" else 0.5)
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                message = GatewayWsMessage.from_dict(json.loads(raw))
                if message.type == "core_command_batch":
                    for command_raw in message.payload.get("commands", []):
                        command = GatewayCommand.from_dict(command_raw)
                        await _execute_mock_command(ws, command, args, experiment_id, sequence, reconnect_count)


async def _emit_mock_events(ws, args: argparse.Namespace, experiment_id: str, sequence: int, reconnect_count: int) -> None:
    await _send(
        ws,
        "heartbeat",
        {
            "kiwoom_logged_in": True,
            "orderable": False,
            "mode": "OBSERVE",
            "account": "MOCK-ACCOUNT",
            "transport_mode": TRANSPORT_MODE_WEBSOCKET_MOCK,
            "reconnect_count": reconnect_count,
        },
        args,
        experiment_id,
        sequence,
        reconnect_count,
    )
    if args.emit_price_ticks:
        await _send(
            ws,
            "gateway_event",
            {
                "type": "price_tick",
                "payload": BrokerPriceTick(code="005930", price=73000 + random.randint(-50, 50), volume=1000).to_dict(),
            },
            args,
            experiment_id,
            sequence + 1,
            reconnect_count,
        )
    if args.emit_condition_events:
        await _send(
            ws,
            "gateway_event",
            {
                "type": "condition_event",
                "payload": {
                    "condition_name": "mock_ws_condition",
                    "condition_index": 1,
                    "code": "005930",
                    "event_type": "include",
                    "source": "condition",
                },
            },
            args,
            experiment_id,
            sequence + 2,
            reconnect_count,
        )


async def _execute_mock_command(
    ws,
    command: GatewayCommand,
    args: argparse.Namespace,
    experiment_id: str,
    sequence: int,
    reconnect_count: int,
) -> None:
    base_payload = {
        "command_id": command.command_id,
        "command_type": command.type,
        "idempotency_key": command.idempotency_key,
        "request_id": command.request_id,
        "transport_trace": dict(command.payload.get("transport_trace") or {}),
    }
    await _send(ws, "command_started", base_payload, args, experiment_id, sequence, reconnect_count, command_id=command.command_id)
    await asyncio.sleep(max(0.0, args.command_delay_ms) / 1000.0)
    if random.random() < args.ack_failure_rate or args.scenario == "ack-failure":
        await _send(
            ws,
            "command_failed",
            {**base_payload, "error": "mock ack failure", "retryable": command.type not in {"send_order", "cancel_order", "modify_order"}},
            args,
            experiment_id,
            sequence + 1,
            reconnect_count,
            command_id=command.command_id,
        )
        return
    result: dict[str, Any] = {"status": "ACKED", "result_code": 0, "message": f"mock websocket {command.type} accepted", "raw": {"mock": True}}
    if command.type == "send_order":
        request = BrokerOrderRequest.from_dict({**dict(command.payload or {}), "command_id": command.command_id})
        result["order_no"] = f"MOCKWS-{command.command_id[-8:]}"
        result["order_result"] = {
            "ok": True,
            "code": 0,
            "message": result["message"],
            "request": request.to_dict(),
            "order_no": result["order_no"],
            "command_id": command.command_id,
            "idempotency_key": command.idempotency_key,
            "raw": {"mock": True, "transport": "websocket"},
        }
    await _send(
        ws,
        "command_ack",
        {
            **base_payload,
            **result,
            "transport_trace": {
                **base_payload["transport_trace"],
                "gateway_command_ack_created_at_utc": utc_now_ms(),
                "gateway_execute_ms": args.command_delay_ms,
            },
        },
        args,
        experiment_id,
        sequence + 1,
        reconnect_count,
        command_id=command.command_id,
    )


async def _send(
    ws,
    message_type: str,
    payload: dict[str, Any],
    args: argparse.Namespace,
    experiment_id: str,
    sequence: int,
    reconnect_count: int,
    *,
    command_id: str = "",
) -> None:
    traced_payload = ensure_transport_trace(
        payload,
        process="gateway",
        extra={
            "transport_mode": TRANSPORT_MODE_WEBSOCKET_MOCK,
            "experiment_id": experiment_id,
            "scenario": args.scenario,
            "gateway_ws_send_at_utc": utc_now_ms(),
            "gateway_ws_send_monotonic_ms": monotonic_ms(),
            "ws_reconnect_count": reconnect_count,
            "ws_message_sequence": sequence,
        },
    )
    message = GatewayWsMessage(
        type=message_type,
        source="mock_websocket_gateway",
        payload=traced_payload,
        command_id=command_id or str(payload.get("command_id") or ""),
        sequence=sequence,
        metadata={
            "experiment_id": experiment_id,
            "scenario": args.scenario,
            "transport_mode": TRANSPORT_MODE_WEBSOCKET_MOCK,
            "ws_reconnect_count": reconnect_count,
        },
    )
    await ws.send(json.dumps(message.to_dict(), ensure_ascii=False, default=str))


def main() -> int:
    asyncio.run(run(parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
