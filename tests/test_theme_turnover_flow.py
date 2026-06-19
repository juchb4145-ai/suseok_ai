from trading.theme_engine.cohort import ThemeCohortSnapshot
from trading.theme_engine.signals import LiveSeedSignal
from trading.theme_engine.turnover_flow import TurnoverFlowTracker, TurnoverObservation


def test_turnover_flow_splits_cumulative_turnover_from_recent_delta():
    tracker = TurnoverFlowTracker()

    tracker.observe(TurnoverObservation(code="000001", observed_at="2026-06-19T09:20:00", cumulative_turnover_krw=1_000_000_000))
    flow = tracker.observe(TurnoverObservation(code="000001", observed_at="2026-06-19T09:21:00", cumulative_turnover_krw=1_600_000_000))

    assert flow is not None
    assert flow.cumulative_turnover_krw == 1_600_000_000
    assert flow.turnover_delta_1m == 600_000_000
    assert flow.turnover_speed_1m == 600_000_000


def test_turnover_flow_clamps_negative_reset_delta_to_zero():
    tracker = TurnoverFlowTracker()

    tracker.observe(TurnoverObservation(code="000001", observed_at="2026-06-19T09:20:00", cumulative_turnover_krw=1_000_000_000))
    flow = tracker.observe(TurnoverObservation(code="000001", observed_at="2026-06-19T09:21:00", cumulative_turnover_krw=100_000_000))

    assert flow is not None
    assert flow.turnover_delta_1m == 0.0
    assert "TURNOVER_RESET_DETECTED" in flow.reason_codes


def test_turnover_flow_marks_duplicate_and_out_of_order_without_new_delta():
    tracker = TurnoverFlowTracker()

    tracker.observe(TurnoverObservation(code="000001", observed_at="2026-06-19T09:20:00", cumulative_turnover_krw=1_000_000_000))
    first = tracker.observe(TurnoverObservation(code="000001", observed_at="2026-06-19T09:21:00", cumulative_turnover_krw=1_500_000_000))
    duplicate = tracker.observe(TurnoverObservation(code="000001", observed_at="2026-06-19T09:21:00", cumulative_turnover_krw=2_000_000_000))
    out_of_order = tracker.observe(TurnoverObservation(code="000001", observed_at="2026-06-19T09:20:30", cumulative_turnover_krw=2_500_000_000))

    assert first is not None
    assert duplicate is not None
    assert duplicate.turnover_delta_1m == first.turnover_delta_1m
    assert "TURNOVER_DUPLICATE_OBSERVATION" in duplicate.reason_codes
    assert out_of_order is not None
    assert out_of_order.flow_freshness == "STALE"
    assert "TURNOVER_TIMESTAMP_OUT_OF_ORDER" in out_of_order.reason_codes


def test_turnover_flow_observes_realtime_signals_only():
    tracker = TurnoverFlowTracker()

    result = tracker.observe_signals(
        [
            LiveSeedSignal(code="000001", turnover_krw=1_000_000_000, observed_at="2026-06-19T09:20:00", realtime_valid=True),
            LiveSeedSignal(code="000002", turnover_krw=2_000_000_000, observed_at="2026-06-19T09:20:00", tr_backfill_valid=True, realtime_valid=False),
        ]
    )

    assert set(result) == {"000001"}


def test_theme_turnover_flow_uses_recent_member_flow_share():
    tracker = TurnoverFlowTracker()
    tracker.observe(TurnoverObservation(code="000001", observed_at="2026-06-19T09:20:00", cumulative_turnover_krw=1_000_000_000))
    tracker.observe(TurnoverObservation(code="000002", observed_at="2026-06-19T09:20:00", cumulative_turnover_krw=1_000_000_000))
    tracker.observe(TurnoverObservation(code="000001", observed_at="2026-06-19T09:21:00", cumulative_turnover_krw=2_000_000_000))
    tracker.observe(TurnoverObservation(code="000002", observed_at="2026-06-19T09:21:00", cumulative_turnover_krw=1_200_000_000))

    flows = tracker.theme_flows(
        [
            ThemeCohortSnapshot(theme_id="ai", signals=(LiveSeedSignal(code="000001"),)),
            ThemeCohortSnapshot(theme_id="battery", signals=(LiveSeedSignal(code="000002"),)),
        ],
        observed_at="2026-06-19T09:21:00",
    )

    assert flows["ai"].theme_turnover_delta_1m == 1_000_000_000
    assert flows["battery"].theme_turnover_delta_1m == 200_000_000
    assert flows["ai"].theme_flow_share > flows["battery"].theme_flow_share
