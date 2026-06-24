from trading_app.fill_simulator import krx_stock_tick_size, simulate_fill


def test_limit_buy_uses_tick_size_and_quote_for_fill_price():
    result = simulate_fill(
        {
            "side": "buy",
            "quantity": 10,
            "price": 10010,
            "order_amount": 100100,
            "hoga": "00",
            "created_at": "2026-06-01T09:00:00",
            "metadata": {"simulated_latency_ms": 0},
        },
        [
            {
                "timestamp": "2026-06-01T09:00:00",
                "code": "005930",
                "price": 10000,
                "best_bid": 9990,
                "best_ask": 10000,
                "spread_ticks": 1,
                "trade_value": 50_000_000,
                "execution_strength": 160,
            }
        ],
    ).to_dict()

    assert krx_stock_tick_size(1500) == 1
    assert krx_stock_tick_size(15000) == 10
    assert result["requested_price"] == 10010.0
    assert result["fill_price"] == 10000.0
    assert result["fill_ratio"] == 1.0
    assert result["partial_fill"] is False
    assert result["reject_or_skip_reason"] == "OK"
    assert result["slippage_bps"] < 0


def test_limit_order_skips_when_latest_tick_is_stale():
    result = simulate_fill(
        {
            "side": "buy",
            "quantity": 10,
            "price": 10000,
            "order_amount": 100000,
            "hoga": "00",
            "created_at": "2026-06-01T09:00:00",
            "metadata": {"simulated_latency_ms": 0},
        },
        [
            {
                "timestamp": "2026-06-01T08:59:55",
                "code": "005930",
                "price": 10000,
                "best_bid": 9990,
                "best_ask": 10000,
                "trade_value": 50_000_000,
            }
        ],
    ).to_dict()

    assert result["fill_price"] is None
    assert result["fill_ratio"] == 0.0
    assert result["stale_tick"] is True
    assert result["reject_or_skip_reason"] == "STALE_TICK"


def test_market_order_uses_conservative_last_plus_tick_when_quote_missing():
    result = simulate_fill(
        {
            "side": "buy",
            "quantity": 10,
            "price": 0,
            "order_amount": 100000,
            "hoga": "03",
            "created_at": "2026-06-01T09:00:00",
            "metadata": {"simulated_latency_ms": 0},
        },
        [
            {
                "timestamp": "2026-06-01T09:00:00",
                "code": "005930",
                "price": 10000,
                "trade_value": 1_000_000,
                "execution_strength": 120,
            }
        ],
    ).to_dict()

    assert result["order_style"] == "MARKET"
    assert result["requested_price"] == 10000.0
    assert result["fill_price"] == 10010.0
    assert result["partial_fill"] is True
    assert "BEST_QUOTE_MISSING_USED_CONSERVATIVE_LAST_PLUS_TICK" in result["fallback_reasons"]
