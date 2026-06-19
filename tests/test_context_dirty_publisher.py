from datetime import datetime

from trading.strategy.context_dirty_publisher import (
    MarketRegimeDirtyPublisher,
    StrategyContextDirtyPublisher,
    ThemeStateDirtyPublisher,
)
from trading.strategy.market_data_service import DirtyCodeQueue, DirtyReason


def test_market_regime_dirty_publisher_marks_only_changed_side_after_initial_publish():
    queue = DirtyCodeQueue(debounce_ms=0)
    publisher = MarketRegimeDirtyPublisher()
    codes_by_side = {"KOSPI": ["005930"], "KOSDAQ": ["035720"], "UNKNOWN": ["000000"]}

    initial = publisher.publish(
        {
            "global_status": "SELECTIVE",
            "kospi_status": "SELECTIVE",
            "kosdaq_status": "SELECTIVE",
            "market_action": "ALLOW",
            "block_new_entry": False,
        },
        dirty_queue=queue,
        codes_by_side=codes_by_side,
        now=datetime(2026, 6, 19, 9, 20, 0),
    )
    queue.pop_dirty(limit=10)
    changed = publisher.publish(
        {
            "global_status": "SELECTIVE",
            "kospi_status": "WEAK",
            "kosdaq_status": "SELECTIVE",
            "market_action": "ALLOW",
            "block_new_entry": False,
        },
        dirty_queue=queue,
        codes_by_side=codes_by_side,
        now=datetime(2026, 6, 19, 9, 21, 0),
    )
    events = queue.pop_dirty(limit=10)

    assert initial.published_count == 3
    assert changed.codes == ("005930",)
    assert [(event.code, event.reason) for event in events] == [("005930", DirtyReason.MARKET_REGIME_CHANGED.value)]


def test_theme_state_dirty_publisher_accepts_dict_states_and_skips_unchanged():
    queue = DirtyCodeQueue(debounce_ms=0)
    publisher = ThemeStateDirtyPublisher()
    state = {"theme_id": "ai", "theme_state": "LEADING_THEME", "leader_symbol": "000001", "co_leader_symbols": ["000002"]}

    first = publisher.publish([state], dirty_queue=queue, code_by_theme={"ai": ["000001", "000002", "000003"]})
    second = publisher.publish([state], dirty_queue=queue, code_by_theme={"ai": ["000001", "000002", "000003"]})

    assert first.published_count == 3
    assert second.published_count == 0
    assert second.reason == "THEME_UNCHANGED"


def test_theme_state_dirty_publisher_splits_leader_and_role_change_reasons():
    queue = DirtyCodeQueue(debounce_ms=0)
    publisher = ThemeStateDirtyPublisher()
    first_state = {"theme_id": "ai", "theme_state": "LEADING_THEME", "leader_symbol": "000001", "co_leader_symbols": ["000002"]}
    second_state = {"theme_id": "ai", "theme_state": "LEADING_THEME", "leader_symbol": "000002", "co_leader_symbols": []}
    first_roles = [{"theme_id": "ai", "code": "000001", "trade_role": "LEADER_CONFIRMED"}]
    second_roles = [{"theme_id": "ai", "code": "000001", "trade_role": "FOLLOWER_ALLOWED"}]

    publisher.publish([first_state], dirty_queue=queue, code_by_theme={"ai": ["000001", "000002"]}, stock_roles=first_roles)
    queue.pop_dirty(limit=10)
    result = publisher.publish([second_state], dirty_queue=queue, code_by_theme={"ai": ["000001", "000002"]}, stock_roles=second_roles)
    events = queue.pop_dirty(limit=10)

    assert DirtyReason.THEME_LEADER_CHANGED.value in result.reason
    assert DirtyReason.THEME_ROLE_CHANGED.value in result.reason
    assert {event.code for event in events} == {"000001", "000002"}
    assert any(DirtyReason.THEME_LEADER_CHANGED.value in event.reason for event in events)
    assert any(DirtyReason.THEME_ROLE_CHANGED.value in event.reason for event in events)


def test_strategy_context_dirty_publisher_marks_context_id_changes_only():
    queue = DirtyCodeQueue(debounce_ms=0)
    publisher = StrategyContextDirtyPublisher()

    first = publisher.publish([{"code": "A000001", "context_id": "ctx-1"}], dirty_queue=queue)
    second = publisher.publish([{"code": "000001", "context_id": "ctx-1"}], dirty_queue=queue)
    third = publisher.publish([{"code": "000001", "context_id": "ctx-2"}], dirty_queue=queue)

    assert first.published_count == 1
    assert second.published_count == 0
    assert third.published_count == 1
