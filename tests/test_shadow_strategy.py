from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading_app.shadow_strategy import ShadowStrategyConfig, ShadowStrategyEvaluator


def _decision(
    decision_id: str,
    *,
    gate_status: str = "WAIT",
    action_type: str = "WAIT",
    reason_codes=None,
    role: str = "LEADER",
    theme_score: float = 82.0,
    hybrid_score: float = 75.0,
    gate_score: float = 74.0,
    details: dict | None = None,
) -> dict:
    payload = {
        "decision_id": decision_id,
        "runtime_cycle_id": "cycle-1",
        "trade_date": "2026-06-01",
        "decision_at": "2026-06-01T09:00:00",
        "candidate_id": 7,
        "candidate_instance_id": f"ci-{decision_id}",
        "candidate_generation_seq": 2,
        "code": "000001",
        "name": "Alpha",
        "theme_name": "Robot",
        "gate_status": gate_status,
        "action_type": action_type,
        "action_result": "ACCEPTED",
        "reason_codes": list(reason_codes or []),
        "price": 100.0,
        "change_rate": 4.0,
        "trade_value": 1_000_000.0,
        "execution_strength": 120.0,
        "momentum_1m": 0.8,
        "momentum_3m": 1.1,
        "theme_score": theme_score,
        "hybrid_score": hybrid_score,
        "gate_score": gate_score,
        "details": {"gate_details": {"stock_role": role}},
    }
    if details:
        payload["details"].update(details)
    return payload


def test_shadow_policy_loading_and_rule_evaluation(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        evaluator = ShadowStrategyEvaluator(db, config=ShadowStrategyConfig())
        policies = {policy.policy_id: policy for policy in evaluator.load_policies(include_baseline=True)}

        relaxed = evaluator.evaluate_decision(
            _decision("risk-off", gate_status="WAIT", action_type="WAIT", reason_codes=["RISK_OFF"]),
            policies["relaxed_risk_off_leader"],
        )
        late = evaluator.evaluate_decision(
            _decision("late", gate_status="READY", action_type="READY", reason_codes=["LATE_CHASE"]),
            policies["strict_late_chase"],
        )
        entry_risk = evaluator.evaluate_decision(
            _decision("vi", gate_status="READY", action_type="READY", reason_codes=["VI_ACTIVE"]),
            policies["strict_entry_risk"],
        )
        data_wait = evaluator.evaluate_decision(
            _decision("data", gate_status="WAIT", action_type="WAIT", reason_codes=["DATA_INSUFFICIENT"]),
            policies["relaxed_data_wait_for_leader"],
        )
        fast_exit = evaluator.evaluate_decision(
            _decision(
                "hold",
                gate_status="",
                action_type="HOLD",
                reason_codes=["HOLD"],
                details={"action_details": {"current_return_pct": 1.0, "max_return_pct": 4.0}},
            ),
            policies["fast_theme_exit_shadow"],
        )

        assert "baseline" in policies
        assert relaxed["shadow_gate_status"] == "OBSERVE_READY"
        assert relaxed["change_type"] == "WAIT_TO_READY"
        assert relaxed["shadow_action_type"] == "SHADOW_ENTRY_CANDIDATE"
        assert late["shadow_gate_status"] == "BLOCKED"
        assert late["change_type"] == "READY_TO_BLOCK"
        assert entry_risk["shadow_gate_status"] == "BLOCKED"
        assert data_wait["shadow_gate_status"] == "OBSERVE_READY"
        assert fast_exit["shadow_action_type"] == "SHADOW_EXIT"
        assert fast_exit["change_type"] == "HOLD_TO_EXIT"
    finally:
        db.close()


def test_shadow_rebuild_persists_without_order_side_effects_and_prevents_duplicates(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_strategy_decision_events(
            [
                _decision("late-ready", gate_status="READY", action_type="READY", reason_codes=["LATE_CHASE"]),
            ]
        )
        evaluator = ShadowStrategyEvaluator(
            db,
            config=ShadowStrategyConfig(policy_ids=("strict_late_chase",), rebuild_limit=100),
        )

        result = evaluator.rebuild(trade_date="2026-06-01", policy_id="strict_late_chase")
        duplicate = evaluator.rebuild(trade_date="2026-06-01", policy_id="strict_late_chase")
        force = evaluator.rebuild(trade_date="2026-06-01", policy_id="strict_late_chase", force=True)
        rows = db.list_shadow_strategy_evaluations(trade_date="2026-06-01", policy_id="strict_late_chase")

        assert result["persisted_count"] == 1
        assert duplicate["decision_count"] == 0
        assert force["persisted_count"] >= 1
        assert rows[0]["change_type"] == "READY_TO_BLOCK"
        assert db.list_runtime_order_intents(limit=10) == []
        assert db.conn.execute("SELECT COUNT(*) AS count FROM virtual_orders").fetchone()["count"] == 0
        assert db.conn.execute("SELECT COUNT(*) AS count FROM virtual_positions").fetchone()["count"] == 0
    finally:
        db.close()


def test_shadow_summary_joins_outcomes_and_ranks_policies(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        risk_off = _decision("risk-off", gate_status="WAIT", action_type="WAIT", reason_codes=["RISK_OFF"])
        late = _decision("late-ready", gate_status="READY", action_type="READY", reason_codes=["LATE_CHASE"])
        db.save_strategy_decision_events([risk_off, late])
        evaluator = ShadowStrategyEvaluator(
            db,
            config=ShadowStrategyConfig(policy_ids=("relaxed_risk_off_leader", "strict_late_chase"), rebuild_limit=100),
        )
        evaluator.rebuild(trade_date="2026-06-01")
        db.save_strategy_decision_outcomes(
            [
                {
                    **risk_off,
                    "outcome_id": "outcome:risk-off:60",
                    "evaluated_at": "2026-06-01T09:01:00",
                    "horizon_sec": 60,
                    "price_at_decision": 100,
                    "price_at_horizon": 104,
                    "max_return_pct": 4.0,
                    "max_drawdown_pct": 0.0,
                    "current_return_pct": 4.0,
                    "outcome_label": "EARLY_OPPORTUNITY_LOSS",
                    "data_status": "OK",
                    "source": "realtime_tick",
                },
                {
                    **late,
                    "outcome_id": "outcome:late-ready:60",
                    "evaluated_at": "2026-06-01T09:01:00",
                    "horizon_sec": 60,
                    "price_at_decision": 100,
                    "price_at_horizon": 98,
                    "max_return_pct": 0.0,
                    "max_drawdown_pct": -2.0,
                    "current_return_pct": -2.0,
                    "outcome_label": "EARLY_FALSE_POSITIVE",
                    "data_status": "OK",
                    "source": "realtime_tick",
                },
            ]
        )

        summary = db.shadow_strategy_summary(trade_date="2026-06-01", horizon_sec=60)
        ranking = {item["policy_id"]: item for item in summary["policy_ranking"]}

        assert summary["changed_decision_count"] >= 2
        assert summary["estimated_opportunity_loss_reduced_count"] == 1
        assert summary["estimated_risk_block_effective_count"] == 1
        assert ranking["relaxed_risk_off_leader"]["opportunity_loss_reduced_count"] == 1
        assert ranking["strict_late_chase"]["risk_block_effective_count"] == 1
        assert ranking["strict_late_chase"]["recommendation_grade"] in {"WATCH_CANDIDATE", "STRONG_CANDIDATE"}
    finally:
        db.close()


def test_shadow_strategy_api_filters_rebuild_and_dashboard_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    db = TradingDatabase(str(db_path))
    try:
        db.save_strategy_decision_events(
            [
                _decision("api-late", gate_status="READY", action_type="READY", reason_codes=["LATE_CHASE"]),
            ]
        )
    finally:
        db.close()

    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_SHADOW_STRATEGY_POLICIES", "strict_late_chase")
    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        policies = client.get("/api/runtime/shadow-strategies/policies").json()
        rebuild = client.post(
            "/api/runtime/shadow-strategies/rebuild",
            params={"trade_date": "2026-06-01", "policy_id": "strict_late_chase"},
        ).json()
        rows = client.get(
            "/api/runtime/shadow-strategies/evaluations",
            params={"trade_date": "2026-06-01", "change_type": "READY_TO_BLOCK", "changed_decision": True},
        ).json()
        summary = client.get("/api/runtime/shadow-strategies/summary", params={"trade_date": "2026-06-01"}).json()[
            "summary"
        ]
        snapshot = client.get("/api/snapshot").json()

    assert {item["policy_id"] for item in policies["policies"]} == {"baseline", "strict_late_chase"}
    assert policies["allow_apply"] is False
    assert rebuild["persisted_count"] == 1
    assert rows["pagination"]["total"] == 1
    assert rows["items"][0]["shadow_gate_status"] == "BLOCKED"
    assert summary["by_change_type"]["READY_TO_BLOCK"] == 1
    assert "shadow_strategies" in snapshot
