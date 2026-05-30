from __future__ import annotations

from datetime import datetime
from typing import Any

from trading.theme_engine.evidence import ThemeEvidenceService
from trading.theme_engine.membership import ThemeMembershipBuilder
from trading.theme_engine.models import (
    RelationType,
    ThemeEvidenceType,
    ThemeMemberEvidence,
    ThemeSourcePayload,
    ThemeSourceSyncResult,
)
from trading.theme_engine.normalizer import normalize_stock_code
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.resolver import ThemeCanonicalResolver
from trading.theme_engine.source_base import BaseThemeSource


class KiwoomThemeSource(BaseThemeSource):
    """Adapter shell for Kiwoom theme APIs.

    The live OpenAPI calls are intentionally thin wrappers so tests can mock the
    client.  Real opt90001/opt90002 hardening belongs in a follow-up PR.
    """

    source_name = "kiwoom"
    supports_live = True

    def __init__(self, client, repository: ThemeEngineRepository | None = None) -> None:
        super().__init__()
        self.client = client
        self.repository = repository

    def fetch_themes(self) -> list[ThemeSourcePayload]:
        raw = _call_first_available(self.client, ["get_theme_group_list", "GetThemeGroupList"])
        if raw is None:
            raw = _call_first_available(self.client, ["request_opt90001", "opt90001", "request_theme_groups"])
        themes = parse_theme_group_list(raw)
        return [
            ThemeSourcePayload(
                source=self.source_name,
                source_theme_id=theme_id,
                source_theme_name=theme_name,
                raw_payload={"raw": raw},
            )
            for theme_id, theme_name in themes
        ]

    def fetch_members(self, source_theme: ThemeSourcePayload) -> list[ThemeMemberEvidence]:
        raw = _call_first_available(
            self.client,
            ["get_theme_group_code", "GetThemeGroupCode"],
            source_theme.source_theme_id,
        )
        if raw is None:
            raw = _call_first_available(
                self.client,
                ["request_opt90002", "opt90002", "request_theme_members"],
                source_theme.source_theme_id,
            )
        members = parse_theme_member_codes(raw)
        result = []
        for code in members:
            name = _call_first_available(
                self.client,
                ["get_master_code_name", "GetMasterCodeName", "get_code_name"],
                code,
            ) or ""
            result.append(
                ThemeMemberEvidence(
                    theme_id="",
                    stock_code=normalize_stock_code(code),
                    stock_name=str(name),
                    source=self.source_name,
                    evidence_type=ThemeEvidenceType.SOURCE_MEMBER,
                    relation_type=RelationType.SAME_INDUSTRY,
                    reason=source_theme.source_theme_name,
                    confidence=0.75,
                )
            )
        return result

    def sync_all(self) -> ThemeSourceSyncResult:
        started_at = _now_text()
        try:
            themes = self.fetch_themes()
            member_count = 0
            if self.repository is not None:
                resolver = ThemeCanonicalResolver(self.repository)
                evidence = ThemeEvidenceService(self.repository, resolver)
                for theme in themes:
                    members = self.fetch_members(theme)
                    member_count += len(members)
                    evidence.ingest_source_theme(theme, members)
                ThemeMembershipBuilder(self.repository).build_all_current_memberships()
            else:
                for theme in themes:
                    member_count += len(self.fetch_members(theme))
            self.last_sync_at = datetime.now()
            return ThemeSourceSyncResult(
                source=self.source_name,
                status="success",
                theme_count=len(themes),
                member_count=member_count,
                started_at=started_at,
                finished_at=_now_text(),
            )
        except Exception as exc:
            return ThemeSourceSyncResult(
                source=self.source_name,
                status="failed",
                error_count=1,
                message=str(exc),
                started_at=started_at,
                finished_at=_now_text(),
            )


def parse_theme_group_list(raw) -> list[tuple[str, str]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        if "themes" in raw:
            return parse_theme_group_list(raw["themes"])
        result = []
        for key, value in raw.items():
            if isinstance(value, dict):
                result.append((str(value.get("theme_id") or value.get("code") or key), str(value.get("theme_name") or value.get("name") or "")))
            else:
                result.append((str(key), str(value)))
        return _dedupe_theme_pairs(result)
    if isinstance(raw, (list, tuple)):
        result = []
        for item in raw:
            if isinstance(item, dict):
                result.append(
                    (
                        str(item.get("theme_id") or item.get("theme_code") or item.get("code") or item.get("테마코드") or ""),
                        str(item.get("theme_name") or item.get("name") or item.get("테마명") or ""),
                    )
                )
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                result.append((str(item[0]), str(item[1])))
            else:
                result.extend(parse_theme_group_list(str(item)))
        return _dedupe_theme_pairs(result)
    text = str(raw)
    result = []
    for chunk in text.replace("\n", ";").split(";"):
        if not chunk.strip():
            continue
        if "^" in chunk:
            theme_id, name = chunk.split("^", 1)
        elif "|" in chunk:
            theme_id, name = chunk.split("|", 1)
        else:
            theme_id, name = "", chunk
        if theme_id.strip() or name.strip():
            result.append((theme_id.strip(), name.strip()))
    return _dedupe_theme_pairs(result)


def parse_theme_member_codes(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        result = []
        for value in raw:
            if isinstance(value, dict):
                code = normalize_stock_code(
                    str(value.get("stock_code") or value.get("code") or value.get("종목코드") or "")
                )
            else:
                code = normalize_stock_code(str(value))
            if code and code not in result:
                result.append(code)
        return result
    if isinstance(raw, dict):
        if "members" in raw:
            return parse_theme_member_codes(raw["members"])
        if "codes" in raw:
            return parse_theme_member_codes(raw["codes"])
        return parse_theme_member_codes(list(raw.keys()))
    result = []
    for chunk in str(raw).replace("\n", ";").replace(",", ";").split(";"):
        text = chunk.strip()
        if not text:
            continue
        code = normalize_stock_code(text.split("^", 1)[0].split("|", 1)[0])
        if code and code not in result:
            result.append(code)
    return result


def _call_first_available(client, names: list[str], *args):
    for name in names:
        fn = getattr(client, name, None)
        if callable(fn):
            return fn(*args)
    return None


def _dedupe_theme_pairs(values: list[tuple[str, str]]) -> list[tuple[str, str]]:
    result = []
    seen = set()
    for theme_id, name in values:
        key = (theme_id, name)
        if not name or key in seen:
            continue
        seen.add(key)
        result.append((theme_id, name))
    return result


def _now_text() -> str:
    return datetime.now().replace(microsecond=0).isoformat()
