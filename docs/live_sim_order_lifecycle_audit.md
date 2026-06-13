# LIVE_SIM Order Lifecycle Audit

This audit is read-only by default. It does not enable LIVE_REAL, relax LIVE_SIM guards, or change gate/risk thresholds.

## Order Statuses

The LIVE_SIM order ledger recognizes:

- `CREATED`
- `BLOCKED`
- `DUPLICATE`
- `SUBMITTED`
- `UNKNOWN_SUBMIT`
- `ACCEPTED`
- `PARTIAL_FILLED`
- `FILLED`
- `CANCEL_REQUESTED`
- `CANCELLED`
- `REJECTED`
- `FAILED`
- `EXPIRED`
- `RECONCILE_REQUIRED`

Terminal statuses are `BLOCKED`, `DUPLICATE`, `FILLED`, `CANCELLED`, `REJECTED`, `FAILED`, and `EXPIRED`.

## Core Transitions

Allowed transitions include:

- `CREATED -> BLOCKED`
- `CREATED -> DUPLICATE`
- `CREATED -> SUBMITTED`
- `SUBMITTED -> UNKNOWN_SUBMIT`
- `SUBMITTED -> ACCEPTED`
- `ACCEPTED -> PARTIAL_FILLED`
- `ACCEPTED -> FILLED`
- `PARTIAL_FILLED -> FILLED`
- `ACCEPTED -> CANCEL_REQUESTED`
- `PARTIAL_FILLED -> CANCEL_REQUESTED`
- `CANCEL_REQUESTED -> CANCELLED`
- `UNKNOWN_SUBMIT -> RECONCILE_REQUIRED`
- `CANCEL_REQUESTED -> RECONCILE_REQUIRED`
- any non-terminal status to `RECONCILE_REQUIRED`

Invalid transitions are kept as audit warnings. Examples:

- `FILLED -> PARTIAL_FILLED`
- `CANCELLED -> FILLED`
- `BLOCKED -> SUBMITTED`
- `DUPLICATE -> SUBMITTED`
- `REJECTED -> FILLED`

## Operator Surfaces

- API: `/api/runtime/live-sim/audit`
- Snapshot field: `live_sim_audit`
- Dashboard card: `LIVE_SIM 주문 lifecycle audit`
- CLI: `python tools/audit_live_sim_lifecycle.py --db data/trading.sqlite3 --trade-date today --json`

The report highlights missing broker order IDs, stale cancel requests, orphan executions, position quantity mismatches, duplicate open positions, and reconcile-required states.
