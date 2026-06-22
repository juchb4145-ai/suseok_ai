import json

from storage.db import TradingDatabase
from tools.audit_setup_router_v3 import main

from tests.test_setup_router_storage import _observation


TRADE_DATE = "2026-06-22"


def test_setup_router_audit_writes_reports(tmp_path):
    db_path = tmp_path / "setup-audit.db"
    out_dir = tmp_path / "reports"
    db = TradingDatabase(str(db_path))
    db.save_setup_observations([_observation("VALID_OBSERVE", "MATCHED", "fp-audit")])
    db.conn.close()

    rc = main(["--db", str(db_path), "--trade-date", TRADE_DATE, "--output-dir", str(out_dir)])
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))

    assert rc == 0
    assert summary["verdict"] == "STABLE_FOR_OPPORTUNITY_RANKER"
    assert summary["invalid_count"] == 0
    assert (out_dir / "report.md").exists()
