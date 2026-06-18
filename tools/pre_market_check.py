from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Reboot V2 pre-market Go/No-Go check.")
    parser.add_argument("--core-url", default="http://127.0.0.1:8000", help="Core API base URL")
    parser.add_argument("--token", default="", help="TRADING_CORE_TOKEN value")
    parser.add_argument("--mode", default="observe", choices=["observe", "dry-run", "live-sim"], help="Requested operation mode")
    parser.add_argument("--export", default="", help="Optional JSON export path")
    parser.add_argument("--input-json", default="", help="Read an existing pre-market check JSON report instead of calling Core API")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout seconds")
    args = parser.parse_args(argv)

    try:
        report = _load_report(args)
    except Exception as exc:
        print(f"[NO_GO] 장전 점검 실행 실패: {exc}", file=sys.stderr)
        return 2

    _print_report(report)
    if args.export:
        export_path = Path(args.export)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON export: {export_path}")
    return _exit_code(report)


def _load_report(args: argparse.Namespace) -> dict[str, Any]:
    if args.input_json:
        return json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    base = str(args.core_url or "").rstrip("/")
    params = urllib.parse.urlencode({"mode": args.mode})
    request = urllib.request.Request(f"{base}/api/ops/pre-market-check?{params}")
    if args.token:
        request.add_header("X-Local-Token", args.token)
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, float(args.timeout))) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc


def _print_report(report: dict[str, Any]) -> None:
    decision = str(report.get("go_no_go") or "UNKNOWN")
    print(f"장전 점검: {decision}")
    print(f"거래일: {report.get('trade_date', '-')}  점검시각: {report.get('checked_at', '-')}")
    print(f"요청 모드: {report.get('requested_mode', '-')}")
    print(
        "요약: "
        f"PASS {int(report.get('pass_count') or 0)} / "
        f"WARN {int(report.get('warn_count') or 0)} / "
        f"FAIL {int(report.get('fail_count') or 0)} / "
        f"UNKNOWN {int(report.get('unknown_count') or 0)}"
    )
    message = str(report.get("operator_message_ko") or "")
    action = str(report.get("recommended_action_ko") or "")
    if message:
        print(f"운영 메시지: {message}")
    if action:
        print(f"권고 조치: {action}")

    blocking = list(report.get("blocking_reasons") or [])
    warnings = list(report.get("warning_reasons") or [])
    if blocking:
        print("\n차단 사유:")
        for reason in blocking[:20]:
            print(f"  - {reason}")
    if warnings:
        print("\n주의/수동확인 사유:")
        for reason in warnings[:20]:
            print(f"  - {reason}")

    failed_or_warn = [
        item
        for item in list(report.get("items") or [])
        if str(item.get("status") or "") in {"FAIL", "WARN", "UNKNOWN"}
    ]
    if failed_or_warn:
        print("\n점검 항목:")
        for item in failed_or_warn[:30]:
            label = item.get("label_ko") or item.get("key") or "-"
            status = item.get("status") or "-"
            reason = item.get("reason_code") or "-"
            message = item.get("message_ko") or ""
            print(f"  {status:7} {label:32} {reason} {message}")


def _exit_code(report: dict[str, Any]) -> int:
    decision = str(report.get("go_no_go") or "").upper()
    if decision == "NO_GO":
        return 2
    if decision == "MANUAL_REVIEW_REQUIRED" or str(report.get("summary_status") or "").upper() in {"WARN", "UNKNOWN"}:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
