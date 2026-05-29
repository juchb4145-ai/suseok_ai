import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from main import configure_qt_paths

configure_qt_paths()

from PyQt5.QtWidgets import QApplication

from kiwoom.client import MockKiwoomClient
from main import build_observe_runtime
from storage.db import TradingDatabase
from trading.engine import TradingEngine
from trading.strategy.config import StrategyRuntimeConfigRepository
from trading.strategy.models import (
    BlockType,
    Candidate,
    CandidateEvent,
    CandidateSourceType,
    CandidateState,
    EntryPlan,
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


def make_window(tmp_path, qapp, runtime=None):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    client = MockKiwoomClient()
    engine = TradingEngine(client=client, db=db)
    window = MainWindow(engine=engine, db=db, mock_mode=True, strategy_runtime=runtime)
    return window, db, client, engine


def candidate_table_row_count(window):
    return window.strategy_candidate_proxy_model.rowCount()


def candidate_table_text(window, row, column):
    index = window.strategy_candidate_proxy_model.index(row, column)
    return window.strategy_candidate_proxy_model.data(index)


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


def test_observe_start_stop_and_duplicate_noops_do_not_call_order_paths(tmp_path, qapp, monkeypatch):
    runtime = FakeRuntime()
    window, db, client, engine = make_window(tmp_path, qapp, runtime)
    calls = []

    monkeypatch.setattr(client, "send_order", lambda request: calls.append(request))
    monkeypatch.setattr(engine, "register_realtime", lambda: (_ for _ in ()).throw(AssertionError("manual realtime must not be called")))

    window.start_observe_strategy()
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


def test_observe_readiness_summary_displays_snapshot_and_dedupes_warnings(tmp_path, qapp):
    runtime = FakeRuntime(startup_warnings=["THEME_MAPPING_EMPTY", "THEME_MAPPING_EMPTY"])
    window, db, _, _ = make_window(tmp_path, qapp, runtime)

    window.start_observe_strategy()

    readiness_text = window.strategy_readiness_view.toPlainText()
    warning_text = window.strategy_warning_view.toPlainText()
    assert window.strategy_readiness_view.isReadOnly()
    assert "conditions=3 unresolved=1" in readiness_text
    assert "themes=0 enabled=0" in readiness_text
    assert "active candidates=2 mapped=0 unmapped=2" in readiness_text
    assert "protected subs=4/85" in readiness_text
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

    assert runtime.start_calls == 1
    assert not window.strategy_timer.isActive()
    assert "STRATEGY_RUNTIME_START_FAILED" in window.strategy_warning_view.toPlainText()
    db.close()
    window.close()


def test_stop_failure_still_stops_timer_and_marks_stopped(tmp_path, qapp):
    runtime = FakeRuntime(stop_fail=True)
    window, db, _, _ = make_window(tmp_path, qapp, runtime)

    window.start_observe_strategy()
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

    window._strategy_cycle_running = True
    window._run_strategy_cycle()
    assert runtime.cycle_calls == 0
    assert "STRATEGY_CYCLE_REENTRY_SKIPPED" in window.strategy_warning_view.toPlainText()

    window._strategy_cycle_running = False
    runtime.cycle_fail = True
    window._run_strategy_cycle()
    assert runtime.cycle_calls == 1
    assert "STRATEGY_CYCLE_FAILED" in window.strategy_warning_view.toPlainText()
    db.close()
    window.close()


def test_strategy_cycle_throttles_candidate_table_auto_refresh(tmp_path, qapp):
    runtime = QuietRuntime()
    window, db, _, _ = make_window(tmp_path, qapp, runtime)
    window.start_observe_strategy()
    refresh_calls = []
    window.refresh_strategy_candidates = lambda: refresh_calls.append("refresh")
    window._strategy_last_auto_refresh_at = perf_counter()
    window._strategy_last_snapshot = StrategyRuntimeSnapshot(started=True, active_candidate_count=2)

    window._run_strategy_cycle()
    window._run_strategy_cycle()

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
    window.refresh_strategy_candidates()

    monkeypatch.setattr(window.strategy_runtime, "cycle", lambda: (_ for _ in ()).throw(AssertionError("filter must not cycle")))
    monkeypatch.setattr(db, "save_candidate", lambda candidate: (_ for _ in ()).throw(AssertionError("filter must not save candidate")))
    monkeypatch.setattr(db, "save_trade_review", lambda review: (_ for _ in ()).throw(AssertionError("filter must not save review")))
    monkeypatch.setattr(db, "save_virtual_order", lambda order: (_ for _ in ()).throw(AssertionError("filter must not save virtual order")))
    monkeypatch.setattr(db, "save_virtual_position", lambda position: (_ for _ in ()).throw(AssertionError("filter must not save virtual position")))
    monkeypatch.setattr(client, "send_order", lambda request: (_ for _ in ()).throw(AssertionError("filter must not call order path")))
    monkeypatch.setattr(engine, "register_realtime", lambda: (_ for _ in ()).throw(AssertionError("filter must not register realtime")))

    window.strategy_candidate_filter_bar.state_combo.setCurrentText("READY")
    assert candidate_table_codes(window) == ["111111"]

    window.strategy_candidate_filter_bar.clear_filters()
    window.strategy_candidate_filter_bar.search_edit.setText("Watch Search")
    assert candidate_table_codes(window) == ["333333"]

    window.strategy_candidate_filter_bar.clear_filters()
    window.strategy_candidate_filter_bar.search_edit.setText("robot")
    assert candidate_table_codes(window) == ["111111"]

    window.strategy_candidate_filter_bar.search_edit.setText("Robot Theme")
    assert candidate_table_codes(window) == ["111111"]

    window.strategy_candidate_filter_bar.clear_filters()
    window.strategy_candidate_filter_bar.search_edit.setText("BLOCK_REASON")
    assert candidate_table_codes(window) == ["222222"]

    window.strategy_candidate_filter_bar.clear_filters()
    window.strategy_candidate_filter_bar.recover_combo.setCurrentIndex(1)
    assert candidate_table_codes(window) == ["222222"]

    window.strategy_candidate_filter_bar.clear_filters()
    window.strategy_candidate_filter_bar.theme_combo.setCurrentIndex(1)
    assert candidate_table_codes(window) == ["111111"]

    window.strategy_candidate_filter_bar.theme_combo.setCurrentIndex(2)
    assert candidate_table_codes(window) == ["222222", "333333", "444444"]
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
