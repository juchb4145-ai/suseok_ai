from datetime import datetime, timedelta

from trading.strategy.market_index import IndexCodeMapper, IndexTick, MarketIndexStore


def index_tick(price, at, code="101", cum_volume=0, change_rate=0.0):
    return IndexTick.from_realtime(
        index_code=code,
        name="KOSDAQ",
        price=price,
        change_rate=change_rate,
        cum_volume=cum_volume,
        timestamp=at,
    )


def test_index_tick_normalization():
    tick = IndexTick.from_realtime("101", "KOSDAQ", "-950.5", change_rate="+1.25%", cum_volume="-100")

    assert tick.index_code == "101"
    assert tick.price == 950
    assert tick.change_rate == 1.25
    assert tick.cum_volume == 100


def test_index_code_mapper_converts_raw_codes_to_logical_codes():
    mapper = IndexCodeMapper()

    assert mapper.logical_code("001") == "KOSPI"
    assert mapper.logical_code("101") == "KOSDAQ"
    assert mapper.logical_code("KOSDAQ") == "KOSDAQ"
    assert mapper.logical_code("005930") is None


def test_kosdaq_text_code_is_preserved_in_state():
    store = MarketIndexStore()

    state = store.update_index_tick(index_tick(950, datetime(2026, 5, 29, 9, 0), code="KOSDAQ"))

    assert state.index_code == "KOSDAQ"
    assert state.price == 950


def test_first_tick_low_break_recent_is_false():
    store = MarketIndexStore()

    state = store.update_index_tick(index_tick(950, datetime(2026, 5, 29, 9, 0)))

    assert state.low_break_recent is False
    assert state.day_high == 950
    assert state.day_low == 950


def test_index_tick_without_day_high_low_uses_price_fallback():
    store = MarketIndexStore()

    state = store.update_index_tick(index_tick(950, datetime(2026, 5, 29, 9, 0), code="KOSDAQ"))

    assert state.day_high == 950
    assert state.day_low == 950
    assert state.day_mid == 950


def test_index_tick_with_explicit_day_high_low_updates_state():
    store = MarketIndexStore()
    tick = IndexTick.from_realtime(
        "KOSDAQ",
        "KOSDAQ",
        940,
        day_high=960,
        day_low=930,
        timestamp=datetime(2026, 5, 29, 9, 0),
    )

    state = store.update_index_tick(tick)

    assert state.day_high == 960
    assert state.day_low == 930
    assert state.low_break_recent is False


def test_new_low_after_existing_low_sets_low_break_metadata():
    store = MarketIndexStore()
    store.update_index_tick(index_tick(950, datetime(2026, 5, 29, 9, 0)))

    state = store.update_index_tick(index_tick(940, datetime(2026, 5, 29, 9, 1)))

    assert state.low_break_recent is True
    assert state.metadata["low_break_count"] == 1
    assert state.metadata["low_break_at"] == "2026-05-29T09:01:00"


def test_direction_uses_completed_5m_candles_only():
    store = MarketIndexStore()
    start = datetime(2026, 5, 29, 9, 0)
    cum_volume = 1_000
    for minute in range(10):
        price = 900 + minute * 10
        at = start + timedelta(minutes=minute)
        store.update_index_tick(index_tick(price, at, cum_volume=cum_volume))
        cum_volume += 100
        store.update_index_tick(index_tick(price + 5, at + timedelta(seconds=20), cum_volume=cum_volume))
        store.candle_builder.flush("101", at + timedelta(minutes=1))

    state = store.state("101")

    assert state.direction_5m == "UP"

    store.update_index_tick(index_tick(800, start + timedelta(minutes=10), cum_volume=cum_volume + 100))
    state_after_active_drop = store.state("101")

    assert state_after_active_drop.direction_5m == "UP"


def test_day_mid_and_mid_position():
    store = MarketIndexStore()
    store.update_index_tick(index_tick(950, datetime(2026, 5, 29, 9, 0)))
    store.update_index_tick(index_tick(900, datetime(2026, 5, 29, 9, 1)))
    state = store.update_index_tick(index_tick(930, datetime(2026, 5, 29, 9, 2)))

    assert state.day_mid == 925
    assert state.mid_position == "ABOVE_MID"
