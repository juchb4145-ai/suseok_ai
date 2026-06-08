import asyncio

from trading_app.websocket import DashboardConnectionManager


class _FakeWebSocket:
    def __init__(self, *, delay_sec: float = 0.0, fail: bool = False) -> None:
        self.delay_sec = delay_sec
        self.fail = fail
        self.accepted = False
        self.sent: list[str] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, message: str) -> None:
        if self.delay_sec:
            await asyncio.sleep(self.delay_sec)
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append(message)


def test_dashboard_ws_broadcast_removes_slow_client() -> None:
    async def run() -> None:
        manager = DashboardConnectionManager(send_timeout_sec=0.05)
        fast = _FakeWebSocket()
        slow = _FakeWebSocket(delay_sec=0.2)
        await manager.connect(fast)
        await manager.connect(slow)

        await manager.broadcast_json({"ok": True})

        assert manager.client_count == 1
        assert fast.sent
        assert manager.send_error_count == 1
        assert manager.last_send_error == "TimeoutError"

    asyncio.run(run())


def test_dashboard_ws_direct_send_disconnects_failed_client() -> None:
    async def run() -> None:
        manager = DashboardConnectionManager(send_timeout_sec=0.05)
        broken = _FakeWebSocket(fail=True)
        await manager.connect(broken)

        try:
            await manager.send_json(broken, {"ok": True})
        except RuntimeError:
            pass

        assert manager.client_count == 0
        assert manager.send_error_count == 1
        assert manager.last_send_error == "RuntimeError"

    asyncio.run(run())
