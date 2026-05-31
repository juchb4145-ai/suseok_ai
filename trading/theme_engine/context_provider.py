from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Optional

from trading.theme_engine.models import StockThemeState, ThemeActivitySnapshot, ThemeContext, ThemeMembership, ThemeStatus
from trading.theme_engine.normalizer import normalize_stock_code
from trading.theme_engine.repository import ThemeEngineRepository

if TYPE_CHECKING:
    from trading.strategy.models import Candidate


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
            enriched.metadata.update(_dry_run_payload("not_ready", reason="THEME_CONTEXT_NOT_READY"))
            return enriched
        if not contexts:
            enriched.metadata["theme_context_status"] = "no_active_theme"
            enriched.metadata["reason_code"] = "NO_ACTIVE_THEME"
            enriched.metadata.update(_dry_run_payload("blocked", reason="NO_ACTIVE_THEME"))
            return enriched
        details = []
        for context in contexts:
            _add_unique(enriched.theme_ids, context.theme_id)
            details.append(
                {
                    "theme_id": context.theme_id,
                    "theme_name": context.theme_name,
                    "status": _value(context.status),
                    "membership_score": context.membership_score,
                    "relation_type": _value(context.relation_type),
                    "trade_eligible": context.trade_eligible,
                    "rank": context.rank,
                    "rank_in_theme": context.rank_in_theme,
                }
            )
        if not enriched.name:
            first_name = next((context.membership.stock_name for context in contexts if context.membership and context.membership.stock_name), "")
            enriched.name = first_name
        enriched.metadata["theme_context_status"] = "ready"
        enriched.metadata["dynamic_theme_context"] = details
        primary = contexts[0]
        activity = primary.activity
        reason_codes = list(activity.details.get("reason_codes") or []) if activity else []
        dry_run_status = "ready" if primary.trade_eligible else "wait"
        if "LEADER_ONLY_THEME" in reason_codes or "LOW_BREADTH" in reason_codes:
            dry_run_status = "wait"
        enriched.metadata.update(
            _dry_run_payload(
                dry_run_status,
                active_theme_id=primary.theme_id,
                active_theme_name=primary.theme_name,
                active_theme_score=activity.theme_score if activity else 0.0,
                active_theme_rank=primary.rank,
                stock_rank_in_theme=primary.rank_in_theme,
                stock_membership_score=primary.membership_score,
                theme_reason_codes=reason_codes,
                reason="STRONG_ACTIVE_THEME" if dry_run_status == "ready" else (reason_codes[0] if reason_codes else "LOW_MEMBERSHIP_SCORE"),
            )
        )
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
            rank_in_theme = self._rank_in_theme(membership.theme_id, membership.stock_code, activity)
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
                    rank_in_theme=rank_in_theme,
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

    def _rank_in_theme(self, theme_id: str, stock_code: str, activity: ThemeActivitySnapshot | None = None) -> int:
        top_stocks = list((activity.details if activity else {}).get("top_stocks") or [])
        for item in top_stocks:
            if normalize_stock_code(str(item.get("stock_code") or "")) == normalize_stock_code(stock_code):
                return int(item.get("rank") or 0)
        members = self.repository.get_members_by_theme(theme_id, active=True)
        ranked = sorted(members, key=lambda item: (item.trade_eligible, item.membership_score, item.source_count), reverse=True)
        for index, member in enumerate(ranked, start=1):
            if member.stock_code == stock_code:
                return index
        return 0


def _value(value) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _add_unique(items: list, value) -> None:
    if value not in items:
        items.append(value)


def _dry_run_payload(
    status: str,
    *,
    active_theme_id: str = "",
    active_theme_name: str = "",
    active_theme_score: float = 0.0,
    active_theme_rank: int = 0,
    stock_rank_in_theme: int = 0,
    stock_membership_score: float = 0.0,
    theme_reason_codes: list[str] | None = None,
    reason: str = "",
) -> dict:
    return {
        "dynamic_theme_status": status,
        "active_theme_id": active_theme_id,
        "active_theme_name": active_theme_name,
        "active_theme_score": active_theme_score,
        "active_theme_rank": active_theme_rank,
        "stock_rank_in_theme": stock_rank_in_theme,
        "stock_membership_score": stock_membership_score,
        "theme_reason_codes": list(theme_reason_codes or []),
        "theme_gate_dry_run_status": status,
        "theme_gate_dry_run_reason": reason,
    }
