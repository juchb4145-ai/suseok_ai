from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from trading.broker.command_persistence import SQLiteCommandStore
from trading.broker.command_queue import CommandPriority, CommandStatus
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayCommand
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.theme_engine.backfill import (
    THEME_BACKFILL_PURPOSE,
    ThemeBackfillConfig,
    ThemeBackfillService,
    apply_dispatch_guard,
    build_backfill_candidates,
    parse_opt10001_backfill,
    parse_opt10081_backfill,
)
from trading.theme_engine.lab import ThemeBreadthEngine
from trading.theme_engine.models import StockSnapshot, ThemeMembership


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def test_theme_backfill_planner_enqueues_high_before_medium_and_limits_cycle():
    state = _healthy_state()
    result = _result(
        [
            _theme("medium", 2, [_hit("000002", ("MISSING_PREV_CLOSE",))]),
            _theme("high", 1, [_hit("000001", ("MISSING_CURRENT_PRICE", "MISSING_PREV_CLOSE")) for _ in range(4)]),
            _theme("high2", 3, [_hit("000003", ("MISSING_CURRENT_PRICE",))]),
            _theme("high3", 4, [_hit("000004", ("MISSING_CURRENT_PRICE",))]),
        ]
    )
    service = ThemeBackfillService(state, config=ThemeBackfillConfig(enabled=True, max_per_cycle=3, max_pending=5))

    summary = service.plan_and_enqueue(result, NOW)

    assert summary["enqueued_count"] == 3
    dispatched = state.dispatch_commands(limit=3, now=NOW)
    codes = [command.payload["code"] for command in dispatched]
    assert codes == ["000001", "000003", "000004"]
    records = state.list_commands(limit=10, include_finished=True)
    assert all(record["priority"] == CommandPriority.LOW.value for record in records)
    assert all(record["command"]["payload"]["purpose"] == THEME_BACKFILL_PURPOSE for record in records)
    assert summary["tr_backfill_caused_ready_count"] == 0


def test_theme_backfill_planner_respects_ready_order_low_and_duplicate_bucket():
    state = _healthy_state()
    ready = _result([_theme("high", 1, [_hit("000001", ("MISSING_CURRENT_PRICE",))])], ready=True)
    service = ThemeBackfillService(state, config=ThemeBackfillConfig(enabled=True))
    assert service.plan_and_enqueue(ready, NOW)["paused_reason"] == "READY_EXISTS"

    state = _healthy_state()
    state.enqueue_command(GatewayCommand(type="send_order", command_id="cmd-order"), priority=CommandPriority.HIGH)
    service = ThemeBackfillService(state, config=ThemeBackfillConfig(enabled=True))
    assert service.plan_and_enqueue(_result([_theme("high", 1, [_hit("000001", ("MISSING_CURRENT_PRICE",))])]), NOW)[
        "paused_reason"
    ] == "ORDER_PENDING"

    state = _healthy_state()
    service = ThemeBackfillService(state, config=ThemeBackfillConfig(enabled=True))
    low = _result([_theme("low", 1, [_hit("000010", ())])])
    assert service.plan_and_enqueue(low, NOW)["enqueued_count"] == 0

    high = _result([_theme("high", 1, [_hit("000001", ("MISSING_CURRENT_PRICE",))])])
    assert service.plan_and_enqueue(high, NOW)["enqueued_count"] == 1
    assert service.plan_and_enqueue(high, NOW)["duplicated_bucket_count"] == 1


def test_theme_backfill_candidate_scope_limits_themes_and_hits_per_theme():
    result = _result(
        [
            _theme(
                "top",
                1,
                [
                    _hit("000005", ("MISSING_PREV_CLOSE",)),
                    _hit("000001", ("MISSING_PREV_CLOSE",), leader=True, return_pct=7.0, turnover_krw=3000000),
                    _hit("000002", ("MISSING_PREV_CLOSE",), strong=True, return_pct=4.0, turnover_krw=2000000),
                    _hit("000003", ("MISSING_CURRENT_PRICE",)),
                ],
            ),
            _theme(
                "second",
                2,
                [
                    _hit("000101", ("MISSING_PREV_CLOSE",)),
                    _hit("000102", ("MISSING_PREV_CLOSE",)),
                    _hit("000103", ("MISSING_PREV_CLOSE",)),
                ],
            ),
            _theme("third", 3, [_hit("000201", ("MISSING_PREV_CLOSE",))]),
        ]
    )
    cfg = ThemeBackfillConfig(enabled=True, max_themes=2, max_hits_per_theme=2)

    candidates = build_backfill_candidates(result, cfg=cfg, now=NOW)

    assert [item.code for item in candidates] == ["000001", "000002", "000101", "000102"]
    assert all(item.primary_theme_id != "third" for item in candidates)
    assert candidates[0].hit_rank == 1
    assert candidates[1].hit_rank == 2


def test_theme_backfill_hydrates_recent_acked_cache_after_state_recreation(tmp_path):
    db_path = tmp_path / "commands.sqlite3"
    first = GatewayStateStore(command_store=SQLiteCommandStore(db_path, dedupe_retention_sec=86400, history_retention_sec=86400))
    first.status.connected = True
    first.status.kiwoom_logged_in = True
    first.status.last_heartbeat_at = NOW.isoformat()
    first.enqueue_command(
        GatewayCommand(
            type="tr_request",
            command_id="cmd-cache",
            payload={
                "purpose": THEME_BACKFILL_PURPOSE,
                "code": "000001",
                "trade_date": NOW.date().isoformat(),
                "tr_code": "opt10001",
            },
        ),
        priority=CommandPriority.LOW,
        ttl_sec=60,
        max_attempts=1,
        now=NOW,
    )
    first.dispatch_commands(limit=1, now=NOW)
    first.ack_command(
        "cmd-cache",
        status=CommandStatus.ACKED.value,
        result_payload={
            "purpose": THEME_BACKFILL_PURPOSE,
            "code": "000001",
            "trade_date": NOW.date().isoformat(),
            "parsed_backfill": {
                "code": "000001",
                "current_price": 1234,
                "prev_close": 1200,
                "stock_name": "캐시",
            },
        },
    )

    restarted = GatewayStateStore(command_store=SQLiteCommandStore(db_path, dedupe_retention_sec=86400, history_retention_sec=86400))
    market_data = MarketDataStore()
    service = ThemeBackfillService(restarted, config=ThemeBackfillConfig(enabled=True, cache_enabled=True, cache_ttl_sec=86400))

    summary = service.hydrate_cache(market_data, NOW + timedelta(minutes=1))

    tick = market_data.latest_tick("000001")
    assert summary["backfill_cache_record_count"] == 1
    assert summary["backfill_cache_applied_count"] == 1
    assert tick is not None
    assert tick.price == 1234
    assert tick.metadata["prev_close"] == 1200
    assert tick.metadata["price_source"] == "TR_BACKFILL"
    assert tick.metadata["gate_usable"] is False


def test_theme_backfill_cache_skips_other_trade_date():
    state = _healthy_state()
    _enqueue_backfill(state, "000001", now=NOW)
    state.dispatch_commands(limit=1, now=NOW)
    state.ack_command(
        "cmd-backfill-000001",
        status=CommandStatus.ACKED.value,
        result_payload={
            "purpose": THEME_BACKFILL_PURPOSE,
            "code": "000001",
            "trade_date": "1999-01-01",
            "parsed_backfill": {"code": "000001", "current_price": 1234, "prev_close": 1200},
        },
    )
    service = ThemeBackfillService(state, config=ThemeBackfillConfig(enabled=True, cache_enabled=True, cache_ttl_sec=86400))
    market_data = MarketDataStore()

    summary = service.hydrate_cache(market_data, NOW)

    assert summary["backfill_cache_record_count"] == 1
    assert summary["backfill_cache_stale_count"] == 1
    assert summary["backfill_cache_applied_count"] == 0
    assert market_data.latest_tick("000001") is None


def test_theme_backfill_cache_keeps_same_trade_date_even_when_record_clock_is_old():
    state = _healthy_state()
    old = NOW - timedelta(days=2)
    _enqueue_backfill(state, "000001", now=old)
    state.dispatch_commands(limit=1, now=old)
    state.ack_command(
        "cmd-backfill-000001",
        status=CommandStatus.ACKED.value,
        result_payload={
            "purpose": THEME_BACKFILL_PURPOSE,
            "code": "000001",
            "trade_date": NOW.date().isoformat(),
            "parsed_backfill": {"code": "000001", "current_price": 1234, "prev_close": 1200},
        },
    )
    service = ThemeBackfillService(state, config=ThemeBackfillConfig(enabled=True, cache_enabled=True, cache_ttl_sec=1))
    market_data = MarketDataStore()

    summary = service.hydrate_cache(market_data, NOW)

    assert summary["backfill_cache_applied_count"] == 1
    assert summary["backfill_cache_stale_count"] == 0
    assert market_data.latest_tick("000001").price == 1234


def test_theme_backfill_observe_pilot_blocks_non_observe_mode():
    state = _healthy_state()
    service = ThemeBackfillService(state, config=ThemeBackfillConfig(enabled=True, trading_mode="LIVE", observe_only=True))
    summary = service.plan_and_enqueue(_result([_theme("high", 1, [_hit("000001", ("MISSING_CURRENT_PRICE",))])]), NOW)
    assert summary["paused_reason"] == "NOT_OBSERVE_MODE"
    assert summary["observe_pilot_active"] is False
    assert state.list_commands(limit=10, include_finished=True) == []


def test_theme_backfill_can_pause_during_regular_session():
    state = _healthy_state()
    service = ThemeBackfillService(state, config=ThemeBackfillConfig(enabled=True, allow_regular_session=False))
    regular_at = datetime(2026, 6, 5, 9, 10, 0)

    summary = service.plan_and_enqueue(
        _result([_theme("high", 1, [_hit("000001", ("MISSING_CURRENT_PRICE",))])]),
        regular_at,
    )

    assert summary["paused_reason"] == "REGULAR_SESSION_DISABLED"
    assert summary["backfill_paused_by_regular_session_count"] == 1
    assert state.list_commands(limit=10, include_finished=True) == []


def test_theme_backfill_dispatch_guard_skips_queued_backfill_after_ready_or_non_backfill():
    state = _healthy_state()
    _enqueue_backfill(state, "000001")
    apply_dispatch_guard(state, {"watchset_snapshots": [{"gate_status": "READY"}]})
    assert state.list_commands(include_finished=True)[0]["status"] == CommandStatus.SKIPPED_READY.value

    state = _healthy_state()
    _enqueue_backfill(state, "000001")
    state.enqueue_command(GatewayCommand(type="register_realtime", command_id="cmd-real"), priority=CommandPriority.NORMAL)
    apply_dispatch_guard(state, {"watchset_snapshots": []})
    backfill = [item for item in state.list_commands(include_finished=True) if item["command_type"] == "tr_request"][0]
    assert backfill["status"] == CommandStatus.SKIPPED_NON_BACKFILL_PENDING.value


def test_theme_backfill_dispatch_guard_marks_expired_before_dispatch():
    state = _healthy_state()
    command = _enqueue_backfill(state, "000001", ttl_sec=1, now=datetime.now(timezone.utc) - timedelta(seconds=5))
    apply_dispatch_guard(state, {"watchset_snapshots": []})
    assert state.get_command(command.command_id).status == CommandStatus.EXPIRED_BEFORE_DISPATCH


def test_theme_backfill_dispatch_guard_expires_stale_dispatched_backfill():
    state = _healthy_state()
    command = _enqueue_backfill(state, "000001", ttl_sec=60, now=datetime.now(timezone.utc))
    state.dispatch_commands(limit=1)
    record = state.get_command(command.command_id)
    record.dispatched_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="seconds")

    summary = apply_dispatch_guard(
        state,
        {"watchset_snapshots": []},
        config=ThemeBackfillConfig(enabled=True, ttl_sec=1),
    )

    record = state.get_command(command.command_id)
    assert record.status == CommandStatus.EXPIRED
    assert record.last_error == "STALE_DISPATCHED_BACKFILL_CLEANUP"
    assert summary["skipped"][CommandStatus.EXPIRED.value] == 1


def test_theme_backfill_dispatch_guard_skips_non_observe_mode():
    state = _healthy_state()
    _enqueue_backfill(state, "000001")
    apply_dispatch_guard(
        state,
        {"watchset_snapshots": []},
        config=ThemeBackfillConfig(enabled=True, trading_mode="LIVE", observe_only=True),
    )
    assert state.list_commands(include_finished=True)[0]["status"] == CommandStatus.SKIPPED_NOT_OBSERVE_MODE.value


def test_theme_backfill_dispatch_guard_skips_regular_session_when_disabled(monkeypatch):
    monkeypatch.setattr("trading.theme_engine.backfill._is_regular_session", lambda now: True)
    state = _healthy_state()
    _enqueue_backfill(state, "000001")

    apply_dispatch_guard(
        state,
        {"watchset_snapshots": []},
        config=ThemeBackfillConfig(enabled=True, allow_regular_session=False),
    )

    assert state.list_commands(include_finished=True)[0]["status"] == CommandStatus.SKIPPED_REGULAR_SESSION.value


def test_theme_backfill_parsers_normalize_prices_and_prev_close():
    parsed = parse_opt10001_backfill(
        [{"종목명": "파두", "현재가": "-12,340", "등락율": "+5.70", "거래량": "1,000", "거래대금": "123,000", "기준가": "11,000"}],
        code="440110",
    )
    assert parsed["current_price"] == 12340
    assert parsed["change_rate"] == 5.7
    assert parsed["prev_close"] == 11000

    daily = parse_opt10081_backfill(
        [{"일자": "20260605", "현재가": "12,000"}, {"일자": "20260604", "현재가": "-11,500"}],
        code="440110",
        trade_date="20260605",
    )
    assert daily["prev_close"] == 11500


def test_theme_backfill_parser_aliases_master_prev_close_and_missing_fields():
    parsed = parse_opt10001_backfill(
        [{" 종목명 ": "테스트", " 현재가 ": " -12,340 ", "등락률": "+5.70%", "거래량": "1,000"}],
        code="440110",
        master_prev_close=" 11,000 ",
    )
    assert parsed["stock_name"] == "테스트"
    assert parsed["current_price"] == 12340
    assert parsed["change_rate"] == 5.7
    assert parsed["prev_close"] == 11000
    assert parsed["prev_close_source"] == "GetMasterLastPrice"
    assert parsed["master_prev_close_used"] is True
    assert parsed["parser_status"] == "OK"

    partial = parse_opt10001_backfill([{"종목명": "테스트"}], code="440110")
    assert partial["parser_status"] == "PARTIAL"
    assert "current_price" in partial["parser_missing_fields"]
    assert "prev_close" in partial["parser_missing_fields"]


def test_theme_backfill_merge_does_not_overwrite_recent_realtime_price_and_marks_tr_only_gate_unusable():
    store = MarketDataStore()
    store.update_tick(StrategyTick.from_realtime("000001", price=1000, timestamp=NOW, metadata={"stock_name": ""}))

    store.apply_theme_backfill("000001", {"current_price": 2000, "prev_close": 900, "stock_name": "테스트"}, now=NOW)
    tick = store.latest_tick("000001")
    assert tick.price == 1000
    assert tick.metadata["prev_close"] == 900
    assert tick.metadata["stock_name"] == "테스트"

    store.apply_theme_backfill("000002", {"current_price": 3000, "prev_close": 2500, "stock_name": "백필"}, now=NOW)
    tr_tick = store.latest_tick("000002")
    assert tr_tick.price == 3000
    assert tr_tick.metadata["price_source"] == "TR_BACKFILL"
    assert tr_tick.metadata["gate_usable"] is False


def test_tr_backfill_snapshot_improves_coverage_without_alive_score():
    engine = ThemeBreadthEngine()
    snapshots = {
        "000001": StockSnapshot(
            stock_code="000001",
            stock_name="백필",
            current_price=3000,
            turnover=1000000,
            metadata={"price_source": "TR_BACKFILL", "gate_usable": False},
        )
    }
    result = engine.calculate(
        [("theme", "테마", [ThemeMembership(theme_id="theme", stock_code="000001", active=True, trade_eligible=True)])],
        snapshots,
        calculated_at=NOW.isoformat(),
    )[0]
    assert result.member_hits[0].current_price == 3000
    assert result.alive_count == 0
    assert result.strong_count == 0
    assert result.leader_count == 0
    assert result.theme_turnover_krw == 0


def test_tr_backfill_prev_close_is_not_used_for_gate_return():
    engine = ThemeBreadthEngine()
    snapshots = {
        "000001": StockSnapshot(
            stock_code="000001",
            stock_name="諛깊븘",
            current_price=3000,
            change_rate=None,
            turnover=1000000,
            metadata={"prev_close": 1000, "prev_close_source": "opt10001"},
        )
    }
    result = engine.calculate(
        [("theme", "?뚮쭏", [ThemeMembership(theme_id="theme", stock_code="000001", active=True, trade_eligible=True)])],
        snapshots,
        calculated_at=NOW.isoformat(),
    )[0]
    assert result.member_hits[0].return_pct == 0.0
    assert result.alive_count == 0
    assert result.strong_count == 0
    assert result.leader_count == 0


@dataclass
class _RunnerResult:
    rows: list[dict[str, str]]
    warnings: list[str]
    errors: list[str]


class _Runner:
    def __init__(self, rows=None, errors=None):
        self.rows = rows or [{"종목명": "파두", "현재가": "12340", "기준가": "11000"}]
        self.errors = errors or []
        self.warnings = []
        self.called = False

    def request_pages(self, **kwargs):
        self.called = True
        return _RunnerResult(rows=self.rows, warnings=self.warnings, errors=self.errors)


class _Client:
    def __init__(self):
        self.comm_called = False
        self.master_last_price = ""
        self.master_last_price_called = False

    def set_input_value(self, key, value):
        return None

    def comm_rq_data(self, rq_name, tr_code, prev_next, screen_no):
        self.comm_called = True
        return 0

    def get_master_last_price(self, code):
        self.master_last_price_called = True
        return self.master_last_price


def test_gateway_capture_tr_uses_runner_and_regular_tr_keeps_comm_rq_data_path():
    from apps.kiwoom_gateway import _execute_command

    runner = _Runner()
    client = _Client()
    capture = GatewayCommand(
        type="tr_request",
        payload={
            "purpose": THEME_BACKFILL_PURPOSE,
            "response_mode": "capture",
            "code": "440110",
            "tr_code": "opt10001",
            "rq_name": "ThemeBackfill_opt10001",
            "fields": ["종목명", "현재가", "기준가"],
            "inputs": {"종목코드": "440110"},
        },
    )
    result = _execute_command(client, capture, tr_runner=runner)
    assert runner.called is True
    assert client.comm_called is False
    assert result["status"] == "ACKED"
    assert result["parsed_backfill"]["prev_close"] == 11000

    regular = GatewayCommand(type="tr_request", payload={"tr_code": "opt", "rq_name": "rq", "inputs": {"x": "y"}})
    result = _execute_command(client, regular, tr_runner=runner)
    assert client.comm_called is True
    assert result["status"] == "ACKED"


def test_gateway_capture_uses_master_last_price_only_when_opt10001_base_missing():
    from apps.kiwoom_gateway import _execute_command

    client = _Client()
    runner = _Runner(rows=[{"현재가": "12340", "기준가": "11000"}])
    capture = GatewayCommand(
        type="tr_request",
        payload={
            "purpose": THEME_BACKFILL_PURPOSE,
            "response_mode": "capture",
            "code": "440110",
            "tr_code": "opt10001",
            "rq_name": "ThemeBackfill_opt10001",
            "fields": ["현재가", "기준가"],
            "inputs": {"종목코드": "440110"},
        },
    )
    result = _execute_command(client, capture, tr_runner=runner)
    assert result["parsed_backfill"]["prev_close"] == 11000
    assert client.master_last_price_called is False

    client = _Client()
    client.master_last_price = " 10,500 "
    runner = _Runner(rows=[{"현재가": "12340"}])
    result = _execute_command(client, capture, tr_runner=runner)
    assert result["parsed_backfill"]["prev_close"] == 10500
    assert result["parsed_backfill"]["prev_close_source"] == "GetMasterLastPrice"
    assert result["parsed_backfill"]["master_prev_close_used"] is True
    assert client.master_last_price_called is True


def _healthy_state() -> GatewayStateStore:
    state = GatewayStateStore()
    state.status.connected = True
    state.status.kiwoom_logged_in = True
    state.status.last_heartbeat_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return state


def _enqueue_backfill(state: GatewayStateStore, code: str, *, ttl_sec: int = 90, now: datetime | None = None) -> GatewayCommand:
    current = now or datetime.now(timezone.utc).replace(microsecond=0)
    command = GatewayCommand(
        type="tr_request",
        command_id=f"cmd-backfill-{code}",
        idempotency_key=f"theme_backfill:2026-06-05:{code}:opt10001:1",
        payload={"purpose": THEME_BACKFILL_PURPOSE, "code": code, "tr_code": "opt10001"},
    )
    state.enqueue_command(command, priority=CommandPriority.LOW, ttl_sec=ttl_sec, max_attempts=1, now=current)
    return command


def _result(themes, *, ready: bool = False):
    watchset = [SimpleNamespace(gate_status="READY", final_gate_status="READY")] if ready else []
    return SimpleNamespace(themes=themes, watchset=watchset)


def _theme(theme_id: str, rank: int, hits):
    total = max(len(hits), 1)
    return SimpleNamespace(
        theme_id=theme_id,
        eligible_total_members=total,
        data_quality_flags=tuple(sorted({flag for hit in hits for flag in hit.data_quality_flags})),
        member_hits=tuple(hits),
    )


def _hit(symbol: str, flags, *, leader=False, strong=False, alive=False, return_pct=0.0, turnover_krw=0.0):
    return SimpleNamespace(
        symbol=symbol,
        name=f"종목{symbol}",
        excluded=False,
        current_price=0,
        return_pct=return_pct,
        turnover_krw=turnover_krw,
        alive_hit=alive or strong or leader,
        strong_hit=strong or leader,
        leader_hit=leader,
        data_quality_flags=tuple(flags),
    )
