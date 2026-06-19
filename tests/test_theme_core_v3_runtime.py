from datetime import datetime

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.strategy.candidate_ingestion import CandidateIngestionService
from trading.strategy.candidate_hydrator import CandidateHydrator
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.market_index import MarketIndexStore
from trading.strategy.realtime import RealTimeSubscriptionManager
from trading.strategy.reboot_v2 import RebootV2RuntimeProfile
from trading.strategy.reboot_v2_runtime import RebootV2Runtime
from trading.strategy.runtime import StrategyRuntimeConfig
from trading.theme_engine.core_v3_runtime import ThemeCoreV3RuntimeConfig, ThemeCoreV3RuntimePipeline
from trading.theme_engine.models import CanonicalTheme, ThemeMembership, ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository
from trading_app.runtime_adapters import GatewayCommandRealtimeClient


def test_theme_core_v3_runtime_saves_observe_theme_board_snapshot_without_candidates(tmp_path):
    db = TradingDatabase(str(tmp_path / "theme-core-v3.db"))
    repo = ThemeEngineRepository(db)
    market_data = MarketDataStore()
    _theme(repo, "ai", "AI Infra", ["000001", "000002", "000003", "000004", "000005"])
    _seed_batch(
        db,
        "2026-06-18",
        [
            ("000001", 1, 12_000_000_000, 6.4),
            ("000002", 2, 9_000_000_000, 5.8),
            ("000003", 3, 4_000_000_000, 3.6),
        ],
    )
    _tick(market_data, "000001", 6.4, 12_000_000_000, speed=1_500_000_000, execution=170, timestamp=datetime(2026, 6, 18, 9, 21, 5))
    _tick(market_data, "000002", 5.8, 9_000_000_000, speed=1_100_000_000, execution=155, timestamp=datetime(2026, 6, 18, 9, 21, 5))
    _tick(market_data, "000003", 3.6, 4_000_000_000, speed=700_000_000, execution=135, timestamp=datetime(2026, 6, 18, 9, 21, 5))

    pipeline = ThemeCoreV3RuntimePipeline(
        db=db,
        market_data=market_data,
        repository=repo,
        config=ThemeCoreV3RuntimeConfig(enabled=True, interval_sec=1),
        candidate_ingestion_service=CandidateIngestionService(db),
    )
    first = pipeline.run(datetime(2026, 6, 18, 9, 5, 0))
    second = pipeline.run(datetime(2026, 6, 18, 9, 5, 5))
    snapshot = db.latest_theme_board_snapshot(trade_date="2026-06-18")
    source_events = db.list_candidate_source_events(trade_date="2026-06-18")

    assert first["top_themes"][0]["theme_status"] == "SPREADING_THEME"
    assert second["top_themes"][0]["theme_status"] == "LEADING_THEME"
    assert second["ready_allowed"] is False
    assert second["order_intent_allowed"] is False
    assert snapshot["board_status"] == "OBSERVE"
    assert snapshot["top_themes"][0]["theme_status"] == "LEADING_THEME"
    assert {stock["code"] for stock in snapshot["stocks"] if stock["stock_role"] in {"LEADER", "CO_LEADER"}} == {"000001", "000002"}
    assert source_events
    assert {event["status"] for event in source_events} == {"OBSERVED"}
    assert db.list_candidates(trade_date="2026-06-18") == []
    assert db.conn.execute("SELECT COUNT(*) AS count FROM entry_plans").fetchone()["count"] == 0
    assert db.list_runtime_order_intents(limit=10) == []


def test_theme_core_v3_runtime_can_run_without_opening_seed_when_realtime_theme_ticks_exist(tmp_path):
    db = TradingDatabase(str(tmp_path / "theme-core-v3-no-seed.db"))
    repo = ThemeEngineRepository(db)
    market_data = MarketDataStore()
    _theme(repo, "robot", "Robot", ["000011", "000012", "000013"])
    _tick(market_data, "000011", 5.4, 8_000_000_000, speed=900_000_000, execution=150)
    _tick(market_data, "000012", 5.0, 7_000_000_000, speed=800_000_000, execution=145)
    _tick(market_data, "000013", 3.2, 3_000_000_000, speed=500_000_000, execution=125)

    pipeline = ThemeCoreV3RuntimePipeline(
        db=db,
        market_data=market_data,
        repository=repo,
        config=ThemeCoreV3RuntimeConfig(enabled=True, interval_sec=1),
    )
    result = pipeline.run(datetime(2026, 6, 18, 9, 7, 0))

    assert result["status"] == "OK"
    assert result["seed_signal_count"] == 3
    assert result["top_themes"][0]["theme_id"] == "robot"
    assert result["ready_allowed"] is False


def test_theme_core_v3_runtime_uses_intraday_discovery_rows_as_active_seed_source(tmp_path):
    db = TradingDatabase(str(tmp_path / "theme-core-v3-intraday.db"))
    repo = ThemeEngineRepository(db)
    market_data = MarketDataStore()
    _theme(repo, "ai", "AI Infra", ["000001", "000002", "000003"])
    db.save_intraday_theme_discovery_batch(
        {
            "trade_date": "2026-06-18",
            "observed_at": "2026-06-18T09:21:00",
            "session_phase": "MORNING",
            "bucket": "09:20",
            "command_id": "cmd-intraday",
            "status": "OK",
            "parser_status": "OK",
            "row_count": 3,
            "parsed_count": 3,
            "rows": [
                {"stock_code": "000001", "stock_name": "One", "rank": 1, "current_turnover_krw": 12_000_000_000, "change_rate_pct": 6.4, "observed_at": "2026-06-18T09:21:00"},
                {"stock_code": "000002", "stock_name": "Two", "rank": 2, "current_turnover_krw": 9_000_000_000, "change_rate_pct": 5.8, "observed_at": "2026-06-18T09:21:00"},
                {"stock_code": "000003", "stock_name": "Three", "rank": 3, "current_turnover_krw": 4_000_000_000, "change_rate_pct": 3.6, "observed_at": "2026-06-18T09:21:00"},
            ],
        }
    )
    _tick(market_data, "000001", 6.4, 12_000_000_000, speed=1_500_000_000, execution=170, timestamp=datetime(2026, 6, 18, 9, 21, 5))
    _tick(market_data, "000002", 5.8, 9_000_000_000, speed=1_100_000_000, execution=155, timestamp=datetime(2026, 6, 18, 9, 21, 5))
    _tick(market_data, "000003", 3.6, 4_000_000_000, speed=700_000_000, execution=135, timestamp=datetime(2026, 6, 18, 9, 21, 5))

    pipeline = ThemeCoreV3RuntimePipeline(
        db=db,
        market_data=market_data,
        repository=repo,
        config=ThemeCoreV3RuntimeConfig(enabled=True, interval_sec=1),
    )
    result = pipeline.run(datetime(2026, 6, 18, 9, 21, 5))

    active = db.list_active_seed_signals(trade_date="2026-06-18")
    observations = db.list_turnover_flow_observations(trade_date="2026-06-18")

    assert result["status"] == "OK"
    assert result["source_delta_signal_count"] == 6
    assert result["active_seed_registry"]["source_counts"] == {"MANUAL": 3, "INTRADAY": 3}
    assert result["top_themes"][0]["theme_id"] == "ai"
    assert {row["source_type"] for row in active} == {"INTRADAY", "MANUAL"}
    assert {row["code"] for row in observations} == {"000001", "000002", "000003"}
    assert db.conn.execute("SELECT COUNT(*) AS count FROM entry_plans").fetchone()["count"] == 0
    assert db.list_runtime_order_intents(limit=10) == []


def test_theme_core_v3_runtime_does_not_reactivate_expired_seed_from_same_raw_row(tmp_path):
    db = TradingDatabase(str(tmp_path / "theme-core-v3-expired-seed.db"))
    repo = ThemeEngineRepository(db)
    market_data = MarketDataStore()
    _theme(repo, "ai", "AI Infra", ["000001", "000002", "000003"])
    _seed_batch(
        db,
        "2026-06-18",
        [
            ("000001", 1, 12_000_000_000, 6.4),
            ("000002", 2, 9_000_000_000, 5.8),
            ("000003", 3, 4_000_000_000, 3.6),
        ],
    )
    _tick(market_data, "000001", 6.4, 12_000_000_000, speed=1_500_000_000, execution=170, timestamp=datetime(2026, 6, 18, 9, 3, 30))
    _tick(market_data, "000002", 5.8, 9_000_000_000, speed=1_100_000_000, execution=155, timestamp=datetime(2026, 6, 18, 9, 3, 30))
    _tick(market_data, "000003", 3.6, 4_000_000_000, speed=700_000_000, execution=135, timestamp=datetime(2026, 6, 18, 9, 3, 30))

    pipeline = ThemeCoreV3RuntimePipeline(
        db=db,
        market_data=market_data,
        repository=repo,
        config=ThemeCoreV3RuntimeConfig(enabled=True, interval_sec=1, signal_ttl_sec=60),
    )
    first = pipeline.run(datetime(2026, 6, 18, 9, 3, 30))
    expired = pipeline.run(datetime(2026, 6, 18, 9, 4, 5))

    assert first["active_seed_count"] == 6
    assert expired["status"] == "OK"
    assert expired["active_seed_count"] == 3
    assert expired["expired_seed_count"] == 3
    assert expired["active_seed_registry"]["source_counts"] == {"MANUAL": 3}
    assert {row["source_type"] for row in db.list_active_seed_signals(trade_date="2026-06-18")} == {"MANUAL"}


def test_theme_core_v3_runtime_requires_runtime_market_context_when_enabled(tmp_path):
    db = TradingDatabase(str(tmp_path / "theme-core-v3-market-wait.db"))
    repo = ThemeEngineRepository(db)
    market_data = MarketDataStore()
    _theme(repo, "ai", "AI Infra", ["000001", "000002", "000003"])
    _tick(market_data, "000001", 5.5, 8_000_000_000, speed=900_000_000, execution=150)
    _tick(market_data, "000002", 5.1, 7_000_000_000, speed=800_000_000, execution=145)
    _tick(market_data, "000003", 3.4, 3_000_000_000, speed=500_000_000, execution=125)

    pipeline = ThemeCoreV3RuntimePipeline(
        db=db,
        market_data=market_data,
        repository=repo,
        config=ThemeCoreV3RuntimeConfig(enabled=True, interval_sec=1, use_runtime_market_context=True),
    )
    result = pipeline.run(datetime(2026, 6, 18, 9, 7, 0))

    assert result["status"] == "DATA_WAIT"
    assert result["market_context_status"] == "DATA_WAIT"
    assert "MARKET_CONTEXT_NOT_READY" in result["reason_codes"]


def test_theme_core_v3_candidate_bridge_ingests_confirmed_roles_only_when_enabled(tmp_path):
    db = TradingDatabase(str(tmp_path / "theme-core-v3-ingest.db"))
    repo = ThemeEngineRepository(db)
    market_data = MarketDataStore()
    _theme(repo, "ai", "AI Infra", ["000001", "000002", "000003"])
    _tick(market_data, "000001", 5.5, 8_000_000_000, speed=900_000_000, execution=150)
    _tick(market_data, "000002", 5.1, 7_000_000_000, speed=800_000_000, execution=145)
    _tick(market_data, "000003", 3.4, 3_000_000_000, speed=500_000_000, execution=125)

    pipeline = ThemeCoreV3RuntimePipeline(
        db=db,
        market_data=market_data,
        repository=repo,
        config=ThemeCoreV3RuntimeConfig(enabled=True, interval_sec=1, ingest_candidate_source_events=True),
        candidate_ingestion_service=CandidateIngestionService(db),
    )
    pipeline.run(datetime(2026, 6, 18, 9, 5, 0))
    result = pipeline.run(datetime(2026, 6, 18, 9, 5, 5))

    candidates = db.list_candidates(trade_date="2026-06-18")
    source_events = db.list_candidate_source_events(trade_date="2026-06-18")

    assert result["candidate_ingestion_enabled"] is True
    assert candidates
    assert {event["status"] for event in source_events} == {"INGESTED"}
    assert db.conn.execute("SELECT COUNT(*) AS count FROM entry_plans").fetchone()["count"] == 0
    assert db.list_runtime_order_intents(limit=10) == []


def test_reboot_v2_cycle_runs_theme_core_v3_pipeline_without_order_path(tmp_path):
    db = TradingDatabase(str(tmp_path / "theme-core-v3-reboot.db"))
    gateway = GatewayStateStore()
    repo = ThemeEngineRepository(db)
    market_data = MarketDataStore()
    candle_builder = CandleBuilder()
    market_index_store = MarketIndexStore()
    _theme(repo, "ai", "AI Infra", ["000001", "000002", "000003"])
    _tick(market_data, "000001", 5.5, 8_000_000_000, speed=900_000_000, execution=150)
    _tick(market_data, "000002", 5.1, 7_000_000_000, speed=800_000_000, execution=145)
    _tick(market_data, "000003", 3.4, 3_000_000_000, speed=500_000_000, execution=125)
    candidate_ingestion = CandidateIngestionService(db)
    theme_core = ThemeCoreV3RuntimePipeline(
        db=db,
        market_data=market_data,
        repository=repo,
        config=ThemeCoreV3RuntimeConfig(enabled=True, interval_sec=1),
        candidate_ingestion_service=candidate_ingestion,
    )
    runtime = RebootV2Runtime(
        db=db,
        subscription_manager=RealTimeSubscriptionManager(GatewayCommandRealtimeClient(gateway), max_codes=20),
        candle_builder=candle_builder,
        market_data=market_data,
        market_index_store=market_index_store,
        config=StrategyRuntimeConfig(max_candidates_to_watch=20, realtime_subscription_limit=20),
        profile=RebootV2RuntimeProfile.V2_OBSERVE,
        candidate_ingestion_service=candidate_ingestion,
        candidate_hydrator=CandidateHydrator(db, gateway, market_data=market_data),
        theme_board_pipeline=theme_core,
    )

    runtime.start(datetime(2026, 6, 18, 9, 5, 0))
    first = runtime.cycle(datetime(2026, 6, 18, 9, 5, 5))
    second = runtime.cycle(datetime(2026, 6, 18, 9, 5, 10))

    assert first["theme_board"]["top_themes"][0]["theme_status"] == "SPREADING_THEME"
    assert second["theme_board"]["top_themes"][0]["theme_status"] == "LEADING_THEME"
    assert second["theme_board"]["ready_allowed"] is False
    assert second["theme_board"]["order_intent_allowed"] is False
    assert second["entry_plan_count"] == 0
    assert second["virtual_order_count"] == 0
    assert [row for row in gateway.list_commands(include_finished=True, limit=50) if row["command_type"] == "send_order"] == []


def test_reboot_v2_cycle_registers_focused_expansion_subscriptions(tmp_path):
    db = TradingDatabase(str(tmp_path / "theme-core-v3-expansion.db"))
    gateway = GatewayStateStore()
    repo = ThemeEngineRepository(db)
    market_data = MarketDataStore()
    candle_builder = CandleBuilder()
    market_index_store = MarketIndexStore()
    _theme(repo, "ai", "AI Infra", ["000001", "000002", "000003"])
    _tick(market_data, "000001", 5.5, 8_000_000_000, speed=900_000_000, execution=150)
    _tick(market_data, "000002", 5.1, 7_000_000_000, speed=800_000_000, execution=145)
    _tick(market_data, "000003", 3.4, 3_000_000_000, speed=500_000_000, execution=125)
    theme_core = ThemeCoreV3RuntimePipeline(
        db=db,
        market_data=market_data,
        repository=repo,
        config=ThemeCoreV3RuntimeConfig(
            enabled=True,
            interval_sec=1,
            use_runtime_market_context=True,
            theme_expansion_subscriptions_enabled=True,
        ),
    )
    runtime = RebootV2Runtime(
        db=db,
        subscription_manager=RealTimeSubscriptionManager(GatewayCommandRealtimeClient(gateway), max_codes=20),
        candle_builder=candle_builder,
        market_data=market_data,
        market_index_store=market_index_store,
        config=StrategyRuntimeConfig(max_candidates_to_watch=20, realtime_subscription_limit=20),
        profile=RebootV2RuntimeProfile.V2_OBSERVE,
        theme_board_pipeline=theme_core,
        market_regime_pipeline=_MarketContextPipeline(),
    )

    runtime.start(datetime(2026, 6, 18, 9, 5, 0))
    first = runtime.cycle(datetime(2026, 6, 18, 9, 5, 5))
    second = runtime.cycle(datetime(2026, 6, 18, 9, 5, 10))

    assert second["theme_expansion_subscription"]["selected_count"] >= 2
    assert any("reboot_v2_theme_expansion" in record.sources for record in runtime.subscription_manager.records.values())
    assert db.conn.execute("SELECT COUNT(*) AS count FROM theme_expansion_subscription_decisions").fetchone()["count"] > 0
    assert first["theme_board"]["market_context_status"] == "OK"


def _theme(repo: ThemeEngineRepository, theme_id: str, name: str, codes: list[str]) -> None:
    repo.upsert_canonical_theme(
        CanonicalTheme(
            theme_id=theme_id,
            canonical_name=name,
            display_name=name,
            status=ThemeStatus.ACTIVE,
            confidence=0.9,
            trade_eligible=True,
        )
    )
    for code in codes:
        repo.upsert_current_membership(
            ThemeMembership(
                theme_id=theme_id,
                stock_code=code,
                stock_name=f"Stock {code}",
                membership_score=0.9,
                active=True,
                trade_eligible=True,
            )
        )


def _seed_batch(db: TradingDatabase, trade_date: str, rows: list[tuple[str, int, float, float]]) -> None:
    db.save_opening_turnover_seed_batch(
        {
            "trade_date": trade_date,
            "batch_time": "09:03",
            "command_id": f"cmd-{trade_date}",
            "row_count": len(rows),
            "parsed_count": len(rows),
            "parser_status": "OK",
            "rows": [
                {
                    "stock_code": code,
                    "stock_name": f"Stock {code}",
                    "rank": rank,
                    "turnover_krw": turnover,
                    "change_rate_pct": change,
                    "raw": {},
                }
                for code, rank, turnover, change in rows
            ],
        }
    )


def _tick(
    market_data: MarketDataStore,
    code: str,
    change: float,
    turnover: float,
    *,
    speed: float,
    execution: float,
    timestamp: datetime | None = None,
) -> None:
    market_data.update_tick(
        StrategyTick.from_realtime(
            code,
            price=10000,
            change_rate=change,
            cum_volume=100_000,
            trade_value=turnover,
            execution_strength=execution,
            spread_ticks=1,
            timestamp=timestamp or datetime(2026, 6, 18, 9, 5, 0),
            metadata={
                "turnover_speed": speed,
                "market": "KOSDAQ",
                "momentum_1m": 1.0,
                "momentum_3m": 0.8,
                "momentum_5m": 0.6,
                "upper_limit_gap_pct": 10.0,
            },
        )
    )


class _MarketContextPipeline:
    config = type("_Config", (), {"enabled": True})()

    def run_if_due(self, now):
        return {
            "enabled": True,
            "status": "OK",
            "trade_date": now.date().isoformat(),
            "calculated_at": now.isoformat(),
            "global_status": "EXPANSION",
            "kospi_status": "SELECTIVE",
            "kosdaq_status": "EXPANSION",
            "output_mode": "OBSERVE",
            "reason_codes": ["INDEX_UP"],
        }
