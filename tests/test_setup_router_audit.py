import json

from storage.db import TradingDatabase
from tools.audit_setup_router_v3 import main

from tests.test_setup_router_storage import _observation


TRADE_DATE = "2026-06-22"


def test_setup_router_audit_writes_reports(tmp_path):
    db_path = tmp_path / "setup-audit.db"
    out_dir = tmp_path / "reports"
    db = TradingDatabase(str(db_path))
    observation = _observation("VALID_OBSERVE", "MATCHED", "fp-audit")
    db.save_setup_router_states([observation])
    db.save_setup_observations([observation])
    db.conn.close()

    rc = main(["--db", str(db_path), "--trade-date", TRADE_DATE, "--output-dir", str(out_dir)])
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))

    assert rc == 0
    assert summary["verdict"] == "CONDITIONALLY_STABLE"
    assert summary["invalid_count"] == 0
    assert (out_dir / "report.md").exists()


def test_setup_router_audit_counts_only_gateway_order_commands_as_side_effects(tmp_path):
    db_path = tmp_path / "setup-audit-side-effects.db"
    out_dir = tmp_path / "reports"
    db = TradingDatabase(str(db_path))
    observation = _observation("VALID_OBSERVE", "MATCHED", "fp-audit")
    db.save_setup_router_states([observation])
    db.save_setup_observations([observation])
    db.conn.execute(
        """
        INSERT INTO gateway_commands(
            command_id, command_type, status, priority, source,
            payload_json, command_json, metadata_json, result_payload_json,
            created_at, updated_at, trade_date
        ) VALUES (?, ?, ?, ?, ?, '{}', '{}', '{}', '{}', ?, ?, ?)
        """,
        ("cmd-register", "register_realtime", "queued", "normal", "setup_router_v3", "2026-06-22T09:05:00", "2026-06-22T09:05:00", TRADE_DATE),
    )
    db.conn.commit()
    db.conn.close()

    rc = main(["--db", str(db_path), "--trade-date", TRADE_DATE, "--output-dir", str(out_dir)])
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))

    assert rc == 0
    assert summary["side_effect_counts"]["gateway_order_commands"] == 0

    db = TradingDatabase(str(db_path))
    db.conn.execute(
        """
        INSERT INTO gateway_commands(
            command_id, command_type, status, priority, source,
            payload_json, command_json, metadata_json, result_payload_json,
            created_at, updated_at, trade_date
        ) VALUES (?, ?, ?, ?, ?, '{}', '{}', '{}', '{}', ?, ?, ?)
        """,
        ("cmd-send", "send_order", "queued", "high", "setup_router_v3", "2026-06-22T09:06:00", "2026-06-22T09:06:00", TRADE_DATE),
    )
    db.conn.commit()
    db.conn.close()

    rc = main(["--db", str(db_path), "--trade-date", TRADE_DATE, "--output-dir", str(out_dir)])
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))

    assert rc == 1
    assert summary["side_effect_counts"]["gateway_order_commands"] == 1
    assert "SETUP_ROUTER_ORDER_SIDE_EFFECTS_PRESENT" in summary["failures"]


def test_setup_router_audit_fails_terminal_revival_same_generation(tmp_path):
    db_path = tmp_path / "setup-audit-terminal.db"
    out_dir = tmp_path / "reports"
    db = TradingDatabase(str(db_path))
    observation = _observation("VALID_OBSERVE", "MATCHED", "fp-audit")
    db.save_setup_router_states([observation])
    db.conn.execute(
        """
        INSERT INTO setup_router_state_transitions_v3(
            transition_id, trade_date, router_version, state_version,
            candidate_instance_id, theme_id, setup_type, setup_generation,
            setup_instance_id, code, candidate_id, previous_state, current_state,
            detector_phase_from, detector_phase_to, material_change_kind,
            material_state_fingerprint_from, material_state_fingerprint_to,
            occurred_at, context_id, reason_codes_json, state_payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', '{}')
        """,
        (
            "terminal-revival-1",
            TRADE_DATE,
            "setup_router_v3.5",
            "setup_router_v3.state.v3.2",
            "ci-1",
            "ai",
            "VWAP_RECLAIM",
            1,
            "setup-ci-1-vwap-1",
            "000001",
            1,
            "MATCHED",
            "FORMING",
            "MATCHED",
            "BELOW_CONFIRMED",
            "PHASE_CHANGED",
            "m1",
            "m2",
            "2026-06-22T09:06:00",
            "ctx-1",
        ),
    )
    db.conn.commit()
    db.conn.close()

    rc = main(["--db", str(db_path), "--trade-date", TRADE_DATE, "--output-dir", str(out_dir)])
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))

    assert rc == 1
    assert summary["state_integrity"]["metrics"]["terminal_revival_same_generation_count"] == 1
    assert "TERMINAL_REVIVAL_SAME_GENERATION" in summary["failures"]
