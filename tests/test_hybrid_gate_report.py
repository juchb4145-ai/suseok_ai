from trading.strategy.export import ReviewExporter
from trading.strategy.hybrid_gate import summarize_hybrid_gate_reviews
from trading.strategy.models import ReviewFinalStatus, TradeReview


def test_hybrid_gate_summary_counts_status_and_reasons():
    reviews = [
        _review(
            "000001",
            ReviewFinalStatus.BLOCKED_TEMP.value,
            {
                "hybrid_status": "READY",
                "hybrid_score": 84.5,
                "hybrid_position_tier": "normal_first_entry",
                "hybrid_reason_codes": ["STRONG_ACTIVE_THEME"],
                "dynamic_theme_score": 84.5,
                "membership_score": 0.9,
            },
        ),
        _review(
            "000002",
            ReviewFinalStatus.VIRTUAL_FILLED.value,
            {
                "hybrid_status": "BLOCKED",
                "hybrid_score": 42.0,
                "hybrid_position_tier": "blocked",
                "hybrid_reason_codes": ["LEADER_ONLY_THEME_LAGGARD_BLOCK"],
                "dynamic_theme_score": 82.0,
                "membership_score": 0.6,
            },
        ),
        _review(
            "000003",
            ReviewFinalStatus.BLOCKED_TEMP.value,
            {
                "hybrid_status": "WAIT",
                "hybrid_score": 68.0,
                "hybrid_position_tier": "none",
                "hybrid_reason_codes": ["LOW_BREADTH"],
                "dynamic_theme_score": 70.0,
                "membership_score": 0.7,
            },
        ),
    ]

    summary = summarize_hybrid_gate_reviews(reviews)

    assert summary["status_counts"] == {"READY": 1, "BLOCKED": 1, "WAIT": 1}
    assert summary["ready_but_legacy_not_bought"] == ["000001"]
    assert summary["legacy_ready_but_hybrid_blocked"] == ["000002"]
    assert summary["leader_only_blocked"] == ["000002"]
    assert summary["wait_reason_top"] == [{"reason_code": "LOW_BREADTH", "count": 1}]


def test_review_export_includes_hybrid_gate_section(tmp_path):
    reviews = [
        _review(
            "000001",
            ReviewFinalStatus.BLOCKED_TEMP.value,
            {
                "hybrid_status": "READY",
                "hybrid_score": 84.5,
                "hybrid_position_tier": "normal_first_entry",
                "hybrid_reason_codes": ["STRONG_ACTIVE_THEME"],
                "dynamic_theme_score": 84.5,
                "membership_score": 0.9,
            },
        )
    ]

    exporter = ReviewExporter()
    summary = exporter.build_summary(reviews)
    md_path = exporter.export_markdown(reviews, tmp_path / "hybrid.md")
    markdown = md_path.read_text(encoding="utf-8")

    assert summary["hybrid_gate_summary"]["candidate_count"] == 1
    assert "## Hybrid Gate Summary" in markdown
    assert "READY but legacy not bought" in markdown
    assert "000001" in markdown


def _review(code: str, final_status: str, details: dict):
    return TradeReview(
        candidate_id=int(code),
        trade_date="2026-05-30",
        code=code,
        name=f"MOCK-{code}",
        theme_id="furiosa_ai",
        theme_name="퓨리오사AI",
        final_grade="C",
        final_status=final_status,
        max_return_20m=4.2,
        max_drawdown_20m=-1.1,
        details=dict(details),
        created_at="2026-05-30T09:10:00",
    )
