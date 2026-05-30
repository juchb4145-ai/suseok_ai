import pytest

from trading.broker.rate_limit import RateLimiter


def test_rate_limiter_blocks_until_min_interval_passes():
    limiter = RateLimiter({"send_order": 0.5, "*": 0.1})

    assert limiter.allow("send_order", now=10.0) is True
    limiter.record("send_order", now=10.0)

    assert limiter.allow("send_order", now=10.2) is False
    assert limiter.wait_time("send_order", now=10.2) == pytest.approx(0.3)
    assert limiter.allow("send_order", now=10.5) is True
    assert limiter.snapshot()["commands"]["send_order"]["limited_count"] == 1
