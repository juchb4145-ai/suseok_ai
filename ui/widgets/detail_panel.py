from __future__ import annotations

from PyQt5.QtWidgets import QLabel, QTextEdit, QVBoxLayout, QWidget


class CandidateDetailPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.title_label = QLabel("후보 상세")
        self.title_label.setStyleSheet("font-weight: 700;")
        self.detail_view = QTextEdit()
        self.detail_view.setReadOnly(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.title_label)
        layout.addWidget(self.detail_view, 1)
        self.show_empty()

    def show_empty(self) -> None:
        self.title_label.setText("후보 상세")
        self.detail_view.setPlainText("선택된 후보 없음")

    def show_detail(self, title: str, text: str) -> None:
        self.title_label.setText(title)
        self.detail_view.setPlainText(text)

    def text(self) -> str:
        return self.detail_view.toPlainText()
