from __future__ import annotations

import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading.strategy.models import Candidate, CandidateState
from trading_app.market_gate_review import MarketGateReviewAnalyzer, REVIEW_COLUMNS


TRADE_DATE = "2026-06-03"


def test_market_gate_review_builds_status_columns_and_transition_kpis(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        _save_candidate(
            db,
            "000001",
            {
                "candidate_instance_id": "ci-chase",
                "candidate_market": "KOSDAQ",
                "final_gate_status": "BLOCKED",
                "chase_risk": True,
                "reason_codes": ["CHASE_RISK"],
                "support_ready": True,
                "latest_tick_ready": True,
            },
        )
        _save_candidate(
            db,
            "000002",
            {
                "candidate_instance_id": "ci-late",
                "candidate_market": "KOSDAQ",
                "final_gate_status": "LATE_CHASE_TEMP_WAIT",
                "late_chase_level": "soft_block",
                "late_chase_block_type": "temporary",
                "late_chase_recheck_after_sec": 60,
                "reason_codes": ["LATE_CHASE_TEMP_WAIT"],
                "support_ready": True,
                "latest_tick_ready": True,
            },
        )
        _save_candidate(
            db,
            "000003",
            {
                "candidate_market": "KOSPI",
                "final_gate_status": "WAIT_MARKET_CONFIRMATION_PENDING",
                "candidate_market_confirmation_pending": True,
                "reason_codes": ["WAIT_MARKET_CONFIRMATION_PENDING"],
                "support_ready": True,
                "latest_tick_ready": True,
            },
        )
        _save_candidate(
            db,
            "000004",
            {
                "candidate_instance_id": "ci-restored",
                "candidate_market": "KOSDAQ",
                "final_gate_status": "WAIT_CANDIDATE_MARKET_WEAK",
                "candidate_market_confirmed_status": "WEAK",
                "market_confirmation_state_restored": True,
                "market_confirmation_state_age_sec": 890,
                "market_confirmation_state_max_restore_age_sec": 900,
                "market_confirmation_state_restore_reason": "MARKET_CONFIRMATION_STATE_RESTORED",
                "market_side_recovered_at": "2026-06-03T09:12:00",
                "market_side_cycles_to_recover": 2,
                "post_block_return_5m_pct": 1.2,
                "reason_codes": ["WAIT_CANDIDATE_MARKET_WEAK"],
                "support_ready": True,
                "latest_tick_ready": True,
            },
        )
        _save_candidate(
            db,
            "000005",
            {
                "candidate_instance_id": "ci-reset",
                "candidate_market": "KOSDAQ",
                "final_gate_status": "WAIT_MARKET_STATE_CONSERVATIVE_FALLBACK",
                "market_confirmation_state_reset_reason": "MARKET_CONFIRMATION_STATE_RESET_ON_MARKET_CLOSE",
                "market_reset_required": True,
                "market_session_type": "post_close",
                "market_confirmation_state_restore_reason": "MARKET_CONFIRMATION_STATE_RESET_ON_MARKET_CLOSE",
                "reason_codes": ["MARKET_CONFIRMATION_STATE_CONSERVATIVE_FALLBACK"],
                "support_ready": True,
                "latest_tick_ready": True,
            },
        )
        _save_transitions(db)

        report = MarketGateReviewAnalyzer(db).build_report(trade_date=TRADE_DATE)
    finally:
        db.close()

    rows = {row["code"]: row for row in report["rows"]}
    assert rows["000001"]["display_status"] == "CHASE_RISK_BLOCKED"
    assert rows["000002"]["display_status"] == "LATE_CHASE_TEMP_WAIT"
    assert rows["000002"]["late_chase_temp_wait"] is True
    assert rows["000003"]["display_status"] == "WAIT_MARKET_CONFIRMATION_PENDING"
    assert rows["000003"]["attribution_confidence"] == "LOW"
    assert "MARKET_GATE_REVIEW_ATTRIBUTION_LOW_CONFIDENCE" in rows["000003"]["review_reason_codes"]
    assert "MARKET_GATE_REVIEW_PRICE_DATA_MISSING" in rows["000003"]["review_reason_codes"]
    assert rows["000004"]["false_block_candidate"] is True
    assert "MARKET_GATE_REVIEW_FALSE_BLOCK_CANDIDATE" in rows["000004"]["review_reason_codes"]
    assert "MARKET_GATE_REVIEW_RESTORED_STATE_ANALYZED" in rows["000004"]["review_reason_codes"]
    assert "MARKET_GATE_REVIEW_SESSION_RESET_ANALYZED" in rows["000005"]["review_reason_codes"]
    assert rows["000005"]["runtime_order_intent_created"] is False
    assert rows["000005"]["virtual_order_created"] is False

    summary = report["summary"]
    assert summary["total_candidates_seen"] == 5
    assert summary["total_market_wait_count"] == 3
    assert summary["market_wait_buy_intent_blocked_count"] == 3
    assert summary["false_block_candidate_count"] == 1
    assert summary["restore_age_sec_p90"] == 890.0
    assert summary["max_restore_age_sec_regular_reference"] == 900
    assert summary["session_reset_count"] >= 2
    assert summary["post_close_restore_skipped_count"] >= 1
    assert report["transition_summary"]["source_conflict_count"] == 1


def test_market_gate_review_exports_read_only_files(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        _save_candidate(
            db,
            "000010",
            {
                "candidate_instance_id": "ci-ready",
                "candidate_market": "KOSPI",
                "final_gate_status": "READY_PULLBACK",
                "reason_codes": ["READY_PULLBACK"],
                "support_ready": True,
                "latest_tick_ready": True,
            },
        )
        analyzer = MarketGateReviewAnalyzer(db)
        report = analyzer.build_report(trade_date=TRADE_DATE)
        exported = analyzer.export_all(report, report_dir=tmp_path / "reports", stem="review")
    finally:
        db.close()

    for key in ("json", "csv", "md"):
        assert Path(exported[key]).exists()
    assert set(REVIEW_COLUMNS).issubset(set((tmp_path / "reports" / "review.csv").read_text(encoding="utf-8-sig").splitlines()[0].split(",")))
    assert "read-only observability report" in (tmp_path / "reports" / "review.md").read_text(encoding="utf-8")


def test_market_gate_review_api_is_read_only(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    import trading_app.api as api_module

    api = importlib.reload(api_module)
    db = TradingDatabase(str(db_path))
    try:
        _save_candidate(
            db,
            "000020",
            {
                "candidate_instance_id": "ci-api",
                "candidate_market": "KOSPI",
                "final_gate_status": "WAIT_CANDIDATE_MARKET_WEAK",
                "candidate_market_confirmed_status": "WEAK",
                "reason_codes": ["WAIT_CANDIDATE_MARKET_WEAK"],
                "support_ready": True,
                "latest_tick_ready": True,
            },
        )
    finally:
        db.close()

    with TestClient(api.app) as client:
        response = client.get(f"/api/runtime/market-gate/review?trade_date={TRADE_DATE}")

    payload = response.json()
    assert response.status_code == 200
    assert payload["trade_date"] == TRADE_DATE
    assert payload["rows"][0]["display_status"] == "WAIT_CANDIDATE_MARKET_WEAK"
    assert payload["rows"][0]["runtime_order_intent_created"] is False
    assert payload["notes"][0] == "read_only_observability_report"


def _save_candidate(db: TradingDatabase, code: str, details: dict) -> Candidate:
    metadata = {
        "candidate_instance_id": details.get("candidate_instance_id", ""),
        "gate_results_by_theme": {
            details.get("theme_id", "power"): {
                "theme_name": "power",
                "theme_score": 80,
                **details,
            }
        },
    }
    return db.save_candidate(
        Candidate(
            trade_date=TRADE_DATE,
            code=code,
            name=f"stock-{code}",
            market=str(details.get("candidate_market") or "KOSDAQ"),
            state=CandidateState.BLOCKED,
            metadata=metadata,
        )
    )


def _save_transitions(db: TradingDatabase) -> None:
    for index, transition_type in enumerate(
        [
            "WEAK_PENDING",
            "WEAK_CONFIRMED",
            "RECOVERY_PENDING",
            "RECOVERY_CONFIRMED",
            "SOURCE_CONFLICT",
            "RESET_ON_MARKET_CLOSE",
            "RESTORE_SKIPPED",
        ],
        start=1,
    ):
        db.save_market_side_confirmation_transition(
            {
                "trade_date": TRADE_DATE,
                "session_id": f"{TRADE_DATE}:post_close" if transition_type in {"RESET_ON_MARKET_CLOSE", "RESTORE_SKIPPED"} else f"{TRADE_DATE}:regular",
                "market_side": "KOSDAQ",
                "cycle_id": f"cycle-{index}",
                "transition_type": transition_type,
                "transition_reason_codes": _transition_reasons(transition_type),
                "created_at": f"{TRADE_DATE}T09:{index:02d}:00",
                "source_conflict": transition_type == "SOURCE_CONFLICT",
            }
        )


def _transition_reasons(transition_type: str) -> list[str]:
    if transition_type == "RESET_ON_MARKET_CLOSE":
        return ["MARKET_CONFIRMATION_STATE_RESET_ON_MARKET_CLOSE"]
    if transition_type == "RESTORE_SKIPPED":
        return ["MARKET_CONFIRMATION_STATE_RESTORE_NOT_ALLOWED"]
    return []
