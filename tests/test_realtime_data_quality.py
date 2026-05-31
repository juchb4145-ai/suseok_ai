from trading.broker.data_quality import RealtimeDataQualityTracker


def test_realtime_data_quality_counts_coverage_and_reason_codes():
    tracker = RealtimeDataQualityTracker()

    tracker.observe_price_tick(
        {
            "code": "005930",
            "price": 70000,
            "change_rate": 1.2,
            "volume": 1200,
            "best_ask": 70100,
            "best_bid": 70000,
            "metadata": {
                "reason_codes": ["TRADE_VALUE_MISSING", "TURNOVER_ESTIMATED", "REAL_PARSE_FALLBACK"],
                "momentum_1m": 0.0,
                "momentum_3m": 0.0,
                "momentum_5m": 0.0,
            },
        }
    )

    snapshot = tracker.snapshot()
    assert snapshot["total_price_ticks"] == 1
    assert snapshot["field_coverage"]["price"] == 1.0
    assert snapshot["field_coverage"]["trade_value"] == 0.0
    assert snapshot["field_coverage"]["best_bid_ask"] == 1.0
    assert snapshot["field_coverage"]["momentum"] == 1.0
    assert snapshot["reason_code_counts"]["TRADE_VALUE_MISSING"] == 1
    assert snapshot["reason_code_counts"]["REAL_PARSE_FALLBACK"] == 1
    assert snapshot["estimated_turnover_count"] == 1
    assert snapshot["parse_fallback_count"] == 1
    assert snapshot["summary"].startswith("REALTIME_DATA_QUALITY total_ticks=1")
