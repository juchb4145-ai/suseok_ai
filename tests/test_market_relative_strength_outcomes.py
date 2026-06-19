from __future__ import annotations

from pathlib import Path

from storage.db import TradingDatabase
from trading_app.market_relative_strength_outcomes import (
    MarketRelativeStrengthOutcomeAnalyzer,
    MarketRelativeStrengthOutcomeConfig,
    label_shadow_outcome,
)


def test_report_groups_shadow_outcomes_across_5_10_20_minutes_and_exports(tmp_path) -> None:
    db = TradingDatabase(str(tmp_path / "market-rs-outcomes.db"))
    decision = _decision_details(
        decision_id="mrs-1",
        code="000001",
        scenario="WEAK_SIDE_STRICT_SHADOW",
        variant="STRICT",
        market_side="KOSPI",
        side_regime="WEAK",
    )
    db.save_strategy_decision_events([_event_from_decision(decision)])
    db.save_strategy_decision_outcomes(
        [
            _outcome(decision, 300, mfe=1.2, mae=-0.2, ret=0.3),
            _outcome(decision, 600, mfe=1.8, mae=-0.4, ret=0.8),
            _outcome(decision, 1200, mfe=2.2, mae=-0.6, ret=1.1),
        ]
    )

    analyzer = MarketRelativeStrengthOutcomeAnalyzer(db, report_root=tmp_path / "reports")
    report = analyzer.build_report(trade_date="2026-06-19")
    exported = analyzer.export_all(report)

    row = report["rows"][0]
    assert report["report_name"] == "split_market_relative_strength_outcomes"
    assert row["mfe_5m"] == 1.2
    assert row["mfe_10m"] == 1.8
    assert row["mfe_20m"] == 2.2
    assert row["shadow_outcome_label"] == "SHADOW_EDGE_CANDIDATE"
    assert report["summary"]["shadow_edge_rate_10m"] == 1.0
    assert report["groups"]["shadow_scenario"][0]["key"] == "WEAK_SIDE_STRICT_SHADOW"
    assert report["groups"]["relative_strength_band"][0]["key"] == "4_TO_6"
    assert set(exported) == {"json", "csv", "md"}
    assert all(Path(path).exists() for path in exported.values())


def test_weak_side_review_recommendation_requires_sample_distribution() -> None:
    rows = []
    for idx in range(30):
        decision = _decision_details(
            decision_id=f"weak-{idx}",
            trade_date="2026-06-19" if idx < 15 else "2026-06-20",
            code=f"{idx % 5:06d}",
            theme_name=f"Theme {idx % 5}",
            scenario="WEAK_SIDE_STRICT_SHADOW",
            variant="STRICT",
            market_side="KOSDAQ" if idx % 2 else "KOSPI",
            side_regime="WEAK",
        )
        rows.append(_outcome(decision, 600, mfe=1.7, mae=-0.5, ret=0.9))
    report = MarketRelativeStrengthOutcomeAnalyzer(
        db=None,
        config=MarketRelativeStrengthOutcomeConfig(weak_side_min_labeled_count=30),
    ).build_report(source_items=rows)

    assert report["recommendations"]["current_recommendation"] == "REVIEW_WEAK_SIDE_SMALL_CANARY"
    assert report["recommendations"]["weak_side_checks"]["sample_distribution_ok"] is True
    assert report["summary"]["weak_side_shadow_candidate_count"] == 30


def test_recommendation_blocks_concentrated_or_risky_weak_side_sample() -> None:
    concentrated = []
    for idx in range(30):
        decision = _decision_details(
            decision_id=f"concentrated-{idx}",
            trade_date="2026-06-19" if idx < 15 else "2026-06-20",
            code="000001",
            theme_name="One Theme",
            scenario="WEAK_SIDE_STRICT_SHADOW",
            variant="STRICT",
            market_side="KOSPI",
            side_regime="WEAK",
        )
        concentrated.append(_outcome(decision, 600, mfe=1.8, mae=-0.4, ret=0.8))
    risky = []
    for idx in range(30):
        decision = _decision_details(
            decision_id=f"risky-{idx}",
            trade_date="2026-06-19" if idx < 15 else "2026-06-20",
            code=f"{idx % 5:06d}",
            theme_name=f"Theme {idx % 5}",
            scenario="WEAK_SIDE_STRICT_SHADOW",
            variant="STRICT",
            market_side="KOSPI",
            side_regime="WEAK",
        )
        risky.append(_outcome(decision, 600, mfe=0.7, mae=-1.7, ret=-0.4))

    analyzer = MarketRelativeStrengthOutcomeAnalyzer(db=None, config=MarketRelativeStrengthOutcomeConfig(weak_side_min_labeled_count=30))
    concentrated_report = analyzer.build_report(source_items=concentrated)
    risky_report = analyzer.build_report(source_items=risky)

    assert concentrated_report["recommendations"]["current_recommendation"] == "WATCH_MORE"
    assert concentrated_report["recommendations"]["weak_side_checks"]["concentration_dominance"] is True
    assert risky_report["recommendations"]["current_recommendation"] == "DO_NOT_PROMOTE"


def test_risk_off_report_is_observe_only_no_promotion() -> None:
    decision = _decision_details(
        decision_id="riskoff-1",
        code="000099",
        scenario="RISK_OFF_SIDE_DIAGNOSTIC",
        variant="STRICT",
        market_side="KOSDAQ",
        side_regime="RISK_OFF",
    )
    report = MarketRelativeStrengthOutcomeAnalyzer(db=None).build_report(source_items=[_outcome(decision, 600, mfe=2.0, mae=-0.3, ret=1.0)])

    assert report["recommendations"]["risk_off_side"] == "RISK_OFF_OBSERVE_ONLY_NO_PROMOTION"
    assert report["summary"]["risk_off_side_diagnostic_count"] == 1


def test_shadow_outcome_labels_are_risk_adjusted() -> None:
    assert label_shadow_outcome({"mfe_10m": 1.6, "return_10m": 0.6, "mae_10m": -0.9, "data_status_10m": "OK"}) == "SHADOW_EDGE_CANDIDATE"
    assert label_shadow_outcome({"mfe_10m": 3.0, "return_10m": 0.9, "mae_10m": -1.2, "data_status_10m": "OK"}) == "SHADOW_RISK_CASE"
    assert label_shadow_outcome({"mfe_10m": None, "return_10m": None, "mae_10m": None, "data_status_10m": "INSUFFICIENT"}) == "SHADOW_INSUFFICIENT_DATA"


def _decision_details(
    *,
    decision_id: str,
    trade_date: str = "2026-06-19",
    code: str,
    theme_name: str = "AI",
    scenario: str,
    variant: str,
    market_side: str,
    side_regime: str,
) -> dict:
    return {
        "shadow_decision_id": decision_id,
        "trade_date": trade_date,
        "calculated_at": f"{trade_date}T09:30:00",
        "candidate_id": 1,
        "candidate_instance_id": f"ci-{code}",
        "code": code,
        "name": f"Stock {code}",
        "market_side": market_side,
        "side_market_regime": side_regime,
        "counterpart_market_regime": "EXPANSION",
        "composite_market_mode": "SPLIT_KOSPI_ON",
        "systemic_risk_off": False,
        "actual_market_action": "WAIT_MARKET" if scenario == "WEAK_SIDE_STRICT_SHADOW" else "BLOCK_NEW_ENTRY",
        "actual_entry_status": "MARKET_WAIT",
        "actual_ready_allowed": False,
        "shadow_scenario": scenario,
        "shadow_variant": variant,
        "shadow_status": "SHADOW_CANDIDATE",
        "counterfactual_action": "OBSERVE_SMALL",
        "trade_stock_role": "LEADER_CONFIRMED",
        "theme_name": theme_name,
        "theme_state": "LEADING_THEME",
        "theme_score": 88.0,
        "relative_strength_vs_index_pct": 4.5,
        "relative_strength_band": "4_TO_6",
        "price_location": "GOOD_PULLBACK",
        "data_quality_status": "OK",
        "reason_codes": ["MARKET_RS_SHADOW_CANDIDATE"],
        "feature_snapshot": {"context": {"session_phase": "MORNING_TREND"}},
    }


def _event_from_decision(decision: dict) -> dict:
    return {
        "decision_id": decision["shadow_decision_id"],
        "trade_date": decision["trade_date"],
        "decision_at": decision["calculated_at"],
        "candidate_id": decision["candidate_id"],
        "candidate_instance_id": decision["candidate_instance_id"],
        "code": decision["code"],
        "name": decision["name"],
        "theme_name": decision["theme_name"],
        "strategy_name": "reboot_v2_market_relative_strength_shadow",
        "gate_status": "OBSERVE",
        "gate_reason": decision["shadow_scenario"],
        "reason_status": decision["shadow_status"],
        "reason_family": "MARKET_RELATIVE_STRENGTH_SHADOW",
        "reason_codes": decision["reason_codes"],
        "action_type": "MARKET_RELATIVE_STRENGTH_SHADOW",
        "action_result": decision["shadow_status"],
        "price": 10000,
        "details": decision,
    }


def _outcome(decision: dict, horizon: int, *, mfe: float, mae: float, ret: float) -> dict:
    return {
        "decision_id": decision["shadow_decision_id"],
        "trade_date": decision["trade_date"],
        "code": decision["code"],
        "candidate_id": decision["candidate_id"],
        "candidate_instance_id": decision["candidate_instance_id"],
        "decision_at": decision["calculated_at"],
        "evaluated_at": decision["calculated_at"],
        "horizon_sec": horizon,
        "price_at_decision": 10000,
        "price_at_horizon": 10000 * (1 + ret / 100),
        "max_price_after_decision": 10000 * (1 + mfe / 100),
        "min_price_after_decision": 10000 * (1 + mae / 100),
        "max_return_pct": mfe,
        "max_drawdown_pct": mae,
        "current_return_pct": ret,
        "outcome_label": "NEUTRAL_OUTCOME",
        "data_status": "OK",
        "details": {"metrics": {"sample_count": 6}},
        "decision_details": decision,
    }
