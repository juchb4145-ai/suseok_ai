from __future__ import annotations

from trading.strategy.support_readiness import (
    INSUFFICIENT_WARMUP_BARS,
    LOW_RECENT_BAR_COUNT,
    MISSING_1M_BARS,
    STALE_MINUTE_BARS,
    SUPPORT_DATA_MISSING,
    SUPPORT_STALE_VWAP,
    SUPPORT_STRUCTURALLY_MISSING,
    VALID_RECENT_MINUTE_BARS,
    minute_bar_quality,
    support_coverage,
    support_missing_taxonomy,
)
from storage.db import TradingDatabase
from trading.strategy.models import Candidate, CandidateSourceType, CandidateState
from trading_app.api import build_candidates_snapshot


def test_structurally_missing_when_metadata_exists_but_no_support_candidate():
    metadata = {"vwap": 9700, "vwap_ready": True, "completed_minute_bar_count": 10}

    assert support_missing_taxonomy(metadata, {}) == SUPPORT_STRUCTURALLY_MISSING


def test_data_missing_when_no_support_metadata_exists():
    assert support_missing_taxonomy({}, {}) == SUPPORT_DATA_MISSING


def test_stale_vwap_has_specific_taxonomy():
    metadata = {"vwap": 9700, "vwap_ready": True, "vwap_stale": True}

    assert support_missing_taxonomy(metadata, {"vwap": 9700}) == SUPPORT_STALE_VWAP
    assert support_coverage(metadata, {"vwap": 9700})["vwap_stale"] is True


def test_minute_bar_quality_early_warmup_and_intraday_low_count():
    early = minute_bar_quality({"session_bucket": "OPEN", "completed_minute_bar_count": 2})
    intraday = minute_bar_quality({"session_bucket": "MID", "completed_minute_bar_count": 5})

    assert early["minute_bar_quality_status"] == INSUFFICIENT_WARMUP_BARS
    assert intraday["minute_bar_quality_status"] == LOW_RECENT_BAR_COUNT


def test_minute_bar_quality_missing_and_stale():
    missing = minute_bar_quality({})
    stale = minute_bar_quality({"completed_minute_bar_count": 20, "minute_bar_age_sec": 400})

    assert missing["minute_bar_quality_status"] == MISSING_1M_BARS
    assert stale["minute_bar_quality_status"] == STALE_MINUTE_BARS


def test_minute_bar_quality_valid_when_recent_and_sufficient():
    quality = minute_bar_quality({"completed_minute_bar_count": 12, "recent_3m_bar_count": 4, "recent_5m_bar_count": 2, "minute_bar_age_sec": 30})

    assert quality["minute_bar_quality_status"] == VALID_RECENT_MINUTE_BARS


def test_candidate_dashboard_snapshot_exposes_support_coverage_summary(tmp_path):
    db = TradingDatabase(str(tmp_path / "dashboard.sqlite3"))
    try:
        coverage = support_coverage(
            {"vwap": 9700, "vwap_ready": True, "completed_minute_bar_count": 12, "recent_3m_bar_count": 4, "recent_5m_bar_count": 2},
            {"vwap": 9700},
        )
        db.save_candidate(
            Candidate(
                trade_date="2026-06-01",
                code="111111",
                name="leader",
                sources=[CandidateSourceType.THEME_WATCH],
                state=CandidateState.WATCHING,
                detected_at="2026-06-01T09:00:00",
                last_seen_at="2026-06-01T09:01:00",
                metadata={
                    "support_coverage": coverage,
                    "support_missing_reason": SUPPORT_STRUCTURALLY_MISSING,
                },
            )
        )
        snapshot = build_candidates_snapshot(db, trade_date="2026-06-01")
    finally:
        db.close()

    summary = snapshot["summary"]["support_coverage_summary"]
    assert summary["sample_count"] == 1
    assert summary["vwap_metadata_coverage_pct"] == 1.0
    assert summary["support_missing_count_by_reason"][0]["reason"] == SUPPORT_STRUCTURALLY_MISSING
    assert summary["support_source_distribution"][0]["source"] == "vwap"
