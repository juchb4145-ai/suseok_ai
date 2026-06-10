from __future__ import annotations

import asyncio
from typing import Any


class ThemeWebSocketBroadcaster:
    def __init__(self, *, send_timeout_sec: float = 1.0) -> None:
        self.clients: set[Any] = set()
        self.last_payloads: list[dict] = []
        self.send_timeout_sec = max(0.05, float(send_timeout_sec or 1.0))
        self.error_count = 0
        self.last_error_type = ""

    @property
    def client_count(self) -> int:
        return len(self.clients)

    async def register(self, websocket) -> None:
        self.clients.add(websocket)

    async def unregister(self, websocket) -> None:
        self.clients.discard(websocket)

    def publish(self, payload: dict) -> None:
        self.last_payloads.append(payload)
        self.last_payloads = self.last_payloads[-100:]
        if not self.clients:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.publish_async(payload))

    async def publish_async(self, payload: dict) -> None:
        clients = list(self.clients)
        if not clients:
            return
        results = await asyncio.gather(
            *(
                asyncio.wait_for(client.send_json(payload), timeout=self.send_timeout_sec)
                for client in clients
            ),
            return_exceptions=True,
        )
        dead = [client for client, result in zip(clients, results) if isinstance(result, Exception)]
        self.error_count += len(dead)
        if dead:
            self.last_error_type = type(next(result for result in results if isinstance(result, Exception))).__name__
        for client in dead:
            self.clients.discard(client)
