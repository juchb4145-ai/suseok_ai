from __future__ import annotations

import json
from datetime import datetime, timedelta

from kiwoom.client import MockKiwoomClient
from main import build_observe_runtime
from storage.db import TradingDatabase
from trading.strategy.config import StrategyRuntimeConfigRepository
from trading.strategy.market_data import StrategyTick
from trading.strategy.market_data import MarketDataStore
from trading.strategy.market_index import IndexTick, MarketIndexStore
from trading.strategy.models import Candidate, CandidateState, OrderMode
from trading.strategy.runtime import StrategyRuntimeConfig
from trading.strategy.runtime_settings import LEGACY_DEFAULT_SETTINGS, legacy_profile_payload
from trading.theme_engine.lab import (
    ConditionHitSnapshot,
    MarketStatus,
    MarketStrengthSnapshot,
    PriceLocationStatus,
    ThemeConditionSnapshot,
    ThemeLabFlowResult,
    ThemeLabThemeStatus,
)
from trading.theme_engine.models import CanonicalTheme, ThemeMembership, ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.runtime_pipeline import ThemeLabRuntimePipeline


def test_themelab_flow_is_default_and_runtime_contains_pipeline(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))

    runtime = build_observe_runtime(MockKiwoomClient(), db)

    assert runtime.config.theme_engine_mode == "themelab_flow"
    assert runtime.theme_lab_pipeline is not None
    db.close()


def test_themelab_runtime_uses_saved_risk_off_entry_settings(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    payload = legacy_profile_payload()
    settings_json = json.loads(json.dumps(LEGACY_DEFAULT_SETTINGS))
    settings_json["risk_off_entry"] = {
        **settings_json["risk_off_entry"],
        "enabled": True,
        "observe_only": True,
        "min_relative_strength_vs_index_pct": 5.5,
        "max_position_size_multiplier": 0.15,
    }
    payload["settings_json"] = json.dumps(settings_json)
    payload["config_json"] = payload["settings_json"]
    db.save_strategy_runtime_settings_profile(payload)

    runtime = build_observe_runtime(MockKiwoomClient(), db)

    config = runtime.theme_lab_pipeline.engine.config.risk_off_entry
    assert config.enabled is True
    assert config.observe_only is True
    assert config.min_relative_strength_vs_index_pct == 5.5
    assert config.max_position_size_multiplier == 0.15
    db.close()


def test_legacy_mode_uses_gate_pipeline_without_theme_lab_pipeline(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    StrategyRuntimeConfigRepository(db).save(StrategyRuntimeConfig(theme_engine_mode="legacy"))

    runtime = build_observe_runtime(MockKiwoomClient(), db)

    assert runtime.config.theme_engine_mode == "legacy"
    assert runtime.theme_lab_pipeline is None
    assert runtime.gate_pipeline is not None
    db.close()


def test_theme_lab_unresolved_conditions_emit_specific_readiness_warnings(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))

    runtime = build_observe_runtime(MockKiwoomClient(), db)

    assert "THEME_LAB_CONDITION_ALIVE_UNRESOLVED" in runtime.readiness_report.warnings
    assert "THEME_LAB_CONDITION_STRONG_UNRESOLVED" in runtime.readiness_report.warnings
    assert "THEME_LAB_CONDITION_LEADER_UNRESOLVED" in runtime.readiness_report.warnings
    db.close()


def test_theme_lab_uses_kiwoom_symbol_master_when_legacy_market_mapping_missing(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        _seed_theme(db)
        db.upsert_kiwoom_symbol_master(
            [
                {"code": "000001", "name": "stock-000001", "market": "KOSPI", "market_code": "0"},
                {"code": "000002", "name": "stock-000002", "market": "KOSDAQ", "market_code": "10"},
            ]
        )
        now = datetime(2026, 6, 5, 10, 0, 0)
        market_data = MarketDataStore()
        market_data.update_tick(_tick("000001", 105, 5.0, now))
        market_data.update_tick(_tick("000002", 104, 4.0, now))
        market_index_store = MarketIndexStore()
        market_index_store.update_index_tick(IndexTick.from_realtime("KOSPI", "KOSPI", 3000, change_rate=0.2, timestamp=now))
        market_index_store.update_index_tick(IndexTick.from_realtime("KOSDAQ", "KOSDAQ", 900, change_rate=-1.2, timestamp=now))
        pipeline = ThemeLabRuntimePipeline(db=db, market_data=market_data, market_index_store=market_index_store)

        result = pipeline.run(now)

        watch_by_symbol = {item.symbol: item for item in result.watchset}
        assert watch_by_symbol["000001"].candidate_market == "KOSPI"
        assert watch_by_symbol["000001"].candidate_market_source == "metadata_by_symbol.raw.market"
        assert watch_by_symbol["000002"].candidate_market == "KOSDAQ"
    finally:
        db.close()


def test_theme_lab_runtime_tick_runs_pipeline_saves_result_and_syncs_watchset(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    client = MockKiwoomClient()
    runtime = build_observe_runtime(client, db)
    _seed_theme(db)
    market_data = runtime.theme_lab_pipeline.market_data
    now = datetime(2026, 6, 1, 9, 1, 0)
    market_data.update_tick(_tick("000001", 106, 6.0, now))
    market_data.update_tick(_tick("000002", 104, 4.0, now))
    market_data.update_tick(_tick("000003", 100, 0.0, now))

    runtime.start(now)
    snapshot = runtime.cycle(now + timedelta(seconds=3))

    rows = db.conn.execute("SELECT * FROM theme_lab_flow_snapshots").fetchall()
    assert rows
    payload = json.loads(rows[-1]["payload_json"])
    assert payload["gate_decisions"]
    assert {item["symbol"] for item in payload["watchset_snapshots"]} == {"000001", "000002"}
    assert {"000001", "000002"} <= set(client.registered_codes)
    assert "000003" not in set(client.registered_codes)
    assert snapshot.gate_result_count == len(payload["gate_decisions"])
    db.close()


def test_theme_lab_runtime_uses_legacy_market_metadata_when_tick_has_no_market(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    _seed_theme(db)
    _seed_legacy_market(db, {"000001": "KOSDAQ", "000002": "KOSDAQ"})
    market_data = MarketDataStore()
    now = datetime(2026, 6, 1, 9, 1, 0)
    market_data.update_tick(_tick("000001", 106, 6.0, now))
    market_data.update_tick(_tick("000002", 104, 4.0, now))
    market_data.update_tick(_tick("000003", 100, 0.0, now))

    result = ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=MarketIndexStore(),
    ).run(now)

    watch_by_symbol = {item.symbol: item for item in result.watchset}

    assert watch_by_symbol["000001"].candidate_market == "KOSDAQ"
    assert watch_by_symbol["000001"].candidate_market_source == "metadata_by_symbol.raw.market"
    assert watch_by_symbol["000002"].candidate_market == "KOSDAQ"
    assert result.data_quality["market_classification_unknown_count"] == 0
    db.close()


def test_theme_lab_runtime_uses_realtime_price_context_for_price_location(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    _seed_theme(db)
    _seed_legacy_market(db, {"000001": "KOSDAQ", "000002": "KOSDAQ"})
    market_data = MarketDataStore()
    now = datetime(2026, 6, 1, 9, 2, 0)
    market_data.update_tick(
        StrategyTick.from_realtime(
            "000001",
            price=106,
            change_rate=6.0,
            cum_volume=10_000,
            trade_value=1_060_000,
            execution_strength=120,
            timestamp=now,
            metadata={
                "prev_close": 100,
                "name": "stock-000001",
                "session_high": 108,
                "day_high": 108,
                "vwap": 104,
                "vwap_ready": True,
                "recent_support_price": 103,
                "recent_support_ready": True,
                "recent_candles_1m": [{"high": 108, "low": 105, "close": 106}],
                "momentum_1m": 0.5,
                "momentum_3m": 0.3,
            },
        )
    )
    market_data.update_tick(_tick("000002", 104, 4.0, now))
    market_data.update_tick(_tick("000003", 100, 0.0, now))

    result = ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=MarketIndexStore(),
    ).run(now)

    watch = next(item for item in result.watchset if item.symbol == "000001")

    assert watch.price_location_status == PriceLocationStatus.PULLBACK_RECLAIM
    assert watch.gate_status.value == "READY"
    assert "MISSING_VWAP" not in watch.price_location_data_quality_flags
    assert "MISSING_RECENT_SUPPORT_PRICE" not in watch.price_location_data_quality_flags
    assert "MISSING_RECENT_CANDLES" not in watch.price_location_data_quality_flags
    db.close()


def test_theme_lab_pipeline_watchset_codes_bootstraps_from_condition_hits(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    pipeline = ThemeLabRuntimePipeline(
        db=db,
        market_data=MarketDataStore(),
        market_index_store=MarketIndexStore(),
    )
    pipeline.last_result = ThemeLabFlowResult(
        market=MarketStrengthSnapshot(MarketStatus.CHOPPY),
        themes=(
            ThemeConditionSnapshot(
                calculated_at="2026-06-04T09:01:00",
                theme_id="ai",
                theme_name="AI",
                theme_status=ThemeLabThemeStatus.WEAK_THEME,
                member_hits=(
                    ConditionHitSnapshot(
                        calculated_at="2026-06-04T09:01:00",
                        symbol="000001",
                        name="alive-only",
                        alive_hit=True,
                    ),
                    ConditionHitSnapshot(
                        calculated_at="2026-06-04T09:01:00",
                        symbol="000002",
                        name="strong",
                        alive_hit=True,
                        strong_hit=True,
                        return_pct=3.1,
                    ),
                    ConditionHitSnapshot(
                        calculated_at="2026-06-04T09:01:00",
                        symbol="000003",
                        name="leader",
                        alive_hit=True,
                        strong_hit=True,
                        leader_hit=True,
                        return_pct=5.2,
                    ),
                ),
            ),
        ),
        watchset=(),
        gate_decisions=(),
        data_quality={},
    )

    assert pipeline.watchset_codes() == ["000003", "000002"]
    db.close()


def test_theme_lab_empty_watchset_bootstraps_candidate_realtime_subscriptions(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    client = MockKiwoomClient()
    runtime = build_observe_runtime(client, db)
    _seed_theme(db)
    now = datetime(2026, 6, 1, 9, 1, 0)
    for code in ("000001", "000002", "000003"):
        db.save_candidate(
            Candidate(
                trade_date="2026-06-01",
                code=code,
                state=CandidateState.DETECTED,
                detected_at=now.isoformat(),
                last_seen_at=now.isoformat(),
                expires_at=(now + timedelta(minutes=30)).isoformat(),
                condition_names=["테마랩_생존_-1"],
                metadata={"sub_status": "DATA_INSUFFICIENT", "insufficient_reason": ["NO_GATE_RESULT"]},
            )
        )

    snapshot = runtime.start(now)

    assert {"000001", "000002", "000003"} <= set(client.registered_codes)
    assert snapshot.candidate_subscription_selected_count == 3
    assert "THEME_LAB_BOOTSTRAP_SUBSCRIPTIONS=3" in snapshot.warnings
    for code in ("000001", "000002", "000003"):
        record = runtime.subscription_manager.records[code]
        assert "theme_lab_bootstrap" in record.sources
        assert "candidate_watch" not in record.sources
    db.close()


def test_theme_lab_condition_adapter_registers_only_three_lab_conditions(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    client = MockKiwoomClient()
    client.set_conditions([(1, "테마랩_생존_-1"), (2, "테마랩_강세_3"), (3, "테마랩_주도_5"), (4, "legacy")])
    runtime = build_observe_runtime(client, db)

    runtime.start(datetime(2026, 6, 1, 9, 0, 0))
    client.emit_condition_load_result(True, "ok")

    assert [call["condition_name"] for call in client.send_condition_calls] == [
        "테마랩_생존_-1",
        "테마랩_강세_3",
        "테마랩_주도_5",
    ]
    db.close()


def _seed_theme(db: TradingDatabase) -> None:
    repo = ThemeEngineRepository(db)
    repo.upsert_canonical_theme(
        CanonicalTheme(
            theme_id="ai",
            canonical_name="AI",
            display_name="AI",
            status=ThemeStatus.ACTIVE,
            trade_eligible=True,
        )
    )
    for code in ("000001", "000002", "000003"):
        repo.upsert_current_membership(
            ThemeMembership(
                theme_id="ai",
                stock_code=code,
                stock_name=f"stock-{code}",
                membership_score=1.0,
                active=True,
                trade_eligible=True,
            )
        )


def _seed_legacy_market(db: TradingDatabase, markets: dict[str, str]) -> None:
    db.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS legacy_theme_mappings_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            market TEXT NOT NULL DEFAULT '',
            theme_id TEXT NOT NULL DEFAULT '',
            theme_name TEXT NOT NULL DEFAULT '',
            strategy_profile TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    with db.conn:
        for code, market in markets.items():
            db.conn.execute(
                """
                INSERT INTO legacy_theme_mappings_archive(
                    code, name, market, theme_id, theme_name, strategy_profile, enabled
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (code, f"stock-{code}", market, "ai", "AI", f"{market}_THEME_PROFILE"),
            )


def _tick(code: str, price: int, change_rate: float, now: datetime) -> StrategyTick:
    return StrategyTick.from_realtime(
        code=code,
        price=price,
        change_rate=change_rate,
        cum_volume=1000,
        trade_value=10_000_000,
        timestamp=now,
        metadata={"prev_close": 100, "name": f"stock-{code}", "day_high": price},
    )
