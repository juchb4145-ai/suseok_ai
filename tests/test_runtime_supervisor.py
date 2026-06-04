import asyncio
import time
from pathlib import Path

from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent
from trading_app.dependencies import CoreSettings
from trading_app.runtime_factory import CoreRuntimeBundle
from trading_app.runtime_supervisor import RuntimeSupervisor


class _FakeRuntime:
    def __init__(self, *, fail_cycle=False, sleep_cycle=0.0):
        self.started = False
        self.fail_cycle = fail_cycle
        self.sleep_cycle = sleep_cycle
        self.start_calls = 0
        self.stop_calls = 0
        self.cycle_calls = 0
        self.subscription_manager = _FakeSubscriptionManager()

    def start(self):
        self.started = True
        self.start_calls += 1
        return {"started": True, "warnings": []}

    def stop(self):
        self.started = False
        self.stop_calls += 1
        return {"started": False, "warnings": []}

    def cycle(self):
        self.cycle_calls += 1
        if self.sleep_cycle:
            time.sleep(self.sleep_cycle)
        if self.fail_cycle:
            raise RuntimeError("cycle boom")
        return {"started": self.started, "active_candidate_count": 1, "warnings": []}


class _FakeBridge:
    def __init__(self):
        self.events = []

    def handle_event(self, event):
        self.events.append(event)
        return True


class _FakeSubscriptionManager:
    def __init__(self):
        self.stale_reasons = []

    def mark_all_stale(self, reason=""):
        self.stale_reasons.append(reason)


class _FakeDb:
    def close(self):
        pass


def _settings(tmp_path, *, enabled=True):
    return CoreSettings(
        db_path=Path(tmp_path) / "runtime.sqlite3",
        local_token="test-token",
        runtime_enabled=enabled,
        runtime_auto_start=False,
        runtime_evaluation_interval_sec=60,
        runtime_cycle_timeout_sec=5,
    )


def _supervisor(tmp_path, runtime):
    bridge = _FakeBridge()

    def builder(*args, **kwargs):
        return CoreRuntimeBundle(runtime=runtime, market_data_bridge=bridge, db=_FakeDb())

    return RuntimeSupervisor(settings=_settings(tmp_path), gateway_state=GatewayStateStore(), runtime_builder=builder), bridge


def test_disabled_runtime_start_is_safe(tmp_path):
    supervisor = RuntimeSupervisor(settings=_settings(tmp_path, enabled=False), gateway_state=GatewayStateStore())

    status = asyncio.run(supervisor.start())

    assert status["enabled"] is False
    assert status["running"] is False
    asyncio.run(supervisor.shutdown())


def test_start_stop_and_manual_cycle(tmp_path):
    runtime = _FakeRuntime()
    supervisor, _ = _supervisor(tmp_path, runtime)

    async def scenario():
        started = await supervisor.start()
        cycled = await supervisor.run_once()
        stopped = await supervisor.stop()
        await supervisor.shutdown()
        return started, cycled, stopped

    started, cycled, stopped = asyncio.run(scenario())

    assert started["running"] is True
    assert cycled["cycle_count"] == 1
    assert cycled["manual_cycle_count"] == 1
    assert runtime.start_calls == 1
    assert runtime.cycle_calls == 1
    assert runtime.stop_calls == 1
    assert stopped["running"] is False


def test_duplicate_cycle_is_skipped(tmp_path):
    runtime = _FakeRuntime(sleep_cycle=0.2)
    supervisor, _ = _supervisor(tmp_path, runtime)

    async def scenario():
        await supervisor.start()
        first = asyncio.create_task(supervisor.run_once())
        await asyncio.sleep(0.02)
        second = await supervisor.run_once()
        await first
        await supervisor.shutdown()
        return second

    skipped = asyncio.run(scenario())

    assert skipped["skipped_cycle_count"] == 1
    assert runtime.cycle_calls == 1


def test_cycle_exception_is_captured(tmp_path):
    runtime = _FakeRuntime(fail_cycle=True)
    supervisor, _ = _supervisor(tmp_path, runtime)

    async def scenario():
        await supervisor.start()
        status = await supervisor.run_once()
        await supervisor.shutdown()
        return status

    status = asyncio.run(scenario())

    assert status["failed_cycle_count"] == 1
    assert "cycle boom" in status["last_error"]


def test_blank_cycle_exception_uses_type_name(tmp_path):
    class BlankFailureRuntime(_FakeRuntime):
        def cycle(self):
            self.cycle_calls += 1
            raise TimeoutError()

    runtime = BlankFailureRuntime()
    supervisor, _ = _supervisor(tmp_path, runtime)

    async def scenario():
        await supervisor.start()
        status = await supervisor.run_once()
        await supervisor.shutdown()
        return status

    status = asyncio.run(scenario())

    assert status["failed_cycle_count"] == 1
    assert status["last_error"] == "TimeoutError"


def test_gateway_event_is_forwarded_to_runtime_bridge(tmp_path):
    runtime = _FakeRuntime()
    supervisor, bridge = _supervisor(tmp_path, runtime)

    async def scenario():
        await supervisor.start()
        await supervisor.handle_gateway_event(GatewayEvent(type="price_tick", payload={"code": "005930", "price": 70000}))
        queued = supervisor.status()
        await supervisor.run_once()
        await supervisor.shutdown()
        return queued

    queued = asyncio.run(scenario())

    assert queued["pending_price_tick_count"] == 1
    assert bridge.events[0].type == "price_tick"


def test_gateway_login_marks_realtime_subscriptions_stale(tmp_path):
    runtime = _FakeRuntime()
    supervisor, _ = _supervisor(tmp_path, runtime)

    async def scenario():
        await supervisor.start()
        await supervisor.handle_gateway_event(GatewayEvent(type="login_status", payload={"logged_in": True}))
        status = supervisor.status()
        await supervisor.shutdown()
        return status

    status = asyncio.run(scenario())

    assert runtime.subscription_manager.stale_reasons == ["LOGIN_STATUS_TRUE"]
    assert "REALTIME_SUBSCRIPTIONS_STALE:LOGIN_STATUS_TRUE" in status["warnings"]


def test_gateway_price_ticks_are_coalesced_until_cycle(tmp_path):
    runtime = _FakeRuntime()
    supervisor, bridge = _supervisor(tmp_path, runtime)

    async def scenario():
        await supervisor.start()
        await supervisor.handle_gateway_event(GatewayEvent(type="price_tick", payload={"code": "005930", "price": 70000}))
        await supervisor.handle_gateway_event(GatewayEvent(type="price_tick", payload={"code": "005930", "price": 70100}))
        queued = supervisor.status()
        cycled = await supervisor.run_once()
        await supervisor.shutdown()
        return queued, cycled

    queued, cycled = asyncio.run(scenario())

    assert queued["pending_price_tick_count"] == 1
    assert cycled["latest_snapshot"]["runtime_forwarded_price_tick_count"] == 1
    assert len(bridge.events) == 1
    assert bridge.events[0].payload["price"] == 70100


def test_gateway_price_tick_coalescing_keeps_trade_tick_over_later_quote_only_tick(tmp_path):
    runtime = _FakeRuntime()
    supervisor, bridge = _supervisor(tmp_path, runtime)

    async def scenario():
        await supervisor.start()
        await supervisor.handle_gateway_event(
            GatewayEvent(
                type="price_tick",
                payload={"code": "005930", "price": 70000, "change_rate": 1.2, "metadata": {"real_type": "trade"}},
            )
        )
        await supervisor.handle_gateway_event(
            GatewayEvent(
                type="price_tick",
                payload={"code": "005930", "price": 0, "best_ask": 70100, "best_bid": 70000, "metadata": {"real_type": "quote"}},
            )
        )
        queued = supervisor.status()
        cycled = await supervisor.run_once()
        await supervisor.shutdown()
        return queued, cycled

    queued, cycled = asyncio.run(scenario())

    assert queued["pending_price_tick_count"] == 1
    assert cycled["latest_snapshot"]["runtime_forwarded_price_tick_count"] == 1
    assert len(bridge.events) == 1
    assert bridge.events[0].payload["price"] == 70000
    assert bridge.events[0].payload["metadata"]["real_type"] == "trade"
