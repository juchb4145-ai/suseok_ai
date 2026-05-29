from __future__ import annotations


VALID_TONES = {"neutral", "success", "warning", "danger", "unavailable", "info", "muted"}

TONE_COLORS = {
    "neutral": ("#f5f7fb", "#1f2937", "#d7dde8"),
    "success": ("#e9f7ef", "#17633a", "#b7e0c8"),
    "warning": ("#ffefe0", "#8a3d00", "#f3c28a"),
    "danger": ("#ffe8e8", "#9f1d1d", "#f1a8a8"),
    "unavailable": ("#eceff3", "#5b6472", "#c9d0d9"),
    "info": ("#eef4ff", "#1d4f8f", "#b8cdf6"),
    "muted": ("#f5f7fb", "#5b6472", "#d7dde8"),
}


def normalize_tone(tone: str) -> str:
    return tone if tone in VALID_TONES else "neutral"


def badge_style(tone: str) -> str:
    bg, fg, border = TONE_COLORS[normalize_tone(tone)]
    return (
        "font-weight: 700; padding: 4px 8px; border-radius: 4px; "
        f"background: {bg}; color: {fg}; border: 1px solid {border};"
    )


def summary_card_style(tone: str) -> str:
    bg, fg, border = TONE_COLORS[normalize_tone(tone)]
    return (
        "QFrame {"
        f"background: {bg}; border: 1px solid {border}; border-radius: 6px;"
        "}"
        "QLabel {"
        f"color: {fg};"
        "}"
    )
