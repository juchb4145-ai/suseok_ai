from storage.db import TradingDatabase
from trading.strategy.hybrid_gate import HybridDynamicThemeGate
from trading.strategy.hybrid_validation import HybridValidationRepository, build_validation_event
from trading.strategy.models import Candidate, CandidateSourceType, GateDecision
from trading.theme_engine.models import (
    RelationType,
    StockLeadershipResult,
    ThemeActivitySnapshot,
    ThemeContext,
    ThemeStatus,
    ThemeStrengthResult,
)


def test_hybrid_validation_event_can_be_built_without_legacy_result():
    decision = _decision()
    event = build_validation_event(
        candidate=Candidate(
            id=1,
            trade_date="2026-05-30",
            code="000001",
            name="MOCK-LEADER",
            sources=[CandidateSourceType.CONDITION],
        ),
        decision=decision,
        ts="2026-05-30T09:10:00",
    )

    assert event.stock_code == "000001"
    assert event.hybrid_status == "READY"
    assert event.theme_score == 88
    assert event.membership_score == 0.9
    assert event.details_json["hybrid_result"]["status"] == "READY"
    assert "legacy_result" in event.details_json


def test_hybrid_validation_event_saves_reason_codes_json(tmp_path):
    db = TradingDatabase(str(tmp_path / "hybrid.sqlite3"))
    repo = HybridValidationRepository(db)
    event = build_validation_event(
        candidate=Candidate(id=1, trade_date="2026-05-30", code="000001"),
        decision=_decision(),
        ts="2026-05-30T09:10:00",
    )

    saved = repo.save_event(event)
    loaded = repo.list_events(trade_date="2026-05-30")[0]

    assert saved.id is not None
    assert loaded.hybrid_reason_codes == ["STRONG_ACTIVE_THEME"]
    assert loaded.theme_score == 88
    assert loaded.membership_score == 0.9
    db.close()


def test_new_db_creates_hybrid_validation_table_without_theme_mappings(tmp_path):
    db = TradingDatabase(str(tmp_path / "schema.sqlite3"))
    names = {row["name"] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    assert "hybrid_gate_validation_events" in names
    assert "theme_mappings" not in names
    db.close()


def _decision():
    return HybridDynamicThemeGate().evaluate(
        candidate=Candidate(id=1, code="000001"),
        theme_context=ThemeContext(
            theme_id="furiosa_ai",
            theme_name="퓨리오사AI",
            status=ThemeStatus.ACTIVE,
            activity=ThemeActivitySnapshot(
                theme_id="furiosa_ai",
                theme_name="퓨리오사AI",
                theme_score=88,
                status=ThemeStatus.ACTIVE,
                rank=1,
                breadth=0.7,
                rising_count=4,
                total_count=5,
                leader_gap=1.2,
                top3_concentration=0.4,
                details={"reason_codes": []},
            ),
            membership_score=0.9,
            relation_type=RelationType.INVESTOR,
            trade_eligible=True,
            source_count=2,
            rank=1,
            rank_in_theme=1,
        ),
        theme_result=ThemeStrengthResult(theme_id="furiosa_ai", theme_name="퓨리오사AI", score=88, grade="A"),
        leadership_result=StockLeadershipResult(1, "000001", "furiosa_ai", score=95, leadership_rank=1, leadership_role="leader"),
        market_decision=GateDecision(gate_name="MarketIndexGate", passed=True, score=100),
        theme_strength_decision=GateDecision(gate_name="ThemeStrengthGate", passed=True, score=88),
        theme_pullback_decision=GateDecision(gate_name="ThemePullbackGate", passed=True, score=90),
        leadership_decision=GateDecision(gate_name="StockLeadershipGate", passed=True, score=95),
        stock_pullback_decision=GateDecision(
            gate_name="StockPullbackEntryGate",
            passed=True,
            score=100,
            details={"support_touched": True, "support_reclaimed": True, "volume_reaccel": True},
        ),
    )
