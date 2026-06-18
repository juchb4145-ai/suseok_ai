import asyncio
import time
from pathlib import Path

from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent, utc_timestamp
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
        return {
            "started": self.started,
            "active_candidate_count": 1,
            "subscription_active_count": 1,
            "market_session_status": "open",
            "warnings": [],
        }


class _FakeBridge:
    def __init__(self):
        self.events = []
        self.quality_snapshot = {
            "total_price_ticks": 1,
            "realtime_reliability_score": 96.0,
            "realtime_reliability_bucket": "HIGH",
        }

    def handle_event(self, event):
        self.events.append(event)
        return True

    def data_quality_snapshot(self):
        return dict(self.quality_snapshot)


class _FakeSubscriptionManager:
    def __init__(self):
        self.stale_reasons = []

    def mark_all_stale(self, reason=""):
        self.stale_reasons.append(reason)


class _FakeDb:
    def close(self):
        pass


def _settings(
    tmp_path,
    *,
    enabled=True,
    intraday_outcome_enabled=False,
    shadow_strategy_enabled=False,
    runtime_cycle_timeout_sec=5,
):
    return CoreSettings(
        db_path=Path(tmp_path) / "runtime.sqlite3",
        local_token="test-token",
        runtime_enabled=enabled,
        runtime_auto_start=False,
        runtime_evaluation_interval_sec=60,
        runtime_cycle_timeout_sec=runtime_cycle_timeout_sec,
        intraday_outcome_enabled=intraday_outcome_enabled,
        shadow_strategy_enabled=shadow_strategy_enabled,
    )


def _supervisor(tmp_path, runtime, **settings_kwargs):
    bridge = _FakeBridge()

    def builder(*args, **kwargs):
        return CoreRuntimeBundle(runtime=runtime, market_data_bridge=bridge, db=_FakeDb())

    return RuntimeSupervisor(settings=_settings(tmp_path, **settings_kwargs), gateway_state=GatewayStateStore(), runtime_builder=builder), bridge


async def _wait_for_post_cycle_diagnostics(supervisor, *, timeout_sec=2.0):
    deadline = time.perf_counter() + timeout_sec
    while time.perf_counter() < deadline:
        status = supervisor.status()
        diagnostics = status["post_cycle_diagnostics"]
        if not diagnostics["running"] and (diagnostics["run_count"] or diagnostics["failed_count"]):
            return status
        await asyncio.sleep(0.01)
    raise AssertionError("post-cycle diagnostics did not finish")


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


def test_status_exposes_realtime_data_quality_snapshot(tmp_path):
    runtime = _FakeRuntime()
    supervisor, bridge = _supervisor(tmp_path, runtime)
    bridge.quality_snapshot["reliability"] = {"bucket_counts": {"HIGH": 1}}

    async def scenario():
        await supervisor.start()
        status = supervisor.status()
        await supervisor.shutdown()
        return status

    status = asyncio.run(scenario())

    assert status["realtime_data_quality"]["realtime_reliability_bucket"] == "HIGH"
    assert status["realtime_data_quality"]["reliability"]["bucket_counts"]["HIGH"] == 1


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


def test_timed_out_cycle_does_not_queue_overlapping_worker(tmp_path):
    runtime = _FakeRuntime(sleep_cycle=1.35)
    supervisor, _ = _supervisor(tmp_path, runtime, runtime_cycle_timeout_sec=1)

    async def scenario():
        await supervisor.start()
        timed_out = await supervisor.run_once()
        skipped = await supervisor.run_once()
        while supervisor.status()["cycle_worker_pending"]:
            await asyncio.sleep(0.05)
        completed = supervisor.status()
        await supervisor.shutdown()
        return timed_out, skipped, completed

    timed_out, skipped, completed = asyncio.run(scenario())

    assert timed_out["failed_cycle_count"] == 1
    assert timed_out["cycle_worker_pending"] is True
    assert timed_out["dry_run_orders"]["reason"] == "CYCLE_WORKER_PENDING"
    assert skipped["skipped_cycle_count"] == 1
    assert runtime.cycle_calls == 1
    assert completed["cycle_worker_pending"] is False


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


def test_no_tick_watchdog_marks_realtime_subscriptions_stale(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_REALTIME_NO_TICK_REPAIR_AFTER_SEC", "0")
    monkeypatch.setenv("TRADING_REALTIME_NO_TICK_REPAIR_COOLDOWN_SEC", "1")
    runtime = _FakeRuntime()
    supervisor, bridge = _supervisor(tmp_path, runtime)
    bridge.quality_snapshot["total_price_ticks"] = 0
    supervisor.gateway_state.status.connected = True
    supervisor.gateway_state.status.kiwoom_logged_in = True
    supervisor.gateway_state.status.last_heartbeat_at = utc_timestamp()

    async def scenario():
        await supervisor.start()
        status = await supervisor.run_once()
        await supervisor.shutdown()
        return status

    status = asyncio.run(scenario())

    assert runtime.subscription_manager.stale_reasons == ["NO_PRICE_TICKS_AFTER_REGISTER"]
    assert "REALTIME_SUBSCRIPTIONS_STALE:NO_PRICE_TICKS_AFTER_REGISTER" in status["warnings"]
    assert status["latest_snapshot"]["realtime_subscription_repair"]["status"] == "STALE_MARKED"


def test_realtime_stale_warning_clears_after_ticks_recover(tmp_path):
    runtime = _FakeRuntime()
    supervisor, bridge = _supervisor(tmp_path, runtime)
    bridge.quality_snapshot["total_price_ticks"] = 3

    async def scenario():
        await supervisor.start()
        supervisor._warn("REALTIME_SUBSCRIPTIONS_STALE:NO_PRICE_TICKS_AFTER_REGISTER")
        supervisor._warn("REALTIME_NO_TICK_REPAIR_ENQUEUED")
        with supervisor._state_lock:
            supervisor.last_snapshot = {
                "market_session_status": "open",
                "warnings": ["REALTIME_SUBSCRIPTIONS_STALE:NO_PRICE_TICKS_AFTER_REGISTER", "KEEP_ME"],
                "realtime_subscription_repair": {
                    "status": "STALE_MARKED",
                    "reason": "NO_PRICE_TICKS_AFTER_REGISTER",
                    "total_price_ticks": 0,
                },
            }
        status = supervisor.status()
        await supervisor.shutdown()
        return status

    status = asyncio.run(scenario())

    assert status["warnings"] == []
    assert status["latest_snapshot"]["warnings"] == ["KEEP_ME"]
    assert status["latest_snapshot"]["realtime_subscription_repair"]["status"] == "RECOVERED"
    assert status["latest_snapshot"]["realtime_subscription_repair"]["reason"] == "PRICE_TICK_RECEIVED"
    assert status["latest_snapshot"]["realtime_subscription_repair"]["total_price_ticks"] == 3


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


def test_post_cycle_diagnostics_runs_in_background(tmp_path):
    runtime = _FakeRuntime()
    supervisor, _ = _supervisor(
        tmp_path,
        runtime,
        intraday_outcome_enabled=True,
        shadow_strategy_enabled=True,
    )

    def slow_labeler(db=None):
        time.sleep(0.4)
        return {"status": "OK", "persisted_count": 1, "outcome_count": 1}

    def shadow_evaluator(db=None):
        return {"status": "OK", "persisted_count": 2, "evaluated_count": 2}

    supervisor._label_intraday_outcomes_in_worker = slow_labeler
    supervisor._evaluate_shadow_strategies_in_worker = shadow_evaluator

    async def scenario():
        await supervisor.start()
        started = time.perf_counter()
        status = await supervisor.run_once()
        elapsed = time.perf_counter() - started
        completed = await _wait_for_post_cycle_diagnostics(supervisor)
        await supervisor.shutdown()
        return elapsed, status, completed

    elapsed, status, completed = asyncio.run(scenario())

    assert elapsed < 0.25
    assert status["cycle_count"] == 1
    assert status["post_cycle_diagnostics"]["running"] is True
    assert status["latest_snapshot"]["post_cycle_diagnostics"]["status"] == "QUEUED"
    assert completed["post_cycle_diagnostics"]["run_count"] == 1
    assert completed["latest_snapshot"]["intraday_outcome_labeler"]["outcome_count"] == 1
    assert completed["latest_snapshot"]["shadow_strategy_evaluator"]["evaluated_count"] == 2


def test_post_cycle_diagnostics_skips_overlapping_runs(tmp_path):
    runtime = _FakeRuntime()
    supervisor, _ = _supervisor(
        tmp_path,
        runtime,
        intraday_outcome_enabled=True,
        shadow_strategy_enabled=True,
    )

    def slow_labeler(db=None):
        time.sleep(0.35)
        return {"status": "OK", "persisted_count": 1, "outcome_count": 1}

    def shadow_evaluator(db=None):
        return {"status": "OK", "persisted_count": 1, "evaluated_count": 1}

    supervisor._label_intraday_outcomes_in_worker = slow_labeler
    supervisor._evaluate_shadow_strategies_in_worker = shadow_evaluator

    async def scenario():
        await supervisor.start()
        first = await supervisor.run_once()
        second = await supervisor.run_once()
        completed = await _wait_for_post_cycle_diagnostics(supervisor)
        await supervisor.shutdown()
        return first, second, completed

    first, second, completed = asyncio.run(scenario())

    assert first["post_cycle_diagnostics"]["running"] is True
    assert second["post_cycle_diagnostics"]["skipped_count"] == 1
    assert completed["post_cycle_diagnostics"]["run_count"] == 1
    assert runtime.cycle_calls == 2
