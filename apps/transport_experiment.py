from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import requests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare REST long-poll and mock WebSocket transport metrics.")
    parser.add_argument("--core-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default="local-dev-token")
    parser.add_argument("--scenario", default="command-heavy")
    parser.add_argument("--experiment-id", default="")
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--export", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    experiment_id = args.experiment_id or f"exp_compare_{int(time.time())}"
    base = args.core_url.rstrip("/")
    headers = {"X-Local-Token": args.token}
    health = requests.get(f"{base}/health", timeout=5)
    health.raise_for_status()
    params: dict[str, Any] = {
        "experiment_id": experiment_id,
        "scenario": args.scenario,
        "persist": "true",
        "export": "true" if args.export else "false",
    }
    if args.trade_date:
        params["trade_date"] = args.trade_date
    response = requests.post(f"{base}/api/gateway/transport/experiments/rebuild", params=params, headers=headers, timeout=30)
    response.raise_for_status()
    payload = response.json()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
