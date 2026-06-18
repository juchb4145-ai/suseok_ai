from datetime import datetime, timedelta, timezone

from storage.db import TradingDatabase
from trading.broker.command_persistence import SQLiteCommandStore
from tests.theme_naver_helpers import repo_with_naver_fixture
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import BrokerPriceTick, GatewayCommand, GatewayEvent, utc_timestamp
from trading.strategy.candles import CandleBuilder
from trading.strategy.conditions import ConditionProfile, ConditionProfileRepository
from trading.strategy.market_data import MarketDataStore
from trading.strategy.market_index import MarketIndexStore
from trading.strategy.models import StrategyProfile
from trading.strategy.realtime import RealTimeSubscriptionManager
from trading.theme_engine.runtime import RealTimeThemeRuntime
from trading_app.runtime_adapters import (
    GatewayCommandConditionAdapter,
    GatewayCommandRealtimeClient,
    GatewayEventMarketDataBridge,
    GatewayEventThemeRuntimeBridge,
)


def test_gateway_price_tick_updates_stock_market_data():
    market_data = MarketDataStore()
    bridge = GatewayEventMarketDataBridge(market_data, CandleBuilder(), MarketIndexStore())
    event = GatewayEvent(
        type="price_tick",
        payload=BrokerPriceTick(code="005930", price=70000, change_rate=1.2, volume=1000).to_dict(),
    )

    assert bridge.handle_event(event) is True

    tick = market_data.latest_tick("005930")
    assert tick is not None
    assert tick.price == 70000
    assert tick.cum_volume == 1000


def test_gateway_price_tick_passes_rich_payload_to_strategy_tick():
    market_data = MarketDataStore()
    bridge = GatewayEventMarketDataBridge(market_data, CandleBuilder(), MarketIndexStore())

    assert bridge.handle_price_tick(
        {
            "code": "005930",
            "price": 70000,
            "change_rate": 1.2,
            "volume": 1200,
            "cum_volume": 1200,
            "trade_value": 84_000_000,
            "execution_strength": 123.4,
            "best_ask": 70100,
            "best_bid": 70000,
            "spread_ticks": 1,
            "day_high": 71000,
            "day_low": 69000,
            "trade_time": "093015",
            "timestamp": "2026-06-01T00:00:00+00:00",
            "transport_trace": {
                "gateway_event_created_at_utc": "2026-06-01T00:00:00+00:00",
                "core_event_received_at_utc": "2026-06-01T00:00:00.250000+00:00",
            },
            "metadata": {"reason_codes": ["SPREAD_APPROXIMATED"], "raw_fids_present": [10, 14, 228]},
        }
    ) is True

    tick = market_data.latest_tick("005930")
    assert tick.trade_value == 84_000_000
    assert tick.execution_strength == 123.4
    assert tick.spread_ticks == 1
    assert tick.metadata["session_high"] == 71000
    assert tick.metadata["session_low"] == 69000
    assert tick.metadata["trade_time"] == "093015"
    assert tick.metadata["raw_fids_present"] == [10, 14, 228]
    assert tick.metadata["transport_trace"]["core_event_received_at_utc"] == "2026-06-01T00:00:00.250000+00:00"
    assert tick.metadata["realtime_transport_latency_ms"] == 250.0
    assert tick.metadata["realtime_reliability_bucket"] == "HIGH"
    assert "SPREAD_APPROXIMATED" in tick.metadata["reason_codes"]

    quality = bridge.data_quality_snapshot()
    assert quality["total_price_ticks"] == 1
    assert quality["field_coverage"]["execution_strength"] == 1.0
    assert quality["reliability"]["transport_latency_sample_count"] == 1


def test_gateway_price_tick_updates_index_store():
    market_index_store = MarketIndexStore()
    bridge = GatewayEventMarketDataBridge(MarketDataStore(), CandleBuilder(), market_index_store)

    assert bridge.handle_price_tick({"code": "001", "price": 330000, "instrument_type": "index", "name": "KOSPI"}) is True

    assert market_index_store.state("KOSPI").price == 330000


def test_gateway_price_tick_routes_known_index_even_when_payload_says_stock():
    market_data = MarketDataStore()
    market_index_store = MarketIndexStore()
    bridge = GatewayEventMarketDataBridge(market_data, CandleBuilder(), market_index_store)

    assert bridge.handle_price_tick({"code": "101", "price": 950, "instrument_type": "stock", "name": "KOSDAQ"}) is True

    assert market_index_store.state("KOSDAQ").price == 950
    assert market_data.latest_tick("101") is None


def test_gateway_price_tick_routes_zero_padded_index_alias():
    market_data = MarketDataStore()
    market_index_store = MarketIndexStore()
    bridge = GatewayEventMarketDataBridge(market_data, CandleBuilder(), market_index_store)

    assert bridge.handle_price_tick({"code": "000001", "price": 330000, "instrument_type": "stock"}) is True

    assert market_index_store.state("KOSPI").price == 330000
    assert market_data.latest_tick("000001") is None


def test_gateway_price_tick_updates_theme_runtime_from_kiwoom_tick(tmp_path):
    db, repo = repo_with_naver_fixture(tmp_path)
    try:
        runtime = RealTimeThemeRuntime(repo, scoring_interval_sec=0, db_snapshot_interval_sec=0, ws_push_interval_sec=0)
        bridge = GatewayEventThemeRuntimeBridge(runtime)

        assert bridge.handle_price_tick(
            {
                "code": "000001",
                "price": 1000,
                "change_rate": 8.0,
                "cum_volume": 1000,
                "trade_value": 1000000,
                "execution_strength": 150,
            }
        ) is True

        assert runtime.realtime_adapter.latest_snapshot("000001") is not None
        assert runtime.get_latest_rank(1)[0].leader_code == "000001"
    finally:
        db.close()


def test_theme_runtime_ignores_known_index_even_when_payload_says_stock(tmp_path):
    db, repo = repo_with_naver_fixture(tmp_path)
    try:
        runtime = RealTimeThemeRuntime(repo, scoring_interval_sec=0, db_snapshot_interval_sec=0, ws_push_interval_sec=0)
        bridge = GatewayEventThemeRuntimeBridge(runtime)

        assert bridge.handle_price_tick({"code": "001", "price": 330000, "instrument_type": "stock"}) is False

        assert runtime.realtime_adapter.latest_snapshot("001") is None
    finally:
        db.close()


def test_realtime_adapter_enqueues_gateway_commands():
    state = GatewayStateStore()
    client = GatewayCommandRealtimeClient(state)

    client.register_realtime(["005930"], screen_no="7000")
    client.remove_realtime(["005930"], screen_no="7000")

    history = state.list_commands(limit=10, include_finished=True)
    assert [item["command_type"] for item in history] == ["remove_realtime", "register_realtime"]


def test_realtime_adapter_register_command_carries_subscription_sources():
    state = GatewayStateStore()
    client = GatewayCommandRealtimeClient(state)
    manager = RealTimeSubscriptionManager(client, max_codes=10)

    manager.ensure_subscription("000001", "theme_lab_watchset")
    manager.ensure_subscription("000270", "holding", protected=True)
    manager.ensure_subscription("005930", "candidate_watch")
    manager.sync()

    command = state.list_commands(limit=10, include_finished=True, command_type="register_realtime")[0]
    payload = command["command"]["payload"]

    assert payload["code_sources"]["000001"] == ["theme_lab_watchset"]
    assert payload["code_sources"]["000270"] == ["holding"]
    assert payload["code_sources"]["005930"] == ["candidate_watch"]
    assert payload["code_protected"]["000270"] is True


def test_realtime_adapter_register_key_advances_for_repair_and_restart(tmp_path):
    db_path = tmp_path / "commands.sqlite3"
    store = SQLiteCommandStore(db_path, dedupe_retention_sec=86400, history_retention_sec=86400)
    try:
        state = GatewayStateStore(command_store=store)
        client = GatewayCommandRealtimeClient(state)

        client.register_realtime(["005930"], screen_no="7000")
        first = state.dispatch_commands(limit=1)[0]
        state.ack_command(first.command_id, status="ACKED", result_payload={"message": "registered"})
        client.register_realtime(["005930"], screen_no="7000")

        assert len(state.list_commands(limit=10, include_finished=True, command_type="register_realtime")) == 1

        client.advance_subscription_generation("NO_PRICE_TICKS_AFTER_REGISTER")
        client.register_realtime(["005930"], screen_no="7000")

        assert len(state.list_commands(limit=10, include_finished=True, command_type="register_realtime")) == 2
    finally:
        store.close()

    restarted_store = SQLiteCommandStore(db_path, dedupe_retention_sec=86400, history_retention_sec=86400)
    try:
        restarted_state = GatewayStateStore(command_store=restarted_store)
        restarted_client = GatewayCommandRealtimeClient(restarted_state)

        restarted_client.register_realtime(["005930"], screen_no="7000")

        assert len(restarted_state.list_commands(limit=10, include_finished=True, command_type="register_realtime")) == 3
    finally:
        restarted_store.close()


def test_realtime_adapter_expires_stale_dispatched_register_command():
    state = GatewayStateStore()
    state.status.connected = True
    state.status.kiwoom_logged_in = True
    state.status.last_heartbeat_at = utc_timestamp()
    client = GatewayCommandRealtimeClient(state, stale_dispatch_timeout_sec=5)
    created_at = datetime.now(timezone.utc)

    client.register_realtime(["005930"], screen_no="7000")
    command = state.dispatch_commands(now=created_at, limit=1)[0]
    expired = client.expire_stale_register_commands(now=created_at + timedelta(seconds=10))

    assert expired == 1
    record = state.get_command(command.command_id)
    assert record.status.value == "EXPIRED"
    assert record.last_error == "STALE_REGISTER_REALTIME_DISPATCHED"


def test_condition_adapter_warns_when_index_is_not_ready(tmp_path):
    db = TradingDatabase(str(tmp_path / "runtime.sqlite3"))
    try:
        repository = ConditionProfileRepository(db)
        repository.upsert_profile(
            ConditionProfile(
                condition_name="entry",
                strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
                enabled=True,
                priority=100,
                purpose="kosdaq_pullback_candidate",
                last_resolved_index=None,
            )
        )
        state = GatewayStateStore()
        state.status.connected = True
        state.status.kiwoom_logged_in = True
        state.status.last_heartbeat_at = utc_timestamp()
        adapter = GatewayCommandConditionAdapter(state, repository)

        warnings = adapter.start()

        assert any(warning.startswith("CONDITION_INDEX_NOT_READY") for warning in warnings)
        commands = state.list_commands(limit=10, include_finished=True)
        assert any(item["command_type"] == "load_conditions" for item in commands)
        assert not any(item["command_type"] == "send_condition" for item in commands)
    finally:
        db.close()


def test_condition_adapter_does_not_send_from_stale_resolved_index_before_load_event(tmp_path):
    db = TradingDatabase(str(tmp_path / "runtime.sqlite3"))
    try:
        repository = ConditionProfileRepository(db)
        repository.upsert_profile(
            ConditionProfile(
                condition_name="leader",
                strategy_profile=StrategyProfile.THEME_DISCOVERY_PROFILE,
                enabled=True,
                priority=200,
                purpose="theme_lab_leader",
                last_resolved_index=85,
            )
        )
        state = GatewayStateStore()
        state.status.connected = True
        state.status.kiwoom_logged_in = True
        state.status.last_heartbeat_at = utc_timestamp()
        adapter = GatewayCommandConditionAdapter(state, repository)

        adapter.start()

        commands = state.list_commands(limit=10, include_finished=True)
        assert [item["command_type"] for item in commands] == ["load_conditions"]
    finally:
        db.close()


def test_condition_adapter_resolves_indexes_from_gateway_condition_loaded(tmp_path):
    db = TradingDatabase(str(tmp_path / "runtime.sqlite3"))
    try:
        repository = ConditionProfileRepository(db)
        repository.upsert_profile(
            ConditionProfile(
                condition_name="테마랩_생존_-1",
                strategy_profile=StrategyProfile.THEME_DISCOVERY_PROFILE,
                enabled=True,
                priority=200,
                purpose="theme_lab_alive",
                last_resolved_index=None,
            )
        )
        state = GatewayStateStore()
        adapter = GatewayCommandConditionAdapter(
            state,
            repository,
            purpose_filter={"theme_lab_alive"},
        )

        handled = adapter.handle_event(
            GatewayEvent(
                type="condition_loaded",
                payload={"conditions": [{"index": 83, "name": "테마랩_생존_-1"}]},
            )
        )

        assert handled is True
        assert repository.enabled_profiles()[0].last_resolved_index == 83
        assert ("테마랩_생존_-1", 83) in adapter.registered_conditions
        commands = state.list_commands(limit=10, include_finished=True)
        assert any(
            item["command_type"] == "send_condition"
            and item["command"]["payload"]["condition_name"] == "테마랩_생존_-1"
            and item["command"]["payload"]["condition_index"] == 83
            for item in commands
        )
    finally:
        db.close()


def test_condition_adapter_recovers_expired_send_condition(tmp_path):
    db = TradingDatabase(str(tmp_path / "runtime.sqlite3"))
    try:
        repository = ConditionProfileRepository(db)
        repository.upsert_profile(
            ConditionProfile(
                condition_name="theme_lab_strong",
                strategy_profile=StrategyProfile.THEME_DISCOVERY_PROFILE,
                enabled=True,
                priority=200,
                purpose="theme_lab_strong",
                last_resolved_index=84,
            )
        )
        state = GatewayStateStore()
        state.status.connected = True
        state.status.kiwoom_logged_in = True
        state.status.last_heartbeat_at = utc_timestamp()
        created_at = datetime.now()
        state.enqueue_command(
            GatewayCommand(
                type="send_condition",
                command_id="cmd-expired-strong",
                idempotency_key="runtime:send_condition:theme_lab_strong:84:7600",
                source="strategy_runtime",
                payload={
                    "screen_no": "7600",
                    "condition_name": "theme_lab_strong",
                    "condition_index": 84,
                    "realtime": True,
                    "search_type": 1,
                },
            ),
            now=created_at,
            ttl_sec=1,
        )
        state.dispatch_commands(now=created_at)
        state.expire_old_commands(now=created_at + timedelta(seconds=2))
        adapter = GatewayCommandConditionAdapter(
            state,
            repository,
            purpose_filter={"theme_lab_strong"},
            send_condition_recovery_cooldown_sec=30,
        )

        warnings = adapter.recover_unacked_conditions(now=created_at + timedelta(seconds=3))

        assert "CONDITION_SEND_RECOVERY_ENQUEUED:theme_lab_strong:EXPIRED" in warnings
        commands = state.list_commands(limit=10, include_finished=True, command_type="send_condition")
        newest = commands[0]
        assert newest["status"] == "QUEUED"
        assert newest["idempotency_key"].startswith("runtime:send_condition_recover:theme_lab_strong:84:7600:")
        assert newest["command"]["payload"]["condition_name"] == "theme_lab_strong"
        assert newest["metadata"]["condition_recovery"] is True
        assert newest["metadata"]["recovered_from_command_id"] == "cmd-expired-strong"
    finally:
        db.close()


def test_condition_adapter_expires_and_recovers_stale_dispatched_send_condition(tmp_path):
    db = TradingDatabase(str(tmp_path / "runtime.sqlite3"))
    try:
        repository = ConditionProfileRepository(db)
        repository.upsert_profile(
            ConditionProfile(
                condition_name="theme_lab_strong",
                strategy_profile=StrategyProfile.THEME_DISCOVERY_PROFILE,
                enabled=True,
                priority=200,
                purpose="theme_lab_strong",
                last_resolved_index=84,
            )
        )
        state = GatewayStateStore()
        state.status.connected = True
        state.status.kiwoom_logged_in = True
        state.status.last_heartbeat_at = utc_timestamp()
        created_at = datetime.now()
        state.enqueue_command(
            GatewayCommand(
                type="send_condition",
                command_id="cmd-stale-strong",
                idempotency_key="runtime:send_condition:theme_lab_strong:84:7600",
                source="strategy_runtime",
                payload={
                    "screen_no": "7600",
                    "condition_name": "theme_lab_strong",
                    "condition_index": 84,
                    "realtime": True,
                    "search_type": 1,
                },
            ),
            now=created_at,
            ttl_sec=60,
        )
        state.dispatch_commands(now=created_at)
        adapter = GatewayCommandConditionAdapter(
            state,
            repository,
            purpose_filter={"theme_lab_strong"},
            send_condition_recovery_cooldown_sec=30,
            send_condition_stale_dispatch_timeout_sec=5,
        )

        warnings = adapter.recover_unacked_conditions(now=created_at + timedelta(seconds=10))

        assert "CONDITION_SEND_STALE_EXPIRED:theme_lab_strong:cmd-stale-strong" in warnings
        assert "CONDITION_SEND_RECOVERY_ENQUEUED:theme_lab_strong:EXPIRED" in warnings
        old = next(item for item in state.list_commands(limit=10, include_finished=True) if item["command_id"] == "cmd-stale-strong")
        assert old["status"] == "EXPIRED"
        assert old["last_error"] == "STALE_SEND_CONDITION_DISPATCHED"
        newest = state.list_commands(limit=10, include_finished=True, command_type="send_condition")[0]
        assert newest["status"] == "QUEUED"
        assert newest["metadata"]["recovered_from_command_id"] == "cmd-stale-strong"
    finally:
        db.close()


def test_condition_adapter_does_not_recover_acked_send_condition(tmp_path):
    db = TradingDatabase(str(tmp_path / "runtime.sqlite3"))
    try:
        repository = ConditionProfileRepository(db)
        repository.upsert_profile(
            ConditionProfile(
                condition_name="theme_lab_strong",
                strategy_profile=StrategyProfile.THEME_DISCOVERY_PROFILE,
                enabled=True,
                priority=200,
                purpose="theme_lab_strong",
                last_resolved_index=84,
            )
        )
        state = GatewayStateStore()
        state.status.connected = True
        state.status.kiwoom_logged_in = True
        state.status.last_heartbeat_at = utc_timestamp()
        state.enqueue_command(
            GatewayCommand(
                type="send_condition",
                command_id="cmd-acked-strong",
                idempotency_key="runtime:send_condition:theme_lab_strong:84:7600",
                source="strategy_runtime",
                payload={
                    "screen_no": "7600",
                    "condition_name": "theme_lab_strong",
                    "condition_index": 84,
                    "realtime": True,
                    "search_type": 1,
                },
            )
        )
        state.ack_command("cmd-acked-strong", status="ACKED", result_payload={"message": "condition sent"})
        adapter = GatewayCommandConditionAdapter(state, repository, purpose_filter={"theme_lab_strong"})

        warnings = adapter.recover_unacked_conditions()

        commands = state.list_commands(limit=10, include_finished=True, command_type="send_condition")
        assert warnings == []
        assert len(commands) == 1
        assert ("theme_lab_strong", 84) in adapter.registered_conditions
    finally:
        db.close()


def test_condition_adapter_prefers_current_session_ack_over_later_failed_duplicate(tmp_path):
    db = TradingDatabase(str(tmp_path / "runtime.sqlite3"))
    try:
        repository = ConditionProfileRepository(db)
        repository.upsert_profile(
            ConditionProfile(
                condition_name="theme_lab_strong",
                strategy_profile=StrategyProfile.THEME_DISCOVERY_PROFILE,
                enabled=True,
                priority=200,
                purpose="theme_lab_strong",
                last_resolved_index=84,
            )
        )
        state = GatewayStateStore()
        state.status.connected = True
        state.status.kiwoom_logged_in = True
        state.status.last_heartbeat_at = utc_timestamp()
        state.status.last_heartbeat_payload = {"ws_session_id": "current-session"}
        state.enqueue_command(
            GatewayCommand(
                type="send_condition",
                command_id="cmd-acked-current",
                idempotency_key="runtime:send_condition:theme_lab_strong:84:7600",
                source="strategy_runtime",
                payload={
                    "screen_no": "7600",
                    "condition_name": "theme_lab_strong",
                    "condition_index": 84,
                    "realtime": True,
                    "search_type": 1,
                },
            )
        )
        state.ack_command(
            "cmd-acked-current",
            status="ACKED",
            result_payload={"message": "condition sent", "transport_trace": {"ws_session_id": "current-session"}},
        )
        state.enqueue_command(
            GatewayCommand(
                type="send_condition",
                command_id="cmd-failed-duplicate",
                idempotency_key="runtime:send_condition_recover:theme_lab_strong:84:7600:20260608094500",
                source="strategy_runtime",
                payload={
                    "screen_no": "7600",
                    "condition_name": "theme_lab_strong",
                    "condition_index": 84,
                    "realtime": True,
                    "search_type": 1,
                },
            )
        )
        state.ack_command("cmd-failed-duplicate", status="FAILED", error="condition sent")
        adapter = GatewayCommandConditionAdapter(state, repository, purpose_filter={"theme_lab_strong"})

        warnings = adapter.recover_unacked_conditions()

        commands = state.list_commands(limit=10, include_finished=True, command_type="send_condition")
        assert warnings == []
        assert len(commands) == 2
        assert ("theme_lab_strong", 84) in adapter.registered_conditions
    finally:
        db.close()


def test_condition_adapter_recovers_ack_from_previous_gateway_session(tmp_path):
    db = TradingDatabase(str(tmp_path / "runtime.sqlite3"))
    try:
        repository = ConditionProfileRepository(db)
        repository.upsert_profile(
            ConditionProfile(
                condition_name="theme_lab_strong",
                strategy_profile=StrategyProfile.THEME_DISCOVERY_PROFILE,
                enabled=True,
                priority=200,
                purpose="theme_lab_strong",
                last_resolved_index=84,
            )
        )
        state = GatewayStateStore()
        state.status.connected = True
        state.status.kiwoom_logged_in = True
        state.status.last_heartbeat_at = utc_timestamp()
        state.status.last_heartbeat_payload = {"ws_session_id": "current-session"}
        state.enqueue_command(
            GatewayCommand(
                type="send_condition",
                command_id="cmd-acked-old-session",
                idempotency_key="runtime:send_condition:theme_lab_strong:84:7600",
                source="strategy_runtime",
                payload={
                    "screen_no": "7600",
                    "condition_name": "theme_lab_strong",
                    "condition_index": 84,
                    "realtime": True,
                    "search_type": 1,
                },
            )
        )
        state.ack_command(
            "cmd-acked-old-session",
            status="ACKED",
            result_payload={"message": "condition sent", "transport_trace": {"ws_session_id": "old-session"}},
        )
        adapter = GatewayCommandConditionAdapter(
            state,
            repository,
            purpose_filter={"theme_lab_strong"},
            send_condition_recovery_cooldown_sec=30,
        )

        warnings = adapter.recover_unacked_conditions()

        assert "CONDITION_SEND_ACK_STALE_SESSION:theme_lab_strong:cmd-acked-old-session" in warnings
        assert "CONDITION_SEND_RECOVERY_ENQUEUED:theme_lab_strong:EXPIRED" in warnings
        newest = state.list_commands(limit=10, include_finished=True, command_type="send_condition")[0]
        assert newest["status"] == "QUEUED"
        assert newest["metadata"]["recovered_from_command_id"] == "cmd-acked-old-session"
        assert ("theme_lab_strong", 84) not in adapter.registered_conditions
    finally:
        db.close()


def test_condition_adapter_recovers_ack_without_session_when_gateway_has_session(tmp_path):
    db = TradingDatabase(str(tmp_path / "runtime.sqlite3"))
    try:
        repository = ConditionProfileRepository(db)
        repository.upsert_profile(
            ConditionProfile(
                condition_name="theme_lab_strong",
                strategy_profile=StrategyProfile.THEME_DISCOVERY_PROFILE,
                enabled=True,
                priority=200,
                purpose="theme_lab_strong",
                last_resolved_index=84,
            )
        )
        state = GatewayStateStore()
        state.status.connected = True
        state.status.kiwoom_logged_in = True
        state.status.last_heartbeat_at = utc_timestamp()
        state.status.last_heartbeat_payload = {"ws_session_id": "current-session"}
        state.enqueue_command(
            GatewayCommand(
                type="send_condition",
                command_id="cmd-acked-no-session",
                idempotency_key="runtime:send_condition:theme_lab_strong:84:7600",
                source="strategy_runtime",
                payload={
                    "screen_no": "7600",
                    "condition_name": "theme_lab_strong",
                    "condition_index": 84,
                    "realtime": True,
                    "search_type": 1,
                },
            )
        )
        state.ack_command("cmd-acked-no-session", status="ACKED", result_payload={"message": "condition sent"})
        adapter = GatewayCommandConditionAdapter(
            state,
            repository,
            purpose_filter={"theme_lab_strong"},
            send_condition_recovery_cooldown_sec=30,
        )

        warnings = adapter.recover_unacked_conditions()

        assert "CONDITION_SEND_ACK_STALE_SESSION:theme_lab_strong:cmd-acked-no-session" in warnings
        assert "CONDITION_SEND_RECOVERY_ENQUEUED:theme_lab_strong:EXPIRED" in warnings
        old_record = next(
            item
            for item in state.list_commands(limit=10, include_finished=True, command_type="send_condition")
            if item["command_id"] == "cmd-acked-no-session"
        )
        assert old_record["status"] == "EXPIRED"
        newest = state.list_commands(limit=10, include_finished=True, command_type="send_condition")[0]
        assert newest["status"] == "QUEUED"
        assert newest["metadata"]["recovered_from_command_id"] == "cmd-acked-no-session"
        assert ("theme_lab_strong", 84) not in adapter.registered_conditions
    finally:
        db.close()
