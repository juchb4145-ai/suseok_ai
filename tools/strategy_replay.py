from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trading_app.dependencies import get_settings
from trading_app.strategy_replay import (
    DEFAULT_BUNDLE_ROOT,
    DEFAULT_REPLAY_DB_ROOT,
    StrategyReplayBundleExporter,
    StrategyRuntimeReplayRunner,
    get_replay_run_detail,
    scan_replay_reports,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export and run suseok_ai strategy replay bundles.")
    parser.add_argument("--db-path", default="", help="source operating DB path; defaults to TRADING_DB_PATH")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="export a strategy replay bundle")
    export_parser.add_argument("--trade-date", required=True)
    export_parser.add_argument("--start-time", default=None)
    export_parser.add_argument("--end-time", default=None)
    export_parser.add_argument("--codes", default="")
    export_parser.add_argument("--theme-names", default="")
    export_parser.add_argument("--output-dir", default=str(DEFAULT_BUNDLE_ROOT))
    export_parser.add_argument("--force", action="store_true")

    run_parser = subparsers.add_parser("run", help="run a replay bundle")
    run_parser.add_argument("--bundle", default="")
    run_parser.add_argument("--trade-date", default="")
    run_parser.add_argument("--mode", default="decision_led", choices=["data_only", "decision_led", "full_runtime"])
    run_parser.add_argument("--cycle-interval-sec", type=float, default=None)
    run_parser.add_argument("--speed", type=float, default=1.0)
    run_parser.add_argument("--output-dir", default=str(DEFAULT_BUNDLE_ROOT))
    run_parser.add_argument("--replay-db-root", default=str(DEFAULT_REPLAY_DB_ROOT))
    run_parser.add_argument("--replay-db", default="")
    run_parser.add_argument("--force", action="store_true")
    run_parser.add_argument("--limit", type=int, default=None)
    run_parser.add_argument("--export-report", action="store_true", default=True)

    compare_parser = subparsers.add_parser("compare", help="show the latest report for a replay id")
    compare_parser.add_argument("--replay-id", required=True)
    compare_parser.add_argument("--replay-db-root", default=str(DEFAULT_REPLAY_DB_ROOT))

    args = parser.parse_args()
    source_db_path = Path(args.db_path).expanduser() if args.db_path else get_settings().db_path

    if args.command == "export":
        exporter = StrategyReplayBundleExporter(source_db_path, output_root=args.output_dir)
        bundle = exporter.export_bundle(
            args.trade_date,
            start_time=args.start_time,
            end_time=args.end_time,
            codes=_split_arg(args.codes),
            theme_names=_split_arg(args.theme_names),
            force=args.force,
        )
        _print_json(
            {
                "replay_id": bundle.manifest.replay_id,
                "bundle_path": str(bundle.path),
                "summary": bundle.manifest.data_quality,
                "warnings": bundle.manifest.warnings,
            }
        )
        return 0

    if args.command == "run":
        runner = StrategyRuntimeReplayRunner(
            source_db_path=source_db_path,
            replay_db_root=args.replay_db_root,
            bundle_root=args.output_dir,
        )
        result = runner.run(
            bundle_path=args.bundle or None,
            trade_date=args.trade_date or None,
            mode=args.mode,
            cycle_interval_sec=args.cycle_interval_sec,
            speed=args.speed,
            replay_db=args.replay_db or None,
            force=args.force,
            limit=args.limit,
            export_report=args.export_report,
        )
        _print_json(
            {
                "replay_id": result.replay_id,
                "replay_db_path": result.replay_db_path,
                "source_bundle_path": result.source_bundle_path,
                "report_id": (result.report or {}).get("report_id", ""),
                "status": result.status,
                "summary": result.summary,
                "warnings": result.warnings,
                "error": result.error,
            }
        )
        return 0 if result.status in {"OK", "PARTIAL_REPLAY"} else 2

    if args.command == "compare":
        detail = get_replay_run_detail(args.replay_id, args.replay_db_root)
        reports = scan_replay_reports(args.replay_db_root, replay_id=args.replay_id, limit=1)
        _print_json({"run": detail, "latest_report": reports[0] if reports else None})
        return 0 if detail.get("found") else 1

    return 1


def _split_arg(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
