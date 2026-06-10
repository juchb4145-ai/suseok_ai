from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket


class DashboardConnectionManager:
    def __init__(self, *, send_timeout_sec: float = 1.0) -> None:
        self._connections: set[WebSocket] = set()
        self.send_timeout_sec = max(0.05, float(send_timeout_sec or 1.0))
        self.send_error_count = 0
        self.last_send_error = ""

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def send_json(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        try:
            await asyncio.wait_for(websocket.send_text(_json_payload(payload)), timeout=self.send_timeout_sec)
        except Exception as exc:
            self.send_error_count += 1
            self.last_send_error = type(exc).__name__
            self.disconnect(websocket)
            raise

    async def broadcast_json(self, payload: dict[str, Any]) -> None:
        connections = list(self._connections)
        if not connections:
            return
        message = _json_payload(payload)
        results = await asyncio.gather(
            *(
                asyncio.wait_for(websocket.send_text(message), timeout=self.send_timeout_sec)
                for websocket in connections
            ),
            return_exceptions=True,
        )
        stale = [
            websocket
            for websocket, result in zip(connections, results)
            if isinstance(result, Exception)
        ]
        if stale:
            self.send_error_count += len(stale)
            self.last_send_error = type(next(result for result in results if isinstance(result, Exception))).__name__
        for websocket in stale:
            self.disconnect(websocket)

    @property
    def client_count(self) -> int:
        return len(self._connections)


def _json_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
