import importlib
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStatusSnapshot
from trading.strategy.runtime_settings import StrategyRuntimeSettingsRepository
from trading.strategy.shadow_small_entry_promotion import (
    MODE_LIVE_SIM_GUARDED,
    STATUS_OBSERVE_ONLY,
    STATUS_PROMOTED,
    ShadowSmallEntryPromotionConfig,
    evaluate_shadow_small_entry_promotion,
)
from trading_app.dependencies import CoreSettings
from trading_app.shadow_small_entry_ops import (
    STATUS_LIVE_SIM_ACTIVE,
    STATUS_LIVE_SIM_ARMED,
    STATUS_OBSERVE_ONLY,
    STATUS_PAUSED_BY_OPERATOR,
    STATUS_PAUSED_BY_RISK,
    STATUS_ROLLED_BACK,
    ShadowSmallEntryOpsService,
)


TODAY = date.today().isoformat()


class _FakeGateway:
    def __init__(self, *, heartbeat_ok: bool = True, orderable: bool = True, available_cash_krw: int = 0) -> None:
        heartbeat = {
            "kiwoom_logged_in": True,
            "orderable": orderable,
            "server_mode": "SIMULATION",
        }
        if available_cash_krw:
            heartbeat["available_cash_krw"] = available_cash_krw
        self._snapshot = GatewayStatusSnapshot(
            connection_state="CONNECTED",
            connected=True,
            kiwoom_logged_in=True,
            orderable=orderable,
            mode="DRY_RUN",
            heartbeat_ok=heartbeat_ok,
            last_heartbeat_payload=heartbeat,
        )

    def snapshot(self) -> GatewayStatusSnapshot:
        return self._snapshot

    def list_commands(self, **_: object) -> list[dict]:
        return []

    def command_events(self, *_: object, **__: object) -> list[dict]:
        return []


def test_shadow_small_entry_ops_defaults_to_observe_only(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        service = ShadowSmallEntryOpsService(db, gateway_state=None, core_settings=_core_settings(tmp_path))
        status = service.status(trade_date=TODAY)
        settings = StrategyRuntimeSettingsRepository(db).load()

        assert status["status"] == STATUS_OBSERVE_ONLY
        assert status["order_enabled"] is False
        assert settings.value("shadow_small_entry_promotion.order_enabled") is False
        assert settings.value("shadow_small_entry_promotion.mode") == "observe_only"
    finally:
        db.close()


def test_shadow_small_entry_ops_preflight_fail_blocks_arm(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        service = ShadowSmallEntryOpsService(db, gateway_state=None, core_settings=_core_settings(tmp_path))
        preflight = service.preflight(trade_date=TODAY)
        armed = service.arm(operator="tester", note="try arm", trade_date=TODAY)

        assert preflight["status"] == "FAIL"
        assert "GATEWAY_DISCONNECTED" in preflight["blocking_reasons"]
        assert armed["ok"] is False
        assert armed["reason"] == "PREFLIGHT_FAILED"
    finally:
        db.close()


def test_shadow_small_entry_ops_arm_confirm_sets_live_sim_active(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        now = datetime.fromisoformat(f"{TODAY}T09:30:00")
        service = _passing_service(db, tmp_path, now=now)
        preflight = service.preflight(trade_date=TODAY)
        armed = service.arm(operator="tester", note="morning pilot", trade_date=TODAY)
        confirmed = service.confirm(
            activation_token=armed["activation_token"],
            operator="tester",
            note="confirm live_sim guarded",
            trade_date=TODAY,
        )
        settings = StrategyRuntimeSettingsRepository(db).load()
        traces = db.list_buy_zero_trace_events(trade_date=TODAY, limit=20)

        assert preflight["status"] == "PASS"
        assert armed["status"] == STATUS_LIVE_SIM_ARMED
        assert confirmed["status"] == STATUS_LIVE_SIM_ACTIVE
        assert settings.value("shadow_small_entry_promotion.mode") == "live_sim_guarded"
        assert settings.value("shadow_small_entry_promotion.order_enabled") is True
        assert settings.value("shadow_small_entry_ops.current_status") == STATUS_LIVE_SIM_ACTIVE
        assert {row["stage"] for row in traces} >= {
            "SHADOW_SMALL_ENTRY_OPS_PREFLIGHT",
            "SHADOW_SMALL_ENTRY_OPS_ARMED",
            "SHADOW_SMALL_ENTRY_OPS_ACTIVATED",
        }
    finally:
        db.close()


def test_shadow_small_entry_ops_uses_cash_based_daily_notional_limit(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        service = ShadowSmallEntryOpsService(
            db,
            gateway_state=_FakeGateway(available_cash_krw=46_000_000),
            core_settings=_core_settings(tmp_path),
            now_provider=lambda: datetime.fromisoformat(f"{TODAY}T09:30:00"),
            report_root=tmp_path / "reports",
        )
        service._promotion_evidence = lambda trade_date: {"available": True, "status": "READY", "source_report_trade_date": trade_date}  # type: ignore[method-assign]

        status = service.status(trade_date=TODAY)
        preflight = service.preflight(trade_date=TODAY, persist=False)

        assert status["limits"]["max_total_notional_krw"] == 23_000_000
        assert status["limits"]["cash_based_limits_enabled"] is True
        assert preflight["details"]["limits"]["max_total_notional_krw"] == 23_000_000
        assert preflight["status"] == "PASS"
    finally:
        db.close()


def test_shadow_small_entry_ops_confirm_requires_token_note_and_ttl(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        now = datetime.fromisoformat(f"{TODAY}T09:30:00")
        service = _passing_service(db, tmp_path, now=now)
        armed = service.arm(operator="tester", note="morning pilot", trade_date=TODAY)
        missing_note = service.confirm(activation_token=armed["activation_token"], operator="tester", note="", trade_date=TODAY)
        expired_service = _passing_service(db, tmp_path, now=now + timedelta(seconds=301))
        expired = expired_service.confirm(
            activation_token=armed["activation_token"],
            operator="tester",
            note="confirm after ttl",
            trade_date=TODAY,
        )

        assert missing_note["ok"] is False
        assert missing_note["reason"] == "OPERATOR_NOTE_REQUIRED"
        assert expired["ok"] is False
        assert expired["reason"] == "ACTIVATION_TOKEN_EXPIRED"
    finally:
        db.close()


def test_shadow_small_entry_ops_pause_and_rollback_disable_orders(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        service = _activated_service(db, tmp_path)
        paused = service.pause(operator="tester", note="manual pause", trade_date=TODAY)
        rolled = service.rollback(operator="tester", note="back to observe", trade_date=TODAY)
        settings = StrategyRuntimeSettingsRepository(db).load()
        audit = db.list_shadow_small_entry_ops_audit_log(trade_date=TODAY, limit=10)
        traces = db.list_buy_zero_trace_events(trade_date=TODAY, limit=20)

        assert paused["status"] == STATUS_PAUSED_BY_OPERATOR
        assert rolled["status"] == STATUS_ROLLED_BACK
        assert settings.value("shadow_small_entry_promotion.order_enabled") is False
        assert settings.value("shadow_small_entry_promotion.mode") == "observe_only"
        assert {row["next_status"] for row in audit} >= {STATUS_PAUSED_BY_OPERATOR, STATUS_ROLLED_BACK}
        assert "SHADOW_SMALL_ENTRY_OPS_ROLLED_BACK" in {row["stage"] for row in traces}
    finally:
        db.close()


def test_shadow_small_entry_promotion_guard_requires_active_ops():
    blocked = evaluate_shadow_small_entry_promotion(
        trace=_candidate_trace(),
        evidence=_evidence(),
        config=ShadowSmallEntryPromotionConfig(
            mode=MODE_LIVE_SIM_GUARDED,
            order_enabled=True,
            ops_status=STATUS_PAUSED_BY_RISK,
            min_sample_count=10,
            strong_sample_count=20,
            min_confidence=0.55,
        ),
    )
    active = evaluate_shadow_small_entry_promotion(
        trace=_candidate_trace(),
        evidence=_evidence(),
        config=ShadowSmallEntryPromotionConfig(
            mode=MODE_LIVE_SIM_GUARDED,
            order_enabled=True,
            ops_status=STATUS_LIVE_SIM_ACTIVE,
            min_sample_count=10,
            strong_sample_count=20,
            min_confidence=0.55,
        ),
    )

    assert blocked.promotion_status == STATUS_OBSERVE_ONLY
    assert "SHADOW_SMALL_ENTRY_OPS_PAUSED_BY_RISK" in blocked.reason_codes
    assert blocked.strategy_eligible is False
    assert active.promotion_status == STATUS_PROMOTED
    assert active.strategy_eligible is True


def test_shadow_small_entry_ops_risk_check_auto_pauses_on_daily_limit(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        service = _activated_service(db, tmp_path)
        db.save_live_sim_order(
            {
                "order_intent_id": "shadow-order-1",
                "trade_date": TODAY,
                "code": "700001",
                "name": "name",
                "side": "buy",
                "order_status": "SUBMITTED",
                "submitted_qty": 1,
                "submitted_price": 100000,
                "requested_qty": 1,
                "requested_price": 100000,
                "details": {"ready_type": "READY_SHADOW_SMALL_ENTRY"},
            }
        )
        db.save_live_sim_order(
            {
                "order_intent_id": "shadow-order-2",
                "trade_date": TODAY,
                "code": "700002",
                "name": "name",
                "side": "buy",
                "order_status": "SUBMITTED",
                "submitted_qty": 1,
                "submitted_price": 250000,
                "requested_qty": 1,
                "requested_price": 250000,
                "details": {"ready_type": "READY_SHADOW_SMALL_ENTRY"},
            }
        )

        risk = service.risk_check(trade_date=TODAY, auto_pause=True)
        settings = StrategyRuntimeSettingsRepository(db).load()

        assert any(item["metric"] == "total_notional_krw" for item in risk["breaches"])
        assert risk["pause_result"]["status"] == STATUS_PAUSED_BY_RISK
        assert settings.value("shadow_small_entry_promotion.order_enabled") is False
    finally:
        db.close()


def test_shadow_small_entry_ops_api_snapshot_and_cli(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")
    db = TradingDatabase(str(db_path))
    db.close()

    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        snapshot = client.get("/api/snapshot?refresh=true").json()
        status = client.get("/api/shadow-small-entry-ops/status", params={"trade_date": TODAY}).json()
        preflight = client.post(
            "/api/shadow-small-entry-ops/preflight",
            json={"trade_date": TODAY},
            headers={"X-Local-Token": "test-token"},
        ).json()

    assert "shadow_small_entry_ops" in snapshot
    assert "shadow_small_entry_ops" in snapshot["runtime"]
    assert status["status"] == STATUS_OBSERVE_ONLY
    assert preflight["status"] == "FAIL"

    result = subprocess.run(
        [
            sys.executable,
            "tools/shadow_small_entry_ops.py",
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
    assert payload["status"] == STATUS_OBSERVE_ONLY


def _passing_service(db: TradingDatabase, tmp_path: Path, *, now: datetime) -> ShadowSmallEntryOpsService:
    service = ShadowSmallEntryOpsService(
        db,
        gateway_state=_FakeGateway(),
        core_settings=_core_settings(tmp_path),
        now_provider=lambda: now,
        report_root=tmp_path / "reports",
    )
    service._promotion_evidence = lambda trade_date: {"available": True, "status": "READY", "source_report_trade_date": trade_date}  # type: ignore[method-assign]
    return service


def _activated_service(db: TradingDatabase, tmp_path: Path) -> ShadowSmallEntryOpsService:
    service = _passing_service(db, tmp_path, now=datetime.fromisoformat(f"{TODAY}T09:30:00"))
    armed = service.arm(operator="tester", note="morning pilot", trade_date=TODAY)
    service.confirm(
        activation_token=armed["activation_token"],
        operator="tester",
        note="confirm live_sim guarded",
        trade_date=TODAY,
    )
    return service


def _core_settings(tmp_path: Path) -> CoreSettings:
    return CoreSettings(db_path=tmp_path / "trader.sqlite3", local_token="test-token", mode="OBSERVE", allow_live=False)


def _candidate_trace() -> dict:
    return {
        "trade_date": TODAY,
        "code": "700001",
        "name": "name-700001",
        "candidate_instance_id": "ci-shadow",
        "status": "WAIT",
        "reason_codes": ["WAIT_DATA_SUPPORT_NOT_READY"],
        "primary_reason": "WAIT_DATA_SUPPORT_NOT_READY",
        "primary_group": "DATA_QUALITY_RISK",
        "stock_role": "LEADER",
        "theme_status": "LEADING_THEME",
        "price_location_status": "VWAP_RECLAIM",
        "price_location_readiness": "READY",
        "risk_level": "PASS",
        "current_price": 100,
        "trade_value": 1_000_000,
        "latest_tick_ready": True,
        "support_ready": True,
        "vwap_ready": True,
    }


def _evidence() -> dict:
    return {
        "available": True,
        "status": "READY",
        "report_id": "report-shadow",
        "source_report_trade_date": TODAY,
        "eligible_reason_codes": ["WAIT_DATA_SUPPORT_NOT_READY"],
        "eligible_reason_groups": ["DATA_QUALITY_RISK"],
        "reason_code_rows": [
            {
                "reason_code": "WAIT_DATA_SUPPORT_NOT_READY",
                "recommendation": "REVIEW_FOR_SMALL_ENTRY",
                "sample_count": 30,
                "labeled_count": 30,
                "event_count": 30,
                "confidence": 0.8,
                "missed_opportunity_rate": 0.5,
                "risk_avoided_rate": 0.1,
                "good_block_rate": 0.1,
                "avg_mfe_15m_pct": 3.0,
                "avg_mae_15m_pct": -1.0,
                "eligible": True,
            }
        ],
        "group_rows": [],
    }
