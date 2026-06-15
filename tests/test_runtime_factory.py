from trading_app.runtime_factory import _cached_report_provider, _provider_cache_ttl_sec


def test_cached_report_provider_reuses_trade_date_payload():
    calls: list[str] = []

    def loader(trade_date: str) -> dict:
        calls.append(trade_date)
        return {"trade_date": trade_date, "calls": len(calls)}

    provider = _cached_report_provider(loader, ttl_sec=60)

    first = provider("2026-06-15")
    second = provider("2026-06-15")
    third = provider("2026-06-16")

    assert first == second
    assert third["calls"] == 2
    assert calls == ["2026-06-15", "2026-06-16"]


def test_cached_report_provider_ttl_zero_disables_cache():
    calls = 0

    def loader(trade_date: str) -> dict:
        nonlocal calls
        calls += 1
        return {"trade_date": trade_date, "calls": calls}

    provider = _cached_report_provider(loader, ttl_sec=0)

    assert provider("2026-06-15")["calls"] == 1
    assert provider("2026-06-15")["calls"] == 2


def test_provider_cache_ttl_accepts_policy_aliases():
    assert _provider_cache_ttl_sec({}, default=300) == 300
    assert _provider_cache_ttl_sec({"cache_ttl_sec": "15"}, default=60) == 15
    assert _provider_cache_ttl_sec({"report_cache_ttl_sec": "30"}, default=60) == 30
    assert _provider_cache_ttl_sec({"evidence_cache_ttl_sec": "45"}, default=60) == 45
    assert _provider_cache_ttl_sec({"cache_ttl_sec": "bad"}, default=60) == 60
