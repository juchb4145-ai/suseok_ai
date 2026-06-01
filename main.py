from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from storage.db import TradingDatabase
from trading.engine import TradingEngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kiwoom pullback semi-auto trader")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run without Kiwoom OpenAPI ActiveX for UI and logic testing.",
    )
    parser.add_argument(
        "--db",
        default=str(Path("data") / "trader.sqlite3"),
        help="SQLite database path.",
    )
    return parser.parse_args()


def configure_qt_paths() -> None:
    try:
        import PyQt5
    except ImportError:
        return

    pyqt_dir = Path(PyQt5.__file__).resolve().parent
    qt_root = pyqt_dir / "Qt5"
    platforms_dir = qt_root / "plugins" / "platforms"
    qt_bin = qt_root / "bin"

    if platforms_dir.exists():
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(platforms_dir))
    if qt_bin.exists():
        os.environ["PATH"] = f"{qt_bin}{os.pathsep}{os.environ.get('PATH', '')}"


def build_observe_runtime(client, db: TradingDatabase):
    from trading.strategy.bridge import StrategyMarketDataBridge
    from trading.strategy.candidates import CandidateCollector
    from trading.strategy.candles import CandleBuilder
    from trading.strategy.conditions import (
        ConditionProfileRepository,
        KiwoomConditionAdapter,
        ensure_default_condition_profiles,
        ensure_theme_lab_condition_profiles,
    )
    from trading.strategy.entry import EntryPlanBuilder
    from trading.strategy.exit import ExitDecisionEngine, VirtualPositionService
    from trading.strategy.holding import StaticHoldingProvider
    from trading.strategy.indicators import IndicatorCalculator
    from trading.strategy.intraday import IntradayStateTracker
    from trading.strategy.market_data import MarketDataStore
    from trading.strategy.market_index import IndexCodeMapper, MarketIndexStore
    from trading.strategy.pipeline import GatePipeline
    from trading.strategy.readiness import build_readiness_report, dedupe_warnings
    from trading.strategy.realtime import RealTimeSubscriptionManager
    from trading.strategy.review import TradeReviewService
    from trading.strategy.config import StrategyRuntimeConfigRepository
    from trading.strategy.runtime_settings import StrategyRuntimeSettingsRepository
    from trading.strategy.runtime import StrategyRuntime
    from trading.strategy.virtual_orders import VirtualOrderService
    from trading.strategy.hybrid_validation import HybridValidationRepository
    from trading.theme_engine.runtime_pipeline import ThemeLabRuntimePipeline
    from trading.theme_engine.context_provider import DynamicThemeContextProvider
    from trading.theme_engine.repository import ThemeEngineRepository

    config_result = StrategyRuntimeConfigRepository(db).load()
    settings = StrategyRuntimeSettingsRepository(db).load()
    condition_seed_result = ensure_default_condition_profiles(db)
    config = config_result.config
    theme_lab_condition_seed_result = ensure_theme_lab_condition_profiles(
        db,
        condition_names=config.theme_lab_condition_names,
        condition_purposes=config.theme_lab_condition_purposes,
    )
    market_data = MarketDataStore()
    candle_builder = CandleBuilder()
    market_index_store = MarketIndexStore()
    bridge = StrategyMarketDataBridge(
        market_data,
        candle_builder,
        market_index_store=market_index_store,
        index_code_mapper=IndexCodeMapper(),
    )
    bridge.attach(client)

    candidate_collector = CandidateCollector(db, client=client)
    theme_context_provider = DynamicThemeContextProvider(ThemeEngineRepository(db))
    indicator_calculator = IndicatorCalculator(market_data, candle_builder)
    gate_pipeline = GatePipeline(
        theme_context_provider,
        market_data,
        candle_builder,
        indicator_calculator,
        IntradayStateTracker(settings),
        market_index_store,
        settings,
        hybrid_validation_repository=HybridValidationRepository(db),
    )
    lab_purposes = set(config.theme_lab_condition_purposes.values())
    condition_adapter = KiwoomConditionAdapter(
        client,
        ConditionProfileRepository(db),
        purpose_filter=lab_purposes if config.theme_engine_mode == "themelab_flow" else None,
    )
    candidate_collector.attach(condition_adapter)
    theme_lab_pipeline = None
    if config.theme_engine_mode == "themelab_flow":
        theme_lab_pipeline = ThemeLabRuntimePipeline(
            db=db,
            market_data=market_data,
            market_index_store=market_index_store,
            interval_sec=config.theme_lab_pipeline_interval_sec,
        )
    runtime = StrategyRuntime(
        db=db,
        candidate_collector=candidate_collector,
        subscription_manager=RealTimeSubscriptionManager(client, max_codes=config.realtime_subscription_limit),
        candle_builder=candle_builder,
        gate_pipeline=gate_pipeline,
        entry_plan_builder=EntryPlanBuilder(settings=settings),
        virtual_order_service=VirtualOrderService(db=db, settings=settings),
        virtual_position_service=VirtualPositionService(db=db),
        exit_decision_engine=ExitDecisionEngine(settings),
        trade_review_service=TradeReviewService(settings),
        config=config,
        condition_adapter=condition_adapter,
        holding_provider=StaticHoldingProvider(set(config.holding_watch_codes)),
        theme_lab_pipeline=theme_lab_pipeline,
    )
    readiness_report = build_readiness_report(
        db,
        subscription_manager=runtime.subscription_manager,
        theme_engine_mode=config.theme_engine_mode,
        theme_lab_flow_wired=theme_lab_pipeline is not None,
        condition_adapter=condition_adapter,
    )
    runtime.readiness_report = readiness_report
    runtime.startup_warnings = dedupe_warnings(
        list(config_result.warnings)
        + settings.validation_warnings
        + condition_seed_result.warnings
        + theme_lab_condition_seed_result.warnings
        + readiness_report.warnings
    )
    return runtime


def main() -> int:
    args = parse_args()
    configure_qt_paths()

    try:
        from PyQt5.QtWidgets import QApplication
    except ImportError as exc:
        print("PyQt5 is required. Install dependencies in 32bit Python 3.9.13.", file=sys.stderr)
        raise SystemExit(1) from exc

    from kiwoom.client import KiwoomClient, MockKiwoomClient
    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    db = TradingDatabase(args.db)
    client = MockKiwoomClient() if args.mock else KiwoomClient()
    legacy_live_allowed = bool(args.mock or os.environ.get("TRADING_ALLOW_LEGACY_LIVE", "0") == "1")
    engine = TradingEngine(client=client, db=db, legacy_live_allowed=legacy_live_allowed)
    strategy_runtime_unavailable_reason = ""
    try:
        strategy_runtime = build_observe_runtime(client, db)
    except Exception as exc:
        strategy_runtime_unavailable_reason = f"OBSERVE runtime disabled: {exc}"
        print(strategy_runtime_unavailable_reason, file=sys.stderr)
        strategy_runtime = None
    window = MainWindow(
        engine=engine,
        db=db,
        mock_mode=args.mock,
        strategy_runtime=strategy_runtime,
        strategy_runtime_unavailable_reason=strategy_runtime_unavailable_reason,
    )
    app.aboutToQuit.connect(db.close)
    window.resize(1320, 820)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
