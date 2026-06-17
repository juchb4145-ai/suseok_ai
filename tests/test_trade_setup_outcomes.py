from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from storage.db import TradingDatabase
from trading.strategy.runtime_settings import StrategyRuntimeSettings
from trading_app.themelab_dashboard import build_theme_lab_dashboard_snapshot
from trading_app.trade_setup_outcomes import TradeSetupOutcomeAnalyzer


START = datetime(2026, 6, 1, 9, 1, 0)


def test_leader_probe_mfe_labels_good_and_missed_opportunity():
    report = TradeSetupOutcomeAnalyzer(None).build_report(
        trade_date="2026-06-01",
        source_items=[
            _item(
                "000001",
                setup_type="LEADER_PROBE",
                action="SMALL_OBSERVE",
                status="WAIT",
                mfe_15m=2.2,
                mae_15m=-0.4,
                return_15m=1.1,
            )
        ],
    )

    row = report["rows"][0]
    assert row["good_candidate_15m"] is True
    assert row["missed_opportunity_15m"] is True
    assert report["summary_by_type"]["LEADER_PROBE"]["good_candidate_count_15m"] == 1
    assert report["top_missed_opportunities"][0]["code"] == "000001"


def test_leader_probe_insufficient_sample_recommendation():
    report = TradeSetupOutcomeAnalyzer(None).build_report(
        trade_date="2026-06-01",
        source_items=[
            _item(
                "000001",
                setup_type="LEADER_PROBE",
                action="SMALL_OBSERVE",
                status="WAIT",
                mfe_15m=2.2,
                mae_15m=-0.4,
                return_15m=1.1,
            )
        ],
    )

    recommendation = report["recommendations"]["LEADER_PROBE"]
    assert recommendation["recommendation"] == "INSUFFICIENT_SAMPLE"
    assert "1/20" in recommendation["operator_message_ko"]


def test_leader_probe_high_risk_rate_blocks_promotion():
    items = [
        _item(f"1{i:05d}", setup_type="LEADER_PROBE", action="SMALL_OBSERVE", status="WAIT", mfe_15m=2.2, mae_15m=-0.4, return_15m=1.0)
        for i in range(15)
    ]
    items.extend(
        _item(f"2{i:05d}", setup_type="LEADER_PROBE", action="SMALL_OBSERVE", status="WAIT", mfe_15m=0.6, mae_15m=-2.0, return_15m=-1.2)
        for i in range(5)
    )

    report = TradeSetupOutcomeAnalyzer(None).build_report(trade_date="2026-06-01", source_items=items)

    summary = report["summary_by_type"]["LEADER_PROBE"]
    assert summary["labeled_count"] == 20
    assert summary["risk_case_rate_15m"] == 0.25
    assert report["recommendations"]["LEADER_PROBE"]["recommendation"] == "DO_NOT_PROMOTE"


def test_relative_strength_uses_separate_thresholds():
    items = [
        _item(f"3{i:05d}", setup_type="RELATIVE_STRENGTH", action="SMALL_OBSERVE", status="WAIT", mfe_15m=2.0, mae_15m=-0.3, return_15m=0.8)
        for i in range(17)
    ]
    items.extend(
        _item(f"4{i:05d}", setup_type="RELATIVE_STRENGTH", action="SMALL_OBSERVE", status="WAIT", mfe_15m=2.0, mae_15m=-1.3, return_15m=0.3)
        for i in range(3)
    )

    report = TradeSetupOutcomeAnalyzer(None).build_report(trade_date="2026-06-01", source_items=items)

    summary = report["summary_by_type"]["RELATIVE_STRENGTH"]
    assert summary["risk_case_rate_15m"] == 0.15
    assert report["recommendations"]["RELATIVE_STRENGTH"]["recommendation"] == "REVIEW_FOR_LIVE_SIM_SMALL"


def test_avoid_rally_is_reported_as_good_block_miss():
    report = TradeSetupOutcomeAnalyzer(None).build_report(
        trade_date="2026-06-01",
        source_items=[
            _item(
                "000009",
                setup_type="AVOID",
                action="BLOCK",
                status="BLOCKED",
                mfe_15m=3.6,
                mae_15m=-0.2,
                return_15m=3.2,
                reason_codes=["VI_ACTIVE"],
            )
        ],
    )

    row = report["rows"][0]
    assert row["good_block_miss_15m"] is True
    assert report["summary_by_type"]["AVOID"]["good_block_miss_count"] == 1
    assert report["recommendations"]["AVOID"]["recommendation"] == "REVIEW_AVOID_MISSES"


def test_trade_setup_outcome_report_exports_json_csv_and_markdown(tmp_path):
    analyzer = TradeSetupOutcomeAnalyzer(None, report_root=tmp_path)
    report = analyzer.build_report(
        trade_date="2026-06-01",
        source_items=[
            _item("000001", setup_type="LEADER_PROBE", action="SMALL_OBSERVE", status="WAIT", mfe_15m=2.2, mae_15m=-0.4, return_15m=1.1)
        ],
    )

    exported = analyzer.export_all(report)

    assert Path(exported["json"]).name == "trade_setup_outcomes_20260601.json"
    assert Path(exported["csv"]).exists()
    assert Path(exported["md"]).exists()
    assert "LEADER_PROBE" in Path(exported["md"]).read_text(encoding="utf-8")


def test_dashboard_payload_includes_trade_setup_outcomes(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        watch = _watch(
            "000777",
            "WAIT",
            setup_type="LEADER_PROBE",
            action="SMALL_OBSERVE",
            mfe_reason="PRICE_LOCATION_PROVISIONAL",
        )
        db.save_theme_lab_flow_result(
            START.isoformat(),
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [],
                "theme_condition_snapshots": [],
                "condition_hit_snapshots": [],
                "watchset_snapshots": [watch],
                "gate_decisions": [],
                "data_quality": {},
            },
        )
        db.save_theme_lab_outcome_observations(
            [
                {
                    "observed_at": (START + timedelta(minutes=15)).isoformat(),
                    "trade_date": "2026-06-01",
                    "stock_code": "000777",
                    "price": 102.5,
                    "source": "theme_lab_outcome_tracking",
                },
                {
                    "observed_at": (START + timedelta(minutes=60)).isoformat(),
                    "trade_date": "2026-06-01",
                    "stock_code": "000777",
                    "price": 103.0,
                    "source": "theme_lab_outcome_tracking",
                },
            ]
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    outcomes = payload["trade_setup_outcomes"]
    assert outcomes["available"] is True
    assert outcomes["trade_date"] == "2026-06-01"
    assert outcomes["summary_by_type"]["LEADER_PROBE"]["candidate_count"] == 1
    assert outcomes["leader_probe_recommendation"]["recommendation"] == "INSUFFICIENT_SAMPLE"
    assert outcomes["top_missed_opportunities"][0]["code"] == "000777"


def test_trade_setup_outcome_pr_keeps_order_defaults_disabled():
    settings = StrategyRuntimeSettings.legacy_default()

    assert settings.value("hybrid_gate.observe_only") is True
    assert settings.value("data_quality_early_small.order_enabled") is False
    assert settings.value("shadow_small_entry_promotion.order_enabled") is False
    assert settings.value("live_sim_hybrid_ready_canary.enabled") is False
    assert settings.value("live_sim_hybrid_ready_canary.order_enabled") is False


def _item(
    code: str,
    *,
    setup_type: str,
    action: str,
    status: str,
    mfe_15m: float,
    mae_15m: float,
    return_15m: float,
    reason_codes: list[str] | None = None,
) -> dict:
    return {
        "trade_date": "2026-06-01",
        "observed_at": START.isoformat(),
        "code": code,
        "name": f"name-{code}",
        "theme_name": "AI",
        "trade_setup_type": setup_type,
        "trade_setup_action": action,
        "trade_setup_confidence_score": 0.82,
        "final_gate_status": status,
        "gate_status": status,
        "price_location_status": "VWAP_RECLAIM",
        "price_location_readiness": "PROVISIONAL",
        "stock_role": "LEADER",
        "risk_level": "PASS",
        "reason_codes": reason_codes or ["RELATIVE_STRENGTH_VS_MARKET"],
        "base_price": 100,
        "mfe_5m": mfe_15m,
        "mae_5m": mae_15m,
        "return_5m": return_15m,
        "mfe_15m": mfe_15m,
        "mae_15m": mae_15m,
        "return_15m": return_15m,
        "mfe_25m": mfe_15m,
        "mae_25m": mae_15m,
        "return_25m": return_15m,
        "mfe_60m": mfe_15m,
        "mae_60m": mae_15m,
        "return_60m": return_15m,
    }


def _watch(symbol: str, gate: str, *, setup_type: str, action: str, mfe_reason: str) -> dict:
    return {
        "symbol": symbol,
        "code": symbol,
        "name": f"name-{symbol}",
        "stock_name": f"name-{symbol}",
        "current_price": 100,
        "gate_status": gate,
        "final_gate_status": gate,
        "reason_codes": [mfe_reason],
        "theme_name": "AI",
        "primary_theme": "AI",
        "stock_role": "LEADER",
        "risk_level": "PASS",
        "price_location_status": "VWAP_RECLAIM",
        "price_location_readiness": "PROVISIONAL",
        "trade_setup_type": setup_type,
        "trade_setup_action": action,
        "trade_setup_confidence_score": 0.82,
        "trade_setup_position_size_multiplier": 0.1,
        "trade_setup_reason_codes": ["LEADER_PROBE_OBSERVE_ONLY"],
    }
