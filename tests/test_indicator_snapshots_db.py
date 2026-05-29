import sqlite3

from storage.db import TradingDatabase
from trading.strategy.models import IndicatorSnapshot


def test_indicator_snapshot_metadata_round_trip(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    snapshot = IndicatorSnapshot(
        candidate_id=1,
        code="005930",
        created_at="2026-05-29T09:00:00",
        price=80_000,
        metadata={
            "vwap_ready": True,
            "pullback_phase": "unknown",
            "insufficient_reason": ["ema20_5m_missing"],
        },
    )

    saved = db.save_indicator_snapshot(snapshot)
    loaded = db.list_indicator_snapshots(1)

    assert saved.id is not None
    assert loaded[0].metadata == snapshot.metadata
    db.close()


def test_indicator_snapshot_missing_metadata_uses_default(tmp_path):
    db_path = tmp_path / "trader.sqlite3"
    db = TradingDatabase(str(db_path))
    db.conn.execute(
        """
        INSERT INTO indicator_snapshots(candidate_id, code, created_at, price)
        VALUES (?, ?, ?, ?)
        """,
        (2, "000660", "2026-05-29T09:01:00", 250_000),
    )
    db.conn.commit()

    loaded = db.list_indicator_snapshots(2)

    assert loaded[0].metadata == {}
    db.close()


def test_indicator_snapshot_migration_adds_metadata_to_existing_table(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE indicator_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER,
            code TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            price INTEGER NOT NULL DEFAULT 0,
            vwap REAL,
            ema20_5m REAL,
            base_line_120 REAL,
            envelope_mid REAL,
            day_high INTEGER NOT NULL DEFAULT 0,
            day_low INTEGER NOT NULL DEFAULT 0,
            day_mid REAL,
            prev_high INTEGER NOT NULL DEFAULT 0,
            prev_low INTEGER NOT NULL DEFAULT 0,
            pullback_pct REAL,
            volume_reaccel INTEGER NOT NULL DEFAULT 0,
            failed_low_break_rebound INTEGER NOT NULL DEFAULT 0,
            chase_risk INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()

    db = TradingDatabase(str(db_path))
    columns = {row["name"] for row in db.conn.execute("PRAGMA table_info(indicator_snapshots)").fetchall()}

    assert "metadata_json" in columns
    db.close()
