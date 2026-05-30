from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="64bit Trading Core/API server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Keep 127.0.0.1 for local trading.")
    parser.add_argument("--port", type=int, default=8000, help="Bind port.")
    parser.add_argument("--db", default="", help="SQLite database path. Defaults to data/trader.sqlite3.")
    parser.add_argument("--token", default="", help="Local gateway token. Defaults to TRADING_CORE_TOKEN/local-dev-token.")
    parser.add_argument("--mode", choices=["OBSERVE", "DRY_RUN", "LIVE"], default="", help="Core order mode.")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn reload for development.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.db:
        os.environ["TRADING_DB_PATH"] = args.db
    if args.token:
        os.environ["TRADING_CORE_TOKEN"] = args.token
    if args.mode:
        os.environ["TRADING_MODE"] = args.mode

    import uvicorn

    uvicorn.run(
        "trading_app.api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
