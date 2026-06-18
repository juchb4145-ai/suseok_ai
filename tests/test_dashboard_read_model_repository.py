import sqlite3

from storage.dashboard_read_model import DashboardReadModelRepository
from storage.db import TradingDatabase


def test_trading_database_migration_creates_dashboard_read_models(tmp_path):
    db = TradingDatabase(str(tmp_path / "schema.sqlite3"))
    try:
        row = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'dashboard_read_models'"
        ).fetchone()
    finally:
        db.close()

    assert row is not None


def test_snapshot_atomic_upsert_and_checksum_skip(tmp_path):
    repo = DashboardReadModelRepository(tmp_path / "read_model.sqlite3")
    try:
        first = repo.save_snapshot(
            {"schema_version": "dashboard_v2.reboot_ops.v1", "value": 1},
            snapshot_at="2026-06-18T09:00:00+00:00",
        )
        second = repo.save_snapshot(
            {"schema_version": "dashboard_v2.reboot_ops.v1", "value": 1},
            snapshot_at="2026-06-18T09:00:01+00:00",
        )
        third = repo.save_snapshot(
            {"schema_version": "dashboard_v2.reboot_ops.v1", "value": 2},
            snapshot_at="2026-06-18T09:00:02+00:00",
        )
        count = repo.conn.execute("SELECT COUNT(*) AS count FROM dashboard_read_models").fetchone()["count"]
    finally:
        repo.close()

    assert count == 1
    assert first.generation == 1
    assert second.unchanged is True
    assert second.generation == first.generation
    assert third.generation == 2
    assert third.snapshot["value"] == 2


def test_corrupt_json_is_reported_without_crashing(tmp_path):
    repo = DashboardReadModelRepository(tmp_path / "read_model.sqlite3")
    try:
        repo.save_snapshot({"value": 1}, snapshot_at="2026-06-18T09:00:00+00:00")
        repo.conn.execute("UPDATE dashboard_read_models SET snapshot_json = '{broken' WHERE view_name = 'main'")
        repo.conn.commit()

        record = repo.read_main_snapshot()
    finally:
        repo.close()

    assert record is not None
    assert record.status == "CORRUPT"
    assert "SNAPSHOT_JSON_CORRUPT" in record.last_error


def test_persisted_snapshot_restart_recovery(tmp_path):
    db_path = tmp_path / "read_model.sqlite3"
    repo = DashboardReadModelRepository(db_path)
    try:
        repo.save_snapshot({"value": 7}, snapshot_at="2026-06-18T09:00:00+00:00")
    finally:
        repo.close()

    recovered_repo = DashboardReadModelRepository(db_path)
    try:
        record = recovered_repo.recover_latest_snapshot()
    finally:
        recovered_repo.close()

    assert record is not None
    assert record.recovered is True
    assert record.snapshot["value"] == 7


def test_failed_write_leaves_previous_snapshot_readable(tmp_path):
    repo = DashboardReadModelRepository(tmp_path / "read_model.sqlite3")
    try:
        repo.save_snapshot({"value": "good"}, snapshot_at="2026-06-18T09:00:00+00:00")
        repo.conn.close()
        try:
            repo.save_snapshot({"value": "bad"}, snapshot_at="2026-06-18T09:00:01+00:00")
        except sqlite3.ProgrammingError:
            pass
    finally:
        try:
            repo.close()
        except Exception:
            pass

    reopened = DashboardReadModelRepository(tmp_path / "read_model.sqlite3")
    try:
        record = reopened.read_main_snapshot()
    finally:
        reopened.close()

    assert record is not None
    assert record.snapshot["value"] == "good"
