from __future__ import annotations

from trading.strategy.hybrid_gate import (
    HybridDynamicThemeGate,
    HybridGateConfig,
    HybridGateStatus,
    HybridPositionTier,
)
from trading.strategy.models import BlockType, Candidate, GateDecision
from trading.theme_engine.models import (
    RelationType,
    StockLeadershipResult,
    ThemeActivitySnapshot,
    ThemeContext,
    ThemeStatus,
    ThemeStrengthResult,
)


def test_active_theme_good_pullback_is_ready():
    decision = _evaluate()

    assert decision.status == HybridGateStatus.READY
    assert decision.position_tier == HybridPositionTier.NORMAL_FIRST_ENTRY
    assert decision.primary_reason == "STRONG_ACTIVE_THEME"


def test_active_theme_with_chase_risk_waits():
    decision = _evaluate(stock_pullback=_stock_pullback(score=0, chase=True, block_type=BlockType.FINAL, reason_codes=["CHASE_RISK"]))

    assert decision.status == HybridGateStatus.WAIT
    assert "CHASE_RISK" in decision.reason_codes


def test_watch_theme_leader_good_pullback_observes_by_default():
    decision = _evaluate(context=_context(status=ThemeStatus.WATCH))

    assert decision.status == HybridGateStatus.OBSERVE
    assert decision.position_tier == HybridPositionTier.OBSERVE_ONLY
    assert "WATCH_THEME_OBSERVE_ONLY" in decision.reason_codes


def test_watch_theme_can_emit_small_first_entry_when_enabled():
    config = HybridGateConfig(watch_theme_allows_small_entry=True)
    decision = _evaluate(config=config, context=_context(status=ThemeStatus.WATCH))

    assert decision.status == HybridGateStatus.READY
    assert decision.position_tier == HybridPositionTier.SMALL_FIRST_ENTRY


def test_candidate_theme_is_observe_only():
    decision = _evaluate(context=_context(status=ThemeStatus.CANDIDATE))

    assert decision.status == HybridGateStatus.OBSERVE
    assert "CANDIDATE_THEME_OBSERVE_ONLY" in decision.reason_codes


def test_stale_theme_is_blocked():
    decision = _evaluate(context=_context(status=ThemeStatus.STALE))

    assert decision.status == HybridGateStatus.BLOCKED
    assert "THEME_STALE" in decision.reason_codes


def test_leader_only_theme_blocks_laggard():
    decision = _evaluate(
        context=_context(rank_in_theme=6, activity=_activity(reason_codes=["LEADER_ONLY_THEME"])),
        leadership=_leadership(role="late_laggard", rank=6, score=45),
    )

    assert decision.status == HybridGateStatus.BLOCKED
    assert "LEADER_ONLY_THEME_LAGGARD_BLOCK" in decision.reason_codes


def test_strong_entry_with_weak_theme_waits():
    decision = _evaluate(context=_context(activity=_activity(score=45)))

    assert decision.status == HybridGateStatus.WAIT
    assert "WEAK_THEME_STRONG_ENTRY_WAIT" in decision.reason_codes


def test_strong_theme_with_bad_entry_waits():
    decision = _evaluate(stock_pullback=_stock_pullback(score=45, passed=False, reason_codes=["WAIT_PULLBACK_CONFIRMATION"]))

    assert decision.status == HybridGateStatus.WAIT
    assert "STRONG_THEME_ENTRY_NOT_READY" in decision.reason_codes


def test_low_membership_blocks():
    decision = _evaluate(context=_context(membership_score=0.3))

    assert decision.status == HybridGateStatus.BLOCKED
    assert "LOW_MEMBERSHIP_SCORE" in decision.reason_codes


def test_hard_guard_violation_blocks():
    decision = _evaluate(market=_market(block_type=BlockType.FINAL, reason_codes=["MARKET_CRASH_RISK"], score=0))

    assert decision.status == HybridGateStatus.BLOCKED
    assert "MARKET_CRASH_RISK" in decision.reason_codes


def _evaluate(
    *,
    config: HybridGateConfig | None = None,
    context: ThemeContext | None = None,
    leadership: StockLeadershipResult | None = None,
    market: GateDecision | None = None,
    stock_pullback: GateDecision | None = None,
):
    gate = HybridDynamicThemeGate(config)
    return gate.evaluate(
        candidate=Candidate(id=1, code="000001"),
        theme_context=context or _context(),
        theme_result=ThemeStrengthResult(theme_id="furiosa_ai", theme_name="퓨리오사AI", score=88, grade="A"),
        leadership_result=leadership or _leadership(),
        market_decision=market or _market(),
        theme_strength_decision=GateDecision(gate_name="ThemeStrengthGate", passed=True, score=88),
        theme_pullback_decision=GateDecision(gate_name="ThemePullbackGate", passed=True, score=90),
        leadership_decision=GateDecision(gate_name="StockLeadershipGate", passed=True, score=95),
        stock_pullback_decision=stock_pullback or _stock_pullback(),
    )


def _context(
    *,
    status=ThemeStatus.ACTIVE,
    membership_score=0.9,
    rank_in_theme=1,
    activity: ThemeActivitySnapshot | None = None,
):
    return ThemeContext(
        theme_id="furiosa_ai",
        theme_name="퓨리오사AI",
        status=status,
        activity=activity or _activity(),
        membership_score=membership_score,
        relation_type=RelationType.INVESTOR,
        trade_eligible=True,
        source_count=2,
        rank=1,
        rank_in_theme=rank_in_theme,
    )


def _activity(score=88, breadth=0.7, reason_codes=None):
    return ThemeActivitySnapshot(
        theme_id="furiosa_ai",
        theme_name="퓨리오사AI",
        theme_score=score,
        status=ThemeStatus.ACTIVE,
        trade_eligible=True,
        rank=1,
        breadth=breadth,
        rising_count=4,
        total_count=5,
        leader_gap=1.2,
        top3_concentration=0.45,
        details={"reason_codes": list(reason_codes or [])},
    )


def _leadership(role="leader", rank=1, score=95):
    return StockLeadershipResult(
        candidate_id=1,
        code="000001",
        theme_id="furiosa_ai",
        theme_name="퓨리오사AI",
        score=score,
        leadership_rank=rank,
        leadership_role=role,
        details={"comparison_reason_codes": []},
    )


def _market(*, block_type=BlockType.NONE, reason_codes=None, score=100):
    return GateDecision(
        gate_name="MarketIndexGate",
        passed=block_type == BlockType.NONE,
        score=score,
        block_type=block_type,
        reason_codes=list(reason_codes or []),
        details={"position_vs_mid": "ABOVE_MID"},
    )


def _stock_pullback(*, score=100, chase=False, passed=True, block_type=BlockType.NONE, reason_codes=None):
    return GateDecision(
        gate_name="StockPullbackEntryGate",
        passed=passed,
        score=score,
        block_type=block_type,
        reason_codes=list(reason_codes or []),
        details={
            "sub_status": "PASS" if passed else "WAIT_PULLBACK_CONFIRMATION",
            "support_touched": passed,
            "support_reclaimed": passed,
            "volume_reaccel": passed,
            "failed_low_break_rebound": False,
            "chase_risk": chase,
            "late_chase_level": "soft_block" if chase else "none",
        },
    )
