from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket


class DashboardConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def send_json(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        await websocket.send_text(_json_payload(payload))

    async def broadcast_json(self, payload: dict[str, Any]) -> None:
        connections = list(self._connections)
        if not connections:
            return
        message = _json_payload(payload)
        results = await asyncio.gather(
            *(websocket.send_text(message) for websocket in connections),
            return_exceptions=True,
        )
        stale = [
            websocket
            for websocket, result in zip(connections, results)
            if isinstance(result, Exception)
        ]
        for websocket in stale:
            self.disconnect(websocket)

    @property
    def client_count(self) -> int:
        return len(self._connections)


def _json_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
