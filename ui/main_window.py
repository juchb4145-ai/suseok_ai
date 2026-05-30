from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from time import perf_counter
from typing import Optional

from PyQt5.QtCore import QItemSelectionModel, QTimer, Qt
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableView,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from storage.db import TradingDatabase
from trading.engine import TradingEngine
from trading.models import BuyLeg, LegStatus, WatchItem
from trading.rules import tick_size
from trading.strategy.candidates import candidate_is_discovery_only, candidate_quality_status
from trading.strategy.config import StrategyRuntimeConfigRepository, config_from_dict, config_to_dict
from trading.strategy.export import ReviewExporter
from trading.strategy.models import CandidateState, FillPolicy, TradeReview
from trading.strategy.runtime import StrategyRuntime, StrategyRuntimeConfig, StrategyRuntimeSnapshot
from trading.theme_engine.context_provider import DynamicThemeContextProvider
from trading.theme_engine.repository import ThemeEngineRepository
from ui.formatters import dedupe_text, format_metric, format_money, format_percent
from ui.table_models import (
    CandidateFilterProxyModel,
    CandidateTableModel,
    ReviewFilterProxyModel,
    ReviewTableModel,
    WatchItemFilterProxyModel,
    WatchItemTableModel,
)
from ui.ui_state import (
    restore_splitter_state,
    restore_table_state,
    restore_window_state,
    save_splitter_state,
    save_table_state,
    save_window_state,
    settings as ui_settings,
)
from ui.widgets import CandidateDetailPanel, CandidateFilterBar, ReviewFilterBar, StatusBadge, SummaryCard, WatchItemFilterBar


class TickPriceSpinBox(QSpinBox):
    def stepBy(self, steps: int) -> None:
        current = self.value()
        step = tick_size(current if current > 0 else 1)
        self.setSingleStep(step)
        self.setValue(max(self.minimum(), min(self.maximum(), current + (steps * step))))


class MainWindow(QMainWindow):
    headers = [
        "코드",
        "종목명",
        "현재가",
        "예산",
        "손절가",
        "1차",
        "1차%",
        "1차상태",
        "2차",
        "2차%",
        "2차상태",
        "3차",
        "3차%",
        "3차상태",
        "보유",
        "평단",
        "익절완료",
        "자동매수",
    ]
    review_headers = [
        "시각",
        "코드",
        "종목명",
        "시장",
        "테마",
        "등급",
        "상태",
        "주문",
        "청산",
        "5m",
        "10m",
        "20m",
        "20m DD",
        "Missed",
        "False+",
        "사유",
    ]

    strategy_candidate_headers = [
        "Code",
        "Name",
        "State",
        "Block",
        "Recover",
        "Best Theme",
        "Gate Key",
        "Sub Status",
        "Last Seen",
        "Expires",
        "Reasons",
        "Quality",
    ]

    def __init__(
        self,
        engine: TradingEngine,
        db: TradingDatabase,
        mock_mode: bool = False,
        strategy_runtime: Optional[StrategyRuntime] = None,
        strategy_runtime_unavailable_reason: str = "",
    ) -> None:
        super().__init__()
        self.engine = engine
        self.db = db
        self.mock_mode = mock_mode
        self.strategy_runtime = strategy_runtime
        self.strategy_runtime_unavailable_reason = strategy_runtime_unavailable_reason
        self.strategy_config_repository = StrategyRuntimeConfigRepository(db)
        self._strategy_saved_config: Optional[StrategyRuntimeConfig] = None
        self._strategy_settings_dirty = False
        self._strategy_settings_loading = False
        self._refresh_pending = False
        self._strategy_running = False
        self._strategy_starting = False
        self._strategy_cycle_running = False
        self._strategy_last_snapshot: Optional[StrategyRuntimeSnapshot] = None
        self._strategy_last_warning_at = ""
        self._strategy_last_auto_refresh_at = 0.0
        self._strategy_ui_refresh_count = 0
        self._strategy_ui_refresh_skipped_count = 0
        self._strategy_auto_refresh_interval_sec = 15.0
        self._strategy_dashboard_candidate_count = 0
        self._strategy_dashboard_ready_count = 0
        self._strategy_dashboard_blocked_count = 0
        self._dashboard_last_values: dict[str, tuple[str, str, str]] = {}
        self._log_lines: list[str] = []
        self._log_max_lines = 3000
        self.strategy_timer = QTimer(self)
        self.strategy_timer.timeout.connect(self._run_strategy_cycle)
        self.watch_filter_timer = QTimer(self)
        self.watch_filter_timer.setSingleShot(True)
        self.watch_filter_timer.setInterval(200)
        self.watch_filter_timer.timeout.connect(self._apply_watch_filters)
        self.strategy_candidate_filter_timer = QTimer(self)
        self.strategy_candidate_filter_timer.setSingleShot(True)
        self.strategy_candidate_filter_timer.setInterval(200)
        self.strategy_candidate_filter_timer.timeout.connect(self._apply_strategy_candidate_filters)
        self.review_filter_timer = QTimer(self)
        self.review_filter_timer.setSingleShot(True)
        self.review_filter_timer.setInterval(200)
        self.review_filter_timer.timeout.connect(self._apply_review_filters)
        self.setWindowTitle("키움 눌림목 반자동 매매")
        self._build_ui()
        self._wire_events()
        self._load_initial()

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)

        dashboard = self._build_dashboard()
        self.connection_label = StatusBadge("연결 안됨", "neutral")
        self.mode_label = StatusBadge("MOCK" if self.mock_mode else "실거래 가능", "success" if self.mock_mode else "warning")
        self.account_combo = QComboBox()
        self.login_button = QPushButton("로그인")
        self.ordering_check = QCheckBox("주문 가능")
        self.realtime_button = QPushButton("실시간 등록")

        status_controls = QHBoxLayout()
        status_controls.addWidget(QLabel("상태"))
        status_controls.addWidget(self.connection_label)
        status_controls.addWidget(self.mode_label)
        status_controls.addWidget(QLabel("계좌"))
        status_controls.addWidget(self.account_combo, 1)
        status_controls.addWidget(self.login_button)
        status_controls.addWidget(self.realtime_button)
        status_controls.addWidget(self.ordering_check)
        outer.addLayout(dashboard)
        outer.addLayout(status_controls)

        self.main_splitter = QSplitter()
        self.main_splitter.addWidget(self._build_form())
        self.main_splitter.addWidget(self._build_tabs())
        self.main_splitter.setSizes([420, 900])
        outer.addWidget(self.main_splitter, 1)
        self.setCentralWidget(central)

        toolbar = self.addToolBar("tools")
        toolbar.setObjectName("main_tools_toolbar")
        mock_tick = QAction("MOCK 가격 테스트", self)
        mock_tick.triggered.connect(self._mock_price_tick)
        mock_fill = QAction("MOCK 체결 테스트", self)
        mock_fill.triggered.connect(self._mock_fill)
        toolbar.addAction(mock_tick)
        toolbar.addAction(mock_fill)
        self._restore_ui_state()

    def _build_dashboard(self) -> QGridLayout:
        layout = QGridLayout()
        self.dashboard_cards = {
            "connection": SummaryCard("연결", "연결 안됨", "-", "neutral"),
            "mode": SummaryCard("거래 모드", "MOCK" if self.mock_mode else "실거래 가능", "-", "success" if self.mock_mode else "warning"),
            "ordering": SummaryCard("주문 가능", "OFF", "-", "neutral"),
            "observe": SummaryCard("OBSERVE", "stopped", "-", "neutral"),
            "candidates": SummaryCard("후보 수", "0", "-", "neutral"),
            "ready_blocked": SummaryCard("READY/BLOCKED", "0 / 0", "-", "neutral"),
            "theme_engine": SummaryCard("테마 엔진", "warming", "-", "neutral"),
            "active_theme": SummaryCard("ACTIVE 테마", "0", "-", "neutral"),
            "watch_theme": SummaryCard("WATCH 테마", "0", "-", "neutral"),
            "theme_candidates": SummaryCard("테마 후보", "0", "-", "neutral"),
            "subscription": SummaryCard("WS 구독", "-", "-", "neutral"),
            "warnings": SummaryCard("경고", "0", "last: -", "neutral"),
        }
        for index, card in enumerate(self.dashboard_cards.values()):
            layout.addWidget(card, index // 5, index % 5)
        return layout

    def _build_form(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)

        info_group = QGroupBox("매수종목")
        form = QFormLayout(info_group)
        self.code_edit = QLineEdit()
        self.name_edit = QLineEdit()
        self.lookup_button = QPushButton("종목명 조회")
        code_row = QHBoxLayout()
        code_row.addWidget(self.code_edit)
        code_row.addWidget(self.lookup_button)
        form.addRow("종목코드", code_row)
        form.addRow("종목명", self.name_edit)

        self.budget_spin = QSpinBox()
        self.budget_spin.setRange(0, 2_000_000_000)
        self.budget_spin.setSingleStep(100_000)
        self.budget_spin.setSuffix(" 원")
        self.stop_spin = TickPriceSpinBox()
        self.stop_spin.setRange(0, 2_000_000)
        self.stop_spin.setSuffix(" 원")
        self.tick_spin = QSpinBox()
        self.tick_spin.setRange(0, 100)
        self.tick_spin.setValue(1)
        self.tick_spin.setSuffix(" 틱")
        form.addRow("총투입예정금", self.budget_spin)
        form.addRow("손절가", self.stop_spin)
        form.addRow("접근 기준", self.tick_spin)

        self.target_spins: dict[int, QSpinBox] = {}
        self.weight_spins: dict[int, QDoubleSpinBox] = {}
        legs_group = QGroupBox("분할 매수")
        grid = QGridLayout(legs_group)
        grid.addWidget(QLabel("차수"), 0, 0)
        grid.addWidget(QLabel("목표매수가"), 0, 1)
        grid.addWidget(QLabel("비중"), 0, 2)
        defaults = {1: 40.0, 2: 30.0, 3: 30.0}
        for row, idx in enumerate([1, 2, 3], start=1):
            target = TickPriceSpinBox()
            target.setRange(0, 2_000_000)
            target.setSuffix(" 원")
            weight = QDoubleSpinBox()
            weight.setRange(0, 100)
            weight.setDecimals(1)
            weight.setValue(defaults[idx])
            weight.setSuffix(" %")
            self.target_spins[idx] = target
            self.weight_spins[idx] = weight
            grid.addWidget(QLabel(f"{idx}차"), row, 0)
            grid.addWidget(target, row, 1)
            grid.addWidget(weight, row, 2)

        sell_group = QGroupBox("익절")
        sell_form = QFormLayout(sell_group)
        self.take_profit_rate_spin = QDoubleSpinBox()
        self.take_profit_rate_spin.setRange(0, 100)
        self.take_profit_rate_spin.setValue(5.0)
        self.take_profit_rate_spin.setDecimals(1)
        self.take_profit_rate_spin.setSuffix(" %")
        self.take_profit_sell_spin = QDoubleSpinBox()
        self.take_profit_sell_spin.setRange(0, 100)
        self.take_profit_sell_spin.setValue(70.0)
        self.take_profit_sell_spin.setDecimals(1)
        self.take_profit_sell_spin.setSuffix(" %")
        self.auto_buy_check = QCheckBox("자동매수")
        self.auto_sell_check = QCheckBox("자동익절")
        self.auto_sell_check.setChecked(True)
        sell_form.addRow("익절률", self.take_profit_rate_spin)
        sell_form.addRow("매도비율", self.take_profit_sell_spin)
        sell_form.addRow(self.auto_buy_check)
        sell_form.addRow(self.auto_sell_check)

        buttons = QHBoxLayout()
        self.save_button = QPushButton("저장")
        self.delete_button = QPushButton("삭제")
        self.clear_button = QPushButton("초기화")
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.delete_button)
        buttons.addWidget(self.clear_button)

        order_group = QGroupBox("미체결 수동관리")
        order_layout = QHBoxLayout(order_group)
        self.order_leg_combo = QComboBox()
        self.order_leg_combo.addItems(["1차", "2차", "3차"])
        self.cancel_leg_button = QPushButton("취소")
        self.modify_leg_button = QPushButton("목표가로 정정")
        order_layout.addWidget(self.order_leg_combo)
        order_layout.addWidget(self.cancel_leg_button)
        order_layout.addWidget(self.modify_leg_button)

        layout.addWidget(info_group)
        layout.addWidget(legs_group)
        layout.addWidget(sell_group)
        layout.addWidget(order_group)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return box

    def _build_tabs(self) -> QWidget:
        tabs = QTabWidget()
        self.tabs = tabs
        self.tabs.setObjectName("main_tabs")
        self.log_view = QTextEdit()
        self.log_view.setObjectName("log_view")
        self.log_view.setReadOnly(True)
        self.log_filter_input = QLineEdit()
        self.log_filter_input.setObjectName("log_filter_input")
        self.log_filter_input.setPlaceholderText("로그 검색")
        self.log_autoscroll_check = QCheckBox("자동 스크롤")
        self.log_autoscroll_check.setObjectName("log_autoscroll_check")
        self.log_autoscroll_check.setChecked(True)
        self.log_clear_button = QPushButton("Clear")
        log_controls = QHBoxLayout()
        log_controls.addWidget(QLabel("검색"))
        log_controls.addWidget(self.log_filter_input, 1)
        log_controls.addWidget(self.log_autoscroll_check)
        log_controls.addWidget(self.log_clear_button)
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        log_layout.addLayout(log_controls)
        log_layout.addWidget(self.log_view, 1)
        tabs.addTab(self._build_watch_tab(), "매수종목/상태")
        tabs.addTab(self._build_strategy_tab(), "전략 후보")
        tabs.addTab(self._build_review_tab(), "전략 리뷰")
        tabs.addTab(log_tab, "로그")
        tabs.insertTab(2, self._build_strategy_settings_tab(), "전략 설정")
        return tabs

    def _build_watch_tab(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        self.watch_filter_bar = WatchItemFilterBar()
        self.watch_filter_bar.search_edit.setObjectName("watch_filter_input")
        self.watch_last_refresh_label = QLabel("마지막 갱신: -")
        self.watch_last_refresh_label.setObjectName("watch_last_refresh_label")
        self.watch_refresh_status_label = QLabel("")
        self.watch_model = WatchItemTableModel(mock_mode=self.mock_mode, ordering_enabled=self.engine.ordering_enabled)
        self.watch_proxy_model = WatchItemFilterProxyModel()
        self.watch_proxy_model.setSourceModel(self.watch_model)
        self.watch_proxy_model.setSortRole(WatchItemTableModel.SortRole)
        self.table = QTableView()
        self.table.setObjectName("watch_table")
        self.table.setModel(self.watch_proxy_model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.watch_detail_panel = CandidateDetailPanel("종목 상세", "선택된 종목 없음")
        self.watch_splitter = QSplitter()
        self.watch_splitter.addWidget(self.table)
        self.watch_splitter.addWidget(self.watch_detail_panel)
        self.watch_splitter.setSizes([900, 360])
        layout.addWidget(self.watch_filter_bar)
        layout.addWidget(self.watch_last_refresh_label)
        layout.addWidget(self.watch_refresh_status_label)
        layout.addWidget(self.watch_splitter, 1)
        self._restore_watch_table_columns()
        self.table.horizontalHeader().sectionResized.connect(lambda *_args: self._save_watch_table_columns())
        return box

    def _restore_watch_table_columns(self) -> None:
        restore_table_state(ui_settings(), self.table, "watch_table", self._watch_table_default_widths())

    def _save_watch_table_columns(self) -> None:
        if not hasattr(self, "table"):
            return
        save_table_state(ui_settings(), self.table, "watch_table")

    def _restore_ui_state(self) -> None:
        store = ui_settings()
        restore_window_state(store, self)
        restore_splitter_state(store, self.main_splitter, "splitters/main", [420, 900])
        restore_splitter_state(store, self.watch_splitter, "splitters/watch", [900, 360])
        restore_splitter_state(store, self.strategy_candidate_splitter, "splitters/candidates", [1080, 520])
        restore_splitter_state(store, self.review_splitter, "splitters/reviews", [860, 420])
        restore_table_state(store, self.table, "watch_table", self._watch_table_default_widths())
        restore_table_state(store, self.strategy_candidate_table, "candidate_table", self._candidate_table_default_widths())
        self._enforce_table_minimum_widths(self.strategy_candidate_table, self._candidate_table_default_widths())
        restore_table_state(store, self.review_table, "review_table", self._review_table_default_widths())
        try:
            tab_index = int(store.value("tabs/current_index", 0))
            if 0 <= tab_index < self.tabs.count():
                self.tabs.setCurrentIndex(tab_index)
        except (TypeError, ValueError):
            self.tabs.setCurrentIndex(0)
        autoscroll = store.value("logs/autoscroll", True)
        self.log_autoscroll_check.setChecked(str(autoscroll).lower() not in {"false", "0"})

    def _save_ui_state(self) -> None:
        if not hasattr(self, "tabs"):
            return
        store = ui_settings()
        save_window_state(store, self)
        save_splitter_state(store, self.main_splitter, "splitters/main")
        save_splitter_state(store, self.watch_splitter, "splitters/watch")
        save_splitter_state(store, self.strategy_candidate_splitter, "splitters/candidates")
        save_splitter_state(store, self.review_splitter, "splitters/reviews")
        save_table_state(store, self.table, "watch_table")
        save_table_state(store, self.strategy_candidate_table, "candidate_table")
        save_table_state(store, self.review_table, "review_table")
        try:
            store.setValue("tabs/current_index", self.tabs.currentIndex())
            store.setValue("logs/autoscroll", self.log_autoscroll_check.isChecked())
        except Exception:
            return

    def closeEvent(self, event) -> None:
        try:
            self._save_ui_state()
        except Exception:
            pass
        super().closeEvent(event)

    @staticmethod
    def _watch_table_default_widths() -> list[int]:
        return [78, 120, 86, 90, 86, 82, 66, 92, 82, 66, 92, 82, 66, 92, 64, 86, 78, 78]

    @staticmethod
    def _candidate_table_default_widths() -> list[int]:
        return [84, 130, 96, 96, 82, 160, 170, 180, 165, 165, 360, 120]

    @staticmethod
    def _review_table_default_widths() -> list[int]:
        return [150, 78, 120, 72, 90, 60, 92, 80, 80, 70, 70, 70, 80, 78, 78, 220]

    @staticmethod
    def _enforce_table_minimum_widths(table: QTableView, minimum_widths: list[int]) -> None:
        for column, width in enumerate(minimum_widths):
            if table.columnWidth(column) < width:
                table.setColumnWidth(column, width)

    def _build_strategy_tab(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        controls = QHBoxLayout()
        self.strategy_status_label = QLabel("OBSERVE stopped")
        self.strategy_safety_label = QLabel("OBSERVE ONLY / no real orders / auto buy disabled")
        self.strategy_safety_label.setWordWrap(True)
        self.strategy_start_button = QPushButton("OBSERVE 시작")
        self.strategy_stop_button = QPushButton("OBSERVE 중지")
        self.strategy_refresh_button = QPushButton("후보 새로고침")
        self.strategy_candidate_last_refresh_label = QLabel("마지막 갱신: -")
        self.strategy_candidate_last_refresh_label.setObjectName("candidate_last_refresh_label")
        self.strategy_candidate_last_refresh_label.setMinimumWidth(220)
        self.strategy_candidate_last_refresh_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.strategy_candidate_refresh_status_label = QLabel("")
        controls.addWidget(self.strategy_status_label)
        controls.addWidget(self.strategy_safety_label, 1)
        controls.addWidget(self.strategy_candidate_last_refresh_label)
        controls.addWidget(self.strategy_start_button)
        controls.addWidget(self.strategy_stop_button)
        controls.addWidget(self.strategy_refresh_button)

        self.strategy_snapshot_label = QLabel("cycle: - / candidates: - / warnings: 0")
        self.strategy_snapshot_label.setWordWrap(True)
        self.dashboard_last_snapshot_label = QLabel("대시보드 마지막 스냅샷: -")
        self.dashboard_last_snapshot_label.setWordWrap(True)
        self.strategy_readiness_view = QTextEdit()
        self.strategy_readiness_view.setReadOnly(True)
        self.strategy_readiness_view.setMaximumHeight(100)
        self.strategy_warning_view = QTextEdit()
        self.strategy_warning_view.setReadOnly(True)
        self.strategy_warning_view.setMaximumHeight(90)
        self.strategy_candidate_filter_bar = CandidateFilterBar()
        self.strategy_candidate_filter_bar.search_edit.setObjectName("candidate_filter_input")
        self.strategy_candidate_model = CandidateTableModel()
        self.strategy_candidate_proxy_model = CandidateFilterProxyModel()
        self.strategy_candidate_proxy_model.setSourceModel(self.strategy_candidate_model)
        self.strategy_candidate_table = QTableView()
        self.strategy_candidate_table.setObjectName("candidate_table")
        self.strategy_candidate_table.setModel(self.strategy_candidate_proxy_model)
        self.strategy_candidate_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.strategy_candidate_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.strategy_candidate_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.strategy_candidate_table.setSortingEnabled(True)
        self.strategy_candidate_proxy_model.sort(-1)
        self.strategy_candidate_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.strategy_candidate_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.strategy_candidate_table.horizontalHeader().setMinimumSectionSize(64)
        self.strategy_candidate_table.horizontalHeader().setStretchLastSection(True)
        self.strategy_candidate_detail_panel = CandidateDetailPanel()
        self.strategy_candidate_splitter = QSplitter()
        self.strategy_candidate_splitter.addWidget(self.strategy_candidate_table)
        self.strategy_candidate_splitter.addWidget(self.strategy_candidate_detail_panel)
        self.strategy_candidate_splitter.setCollapsible(0, False)
        self.strategy_candidate_splitter.setCollapsible(1, False)
        self.strategy_candidate_splitter.setStretchFactor(0, 3)
        self.strategy_candidate_splitter.setStretchFactor(1, 1)
        self.strategy_candidate_splitter.setSizes([1080, 520])

        layout.addLayout(controls)
        layout.addWidget(self.strategy_candidate_filter_bar)
        layout.addWidget(self.strategy_snapshot_label)
        layout.addWidget(self.dashboard_last_snapshot_label)
        layout.addWidget(self.strategy_readiness_view)
        layout.addWidget(self.strategy_warning_view)
        layout.addWidget(self.strategy_candidate_refresh_status_label)
        layout.addWidget(self.strategy_candidate_splitter, 1)
        return box

    def _build_strategy_settings_tab(self) -> QWidget:
        box = QWidget()
        outer = QVBoxLayout(box)

        self.strategy_settings_status_label = QLabel("저장됨: - / 실행 중: -")
        self.strategy_settings_status_label.setObjectName("settings_status_label")
        self.strategy_settings_pending_label = QLabel("")
        self.strategy_settings_pending_label.setObjectName("strategySettingsPendingLabel")
        self.strategy_settings_last_refresh_label = QLabel("마지막 로드/저장: -")
        self.strategy_settings_last_refresh_label.setObjectName("settings_last_refresh_label")
        self.strategy_settings_status_cards = {
            "saved": SummaryCard("저장된 설정", "-", "-", "neutral"),
            "active": SummaryCard("실행 중 설정", "-", "-", "neutral"),
            "pending": SummaryCard("적용 상태", "-", "-", "neutral"),
            "safety": SummaryCard("안전 상태", "-", "no real orders from OBSERVE UI", "neutral"),
        }
        cards = QGridLayout()
        for column, card in enumerate(self.strategy_settings_status_cards.values()):
            cards.addWidget(card, 0, column)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)

        self.config_interval_spin = QSpinBox()
        self.config_interval_spin.setRange(0, 3600)
        self.config_interval_spin.setSuffix(" 초")
        self.config_interval_spin.setToolTip("OBSERVE 평가 루프의 실행 간격입니다. 실행 중 저장한 값은 다음 시작부터 적용됩니다.")
        self.config_condition_enabled_check = QCheckBox("조건식 프로필 사용")
        self.config_condition_enabled_check.setToolTip("조건식 기반 후보 수집을 사용할지 결정합니다. 끄면 후보 생성 품질에 영향을 줄 수 있습니다.")
        self.config_leader_codes_edit = QLineEdit()
        self.config_leader_codes_edit.setToolTip("테마 대장주로 항상 감시할 6자리 종목코드입니다. 쉼표, 공백, 줄바꿈으로 구분할 수 있습니다.")
        self.config_signal_codes_edit = QLineEdit()
        self.config_signal_codes_edit.setToolTip("반도체/신호주 감시에 사용할 6자리 종목코드입니다. 중복 코드는 저장 시 정리됩니다.")
        self.config_holding_codes_edit = QLineEdit()
        self.config_holding_codes_edit.setToolTip("보유 종목 보호 감시에 사용할 6자리 종목코드입니다.")
        self.config_kospi_index_edit = QLineEdit()
        self.config_kospi_index_edit.setToolTip("코스피 종목 후보가 참조하는 지수 코드입니다. 비어 있으면 저장이 차단됩니다.")
        self.config_kosdaq_index_edit = QLineEdit()
        self.config_kosdaq_index_edit.setToolTip("코스닥 종목 후보가 참조하는 지수 코드입니다. 비어 있으면 저장이 차단됩니다.")
        self.config_fill_policy_combo = QComboBox()
        for policy in [FillPolicy.OPTIMISTIC, FillPolicy.NORMAL, FillPolicy.CONSERVATIVE]:
            self.config_fill_policy_combo.addItem(policy.value, policy.value)
        self.config_fill_policy_combo.setToolTip("가상 주문의 체결 판정 정책입니다. 실제 주문 정책은 변경하지 않습니다.")
        self.config_review_enabled_check = QCheckBox("리뷰 저장")
        self.config_review_enabled_check.setToolTip("꺼두면 전략 리뷰 탭에 쌓이는 분석 데이터가 줄어듭니다.")
        self.config_max_candidates_spin = QSpinBox()
        self.config_max_candidates_spin.setRange(0, 10_000)
        self.config_max_candidates_spin.setToolTip("동시에 추적할 전략 후보 상한입니다. 0은 저장할 수 없습니다.")
        self.config_realtime_limit_spin = QSpinBox()
        self.config_realtime_limit_spin.setRange(0, 10_000)
        self.config_realtime_limit_spin.setToolTip("실시간 구독 보호 상한입니다. 너무 작으면 후보 감시가 누락될 수 있습니다.")

        self.strategy_settings_validation_badge = StatusBadge("검증 대기", "neutral")
        self.strategy_settings_safety_label = QLabel("OBSERVE 전략 UI는 분석/가상 체결 중심이며 no real orders 경로를 만들지 않습니다.")
        self.strategy_settings_safety_label.setWordWrap(True)

        cycle_group = QGroupBox("실행 주기")
        cycle_form = QFormLayout(cycle_group)
        cycle_form.addRow("평가 주기(초)", self.config_interval_spin)
        cycle_form.addRow("적용 시점", QLabel("OBSERVE 실행 중 변경하면 저장 후 다음 시작부터 적용됩니다."))

        candidate_group = QGroupBox("조건식/후보")
        candidate_form = QFormLayout(candidate_group)
        candidate_form.addRow("조건식 프로필", self.config_condition_enabled_check)
        candidate_form.addRow("최대 후보 수", self.config_max_candidates_spin)

        index_group = QGroupBox("지수 감시")
        index_form = QFormLayout(index_group)
        index_form.addRow("코스피 지수 코드", self.config_kospi_index_edit)
        index_form.addRow("코스닥 지수 코드", self.config_kosdaq_index_edit)

        theme_group = QGroupBox("테마/대장주 감시")
        theme_form = QFormLayout(theme_group)
        theme_form.addRow("대장주 감시 코드", self.config_leader_codes_edit)
        theme_form.addRow("신호주 감시 코드", self.config_signal_codes_edit)
        theme_form.addRow("보유 종목 감시 코드", self.config_holding_codes_edit)

        virtual_group = QGroupBox("가상 체결/리뷰")
        virtual_form = QFormLayout(virtual_group)
        virtual_form.addRow("가상 체결 정책", self.config_fill_policy_combo)
        virtual_form.addRow("리뷰 저장", self.config_review_enabled_check)

        realtime_group = QGroupBox("실시간 구독")
        realtime_form = QFormLayout(realtime_group)
        realtime_form.addRow("실시간 구독 제한", self.config_realtime_limit_spin)
        realtime_form.addRow("보호 구독", QLabel("지수/대장주/신호주/보유 종목은 후보보다 우선 보호됩니다."))

        safety_group = QGroupBox("안전 설정")
        safety_layout = QVBoxLayout(safety_group)
        safety_layout.addWidget(self.strategy_settings_safety_label)

        buttons = QHBoxLayout()
        self.strategy_settings_load_button = QPushButton("설정 다시 불러오기")
        self.strategy_settings_diff_button = QPushButton("변경사항 보기")
        self.strategy_settings_import_button = QPushButton("설정 가져오기")
        self.strategy_settings_export_button = QPushButton("설정 내보내기")
        self.strategy_settings_defaults_button = QPushButton("기본값 복원")
        self.strategy_settings_save_button = QPushButton("설정 저장")
        buttons.addWidget(self.strategy_settings_load_button)
        buttons.addWidget(self.strategy_settings_diff_button)
        buttons.addWidget(self.strategy_settings_import_button)
        buttons.addWidget(self.strategy_settings_export_button)
        buttons.addWidget(self.strategy_settings_defaults_button)
        buttons.addWidget(self.strategy_settings_save_button)
        buttons.addStretch(1)

        self.strategy_settings_diff_view = QTextEdit()
        self.strategy_settings_diff_view.setReadOnly(True)
        self.strategy_settings_diff_view.setMaximumHeight(140)
        self.strategy_settings_diff_view.setPlaceholderText("변경사항 또는 저장값/실행 중 설정 차이가 여기에 표시됩니다.")
        self.strategy_settings_warning_view = QTextEdit()
        self.strategy_settings_warning_view.setReadOnly(True)
        self.strategy_settings_warning_view.setMaximumHeight(120)

        layout.addWidget(cycle_group)
        layout.addWidget(candidate_group)
        layout.addWidget(index_group)
        layout.addWidget(theme_group)
        layout.addWidget(virtual_group)
        layout.addWidget(realtime_group)
        layout.addWidget(safety_group)
        layout.addLayout(buttons)
        layout.addWidget(self.strategy_settings_validation_badge)
        layout.addWidget(QLabel("변경사항 / 실행 중 차이"))
        layout.addWidget(self.strategy_settings_diff_view)
        layout.addWidget(QLabel("검증 메시지"))
        layout.addWidget(self.strategy_settings_warning_view)
        layout.addStretch(1)
        scroll.setWidget(content)

        outer.addLayout(cards)
        outer.addWidget(self.strategy_settings_status_label)
        outer.addWidget(self.strategy_settings_pending_label)
        outer.addWidget(self.strategy_settings_last_refresh_label)
        outer.addWidget(scroll, 1)
        return box

    def _build_review_tab(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        buttons = QHBoxLayout()
        self.review_refresh_button = QPushButton("새로고침")
        self.review_filtered_export_check = QCheckBox("현재 필터만 Export")
        self.review_csv_button = QPushButton("CSV Export")
        self.review_markdown_button = QPushButton("Markdown Export")
        self.review_last_refresh_label = QLabel("마지막 갱신: -")
        self.review_last_refresh_label.setObjectName("review_last_refresh_label")
        self.review_refresh_status_label = QLabel("")
        buttons.addWidget(self.review_refresh_button)
        buttons.addWidget(self.review_filtered_export_check)
        buttons.addWidget(self.review_csv_button)
        buttons.addWidget(self.review_markdown_button)
        buttons.addWidget(self.review_last_refresh_label)
        buttons.addStretch(1)
        self.review_filter_bar = ReviewFilterBar()
        self.review_filter_bar.search_edit.setObjectName("review_filter_input")
        self.review_summary_cards = {
            "total": SummaryCard("리뷰 수", "0", "필터 0 / 전체 0", "neutral"),
            "entered": SummaryCard("진입 성공", "0", "-", "success"),
            "missed": SummaryCard("Missed", "0", "-", "warning"),
            "false_negative": SummaryCard("False Negative", "0", "-", "warning"),
            "false_positive": SummaryCard("False Positive", "0", "-", "danger"),
            "avg_5m": SummaryCard("5m 평균", "-", "-", "neutral"),
            "avg_10m": SummaryCard("10m 평균", "-", "-", "neutral"),
            "avg_20m": SummaryCard("20m 평균", "-", "-", "neutral"),
            "avg_dd_20m": SummaryCard("20m 평균 DD", "-", "-", "neutral"),
            "blocked_rallied": SummaryCard("Blocked 후 반등", "0", "-", "warning"),
            "expired_rallied": SummaryCard("Expired 후 반등", "0", "-", "warning"),
        }
        summary_grid = QGridLayout()
        for index, card in enumerate(self.review_summary_cards.values()):
            summary_grid.addWidget(card, index // 6, index % 6)
        self.review_missed_reason_view = QTextEdit()
        self.review_missed_reason_view.setReadOnly(True)
        self.review_missed_reason_view.setMaximumHeight(80)
        self.review_model = ReviewTableModel()
        self.review_proxy_model = ReviewFilterProxyModel()
        self.review_proxy_model.setSourceModel(self.review_model)
        self.review_proxy_model.setSortRole(ReviewTableModel.SortRole)
        self.review_table = QTableView()
        self.review_table.setObjectName("review_table")
        self.review_table.setModel(self.review_proxy_model)
        self.review_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.review_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.review_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.review_table.setSortingEnabled(True)
        self.review_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.review_table.horizontalHeader().setStretchLastSection(True)
        self.review_detail_panel = CandidateDetailPanel("리뷰 상세", "선택된 리뷰 없음")
        self.review_splitter = QSplitter()
        self.review_splitter.addWidget(self.review_table)
        self.review_splitter.addWidget(self.review_detail_panel)
        self.review_splitter.setSizes([860, 420])
        layout.addLayout(buttons)
        layout.addWidget(self.review_filter_bar)
        layout.addLayout(summary_grid)
        layout.addWidget(QLabel("Missed Reason TOP 5"))
        layout.addWidget(self.review_missed_reason_view)
        layout.addWidget(self.review_refresh_status_label)
        layout.addWidget(self.review_splitter, 1)
        return box

    def _wire_events(self) -> None:
        self.login_button.clicked.connect(self._login)
        self.engine.client.connected.connect(self._on_connection_changed)
        self.account_combo.currentTextChanged.connect(self.engine.set_account)
        self.account_combo.currentTextChanged.connect(lambda _account: self._update_dashboard())
        self.realtime_button.clicked.connect(self.engine.register_realtime)
        self.ordering_check.toggled.connect(self.engine.set_ordering_enabled)
        self.ordering_check.toggled.connect(lambda _enabled: self._update_dashboard())
        self.ordering_check.toggled.connect(lambda _enabled: self._update_watch_runtime_context())
        self.ordering_check.toggled.connect(lambda _enabled: self._display_strategy_settings_status([]))
        self.lookup_button.clicked.connect(self._lookup_name)
        self.save_button.clicked.connect(self._save_current)
        self.delete_button.clicked.connect(self._delete_current)
        self.clear_button.clicked.connect(self._clear_form)
        self.cancel_leg_button.clicked.connect(self._cancel_selected_leg)
        self.modify_leg_button.clicked.connect(self._modify_selected_leg)
        self.tabs.currentChanged.connect(lambda _index: self._save_ui_state())
        self.main_splitter.splitterMoved.connect(lambda *_args: self._save_ui_state())
        self.watch_splitter.splitterMoved.connect(lambda *_args: self._save_ui_state())
        self.strategy_candidate_splitter.splitterMoved.connect(lambda *_args: self._save_ui_state())
        self.review_splitter.splitterMoved.connect(lambda *_args: self._save_ui_state())
        self.log_filter_input.textChanged.connect(lambda _text: self._render_logs())
        self.log_autoscroll_check.toggled.connect(lambda _checked: self._save_ui_state())
        self.log_clear_button.clicked.connect(self._clear_logs)
        self.watch_filter_bar.filters_changed.connect(self._apply_watch_filters)
        self.watch_filter_bar.search_changed.connect(lambda: self.watch_filter_timer.start())
        self.table.selectionModel().selectionChanged.connect(lambda _selected, _deselected: self._load_selected())
        self.table.horizontalHeader().sectionResized.connect(lambda *_args: self._save_ui_state())
        self.table.horizontalHeader().sortIndicatorChanged.connect(lambda *_args: self._save_ui_state())
        self.review_refresh_button.clicked.connect(self.refresh_review_table)
        self.review_csv_button.clicked.connect(self._export_reviews_csv)
        self.review_markdown_button.clicked.connect(self._export_reviews_markdown)
        self.review_filter_bar.filters_changed.connect(self._apply_review_filters)
        self.review_filter_bar.search_changed.connect(lambda: self.review_filter_timer.start())
        self.review_table.selectionModel().selectionChanged.connect(
            lambda _selected, _deselected: self._display_selected_review_detail()
        )
        self.review_table.horizontalHeader().sectionResized.connect(lambda *_args: self._save_ui_state())
        self.review_table.horizontalHeader().sortIndicatorChanged.connect(lambda *_args: self._save_ui_state())
        self.strategy_start_button.clicked.connect(self.start_observe_strategy)
        self.strategy_stop_button.clicked.connect(self.stop_observe_strategy)
        self.strategy_refresh_button.clicked.connect(self.refresh_strategy_candidates)
        self.strategy_candidate_filter_bar.filters_changed.connect(self._apply_strategy_candidate_filters)
        self.strategy_candidate_filter_bar.search_changed.connect(lambda: self.strategy_candidate_filter_timer.start())
        self.strategy_candidate_table.selectionModel().selectionChanged.connect(
            lambda _selected, _deselected: self._display_selected_candidate_detail()
        )
        self.strategy_candidate_table.horizontalHeader().sectionResized.connect(lambda *_args: self._save_ui_state())
        self.strategy_candidate_table.horizontalHeader().sortIndicatorChanged.connect(lambda *_args: self._save_ui_state())
        self.strategy_settings_load_button.clicked.connect(self.load_strategy_settings)
        self.strategy_settings_diff_button.clicked.connect(self.show_strategy_settings_diff)
        self.strategy_settings_import_button.clicked.connect(self.import_strategy_settings_json)
        self.strategy_settings_export_button.clicked.connect(self.export_strategy_settings_json)
        self.strategy_settings_defaults_button.clicked.connect(self.restore_default_strategy_settings)
        self.strategy_settings_save_button.clicked.connect(self.save_strategy_settings)
        for widget in [
            self.config_interval_spin,
            self.config_max_candidates_spin,
            self.config_realtime_limit_spin,
        ]:
            widget.valueChanged.connect(lambda _value: self._on_strategy_settings_changed())
        for widget in [
            self.config_leader_codes_edit,
            self.config_signal_codes_edit,
            self.config_holding_codes_edit,
            self.config_kospi_index_edit,
            self.config_kosdaq_index_edit,
        ]:
            widget.textChanged.connect(lambda _text: self._on_strategy_settings_changed())
        self.config_condition_enabled_check.toggled.connect(lambda _checked: self._on_strategy_settings_changed())
        self.config_review_enabled_check.toggled.connect(lambda _checked: self._on_strategy_settings_changed())
        self.config_fill_policy_combo.currentIndexChanged.connect(lambda _index: self._on_strategy_settings_changed())
        self.engine.log_handlers.append(self._append_log)
        self.engine.item_handlers.append(lambda item: self._schedule_refresh_table())
        self.engine.alert_handlers.append(self._show_alert)
        self.engine.order_handlers.append(lambda result: self._schedule_refresh_table())
        self._update_strategy_buttons()

    def _load_initial(self) -> None:
        for line in self.db.recent_logs():
            self.log_view.append(line)
        self.refresh_table()
        self.refresh_strategy_candidates()
        self.refresh_review_table()
        self.load_strategy_settings()
        if self.mock_mode:
            self._login()

    def start_observe_strategy(self) -> None:
        if self.strategy_runtime is None:
            self._strategy_warning(self.strategy_runtime_unavailable_reason or "STRATEGY_RUNTIME_UNAVAILABLE")
            return
        if self._strategy_starting:
            self._strategy_warning("STRATEGY_RUNTIME_START_ALREADY_RUNNING")
            return
        if self._strategy_running:
            self._strategy_warning("STRATEGY_RUNTIME_ALREADY_STARTED")
            return
        self._strategy_starting = True
        self.strategy_status_label.setText("OBSERVE starting...")
        self.strategy_start_button.setEnabled(False)
        self.strategy_stop_button.setEnabled(False)
        self._update_strategy_buttons()
        QApplication.processEvents()
        started = perf_counter()
        try:
            snapshot = self.strategy_runtime.start(timing_callback=self._strategy_start_timing)
        except TypeError as exc:
            if "timing_callback" not in str(exc):
                self._strategy_start_failed(exc)
                return
            try:
                snapshot = self.strategy_runtime.start()
            except Exception as fallback_exc:
                self._strategy_start_failed(fallback_exc)
                return
        except Exception as exc:
            self._strategy_start_failed(exc)
            return
        self._strategy_starting = False
        self._strategy_running = True
        self._strategy_last_snapshot = snapshot
        interval_ms = max(1, self.strategy_runtime.config.evaluation_interval_sec) * 1000
        self.strategy_timer.start(interval_ms)
        self._display_strategy_snapshot(snapshot, perf_counter() - started)
        self.refresh_strategy_candidates()
        self._strategy_last_auto_refresh_at = perf_counter()
        self._update_strategy_buttons()

    def _strategy_start_timing(self, step: str, duration_sec: float) -> None:
        message = f"OBSERVE start step: {step} {duration_sec:.3f}s"
        self._append_log(message)
        self.statusBar().showMessage(message, 5000)
        QApplication.processEvents()

    def _strategy_start_failed(self, exc: Exception) -> None:
        self._strategy_starting = False
        self.strategy_timer.stop()
        self._strategy_running = False
        self._strategy_warning(f"STRATEGY_RUNTIME_START_FAILED:{exc}")
        self._update_strategy_buttons()

    def stop_observe_strategy(self) -> None:
        if self._strategy_starting:
            self._strategy_warning("STRATEGY_RUNTIME_START_IN_PROGRESS")
            return
        if not self._strategy_running:
            self._strategy_warning("STRATEGY_RUNTIME_ALREADY_STOPPED")
            self.strategy_timer.stop()
            self._update_strategy_buttons()
            return
        self.strategy_timer.stop()
        snapshot = None
        try:
            if self.strategy_runtime is not None:
                snapshot = self.strategy_runtime.stop()
        except Exception as exc:
            self._strategy_warning(f"STRATEGY_RUNTIME_STOP_FAILED:{exc}")
        self._strategy_running = False
        self._strategy_cycle_running = False
        if snapshot is not None:
            self._strategy_last_snapshot = snapshot
            self._display_strategy_snapshot(snapshot, 0.0)
        else:
            self.strategy_status_label.setText("OBSERVE stopped")
        self._update_strategy_buttons()

    def _run_strategy_cycle(self) -> None:
        if self.strategy_runtime is None or not self._strategy_running:
            return
        if self._strategy_cycle_running:
            self._strategy_warning("STRATEGY_CYCLE_REENTRY_SKIPPED")
            return
        self._strategy_cycle_running = True
        started = perf_counter()
        try:
            snapshot = self.strategy_runtime.cycle()
            duration = perf_counter() - started
            if duration > self.strategy_runtime.config.evaluation_interval_sec:
                snapshot.warnings.append(f"STRATEGY_CYCLE_SLOW:{duration:.2f}s")
            self._refresh_strategy_candidates_after_cycle(snapshot)
            self._strategy_last_snapshot = snapshot
            self._display_strategy_snapshot(snapshot, duration)
        except Exception as exc:
            self._strategy_warning(f"STRATEGY_CYCLE_FAILED:{exc}")
        finally:
            self._strategy_cycle_running = False

    def _display_strategy_snapshot(self, snapshot: StrategyRuntimeSnapshot, duration_sec: float) -> None:
        warnings = self._dedupe_text(snapshot.warnings or [])
        if warnings:
            self._strategy_last_warning_at = datetime.now().replace(microsecond=0).isoformat()
        self.strategy_status_label.setText("OBSERVE running" if snapshot.started else "OBSERVE stopped")
        self.dashboard_last_snapshot_label.setText(f"대시보드 마지막 스냅샷: {snapshot.cycle_at or self._timestamp_text()}")
        self.strategy_snapshot_label.setText(
            "cycle: {cycle} / duration: {duration:.3f}s / candidates: {candidates} / evaluated: {evaluated} / "
            "db writes: {writes} / gates: {gates} / entry: {entry} / virtual orders: {orders} / "
            "positions: {positions} / reviews: {reviews} / subs: {subs} / ui: {ui_refresh}/{ui_skipped} / "
            "warnings: {warnings} / last warning: {warning_at}".format(
                cycle=snapshot.cycle_at or "-",
                duration=duration_sec,
                candidates=snapshot.active_candidate_count,
                evaluated=getattr(snapshot, "evaluated_candidate_count", 0),
                writes=getattr(snapshot, "db_write_count_per_cycle", 0),
                gates=snapshot.gate_result_count,
                entry=snapshot.entry_plan_count,
                orders=snapshot.virtual_order_count,
                positions=snapshot.open_position_count,
                reviews=snapshot.review_count,
                subs=getattr(snapshot, "subscription_active_count", 0),
                ui_refresh=getattr(snapshot, "ui_refresh_count", 0),
                ui_skipped=getattr(snapshot, "ui_refresh_skipped_count", 0),
                warnings=len(warnings),
                warning_at=self._strategy_last_warning_at or "-",
            )
        )
        self._display_strategy_readiness(snapshot)
        if warnings:
            self.strategy_warning_view.setPlainText("\n".join(warnings[-20:]))
        else:
            self.strategy_warning_view.clear()
        self._update_dashboard(snapshot)

    def _display_strategy_readiness(self, snapshot: StrategyRuntimeSnapshot) -> None:
        lines = [
            "readiness: conditions={conditions} unresolved={unresolved} / theme_engine={engine} data={data} active={active_themes} watch={watch_themes} candidates={candidate_themes}".format(
                conditions=getattr(snapshot, "condition_profiles_count", 0),
                unresolved=getattr(snapshot, "unresolved_condition_profiles_count", 0),
                engine=getattr(snapshot, "theme_engine_status", "stopped"),
                data=getattr(snapshot, "theme_data_status", "warming"),
                active_themes=getattr(snapshot, "active_theme_count", 0),
                watch_themes=getattr(snapshot, "watch_theme_count", 0),
                candidate_themes=getattr(snapshot, "candidate_theme_count", 0),
            ),
            "active candidates={active} themed={mapped} no_active_theme={unmapped} coverage={coverage:.2f}% / protected subs={protected}".format(
                active=snapshot.active_candidate_count,
                mapped=getattr(snapshot, "active_candidates_with_active_theme", 0),
                unmapped=getattr(snapshot, "active_candidates_without_active_theme", 0),
                coverage=float(getattr(snapshot, "theme_context_coverage_pct", 0.0) or 0.0),
                protected=getattr(snapshot, "protected_subscription_usage", "") or "-",
            ),
            "quality: actionable={actionable} data_wait={data_wait} discovery={discovery} unmapped={unmapped} invalid={invalid}".format(
                actionable=getattr(snapshot, "quality_actionable_count", 0),
                data_wait=getattr(snapshot, "quality_data_wait_count", 0),
                discovery=getattr(snapshot, "quality_discovery_only_count", 0),
                unmapped=getattr(snapshot, "quality_unmapped_count", 0),
                invalid=getattr(snapshot, "quality_invalid_code_count", 0),
            ),
            "flow: market={market} warmup={warmup} gate_skip={skip}".format(
                market=getattr(snapshot, "market_session_status", "open") or "open",
                warmup=getattr(snapshot, "data_warmup_status", "ready") or "ready",
                skip=getattr(snapshot, "gate_skip_reason", "") or "-",
            ),
            "candidate subscriptions: selected={selected} skipped_discovery={discovery} skipped_unmapped={unmapped}".format(
                selected=getattr(snapshot, "candidate_subscription_selected_count", 0),
                discovery=getattr(snapshot, "candidate_subscription_skipped_discovery_count", 0),
                unmapped=getattr(snapshot, "candidate_subscription_skipped_unmapped_count", 0),
            ),
        ]
        startup_warnings = self._dedupe_text(getattr(self.strategy_runtime, "startup_warnings", []) if self.strategy_runtime is not None else [])
        if startup_warnings:
            lines.append("startup warnings: " + ", ".join(startup_warnings[-8:]))
        self.strategy_readiness_view.setPlainText("\n".join(lines))

    def refresh_strategy_candidates(self) -> None:
        self._strategy_ui_refresh_count += 1
        selected_candidate_id = self._selected_strategy_candidate_id()
        trade_date = self._strategy_trade_date()
        try:
            candidates = self.db.list_candidates(trade_date=trade_date)
        except Exception as exc:
            message = f"후보 새로고침 실패: {exc}"
            self.strategy_candidate_refresh_status_label.setText(message)
            self._strategy_warning(f"CANDIDATE_REFRESH_FAILED:{exc}")
            return
        themed_codes = set()
        theme_text_by_code = {}
        for candidate in candidates:
            metadata, metadata_warning = self._safe_candidate_metadata(candidate)
            if metadata_warning:
                self._strategy_warning(metadata_warning)
            contexts = self._candidate_theme_contexts(candidate)
            if contexts:
                themed_codes.add(candidate.code)
                theme_text_by_code[candidate.code] = self._theme_context_search_text(contexts)
        candidates.sort(key=lambda candidate: candidate.last_seen_at or "", reverse=True)
        candidates.sort(key=lambda candidate: self._candidate_quality_priority(candidate, candidate.code in themed_codes))
        candidates.sort(key=lambda candidate: self._candidate_state_priority(candidate.state))
        candidates = candidates[:200]
        themed_codes = {code for code in themed_codes if any(candidate.code == code for candidate in candidates)}
        theme_text_by_code = {code: text for code, text in theme_text_by_code.items() if code in themed_codes}
        self._set_strategy_candidate_dashboard_counts(candidates)
        self.strategy_candidate_model.set_candidates(candidates, themed_codes, theme_text_by_code)
        self._set_last_refresh(self.strategy_candidate_last_refresh_label)
        message = f"후보 새로고침 완료: {len(candidates)}개"
        self.strategy_candidate_refresh_status_label.setText(message)
        self.statusBar().showMessage(message, 3000)
        self._update_dashboard()
        if selected_candidate_id is not None and self._select_strategy_candidate(selected_candidate_id):
            self._display_selected_candidate_detail()
        else:
            self.strategy_candidate_table.clearSelection()
            self.strategy_candidate_detail_panel.show_empty()

    def _apply_strategy_candidate_filters(self) -> None:
        self.strategy_candidate_proxy_model.set_search_text(self.strategy_candidate_filter_bar.search_text())
        self.strategy_candidate_proxy_model.set_state_filter(self.strategy_candidate_filter_bar.state_filter())
        self.strategy_candidate_proxy_model.set_recover_only(self.strategy_candidate_filter_bar.recover_only())
        self.strategy_candidate_proxy_model.set_theme_filter(self.strategy_candidate_filter_bar.theme_filter())
        self.strategy_candidate_proxy_model.set_quality_filter(self.strategy_candidate_filter_bar.quality_filter())
        selected_candidate_id = self._selected_strategy_candidate_id()
        if selected_candidate_id is not None and self._select_strategy_candidate(selected_candidate_id):
            self._display_selected_candidate_detail()
        else:
            self.strategy_candidate_table.clearSelection()
            self.strategy_candidate_detail_panel.show_empty()

    def _selected_strategy_candidate_id(self) -> Optional[int]:
        if not hasattr(self, "strategy_candidate_table"):
            return None
        current = self.strategy_candidate_table.currentIndex()
        if not current.isValid():
            return None
        source_index = self.strategy_candidate_proxy_model.mapToSource(current)
        candidate_id = self.strategy_candidate_model.data(source_index, CandidateTableModel.CandidateIdRole)
        return int(candidate_id) if candidate_id is not None else None

    def _select_strategy_candidate(self, candidate_id: int) -> bool:
        source_candidate = self.strategy_candidate_model.candidate_by_id(candidate_id)
        if source_candidate is None:
            return False
        for source_row in range(self.strategy_candidate_model.rowCount()):
            candidate = self.strategy_candidate_model.candidate_at(source_row)
            if candidate is None or candidate.id != candidate_id:
                continue
            source_index = self.strategy_candidate_model.index(source_row, 0)
            proxy_index = self.strategy_candidate_proxy_model.mapFromSource(source_index)
            if not proxy_index.isValid():
                return False
            self.strategy_candidate_table.setCurrentIndex(proxy_index)
            selection_model = self.strategy_candidate_table.selectionModel()
            selection_model.select(proxy_index, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
            return True
        return False

    def _selected_strategy_candidate(self):
        current = self.strategy_candidate_table.currentIndex()
        if not current.isValid():
            return None
        source_index = self.strategy_candidate_proxy_model.mapToSource(current)
        return self.strategy_candidate_model.data(source_index, CandidateTableModel.CandidateRole)

    def _display_selected_candidate_detail(self) -> None:
        candidate = self._selected_strategy_candidate()
        if candidate is None or candidate.id is None:
            self.strategy_candidate_detail_panel.show_empty()
            return
        try:
            detail_text = self._candidate_detail_text(candidate)
        except Exception as exc:
            detail_text = f"상세 조회 실패: {exc}"
            self._strategy_warning(f"CANDIDATE_DETAIL_FAILED:{exc}")
        self.strategy_candidate_detail_panel.show_detail(f"{candidate.code} {candidate.name}".strip(), detail_text)

    def _candidate_has_active_theme(self, candidate) -> bool:
        return bool(self._candidate_theme_contexts(candidate))

    @staticmethod
    def _candidate_entry_excluded(candidate) -> bool:
        return candidate_is_discovery_only(candidate)

    def _candidate_theme_contexts(self, candidate):
        try:
            return self._theme_context_provider().themes_for_code(candidate.code)
        except Exception:
            return []

    def _theme_context_search_text(self, contexts) -> str:
        parts = []
        for context in contexts:
            parts.extend(
                [
                    context.theme_id,
                    context.theme_name,
                    self._value_text(context.status),
                    self._value_text(context.relation_type),
                ]
            )
        return " ".join(str(part or "") for part in parts)

    def _theme_context_provider(self) -> DynamicThemeContextProvider:
        pipeline = getattr(getattr(self, "strategy_runtime", None), "gate_pipeline", None)
        provider = getattr(pipeline, "theme_context_provider", None)
        if provider is not None:
            return provider
        return DynamicThemeContextProvider(ThemeEngineRepository(self.db))

    def _candidate_detail_text(self, candidate) -> str:
        loaded = self.db.load_candidate_by_id(candidate.id) if candidate.id is not None else None
        candidate = loaded or candidate
        metadata, metadata_warning = self._safe_candidate_metadata(candidate)
        sections = [
            self._candidate_basic_detail(candidate, metadata, metadata_warning),
            self._candidate_dynamic_theme_detail(candidate),
            self._candidate_indicator_detail(candidate),
            self._candidate_entry_plan_detail(candidate),
            self._candidate_virtual_activity_detail(candidate),
            self._candidate_event_detail(candidate),
        ]
        return "\n\n".join(section for section in sections if section)

    def _candidate_basic_detail(self, candidate, metadata: dict, metadata_warning: str) -> str:
        lines = [
            "[기본 정보]",
            f"candidate id: {candidate.id or '-'}",
            f"code: {candidate.code}",
            f"name: {candidate.name}",
            f"market: {candidate.market or '-'}",
            f"trade_date: {candidate.trade_date or '-'}",
            f"state: {self._value_text(candidate.state)}",
            f"block_type: {self._value_text(candidate.block_type)}",
            f"can_recover: {'Y' if candidate.can_recover else 'N'}",
            f"detected_at: {candidate.detected_at or '-'}",
            f"last_seen_at: {candidate.last_seen_at or '-'}",
            f"expires_at: {candidate.expires_at or '-'}",
            f"strategy_profile: {self._value_text(candidate.strategy_profile) or '-'}",
            "condition_names: " + (", ".join(candidate.condition_names) if candidate.condition_names else "-"),
            "theme_ids: " + (", ".join(candidate.theme_ids) if candidate.theme_ids else "-"),
            f"quality_status: {candidate_quality_status(candidate, self._candidate_has_active_theme(candidate))}",
            f"quality_reason: {self._metadata_text(metadata, 'quality_reason') or '-'}",
            f"active_dynamic_theme: {'Y' if self._candidate_has_active_theme(candidate) else 'N'}",
            f"entry_evaluation_target: {'N' if self._candidate_entry_excluded(candidate) else 'Y'}",
            f"entry_excluded_reason: {self._metadata_text(metadata, 'entry_excluded_reason') or '-'}",
            f"subscription_excluded_reason: {self._metadata_text(metadata, 'subscription_excluded_reason') or '-'}",
            f"warmup_wait_reason: {self._metadata_text(metadata, 'warmup_wait_reason') or '-'}",
            f"best_theme_id: {self._metadata_text(metadata, 'best_theme_id') or '-'}",
            f"best_gate_result_key: {self._metadata_text(metadata, 'best_gate_result_key') or '-'}",
            f"sub_status: {self._metadata_text(metadata, 'sub_status') or '-'}",
            f"block reasons: {self._block_reason_summary(metadata) or '-'}",
        ]
        if metadata_warning:
            lines.append(f"metadata warning: {metadata_warning}")
        return "\n".join(lines)

    def _candidate_dynamic_theme_detail(self, candidate) -> str:
        contexts = self._candidate_theme_contexts(candidate)
        lines = ["[테마 매핑]"]
        if not contexts:
            lines.append("-")
            return "\n".join(lines)
        for context in contexts:
            lines.append(
                "theme_id={theme_id} / theme_name={theme_name} / status={sub_theme} / "
                "relation={profile} / trade_eligible={leader} / membership={signal} / rank={priority}".format(
                    theme_id=context.theme_id or "-",
                    theme_name=context.theme_name or "-",
                    sub_theme=self._value_text(context.status) or "-",
                    profile=self._value_text(context.relation_type) or "-",
                    leader="Y" if context.trade_eligible else "N",
                    signal=f"{float(context.membership_score or 0.0):.2f}",
                    priority=context.rank or 0,
                )
            )
        return "\n".join(lines)

    def _candidate_indicator_detail(self, candidate) -> str:
        snapshots = self.db.list_indicator_snapshots(candidate.id)
        lines = ["[최신 지표 스냅샷]"]
        if not snapshots:
            lines.append("-")
            return "\n".join(lines)
        snapshot = snapshots[-1]
        lines.extend(
            [
                f"created_at: {snapshot.created_at or '-'}",
                f"price: {self._money(snapshot.price)}",
                f"vwap: {self._metric(snapshot.vwap)}",
                f"ema20_5m: {self._metric(snapshot.ema20_5m)}",
                f"base_line_120: {self._metric(snapshot.base_line_120)}",
                f"envelope_mid: {self._metric(snapshot.envelope_mid)}",
                f"day_high: {self._money(snapshot.day_high)}",
                f"day_low: {self._money(snapshot.day_low)}",
                f"day_mid: {self._metric(snapshot.day_mid)}",
                f"pullback_pct: {self._metric(snapshot.pullback_pct)}",
                f"volume_reaccel: {snapshot.volume_reaccel}",
                f"failed_low_break_rebound: {snapshot.failed_low_break_rebound}",
                f"chase_risk: {snapshot.chase_risk}",
            ]
        )
        return "\n".join(lines)

    def _candidate_entry_plan_detail(self, candidate) -> str:
        plans = self.db.list_entry_plans(candidate.id)
        lines = ["[진입 계획]"]
        if not plans:
            lines.append("-")
            return "\n".join(lines)
        for plan in plans[-3:]:
            lines.append(
                "entry_type={entry_type} / base_price_source={source} / limit_price={price} / "
                "tick_offset={tick_offset} / max_chase_pct={chase} / split_plan={split_plan} / "
                "fill_policy={fill_policy} / created_at={created_at}".format(
                    entry_type=plan.entry_type or "-",
                    source=plan.base_price_source or "-",
                    price=self._money(plan.limit_price),
                    tick_offset=plan.tick_offset,
                    chase=self._metric(plan.max_chase_pct),
                    split_plan=plan.split_plan,
                    fill_policy=self._value_text(plan.fill_policy),
                    created_at=plan.created_at or "-",
                )
            )
        return "\n".join(lines)

    def _candidate_virtual_activity_detail(self, candidate) -> str:
        lines = ["[가상 주문/포지션/리뷰]"]
        virtual_orders = self.db.list_virtual_orders(candidate.id)
        virtual_positions = self.db.list_virtual_positions(candidate.id)
        reviews = self.db.list_trade_reviews(candidate.id)
        lines.append("virtual orders:")
        if virtual_orders:
            for item in virtual_orders[-3:]:
                lines.append(
                    "  status={status} / limit_price={limit_price} / virtual_fill_price={fill_price} / "
                    "fill_policy={fill_policy} / submitted_at={submitted_at} / filled_at={filled_at} / "
                    "cancelled_at={cancelled_at} / unfilled_reason={reason}".format(
                        status=self._value_text(item.status),
                        limit_price=self._money(item.limit_price),
                        fill_price=self._money(item.virtual_fill_price),
                        fill_policy=self._value_text(item.fill_policy),
                        submitted_at=item.submitted_at or "-",
                        filled_at=item.filled_at or "-",
                        cancelled_at=item.cancelled_at or "-",
                        reason=item.unfilled_reason or "-",
                    )
                )
        else:
            lines.append("  -")
        lines.append("virtual positions:")
        if virtual_positions:
            for item in virtual_positions[-3:]:
                lines.append(
                    "  entry_price={entry_price} / quantity={quantity} / opened_at={opened_at} / "
                    "closed_at={closed_at} / close_price={close_price} / close_reason={reason} / "
                    "max_return_pct={max_return} / max_drawdown_pct={drawdown} / realized_return_pct={realized}".format(
                        entry_price=self._money(item.entry_price),
                        quantity=item.quantity,
                        opened_at=item.opened_at or "-",
                        closed_at=item.closed_at or "-",
                        close_price=self._money(item.close_price),
                        reason=item.close_reason or "-",
                        max_return=self._metric(item.max_return_pct),
                        drawdown=self._metric(item.max_drawdown_pct),
                        realized=self._metric(item.realized_return_pct),
                    )
                )
        else:
            lines.append("  -")
        lines.append("trade reviews:")
        if reviews:
            for item in reviews[-3:]:
                lines.append(
                    "  final_status={status} / max_return_5m={r5} / max_return_10m={r10} / "
                    "max_return_20m={r20} / max_drawdown_20m={dd20} / missed_reason={missed} / "
                    "false_negative={fn} / false_positive={fp}".format(
                        status=item.final_status or "-",
                        r5=self._metric(item.max_return_5m),
                        r10=self._metric(item.max_return_10m),
                        r20=self._metric(item.max_return_20m),
                        dd20=self._metric(item.max_drawdown_20m),
                        missed=item.missed_reason or "-",
                        fn=item.false_negative_flag,
                        fp=item.false_positive_flag,
                    )
                )
        else:
            lines.append("  -")
        return "\n".join(lines)

    def _candidate_event_detail(self, candidate) -> str:
        events = self.db.list_candidate_events(candidate.id)
        lines = ["[이벤트 타임라인 - 시간순]"]
        if not events:
            lines.append("-")
            return "\n".join(lines)
        for event in events:
            lines.append(
                "event_type={event_type} / from_state={from_state} / to_state={to_state} / "
                "source={source} / reason={reason} / created_at={created_at} / payload={payload}".format(
                    event_type=event.event_type or "-",
                    from_state=self._value_text(event.from_state) or "-",
                    to_state=self._value_text(event.to_state) or "-",
                    source=self._value_text(event.source) or "-",
                    reason=event.reason or "-",
                    created_at=event.created_at or "-",
                    payload=self._payload_summary(event.payload),
                )
            )
        return "\n".join(lines)

    @staticmethod
    def _value_text(value) -> str:
        if value is None:
            return ""
        return str(value.value if hasattr(value, "value") else value)

    @staticmethod
    def _payload_summary(payload) -> str:
        if not payload:
            return "-"
        text = str(payload)
        return text if len(text) <= 160 else text[:157] + "..."

    def _set_strategy_candidate_dashboard_counts(self, candidates) -> None:
        self._strategy_dashboard_candidate_count = len(candidates)
        self._strategy_dashboard_ready_count = sum(1 for candidate in candidates if candidate.state == CandidateState.READY)
        self._strategy_dashboard_blocked_count = sum(1 for candidate in candidates if candidate.state == CandidateState.BLOCKED)

    def _update_dashboard(self, snapshot: Optional[StrategyRuntimeSnapshot] = None) -> None:
        if not hasattr(self, "dashboard_cards"):
            return
        current_snapshot = snapshot or self._strategy_last_snapshot
        order_enabled = self.ordering_check.isChecked() if hasattr(self, "ordering_check") else False
        live_order_enabled = not self.mock_mode and order_enabled

        account = self.account_combo.currentText() if hasattr(self, "account_combo") else ""
        connection_text = self.connection_label.text() if hasattr(self, "connection_label") else "연결 안됨"
        connection_tone = "success" if "완료" in connection_text else "warning" if "요청" in connection_text else "neutral"
        self.connection_label.set_status(connection_text, connection_tone)
        self._set_dashboard_card("connection", connection_text, account or "계좌 -", connection_tone)

        mode_text = "MOCK" if self.mock_mode else "실거래 가능"
        mode_tone = "danger" if live_order_enabled else "success" if self.mock_mode else "warning"
        self.mode_label.set_status(mode_text, mode_tone)
        self._set_dashboard_card("mode", mode_text, "주문 ON" if order_enabled else "주문 OFF", mode_tone)

        ordering_tone = "danger" if live_order_enabled else "warning" if order_enabled else "neutral"
        self._set_dashboard_card("ordering", "ON" if order_enabled else "OFF", "engine enabled" if order_enabled else "engine disabled", ordering_tone)

        if self.strategy_runtime is None:
            observe_text = "unavailable"
            observe_detail = self.strategy_runtime_unavailable_reason or "-"
            observe_tone = "unavailable"
        else:
            observe_running = self._strategy_running or bool(current_snapshot and current_snapshot.started)
            observe_text = "running" if observe_running else "stopped"
            observe_detail = getattr(current_snapshot, "cycle_at", "") if current_snapshot is not None else "-"
            observe_tone = "success" if observe_running else "neutral"
        self._set_dashboard_card("observe", observe_text, observe_detail or "-", observe_tone)

        if current_snapshot is not None:
            candidate_count = getattr(current_snapshot, "active_candidate_count", 0)
        else:
            candidate_count = self._strategy_dashboard_candidate_count
        self._set_dashboard_card("candidates", str(candidate_count), "active candidates", "neutral")
        self._set_dashboard_card(
            "ready_blocked",
            f"{self._strategy_dashboard_ready_count} / {self._strategy_dashboard_blocked_count}",
            "READY / BLOCKED",
            "warning" if self._strategy_dashboard_blocked_count else "neutral",
        )

        theme_status = getattr(current_snapshot, "theme_engine_status", "stopped") if current_snapshot is not None else "warming"
        theme_data = getattr(current_snapshot, "theme_data_status", "warming") if current_snapshot is not None else "warming"
        active_themes = int(getattr(current_snapshot, "active_theme_count", 0) or 0) if current_snapshot is not None else 0
        watch_themes = int(getattr(current_snapshot, "watch_theme_count", 0) or 0) if current_snapshot is not None else 0
        candidate_themes = int(getattr(current_snapshot, "candidate_theme_count", 0) or 0) if current_snapshot is not None else 0
        theme_tone = "success" if theme_status == "running" and active_themes else "warning" if theme_status == "running" else "neutral"
        self._set_dashboard_card("theme_engine", str(theme_status), f"data {theme_data}", theme_tone)
        self._set_dashboard_card("active_theme", str(active_themes), "trade radar", "success" if active_themes else "neutral")
        self._set_dashboard_card("watch_theme", str(watch_themes), "warming watch", "warning" if watch_themes else "neutral")
        self._set_dashboard_card("theme_candidates", str(candidate_themes), "canonical candidates", "neutral")

        subscription = ""
        if current_snapshot is not None:
            subscription = getattr(current_snapshot, "protected_subscription_usage", "") or str(getattr(current_snapshot, "subscription_active_count", 0) or "")
        self._set_dashboard_card("subscription", subscription or "-", "WS clients / protected", self._subscription_tone(subscription))

        warnings = []
        if hasattr(self, "strategy_warning_view"):
            warnings = self._dedupe_text(self.strategy_warning_view.toPlainText().splitlines())
        warning_tone = "danger" if warnings else "neutral"
        self._set_dashboard_card("warnings", str(len(warnings)), f"last: {self._strategy_last_warning_at or '-'}", warning_tone)

    def _set_dashboard_card(self, key: str, value: str, detail: str, tone: str) -> None:
        payload = (str(value), str(detail), str(tone))
        if self._dashboard_last_values.get(key) == payload:
            return
        self._dashboard_last_values[key] = payload
        self.dashboard_cards[key].set_summary(*payload)

    @staticmethod
    def _subscription_tone(value: str) -> str:
        if "/" not in str(value or ""):
            return "neutral"
        used_text, limit_text = str(value).split("/", 1)
        try:
            used = int(used_text)
            limit = int(limit_text)
        except ValueError:
            return "neutral"
        if limit <= 0:
            return "neutral"
        ratio = used / limit
        if ratio >= 0.95:
            return "danger"
        if ratio >= 0.8:
            return "warning"
        return "success"

    def _refresh_strategy_candidates_after_cycle(self, snapshot: StrategyRuntimeSnapshot) -> None:
        if self._should_auto_refresh_strategy_candidates(snapshot):
            self.refresh_strategy_candidates()
            self._strategy_last_auto_refresh_at = perf_counter()
        else:
            self._strategy_ui_refresh_skipped_count += 1
        snapshot.ui_refresh_count = self._strategy_ui_refresh_count
        snapshot.ui_refresh_skipped_count = self._strategy_ui_refresh_skipped_count

    def _should_auto_refresh_strategy_candidates(self, snapshot: StrategyRuntimeSnapshot) -> bool:
        previous = self._strategy_last_snapshot
        if self._strategy_last_auto_refresh_at <= 0:
            return True
        if previous is not None and previous.active_candidate_count != snapshot.active_candidate_count:
            return True
        if any(
            int(getattr(snapshot, name, 0) or 0) > 0
            for name in [
                "expired_count",
                "candidate_save_count",
                "entry_plan_count",
                "virtual_order_count",
                "filled_order_count",
                "open_position_count",
                "exit_decision_count",
                "review_count",
                "virtual_order_status_change_count",
            ]
        ):
            return True
        return perf_counter() - self._strategy_last_auto_refresh_at >= self._strategy_auto_refresh_interval_sec

    @staticmethod
    def _candidate_state_priority(state: CandidateState) -> int:
        return {
            CandidateState.READY: 0,
            CandidateState.BLOCKED: 1,
            CandidateState.WATCHING: 2,
            CandidateState.DETECTED: 3,
        }.get(state, 9)

    @staticmethod
    def _candidate_quality_priority(candidate, has_active_theme: bool) -> int:
        return {
            "actionable": 0,
            "data_wait": 1,
            "discovery_only": 2,
            "unmapped": 3,
            "invalid_code": 4,
        }.get(candidate_quality_status(candidate, has_active_theme), 9)

    @staticmethod
    def _safe_candidate_metadata(candidate) -> tuple[dict, str]:
        metadata = candidate.metadata
        if isinstance(metadata, dict):
            return metadata, ""
        return {}, f"CANDIDATE_METADATA_INVALID:{candidate.code}"

    @staticmethod
    def _metadata_text(metadata: dict, key: str) -> str:
        value = metadata.get(key, "")
        return "" if value is None else str(value)

    @staticmethod
    def _block_reason_summary(metadata: dict) -> str:
        reasons = []
        for record in dict(metadata.get("block_reasons_by_theme", {})).values():
            if isinstance(record, dict):
                reasons.extend(record.get("reason_codes", []))
        return ", ".join(str(reason) for reason in reasons[:5])

    def _strategy_trade_date(self) -> str:
        collector = getattr(self.strategy_runtime, "candidate_collector", None)
        if collector is not None and hasattr(collector, "_trade_date"):
            try:
                return collector._trade_date()
            except Exception:
                pass
        if self.mock_mode:
            latest = self._latest_candidate_trade_date()
            if latest:
                return latest
        return date.today().isoformat()

    def _latest_candidate_trade_date(self) -> str:
        try:
            dates = [candidate.trade_date for candidate in self.db.list_candidates() if candidate.trade_date]
        except Exception:
            return ""
        return max(dates, default="")

    def _strategy_warning(self, message: str) -> None:
        self._strategy_last_warning_at = datetime.now().replace(microsecond=0).isoformat()
        if hasattr(self, "strategy_warning_view"):
            current = self.strategy_warning_view.toPlainText().splitlines()
            current = self._dedupe_text(current + [str(message)])
            self.strategy_warning_view.setPlainText("\n".join(current[-20:]))
        self._append_log(f"OBSERVE warning: {message}")
        self._update_dashboard()

    def _update_strategy_buttons(self) -> None:
        has_runtime = self.strategy_runtime is not None
        self.strategy_start_button.setEnabled(has_runtime and not self._strategy_running and not self._strategy_starting)
        self.strategy_stop_button.setEnabled(has_runtime and self._strategy_running and not self._strategy_starting)
        self.strategy_refresh_button.setEnabled(True)
        if self._strategy_starting:
            self.strategy_status_label.setText("OBSERVE starting...")
        if not has_runtime and self.strategy_runtime_unavailable_reason:
            self.strategy_status_label.setText("OBSERVE unavailable")
        self._update_dashboard()

    def load_strategy_settings(self) -> None:
        try:
            result = self.strategy_config_repository.load()
        except Exception as exc:
            self._strategy_settings_warning(f"CONFIG_LOAD_FAILED:{exc}")
            return
        self._strategy_saved_config = result.config
        self._strategy_settings_loading = True
        try:
            self._display_strategy_settings(result.config)
        finally:
            self._strategy_settings_loading = False
        self._strategy_settings_dirty = False
        self._set_last_refresh(self.strategy_settings_last_refresh_label, "마지막 로드")
        self._display_strategy_settings_status(result.warnings)

    def save_strategy_settings(self) -> None:
        try:
            config = self._strategy_settings_to_config()
            errors, ui_warnings = self._validate_strategy_settings(config)
            if errors:
                raise ValueError("; ".join(errors))
            result = self.strategy_config_repository.save(config)
        except Exception as exc:
            self._strategy_settings_warning(f"CONFIG_SAVE_FAILED:{exc}")
            return
        self._strategy_saved_config = result.config
        self._strategy_settings_loading = True
        try:
            self._display_strategy_settings(result.config)
        finally:
            self._strategy_settings_loading = False
        self._strategy_settings_dirty = False
        self._set_last_refresh(self.strategy_settings_last_refresh_label, "마지막 저장")
        warnings = list(ui_warnings) + list(result.warnings)
        if self._strategy_running:
            warnings.append("CONFIG_SAVED_FOR_NEXT_OBSERVE_START")
        self._display_strategy_settings_status(warnings)

    def show_strategy_settings_diff(self) -> None:
        try:
            config = self._strategy_settings_to_config()
        except Exception as exc:
            self._strategy_settings_warning(f"CONFIG_DIFF_FAILED:{exc}")
            return
        saved_lines = self._strategy_config_diff_lines(self._strategy_saved_config, config)
        active_config = getattr(self.strategy_runtime, "config", None)
        active_lines = []
        if isinstance(active_config, StrategyRuntimeConfig):
            active_lines = self._strategy_config_diff_lines(active_config, config)
        sections = ["저장값 대비 입력값"]
        sections.extend(saved_lines or ["변경사항 없음"])
        if isinstance(active_config, StrategyRuntimeConfig):
            sections.append("")
            sections.append("실행 중 설정 대비 입력값")
            sections.extend(active_lines or ["변경사항 없음"])
        elif self.strategy_runtime is None:
            sections.append("")
            sections.append(f"실행 중 설정: unavailable {self.strategy_runtime_unavailable_reason}".strip())
        self.strategy_settings_diff_view.setPlainText("\n".join(sections))

    def export_strategy_settings_json(self) -> None:
        config = self._strategy_saved_config
        if config is None:
            try:
                result = self.strategy_config_repository.load()
            except Exception as exc:
                self._strategy_settings_warning(f"CONFIG_EXPORT_FAILED:{exc}")
                return
            config = result.config
            self._strategy_saved_config = config
        default_name = f"strategy_config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "전략 설정 내보내기",
            str(Path.home() / default_name),
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        try:
            Path(path).write_text(
                json.dumps(config_to_dict(config), ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception as exc:
            self._strategy_settings_warning(f"CONFIG_EXPORT_FAILED:{exc}")
            return
        self._strategy_settings_warning(f"CONFIG_EXPORTED:{path}")

    def import_strategy_settings_json(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "전략 설정 가져오기",
            str(Path.home()),
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("config JSON must be an object")
            config = config_from_dict(raw)
            errors, warnings = self._validate_strategy_settings(config)
            if errors:
                raise ValueError("; ".join(errors))
        except Exception as exc:
            self._strategy_settings_warning(f"CONFIG_IMPORT_FAILED:{exc}")
            return
        self._strategy_settings_loading = True
        try:
            self._display_strategy_settings(config)
        finally:
            self._strategy_settings_loading = False
        self._strategy_settings_dirty = True
        warnings = list(warnings) + ["CONFIG_IMPORT_PREVIEW_NOT_SAVED"]
        self._display_strategy_settings_status(warnings)

    def restore_default_strategy_settings(self) -> None:
        answer = QMessageBox.question(
            self,
            "기본값 복원",
            "입력값을 기본 전략 설정으로 되돌릴까요? 저장 버튼을 누르기 전까지 실제 설정은 바뀌지 않습니다.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._strategy_settings_loading = True
        try:
            self._display_strategy_settings(StrategyRuntimeConfig())
        finally:
            self._strategy_settings_loading = False
        self._strategy_settings_dirty = True
        self._display_strategy_settings_status(["CONFIG_DEFAULT_PREVIEW_NOT_SAVED"])

    def _display_strategy_settings(self, config: StrategyRuntimeConfig) -> None:
        self.config_interval_spin.setValue(int(config.evaluation_interval_sec))
        self.config_condition_enabled_check.setChecked(bool(config.condition_profiles_enabled))
        self.config_leader_codes_edit.setText(", ".join(config.leader_watch_codes))
        self.config_signal_codes_edit.setText(", ".join(config.semiconductor_signal_codes))
        self.config_holding_codes_edit.setText(", ".join(config.holding_watch_codes))
        self.config_kospi_index_edit.setText(str(config.index_watch_codes.get("KOSPI", "")))
        self.config_kosdaq_index_edit.setText(str(config.index_watch_codes.get("KOSDAQ", "")))
        index = self.config_fill_policy_combo.findData(config.virtual_fill_policy.value)
        self.config_fill_policy_combo.setCurrentIndex(max(0, index))
        self.config_review_enabled_check.setChecked(bool(config.review_save_enabled))
        self.config_max_candidates_spin.setValue(int(config.max_candidates_to_watch))
        self.config_realtime_limit_spin.setValue(int(config.realtime_subscription_limit))

    def _strategy_settings_to_config(self) -> StrategyRuntimeConfig:
        return StrategyRuntimeConfig(
            evaluation_interval_sec=self.config_interval_spin.value(),
            condition_profiles_enabled=self.config_condition_enabled_check.isChecked(),
            index_watch_codes={
                "KOSPI": self.config_kospi_index_edit.text().strip(),
                "KOSDAQ": self.config_kosdaq_index_edit.text().strip(),
            },
            leader_watch_codes=self._split_codes(self.config_leader_codes_edit.text()),
            semiconductor_signal_codes=self._split_codes(self.config_signal_codes_edit.text()),
            holding_watch_codes=self._split_codes(self.config_holding_codes_edit.text()),
            virtual_fill_policy=FillPolicy(self.config_fill_policy_combo.currentData()),
            review_save_enabled=self.config_review_enabled_check.isChecked(),
            max_candidates_to_watch=self.config_max_candidates_spin.value(),
            realtime_subscription_limit=self.config_realtime_limit_spin.value(),
        )

    def _display_strategy_settings_status(self, warnings: list[str]) -> None:
        saved = self._strategy_config_summary(self._strategy_saved_config)
        active_config = getattr(self.strategy_runtime, "config", None)
        active = self._strategy_config_summary(active_config)
        dirty_text = "변경사항 있음" if self._strategy_settings_dirty else "변경사항 없음"
        self.strategy_settings_status_label.setText(f"saved: {saved} / active: {active} / {dirty_text}")
        if self.strategy_runtime is None and self.strategy_runtime_unavailable_reason:
            warnings = list(warnings) + [self.strategy_runtime_unavailable_reason]
        pending_text = ""
        pending_tone = "neutral"
        if self._strategy_saved_config is not None and isinstance(active_config, StrategyRuntimeConfig):
            if config_to_dict(self._strategy_saved_config) != config_to_dict(active_config):
                pending_text = "saved config differs from active runtime; applies on next start"
                pending_tone = "warning"
            else:
                pending_text = "saved and active runtime config are aligned"
                pending_tone = "success"
        elif self.strategy_runtime is None:
            pending_text = "runtime unavailable"
            pending_tone = "unavailable"
        elif active_config is not None:
            pending_text = "active runtime config summary unavailable"
            pending_tone = "warning"
        if self._strategy_settings_dirty and pending_tone != "unavailable":
            pending_text = f"{pending_text} / 변경사항 있음".strip(" /")
            pending_tone = "warning"
        self.strategy_settings_pending_label.setText(pending_text)
        if hasattr(self, "strategy_settings_status_cards"):
            self.strategy_settings_status_cards["saved"].set_summary(saved, dirty_text, "warning" if self._strategy_settings_dirty else "neutral")
            active_tone = "unavailable" if self.strategy_runtime is None else "neutral"
            self.strategy_settings_status_cards["active"].set_summary(active, "runtime unavailable" if active_tone == "unavailable" else "-", active_tone)
            self.strategy_settings_status_cards["pending"].set_summary("대기" if pending_tone == "warning" else "정상", pending_text or "-", pending_tone)
            safety_tone = self._strategy_settings_safety_tone()
            self.strategy_settings_status_cards["safety"].set_summary(self._strategy_settings_safety_value(), self._strategy_settings_safety_detail(), safety_tone)
            self.strategy_settings_safety_label.setText(self._strategy_settings_safety_detail())
        self._update_strategy_settings_validation_view(warnings)

    def _update_strategy_settings_validation_view(self, warnings: list[str]) -> None:
        config_available = True
        try:
            config = self._strategy_settings_to_config()
            errors, ui_warnings = self._validate_strategy_settings(config)
        except Exception as exc:
            config_available = False
            errors = [str(exc)]
            ui_warnings = []
        messages = list(errors) + list(ui_warnings) + list(warnings)
        if errors:
            self.strategy_settings_validation_badge.set_status("저장 불가", "danger")
        elif ui_warnings or warnings:
            self.strategy_settings_validation_badge.set_status("확인 필요", "warning")
        else:
            self.strategy_settings_validation_badge.set_status("검증 통과", "success")
        if warnings:
            self.strategy_settings_warning_view.setPlainText("\n".join(str(message) for message in messages[-30:]))
        elif messages:
            self.strategy_settings_warning_view.setPlainText("\n".join(str(message) for message in messages[-30:]))
        else:
            self.strategy_settings_warning_view.clear()
        if config_available:
            self.show_strategy_settings_diff()
        else:
            self.strategy_settings_diff_view.setPlainText("입력값이 유효하지 않아 변경사항을 계산할 수 없습니다.")

    def _strategy_settings_warning(self, message: str) -> None:
        current = self.strategy_settings_warning_view.toPlainText().splitlines()
        current = self._dedupe_text(current + [str(message)])
        self.strategy_settings_warning_view.setPlainText("\n".join(current[-20:]))
        self._append_log(f"OBSERVE config warning: {message}")

    def _on_strategy_settings_changed(self) -> None:
        if self._strategy_settings_loading:
            return
        self._strategy_settings_dirty = True
        self._display_strategy_settings_status([])

    def _validate_strategy_settings(self, config: StrategyRuntimeConfig) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        if config.evaluation_interval_sec <= 0:
            errors.append("evaluation_interval_sec must be > 0")
        elif config.evaluation_interval_sec < 2:
            warnings.append("EVALUATION_INTERVAL_TOO_SHORT")
        if config.max_candidates_to_watch <= 0:
            errors.append("max_candidates_to_watch must be > 0")
        elif config.max_candidates_to_watch > 1000:
            warnings.append("MAX_CANDIDATES_HIGH")
        if config.realtime_subscription_limit <= 0:
            errors.append("realtime_subscription_limit must be > 0")
        elif config.realtime_subscription_limit < config.max_candidates_to_watch:
            warnings.append("REALTIME_LIMIT_BELOW_MAX_CANDIDATES")
        for market, code in config.index_watch_codes.items():
            text = str(code or "").strip()
            if not text:
                errors.append(f"index_watch_codes.{market} must not be empty")
            elif not (text.isdigit() and len(text) in {3, 6}):
                warnings.append(f"INDEX_CODE_FORMAT_CHECK:{market}:{text}")
        code_fields = [
            ("leader_watch_codes", self.config_leader_codes_edit.text(), config.leader_watch_codes),
            ("semiconductor_signal_codes", self.config_signal_codes_edit.text(), config.semiconductor_signal_codes),
            ("holding_watch_codes", self.config_holding_codes_edit.text(), config.holding_watch_codes),
        ]
        for field_name, raw_text, codes in code_fields:
            invalid = [code for code in codes if not (len(str(code)) == 6 and str(code).isdigit())]
            errors.extend(f"{field_name} contains invalid stock code {code}" for code in invalid)
            duplicates = self._duplicate_codes(raw_text)
            if duplicates:
                warnings.append(f"{field_name} duplicate codes normalized:{','.join(duplicates)}")
        if not self.config_condition_enabled_check.isChecked():
            warnings.append("CONDITION_PROFILES_DISABLED")
        if not self.config_review_enabled_check.isChecked():
            warnings.append("REVIEW_SAVE_DISABLED_REVIEW_TAB_MAY_BE_SPARSE")
        if not isinstance(config.virtual_fill_policy, FillPolicy):
            errors.append("virtual_fill_policy is invalid")
        if self._strategy_running:
            warnings.append("OBSERVE_RUNNING_CHANGES_APPLY_ON_NEXT_START")
        return self._dedupe_text(errors), self._dedupe_text(warnings)

    def _strategy_config_diff_lines(self, before, after: StrategyRuntimeConfig) -> list[str]:
        if not isinstance(after, StrategyRuntimeConfig):
            return []
        if not isinstance(before, StrategyRuntimeConfig):
            return [f"{key}: - -> {self._format_config_value(value)}" for key, value in config_to_dict(after).items()]
        before_dict = config_to_dict(before)
        after_dict = config_to_dict(after)
        lines = []
        for key in sorted(set(before_dict) | set(after_dict)):
            before_value = before_dict.get(key)
            after_value = after_dict.get(key)
            if before_value != after_value:
                lines.append(
                    f"{key}: {self._format_config_value(before_value)} -> {self._format_config_value(after_value)}"
                )
        return lines

    @staticmethod
    def _format_config_value(value) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return str(value)

    def _strategy_settings_safety_tone(self) -> str:
        if not self.mock_mode and self.ordering_check.isChecked():
            return "danger"
        if self.ordering_check.isChecked():
            return "warning"
        return "success" if self.mock_mode else "warning"

    def _strategy_settings_safety_value(self) -> str:
        if self.mock_mode:
            return "MOCK"
        return "실거래 가능"

    def _strategy_settings_safety_detail(self) -> str:
        order_state = "주문 가능 ON" if self.ordering_check.isChecked() else "주문 가능 OFF"
        if not self.mock_mode and self.ordering_check.isChecked():
            return "실거래 가능 + 주문 가능 ON 상태입니다. OBSERVE 설정 화면은 no real orders 원칙을 유지하지만 운영 전 반드시 확인하세요."
        return f"{order_state}. OBSERVE 전략 UI는 분석/가상 체결 중심이며 no real orders 경로를 만들지 않습니다."

    @staticmethod
    def _strategy_config_summary(config) -> str:
        if not isinstance(config, StrategyRuntimeConfig):
            return "-"
        return (
            f"interval={config.evaluation_interval_sec}s, "
            f"fill={config.virtual_fill_policy.value}, "
            f"limit={config.realtime_subscription_limit}"
        )

    @staticmethod
    def _split_codes(text: str) -> list[str]:
        result: list[str] = []
        for part in re.split(r"[,\s]+", str(text or "")):
            code = part.strip().upper()
            if code and code not in result:
                result.append(code)
        return result

    @staticmethod
    def _duplicate_codes(text: str) -> list[str]:
        seen: set[str] = set()
        duplicates: list[str] = []
        for part in re.split(r"[,\s]+", str(text or "")):
            code = part.strip().upper()
            if not code:
                continue
            if code in seen and code not in duplicates:
                duplicates.append(code)
            seen.add(code)
        return duplicates

    @staticmethod
    def _dedupe_text(values) -> list[str]:
        return dedupe_text(values)

    def _on_connection_changed(self, ok: bool, error_code: int, message: str) -> None:
        if not ok:
            self.connection_label.setText(f"로그인 실패({error_code})")
            self._append_log(f"로그인 실패: {message}")
            self._update_dashboard()
            return
        self.connection_label.setText("로그인 완료")
        self._refresh_accounts()
        self._update_dashboard()

    def _refresh_accounts(self) -> None:
        try:
            accounts = self.engine.client.get_accounts()
        except Exception as exc:
            self.account_combo.clear()
            self.engine.set_account("")
            self.connection_label.setText("로그인 완료 / 계좌 조회 실패")
            self._append_log(f"계좌 조회 실패: {exc}")
            self._update_dashboard()
            return

        selected_account = self.account_combo.currentText()
        self.account_combo.clear()
        self.account_combo.addItems(accounts)
        if selected_account not in accounts:
            selected_account = accounts[0] if accounts else ""
        if selected_account:
            self.account_combo.setCurrentText(selected_account)
        self.engine.set_account(selected_account)
        if not accounts:
            self.connection_label.setText("로그인 완료 / 계좌 없음")
        self._update_dashboard()

    def _login(self) -> None:
        self.connection_label.setText("로그인 요청 중")
        result = self.engine.client.login()
        if result < 0:
            self._append_log(f"로그인 요청 실패: {result}")
            self.connection_label.setText("로그인 요청 실패")
            return

    def _lookup_name(self) -> None:
        code = self._clean_code(self.code_edit.text())
        if not code:
            return
        try:
            self.name_edit.setText(self.engine.client.get_code_name(code))
        except Exception as exc:
            self._append_log(f"종목명 조회 실패: {exc}")

    def _save_current(self) -> None:
        item = self._form_to_item()
        ok, message = self.engine.add_or_update_item(item)
        if not ok:
            QMessageBox.warning(self, "저장 실패", message)
            return
        self._append_log(f"{item.code} 저장: {message}")
        self.refresh_table()
        self._select_watch_item(item.code)

    def _delete_current(self) -> None:
        code = self._clean_code(self.code_edit.text())
        if not code:
            return
        self.engine.remove_item(code)
        self._append_log(f"{code} 삭제")
        self._clear_form()
        self.refresh_table()

    def _cancel_selected_leg(self) -> None:
        code = self._clean_code(self.code_edit.text())
        leg_index = self.order_leg_combo.currentIndex() + 1
        ok, message = self.engine.cancel_leg_order(code, leg_index)
        if not ok:
            QMessageBox.warning(self, "취소 실패", message)

    def _modify_selected_leg(self) -> None:
        code = self._clean_code(self.code_edit.text())
        leg_index = self.order_leg_combo.currentIndex() + 1
        new_price = self.target_spins[leg_index].value()
        ok, message = self.engine.modify_leg_order(code, leg_index, new_price)
        if not ok:
            QMessageBox.warning(self, "정정 실패", message)

    def _clear_form(self) -> None:
        self.code_edit.clear()
        self.name_edit.clear()
        self.budget_spin.setValue(0)
        self.stop_spin.setValue(0)
        self.tick_spin.setValue(1)
        for idx in [1, 2, 3]:
            self.target_spins[idx].setValue(0)
        self.weight_spins[1].setValue(40.0)
        self.weight_spins[2].setValue(30.0)
        self.weight_spins[3].setValue(30.0)
        self.take_profit_rate_spin.setValue(5.0)
        self.take_profit_sell_spin.setValue(70.0)
        self.auto_buy_check.setChecked(False)
        self.auto_sell_check.setChecked(True)

    def _load_selected(self) -> None:
        item = self._selected_watch_item()
        if item is None:
            if hasattr(self, "watch_detail_panel"):
                self.watch_detail_panel.show_empty()
            return
        self._item_to_form(item)
        self._display_watch_detail(item)

    def _form_to_item(self) -> WatchItem:
        legs = [
            BuyLeg(
                index=idx,
                target_price=self.target_spins[idx].value(),
                weight_percent=self.weight_spins[idx].value(),
            )
            for idx in [1, 2, 3]
        ]
        return WatchItem(
            code=self._clean_code(self.code_edit.text()),
            name=self.name_edit.text().strip(),
            budget=self.budget_spin.value(),
            stop_loss_price=self.stop_spin.value(),
            tick_threshold=self.tick_spin.value(),
            take_profit_rate=self.take_profit_rate_spin.value(),
            take_profit_sell_percent=self.take_profit_sell_spin.value(),
            auto_buy_enabled=self.auto_buy_check.isChecked(),
            auto_sell_enabled=self.auto_sell_check.isChecked(),
            legs=legs,
        )

    def _item_to_form(self, item: WatchItem) -> None:
        self.code_edit.setText(item.code)
        self.name_edit.setText(item.name)
        self.budget_spin.setValue(item.budget)
        self.stop_spin.setValue(item.stop_loss_price)
        self.tick_spin.setValue(item.tick_threshold)
        self.take_profit_rate_spin.setValue(item.take_profit_rate)
        self.take_profit_sell_spin.setValue(item.take_profit_sell_percent)
        self.auto_buy_check.setChecked(item.auto_buy_enabled)
        self.auto_sell_check.setChecked(item.auto_sell_enabled)
        for leg in item.legs:
            self.target_spins[leg.index].setValue(leg.target_price)
            self.weight_spins[leg.index].setValue(leg.weight_percent)

    def refresh_table(self) -> None:
        self._refresh_pending = False
        selected_code = self._selected_watch_code() or self._clean_code(self.code_edit.text())
        items = list(self.engine.items.values())
        self._update_watch_runtime_context()
        self.watch_model.set_items(items)
        self._set_last_refresh(self.watch_last_refresh_label)
        message = f"매수종목 갱신 완료: {len(items)}개"
        self.watch_refresh_status_label.setText(message)
        self.statusBar().showMessage(message, 3000)
        if selected_code and self._select_watch_item(selected_code):
            self._load_selected()
        else:
            self.table.clearSelection()
            self.watch_detail_panel.show_empty()

    def _apply_watch_filters(self) -> None:
        selected_code = self._selected_watch_code()
        self.watch_proxy_model.set_filters(
            search_text=self.watch_filter_bar.search_text(),
            holding_only=self.watch_filter_bar.holding_only(),
            auto_buy_only=self.watch_filter_bar.auto_buy_only(),
            open_order_only=self.watch_filter_bar.open_order_only(),
            stop_risk_only=self.watch_filter_bar.stop_risk_only(),
            take_profit_only=self.watch_filter_bar.take_profit_only(),
            watching_only=self.watch_filter_bar.watching_only(),
            pending_only=self.watch_filter_bar.pending_only(),
        )
        if selected_code and self._select_watch_item(selected_code):
            self._load_selected()
        else:
            self.table.clearSelection()
            self.watch_detail_panel.show_empty()

    def _selected_watch_code(self) -> str:
        current = self.table.currentIndex()
        if not current.isValid():
            return ""
        source_index = self.watch_proxy_model.mapToSource(current)
        return str(self.watch_model.data(source_index, WatchItemTableModel.CodeRole) or "")

    def _selected_watch_item(self) -> WatchItem | None:
        current = self.table.currentIndex()
        if not current.isValid():
            return None
        source_index = self.watch_proxy_model.mapToSource(current)
        return self.watch_model.data(source_index, WatchItemTableModel.ItemRole)

    def _select_watch_item(self, code: str) -> bool:
        if not code or self.watch_model.item_by_code(code) is None:
            return False
        for source_row in range(self.watch_model.rowCount()):
            item = self.watch_model.item_at(source_row)
            if item is None or item.code != code:
                continue
            source_index = self.watch_model.index(source_row, 0)
            proxy_index = self.watch_proxy_model.mapFromSource(source_index)
            if not proxy_index.isValid():
                return False
            self.table.setCurrentIndex(proxy_index)
            self.table.selectionModel().select(proxy_index, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
            return True
        return False

    def _update_watch_runtime_context(self) -> None:
        if hasattr(self, "watch_model"):
            self.watch_model.set_runtime_context(mock_mode=self.mock_mode, ordering_enabled=self.ordering_check.isChecked())

    def _display_watch_detail(self, item: WatchItem) -> None:
        try:
            detail_text = self._watch_detail_text(item)
        except Exception as exc:
            detail_text = f"상세 표시 실패: {exc}"
            self._append_log(f"WATCH_DETAIL_FAILED:{exc}")
        self.watch_detail_panel.show_detail(f"{item.code} {item.name}".strip(), detail_text)

    def _watch_detail_text(self, item: WatchItem) -> str:
        sections = [
            self._watch_basic_detail(item),
            self._watch_plan_detail(item),
            self._watch_order_detail(item),
            self._watch_risk_detail(item),
            "[최근 로그]\n이번 PR에서는 종목별 로그 필터링을 연결하지 않았습니다.",
        ]
        return "\n\n".join(sections)

    def _watch_basic_detail(self, item: WatchItem) -> str:
        valuation = item.current_price * item.holding_quantity if item.current_price and item.holding_quantity else 0
        distance = self._stop_distance_text(item)
        return "\n".join(
            [
                "[기본 정보]",
                f"코드: {item.code}",
                f"종목명: {item.name or '-'}",
                f"현재가: {self._money(item.current_price) or '-'}",
                f"예산: {self._money(item.budget) or '-'}",
                f"자동매수: {'ON' if item.auto_buy_enabled else 'OFF'}",
                f"보유수량: {item.holding_quantity}",
                f"평균단가: {item.average_price:,.1f}",
                f"추정 평가금액: {self._money(valuation) or '-'}",
                f"손절가: {self._money(item.stop_loss_price) or '-'}",
                f"손절가 대비 현재가 거리: {distance}",
                f"익절완료: {'Y' if item.take_profit_done else 'N'}",
            ]
        )

    def _watch_plan_detail(self, item: WatchItem) -> str:
        lines = ["[분할 매수/익절 계획]"]
        for index in [1, 2, 3]:
            leg = item.leg(index)
            lines.append(
                "{idx}차 목표가={price} / 비중={weight:.1f}% / 상태={status} / tone={tone}".format(
                    idx=index,
                    price=self._money(leg.target_price) or "-",
                    weight=leg.weight_percent,
                    status=leg.status.value,
                    tone=WatchItemTableModel.leg_status_tone(leg.status),
                )
            )
        lines.append(f"익절률: {item.take_profit_rate:.1f}% / 매도비중: {item.take_profit_sell_percent:.1f}%")
        return "\n".join(lines)

    def _watch_order_detail(self, item: WatchItem) -> str:
        lines = ["[주문/체결 상태]"]
        has_order = False
        for leg in item.legs:
            remaining = max(0, leg.ordered_quantity - leg.filled_quantity)
            if leg.order_no or leg.ordered_quantity or leg.filled_quantity:
                has_order = True
                lines.append(
                    "{idx}차 주문번호={order_no} / 주문가격={price} / 주문수량={ordered} / "
                    "체결수량={filled} / 미체결수량={remaining} / 주문상태={status}".format(
                        idx=leg.index,
                        order_no=leg.order_no or "-",
                        price=self._money(leg.target_price) or "-",
                        ordered=leg.ordered_quantity,
                        filled=leg.filled_quantity,
                        remaining=remaining,
                        status=leg.status.value,
                    )
                )
        if not has_order:
            lines.append("미체결 주문 없음")
        return "\n".join(lines)

    def _watch_risk_detail(self, item: WatchItem) -> str:
        stop_tone = WatchItemTableModel.stop_risk_tone(item)
        live_auto_risk = bool(item.auto_buy_enabled and self.ordering_check.isChecked() and not self.mock_mode)
        lines = [
            "[위험/알림]",
            f"손절 위험: {stop_tone}",
            f"자동매수 위험: {'Y' if live_auto_risk else 'N'}",
            f"주문 가능 체크: {'ON' if self.ordering_check.isChecked() else 'OFF'}",
            f"실거래 가능 여부: {'Y' if not self.mock_mode else 'N'}",
            f"현재 모드: {'MOCK' if self.mock_mode else '실거래'}",
        ]
        if live_auto_risk:
            lines.append("경고: 실거래 가능 + 주문 가능 ON + 자동매수 ON")
        if WatchItemTableModel.has_open_order(item):
            lines.append("경고: 미체결 주문 존재")
        return "\n".join(lines)

    @staticmethod
    def _stop_distance_text(item: WatchItem) -> str:
        if not item.current_price or not item.stop_loss_price:
            return "-"
        distance = ((item.current_price - item.stop_loss_price) / item.stop_loss_price) * 100.0
        return f"{distance:.2f}%"

    def refresh_review_table(self) -> None:
        selected_review_id = self._selected_review_id()
        try:
            reviews = self.db.latest_trade_reviews(200)
        except Exception as exc:
            message = f"리뷰 새로고침 실패: {exc}"
            self.review_refresh_status_label.setText(message)
            self._append_log(message)
            return
        self.review_proxy_model.set_reference_date(self._review_reference_date(reviews) if self.mock_mode else None)
        self.review_filter_bar.set_status_values(sorted({review.final_status for review in reviews if review.final_status}))
        self.review_model.set_reviews(reviews)
        self._update_review_analysis()
        self._set_last_refresh(self.review_last_refresh_label)
        message = f"리뷰 새로고침 완료: {len(reviews)}개"
        self.review_refresh_status_label.setText(message)
        self.statusBar().showMessage(message, 3000)
        if selected_review_id is not None and self._select_review(selected_review_id):
            self._display_selected_review_detail()
        else:
            self.review_table.clearSelection()
            self.review_detail_panel.show_empty()

    @staticmethod
    def _review_reference_date(reviews: list[TradeReview]) -> Optional[date]:
        dates = []
        for review in reviews:
            raw = str(review.trade_date or "")
            if not raw and review.created_at:
                raw = str(review.created_at)[:10]
            try:
                dates.append(date.fromisoformat(raw))
            except ValueError:
                continue
        return max(dates) if dates else None

    def _apply_review_filters(self) -> None:
        selected_review_id = self._selected_review_id()
        self.review_proxy_model.set_filters(
            search_text=self.review_filter_bar.search_text(),
            date_range=self.review_filter_bar.date_range(),
            start_date=self.review_filter_bar.start_date(),
            end_date=self.review_filter_bar.end_date(),
            status_filter=self.review_filter_bar.final_status_filter(),
            grade_filter=self.review_filter_bar.final_grade_filter(),
            false_negative_only=self.review_filter_bar.false_negative_only(),
            false_positive_only=self.review_filter_bar.false_positive_only(),
            metric_thresholds=self.review_filter_bar.metric_thresholds(),
        )
        self._update_review_analysis()
        if selected_review_id is not None and self._select_review(selected_review_id):
            self._display_selected_review_detail()
        else:
            self.review_table.clearSelection()
            self.review_detail_panel.show_empty()

    def _filtered_reviews(self) -> list:
        reviews = []
        for row in range(self.review_proxy_model.rowCount()):
            proxy_index = self.review_proxy_model.index(row, 0)
            source_index = self.review_proxy_model.mapToSource(proxy_index)
            review = self.review_model.data(source_index, ReviewTableModel.ReviewRole)
            if review is not None:
                reviews.append(review)
        return reviews

    def _selected_review_id(self) -> Optional[int]:
        if not hasattr(self, "review_table"):
            return None
        current = self.review_table.currentIndex()
        if not current.isValid():
            return None
        source_index = self.review_proxy_model.mapToSource(current)
        review_id = self.review_model.data(source_index, ReviewTableModel.ReviewIdRole)
        return int(review_id) if review_id is not None else None

    def _select_review(self, review_id: int) -> bool:
        if self.review_model.review_by_id(review_id) is None:
            return False
        for source_row in range(self.review_model.rowCount()):
            review = self.review_model.review_at(source_row)
            if review is None or review.id != review_id:
                continue
            source_index = self.review_model.index(source_row, 0)
            proxy_index = self.review_proxy_model.mapFromSource(source_index)
            if not proxy_index.isValid():
                return False
            self.review_table.setCurrentIndex(proxy_index)
            selection_model = self.review_table.selectionModel()
            selection_model.select(proxy_index, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
            return True
        return False

    def _selected_review(self):
        current = self.review_table.currentIndex()
        if not current.isValid():
            return None
        source_index = self.review_proxy_model.mapToSource(current)
        return self.review_model.data(source_index, ReviewTableModel.ReviewRole)

    def _display_selected_review_detail(self) -> None:
        review = self._selected_review()
        if review is None:
            self.review_detail_panel.show_empty()
            return
        try:
            detail_text = self._review_detail_text(review)
        except Exception as exc:
            detail_text = f"상세 조회 실패: {exc}"
            self._append_log(f"REVIEW_DETAIL_FAILED:{exc}")
        self.review_detail_panel.show_detail(f"{review.code} {review.final_status}".strip(), detail_text)

    def _update_review_analysis(self) -> None:
        if not hasattr(self, "review_summary_cards"):
            return
        reviews = self._filtered_reviews()
        total = self.review_model.rowCount()
        cards = self.review_summary_cards
        entered = [review for review in reviews if self._review_is_entered(review)]
        missed = [review for review in reviews if self._review_missed_reason(review)]
        fn = [review for review in reviews if review.false_negative_flag]
        fp = [review for review in reviews if review.false_positive_flag]
        cards["total"].set_summary(str(len(reviews)), f"필터 {len(reviews)} / 전체 {total}", "neutral")
        cards["entered"].set_summary(str(len(entered)), "-", "success")
        cards["missed"].set_summary(str(len(missed)), "-", "warning" if missed else "neutral")
        cards["false_negative"].set_summary(str(len(fn)), "-", "warning" if fn else "neutral")
        cards["false_positive"].set_summary(str(len(fp)), "-", "danger" if fp else "neutral")
        cards["avg_5m"].set_summary(self._avg_metric(reviews, "max_return_5m"), "-", self._metric_tone(self._avg_raw(reviews, "max_return_5m")))
        cards["avg_10m"].set_summary(self._avg_metric(reviews, "max_return_10m"), "-", self._metric_tone(self._avg_raw(reviews, "max_return_10m")))
        cards["avg_20m"].set_summary(self._avg_metric(reviews, "max_return_20m"), "-", self._metric_tone(self._avg_raw(reviews, "max_return_20m")))
        avg_dd = self._avg_raw(reviews, "max_drawdown_20m")
        cards["avg_dd_20m"].set_summary(self._avg_metric(reviews, "max_drawdown_20m"), "-", "danger" if avg_dd is not None and avg_dd <= -5.0 else "warning" if avg_dd is not None and avg_dd < 0 else "neutral")
        cards["blocked_rallied"].set_summary(str(sum(1 for review in reviews if review.blocked_but_later_rallied)), "-", "warning")
        cards["expired_rallied"].set_summary(str(sum(1 for review in reviews if review.expired_but_later_rallied)), "-", "warning")
        self.review_missed_reason_view.setPlainText(self._missed_reason_top_text(reviews))

    def _review_detail_text(self, review) -> str:
        sections = [
            self._review_basic_detail(review),
            self._review_metric_detail(review),
            self._review_reason_detail(review),
            self._review_linked_detail(review),
        ]
        return "\n\n".join(section for section in sections if section)

    def _review_basic_detail(self, review) -> str:
        return "\n".join(
            [
                "[기본 정보]",
                f"review id: {review.id or '-'}",
                f"candidate_id: {review.candidate_id or '-'}",
                f"virtual_position_id: {review.virtual_position_id or '-'}",
                f"created_at: {review.created_at or '-'}",
                f"code: {review.code or '-'}",
                f"name: {review.name or '-'}",
                f"market: {review.market or '-'}",
                f"theme_id: {review.theme_id or '-'}",
                f"theme_name: {review.theme_name or '-'}",
                f"final_grade: {review.final_grade or '-'}",
                f"final_status: {review.final_status or '-'}",
                f"virtual_order_status: {review.virtual_order_status or '-'}",
                f"exit_reason: {review.exit_reason or '-'}",
            ]
        )

    def _review_metric_detail(self, review) -> str:
        realized = "-"
        if review.virtual_position_id is not None:
            for position in self.db.list_virtual_positions(review.candidate_id):
                if position.id == review.virtual_position_id:
                    realized = self._metric(position.realized_return_pct)
                    break
        return "\n".join(
            [
                "[성과 지표]",
                f"max_return_5m: {self._metric(review.max_return_5m)}",
                f"max_return_10m: {self._metric(review.max_return_10m)}",
                f"max_return_20m: {self._metric(review.max_return_20m)}",
                f"max_drawdown_20m: {self._metric(review.max_drawdown_20m)}",
                f"realized_return_pct: {realized}",
                f"false_negative_flag: {review.false_negative_flag}",
                f"false_positive_flag: {review.false_positive_flag}",
                f"blocked_but_later_rallied: {review.blocked_but_later_rallied}",
                f"expired_but_later_rallied: {review.expired_but_later_rallied}",
            ]
        )

    def _review_reason_detail(self, review) -> str:
        details = dict(review.details or {})
        reason_codes = details.get("reason_codes") or details.get("block_reasons") or details.get("reason") or "-"
        block_summary = details.get("block_reason_summary") or details.get("block_reasons_by_theme") or "-"
        return "\n".join(
            [
                "[사유/세부 정보]",
                f"missed_reason: {review.missed_reason or '-'}",
                f"false_negative_type: {details.get('false_negative_type') or '-'}",
                f"false_positive_type: {details.get('false_positive_type') or '-'}",
                f"reason codes: {reason_codes}",
                f"block reason summary: {block_summary}",
                f"details: {self._payload_summary(details)}",
            ]
        )

    def _review_linked_detail(self, review) -> str:
        lines = ["[연결 데이터]"]
        if review.candidate_id is not None:
            candidate = self.db.load_candidate_by_id(review.candidate_id)
            if candidate is not None:
                lines.append(
                    "candidate: id={id} / code={code} / state={state} / block={block} / last_seen={last_seen}".format(
                        id=candidate.id,
                        code=candidate.code,
                        state=self._value_text(candidate.state),
                        block=self._value_text(candidate.block_type),
                        last_seen=candidate.last_seen_at or "-",
                    )
                )
            orders = self.db.list_virtual_orders(review.candidate_id)
            lines.append("virtual orders:")
            if orders:
                for item in orders[-3:]:
                    lines.append(
                        "  status={status} / limit_price={limit_price} / fill_price={fill_price} / submitted_at={submitted}".format(
                            status=self._value_text(item.status),
                            limit_price=self._money(item.limit_price),
                            fill_price=self._money(item.virtual_fill_price),
                            submitted=item.submitted_at or "-",
                        )
                    )
            else:
                lines.append("  -")
            positions = self.db.list_virtual_positions(review.candidate_id)
            lines.append("virtual positions:")
            if positions:
                for item in positions[-3:]:
                    lines.append(
                        "  id={id} / entry_price={entry} / quantity={quantity} / opened_at={opened} / "
                        "closed_at={closed} / close_reason={reason} / realized_return_pct={realized}".format(
                            id=item.id,
                            entry=self._money(item.entry_price),
                            quantity=item.quantity,
                            opened=item.opened_at or "-",
                            closed=item.closed_at or "-",
                            reason=item.close_reason or "-",
                            realized=self._metric(item.realized_return_pct),
                        )
                    )
            else:
                lines.append("  -")
        if review.virtual_position_id is not None:
            decisions = self.db.list_exit_decisions(review.virtual_position_id)
            lines.append("exit decisions:")
            if decisions:
                for decision in decisions[-3:]:
                    lines.append(
                        "  decision_type={decision_type} / trigger_price={price} / filled={filled} / "
                        "reason_codes={reasons} / created_at={created_at}".format(
                            decision_type=decision.decision_type or "-",
                            price=self._money(decision.trigger_price),
                            filled=decision.filled,
                            reasons=decision.reason_codes,
                            created_at=decision.created_at or "-",
                        )
                    )
            else:
                lines.append("  -")
        return "\n".join(lines)

    def _missed_reason_top_text(self, reviews) -> str:
        counts = Counter(self._review_missed_reason(review) for review in reviews)
        counts.pop("", None)
        if not counts:
            return "-"
        return "\n".join(f"{reason}: {count}" for reason, count in counts.most_common(5))

    @staticmethod
    def _review_is_entered(review) -> bool:
        return "virtual" in str(review.final_status or "").lower() or bool(review.virtual_order_status)

    @staticmethod
    def _review_missed_reason(review) -> str:
        return ReviewTableModel.missed_reason(review)

    def _avg_metric(self, reviews, field: str) -> str:
        value = self._avg_raw(reviews, field)
        return "-" if value is None else self._metric(value)

    @staticmethod
    def _avg_raw(reviews, field: str):
        values = [float(getattr(review, field)) for review in reviews if getattr(review, field) is not None]
        return (sum(values) / len(values)) if values else None

    @staticmethod
    def _metric_tone(value) -> str:
        if value is None:
            return "neutral"
        return "success" if value > 0 else "danger" if value < 0 else "neutral"

    def _reviews_for_export(self) -> list:
        if self.review_filtered_export_check.isChecked():
            return self._filtered_reviews()
        return self.db.latest_trade_reviews(10_000)

    def _export_reviews_csv(self) -> None:
        default = str(Path("data") / "reviews" / "strategy_reviews.csv")
        path, _ = QFileDialog.getSaveFileName(self, "CSV Export", default, "CSV Files (*.csv)")
        if not path:
            return
        ReviewExporter().export_csv(self._reviews_for_export(), path)
        self._append_log(f"전략 리뷰 CSV export: {path}")

    def _export_reviews_markdown(self) -> None:
        default = str(Path("data") / "reviews" / "strategy_reviews.md")
        path, _ = QFileDialog.getSaveFileName(self, "Markdown Export", default, "Markdown Files (*.md)")
        if not path:
            return
        ReviewExporter().export_markdown(self._reviews_for_export(), path)
        self._append_log(f"전략 리뷰 Markdown export: {path}")

    def _append_log(self, message: str) -> None:
        if not hasattr(self, "_log_lines"):
            self._log_lines = []
        self._log_lines.append(str(message))
        if len(self._log_lines) > self._log_max_lines:
            self._log_lines = self._log_lines[-self._log_max_lines :]
        self._render_logs()

    def _clear_logs(self) -> None:
        self._log_lines = []
        self.log_view.clear()

    def _render_logs(self) -> None:
        if not hasattr(self, "log_view"):
            return
        filter_text = self.log_filter_input.text().strip().lower() if hasattr(self, "log_filter_input") else ""
        if filter_text:
            lines = [line for line in self._log_lines if filter_text in line.lower()]
        else:
            lines = list(self._log_lines)
        scrollbar = self.log_view.verticalScrollBar()
        previous_value = scrollbar.value()
        autoscroll = self.log_autoscroll_check.isChecked() if hasattr(self, "log_autoscroll_check") else True
        self.log_view.setPlainText("\n".join(lines))
        if autoscroll:
            scrollbar.setValue(scrollbar.maximum())
        else:
            scrollbar.setValue(min(previous_value, scrollbar.maximum()))

    def _show_alert(self, message: str) -> None:
        self.statusBar().showMessage(message, 10_000)
        self._append_log(f"알림: {message}")

    @staticmethod
    def _timestamp_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _set_last_refresh(self, label: QLabel, prefix: str = "마지막 갱신") -> str:
        text = f"{prefix}: {self._timestamp_text()}"
        label.setText(text)
        return text

    def _schedule_refresh_table(self) -> None:
        if self._refresh_pending:
            return
        self._refresh_pending = True
        QTimer.singleShot(500, self.refresh_table)

    def _mock_price_tick(self) -> None:
        if not self.mock_mode:
            QMessageBox.information(self, "MOCK 전용", "MOCK 모드에서만 사용할 수 있습니다.")
            return
        item = self._current_item()
        if not item:
            return
        price = item.leg(1).target_price or item.current_price or 10_000
        self.engine.client.emit_price(item.code, price)

    def _mock_fill(self) -> None:
        if not self.mock_mode:
            QMessageBox.information(self, "MOCK 전용", "MOCK 모드에서만 사용할 수 있습니다.")
            return
        item = self._current_item()
        if not item:
            return
        for leg in item.legs:
            if leg.ordered_quantity > leg.filled_quantity:
                quantity = leg.ordered_quantity - leg.filled_quantity
                self.engine.client.emit_execution(item.code, "buy", quantity, leg.target_price, tag=f"BUY{leg.index}_{item.code}")
                break

    def _current_item(self) -> WatchItem | None:
        return self._selected_watch_item()

    @staticmethod
    def _clean_code(code: str) -> str:
        return code.strip().replace("A", "")

    @staticmethod
    def _money(value: int) -> str:
        return format_money(value)

    @staticmethod
    def _metric(value) -> str:
        return format_metric(value)
