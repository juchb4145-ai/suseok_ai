import json

from storage.db import TradingDatabase
from tools.audit_setup_router_v3 import main

from tests.test_setup_router_storage import TRADE_DATE, _observation


def test_audit_uses_full_sql_for_market_action_unknown_beyond_limit(tmp_path):
    db_path = tmp_path / "audit-full-sql.db"
    out_dir = tmp_path / "reports"
    db = TradingDatabase(str(db_path))
    for index in range(3):
        db.save_setup_router_readiness_snapshots(
            [
                {
                    "trade_date": TRADE_DATE,
                    "router_version": "setup_router_v3.5.1",
                    "candidate_instance_id": f"ci-{index}",
                    "candidate_id": index,
                    "code": f"00000{index}",
                    "readiness_status": "READY",
                    "readiness_ready": True,
                    "readiness_fingerprint": f"rf-{index}",
                    "canonical_market_action": "UNKNOWN" if index == 2 else "ALLOW_NORMAL",
                    "calculated_at": f"2026-06-22T09:05:0{index}",
                }
            ]
        )
    db.conn.close()

    rc = main(["--db", str(db_path), "--trade-date", TRADE_DATE, "--output-dir", str(out_dir), "--limit", "1"])
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))

    assert rc == 1
    assert summary["readiness_metrics"]["market_action_unknown_output_count"] == 1
    assert "SETUP_ROUTER_MARKET_ACTION_UNKNOWN_OUTPUT" in summary["failures"]


def test_audit_does_not_fail_only_because_legacy_router_version_rows_exist(tmp_path):
    db_path = tmp_path / "audit-legacy-version.db"
    out_dir = tmp_path / "reports"
    db = TradingDatabase(str(db_path))
    db.save_setup_router_states([_observation("VALID_OBSERVE", "MATCHED", "current")])
    db.save_setup_observations([_observation("VALID_OBSERVE", "MATCHED", "current")])
    db.conn.execute(
        """
        INSERT INTO setup_router_pending_evaluations_v5(
            trade_date, router_version, candidate_instance_id, code, state_version,
            selected_theme_id, pending_epoch, pending_instance_id, status,
            pending_priority, pending_reasons_json, first_pending_at, last_pending_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            TRADE_DATE,
            "setup_router_v3.5",
            "legacy-ci",
            "000999",
            "setup_router_v3.state.v3.2",
            "ai",
            1,
            "legacy-pending",
            "COMPLETED",
            3,
            '["LEGACY_ROW"]',
            "2026-06-22T09:00:00",
            "2026-06-22T09:00:00",
        ),
    )
    db.conn.commit()
    db.conn.close()

    rc = main(["--db", str(db_path), "--trade-date", TRADE_DATE, "--output-dir", str(out_dir)])
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))

    assert rc == 0
    assert summary["state_integrity"]["metrics"]["foreign_version_row_count"] > 0
    assert "SETUP_ROUTER_VERSION_MISMATCH_ROWS_PRESENT" not in summary["failures"]
