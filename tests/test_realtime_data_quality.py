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
    assert snapshot["realtime_reliability_bucket"] in {"BROKEN", "LOW", "MEDIUM", "HIGH"}
    assert snapshot["reliability"]["bucket_counts"]
    assert snapshot["summary"].startswith("REALTIME_DATA_QUALITY total_ticks=1")


def test_realtime_data_quality_scores_latency_and_missing_fields():
    tracker = RealtimeDataQualityTracker()

    high = tracker.observe_price_tick(
        {
            "code": "005930",
            "price": 70000,
            "change_rate": 1.2,
            "volume": 1200,
            "trade_value": 84_000_000,
            "execution_strength": 123.4,
            "best_ask": 70100,
            "best_bid": 70000,
            "day_high": 71000,
            "day_low": 69000,
            "metadata": {
                "momentum_1m": 0.1,
                "momentum_3m": 0.2,
                "momentum_5m": 0.3,
                "transport_trace": {
                    "gateway_event_created_at_utc": "2026-06-01T00:00:00+00:00",
                    "core_event_received_at_utc": "2026-06-01T00:00:00.500000+00:00",
                },
            },
        }
    )
    assert high.bucket == "HIGH"
    assert high.transport_latency_ms == 500.0
    assert high.transport_latency_bucket == "STABLE"

    degraded = tracker.observe_price_tick(
        {
            "code": "000001",
            "price": 0,
            "change_rate": 0.0,
            "volume": 0,
            "metadata": {
                "reason_codes": ["REAL_PARSE_FALLBACK", "BEST_BID_ASK_MISSING"],
                "transport_trace": {
                    "gateway_event_created_at_utc": "2026-06-01T00:00:00+00:00",
                    "core_event_received_at_utc": "2026-06-01T00:00:12+00:00",
                },
            },
        }
    )

    snapshot = tracker.snapshot()
    assert degraded.bucket == "BROKEN"
    assert "PRICE_MISSING" in degraded.reasons
    assert degraded.transport_latency_bucket == "BROKEN"
    assert snapshot["reliability"]["bucket_counts"]["HIGH"] == 1
    assert snapshot["reliability"]["bucket_counts"]["BROKEN"] == 1
    assert snapshot["reliability"]["low_reliability_count"] == 1
    assert snapshot["reliability"]["transport_latency_sample_count"] == 2


def test_index_price_tick_ignores_stock_only_missing_fields():
    tracker = RealtimeDataQualityTracker()

    assessment = tracker.observe_price_tick(
        {
            "code": "001",
            "instrument_type": "stock",
            "name": "KOSPI",
            "price": 330000,
            "change_rate": 0.8,
            "volume": 1000,
            "trade_value": 10_000_000,
            "metadata": {
                "real_type": "업종등락",
                "reason_codes": [
                    "BEST_BID_ASK_MISSING",
                    "EXECUTION_STRENGTH_MISSING",
                    "DAY_HIGH_LOW_MISSING",
                ],
            },
        }
    )

    snapshot = tracker.snapshot()
    assert assessment.bucket == "HIGH"
    assert "BEST_BID_ASK_MISSING" not in assessment.reasons
    assert "EXECUTION_STRENGTH_MISSING" not in assessment.reasons
    assert "DAY_HIGH_LOW_MISSING" not in assessment.reasons
    assert "best_bid_ask" not in assessment.missing_fields
    assert "execution_strength" not in assessment.missing_fields
    assert snapshot["reason_code_counts"] == {}
