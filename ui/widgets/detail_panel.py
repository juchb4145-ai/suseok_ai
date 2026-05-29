from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QTextOption
from PyQt5.QtWidgets import QLabel, QSizePolicy, QTextEdit, QVBoxLayout, QWidget


class CandidateDetailPanel(QWidget):
    def __init__(self, title: str = "후보 상세", empty_text: str = "선택된 후보 없음") -> None:
        super().__init__()
        self._default_title = title
        self._empty_text = empty_text
        self.setMinimumWidth(320)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.title_label = QLabel(title)
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet("font-weight: 700;")
        self.detail_view = QTextEdit()
        self.detail_view.setReadOnly(True)
        self.detail_view.setAcceptRichText(False)
        self.detail_view.setLineWrapMode(QTextEdit.WidgetWidth)
        self.detail_view.setWordWrapMode(QTextOption.WrapAnywhere)
        self.detail_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.detail_view.setMinimumWidth(300)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.title_label)
        layout.addWidget(self.detail_view, 1)
        self.show_empty()

    def show_empty(self) -> None:
        self.title_label.setText(self._default_title)
        self.detail_view.setPlainText(self._empty_text)

    def show_detail(self, title: str, text: str) -> None:
        self.title_label.setText(title)
        self.detail_view.setPlainText(text)

    def text(self) -> str:
        return self.detail_view.toPlainText()
