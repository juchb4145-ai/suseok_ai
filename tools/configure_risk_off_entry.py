from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from storage.db import TradingDatabase
from trading.strategy.runtime_settings import (
    LEGACY_DEFAULT_SETTINGS,
    StrategyRuntimeSettings,
    StrategyRuntimeSettingsRepository,
)


DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "trader.sqlite3"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Configure RISK_OFF small-entry observation in strategy_runtime_settings.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["observe", "disable"],
        help="observe enables RISK_OFF candidates with observe_only=True; disable turns the feature off.",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="SQLite database path. Defaults to data/trader.sqlite3.",
    )
    parser.add_argument("--min-relative-strength-pct", type=float, default=4.0)
    parser.add_argument("--max-position-size-multiplier", type=float, default=0.25)
    parser.add_argument("--json", action="store_true", help="Print a machine-readable summary.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser()
    db = TradingDatabase(str(db_path))
    try:
        repo = StrategyRuntimeSettingsRepository(db)
        current = repo.load()
        payload = _settings_payload(current)
        if args.mode == "observe":
            _apply_observe(payload, args)
        else:
            _apply_disable(payload)
        next_settings = StrategyRuntimeSettings.from_settings_json(
            payload,
            strategy_name=current.strategy_name,
            profile_name=current.profile_name,
            profile_version=current.profile_version,
            mode=current.mode,
            loaded_from="configure_risk_off_entry",
        )
        saved = repo.save(next_settings)
        summary = _summary(db_path, saved)
    finally:
        db.close()

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_summary(summary)
    return 0


def _settings_payload(current: StrategyRuntimeSettings) -> dict[str, Any]:
    base = deepcopy(LEGACY_DEFAULT_SETTINGS)
    raw = deepcopy(current.settings_json or {})
    return _deep_merge(base, raw)


def _apply_observe(payload: dict[str, Any], args: argparse.Namespace) -> None:
    config = dict(payload.get("risk_off_entry") or {})
    config.update(
        {
            "enabled": True,
            "observe_only": True,
            "block_extreme_risk_off": True,
            "require_candidate_breadth_ready": True,
            "require_candidate_breadth_gate_usable": True,
            "require_latest_tick_ready": True,
            "require_support_ready": True,
            "require_vwap_or_recent_support_ready": True,
            "min_relative_strength_vs_index_pct": float(args.min_relative_strength_pct),
            "max_position_size_multiplier": float(args.max_position_size_multiplier),
            "max_ready_per_cycle": 1,
            "reason_code": "RISK_OFF_SMALL_ENTRY",
        }
    )
    payload["risk_off_entry"] = config


def _apply_disable(payload: dict[str, Any]) -> None:
    config = dict(payload.get("risk_off_entry") or {})
    config.update(
        {
            "enabled": False,
            "observe_only": True,
        }
    )
    payload["risk_off_entry"] = config


def _deep_merge(base: Any, raw: Any) -> Any:
    if isinstance(base, dict) and isinstance(raw, dict):
        merged = deepcopy(base)
        for key, value in raw.items():
            merged[key] = _deep_merge(merged.get(key), value)
        return merged
    return deepcopy(raw) if raw is not None else deepcopy(base)


def _summary(db_path: Path, settings: StrategyRuntimeSettings) -> dict[str, Any]:
    risk_off = dict(settings.value("risk_off_entry", {}) or {})
    execution = dict(settings.value("order_execution", {}) or {})
    return {
        "db_path": str(db_path),
        "risk_off_entry_enabled": bool(risk_off.get("enabled")),
        "risk_off_entry_observe_only": bool(risk_off.get("observe_only")),
        "min_relative_strength_vs_index_pct": risk_off.get("min_relative_strength_vs_index_pct"),
        "max_position_size_multiplier": risk_off.get("max_position_size_multiplier"),
        "max_ready_per_cycle": risk_off.get("max_ready_per_cycle"),
        "block_extreme_risk_off": bool(risk_off.get("block_extreme_risk_off")),
        "order_execution_mode": execution.get("mode", ""),
        "live_sim_enabled": bool(execution.get("live_sim_enabled")),
        "live_real_enabled": bool(execution.get("live_real_enabled")),
        "kill_switch_active": bool(execution.get("kill_switch_active")),
    }


def _print_summary(summary: dict[str, Any]) -> None:
    print("RISK_OFF small-entry runtime setting saved.")
    for key, value in summary.items():
        print(f"- {key}: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
