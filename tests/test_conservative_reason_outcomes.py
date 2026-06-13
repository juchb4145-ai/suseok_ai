import importlib
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading_app.conservative_reason_outcomes import (
    ConservativeReasonOutcomeAnalyzer,
    ConservativeReasonOutcomeConfig,
    normalize_conservative_reason_group,
)
from trading_app.strategy_change_proposals import StrategyChangeProposalGenerator


START = datetime(2026, 6, 1, 9, 1, 0)


def test_conservative_reason_group_classification_supports_multi_group():
    result = normalize_conservative_reason_group(
        ["LOW_BREADTH", "LATE_CHASE_TEMP_WAIT", "WAIT_DATA_SUPPORT_NOT_READY"],
        {"candidate_market_status": "RISK_OFF"},
    )

    assert result["primary_group"] == "MARKET_RISK"
    assert set(result["all_groups"]) >= {"MARKET_RISK", "BREADTH_RISK", "CHASE_RISK", "DATA_QUALITY_RISK"}


def test_warmup_optional_high_mfe_low_mae_reviews_for_small_entry(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        for minute in range(12):
            _save_snapshot(
                db,
                START + timedelta(minutes=minute),
                [
                    _watch(
                        code=f"10{minute:04d}",
                        reason="WAIT_PRICE_LOCATION_WARMUP",
                        data_quality_bucket="WARMUP_OPTIONAL",
                        price_location_status="PULLBACK_RECLAIM",
                    )
                ],
            )
        observations = []
        for minute in range(12):
            code = f"10{minute:04d}"
            observations.extend(
                [
                    _obs(code, START + timedelta(minutes=minute + 5), 101),
                    _obs(code, START + timedelta(minutes=minute + 15), 104),
                ]
            )
        db.save_theme_lab_outcome_observations(observations)

        report = ConservativeReasonOutcomeAnalyzer(
            db,
            config=ConservativeReasonOutcomeConfig(min_sample_count=2, data_quality_min_observation_count=2),
        ).build_report(trade_date="2026-06-01")

        warmup = {row["data_quality_bucket"]: row for row in report["data_quality_bucket_summary"]}["WARMUP_OPTIONAL"]
        assert warmup["recommendation"] == "REVIEW_FOR_SMALL_ENTRY"
        assert report["review_for_small_entry"]["summary"]["candidate_count"] == 12
        assert report["summary"]["missed_opportunity_count"] == 12
    finally:
        db.close()


def test_chase_high_with_bad_mae_keeps_block(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        for minute in range(12):
            _save_snapshot(db, START + timedelta(minutes=minute), [_watch(code=f"20{minute:04d}", reason="CHASE_HIGH", status="BLOCKED")])
        db.save_theme_lab_outcome_observations(
            [
                event
                for minute in range(12)
                for event in (
                    _obs(f"20{minute:04d}", START + timedelta(minutes=minute + 5), 99),
                    _obs(f"20{minute:04d}", START + timedelta(minutes=minute + 15), 97),
                )
            ]
        )

        report = ConservativeReasonOutcomeAnalyzer(
            db,
            config=ConservativeReasonOutcomeConfig(min_sample_count=2, data_quality_min_observation_count=2),
        ).build_report(trade_date="2026-06-01")
        chase = next(row for row in report["by_reason_code"] if row["reason_code"] == "CHASE_HIGH")

        assert chase["recommendation"] == "KEEP_BLOCK"
        assert report["summary"]["risk_avoided_count"] == 12
        assert report["items"][0]["outcome_label"] in {"CHASE_PROTECTED", "GOOD_BLOCK"}
    finally:
        db.close()


def test_risk_off_low_sample_requests_more_samples(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        _save_snapshot(db, START, [_watch(code="000301", reason="RISK_OFF", status="BLOCKED", candidate_market_status="RISK_OFF")])
        db.save_theme_lab_outcome_observations([_obs("000301", START + timedelta(minutes=5), 101), _obs("000301", START + timedelta(minutes=15), 101)])

        report = ConservativeReasonOutcomeAnalyzer(db).build_report(trade_date="2026-06-01")
        market = next(row for row in report["by_group"] if row["group"] == "MARKET_RISK")

        assert market["recommendation"] == "DATA_INSUFFICIENT_MORE_SAMPLES"
    finally:
        db.close()


def test_low_breadth_leader_and_follower_are_separable(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        _save_snapshot(
            db,
            START,
            [
                _watch(code="000401", reason="LOW_BREADTH", stock_role="LEADER"),
                _watch(code="000402", reason="LOW_BREADTH", stock_role="FOLLOWER"),
            ],
        )
        db.save_theme_lab_outcome_observations(
            [
                _obs("000401", START + timedelta(minutes=5), 101),
                _obs("000401", START + timedelta(minutes=15), 104),
                _obs("000402", START + timedelta(minutes=5), 100),
                _obs("000402", START + timedelta(minutes=15), 99),
            ]
        )

        report = ConservativeReasonOutcomeAnalyzer(
            db,
            config=ConservativeReasonOutcomeConfig(min_sample_count=1, data_quality_min_observation_count=2),
        ).build_report(trade_date="2026-06-01")

        leader = next(item for item in report["items"] if item["code"] == "000401")
        follower = next(item for item in report["items"] if item["code"] == "000402")
        assert leader["stock_role"] == "LEADER"
        assert follower["stock_role"] == "FOLLOWER"
        assert leader["missed_opportunity"] is True
        assert "BREADTH_RISK" in leader["all_groups"]
    finally:
        db.close()


def test_labels_and_exports(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        _save_snapshot(db, START, [_watch(code="000501", reason="WAIT_DATA_SUPPORT_NOT_READY")])
        db.save_theme_lab_outcome_observations([_obs("000501", START + timedelta(minutes=5), 102), _obs("000501", START + timedelta(minutes=15), 105)])
        analyzer = ConservativeReasonOutcomeAnalyzer(
            db,
            config=ConservativeReasonOutcomeConfig(min_sample_count=1, data_quality_min_observation_count=2),
            report_root=tmp_path / "reports",
        )
        report = analyzer.build_report(trade_date="2026-06-01")
        exports = analyzer.export_report(report, fmt="all")

        assert report["items"][0]["outcome_label"] == "STRONG_MISSED_OPPORTUNITY"
        assert Path(exports["json"]).exists()
        assert Path(exports["csv"]).exists()
        assert Path(exports["md"]).exists()
    finally:
        db.close()


def test_api_snapshot_items_filters_and_strategy_change_evidence(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    db = TradingDatabase(str(db_path))
    try:
        for minute in range(12):
            _save_snapshot(
                db,
                START + timedelta(minutes=minute),
                [
                    _watch(
                        code=f"60{minute:04d}",
                        reason="WAIT_DATA_SUPPORT_NOT_READY",
                        data_quality_bucket="WARMUP_OPTIONAL",
                        price_location_status="PULLBACK_RECLAIM",
                    )
                ],
            )
        db.save_theme_lab_outcome_observations(
            [
                event
                for minute in range(12)
                for event in (
                    _obs(f"60{minute:04d}", START + timedelta(minutes=minute + 5), 101),
                    _obs(f"60{minute:04d}", START + timedelta(minutes=minute + 15), 104),
                )
            ]
        )

        generator = StrategyChangeProposalGenerator(db)
        generated = generator.generate(trade_date="2026-06-01", source_type="conservative_reason_outcome")
        evidence_metrics = {evidence["metric_name"] for proposal in generated["proposals"] for evidence in proposal["evidence"]}
        assert "conservative_reason_event_count" in evidence_metrics
        assert "small_entry_candidate_count" in evidence_metrics
        assert all(proposal["status"] == "REVIEW_READY" for proposal in generated["proposals"])
    finally:
        db.close()

    import trading_app.api as api

    api = importlib.reload(api)
    client = TestClient(api.app)

    summary = client.get("/api/conservative-reason-outcomes/summary", params={"trade_date": "2026-06-01"}).json()
    assert summary["available"] is True
    assert "conservative_reason_outcomes" in client.get("/api/snapshot").json()

    by_group = client.get(
        "/api/conservative-reason-outcomes/items",
        params={"trade_date": "2026-06-01", "reason_group": "DATA_QUALITY_RISK"},
    ).json()
    by_reason = client.get(
        "/api/conservative-reason-outcomes/items",
        params={"trade_date": "2026-06-01", "reason_code": "WAIT_DATA_SUPPORT_NOT_READY"},
    ).json()
    by_code = client.get(
        "/api/conservative-reason-outcomes/items",
        params={"trade_date": "2026-06-01", "code": "600000"},
    ).json()
    assert by_group["total"] == 12
    assert by_reason["total"] == 12
    assert by_code["total"] == 1


def _watch(
    *,
    code: str,
    reason: str,
    status: str = "WAIT",
    stock_role: str = "LEADER",
    risk_level: str = "PASS",
    data_quality_bucket: str = "OK",
    price_location_status: str = "PULLBACK_RECLAIM",
    candidate_market_status: str = "HEALTHY",
) -> dict:
    return {
        "symbol": code,
        "name": f"name-{code}",
        "current_price": 100,
        "final_gate_status": status,
        "reason_codes": [reason],
        "primary_theme": "AI",
        "stock_role": stock_role,
        "risk_level": risk_level,
        "condition_level": 3,
        "data_quality_bucket": data_quality_bucket,
        "price_location_status": price_location_status,
        "price_location_readiness": "PROVISIONAL",
        "candidate_market_status": candidate_market_status,
    }


def _obs(code: str, at: datetime, price: float) -> dict:
    return {
        "observed_at": at.isoformat(),
        "trade_date": "2026-06-01",
        "stock_code": code,
        "price": price,
        "source": "theme_lab_outcome_tracking",
    }


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
