import json
import subprocess
import sys

from storage.db import TradingDatabase


TRADE_DATE = "2026-06-19"


def test_theme_rotation_full_session_audit_writes_summary(tmp_path):
    db_path = tmp_path / "audit.db"
    output_dir = tmp_path / "reports"
    db = TradingDatabase(str(db_path))
    db.save_theme_leadership_transition(
        {
            "trade_date": TRADE_DATE,
            "theme_id": "ai",
            "previous_status": "CHALLENGER",
            "current_status": "INCUMBENT",
            "detected_at": "2026-06-19T09:20:00",
        }
    )
    db.save_strategy_context_snapshot(
        {
            "trade_date": TRADE_DATE,
            "code": "000001",
            "candidate_id": 1,
            "context_id": "ctx-1",
            "calculated_at": "2026-06-19T09:20:00",
            "selected_theme_id": "ai",
            "theme": {"theme_id": "ai", "state_leadership_consistent": True},
        }
    )
    db.conn.close()

    result = subprocess.run(
        [
            sys.executable,
            "tools/audit_theme_rotation_full_session.py",
            "--db-path",
            str(db_path),
            "--trade-date",
            TRADE_DATE,
            "--output-dir",
            str(output_dir),
        ],
        cwd=".",
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    summary = json.loads((output_dir / "audit_summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "PASS"
    assert summary["verdict"] == "STABLE_FOR_SETUP_ROUTER"
    assert (output_dir / "audit_report.md").exists()
    assert (output_dir / "expansion_leases.json").exists()
