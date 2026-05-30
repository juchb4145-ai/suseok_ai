from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    print(
        "Deprecated legacy PyQt entrypoint. New work should use the 64bit Core/API "
        "and 32bit Kiwoom Gateway split.",
        file=sys.stderr,
    )
    from main import main as legacy_main

    return int(legacy_main())


if __name__ == "__main__":
    raise SystemExit(main())
