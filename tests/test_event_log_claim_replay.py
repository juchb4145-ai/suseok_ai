from __future__ import annotations

from datetime import datetime, timedelta, timezone

from storage.event_log import EventLogRepository
from trading.broker.models import GatewayEvent


def test_pending_event_atomic_claim(tmp_path):
    repo = EventLogRepository(tmp_path / "events.db")
    try:
        repo.append_gateway_event(GatewayEvent(type="command_ack", event_id="evt-claim-1", command_id="cmd-1"))

        claimed = repo.claim_pending_events(limit=1, worker_id="worker-a", lease_sec=30)
        assert len(claimed) == 1
        assert claimed[0].processing_status == "PROCESSING"
        assert claimed[0].processing_attempts == 1

        second = repo.claim_pending_events(limit=1, worker_id="worker-b", lease_sec=30)
        assert second == []
    finally:
        repo.close()


def test_stale_processing_claim_recovery(tmp_path):
    repo = EventLogRepository(tmp_path / "events.db")
    try:
        now = datetime(2026, 6, 18, tzinfo=timezone.utc)
        repo.append_gateway_event(GatewayEvent(type="command_ack", event_id="evt-stale-1", command_id="cmd-1"))
        claimed = repo.claim_pending_events(limit=1, worker_id="worker-a", lease_sec=1, now=now)
        assert claimed

        recovered = repo.recover_stale_claims(now=now + timedelta(seconds=2))
        assert recovered == 1
        event = repo.get_by_event_id("evt-stale-1")
        assert event is not None
        assert event.processing_status == "PENDING"
    finally:
        repo.close()


def test_retry_wait_and_dead_letter(tmp_path):
    repo = EventLogRepository(tmp_path / "events.db")
    try:
        now = datetime(2026, 6, 18, tzinfo=timezone.utc)
        repo.append_gateway_event(GatewayEvent(type="command_failed", event_id="evt-retry-1", command_id="cmd-1"))
        claimed = repo.claim_pending_events(limit=1, worker_id="worker-a", now=now)
        assert claimed

        repo.mark_retry_wait(claimed[0].id, error="database is busy", next_retry_at=(now + timedelta(seconds=5)).isoformat())
        assert repo.claim_pending_events(limit=1, worker_id="worker-b", now=now + timedelta(seconds=1)) == []

        retry = repo.claim_pending_events(limit=1, worker_id="worker-b", now=now + timedelta(seconds=6))
        assert len(retry) == 1
        repo.mark_dead_letter(retry[0].id, error="malformed payload")
        event = repo.get_event(retry[0].id)
        assert event is not None
        assert event.processing_status == "DEAD_LETTER"
    finally:
        repo.close()


def test_price_tick_and_heartbeat_are_not_claimed_by_default(tmp_path):
    repo = EventLogRepository(tmp_path / "events.db")
    try:
        assert repo.append_gateway_event(GatewayEvent(type="price_tick", event_id="evt-price")).ignored
        assert repo.append_gateway_event(GatewayEvent(type="heartbeat", event_id="evt-heart")).ignored
        assert repo.claim_pending_events(limit=10, worker_id="worker") == []
    finally:
        repo.close()
