import importlib
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading.strategy.shadow_small_entry_promotion import (
    MODE_LIVE_SIM_GUARDED,
    READY_SHADOW_SMALL_ENTRY,
    SHADOW_OBSERVE_ONLY_REASON,
    STATUS_BLOCKED,
    STATUS_NO_EVIDENCE,
    STATUS_OBSERVE_ONLY,
    STATUS_PROMOTED,
    WAIT_SHADOW_SMALL_ENTRY_CANDIDATE,
    ShadowSmallEntryPromotionConfig,
    evaluate_shadow_small_entry_promotion,
)
from trading_app.shadow_small_entry_promotion import (
    ShadowSmallEntryPromotionAnalyzer,
    record_promotion_trace_events,
)
from trading_app.strategy_change_proposals import StrategyChangeProposalGenerator


TODAY = date.today().isoformat()
START = datetime.combine(date.today(), datetime.min.time()).replace(hour=9, minute=1)


def test_shadow_small_entry_no_report_is_unavailable(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        report = ShadowSmallEntryPromotionAnalyzer(db).build_report(trade_date=TODAY)
        evaluation = evaluate_shadow_small_entry_promotion(
            trace=_candidate_trace(),
            evidence={"available": False, "status": "NO_DATA"},
        )

        assert report["available"] is False
        assert report["summary"]["candidate_count"] == 0
        assert evaluation.promotion_status == STATUS_NO_EVIDENCE
        assert evaluation.strategy_eligible is False
    finally:
        db.close()


def test_shadow_small_entry_defaults_to_observe_only_when_order_disabled():
    evaluation = evaluate_shadow_small_entry_promotion(
        trace=_candidate_trace(reason="WAIT_DATA_SUPPORT_NOT_READY"),
        evidence=_evidence(sample_count=12, confidence=0.7),
        config=ShadowSmallEntryPromotionConfig(min_sample_count=10, min_confidence=0.55),
    )

    assert evaluation.promotion_status == STATUS_OBSERVE_ONLY
    assert evaluation.final_status == WAIT_SHADOW_SMALL_ENTRY_CANDIDATE
    assert evaluation.strategy_eligible is False
    assert SHADOW_OBSERVE_ONLY_REASON in evaluation.reason_codes
    assert evaluation.position_size_multiplier <= 0.15


def test_shadow_small_entry_live_sim_guarded_promotes_only_when_enabled():
    evaluation = evaluate_shadow_small_entry_promotion(
        trace=_candidate_trace(reason="WAIT_DATA_SUPPORT_NOT_READY"),
        evidence=_evidence(sample_count=35, confidence=0.8),
        config=ShadowSmallEntryPromotionConfig(
            mode=MODE_LIVE_SIM_GUARDED,
            order_enabled=True,
            min_sample_count=10,
            strong_sample_count=30,
            min_confidence=0.55,
        ),
        live_sim_audit={"status": "OK", "summary": {}, "exit_guard_ready": True},
    )

    assert evaluation.promotion_status == STATUS_PROMOTED
    assert evaluation.ready_type == READY_SHADOW_SMALL_ENTRY
    assert evaluation.strategy_eligible is True
    assert evaluation.order_eligibility == "BUY_ELIGIBLE_SHADOW_SMALL_ENTRY_GUARDED"
    assert 0 < evaluation.position_size_multiplier <= 0.25


def test_shadow_small_entry_thin_sample_and_risk_blocks():
    thin = evaluate_shadow_small_entry_promotion(
        trace=_candidate_trace(reason="WAIT_DATA_SUPPORT_NOT_READY"),
        evidence=_evidence(sample_count=3, confidence=0.2),
        config=ShadowSmallEntryPromotionConfig(min_sample_count=10, min_confidence=0.55),
    )
    chase = evaluate_shadow_small_entry_promotion(
        trace=_candidate_trace(reason="CHASE_HIGH"),
        evidence=_evidence(reason_code="CHASE_HIGH", sample_count=20, confidence=0.8),
        config=ShadowSmallEntryPromotionConfig(
            mode=MODE_LIVE_SIM_GUARDED,
            order_enabled=True,
            min_sample_count=10,
            min_confidence=0.55,
        ),
    )
    stale_tick = evaluate_shadow_small_entry_promotion(
        trace={**_candidate_trace(reason="WAIT_DATA_SUPPORT_NOT_READY"), "latest_tick_ready": False},
        evidence=_evidence(sample_count=20, confidence=0.8),
        config=ShadowSmallEntryPromotionConfig(
            mode=MODE_LIVE_SIM_GUARDED,
            order_enabled=True,
            min_sample_count=10,
            min_confidence=0.55,
        ),
    )

    assert thin.promotion_status == STATUS_OBSERVE_ONLY
    assert thin.position_size_multiplier == 0.1
    assert chase.promotion_status == STATUS_BLOCKED
    assert chase.rejected_reason == "CHASE_HIGH"
    assert "SHADOW_SMALL_ENTRY_CHASE_BLOCKED" in chase.reason_codes
    assert stale_tick.promotion_status == STATUS_BLOCKED
    assert stale_tick.rejected_reason == "LATEST_TICK_NOT_READY"


def test_shadow_small_entry_trace_columns_are_persisted(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        evaluation = evaluate_shadow_small_entry_promotion(
            trace=_candidate_trace(candidate_instance_id="ci-shadow-trace"),
            evidence=_evidence(sample_count=12, confidence=0.7),
            config=ShadowSmallEntryPromotionConfig(min_sample_count=10, min_confidence=0.55),
        )
        saved = record_promotion_trace_events(
            db,
            evaluation.to_dict(),
            trade_date=TODAY,
            runtime_cycle_id="cycle-shadow",
            decision_cycle_id="decision-shadow",
        )
        traces = db.list_buy_zero_trace_events(trade_date=TODAY, candidate_instance_id="ci-shadow-trace", limit=10)

        assert saved == 3
        assert {row["stage"] for row in traces} >= {
            "SHADOW_SMALL_ENTRY_EVIDENCE_LOADED",
            "SHADOW_SMALL_ENTRY_CANDIDATE_EVALUATED",
            "SHADOW_SMALL_ENTRY_OBSERVE_ONLY",
        }
        assert traces[0]["promotion_status"] == STATUS_OBSERVE_ONLY
        assert traces[0]["source_report_id"] == "report-shadow"
        assert traces[0]["sample_count"] == 12
        assert traces[0]["mode"] == "observe_only"
        assert traces[0]["order_enabled"] is False
    finally:
        db.close()


def test_shadow_small_entry_report_api_export_and_strategy_proposal(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")
    db = TradingDatabase(str(db_path))
    try:
        _seed_warmup_optional_outcomes(db, count=12)
        analyzer = ShadowSmallEntryPromotionAnalyzer(
            db,
            config=ShadowSmallEntryPromotionConfig(min_sample_count=10, min_confidence=0.55),
            report_root=tmp_path / "reports",
        )
        report = analyzer.build_report(trade_date=TODAY)
        exports = analyzer.export_all(report)
        generator = StrategyChangeProposalGenerator(db)
        generated = generator.generate(trade_date=TODAY, source_type="shadow_small_entry_promotion", persist=False)

        assert report["available"] is True
        assert report["summary"]["candidate_count"] == 12
        assert report["summary"]["observe_only_count"] == 12
        assert Path(exports["json"]).exists()
        assert Path(exports["csv"]).exists()
        assert Path(exports["md"]).exists()
        assert generated["proposal_count"] >= 1
        assert any(
            proposal["candidate_config_patch"].get("shadow_small_entry_promotion.order_enabled") is False
            for proposal in generated["proposals"]
        )
        assert all(proposal["status"] == "REVIEW_READY" for proposal in generated["proposals"])

        evaluation = evaluate_shadow_small_entry_promotion(
            trace=_candidate_trace(code="710000", candidate_instance_id="ci-api-shadow"),
            evidence=_evidence(sample_count=12, confidence=0.7),
            config=ShadowSmallEntryPromotionConfig(min_sample_count=10, min_confidence=0.55),
        )
        record_promotion_trace_events(db, evaluation.to_dict(), trade_date=TODAY)
    finally:
        db.close()

    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        snapshot = client.get("/api/snapshot?refresh=true").json()
        summary = client.get("/api/shadow-small-entry-promotion/summary", params={"trade_date": TODAY}).json()
        candidates = client.get("/api/shadow-small-entry-promotion/candidates", params={"trade_date": TODAY}).json()
        traces = client.get(
            "/api/shadow-small-entry-promotion/traces",
            params={"trade_date": TODAY, "candidate_instance_id": "ci-api-shadow"},
        ).json()
        proposals = client.post(
            "/api/runtime/change-proposals/generate",
            params={"trade_date": TODAY, "source_type": "shadow_small_entry_promotion", "persist": False},
            headers={"X-Local-Token": "test-token"},
        ).json()

    assert snapshot["shadow_small_entry_promotion"]["candidate_count"] == 12
    assert snapshot["runtime"]["shadow_small_entry_promotion"]["order_enabled"] is False
    assert summary["observe_only_count"] == 12
    assert candidates["total"] == 12
    assert traces["total"] >= 3
    assert proposals["source_type"] == "shadow_small_entry_promotion"
    assert proposals["proposal_count"] >= 1


def test_shadow_small_entry_cli_exports_json_csv_md(tmp_path):
    db_path = tmp_path / "trader.sqlite3"
    db = TradingDatabase(str(db_path))
    try:
        _seed_warmup_optional_outcomes(db, count=12)
    finally:
        db.close()

    result = subprocess.run(
        [
            sys.executable,
            "tools/build_shadow_small_entry_promotion.py",
            "--db",
            str(db_path),
            "--trade-date",
            TODAY,
            "--export",
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    exports = payload["exports"]

    assert payload["summary"]["candidate_count"] == 12
    assert Path(exports["json"]).exists()
    assert Path(exports["csv"]).exists()
    assert Path(exports["md"]).exists()


def _candidate_trace(
    *,
    code: str = "700001",
    candidate_instance_id: str = "ci-shadow",
    reason: str = "WAIT_DATA_SUPPORT_NOT_READY",
) -> dict:
    return {
        "trade_date": TODAY,
        "code": code,
        "name": f"name-{code}",
        "candidate_instance_id": candidate_instance_id,
        "status": "WAIT",
        "reason_codes": [reason],
        "primary_reason": reason,
        "primary_group": "DATA_QUALITY_RISK",
        "stock_role": "LEADER",
        "theme_status": "LEADING_THEME",
        "price_location_status": "VWAP_RECLAIM",
        "price_location_readiness": "READY",
        "risk_level": "PASS",
        "current_price": 100,
        "trade_value": 1_000_000,
        "latest_tick_ready": True,
        "support_ready": True,
        "vwap_ready": True,
    }


def _evidence(*, reason_code: str = "WAIT_DATA_SUPPORT_NOT_READY", sample_count: int = 12, confidence: float = 0.7) -> dict:
    return {
        "available": True,
        "status": "READY",
        "report_id": "report-shadow",
        "source_report_trade_date": TODAY,
        "eligible_reason_codes": [reason_code],
        "eligible_reason_groups": ["DATA_QUALITY_RISK"],
        "reason_code_rows": [
            {
                "reason_code": reason_code,
                "recommendation": "REVIEW_FOR_SMALL_ENTRY",
                "sample_count": sample_count,
                "labeled_count": sample_count,
                "event_count": sample_count,
                "confidence": confidence,
                "missed_opportunity_rate": 0.5,
                "risk_avoided_rate": 0.1,
                "good_block_rate": 0.1,
                "avg_mfe_15m_pct": 3.0,
                "avg_mae_15m_pct": -1.0,
                "eligible": True,
            }
        ],
        "group_rows": [],
    }


def _seed_warmup_optional_outcomes(db: TradingDatabase, *, count: int) -> None:
    for minute in range(count):
        code = f"71{minute:04d}"
        _save_snapshot(
            db,
            START + timedelta(minutes=minute),
            [
                {
                    "symbol": code,
                    "name": f"name-{code}",
                    "current_price": 100,
                    "final_gate_status": "WAIT",
                    "reason_codes": ["WAIT_DATA_SUPPORT_NOT_READY", "WARMUP_OPTIONAL"],
                    "primary_theme": "AI",
                    "stock_role": "LEADER",
                    "risk_level": "PASS",
                    "condition_level": 3,
                    "data_quality_bucket": "WARMUP_OPTIONAL",
                    "price_location_status": "VWAP_RECLAIM",
                    "price_location_readiness": "READY",
                    "candidate_market_status": "HEALTHY",
                    "support_ready": True,
                    "vwap_ready": True,
                    "trade_value": 1_000_000,
                }
            ],
        )
    observations = []
    for minute in range(count):
        code = f"71{minute:04d}"
        observations.extend(
            [
                {
                    "observed_at": (START + timedelta(minutes=minute + 5)).isoformat(),
                    "trade_date": TODAY,
                    "stock_code": code,
                    "price": 102,
                    "source": "theme_lab_outcome_tracking",
                },
                {
                    "observed_at": (START + timedelta(minutes=minute + 15)).isoformat(),
                    "trade_date": TODAY,
                    "stock_code": code,
                    "price": 104,
                    "source": "theme_lab_outcome_tracking",
                },
            ]
        )
    db.save_theme_lab_outcome_observations(observations)


def _save_snapshot(db: TradingDatabase, at: datetime, watchset: list[dict]) -> None:
    db.save_theme_lab_flow_result(
        at.isoformat(),
        {
            "market_status": {"market_status": "HEALTHY"},
            "theme_rankings": [],
            "theme_condition_snapshots": [],
            "condition_hit_snapshots": [],
            "watchset_snapshots": watchset,
            "gate_decisions": [],
            "data_quality": {},
        },
    )
