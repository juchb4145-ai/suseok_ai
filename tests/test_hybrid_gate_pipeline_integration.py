from copy import deepcopy

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.hybrid_validation import HybridValidationRepository
from trading.strategy.indicators import IndicatorCalculator
from trading.strategy.intraday import IntradayStateTracker
from trading.strategy.market_data import MarketDataStore
from trading.strategy.market_index import MarketIndexStore
from trading.strategy.models import BlockType, Candidate, GateDecision, IndicatorSnapshot
from trading.strategy.pipeline import GatePipeline
from trading.strategy.runtime_settings import LEGACY_DEFAULT_SETTINGS, StrategyRuntimeSettings
from trading.theme_engine.models import (
    RelationType,
    StockLeadershipResult,
    ThemeActivitySnapshot,
    ThemeContext,
    ThemeStatus,
    ThemeStrengthResult,
)


def test_pipeline_records_hybrid_decision_without_changing_legacy_result():
    pipeline = _pipeline()
    result = pipeline._evaluate_candidate_theme(
        Candidate(id=1, code="000001"),
        _context(),
        ThemeStrengthResult(theme_id="furiosa_ai", theme_name="퓨리오사AI", score=35, grade="C"),
        _leadership(),
        _MarketPass(),
        _ThemePullbackPass(),
        _StockPullbackPass(),
    )

    assert result.strategy_eligible is False
    assert result.final_grade == "C"
    assert result.details["hybrid_result"]["status"] == "READY"
    assert result.details["hybrid_status"] == "READY"
    assert result.details["hybrid_observe_only"] is True
    assert result.details["hybrid_live_applied"] is False
    assert not hasattr(pipeline, "theme_repository")


def test_pipeline_can_apply_hybrid_decision_when_observe_only_false():
    settings_json = deepcopy(LEGACY_DEFAULT_SETTINGS)
    settings_json["hybrid_gate"]["observe_only"] = False
    pipeline = _pipeline(settings=StrategyRuntimeSettings.from_settings_json(settings_json))
    result = pipeline._evaluate_candidate_theme(
        Candidate(id=1, code="000001"),
        _context(),
        ThemeStrengthResult(theme_id="furiosa_ai", theme_name="퓨리오사AI", score=35, grade="C"),
        _leadership(),
        _MarketPass(),
        _ThemePullbackPass(),
        _StockPullbackPass(),
    )

    assert result.strategy_eligible is True
    assert result.final_grade == "A"
    assert result.details["hybrid_live_applied"] is True
    assert result.details["hybrid_status"] == "READY"


def test_pipeline_saves_hybrid_validation_event_when_repository_is_configured(tmp_path):
    db = TradingDatabase(str(tmp_path / "hybrid_events.sqlite3"))
    pipeline = _pipeline(hybrid_validation_repository=HybridValidationRepository(db))
    result = pipeline._evaluate_candidate_theme(
        Candidate(id=1, trade_date="2026-05-30", code="000001", name="MOCK-LEADER"),
        _context(),
        ThemeStrengthResult(theme_id="furiosa_ai", theme_name="퓨리오사AI", score=35, grade="C"),
        _leadership(),
        _MarketPass(),
        _ThemePullbackPass(),
        _StockPullbackPass(),
    )

    events = HybridValidationRepository(db).list_events(trade_date="2026-05-30")
    assert result.details["hybrid_validation_event_saved"] is True
    assert len(events) == 1
    assert events[0].hybrid_status == "READY"
    assert events[0].theme_id == "furiosa_ai"
    db.close()


def test_pipeline_apply_mode_can_block_with_hybrid_decision():
    settings_json = deepcopy(LEGACY_DEFAULT_SETTINGS)
    settings_json["hybrid_gate"]["observe_only"] = False
    pipeline = _pipeline(settings=StrategyRuntimeSettings.from_settings_json(settings_json))
    result = pipeline._evaluate_candidate_theme(
        Candidate(id=1, code="000001"),
        _context(membership_score=0.2),
        ThemeStrengthResult(theme_id="furiosa_ai", theme_name="퓨리오사AI", score=90, grade="A"),
        _leadership(),
        _MarketPass(),
        _ThemePullbackPass(),
        _StockPullbackPass(),
    )

    assert result.strategy_eligible is False
    assert result.block_type == BlockType.FINAL
    assert result.details["hybrid_status"] == "BLOCKED"
    assert "LOW_MEMBERSHIP_SCORE" in result.details["hybrid_reason_codes"]


def _pipeline(settings=None, hybrid_validation_repository=None):
    market_data = MarketDataStore()
    candle_builder = CandleBuilder()
    return GatePipeline(
        theme_context_provider=object(),
        market_data=market_data,
        candle_builder=candle_builder,
        indicator_calculator=IndicatorCalculator(market_data, candle_builder),
        intraday_tracker=IntradayStateTracker(),
        market_index_store=MarketIndexStore(),
        settings=settings,
        hybrid_validation_repository=hybrid_validation_repository,
    )


def _context(membership_score=0.9):
    return ThemeContext(
        theme_id="furiosa_ai",
        theme_name="퓨리오사AI",
        status=ThemeStatus.ACTIVE,
        activity=ThemeActivitySnapshot(
            theme_id="furiosa_ai",
            theme_name="퓨리오사AI",
            theme_score=92,
            status=ThemeStatus.ACTIVE,
            trade_eligible=True,
            rank=1,
            breadth=0.7,
            rising_count=4,
            total_count=5,
            leader_gap=1.2,
            top3_concentration=0.45,
            details={"reason_codes": []},
        ),
        membership_score=membership_score,
        relation_type=RelationType.INVESTOR,
        trade_eligible=True,
        source_count=2,
        rank=1,
        rank_in_theme=1,
    )


def _leadership():
    return StockLeadershipResult(
        candidate_id=1,
        code="000001",
        theme_id="furiosa_ai",
        theme_name="퓨리오사AI",
        score=96,
        leadership_rank=1,
        leadership_role="leader",
        details={"comparison_reason_codes": []},
    )


class _MarketPass:
    def evaluate(self, candidate, mapping):
        return GateDecision(
            gate_name="MarketIndexGate",
            passed=True,
            score=100,
            block_type=BlockType.NONE,
            details={"position_vs_mid": "ABOVE_MID"},
        )


class _ThemePullbackPass:
    def evaluate(self, theme_result):
        return GateDecision(
            gate_name="ThemePullbackGate",
            passed=True,
            score=100,
            block_type=BlockType.NONE,
            details={"sub_status": "PASS"},
        )


class _StockPullbackPass:
    def evaluate(self, candidate, theme_result, leadership_result, market_decision):
        snapshot = IndicatorSnapshot(candidate_id=candidate.id, code=candidate.code, price=1000, created_at="2026-05-30T09:10:00")
        return (
            GateDecision(
                gate_name="StockPullbackEntryGate",
                passed=True,
                score=100,
                block_type=BlockType.NONE,
                details={
                    "sub_status": "PASS",
                    "support_touched": True,
                    "support_reclaimed": True,
                    "volume_reaccel": True,
                    "failed_low_break_rebound": False,
                    "chase_risk": False,
                    "late_chase_level": "none",
                },
            ),
            snapshot,
        )
