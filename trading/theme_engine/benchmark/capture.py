from __future__ import annotations

import argparse
import html
import json
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from trading.theme_engine.benchmark.schemas import ExternalThemeBenchmarkSnapshot
from trading.theme_engine.normalizer import normalize_stock_code


DEFAULT_TIMEOUT_SEC = 10.0
PARSE_FAILED = "PARSE_FAILED"


class CaptureError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class BenchmarkCaptureProvider(ABC):
    source: str = ""

    def capture(self, url: str, trade_date: str, *, timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> ExternalThemeBenchmarkSnapshot:
        payload = _fetch_once(url, timeout_sec=timeout_sec)
        if _looks_like_login_required(payload):
            raise CaptureError("LOGIN_REQUIRED")
        return self.parse_payload(payload, trade_date=trade_date)

    @abstractmethod
    def parse_payload(self, payload: str, *, trade_date: str) -> ExternalThemeBenchmarkSnapshot:
        raise NotImplementedError


class RoyalroaderCaptureProvider(BenchmarkCaptureProvider):
    source = "royalroader"

    def parse_payload(self, payload: str, *, trade_date: str) -> ExternalThemeBenchmarkSnapshot:
        return parse_royalroader_payload(payload, trade_date=trade_date, source=self.source)


class ThemelabCaptureProvider(BenchmarkCaptureProvider):
    source = "themelab"

    def parse_payload(self, payload: str, *, trade_date: str) -> ExternalThemeBenchmarkSnapshot:
        return parse_external_benchmark_payload(payload, trade_date=trade_date, source=self.source)


def parse_royalroader_payload(
    payload: str,
    *,
    trade_date: str,
    source: str = "royalroader",
) -> ExternalThemeBenchmarkSnapshot:
    return parse_external_benchmark_payload(payload, trade_date=trade_date, source=source)


def parse_external_benchmark_payload(
    payload: str,
    *,
    trade_date: str,
    source: str,
) -> ExternalThemeBenchmarkSnapshot:
    try:
        raw = _extract_json_payload(payload)
        themes = [_theme_dict(item, index) for index, item in enumerate(_theme_items(raw), start=1)]
    except CaptureError:
        raise
    except Exception as exc:
        raise CaptureError(PARSE_FAILED, f"{PARSE_FAILED}: {exc}") from exc
    if not themes:
        raise CaptureError(PARSE_FAILED)
    return {
        "source": source,
        "captured_at": _now_ts(),
        "trade_date": trade_date,
        "ranking_basis": str(raw.get("ranking_basis") or raw.get("rankingBasis") or "change_rate"),
        "themes": themes,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if os.environ.get("CI"):
        raise SystemExit("benchmark capture is disabled in CI")
    provider = _provider(args.source)
    try:
        snapshot = provider.capture(args.url, args.trade_date, timeout_sec=float(args.timeout_sec))
    except CaptureError as exc:
        raise SystemExit(f"{exc.code}: {exc}") from None
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manually capture an external theme benchmark snapshot.")
    parser.add_argument("--source", required=True, choices=["royalroader", "themelab"])
    parser.add_argument("--url", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout-sec", type=float, default=DEFAULT_TIMEOUT_SEC)
    return parser.parse_args(argv)


def _provider(source: str) -> BenchmarkCaptureProvider:
    if source == "royalroader":
        return RoyalroaderCaptureProvider()
    if source == "themelab":
        return ThemelabCaptureProvider()
    raise CaptureError("UNSUPPORTED_SOURCE", source)


def _fetch_once(url: str, *, timeout_sec: float) -> str:
    request = Request(url, headers={"User-Agent": "DynamicThemeBenchmarkCapture/1.0"})
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            status = int(getattr(response, "status", None) or 200)
            if status in {401, 403}:
                raise CaptureError("LOGIN_REQUIRED" if status == 401 else "HTTP_403")
            if status == 429:
                raise CaptureError("HTTP_429")
            content_type = str(response.headers.get("Content-Type") or "")
            charset = "utf-8"
            match = re.search(r"charset=([^;\s]+)", content_type, re.I)
            if match:
                charset = match.group(1)
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise CaptureError("LOGIN_REQUIRED" if exc.code == 401 else "HTTP_403") from exc
        if exc.code == 429:
            raise CaptureError("HTTP_429") from exc
        raise CaptureError(f"HTTP_{exc.code}") from exc
    except URLError as exc:
        raise CaptureError("REQUEST_FAILED", str(exc)) from exc


def _extract_json_payload(payload: str) -> dict[str, Any]:
    text = str(payload or "").strip()
    if not text:
        raise CaptureError(PARSE_FAILED)
    if text.startswith("{"):
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    script_patterns = [
        r"<script[^>]+id=[\"'](?:benchmark-data|royalroader-data|themelab-data|__BENCHMARK_DATA__)[\"'][^>]*>(.*?)</script>",
        r"<script[^>]+type=[\"']application/json[\"'][^>]*>(.*?)</script>",
    ]
    for pattern in script_patterns:
        match = re.search(pattern, text, re.I | re.S)
        if not match:
            continue
        value = json.loads(html.unescape(match.group(1)).strip())
        if isinstance(value, dict):
            return value
    marker_match = re.search(r"window\.__BENCHMARK_DATA__\s*=", text)
    if marker_match:
        value = json.loads(_balanced_json_object(text[marker_match.end() :]))
        if isinstance(value, dict):
            return value
    raise CaptureError(PARSE_FAILED)


def _balanced_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        raise CaptureError(PARSE_FAILED)
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text[start:], start=start):
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise CaptureError(PARSE_FAILED)


def _theme_items(payload: dict[str, Any]) -> list[Any]:
    for key in ("themes", "themeList", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    data = payload.get("data")
    if isinstance(data, dict):
        return _theme_items(data)
    raise CaptureError(PARSE_FAILED)


def _theme_dict(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise CaptureError(PARSE_FAILED)
    name = _first_text(item, "external_theme_name", "theme_name", "themeName", "name", "title")
    if not name:
        raise CaptureError(PARSE_FAILED)
    top_stocks = _stock_list(_first_list(item, "top_stocks", "topStocks", "stocks", "stockList"), require_rank=True)
    members = _stock_list(_first_list(item, "members", "memberStocks", "memberList"), require_rank=False)
    if not members:
        members = [{"stock_code": stock["stock_code"], "stock_name": stock.get("stock_name", "")} for stock in top_stocks]
    if not top_stocks and not members:
        raise CaptureError(PARSE_FAILED)
    return {
        "external_theme_name": name,
        "canonical_theme_hint": _first_text(item, "canonical_theme_hint", "canonicalThemeHint") or name,
        "rank": _int_value(_first_value(item, "rank", "theme_rank", "themeRank"), default=index),
        "score": _float_value(_first_value(item, "score"), default=0.0),
        "top_stocks": sorted(top_stocks, key=lambda stock: int(stock.get("rank") or 0)),
        "members": members,
    }


def _stock_list(values: list[Any], *, require_rank: bool) -> list[dict[str, Any]]:
    result = []
    for index, item in enumerate(values, start=1):
        if not isinstance(item, dict):
            raise CaptureError(PARSE_FAILED)
        raw_code = _first_text(item, "stock_code", "stockCode", "code", "symbol")
        code = normalize_stock_code(raw_code)
        if not code:
            raise CaptureError(PARSE_FAILED, f"{PARSE_FAILED}: invalid stock_code")
        stock = {
            "stock_code": code,
            "stock_name": _first_text(item, "stock_name", "stockName", "name") or "",
        }
        rank = _int_value(_first_value(item, "rank", "stock_rank", "stockRank"), default=index)
        if require_rank or rank:
            stock["rank"] = rank
        change_rate = _optional_float(_first_value(item, "change_rate", "changeRate", "rate"))
        turnover = _optional_float(_first_value(item, "turnover", "tradeValue", "trade_value"))
        if change_rate is not None:
            stock["change_rate"] = change_rate
        if turnover is not None:
            stock["turnover"] = turnover
        result.append(stock)
    return result


def _looks_like_login_required(payload: str) -> bool:
    text = str(payload or "").lower()
    return any(value in text for value in ("login required", "sign in", "로그인"))


def _first_value(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def _first_text(item: dict[str, Any], *keys: str) -> str:
    value = _first_value(item, *keys)
    return str(value or "").strip()


def _first_list(item: dict[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        value = item.get(key)
        if isinstance(value, list):
            return value
    return []


def _int_value(value: Any, *, default: int) -> int:
    if value is None or str(value).strip() == "":
        return int(default)
    try:
        return int(float(str(value).replace(",", "").strip()))
    except ValueError as exc:
        raise CaptureError(PARSE_FAILED, f"{PARSE_FAILED}: invalid rank") from exc


def _float_value(value: Any, *, default: float) -> float:
    if value is None or str(value).strip() == "":
        return float(default)
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError as exc:
        raise CaptureError(PARSE_FAILED, f"{PARSE_FAILED}: invalid numeric value") from exc


def _optional_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return _float_value(value, default=0.0)


def _now_ts() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
