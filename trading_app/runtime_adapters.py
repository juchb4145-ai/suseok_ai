from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Iterable

from trading.broker.command_queue import CommandPriority
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayCommand, GatewayEvent, Signal, new_message_id
from trading.strategy.bridge import StrategyMarketDataBridge
from trading.strategy.candles import CandleBuilder
from trading.strategy.conditions import ConditionProfileRepository, RegisteredCondition
from trading.strategy.market_data import MarketDataStore
from trading.strategy.market_index import IndexCodeMapper, MarketIndexStore


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
    ) -> None:
        self.warning_sink = warning_sink
        self._bridge = StrategyMarketDataBridge(
            market_data,
            candle_builder,
            market_index_store=market_index_store,
            index_code_mapper=IndexCodeMapper(),
            clock=clock,
        )

    def handle_event(self, event: GatewayEvent) -> bool:
        if event.type != "price_tick":
            return False
        return self.handle_price_tick(dict(event.payload or {}))

    def handle_price_tick(self, payload: dict[str, Any]) -> bool:
        code = str(payload.get("code") or "").strip()
        if not code:
            self._warn("PRICE_TICK_CODE_MISSING")
            return False
        try:
            return bool(
                self._bridge.on_realtime_tick(
                    code=code,
                    price=payload.get("price", 0),
                    change_rate=payload.get("change_rate", 0.0),
                    cum_volume=payload.get("cum_volume", payload.get("volume", 0)),
                    best_ask=payload.get("best_ask", 0),
                    best_bid=payload.get("best_bid", 0),
                    trade_value=payload.get("trade_value", 0),
                    execution_strength=payload.get("execution_strength", 0),
                    spread_ticks=payload.get("spread_ticks", 0),
                    instrument_type=payload.get("instrument_type"),
                    name=str(payload.get("name") or ""),
                    day_high=payload.get("day_high", 0),
                    day_low=payload.get("day_low", 0),
                    trade_time=str(payload.get("trade_time") or ""),
                    open_price=payload.get("open_price", 0),
                    metadata=_tick_metadata(payload),
                )
            )
        except Exception as exc:
            self._warn(f"PRICE_TICK_BRIDGE_FAILED:{code}:{exc}")
            return False

    def data_quality_snapshot(self) -> dict[str, Any]:
        return self._bridge.data_quality_snapshot()

    def _warn(self, warning: str) -> None:
        if self.warning_sink is not None:
            self.warning_sink(warning)


class GatewayCommandRealtimeClient:
    def __init__(self, gateway_state: GatewayStateStore, *, warning_sink: WarningSink | None = None) -> None:
        self.gateway_state = gateway_state
        self.warning_sink = warning_sink

    def register_realtime(self, codes: Iterable[str], screen_no: str = "") -> None:
        clean_codes = _clean_codes(codes)
        if not clean_codes:
            return
        self._enqueue(
            "register_realtime",
            payload={"codes": clean_codes, "screen_no": str(screen_no or "")},
            key=f"runtime:register_realtime:{screen_no}:{','.join(clean_codes)}",
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

    def remove_all_realtime(self) -> None:
        self._enqueue(
            "remove_all_realtime",
            payload={"scope": "runtime"},
            key=f"runtime:remove_all_realtime:{new_message_id('scope')}",
        )

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
    ) -> None:
        self.gateway_state = gateway_state
        self.repository = repository
        self.warning_sink = warning_sink
        self.require_gateway_heartbeat = require_gateway_heartbeat
        self.require_kiwoom_login = require_kiwoom_login
        self.max_realtime_conditions = max(0, int(max_realtime_conditions))
        self.condition_screen_base = int(condition_screen_base)
        self.condition_candidate_included = Signal()
        self.condition_candidate_removed = Signal()
        self.registered_conditions: dict[tuple[str, int], RegisteredCondition] = {}
        self.warnings: list[str] = []

    def start(self, now: datetime | None = None) -> list[str]:
        self.warnings = []
        if not self._gateway_ready():
            return list(self.warnings)
        self._enqueue(
            "load_conditions",
            payload={},
            key=f"runtime:load_conditions:{(now or datetime.now()).date().isoformat()}",
        )
        profiles = sorted(self.repository.enabled_profiles(), key=lambda profile: profile.priority, reverse=True)
        selected = profiles[: self.max_realtime_conditions]
        for skipped in profiles[self.max_realtime_conditions :]:
            self._warn(f"CONDITION_PROFILE_SKIPPED_LIMIT:{skipped.condition_name}")
        for index, profile in enumerate(selected):
            if profile.last_resolved_index is None:
                self._warn(f"CONDITION_INDEX_NOT_READY:{profile.condition_name}")
                continue
            condition_index = int(profile.last_resolved_index)
            screen_no = f"{self.condition_screen_base + index:04d}"
            self._enqueue(
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
            self.registered_conditions[(profile.condition_name, condition_index)] = RegisteredCondition(
                condition_name=profile.condition_name,
                condition_index=condition_index,
                screen_no=screen_no,
                strategy_profile=profile.strategy_profile,
                purpose=profile.purpose,
                registered_at=(now or datetime.now()).replace(microsecond=0).isoformat(),
            )
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

    def get_code_name(self, code: str) -> str:
        return ""

    def _gateway_ready(self) -> bool:
        snapshot = self.gateway_state.snapshot()
        if self.require_gateway_heartbeat and not snapshot.heartbeat_ok:
            self._warn("GATEWAY_HEARTBEAT_REQUIRED_FOR_CONDITIONS")
            return False
        if self.require_kiwoom_login and not snapshot.kiwoom_logged_in:
            self._warn("KIWOOM_LOGIN_REQUIRED_FOR_CONDITIONS")
            return False
        return True

    def _enqueue(self, command_type: str, *, payload: dict[str, Any], key: str) -> None:
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
            metadata={"runtime": "strategy", "adapter": "condition"},
        )
        if not result.accepted:
            self._warn(f"CONDITION_COMMAND_REJECTED:{command_type}:{result.reason}:{result.duplicate_of}")

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


def _tick_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(payload.get("metadata") or {})
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
