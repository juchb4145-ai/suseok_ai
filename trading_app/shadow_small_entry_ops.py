from __future__ import annotations

import csv
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional

from storage.db import TradingDatabase
from trading.strategy.runtime_settings import StrategyRuntimeSettings, StrategyRuntimeSettingsRepository
from trading_app.dependencies import CoreSettings, get_settings
from trading_app.live_sim_audit import LiveSimLifecycleAuditor
from trading_app.shadow_small_entry_promotion import ShadowSmallEntryPromotionAnalyzer


STATUS_DISABLED = "DISABLED"
STATUS_OBSERVE_ONLY = "OBSERVE_ONLY"
STATUS_LIVE_SIM_ARMED = "LIVE_SIM_ARMED"
STATUS_LIVE_SIM_ACTIVE = "LIVE_SIM_ACTIVE"
STATUS_PAUSED_BY_OPERATOR = "PAUSED_BY_OPERATOR"
STATUS_PAUSED_BY_RISK = "PAUSED_BY_RISK"
STATUS_PAUSED_BY_AUDIT = "PAUSED_BY_AUDIT"
STATUS_PAUSED_BY_RECONCILE = "PAUSED_BY_RECONCILE"
STATUS_PAUSED_BY_ORDER_ERROR = "PAUSED_BY_ORDER_ERROR"
STATUS_ROLLED_BACK = "ROLLED_BACK"
STATUS_BROKEN = "BROKEN"

OPS_TRACE_STAGES = {
    "preflight": "SHADOW_SMALL_ENTRY_OPS_PREFLIGHT",
    "armed": "SHADOW_SMALL_ENTRY_OPS_ARMED",
    "activated": "SHADOW_SMALL_ENTRY_OPS_ACTIVATED",
    "paused": "SHADOW_SMALL_ENTRY_OPS_PAUSED",
    "rolled_back": "SHADOW_SMALL_ENTRY_OPS_ROLLED_BACK",
    "risk_check": "SHADOW_SMALL_ENTRY_OPS_RISK_CHECK",
    "blocked_entry": "SHADOW_SMALL_ENTRY_OPS_BLOCKED_ENTRY",
}
REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports" / "shadow_small_entry_ops"


@dataclass(frozen=True)
class ShadowSmallEntryOpsConfig:
    enabled: bool = True
    default_status: str = STATUS_OBSERVE_ONLY
    current_status: str = STATUS_OBSERVE_ONLY
    allow_live_sim_activation: bool = True
    require_two_step_activation: bool = True
    require_preflight_pass: bool = True
    require_operator_note: bool = True
    allow_rollback: bool = True
    rollback_target_mode: str = "observe_only"
    rollback_sets_order_enabled_false: bool = True
    activation_ttl_sec: int = 300
    daily_limits: dict[str, Any] = None  # type: ignore[assignment]
    risk_limits: dict[str, Any] = None  # type: ignore[assignment]
    emergency: dict[str, Any] = None  # type: ignore[assignment]

    @classmethod
    def from_settings(cls, settings: StrategyRuntimeSettings | Mapping[str, Any] | None) -> "ShadowSmallEntryOpsConfig":
        raw: Any = {}
        if settings is not None and hasattr(settings, "value"):
            raw = settings.value("shadow_small_entry_ops", {})
        elif isinstance(settings, Mapping):
            raw = settings.get("shadow_small_entry_ops", settings)
        data = dict(raw or {})
        default = cls.default_payload()
        daily = {**default["daily_limits"], **dict(data.get("daily_limits") or {})}
        risk = {**default["risk_limits"], **dict(data.get("risk_limits") or {})}
        emergency = {**default["emergency"], **dict(data.get("emergency") or {})}
        return cls(
            enabled=_bool(data.get("enabled"), True),
            default_status=str(data.get("default_status") or STATUS_OBSERVE_ONLY).upper(),
            current_status=str(data.get("current_status") or data.get("status") or data.get("default_status") or STATUS_OBSERVE_ONLY).upper(),
            allow_live_sim_activation=_bool(data.get("allow_live_sim_activation"), True),
            require_two_step_activation=_bool(data.get("require_two_step_activation"), True),
            require_preflight_pass=_bool(data.get("require_preflight_pass"), True),
            require_operator_note=_bool(data.get("require_operator_note"), True),
            allow_rollback=_bool(data.get("allow_rollback"), True),
            rollback_target_mode=str(data.get("rollback_target_mode") or "observe_only"),
            rollback_sets_order_enabled_false=_bool(data.get("rollback_sets_order_enabled_false"), True),
            activation_ttl_sec=max(30, int(data.get("activation_ttl_sec") or 300)),
            daily_limits=daily,
            risk_limits=risk,
            emergency=emergency,
        )

    @staticmethod
    def default_payload() -> dict[str, Any]:
        return {
            "daily_limits": {
                "max_promotions_per_day": 3,
                "max_submitted_orders_per_day": 3,
                "max_filled_orders_per_day": 3,
                "max_total_notional_krw": 300000,
                "max_open_positions": 1,
                "max_same_theme_promotions_per_day": 1,
                "max_same_code_promotions_per_day": 1,
            },
            "risk_limits": {
                "max_realized_loss_pct": -1.5,
                "max_unrealized_loss_pct": -1.5,
                "max_daily_realized_loss_krw": 15000,
                "max_daily_unrealized_loss_krw": 15000,
                "max_consecutive_losing_trades": 2,
                "max_order_reject_count": 2,
                "max_duplicate_block_count": 3,
                "max_unknown_submit_count": 1,
                "max_reconcile_required_count": 0,
                "max_cancel_requested_stale_count": 0,
                "pause_on_live_sim_audit_broken": True,
                "pause_on_live_sim_audit_reconcile_required": True,
                "pause_on_exit_guard_not_ready": True,
                "pause_on_gateway_disconnected": True,
                "pause_on_heartbeat_bad": True,
            },
            "emergency": {
                "enable_emergency_pause": True,
                "emergency_pause_cancels_unfilled_shadow_orders": False,
                "emergency_pause_blocks_new_shadow_entries": True,
                "emergency_pause_preserves_existing_exit_logic": True,
            },
        }


class ShadowSmallEntryOpsService:
    def __init__(
        self,
        db: TradingDatabase,
        *,
        gateway_state: Any | None = None,
        core_settings: CoreSettings | None = None,
        now_provider: Any | None = None,
        report_root: Path | None = None,
    ) -> None:
        self.db = db
        self.gateway_state = gateway_state
        self.core_settings = core_settings or get_settings()
        self.now_provider = now_provider or (lambda: datetime.now().replace(microsecond=0))
        self.report_root = Path(report_root) if report_root is not None else REPORT_ROOT

    def status(self, *, trade_date: str | None = None) -> dict[str, Any]:
        settings = self._settings()
        cfg = ShadowSmallEntryOpsConfig.from_settings(settings)
        today = trade_date or self._today()
        state = self._state(settings=settings, cfg=cfg)
        preflight = self.preflight(trade_date=today, persist=False)
        risk = self._risk_snapshot(trade_date=today)
        audit_rows = self.db.list_shadow_small_entry_ops_audit_log(trade_date=today, limit=20)
        payload = {
            "available": True,
            "status": state["status"],
            "mode": state["mode"],
            "order_enabled": bool(state["order_enabled"]),
            "preflight_status": preflight["status"],
            "preflight_blocking_reasons": list(preflight["blocking_reasons"]),
            "activation_armed": state["status"] == STATUS_LIVE_SIM_ARMED and bool(state.get("activation_token_id")),
            "activation_token_id": state.get("activation_token_id") or "",
            "activation_expires_at": state.get("activation_expires_at") or "",
            "last_status_change_at": state.get("last_status_change_at") or "",
            "last_status_change_reason": state.get("last_status_change_reason") or "",
            "today": risk["today"],
            "limits": cfg.daily_limits | {
                "max_daily_realized_loss_krw": cfg.risk_limits.get("max_daily_realized_loss_krw"),
                "max_daily_unrealized_loss_krw": cfg.risk_limits.get("max_daily_unrealized_loss_krw"),
            },
            "audit": {
                "status": risk["status"],
                "live_sim_audit_status": risk["live_sim_audit_status"],
                "reconcile_status": risk["reconcile_status"],
                "exit_guard_ready": risk["exit_guard_ready"],
                "gateway_heartbeat_ok": risk["gateway_heartbeat_ok"],
            },
            "warnings": list(dict.fromkeys([*state.get("warnings", []), *preflight.get("warnings", [])])),
            "operator_message_ko": _operator_message(state["status"], preflight),
            "last_audit_log": audit_rows[:5],
            "config": {
                "enabled": cfg.enabled,
                "allow_live_sim_activation": cfg.allow_live_sim_activation,
                "require_two_step_activation": cfg.require_two_step_activation,
                "require_preflight_pass": cfg.require_preflight_pass,
                "require_operator_note": cfg.require_operator_note,
                "activation_ttl_sec": cfg.activation_ttl_sec,
            },
            "last_updated_at": self._now(),
        }
        return payload

    def preflight(self, *, trade_date: str | None = None, persist: bool = True) -> dict[str, Any]:
        settings = self._settings()
        cfg = ShadowSmallEntryOpsConfig.from_settings(settings)
        today = trade_date or self._today()
        core = self.core_settings
        gateway = self._gateway_snapshot()
        heartbeat = dict(gateway.get("last_heartbeat_payload") or {})
        live_audit = self._live_audit(today)
        live_summary = dict(live_audit.get("summary") or {})
        usage = self._usage(today)
        blocking: list[str] = []
        warnings: list[str] = []

        if not cfg.enabled:
            blocking.append("SHADOW_SMALL_ENTRY_OPS_DISABLED")
        if not cfg.allow_live_sim_activation:
            blocking.append("SHADOW_SMALL_ENTRY_LIVE_SIM_ACTIVATION_DISABLED")
        if core.live_order_enabled or core.mode == "LIVE" or core.allow_live or core.runtime_allow_live_orders:
            blocking.append("LIVE_REAL_CONFIG_ENABLED")
        server_mode = str(heartbeat.get("server_mode") or heartbeat.get("broker_env") or gateway.get("broker_env") or "SIMULATION").upper()
        if server_mode not in {"SIMULATION", "SIM", "MOCK", ""}:
            blocking.append("BROKER_ENV_NOT_SIMULATION")
        if not gateway.get("connected"):
            blocking.append("GATEWAY_DISCONNECTED")
        if not gateway.get("heartbeat_ok"):
            blocking.append("GATEWAY_HEARTBEAT_BAD")
        if not gateway.get("kiwoom_logged_in") and not heartbeat.get("kiwoom_logged_in"):
            blocking.append("KIWOOM_NOT_LOGGED_IN")
        if not gateway.get("orderable") and not heartbeat.get("orderable"):
            blocking.append("KIWOOM_NOT_ORDERABLE")
        if bool(gateway.get("kill_switch_active") or heartbeat.get("kill_switch_active")):
            blocking.append("KILL_SWITCH_ACTIVE")
        if _exit_guard_disabled(settings):
            blocking.append("EXIT_GUARD_NOT_READY")
        audit_status = str(live_audit.get("status") or "").upper()
        if audit_status in {"BROKEN", "RECONCILE_REQUIRED"}:
            blocking.append("LIVE_SIM_AUDIT_BROKEN")
        elif audit_status == "WARN":
            warnings.append("LIVE_SIM_AUDIT_WARN")
        if int(live_summary.get("reconcile_required_order_count") or 0) > int(cfg.risk_limits.get("max_reconcile_required_count") or 0):
            blocking.append("RECONCILE_REQUIRED")
        if int(live_summary.get("broker_order_id_missing_count") or 0) > 0:
            blocking.append("BROKER_ORDER_ID_MISSING")
        if int(live_summary.get("cancel_requested_stale_count") or 0) > int(cfg.risk_limits.get("max_cancel_requested_stale_count") or 0):
            blocking.append("CANCEL_REQUESTED_STALE")
        evidence = self._promotion_evidence(today)
        if not bool(evidence.get("available")):
            blocking.append("SHADOW_PROMOTION_EVIDENCE_NOT_READY")
        if usage["promotion_count"] >= int(cfg.daily_limits.get("max_promotions_per_day") or 0):
            blocking.append("SHADOW_SMALL_ENTRY_DAILY_PROMOTION_LIMIT")
        if usage["submitted_count"] >= int(cfg.daily_limits.get("max_submitted_orders_per_day") or 0):
            blocking.append("SHADOW_SMALL_ENTRY_DAILY_SUBMIT_LIMIT")
        if usage["filled_count"] >= int(cfg.daily_limits.get("max_filled_orders_per_day") or 0):
            blocking.append("SHADOW_SMALL_ENTRY_DAILY_FILL_LIMIT")
        if usage["total_notional_krw"] >= int(cfg.daily_limits.get("max_total_notional_krw") or 0):
            blocking.append("SHADOW_SMALL_ENTRY_NOTIONAL_LIMIT")
        if usage["open_position_count"] > int(cfg.daily_limits.get("max_open_positions") or 1):
            blocking.append("SHADOW_SMALL_ENTRY_OPEN_POSITION_LIMIT")

        status = "PASS" if not blocking else "FAIL"
        result = {
            "ok": status == "PASS",
            "status": status,
            "blocking_reasons": sorted(set(blocking)),
            "warnings": sorted(set(warnings)),
            "operator_message_ko": "LIVE_SIM 사전 점검을 통과했습니다." if status == "PASS" else "LIVE_SIM 사전 점검 실패: 신규 shadow 소액 진입을 켤 수 없습니다.",
            "checked_at": self._now(),
            "details": {
                "gateway": gateway,
                "server_mode": server_mode,
                "live_sim_audit": live_audit,
                "usage": usage,
                "evidence_status": evidence.get("status"),
                "source_report_trade_date": evidence.get("source_report_trade_date"),
            },
        }
        if persist:
            self._record_trace("preflight", next_status=self._state(settings=settings, cfg=cfg)["status"], preflight=result)
            self._save_state_patch(settings=settings, cfg=cfg, preflight=result)
        return result

    def arm(self, *, operator: str, note: str, trade_date: str | None = None) -> dict[str, Any]:
        settings = self._settings()
        cfg = ShadowSmallEntryOpsConfig.from_settings(settings)
        if cfg.require_operator_note and not str(note or "").strip():
            return {"ok": False, "status": "REJECTED", "reason": "OPERATOR_NOTE_REQUIRED"}
        preflight = self.preflight(trade_date=trade_date, persist=True)
        if cfg.require_preflight_pass and not preflight["ok"]:
            return {"ok": False, "status": "REJECTED", "reason": "PREFLIGHT_FAILED", "preflight": preflight}
        raw_token = secrets.token_urlsafe(18)
        token_hash = _hash_text(raw_token)
        token_id = f"sse_ops_token_{secrets.token_hex(8)}"
        expires_at = (self.now_provider() + timedelta(seconds=int(cfg.activation_ttl_sec))).isoformat(timespec="seconds")
        token = self.db.save_shadow_small_entry_ops_token(
            {
                "token_id": token_id,
                "token_hash": token_hash,
                "status": "ARMED",
                "created_at": self._now(),
                "expires_at": expires_at,
                "created_by": operator,
                "operator_note": note,
                "preflight": preflight,
            }
        )
        result = self._transition(
            settings=settings,
            previous_status=self._state(settings=settings, cfg=cfg)["status"],
            next_status=STATUS_LIVE_SIM_ARMED,
            changed_by=operator,
            reason="SHADOW_SMALL_ENTRY_OPERATOR_ARM",
            reason_codes=["SHADOW_SMALL_ENTRY_OPS_ARMED", "SHADOW_SMALL_ENTRY_ORDER_DISABLED"],
            operator_note=note,
            mode_after="observe_only",
            order_enabled_after=False,
            activation_token_id=token_id,
            activation_expires_at=expires_at,
            preflight=preflight,
            event_type="arm",
        )
        return {**result, "ok": True, "activation_token": raw_token, "activation_token_id": token_id, "activation_expires_at": expires_at, "token": token}

    def confirm(self, *, activation_token: str, operator: str, note: str, trade_date: str | None = None) -> dict[str, Any]:
        settings = self._settings()
        cfg = ShadowSmallEntryOpsConfig.from_settings(settings)
        if cfg.require_operator_note and not str(note or "").strip():
            return {"ok": False, "status": "REJECTED", "reason": "OPERATOR_NOTE_REQUIRED"}
        token = self.db.get_shadow_small_entry_ops_token_by_hash(_hash_text(activation_token))
        if not token or token.get("status") != "ARMED":
            return {"ok": False, "status": "REJECTED", "reason": "ACTIVATION_TOKEN_INVALID"}
        if _parse_dt(str(token.get("expires_at") or "")) < self.now_provider():
            self.db.update_shadow_small_entry_ops_token(str(token.get("token_id")), {"status": "EXPIRED"})
            return {"ok": False, "status": "REJECTED", "reason": "ACTIVATION_TOKEN_EXPIRED"}
        preflight = self.preflight(trade_date=trade_date, persist=True)
        if cfg.require_preflight_pass and not preflight["ok"]:
            return {"ok": False, "status": "REJECTED", "reason": "PREFLIGHT_FAILED", "preflight": preflight}
        self.db.update_shadow_small_entry_ops_token(str(token.get("token_id")), {"status": "CONSUMED", "consumed_at": self._now()})
        return self._transition(
            settings=settings,
            previous_status=self._state(settings=settings, cfg=cfg)["status"],
            next_status=STATUS_LIVE_SIM_ACTIVE,
            changed_by=operator,
            reason="SHADOW_SMALL_ENTRY_OPERATOR_CONFIRM",
            reason_codes=["SHADOW_SMALL_ENTRY_OPS_ACTIVATED", "LIVE_SIM_GUARDED_SMALL_ENTRY"],
            operator_note=note,
            mode_after="live_sim_guarded",
            order_enabled_after=True,
            activation_token_id=str(token.get("token_id") or ""),
            activation_expires_at="",
            preflight=preflight,
            event_type="confirm",
        )

    def pause(self, *, operator: str, note: str, reason: str = "SHADOW_SMALL_ENTRY_OPERATOR_PAUSE", status: str = STATUS_PAUSED_BY_OPERATOR, trade_date: str | None = None) -> dict[str, Any]:
        settings = self._settings()
        cfg = ShadowSmallEntryOpsConfig.from_settings(settings)
        return self._transition(
            settings=settings,
            previous_status=self._state(settings=settings, cfg=cfg)["status"],
            next_status=status,
            changed_by=operator,
            reason=reason,
            reason_codes=["SHADOW_SMALL_ENTRY_OPS_PAUSED", "SHADOW_SMALL_ENTRY_NEW_ENTRY_BLOCKED", "SHADOW_SMALL_ENTRY_ORDER_DISABLED"],
            operator_note=note,
            mode_after="observe_only",
            order_enabled_after=False,
            event_type="pause",
        )

    def rollback(self, *, operator: str, note: str, trade_date: str | None = None) -> dict[str, Any]:
        settings = self._settings()
        cfg = ShadowSmallEntryOpsConfig.from_settings(settings)
        if not cfg.allow_rollback:
            return {"ok": False, "status": "REJECTED", "reason": "ROLLBACK_DISABLED"}
        return self._transition(
            settings=settings,
            previous_status=self._state(settings=settings, cfg=cfg)["status"],
            next_status=STATUS_ROLLED_BACK,
            changed_by=operator,
            reason="SHADOW_SMALL_ENTRY_OPERATOR_ROLLBACK",
            reason_codes=[
                "SHADOW_SMALL_ENTRY_OPERATOR_ROLLBACK",
                "SHADOW_SMALL_ENTRY_ORDER_DISABLED",
                "SHADOW_SMALL_ENTRY_NEW_ENTRY_BLOCKED",
                "SHADOW_SMALL_ENTRY_EXISTING_EXIT_PRESERVED",
            ],
            operator_note=note,
            mode_after=cfg.rollback_target_mode,
            order_enabled_after=False,
            event_type="rollback",
        )

    def risk_check(self, *, trade_date: str | None = None, auto_pause: bool = True) -> dict[str, Any]:
        today = trade_date or self._today()
        settings = self._settings()
        cfg = ShadowSmallEntryOpsConfig.from_settings(settings)
        risk = self._risk_snapshot(trade_date=today)
        breached = risk["breaches"]
        if breached and auto_pause:
            severity = breached[0]["pause_status"]
            paused = self.pause(
                operator="system",
                note=f"auto pause: {breached[0]['metric']}",
                reason=breached[0]["reason"],
                status=severity,
                trade_date=today,
            )
            risk["pause_result"] = paused
        self._record_trace("risk_check", next_status=self._state(settings=self._settings(), cfg=ShadowSmallEntryOpsConfig.from_settings(self._settings()))["status"], risk=risk)
        return risk

    def audit_log(self, *, trade_date: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.db.list_shadow_small_entry_ops_audit_log(trade_date=trade_date, limit=limit)

    def export_report(self, report: dict[str, Any], *, fmt: str = "all") -> dict[str, str]:
        trade_date = str(report.get("trade_date") or self._today())
        target = self.report_root / trade_date
        target.mkdir(parents=True, exist_ok=True)
        stem = f"shadow_small_entry_ops_{trade_date}"
        exports: dict[str, str] = {}
        if fmt in {"json", "all"}:
            path = target / f"{stem}.json"
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
            exports["json"] = str(path)
        if fmt in {"csv", "all"}:
            path = target / f"{stem}.csv"
            rows = report.get("audit_log") or []
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["created_at", "event_type", "previous_status", "next_status", "changed_by", "reason", "operator_note"])
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: row.get(key, "") for key in writer.fieldnames})
            exports["csv"] = str(path)
        if fmt in {"md", "markdown", "all"}:
            path = target / f"{stem}.md"
            summary = report.get("status") or {}
            lines = [
                f"# Shadow Small Entry Ops ({trade_date})",
                "",
                f"- status: {summary.get('status')}",
                f"- mode: {summary.get('mode')}",
                f"- order_enabled: {summary.get('order_enabled')}",
                f"- preflight_status: {summary.get('preflight_status')}",
                f"- blocking_reasons: {', '.join(summary.get('preflight_blocking_reasons') or [])}",
                "",
                "## Audit Log",
            ]
            for row in (report.get("audit_log") or [])[:20]:
                lines.append(f"- {row.get('created_at')} {row.get('event_type')} {row.get('previous_status')} -> {row.get('next_status')} {row.get('reason')}")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            exports["md"] = str(path)
        return exports

    def build_report(self, *, trade_date: str | None = None, limit: int = 100) -> dict[str, Any]:
        today = trade_date or self._today()
        return {
            "trade_date": today,
            "generated_at": self._now(),
            "status": self.status(trade_date=today),
            "preflight": self.preflight(trade_date=today, persist=False),
            "risk_check": self._risk_snapshot(trade_date=today),
            "audit_log": self.audit_log(trade_date=today, limit=limit),
        }

    def _transition(
        self,
        *,
        settings: StrategyRuntimeSettings,
        previous_status: str,
        next_status: str,
        changed_by: str,
        reason: str,
        reason_codes: list[str],
        operator_note: str,
        mode_after: str,
        order_enabled_after: bool,
        event_type: str,
        activation_token_id: str = "",
        activation_expires_at: str = "",
        preflight: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        before_hash = _settings_hash(settings.settings_json)
        mode_before = str(settings.value("shadow_small_entry_promotion.mode", "observe_only"))
        order_before = bool(settings.value("shadow_small_entry_promotion.order_enabled", False))
        updated_settings = self._save_settings(
            settings,
            ops_status=next_status,
            promotion_mode=mode_after,
            promotion_order_enabled=order_enabled_after,
        )
        after_hash = _settings_hash(updated_settings.settings_json)
        now = self._now()
        state = self.db.save_shadow_small_entry_ops_state(
            {
                "status": next_status,
                "mode": mode_after,
                "order_enabled": order_enabled_after,
                "activation_token_id": activation_token_id,
                "activation_expires_at": activation_expires_at,
                "last_status_change_at": now,
                "last_status_change_reason": reason,
                "last_changed_by": changed_by,
                "last_operator_note": operator_note,
                "runtime_settings_hash": after_hash,
                "preflight_status": (preflight or {}).get("status") or "",
                "preflight_blocking_reasons": (preflight or {}).get("blocking_reasons") or [],
                "details": {"reason_codes": reason_codes, "preflight": preflight or {}},
            }
        )
        audit = self.db.append_shadow_small_entry_ops_audit_log(
            {
                "trade_date": self._today(),
                "event_type": event_type,
                "previous_status": previous_status,
                "next_status": next_status,
                "changed_by": changed_by,
                "reason": reason,
                "reason_codes": reason_codes,
                "operator_note": operator_note,
                "runtime_settings_before_hash": before_hash,
                "runtime_settings_after_hash": after_hash,
                "details": {
                    "mode_before": mode_before,
                    "mode_after": mode_after,
                    "order_enabled_before": order_before,
                    "order_enabled_after": order_enabled_after,
                    "activation_token_id": activation_token_id,
                },
            }
        )
        self._record_trace(
            "activated" if next_status == STATUS_LIVE_SIM_ACTIVE else ("armed" if next_status == STATUS_LIVE_SIM_ARMED else ("rolled_back" if next_status == STATUS_ROLLED_BACK else "paused")),
            previous_status=previous_status,
            next_status=next_status,
            changed_by=changed_by,
            reason=reason,
            reason_codes=reason_codes,
            operator_note=operator_note,
            mode_before=mode_before,
            mode_after=mode_after,
            order_enabled_before=order_before,
            order_enabled_after=order_enabled_after,
            activation_token_id=activation_token_id,
            preflight=preflight or {},
        )
        return {"ok": True, "status": next_status, "state": state, "audit": audit}

    def _save_state_patch(self, *, settings: StrategyRuntimeSettings, cfg: ShadowSmallEntryOpsConfig, preflight: dict[str, Any]) -> None:
        state = self._state(settings=settings, cfg=cfg)
        self.db.save_shadow_small_entry_ops_state(
            {
                **state,
                "preflight_status": preflight.get("status") or "",
                "preflight_blocking_reasons": preflight.get("blocking_reasons") or [],
                "warnings": preflight.get("warnings") or [],
                "details": {"preflight": preflight},
            }
        )

    def _save_settings(self, settings: StrategyRuntimeSettings, *, ops_status: str, promotion_mode: str, promotion_order_enabled: bool) -> StrategyRuntimeSettings:
        payload = json.loads(json.dumps(settings.settings_json, ensure_ascii=False))
        payload.setdefault("shadow_small_entry_ops", {})
        payload.setdefault("shadow_small_entry_promotion", {})
        payload["shadow_small_entry_ops"]["current_status"] = ops_status
        payload["shadow_small_entry_promotion"]["mode"] = promotion_mode
        payload["shadow_small_entry_promotion"]["order_enabled"] = bool(promotion_order_enabled)
        updated = StrategyRuntimeSettings.from_settings_json(
            payload,
            strategy_name=settings.strategy_name,
            profile_name=settings.profile_name,
            profile_version=settings.profile_version,
            mode=settings.mode,
            loaded_from="shadow_small_entry_ops",
        )
        return StrategyRuntimeSettingsRepository(self.db).save(updated)

    def _state(self, *, settings: StrategyRuntimeSettings, cfg: ShadowSmallEntryOpsConfig) -> dict[str, Any]:
        saved = self.db.load_shadow_small_entry_ops_state()
        mode = str(settings.value("shadow_small_entry_promotion.mode", "observe_only"))
        order_enabled = bool(settings.value("shadow_small_entry_promotion.order_enabled", False))
        status = str((saved or {}).get("status") or cfg.current_status or cfg.default_status or STATUS_OBSERVE_ONLY).upper()
        if order_enabled and mode == "live_sim_guarded" and status not in {STATUS_LIVE_SIM_ACTIVE, STATUS_LIVE_SIM_ARMED}:
            status = STATUS_BROKEN
        return {
            "state_key": "default",
            "status": status,
            "mode": mode,
            "order_enabled": order_enabled,
            "activation_token_id": str((saved or {}).get("activation_token_id") or ""),
            "activation_expires_at": str((saved or {}).get("activation_expires_at") or ""),
            "last_status_change_at": str((saved or {}).get("last_status_change_at") or ""),
            "last_status_change_reason": str((saved or {}).get("last_status_change_reason") or ""),
            "warnings": list((saved or {}).get("warnings") or []),
            "details": dict((saved or {}).get("details") or {}),
        }

    def _settings(self) -> StrategyRuntimeSettings:
        return StrategyRuntimeSettingsRepository(self.db).load()

    def _gateway_snapshot(self) -> dict[str, Any]:
        if self.gateway_state is None:
            return {"connected": False, "heartbeat_ok": False, "kiwoom_logged_in": False, "orderable": False}
        try:
            return self.gateway_state.snapshot().to_dict()
        except Exception:
            return {"connected": False, "heartbeat_ok": False, "kiwoom_logged_in": False, "orderable": False}

    def _live_audit(self, trade_date: str) -> dict[str, Any]:
        try:
            return LiveSimLifecycleAuditor(self.db, self.gateway_state).build_report(trade_date=trade_date, limit=1000)
        except Exception as exc:
            return {"available": False, "status": "WARN", "summary": {}, "error": str(exc)}

    def _promotion_evidence(self, trade_date: str) -> dict[str, Any]:
        try:
            return ShadowSmallEntryPromotionAnalyzer(self.db).load_evidence(trade_date=trade_date, limit=50000)
        except Exception as exc:
            return {"available": False, "status": "ERROR", "warnings": [str(exc)]}

    def _usage(self, trade_date: str) -> dict[str, int]:
        orders = [row for row in self.db.list_live_sim_orders(trade_date=trade_date, limit=1000) if _is_shadow_order(row)]
        submitted = [row for row in orders if str(row.get("order_status") or "") not in {"BLOCKED", "DUPLICATE", "REJECTED", "FAILED"}]
        filled = [row for row in orders if str(row.get("order_status") or "") == "FILLED"]
        open_positions = [row for row in self.db.list_live_sim_positions(limit=1000) if _is_shadow_position(row) and str(row.get("status") or "") in {"OPEN", "PARTIAL", "EXIT_ORDERED", "RECONCILE_REQUIRED"}]
        notional = sum(int(row.get("submitted_qty") or row.get("requested_qty") or 0) * int(row.get("submitted_price") or row.get("requested_price") or 0) for row in submitted if str(row.get("side") or "").lower() == "buy")
        return {
            "promotion_count": len(orders),
            "submitted_count": len(submitted),
            "filled_count": len(filled),
            "open_position_count": len(open_positions),
            "total_notional_krw": int(notional),
        }

    def _risk_snapshot(self, *, trade_date: str) -> dict[str, Any]:
        settings = self._settings()
        cfg = ShadowSmallEntryOpsConfig.from_settings(settings)
        usage = self._usage(trade_date)
        live_audit = self._live_audit(trade_date)
        summary = dict(live_audit.get("summary") or {})
        gateway = self._gateway_snapshot()
        breaches: list[dict[str, Any]] = []
        def add(metric: str, value: float, limit: float, reason: str, pause_status: str) -> None:
            if value > limit:
                breaches.append({"metric": metric, "value": value, "limit": limit, "reason": reason, "pause_status": pause_status})

        daily = cfg.daily_limits
        risk = cfg.risk_limits
        add("promotion_count", usage["promotion_count"], int(daily.get("max_promotions_per_day") or 3), "SHADOW_SMALL_ENTRY_MAX_PROMOTIONS_EXCEEDED", STATUS_PAUSED_BY_RISK)
        add("submitted_count", usage["submitted_count"], int(daily.get("max_submitted_orders_per_day") or 3), "SHADOW_SMALL_ENTRY_MAX_SUBMITTED_EXCEEDED", STATUS_PAUSED_BY_RISK)
        add("filled_count", usage["filled_count"], int(daily.get("max_filled_orders_per_day") or 3), "SHADOW_SMALL_ENTRY_MAX_FILLED_EXCEEDED", STATUS_PAUSED_BY_RISK)
        add("total_notional_krw", usage["total_notional_krw"], int(daily.get("max_total_notional_krw") or 300000), "SHADOW_SMALL_ENTRY_NOTIONAL_LIMIT_EXCEEDED", STATUS_PAUSED_BY_RISK)
        add("open_position_count", usage["open_position_count"], int(daily.get("max_open_positions") or 1), "SHADOW_SMALL_ENTRY_OPEN_POSITION_LIMIT_EXCEEDED", STATUS_PAUSED_BY_RISK)
        add("unknown_submit_count", int(summary.get("unknown_submit_count") or 0), int(risk.get("max_unknown_submit_count") or 1), "SHADOW_SMALL_ENTRY_UNKNOWN_SUBMIT_LIMIT", STATUS_PAUSED_BY_ORDER_ERROR)
        add("reconcile_required_count", int(summary.get("reconcile_required_order_count") or 0), int(risk.get("max_reconcile_required_count") or 0), "SHADOW_SMALL_ENTRY_RECONCILE_REQUIRED", STATUS_PAUSED_BY_RECONCILE)
        add("cancel_requested_stale_count", int(summary.get("cancel_requested_stale_count") or 0), int(risk.get("max_cancel_requested_stale_count") or 0), "SHADOW_SMALL_ENTRY_CANCEL_STALE_LIMIT", STATUS_PAUSED_BY_ORDER_ERROR)
        if str(live_audit.get("status") or "").upper() in {"BROKEN", "RECONCILE_REQUIRED"} and bool(risk.get("pause_on_live_sim_audit_broken", True)):
            breaches.append({"metric": "live_sim_audit_status", "value": live_audit.get("status"), "limit": "OK/WARN", "reason": "SHADOW_SMALL_ENTRY_LIVE_SIM_AUDIT_BLOCK", "pause_status": STATUS_PAUSED_BY_AUDIT})
        if not bool(gateway.get("heartbeat_ok")) and bool(risk.get("pause_on_heartbeat_bad", True)):
            breaches.append({"metric": "gateway_heartbeat_ok", "value": 0, "limit": 1, "reason": "SHADOW_SMALL_ENTRY_GATEWAY_HEARTBEAT_BAD", "pause_status": STATUS_PAUSED_BY_AUDIT})
        status = "RECONCILE_REQUIRED" if any(item["pause_status"] == STATUS_PAUSED_BY_RECONCILE for item in breaches) else ("WARN" if breaches else "OK")
        return {
            "status": status,
            "breaches": breaches,
            "today": {
                **usage,
                "realized_pnl_krw": 0,
                "unrealized_pnl_krw": 0,
                "max_drawdown_pct": 0.0,
                "consecutive_losing_trades": 0,
                "order_reject_count": int(summary.get("rejected_count") or 0),
                "unknown_submit_count": int(summary.get("unknown_submit_count") or 0),
                "reconcile_required_count": int(summary.get("reconcile_required_order_count") or 0),
            },
            "live_sim_audit_status": str(live_audit.get("status") or "UNKNOWN"),
            "reconcile_status": "RECONCILE_REQUIRED" if int(summary.get("reconcile_required_order_count") or 0) else "OK",
            "exit_guard_ready": not _exit_guard_disabled(settings),
            "gateway_heartbeat_ok": bool(gateway.get("heartbeat_ok")),
            "checked_at": self._now(),
        }

    def _record_trace(self, kind: str, **payload: Any) -> None:
        stage = OPS_TRACE_STAGES.get(kind, kind)
        now = self._now()
        preflight = dict(payload.get("preflight") or {})
        risk = dict(payload.get("risk") or {})
        blocking_reasons = (
            list(payload.get("blocking_reasons") or [])
            or list(preflight.get("blocking_reasons") or [])
            or [item.get("reason") for item in (risk.get("breaches") or []) if item.get("reason")]
        )
        passed = not bool(blocking_reasons)
        self.db.save_buy_zero_trace_events(
            [
                {
                    "trace_id": f"shadow_small_entry_ops:{stage}:{now}:{secrets.token_hex(4)}",
                    "trade_date": self._today(),
                    "stage": stage,
                    "stage_status": str(payload.get("next_status") or preflight.get("status") or risk.get("status") or ""),
                    "pass_fail": "PASS" if passed else "FAIL",
                    "passed": passed,
                    "primary_block_reason": str(payload.get("reason") or ""),
                    "reason_codes": payload.get("reason_codes") or blocking_reasons,
                    "ops_status": str(payload.get("next_status") or payload.get("ops_status") or ""),
                    "previous_ops_status": str(payload.get("previous_status") or ""),
                    "next_ops_status": str(payload.get("next_status") or ""),
                    "preflight_status": str(preflight.get("status") or ""),
                    "blocking_reasons": blocking_reasons,
                    "risk_check_status": str(risk.get("status") or ""),
                    "risk_limit_breached": bool(risk.get("breaches")),
                    "breached_metric": str(((risk.get("breaches") or [{}])[0]).get("metric") or ""),
                    "breached_value": ((risk.get("breaches") or [{}])[0]).get("value"),
                    "breached_limit": ((risk.get("breaches") or [{}])[0]).get("limit"),
                    "operator_note": str(payload.get("operator_note") or ""),
                    "changed_by": str(payload.get("changed_by") or ""),
                    "activation_token_id": str(payload.get("activation_token_id") or ""),
                    "order_enabled_before": payload.get("order_enabled_before"),
                    "order_enabled_after": payload.get("order_enabled_after"),
                    "mode_before": str(payload.get("mode_before") or ""),
                    "mode_after": str(payload.get("mode_after") or ""),
                    "operator_message_ko": _operator_message(str(payload.get("next_status") or ""), preflight),
                    "details": {"shadow_small_entry_ops": payload},
                    "created_at": now,
                }
            ]
        )

    def _today(self) -> str:
        return self.now_provider().date().isoformat()

    def _now(self) -> str:
        return self.now_provider().isoformat(timespec="seconds")


def snapshot_payload(status: dict[str, Any]) -> dict[str, Any]:
    return dict(status or {})


def _is_shadow_order(row: Mapping[str, Any]) -> bool:
    details = dict(row.get("details") or {})
    return any(
        str(value or "").upper() in {"READY_SHADOW_SMALL_ENTRY", "BUY_ELIGIBLE_SHADOW_SMALL_ENTRY_GUARDED", "PROMOTED"}
        for value in [
            details.get("ready_type"),
            details.get("order_eligibility"),
            details.get("shadow_small_entry_promotion_status"),
            row.get("order_eligibility"),
        ]
    )


def _is_shadow_position(row: Mapping[str, Any]) -> bool:
    details = dict(row.get("details") or {})
    return bool(details.get("shadow_small_entry_promotion") or details.get("shadow_small_entry_promotion_status"))


def _settings_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _exit_guard_disabled(settings: StrategyRuntimeSettings) -> bool:
    guard = settings.value("live_sim_exit_guard", {})
    if isinstance(guard, Mapping):
        return guard.get("enabled") is False or guard.get("exit_guard_enabled") is False
    return False


def _operator_message(status: str, preflight: Mapping[str, Any]) -> str:
    status = str(status or "").upper()
    if status == STATUS_LIVE_SIM_ACTIVE:
        return "LIVE_SIM shadow 소액 진입이 활성화되어 있습니다."
    if status == STATUS_LIVE_SIM_ARMED:
        return "LIVE_SIM 활성화 확인 전 단계입니다. confirm과 operator note가 필요합니다."
    if status.startswith("PAUSED"):
        return "shadow 소액 신규 진입이 중단되었습니다. 기존 포지션 청산 로직은 유지됩니다."
    if status == STATUS_ROLLED_BACK:
        return "롤백 완료: 신규 shadow 소액 진입은 중단되고 관측 전용으로 전환되었습니다."
    if preflight and preflight.get("status") == "FAIL":
        return "사전 점검 실패로 LIVE_SIM 활성화를 진행할 수 없습니다."
    return "현재는 관측 전용입니다. shadow 주문은 나가지 않습니다."


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return datetime.min
