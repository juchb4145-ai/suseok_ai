import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
from trading.strategy.models import BlockType, Candidate, CandidateState, ReviewFinalStatus, TradeReview
from trading.strategy.runtime import StrategyRuntimeConfig, StrategyRuntimeSnapshot
from ui.main_window import MainWindow


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

    def start(self):
        self.start_calls += 1
        if self.start_fail:
            raise RuntimeError("start boom")
        return StrategyRuntimeSnapshot(
            started=True,
            cycle_at=NOW.isoformat(),
            active_candidate_count=2,
            warnings=["STARTED"],
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


def save_candidate(db, code, state, last_seen, metadata=None):
    return db.save_candidate(
        Candidate(
            trade_date="2026-05-29",
            code=code,
            name=code,
            state=state,
            detected_at=NOW.isoformat(),
            last_seen_at=last_seen.isoformat(),
            expires_at=(NOW + timedelta(minutes=30)).isoformat(),
            block_type=BlockType.TEMPORARY if state == CandidateState.BLOCKED else BlockType.NONE,
            can_recover=state == CandidateState.BLOCKED,
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

    assert window.strategy_candidate_table.rowCount() == 3
    assert window.strategy_candidate_table.item(0, 0).text() == "333333"
    assert window.strategy_candidate_table.item(1, 0).text() == "222222"
    assert window.strategy_candidate_table.item(1, 5).text() == "robot"
    assert window.strategy_candidate_table.item(1, 7).text() == "PASS"
    assert "999999" not in [window.strategy_candidate_table.item(row, 0).text() for row in range(3)]
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
    assert window.strategy_candidate_table.rowCount() == 1
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

    assert window.strategy_candidate_table.rowCount() == 1
    assert "CANDIDATE_METADATA_INVALID:111111" in window.strategy_warning_view.toPlainText()
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
