import importlib
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading_app.theme_lab_gate_reason_outcomes import ThemeLabGateReasonOutcomeAnalyzer


START = datetime(2026, 6, 1, 9, 1, 0)


def test_theme_lab_gate_reason_outcome_labels_missed_after_watchset_drop(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        _save_snapshot(
            db,
            START,
            [
                {
                    "symbol": "000001",
                    "name": "warmup",
                    "current_price": 100,
                    "final_gate_status": "WAIT",
                    "price_location_status": "UNKNOWN",
                    "price_location_readiness": "WARMUP",
                    "price_location_readiness_reason_codes": ["PRICE_LOCATION_WARMUP"],
                    "primary_theme": "AI",
                    "stock_role": "LEADER",
                    "data_quality_bucket": "WARMUP_OPTIONAL",
                    "data_quality_action": "ALLOW_EARLY_SMALL_CANDIDATE",
                }
            ],
        )
        db.save_theme_lab_outcome_observations(
            [
                {
                    "observed_at": (START + timedelta(minutes=5)).isoformat(),
                    "trade_date": "2026-06-01",
                    "stock_code": "000001",
                    "price": 102,
                    "source": "theme_lab_outcome_tracking",
                },
                {
                    "observed_at": (START + timedelta(minutes=15)).isoformat(),
                    "trade_date": "2026-06-01",
                    "stock_code": "000001",
                    "price": 105,
                    "source": "theme_lab_outcome_tracking",
                },
            ]
        )

        report = ThemeLabGateReasonOutcomeAnalyzer(db).build_report(trade_date="2026-06-01")

        assert report["summary"]["event_count"] == 1
        assert report["summary"]["missed_opportunity_count"] == 1
        item = report["items"][0]
        assert item["primary_reason"] == "PRICE_LOCATION_WARMUP"
        assert item["return_5m_pct"] == 2.0
        assert item["mfe_15m_pct"] == 5.0
        assert item["outcome_label"] == "MISSED_OPPORTUNITY"
        assert item["data_quality_bucket"] == "WARMUP_OPTIONAL"
        assert report["summary"]["by_data_quality_bucket"][0]["data_quality_bucket"] == "WARMUP_OPTIONAL"
        reason = report["by_reason"][0]
        assert reason["reason_code"] == "PRICE_LOCATION_WARMUP"
        assert reason["missed_opportunity_rate"] == 1.0
    finally:
        db.close()


def test_theme_lab_gate_reason_outcome_detects_ready_later_from_snapshot(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        _save_snapshot(
            db,
            START,
            [
                {
                    "symbol": "000002",
                    "name": "provisional",
                    "current_price": 100,
                    "final_gate_status": "WAIT",
                    "price_location_status": "PULLBACK_RECLAIM",
                    "price_location_readiness": "PROVISIONAL",
                    "price_location_readiness_reason_codes": ["PRICE_LOCATION_PROVISIONAL"],
                }
            ],
        )
        _save_snapshot(
            db,
            START + timedelta(minutes=6),
            [
                {
                    "symbol": "000002",
                    "name": "provisional",
                    "current_price": 101,
                    "final_gate_status": "READY",
                    "price_location_status": "PULLBACK_RECLAIM",
                    "price_location_readiness": "READY",
                }
            ],
        )

        report = ThemeLabGateReasonOutcomeAnalyzer(db).build_report(trade_date="2026-06-01")

        item = report["items"][0]
        assert item["would_have_triggered_ready"] is True
        assert item["minutes_to_ready"] == 6.0
        assert item["outcome_label"] == "WAIT_RESOLVED_TO_READY"
    finally:
        db.close()


def test_theme_lab_provisional_small_entry_shadow_policy_is_reported(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        _save_snapshot(
            db,
            START,
            [
                {
                    "symbol": "000004",
                    "name": "shadow-win",
                    "current_price": 100,
                    "final_gate_status": "WAIT",
                    "price_location_status": "PULLBACK_RECLAIM",
                    "price_location_readiness": "PROVISIONAL",
                    "price_location_readiness_reason_codes": ["PRICE_LOCATION_PROVISIONAL"],
                    "condition_level": 3,
                    "stock_role": "LEADER",
                    "risk_level": "PASS",
                    "candidate_market_status": "HEALTHY",
                },
                {
                    "symbol": "000005",
                    "name": "risk-off",
                    "current_price": 100,
                    "final_gate_status": "WAIT",
                    "price_location_status": "PULLBACK_RECLAIM",
                    "price_location_readiness": "PROVISIONAL",
                    "price_location_readiness_reason_codes": ["PRICE_LOCATION_PROVISIONAL"],
                    "condition_level": 3,
                    "stock_role": "CO_LEADER",
                    "risk_level": "PASS",
                    "candidate_market_status": "RISK_OFF",
                },
            ],
        )
        db.save_theme_lab_outcome_observations(
            [
                {
                    "observed_at": (START + timedelta(minutes=5)).isoformat(),
                    "trade_date": "2026-06-01",
                    "stock_code": "000006",
                    "price": 99,
                    "source": "theme_lab_outcome_tracking",
                },
                {
                    "observed_at": (START + timedelta(minutes=15)).isoformat(),
                    "trade_date": "2026-06-01",
                    "stock_code": "000004",
                    "price": 104,
                    "source": "theme_lab_outcome_tracking",
                },
                {
                    "observed_at": (START + timedelta(minutes=15)).isoformat(),
                    "trade_date": "2026-06-01",
                    "stock_code": "000005",
                    "price": 104,
                    "source": "theme_lab_outcome_tracking",
                },
            ]
        )

        report = ThemeLabGateReasonOutcomeAnalyzer(db).build_report(trade_date="2026-06-01")

        by_code = {item["code"]: item for item in report["items"]}
        assert by_code["000004"]["shadow_small_entry_candidate"] is True
        assert by_code["000004"]["shadow_small_entry_win_15m"] is True
        assert by_code["000004"]["shadow_position_size_multiplier"] == 0.25
        assert by_code["000005"]["shadow_small_entry_candidate"] is False
        assert by_code["000005"]["shadow_small_entry_reason"] == "MARKET_STATUS_EXCLUDED"
        shadow = report["shadow_small_entry"]["summary"]
        assert shadow["candidate_count"] == 1
        assert shadow["win_rate_15m"] == 1.0
        assert shadow["risk_case_rate_15m"] == 0.0
        assert report["shadow_small_entry"]["rejected_reason_counts"]["MARKET_STATUS_EXCLUDED"] == 1
    finally:
        db.close()


def test_theme_lab_provisional_small_entry_ab_calibrator_compares_scenarios(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        _save_snapshot(
            db,
            START,
            [
                {
                    "symbol": "000006",
                    "name": "strict-win",
                    "current_price": 100,
                    "final_gate_status": "WAIT",
                    "price_location_status": "PULLBACK_RECLAIM",
                    "price_location_readiness": "PROVISIONAL",
                    "price_location_readiness_reason_codes": ["PRICE_LOCATION_PROVISIONAL"],
                    "condition_level": 3,
                    "stock_role": "LEADER",
                    "risk_level": "PASS",
                    "candidate_market_status": "HEALTHY",
                },
                {
                    "symbol": "000007",
                    "name": "wide-risk",
                    "current_price": 100,
                    "final_gate_status": "WAIT",
                    "price_location_status": "PULLBACK_RECLAIM",
                    "price_location_readiness": "PROVISIONAL",
                    "price_location_readiness_reason_codes": ["PRICE_LOCATION_PROVISIONAL"],
                    "condition_level": 2,
                    "stock_role": "CO_LEADER",
                    "risk_level": "RISK_ADJUST",
                    "candidate_market_status": "CHOPPY",
                },
            ],
        )
        db.save_theme_lab_outcome_observations(
            [
                {
                    "observed_at": (START + timedelta(minutes=5)).isoformat(),
                    "trade_date": "2026-06-01",
                    "stock_code": "000006",
                    "price": 99,
                    "source": "theme_lab_outcome_tracking",
                },
                {
                    "observed_at": (START + timedelta(minutes=15)).isoformat(),
                    "trade_date": "2026-06-01",
                    "stock_code": "000006",
                    "price": 104,
                    "source": "theme_lab_outcome_tracking",
                },
                {
                    "observed_at": (START + timedelta(minutes=15)).isoformat(),
                    "trade_date": "2026-06-01",
                    "stock_code": "000007",
                    "price": 97,
                    "source": "theme_lab_outcome_tracking",
                },
            ]
        )

        report = ThemeLabGateReasonOutcomeAnalyzer(db).build_report(trade_date="2026-06-01")
        scenarios = {row["scenario_id"]: row for row in report["shadow_small_entry_ab"]["scenarios"]}

        strict = scenarios["leader_pass_l3_x10"]
        wide = scenarios["leader_co_pass_risk_l2_x25"]
        assert strict["candidate_count"] == 1
        assert strict["win_rate_15m"] == 1.0
        assert wide["candidate_count"] == 2
        assert wide["risk_case_rate_15m"] == 0.5
        assert wide["scaled_avg_mae_15m_pct"] == -0.5
        assert report["shadow_small_entry_ab"]["best_scenarios"][0]["scenario_id"] == "leader_pass_l3_x10"
        assert report["shadow_small_entry_ab"]["matrix"]["by_multiplier"]
    finally:
        db.close()


def test_theme_lab_gate_reason_outcome_api_and_export(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")
    db = TradingDatabase(str(db_path))
    try:
        _save_snapshot(
            db,
            START,
            [
                {
                    "symbol": "000003",
                    "name": "blocked",
                    "current_price": 100,
                    "final_gate_status": "BLOCKED",
                    "reason_codes": ["WAIT_CANDIDATE_MARKET_RISK_OFF"],
                }
            ],
        )
        db.save_theme_lab_outcome_observations(
            [
                {
                    "observed_at": (START + timedelta(minutes=15)).isoformat(),
                    "trade_date": "2026-06-01",
                    "stock_code": "000003",
                    "price": 99,
                    "source": "theme_lab_outcome_tracking",
                }
            ]
        )
    finally:
        db.close()

    import trading_app.api as api

    api = importlib.reload(api)
    client = TestClient(api.app)

    report = client.get("/api/runtime/performance/theme-lab-gate-reasons?trade_date=2026-06-01").json()
    assert report["summary"]["good_block_count"] == 1
    assert report["items"][0]["outcome_label"] == "GOOD_BLOCK"

    exported = client.get(
        "/api/runtime/performance/theme-lab-gate-reasons/export?trade_date=2026-06-01&format=md",
        headers={"X-Local-Token": "test-token"},
    ).json()
    assert Path(exported["exports"]["md"]).exists()


def _save_snapshot(db: TradingDatabase, at: datetime, watchset: list[dict]) -> None:
    db.save_theme_lab_flow_result(
        at.isoformat(),
        {
            "market_status": {"market_status": "CHOPPY"},
            "theme_rankings": [],
            "theme_condition_snapshots": [],
            "condition_hit_snapshots": [],
            "watchset_snapshots": watchset,
            "gate_decisions": [],
            "data_quality": {},
        },
    )
