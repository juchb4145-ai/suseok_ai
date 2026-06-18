from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading.reliability.models import ReliabilityQualificationConfig  # noqa: E402
from trading.reliability.qualification import ReliabilityQualificationRunner, qualification_exit_code  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OBSERVE reliability qualification gate")
    parser.add_argument("--profile", choices=["quick-ci", "replay", "fault-suite", "observe-soak", "full"], default="quick-ci")
    parser.add_argument("--output-dir", default="reports/reliability")
    parser.add_argument("--db-path", default="")
    parser.add_argument("--core-url", default="")
    parser.add_argument("--bundle", dest="bundle_path", default="")
    parser.add_argument("--repeat", type=int, default=None)
    parser.add_argument("--duration-sec", type=float, default=None)
    parser.add_argument("--sample-interval-sec", type=float, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--code-count", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="print compact JSON summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = ReliabilityQualificationConfig.from_env(
            profile=args.profile,
            output_dir=args.output_dir,
            db_path=args.db_path,
            core_url=args.core_url,
            bundle_path=args.bundle_path,
            duration_sec=args.duration_sec,
            sample_interval_sec=args.sample_interval_sec,
            repeat=args.repeat,
            seed=args.seed,
            code_count=args.code_count,
        )
        report = ReliabilityQualificationRunner(config).run()
    except Exception as exc:
        print(f"qualification execution error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    payload = report.to_dict()
    if args.json:
        print(json.dumps({key: payload.get(key) for key in ("run_id", "profile", "status", "recommendation", "report_dir")}, ensure_ascii=False, sort_keys=True))
    else:
        print(f"run_id={payload.get('run_id')}")
        print(f"profile={payload.get('profile')}")
        print(f"status={payload.get('status')}")
        print(f"recommendation={payload.get('recommendation')}")
        print(f"report_dir={payload.get('report_dir')}")
    return qualification_exit_code(payload.get("status", "ERROR"))


if __name__ == "__main__":
    raise SystemExit(main())
