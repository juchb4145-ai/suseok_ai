from pathlib import Path

from trading_app.dry_run_threshold_ab import DryRunThresholdABAnalyzer, ThresholdABConfig


def _item(identifier: str, **overrides) -> dict:
    payload = {
        "lifecycle_id": identifier,
        "trade_date": "2026-05-30",
        "code": "005930",
        "theme_name": "AI",
        "strategy_name": "KOSDAQ_THEME_PROFILE",
        "session_bucket": "OPEN",
        "gate_reason": "READY",
        "entry_intent_id": f"entry-{identifier}",
        "entry_intent_status": "DRY_RUN_ACCEPTED",
        "entry_live_would_pass": True,
        "entry_live_reject_reason": "",
        "realized_return_pct": 1.5,
        "max_return_20m": 3.2,
        "max_drawdown_20m": -0.5,
        "dry_run_false_positive_type": "",
        "dry_run_false_negative_type": "",
        "opportunity_loss_type": "",
        "signal_classification": "true_positive",
        "exit_decision_id": f"exit-{identifier}",
        "quality_bucket": "GOOD",
        "theme_score": 82.0,
        "hybrid_score": 84.0,
        "gate_score": 80.0,
        "details": {},
    }
    payload.update(overrides)
    return payload


def _report(items: list[dict]) -> dict:
    return {
        "trade_date": "2026-05-30",
        "items": items,
        "summary": {"total_lifecycle_count": len(items)},
        "false_signal_summary": {},
        "grouped": {},
    }


def _loose_config(**overrides) -> ThresholdABConfig:
    payload = {
        "min_sample_count": 1,
        "min_trade_days": 1,
        "min_completed_lifecycles": 1,
        "min_entry_intents": 1,
        "min_exit_decisions": 1,
        "min_signal_samples": 1,
    }
    payload.update(overrides)
    return ThresholdABConfig(**payload)


def test_late_chase_false_positive_generates_risk_candidate():
    items = [
        _item(
            "late-1",
            gate_reason="LATE_CHASE",
            realized_return_pct=-2.0,
            max_drawdown_20m=-4.0,
            dry_run_false_positive_type="LATE_CHASE_FALSE_POSITIVE",
            signal_classification="false_positive",
            theme_score=45.0,
            hybrid_score=50.0,
            gate_score=48.0,
        ),
        _item("good-1"),
    ]
    analyzer = DryRunThresholdABAnalyzer(config=_loose_config(strong_fp_reduction_min=1))

    report = analyzer.build_report(_report(items), limit=100)

    candidate = next(item for item in report["candidates"] if item["candidate_id"] == "risk:late_chase:block")
    result = report["results"][candidate["candidate_id"]]
    assert candidate["label_ko"] == "추격매수 위험 차단 강화"
    assert result["delta"]["avoided_false_positive_count"] == 1
    assert result["recommendation"]["grade"] in {"STRONG_CANDIDATE", "OBSERVE_CANDIDATE", "WATCH_CANDIDATE"}


def test_low_breadth_rallied_generates_watch_allow_candidate():
    items = [
        _item(
            "low-breadth-1",
            gate_reason="LOW_BREADTH",
            entry_intent_status="DRY_RUN_REJECTED",
            entry_live_would_pass=False,
            entry_live_reject_reason="GATEWAY_NOT_CONNECTED",
            dry_run_false_negative_type="DRY_RUN_REJECTED_BUT_RALLIED",
            opportunity_loss_type="SAFETY_REJECT_REASON_OPPORTUNITY_LOSS",
            signal_classification="false_negative",
            max_return_20m=5.0,
            theme_score=78.0,
        ),
        _item("good-2"),
    ]
    analyzer = DryRunThresholdABAnalyzer(config=_loose_config())

    report = analyzer.build_report(_report(items), limit=100)

    candidate = next(item for item in report["candidates"] if item["candidate_id"] == "gate:low_breadth:watch_allow")
    result = report["results"][candidate["candidate_id"]]
    assert candidate["category"] == "gate"
    assert result["delta"]["newly_allowed_count"] == 1
    assert "기회손실" in candidate["expected_effect_ko"]


def test_score_threshold_and_low_sample_grading():
    items = [
        _item(
            "weak-score-1",
            theme_score=35,
            hybrid_score=42,
            gate_score=38,
            realized_return_pct=-1.5,
            dry_run_false_positive_type="LIVE_WOULD_PASS_BUT_NEGATIVE_RETURN",
            signal_classification="false_positive",
        ),
        _item("strong-score-1", theme_score=90, hybrid_score=88, gate_score=85),
    ]
    analyzer = DryRunThresholdABAnalyzer(config=ThresholdABConfig(min_sample_count=10))

    report = analyzer.build_report(_report(items), limit=100)

    theme_candidates = [item for item in report["candidates"] if item["parameter_name"] == "theme_score_min"]
    assert theme_candidates
    result = report["results"][theme_candidates[0]["candidate_id"]]
    assert result["recommendation"]["grade"] == "DATA_INSUFFICIENT_FOR_THRESHOLD_CHANGE"


def test_score_threshold_skips_unknown_above_win_rate_without_crashing():
    items = [
        _item("weak-score-unknown", theme_score=50, hybrid_score=50, gate_score=50, realized_return_pct=None),
        _item("strong-score-unknown", theme_score=90, hybrid_score=90, gate_score=90, realized_return_pct=None),
    ]
    analyzer = DryRunThresholdABAnalyzer(config=_loose_config())

    report = analyzer.build_report(_report(items), limit=100)

    assert report["status"] == "READY"


def test_export_markdown_csv_json_are_korean(tmp_path):
    items = [_item("export-1", gate_reason="LATE_CHASE", dry_run_false_positive_type="LATE_CHASE_FALSE_POSITIVE", realized_return_pct=-2.0)]
    analyzer = DryRunThresholdABAnalyzer(
        config=_loose_config(export_root=Path(tmp_path) / "reports")
    )
    report = analyzer.build_report(_report(items), limit=100)

    exports = analyzer.export_report(report, fmt="all")

    assert set(exports) == {"json", "csv", "md"}
    for path in exports.values():
        assert Path(path).exists()
    markdown = Path(exports["md"]).read_text(encoding="utf-8")
    assert "DRY_RUN 기반 게이트/리스크 기준 A/B 제안 리포트" in markdown
    assert "실제 전략 설정에 자동 적용하지 않습니다" in markdown
    csv_text = Path(exports["csv"]).read_text(encoding="utf-8-sig")
    assert "label_ko" in csv_text


def test_small_sample_is_insufficient_for_threshold_change():
    items = [
        _item(
            "small-1",
            gate_reason="LATE_CHASE",
            realized_return_pct=-2.0,
            max_return_20m=0.8,
            dry_run_false_positive_type="LATE_CHASE_FALSE_POSITIVE",
            signal_classification="false_positive",
        )
    ]
    analyzer = DryRunThresholdABAnalyzer(config=ThresholdABConfig(min_sample_count=1, strong_fp_reduction_min=1))

    report = analyzer.build_report(_report(items), limit=100)

    result = report["results"]["risk:late_chase:block"]
    assert result["recommendation"]["raw_grade"] == "STRONG_CANDIDATE"
    assert result["recommendation"]["grade"] == "DATA_INSUFFICIENT_FOR_THRESHOLD_CHANGE"
    assert result["recommendation"]["guardrail_passed"] is False
    assert "MIN_TRADE_DAYS" in result["recommendation"]["blocked_by_guardrail_reason"]


def test_min_sample_count_miss_never_becomes_strong_candidate():
    items = [
        _item(
            f"late-{index}",
            trade_date=f"2026-05-{30 + index}",
            gate_reason="LATE_CHASE",
            realized_return_pct=-2.0,
            max_return_20m=0.8,
            dry_run_false_positive_type="LATE_CHASE_FALSE_POSITIVE",
            signal_classification="false_positive",
        )
        for index in range(1, 5)
    ]
    analyzer = DryRunThresholdABAnalyzer(
        config=_loose_config(min_sample_count=10, strong_fp_reduction_min=1, min_trade_days=1)
    )

    report = analyzer.build_report(_report(items), limit=100)

    result = report["results"]["risk:late_chase:block"]
    assert result["recommendation"]["grade"] == "DATA_INSUFFICIENT_FOR_THRESHOLD_CHANGE"
    assert result["recommendation"]["grade"] != "STRONG_CANDIDATE"
    assert "MIN_SAMPLE_COUNT" in result["recommendation"]["blocked_by_guardrail_reason"]


def test_multi_day_repeated_candidate_can_reach_observe_or_better():
    items = [
        _item(
            f"late-{index}",
            trade_date=f"2026-06-0{index}",
            gate_reason="LATE_CHASE",
            realized_return_pct=-2.0,
            max_return_20m=0.8,
            dry_run_false_positive_type="LATE_CHASE_FALSE_POSITIVE",
            signal_classification="false_positive",
        )
        for index in range(1, 6)
    ]
    analyzer = DryRunThresholdABAnalyzer(
        config=_loose_config(min_trade_days=5, min_sample_count=5, strong_fp_reduction_min=10)
    )

    report = analyzer.build_report(_report(items), limit=100)

    result = report["results"]["risk:late_chase:block"]
    assert result["recommendation"]["sample_trade_days"] == 5
    assert result["recommendation"]["guardrail_passed"] is True
    assert result["recommendation"]["grade"] in {"OBSERVE_CANDIDATE", "WATCH_CANDIDATE"}


def test_safety_candidate_is_operational_review_only():
    items = [
        _item(
            f"safety-{index}",
            trade_date=f"2026-06-0{index}",
            entry_intent_status="DRY_RUN_REJECTED",
            entry_live_reject_reason="GATEWAY_NOT_CONNECTED",
            gate_reason="READY",
            dry_run_false_negative_type="LIVE_SAFETY_REJECTED_BUT_RALLIED",
            opportunity_loss_type="SAFETY_REJECT_REASON_OPPORTUNITY_LOSS",
            signal_classification="false_negative",
            max_return_20m=5.0,
        )
        for index in range(1, 6)
    ]
    analyzer = DryRunThresholdABAnalyzer(config=_loose_config(min_trade_days=5, min_sample_count=5))

    report = analyzer.build_report(_report(items), limit=100)

    safety_candidates = [item for item in report["candidates"] if item["category"] == "safety"]
    assert safety_candidates
    result = report["results"][safety_candidates[0]["candidate_id"]]
    assert result["recommendation"]["grade"] == "OPERATIONAL_REVIEW_ONLY"
    assert "OPERATIONAL_REVIEW_ONLY" in result["recommendation"]["blocked_by_guardrail_reason"]
