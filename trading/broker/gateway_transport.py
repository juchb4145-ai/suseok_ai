from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trading.broker.models import GatewayCommand, GatewayEvent


@dataclass
class RestLongPollCoreClient:
    core_url: str
    token: str
    timeout_sec: float = 5.0
    _session: Any = field(default=None, init=False, repr=False)

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Local-Token": self.token}

    def post_event(self, event: GatewayEvent) -> dict:
        response = self.session.post(
            f"{self.core_url.rstrip('/')}/api/gateway/events",
            json=event.to_dict(),
            headers=self.headers,
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        return dict(response.json())

    def poll_commands(self, *, limit: int = 20, wait_sec: float = 1.0) -> list[GatewayCommand]:
        response = self.session.get(
            f"{self.core_url.rstrip('/')}/api/gateway/commands",
            params={"limit": limit, "wait_sec": wait_sec},
            headers=self.headers,
            timeout=max(self.timeout_sec, wait_sec + 2.0),
        )
        response.raise_for_status()
        return [GatewayCommand.from_dict(item) for item in response.json().get("commands", [])]

    def close(self) -> None:
        if self._session is not None:
            self._session.close()


@dataclass
class WebSocketCoreClient:
    ws_url: str
    token: str
    mock_only: bool = True

    def post_event(self, event: GatewayEvent) -> dict:
        raise RuntimeError("WebSocketCoreClient is asynchronous and mock-only; use apps/mock_websocket_gateway.py")

    def poll_commands(self, *, limit: int = 20, wait_sec: float = 1.0) -> list[GatewayCommand]:
        raise RuntimeError("WebSocketCoreClient receives pushed commands over the mock WebSocket channel")

    def close(self) -> None:
        return None
