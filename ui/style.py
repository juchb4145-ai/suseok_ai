from __future__ import annotations


VALID_TONES = {"neutral", "success", "warning", "danger", "unavailable", "info", "muted"}

THEME_LAB_COLORS = {
    "app_bg": "#0B0F14",
    "panel_bg": "#111827",
    "panel_bg_alt": "#0F172A",
    "panel_hover": "#172033",
    "border": "#1F2937",
    "border_active": "#334155",
    "text_primary": "#E5E7EB",
    "text_secondary": "#9CA3AF",
    "text_muted": "#6B7280",
    "positive": "#22C55E",
    "negative": "#EF4444",
    "warning": "#F59E0B",
    "info": "#38BDF8",
    "ready": "#10B981",
    "ready_small": "#38BDF8",
    "wait": "#FACC15",
    "blocked": "#EF4444",
    "observe": "#6B7280",
    "theme_leading": "#10B981",
    "theme_spreading": "#38BDF8",
    "theme_leader_only": "#F59E0B",
    "theme_watch": "#A78BFA",
    "theme_weak": "#6B7280",
    "risk_pass": "#10B981",
    "risk_adjust": "#38BDF8",
    "risk_soft_block": "#FACC15",
    "risk_hard_block": "#EF4444",
}

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


def tone_for_value(value: str) -> str:
    normalized = str(value or "").upper()
    if normalized in {"EXPANSION", "READY", "LEADING_THEME", "PASS"}:
        return "success"
    if normalized in {"SELECTIVE", "READY_SMALL", "SPREADING_THEME", "RISK_ADJUST"}:
        return "info"
    if normalized in {"CHOPPY", "WAIT", "LEADER_ONLY_THEME", "WATCH_THEME", "SOFT_BLOCK"}:
        return "warning"
    if normalized in {"RISK_OFF", "BLOCKED", "HARD_BLOCK"}:
        return "danger"
    if normalized in {"WEAK", "OBSERVE", "WEAK_THEME", "UNKNOWN"}:
        return "muted"
    return "neutral"
