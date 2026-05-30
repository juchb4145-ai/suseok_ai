from __future__ import annotations

from typing import Iterable, Protocol

from trading.broker.models import (
    BrokerOrderRequest,
    BrokerOrderResult,
    GatewayCommand,
    GatewayEvent,
)


class BrokerSignal(Protocol):
    def connect(self, handler) -> None: ...


class BrokerClientProtocol(Protocol):
    price_received: BrokerSignal
    order_result: BrokerSignal
    execution_received: BrokerSignal
    message_received: BrokerSignal

    def send_order(self, request: BrokerOrderRequest) -> BrokerOrderResult: ...

    def cancel_order(
        self,
        account: str,
        code: str,
        quantity: int,
        original_order_no: str,
        tag: str,
    ) -> BrokerOrderResult: ...

    def modify_buy_order(
        self,
        account: str,
        code: str,
        quantity: int,
        price: int,
        original_order_no: str,
        tag: str,
    ) -> BrokerOrderResult: ...

    def register_realtime(self, codes: Iterable[str], screen_no: str | None = None) -> None: ...

    def remove_realtime(self, codes: Iterable[str], screen_no: str | None = None) -> None: ...

    def get_code_name(self, code: str) -> str: ...


class GatewayEventSink(Protocol):
    def publish_event(self, event: GatewayEvent) -> None: ...


class GatewayCommandSource(Protocol):
    def next_commands(self, limit: int = 20) -> list[GatewayCommand]: ...
