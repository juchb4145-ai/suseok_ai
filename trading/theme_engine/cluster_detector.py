from __future__ import annotations

from datetime import datetime
from hashlib import sha1

from trading.theme_engine.models import CanonicalTheme, DynamicThemeCluster, StockSnapshot, ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository


class DynamicThemeClusterDetector:
    def __init__(self, repository: ThemeEngineRepository) -> None:
        self.repository = repository

    def detect(
        self,
        snapshots: list[StockSnapshot],
        *,
        keywords: list[str] | None = None,
        now: datetime | None = None,
    ) -> list[DynamicThemeCluster]:
        rising = [
            snapshot
            for snapshot in snapshots
            if snapshot.momentum_5m > 0
            and snapshot.change_rate >= 1.5
            and snapshot.turnover_strength >= 2.0
        ]
        if len(rising) < 3:
            return []
        avg_turnover_strength = sum(item.turnover_strength for item in rising) / len(rising)
        avg_change = sum(item.change_rate for item in rising) / len(rising)
        if avg_turnover_strength < 2.0 or avg_change < 1.5:
            return []
        codes = sorted(item.stock_code for item in rising)
        matched_theme_id = self._match_existing_theme(codes)
        if not matched_theme_id:
            stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M")
            digest = sha1(",".join(codes).encode("utf-8")).hexdigest()[:6]
            matched_theme_id = f"dynamic_{stamp}_{digest}"
            self.repository.upsert_canonical_theme(
                CanonicalTheme(
                    theme_id=matched_theme_id,
                    canonical_name=f"Dynamic cluster {stamp}",
                    display_name=f"Dynamic {stamp}",
                    status=ThemeStatus.WATCH,
                    confidence=0.55,
                    trade_eligible=False,
                )
            )
        cluster_id = f"cluster_{sha1(('|'.join(codes) + matched_theme_id).encode('utf-8')).hexdigest()[:12]}"
        score = min(100.0, (avg_change * 12.0) + (avg_turnover_strength * 12.0) + len(rising) * 5.0)
        status = ThemeStatus.ACTIVE if score >= 80 else ThemeStatus.WATCH
        cluster = DynamicThemeCluster(
            cluster_id=cluster_id,
            matched_theme_id=matched_theme_id,
            status=status,
            stock_codes=codes,
            keywords=list(keywords or []),
            score=round(score, 4),
            reason="intraday_co_movement",
        )
        return [self.repository.save_dynamic_cluster(cluster)]

    def _match_existing_theme(self, stock_codes: list[str]) -> str:
        scores: dict[str, int] = {}
        for code in stock_codes:
            for membership in self.repository.get_themes_by_stock(code, active=True):
                scores[membership.theme_id] = scores.get(membership.theme_id, 0) + 1
        if not scores:
            return ""
        theme_id, overlap = max(scores.items(), key=lambda item: item[1])
        return theme_id if overlap >= 2 else ""
