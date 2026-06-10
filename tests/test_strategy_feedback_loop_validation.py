from __future__ import annotations

import importlib
import json
import sqlite3
from argparse import Namespace

from fastapi.testclient import TestClient

from tools.validate_strategy_feedback_loop import (
    render_markdown_report,
    run_validation,
    validate_approval_config_unchanged,
)


def _args(db_path, **overrides) -> Namespace:
    values = {
        "db": str(db_path),
        "base_url": "http://127.0.0.1:9",
        "trade_date": "2026-06-01",
        "token": "",
        "skip_api": True,
        "skip_db": False,
        "skip_replay": False,
        "skip_proposal": False,
        "replay_bundle": "",
        "horizon_sec": 300,
        "output_dir": str(db_path.parent / "reports"),
        "fail_on_unsafe": True,
    }
    values.update(overrides)
    return Namespace(**values)


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _empty_db(path):
    conn = _connect(path)
    conn.close()
    return path


def _create_decision_table(conn):
    conn.execute(
        """
        CREATE TABLE strategy_decision_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT,
            trade_date TEXT,
            decision_at TEXT,
            candidate_instance_id TEXT DEFAULT '',
            gate_status TEXT DEFAULT '',
            action_type TEXT DEFAULT '',
            action_result TEXT DEFAULT '',
            reason_codes_json TEXT DEFAULT '[]',
            order_intent_id TEXT DEFAULT '',
            virtual_order_id INTEGER,
            details_json TEXT DEFAULT '{}'
        )
        """
    )


def _create_outcome_table(conn):
    conn.execute(
        """
        CREATE TABLE strategy_decision_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outcome_id TEXT,
            decision_id TEXT,
            trade_date TEXT,
            decision_at TEXT,
            evaluated_at TEXT,
            horizon_sec INTEGER,
            price_at_decision REAL,
            max_return_pct REAL,
            max_drawdown_pct REAL,
            data_status TEXT DEFAULT '',
            outcome_label TEXT DEFAULT '',
            data_quality_issues_json TEXT DEFAULT '[]',
            details_json TEXT DEFAULT '{}'
        )
        """
    )


def _create_shadow_table(conn):
    conn.execute(
        """
        CREATE TABLE shadow_strategy_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluation_id TEXT,
            decision_id TEXT,
            policy_id TEXT,
            trade_date TEXT,
            changed_decision INTEGER DEFAULT 0,
            change_type TEXT DEFAULT '',
            baseline_reason_codes_json TEXT DEFAULT '[]',
            shadow_reason_codes_json TEXT DEFAULT '[]',
            data_quality_issues_json TEXT DEFAULT '[]',
            details_json TEXT DEFAULT '{}'
        )
        """
    )


def _create_replay_tables(conn):
    conn.execute(
        """
        CREATE TABLE strategy_replay_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            replay_id TEXT,
            trade_date TEXT,
            replay_db_path TEXT,
            status TEXT,
            warnings_json TEXT DEFAULT '[]'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE strategy_replay_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id TEXT,
            replay_id TEXT,
            trade_date TEXT,
            mode TEXT,
            summary_json TEXT DEFAULT '{}',
            funnel_json TEXT DEFAULT '{}',
            outcome_summary_json TEXT DEFAULT '{}',
            shadow_summary_json TEXT DEFAULT '{}',
            diff_summary_json TEXT DEFAULT '{}',
            recommendations_json TEXT DEFAULT '[]'
        )
        """
    )


def _create_proposal_tables(conn):
    conn.execute(
        """
        CREATE TABLE strategy_change_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id TEXT,
            trade_date TEXT,
            status TEXT,
            recommendation_grade TEXT,
            guardrail_passed INTEGER DEFAULT 0,
            blocked_by_guardrail_reason TEXT DEFAULT '',
            candidate_config_patch_json TEXT DEFAULT '{}',
            source_ids_json TEXT DEFAULT '[]',
            baseline_config_snapshot_json TEXT DEFAULT '{}',
            data_quality_issues_json TEXT DEFAULT '[]',
            rollout_plan_json TEXT DEFAULT '{}',
            rollback_plan_json TEXT DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE strategy_change_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_id TEXT,
            proposal_id TEXT,
            trade_date TEXT,
            evidence_payload_json TEXT DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE strategy_change_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_id TEXT,
            proposal_id TEXT,
            action TEXT
        )
        """
    )


def test_validation_reports_missing_components_without_crashing(tmp_path):
    db_path = _empty_db(tmp_path / "empty.sqlite3")

    report = run_validation(_args(db_path))

    assert report["overall_status"] == "WARN"
    assert report["component_status"]["decision_ledger"]["status"] == "MISSING_COMPONENT"
    assert "MISSING_COMPONENT" in json.dumps(report, ensure_ascii=False)


def test_duplicate_decision_id_is_detected(tmp_path):
    db_path = tmp_path / "dup_decision.sqlite3"
    conn = _connect(db_path)
    _create_decision_table(conn)
    conn.executemany(
        "INSERT INTO strategy_decision_events(decision_id, trade_date, decision_at, details_json) VALUES (?, ?, ?, '{}')",
        [("decision-1", "2026-06-01", "2026-06-01T09:00:00"), ("decision-1", "2026-06-01", "2026-06-01T09:00:01")],
    )
    conn.commit()
    conn.close()

    report = run_validation(_args(db_path))

    assert report["overall_status"] == "FAIL"
    assert report["decision_ledger"]["duplicate_decision_ids"][0]["decision_id"] == "decision-1"


def test_duplicate_outcome_and_lookahead_bias_are_detected(tmp_path):
    db_path = tmp_path / "outcome.sqlite3"
    conn = _connect(db_path)
    _create_outcome_table(conn)
    conn.executemany(
        """
        INSERT INTO strategy_decision_outcomes(
            outcome_id, decision_id, trade_date, decision_at, evaluated_at, horizon_sec,
            price_at_decision, max_return_pct, max_drawdown_pct
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("outcome-1", "decision-1", "2026-06-01", "2026-06-01T09:00:00", "2026-06-01T09:01:00", 300, 100, 1, -1),
            ("outcome-2", "decision-1", "2026-06-01", "2026-06-01T09:00:00", "2026-06-01T09:06:00", 300, 100, 1, -1),
        ],
    )
    conn.commit()
    conn.close()

    report = run_validation(_args(db_path))

    assert report["overall_status"] == "FAIL"
    assert report["outcome_labeler"]["duplicate_decision_horizon"]
    assert report["outcome_labeler"]["lookahead_bias_hits"]


def test_duplicate_shadow_policy_is_detected(tmp_path):
    db_path = tmp_path / "shadow.sqlite3"
    conn = _connect(db_path)
    _create_shadow_table(conn)
    conn.executemany(
        "INSERT INTO shadow_strategy_evaluations(evaluation_id, decision_id, policy_id, trade_date) VALUES (?, ?, ?, ?)",
        [("eval-1", "decision-1", "policy-a", "2026-06-01"), ("eval-2", "decision-1", "policy-a", "2026-06-01")],
    )
    conn.commit()
    conn.close()

    report = run_validation(_args(db_path))

    assert report["overall_status"] == "FAIL"
    assert report["shadow_strategy"]["duplicate_decision_policy"][0]["policy_id"] == "policy-a"


def test_sensitive_keys_are_detected_without_exposing_values(tmp_path):
    db_path = tmp_path / "sensitive.sqlite3"
    conn = _connect(db_path)
    _create_decision_table(conn)
    conn.execute(
        """
        INSERT INTO strategy_decision_events(decision_id, trade_date, decision_at, details_json)
        VALUES (?, ?, ?, ?)
        """,
        ("decision-secret", "2026-06-01", "2026-06-01T09:00:00", json.dumps({"nested": {"token": "secret-value"}})),
    )
    conn.commit()
    conn.close()

    report = run_validation(_args(db_path))

    assert report["overall_status"] == "FAIL"
    assert report["decision_ledger"]["sensitive_json_hits"][0]["path"] == "nested.token"
    assert "secret-value" not in json.dumps(report, ensure_ascii=False)


def test_replay_db_same_as_operating_db_fails(tmp_path):
    db_path = tmp_path / "replay.sqlite3"
    conn = _connect(db_path)
    _create_replay_tables(conn)
    conn.execute(
        "INSERT INTO strategy_replay_runs(replay_id, trade_date, replay_db_path, status) VALUES (?, ?, ?, ?)",
        ("replay-1", "2026-06-01", str(db_path), "OK"),
    )
    conn.commit()
    conn.close()

    report = run_validation(_args(db_path))

    assert report["overall_status"] == "FAIL"
    assert report["replay"]["replay_db_same_as_operating_db"]


def test_forbidden_proposal_patch_guardrail_miss_fails(tmp_path):
    db_path = tmp_path / "proposal.sqlite3"
    conn = _connect(db_path)
    _create_proposal_tables(conn)
    conn.execute(
        """
        INSERT INTO strategy_change_proposals(
            proposal_id, trade_date, status, recommendation_grade, guardrail_passed, candidate_config_patch_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "proposal-live",
            "2026-06-01",
            "REVIEW_READY",
            "STRONG_CANDIDATE",
            1,
            json.dumps({"live_order_enabled": True}),
        ),
    )
    conn.execute(
        "INSERT INTO strategy_change_evidence(evidence_id, proposal_id, trade_date, evidence_payload_json) VALUES (?, ?, ?, '{}')",
        ("evidence-1", "proposal-live", "2026-06-01"),
    )
    conn.commit()
    conn.close()

    report = run_validation(_args(db_path))

    assert report["overall_status"] == "FAIL"
    assert report["change_proposals"]["forbidden_patch_guardrail_misses"][0]["proposal_id"] == "proposal-live"


def test_approval_config_unchanged_helper_detects_config_mutation():
    before = {
        "status": "REVIEW_READY",
        "baseline_config_hash": "base",
        "candidate_config_hash": "candidate",
        "candidate_config_patch": {"entry_gate.late_chase.block_followers": True},
    }
    after_ok = {**before, "status": "APPROVED_FOR_OBSERVE"}
    after_bad = {**after_ok, "candidate_config_hash": "mutated"}

    assert validate_approval_config_unchanged(before, after_ok)["status"] == "OK"
    assert validate_approval_config_unchanged(before, after_bad)["status"] == "FAIL"


def test_validation_report_json_and_markdown_are_renderable(tmp_path):
    db_path = _empty_db(tmp_path / "empty.sqlite3")
    report = run_validation(_args(db_path))
    markdown = render_markdown_report(report)
    json_payload = json.dumps(report, ensure_ascii=False)

    assert "Strategy Feedback Loop Validation Report" in markdown
    assert "overall_status" in json_payload


def test_core_api_feedback_loop_smoke_endpoints_do_not_500(tmp_path, monkeypatch):
    db_path = tmp_path / "api.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "local-test-token")
    import trading_app.api as api_module

    api_module = importlib.reload(api_module)
    api_module.DEFAULT_REPLAY_DB_ROOT = tmp_path / "replay"
    api_module.DEFAULT_BUNDLE_ROOT = tmp_path / "bundles"

    endpoints = [
        "/api/status",
        "/api/runtime/decisions/summary?trade_date=2026-06-01",
        "/api/runtime/outcomes/intraday/summary?trade_date=2026-06-01",
        "/api/runtime/shadow-strategies/summary?trade_date=2026-06-01",
        "/api/runtime/change-proposals/summary?trade_date=2026-06-01",
    ]
    with TestClient(api_module.app) as client:
        responses = [client.get(endpoint) for endpoint in endpoints]

    assert all(response.status_code < 500 for response in responses)
    assert all(response.status_code == 200 for response in responses)
