from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from trading.broker.models import BrokerConditionEvent, BrokerExecutionEvent, BrokerPriceTick, GatewayEvent


@dataclass(frozen=True)
class SyntheticWorkloadConfig:
    code_count: int = 30
    ticks_per_sec: float = 20.0
    duration_sec: float = 10.0
    event_burst_size: int = 10
    duplicate_rate: float = 0.05
    out_of_order_rate: float = 0.05
    malformed_event_rate: float = 0.0
    reconnect_rate: float = 0.0
    order_event_rate: float = 0.05
    seed: int = 20260618
    account: str = "QUAL-ACC"


class SyntheticGatewayEventGenerator:
    def __init__(self, config: SyntheticWorkloadConfig | None = None) -> None:
        self.config = config or SyntheticWorkloadConfig()
        self.random = random.Random(self.config.seed)
        self.codes = [f"{index:06d}" for index in range(1, max(1, int(self.config.code_count)) + 1)]
        self.base_time = datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc)

    def generate(self) -> list[GatewayEvent]:
        events: list[GatewayEvent] = []
        events.append(self.heartbeat(logged_in=True))
        tick_count = max(1, int(self.config.duration_sec * self.config.ticks_per_sec))
        for index in range(tick_count):
            code = self.codes[index % len(self.codes)]
            timestamp = self.base_time + timedelta(milliseconds=int(index * 1000 / max(1.0, self.config.ticks_per_sec)))
            events.append(self.price_tick(code, index=index, timestamp=timestamp))
            if index % max(1, self.config.event_burst_size) == 0:
                events.append(self.condition_event(code, include=True, index=index, timestamp=timestamp))
            if self.random.random() < self.config.order_event_rate:
                events.extend(self.order_lifecycle_events(code, index=index, timestamp=timestamp))
            if self.random.random() < self.config.reconnect_rate:
                events.extend([self.heartbeat(logged_in=False), self.heartbeat(logged_in=True)])
        duplicates = [event for event in events if self.random.random() < self.config.duplicate_rate]
        events.extend(duplicates)
        if self.config.malformed_event_rate > 0:
            for index, event in enumerate(list(events)):
                if self.random.random() < self.config.malformed_event_rate:
                    events[index] = GatewayEvent(type=event.type, event_id=f"evt-malformed-{index}", payload={"malformed": True})
        if self.config.out_of_order_rate > 0:
            self._shuffle_windows(events)
        return events

    def price_tick(self, code: str, *, index: int = 0, timestamp: datetime | None = None) -> GatewayEvent:
        ts = timestamp or self.base_time
        price = 50_000 + (index % 200)
        return GatewayEvent(
            type="price_tick",
            event_id=f"evt-price-{code}-{index}",
            timestamp=ts.isoformat(),
            payload=BrokerPriceTick(
                code=code,
                price=price,
                change_rate=(index % 100) / 100.0,
                volume=1000 + index,
                trade_value=float(price * (1000 + index)),
                execution_strength=100.0 + (index % 20),
                best_ask=price + 5,
                best_bid=price - 5,
                spread_ticks=1,
                trade_time=ts.strftime("%H%M%S"),
            ).to_dict(),
        )

    def condition_event(self, code: str, *, include: bool = True, index: int = 0, timestamp: datetime | None = None) -> GatewayEvent:
        ts = timestamp or self.base_time
        return GatewayEvent(
            type="condition_event",
            event_id=f"evt-condition-{code}-{index}-{1 if include else 0}",
            timestamp=ts.isoformat(),
            payload=BrokerConditionEvent(
                condition_name="qual_synthetic_condition",
                condition_index=1,
                code=code,
                event_type="include" if include else "remove",
                purpose="reliability_qualification",
                timestamp=ts.isoformat(),
            ).to_dict(),
        )

    def tr_response(self, code: str, *, command_id: str = "", index: int = 0) -> GatewayEvent:
        return GatewayEvent(
            type="tr_response",
            event_id=f"evt-tr-{code}-{index}",
            command_id=command_id,
            payload={"code": code, "request_id": f"rq-{index}", "rows": [{"code": code, "price": 50_000 + index}]},
        )

    def heartbeat(self, *, logged_in: bool = True) -> GatewayEvent:
        state = "on" if logged_in else "off"
        return GatewayEvent(
            type="heartbeat",
            event_id=f"evt-heartbeat-{state}-{self.random.randint(1, 10**9)}",
            payload={
                "kiwoom_logged_in": logged_in,
                "orderable": False,
                "mode": "OBSERVE",
                "broker_env": "SIMULATION",
                "account_mode": "SIMULATION",
                "account": self.config.account,
            },
        )

    def order_lifecycle_events(self, code: str, *, index: int = 0, timestamp: datetime | None = None) -> list[GatewayEvent]:
        ts = timestamp or self.base_time
        command_id = f"cmd-qual-{index}"
        order_no = f"QOID-{index}"
        accepted = GatewayEvent(
            type="command_ack",
            event_id=f"evt-order-accepted-{index}",
            command_id=command_id,
            timestamp=ts.isoformat(),
            payload={
                "command_id": command_id,
                "command_type": "send_order",
                "status": "ACKED",
                "result_code": 0,
                "order_no": order_no,
                "order_result": {"order_no": order_no, "code": 0, "message": "accepted"},
                "account": self.config.account,
                "code": code,
                "side": "BUY",
                "quantity": 2,
                "price": 50_000 + index,
                "idempotency_key": f"qual-idem-{index}",
            },
        )
        partial = GatewayEvent(
            type="execution_event",
            event_id=f"evt-order-partial-{index}",
            timestamp=(ts + timedelta(milliseconds=50)).isoformat(),
            payload=BrokerExecutionEvent(
                account=self.config.account,
                code=code,
                order_no=order_no,
                side="BUY",
                quantity=2,
                price=50_000 + index,
                filled_quantity=1,
                remaining_quantity=1,
                execution_id=f"QEXEC-{index}-1",
                command_id=command_id,
                idempotency_key=f"qual-idem-{index}",
            ).to_dict(),
        )
        full = GatewayEvent(
            type="execution_event",
            event_id=f"evt-order-full-{index}",
            timestamp=(ts + timedelta(milliseconds=100)).isoformat(),
            payload=BrokerExecutionEvent(
                account=self.config.account,
                code=code,
                order_no=order_no,
                side="BUY",
                quantity=2,
                price=50_000 + index,
                filled_quantity=2,
                remaining_quantity=0,
                execution_id=f"QEXEC-{index}-2",
                command_id=command_id,
                idempotency_key=f"qual-idem-{index}",
            ).to_dict(),
        )
        return [accepted, partial, full]

    def balance_snapshot(self, code: str, *, quantity: int = 0) -> GatewayEvent:
        return GatewayEvent(
            type="balance_snapshot",
            event_id=f"evt-balance-{code}-{quantity}",
            payload={
                "account": self.config.account,
                "positions": [{"account": self.config.account, "code": code, "quantity": quantity, "available_quantity": quantity, "avg_price": 50_000}],
            },
        )

    def _shuffle_windows(self, events: list[GatewayEvent]) -> None:
        size = max(2, int(self.config.event_burst_size or 10))
        for start in range(0, len(events), size):
            if self.random.random() < self.config.out_of_order_rate:
                window = events[start : start + size]
                self.random.shuffle(window)
                events[start : start + size] = window


def event_stream_digest(events: Iterable[GatewayEvent]) -> str:
    import hashlib
    import json

    payload = [
        {
            "type": event.type,
            "event_id": event.event_id,
            "command_id": event.command_id,
            "payload": event.payload,
        }
        for event in events
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = ["SyntheticGatewayEventGenerator", "SyntheticWorkloadConfig", "event_stream_digest"]
