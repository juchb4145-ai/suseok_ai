from trading.theme_engine.signals import LiveSeedSignal, SeedSourceType, merge_seed_signals


def test_live_seed_signal_merges_opt10032_and_condition_include_as_sources_only():
    merged = merge_seed_signals(
        [
            LiveSeedSignal(
                code="A000001",
                source_types=(SeedSourceType.OPT10032.value,),
                seed_rank=4,
                turnover_krw=3_000_000_000,
                change_rate_pct=4.2,
                realtime_valid=True,
            ),
            LiveSeedSignal(
                code="000001",
                source_types=(SeedSourceType.CONDITION_INCLUDE.value,),
                seed_rank=0,
                turnover_krw=100_000_000,
                change_rate_pct=2.0,
                realtime_valid=False,
                reason_codes=("CONDITION_INCLUDE_BOOSTER_ONLY",),
            ),
        ]
    )

    assert len(merged) == 1
    signal = merged[0]
    assert signal.code == "000001"
    assert signal.seed_rank == 4
    assert signal.turnover_krw == 3_000_000_000
    assert set(signal.source_types) == {SeedSourceType.OPT10032.value, SeedSourceType.CONDITION_INCLUDE.value}
    assert "CONDITION_INCLUDE_BOOSTER_ONLY" in signal.reason_codes


def test_live_seed_signal_normalizes_market_and_data_quality():
    signal = LiveSeedSignal(code="A000002", market="KQ", realtime_valid=True).normalized()

    assert signal.code == "000002"
    assert signal.market == "KOSDAQ"
    assert signal.data_quality_status == "REALTIME_VALID"
