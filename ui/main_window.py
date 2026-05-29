from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from time import perf_counter
from typing import Optional

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QAction,
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
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from storage.db import TradingDatabase
from trading.engine import TradingEngine
from trading.models import BuyLeg, WatchItem
from trading.rules import tick_size
from trading.strategy.config import StrategyRuntimeConfigRepository, config_to_dict
from trading.strategy.export import ReviewExporter
from trading.strategy.models import CandidateState, FillPolicy
from trading.strategy.runtime import StrategyRuntime, StrategyRuntimeConfig, StrategyRuntimeSnapshot


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
        self._refresh_pending = False
        self._strategy_running = False
        self._strategy_cycle_running = False
        self._strategy_last_snapshot: Optional[StrategyRuntimeSnapshot] = None
        self._strategy_last_warning_at = ""
        self._strategy_last_auto_refresh_at = 0.0
        self._strategy_ui_refresh_count = 0
        self._strategy_ui_refresh_skipped_count = 0
        self._strategy_auto_refresh_interval_sec = 15.0
        self.strategy_timer = QTimer(self)
        self.strategy_timer.timeout.connect(self._run_strategy_cycle)
        self.setWindowTitle("키움 눌림목 반자동 매매")
        self._build_ui()
        self._wire_events()
        self._load_initial()

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)

        status_bar = QHBoxLayout()
        self.connection_label = QLabel("연결 안됨")
        self.mode_label = QLabel("MOCK" if self.mock_mode else "실거래 가능")
        self.mode_label.setStyleSheet(
            "font-weight: 700; padding: 6px 10px; background: #ffefe0; color: #8a3d00;"
            if not self.mock_mode
            else "font-weight: 700; padding: 6px 10px; background: #e9f7ef; color: #17633a;"
        )
        self.account_combo = QComboBox()
        self.login_button = QPushButton("로그인")
        self.ordering_check = QCheckBox("주문 가능")
        self.realtime_button = QPushButton("실시간 등록")
        status_bar.addWidget(QLabel("상태"))
        status_bar.addWidget(self.connection_label)
        status_bar.addWidget(self.mode_label)
        status_bar.addWidget(QLabel("계좌"))
        status_bar.addWidget(self.account_combo, 1)
        status_bar.addWidget(self.login_button)
        status_bar.addWidget(self.realtime_button)
        status_bar.addWidget(self.ordering_check)
        outer.addLayout(status_bar)

        splitter = QSplitter()
        splitter.addWidget(self._build_form())
        splitter.addWidget(self._build_tabs())
        splitter.setSizes([420, 900])
        outer.addWidget(splitter, 1)
        self.setCentralWidget(central)

        toolbar = self.addToolBar("tools")
        mock_tick = QAction("MOCK 가격 테스트", self)
        mock_tick.triggered.connect(self._mock_price_tick)
        mock_fill = QAction("MOCK 체결 테스트", self)
        mock_fill.triggered.connect(self._mock_fill)
        toolbar.addAction(mock_tick)
        toolbar.addAction(mock_fill)

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
        self.table = QTableWidget(0, len(self.headers))
        self.table.setHorizontalHeaderLabels(self.headers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        tabs.addTab(self.table, "매수종목/상태")
        tabs.addTab(self._build_strategy_tab(), "전략 후보")
        tabs.addTab(self._build_review_tab(), "전략 리뷰")
        tabs.addTab(self.log_view, "로그")
        tabs.insertTab(2, self._build_strategy_settings_tab(), "전략 설정")
        return tabs

    def _build_strategy_tab(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        controls = QHBoxLayout()
        self.strategy_status_label = QLabel("OBSERVE stopped")
        self.strategy_safety_label = QLabel("OBSERVE ONLY / no real orders / auto buy disabled")
        self.strategy_start_button = QPushButton("OBSERVE 시작")
        self.strategy_stop_button = QPushButton("OBSERVE 중지")
        self.strategy_refresh_button = QPushButton("후보 새로고침")
        controls.addWidget(self.strategy_status_label)
        controls.addWidget(self.strategy_safety_label, 1)
        controls.addWidget(self.strategy_start_button)
        controls.addWidget(self.strategy_stop_button)
        controls.addWidget(self.strategy_refresh_button)

        self.strategy_snapshot_label = QLabel("cycle: - / candidates: - / warnings: 0")
        self.strategy_readiness_view = QTextEdit()
        self.strategy_readiness_view.setReadOnly(True)
        self.strategy_readiness_view.setMaximumHeight(100)
        self.strategy_warning_view = QTextEdit()
        self.strategy_warning_view.setReadOnly(True)
        self.strategy_warning_view.setMaximumHeight(90)
        self.strategy_candidate_table = QTableWidget(0, len(self.strategy_candidate_headers))
        self.strategy_candidate_table.setHorizontalHeaderLabels(self.strategy_candidate_headers)
        self.strategy_candidate_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.strategy_candidate_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.strategy_candidate_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.strategy_candidate_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.strategy_candidate_table.horizontalHeader().setStretchLastSection(True)

        layout.addLayout(controls)
        layout.addWidget(self.strategy_snapshot_label)
        layout.addWidget(self.strategy_readiness_view)
        layout.addWidget(self.strategy_warning_view)
        layout.addWidget(self.strategy_candidate_table, 1)
        return box

    def _build_strategy_settings_tab(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)

        self.strategy_settings_status_label = QLabel("saved: - / active: -")
        self.strategy_settings_pending_label = QLabel("")
        form_group = QGroupBox("OBSERVE Runtime Settings")
        form = QFormLayout(form_group)

        self.config_interval_spin = QSpinBox()
        self.config_interval_spin.setRange(1, 3600)
        self.config_interval_spin.setSuffix(" sec")
        self.config_condition_enabled_check = QCheckBox("condition profiles enabled")
        self.config_leader_codes_edit = QLineEdit()
        self.config_signal_codes_edit = QLineEdit()
        self.config_holding_codes_edit = QLineEdit()
        self.config_kospi_index_edit = QLineEdit()
        self.config_kosdaq_index_edit = QLineEdit()
        self.config_fill_policy_combo = QComboBox()
        for policy in [FillPolicy.OPTIMISTIC, FillPolicy.NORMAL, FillPolicy.CONSERVATIVE]:
            self.config_fill_policy_combo.addItem(policy.value, policy.value)
        self.config_review_enabled_check = QCheckBox("review save enabled")
        self.config_max_candidates_spin = QSpinBox()
        self.config_max_candidates_spin.setRange(0, 10_000)
        self.config_realtime_limit_spin = QSpinBox()
        self.config_realtime_limit_spin.setRange(1, 10_000)

        form.addRow("evaluation interval", self.config_interval_spin)
        form.addRow("condition profiles", self.config_condition_enabled_check)
        form.addRow("leader watch codes", self.config_leader_codes_edit)
        form.addRow("semiconductor signal codes", self.config_signal_codes_edit)
        form.addRow("holding watch codes", self.config_holding_codes_edit)
        form.addRow("KOSPI raw index code", self.config_kospi_index_edit)
        form.addRow("KOSDAQ raw index code", self.config_kosdaq_index_edit)
        form.addRow("virtual fill policy", self.config_fill_policy_combo)
        form.addRow("review save", self.config_review_enabled_check)
        form.addRow("max candidates to watch", self.config_max_candidates_spin)
        form.addRow("realtime subscription limit", self.config_realtime_limit_spin)

        buttons = QHBoxLayout()
        self.strategy_settings_load_button = QPushButton("설정 불러오기")
        self.strategy_settings_save_button = QPushButton("설정 저장")
        buttons.addWidget(self.strategy_settings_load_button)
        buttons.addWidget(self.strategy_settings_save_button)
        buttons.addStretch(1)

        self.strategy_settings_warning_view = QTextEdit()
        self.strategy_settings_warning_view.setReadOnly(True)
        self.strategy_settings_warning_view.setMaximumHeight(120)

        layout.addWidget(QLabel("OBSERVE ONLY / real orders disabled / changes apply on next OBSERVE start"))
        layout.addWidget(self.strategy_settings_status_label)
        layout.addWidget(self.strategy_settings_pending_label)
        layout.addWidget(form_group)
        layout.addLayout(buttons)
        layout.addWidget(self.strategy_settings_warning_view)
        layout.addStretch(1)
        return box

    def _build_review_tab(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        buttons = QHBoxLayout()
        self.review_refresh_button = QPushButton("새로고침")
        self.review_csv_button = QPushButton("CSV Export")
        self.review_markdown_button = QPushButton("Markdown Export")
        buttons.addWidget(self.review_refresh_button)
        buttons.addWidget(self.review_csv_button)
        buttons.addWidget(self.review_markdown_button)
        buttons.addStretch(1)
        self.review_table = QTableWidget(0, len(self.review_headers))
        self.review_table.setHorizontalHeaderLabels(self.review_headers)
        self.review_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.review_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.review_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.review_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.review_table.horizontalHeader().setStretchLastSection(True)
        layout.addLayout(buttons)
        layout.addWidget(self.review_table, 1)
        return box

    def _wire_events(self) -> None:
        self.login_button.clicked.connect(self._login)
        self.engine.client.connected.connect(self._on_connection_changed)
        self.account_combo.currentTextChanged.connect(self.engine.set_account)
        self.realtime_button.clicked.connect(self.engine.register_realtime)
        self.ordering_check.toggled.connect(self.engine.set_ordering_enabled)
        self.lookup_button.clicked.connect(self._lookup_name)
        self.save_button.clicked.connect(self._save_current)
        self.delete_button.clicked.connect(self._delete_current)
        self.clear_button.clicked.connect(self._clear_form)
        self.cancel_leg_button.clicked.connect(self._cancel_selected_leg)
        self.modify_leg_button.clicked.connect(self._modify_selected_leg)
        self.table.itemSelectionChanged.connect(self._load_selected)
        self.review_refresh_button.clicked.connect(self.refresh_review_table)
        self.review_csv_button.clicked.connect(self._export_reviews_csv)
        self.review_markdown_button.clicked.connect(self._export_reviews_markdown)
        self.strategy_start_button.clicked.connect(self.start_observe_strategy)
        self.strategy_stop_button.clicked.connect(self.stop_observe_strategy)
        self.strategy_refresh_button.clicked.connect(self.refresh_strategy_candidates)
        self.strategy_settings_load_button.clicked.connect(self.load_strategy_settings)
        self.strategy_settings_save_button.clicked.connect(self.save_strategy_settings)
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
        if self._strategy_running:
            self._strategy_warning("STRATEGY_RUNTIME_ALREADY_STARTED")
            return
        try:
            snapshot = self.strategy_runtime.start()
        except Exception as exc:
            self._strategy_warning(f"STRATEGY_RUNTIME_START_FAILED:{exc}")
            self.strategy_timer.stop()
            self._strategy_running = False
            self._update_strategy_buttons()
            return
        self._strategy_running = True
        self._strategy_last_snapshot = snapshot
        interval_ms = max(1, self.strategy_runtime.config.evaluation_interval_sec) * 1000
        self.strategy_timer.start(interval_ms)
        self._display_strategy_snapshot(snapshot, 0.0)
        self.refresh_strategy_candidates()
        self._strategy_last_auto_refresh_at = perf_counter()
        self._update_strategy_buttons()

    def stop_observe_strategy(self) -> None:
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

    def _display_strategy_readiness(self, snapshot: StrategyRuntimeSnapshot) -> None:
        lines = [
            "readiness: conditions={conditions} unresolved={unresolved} / themes={themes} enabled={enabled}".format(
                conditions=getattr(snapshot, "condition_profiles_count", 0),
                unresolved=getattr(snapshot, "unresolved_condition_profiles_count", 0),
                themes=getattr(snapshot, "theme_mappings_count", 0),
                enabled=getattr(snapshot, "enabled_theme_mappings_count", 0),
            ),
            "active candidates={active} mapped={mapped} unmapped={unmapped} coverage={coverage:.2f}% / protected subs={protected}".format(
                active=snapshot.active_candidate_count,
                mapped=getattr(snapshot, "active_candidates_with_theme_mapping", 0),
                unmapped=getattr(snapshot, "active_candidates_without_theme_mapping", 0),
                coverage=float(getattr(snapshot, "theme_mapping_coverage_pct", 0.0) or 0.0),
                protected=getattr(snapshot, "protected_subscription_usage", "") or "-",
            ),
        ]
        startup_warnings = self._dedupe_text(getattr(self.strategy_runtime, "startup_warnings", []) if self.strategy_runtime is not None else [])
        if startup_warnings:
            lines.append("startup warnings: " + ", ".join(startup_warnings[-8:]))
        self.strategy_readiness_view.setPlainText("\n".join(lines))

    def refresh_strategy_candidates(self) -> None:
        self._strategy_ui_refresh_count += 1
        trade_date = self._strategy_trade_date()
        candidates = self.db.list_candidates(trade_date=trade_date)
        candidates.sort(key=lambda candidate: candidate.last_seen_at or "", reverse=True)
        candidates.sort(key=lambda candidate: self._candidate_state_priority(candidate.state))
        candidates = candidates[:200]
        self.strategy_candidate_table.setRowCount(len(candidates))
        for row, candidate in enumerate(candidates):
            metadata, metadata_warning = self._safe_candidate_metadata(candidate)
            if metadata_warning:
                self._strategy_warning(metadata_warning)
            values = [
                candidate.code,
                candidate.name,
                candidate.state.value,
                candidate.block_type.value,
                "Y" if candidate.can_recover else "",
                self._metadata_text(metadata, "best_theme_id"),
                self._metadata_text(metadata, "best_gate_result_key"),
                self._metadata_text(metadata, "sub_status"),
                candidate.last_seen_at,
                candidate.expires_at,
                self._block_reason_summary(metadata),
            ]
            for column, value in enumerate(values):
                self.strategy_candidate_table.setItem(row, column, QTableWidgetItem(str(value or "")))

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
        return date.today().isoformat()

    def _strategy_warning(self, message: str) -> None:
        self._strategy_last_warning_at = datetime.now().replace(microsecond=0).isoformat()
        if hasattr(self, "strategy_warning_view"):
            current = self.strategy_warning_view.toPlainText().splitlines()
            current = self._dedupe_text(current + [str(message)])
            self.strategy_warning_view.setPlainText("\n".join(current[-20:]))
        self._append_log(f"OBSERVE warning: {message}")

    def _update_strategy_buttons(self) -> None:
        has_runtime = self.strategy_runtime is not None
        self.strategy_start_button.setEnabled(has_runtime and not self._strategy_running)
        self.strategy_stop_button.setEnabled(has_runtime and self._strategy_running)
        self.strategy_refresh_button.setEnabled(True)
        if not has_runtime and self.strategy_runtime_unavailable_reason:
            self.strategy_status_label.setText("OBSERVE unavailable")

    def load_strategy_settings(self) -> None:
        try:
            result = self.strategy_config_repository.load()
        except Exception as exc:
            self._strategy_settings_warning(f"CONFIG_LOAD_FAILED:{exc}")
            return
        self._strategy_saved_config = result.config
        self._display_strategy_settings(result.config)
        self._display_strategy_settings_status(result.warnings)

    def save_strategy_settings(self) -> None:
        try:
            config = self._strategy_settings_to_config()
            result = self.strategy_config_repository.save(config)
        except Exception as exc:
            self._strategy_settings_warning(f"CONFIG_SAVE_FAILED:{exc}")
            return
        self._strategy_saved_config = result.config
        self._display_strategy_settings(result.config)
        warnings = list(result.warnings)
        if self._strategy_running:
            warnings.append("CONFIG_SAVED_FOR_NEXT_OBSERVE_START")
        self._display_strategy_settings_status(warnings)

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
        self.strategy_settings_status_label.setText(f"saved: {saved} / active: {active}")
        if self.strategy_runtime is None and self.strategy_runtime_unavailable_reason:
            warnings = list(warnings) + [self.strategy_runtime_unavailable_reason]
        if self._strategy_saved_config is not None and isinstance(active_config, StrategyRuntimeConfig):
            if config_to_dict(self._strategy_saved_config) != config_to_dict(active_config):
                self.strategy_settings_pending_label.setText("saved config differs from active runtime; applies on next start")
            else:
                self.strategy_settings_pending_label.setText("")
        elif self.strategy_runtime is None:
            self.strategy_settings_pending_label.setText("runtime unavailable")
        if warnings:
            self.strategy_settings_warning_view.setPlainText("\n".join(str(warning) for warning in warnings[-20:]))
        else:
            self.strategy_settings_warning_view.clear()

    def _strategy_settings_warning(self, message: str) -> None:
        current = self.strategy_settings_warning_view.toPlainText().splitlines()
        current = self._dedupe_text(current + [str(message)])
        self.strategy_settings_warning_view.setPlainText("\n".join(current[-20:]))
        self._append_log(f"OBSERVE config warning: {message}")

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
        return [part.strip() for part in str(text or "").replace("\n", ",").split(",") if part.strip()]

    @staticmethod
    def _dedupe_text(values) -> list[str]:
        result: list[str] = []
        for value in values:
            text = str(value or "")
            if text and text not in result:
                result.append(text)
        return result

    def _on_connection_changed(self, ok: bool, error_code: int, message: str) -> None:
        if not ok:
            self.connection_label.setText(f"로그인 실패({error_code})")
            self._append_log(f"로그인 실패: {message}")
            return
        self.connection_label.setText("로그인 완료")
        self._refresh_accounts()

    def _refresh_accounts(self) -> None:
        try:
            accounts = self.engine.client.get_accounts()
        except Exception as exc:
            self.account_combo.clear()
            self.engine.set_account("")
            self.connection_label.setText("로그인 완료 / 계좌 조회 실패")
            self._append_log(f"계좌 조회 실패: {exc}")
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
        selected = self.table.selectedItems()
        if not selected:
            return
        code = self.table.item(selected[0].row(), 0).text()
        item = self.engine.items.get(code)
        if item:
            self._item_to_form(item)

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
        items = list(self.engine.items.values())
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            values = [
                item.code,
                item.name,
                self._money(item.current_price),
                self._money(item.budget),
                self._money(item.stop_loss_price),
                self._money(item.leg(1).target_price),
                f"{item.leg(1).weight_percent:.1f}",
                item.leg(1).status.value,
                self._money(item.leg(2).target_price),
                f"{item.leg(2).weight_percent:.1f}",
                item.leg(2).status.value,
                self._money(item.leg(3).target_price),
                f"{item.leg(3).weight_percent:.1f}",
                item.leg(3).status.value,
                str(item.holding_quantity),
                f"{item.average_price:,.1f}",
                "Y" if item.take_profit_done else "",
                "Y" if item.auto_buy_enabled else "",
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if item.stop_loss_price and item.current_price and item.current_price <= item.stop_loss_price:
                    cell.setBackground(self.palette().color(self.palette().Highlight))
                self.table.setItem(row, column, cell)

    def refresh_review_table(self) -> None:
        reviews = self.db.latest_trade_reviews(200)
        self.review_table.setRowCount(len(reviews))
        for row, review in enumerate(reviews):
            values = [
                review.created_at,
                review.code,
                review.name,
                review.market,
                review.theme_name or review.theme_id,
                review.final_grade,
                review.final_status,
                review.virtual_order_status,
                review.exit_reason,
                self._metric(review.max_return_5m),
                self._metric(review.max_return_10m),
                self._metric(review.max_return_20m),
                self._metric(review.max_drawdown_20m),
                "Y" if review.false_negative_flag else "",
                "Y" if review.false_positive_flag else "",
                review.missed_reason or review.details.get("false_negative_type", ""),
            ]
            for column, value in enumerate(values):
                self.review_table.setItem(row, column, QTableWidgetItem(str(value)))

    def _export_reviews_csv(self) -> None:
        default = str(Path("data") / "reviews" / "strategy_reviews.csv")
        path, _ = QFileDialog.getSaveFileName(self, "CSV Export", default, "CSV Files (*.csv)")
        if not path:
            return
        ReviewExporter().export_csv(self.db.latest_trade_reviews(10_000), path)
        self._append_log(f"전략 리뷰 CSV export: {path}")

    def _export_reviews_markdown(self) -> None:
        default = str(Path("data") / "reviews" / "strategy_reviews.md")
        path, _ = QFileDialog.getSaveFileName(self, "Markdown Export", default, "Markdown Files (*.md)")
        if not path:
            return
        ReviewExporter().export_markdown(self.db.latest_trade_reviews(10_000), path)
        self._append_log(f"전략 리뷰 Markdown export: {path}")

    def _append_log(self, message: str) -> None:
        self.log_view.append(message)

    def _show_alert(self, message: str) -> None:
        self.statusBar().showMessage(message, 10_000)
        self._append_log(f"알림: {message}")

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
        selected = self.table.selectedItems()
        if not selected:
            return None
        code = self.table.item(selected[0].row(), 0).text()
        return self.engine.items.get(code)

    @staticmethod
    def _clean_code(code: str) -> str:
        return code.strip().replace("A", "")

    @staticmethod
    def _money(value: int) -> str:
        return f"{int(value):,}" if value else ""

    @staticmethod
    def _metric(value) -> str:
        return "" if value is None else f"{float(value):.2f}"
