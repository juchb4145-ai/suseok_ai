from trading.broker.transport_metrics import (
    TransportLatencySample,
    TransportTracePoint,
    monotonic_delta_ms,
    should_sample_transport_message,
)


def test_transport_trace_point_round_trip():
    point = TransportTracePoint(
        trace_id="trace-1",
        direction="gateway_to_core",
        message_type="condition_event",
        event_id="evt-1",
        process="gateway",
        stage="post_start",
        payload_size_bytes=123,
        metadata={"a": 1},
    )

    restored = TransportTracePoint.from_dict(point.to_dict())

    assert restored == point


def test_transport_latency_sample_round_trip_and_stage_fields():
    sample = TransportLatencySample(
        sample_id="lat-1",
        trace_id="trace-1",
        trade_date="2026-05-30",
        direction="gateway_ack_to_core",
        message_type="command_ack",
        command_id="cmd-1",
        stage_ms={"gateway_execute_ms": 42.0},
        total_wall_ms=100.0,
        gateway_execute_ms=42.0,
    )

    restored = TransportLatencySample.from_dict(sample.to_dict())

    assert restored.command_id == "cmd-1"
    assert restored.stage_ms["gateway_execute_ms"] == 42.0
    assert restored.total_wall_ms == 100.0


def test_monotonic_delta_is_only_local_math_not_cross_process_inferred():
    assert monotonic_delta_ms(10.0, 15.5) == 5.5
    assert monotonic_delta_ms(None, 15.5) is None


def test_price_tick_sampling_is_deterministic():
    first = should_sample_transport_message(message_type="price_tick", sample_key="evt-1", price_tick_rate=0.5)
    second = should_sample_transport_message(message_type="price_tick", sample_key="evt-1", price_tick_rate=0.5)

    assert first == second
    assert should_sample_transport_message(message_type="condition_event", sample_key="evt-2", price_tick_rate=0.0)
