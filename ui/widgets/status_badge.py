from __future__ import annotations

from PyQt5.QtWidgets import QLabel

from ui.style import badge_style, normalize_tone


class StatusBadge(QLabel):
    def __init__(self, text: str = "", tone: str = "neutral") -> None:
        super().__init__(text)
        self.tone = normalize_tone(tone)
        self.setStyleSheet(badge_style(self.tone))

    def set_status(self, text: str, tone: str = "neutral") -> None:
        self.tone = normalize_tone(tone)
        self.setText(text)
        self.setStyleSheet(badge_style(self.tone))
