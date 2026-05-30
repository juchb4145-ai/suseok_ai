from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Core/Gateway 운영 상태를 빠르게 점검합니다.")
    parser.add_argument("--core-url", default="http://127.0.0.1:8000", help="Core/API base URL")
    parser.add_argument("--token", default="", help="X-Local-Token. 조회만 할 때는 없어도 됩니다.")
    parser.add_argument("--timeout-sec", type=float, default=5.0)
    parser.add_argument("--json", action="store_true", help="원본 JSON을 출력합니다.")
    parser.add_argument("--fail-on-critical", action="store_true", help="긴급 알림이 있으면 exit code 2로 종료합니다.")
    parser.add_argument("--fail-on-warning", action="store_true", help="주의 이상 알림이 있으면 exit code 3으로 종료합니다.")
    return parser.parse_args()


def api_get(core_url: str, path: str, *, token: str = "", timeout_sec: float = 5.0) -> dict[str, Any]:
    url = urljoin(core_url.rstrip("/") + "/", path.lstrip("/"))
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Local-Token"] = token
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout_sec) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    args = parse_args()
    try:
        alerts = api_get(args.core_url, "/api/ops/alerts", token=args.token, timeout_sec=args.timeout_sec)
        gateway = api_get(args.core_url, "/api/gateway/status", token=args.token, timeout_sec=args.timeout_sec)
        ws_pilot = api_get(
            args.core_url,
            "/api/gateway/transport/websocket-pilot/status",
            token=args.token,
            timeout_sec=args.timeout_sec,
        )
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        print(f"[긴급] Core API에 연결할 수 없습니다: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"alerts": alerts, "gateway": gateway, "websocket_pilot": ws_pilot}, ensure_ascii=False, indent=2))
    else:
        print_summary(alerts, gateway, ws_pilot)

    summary = alerts.get("summary") or {}
    if args.fail_on_critical and int(summary.get("critical") or 0) > 0:
        return 2
    if args.fail_on_warning and (int(summary.get("critical") or 0) > 0 or int(summary.get("warning") or 0) > 0):
        return 3
    return 0


def print_summary(alerts: dict[str, Any], gateway: dict[str, Any], ws_pilot: dict[str, Any]) -> None:
    summary = alerts.get("summary") or {}
    print("== 자동매매 운영 점검 ==")
    print(f"- Gateway: {gateway.get('connection_state')} / heartbeat_ok={gateway.get('heartbeat_ok')} / age={gateway.get('heartbeat_age_sec')}s")
    print(f"- Kiwoom: logged_in={gateway.get('kiwoom_logged_in')} / orderable={gateway.get('orderable')} / account={gateway.get('account') or '-'}")
    print(
        "- WebSocket Pilot: "
        f"enabled={ws_pilot.get('enabled')} / connected={ws_pilot.get('connected')} / "
        f"state={ws_pilot.get('state')} / reconnect={ws_pilot.get('reconnect_count')}"
    )
    print(
        "- 알림: "
        f"긴급 {summary.get('critical', 0)} / 주의 {summary.get('warning', 0)} / 정보 {summary.get('info', 0)} "
        f"/ 데이터 수집 {'가능' if summary.get('safe_to_collect_data') else '확인 필요'}"
    )
    print("- LIVE 자동주문: 차단")
    items = alerts.get("alerts") or []
    if not items:
        print("- 현재 표시할 운영 알림이 없습니다.")
        return
    print("\n상위 알림:")
    for item in items[:8]:
        print(f"- [{item.get('severity')}] {item.get('title')}: {item.get('message')}")
        if item.get("action"):
            print(f"  조치: {item.get('action')}")


if __name__ == "__main__":
    raise SystemExit(main())
