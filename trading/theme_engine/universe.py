from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from trading.theme_engine.models import ThemeMembership, ThemeStatus
from trading.theme_engine.normalizer import normalize_stock_code
from trading.theme_engine.repository import ThemeEngineRepository


@dataclass
class ThemeUniverseConfig:
    max_size: int = 500
    min_membership_score: float = 0.55
    min_trade_membership_score: float = 0.65


@dataclass(frozen=True)
class ThemeUniverseSnapshot:
    theme_id: str
    theme_name: str = ""
    member_count: int = 0
    tradable_member_count: int = 0
    kospi_member_count: int = 0
    kosdaq_member_count: int = 0
    membership_quality: float = 0.0
    reason_codes: tuple[str, ...] = ()


class ThemeRegistry:
    def __init__(self, repository: ThemeEngineRepository | None = None, config: ThemeUniverseConfig | None = None) -> None:
        self.repository = repository
        self.config = config or ThemeUniverseConfig()

    def build(
        self,
        theme_inputs: Iterable[tuple[str, str, list[ThemeMembership]]] | None = None,
    ) -> list[ThemeUniverseSnapshot]:
        groups = self._groups_from_inputs(theme_inputs) if theme_inputs is not None else self._groups_from_repository()
        snapshots = [self._snapshot(theme_id, theme_name, memberships) for theme_id, theme_name, memberships in groups]
        return sorted(snapshots, key=lambda item: (item.membership_quality, item.tradable_member_count, item.member_count), reverse=True)

    def _groups_from_repository(self) -> list[tuple[str, str, list[ThemeMembership]]]:
        if self.repository is None:
            return []
        grouped: dict[str, list[ThemeMembership]] = defaultdict(list)
        for membership in self.repository.list_current_memberships(active=True):
            grouped[membership.theme_id].append(membership)
        groups: list[tuple[str, str, list[ThemeMembership]]] = []
        for theme_id, memberships in grouped.items():
            theme = self.repository.get_canonical_theme(theme_id)
            theme_name = (
                (theme.display_name or theme.canonical_name or theme.theme_id)
                if theme is not None
                else theme_id
            )
            groups.append((theme_id, theme_name, memberships))
        return groups

    def _groups_from_inputs(
        self,
        theme_inputs: Iterable[tuple[str, str, list[ThemeMembership]]],
    ) -> list[tuple[str, str, list[ThemeMembership]]]:
        return [(theme_id, theme_name, list(memberships or [])) for theme_id, theme_name, memberships in theme_inputs]

    def _snapshot(
        self,
        theme_id: str,
        theme_name: str,
        memberships: list[ThemeMembership],
    ) -> ThemeUniverseSnapshot:
        active = [item for item in memberships if item.active]
        tradable = [
            item
            for item in active
            if item.trade_eligible and _normalized_membership_score(item.membership_score) >= self.config.min_trade_membership_score
        ]
        reasons: list[str] = []
        if not active:
            reasons.append("UNIVERSE_EMPTY")
        if not tradable:
            reasons.append("NO_TRADABLE_MEMBERS")
        quality = _membership_quality(active)
        if quality < self.config.min_membership_score:
            reasons.append("LOW_MEMBERSHIP_QUALITY")
        return ThemeUniverseSnapshot(
            theme_id=theme_id,
            theme_name=theme_name or theme_id,
            member_count=len(active),
            tradable_member_count=len(tradable),
            kospi_member_count=sum(1 for item in active if _member_market(item) == "KOSPI"),
            kosdaq_member_count=sum(1 for item in active if _member_market(item) == "KOSDAQ"),
            membership_quality=round(quality, 4),
            reason_codes=tuple(dict.fromkeys(reasons)),
        )


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


def _membership_quality(memberships: list[ThemeMembership]) -> float:
    if not memberships:
        return 0.0
    return sum(_normalized_membership_score(item.membership_score) for item in memberships) / len(memberships)


def _normalized_membership_score(value: float) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if number > 1.0:
        return max(0.0, min(1.0, number / 100.0))
    return max(0.0, min(1.0, number))


def _member_market(member: ThemeMembership) -> str:
    raw = getattr(member, "market", "")
    if not raw:
        raw_payload = getattr(member, "raw", None)
        if isinstance(raw_payload, dict):
            raw = raw_payload.get("market", "")
    value = str(raw or "").strip().upper()
    if value in {"KOSPI", "KS", "P"}:
        return "KOSPI"
    if value in {"KOSDAQ", "KQ", "Q"}:
        return "KOSDAQ"
    return ""


__all__ = [
    "ThemeRegistry",
    "ThemeUniverseBuilder",
    "ThemeUniverseConfig",
    "ThemeUniverseSnapshot",
]
