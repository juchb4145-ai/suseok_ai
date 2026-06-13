import importlib
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStatusSnapshot
from trading_app.shadow_small_entry_pilot import (
    RECOMMEND_CONTINUE_OBSERVE_ONLY,
    RECOMMEND_INVESTIGATE_RECONCILE,
    STATUS_REVIEW_READY,
    ShadowSmallEntryPilotService,
)
from trading_app.strategy_change_proposals import StrategyChangeProposalGenerator


TODAY = date.today().isoformat()


class _FakeGateway:
    def snapshot(self) -> GatewayStatusSnapshot:
        return GatewayStatusSnapshot(
            connection_state="CONNECTED",
            connected=True,
            kiwoom_logged_in=True,
            orderable=True,
            mode="DRY_RUN",
            heartbeat_ok=True,
            last_heartbeat_payload={
                "kiwoom_logged_in": True,
                "orderable": True,
                "server_mode": "SIMULATION",
            },
        )

    def list_commands(self, **_: object) -> list[dict]:
        return []

    def command_events(self, *_: object, **__: object) -> list[dict]:
        return []


def test_shadow_small_entry_pilot_start_stores_run_and_preflight_event(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        service = ShadowSmallEntryPilotService(db, gateway_state=_FakeGateway(), now_provider=lambda: datetime(2026, 6, 13, 9, 0))
        result = service.start(trade_date=TODAY, operator="tester", operator_note="pilot open")
        run = db.latest_shadow_small_entry_pilot_run(trade_date=TODAY)
        events = db.list_shadow_small_entry_pilot_events(trade_date=TODAY, pilot_id=run["pilot_id"])

        assert result["run"]["pilot_id"] == f"shadow_small_entry_pilot:{TODAY}"
        assert run["operator"] == "tester"
        assert events[0]["event_type"] == "PREFLIGHT_RUN"
        assert "preflight" in events[0]["details"]
    finally:
        db.close()


def test_shadow_small_entry_pilot_report_links_orders_fills_positions_and_export(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        _seed_filled_shadow_order(db)
        service = ShadowSmallEntryPilotService(db, report_root=tmp_path / "reports")
        report = service.build_report(trade_date=TODAY, persist=True)
        exports = service.export_report(report, fmt="all")
        events = db.list_shadow_small_entry_pilot_events(trade_date=TODAY, pilot_id=report["pilot_id"], limit=100)

        assert report["summary"]["candidate_count"] == 1
        assert report["summary"]["submitted_order_count"] == 1
        assert report["summary"]["filled_order_count"] == 1
        assert report["summary"]["open_position_count"] == 1
        assert {"ORDER_SUBMITTED", "ORDER_ACCEPTED", "FILLED", "POSITION_OPENED"} <= {event["event_type"] for event in events}
        assert {"json", "csv", "md"} <= set(exports)
        assert Path(exports["json"]).exists()
    finally:
        db.close()


def test_shadow_small_entry_pilot_complete_marks_review_ready(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        _seed_filled_shadow_order(db)
        service = ShadowSmallEntryPilotService(db)
        service.start(trade_date=TODAY, operator="tester", operator_note="pilot")
        result = service.complete(trade_date=TODAY, operator="tester", operator_note="review")

        assert result["report"]["status"] == STATUS_REVIEW_READY
        assert db.latest_shadow_small_entry_pilot_run(trade_date=TODAY)["status"] == STATUS_REVIEW_READY
        assert result["report"]["recommendation"] in {RECOMMEND_CONTINUE_OBSERVE_ONLY, "CONTINUE_LIVE_SIM_GUARDED"}
    finally:
        db.close()


def test_shadow_small_entry_pilot_recommends_reconcile_for_unknown_submit(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_live_sim_order(
            {
                "order_intent_id": "shadow-unknown",
                "trade_date": TODAY,
                "code": "700002",
                "name": "unknown",
                "side": "buy",
                "order_status": "UNKNOWN_SUBMIT",
                "requested_qty": 1,
                "requested_price": 10000,
                "submitted_qty": 1,
                "submitted_price": 10000,
                "details": {"ready_type": "READY_SHADOW_SMALL_ENTRY"},
            }
        )
        report = ShadowSmallEntryPilotService(db).build_report(trade_date=TODAY, persist=True)

        assert report["recommendation"] == RECOMMEND_INVESTIGATE_RECONCILE
        assert "RECONCILE_REQUIRED" in report["recommendation_reason_codes"]
        assert report["summary"]["unknown_submit_count"] >= 1
    finally:
        db.close()


def test_shadow_small_entry_pilot_api_snapshot_items_and_cli(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")
    db = TradingDatabase(str(db_path))
    _seed_filled_shadow_order(db)
    ShadowSmallEntryPilotService(db).build_report(trade_date=TODAY, persist=True)
    db.close()

    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        snapshot = client.get("/api/snapshot?refresh=true").json()
        status = client.get("/api/shadow-small-entry-pilot/status", params={"trade_date": TODAY}).json()
        items = client.get("/api/shadow-small-entry-pilot/items", params={"trade_date": TODAY}).json()
        generated = client.post(
            "/api/shadow-small-entry-pilot/generate-report",
            json={"trade_date": TODAY, "export": False},
            headers={"X-Local-Token": "test-token"},
        ).json()

    assert "shadow_small_entry_pilot" in snapshot
    assert "shadow_small_entry_pilot" in snapshot["runtime"]
    assert status["summary"]["candidate_count"] >= 1
    assert items["items"][0]["code"] == "700001"
    assert generated["ok"] is True

    result = subprocess.run(
        [
            sys.executable,
            "tools/shadow_small_entry_pilot.py",
            "--db",
            str(db_path),
            "status",
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["summary"]["candidate_count"] >= 1


def test_shadow_small_entry_pilot_strategy_change_proposal_source(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        _seed_filled_shadow_order(db)
        ShadowSmallEntryPilotService(db).build_report(trade_date=TODAY, persist=True)
        result = StrategyChangeProposalGenerator(db).generate(
            trade_date=TODAY,
            source_type="shadow_small_entry_pilot",
            persist=False,
        )

        assert result["proposal_count"] >= 1
        proposal = result["proposals"][0]
        assert proposal["status"] == "REVIEW_READY"
        assert proposal["source_type"] == "shadow_small_entry_pilot"
        assert proposal["candidate_config_patch"].get("shadow_small_entry_promotion.order_enabled") is False
    finally:
        db.close()


def _seed_filled_shadow_order(db: TradingDatabase) -> None:
    db.save_live_sim_order(
        {
            "order_intent_id": "shadow-order-1",
            "command_id": "cmd-shadow-1",
            "trade_date": TODAY,
            "code": "700001",
            "name": "shadow-one",
            "side": "buy",
            "order_status": "FILLED",
            "broker_order_id": "900001",
            "requested_qty": 2,
            "requested_price": 10000,
            "submitted_qty": 2,
            "submitted_price": 10000,
            "accepted_at": f"{TODAY}T09:01:00",
            "last_fill_at": f"{TODAY}T09:01:10",
            "candidate_instance_id": "ci-shadow-1",
            "details": {
                "ready_type": "READY_SHADOW_SMALL_ENTRY",
                "theme_name": "theme-a",
                "shadow_small_entry_promotion_status": "PROMOTED",
            },
        }
    )
    db.save_live_sim_fill_event(
        {
            "order_intent_id": "shadow-order-1",
            "broker_order_id": "900001",
            "fill_id": "fill-1",
            "code": "700001",
            "side": "buy",
            "fill_qty": 2,
            "fill_price": 10000,
            "cumulative_fill_qty": 2,
            "remaining_qty": 0,
            "event_time": f"{TODAY}T09:01:10",
            "received_at": f"{TODAY}T09:01:10",
        }
    )
    db.save_live_sim_position(
        {
            "position_id": "LIVE_SIM:test:700001:ci-shadow-1",
            "candidate_instance_id": "ci-shadow-1",
            "code": "700001",
            "name": "shadow-one",
            "opened_at": f"{TODAY}T09:01:10",
            "entry_qty": 2,
            "entry_avg_price": 10000,
            "current_qty": 2,
            "realized_pnl": 0,
            "unrealized_pnl": 500,
            "unrealized_pnl_pct": 2.5,
            "max_favorable_excursion_pct": 3.0,
            "max_adverse_excursion_pct": -0.7,
            "stop_loss_price": 9800,
            "take_profit_price": 10500,
            "max_hold_exit_at": f"{TODAY}T10:01:10",
            "status": "OPEN",
            "details": {"shadow_small_entry_promotion": True},
        }
    )
