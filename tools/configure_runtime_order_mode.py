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
        description="Configure runtime order execution mode for DRY_RUN or Kiwoom LIVE_SIM.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["DRY_RUN", "LIVE_SIM"],
        help="Runtime order execution mode to save into strategy_runtime_settings.",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="SQLite database path. Defaults to data/trader.sqlite3.",
    )
    parser.add_argument(
        "--allowed-account",
        action="append",
        default=[],
        help="Simulation account number allowed for LIVE_SIM. Repeat for multiple accounts.",
    )
    parser.add_argument("--max-orders-per-day", type=int, default=5)
    parser.add_argument("--max-new-positions-per-day", type=int, default=3)
    parser.add_argument("--max-order-amount-krw", type=int, default=300_000)
    parser.add_argument("--max-position-amount-krw", type=int, default=300_000)
    parser.add_argument("--max-total-exposure-krw", type=int, default=1_000_000)
    parser.add_argument(
        "--kill-switch-active",
        action="store_true",
        help="Save LIVE_SIM with the buy kill switch active. Useful for a guarded dry rehearsal.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable summary.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser()
    db = TradingDatabase(str(db_path))
    try:
        repo = StrategyRuntimeSettingsRepository(db)
        current = repo.load()
        payload = _settings_payload(current)
        if args.mode == "DRY_RUN":
            _apply_dry_run(payload)
        else:
            _apply_live_sim(payload, args)
        next_settings = StrategyRuntimeSettings.from_settings_json(
            payload,
            strategy_name=current.strategy_name,
            profile_name=current.profile_name,
            profile_version=current.profile_version,
            mode=current.mode,
            loaded_from="configure_runtime_order_mode",
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


def _apply_dry_run(payload: dict[str, Any]) -> None:
    execution = dict(payload.get("order_execution") or {})
    execution.update(
        {
            "mode": "DRY_RUN",
            "live_sim_enabled": False,
            "live_real_enabled": False,
            "require_simulated_account": True,
            "allowed_account_mode": "SIMULATION",
            "block_real_account": True,
            "kill_switch_active": False,
        }
    )
    payload["order_execution"] = execution


def _apply_live_sim(payload: dict[str, Any], args: argparse.Namespace) -> None:
    execution = dict(payload.get("order_execution") or {})
    execution.update(
        {
            "mode": "LIVE_SIM",
            "live_sim_enabled": True,
            "live_real_enabled": False,
            "require_simulated_account": True,
            "allowed_account_mode": "SIMULATION",
            "allowed_account_numbers": list(args.allowed_account or []),
            "block_real_account": True,
            "fail_closed_on_account_unknown": True,
            "submit_first_leg_only": True,
            "allow_second_third_leg_before_first_fill": False,
            "max_orders_per_day": int(args.max_orders_per_day),
            "max_new_positions_per_day": int(args.max_new_positions_per_day),
            "max_order_amount_krw": int(args.max_order_amount_krw),
            "max_position_amount_krw": int(args.max_position_amount_krw),
            "max_total_exposure_krw": int(args.max_total_exposure_krw),
            "allow_market_order": False,
            "kill_switch_enabled": True,
            "kill_switch_active": bool(args.kill_switch_active),
            "force_dry_run_on_guard_error": True,
        }
    )
    payload["order_execution"] = execution
    payload["live_sim_exit_guard"] = {
        **dict(payload.get("live_sim_exit_guard") or {}),
        "enabled": True,
        "stop_loss_pct": -2.0,
        "take_profit_pct": 5.0,
        "max_hold_minutes": 60,
        "market_close_liquidation_enabled": True,
    }
    payload["live_sim_order_lifecycle"] = {
        **dict(payload.get("live_sim_order_lifecycle") or {}),
        "enabled": True,
        "cancel_unfilled_buy_after_sec": 60,
        "cancel_unfilled_sell_after_sec": 60,
        "cancel_partial_remainder_after_sec": 90,
        "block_new_order_when_cancel_pending": True,
    }
    payload["live_sim_reconcile"] = {
        **dict(payload.get("live_sim_reconcile") or {}),
        "enabled": True,
        "reconcile_on_startup": True,
        "reconcile_on_reconnect": True,
        "block_new_buy_on_reconcile_failure": True,
    }


def _deep_merge(base: Any, raw: Any) -> Any:
    if isinstance(base, dict) and isinstance(raw, dict):
        merged = deepcopy(base)
        for key, value in raw.items():
            merged[key] = _deep_merge(merged.get(key), value)
        return merged
    return deepcopy(raw) if raw is not None else deepcopy(base)


def _summary(db_path: Path, settings: StrategyRuntimeSettings) -> dict[str, Any]:
    execution = dict(settings.value("order_execution", {}) or {})
    return {
        "db_path": str(db_path),
        "mode": execution.get("mode", ""),
        "live_sim_enabled": bool(execution.get("live_sim_enabled")),
        "live_real_enabled": bool(execution.get("live_real_enabled")),
        "allowed_account_numbers_masked": [_mask_account(value) for value in execution.get("allowed_account_numbers", [])],
        "max_orders_per_day": execution.get("max_orders_per_day"),
        "max_new_positions_per_day": execution.get("max_new_positions_per_day"),
        "max_order_amount_krw": execution.get("max_order_amount_krw"),
        "max_position_amount_krw": execution.get("max_position_amount_krw"),
        "max_total_exposure_krw": execution.get("max_total_exposure_krw"),
        "kill_switch_active": bool(execution.get("kill_switch_active")),
    }


def _print_summary(summary: dict[str, Any]) -> None:
    print("Runtime order execution setting saved.")
    for key, value in summary.items():
        print(f"- {key}: {value}")


def _mask_account(value: object) -> str:
    text = str(value or "")
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) <= 4:
        return "****" if digits else ""
    return f"{digits[:2]}{'*' * max(2, len(digits) - 4)}{digits[-2:]}"


if __name__ == "__main__":
    raise SystemExit(main())
