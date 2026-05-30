from __future__ import annotations

from trading.theme_engine.models import ThemeMemberEvidence, ThemeSourcePayload
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.resolver import ThemeCanonicalResolver
from trading.theme_engine.source_base import BaseThemeSource


class ThemeEvidenceService:
    def __init__(self, repository: ThemeEngineRepository, resolver: ThemeCanonicalResolver) -> None:
        self.repository = repository
        self.resolver = resolver

    def sync_source(self, source: BaseThemeSource) -> list[ThemeMemberEvidence]:
        saved: list[ThemeMemberEvidence] = []
        for source_theme in source.fetch_themes():
            saved.extend(self.ingest_source_theme(source_theme, source.fetch_members(source_theme)))
        return saved

    def ingest_source_theme(
        self,
        source_theme: ThemeSourcePayload,
        member_evidence: list[ThemeMemberEvidence],
    ) -> list[ThemeMemberEvidence]:
        canonical = self.resolver.match_or_create_theme(
            source_theme.source,
            source_theme.source_theme_name,
            source_theme.source_theme_id,
        )
        for alias in source_theme.aliases:
            self.resolver.add_alias(canonical.theme_id, alias, source_theme.source)
            self.resolver.add_alias(canonical.theme_id, alias, "")
        saved: list[ThemeMemberEvidence] = []
        for evidence in member_evidence:
            evidence.theme_id = canonical.theme_id
            if not evidence.source:
                evidence.source = source_theme.source
            saved.append(self.repository.add_member_evidence(evidence))
        return saved
