from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, time, timedelta
from typing import Any, Optional

from trading.strategy.candidates import normalize_code
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.market_index import MarketIndexStore
from trading.strategy.runtime_settings import legacy_strategy_runtime_settings
from trading.theme_engine.lab import (
    InstrumentMetadata,
    LabGateDecision,
    MarketSide,
    ThemeLabConfig,
    ThemeLabFlowEngine,
    ThemeLabFlowResult,
    WatchSetSnapshot,
    normalize_market_side,
)
from trading.theme_engine.models import StockSnapshot, ThemeMembership, ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository


@dataclass(frozen=True)
class MarketSideConfirmationPersistenceConfig:
    enabled: bool = True
    storage: str = "db"
    state_version: int = 1
    session_scope: str = "trade_date"
    ttl_sec: int = 21600
    reset_on_trade_date_change: bool = True
    reset_before_market_open: bool = True
    reset_after_market_close: bool = False
    max_state_age_sec: int = 300
    max_restore_age_sec_regular: int = 900
    max_restore_age_sec_pre_open: int = 0
    max_restore_age_sec_after_close: int = 0
    expire_on_regular_close: bool = True
    reset_on_session_id_change: bool = True
    fallback_to_memory_on_db_error: bool = True
    conservative_on_restore_failure: bool = True
    conservative_on_schedule_unknown: bool = True
    transition_log_enabled: bool = True
    transition_log_every_cycle: bool = False
    persist_unknown_market_state: bool = False
    force_reset_market_confirmation_state: bool = False

    @classmethod
    def from_settings(cls, settings: dict | None) -> "MarketSideConfirmationPersistenceConfig":
        payload = dict(settings or {})
        max_age = int(payload.get("max_state_age_sec") or payload.get("max_restore_age_sec_regular") or 300)
        return cls(
            enabled=_bool(payload.get("enabled"), True),
            storage=str(payload.get("storage") or "db"),
            state_version=int(payload.get("state_version") or 1),
            session_scope=str(payload.get("session_scope") or "trade_date"),
            ttl_sec=int(payload.get("ttl_sec") or 21600),
            reset_on_trade_date_change=_bool(payload.get("reset_on_trade_date_change"), True),
            reset_before_market_open=_bool(payload.get("reset_before_market_open"), True),
            reset_after_market_close=_bool(payload.get("reset_after_market_close"), False),
            max_state_age_sec=max_age,
            max_restore_age_sec_regular=int(payload.get("max_restore_age_sec_regular") or max_age or 900),
            max_restore_age_sec_pre_open=int(payload.get("max_restore_age_sec_pre_open") or 0),
            max_restore_age_sec_after_close=int(payload.get("max_restore_age_sec_after_close") or 0),
            expire_on_regular_close=_bool(payload.get("expire_on_regular_close"), True),
            reset_on_session_id_change=_bool(payload.get("reset_on_session_id_change"), True),
            fallback_to_memory_on_db_error=_bool(payload.get("fallback_to_memory_on_db_error"), True),
            conservative_on_restore_failure=_bool(payload.get("conservative_on_restore_failure"), True),
            conservative_on_schedule_unknown=_bool(payload.get("conservative_on_schedule_unknown"), True),
            transition_log_enabled=_bool(payload.get("transition_log_enabled"), True),
            transition_log_every_cycle=_bool(payload.get("transition_log_every_cycle"), False),
            persist_unknown_market_state=_bool(payload.get("persist_unknown_market_state"), False),
            force_reset_market_confirmation_state=_bool(payload.get("force_reset_market_confirmation_state"), False),
        )


@dataclass(frozen=True)
class MarketSessionConfig:
    enabled: bool = True
    timezone: str = "Asia/Seoul"
    exchange: str = "KRX"
    regular_open: str = "09:00"
    regular_close: str = "15:30"
    pre_open_start: str = "08:30"
    post_close_end: str = "16:00"
    session_id_format: str = "{trade_date}:{session_type}"
    reset_on_trade_date_change: bool = True
    reset_before_regular_open: bool = True
    allow_restore_during_regular_session: bool = True
    allow_restore_during_pre_open: bool = False
    allow_restore_after_close: bool = False
    allow_restore_on_holiday: bool = False
    holiday_calendar_source: str = "config_or_existing"
    holidays: tuple[str, ...] = ()
    schedule_source: str = "runtime_settings"
    fail_closed_on_schedule_error: bool = True

    @classmethod
    def from_settings(cls, settings: dict | None) -> "MarketSessionConfig":
        payload = dict(settings or {})
        holidays = payload.get("holidays") or payload.get("holiday_dates") or ()
        return cls(
            enabled=_bool(payload.get("enabled"), True),
            timezone=str(payload.get("timezone") or "Asia/Seoul"),
            exchange=str(payload.get("exchange") or "KRX"),
            regular_open=str(payload.get("regular_open") or "09:00"),
            regular_close=str(payload.get("regular_close") or "15:30"),
            pre_open_start=str(payload.get("pre_open_start") or "08:30"),
            post_close_end=str(payload.get("post_close_end") or "16:00"),
            session_id_format=str(payload.get("session_id_format") or "{trade_date}:{session_type}"),
            reset_on_trade_date_change=_bool(payload.get("reset_on_trade_date_change"), True),
            reset_before_regular_open=_bool(payload.get("reset_before_regular_open"), True),
            allow_restore_during_regular_session=_bool(payload.get("allow_restore_during_regular_session"), True),
            allow_restore_during_pre_open=_bool(payload.get("allow_restore_during_pre_open"), False),
            allow_restore_after_close=_bool(payload.get("allow_restore_after_close"), False),
            allow_restore_on_holiday=_bool(payload.get("allow_restore_on_holiday"), False),
            holiday_calendar_source=str(payload.get("holiday_calendar_source") or "config_or_existing"),
            holidays=tuple(str(item) for item in holidays),
            schedule_source=str(payload.get("schedule_source") or "runtime_settings"),
            fail_closed_on_schedule_error=_bool(payload.get("fail_closed_on_schedule_error"), True),
        )


@dataclass(frozen=True)
class MarketSessionContext:
    trade_date: str
    session_id: str
    session_type: str
    timezone: str
    schedule_source: str
    schedule_known: bool
    is_regular_session: bool
    restore_allowed: bool
    reset_required: bool
    reset_reason: str = ""
    reason_codes: tuple[str, ...] = ()
    transition_type: str = ""
    max_restore_age_sec: int = 0


class ThemeLabRuntimePipeline:
    def __init__(
        self,
        *,
        db,
        market_data: MarketDataStore,
        market_index_store: MarketIndexStore,
        interval_sec: int = 3,
        engine: Optional[ThemeLabFlowEngine] = None,
        persistence_config: MarketSideConfirmationPersistenceConfig | None = None,
        session_config: MarketSessionConfig | None = None,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.market_index_store = market_index_store
        self.interval_sec = max(1, int(interval_sec))
        self.engine = engine or ThemeLabFlowEngine(ThemeLabConfig())
        self.persistence_config = persistence_config or MarketSideConfirmationPersistenceConfig.from_settings(
            legacy_strategy_runtime_settings().value("market_side_confirmation_persistence", {})
        )
        self.session_config = session_config or MarketSessionConfig.from_settings(
            legacy_strategy_runtime_settings().value("market_session", {})
        )
        self.last_run_at: Optional[datetime] = None
        self.last_result: Optional[ThemeLabFlowResult] = None
        self._warnings: list[str] = []
        self._restored_session_key: tuple[str, str, int] | None = None
        self._last_session_context: MarketSessionContext | None = None
        self._market_confirmation_metrics: dict[str, Any] = _empty_market_confirmation_metrics()

    def run_if_due(self, now: datetime) -> Optional[ThemeLabFlowResult]:
        current = now.replace(microsecond=0)
        if self.last_run_at is not None and current < self.last_run_at + timedelta(seconds=self.interval_sec):
            return None
        self.last_run_at = current
        return self.run(current)

    def run(self, now: datetime) -> ThemeLabFlowResult:
        self._warnings = []
        repository = ThemeEngineRepository(self.db)
        theme_inputs = self._theme_inputs(repository)
        snapshots = self._snapshots(theme_inputs)
        if not theme_inputs:
            self._warnings.append("THEME_LAB_MAPPING_EMPTY")
        if not snapshots:
            self._warnings.append("THEME_LAB_QUOTES_EMPTY")
        missing_prev_close = [code for code, snapshot in snapshots.items() if not _prev_close(snapshot)]
        if missing_prev_close:
            self._warnings.append("THEME_LAB_PREV_CLOSE_MISSING")
        metadata_by_symbol = self._metadata_by_symbol(theme_inputs)
        session = self._market_session_context(now)
        self._market_confirmation_metrics = _empty_market_confirmation_metrics()
        self._restore_confirmation_state_once(now, session=session)
        result = self.engine.run_pipeline(
            theme_inputs=theme_inputs,
            snapshots=snapshots,
            metadata_by_symbol=metadata_by_symbol,
            kospi_return_pct=self.market_index_store.state("KOSPI").change_rate,
            kosdaq_return_pct=self.market_index_store.state("KOSDAQ").change_rate,
            calculated_at=now.isoformat(),
        )
        result = self._persist_confirmation_state(result, now, session=session)
        result = _annotate_market_session(result, session, self._market_confirmation_metrics)
        self.last_result = result
        self._save_result(result, now)
        return result

    def _session_id(self, now: datetime) -> str:
        return self._market_session_context(now).session_id

    def _market_session_context(self, now: datetime) -> MarketSessionContext:
        cfg = self.session_config
        trade_date = now.date().isoformat()
        if not cfg.enabled:
            return MarketSessionContext(
                trade_date=trade_date,
                session_id=trade_date,
                session_type="regular",
                timezone=cfg.timezone,
                schedule_source=cfg.schedule_source,
                schedule_known=True,
                is_regular_session=True,
                restore_allowed=True,
                reset_required=False,
                reason_codes=("MARKET_SESSION_REGULAR_OPEN",),
                transition_type="SESSION_OPEN",
                max_restore_age_sec=self.persistence_config.max_restore_age_sec_regular,
            )
        parsed = _parse_market_session_times(cfg)
        if parsed is None:
            reason = "MARKET_CONFIRMATION_STATE_SCHEDULE_UNKNOWN"
            return self._session_context(
                now,
                session_type="closed",
                schedule_known=False,
                restore_allowed=not cfg.fail_closed_on_schedule_error,
                reset_required=cfg.fail_closed_on_schedule_error,
                reset_reason=reason if cfg.fail_closed_on_schedule_error else "",
                reason_codes=("MARKET_SESSION_CLOSED", reason, "MARKET_CONFIRMATION_STATE_RESTORE_NOT_ALLOWED"),
                transition_type="SCHEDULE_UNKNOWN_CONSERVATIVE",
            )
        pre_open, regular_open, regular_close, post_close = parsed
        is_holiday = trade_date in set(cfg.holidays) or now.weekday() >= 5
        if is_holiday:
            return self._session_context(
                now,
                session_type="holiday",
                schedule_known=True,
                restore_allowed=cfg.allow_restore_on_holiday,
                reset_required=not cfg.allow_restore_on_holiday,
                reset_reason="" if cfg.allow_restore_on_holiday else "MARKET_CONFIRMATION_STATE_HOLIDAY",
                reason_codes=("MARKET_CONFIRMATION_STATE_HOLIDAY", "MARKET_CONFIRMATION_STATE_RESTORE_NOT_ALLOWED"),
                transition_type="RESTORE_SKIPPED",
            )
        current = now.time().replace(microsecond=0)
        if pre_open <= current < regular_open:
            return self._session_context(
                now,
                session_type="pre_open",
                schedule_known=True,
                restore_allowed=cfg.allow_restore_during_pre_open,
                reset_required=not cfg.allow_restore_during_pre_open,
                reset_reason="" if cfg.allow_restore_during_pre_open else "MARKET_CONFIRMATION_STATE_RESTORE_NOT_ALLOWED",
                reason_codes=("MARKET_SESSION_PRE_OPEN", "MARKET_CONFIRMATION_STATE_RESTORE_NOT_ALLOWED"),
                transition_type="RESTORE_SKIPPED",
            )
        if regular_open <= current < regular_close:
            return self._session_context(
                now,
                session_type="regular",
                schedule_known=True,
                restore_allowed=cfg.allow_restore_during_regular_session,
                reset_required=False,
                reason_codes=("MARKET_SESSION_REGULAR_OPEN",),
                transition_type="SESSION_OPEN",
            )
        if regular_close <= current <= post_close:
            reason = "MARKET_CONFIRMATION_STATE_RESET_ON_MARKET_CLOSE" if self.persistence_config.expire_on_regular_close else "MARKET_CONFIRMATION_STATE_SESSION_CLOSED"
            return self._session_context(
                now,
                session_type="post_close",
                schedule_known=True,
                restore_allowed=cfg.allow_restore_after_close,
                reset_required=not cfg.allow_restore_after_close,
                reset_reason="" if cfg.allow_restore_after_close else reason,
                reason_codes=("MARKET_SESSION_POST_CLOSE", "MARKET_CONFIRMATION_STATE_RESTORE_NOT_ALLOWED", reason),
                transition_type="SESSION_CLOSE",
            )
        reason = "MARKET_CONFIRMATION_STATE_SESSION_CLOSED"
        return self._session_context(
            now,
            session_type="closed",
            schedule_known=True,
            restore_allowed=False,
            reset_required=True,
            reset_reason=reason,
            reason_codes=("MARKET_SESSION_CLOSED", "MARKET_CONFIRMATION_STATE_RESTORE_NOT_ALLOWED", reason),
            transition_type="RESTORE_SKIPPED",
        )

    def _session_context(
        self,
        now: datetime,
        *,
        session_type: str,
        schedule_known: bool,
        restore_allowed: bool,
        reset_required: bool,
        reset_reason: str = "",
        reason_codes: tuple[str, ...] = (),
        transition_type: str = "",
    ) -> MarketSessionContext:
        trade_date = now.date().isoformat()
        cfg = self.session_config
        max_restore_age = _max_restore_age_for_session(self.persistence_config, session_type)
        session_id = _format_session_id(cfg.session_id_format, trade_date=trade_date, session_type=session_type)
        boundary_reasons = ("MARKET_SESSION_BOUNDARY_DETECTED",) if session_type != "regular" else ()
        return MarketSessionContext(
            trade_date=trade_date,
            session_id=session_id,
            session_type=session_type,
            timezone=cfg.timezone,
            schedule_source=cfg.schedule_source,
            schedule_known=schedule_known,
            is_regular_session=session_type == "regular",
            restore_allowed=bool(restore_allowed and schedule_known),
            reset_required=bool(reset_required),
            reset_reason=reset_reason,
            reason_codes=_dedupe_tuple(boundary_reasons + reason_codes),
            transition_type=transition_type,
            max_restore_age_sec=max_restore_age,
        )

    def _restore_confirmation_state_once(self, now: datetime, *, session: MarketSessionContext) -> None:
        cfg = self.persistence_config
        trade_date = session.trade_date
        session_id = session.session_id
        key = (trade_date, session_id, int(cfg.state_version))
        if self._restored_session_key == key:
            return
        self._restored_session_key = key
        tracker = getattr(self.engine, "market_side_confirmation", None)
        if tracker is None or not cfg.enabled or str(cfg.storage).lower() != "db":
            return
        self._record_restore_metric("attempt")
        session_change_reason = self._session_change_reset_reason(session)
        if session_change_reason:
            tracker.mark_state_reset(session_change_reason)
            self._record_reset_metric(session_change_reason)
            self._log_restore_reset(now, session, session_change_reason, transition_type=_transition_type_for_reset_reason(session_change_reason))
        self._last_session_context = session
        if cfg.force_reset_market_confirmation_state:
            reason = "MARKET_CONFIRMATION_STATE_FORCE_RESET"
            tracker.mark_state_reset(reason)
            tracker.mark_restore_skipped(reason=reason, conservative=True)
            self._record_restore_metric("skipped")
            self._record_reset_metric(reason)
            self._log_restore_reset(now, session, reason, transition_type="RESET_FORCE")
            return
        if not session.schedule_known:
            reason = "MARKET_CONFIRMATION_STATE_SCHEDULE_UNKNOWN"
            tracker.mark_state_reset(reason)
            tracker.mark_restore_skipped(reason=reason, conservative=cfg.conservative_on_schedule_unknown)
            self._record_restore_metric("skipped")
            self._record_reset_metric(reason)
            self._market_confirmation_metrics["market_confirmation_schedule_unknown_count"] += 1
            if cfg.conservative_on_schedule_unknown:
                self._market_confirmation_metrics["market_confirmation_conservative_fallback_count"] += 1
            self._log_restore_reset(now, session, reason, transition_type="SCHEDULE_UNKNOWN_CONSERVATIVE")
            return
        if not session.restore_allowed:
            reason = session.reset_reason or "MARKET_CONFIRMATION_STATE_RESTORE_NOT_ALLOWED"
            tracker.mark_state_reset(reason)
            tracker.mark_restore_skipped(reason=reason, conservative=True)
            self._record_restore_metric("skipped")
            self._record_reset_metric(reason)
            self._market_confirmation_metrics["market_confirmation_conservative_fallback_count"] += 1
            self._log_restore_reset(now, session, reason, transition_type=session.transition_type or _transition_type_for_reset_reason(reason))
            return
        load = getattr(self.db, "load_market_side_confirmation_states", None)
        if not callable(load):
            return
        try:
            rows = load(trade_date=trade_date, session_id=session_id, state_version=cfg.state_version)
            valid_rows, age_by_side, reset_reason = self._valid_restore_rows(rows, now, session=session)
            if not rows:
                load_any = getattr(self.db, "load_any_market_side_confirmation_states", None)
                if callable(load_any):
                    any_rows = load_any(trade_date=trade_date, session_id=session_id)
                    _, _, any_reset_reason = self._valid_restore_rows(
                        any_rows,
                        now,
                        session=session,
                    )
                    reset_reason = reset_reason or any_reset_reason
                load_trade_date = getattr(self.db, "load_market_side_confirmation_states_for_trade_date", None)
                if not reset_reason and callable(load_trade_date):
                    trade_date_rows = load_trade_date(trade_date=trade_date)
                    _, _, trade_date_reset_reason = self._valid_restore_rows(trade_date_rows, now, session=session)
                    reset_reason = reset_reason or trade_date_reset_reason
                load_recent = getattr(self.db, "load_recent_market_side_confirmation_states", None)
                if not reset_reason and callable(load_recent):
                    recent_rows = load_recent(limit=8)
                    _, _, recent_reset_reason = self._valid_restore_rows(recent_rows, now, session=session)
                    reset_reason = recent_reset_reason if recent_reset_reason == "MARKET_CONFIRMATION_STATE_DATE_MISMATCH" else ""
            if reset_reason:
                tracker.mark_state_reset(reset_reason)
                tracker.mark_restore_skipped(reason=reset_reason, conservative=False)
                self._record_restore_metric("skipped")
                self._record_reset_metric(reset_reason)
                self._log_restore_reset(now, session, reset_reason, transition_type=_transition_type_for_reset_reason(reset_reason))
            if valid_rows:
                tracker.restore_states(valid_rows, restored_at=now.isoformat(), state_version=cfg.state_version, state_age_by_side=age_by_side)
                self._record_restore_metric("success", rows=valid_rows, age_by_side=age_by_side)
                self._log_session_transition(now, session, "RESTORE_ALLOWED", ("MARKET_CONFIRMATION_STATE_RESTORED",))
                self._warnings.append("MARKET_CONFIRMATION_STATE_RESTORED")
        except Exception as exc:
            self._warnings.append(f"MARKET_CONFIRMATION_STATE_DB_ERROR:{exc}")
            self._save_runtime_event_safe(
                "market_confirmation_state_restore",
                "error",
                "MARKET_CONFIRMATION_STATE_DB_ERROR",
                {"error": str(exc), "trade_date": trade_date, "session_id": session_id},
            )
            tracker.mark_restore_failure(
                reason="MARKET_CONFIRMATION_STATE_DB_ERROR",
                conservative=cfg.conservative_on_restore_failure,
            )
            self._record_restore_metric("failed")
            self._market_confirmation_metrics["market_confirmation_memory_fallback_count"] += 1
            if cfg.conservative_on_restore_failure:
                self._market_confirmation_metrics["market_confirmation_conservative_fallback_count"] += 1

    def _valid_restore_rows(
        self,
        rows: list[dict],
        now: datetime,
        *,
        session: MarketSessionContext,
    ) -> tuple[list[dict], dict[str, float | None], str]:
        cfg = self.persistence_config
        valid: list[dict] = []
        age_by_side: dict[str, float | None] = {}
        reset_reason = ""
        for row in rows or []:
            side = str(row.get("market_side") or "")
            if side not in {MarketSide.KOSPI.value, MarketSide.KOSDAQ.value} and not cfg.persist_unknown_market_state:
                continue
            if not str(row.get("confirmed_status") or ""):
                reset_reason = "MARKET_CONFIRMATION_STATE_ROW_INVALID"
                continue
            if str(row.get("trade_date") or "") != session.trade_date:
                reset_reason = "MARKET_CONFIRMATION_STATE_DATE_MISMATCH"
                continue
            if str(row.get("session_id") or "") != session.session_id:
                reset_reason = "MARKET_CONFIRMATION_STATE_SESSION_MISMATCH"
                continue
            if int(row.get("state_version") or 0) != int(cfg.state_version):
                reset_reason = "MARKET_CONFIRMATION_STATE_VERSION_MISMATCH"
                continue
            expires_at = _parse_datetime(row.get("expires_at"))
            if expires_at is not None and expires_at < now.replace(tzinfo=None):
                reset_reason = "MARKET_CONFIRMATION_STATE_EXPIRED"
                continue
            updated_at = _parse_datetime(row.get("updated_at"))
            age_sec = None
            if updated_at is not None:
                age_sec = max(0.0, (now.replace(tzinfo=None) - updated_at).total_seconds())
                if session.max_restore_age_sec > 0 and age_sec > session.max_restore_age_sec:
                    reset_reason = "MARKET_CONFIRMATION_STATE_RESTORE_AGE_EXCEEDED"
                    continue
            age_by_side[side] = age_sec
            valid.append(row)
        return valid, age_by_side, reset_reason

    def _persist_confirmation_state(
        self,
        result: ThemeLabFlowResult,
        now: datetime,
        *,
        session: MarketSessionContext,
    ) -> ThemeLabFlowResult:
        cfg = self.persistence_config
        tracker = getattr(self.engine, "market_side_confirmation", None)
        if tracker is None or not cfg.enabled or str(cfg.storage).lower() != "db":
            return result
        states = tracker.states_for_persistence()
        if not states:
            return result
        persisted_by_side: dict[str, dict] = {}
        now_text = now.isoformat()
        expires_at = (now + timedelta(seconds=max(1, int(cfg.ttl_sec or 1)))).isoformat()
        for state in states:
            side = str(state.get("side") or "")
            if side not in {MarketSide.KOSPI.value, MarketSide.KOSDAQ.value} and not cfg.persist_unknown_market_state:
                continue
            payload = {
                **state,
                "market_side": side,
                "raw_status": state.get("current_raw_status", ""),
                "last_reason_codes": state.get("reason_codes") or [],
                "last_cycle_id": state.get("cycle_id") or now_text,
                "last_evaluated_at": now_text,
                "trade_date": session.trade_date,
                "session_id": session.session_id,
                "state_version": cfg.state_version,
                "updated_at": now_text,
                "created_at": now_text,
                "expires_at": expires_at,
            }
            try:
                saved = self.db.upsert_market_side_confirmation_state(payload)
                tracker.mark_persist_result(side, persisted=True, updated_at=now_text)
                state_source = str(state.get("market_confirmation_state_source") or "memory")
                if state_source == "memory":
                    state_source = "restored_db" if state.get("market_confirmation_state_restored") else "db"
                persisted_by_side[side] = {
                    "market_confirmation_state_persisted": True,
                    "market_confirmation_state_last_updated_at": saved.get("updated_at") or now_text,
                    "market_confirmation_state_version": cfg.state_version,
                    "market_confirmation_state_source": state_source,
                    "market_confirmation_state_expires_at": saved.get("expires_at") or expires_at,
                    "market_confirmation_transition_type": state.get("transition_type") or "",
                }
                self._persist_transition_log(state, now, session=session)
            except Exception as exc:
                tracker.mark_persist_result(side, persisted=False, reason="MARKET_CONFIRMATION_STATE_DB_ERROR", updated_at=now_text)
                persisted_by_side[side] = {
                    "market_confirmation_state_persisted": False,
                    "market_confirmation_state_source": "db_failed_memory_fallback",
                    "market_confirmation_state_restore_reason": "MARKET_CONFIRMATION_STATE_DB_ERROR",
                    "market_confirmation_transition_type": state.get("transition_type") or "",
                }
                self._warnings.append(f"MARKET_CONFIRMATION_STATE_PERSIST_FAILED:{side}:{exc}")
                self._save_runtime_event_safe(
                    "market_confirmation_state_persist",
                    "error",
                    "MARKET_CONFIRMATION_STATE_PERSIST_FAILED",
                    {"error": str(exc), "side": side, "trade_date": session.trade_date, "session_id": session.session_id},
                )
        return _annotate_market_confirmation_persistence(result, persisted_by_side)

    def _persist_transition_log(self, state: dict, now: datetime, *, session: MarketSessionContext) -> None:
        cfg = self.persistence_config
        transition_type = str(state.get("transition_type") or "")
        if not cfg.transition_log_enabled:
            return
        if not cfg.transition_log_every_cycle and transition_type in {"", "NO_CHANGE"}:
            return
        try:
            inserted = self.db.save_market_side_confirmation_transition(
                {
                    **state,
                    "trade_date": session.trade_date,
                    "session_id": session.session_id,
                    "market_side": state.get("side") or "",
                    "cycle_id": state.get("cycle_id") or now.isoformat(),
                    "previous_raw_status": "",
                    "new_raw_status": state.get("current_raw_status") or "",
                    "new_confirmed_status": state.get("confirmed_status") or "",
                    "new_confirmation_pending": state.get("confirmation_pending"),
                    "new_recovery_pending": state.get("recovery_pending"),
                    "transition_reason_codes": state.get("reason_codes") or [],
                    "transition_type": transition_type or "NO_CHANGE",
                    "created_at": now.isoformat(),
                }
            )
            if inserted:
                self._warnings.append("MARKET_CONFIRMATION_TRANSITION_LOGGED")
        except Exception as exc:
            self._warnings.append(f"MARKET_CONFIRMATION_TRANSITION_LOG_FAILED:{exc}")
            self._save_runtime_event_safe(
                "market_confirmation_transition_log",
                "error",
                "MARKET_CONFIRMATION_TRANSITION_LOG_FAILED",
                {"error": str(exc), "side": state.get("side") or "", "transition_type": transition_type},
            )

    def _log_restore_reset(self, now: datetime, session: MarketSessionContext, reset_reason: str, *, transition_type: str = "") -> None:
        if not self.persistence_config.transition_log_enabled:
            return
        save = getattr(self.db, "save_market_side_confirmation_transition", None)
        if not callable(save):
            return
        for side in (MarketSide.KOSPI.value, MarketSide.KOSDAQ.value):
            try:
                save(
                    {
                        "trade_date": session.trade_date,
                        "session_id": session.session_id,
                        "market_side": side,
                        "cycle_id": now.isoformat(),
                        "transition_type": transition_type or _transition_type_for_reset_reason(reset_reason),
                        "transition_reason_codes": ["MARKET_CONFIRMATION_STATE_RESET", reset_reason],
                        "created_at": now.isoformat(),
                    }
                )
            except Exception:
                return

    def _log_session_transition(
        self,
        now: datetime,
        session: MarketSessionContext,
        transition_type: str,
        reason_codes: tuple[str, ...],
    ) -> None:
        if not self.persistence_config.transition_log_enabled:
            return
        save = getattr(self.db, "save_market_side_confirmation_transition", None)
        if not callable(save):
            return
        for side in (MarketSide.KOSPI.value, MarketSide.KOSDAQ.value):
            try:
                save(
                    {
                        "trade_date": session.trade_date,
                        "session_id": session.session_id,
                        "market_side": side,
                        "cycle_id": now.isoformat(),
                        "transition_type": transition_type,
                        "transition_reason_codes": list(reason_codes),
                        "created_at": now.isoformat(),
                    }
                )
            except Exception:
                return

    def _session_change_reset_reason(self, session: MarketSessionContext) -> str:
        previous = self._last_session_context
        if previous is None:
            return ""
        if previous.trade_date != session.trade_date and self.persistence_config.reset_on_trade_date_change:
            return "MARKET_CONFIRMATION_STATE_RESET_ON_TRADE_DATE_CHANGE"
        if previous.session_id != session.session_id and self.persistence_config.reset_on_session_id_change:
            return "MARKET_CONFIRMATION_STATE_RESET_ON_SESSION_CHANGE"
        return ""

    def _record_restore_metric(
        self,
        status: str,
        *,
        rows: list[dict] | None = None,
        age_by_side: dict[str, float | None] | None = None,
    ) -> None:
        metrics = self._market_confirmation_metrics
        if status == "attempt":
            metrics["market_confirmation_restore_attempt_count"] += 1
        elif status == "success":
            metrics["market_confirmation_restore_success_count"] += 1
            by_side = {str(row.get("market_side") or "") for row in rows or []}
            if MarketSide.KOSPI.value in by_side:
                metrics["kospi_confirmation_restore_success_count"] += 1
            if MarketSide.KOSDAQ.value in by_side:
                metrics["kosdaq_confirmation_restore_success_count"] += 1
            ages = [float(age) for age in (age_by_side or {}).values() if age is not None]
            if ages:
                metrics["market_confirmation_avg_state_age_sec"] = sum(ages) / len(ages)
                metrics["market_confirmation_max_state_age_sec"] = max(ages)
        elif status == "skipped":
            metrics["market_confirmation_restore_skipped_count"] += 1
        elif status == "failed":
            metrics["market_confirmation_restore_failed_count"] += 1

    def _record_reset_metric(self, reason: str) -> None:
        metrics = self._market_confirmation_metrics
        metrics["market_confirmation_reset_count"] += 1
        by_reason = dict(metrics.get("market_confirmation_reset_by_reason") or {})
        by_reason[reason] = int(by_reason.get(reason) or 0) + 1
        metrics["market_confirmation_reset_by_reason"] = by_reason
        if reason in {"MARKET_CONFIRMATION_STATE_STALE", "MARKET_CONFIRMATION_STATE_RESTORE_AGE_EXCEEDED"}:
            metrics["market_confirmation_state_stale_count"] += 1
        if reason == "MARKET_CONFIRMATION_STATE_EXPIRED":
            metrics["market_confirmation_state_expired_count"] += 1
        if reason in {"MARKET_CONFIRMATION_STATE_SESSION_MISMATCH", "MARKET_CONFIRMATION_STATE_RESET_ON_SESSION_CHANGE"}:
            metrics["kospi_confirmation_reset_count"] += 1
            metrics["kosdaq_confirmation_reset_count"] += 1

    def _save_runtime_event_safe(self, event_type: str, status: str, message: str, payload: dict) -> None:
        save_event = getattr(self.db, "save_runtime_event", None)
        if not callable(save_event):
            return
        try:
            save_event(event_type, status=status, message=message, payload=payload)
        except Exception:
            return

    def watchset_codes(self) -> list[str]:
        if self.last_result is None:
            return []
        watchset_limits = getattr(getattr(self.engine, "config", None), "watchset_limits", None)
        limit = max(1, int(getattr(watchset_limits, "max_watchset_size", 100) or 100))
        codes: list[str] = []

        def add_code(raw_code: str) -> None:
            code = normalize_code(raw_code)
            if code and code not in codes and len(codes) < limit:
                codes.append(code)

        for item in self.last_result.watchset:
            if int(item.condition_level or 0) >= 2:
                add_code(item.symbol)
        if len(codes) >= limit:
            return codes

        for theme in self.last_result.themes:
            hits = [
                hit
                for hit in theme.member_hits
                if not hit.excluded and (hit.leader_hit or hit.strong_hit)
            ]
            for hit in sorted(
                hits,
                key=lambda item: (
                    1 if item.leader_hit else 0,
                    1 if item.strong_hit else 0,
                    float(item.turnover_krw or 0),
                    float(item.return_pct or 0),
                ),
                reverse=True,
            ):
                add_code(hit.symbol)
                if len(codes) >= limit:
                    return codes
        return codes

    def drain_warnings(self) -> list[str]:
        warnings = list(self._warnings)
        self._warnings = []
        return warnings

    def _theme_inputs(self, repository: ThemeEngineRepository) -> list[tuple[str, str, list[ThemeMembership]]]:
        themes = [
            theme
            for theme in repository.list_canonical_themes()
            if _enum_value(theme.status) in {ThemeStatus.ACTIVE.value, ThemeStatus.WATCH.value, ThemeStatus.CANDIDATE.value}
        ]
        inputs: list[tuple[str, str, list[ThemeMembership]]] = []
        for theme in themes:
            members = repository.get_members_by_theme(theme.theme_id, active=True)
            if members:
                inputs.append((theme.theme_id, theme.display_name or theme.canonical_name, members))
        return inputs

    def _snapshots(self, theme_inputs: list[tuple[str, str, list[ThemeMembership]]]) -> dict[str, StockSnapshot]:
        codes = {
            normalize_code(member.stock_code)
            for _, _, members in theme_inputs
            for member in members
            if normalize_code(member.stock_code)
        }
        snapshots: dict[str, StockSnapshot] = {}
        for code in sorted(codes):
            tick = self.market_data.latest_tick(code)
            if tick is None:
                continue
            snapshots[code] = _stock_snapshot_from_tick(tick)
        return snapshots

    def _metadata_by_symbol(self, theme_inputs: list[tuple[str, str, list[ThemeMembership]]]) -> dict[str, InstrumentMetadata]:
        codes = {
            normalize_code(member.stock_code)
            for _, _, members in theme_inputs
            for member in members
            if normalize_code(member.stock_code)
        }
        return _legacy_market_metadata(getattr(self.db, "conn", None), codes)

    def _save_result(self, result: ThemeLabFlowResult, now: datetime) -> None:
        payload = _result_payload(result)
        save = getattr(self.db, "save_theme_lab_flow_result", None)
        if callable(save):
            save(now.isoformat(), payload)


def _annotate_market_confirmation_persistence(
    result: ThemeLabFlowResult,
    persisted_by_side: dict[str, dict],
) -> ThemeLabFlowResult:
    if not persisted_by_side:
        return result

    def fields_for(market_side: str) -> dict:
        side = normalize_market_side(market_side)
        return dict(persisted_by_side.get(side.value) or {})

    watchset: list[WatchSetSnapshot] = []
    for watch in result.watchset:
        fields = fields_for(watch.candidate_market)
        watchset.append(replace(watch, **fields) if fields else watch)

    decisions: list[LabGateDecision] = []
    for decision in result.gate_decisions:
        fields = fields_for(decision.candidate_market)
        decisions.append(replace(decision, **fields) if fields else decision)

    side_states = {
        side: {**dict(state or {}), **dict(persisted_by_side.get(side) or {})}
        for side, state in dict(result.market.side_confirmation_states or {}).items()
    }
    return replace(
        result,
        market=replace(result.market, side_confirmation_states=side_states),
        watchset=tuple(watchset),
        gate_decisions=tuple(decisions),
    )


def _annotate_market_session(
    result: ThemeLabFlowResult,
    session: MarketSessionContext,
    metrics: dict[str, Any],
) -> ThemeLabFlowResult:
    session_fields = _market_session_fields(session, metrics)
    watchset = tuple(
        replace(
            watch,
            **{
                **session_fields,
                "market_side_reason_codes": _dedupe_tuple(tuple(watch.market_side_reason_codes or ()) + session.reason_codes),
            },
        )
        for watch in result.watchset
    )
    decisions = tuple(
        replace(
            decision,
            **{
                **session_fields,
                "market_side_reason_codes": _dedupe_tuple(tuple(decision.market_side_reason_codes or ()) + session.reason_codes),
            },
        )
        for decision in result.gate_decisions
    )
    data_quality = dict(result.data_quality or {})
    data_quality["market_confirmation_session"] = dict(metrics)
    return replace(
        result,
        market=replace(result.market, **session_fields),
        watchset=watchset,
        gate_decisions=decisions,
        data_quality=data_quality,
    )


def _market_session_fields(session: MarketSessionContext, metrics: dict[str, Any]) -> dict[str, Any]:
    reset_count = int(metrics.get("market_confirmation_reset_count") or 0)
    fields = {
        "market_session_id": session.session_id,
        "market_session_type": session.session_type,
        "market_trade_date": session.trade_date,
        "market_timezone": session.timezone,
        "market_schedule_source": session.schedule_source,
        "market_schedule_known": session.schedule_known,
        "market_is_regular_session": session.is_regular_session,
        "market_restore_allowed": session.restore_allowed,
        "market_reset_required": session.reset_required,
        "market_reset_reason": session.reset_reason,
        "market_confirmation_state_restore_skipped": int(metrics.get("market_confirmation_restore_skipped_count") or 0) > 0,
        "market_confirmation_state_reset_count": reset_count,
        "market_confirmation_state_max_restore_age_sec": session.max_restore_age_sec,
        "market_confirmation_metrics": dict(metrics),
        "market_session_reason_codes": session.reason_codes,
    }
    if session.reset_reason:
        fields["market_confirmation_state_restore_reason"] = session.reset_reason
    return fields


def _stock_snapshot_from_tick(tick: StrategyTick) -> StockSnapshot:
    metadata = dict(tick.metadata or {})
    return StockSnapshot(
        stock_code=tick.code,
        stock_name=str(metadata.get("name") or metadata.get("stock_name") or ""),
        current_price=float(tick.price or 0),
        change_rate=float(tick.change_rate or 0.0),
        volume=int(tick.cum_volume or 0),
        turnover=float(tick.trade_value or 0.0),
        execution_strength=float(tick.execution_strength or 0.0),
        best_bid=float(tick.best_bid or 0),
        best_ask=float(tick.best_ask or 0),
        session_high=float(metadata.get("session_high") or metadata.get("day_high") or 0),
        session_low=float(metadata.get("session_low") or metadata.get("day_low") or 0),
        momentum_1m=_float(metadata.get("momentum_1m")),
        momentum_3m=_float(metadata.get("momentum_3m")),
        momentum_5m=_float(metadata.get("momentum_5m")),
        turnover_strength=max(0.0, _float(metadata.get("turnover_strength")) or 1.0),
        ts=tick.timestamp.isoformat() if tick.timestamp else "",
        updated_at=tick.timestamp.isoformat() if tick.timestamp else "",
        metadata=metadata,
    )


def _legacy_market_metadata(conn: Any, codes: set[str]) -> dict[str, InstrumentMetadata]:
    if conn is None or not codes or not _table_exists(conn, "legacy_theme_mappings_archive"):
        return {}
    columns = _table_columns(conn, "legacy_theme_mappings_archive")
    required = {"code", "market"}
    if not required <= columns:
        return {}
    select_columns = ["code", "market"]
    for optional in ("name", "strategy_profile", "theme_id", "theme_name", "enabled"):
        if optional in columns:
            select_columns.append(optional)
    placeholders = ",".join("?" for _ in codes)
    order = " ORDER BY code"
    if "enabled" in columns:
        order = " ORDER BY code, enabled DESC"
    rows = conn.execute(
        f"""
        SELECT {", ".join(select_columns)}
        FROM legacy_theme_mappings_archive
        WHERE code IN ({placeholders})
        {order}
        """,
        tuple(sorted(codes)),
    ).fetchall()
    result: dict[str, InstrumentMetadata] = {}
    for row in rows:
        payload = _row_mapping(row, select_columns)
        code = normalize_code(payload.get("code"))
        if not code or code in result:
            continue
        market = str(payload.get("market") or "").strip()
        if normalize_market_side(market) == MarketSide.UNKNOWN:
            continue
        raw = {
            "market": market,
            "market_source": "legacy_theme_mappings_archive",
            "strategy_profile": str(payload.get("strategy_profile") or ""),
            "theme_id": str(payload.get("theme_id") or ""),
            "theme_name": str(payload.get("theme_name") or ""),
        }
        result[code] = InstrumentMetadata(
            symbol=code,
            name=str(payload.get("name") or ""),
            raw=raw,
        )
    return result


def _table_exists(conn: Any, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: Any, table_name: str) -> set[str]:
    columns: set[str] = set()
    for row in conn.execute(f"PRAGMA table_info({table_name})"):
        columns.add(str(row["name"] if hasattr(row, "keys") else row[1]))
    return columns


def _row_mapping(row: Any, columns: list[str]) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return dict(row)
    return {column: row[index] for index, column in enumerate(columns)}


def _float(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "").replace("+", "").replace("%", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _prev_close(snapshot: StockSnapshot) -> float:
    for key in ("prev_close", "previous_close", "yesterday_close"):
        try:
            value = float((snapshot.metadata or {}).get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return 0.0


def _parse_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def _parse_market_session_times(cfg: MarketSessionConfig) -> tuple[time, time, time, time] | None:
    pre_open = _parse_time(cfg.pre_open_start)
    regular_open = _parse_time(cfg.regular_open)
    regular_close = _parse_time(cfg.regular_close)
    post_close = _parse_time(cfg.post_close_end)
    if None in {pre_open, regular_open, regular_close, post_close}:
        return None
    if not (pre_open <= regular_open < regular_close <= post_close):
        return None
    return pre_open, regular_open, regular_close, post_close


def _parse_time(value: str) -> time | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        hour, minute = text.split(":", 1)
        return time(int(hour), int(minute))
    except (TypeError, ValueError):
        return None


def _format_session_id(format_text: str, *, trade_date: str, session_type: str) -> str:
    template = str(format_text or "{trade_date}:{session_type}")
    try:
        return template.format(trade_date=trade_date, session_type=session_type)
    except (KeyError, ValueError):
        return f"{trade_date}:{session_type}"


def _max_restore_age_for_session(cfg: MarketSideConfirmationPersistenceConfig, session_type: str) -> int:
    if session_type == "regular":
        return int(cfg.max_restore_age_sec_regular or cfg.max_state_age_sec or 0)
    if session_type == "pre_open":
        return int(cfg.max_restore_age_sec_pre_open or 0)
    return int(cfg.max_restore_age_sec_after_close or 0)


def _transition_type_for_reset_reason(reason: str) -> str:
    mapping = {
        "MARKET_CONFIRMATION_STATE_FORCE_RESET": "RESET_FORCE",
        "MARKET_CONFIRMATION_STATE_RESET_ON_TRADE_DATE_CHANGE": "RESET_ON_TRADE_DATE_CHANGE",
        "MARKET_CONFIRMATION_STATE_DATE_MISMATCH": "RESET_ON_TRADE_DATE_CHANGE",
        "MARKET_CONFIRMATION_STATE_RESET_ON_SESSION_CHANGE": "RESET_ON_SESSION_BOUNDARY",
        "MARKET_CONFIRMATION_STATE_SESSION_MISMATCH": "RESET_ON_SESSION_BOUNDARY",
        "MARKET_CONFIRMATION_STATE_RESET_ON_MARKET_CLOSE": "RESET_ON_MARKET_CLOSE",
        "MARKET_CONFIRMATION_STATE_RESTORE_AGE_EXCEEDED": "RESET_STALE",
        "MARKET_CONFIRMATION_STATE_STALE": "RESET_STALE",
        "MARKET_CONFIRMATION_STATE_EXPIRED": "RESET_EXPIRED",
        "MARKET_CONFIRMATION_STATE_SCHEDULE_UNKNOWN": "SCHEDULE_UNKNOWN_CONSERVATIVE",
        "MARKET_CONFIRMATION_STATE_RESTORE_NOT_ALLOWED": "RESTORE_SKIPPED",
    }
    return mapping.get(str(reason or ""), "STATE_RESET")


def _empty_market_confirmation_metrics() -> dict[str, Any]:
    return {
        "market_confirmation_restore_attempt_count": 0,
        "market_confirmation_restore_success_count": 0,
        "market_confirmation_restore_skipped_count": 0,
        "market_confirmation_restore_failed_count": 0,
        "market_confirmation_reset_count": 0,
        "market_confirmation_reset_by_reason": {},
        "market_confirmation_state_stale_count": 0,
        "market_confirmation_state_expired_count": 0,
        "market_confirmation_schedule_unknown_count": 0,
        "market_confirmation_memory_fallback_count": 0,
        "market_confirmation_conservative_fallback_count": 0,
        "market_confirmation_avg_state_age_sec": None,
        "market_confirmation_max_state_age_sec": None,
        "kospi_confirmation_restore_success_count": 0,
        "kosdaq_confirmation_restore_success_count": 0,
        "kospi_confirmation_reset_count": 0,
        "kosdaq_confirmation_reset_count": 0,
    }


def _dedupe_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _bool(value, default: bool) -> bool:
    if value in (None, ""):
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _result_payload(result: ThemeLabFlowResult) -> dict:
    return {
        "market_status": _asdict(result.market),
        "theme_rankings": [_asdict(item) for item in result.themes],
        "theme_condition_snapshots": [_asdict(item) for item in result.themes],
        "condition_hit_snapshots": [
            _asdict(hit)
            for theme in result.themes
            for hit in theme.member_hits
        ],
        "watchset_snapshots": [_asdict(item) for item in result.watchset],
        "gate_decisions": [_asdict(item) for item in result.gate_decisions],
        "data_quality": dict(result.data_quality),
    }


def _asdict(value) -> dict:
    def normalize(item):
        if hasattr(item, "value"):
            return item.value
        if isinstance(item, tuple):
            return [normalize(child) for child in item]
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, dict):
            return {str(key): normalize(child) for key, child in item.items()}
        return item

    return normalize(asdict(value))


def dumps_result_payload(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _enum_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)
