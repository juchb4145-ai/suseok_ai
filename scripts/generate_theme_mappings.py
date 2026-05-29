from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a review-only Kiwoom theme mapping CSV draft.")
    parser.add_argument("--output", default=str(Path("data") / "theme_mappings_auto.csv"))
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--default-enabled", type=int, choices=[0, 1], default=0)
    parser.add_argument("--date-range-days", type=int, default=5)
    parser.add_argument("--request-delay-ms", type=int, default=1200)
    parser.add_argument("--timeout-sec", type=int, default=20)
    parser.add_argument("--max-themes", type=int, default=None)
    parser.add_argument(
        "--include-keywords",
        default="",
        help="Comma-separated theme keywords, for example: 반도체,로봇,전력",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    from main import configure_qt_paths

    configure_qt_paths()
    try:
        from PyQt5.QtWidgets import QApplication
    except ImportError as exc:
        print("PyQt5 is required in the Kiwoom 32-bit Python environment.", file=sys.stderr)
        raise SystemExit(1) from exc

    from kiwoom.client import KiwoomClient
    from trading.strategy.theme_template import generate_theme_mappings_auto_csv

    app = QApplication.instance() or QApplication(sys.argv[:1])
    client = KiwoomClient()
    if not _login(client, app):
        print("Kiwoom login failed or timed out.", file=sys.stderr)
        return 1

    keywords = [part.strip() for part in str(args.include_keywords or "").split(",") if part.strip()]
    result = generate_theme_mappings_auto_csv(
        client,
        output_path=args.output,
        overwrite=args.overwrite,
        default_enabled=args.default_enabled,
        date_range_days=args.date_range_days,
        request_delay_ms=args.request_delay_ms,
        timeout_sec=args.timeout_sec,
        max_themes=args.max_themes,
        include_keywords=keywords,
        progress=print,
    )
    _print_result(result)
    return 1 if result.errors else 0


def _login(client, app, timeout_sec: int = 60) -> bool:
    login_state: list[bool] = []
    client.connected.connect(lambda ok, *_args: login_state.append(bool(ok)))
    result = int(client.login() or 0)
    if result < 0:
        return False
    deadline = time.monotonic() + timeout_sec
    while not login_state and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.05)
    return bool(login_state and login_state[0])


def _print_result(result) -> None:
    from trading.strategy.theme_template import NEXT_STEPS

    print(f"output={result.output_path}")
    print(
        "themes_total={total}, themes_to_fetch={fetch}, rows_written={rows}, requests={requests}".format(
            total=result.themes_total,
            fetch=result.themes_to_fetch,
            rows=result.rows_written,
            requests=result.request_count,
        )
    )
    if result.warnings:
        print("warnings:")
        for warning in result.warnings[:20]:
            print(f"  - {warning}")
    if result.errors:
        print("errors:")
        for error in result.errors[:20]:
            print(f"  - {error}")
    print("WARNING: generated CSV is a manual-review draft. Default enabled is 0 unless explicitly changed.")
    print(NEXT_STEPS)


if __name__ == "__main__":
    raise SystemExit(main())
