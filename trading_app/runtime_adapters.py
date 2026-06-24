from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from trading.broker.command_queue import CommandPriority, CommandStatus, EnqueueResult
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import ConditionInfo, GatewayCommand, GatewayEvent, Signal, new_message_id
from trading.strategy.candles import CandleBuilder
from trading.strategy.conditions import ConditionProfileRepository, RegisteredCondition
from trading.strategy.market_data import MarketDataStore
from trading.strategy.market_index import IndexCodeMapper, MarketIndexStore, zero_padded_index_logical_code
from trading.strategy.market_data_service import MarketDataService, MarketDataServiceConfig
from trading.theme_engine.runtime import RealTimeThemeRuntime


WarningSink = Callable[[str], None]


class GatewayEventMarketDataBridge:
    def __init__(
        self,
        market_data: MarketDataStore,
        candle_builder: CandleBuilder,
        market_index_store: MarketIndexStore,
        *,
        warning_sink: WarningSink | None = None,
        clock: Callable[[], datetime] | None = None,
        market_data_service: MarketDataService | None = None,
        service_config: MarketDataServiceConfig | None = None,
    ) -> None:
        self.warning_sink = warning_sink
        self.index_code_mapper = IndexCodeMapper()
        self.service = market_data_service or MarketDataService(
            market_data,
            candle_builder,
            market_index_store=market_index_store,
            index_code_mapper=self.index_code_mapper,
            config=service_config or MarketDataServiceConfig.from_env(),
            warning_sink=warning_sink,
            clock=clock,
        )

    def handle_event(self, event: GatewayEvent) -> bool:
        if event.type != "price_tick":
            if event.type == "command_ack":
                return self.handle_theme_backfill_ack(dict(event.payload or {}))
            return False
        try:
            return self.service.update_from_gateway_event(event)
        except Exception as exc:
            code = str(dict(event.payload or {}).get("code") or "")
            self._warn(f"PRICE_TICK_BRIDGE_FAILED:{code}:{exc}")
            return False

    def handle_theme_backfill_ack(self, payload: dict[str, Any]) -> bool:
        if str(payload.get("purpose") or "") != "theme_data_backfill":
            return False
        parsed = dict(payload.get("parsed_backfill") or dict(payload.get("raw") or {}).get("parsed_backfill") or {})
        code = str(payload.get("code") or parsed.get("code") or "").strip()
        if not code or not parsed:
            return False
        try:
            return bool(self.service.market_data.apply_theme_backfill(code, parsed))
        except Exception as exc:
            self._warn(f"THEME_BACKFILL_MERGE_FAILED:{code}:{exc}")
            return False

    def handle_price_tick(self, payload: dict[str, Any]) -> bool:
        code = str(payload.get("code") or "").strip()
        if not code:
            self._warn("PRICE_TICK_CODE_MISSING")
            return False
        try:
            return bool(self.service.handle_price_tick(payload))
        except Exception as exc:
            self._warn(f"PRICE_TICK_BRIDGE_FAILED:{code}:{exc}")
            return False

    def data_quality_snapshot(self) -> dict[str, Any]:
        return self.service.data_quality_snapshot()

    def latest_snapshot(self, code: str):
        return self.service.latest_snapshot(code)

    def pop_dirty_codes(self, limit: int = 100) -> list[str]:
        return self.service.dirty_codes(limit=limit)

    def dirty_queue_snapshot(self) -> dict[str, Any]:
        return self.service.dirty_queue.snapshot()

    def flush_batch(self) -> dict[str, Any]:
        return self.service.flush_batch()

    def _instrument_type(self, code: str, instrument_type: Any, name: Any = "") -> Any:
        explicit = str(instrument_type or "").strip().lower()
        if explicit == "index":
            return "index"
        if self.index_code_mapper.is_index_code(code):
            return "index"
        if zero_padded_index_logical_code(code) is not None:
            return "index"
        return instrument_type

    def _warn(self, warning: str) -> None:
        if self.warning_sink is not None:
            self.warning_sink(warning)


class GatewayEventThemeRuntimeBridge:
    def __init__(
        self,
        theme_runtime: RealTimeThemeRuntime,
        *,
        warning_sink: WarningSink | None = None,
        index_code_mapper: IndexCodeMapper | None = None,
    ) -> None:
        self.theme_runtime = theme_runtime
        self.warning_sink = warning_sink
        self.index_code_mapper = index_code_mapper or IndexCodeMapper()

    def handle_event(self, event: GatewayEvent) -> bool:
        if event.type != "price_tick":
            return False
        return self.handle_price_tick(dict(event.payload or {}))

    def handle_events(self, events: Iterable[GatewayEvent]) -> int:
        snapshots = []
        for event in events:
            if event.type != "price_tick":
                continue
            payload = dict(event.payload or {})
            code = str(payload.get("code") or payload.get("stock_code") or "").strip()
            if not code or self._is_index_payload(code, payload):
                continue
            instrument_type = str(payload.get("instrument_type") or "").strip().lower()
            if instrument_type == "index":
                continue
            try:
                snapshots.append(self.theme_runtime.realtime_adapter.from_kiwoom_real_data(code, payload))
            except Exception as exc:
                self._warn(f"THEME_RUNTIME_TICK_FAILED:{code}:{exc}")
        if not snapshots:
            return 0
        try:
            batch_handler = getattr(self.theme_runtime, "on_stock_snapshots", None)
            if callable(batch_handler):
                batch_handler(snapshots)
            else:
                for snapshot in snapshots:
                    self.theme_runtime.on_stock_snapshot(snapshot)
            return len(snapshots)
        except Exception as exc:
            self._warn(f"THEME_RUNTIME_BATCH_FAILED:{exc}")
            return 0

    def handle_price_tick(self, payload: dict[str, Any]) -> bool:
        code = str(payload.get("code") or payload.get("stock_code") or "").strip()
        if not code:
            self._warn("THEME_PRICE_TICK_CODE_MISSING")
            return False
        if self._is_index_payload(code, payload):
            return False
        instrument_type = str(payload.get("instrument_type") or "").strip().lower()
        if instrument_type == "index":
            return False
        try:
            snapshot = self.theme_runtime.realtime_adapter.from_kiwoom_real_data(code, payload)
            self.theme_runtime.on_stock_snapshot(snapshot)
            return True
        except Exception as exc:
            self._warn(f"THEME_RUNTIME_TICK_FAILED:{code}:{exc}")
            return False

    def _warn(self, warning: str) -> None:
        if self.warning_sink is not None:
            self.warning_sink(warning)

    def _is_index_payload(self, code: str, payload: dict[str, Any]) -> bool:
        if self.index_code_mapper.is_index_code(code):
            return True
        return zero_padded_index_logical_code(code) is not None and _index_payload_hint(
            payload.get("instrument_type"),
            payload.get("name"),
        )


class GatewayCommandRealtimeClient:
    def __init__(
        self,
        gateway_state: GatewayStateStore,
        *,
        warning_sink: WarningSink | None = None,
        stale_dispatch_timeout_sec: int = 30,
    ) -> None:
        self.gateway_state = gateway_state
        self.warning_sink = warning_sink
        self.stale_dispatch_timeout_sec = max(5, int(stale_dispatch_timeout_sec))
        self.subscription_session_id = new_message_id("rt_session")
        self.subscription_generation = 0

    def advance_subscription_generation(self, reason: str = "") -> int:
        self.subscription_generation += 1
        return self.subscription_generation

    def register_realtime(self, codes: Iterable[str], screen_no: str = "") -> None:
        clean_codes = _clean_codes(codes)
        if not clean_codes:
            return
        self._enqueue(
            "register_realtime",
            payload={
                "codes": clean_codes,
                "screen_no": str(screen_no or ""),
                "subscription_session_id": self.subscription_session_id,
                "subscription_generation": self.subscription_generation,
            },
            key=self._register_key(screen_no, clean_codes),
        )

    def register_realtime_records(self, records: Iterable[Any], screen_no: str = "") -> None:
        record_list = list(records or [])
        clean_codes = _clean_codes(getattr(record, "code", "") for record in record_list)
        if not clean_codes:
            return
        code_sources: dict[str, list[str]] = {}
        code_protected: dict[str, bool] = {}
        for record in record_list:
            codes = _clean_codes([getattr(record, "code", "")])
            if not codes:
                continue
            code = codes[0]
            sources = sorted(str(source) for source in getattr(record, "sources", set()) or [])
            code_sources[code] = sources
            code_protected[code] = bool(getattr(record, "protected", False))
        self._enqueue(
            "register_realtime",
            payload={
                "codes": clean_codes,
                "screen_no": str(screen_no or ""),
                "code_sources": code_sources,
                "code_protected": code_protected,
                "subscription_session_id": self.subscription_session_id,
                "subscription_generation": self.subscription_generation,
            },
            key=self._register_key(screen_no, clean_codes),
        )

    def remove_realtime(self, codes: Iterable[str], screen_no: str = "") -> None:
        clean_codes = _clean_codes(codes)
        if not clean_codes:
            return
        self._enqueue(
            "remove_realtime",
            payload={"codes": clean_codes, "screen_no": str(screen_no or "")},
            key=f"runtime:remove_realtime:{screen_no}:{','.join(clean_codes)}",
        )

    def remove_all_realtime(self, reason: str = "") -> None:
        clean_reason = _clean_idempotency_part(reason) or "unspecified"
        self._enqueue(
            "remove_all_realtime",
            payload={
                "scope": "runtime",
                "reason": clean_reason,
                "subscription_session_id": self.subscription_session_id,
                "subscription_generation": self.subscription_generation,
            },
            key=(
                "runtime:remove_all_realtime:"
                f"{self.subscription_session_id}:g{self.subscription_generation}:{clean_reason}"
            ),
        )

    def expire_stale_register_commands(self, now: datetime | None = None) -> int:
        current = _as_utc(now or datetime.now(timezone.utc))
        expired = 0
        for record in self.gateway_state.list_commands(limit=1000, include_finished=False, command_type="register_realtime"):
            if str(record.get("status") or "").upper() != CommandStatus.DISPATCHED.value:
                continue
            if not _stale_dispatched(record, current, self.stale_dispatch_timeout_sec):
                continue
            command_id = str(record.get("command_id") or "")
            if not command_id:
                continue
            if self.gateway_state.ack_command(
                command_id,
                status=CommandStatus.EXPIRED.value,
                result_payload={
                    "recovery_reason": "STALE_REGISTER_REALTIME_DISPATCHED",
                    "requeue": "subscription_manager_sync",
                },
                error="STALE_REGISTER_REALTIME_DISPATCHED",
            ):
                expired += 1
                self._warn(f"REALTIME_REGISTER_STALE_EXPIRED:{command_id}")
        return expired

    def _enqueue(self, command_type: str, *, payload: dict[str, Any], key: str) -> None:
        command = GatewayCommand(
            type=command_type,
            command_id=new_message_id("cmd_rt"),
            idempotency_key=key,
            source="strategy_runtime",
            payload=payload,
        )
        result = self.gateway_state.enqueue_command(
            command,
            priority=CommandPriority.NORMAL,
            metadata={"runtime": "strategy", "adapter": "realtime"},
        )
        if not result.accepted:
            self._warn(f"REALTIME_COMMAND_REJECTED:{command_type}:{result.reason}:{result.duplicate_of}")

    def _register_key(self, screen_no: str, clean_codes: list[str]) -> str:
        return (
            "runtime:register_realtime:"
            f"{self.subscription_session_id}:g{self.subscription_generation}:"
            f"{screen_no}:{','.join(clean_codes)}"
        )

    def _warn(self, warning: str) -> None:
        if self.warning_sink is not None:
            self.warning_sink(warning)


class GatewayCommandConditionAdapter:
    def __init__(
        self,
        gateway_state: GatewayStateStore,
        repository: ConditionProfileRepository,
        *,
        warning_sink: WarningSink | None = None,
        require_gateway_heartbeat: bool = True,
        require_kiwoom_login: bool = True,
        max_realtime_conditions: int = 10,
        condition_screen_base: int = 7600,
        purpose_filter: set[str] | None = None,
        send_condition_recovery_cooldown_sec: int = 60,
        send_condition_stale_dispatch_timeout_sec: int = 30,
    ) -> None:
        self.gateway_state = gateway_state
        self.repository = repository
        self.warning_sink = warning_sink
        self.require_gateway_heartbeat = require_gateway_heartbeat
        self.require_kiwoom_login = require_kiwoom_login
        self.max_realtime_conditions = max(0, int(max_realtime_conditions))
        self.condition_screen_base = int(condition_screen_base)
        self.purpose_filter = set(purpose_filter or set())
        self.send_condition_recovery_cooldown_sec = max(5, int(send_condition_recovery_cooldown_sec))
        self.send_condition_stale_dispatch_timeout_sec = max(5, int(send_condition_stale_dispatch_timeout_sec))
        self.condition_candidate_included = Signal()
        self.condition_candidate_removed = Signal()
        self.registered_conditions: dict[tuple[str, int], RegisteredCondition] = {}
        self.condition_event_counts: dict[str, dict[str, int | str]] = {}
        self.warnings: list[str] = []
        self._load_succeeded = False
        self._send_condition_recovery_at: dict[tuple[str, int, str], datetime] = {}

    def start(self, now: datetime | None = None) -> list[str]:
        self.warnings = []
        self._load_succeeded = False
        if not self._gateway_ready():
            return list(self.warnings)
        self._enqueue(
            "load_conditions",
            payload={},
            key=f"runtime:load_conditions:{(now or datetime.now()).date().isoformat()}",
        )
        profiles = sorted(self.repository.enabled_profiles(), key=lambda profile: profile.priority, reverse=True)
        if self.purpose_filter:
            profiles = [profile for profile in profiles if profile.purpose in self.purpose_filter]
        for skipped in profiles[self.max_realtime_conditions :]:
            self._warn(f"CONDITION_PROFILE_SKIPPED_LIMIT:{skipped.condition_name}")
        for profile in profiles[: self.max_realtime_conditions]:
            if profile.last_resolved_index is None:
                self._warn(f"CONDITION_INDEX_NOT_READY:{profile.condition_name}")
        return list(self.warnings)

    def stop(self) -> list[str]:
        self.warnings = []
        for condition in list(self.registered_conditions.values()):
            self._enqueue(
                "stop_condition",
                payload={
                    "screen_no": condition.screen_no,
                    "condition_name": condition.condition_name,
                    "condition_index": condition.condition_index,
                },
                key=f"runtime:stop_condition:{condition.condition_name}:{condition.condition_index}:{condition.screen_no}",
            )
        self.registered_conditions.clear()
        return list(self.warnings)

    def recover_unacked_conditions(self, now: datetime | None = None) -> list[str]:
        """Re-enqueue condition registration when the gateway never ACKed it.

        Kiwoom condition registration is stateful in the 32-bit gateway. If a
        send_condition command expires in the command queue, the strategy must
        not assume that the condition is live just because the profile index was
        resolved earlier.
        """
        self.warnings = []
        if not self._gateway_ready():
            return list(self.warnings)
        current = now or datetime.now()
        current_session = _gateway_session_tokens(self.gateway_state.snapshot().last_heartbeat_payload)
        latest = self._latest_send_condition_commands()
        for screen_index, profile in enumerate(self._selected_profiles()):
            if profile.last_resolved_index is None:
                continue
            condition_index = int(profile.last_resolved_index)
            screen_no = f"{self.condition_screen_base + screen_index:04d}"
            key = (profile.condition_name, condition_index, screen_no)
            record = latest.get(key)
            if record is None:
                continue
            status = str(record.get("status") or "").upper()
            if status == CommandStatus.DISPATCHED.value and _stale_dispatched(
                record,
                _as_utc(current),
                self.send_condition_stale_dispatch_timeout_sec,
            ):
                command_id = str(record.get("command_id") or "")
                if command_id and self.gateway_state.ack_command(
                    command_id,
                    status=CommandStatus.EXPIRED.value,
                    result_payload={
                        "recovery_reason": "STALE_SEND_CONDITION_DISPATCHED",
                        "condition_name": profile.condition_name,
                        "condition_index": condition_index,
                        "screen_no": screen_no,
                    },
                    error="STALE_SEND_CONDITION_DISPATCHED",
                ):
                    status = CommandStatus.EXPIRED.value
                    record["status"] = status
                    record["last_error"] = "STALE_SEND_CONDITION_DISPATCHED"
                    self._warn(f"CONDITION_SEND_STALE_EXPIRED:{profile.condition_name}:{command_id}")
            if status == "ACKED":
                if _record_matches_session(record, current_session):
                    self._remember_registered_condition(profile, condition_index, screen_no, current)
                    continue
                status = CommandStatus.EXPIRED.value
                command_id = str(record.get("command_id") or "")
                if command_id:
                    self.gateway_state.ack_command(
                        command_id,
                        status=CommandStatus.EXPIRED.value,
                        result_payload={
                            "recovery_reason": "STALE_SEND_CONDITION_ACK_SESSION",
                            "condition_name": profile.condition_name,
                            "condition_index": condition_index,
                            "screen_no": screen_no,
                        },
                        error="STALE_SEND_CONDITION_ACK_SESSION",
                    )
                record["status"] = status
                record["last_error"] = "STALE_SEND_CONDITION_ACK_SESSION"
                self._warn(f"CONDITION_SEND_ACK_STALE_SESSION:{profile.condition_name}:{record.get('command_id') or ''}")
            if status in {"QUEUED", "DISPATCHED"}:
                continue
            if not self._condition_recovery_due(key, current):
                continue
            recovery_key = (
                f"runtime:send_condition_recover:{profile.condition_name}:"
                f"{condition_index}:{screen_no}:{current.strftime('%Y%m%d%H%M%S')}"
            )
            result = self._enqueue(
                "send_condition",
                payload={
                    "screen_no": screen_no,
                    "condition_name": profile.condition_name,
                    "condition_index": condition_index,
                    "realtime": True,
                    "search_type": 1,
                },
                key=recovery_key,
                metadata={
                    "runtime": "strategy",
                    "adapter": "condition",
                    "condition_recovery": True,
                    "recovered_from_command_id": str(record.get("command_id") or ""),
                    "recovered_from_status": status,
                },
            )
            if result.accepted:
                self._send_condition_recovery_at[key] = current
                self._warn(f"CONDITION_SEND_RECOVERY_ENQUEUED:{profile.condition_name}:{status}")
        return list(self.warnings)

    def get_code_name(self, code: str) -> str:
        return ""

    def handle_event(self, event: GatewayEvent, now: datetime | None = None) -> bool:
        if event.type == "condition_load_result":
            self._handle_condition_load_result(dict(event.payload or {}))
            return True
        if event.type == "condition_loaded":
            self._handle_condition_loaded(dict(event.payload or {}), now=now)
            return True
        return False

    def _handle_condition_load_result(self, payload: dict[str, Any]) -> None:
        success = bool(payload.get("success"))
        self._load_succeeded = success
        if not success:
            self._warn(f"CONDITION_LOAD_FAILED:{payload.get('message') or ''}")

    def _handle_condition_loaded(self, payload: dict[str, Any], now: datetime | None = None) -> None:
        conditions = _condition_infos(payload.get("conditions") or [])
        if not conditions:
            self._warn("CONDITION_LOADED_EMPTY")
            return
        self._load_succeeded = True
        self._register_resolved_conditions(conditions, now=now)

    def _register_resolved_conditions(self, conditions: list[ConditionInfo], now: datetime | None = None) -> None:
        by_name: dict[str, list[ConditionInfo]] = {}
        for condition in conditions:
            by_name.setdefault(condition.name, []).append(condition)

        profiles = self._filtered_profiles()
        selected = profiles[: self.max_realtime_conditions]
        for skipped in profiles[self.max_realtime_conditions :]:
            self._warn(f"CONDITION_PROFILE_SKIPPED_LIMIT:{skipped.condition_name}")

        current = now or datetime.now()
        for index, profile in enumerate(selected):
            matches = by_name.get(profile.condition_name, [])
            if not matches:
                self._warn(f"CONDITION_PROFILE_UNRESOLVED:{profile.condition_name}")
                continue
            unique_indices = sorted({int(match.index) for match in matches})
            if len(unique_indices) != 1:
                self._warn(f"CONDITION_PROFILE_AMBIGUOUS:{profile.condition_name}")
                continue
            condition_index = unique_indices[0]
            self.repository.update_last_resolved_index(profile.condition_name, condition_index)
            screen_no = f"{self.condition_screen_base + index:04d}"
            result = self._enqueue(
                "send_condition",
                payload={
                    "screen_no": screen_no,
                    "condition_name": profile.condition_name,
                    "condition_index": condition_index,
                    "realtime": True,
                    "search_type": 1,
                },
                key=f"runtime:send_condition:{profile.condition_name}:{condition_index}:{screen_no}",
            )
            if result.accepted:
                self._remember_registered_condition(profile, condition_index, screen_no, current)

    def _gateway_ready(self) -> bool:
        snapshot = self.gateway_state.snapshot()
        if self.require_gateway_heartbeat and not snapshot.heartbeat_ok:
            self._warn("GATEWAY_HEARTBEAT_REQUIRED_FOR_CONDITIONS")
            return False
        if self.require_kiwoom_login and not snapshot.kiwoom_logged_in:
            self._warn("KIWOOM_LOGIN_REQUIRED_FOR_CONDITIONS")
            return False
        return True

    def _filtered_profiles(self) -> list:
        profiles = sorted(self.repository.enabled_profiles(), key=lambda profile: profile.priority, reverse=True)
        if self.purpose_filter:
            profiles = [profile for profile in profiles if profile.purpose in self.purpose_filter]
        return profiles

    def _selected_profiles(self) -> list:
        return self._filtered_profiles()[: self.max_realtime_conditions]

    def _latest_send_condition_commands(self) -> dict[tuple[str, int, str], dict[str, Any]]:
        latest: dict[tuple[str, int, str], dict[str, Any]] = {}
        current_session = _gateway_session_tokens(self.gateway_state.snapshot().last_heartbeat_payload)
        for record in self.gateway_state.list_commands(limit=1000, include_finished=True, command_type="send_condition"):
            command = dict(record.get("command") or {})
            payload = dict(command.get("payload") or {})
            name = str(payload.get("condition_name") or "").strip()
            screen_no = str(payload.get("screen_no") or "").strip()
            try:
                condition_index = int(payload.get("condition_index"))
            except (TypeError, ValueError):
                continue
            if not name or not screen_no:
                continue
            key = (name, condition_index, screen_no)
            current = latest.get(key)
            if current is None:
                latest[key] = record
                continue
            if _prefer_condition_command_record(record, current, current_session):
                latest[key] = record
        return latest

    def _condition_recovery_due(self, key: tuple[str, int, str], current: datetime) -> bool:
        previous = self._send_condition_recovery_at.get(key)
        if previous is None:
            return True
        return (current - previous).total_seconds() >= self.send_condition_recovery_cooldown_sec

    def _remember_registered_condition(self, profile, condition_index: int, screen_no: str, current: datetime) -> None:
        self.registered_conditions[(profile.condition_name, int(condition_index))] = RegisteredCondition(
            condition_name=profile.condition_name,
            condition_index=int(condition_index),
            screen_no=screen_no,
            strategy_profile=profile.strategy_profile,
            purpose=profile.purpose,
            registered_at=current.replace(microsecond=0).isoformat(),
        )

    def _enqueue(
        self,
        command_type: str,
        *,
        payload: dict[str, Any],
        key: str,
        metadata: dict[str, Any] | None = None,
    ) -> EnqueueResult:
        command = GatewayCommand(
            type=command_type,
            command_id=new_message_id("cmd_cond"),
            idempotency_key=key,
            source="strategy_runtime",
            payload=payload,
        )
        result = self.gateway_state.enqueue_command(
            command,
            priority=CommandPriority.NORMAL,
            metadata=metadata or {"runtime": "strategy", "adapter": "condition"},
        )
        if not result.accepted:
            self._warn(f"CONDITION_COMMAND_REJECTED:{command_type}:{result.reason}:{result.duplicate_of}")
        return result

    def _warn(self, warning: str) -> None:
        self.warnings.append(warning)
        if self.warning_sink is not None:
            self.warning_sink(warning)


def _clean_codes(codes: Iterable[str]) -> list[str]:
    result: list[str] = []
    for code in codes:
        text = str(code or "").strip().upper()
        if text and text not in result:
            result.append(text)
    return result


def _index_payload_hint(instrument_type: Any, name: Any = "") -> bool:
    explicit = str(instrument_type or "").strip().lower()
    if explicit == "index":
        return True
    display_name = str(name or "").strip().upper()
    return display_name in {"KOSPI", "KOSDAQ", "코스피", "코스닥"}


def _prefer_condition_command_record(
    candidate: dict[str, Any],
    current: dict[str, Any],
    current_session: set[str],
) -> bool:
    """Prefer a current-session ACK over newer duplicate recovery failures."""
    candidate_status = str(candidate.get("status") or "").upper()
    current_status = str(current.get("status") or "").upper()
    candidate_current_ack = candidate_status == CommandStatus.ACKED.value and _record_matches_session(candidate, current_session)
    current_current_ack = current_status == CommandStatus.ACKED.value and _record_matches_session(current, current_session)
    if candidate_current_ack != current_current_ack:
        return candidate_current_ack
    candidate_created = str(candidate.get("created_at") or "")
    current_created = str(current.get("created_at") or "")
    if candidate_created != current_created:
        return candidate_created > current_created
    return str(candidate.get("updated_at") or "") > str(current.get("updated_at") or "")


def _record_matches_session(record: dict[str, Any], current_session: set[str]) -> bool:
    if not current_session:
        return True
    record_session = _gateway_session_tokens(record.get("result_payload") or {})
    if not record_session:
        return False
    return not record_session.isdisjoint(current_session)


def _gateway_session_tokens(payload: Any) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    tokens: set[str] = set()
    for key in ("ws_session_id", "websocket_session_id", "ws_connection_id", "connection_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            tokens.add(value)
    trace = payload.get("transport_trace")
    if isinstance(trace, dict):
        tokens.update(_gateway_session_tokens(trace))
    return tokens


def _clean_idempotency_part(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    result = []
    for char in text:
        if char.isalnum():
            result.append(char)
        elif char in {"-", "_", ".", ":"}:
            result.append(char)
        else:
            result.append("_")
    return "".join(result).strip("_")[:120]


def _stale_dispatched(record: dict[str, Any], current: datetime, timeout_sec: int) -> bool:
    dispatched_at = _parse_record_time(record.get("dispatched_at")) or _parse_record_time(record.get("created_at"))
    if dispatched_at is None:
        return False
    return _as_utc(current) >= _as_utc(dispatched_at) + timedelta(seconds=max(1, int(timeout_sec)))


def _parse_record_time(value: Any) -> datetime | None:
    text = str(value or "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _condition_infos(raw_conditions: Iterable[Any]) -> list[ConditionInfo]:
    result: list[ConditionInfo] = []
    for raw in raw_conditions:
        if isinstance(raw, ConditionInfo):
            result.append(raw)
            continue
        item = dict(raw or {})
        name = str(item.get("name") or item.get("condition_name") or "").strip()
        if not name:
            continue
        result.append(ConditionInfo(index=int(item.get("index", item.get("condition_index", -1)) or -1), name=name))
    return result


def _tick_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(payload.get("metadata") or {})
    if payload.get("transport_trace"):
        metadata.setdefault("transport_trace", dict(payload.get("transport_trace") or {}))
    if payload.get("trace"):
        metadata.setdefault("transport_trace", dict(payload.get("trace") or {}))
    if payload.get("timestamp"):
        metadata.setdefault("broker_tick_timestamp", str(payload.get("timestamp") or ""))
    if payload.get("gateway_realtime_reliability"):
        metadata.setdefault("gateway_realtime_reliability", dict(payload.get("gateway_realtime_reliability") or {}))
    reason_codes = set(str(value) for value in metadata.get("reason_codes") or [] if str(value or "").strip())
    reason_codes.update(str(value) for value in payload.get("reason_codes") or [] if str(value or "").strip())
    if reason_codes:
        metadata["reason_codes"] = sorted(reason_codes)
    if payload.get("day_high"):
        metadata.setdefault("session_high", payload.get("day_high"))
        metadata.setdefault("day_high", payload.get("day_high"))
    if payload.get("day_low"):
        metadata.setdefault("session_low", payload.get("day_low"))
        metadata.setdefault("day_low", payload.get("day_low"))
    if payload.get("trade_time"):
        metadata.setdefault("trade_time", str(payload.get("trade_time") or ""))
    if payload.get("spread_price"):
        metadata.setdefault("spread_price", payload.get("spread_price"))
    return metadata
