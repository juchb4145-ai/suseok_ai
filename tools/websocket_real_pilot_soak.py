from __future__ import annotations

import argparse
import json
import time
from typing import Any

import requests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Observe WebSocket real Gateway pilot soak metrics from Core API.")
    parser.add_argument("--core-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default="local-dev-token")
    parser.add_argument("--duration-sec", type=float, default=3600.0)
    parser.add_argument("--interval-sec", type=float, default=30.0)
    parser.add_argument("--transport-mode", default="websocket_real_pilot")
    parser.add_argument("--fail-on-duplicate-ack", action="store_true")
    parser.add_argument("--fail-on-session-loss", action="store_true")
    parser.add_argument("--max-reconnect-count", type=int, default=3)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    deadline = time.monotonic() + max(1.0, args.duration_sec)
    headers = {"X-Local-Token": args.token}
    latest: dict[str, Any] = {}
    samples: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = _get_json(args.core_url, "/api/gateway/transport/websocket-pilot/status", headers)
        samples = _get_json(
            args.core_url,
            f"/api/gateway/transport/latency/summary?group_by=message_type&transport_mode={args.transport_mode}",
            headers,
        )
        print(json.dumps(_summary(latest, samples), ensure_ascii=False, default=str))
        time.sleep(max(1.0, args.interval_sec))
    result = _summary(latest, samples)
    failed = False
    if args.fail_on_duplicate_ack and int(result.get("duplicate_ack_count") or 0) > 0:
        failed = True
    if args.fail_on_session_loss and int(result.get("session_loss_count") or 0) > 0:
        failed = True
    if int(result.get("reconnect_count") or 0) > args.max_reconnect_count:
        failed = True
    result["status"] = "FAIL" if failed else "PASS"
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 1 if failed else 0


def _get_json(core_url: str, path: str, headers: dict[str, str]) -> dict[str, Any]:
    response = requests.get(f"{core_url.rstrip('/')}{path}", headers=headers, timeout=10)
    response.raise_for_status()
    return dict(response.json())


def _summary(status: dict[str, Any], latency: dict[str, Any]) -> dict[str, Any]:
    summary = dict(latency.get("summary") or {})
    return {
        "enabled": status.get("enabled", False),
        "connected": status.get("connected", False),
        "state": status.get("state", ""),
        "ws_session_id": status.get("ws_session_id", ""),
        "reconnect_count": status.get("reconnect_count", 0),
        "session_loss_count": status.get("session_loss_count", 0),
        "duplicate_ack_count": status.get("duplicate_ack_count", 0),
        "unknown_ack_count": status.get("unknown_ack_count", 0),
        "fallback_reason": status.get("fallback_reason", ""),
        "sample_count": summary.get("count", 0),
        "event_p95_ms": summary.get("event_latency_p95_ms", 0),
        "command_p95_ms": summary.get("command_latency_p95_ms", 0),
        "ack_p95_ms": summary.get("ack_latency_p95_ms", 0),
        "recommendation": (latency.get("websocket_recommendation") or {}).get("recommendation", ""),
    }


if __name__ == "__main__":
    raise SystemExit(main())
