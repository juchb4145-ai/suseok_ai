from __future__ import annotations

from PyQt5.QtWidgets import QFrame, QLabel, QVBoxLayout

from ui.style import normalize_tone, summary_card_style


class SummaryCard(QFrame):
    def __init__(self, title: str, value: str = "-", detail: str = "", tone: str = "neutral") -> None:
        super().__init__()
        self.tone = normalize_tone(tone)
        self.title_label = QLabel(title)
        self.value_label = QLabel(value)
        self.detail_label = QLabel(detail)
        self.title_label.setObjectName("summaryCardTitle")
        self.value_label.setObjectName("summaryCardValue")
        self.detail_label.setObjectName("summaryCardDetail")
        self.title_label.setStyleSheet("font-size: 11px; font-weight: 700;")
        self.value_label.setStyleSheet("font-size: 16px; font-weight: 700;")
        self.detail_label.setStyleSheet("font-size: 11px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.detail_label)
        self.set_summary(value, detail, self.tone)

    def set_summary(self, value: str, detail: str = "", tone: str = "neutral") -> None:
        self.tone = normalize_tone(tone)
        self.value_label.setText(str(value))
        self.detail_label.setText(str(detail))
        self.setStyleSheet(summary_card_style(self.tone))
