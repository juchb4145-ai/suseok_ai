from __future__ import annotations

import json
from pathlib import Path

from trading.theme_engine.models import RelationType, ThemeEvidenceType, ThemeMemberEvidence, ThemeSourcePayload
from trading.theme_engine.normalizer import normalize_stock_code
from trading.theme_engine.source_base import BaseThemeSource


class FixtureThemeSource(BaseThemeSource):
    source_name = "fixture"
    supports_live = False

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self.payload = json.loads(self.path.read_text(encoding="utf-8"))

    def fetch_themes(self) -> list[ThemeSourcePayload]:
        themes: list[ThemeSourcePayload] = []
        for item in self.payload.get("source_themes", []):
            themes.append(
                ThemeSourcePayload(
                    source=str(item.get("source") or self.source_name),
                    source_theme_id=str(item.get("source_theme_id") or ""),
                    source_theme_name=str(item.get("source_theme_name") or ""),
                    aliases=list(item.get("aliases") or self.payload.get("aliases") or []),
                    raw_payload=dict(item),
                )
            )
        return themes

    def fetch_members(self, source_theme: ThemeSourcePayload) -> list[ThemeMemberEvidence]:
        members = []
        source_members = self.payload.get("members_by_source", {}).get(source_theme.source)
        raw_members = source_members if source_members is not None else self.payload.get("members", [])
        for item in raw_members:
            members.append(
                ThemeMemberEvidence(
                    theme_id="",
                    stock_code=normalize_stock_code(str(item.get("stock_code") or item.get("code") or "")),
                    stock_name=str(item.get("stock_name") or item.get("name") or ""),
                    source=source_theme.source,
                    evidence_type=str(item.get("evidence_type") or ThemeEvidenceType.MANUAL_FIXTURE.value),
                    relation_type=str(item.get("relation_type") or RelationType.UNKNOWN.value),
                    reason=str(item.get("reason") or source_theme.source_theme_name),
                    confidence=float(item.get("confidence", 0.7)),
                )
            )
        return members

    def mock_snapshots(self) -> list[dict]:
        return list(self.payload.get("mock_ticks") or [])
