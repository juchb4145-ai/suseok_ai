from __future__ import annotations

import argparse
import subprocess
import sys


PROFILE_ARGS = {
    "quick": ["--profile=quick", "-q"],
    "unit": ["--profile=unit", "-q"],
    "integration": ["--profile=integration", "-q"],
    "slow": ["--profile=slow", "-q"],
    "full": ["--profile=full", "-q"],
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run pytest with the repository test profiles and optional sharding."
    )
    parser.add_argument(
        "profile",
        choices=sorted(PROFILE_ARGS),
        help="Test profile to run.",
    )
    parser.add_argument(
        "--shard",
        metavar="N/M",
        help="Run one deterministic shard of the selected profile, for example 1/4.",
    )
    parser.add_argument(
        "--durations",
        type=int,
        default=None,
        help="Override pytest slow-test duration count.",
    )
    parser.add_argument(
        "--maxfail",
        type=int,
        default=None,
        help="Stop after this many failures.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the pytest command without running it.",
    )
    args, extra_args = parser.parse_known_args()

    command = [sys.executable, "-m", "pytest", *PROFILE_ARGS[args.profile]]
    if args.shard:
        command.append(f"--shard={args.shard}")
    if args.durations is not None:
        command.append(f"--durations={args.durations}")
    if args.maxfail is not None:
        command.append(f"--maxfail={args.maxfail}")

    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]
    command.extend(extra_args)

    if args.dry_run:
        print(" ".join(command))
        return 0

    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
