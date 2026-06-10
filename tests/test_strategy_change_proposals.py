from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading_app.strategy_change_proposals import (
    StrategyChangeProposalConfig,
    StrategyChangeProposalGenerator,
    StrategyConfigPatchBuilder,
)


def _decision(decision_id: str) -> dict:
    return {
        "decision_id": decision_id,
        "runtime_cycle_id": "cycle-1",
        "trade_date": "2026-06-01",
        "decision_at": "2026-06-01T09:00:00",
        "candidate_id": 1,
        "candidate_instance_id": f"ci-{decision_id}",
        "candidate_generation_seq": 1,
        "code": "000001",
        "name": "Alpha",
        "theme_name": "Robot",
        "gate_status": "READY",
        "action_type": "READY",
        "action_result": "ACCEPTED",
        "reason_codes": ["LATE_CHASE"],
        "price": 100,
    }


def _seed_shadow(db: TradingDatabase, *, policy_id: str = "strict_late_chase", outcome_label: str = "EARLY_FALSE_POSITIVE") -> None:
    decision = _decision(policy_id)
    db.save_strategy_decision_events([decision])
    db.save_shadow_strategy_evaluations(
        [
            {
                "evaluation_id": f"shadow:{policy_id}",
                "trade_date": "2026-06-01",
                "evaluated_at": "2026-06-01T09:01:00",
                "runtime_cycle_id": "cycle-1",
                "decision_id": decision["decision_id"],
                "policy_id": policy_id,
                "policy_name": policy_id,
                "candidate_id": 1,
                "candidate_instance_id": decision["candidate_instance_id"],
                "candidate_generation_seq": 1,
                "code": "000001",
                "name": "Alpha",
                "theme_name": "Robot",
                "baseline_gate_status": "READY",
                "baseline_action_type": "READY",
                "baseline_reason_codes": ["LATE_CHASE"],
                "shadow_gate_status": "BLOCKED",
                "shadow_action_type": "BLOCK",
                "shadow_reason_codes": ["LATE_CHASE", "SHADOW_STRICT_LATE_CHASE"],
                "changed_decision": True,
                "change_type": "READY_TO_BLOCK",
                "expected_effect": "risk_block_effective",
                "expected_risk": "opportunity_loss_possible",
                "data_status": "OK",
            }
        ],
        force=True,
    )
    db.save_strategy_decision_outcomes(
        [
            {
                **decision,
                "outcome_id": f"outcome:{policy_id}:60",
                "evaluated_at": "2026-06-01T09:01:00",
                "horizon_sec": 60,
                "price_at_decision": 100,
                "price_at_horizon": 98,
                "max_return_pct": 0,
                "max_drawdown_pct": -2,
                "current_return_pct": -2,
                "outcome_label": outcome_label,
                "data_status": "OK",
                "source": "test",
            }
        ],
        force=True,
    )


def _generator(db: TradingDatabase, **overrides) -> StrategyChangeProposalGenerator:
    config = StrategyChangeProposalConfig(
        min_sample_count=overrides.pop("min_sample_count", 1),
        min_trade_days=overrides.pop("min_trade_days", 1),
        min_replay_count=overrides.pop("min_replay_count", 0),
        strong_min_confidence=overrides.pop("strong_min_confidence", 0.0),
        **overrides,
    )
    return StrategyChangeProposalGenerator(db, config=config)


def test_intraday_outcome_summary_generates_proposals(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        blocked = {**_decision("blocked-rally"), "gate_status": "BLOCKED", "action_type": "BLOCK", "reason_codes": ["DATA_INSUFFICIENT"]}
        hold = {**_decision("hold-late"), "gate_status": "", "action_type": "HOLD", "reason_codes": ["HOLD"]}
        db.save_strategy_decision_events([blocked, hold])
        db.save_strategy_decision_outcomes(
            [
                {
                    **blocked,
                    "outcome_id": "outcome:blocked-rally:60",
                    "evaluated_at": "2026-06-01T09:01:00",
                    "horizon_sec": 60,
                    "price_at_decision": 100,
                    "price_at_horizon": 104,
                    "max_return_pct": 4,
                    "max_drawdown_pct": 0,
                    "current_return_pct": 4,
                    "outcome_label": "EARLY_OPPORTUNITY_LOSS",
                    "data_status": "OK",
                    "source": "test",
                },
                {
                    **hold,
                    "outcome_id": "outcome:hold-late:60",
                    "evaluated_at": "2026-06-01T09:01:00",
                    "horizon_sec": 60,
                    "price_at_decision": 100,
                    "price_at_horizon": 101,
                    "max_return_pct": 5,
                    "max_drawdown_pct": 0,
                    "current_return_pct": 1,
                    "outcome_label": "EXIT_TOO_LATE_CANDIDATE",
                    "data_status": "OK",
                    "source": "test",
                },
            ],
            force=True,
        )

        result = _generator(db).generate(trade_date="2026-06-01", source_type="intraday_outcome")

        assert result["proposal_count"] == 2
        assert {item["source_type"] for item in result["proposals"]} == {"intraday_outcome"}
        assert {item["target_component"] for item in result["proposals"]} == {"data_quality_gate", "exit_engine"}
        assert all(item["evidence"] for item in result["proposals"])
    finally:
        db.close()


def test_shadow_summary_generates_persists_and_dedupes_proposal(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        _seed_shadow(db)
        generator = _generator(db)

        first = generator.generate(trade_date="2026-06-01", source_type="shadow_strategy")
        second = generator.generate(trade_date="2026-06-01", source_type="shadow_strategy")
        rows = db.list_strategy_change_proposals(trade_date="2026-06-01")
        evidence = db.list_strategy_change_evidence(rows[0]["proposal_id"])

        assert first["proposal_count"] == 1
        assert second["proposal_count"] == 1
        assert len(rows) == 1
        assert rows[0]["category"] == "risk"
        assert rows[0]["target_component"] == "entry_gate"
        assert rows[0]["candidate_config_patch"]["entry_gate.late_chase.block_followers"] is True
        assert evidence
    finally:
        db.close()


def test_replay_report_generates_proposal_with_replay_evidence(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_strategy_replay_report(
            {
                "report_id": "replay_report_1",
                "replay_id": "replay-1",
                "trade_date": "2026-06-01",
                "mode": "decision_led",
                "summary": {"status": "READY"},
                "recommendations": [
                    {
                        "policy_id": "strict_late_chase",
                        "policy_name": "strict_late_chase",
                        "changed_decision_count": 3,
                        "estimated_opportunity_loss_reduced_count": 0,
                        "estimated_false_positive_increase_count": 0,
                        "risk_block_effective_count": 3,
                        "net_benefit_score": 3.0,
                        "confidence": 0.8,
                        "recommendation_grade": "STRONG_CANDIDATE",
                    }
                ],
            }
        )
        result = _generator(db).generate(trade_date="2026-06-01", source_type="replay")

        proposal = result["proposals"][0]
        assert proposal["source_type"] == "replay"
        assert proposal["recommendation_grade"] == "STRONG_CANDIDATE"
        assert any(item["source_type"] == "replay" for item in proposal["evidence"])
    finally:
        db.close()


def test_threshold_ab_result_is_converted_and_reguarded(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_dry_run_threshold_ab_report(
            {
                "report_id": "threshold_ab_1",
                "trade_date": "2026-06-01",
                "status": "READY",
                "summary": {},
                "candidates": [
                    {
                        "candidate_id": "late_chase_block",
                        "category": "risk",
                        "parameter_name": "late_chase.block_followers",
                        "baseline_value": False,
                        "candidate_value": True,
                        "recommendation_grade": "WATCH_CANDIDATE",
                        "confidence": 0.8,
                        "sample_count": 5,
                    }
                ],
                "results": {
                    "late_chase_block": {
                        "recommendation": {
                            "grade": "WATCH_CANDIDATE",
                            "sample_count": 5,
                            "confidence": 0.8,
                            "expected_net_benefit_score": 2.0,
                            "sample_trade_days": 2,
                        },
                        "delta": {"avoided_false_positive_count": 2, "newly_created_false_negative_count": 0},
                    }
                },
            }
        )
        result = _generator(db).generate(trade_date="2026-06-01", source_type="threshold_ab")

        proposal = result["proposals"][0]
        assert proposal["source_type"] == "dry_run_threshold_ab"
        assert proposal["candidate_config_patch"]["entry_risk_gate.threshold_ab.late_chase.block_followers"] is True
        assert proposal["guardrail_passed"] is True
    finally:
        db.close()


def test_guardrails_cover_sample_fp_forbidden_and_observe_only(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        generator = StrategyChangeProposalGenerator(db, config=StrategyChangeProposalConfig(min_sample_count=20))
        low_sample = generator._build_proposal(
            trade_date="2026-06-01",
            source_type="shadow_strategy",
            source_ids=["shadow:low"],
            policy_key="strict_late_chase",
            evidence_metrics={"sample_count": 1, "false_positive_increase_count": 0, "net_benefit_score": 1, "confidence": 0.8},
            raw_source={},
        )
        fp_risk = generator._build_proposal(
            trade_date="2026-06-01",
            source_type="shadow_strategy",
            source_ids=["shadow:fp"],
            policy_key="strict_late_chase",
            evidence_metrics={"sample_count": 30, "false_positive_increase_count": 2, "net_benefit_score": 1, "confidence": 0.8},
            raw_source={},
        )
        forbidden = generator._build_proposal(
            trade_date="2026-06-01",
            source_type="shadow_strategy",
            source_ids=["shadow:forbidden"],
            policy_key="strict_late_chase",
            override_template={
                "title": "bad",
                "summary_ko": "bad",
                "category": "risk",
                "target_component": "runtime",
                "expected_effect_ko": "",
                "expected_risk_ko": "",
                "patch": {"live_order_enabled": True},
            },
            evidence_metrics={"sample_count": 30, "false_positive_increase_count": 0, "net_benefit_score": 5, "confidence": 0.9},
            raw_source={},
        )
        observe_false = generator._build_proposal(
            trade_date="2026-06-01",
            source_type="shadow_strategy",
            source_ids=["shadow:observe"],
            policy_key="strict_late_chase",
            override_template={
                "title": "bad",
                "summary_ko": "bad",
                "category": "gate",
                "target_component": "market_gate",
                "expected_effect_ko": "",
                "expected_risk_ko": "",
                "patch": {"market_gate.risk_off.observe_only": False},
            },
            evidence_metrics={"sample_count": 30, "false_positive_increase_count": 0, "net_benefit_score": 5, "confidence": 0.9},
            raw_source={},
        )

        assert low_sample.recommendation_grade == "DATA_INSUFFICIENT"
        assert fp_risk.recommendation_grade in {"RISKY_CANDIDATE", "DO_NOT_APPLY"}
        assert forbidden.recommendation_grade == "DO_NOT_APPLY"
        assert "FORBIDDEN_CONFIG_KEY" in forbidden.blocked_by_guardrail_reason
        assert observe_false.recommendation_grade == "DO_NOT_APPLY"
    finally:
        db.close()


def test_config_diff_preview_masks_forbidden_keys():
    preview = StrategyConfigPatchBuilder().build_patch({"live_order_enabled": True, "entry_gate.late_chase.block_followers": True})

    assert "live_order_enabled" in preview.forbidden_keys
    assert any(row["path"] == "live_order_enabled" and row["after"] == "***" for row in preview.diff)
    assert any(row["path"] == "entry_gate.late_chase.block_followers" and row["after"] is True for row in preview.diff)


def test_change_proposal_api_generate_filters_approval_and_token(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    db = TradingDatabase(str(db_path))
    try:
        _seed_shadow(db)
    finally:
        db.close()

    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "secret-token")
    monkeypatch.setenv("TRADING_CHANGE_PROPOSAL_MIN_SAMPLE_COUNT", "1")
    monkeypatch.setenv("TRADING_CHANGE_PROPOSAL_MIN_TRADE_DAYS", "1")
    monkeypatch.setenv("TRADING_CHANGE_PROPOSAL_MIN_REPLAY_COUNT", "0")
    import trading_app.api as api

    api = importlib.reload(api)
    api.DEFAULT_REPLAY_DB_ROOT = tmp_path / "replay"
    db = TradingDatabase(str(db_path))
    try:
        settings_before = [
            dict(row)
            for row in db.conn.execute(
                "SELECT config_key, config_version, config_json, settings_json FROM strategy_runtime_settings ORDER BY config_key"
            ).fetchall()
        ]
    finally:
        db.close()

    with TestClient(api.app) as client:
        unauthorized = client.post("/api/runtime/change-proposals/generate", params={"trade_date": "2026-06-01"})
        generated = client.post(
            "/api/runtime/change-proposals/generate",
            params={"trade_date": "2026-06-01", "source_type": "shadow_strategy"},
            headers={"X-Local-Token": "secret-token"},
        ).json()
        rows = client.get(
            "/api/runtime/change-proposals",
            params={"trade_date": "2026-06-01", "category": "risk"},
        ).json()
        summary = client.get("/api/runtime/change-proposals/summary", params={"trade_date": "2026-06-01"}).json()
        proposal_id = rows["items"][0]["proposal_id"]
        detail = client.get(f"/api/runtime/change-proposals/{proposal_id}").json()
        approved = client.post(
            f"/api/runtime/change-proposals/{proposal_id}/approve-observe",
            headers={"X-Local-Token": "secret-token"},
            json={"operator": "tester", "note": "observe only"},
        ).json()
        note = client.post(
            f"/api/runtime/change-proposals/{proposal_id}/note",
            headers={"X-Local-Token": "secret-token"},
            json={"operator": "tester", "note": "tracking"},
        ).json()

    db = TradingDatabase(str(db_path))
    try:
        settings_after = [
            dict(row)
            for row in db.conn.execute(
                "SELECT config_key, config_version, config_json, settings_json FROM strategy_runtime_settings ORDER BY config_key"
            ).fetchall()
        ]
        assert unauthorized.status_code == 401
        assert generated["proposal_count"] == 1
        assert rows["pagination"]["total"] == 1
        assert summary["summary"]["total_count"] == 1
        assert summary["summary"]["by_status"]["REVIEW_READY"] == 1
        assert detail["config_diff"]["diff"]
        assert approved["proposal"]["status"] == "APPROVED_FOR_OBSERVE"
        assert approved["config_changed"] is False
        assert note["approval"]["action"] == "note"
        assert settings_after == settings_before
        assert len(db.list_strategy_change_approvals(proposal_id)) == 2
    finally:
        db.close()
