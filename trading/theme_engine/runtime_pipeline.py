from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Optional

from trading.strategy.candidates import normalize_code
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.market_index import MarketIndexStore
from trading.strategy.runtime_settings import legacy_strategy_runtime_settings
from trading.theme_engine.lab import (
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
    fallback_to_memory_on_db_error: bool = True
    conservative_on_restore_failure: bool = True
    transition_log_enabled: bool = True
    transition_log_every_cycle: bool = False
    persist_unknown_market_state: bool = False
    force_reset_market_confirmation_state: bool = False

    @classmethod
    def from_settings(cls, settings: dict | None) -> "MarketSideConfirmationPersistenceConfig":
        payload = dict(settings or {})
        return cls(
            enabled=_bool(payload.get("enabled"), True),
            storage=str(payload.get("storage") or "db"),
            state_version=int(payload.get("state_version") or 1),
            session_scope=str(payload.get("session_scope") or "trade_date"),
            ttl_sec=int(payload.get("ttl_sec") or 21600),
            reset_on_trade_date_change=_bool(payload.get("reset_on_trade_date_change"), True),
            reset_before_market_open=_bool(payload.get("reset_before_market_open"), True),
            reset_after_market_close=_bool(payload.get("reset_after_market_close"), False),
            max_state_age_sec=int(payload.get("max_state_age_sec") or 300),
            fallback_to_memory_on_db_error=_bool(payload.get("fallback_to_memory_on_db_error"), True),
            conservative_on_restore_failure=_bool(payload.get("conservative_on_restore_failure"), True),
            transition_log_enabled=_bool(payload.get("transition_log_enabled"), True),
            transition_log_every_cycle=_bool(payload.get("transition_log_every_cycle"), False),
            persist_unknown_market_state=_bool(payload.get("persist_unknown_market_state"), False),
            force_reset_market_confirmation_state=_bool(payload.get("force_reset_market_confirmation_state"), False),
        )


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
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.market_index_store = market_index_store
        self.interval_sec = max(1, int(interval_sec))
        self.engine = engine or ThemeLabFlowEngine(ThemeLabConfig())
        self.persistence_config = persistence_config or MarketSideConfirmationPersistenceConfig.from_settings(
            legacy_strategy_runtime_settings().value("market_side_confirmation_persistence", {})
        )
        self.last_run_at: Optional[datetime] = None
        self.last_result: Optional[ThemeLabFlowResult] = None
        self._warnings: list[str] = []
        self._restored_session_key: tuple[str, str, int] | None = None

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
        trade_date = now.date().isoformat()
        session_id = self._session_id(now)
        self._restore_confirmation_state_once(now, trade_date=trade_date, session_id=session_id)
        result = self.engine.run_pipeline(
            theme_inputs=theme_inputs,
            snapshots=snapshots,
            kospi_return_pct=self.market_index_store.state("KOSPI").change_rate,
            kosdaq_return_pct=self.market_index_store.state("KOSDAQ").change_rate,
            calculated_at=now.isoformat(),
        )
        result = self._persist_confirmation_state(result, now, trade_date=trade_date, session_id=session_id)
        self.last_result = result
        self._save_result(result, now)
        return result

    def _session_id(self, now: datetime) -> str:
        cfg = self.persistence_config
        if str(cfg.session_scope or "").lower() == "trade_date":
            return now.date().isoformat()
        return f"{now.date().isoformat()}:{str(cfg.session_scope or 'session')}"

    def _restore_confirmation_state_once(self, now: datetime, *, trade_date: str, session_id: str) -> None:
        cfg = self.persistence_config
        key = (trade_date, session_id, int(cfg.state_version))
        if self._restored_session_key == key:
            return
        self._restored_session_key = key
        tracker = getattr(self.engine, "market_side_confirmation", None)
        if tracker is None or not cfg.enabled or str(cfg.storage).lower() != "db":
            return
        if cfg.force_reset_market_confirmation_state:
            tracker.mark_state_reset("MARKET_CONFIRMATION_STATE_RESET")
            return
        load = getattr(self.db, "load_market_side_confirmation_states", None)
        if not callable(load):
            return
        try:
            rows = load(trade_date=trade_date, session_id=session_id, state_version=cfg.state_version)
            valid_rows, age_by_side, reset_reason = self._valid_restore_rows(rows, now, trade_date=trade_date, session_id=session_id)
            if not rows:
                load_any = getattr(self.db, "load_any_market_side_confirmation_states", None)
                if callable(load_any):
                    any_rows = load_any(trade_date=trade_date, session_id=session_id)
                    _, _, any_reset_reason = self._valid_restore_rows(
                        any_rows,
                        now,
                        trade_date=trade_date,
                        session_id=session_id,
                    )
                    reset_reason = reset_reason or any_reset_reason
            if reset_reason:
                tracker.mark_state_reset(reset_reason)
                self._log_restore_reset(now, trade_date, session_id, reset_reason)
            if valid_rows:
                tracker.restore_states(valid_rows, restored_at=now.isoformat(), state_version=cfg.state_version, state_age_by_side=age_by_side)
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

    def _valid_restore_rows(
        self,
        rows: list[dict],
        now: datetime,
        *,
        trade_date: str,
        session_id: str,
    ) -> tuple[list[dict], dict[str, float | None], str]:
        cfg = self.persistence_config
        valid: list[dict] = []
        age_by_side: dict[str, float | None] = {}
        reset_reason = ""
        for row in rows or []:
            side = str(row.get("market_side") or "")
            if side not in {MarketSide.KOSPI.value, MarketSide.KOSDAQ.value} and not cfg.persist_unknown_market_state:
                continue
            if str(row.get("trade_date") or "") != trade_date:
                reset_reason = "MARKET_CONFIRMATION_STATE_DATE_MISMATCH"
                continue
            if str(row.get("session_id") or "") != session_id:
                reset_reason = "MARKET_CONFIRMATION_STATE_RESET"
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
                if cfg.max_state_age_sec > 0 and age_sec > cfg.max_state_age_sec:
                    reset_reason = "MARKET_CONFIRMATION_STATE_STALE"
                    continue
            age_by_side[side] = age_sec
            valid.append(row)
        return valid, age_by_side, reset_reason

    def _persist_confirmation_state(
        self,
        result: ThemeLabFlowResult,
        now: datetime,
        *,
        trade_date: str,
        session_id: str,
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
                "trade_date": trade_date,
                "session_id": session_id,
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
                    "market_confirmation_transition_type": state.get("transition_type") or "",
                }
                self._persist_transition_log(state, now, trade_date=trade_date, session_id=session_id)
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
                    {"error": str(exc), "side": side, "trade_date": trade_date, "session_id": session_id},
                )
        return _annotate_market_confirmation_persistence(result, persisted_by_side)

    def _persist_transition_log(self, state: dict, now: datetime, *, trade_date: str, session_id: str) -> None:
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
                    "trade_date": trade_date,
                    "session_id": session_id,
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

    def _log_restore_reset(self, now: datetime, trade_date: str, session_id: str, reset_reason: str) -> None:
        if not self.persistence_config.transition_log_enabled:
            return
        save = getattr(self.db, "save_market_side_confirmation_transition", None)
        if not callable(save):
            return
        for side in (MarketSide.KOSPI.value, MarketSide.KOSDAQ.value):
            try:
                save(
                    {
                        "trade_date": trade_date,
                        "session_id": session_id,
                        "market_side": side,
                        "cycle_id": now.isoformat(),
                        "transition_type": "STATE_EXPIRED" if reset_reason == "MARKET_CONFIRMATION_STATE_EXPIRED" else "STATE_RESET",
                        "transition_reason_codes": ["MARKET_CONFIRMATION_STATE_RESET", reset_reason],
                        "created_at": now.isoformat(),
                    }
                )
            except Exception:
                return

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
        return [
            normalize_code(item.symbol)
            for item in self.last_result.watchset
            if normalize_code(item.symbol) and int(item.condition_level or 0) >= 2
        ]

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
        ts=tick.timestamp.isoformat() if tick.timestamp else "",
        updated_at=tick.timestamp.isoformat() if tick.timestamp else "",
        metadata=metadata,
    )


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
