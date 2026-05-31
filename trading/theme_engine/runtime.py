from __future__ import annotations

from datetime import datetime, timedelta

from trading.theme_engine.context_provider import DynamicThemeContextProvider
from trading.theme_engine.evidence import ThemeEvidenceService
from trading.theme_engine.membership import ThemeMembershipBuilder
from trading.theme_engine.models import StockSnapshot, ThemeActivitySnapshot
from trading.theme_engine.realtime_adapter import KiwoomRealtimeThemeAdapter
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.resolver import ThemeCanonicalResolver
from trading.theme_engine.scorer import ThemeScoringEngine
from trading.theme_engine.stock_snapshot import snapshot_from_dict
from trading.theme_engine.universe import ThemeUniverseBuilder, ThemeUniverseConfig
from trading.theme_engine.ws.broadcaster import ThemeWebSocketBroadcaster
from trading.theme_engine.ws.schemas import build_runtime_health_payload, build_theme_rank_payload


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

    def score_ticks(self, ticks: list[dict]):
        snapshots = [snapshot_from_dict(item) for item in ticks]
        themes = self.repository.list_canonical_themes()
        inputs = [
            (theme.theme_id, theme.display_name, self.repository.get_members_by_theme(theme.theme_id, active=True))
            for theme in themes
        ]
        return self.scorer.score_and_rank(inputs, snapshots)


class RealTimeThemeRuntime:
    def __init__(
        self,
        repository: ThemeEngineRepository,
        *,
        universe_builder: ThemeUniverseBuilder | None = None,
        realtime_adapter: KiwoomRealtimeThemeAdapter | None = None,
        scorer: ThemeScoringEngine | None = None,
        broadcaster: ThemeWebSocketBroadcaster | None = None,
        scoring_interval_sec: float = 1.0,
        db_snapshot_interval_sec: float = 5.0,
        ws_push_interval_sec: float = 1.0,
    ) -> None:
        self.repository = repository
        self.universe_builder = universe_builder or ThemeUniverseBuilder(repository, ThemeUniverseConfig())
        self.realtime_adapter = realtime_adapter or KiwoomRealtimeThemeAdapter()
        self.scorer = scorer or ThemeScoringEngine(repository=None)
        self.context_provider = DynamicThemeContextProvider(repository)
        self.broadcaster = broadcaster or ThemeWebSocketBroadcaster()
        self.scoring_interval = timedelta(seconds=float(scoring_interval_sec))
        self.db_snapshot_interval = timedelta(seconds=float(db_snapshot_interval_sec))
        self.ws_push_interval = timedelta(seconds=float(ws_push_interval_sec))
        self.running = False
        self.error_count = 0
        self.last_tick_at = ""
        self.last_rank_at = ""
        self._last_score_at_by_theme: dict[str, datetime] = {}
        self._last_db_save_at = datetime.min
        self._last_ws_push_at = datetime.min
        self._latest_rank: list[ThemeActivitySnapshot] = []
        self._active_universe: list[str] = []

    def start(self) -> None:
        self.running = True
        self._active_universe = self.universe_builder.build_active_universe()

    def stop(self) -> None:
        self.running = False

    def on_stock_snapshot(self, snapshot: StockSnapshot) -> None:
        if not self.running:
            self.start()
        self.realtime_adapter.update_snapshot(snapshot)
        self.last_tick_at = snapshot.updated_at or _now_text()
        for membership in self.universe_builder.themes_by_stock(snapshot.stock_code):
            self.recalculate_theme(membership.theme_id)

    def recalculate_theme(self, theme_id: str) -> ThemeActivitySnapshot | None:
        now = datetime.now()
        previous = self._last_score_at_by_theme.get(theme_id)
        if previous is not None and now - previous < self.scoring_interval:
            return next((item for item in self._latest_rank if item.theme_id == theme_id), None)
        theme = self.repository.get_canonical_theme(theme_id)
        if theme is None:
            return None
        memberships = self.repository.get_members_by_theme(theme_id, active=True)
        stock_codes = [item.stock_code for item in memberships]
        snapshots = self.realtime_adapter.latest_snapshots(stock_codes)
        scored = self.scorer.score_theme(theme_id, theme.display_name, memberships, snapshots)
        scored.status = theme.status
        scored.trade_eligible = theme.trade_eligible
        self._merge_rank([scored])
        self._last_score_at_by_theme[theme_id] = now
        self.last_rank_at = _now_text()
        self._maybe_persist_and_publish(now)
        return scored

    def recalculate_all_themes(self) -> list[ThemeActivitySnapshot]:
        themes = self.repository.list_canonical_themes()
        snapshots = self.realtime_adapter.all_snapshots()
        inputs = [
            (theme.theme_id, theme.display_name, self.repository.get_members_by_theme(theme.theme_id, active=True))
            for theme in themes
        ]
        ranked = self.scorer.score_and_rank(inputs, snapshots)
        theme_by_id = {theme.theme_id: theme for theme in themes}
        for item in ranked:
            theme = theme_by_id.get(item.theme_id)
            if theme is not None:
                item.status = theme.status
                item.trade_eligible = theme.trade_eligible
        self._latest_rank = ranked
        self.last_rank_at = _now_text()
        now = datetime.now()
        for item in ranked:
            self._last_score_at_by_theme[item.theme_id] = now
        self._maybe_persist_and_publish(now)
        return ranked

    def get_latest_rank(self, top_n: int = 20):
        if self._latest_rank:
            return self._latest_rank[: int(top_n)]
        return self.repository.get_latest_theme_rank(top_n)

    def get_stock_theme_state(self, stock_code: str):
        return self.context_provider.get_stock_theme_state(stock_code)

    def health(self) -> dict:
        latest_sync = self.repository.latest_source_sync_run()
        active_theme_count = len([theme for theme in self.repository.list_canonical_themes() if str(theme.status.value if hasattr(theme.status, "value") else theme.status) == "ACTIVE"])
        active_stocks = self.universe_builder.build_active_universe()
        data_ready = bool(active_stocks and self.last_tick_at and self.get_latest_rank(1))
        return {
            "running": self.running,
            "last_sync_at": latest_sync.finished_at if latest_sync else "",
            "last_tick_at": self.last_tick_at,
            "active_theme_count": active_theme_count,
            "active_stock_count": len(active_stocks),
            "latest_rank_count": len(self.get_latest_rank(500)),
            "ws_client_count": self.broadcaster.client_count,
            "error_count": self.error_count + self.broadcaster.error_count,
            "data_ready": data_ready,
        }

    def _merge_rank(self, changed: list[ThemeActivitySnapshot]) -> None:
        by_theme = {item.theme_id: item for item in self._latest_rank}
        for item in changed:
            by_theme[item.theme_id] = item
        self._latest_rank = self.scorer.ranker.rank(list(by_theme.values()))

    def _maybe_persist_and_publish(self, now: datetime) -> None:
        if now - self._last_db_save_at >= self.db_snapshot_interval:
            for item in self._latest_rank:
                self.repository.save_activity_snapshot(item)
            self._last_db_save_at = now
        if now - self._last_ws_push_at >= self.ws_push_interval:
            self.broadcaster.publish(build_theme_rank_payload(self.get_latest_rank(20), top_n=20))
            self.broadcaster.publish(build_runtime_health_payload(self.health()))
            self._last_ws_push_at = now


def _now_text() -> str:
    return datetime.now().replace(microsecond=0).isoformat()
