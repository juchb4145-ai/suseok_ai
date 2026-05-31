from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from trading.theme_engine.models import RelationType, ThemeEvidenceType, ThemeMemberEvidence, ThemeSourcePayload
from trading.theme_engine.normalizer import normalize_stock_code
from trading.theme_engine.source_base import BaseThemeSource


DEFAULT_NAVER_THEME_URL = "https://finance.naver.com/sise/theme.naver?field=change_rate&ordering=desc"
NAVER_THEME_SOURCE_NAME = "naver_theme_universe"


@dataclass(frozen=True)
class NaverThemeListItem:
    source_theme_id: str
    source_theme_name: str
    detail_url: str


@dataclass(frozen=True)
class NaverThemeMemberItem:
    stock_code: str
    stock_name: str
    reason: str = ""


class NaverThemeUniverseSource(BaseThemeSource):
    source_name = NAVER_THEME_SOURCE_NAME
    supports_live = False

    def __init__(
        self,
        base_url: str = DEFAULT_NAVER_THEME_URL,
        session: Any | None = None,
        timeout_sec: float = 5.0,
        max_pages: int = 20,
        request_delay_sec: float = 0.1,
    ) -> None:
        super().__init__()
        self.base_url = str(base_url)
        self.session = session or requests.Session()
        self.timeout_sec = float(timeout_sec)
        self.max_pages = max(1, int(max_pages))
        self.request_delay_sec = max(0.0, float(request_delay_sec))

    def fetch_themes(self) -> list[ThemeSourcePayload]:
        items: list[NaverThemeListItem] = []
        seen: set[tuple[str, str]] = set()
        for page in range(1, self.max_pages + 1):
            html = self._fetch_text(_with_page(self.base_url, page))
            parsed = parse_theme_list(html, base_url=self.base_url)
            new_count = 0
            for item in parsed:
                key = (item.source_theme_id, item.source_theme_name)
                if key in seen:
                    continue
                seen.add(key)
                items.append(item)
                new_count += 1
            if page > 1 and new_count == 0:
                break
            if self.request_delay_sec and page < self.max_pages:
                time.sleep(self.request_delay_sec)
        return [
            ThemeSourcePayload(
                source=self.source_name,
                source_theme_id=item.source_theme_id,
                source_theme_name=item.source_theme_name,
                raw_payload={
                    "detail_no": item.source_theme_id,
                    "detail_url": item.detail_url,
                    "policy": "universe_only",
                },
            )
            for item in items
        ]

    def fetch_members(self, source_theme: ThemeSourcePayload) -> list[ThemeMemberEvidence]:
        detail_url = str(source_theme.raw_payload.get("detail_url") or "")
        if not detail_url:
            detail_url = urljoin(
                self.base_url,
                f"/sise/sise_group_detail.naver?type=theme&no={source_theme.source_theme_id}",
            )
        html = self._fetch_text(detail_url)
        members = parse_theme_detail(html)
        return [
            ThemeMemberEvidence(
                theme_id="",
                stock_code=member.stock_code,
                stock_name=member.stock_name,
                source=self.source_name,
                evidence_type=ThemeEvidenceType.SOURCE_MEMBER,
                relation_type=RelationType.SAME_INDUSTRY,
                reason=member.reason or source_theme.source_theme_name,
                confidence=0.85,
            )
            for member in members
        ]

    def _fetch_text(self, url: str) -> str:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = self.session.get(url, timeout=self.timeout_sec, headers=headers)
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        if isinstance(response, str):
            return response
        content = getattr(response, "content", None)
        if content:
            encoding = getattr(response, "encoding", None) or "utf-8"
            for candidate in (encoding, "utf-8", "cp949", "euc-kr"):
                try:
                    return content.decode(candidate)
                except (LookupError, UnicodeDecodeError):
                    continue
        return str(getattr(response, "text", "") or "")


def parse_theme_list(html: str, *, base_url: str = DEFAULT_NAVER_THEME_URL) -> list[NaverThemeListItem]:
    soup = BeautifulSoup(html or "", "html.parser")
    items: list[NaverThemeListItem] = []
    seen: set[tuple[str, str]] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        match = re.search(r"/sise/sise_group_detail\.naver\?[^\"']*type=theme[^\"']*no=(\d+)", href)
        if not match:
            continue
        theme_name = _clean_text(anchor.get_text(" ", strip=True))
        if not theme_name:
            continue
        item = NaverThemeListItem(
            source_theme_id=match.group(1),
            source_theme_name=theme_name,
            detail_url=urljoin(base_url, href),
        )
        key = (item.source_theme_id, item.source_theme_name)
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def parse_theme_detail(html: str) -> list[NaverThemeMemberItem]:
    soup = BeautifulSoup(html or "", "html.parser")
    members: list[NaverThemeMemberItem] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        match = re.search(r"/item/main\.naver\?code=(\d{6})", href)
        if not match:
            continue
        code = normalize_stock_code(match.group(1))
        if not code or code in seen:
            continue
        row = anchor.find_parent("tr")
        reason = ""
        if row is not None:
            reason_node = row.select_one(".info_txt")
            if reason_node is not None:
                reason = _clean_text(reason_node.get_text(" ", strip=True))
        members.append(
            NaverThemeMemberItem(
                stock_code=code,
                stock_name=_clean_text(anchor.get_text(" ", strip=True)),
                reason=reason,
            )
        )
        seen.add(code)
    return members


def _with_page(url: str, page: int) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["page"] = str(max(1, int(page)))
    return urlunparse(parsed._replace(query=urlencode(query)))


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
