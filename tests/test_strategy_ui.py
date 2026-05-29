import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from main import configure_qt_paths

configure_qt_paths()

from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QGroupBox, QMessageBox

from kiwoom.client import MockKiwoomClient
from main import build_observe_runtime
from storage.db import TradingDatabase
from trading.engine import TradingEngine
from trading.models import BuyLeg, LegStatus, WatchItem
from trading.strategy.config import StrategyRuntimeConfigRepository, config_to_dict
from trading.strategy.models import (
    BlockType,
    Candidate,
    CandidateEvent,
    CandidateSourceType,
    CandidateState,
    EntryPlan,
    ExitDecision,
    FillPolicy,
    IndicatorSnapshot,
    ReviewFinalStatus,
    StrategyProfile,
    TradeReview,
    VirtualOrder,
    VirtualOrderStatus,
    VirtualPosition,
)
from trading.strategy.runtime import StrategyRuntimeConfig, StrategyRuntimeSnapshot
from trading.strategy.themes import ThemeMapping, ThemeRepository
from ui.main_window import MainWindow
from ui.table_models import WatchItemTableModel
from ui.ui_state import settings as ui_settings
from ui.widgets import StatusBadge, SummaryCard


NOW = datetime(2026, 5, 29, 9, 0)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


@dataclass
class FakeRuntimeConfig:
    evaluation_interval_sec: int = 1


@dataclass
class FakeRuntime:
    config: FakeRuntimeConfig = field(default_factory=FakeRuntimeConfig)
    start_calls: int = 0
    stop_calls: int = 0
    cycle_calls: int = 0
    start_fail: bool = False
    stop_fail: bool = False
    cycle_fail: bool = False
    startup_warnings: list[str] = field(default_factory=list)

    def start(self):
        self.start_calls += 1
        if self.start_fail:
            raise RuntimeError("start boom")
        return StrategyRuntimeSnapshot(
            started=True,
            cycle_at=NOW.isoformat(),
            active_candidate_count=2,
            condition_profiles_count=3,
            unresolved_condition_profiles_count=1,
            theme_mappings_count=0,
            enabled_theme_mappings_count=0,
            active_candidates_with_theme_mapping=0,
            active_candidates_without_theme_mapping=2,
            theme_mapping_coverage_pct=0.0,
            protected_subscription_usage="4/85",
            warnings=["STARTED", "THEME_MAPPING_EMPTY", "THEME_MAPPING_EMPTY"],
        )

    def stop(self):
        self.stop_calls += 1
        if self.stop_fail:
            raise RuntimeError("stop boom")
        return StrategyRuntimeSnapshot(started=False, cycle_at=NOW.isoformat(), warnings=["STOPPED"])

    def cycle(self):
        self.cycle_calls += 1
        if self.cycle_fail:
            raise RuntimeError("cycle boom")
        return StrategyRuntimeSnapshot(
            started=True,
            cycle_at=(NOW + timedelta(seconds=self.cycle_calls)).isoformat(),
            active_candidate_count=3,
            gate_result_count=4,
            entry_plan_count=1,
            virtual_order_count=1,
            open_position_count=1,
            review_count=1,
            warnings=["CYCLE_OK"],
        )


@dataclass
class QuietRuntime:
    config: FakeRuntimeConfig = field(default_factory=FakeRuntimeConfig)
    start_calls: int = 0
    stop_calls: int = 0
    cycle_calls: int = 0
    startup_warnings: list[str] = field(default_factory=list)

    def start(self):
        self.start_calls += 1
        return StrategyRuntimeSnapshot(
            started=True,
            cycle_at=NOW.isoformat(),
            active_candidate_count=2,
            warnings=[],
        )

    def stop(self):
        self.stop_calls += 1
        return StrategyRuntimeSnapshot(started=False, cycle_at=NOW.isoformat(), warnings=[])

    def cycle(self):
        self.cycle_calls += 1
        return StrategyRuntimeSnapshot(
            started=True,
            cycle_at=(NOW + timedelta(seconds=self.cycle_calls)).isoformat(),
            active_candidate_count=2,
            warnings=[],
        )


@dataclass
class TimingRuntime(FakeRuntime):
    def start(self, *, timing_callback=None):
        if timing_callback is not None:
            timing_callback("custom_probe", 0.123)
        return super().start()


def make_window(tmp_path, qapp, runtime=None, clear_ui_state=True):
    if clear_ui_state:
        ui_settings().clear()
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    client = MockKiwoomClient()
    engine = TradingEngine(client=client, db=db)
    window = MainWindow(engine=engine, db=db, mock_mode=True, strategy_runtime=runtime)
    return window, db, client, engine


def watch_table_row_count(window):
    return window.watch_proxy_model.rowCount()


def watch_table_text(window, row, column):
    index = window.watch_proxy_model.index(row, column)
    return window.watch_proxy_model.data(index)


def watch_table_numeric(window, row, column):
    index = window.watch_proxy_model.index(row, column)
    source = window.watch_proxy_model.mapToSource(index)
    return window.watch_model.data(source, window.watch_model.SortRole)


def watch_table_codes(window):
    return [watch_table_text(window, row, 0) for row in range(watch_table_row_count(window))]


def watch_table_row_for_code(window, code):
    for row, value in enumerate(watch_table_codes(window)):
        if value == code:
            return row
    raise AssertionError(f"watch code not visible: {code}")


def select_watch_row(window, row):
    index = window.watch_proxy_model.index(row, 0)
    window.table.setCurrentIndex(index)
    window.table.selectRow(row)


def candidate_table_row_count(window):
    return window.strategy_candidate_proxy_model.rowCount()


def candidate_table_text(window, row, column):
    index = window.strategy_candidate_proxy_model.index(row, column)
    return window.strategy_candidate_proxy_model.data(index)


def review_table_row_count(window):
    return window.review_proxy_model.rowCount()


def review_table_text(window, row, column):
    index = window.review_proxy_model.index(row, column)
    return window.review_proxy_model.data(index)


def review_table_numeric(window, row, column):
    index = window.review_proxy_model.index(row, column)
    source = window.review_proxy_model.mapToSource(index)
    return window.review_model.data(source, window.review_model.SortRole)


def review_table_codes(window):
    return [review_table_text(window, row, 1) for row in range(review_table_row_count(window))]


def review_table_row_for_code(window, code):
    for row, value in enumerate(review_table_codes(window)):
        if value == code:
            return row
    raise AssertionError(f"review code not visible: {code}")


def select_review_row(window, row):
    index = window.review_proxy_model.index(row, 0)
    window.review_table.setCurrentIndex(index)
    window.review_table.selectRow(row)


def candidate_table_codes(window):
    return [candidate_table_text(window, row, 0) for row in range(candidate_table_row_count(window))]


def candidate_table_row_for_code(window, code):
    for row, value in enumerate(candidate_table_codes(window)):
        if value == code:
            return row
    raise AssertionError(f"candidate code not visible: {code}")


def select_candidate_row(window, row):
    index = window.strategy_candidate_proxy_model.index(row, 0)
    window.strategy_candidate_table.setCurrentIndex(index)
    window.strategy_candidate_table.selectRow(row)


def wait_for_filter_debounce(qapp):
    QTest.qWait(230)
    qapp.processEvents()


def wait_until(qapp, predicate, timeout_ms=1500):
    deadline = perf_counter() + timeout_ms / 1000.0
    while perf_counter() < deadline:
        qapp.processEvents()
        if predicate():
            return
        QTest.qWait(20)
    qapp.processEvents()
    assert predicate()


def test_status_badge_and_summary_card_state_changes(qapp):
    badge = StatusBadge("대기", "neutral")
    card = SummaryCard("OBSERVE", "stopped", "-", "neutral")

    badge.set_status("위험", "danger")
    card.set_summary("running", "cycle now", "success")

    assert badge.text() == "위험"
    assert badge.tone == "danger"
    assert card.value_label.text() == "running"
    assert card.detail_label.text() == "cycle now"
    assert card.tone == "success"
    badge.close()
    card.close()


def test_login_refreshes_accounts_after_connection_event(tmp_path, qapp):
    class DelayedAccountClient(MockKiwoomClient):
        def __init__(self) -> None:
            super().__init__()
            self.accounts: list[str] = []
            self.login_calls = 0
            self.get_accounts_calls = 0

        def login(self) -> int:
            self.login_calls += 1
            return 0

        def get_accounts(self) -> list[str]:
            self.get_accounts_calls += 1
            return list(self.accounts)

    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    client = DelayedAccountClient()
    engine = TradingEngine(client=client, db=db)
    window = MainWindow(engine=engine, db=db, mock_mode=False, strategy_runtime=FakeRuntime())

    window._login()

    assert client.login_calls == 1
    assert client.get_accounts_calls == 0
    assert window.account_combo.count() == 0

    client.accounts = ["1234567890"]
    client.connected.emit(True, 0, "connected")

    assert client.get_accounts_calls == 1
    assert window.account_combo.itemText(0) == "1234567890"
    assert engine.account == "1234567890"
    db.close()
    window.close()


def test_watch_item_model_displays_and_sorts_numeric_columns(tmp_path, qapp):
    window, db, _, engine = make_window(tmp_path, qapp, FakeRuntime())
    engine.items = {
        "111111": watch_item("111111", "Alpha", current_price=2_000, budget=500_000),
        "222222": watch_item("222222", "Beta", current_price=10_000, budget=1_500_000),
        "333333": watch_item("333333", "Gamma", current_price=900, budget=200_000),
    }

    window.refresh_table()

    assert watch_table_row_count(window) == 3
    row = watch_table_row_for_code(window, "222222")
    assert watch_table_text(window, row, 0) == "222222"
    assert watch_table_text(window, row, 1) == "Beta"
    assert watch_table_text(window, row, 2) == "10,000"
    assert watch_table_text(window, row, 3) == "1,500,000"
    assert watch_table_numeric(window, row, 2) == 10_000.0

    window.table.sortByColumn(2, Qt.AscendingOrder)
    assert watch_table_codes(window) == ["333333", "111111", "222222"]
    window.table.sortByColumn(2, Qt.DescendingOrder)
    assert watch_table_codes(window) == ["222222", "111111", "333333"]
    db.close()
    window.close()


def test_watch_item_filters_are_proxy_only(tmp_path, qapp, monkeypatch):
    runtime = FakeRuntime()
    window, db, client, engine = make_window(tmp_path, qapp, runtime)
    engine.items = {
        "111111": watch_item("111111", "Holding", holding_quantity=5, average_price=9_900),
        "222222": watch_item("222222", "Auto", auto_buy_enabled=True),
        "333333": watch_item("333333", "OpenOrder", leg1_status=LegStatus.UNFILLED, order_no="A1", ordered_quantity=10, filled_quantity=3),
        "444444": watch_item("444444", "StopRisk", current_price=8_900, stop_loss_price=9_000),
        "555555": watch_item("555555", "ProfitDone", take_profit_done=True),
        "666666": watch_item("666666", "Watching", leg1_status=LegStatus.WATCHING),
    }
    window.refresh_table()

    monkeypatch.setattr(runtime, "cycle", lambda: (_ for _ in ()).throw(AssertionError("watch filter must not cycle")))
    monkeypatch.setattr(db, "save_candidate", lambda item: (_ for _ in ()).throw(AssertionError("watch filter must not save candidate")))
    monkeypatch.setattr(db, "save_trade_review", lambda item: (_ for _ in ()).throw(AssertionError("watch filter must not save review")))
    monkeypatch.setattr(db, "save_virtual_order", lambda item: (_ for _ in ()).throw(AssertionError("watch filter must not save virtual order")))
    monkeypatch.setattr(engine, "register_realtime", lambda: (_ for _ in ()).throw(AssertionError("watch filter must not register realtime")))
    monkeypatch.setattr(client, "send_order", lambda request: (_ for _ in ()).throw(AssertionError("watch filter must not call order path")))

    window.watch_filter_bar.search_edit.setText("Holding")
    wait_for_filter_debounce(qapp)
    assert watch_table_codes(window) == ["111111"]

    window.watch_filter_bar.clear_filters()
    window.watch_filter_bar.holding_combo.setCurrentIndex(1)
    assert watch_table_codes(window) == ["111111"]

    window.watch_filter_bar.clear_filters()
    window.watch_filter_bar.auto_buy_combo.setCurrentIndex(1)
    assert watch_table_codes(window) == ["222222"]

    window.watch_filter_bar.clear_filters()
    window.watch_filter_bar.open_order_combo.setCurrentIndex(1)
    assert watch_table_codes(window) == ["333333"]

    window.watch_filter_bar.clear_filters()
    window.watch_filter_bar.stop_risk_combo.setCurrentIndex(1)
    assert watch_table_codes(window) == ["444444"]

    window.watch_filter_bar.clear_filters()
    window.watch_filter_bar.take_profit_combo.setCurrentIndex(1)
    assert watch_table_codes(window) == ["555555"]

    window.watch_filter_bar.clear_filters()
    window.watch_filter_bar.watching_combo.setCurrentIndex(1)
    assert watch_table_codes(window) == ["666666"]

    window.watch_filter_bar.clear_filters()
    window.watch_filter_bar.pending_combo.setCurrentIndex(1)
    assert watch_table_codes(window) == ["333333"]

    window.watch_filter_bar.clear_filters()
    assert watch_table_row_count(window) == 6
    db.close()
    window.close()


def test_watch_selection_loads_form_through_proxy_sort_and_filter(tmp_path, qapp):
    window, db, _, engine = make_window(tmp_path, qapp, FakeRuntime())
    engine.items = {
        "111111": watch_item("111111", "Alpha", current_price=2_000, budget=500_000),
        "222222": watch_item("222222", "Beta", current_price=10_000, budget=1_500_000),
        "333333": watch_item("333333", "Gamma", current_price=900, budget=200_000),
    }
    window.refresh_table()

    select_watch_row(window, watch_table_row_for_code(window, "111111"))
    assert window.code_edit.text() == "111111"
    assert window.name_edit.text() == "Alpha"

    window.table.sortByColumn(2, Qt.DescendingOrder)
    select_watch_row(window, watch_table_row_for_code(window, "222222"))
    assert window.code_edit.text() == "222222"
    assert window.name_edit.text() == "Beta"

    window.watch_filter_bar.search_edit.setText("Gamma")
    wait_for_filter_debounce(qapp)
    select_watch_row(window, 0)
    assert window.code_edit.text() == "333333"
    assert window.name_edit.text() == "Gamma"
    db.close()
    window.close()


def test_watch_detail_panel_and_live_auto_buy_warning(tmp_path, qapp):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    client = MockKiwoomClient()
    engine = TradingEngine(client=client, db=db)
    item = watch_item(
        "111111",
        "Risky",
        current_price=8_900,
        stop_loss_price=9_000,
        holding_quantity=4,
        average_price=9_100,
        auto_buy_enabled=True,
        leg1_status=LegStatus.UNFILLED,
        order_no="O-1",
        ordered_quantity=10,
        filled_quantity=4,
    )
    engine.items = {item.code: item}
    window = MainWindow(engine=engine, db=db, mock_mode=False, strategy_runtime=FakeRuntime())

    window.ordering_check.setChecked(True)
    window.refresh_table()
    select_watch_row(window, 0)
    detail = window.watch_detail_panel.text()

    assert "코드: 111111" in detail
    assert "종목명: Risky" in detail
    assert "현재가: 8,900" in detail
    assert "손절가: 9,000" in detail
    assert "자동매수: ON" in detail
    assert "보유수량: 4" in detail
    assert "1차 목표가=10,100" in detail
    assert "주문번호=O-1" in detail
    assert "경고: 실거래 가능 + 주문 가능 ON + 자동매수 ON" in detail
    db.close()
    window.close()


def test_watch_risk_tones_and_leg_status_tones(tmp_path, qapp):
    window, db, _, engine = make_window(tmp_path, qapp, FakeRuntime())
    engine.items = {
        "111111": watch_item("111111", "Danger", current_price=8_900, stop_loss_price=9_000),
        "222222": watch_item("222222", "Warning", current_price=9_200, stop_loss_price=9_000),
        "333333": watch_item("333333", "Profit", take_profit_done=True),
        "444444": watch_item("444444", "Open", leg1_status=LegStatus.UNFILLED, order_no="O-1", ordered_quantity=5),
        "555555": watch_item("555555", "Watching", leg1_status=LegStatus.WATCHING),
        "666666": watch_item("666666", "Filled", leg1_status=LegStatus.FILLED),
    }
    window.refresh_table()

    def tone(code):
        row = watch_table_row_for_code(window, code)
        source = window.watch_proxy_model.mapToSource(window.watch_proxy_model.index(row, 0))
        return window.watch_model.data(source, WatchItemTableModel.RiskToneRole)

    def leg_tone(code):
        row = watch_table_row_for_code(window, code)
        source = window.watch_proxy_model.mapToSource(window.watch_proxy_model.index(row, 7))
        return window.watch_model.data(source, WatchItemTableModel.LegToneRole)

    assert tone("111111") == "danger"
    assert tone("222222") == "warning"
    assert tone("333333") == "success"
    assert tone("444444") == "warning"
    assert leg_tone("555555") == "info"
    assert leg_tone("666666") == "success"
    db.close()
    window.close()


def test_watch_save_delete_selection_and_filter_state(tmp_path, qapp):
    window, db, _, engine = make_window(tmp_path, qapp, FakeRuntime())
    item = watch_item("111111", "Alpha", auto_buy_enabled=True, budget=500_000)
    engine.items = {item.code: item}
    window.refresh_table()
    window.watch_filter_bar.auto_buy_combo.setCurrentIndex(1)
    select_watch_row(window, 0)

    window.budget_spin.setValue(700_000)
    window._save_current()

    assert window.code_edit.text() == "111111"
    assert engine.items["111111"].budget == 700_000
    assert window.watch_filter_bar.auto_buy_only()
    assert watch_table_codes(window) == ["111111"]

    window._delete_current()

    assert "선택된 종목 없음" in window.watch_detail_panel.text()
    assert watch_table_row_count(window) == 0
    assert window.watch_filter_bar.auto_buy_only()
    db.close()
    window.close()


def test_watch_refresh_filter_detail_are_read_only(tmp_path, qapp, monkeypatch):
    runtime = FakeRuntime()
    window, db, client, engine = make_window(tmp_path, qapp, runtime)
    engine.items = {"111111": watch_item("111111", "Alpha", current_price=10_000)}

    monkeypatch.setattr(runtime, "cycle", lambda: (_ for _ in ()).throw(AssertionError("watch ui must not cycle")))
    monkeypatch.setattr(db, "save_candidate", lambda item: (_ for _ in ()).throw(AssertionError("watch ui must not save candidate")))
    monkeypatch.setattr(db, "save_trade_review", lambda item: (_ for _ in ()).throw(AssertionError("watch ui must not save review")))
    monkeypatch.setattr(db, "save_virtual_order", lambda item: (_ for _ in ()).throw(AssertionError("watch ui must not save virtual order")))
    monkeypatch.setattr(engine, "register_realtime", lambda: (_ for _ in ()).throw(AssertionError("watch ui must not register realtime")))
    monkeypatch.setattr(client, "send_order", lambda request: (_ for _ in ()).throw(AssertionError("watch ui must not call order path")))

    window.refresh_table()
    window.watch_filter_bar.search_edit.setText("Alpha")
    wait_for_filter_debounce(qapp)
    select_watch_row(window, 0)
    window._display_watch_detail(engine.items["111111"])

    assert "코드: 111111" in window.watch_detail_panel.text()
    db.close()
    window.close()


def save_candidate(db, code, state, last_seen, metadata=None, name=None, can_recover=None, theme_ids=None):
    return db.save_candidate(
        Candidate(
            trade_date="2026-05-29",
            code=code,
            name=name or code,
            state=state,
            detected_at=NOW.isoformat(),
            last_seen_at=last_seen.isoformat(),
            expires_at=(NOW + timedelta(minutes=30)).isoformat(),
            block_type=BlockType.TEMPORARY if state == CandidateState.BLOCKED else BlockType.NONE,
            can_recover=(state == CandidateState.BLOCKED) if can_recover is None else can_recover,
            theme_ids=theme_ids or [],
            metadata=metadata or {},
        )
    )


def save_review(db, code, status, grade="", created_at=None, candidate_id=1, **kwargs):
    return db.save_trade_review(
        TradeReview(
            candidate_id=candidate_id,
            trade_date=kwargs.pop("trade_date", "2026-05-29"),
            code=code,
            name=kwargs.pop("name", code),
            market=kwargs.pop("market", "KOSDAQ"),
            theme_id=kwargs.pop("theme_id", "robot"),
            theme_name=kwargs.pop("theme_name", "Robot Theme"),
            final_grade=grade,
            final_status=status,
            virtual_order_status=kwargs.pop("virtual_order_status", ""),
            exit_reason=kwargs.pop("exit_reason", ""),
            max_return_5m=kwargs.pop("max_return_5m", None),
            max_return_10m=kwargs.pop("max_return_10m", None),
            max_return_20m=kwargs.pop("max_return_20m", None),
            max_drawdown_20m=kwargs.pop("max_drawdown_20m", None),
            missed_reason=kwargs.pop("missed_reason", ""),
            false_negative_flag=kwargs.pop("false_negative_flag", False),
            false_positive_flag=kwargs.pop("false_positive_flag", False),
            blocked_but_later_rallied=kwargs.pop("blocked_but_later_rallied", False),
            expired_but_later_rallied=kwargs.pop("expired_but_later_rallied", False),
            details=kwargs.pop("details", {}),
            virtual_position_id=kwargs.pop("virtual_position_id", None),
            created_at=created_at or NOW.isoformat(),
            **kwargs,
        )
    )


def watch_item(
    code,
    name=None,
    *,
    current_price=10_000,
    stop_loss_price=9_000,
    budget=1_000_000,
    holding_quantity=0,
    average_price=0.0,
    auto_buy_enabled=False,
    take_profit_done=False,
    leg1_status=LegStatus.WAITING,
    leg2_status=LegStatus.WAITING,
    leg3_status=LegStatus.WAITING,
    order_no="",
    ordered_quantity=0,
    filled_quantity=0,
):
    return WatchItem(
        code=code,
        name=name or code,
        current_price=current_price,
        stop_loss_price=stop_loss_price,
        budget=budget,
        holding_quantity=holding_quantity,
        average_price=average_price,
        auto_buy_enabled=auto_buy_enabled,
        take_profit_done=take_profit_done,
        legs=[
            BuyLeg(1, 10_100, 40.0, leg1_status, order_no, ordered_quantity, filled_quantity),
            BuyLeg(2, 9_900, 30.0, leg2_status),
            BuyLeg(3, 9_700, 30.0, leg3_status),
        ],
    )


def test_observe_start_stop_and_duplicate_noops_do_not_call_order_paths(tmp_path, qapp, monkeypatch):
    runtime = FakeRuntime()
    window, db, client, engine = make_window(tmp_path, qapp, runtime)
    calls = []

    monkeypatch.setattr(client, "send_order", lambda request: calls.append(request))
    monkeypatch.setattr(engine, "register_realtime", lambda: (_ for _ in ()).throw(AssertionError("manual realtime must not be called")))

    window.start_observe_strategy()
    wait_until(qapp, lambda: runtime.start_calls == 1 and window._strategy_running)
    window.start_observe_strategy()
    window.stop_observe_strategy()
    window.stop_observe_strategy()

    assert runtime.start_calls == 1
    assert runtime.stop_calls == 1
    assert calls == []
    assert client.orders == []
    assert not window.strategy_timer.isActive()
    assert window.strategy_start_button.isEnabled()
    db.close()
    window.close()


def test_observe_start_logs_timing_steps_on_ui_thread(tmp_path, qapp):
    runtime = TimingRuntime()
    window, db, _, _ = make_window(tmp_path, qapp, runtime)

    window.start_observe_strategy()

    assert runtime.start_calls == 1
    assert window._strategy_starting is False
    assert window._strategy_running is True
    assert "OBSERVE start step: custom_probe 0.123s" in window.log_view.toPlainText()
    assert "OBSERVE running" in window.strategy_status_label.text()
    db.close()
    window.close()


def test_observe_readiness_summary_displays_snapshot_and_dedupes_warnings(tmp_path, qapp):
    runtime = FakeRuntime(startup_warnings=["THEME_MAPPING_EMPTY", "THEME_MAPPING_EMPTY"])
    window, db, _, _ = make_window(tmp_path, qapp, runtime)

    window.start_observe_strategy()
    wait_until(qapp, lambda: window._strategy_running)

    readiness_text = window.strategy_readiness_view.toPlainText()
    warning_text = window.strategy_warning_view.toPlainText()
    assert window.strategy_readiness_view.isReadOnly()
    assert "conditions=3 unresolved=1" in readiness_text
    assert "themes=0 enabled=0" in readiness_text
    assert "active candidates=2 mapped=0 unmapped=2" in readiness_text
    assert "protected subs=4/85" in readiness_text
    assert "flow: market=" in readiness_text
    assert "candidate subscriptions:" in readiness_text
    assert warning_text.splitlines().count("THEME_MAPPING_EMPTY") == 1
    db.close()
    window.close()


def test_readiness_summary_render_is_read_only(tmp_path, qapp, monkeypatch):
    runtime = FakeRuntime()
    window, db, _, _ = make_window(tmp_path, qapp, runtime)
    monkeypatch.setattr(runtime, "cycle", lambda: (_ for _ in ()).throw(AssertionError("readiness display must not cycle")))
    monkeypatch.setattr(db, "save_candidate", lambda candidate: (_ for _ in ()).throw(AssertionError("readiness display must not save candidate")))
    monkeypatch.setattr(db, "save_trade_review", lambda review: (_ for _ in ()).throw(AssertionError("readiness display must not save review")))
    monkeypatch.setattr(db, "save_virtual_order", lambda order: (_ for _ in ()).throw(AssertionError("readiness display must not save order")))

    window._display_strategy_snapshot(
        StrategyRuntimeSnapshot(
            started=True,
            active_candidate_count=1,
            condition_profiles_count=3,
            theme_mappings_count=1,
            enabled_theme_mappings_count=1,
            active_candidates_with_theme_mapping=1,
            protected_subscription_usage="4/85",
        ),
        0.0,
    )

    assert "readiness:" in window.strategy_readiness_view.toPlainText()
    db.close()
    window.close()


def test_start_failure_does_not_start_timer(tmp_path, qapp):
    runtime = FakeRuntime(start_fail=True)
    window, db, _, _ = make_window(tmp_path, qapp, runtime)

    window.start_observe_strategy()
    wait_until(qapp, lambda: runtime.start_calls == 1 and not window._strategy_starting)

    assert runtime.start_calls == 1
    assert not window.strategy_timer.isActive()
    assert "STRATEGY_RUNTIME_START_FAILED" in window.strategy_warning_view.toPlainText()
    db.close()
    window.close()


def test_stop_failure_still_stops_timer_and_marks_stopped(tmp_path, qapp):
    runtime = FakeRuntime(stop_fail=True)
    window, db, _, _ = make_window(tmp_path, qapp, runtime)

    window.start_observe_strategy()
    wait_until(qapp, lambda: window._strategy_running)
    assert window.strategy_timer.isActive()
    window.stop_observe_strategy()

    assert runtime.stop_calls == 1
    assert not window.strategy_timer.isActive()
    assert not window._strategy_running
    assert "STRATEGY_RUNTIME_STOP_FAILED" in window.strategy_warning_view.toPlainText()
    db.close()
    window.close()


def test_timer_cycle_reentry_guard_and_exception_warning(tmp_path, qapp):
    runtime = FakeRuntime()
    window, db, _, _ = make_window(tmp_path, qapp, runtime)
    window.start_observe_strategy()
    wait_until(qapp, lambda: window._strategy_running)

    window._strategy_cycle_running = True
    window._run_strategy_cycle()
    assert runtime.cycle_calls == 0
    assert "STRATEGY_CYCLE_REENTRY_SKIPPED" in window.strategy_warning_view.toPlainText()

    window._strategy_cycle_running = False
    runtime.cycle_fail = True
    window._run_strategy_cycle()
    wait_until(qapp, lambda: not window._strategy_cycle_running)
    assert runtime.cycle_calls == 1
    assert "STRATEGY_CYCLE_FAILED" in window.strategy_warning_view.toPlainText()
    db.close()
    window.close()


def test_strategy_cycle_throttles_candidate_table_auto_refresh(tmp_path, qapp):
    runtime = QuietRuntime()
    window, db, _, _ = make_window(tmp_path, qapp, runtime)
    window.start_observe_strategy()
    wait_until(qapp, lambda: window._strategy_running)
    refresh_calls = []
    window.refresh_strategy_candidates = lambda: refresh_calls.append("refresh")
    window._strategy_last_auto_refresh_at = perf_counter()
    window._strategy_last_snapshot = StrategyRuntimeSnapshot(started=True, active_candidate_count=2)

    window._run_strategy_cycle()
    wait_until(qapp, lambda: not window._strategy_cycle_running)
    window._run_strategy_cycle()
    wait_until(qapp, lambda: not window._strategy_cycle_running)

    assert runtime.cycle_calls == 2
    assert refresh_calls == []
    assert window._strategy_ui_refresh_skipped_count >= 2
    db.close()
    window.close()


def test_candidate_refresh_is_today_limited_sorted_and_read_only(tmp_path, qapp, monkeypatch):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())
    save_candidate(db, "111111", CandidateState.WATCHING, NOW + timedelta(minutes=1), {"sub_status": "WATCH"})
    save_candidate(
        db,
        "222222",
        CandidateState.READY,
        NOW,
        {
            "best_theme_id": "robot",
            "best_gate_result_key": "g1",
            "sub_status": "PASS",
            "block_reasons_by_theme": {"robot": {"reason_codes": ["OK"]}},
        },
    )
    save_candidate(db, "333333", CandidateState.READY, NOW + timedelta(minutes=2))
    db.save_candidate(
        Candidate(
            trade_date="2026-05-28",
            code="999999",
            state=CandidateState.READY,
            last_seen_at=NOW.isoformat(),
        )
    )
    window._strategy_trade_date = lambda: "2026-05-29"

    monkeypatch.setattr(window.strategy_runtime, "cycle", lambda: (_ for _ in ()).throw(AssertionError("refresh must not cycle")))
    monkeypatch.setattr(db, "save_trade_review", lambda review: (_ for _ in ()).throw(AssertionError("refresh must not save review")))
    monkeypatch.setattr(db, "save_candidate", lambda candidate: (_ for _ in ()).throw(AssertionError("refresh must not save candidate")))
    monkeypatch.setattr(db, "save_virtual_order", lambda order: (_ for _ in ()).throw(AssertionError("refresh must not save order")))

    window.refresh_strategy_candidates()

    assert candidate_table_row_count(window) == 3
    assert candidate_table_text(window, 0, 0) == "333333"
    assert candidate_table_text(window, 1, 0) == "222222"
    assert candidate_table_text(window, 1, 5) == "robot"
    assert candidate_table_text(window, 1, 7) == "PASS"
    assert "999999" not in [candidate_table_text(window, row, 0) for row in range(3)]
    db.close()
    window.close()


def test_manual_candidate_refresh_runs_immediately(tmp_path, qapp, monkeypatch):
    window, db, _, _ = make_window(tmp_path, qapp, QuietRuntime())
    save_candidate(db, "111111", CandidateState.WATCHING, NOW)
    calls = []
    original = db.list_candidates

    def wrapped_list_candidates(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "list_candidates", wrapped_list_candidates)

    window.refresh_strategy_candidates()

    assert calls
    assert candidate_table_row_count(window) == 1
    db.close()
    window.close()


def test_metadata_parse_error_does_not_break_candidate_refresh(tmp_path, qapp):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())
    candidate = save_candidate(db, "111111", CandidateState.WATCHING, NOW)
    candidate.metadata = "broken"
    db.load_candidate = lambda *args, **kwargs: candidate
    window._strategy_trade_date = lambda: "2026-05-29"
    original = db.list_candidates
    db.list_candidates = lambda *args, **kwargs: [candidate] if kwargs.get("trade_date") == "2026-05-29" else original(*args, **kwargs)

    window.refresh_strategy_candidates()

    assert candidate_table_row_count(window) == 1
    assert "CANDIDATE_METADATA_INVALID:111111" in window.strategy_warning_view.toPlainText()
    db.close()
    window.close()


def test_candidate_filters_are_proxy_only_and_cover_state_search_recover_theme(tmp_path, qapp, monkeypatch):
    window, db, client, engine = make_window(tmp_path, qapp, FakeRuntime())
    ThemeRepository(db).upsert_mapping(
        ThemeMapping(
            code="111111",
            name="Ready Robot",
            theme_id="robot",
            theme_name="Robot Theme",
            enabled=True,
        )
    )
    save_candidate(
        db,
        "111111",
        CandidateState.READY,
        NOW + timedelta(minutes=4),
        {"best_theme_id": "robot", "best_gate_result_key": "gate-a", "sub_status": "PASS"},
        name="Ready Robot",
        theme_ids=["robot"],
    )
    save_candidate(
        db,
        "222222",
        CandidateState.BLOCKED,
        NOW + timedelta(minutes=3),
        {"block_reasons_by_theme": {"robot": {"reason_codes": ["BLOCK_REASON"]}}},
        name="Blocked Recover",
        can_recover=True,
    )
    save_candidate(db, "333333", CandidateState.WATCHING, NOW + timedelta(minutes=2), name="Watch Search")
    save_candidate(db, "444444", CandidateState.DETECTED, NOW + timedelta(minutes=1), name="Detected Name")
    save_candidate(
        db,
        "555555",
        CandidateState.WATCHING,
        NOW + timedelta(minutes=5),
        {
            "condition_purposes": {"주도테마_넓은후보": "theme_broad_candidate"},
            "entry_condition_names": [],
            "entry_excluded": True,
        },
        name="Discovery Only",
    )
    window.refresh_strategy_candidates()

    monkeypatch.setattr(window.strategy_runtime, "cycle", lambda: (_ for _ in ()).throw(AssertionError("filter must not cycle")))
    monkeypatch.setattr(db, "save_candidate", lambda candidate: (_ for _ in ()).throw(AssertionError("filter must not save candidate")))
    monkeypatch.setattr(db, "save_trade_review", lambda review: (_ for _ in ()).throw(AssertionError("filter must not save review")))
    monkeypatch.setattr(db, "save_virtual_order", lambda order: (_ for _ in ()).throw(AssertionError("filter must not save virtual order")))
    monkeypatch.setattr(db, "save_virtual_position", lambda position: (_ for _ in ()).throw(AssertionError("filter must not save virtual position")))
    monkeypatch.setattr(client, "send_order", lambda request: (_ for _ in ()).throw(AssertionError("filter must not call order path")))
    monkeypatch.setattr(engine, "register_realtime", lambda: (_ for _ in ()).throw(AssertionError("filter must not register realtime")))

    assert "555555" not in candidate_table_codes(window)

    window.strategy_candidate_filter_bar.quality_combo.setCurrentIndex(
        window.strategy_candidate_filter_bar.quality_combo.findData("ALL")
    )
    assert "555555" in candidate_table_codes(window)

    window.strategy_candidate_filter_bar.clear_filters()
    assert "555555" not in candidate_table_codes(window)

    window.strategy_candidate_filter_bar.state_combo.setCurrentText("READY")
    assert candidate_table_codes(window) == ["111111"]

    window.strategy_candidate_filter_bar.clear_filters()
    window.strategy_candidate_filter_bar.search_edit.setText("Watch Search")
    wait_for_filter_debounce(qapp)
    assert candidate_table_codes(window) == ["333333"]

    window.strategy_candidate_filter_bar.clear_filters()
    window.strategy_candidate_filter_bar.search_edit.setText("robot")
    wait_for_filter_debounce(qapp)
    assert candidate_table_codes(window) == ["111111"]

    window.strategy_candidate_filter_bar.search_edit.setText("Robot Theme")
    wait_for_filter_debounce(qapp)
    assert candidate_table_codes(window) == ["111111"]

    window.strategy_candidate_filter_bar.clear_filters()
    window.strategy_candidate_filter_bar.search_edit.setText("BLOCK_REASON")
    wait_for_filter_debounce(qapp)
    assert candidate_table_codes(window) == ["222222"]

    window.strategy_candidate_filter_bar.clear_filters()
    window.strategy_candidate_filter_bar.recover_combo.setCurrentIndex(1)
    assert candidate_table_codes(window) == ["222222"]

    window.strategy_candidate_filter_bar.clear_filters()
    window.strategy_candidate_filter_bar.theme_combo.setCurrentIndex(1)
    assert candidate_table_codes(window) == ["111111"]

    window.strategy_candidate_filter_bar.theme_combo.setCurrentIndex(2)
    assert candidate_table_codes(window) == ["222222", "333333", "444444"]

    window.strategy_candidate_filter_bar.clear_filters()
    window.strategy_candidate_filter_bar.quality_combo.setCurrentIndex(
        window.strategy_candidate_filter_bar.quality_combo.findData("actionable")
    )
    assert candidate_table_codes(window) == ["111111"]

    window.strategy_candidate_filter_bar.quality_combo.setCurrentIndex(
        window.strategy_candidate_filter_bar.quality_combo.findData("unmapped")
    )
    assert candidate_table_codes(window) == ["222222", "333333", "444444"]

    window.strategy_candidate_filter_bar.quality_combo.setCurrentIndex(
        window.strategy_candidate_filter_bar.quality_combo.findData("discovery_only")
    )
    assert candidate_table_codes(window) == ["555555"]
    db.close()
    window.close()


def test_candidate_detail_panel_displays_read_only_related_records(tmp_path, qapp, monkeypatch):
    runtime = FakeRuntime()
    window, db, client, engine = make_window(tmp_path, qapp, runtime)
    ThemeRepository(db).upsert_mapping(
        ThemeMapping(
            code="111111",
            name="Ready Robot",
            market="KOSDAQ",
            theme_id="robot",
            theme_name="Robot Theme",
            sub_theme="Automation",
            strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
            is_leader_candidate=True,
            is_signal_stock=True,
            base_priority=100,
            enabled=True,
        )
    )
    candidate = save_candidate(
        db,
        "111111",
        CandidateState.READY,
        NOW,
        {
            "best_theme_id": "robot",
            "best_gate_result_key": "gate-a",
            "sub_status": "PASS",
            "block_reasons_by_theme": {"robot": {"reason_codes": ["OK"]}},
        },
        name="Ready Robot",
        theme_ids=["robot"],
    )
    db.save_candidate_event(
        CandidateEvent(
            candidate_id=candidate.id,
            event_type="state",
            from_state=CandidateState.DETECTED,
            to_state=CandidateState.READY,
            source=CandidateSourceType.CONDITION,
            reason="passed gate",
            created_at=NOW.isoformat(),
            payload={"note": "sample"},
        )
    )
    db.save_indicator_snapshot(
        IndicatorSnapshot(
            candidate_id=candidate.id,
            code="111111",
            created_at=NOW.isoformat(),
            price=12345,
            vwap=12300.0,
            ema20_5m=12250.0,
            base_line_120=12000.0,
            envelope_mid=12100.0,
            day_high=13000,
            day_low=12000,
            day_mid=12500.0,
            pullback_pct=-1.25,
            volume_reaccel=True,
            failed_low_break_rebound=True,
            chase_risk=False,
        )
    )
    plan = db.save_entry_plan(
        EntryPlan(
            candidate_id=candidate.id,
            entry_type="pullback",
            base_price_source="vwap",
            limit_price=12300,
            tick_offset=-1,
            max_chase_pct=1.5,
            split_plan=[{"weight": 50}],
            fill_policy=FillPolicy.NORMAL,
            created_at=NOW.isoformat(),
        )
    )
    virtual_order = db.save_virtual_order(
        VirtualOrder(
            candidate_id=candidate.id,
            entry_plan_id=plan.id,
            status=VirtualOrderStatus.SUBMITTED,
            limit_price=12300,
            virtual_fill_price=0,
            fill_policy=FillPolicy.NORMAL,
            submitted_at=NOW.isoformat(),
        )
    )
    db.save_virtual_position(
        VirtualPosition(
            candidate_id=candidate.id,
            virtual_order_id=virtual_order.id,
            entry_price=12300,
            quantity=3,
            opened_at=NOW.isoformat(),
            max_return_pct=2.2,
            max_drawdown_pct=-0.5,
            realized_return_pct=0.0,
        )
    )
    db.save_trade_review(
        TradeReview(
            candidate_id=candidate.id,
            trade_date="2026-05-29",
            code="111111",
            final_status=ReviewFinalStatus.VIRTUAL_SUBMITTED.value,
            max_return_5m=1.2,
            max_return_10m=1.8,
            max_return_20m=2.5,
            max_drawdown_20m=-0.4,
            missed_reason="none",
            created_at=NOW.isoformat(),
        )
    )
    window.refresh_strategy_candidates()

    monkeypatch.setattr(runtime, "cycle", lambda: (_ for _ in ()).throw(AssertionError("detail must not cycle")))
    monkeypatch.setattr(db, "save_candidate", lambda item: (_ for _ in ()).throw(AssertionError("detail must not save candidate")))
    monkeypatch.setattr(db, "save_trade_review", lambda item: (_ for _ in ()).throw(AssertionError("detail must not save review")))
    monkeypatch.setattr(db, "save_virtual_order", lambda item: (_ for _ in ()).throw(AssertionError("detail must not save virtual order")))
    monkeypatch.setattr(db, "save_virtual_position", lambda item: (_ for _ in ()).throw(AssertionError("detail must not save virtual position")))
    monkeypatch.setattr(client, "send_order", lambda request: (_ for _ in ()).throw(AssertionError("detail must not call order path")))
    monkeypatch.setattr(engine, "register_realtime", lambda: (_ for _ in ()).throw(AssertionError("detail must not register realtime")))

    select_candidate_row(window, 0)
    window._display_selected_candidate_detail()

    detail_text = window.strategy_candidate_detail_panel.text()
    assert "code: 111111" in detail_text
    assert "state: READY" in detail_text
    assert "quality_status: actionable" in detail_text
    assert "enabled_theme_mapping: Y" in detail_text
    assert "entry_evaluation_target: Y" in detail_text
    assert "subscription_excluded_reason: -" in detail_text
    assert "theme_id=robot" in detail_text
    assert "price: 12,345" in detail_text
    assert "entry_type=pullback" in detail_text
    assert "status=submitted" in detail_text
    assert "final_status=VIRTUAL_SUBMITTED" in detail_text
    assert "passed gate" in detail_text
    db.close()
    window.close()


def test_candidate_selection_survives_refresh_until_candidate_disappears(tmp_path, qapp):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())
    candidate_a = save_candidate(db, "111111", CandidateState.READY, NOW + timedelta(minutes=2), name="Candidate A")
    save_candidate(db, "222222", CandidateState.BLOCKED, NOW + timedelta(minutes=1), name="Candidate B")
    window.refresh_strategy_candidates()

    select_candidate_row(window, candidate_table_row_for_code(window, "111111"))
    window._display_selected_candidate_detail()
    assert "code: 111111" in window.strategy_candidate_detail_panel.text()

    window.refresh_strategy_candidates()

    assert window._selected_strategy_candidate_id() == candidate_a.id
    assert "code: 111111" in window.strategy_candidate_detail_panel.text()

    with db.conn:
        db.conn.execute("UPDATE candidates SET trade_date = ? WHERE id = ?", ("2026-05-28", candidate_a.id))
    window.refresh_strategy_candidates()

    assert "선택된 후보 없음" in window.strategy_candidate_detail_panel.text()
    assert candidate_table_codes(window) == ["222222"]
    db.close()
    window.close()


def test_candidate_model_keeps_proxy_display_sorting_and_limit_contract(tmp_path, qapp):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())
    for index in range(205):
        save_candidate(
            db,
            f"{index:06d}",
            CandidateState.DETECTED,
            NOW + timedelta(seconds=index),
        )
    save_candidate(db, "900001", CandidateState.WATCHING, NOW + timedelta(minutes=1))
    save_candidate(db, "900002", CandidateState.BLOCKED, NOW + timedelta(minutes=2))
    save_candidate(db, "900003", CandidateState.READY, NOW + timedelta(minutes=3))
    save_candidate(db, "900004", CandidateState.READY, NOW + timedelta(minutes=4))

    window.refresh_strategy_candidates()

    assert candidate_table_row_count(window) == 200
    assert candidate_table_text(window, 0, 0) == "900004"
    assert candidate_table_text(window, 1, 0) == "900003"
    assert candidate_table_text(window, 2, 0) == "900002"
    assert candidate_table_text(window, 3, 0) == "900001"
    db.close()
    window.close()


def test_main_builds_optional_observe_runtime_without_order_path(tmp_path, monkeypatch):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    client = MockKiwoomClient()
    calls = []
    monkeypatch.setattr(client, "send_order", lambda request: calls.append(request))

    runtime = build_observe_runtime(client, db)
    snapshot = runtime.start(NOW)

    assert runtime.config.order_mode.value == "OBSERVE"
    assert snapshot.started is True
    assert calls == []
    assert client.orders == []
    db.close()


def test_strategy_settings_load_save_is_db_only_and_next_start(tmp_path, qapp, monkeypatch):
    runtime = FakeRuntime()
    window, db, _, _ = make_window(tmp_path, qapp, runtime)
    StrategyRuntimeConfigRepository(db).save(StrategyRuntimeConfig(evaluation_interval_sec=4))
    window.load_strategy_settings()

    assert window.config_interval_spin.value() == 4

    window.start_observe_strategy()
    wait_until(qapp, lambda: window._strategy_running)
    original_interval = window.strategy_timer.interval()
    window.config_interval_spin.setValue(9)
    monkeypatch.setattr(runtime, "cycle", lambda: (_ for _ in ()).throw(AssertionError("settings save must not cycle")))
    monkeypatch.setattr(db, "save_trade_review", lambda review: (_ for _ in ()).throw(AssertionError("settings save must not save review")))

    window.save_strategy_settings()

    assert runtime.config.evaluation_interval_sec == 1
    assert window.strategy_timer.interval() == original_interval
    assert StrategyRuntimeConfigRepository(db).load().config.evaluation_interval_sec == 9
    assert "CONFIG_SAVED_FOR_NEXT_OBSERVE_START" in window.strategy_settings_warning_view.toPlainText()
    db.close()
    window.close()


def test_strategy_settings_invalid_save_keeps_existing_config(tmp_path, qapp):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())
    repo = StrategyRuntimeConfigRepository(db)
    repo.save(StrategyRuntimeConfig(evaluation_interval_sec=6))
    window.load_strategy_settings()
    window.config_leader_codes_edit.setText("KOSPI")

    window.save_strategy_settings()

    assert repo.load().config.evaluation_interval_sec == 6
    assert "CONFIG_SAVE_FAILED" in window.strategy_settings_warning_view.toPlainText()
    db.close()
    window.close()


def test_strategy_settings_show_runtime_unavailable_reason(tmp_path, qapp):
    window, db, _, _ = make_window(tmp_path, qapp, None)
    window.strategy_runtime_unavailable_reason = "runtime disabled for test"

    window.load_strategy_settings()
    window.start_observe_strategy()

    assert "runtime unavailable" in window.strategy_settings_pending_label.text()
    assert "runtime disabled for test" in window.strategy_warning_view.toPlainText()
    db.close()
    window.close()


def test_strategy_settings_groups_render_core_fields(tmp_path, qapp):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())

    titles = {group.title() for group in window.findChildren(QGroupBox)}

    assert {"실행 주기", "조건식/후보", "지수 감시", "테마/대장주 감시", "가상 체결/리뷰", "실시간 구독", "안전 설정"} <= titles
    assert window.config_interval_spin.value() > 0
    assert window.config_leader_codes_edit is not None
    assert window.config_fill_policy_combo.count() == 3
    assert "no real orders" in window.strategy_settings_safety_label.text()
    db.close()
    window.close()


def test_strategy_settings_validation_blocks_invalid_numeric_values(tmp_path, qapp):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())
    repo = StrategyRuntimeConfigRepository(db)
    repo.save(StrategyRuntimeConfig(evaluation_interval_sec=6, max_candidates_to_watch=50))
    window.load_strategy_settings()

    window.config_interval_spin.setValue(0)
    window.config_max_candidates_spin.setValue(0)
    window.config_realtime_limit_spin.setValue(0)
    window.save_strategy_settings()

    loaded = repo.load().config
    assert loaded.evaluation_interval_sec == 6
    assert loaded.max_candidates_to_watch == 50
    assert "CONFIG_SAVE_FAILED" in window.strategy_settings_warning_view.toPlainText()
    assert window.strategy_settings_validation_badge.tone == "danger"
    db.close()
    window.close()


def test_strategy_settings_code_list_normalizes_duplicates(tmp_path, qapp):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())
    repo = StrategyRuntimeConfigRepository(db)
    repo.save(StrategyRuntimeConfig())
    window.load_strategy_settings()

    window.config_leader_codes_edit.setText("005930, 000660\n005930 000660")
    window.save_strategy_settings()

    loaded = repo.load().config
    assert loaded.leader_watch_codes == ["005930", "000660"]
    assert "duplicate codes normalized" in window.strategy_settings_warning_view.toPlainText()
    db.close()
    window.close()


def test_strategy_settings_saved_active_diff_and_dirty_state(tmp_path, qapp):
    runtime = FakeRuntime(config=StrategyRuntimeConfig(evaluation_interval_sec=3))
    window, db, _, _ = make_window(tmp_path, qapp, runtime)
    StrategyRuntimeConfigRepository(db).save(StrategyRuntimeConfig(evaluation_interval_sec=5))

    window.load_strategy_settings()

    assert "saved config differs from active runtime" in window.strategy_settings_pending_label.text()
    assert "evaluation_interval_sec" in window.strategy_settings_diff_view.toPlainText()

    window.config_interval_spin.setValue(7)

    assert "변경사항 있음" in window.strategy_settings_status_label.text()
    assert window.strategy_settings_status_cards["saved"].tone == "warning"

    window.save_strategy_settings()

    assert "변경사항 없음" in window.strategy_settings_status_label.text()
    assert StrategyRuntimeConfigRepository(db).load().config.evaluation_interval_sec == 7
    db.close()
    window.close()


def test_strategy_settings_json_export_import_and_default_restore(tmp_path, qapp, monkeypatch):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())
    repo = StrategyRuntimeConfigRepository(db)
    repo.save(StrategyRuntimeConfig(evaluation_interval_sec=8, leader_watch_codes=["005930"]))
    window.load_strategy_settings()

    export_path = tmp_path / "strategy_config.json"
    import_path = tmp_path / "strategy_config_import.json"
    import_path.write_text(
        json.dumps(config_to_dict(StrategyRuntimeConfig(evaluation_interval_sec=12, leader_watch_codes=["000660"]))),
        encoding="utf-8",
    )
    monkeypatch.setattr("ui.main_window.QFileDialog.getSaveFileName", lambda *args, **kwargs: (str(export_path), ""))
    monkeypatch.setattr("ui.main_window.QFileDialog.getOpenFileName", lambda *args, **kwargs: (str(import_path), ""))

    window.export_strategy_settings_json()
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert exported["evaluation_interval_sec"] == 8

    window.import_strategy_settings_json()

    assert window.config_interval_spin.value() == 12
    assert repo.load().config.evaluation_interval_sec == 8
    assert "CONFIG_IMPORT_PREVIEW_NOT_SAVED" in window.strategy_settings_warning_view.toPlainText()

    window.save_strategy_settings()
    assert repo.load().config.evaluation_interval_sec == 12

    monkeypatch.setattr("ui.main_window.QMessageBox.question", lambda *args, **kwargs: QMessageBox.Yes)
    window.restore_default_strategy_settings()

    assert window.config_interval_spin.value() == StrategyRuntimeConfig().evaluation_interval_sec
    assert repo.load().config.evaluation_interval_sec == 12
    assert "CONFIG_DEFAULT_PREVIEW_NOT_SAVED" in window.strategy_settings_warning_view.toPlainText()
    db.close()
    window.close()


def test_strategy_settings_invalid_import_keeps_ui_and_config(tmp_path, qapp, monkeypatch):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())
    repo = StrategyRuntimeConfigRepository(db)
    repo.save(StrategyRuntimeConfig(evaluation_interval_sec=9))
    window.load_strategy_settings()
    invalid_path = tmp_path / "invalid_strategy_config.json"
    invalid_path.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr("ui.main_window.QFileDialog.getOpenFileName", lambda *args, **kwargs: (str(invalid_path), ""))

    window.import_strategy_settings_json()

    assert window.config_interval_spin.value() == 9
    assert repo.load().config.evaluation_interval_sec == 9
    assert "CONFIG_IMPORT_FAILED" in window.strategy_settings_warning_view.toPlainText()
    db.close()
    window.close()


def test_strategy_settings_render_diff_import_export_are_read_only(tmp_path, qapp, monkeypatch):
    runtime = FakeRuntime(config=StrategyRuntimeConfig())
    window, db, client, engine = make_window(tmp_path, qapp, runtime)
    export_path = tmp_path / "settings_read_only.json"
    import_path = tmp_path / "settings_read_only_import.json"
    import_path.write_text(json.dumps(config_to_dict(StrategyRuntimeConfig(evaluation_interval_sec=11))), encoding="utf-8")
    monkeypatch.setattr("ui.main_window.QFileDialog.getSaveFileName", lambda *args, **kwargs: (str(export_path), ""))
    monkeypatch.setattr("ui.main_window.QFileDialog.getOpenFileName", lambda *args, **kwargs: (str(import_path), ""))
    monkeypatch.setattr("ui.main_window.QMessageBox.question", lambda *args, **kwargs: QMessageBox.Yes)
    monkeypatch.setattr(runtime, "cycle", lambda: (_ for _ in ()).throw(AssertionError("settings UI must not cycle")))
    monkeypatch.setattr(runtime, "start", lambda: (_ for _ in ()).throw(AssertionError("settings UI must not start runtime")))
    monkeypatch.setattr(runtime, "stop", lambda: (_ for _ in ()).throw(AssertionError("settings UI must not stop runtime")))
    monkeypatch.setattr(db, "save_candidate", lambda candidate: (_ for _ in ()).throw(AssertionError("settings UI must not save candidate")))
    monkeypatch.setattr(db, "save_trade_review", lambda review: (_ for _ in ()).throw(AssertionError("settings UI must not save review")))
    monkeypatch.setattr(db, "save_virtual_order", lambda order: (_ for _ in ()).throw(AssertionError("settings UI must not save virtual order")))
    monkeypatch.setattr(db, "save_virtual_position", lambda position: (_ for _ in ()).throw(AssertionError("settings UI must not save virtual position")))
    monkeypatch.setattr(engine, "register_realtime", lambda: (_ for _ in ()).throw(AssertionError("settings UI must not register realtime")))
    monkeypatch.setattr(client, "send_order", lambda request: (_ for _ in ()).throw(AssertionError("settings UI must not call order path")))

    window.load_strategy_settings()
    window.config_interval_spin.setValue(10)
    window.show_strategy_settings_diff()
    window.export_strategy_settings_json()
    window.import_strategy_settings_json()
    window.restore_default_strategy_settings()

    assert export_path.exists()
    assert window.config_interval_spin.value() == StrategyRuntimeConfig().evaluation_interval_sec
    db.close()
    window.close()


def test_observe_dashboard_card_marks_runtime_unavailable(tmp_path, qapp):
    window, db, _, _ = make_window(tmp_path, qapp, None)
    window.strategy_runtime_unavailable_reason = "runtime disabled for test"

    window._update_strategy_buttons()

    card = window.dashboard_cards["observe"]
    assert card.tone == "unavailable"
    assert card.value_label.text() == "unavailable"
    assert "runtime disabled for test" in card.detail_label.text()
    db.close()
    window.close()


def test_live_mode_ordering_enabled_marks_order_cards_danger(tmp_path, qapp):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    client = MockKiwoomClient()
    engine = TradingEngine(client=client, db=db)
    window = MainWindow(engine=engine, db=db, mock_mode=False, strategy_runtime=FakeRuntime())

    window.ordering_check.setChecked(True)

    assert window.dashboard_cards["mode"].tone == "danger"
    assert window.dashboard_cards["ordering"].tone == "danger"
    db.close()
    window.close()


def test_dashboard_update_is_read_only(tmp_path, qapp, monkeypatch):
    runtime = FakeRuntime()
    window, db, client, _ = make_window(tmp_path, qapp, runtime)

    monkeypatch.setattr(runtime, "cycle", lambda: (_ for _ in ()).throw(AssertionError("dashboard update must not cycle")))
    monkeypatch.setattr(db, "save_candidate", lambda candidate: (_ for _ in ()).throw(AssertionError("dashboard update must not save candidate")))
    monkeypatch.setattr(db, "save_trade_review", lambda review: (_ for _ in ()).throw(AssertionError("dashboard update must not save review")))
    monkeypatch.setattr(db, "save_virtual_order", lambda order: (_ for _ in ()).throw(AssertionError("dashboard update must not save virtual order")))
    monkeypatch.setattr(client, "send_order", lambda request: (_ for _ in ()).throw(AssertionError("dashboard update must not call order path")))
    monkeypatch.setattr(window, "refresh_strategy_candidates", lambda: (_ for _ in ()).throw(AssertionError("dashboard update must not refresh candidates")))

    window._update_dashboard(
        StrategyRuntimeSnapshot(
            started=True,
            active_candidate_count=2,
            theme_mapping_coverage_pct=50.0,
            protected_subscription_usage="4/85",
        )
    )

    assert window.dashboard_cards["observe"].value_label.text() == "running"
    assert window.dashboard_cards["candidates"].value_label.text() == "2"
    db.close()
    window.close()


def test_review_model_displays_headers_and_numeric_sort(tmp_path, qapp):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())
    save_review(db, "111111", ReviewFinalStatus.VIRTUAL_SUBMITTED.value, "A", max_return_5m=10.0, max_return_10m=1.0, max_return_20m=2.0, max_drawdown_20m=-1.0)
    save_review(db, "222222", ReviewFinalStatus.BLOCKED_TEMP.value, "B", max_return_5m=2.0, max_return_10m=3.0, max_return_20m=4.0, max_drawdown_20m=-8.0)
    save_review(db, "333333", ReviewFinalStatus.EXPIRED.value, "C", max_return_5m=-1.0, max_return_10m=-2.0, max_return_20m=-3.0, max_drawdown_20m=-2.0)

    window.refresh_review_table()

    assert review_table_row_count(window) == 3
    row = review_table_row_for_code(window, "111111")
    assert review_table_text(window, row, 0) == NOW.isoformat()
    assert review_table_text(window, row, 1) == "111111"
    assert review_table_text(window, row, 4) == "Robot Theme"
    assert review_table_text(window, row, 5) == "A"
    assert review_table_numeric(window, row, 9) == 10.0

    window.review_table.sortByColumn(9, Qt.AscendingOrder)
    assert review_table_codes(window) == ["333333", "222222", "111111"]
    window.review_table.sortByColumn(9, Qt.DescendingOrder)
    assert review_table_codes(window) == ["111111", "222222", "333333"]
    db.close()
    window.close()


def test_review_filters_are_proxy_only(tmp_path, qapp, monkeypatch):
    runtime = FakeRuntime()
    window, db, client, engine = make_window(tmp_path, qapp, runtime)
    save_review(db, "111111", ReviewFinalStatus.VIRTUAL_SUBMITTED.value, "A", max_return_20m=2.0, missed_reason="", virtual_order_status="submitted")
    save_review(db, "222222", ReviewFinalStatus.BLOCKED_TEMP.value, "B", missed_reason="THEME_UNMAPPED", false_negative_flag=True, details={"false_negative_type": "LATE_CHASE"})
    save_review(db, "333333", ReviewFinalStatus.EXPIRED.value, "C", false_positive_flag=True, exit_reason="TIME_EXIT", details={"false_positive_type": "FAKE_BREAK"})
    window.refresh_review_table()

    monkeypatch.setattr(runtime, "cycle", lambda: (_ for _ in ()).throw(AssertionError("review filter must not cycle")))
    monkeypatch.setattr(db, "save_candidate", lambda item: (_ for _ in ()).throw(AssertionError("review filter must not save candidate")))
    monkeypatch.setattr(db, "save_trade_review", lambda item: (_ for _ in ()).throw(AssertionError("review filter must not save review")))
    monkeypatch.setattr(db, "save_virtual_order", lambda item: (_ for _ in ()).throw(AssertionError("review filter must not save virtual order")))
    monkeypatch.setattr(db, "save_virtual_position", lambda item: (_ for _ in ()).throw(AssertionError("review filter must not save virtual position")))
    monkeypatch.setattr(client, "send_order", lambda request: (_ for _ in ()).throw(AssertionError("review filter must not call order path")))
    monkeypatch.setattr(engine, "register_realtime", lambda: (_ for _ in ()).throw(AssertionError("review filter must not register realtime")))

    window.review_filter_bar.status_combo.setCurrentText("blocked")
    assert review_table_codes(window) == ["222222"]

    window.review_filter_bar.clear_filters()
    window.review_filter_bar.grade_combo.setCurrentText("A")
    assert review_table_codes(window) == ["111111"]

    window.review_filter_bar.clear_filters()
    window.review_filter_bar.fn_combo.setCurrentIndex(1)
    assert review_table_codes(window) == ["222222"]

    window.review_filter_bar.clear_filters()
    window.review_filter_bar.fp_combo.setCurrentIndex(1)
    assert review_table_codes(window) == ["333333"]

    window.review_filter_bar.clear_filters()
    window.review_filter_bar.search_edit.setText("THEME_UNMAPPED")
    wait_for_filter_debounce(qapp)
    assert review_table_codes(window) == ["222222"]

    window.review_filter_bar.search_edit.setText("FAKE_BREAK")
    wait_for_filter_debounce(qapp)
    assert review_table_codes(window) == ["333333"]
    db.close()
    window.close()


def test_review_date_filter(tmp_path, qapp):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())
    save_review(db, "111111", ReviewFinalStatus.VIRTUAL_SUBMITTED.value, trade_date="2026-05-29", created_at="2026-05-29T09:00:00")
    save_review(db, "222222", ReviewFinalStatus.BLOCKED_TEMP.value, trade_date="2026-05-25", created_at="2026-05-25T09:00:00")
    save_review(db, "333333", ReviewFinalStatus.EXPIRED.value, trade_date="2026-05-20", created_at="2026-05-20T09:00:00")
    window.refresh_review_table()

    window.review_filter_bar.date_range_combo.setCurrentText("오늘")
    assert review_table_codes(window) == ["111111"]

    window.review_filter_bar.date_range_combo.setCurrentText("최근 7일")
    assert review_table_codes(window) == ["111111", "222222"]

    window.review_filter_bar.date_range_combo.setCurrentText("전체")
    assert review_table_codes(window) == ["111111", "222222", "333333"]
    db.close()
    window.close()


def test_review_summary_cards_and_missed_reason_top_follow_filters(tmp_path, qapp):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())
    save_review(db, "111111", ReviewFinalStatus.VIRTUAL_SUBMITTED.value, "A", max_return_5m=2.0, max_return_10m=4.0, max_return_20m=6.0, max_drawdown_20m=-1.0)
    save_review(db, "222222", ReviewFinalStatus.BLOCKED_TEMP.value, "B", max_return_5m=4.0, max_return_10m=6.0, max_return_20m=8.0, max_drawdown_20m=-3.0, missed_reason="LATE_CHASE", false_negative_flag=True, blocked_but_later_rallied=True)
    save_review(db, "333333", ReviewFinalStatus.EXPIRED.value, "C", max_return_5m=6.0, max_return_10m=8.0, max_return_20m=10.0, max_drawdown_20m=-5.0, missed_reason="THEME_UNMAPPED", false_positive_flag=True, expired_but_later_rallied=True)
    window.refresh_review_table()

    assert window.review_summary_cards["total"].value_label.text() == "3"
    assert window.review_summary_cards["false_negative"].value_label.text() == "1"
    assert window.review_summary_cards["false_positive"].value_label.text() == "1"
    assert window.review_summary_cards["avg_5m"].value_label.text() == "4.00"
    assert "LATE_CHASE: 1" in window.review_missed_reason_view.toPlainText()

    window.review_filter_bar.fn_combo.setCurrentIndex(1)

    assert window.review_summary_cards["total"].value_label.text() == "1"
    assert window.review_summary_cards["false_negative"].value_label.text() == "1"
    assert window.review_summary_cards["false_positive"].value_label.text() == "0"
    assert window.review_summary_cards["avg_5m"].value_label.text() == "4.00"
    assert window.review_missed_reason_view.toPlainText().strip() == "LATE_CHASE: 1"
    db.close()
    window.close()


def test_review_detail_panel_is_read_only_and_shows_linked_records(tmp_path, qapp, monkeypatch):
    runtime = FakeRuntime()
    window, db, client, engine = make_window(tmp_path, qapp, runtime)
    candidate = save_candidate(db, "111111", CandidateState.READY, NOW, name="Review Candidate")
    plan = db.save_entry_plan(EntryPlan(candidate_id=candidate.id, entry_type="pullback", limit_price=12300, created_at=NOW.isoformat()))
    virtual_order = db.save_virtual_order(
        VirtualOrder(
            candidate_id=candidate.id,
            entry_plan_id=plan.id,
            status=VirtualOrderStatus.FILLED,
            limit_price=12300,
            virtual_fill_price=12300,
            submitted_at=NOW.isoformat(),
            filled_at=(NOW + timedelta(minutes=1)).isoformat(),
        )
    )
    position = db.save_virtual_position(
        VirtualPosition(
            candidate_id=candidate.id,
            virtual_order_id=virtual_order.id,
            entry_price=12300,
            quantity=5,
            opened_at=NOW.isoformat(),
            closed_at=(NOW + timedelta(minutes=20)).isoformat(),
            close_price=12600,
            close_reason="TAKE_PROFIT",
            realized_return_pct=2.44,
        )
    )
    db.save_exit_decision(
        ExitDecision(
            virtual_position_id=position.id,
            decision_type="take_profit",
            trigger_price=12600,
            filled=True,
            reason_codes=["TAKE_PROFIT"],
            created_at=(NOW + timedelta(minutes=20)).isoformat(),
        )
    )
    save_review(
        db,
        "111111",
        ReviewFinalStatus.VIRTUAL_CLOSED_TAKE_PROFIT.value,
        "A",
        candidate_id=candidate.id,
        virtual_position_id=position.id,
        max_return_20m=3.0,
        missed_reason="none",
        details={"reason_codes": ["TAKE_PROFIT"]},
    )
    window.refresh_review_table()

    monkeypatch.setattr(runtime, "cycle", lambda: (_ for _ in ()).throw(AssertionError("review detail must not cycle")))
    monkeypatch.setattr(db, "save_candidate", lambda item: (_ for _ in ()).throw(AssertionError("review detail must not save candidate")))
    monkeypatch.setattr(db, "save_trade_review", lambda item: (_ for _ in ()).throw(AssertionError("review detail must not save review")))
    monkeypatch.setattr(db, "save_virtual_order", lambda item: (_ for _ in ()).throw(AssertionError("review detail must not save virtual order")))
    monkeypatch.setattr(db, "save_virtual_position", lambda item: (_ for _ in ()).throw(AssertionError("review detail must not save virtual position")))
    monkeypatch.setattr(db, "close_virtual_position_with_decision", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("review detail must not close position")))
    monkeypatch.setattr(client, "send_order", lambda request: (_ for _ in ()).throw(AssertionError("review detail must not call order path")))
    monkeypatch.setattr(engine, "register_realtime", lambda: (_ for _ in ()).throw(AssertionError("review detail must not register realtime")))

    select_review_row(window, review_table_row_for_code(window, "111111"))
    window._display_selected_review_detail()

    detail = window.review_detail_panel.text()
    assert "code: 111111" in detail
    assert "final_status: VIRTUAL_CLOSED_TAKE_PROFIT" in detail
    assert "max_return_20m: 3.00" in detail
    assert "missed_reason: none" in detail
    assert "virtual positions:" in detail
    assert "realized_return_pct=2.44" in detail
    assert "decision_type=take_profit" in detail
    db.close()
    window.close()


def test_review_filtered_export_uses_proxy_rows(tmp_path, qapp, monkeypatch):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())
    save_review(db, "111111", ReviewFinalStatus.VIRTUAL_SUBMITTED.value, "A")
    save_review(db, "222222", ReviewFinalStatus.BLOCKED_TEMP.value, "B")
    save_review(db, "333333", ReviewFinalStatus.EXPIRED.value, "C")
    window.refresh_review_table()
    window.review_filter_bar.grade_combo.setCurrentText("A")

    captured = []

    class FakeExporter:
        def export_csv(self, reviews, path):
            captured.append(("csv", [review.code for review in reviews], path))

        def export_markdown(self, reviews, path):
            captured.append(("md", [review.code for review in reviews], path))

    paths = iter([("filtered.csv", ""), ("filtered.md", ""), ("all.csv", ""), ("all.md", "")])
    monkeypatch.setattr("ui.main_window.ReviewExporter", lambda: FakeExporter())
    monkeypatch.setattr("ui.main_window.QFileDialog.getSaveFileName", lambda *args, **kwargs: next(paths))

    window.review_filtered_export_check.setChecked(True)
    window._export_reviews_csv()
    window._export_reviews_markdown()
    window.review_filtered_export_check.setChecked(False)
    window._export_reviews_csv()
    window._export_reviews_markdown()

    assert captured[0] == ("csv", ["111111"], "filtered.csv")
    assert captured[1] == ("md", ["111111"], "filtered.md")
    assert captured[2][0] == "csv"
    assert captured[2][1] == ["111111", "222222", "333333"]
    assert captured[3][0] == "md"
    assert captured[3][1] == ["111111", "222222", "333333"]
    db.close()
    window.close()


def test_review_selection_survives_refresh_until_review_disappears(tmp_path, qapp, monkeypatch):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())
    review_a = save_review(db, "111111", ReviewFinalStatus.VIRTUAL_SUBMITTED.value, "A")
    review_b = save_review(db, "222222", ReviewFinalStatus.BLOCKED_TEMP.value, "B")
    window.refresh_review_table()

    select_review_row(window, review_table_row_for_code(window, "111111"))
    window._display_selected_review_detail()
    assert "code: 111111" in window.review_detail_panel.text()

    window.refresh_review_table()

    assert window._selected_review_id() == review_a.id
    assert "code: 111111" in window.review_detail_panel.text()

    monkeypatch.setattr(db, "latest_trade_reviews", lambda limit=200: [review_b])
    window.refresh_review_table()

    assert "선택된 리뷰 없음" in window.review_detail_panel.text()
    assert review_table_codes(window) == ["222222"]
    db.close()
    window.close()


def test_review_export_ui_is_read_only_and_does_not_touch_runtime(tmp_path, qapp, monkeypatch):
    runtime = FakeRuntime()
    window, db, _, _ = make_window(tmp_path, qapp, runtime)
    db.save_trade_review(
        TradeReview(
            candidate_id=1,
            trade_date="2026-05-29",
            code="111111",
            theme_id="robot",
            review_key="ui-export",
            final_status=ReviewFinalStatus.BLOCKED_TEMP.value,
            created_at=NOW.isoformat(),
        )
    )
    csv_path = tmp_path / "ui_reviews.csv"
    md_path = tmp_path / "ui_reviews.md"
    monkeypatch.setattr(runtime, "cycle", lambda: (_ for _ in ()).throw(AssertionError("export must not cycle")))
    monkeypatch.setattr(db, "save_trade_review", lambda review: (_ for _ in ()).throw(AssertionError("export must not save review")))
    paths = iter([(str(csv_path), ""), (str(md_path), "")])
    monkeypatch.setattr("ui.main_window.QFileDialog.getSaveFileName", lambda *args, **kwargs: next(paths))

    before = len(db.list_trade_reviews())
    window._export_reviews_csv()
    window._export_reviews_markdown()
    after = len(db.list_trade_reviews())

    assert before == after == 1
    assert csv_path.exists()
    assert md_path.exists()
    db.close()
    window.close()


def test_ui_state_persists_core_layout_table_sort_and_log_option(tmp_path, qapp):
    ui_settings().clear()
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime(), clear_ui_state=False)
    window.resize(1040, 720)
    window.tabs.setCurrentIndex(3)
    window.main_splitter.setSizes([320, 980])
    window.watch_splitter.setSizes([700, 300])
    window.strategy_candidate_splitter.setSizes([650, 350])
    window.review_splitter.setSizes([640, 360])
    window.table.setColumnWidth(0, 133)
    window.strategy_candidate_table.setColumnWidth(0, 144)
    window.review_table.setColumnWidth(1, 155)
    window.table.sortByColumn(2, Qt.DescendingOrder)
    window.review_table.sortByColumn(9, Qt.DescendingOrder)
    window.log_autoscroll_check.setChecked(False)
    window._save_ui_state()
    window.close()
    db.close()

    restored, db2, _, _ = make_window(tmp_path, qapp, FakeRuntime(), clear_ui_state=False)

    assert restored.tabs.currentIndex() == 3
    assert restored.table.columnWidth(0) == 133
    assert restored.strategy_candidate_table.columnWidth(0) == 144
    assert restored.review_table.columnWidth(1) == 155
    assert restored.table.horizontalHeader().sortIndicatorSection() == 2
    assert restored.review_table.horizontalHeader().sortIndicatorSection() == 9
    assert restored.log_autoscroll_check.isChecked() is False
    db2.close()
    restored.close()


def test_ui_state_restore_falls_back_on_bad_values(tmp_path, qapp):
    store = ui_settings()
    store.clear()
    store.setValue("tabs/current_index", "bad")
    store.setValue("watch_table/column_widths", ["bad"])
    store.setValue("splitters/main/sizes", ["bad"])

    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime(), clear_ui_state=False)

    assert window.tabs.currentIndex() == 0
    assert window.table.columnWidth(0) > 0
    db.close()
    window.close()


def test_table_refresh_preserves_column_widths(tmp_path, qapp):
    window, db, _, engine = make_window(tmp_path, qapp, FakeRuntime())
    engine.items = {"111111": watch_item("111111", "Alpha")}
    save_candidate(db, "111111", CandidateState.READY, NOW, name="Alpha")
    save_review(db, "111111", ReviewFinalStatus.VIRTUAL_SUBMITTED.value, "A")
    window.table.setColumnWidth(0, 141)
    window.strategy_candidate_table.setColumnWidth(0, 142)
    window.review_table.setColumnWidth(1, 143)

    for _ in range(3):
        window.refresh_table()
        window.refresh_strategy_candidates()
        window.refresh_review_table()

    assert window.table.columnWidth(0) == 141
    assert window.strategy_candidate_table.columnWidth(0) == 142
    assert window.review_table.columnWidth(1) == 143
    db.close()
    window.close()


def test_search_filter_debounce_uses_final_text_without_refresh_paths(tmp_path, qapp, monkeypatch):
    window, db, client, engine = make_window(tmp_path, qapp, FakeRuntime())
    save_candidate(db, "111111", CandidateState.READY, NOW, name="Alpha")
    save_candidate(db, "222222", CandidateState.WATCHING, NOW, name="Beta")
    window.refresh_strategy_candidates()
    monkeypatch.setattr(db, "list_candidates", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("filter debounce must not reload candidates")))
    monkeypatch.setattr(window.strategy_runtime, "cycle", lambda: (_ for _ in ()).throw(AssertionError("filter debounce must not cycle")))
    monkeypatch.setattr(engine, "register_realtime", lambda: (_ for _ in ()).throw(AssertionError("filter debounce must not register realtime")))
    monkeypatch.setattr(client, "send_order", lambda request: (_ for _ in ()).throw(AssertionError("filter debounce must not call order path")))

    window.strategy_candidate_filter_bar.search_edit.setText("Al")
    window.strategy_candidate_filter_bar.search_edit.setText("Bet")

    assert candidate_table_row_count(window) == 2
    wait_for_filter_debounce(qapp)
    assert candidate_table_codes(window) == ["222222"]
    db.close()
    window.close()


def test_last_refresh_labels_update_for_major_tabs(tmp_path, qapp):
    window, db, _, engine = make_window(tmp_path, qapp, FakeRuntime())
    engine.items = {"111111": watch_item("111111", "Alpha")}
    save_candidate(db, "111111", CandidateState.READY, NOW, name="Alpha")
    save_review(db, "111111", ReviewFinalStatus.VIRTUAL_SUBMITTED.value, "A")

    window.refresh_table()
    window.refresh_strategy_candidates()
    window.refresh_review_table()
    window.config_interval_spin.setValue(6)
    window.save_strategy_settings()

    pattern = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"
    assert re.search(pattern, window.watch_last_refresh_label.text())
    assert re.search(pattern, window.strategy_candidate_last_refresh_label.text())
    assert re.search(pattern, window.review_last_refresh_label.text())
    assert re.search(pattern, window.strategy_settings_last_refresh_label.text())
    assert "완료" in window.watch_refresh_status_label.text()
    assert "완료" in window.strategy_candidate_refresh_status_label.text()
    assert "완료" in window.review_refresh_status_label.text()
    db.close()
    window.close()


def test_log_tab_filter_clear_autoscroll_and_max_lines(tmp_path, qapp):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())
    window._log_max_lines = 5
    for index in range(7):
        window._append_log(f"INFO line {index}")

    assert "line 0" not in window.log_view.toPlainText()
    assert "line 6" in window.log_view.toPlainText()
    assert window.log_view.verticalScrollBar().value() == window.log_view.verticalScrollBar().maximum()

    window.log_autoscroll_check.setChecked(False)
    window.log_view.verticalScrollBar().setValue(0)
    window._append_log("WARNING pinned")
    assert window.log_view.verticalScrollBar().value() == 0

    window.log_filter_input.setText("pinned")
    assert window.log_view.toPlainText() == "WARNING pinned"

    window.log_clear_button.click()
    assert window.log_view.toPlainText() == ""
    assert window._log_lines == []
    db.close()
    window.close()


def test_dashboard_and_close_ui_state_are_read_only(tmp_path, qapp, monkeypatch):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    client = MockKiwoomClient()
    engine = TradingEngine(client=client, db=db)
    runtime = FakeRuntime()
    window = MainWindow(engine=engine, db=db, mock_mode=False, strategy_runtime=runtime)
    monkeypatch.setattr(window, "refresh_strategy_candidates", lambda: (_ for _ in ()).throw(AssertionError("dashboard must not refresh candidates")))
    monkeypatch.setattr(runtime, "cycle", lambda: (_ for _ in ()).throw(AssertionError("dashboard must not cycle")))
    monkeypatch.setattr(runtime, "start", lambda: (_ for _ in ()).throw(AssertionError("dashboard must not start runtime")))
    monkeypatch.setattr(db, "save_candidate", lambda item: (_ for _ in ()).throw(AssertionError("dashboard must not save candidate")))
    monkeypatch.setattr(db, "save_trade_review", lambda item: (_ for _ in ()).throw(AssertionError("dashboard must not save review")))
    monkeypatch.setattr(db, "save_virtual_order", lambda item: (_ for _ in ()).throw(AssertionError("dashboard must not save virtual order")))
    monkeypatch.setattr(engine, "register_realtime", lambda: (_ for _ in ()).throw(AssertionError("dashboard must not register realtime")))
    monkeypatch.setattr(client, "send_order", lambda request: (_ for _ in ()).throw(AssertionError("dashboard must not call order path")))

    window.ordering_check.setChecked(True)
    snapshot = StrategyRuntimeSnapshot(started=True, cycle_at=NOW.isoformat(), active_candidate_count=2)
    window._update_dashboard(snapshot)
    window._update_dashboard(snapshot)

    assert window.dashboard_cards["ordering"].tone == "danger"
    assert window.dashboard_cards["mode"].tone == "danger"

    monkeypatch.setattr(window, "_save_ui_state", lambda: (_ for _ in ()).throw(RuntimeError("save failed")))
    window.close()
    db.close()


def test_strategy_ui_has_no_auto_or_real_order_controls(tmp_path, qapp):
    window, db, _, _ = make_window(tmp_path, qapp, FakeRuntime())
    source = open("ui/main_window.py", encoding="utf-8").read()

    assert "AUTO_A" not in source
    assert "HYBRID" not in source
    assert "실주문" not in window.strategy_safety_label.text()
    assert "no real orders" in window.strategy_safety_label.text()
    db.close()
    window.close()


def test_ui_modules_do_not_reference_real_order_path():
    forbidden = ["OrderRequest", "send_order", "KiwoomClient"]
    for path in Path("ui").glob("**/*.py"):
        source = path.read_text(encoding="utf-8")
        for text in forbidden:
            assert text not in source, f"{text} found in {path}"
