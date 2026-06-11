from __future__ import annotations

import importlib
from datetime import datetime

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading_app.intraday_outcomes import IntradayOutcomeConfig, IntradayOutcomeLabeler, ThemeLabFlowPricePathProvider


def _decision(decision_id: str, *, gate_status: str = "READY", action_type: str = "READY", reason_codes=None) -> dict:
    return {
        "decision_id": decision_id,
        "runtime_cycle_id": "cycle-1",
        "trade_date": "2026-06-01",
        "decision_at": "2026-06-01T09:00:00",
        "candidate_id": 1,
        "candidate_instance_id": f"ci-{decision_id}",
        "candidate_generation_seq": 3,
        "code": "000001",
        "name": "Alpha",
        "gate_status": gate_status,
        "action_type": action_type,
        "action_result": "ACCEPTED",
        "reason_codes": list(reason_codes or ["PASS"]),
        "price": 100.0,
    }


def _samples(*prices: float) -> list[dict]:
    return [
        {"at": f"2026-06-01T09:0{index}:00", "price": price, "source": "realtime_tick"}
        for index, price in enumerate(prices)
    ]


def test_outcome_labeler_processes_only_due_unlabeled_decisions(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_strategy_decision_events(
            [
                _decision("due-ready"),
                {**_decision("not-due"), "decision_at": "2026-06-01T09:02:00"},
            ]
        )
        labeler = IntradayOutcomeLabeler(
            db,
            config=IntradayOutcomeConfig(horizons_sec=(60,), min_price_samples=2),
            price_provider=lambda _decision, _horizon, _now: _samples(100, 103),
        )

        due = labeler.find_due_decisions(datetime.fromisoformat("2026-06-01T09:01:01"), [60], trade_date="2026-06-01")
        result = labeler.rebuild(trade_date="2026-06-01", horizons_sec=[60], now=datetime.fromisoformat("2026-06-01T09:01:01"))
        due_after = labeler.find_due_decisions(datetime.fromisoformat("2026-06-01T09:01:01"), [60], trade_date="2026-06-01")

        assert [item["decision_id"] for item in due] == ["due-ready"]
        assert result["persisted_count"] == 1
        assert due_after == []
        assert db.get_strategy_decision_outcome("due-ready", 60)["outcome_label"] == "EARLY_TRUE_POSITIVE"
    finally:
        db.close()


def test_outcome_label_rules_cover_entry_block_risk_exit_hold_and_insufficient(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        provider_samples = {
            "ready-tp": _samples(100, 101, 103),
            "ready-fp": _samples(100, 98, 99),
            "blocked-rally": _samples(100, 101, 103),
            "late-chase-effective": _samples(100, 99, 98),
            "exit-early": _samples(100, 101, 103),
            "hold-late": _samples(100, 105, 102),
        }

        def provider(decision, _horizon, _now):
            return provider_samples.get(decision["decision_id"], [])

        labeler = IntradayOutcomeLabeler(db, config=IntradayOutcomeConfig(min_price_samples=2), price_provider=provider)

        cases = [
            (_decision("ready-tp"), "EARLY_TRUE_POSITIVE"),
            (_decision("ready-fp"), "EARLY_FALSE_POSITIVE"),
            (_decision("blocked-rally", gate_status="BLOCKED", action_type="BLOCK", reason_codes=["DATA_INSUFFICIENT"]), "EARLY_OPPORTUNITY_LOSS"),
            (_decision("late-chase-effective", gate_status="BLOCKED", action_type="BLOCK", reason_codes=["LATE_CHASE"]), "RISK_BLOCK_EFFECTIVE"),
            (_decision("exit-early", gate_status="", action_type="EXIT_DECISION", reason_codes=["TAKE_PROFIT"]), "EXIT_TOO_EARLY_CANDIDATE"),
            (_decision("hold-late", gate_status="", action_type="HOLD", reason_codes=["HOLD"]), "EXIT_TOO_LATE_CANDIDATE"),
            (_decision("missing-path"), "INSUFFICIENT_OUTCOME_DATA"),
        ]

        labels = [
            labeler.build_outcome_for_decision(decision, 300, now=datetime.fromisoformat("2026-06-01T09:05:00"))[
                "outcome_label"
            ]
            for decision, _expected in cases
        ]

        assert labels == [expected for _decision_payload, expected in cases]
    finally:
        db.close()


def test_outcome_details_preserve_realtime_reliability_from_decision_details(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        decision = {
            **_decision("realtime-medium"),
            "details": {
                "gate_details": {
                    "realtime_reliability_bucket": "MEDIUM",
                    "realtime_reliability_score": 81.5,
                    "realtime_reliability_gate": {"bucket": "MEDIUM", "status": "SIZE_REDUCED"},
                }
            },
        }
        labeler = IntradayOutcomeLabeler(
            db,
            config=IntradayOutcomeConfig(min_price_samples=2),
            price_provider=lambda _decision, _horizon, _now: _samples(100, 101),
        )

        outcome = labeler.build_outcome_for_decision(
            decision,
            60,
            now=datetime.fromisoformat("2026-06-01T09:01:00"),
        )

        assert outcome["details"]["realtime_reliability_bucket"] == "MEDIUM"
        assert outcome["details"]["realtime_reliability_score"] == 81.5
        assert outcome["details"]["realtime_reliability_gate"]["status"] == "SIZE_REDUCED"
    finally:
        db.close()


def test_theme_lab_flow_price_provider_labels_from_persisted_snapshots(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        decision = _decision("flow-ready")
        db.save_strategy_decision_events([decision])
        db.save_theme_lab_flow_result(
            "2026-06-01T09:00:30",
            {
                "watchset_snapshots": [
                    {"symbol": "000001", "current_price": 101.0, "calculated_at": "2026-06-01T09:00:30"},
                    {"symbol": "000002", "current_price": 50.0, "calculated_at": "2026-06-01T09:00:30"},
                ],
                "gate_decisions": [],
                "condition_hit_snapshots": [],
                "theme_condition_snapshots": [],
                "theme_rankings": [],
            },
        )
        db.save_theme_lab_flow_result(
            "2026-06-01T09:01:00",
            {
                "watchset_snapshots": [
                    {"symbol": "000001", "current_price": 103.0, "calculated_at": "2026-06-01T09:01:00"}
                ],
            },
        )
        db.save_theme_lab_flow_result(
            "2026-06-01T09:02:00",
            {
                "watchset_snapshots": [
                    {"symbol": "000001", "current_price": 80.0, "calculated_at": "2026-06-01T09:02:00"}
                ],
            },
        )

        labeler = IntradayOutcomeLabeler(
            db,
            config=IntradayOutcomeConfig(horizons_sec=(60,), min_price_samples=2),
            price_provider=ThemeLabFlowPricePathProvider(db),
        )
        result = labeler.rebuild(
            trade_date="2026-06-01",
            horizon_sec=60,
            now=datetime.fromisoformat("2026-06-01T09:01:05"),
        )
        outcome = db.get_strategy_decision_outcome("flow-ready", 60)

        assert result["persisted_count"] == 1
        assert outcome["outcome_label"] == "EARLY_TRUE_POSITIVE"
        assert outcome["price_at_horizon"] == 103.0
        assert outcome["data_status"] in {"SPARSE", "OK"}
    finally:
        db.close()


def test_theme_lab_flow_price_provider_does_not_use_future_snapshots(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        decision = _decision("future-only")
        db.save_strategy_decision_events([decision])
        db.save_theme_lab_flow_result(
            "2026-06-01T09:02:00",
            {
                "watchset_snapshots": [
                    {"symbol": "000001", "current_price": 103.0, "calculated_at": "2026-06-01T09:02:00"}
                ],
            },
        )

        labeler = IntradayOutcomeLabeler(
            db,
            config=IntradayOutcomeConfig(horizons_sec=(60,), min_price_samples=2),
            price_provider=ThemeLabFlowPricePathProvider(db),
        )
        result = labeler.rebuild(
            trade_date="2026-06-01",
            horizon_sec=60,
            now=datetime.fromisoformat("2026-06-01T09:01:05"),
        )
        outcome = db.get_strategy_decision_outcome("future-only", 60)

        assert result["persisted_count"] == 1
        assert outcome["outcome_label"] == "INSUFFICIENT_OUTCOME_DATA"
        assert outcome["data_quality_issues"] == ["INSUFFICIENT_PRICE_SAMPLES"]
    finally:
        db.close()


def test_strategy_decision_outcome_db_prevents_duplicates_and_force_updates(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_strategy_decision_events([_decision("dup")])
        first = {
            **_decision("dup"),
            "outcome_id": "outcome:dup:60",
            "evaluated_at": "2026-06-01T09:01:00",
            "horizon_sec": 60,
            "price_at_decision": 100,
            "price_at_horizon": 103,
            "max_return_pct": 3.0,
            "max_drawdown_pct": 0.0,
            "current_return_pct": 3.0,
            "outcome_label": "EARLY_TRUE_POSITIVE",
            "data_status": "OK",
            "source": "realtime_tick",
        }
        second = {**first, "outcome_label": "EARLY_FALSE_POSITIVE", "current_return_pct": -1.0}

        assert db.save_strategy_decision_outcomes([first]) == 1
        assert db.save_strategy_decision_outcomes([second]) == 0
        assert db.get_strategy_decision_outcome("dup", 60)["outcome_label"] == "EARLY_TRUE_POSITIVE"

        db.save_strategy_decision_outcomes([second], force=True)
        assert db.get_strategy_decision_outcome("dup", 60)["outcome_label"] == "EARLY_FALSE_POSITIVE"
    finally:
        db.close()


def test_intraday_outcome_api_summary_filters_and_rebuild(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    db = TradingDatabase(str(db_path))
    try:
        ready = _decision("api-ready")
        blocked = _decision("api-blocked", gate_status="BLOCKED", action_type="BLOCK", reason_codes=["LATE_CHASE"])
        db.save_strategy_decision_events([ready, blocked])
        db.save_strategy_decision_outcomes(
            [
                {
                    **ready,
                    "outcome_id": "outcome:api-ready:60",
                    "evaluated_at": "2026-06-01T09:01:00",
                    "horizon_sec": 60,
                    "price_at_decision": 100,
                    "price_at_horizon": 103,
                    "max_return_pct": 3.0,
                    "max_drawdown_pct": 0.0,
                    "current_return_pct": 3.0,
                    "outcome_label": "EARLY_TRUE_POSITIVE",
                    "data_status": "OK",
                    "source": "realtime_tick",
                },
                {
                    **blocked,
                    "outcome_id": "outcome:api-blocked:60",
                    "evaluated_at": "2026-06-01T09:01:00",
                    "horizon_sec": 60,
                    "price_at_decision": 100,
                    "price_at_horizon": 98,
                    "max_return_pct": 0.0,
                    "max_drawdown_pct": -2.0,
                    "current_return_pct": -2.0,
                    "outcome_label": "RISK_BLOCK_EFFECTIVE",
                    "data_status": "OK",
                    "source": "realtime_tick",
                },
            ]
        )
    finally:
        db.close()

    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_INTRADAY_OUTCOME_HORIZONS_SEC", "120")
    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        rows = client.get(
            "/api/runtime/outcomes/intraday",
            params={"trade_date": "2026-06-01", "reason_code": "LATE_CHASE"},
        ).json()
        summary = client.get("/api/runtime/outcomes/intraday/summary", params={"trade_date": "2026-06-01"}).json()[
            "summary"
        ]
        rebuild = client.post(
            "/api/runtime/outcomes/intraday/rebuild",
            params={"trade_date": "2026-06-01", "horizon_sec": 120, "limit": 10},
        ).json()

    assert rows["pagination"]["total"] == 1
    assert rows["items"][0]["outcome_label"] == "RISK_BLOCK_EFFECTIVE"
    assert summary["early_true_positive_count"] == 1
    assert summary["by_label"]["RISK_BLOCK_EFFECTIVE"] == 1
    assert {"reason": "LATE_CHASE", "count": 1} in summary["top_effective_risk_blocks"]
    assert rebuild["persisted_count"] == 2
