from __future__ import annotations

from PyQt5.QtCore import QDate, pyqtSignal
from PyQt5.QtWidgets import QComboBox, QDateEdit, QDoubleSpinBox, QGridLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QWidget


class CandidateFilterBar(QWidget):
    filters_changed = pyqtSignal()
    search_changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("코드/종목명/테마/Gate/Sub/Reason")
        self.search_edit.setMinimumWidth(220)
        self.search_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.state_combo = QComboBox()
        self.state_combo.addItems(["전체", "READY", "BLOCKED", "WATCHING", "DETECTED"])
        self.recover_combo = QComboBox()
        self.recover_combo.addItem("전체", False)
        self.recover_combo.addItem("Recover 가능만", True)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("전체", "ALL")
        self.theme_combo.addItem("매핑 있음", "mapped")
        self.theme_combo.addItem("매핑 없음", "unmapped")
        self.quality_combo = QComboBox()
        self.quality_combo.addItem("Entry Candidates", "ENTRY")
        self.quality_combo.addItem("All Quality", "ALL")
        self.quality_combo.addItem("Actionable", "actionable")
        self.quality_combo.addItem("Data Wait", "data_wait")
        self.quality_combo.addItem("Unmapped", "unmapped")
        self.quality_combo.addItem("Invalid Code", "invalid_code")
        self.quality_combo.addItem("Discovery", "discovery_only")
        self.clear_button = QPushButton("초기화")

        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(4)
        layout.addWidget(QLabel("검색"), 0, 0)
        layout.addWidget(self.search_edit, 0, 1, 1, 7)
        layout.addWidget(self.clear_button, 0, 8)
        layout.addWidget(QLabel("상태"), 1, 0)
        layout.addWidget(self.state_combo, 1, 1)
        layout.addWidget(QLabel("Recover"), 1, 2)
        layout.addWidget(self.recover_combo, 1, 3)
        layout.addWidget(QLabel("테마"), 1, 4)
        layout.addWidget(self.theme_combo, 1, 5)
        layout.addWidget(QLabel("Quality"), 1, 6)
        layout.addWidget(self.quality_combo, 1, 7)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(8, 1)

        self.search_edit.textChanged.connect(self.search_changed.emit)
        self.state_combo.currentIndexChanged.connect(self.filters_changed.emit)
        self.recover_combo.currentIndexChanged.connect(self.filters_changed.emit)
        self.theme_combo.currentIndexChanged.connect(self.filters_changed.emit)
        self.quality_combo.currentIndexChanged.connect(self.filters_changed.emit)
        self.clear_button.clicked.connect(self.clear_filters)

    def search_text(self) -> str:
        return self.search_edit.text()

    def state_filter(self) -> str:
        return self.state_combo.currentText()

    def recover_only(self) -> bool:
        return bool(self.recover_combo.currentData())

    def theme_filter(self) -> str:
        return str(self.theme_combo.currentData() or "ALL")

    def quality_filter(self) -> str:
        return str(self.quality_combo.currentData() or "ENTRY")

    def clear_filters(self) -> None:
        self.search_edit.clear()
        self.state_combo.setCurrentIndex(0)
        self.recover_combo.setCurrentIndex(0)
        self.theme_combo.setCurrentIndex(0)
        self.quality_combo.setCurrentIndex(0)
        self.filters_changed.emit()


class ReviewFilterBar(QWidget):
    filters_changed = pyqtSignal()
    search_changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("코드/종목명/시장/테마/상태/사유/details")
        self.date_range_combo = QComboBox()
        self.date_range_combo.addItems(["전체", "오늘", "최근 3일", "최근 7일", "사용자 지정"])
        self.start_date_edit = QDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDate(QDate.currentDate())
        self.end_date_edit = QDateEdit()
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.setDate(QDate.currentDate())
        self.status_combo = QComboBox()
        self.status_combo.addItems(["전체", "entered", "missed", "blocked", "expired"])
        self.grade_combo = QComboBox()
        self.grade_combo.addItems(["전체", "A", "B", "C", "빈 값/미분류"])
        self.fn_combo = QComboBox()
        self.fn_combo.addItem("전체", False)
        self.fn_combo.addItem("False Negative만", True)
        self.fp_combo = QComboBox()
        self.fp_combo.addItem("전체", False)
        self.fp_combo.addItem("False Positive만", True)
        self.min_5m_spin = self._threshold_spin()
        self.min_10m_spin = self._threshold_spin()
        self.min_20m_spin = self._threshold_spin()
        self.dd_20m_spin = self._threshold_spin()
        self.clear_button = QPushButton("초기화")

        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("검색"), 0, 0)
        layout.addWidget(self.search_edit, 0, 1, 1, 3)
        layout.addWidget(QLabel("날짜"), 0, 4)
        layout.addWidget(self.date_range_combo, 0, 5)
        layout.addWidget(self.start_date_edit, 0, 6)
        layout.addWidget(self.end_date_edit, 0, 7)
        layout.addWidget(QLabel("상태"), 1, 0)
        layout.addWidget(self.status_combo, 1, 1)
        layout.addWidget(QLabel("등급"), 1, 2)
        layout.addWidget(self.grade_combo, 1, 3)
        layout.addWidget(QLabel("FN"), 1, 4)
        layout.addWidget(self.fn_combo, 1, 5)
        layout.addWidget(QLabel("FP"), 1, 6)
        layout.addWidget(self.fp_combo, 1, 7)
        layout.addWidget(QLabel("5m >="), 2, 0)
        layout.addWidget(self.min_5m_spin, 2, 1)
        layout.addWidget(QLabel("10m >="), 2, 2)
        layout.addWidget(self.min_10m_spin, 2, 3)
        layout.addWidget(QLabel("20m >="), 2, 4)
        layout.addWidget(self.min_20m_spin, 2, 5)
        layout.addWidget(QLabel("20m DD <="), 2, 6)
        layout.addWidget(self.dd_20m_spin, 2, 7)
        layout.addWidget(self.clear_button, 2, 8)

        self.search_edit.textChanged.connect(self.search_changed.emit)
        self.date_range_combo.currentIndexChanged.connect(self.filters_changed.emit)
        self.start_date_edit.dateChanged.connect(self.filters_changed.emit)
        self.end_date_edit.dateChanged.connect(self.filters_changed.emit)
        self.status_combo.currentIndexChanged.connect(self.filters_changed.emit)
        self.grade_combo.currentIndexChanged.connect(self.filters_changed.emit)
        self.fn_combo.currentIndexChanged.connect(self.filters_changed.emit)
        self.fp_combo.currentIndexChanged.connect(self.filters_changed.emit)
        self.min_5m_spin.valueChanged.connect(self.filters_changed.emit)
        self.min_10m_spin.valueChanged.connect(self.filters_changed.emit)
        self.min_20m_spin.valueChanged.connect(self.filters_changed.emit)
        self.dd_20m_spin.valueChanged.connect(self.filters_changed.emit)
        self.clear_button.clicked.connect(self.clear_filters)

    def set_status_values(self, statuses: list[str]) -> None:
        current = self.status_combo.currentText()
        base = ["전체", "entered", "missed", "blocked", "expired"]
        values = base + [status for status in statuses if status and status not in base]
        self.status_combo.blockSignals(True)
        self.status_combo.clear()
        self.status_combo.addItems(values)
        index = self.status_combo.findText(current)
        self.status_combo.setCurrentIndex(max(0, index))
        self.status_combo.blockSignals(False)

    def search_text(self) -> str:
        return self.search_edit.text()

    def date_range(self) -> str:
        return self.date_range_combo.currentText()

    def start_date(self) -> str:
        return self.start_date_edit.date().toString("yyyy-MM-dd")

    def end_date(self) -> str:
        return self.end_date_edit.date().toString("yyyy-MM-dd")

    def final_status_filter(self) -> str:
        return self.status_combo.currentText()

    def final_grade_filter(self) -> str:
        return self.grade_combo.currentText()

    def false_negative_only(self) -> bool:
        return bool(self.fn_combo.currentData())

    def false_positive_only(self) -> bool:
        return bool(self.fp_combo.currentData())

    def metric_thresholds(self) -> dict[str, float | None]:
        return {
            "5m": self._spin_value(self.min_5m_spin),
            "10m": self._spin_value(self.min_10m_spin),
            "20m": self._spin_value(self.min_20m_spin),
            "20m_dd": self._spin_value(self.dd_20m_spin),
        }

    def clear_filters(self) -> None:
        self.search_edit.clear()
        self.date_range_combo.setCurrentIndex(0)
        self.status_combo.setCurrentIndex(0)
        self.grade_combo.setCurrentIndex(0)
        self.fn_combo.setCurrentIndex(0)
        self.fp_combo.setCurrentIndex(0)
        for spin in [self.min_5m_spin, self.min_10m_spin, self.min_20m_spin, self.dd_20m_spin]:
            spin.setValue(-999.0)
        self.filters_changed.emit()

    @staticmethod
    def _threshold_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(-999.0, 999.0)
        spin.setValue(-999.0)
        spin.setDecimals(2)
        spin.setSuffix(" %")
        return spin

    @staticmethod
    def _spin_value(spin: QDoubleSpinBox) -> float | None:
        value = float(spin.value())
        return None if value <= -999.0 else value


class WatchItemFilterBar(QWidget):
    filters_changed = pyqtSignal()
    search_changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("코드/종목명")
        self.holding_combo = self._bool_combo("보유중만")
        self.auto_buy_combo = self._bool_combo("자동매수 ON만")
        self.open_order_combo = self._bool_combo("미체결 있음만")
        self.stop_risk_combo = self._bool_combo("손절위험만")
        self.take_profit_combo = self._bool_combo("익절완료만")
        self.watching_combo = self._bool_combo("접근감시만")
        self.pending_combo = self._bool_combo("주문/미체결만")
        self.clear_button = QPushButton("초기화")

        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("검색"), 0, 0)
        layout.addWidget(self.search_edit, 0, 1, 1, 3)
        layout.addWidget(QLabel("보유"), 0, 4)
        layout.addWidget(self.holding_combo, 0, 5)
        layout.addWidget(QLabel("자동매수"), 0, 6)
        layout.addWidget(self.auto_buy_combo, 0, 7)
        layout.addWidget(QLabel("미체결"), 1, 0)
        layout.addWidget(self.open_order_combo, 1, 1)
        layout.addWidget(QLabel("손절"), 1, 2)
        layout.addWidget(self.stop_risk_combo, 1, 3)
        layout.addWidget(QLabel("익절"), 1, 4)
        layout.addWidget(self.take_profit_combo, 1, 5)
        layout.addWidget(QLabel("상태"), 1, 6)
        layout.addWidget(self.watching_combo, 1, 7)
        layout.addWidget(self.pending_combo, 1, 8)
        layout.addWidget(self.clear_button, 1, 9)

        for widget in [
            self.search_edit,
            self.holding_combo,
            self.auto_buy_combo,
            self.open_order_combo,
            self.stop_risk_combo,
            self.take_profit_combo,
            self.watching_combo,
            self.pending_combo,
        ]:
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self.search_changed.emit)
            else:
                widget.currentIndexChanged.connect(self.filters_changed.emit)
        self.clear_button.clicked.connect(self.clear_filters)

    def search_text(self) -> str:
        return self.search_edit.text()

    def holding_only(self) -> bool:
        return bool(self.holding_combo.currentData())

    def auto_buy_only(self) -> bool:
        return bool(self.auto_buy_combo.currentData())

    def open_order_only(self) -> bool:
        return bool(self.open_order_combo.currentData())

    def stop_risk_only(self) -> bool:
        return bool(self.stop_risk_combo.currentData())

    def take_profit_only(self) -> bool:
        return bool(self.take_profit_combo.currentData())

    def watching_only(self) -> bool:
        return bool(self.watching_combo.currentData())

    def pending_only(self) -> bool:
        return bool(self.pending_combo.currentData())

    def clear_filters(self) -> None:
        self.search_edit.clear()
        for combo in [
            self.holding_combo,
            self.auto_buy_combo,
            self.open_order_combo,
            self.stop_risk_combo,
            self.take_profit_combo,
            self.watching_combo,
            self.pending_combo,
        ]:
            combo.setCurrentIndex(0)
        self.filters_changed.emit()

    @staticmethod
    def _bool_combo(label: str) -> QComboBox:
        combo = QComboBox()
        combo.addItem("전체", False)
        combo.addItem(label, True)
        return combo
