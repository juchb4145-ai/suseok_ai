from __future__ import annotations

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget


class CandidateFilterBar(QWidget):
    filters_changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("코드/종목명/테마/Gate/Sub/Reason")
        self.state_combo = QComboBox()
        self.state_combo.addItems(["전체", "READY", "BLOCKED", "WATCHING", "DETECTED"])
        self.recover_combo = QComboBox()
        self.recover_combo.addItem("전체", False)
        self.recover_combo.addItem("Recover 가능만", True)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("전체", "ALL")
        self.theme_combo.addItem("매핑 있음", "mapped")
        self.theme_combo.addItem("매핑 없음", "unmapped")
        self.clear_button = QPushButton("초기화")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("검색"))
        layout.addWidget(self.search_edit, 2)
        layout.addWidget(QLabel("상태"))
        layout.addWidget(self.state_combo)
        layout.addWidget(QLabel("Recover"))
        layout.addWidget(self.recover_combo)
        layout.addWidget(QLabel("테마"))
        layout.addWidget(self.theme_combo)
        layout.addWidget(self.clear_button)

        self.search_edit.textChanged.connect(self.filters_changed.emit)
        self.state_combo.currentIndexChanged.connect(self.filters_changed.emit)
        self.recover_combo.currentIndexChanged.connect(self.filters_changed.emit)
        self.theme_combo.currentIndexChanged.connect(self.filters_changed.emit)
        self.clear_button.clicked.connect(self.clear_filters)

    def search_text(self) -> str:
        return self.search_edit.text()

    def state_filter(self) -> str:
        return self.state_combo.currentText()

    def recover_only(self) -> bool:
        return bool(self.recover_combo.currentData())

    def theme_filter(self) -> str:
        return str(self.theme_combo.currentData() or "ALL")

    def clear_filters(self) -> None:
        self.search_edit.clear()
        self.state_combo.setCurrentIndex(0)
        self.recover_combo.setCurrentIndex(0)
        self.theme_combo.setCurrentIndex(0)
        self.filters_changed.emit()
