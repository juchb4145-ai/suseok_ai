from __future__ import annotations

from dataclasses import dataclass

from trading.theme_engine.models import ThemeMembership, ThemeStatus
from trading.theme_engine.normalizer import normalize_stock_code
from trading.theme_engine.repository import ThemeEngineRepository


@dataclass
class ThemeUniverseConfig:
    max_size: int = 500
    min_membership_score: float = 0.55
    min_trade_membership_score: float = 0.65


class ThemeUniverseBuilder:
    def __init__(self, repository: ThemeEngineRepository, config: ThemeUniverseConfig | None = None) -> None:
        self.repository = repository
        self.config = config or ThemeUniverseConfig()

    def build_active_universe(self) -> list[str]:
        memberships = [
            item
            for item in self.repository.list_current_memberships(active=True)
            if item.membership_score >= self.config.min_membership_score
            and self._theme_is_watch_or_active(item.theme_id)
        ]
        return self._limit_codes(memberships)

    def build_trade_eligible_universe(self) -> list[str]:
        memberships = [
            item
            for item in self.repository.list_current_memberships(active=True, trade_eligible=True)
            if item.membership_score >= self.config.min_trade_membership_score
            and self._theme_is_watch_or_active(item.theme_id)
        ]
        return self._limit_codes(memberships)

    def themes_by_stock(self, stock_code: str) -> list[ThemeMembership]:
        code = normalize_stock_code(stock_code)
        return [
            item
            for item in self.repository.get_themes_by_stock(code, active=True)
            if item.membership_score >= self.config.min_membership_score
            and self._theme_is_watch_or_active(item.theme_id)
        ]

    def stocks_by_theme(self, theme_id: str) -> list[ThemeMembership]:
        return [
            item
            for item in self.repository.get_members_by_theme(theme_id, active=True)
            if item.membership_score >= self.config.min_membership_score
            and self._theme_is_watch_or_active(item.theme_id)
        ]

    def _theme_is_watch_or_active(self, theme_id: str) -> bool:
        theme = self.repository.get_canonical_theme(theme_id)
        return bool(theme and theme.status in {ThemeStatus.WATCH, ThemeStatus.ACTIVE})

    def _limit_codes(self, memberships: list[ThemeMembership]) -> list[str]:
        latest_rank = {item.theme_id: item for item in self.repository.get_latest_theme_rank(500)}
        ordered = sorted(
            memberships,
            key=lambda item: (
                _status_priority(self.repository.get_canonical_theme(item.theme_id).status if self.repository.get_canonical_theme(item.theme_id) else ""),
                item.trade_eligible,
                item.membership_score,
                item.source_count,
                latest_rank.get(item.theme_id).theme_score if item.theme_id in latest_rank else 0.0,
            ),
            reverse=True,
        )
        codes = []
        seen = set()
        for item in ordered:
            if item.stock_code in seen:
                continue
            seen.add(item.stock_code)
            codes.append(item.stock_code)
            if len(codes) >= self.config.max_size:
                break
        return codes


def _status_priority(status) -> int:
    value = status.value if hasattr(status, "value") else str(status or "")
    if value == ThemeStatus.ACTIVE.value:
        return 2
    if value == ThemeStatus.WATCH.value:
        return 1
    return 0
