import json

from storage.db import TradingDatabase
from storage.event_log import EventLogConfig, EventLogRepository, dedupe_key_for_gateway_event
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent


def _repo(tmp_path, **kwargs) -> EventLogRepository:
    return EventLogRepository(
        tmp_path / "events.sqlite3",
        config=EventLogConfig(**kwargs),
    )


def test_trading_database_migration_creates_gateway_event_log(tmp_path) -> None:
    db = TradingDatabase(str(tmp_path / "schema.sqlite3"))

    row = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'gateway_event_log'"
    ).fetchone()

    assert row is not None
    db.close()


def test_append_gateway_event_persists_event(tmp_path) -> None:
    repo = _repo(tmp_path)
    event = GatewayEvent(
        type="condition_event",
        event_id="evt-condition-1",
        timestamp="2026-06-18T09:01:02+09:00",
        payload={"code": "A005930", "condition_name": "entry", "condition_index": 7, "event_type": "include"},
    )

    result = repo.append_gateway_event(event)

    assert result.appended is True
    assert result.record is not None
    assert result.record.event_id == "evt-condition-1"
    assert result.record.event_type == "condition_event"
    assert result.record.code == "005930"
    assert result.record.trade_date == "2026-06-18"
    assert json.loads(result.record.payload_json)["payload"]["code"] == "A005930"
    repo.close()


def test_append_gateway_event_dedupes_by_dedupe_key(tmp_path) -> None:
    repo = _repo(tmp_path)
    event = GatewayEvent(type="condition_event", event_id="evt-dup", payload={"code": "005930"})

    first = repo.append_gateway_event(event)
    duplicate = repo.append_gateway_event(event)

    assert first.appended is True
    assert duplicate.appended is False
    assert duplicate.duplicate is True
    assert duplicate.record is not None
    assert duplicate.record.id == first.record.id
    assert repo.event_log_snapshot()["total_count"] == 1
    repo.close()


def test_pending_gateway_events_and_status_updates(tmp_path) -> None:
    repo = _repo(tmp_path)
    one = repo.append_gateway_event(GatewayEvent(type="condition_event", event_id="evt-pending-1"))
    two = repo.append_gateway_event(GatewayEvent(type="tr_response", event_id="evt-pending-2"))

    pending = repo.pending_gateway_events(limit=10)

    assert [item.event_id for item in pending] == ["evt-pending-1", "evt-pending-2"]
    repo.mark_processed(one.record.id, processed_at="2026-06-18T00:00:01+00:00")
    repo.mark_failed(two.record.event_id, error="boom")
    snapshot = repo.event_log_snapshot()
    assert snapshot["pending_count"] == 0
    assert snapshot["processed_count"] == 1
    assert snapshot["failed_count"] == 1
    repo.close()


def test_pending_gateway_events_honors_event_type_and_limit_flag(tmp_path) -> None:
    repo = _repo(tmp_path, max_pending_replay=1)
    repo.append_gateway_event(GatewayEvent(type="condition_event", event_id="evt-limit-1"))
    repo.append_gateway_event(GatewayEvent(type="tr_response", event_id="evt-limit-2"))

    assert len(repo.pending_gateway_events(limit=100)) == 1
    assert [item.event_type for item in repo.pending_gateway_events(limit=100, event_type="tr_response")] == ["tr_response"]
    repo.close()


def test_price_tick_and_heartbeat_logging_disabled_by_default(tmp_path) -> None:
    repo = _repo(tmp_path)

    tick = repo.append_gateway_event(GatewayEvent(type="price_tick", event_id="evt-tick", payload={"code": "005930"}))
    heartbeat = repo.append_gateway_event(GatewayEvent(type="heartbeat", event_id="evt-heartbeat"))

    assert tick.ignored is True
    assert tick.reason == "PRICE_TICK_LOGGING_DISABLED"
    assert heartbeat.ignored is True
    assert heartbeat.reason == "HEARTBEAT_LOGGING_DISABLED"
    assert repo.event_log_snapshot()["total_count"] == 0
    repo.close()


def test_dedupe_key_builder_prefers_event_id_and_supports_manual_fallback() -> None:
    assert dedupe_key_for_gateway_event(GatewayEvent(type="command_ack", event_id="evt-ack")) == "event:evt-ack"
    event = GatewayEvent(type="command_ack", event_id="", command_id="cmd-1", payload={"status": "ACKED"})
    assert dedupe_key_for_gateway_event(event) == "command_ack:cmd-1:ACKED"


def test_gateway_state_store_shadow_appends_without_changing_record_event(tmp_path) -> None:
    repo = _repo(tmp_path)
    state = GatewayStateStore(event_log_store=repo)
    event = GatewayEvent(type="condition_event", event_id="evt-shadow", payload={"code": "005930"})

    accepted = state.record_event(event)

    assert accepted is True
    assert state.snapshot().received_event_count == 1
    assert repo.event_log_snapshot()["pending_count"] == 1
    repo.close()


def test_gateway_state_store_ignores_event_log_append_failure() -> None:
    class BrokenEventLog:
        def append_gateway_event(self, event):
            raise RuntimeError("db down")

    state = GatewayStateStore(event_log_store=BrokenEventLog())
    event = GatewayEvent(type="condition_event", event_id="evt-broken")

    accepted = state.record_event(event)

    assert accepted is True
    assert state.snapshot().received_event_count == 1
    assert state.snapshot().event_log_append_error_count == 1
    assert "EVENT_LOG_APPEND_FAILED" in state.event_log_warnings()[0]
