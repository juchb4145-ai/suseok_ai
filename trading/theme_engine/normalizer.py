from __future__ import annotations

import hashlib
import re
import unicodedata


_AI_VARIANTS = (
    ("에이아이", "ai"),
    ("인공지능", "ai"),
    ("a.i.", "ai"),
    ("a/i", "ai"),
)


def normalize_stock_code(code: str) -> str:
    value = str(code or "").strip().upper().replace(",", "")
    if value.startswith("A") and value[1:].isdigit():
        value = value[1:]
    if re.fullmatch(r"\d+\.0+", value):
        value = value.split(".", 1)[0]
    if value.isdigit() and 1 <= len(value) <= 6:
        return value.zfill(6)
    return ""


def normalize_theme_name(name: str) -> str:
    value = unicodedata.normalize("NFKC", str(name or "")).strip().lower()
    for old, new in _AI_VARIANTS:
        value = value.replace(old, new)
    value = re.sub(r"\bai\b", "ai", value)
    value = re.sub(r"[\s/_\-\(\)\[\]\{\}·,.:;|+&]+", "", value)
    value = re.sub(r"[^0-9a-z가-힣]", "", value)
    if "퓨리오사" in value and "창투" in value:
        value = value.replace("ai", "")
    return value


def suggest_theme_id(name: str) -> str:
    normalized = normalize_theme_name(name)
    if not normalized:
        return "theme_unknown"
    if ("퓨리오사" in normalized and "ai" in normalized) or "furiosaai" in normalized:
        return "furiosa_ai"
    asciiish = unicodedata.normalize("NFKD", normalized).encode("ascii", "ignore").decode("ascii")
    asciiish = re.sub(r"[^0-9a-z]+", "_", asciiish.lower()).strip("_")
    if asciiish:
        return asciiish
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
    return f"theme_{digest}"
