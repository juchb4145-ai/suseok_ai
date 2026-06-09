from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from storage.db import TradingDatabase


DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "trader.sqlite3"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect and repair LIVE_SIM order/position ledger inconsistencies.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument("--trade-date", default=_kst_today(), help="Trade date in YYYY-MM-DD.")
    parser.add_argument("--apply", action="store_true", help="Apply proposed repairs. Default is dry-run.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db = TradingDatabase(str(Path(args.db).expanduser()))
    try:
        report = analyze_repairs(db, trade_date=args.trade_date)
        if args.apply:
            report = apply_repairs(db, report)
    finally:
        db.close()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_report(report)
    return 1 if report.get("apply_failed") else 0


def analyze_repairs(db: TradingDatabase, *, trade_date: str) -> dict[str, Any]:
    position_repairs: list[dict[str, Any]] = []
    order_repairs: list[dict[str, Any]] = []
    manual_executions: list[dict[str, Any]] = []

    positions = db.list_live_sim_positions(limit=5000)
    buy_orders_by_position = _buy_orders_by_position(db, trade_date=trade_date)
    for position in positions:
        key = _position_key(position)
        buy_orders = buy_orders_by_position.get(key, [])
        if not buy_orders:
            continue
        actual_qty = max(_max_cumulative_fill_qty(db, order) for order in buy_orders)
        if actual_qty <= 0:
            continue
        entry_qty = int(position.get("entry_qty") or 0)
        current_qty = int(position.get("current_qty") or 0)
        realized_qty = int(position.get("realized_qty") or 0)
        if realized_qty == 0 and (entry_qty > actual_qty or current_qty > actual_qty):
            position_repairs.append(
                {
                    "action": "correct_live_sim_position_qty",
                    "position_id": position.get("position_id"),
                    "code": position.get("code"),
                    "account_id_masked": position.get("account_id_masked"),
                    "candidate_instance_id": position.get("candidate_instance_id"),
                    "previous_entry_qty": entry_qty,
                    "previous_current_qty": current_qty,
                    "corrected_qty": actual_qty,
                    "reason": "CUMULATIVE_FILL_QTY_WAS_APPLIED_AS_DELTA",
                }
            )

    for order in db.list_live_sim_orders(trade_date=trade_date, side="sell", limit=5000):
        requested_qty = int(order.get("requested_qty") or 0)
        if requested_qty <= 0:
            continue
        matching_buys = buy_orders_by_position.get(_order_position_key(order), [])
        max_buy_qty = max((_max_cumulative_fill_qty(db, buy) for buy in matching_buys), default=0)
        if max_buy_qty > 0 and requested_qty > max_buy_qty:
            order_repairs.append(
                {
                    "action": "mark_oversized_exit_reconcile_required",
                    "order_intent_id": order.get("order_intent_id"),
                    "code": order.get("code"),
                    "requested_qty": requested_qty,
                    "max_buy_cumulative_qty": max_buy_qty,
                    "previous_status": order.get("order_status"),
                    "reason": "OVERSIZED_EXIT_QTY_REPAIRED",
                }
            )

    for row in db.conn.execute(
        """
        SELECT id, created_at, code, order_no, side, quantity, price, filled_quantity, remaining_quantity, tag
        FROM executions
        WHERE created_at >= ? AND created_at < ? AND COALESCE(tag, '') = ''
        ORDER BY id
        """,
        (trade_date, _next_day(trade_date)),
    ).fetchall():
        manual_executions.append(dict(row))

    return {
        "trade_date": trade_date,
        "dry_run": True,
        "position_repairs": position_repairs,
        "order_repairs": order_repairs,
        "manual_executions": manual_executions,
        "repair_count": len(position_repairs) + len(order_repairs),
        "manual_execution_count": len(manual_executions),
    }


def apply_repairs(db: TradingDatabase, report: dict[str, Any]) -> dict[str, Any]:
    applied: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()
    for repair in list(report.get("position_repairs") or []):
        try:
            position = db.get_live_sim_position(str(repair.get("position_id") or ""))
            if not position:
                raise ValueError("position not found")
            corrected_qty = int(repair.get("corrected_qty") or 0)
            db.save_live_sim_position(
                {
                    **position,
                    "entry_qty": corrected_qty,
                    "current_qty": corrected_qty,
                    "status": "RECONCILE_REQUIRED",
                    "details": {
                        **dict(position.get("details") or {}),
                        "post_market_repair": {
                            **repair,
                            "repaired_at": now,
                            "requires_broker_reconcile": True,
                        },
                    },
                    "updated_at": now,
                }
            )
            applied.append(repair)
        except Exception as exc:  # pragma: no cover - surfaced in CLI report
            failed.append({**repair, "error": str(exc)})

    for repair in list(report.get("order_repairs") or []):
        try:
            order = db.get_live_sim_order(str(repair.get("order_intent_id") or ""))
            if not order:
                raise ValueError("order not found")
            reason_codes = _unique([*list(order.get("reason_codes") or []), "POST_MARKET_REPAIR_RECONCILE_REQUIRED", str(repair.get("reason") or "")])
            db.update_live_sim_order(
                str(order.get("order_intent_id") or ""),
                {
                    "order_status": "RECONCILE_REQUIRED",
                    "reason_codes": reason_codes,
                    "details": {
                        **dict(order.get("details") or {}),
                        "post_market_repair": {
                            **repair,
                            "repaired_at": now,
                            "requires_broker_reconcile": True,
                        },
                    },
                    "updated_at": now,
                },
            )
            applied.append(repair)
        except Exception as exc:  # pragma: no cover - surfaced in CLI report
            failed.append({**repair, "error": str(exc)})

    db.save_live_sim_runtime_health(
        "reconcile",
        status="RECONCILE_REQUIRED" if applied else "HEALTHY",
        reason="LIVE_SIM_REPAIR_APPLIED" if applied else "OK",
        details={"applied_count": len(applied), "failed_count": len(failed), "manual_execution_count": report.get("manual_execution_count", 0)},
        updated_at=now,
    )
    return {
        **report,
        "dry_run": False,
        "applied_repairs": applied,
        "failed_repairs": failed,
        "applied_count": len(applied),
        "apply_failed": bool(failed),
    }


def _buy_orders_by_position(db: TradingDatabase, *, trade_date: str) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for order in db.list_live_sim_orders(trade_date=trade_date, side="buy", limit=5000):
        grouped.setdefault(_order_position_key(order), []).append(order)
    return grouped


def _position_key(position: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(position.get("account_id_masked") or ""),
        str(position.get("code") or ""),
        str(position.get("candidate_instance_id") or ""),
    )


def _order_position_key(order: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(order.get("account_id_masked") or ""),
        str(order.get("code") or ""),
        str(order.get("candidate_instance_id") or ""),
    )


def _max_cumulative_fill_qty(db: TradingDatabase, order: dict[str, Any]) -> int:
    row = db.conn.execute(
        """
        SELECT COALESCE(MAX(cumulative_fill_qty), 0) AS qty
        FROM live_sim_fill_events
        WHERE order_intent_id = ? OR (broker_order_id != '' AND broker_order_id = ?)
        """,
        (str(order.get("order_intent_id") or ""), str(order.get("broker_order_id") or "")),
    ).fetchone()
    return int(row["qty"] or 0) if row else 0


def _next_day(trade_date: str) -> str:
    return (datetime.fromisoformat(trade_date) + timedelta(days=1)).strftime("%Y-%m-%d")


def _kst_today() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d")


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item or "")
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _print_report(report: dict[str, Any]) -> None:
    mode = "DRY-RUN" if report.get("dry_run") else "APPLIED"
    print(f"LIVE_SIM repair {mode} for {report.get('trade_date')}")
    print(f"- repair_count: {report.get('repair_count', 0)}")
    print(f"- manual_execution_count: {report.get('manual_execution_count', 0)}")
    if report.get("applied_count") is not None:
        print(f"- applied_count: {report.get('applied_count', 0)}")
    for repair in list(report.get("position_repairs") or [])[:20]:
        print(
            f"- position {repair.get('code')} {repair.get('position_id')}: "
            f"{repair.get('previous_current_qty')} -> {repair.get('corrected_qty')}"
        )
    for repair in list(report.get("order_repairs") or [])[:20]:
        print(
            f"- order {repair.get('code')} {repair.get('order_intent_id')}: "
            f"requested={repair.get('requested_qty')} max_buy={repair.get('max_buy_cumulative_qty')}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
