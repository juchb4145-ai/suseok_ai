import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from storage.db import TradingDatabase
from trading.theme_engine.ws.broadcaster import ThemeWebSocketBroadcaster
from trading.theme_engine.ws.server import create_app


class _FakeRuntime:
    def get_latest_rank(self, top_n: int):
        return []

    def health(self):
        return {"running": True, "data_ready": True}


class _FakeThemeClient:
    def __init__(self, *, delay_sec: float = 0.0, fail: bool = False) -> None:
        self.delay_sec = delay_sec
        self.fail = fail
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        if self.delay_sec:
            await asyncio.sleep(self.delay_sec)
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append(payload)


def test_theme_ws_bad_json_returns_error_and_keeps_connection(tmp_path, monkeypatch):
    monkeypatch.delenv("THEME_WS_API_KEY", raising=False)
    db = TradingDatabase(str(tmp_path / "theme.sqlite3"))
    try:
        client = TestClient(create_app(db, runtime=_FakeRuntime()))
        with client.websocket_connect("/ws/themes") as ws:
            assert ws.receive_json()["type"] == "heartbeat"
            ws.send_text("{bad json")
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "BAD_MESSAGE"

            ws.send_json({"action": "subscribe", "channels": ["theme_rank"], "top_n": "bad"})
            rank = ws.receive_json()
            assert rank["type"] == "theme_rank"
            assert rank["top_n"] == 20
    finally:
        db.close()


def test_theme_ws_auth_rejects_bad_token(tmp_path, monkeypatch):
    monkeypatch.setenv("THEME_WS_API_KEY", "theme-token")
    db = TradingDatabase(str(tmp_path / "theme.sqlite3"))
    try:
        client = TestClient(create_app(db))
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/themes?token=wrong"):
                pass
    finally:
        db.close()


def test_theme_ws_broadcaster_removes_slow_client() -> None:
    async def run() -> None:
        broadcaster = ThemeWebSocketBroadcaster(send_timeout_sec=0.05)
        fast = _FakeThemeClient()
        slow = _FakeThemeClient(delay_sec=0.2)
        await broadcaster.register(fast)
        await broadcaster.register(slow)

        await broadcaster.publish_async({"type": "theme_rank"})

        assert broadcaster.client_count == 1
        assert fast.sent == [{"type": "theme_rank"}]
        assert broadcaster.error_count == 1
        assert broadcaster.last_error_type == "TimeoutError"

    asyncio.run(run())
