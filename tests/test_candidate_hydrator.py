from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent
from trading.strategy.candidate_hydrator import CandidateHydrator, hydration_idempotency_key
from trading.strategy.candidate_ingestion import CandidateIngestionService, CandidateSourceEvent
from trading.strategy.market_data import MarketDataStore
from trading.strategy.models import CandidateState


def test_detected_candidate_moves_to_hydrating_and_enqueues_tr_request(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = GatewayStateStore()
    candidate = _candidate(db)

    result = CandidateHydrator(db, gateway).enqueue_candidate(candidate)

    reloaded = db.load_candidate("2026-06-17", "005930")
    assert result.enqueued is True
    assert reloaded.state == CandidateState.HYDRATING
    assert gateway.command_snapshot()["queued_count"] == 1
    command = gateway.list_commands(limit=1)[0]["command"]
    assert command["type"] == "tr_request"
    assert command["payload"]["purpose"] == "candidate_hydration"
    assert command["idempotency_key"] == hydration_idempotency_key(
        trade_date="2026-06-17",
        code="005930",
        tr_code="opt10001",
        bucket="basic",
    )


def test_hydration_idempotency_key_blocks_duplicate_requests(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = GatewayStateStore()
    candidate = _candidate(db)
    hydrator = CandidateHydrator(db, gateway)

    first = hydrator.enqueue_candidate(candidate)
    second = hydrator.enqueue_candidate(db.load_candidate("2026-06-17", "005930"))

    assert first.enqueued is True
    assert second.duplicate is True
    assert gateway.command_snapshot()["queued_count"] == 1
    requests = db.list_candidate_hydration_requests(trade_date="2026-06-17", limit=10)
    assert {request["status"] for request in requests} == {"QUEUED", "DUPLICATE"}
    assert next(request for request in requests if request["status"] == "DUPLICATE")["duplicate_of"] == first.command_id


def test_candidate_hydration_ack_merges_candidate_and_market_data(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = GatewayStateStore()
    market_data = MarketDataStore()
    candidate = _candidate(db, theme_id="semis")
    hydrator = CandidateHydrator(db, gateway, market_data=market_data)
    enqueue = hydrator.enqueue_candidate(candidate)

    hydrator.handle_event(
        GatewayEvent(
            type="command_ack",
            command_id=enqueue.command_id,
            payload={
                "purpose": "candidate_hydration",
                "command_id": enqueue.command_id,
                "trade_date": "2026-06-17",
                "code": "005930",
                "raw": {
                    "tr_rows": [
                        {
                            "code": "005930",
                            "stock_name": "Samsung",
                            "current_price": "70000",
                            "change_rate": "1.2",
                            "volume": "1000",
                            "trade_value": "70000000",
                        }
                    ]
                },
            },
        )
    )

    reloaded = db.load_candidate("2026-06-17", "005930")
    tick = market_data.latest_tick("005930")
    assert reloaded.state == CandidateState.WATCHING
    assert reloaded.metadata["candidate_hydration"]["status"] == "ACKED"
    assert reloaded.metadata["gate_usable_for_entry"] is False
    assert tick is not None
    assert tick.price == 70000
    assert tick.metadata["price_source"] == "TR_BACKFILL"


def test_candidate_hydration_ack_with_missing_data_goes_wait_data(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = GatewayStateStore()
    candidate = _candidate(db, theme_id="semis")
    hydrator = CandidateHydrator(db, gateway)
    enqueue = hydrator.enqueue_candidate(candidate)

    hydrator.handle_event(
        GatewayEvent(
            type="command_ack",
            command_id=enqueue.command_id,
            payload={
                "purpose": "candidate_hydration",
                "command_id": enqueue.command_id,
                "trade_date": "2026-06-17",
                "code": "005930",
                "raw": {"tr_rows": [{"code": "005930", "stock_name": "Samsung"}]},
            },
        )
    )

    reloaded = db.load_candidate("2026-06-17", "005930")
    assert reloaded.state == CandidateState.WAIT_DATA
    assert "WAIT_DATA" in reloaded.metadata["reason_codes"]
    assert "WAIT_DATA_PRICE_MISSING" in reloaded.metadata["reason_codes"]


def _candidate(db: TradingDatabase, *, theme_id: str = ""):
    return CandidateIngestionService(db).ingest(
        CandidateSourceEvent(
            trade_date="2026-06-17",
            code="005930",
            name="Samsung",
            source_type="opening_burst" if theme_id else "condition_search",
            source_id="opening" if theme_id else "condition",
            theme_id=theme_id,
            theme_name="Semiconductors" if theme_id else "",
            stock_role="LEADER" if theme_id else "",
            source_score=88.0 if theme_id else 20.0,
            detected_at="2026-06-17T09:01:00",
        )
    ).candidate
