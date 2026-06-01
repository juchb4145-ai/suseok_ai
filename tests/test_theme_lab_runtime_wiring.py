from __future__ import annotations

import json
from datetime import datetime, timedelta

from kiwoom.client import MockKiwoomClient
from main import build_observe_runtime
from storage.db import TradingDatabase
from trading.strategy.config import StrategyRuntimeConfigRepository
from trading.strategy.market_data import StrategyTick
from trading.strategy.models import OrderMode
from trading.strategy.runtime import StrategyRuntimeConfig
from trading.theme_engine.models import CanonicalTheme, ThemeMembership, ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository


def test_themelab_flow_is_default_and_runtime_contains_pipeline(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))

    runtime = build_observe_runtime(MockKiwoomClient(), db)

    assert runtime.config.theme_engine_mode == "themelab_flow"
    assert runtime.theme_lab_pipeline is not None
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
