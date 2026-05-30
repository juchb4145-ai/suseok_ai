from __future__ import annotations

from dataclasses import replace
from typing import Optional

from trading.strategy.candidates import add_unique
from trading.strategy.models import Candidate
from trading.theme_engine.models import StockThemeState, ThemeActivitySnapshot, ThemeContext, ThemeMembership, ThemeStatus
from trading.theme_engine.normalizer import normalize_stock_code
from trading.theme_engine.repository import ThemeEngineRepository


class DynamicThemeContextProvider:
    def __init__(self, repository: ThemeEngineRepository) -> None:
        self.repository = repository

    def enrich_candidate(self, candidate: Candidate) -> Candidate:
        enriched = replace(candidate)
        enriched.code = normalize_stock_code(candidate.code)
        enriched.theme_ids = list(candidate.theme_ids)
        enriched.metadata = dict(candidate.metadata or {})
        contexts = self.themes_for_code(enriched.code)
        if not self.is_ready():
            enriched.metadata["theme_context_status"] = "not_ready"
            enriched.metadata["reason_code"] = "THEME_CONTEXT_NOT_READY"
            return enriched
        if not contexts:
            enriched.metadata["theme_context_status"] = "no_active_theme"
            enriched.metadata["reason_code"] = "NO_ACTIVE_THEME"
            return enriched
        details = []
        for context in contexts:
            add_unique(enriched.theme_ids, context.theme_id)
            details.append(
                {
                    "theme_id": context.theme_id,
                    "theme_name": context.theme_name,
                    "status": _value(context.status),
                    "membership_score": context.membership_score,
                    "relation_type": _value(context.relation_type),
                    "trade_eligible": context.trade_eligible,
                    "rank": context.rank,
                }
            )
        if not enriched.name:
            first_name = next((context.membership.stock_name for context in contexts if context.membership and context.membership.stock_name), "")
            enriched.name = first_name
        enriched.metadata["theme_context_status"] = "ready"
        enriched.metadata["dynamic_theme_context"] = details
        enriched.metadata.pop("reason_code", None)
        return enriched

    def themes_for_code(self, stock_code: str) -> list[ThemeContext]:
        memberships = self.repository.get_themes_by_stock(normalize_stock_code(stock_code), active=True)
        contexts = []
        for membership in memberships:
            theme = self.repository.get_canonical_theme(membership.theme_id)
            if theme is None or theme.status not in {ThemeStatus.WATCH, ThemeStatus.ACTIVE}:
                continue
            activity = self.get_theme_activity(membership.theme_id)
            rank = activity.rank if activity else 0
            contexts.append(
                ThemeContext(
                    theme_id=membership.theme_id,
                    theme_name=theme.display_name,
                    status=theme.status,
                    membership=membership,
                    activity=activity,
                    membership_score=membership.membership_score,
                    relation_type=membership.relation_type,
                    active=membership.active,
                    trade_eligible=membership.trade_eligible and theme.trade_eligible,
                    source_count=membership.source_count,
                    rank=rank,
                    leader_code=activity.leader_code if activity else "",
                    market="",
                    strategy_profile=None,
                    details={"theme_confidence": theme.confidence},
                )
            )
        return sorted(contexts, key=lambda item: (item.trade_eligible, item.membership_score, -(item.rank or 9999)), reverse=True)

    def members_for_theme(self, theme_id: str) -> list[ThemeMembership]:
        return self.repository.get_members_by_theme(theme_id, active=True)

    def get_theme_activity(self, theme_id: str) -> Optional[ThemeActivitySnapshot]:
        for snapshot in self.repository.latest_activity_snapshots(limit=500):
            if snapshot.theme_id == theme_id:
                return snapshot
        return None

    def get_stock_theme_state(self, stock_code: str) -> StockThemeState:
        clean_code = normalize_stock_code(stock_code)
        if not self.is_ready():
            return StockThemeState(stock_code=clean_code, reason_code="THEME_CONTEXT_NOT_READY", ready=False)
        contexts = self.themes_for_code(clean_code)
        if not contexts:
            return StockThemeState(stock_code=clean_code, reason_code="NO_ACTIVE_THEME", ready=True)
        primary = contexts[0]
        membership = primary.membership
        role = "leader" if primary.leader_code == clean_code else "member"
        return StockThemeState(
            stock_code=clean_code,
            stock_name=membership.stock_name if membership else "",
            themes=contexts,
            primary_theme_id=primary.theme_id,
            primary_theme_name=primary.theme_name,
            primary_rank=primary.rank,
            membership_score=primary.membership_score,
            leadership_role=role,
            ready=True,
        )

    def is_ready(self) -> bool:
        return self.repository.count_current_memberships() > 0


def _value(value) -> str:
    return value.value if hasattr(value, "value") else str(value or "")
