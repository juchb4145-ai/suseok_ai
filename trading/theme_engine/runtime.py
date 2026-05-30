from __future__ import annotations

from trading.theme_engine.evidence import ThemeEvidenceService
from trading.theme_engine.membership import ThemeMembershipBuilder
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.resolver import ThemeCanonicalResolver
from trading.theme_engine.scorer import ThemeScoringEngine
from trading.theme_engine.stock_snapshot import snapshot_from_dict


class DynamicThemeEngineRuntime:
    def __init__(self, repository: ThemeEngineRepository) -> None:
        self.repository = repository
        self.resolver = ThemeCanonicalResolver(repository)
        self.evidence_service = ThemeEvidenceService(repository, self.resolver)
        self.membership_builder = ThemeMembershipBuilder(repository)
        self.scorer = ThemeScoringEngine(repository)
        self.running = False

    def sync_source(self, source) -> None:
        self.running = True
        self.evidence_service.sync_source(source)
        self.membership_builder.build_all_current_memberships()

    def score_fixture_ticks(self, ticks: list[dict]):
        snapshots = [snapshot_from_dict(item) for item in ticks]
        themes = self.repository.list_canonical_themes()
        inputs = [
            (theme.theme_id, theme.display_name, self.repository.get_members_by_theme(theme.theme_id, active=True))
            for theme in themes
        ]
        return self.scorer.score_and_rank(inputs, snapshots)
