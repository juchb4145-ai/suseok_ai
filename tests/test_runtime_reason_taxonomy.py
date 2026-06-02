from pathlib import Path

from storage.db import TradingDatabase
from trading.strategy.models import BlockType, Candidate, CandidateSourceType, CandidateState
from trading.strategy.reason_taxonomy import normalize_reason_status
from trading_app.api import build_candidates_snapshot
from trading_app.dry_run_performance import DryRunPerformanceAnalyzer


def test_reason_taxonomy_distinguishes_wait_data_and_wait_pullback(tmp_path):
    db = TradingDatabase(str(Path(tmp_path) / "taxonomy.sqlite3"))
    try:
        db.save_candidate(
            Candidate(
                trade_date="2026-05-30",
                code="000001",
                name="DataWait",
                sources=[CandidateSourceType.CONDITION],
                state=CandidateState.WATCHING,
                metadata={"reason_codes": ["DATA_INSUFFICIENT"]},
            )
        )
        db.save_candidate(
            Candidate(
                trade_date="2026-05-30",
                code="000002",
                name="PullbackWait",
                sources=[CandidateSourceType.CONDITION],
                state=CandidateState.WATCHING,
                metadata={"reason_codes": ["WAIT_PULLBACK_CONFIRMATION"]},
            )
        )

        snapshot = build_candidates_snapshot(db, trade_date="2026-05-30", limit=10)
    finally:
        db.close()

    by_code = {item["code"]: item for item in snapshot["items"]}
    assert by_code["000001"]["reason_status"] == "WAIT_DATA"
    assert by_code["000002"]["reason_status"] == "WAIT_PULLBACK"
    statuses = {row["status"]: row["count"] for row in snapshot["summary"]["reason_summary"]["by_status"]}
    assert statuses["WAIT_DATA"] == 1
    assert statuses["WAIT_PULLBACK"] == 1


def test_reason_taxonomy_maps_required_legacy_codes():
    assert normalize_reason_status(reason_codes=["CHASE_HIGH"], display_state="OBSERVE") == "OBSERVE_CHASE"
    assert normalize_reason_status(reason_codes=["CHASE_HIGH"], display_state="BLOCKED", block_type="FINAL") == "BLOCK_CHASE"
    assert normalize_reason_status(reason_codes=["THEME_WEAK"], display_state="WAIT") == "BLOCK_THEME"
    assert normalize_reason_status(reason_codes=["DATA_INSUFFICIENT"], display_state="WAIT") == "WAIT_DATA"
    assert normalize_reason_status(reason_codes=["DATA_INSUFFICIENT"], display_state="BLOCKED", block_type="FINAL") == "BLOCK_DATA"


def test_dry_run_performance_report_includes_reason_taxonomy_without_breaking(tmp_path):
    db = TradingDatabase(str(Path(tmp_path) / "perf-taxonomy.sqlite3"))
    try:
        db.save_runtime_order_intent(
            {
                "intent_id": "entry-taxonomy",
                "trade_date": "2026-05-30",
                "source": "strategy_runtime",
                "mode": "DRY_RUN",
                "dry_run": True,
                "status": "DRY_RUN_REJECTED",
                "reason": "GATE_BLOCKED",
                "account": "dryrun",
                "code": "005930",
                "side": "buy",
                "quantity": 0,
                "price": 10000,
                "order_amount": 0,
                "order_type": 1,
                "hoga": "00",
                "tag": "runtime",
                "strategy_name": "KOSDAQ_THEME_PROFILE",
                "candidate_id": 1,
                "order_phase": "entry",
                "gate_reason": "THEME_WEAK",
                "gate_status": "BLOCKED",
                "idempotency_key": "entry-taxonomy",
                "dedupe_key": "entry-taxonomy",
                "safety": {"ok": False, "reason": "THEME_WEAK"},
                "live_safety": {"ok": True, "reason": ""},
                "request": {},
                "metadata": {},
                "created_at": "2026-05-30T09:01:00",
                "updated_at": "2026-05-30T09:01:00",
            }
        )

        report = DryRunPerformanceAnalyzer(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    assert report["items"][0]["reason_status"] == "BLOCK_THEME"
    assert report["items"][0]["reason_family"] == "BLOCKED"
    assert report["grouped"]["by_reason_status"][0]["key"] == "BLOCK_THEME"
    assert report["grouped"]["reason_summary"]["by_status"][0]["status"] == "BLOCK_THEME"
