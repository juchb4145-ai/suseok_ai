from __future__ import annotations

from typing import Protocol

from trading.broker.models import GatewayCommand, GatewayEvent


class CoreTransportClient(Protocol):
    def post_event(self, event: GatewayEvent) -> dict:
        ...

    def poll_commands(self, *, limit: int = 20, wait_sec: float = 1.0) -> list[GatewayCommand]:
        ...

    def close(self) -> None:
        ...
