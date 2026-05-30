from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from trading.theme_engine.models import ThemeActivitySnapshot


@dataclass
class RankHistory:
    maxlen: int = 30
    snapshots: deque[dict[str, int]] = field(default_factory=lambda: deque(maxlen=30))

    def push(self, ranked: list[ThemeActivitySnapshot]) -> None:
        if self.snapshots.maxlen != self.maxlen:
            self.snapshots = deque(self.snapshots, maxlen=self.maxlen)
        self.snapshots.append({item.theme_id: item.rank for item in ranked})

    def rank_delta(self, theme_id: str, lookback: int = 1, current_rank: int = 0) -> int:
        if not self.snapshots or lookback <= 0:
            return 0
        index = max(0, len(self.snapshots) - 1 - lookback)
        previous = self.snapshots[index].get(theme_id)
        if previous is None or current_rank <= 0:
            return 0
        return previous - current_rank


class ThemeRanker:
    def __init__(self, history: RankHistory | None = None) -> None:
        self.history = history or RankHistory()

    def rank(self, snapshots: list[ThemeActivitySnapshot], top_n: int | None = None) -> list[ThemeActivitySnapshot]:
        ranked = sorted(snapshots, key=lambda item: item.theme_score, reverse=True)
        if top_n is not None:
            ranked = ranked[:top_n]
        for index, item in enumerate(ranked, start=1):
            item.rank = index
            item.rank_delta_1m = self.history.rank_delta(item.theme_id, 1, index)
            item.rank_delta_5m = self.history.rank_delta(item.theme_id, 5, index)
        self.history.push(ranked)
        return ranked
