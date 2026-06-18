from trading.reliability.replay import DeterministicReplayVerifier
from trading.reliability.workload import SyntheticGatewayEventGenerator, SyntheticWorkloadConfig


def test_same_gateway_event_input_produces_same_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_SEND_ORDER_ALLOWED", "false")
    monkeypatch.setenv("TRADING_ORDER_MANAGER_OBSERVE_ONLY", "true")
    monkeypatch.setenv("TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND", "false")
    monkeypatch.setenv("TRADING_ORDER_INTENT_ENABLED", "false")
    events = SyntheticGatewayEventGenerator(
        SyntheticWorkloadConfig(code_count=3, ticks_per_sec=2, duration_sec=1, order_event_rate=0.0, duplicate_rate=0.1, seed=7)
    ).generate()
    result = DeterministicReplayVerifier(output_dir=tmp_path).verify_events(events, repeat=2)
    assert result.status == "PASS"
    assert len({item.digest for item in result.digests}) == 1


def test_dynamic_replay_fields_are_excluded_from_digest(tmp_path):
    events = SyntheticGatewayEventGenerator(
        SyntheticWorkloadConfig(code_count=2, ticks_per_sec=1, duration_sec=1, order_event_rate=0.0, duplicate_rate=0.0, seed=11)
    ).generate()
    result = DeterministicReplayVerifier(output_dir=tmp_path).verify_events(events, repeat=2)
    assert not result.mismatch
