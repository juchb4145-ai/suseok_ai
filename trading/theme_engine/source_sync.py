from __future__ import annotations

from datetime import datetime
from typing import Iterable

from trading.theme_engine.evidence import ThemeEvidenceService
from trading.theme_engine.membership import ThemeMembershipBuilder
from trading.theme_engine.models import ThemeSourceSyncResult, ThemeSourceSyncRun
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.resolver import ThemeCanonicalResolver
from trading.theme_engine.source_base import BaseThemeSource


RETIRED_THEME_SOURCE_NAMES = (
    "kiwoom",
    "fixture",
    "manual_fixture",
    "themelab_fixture",
    "infostock_fixture",
    "news_fixture",
    "internal_dynamic_theme_engine",
    "dynamic_theme_cluster",
)


class ThemeSourceSyncService:
    def __init__(self, repository: ThemeEngineRepository, sources: Iterable[BaseThemeSource] | None = None) -> None:
        self.repository = repository
        self.sources: dict[str, BaseThemeSource] = {}
        for source in sources or []:
            self.add_source(source)

    def add_source(self, source: BaseThemeSource) -> None:
        self.sources[source.source_name] = source

    def sync_source(
        self,
        source_name: str,
        *,
        replace: bool = False,
        purge_sources: Iterable[str] | None = None,
    ) -> ThemeSourceSyncResult:
        source = self.sources[source_name]
        result = self._run_source(source, replace=replace, purge_sources=purge_sources)
        self.repository.save_source_sync_run(result)
        return result

    def sync_all_sources(self) -> list[ThemeSourceSyncResult]:
        results = []
        for source_name in list(self.sources):
            results.append(self.sync_source(source_name))
        return results

    def get_last_sync_status(self) -> list[ThemeSourceSyncRun]:
        return self.repository.latest_source_sync_runs(limit=max(1, len(self.sources) or 20))

    def _run_source(
        self,
        source: BaseThemeSource,
        *,
        replace: bool = False,
        purge_sources: Iterable[str] | None = None,
    ) -> ThemeSourceSyncResult:
        started_at = _now_text()
        theme_count = 0
        member_count = 0
        details = {"source": source.source_name, "replace": bool(replace)}
        try:
            resolver = ThemeCanonicalResolver(self.repository)
            evidence_service = ThemeEvidenceService(self.repository, resolver)
            themes = source.fetch_themes()
            theme_count = len(themes)
            source_payloads = []
            for source_theme in themes:
                members = source.fetch_members(source_theme)
                member_count += len(members)
                source_payloads.append((source_theme, members))
            if replace:
                sources_to_purge = [source.source_name]
                sources_to_purge.extend(str(item) for item in list(purge_sources or []))
                purge_result = self.repository.purge_sources(sources_to_purge)
                details["purged_sources"] = sorted({str(item) for item in sources_to_purge if str(item)})
                details["purge_result"] = purge_result
            for source_theme, members in source_payloads:
                evidence_service.ingest_source_theme(source_theme, members)
            ThemeMembershipBuilder(self.repository).build_all_current_memberships()
            source.last_sync_at = datetime.now()
            return ThemeSourceSyncResult(
                source=source.source_name,
                status="success",
                theme_count=theme_count,
                member_count=member_count,
                started_at=started_at,
                finished_at=_now_text(),
                details=details,
            )
        except Exception as exc:
            details["exception_type"] = type(exc).__name__
            return ThemeSourceSyncResult(
                source=source.source_name,
                status="failed",
                theme_count=theme_count,
                member_count=member_count,
                error_count=1,
                message=str(exc),
                started_at=started_at,
                finished_at=_now_text(),
                details=details,
            )


def _now_text() -> str:
    return datetime.now().replace(microsecond=0).isoformat()
