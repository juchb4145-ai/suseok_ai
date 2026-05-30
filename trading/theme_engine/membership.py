from __future__ import annotations

from datetime import datetime, timezone
from math import exp

from trading.theme_engine.models import RelationType, ThemeMembership, ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository


RELATION_WEIGHTS = {
    RelationType.INVESTOR.value: 0.90,
    RelationType.PARTNER.value: 0.85,
    RelationType.SUPPLIER.value: 0.80,
    RelationType.CUSTOMER.value: 0.75,
    RelationType.POLICY_BENEFIT.value: 0.70,
    RelationType.SAME_INDUSTRY.value: 0.55,
    RelationType.NEWS_MENTIONED.value: 0.50,
    RelationType.RUMOR.value: 0.20,
    RelationType.UNKNOWN.value: 0.10,
}


class ThemeMembershipBuilder:
    def __init__(self, repository: ThemeEngineRepository) -> None:
        self.repository = repository

    def build_current_membership(self, theme_id: str) -> list[ThemeMembership]:
        evidences = self.repository.list_member_evidence(theme_id)
        by_stock: dict[str, list] = {}
        for evidence in evidences:
            by_stock.setdefault(evidence.stock_code, []).append(evidence)
        saved = []
        for stock_code, rows in by_stock.items():
            score = self.calculate_membership_score(theme_id, stock_code)
            relation = _best_relation(rows)
            sources = {row.source for row in rows if row.source}
            only_weak = all(str(row.relation_type.value if hasattr(row.relation_type, "value") else row.relation_type) in {RelationType.RUMOR.value, RelationType.UNKNOWN.value} for row in rows)
            membership = ThemeMembership(
                theme_id=theme_id,
                stock_code=stock_code,
                stock_name=next((row.stock_name for row in rows if row.stock_name), ""),
                membership_score=score,
                relation_type=relation,
                source_count=len(sources),
                active=score >= 0.55,
                trade_eligible=score >= 0.65 and not only_weak,
            )
            saved.append(self.repository.upsert_current_membership(membership))
        theme = self.repository.get_canonical_theme(theme_id)
        if theme is not None and saved:
            active_count = sum(1 for item in saved if item.active)
            eligible_count = sum(1 for item in saved if item.trade_eligible)
            theme.status = ThemeStatus.ACTIVE if active_count >= 3 and eligible_count >= 2 else ThemeStatus.WATCH
            theme.trade_eligible = eligible_count > 0
            theme.confidence = max(theme.confidence, min(1.0, active_count / max(1, len(saved))))
            self.repository.upsert_canonical_theme(theme)
        return saved

    def build_all_current_memberships(self) -> list[ThemeMembership]:
        theme_ids = sorted({evidence.theme_id for evidence in self.repository.list_all_member_evidence()})
        result = []
        for theme_id in theme_ids:
            result.extend(self.build_current_membership(theme_id))
        return result

    def calculate_membership_score(self, theme_id: str, stock_code: str) -> float:
        rows = self.repository.list_member_evidence(theme_id, stock_code)
        if not rows:
            return 0.0
        sources = {row.source for row in rows if row.source}
        avg_confidence = sum(max(0.0, min(1.0, float(row.confidence))) for row in rows) / len(rows)
        relation_weight = max(RELATION_WEIGHTS.get(_relation_value(row.relation_type), 0.1) for row in rows)
        source_bonus = min(1.0, len(sources) / 3.0)
        freshness = max(_freshness_score(row.last_seen_at) for row in rows)
        score = (0.45 * avg_confidence) + (0.35 * relation_weight) + (0.15 * source_bonus) + (0.05 * freshness)
        return round(max(0.0, min(1.0, score)), 4)


def _best_relation(rows) -> RelationType:
    best = max(rows, key=lambda row: RELATION_WEIGHTS.get(_relation_value(row.relation_type), 0.1))
    return RelationType(_relation_value(best.relation_type))


def _relation_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value or RelationType.UNKNOWN.value)


def _freshness_score(value: str) -> float:
    if not value:
        return 1.0
    try:
        seen = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.8
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    days = max(0.0, (datetime.now(timezone.utc) - seen.astimezone(timezone.utc)).total_seconds() / 86400.0)
    return max(0.0, min(1.0, exp(-days / 14.0)))
