from datetime import datetime, timedelta

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.entry import EntryPlanBuilder
from trading.strategy.exit import ExitDecisionEngine, VirtualPositionService
from trading.strategy.market_data import StrategyTick
from trading.strategy.models import (
    BlockType,
    Candidate,
    CandidateState,
    EntryPlan,
    ExitDecision,
    FillPolicy,
    GateDecision,
    IndicatorSnapshot,
    ReviewFinalStatus,
    StrategyProfile,
    TradeReview,
    VirtualOrder,
    VirtualOrderStatus,
)
from trading.strategy.pipeline import GatePipelineResult
from trading.strategy.replay import TickReplayRunner
from trading.strategy.review import TradeReviewService
from trading.strategy.virtual_orders import VirtualOrderService


def candidate(state=CandidateState.WATCHING):
    return Candidate(
        id=1,
        trade_date="2026-05-29",
        code="111111",
        name="테스트",
        market="KOSDAQ",
        strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
        theme_ids=["robot"],
        state=state,
        detected_at="2026-05-29T09:00:00",
        expires_at="2026-05-29T09:05:00",
    )


def gate_result(block_type=BlockType.NONE, eligible=True, created_at="2026-05-29T09:00:00"):
    decisions = [
        GateDecision(
            candidate_id=1,
            gate_name="MarketIndexGate",
            passed=eligible,
            score=80,
            grade="A",
            block_type=block_type,
            reason_codes=[] if eligible else ["MARKET_WAIT"],
            details={"index_code": "KOSDAQ"},
            created_at=created_at,
        ),
        GateDecision(
            candidate_id=1,
            gate_name="FinalGrade",
            passed=eligible,
            score=80,
            grade="A" if eligible else "C",
            block_type=block_type,
            reason_codes=[] if eligible else ["MARKET_INDEX_TEMPORARY_CAP"],
            details={
                "theme_name": "로봇",
                "sub_status": "PASS" if eligible else "MARKET_WAIT",
                "actual_order_allowed": False,
                "entry_plan_created": False,
            },
            created_at=created_at,
        ),
    ]
    return GatePipelineResult(
        candidate_id=1,
        code="111111",
        theme_id="robot",
        final_grade="A" if eligible else "C",
        final_score=80,
        strategy_eligible=eligible,
        block_type=block_type,
        decisions=decisions,
        snapshot=IndicatorSnapshot(
            candidate_id=1,
            code="111111",
            created_at=created_at,
            price=10_000,
            metadata={"strategy_profile": StrategyProfile.KOSDAQ_THEME_PROFILE.value},
        ),
        details={"theme_id": "robot", "theme_name": "로봇"},
    )


def plan(id=10, status_theme="robot"):
    return EntryPlan(
        id=id,
        candidate_id=1,
        entry_type="pullback_limit",
        base_price_source="vwap",
        limit_price=10_000,
        split_plan=[{"leg": 1, "weight_pct": 100, "limit_price": 10_000}],
        order_timeout_sec=60,
        cancel_condition={
            "code": "111111",
            "theme_id": status_theme,
            "theme_name": "로봇",
            "strategy_profile": StrategyProfile.KOSDAQ_THEME_PROFILE.value,
            "gate_result_key": "1:111111:robot:A",
            "submittable": True,
        },
        fill_policy=FillPolicy.NORMAL,
        created_at="2026-05-29T09:00:00",
    )


def order(status=VirtualOrderStatus.UNFILLED):
    return VirtualOrder(
        id=20,
        candidate_id=1,
        entry_plan_id=10,
        status=status,
        limit_price=10_000,
        virtual_fill_price=10_000 if status == VirtualOrderStatus.FILLED else 0,
        submitted_at="2026-05-29T09:00:00",
        filled_at="2026-05-29T09:01:00" if status == VirtualOrderStatus.FILLED else "",
        cancelled_at="2026-05-29T09:02:00" if status == VirtualOrderStatus.CANCELLED else "",
    )


def builder_with_candle(high=10_400, low=9_900, close=10_200, start=datetime(2026, 5, 29, 9, 1)):
    builder = CandleBuilder()
    builder.update(StrategyTick.from_realtime("111111", 10_000, cum_volume=1_000, timestamp=start + timedelta(seconds=1)))
    builder.update(StrategyTick.from_realtime("111111", high, cum_volume=1_100, timestamp=start + timedelta(seconds=15)))
    builder.update(StrategyTick.from_realtime("111111", low, cum_volume=1_200, timestamp=start + timedelta(seconds=30)))
    builder.update(StrategyTick.from_realtime("111111", close, cum_volume=1_300, timestamp=start + timedelta(seconds=45)))
    builder.flush("111111", start + timedelta(minutes=1))
    return builder


def test_trade_review_model_db_roundtrip_and_upsert(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    review = TradeReview(
        candidate_id=1,
        trade_date="2026-05-29",
        code="111111",
        name="테스트",
        market="KOSDAQ",
        theme_id="robot",
        theme_name="로봇",
        strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE.value,
        gate_result_key="1:111111:robot:A",
        review_key="1:111111:robot:A",
        final_grade="A",
        final_status=ReviewFinalStatus.VIRTUAL_FILLED.value,
        details={"gate_decisions_snapshot": [{"gate_name": "FinalGrade"}]},
        created_at="2026-05-29T09:05:00",
    )

    first = db.save_trade_review(review)
    review.final_status = ReviewFinalStatus.VIRTUAL_UNFILLED.value
    second = db.save_trade_review(review)
    loaded = db.list_trade_reviews(1)

    assert first.id == second.id
    assert len(loaded) == 1
    assert loaded[0].theme_id == "robot"
    assert loaded[0].strategy_profile == StrategyProfile.KOSDAQ_THEME_PROFILE.value
    assert loaded[0].details["gate_decisions_snapshot"][0]["gate_name"] == "FinalGrade"
    assert loaded[0].final_status == ReviewFinalStatus.VIRTUAL_UNFILLED.value
    db.close()


def test_final_status_enum_values_cover_phase_1_review_states():
    values = {status.value for status in ReviewFinalStatus}

    assert {
        "BLOCKED_TEMP",
        "BLOCKED_FINAL",
        "EXPIRED",
        "VIRTUAL_UNFILLED",
        "VIRTUAL_CANCELLED",
        "VIRTUAL_PARTIAL_TAKE_PROFIT",
        "VIRTUAL_CLOSED_SUPPORT_LOSS",
    }.issubset(values)


def test_blocked_review_horizon_and_gate_snapshot_false_negative():
    review = TradeReviewService().build_review(
        candidate(),
        gate_result(block_type=BlockType.TEMPORARY, eligible=False),
        candle_builder=builder_with_candle(high=10_400),
        created_at=datetime(2026, 5, 29, 9, 3),
    )

    assert review.final_status == ReviewFinalStatus.BLOCKED_TEMP.value
    assert review.details["horizon_start_reason"] == "gate_evaluated_at"
    assert review.max_return_20m == 4.0
    assert review.false_negative_flag is True
    assert review.details["false_negative_type"] == "BLOCKED_LATER_RALLIED"
    assert review.details["gate_decisions_snapshot"][0]["gate_name"] == "MarketIndexGate"
    assert "MARKET_WAIT" in review.details["blocking_reason_codes"]
    assert "MARKET_INDEX_TEMPORARY_CAP" in review.details["blocking_reason_codes"]


def test_unfilled_later_rallied_is_separate_false_negative_type():
    review = TradeReviewService().build_review(
        candidate(),
        gate_result(),
        entry_plan=plan(),
        virtual_order=order(VirtualOrderStatus.UNFILLED),
        candle_builder=builder_with_candle(high=10_400),
        created_at=datetime(2026, 5, 29, 9, 3),
    )

    assert review.final_status == ReviewFinalStatus.VIRTUAL_UNFILLED.value
    assert review.details["horizon_start_reason"] == "virtual_order_submitted_at"
    assert review.details["false_negative_type"] == "UNFILLED_LATER_RALLIED"
    assert review.missed_reason == "UNFILLED_LATER_RALLIED"


def test_expired_review_uses_expired_at_and_keeps_detected_metrics():
    expired = candidate(CandidateState.EXPIRED)
    builder = builder_with_candle(start=datetime(2026, 5, 29, 9, 6), high=10_500)

    review = TradeReviewService().build_review(
        expired,
        gate_result(),
        candle_builder=builder,
        created_at=datetime(2026, 5, 29, 9, 8),
    )

    assert review.final_status == ReviewFinalStatus.EXPIRED.value
    assert review.details["horizon_start_reason"] == "candidate_expired_at"
    assert review.details["detected_at_metrics"]["max_return_20m"] == 5.0


def test_partial_take_profit_weighted_return_prevents_simple_false_positive():
    virtual_position = order_position = None
    from trading.strategy.models import VirtualPosition

    virtual_position = VirtualPosition(
        id=30,
        candidate_id=1,
        virtual_order_id=20,
        entry_price=10_000,
        opened_at="2026-05-29T09:00:00",
        max_drawdown_pct=-4.0,
    )
    take_profit = ExitDecision(
        virtual_position_id=30,
        decision_type="TAKE_PROFIT",
        trigger_price=10_500,
        filled=True,
        details={"partial_exit": True, "exit_percent": 70, "target_return_pct": 5.0, "position_closed": False},
        created_at="2026-05-29T09:01:00",
    )

    review = TradeReviewService().build_review(
        candidate(),
        gate_result(),
        entry_plan=plan(),
        virtual_order=order(VirtualOrderStatus.FILLED),
        virtual_position=virtual_position,
        exit_decisions=[take_profit],
        candle_builder=builder_with_candle(high=10_500, low=9_500),
        created_at=datetime(2026, 5, 29, 9, 3),
    )

    assert order_position is None
    assert review.final_status == ReviewFinalStatus.VIRTUAL_PARTIAL_TAKE_PROFIT.value
    assert review.details["partial_take_profit_hit"] is True
    assert review.details["weighted_virtual_return_pct"] == 3.5
    assert review.false_positive_flag is False


def test_phase_1_acceptance_replay_review_and_export(tmp_path):
    rows = [
        {"timestamp": "2026-05-29T09:00:01", "code": "111111", "price": 10_050, "cum_volume": 1_000},
        {"timestamp": "2026-05-29T09:00:30", "code": "111111", "price": 10_000, "cum_volume": 1_100},
        {"timestamp": "2026-05-29T09:01:01", "code": "111111", "price": 10_040, "cum_volume": 1_200},
        {"timestamp": "2026-05-29T09:01:30", "code": "111111", "price": 9_990, "cum_volume": 1_300},
        {"timestamp": "2026-05-29T09:02:01", "code": "111111", "price": 10_100, "cum_volume": 1_400},
        {"timestamp": "2026-05-29T09:02:30", "code": "111111", "price": 10_600, "cum_volume": 1_500},
    ]
    runner = TickReplayRunner()
    replay_result = runner.replay_rows(rows)
    strategy_result = gate_result()
    strategy_result.snapshot.price = 10_000
    strategy_result.decisions.append(
        GateDecision(
            gate_name="StockPullbackEntryGate",
            passed=True,
            score=90,
            grade="A",
            details={
                "nearest_support": "vwap",
                "nearest_support_price": 10_000,
                "profile": StrategyProfile.KOSDAQ_THEME_PROFILE.value,
            },
        )
    )
    built_plan = EntryPlanBuilder().build(strategy_result, datetime(2026, 5, 29, 9, 0))
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    saved_plan = db.save_entry_plan(built_plan)
    order_service = VirtualOrderService(db=db)
    submitted = order_service.submit_virtual_order(saved_plan, datetime(2026, 5, 29, 9, 0))
    saved_order = db.save_virtual_order(submitted.order)
    fill_result = order_service.evaluate_fill(saved_order, saved_plan, runner.candle_builder, datetime(2026, 5, 29, 9, 3))
    saved_order = db.save_virtual_order(fill_result.order)
    opened = VirtualPositionService(db=db).open_from_filled_order(saved_order, saved_plan)
    exit_decisions = ExitDecisionEngine().evaluate(
        opened.position,
        IndicatorSnapshot(
            candidate_id=1,
            code="111111",
            price=10_600,
            vwap=10_000,
            day_mid=10_000,
            metadata={"strategy_profile": StrategyProfile.KOSDAQ_THEME_PROFILE.value},
        ),
        runner.candle_builder,
        [],
        datetime(2026, 5, 29, 9, 4),
    )
    review = TradeReviewService().build_review(
        candidate(),
        strategy_result,
        saved_plan,
        saved_order,
        opened.position,
        exit_decisions,
        runner.candle_builder,
        datetime(2026, 5, 29, 9, 4),
    )
    saved_review = db.save_trade_review(review)
    from trading.strategy.export import ReviewExporter

    csv_path = ReviewExporter().export_csv(db.list_trade_reviews(), tmp_path / "reviews.csv")
    md_path = ReviewExporter().export_markdown(db.list_trade_reviews(), tmp_path / "reviews.md")

    assert replay_result.completed_1m_count == 3
    assert saved_order.status == VirtualOrderStatus.FILLED
    assert saved_review.final_status == ReviewFinalStatus.VIRTUAL_PARTIAL_TAKE_PROFIT.value
    assert saved_review.max_return_20m > 5.8
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "False Negative" in md_path.read_text(encoding="utf-8")
    db.close()


def test_review_export_has_fixed_columns_and_does_not_create_db_rows(tmp_path):
    from trading.strategy.export import REVIEW_EXPORT_COLUMNS, ReviewExporter

    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    db.save_trade_review(
        TradeReview(
            candidate_id=1,
            trade_date="2026-05-29",
            code="111111",
            theme_id="robot",
            review_key="fixed",
            final_status=ReviewFinalStatus.BLOCKED_TEMP.value,
            created_at="2026-05-29T09:00:00",
        )
    )
    before_count = len(db.list_trade_reviews())

    csv_path = ReviewExporter().export_csv(db.list_trade_reviews(), tmp_path / "out.csv")
    md_path = ReviewExporter().export_markdown(db.list_trade_reviews(), tmp_path / "out.md")
    after_count = len(db.list_trade_reviews())
    csv_text = csv_path.read_text(encoding="utf-8-sig")

    assert before_count == after_count == 1
    assert csv_text.splitlines()[0] == ",".join(REVIEW_EXPORT_COLUMNS)
    assert md_path.read_text(encoding="utf-8").startswith("# Strategy Review")
    db.close()


def test_review_export_groups_false_positive_and_negative_by_reason_code(tmp_path):
    from trading.strategy.export import ReviewExporter

    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    db.save_trade_review(
        TradeReview(
            candidate_id=1,
            trade_date="2026-05-29",
            code="111111",
            theme_id="robot",
            review_key="fn",
            final_status=ReviewFinalStatus.BLOCKED_TEMP.value,
            max_return_20m=4.5,
            max_drawdown_20m=-1.0,
            false_negative_flag=True,
            details={"blocking_reason_codes": ["MARKET_WAIT"]},
            created_at="2026-05-29T09:00:00",
        )
    )
    db.save_trade_review(
        TradeReview(
            candidate_id=2,
            trade_date="2026-05-29",
            code="222222",
            theme_id="robot",
            review_key="fp",
            final_status=ReviewFinalStatus.VIRTUAL_CLOSED_SUPPORT_LOSS.value,
            max_return_20m=1.0,
            max_drawdown_20m=-4.0,
            false_positive_flag=True,
            details={"entry_condition_codes": ["SUPPORT_RECLAIMED"], "exit_reason_codes": ["SUPPORT_LOSS"]},
            created_at="2026-05-29T09:05:00",
        )
    )

    assert len(db.list_trade_reviews(trade_date="2026-05-29")) == 2
    md_path = ReviewExporter().export_markdown(db.list_trade_reviews(), tmp_path / "reason.md")
    text = md_path.read_text(encoding="utf-8")

    assert "Missed Reason Code Performance" in text
    assert "Loss Reason Code Performance" in text
    assert "MARKET_WAIT" in text
    assert "SUPPORT_RECLAIMED" in text
    assert "SUPPORT_LOSS" in text
    db.close()


def test_tick_replay_is_deterministic_with_same_timestamp_order():
    rows = [
        {"timestamp": "2026-05-29T09:00:01", "code": "111111", "price": 10_000, "cum_volume": 1_000},
        {"timestamp": "2026-05-29T09:00:01", "code": "111111", "price": 10_100, "cum_volume": 1_100},
        {"timestamp": "2026-05-29T09:01:01", "code": "111111", "price": 10_200, "cum_volume": 1_200},
    ]

    first = TickReplayRunner()
    second = TickReplayRunner()
    first_result = first.replay_rows(rows)
    second_result = second.replay_rows(rows)

    assert first_result.processed_ticks == second_result.processed_ticks
    assert first.candle_builder.completed_candles("111111", 1) == second.candle_builder.completed_candles("111111", 1)
