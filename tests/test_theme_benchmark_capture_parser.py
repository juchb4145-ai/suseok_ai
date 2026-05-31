from pathlib import Path

import pytest

from trading.theme_engine.benchmark.capture import CaptureError, RoyalroaderCaptureProvider, parse_royalroader_payload
from trading.theme_engine.benchmark.loader import load_external_theme_benchmark


FIXTURE = Path("tests/fixtures/theme_benchmark/royalroader_sample.html")


def test_royalroader_sample_html_extracts_theme_name():
    payload = parse_royalroader_payload(FIXTURE.read_text(encoding="utf-8"), trade_date="2026-05-29")

    assert payload["source"] == "royalroader"
    assert payload["trade_date"] == "2026-05-29"
    assert payload["themes"][0]["external_theme_name"] == "전력설비"
    assert payload["themes"][0]["canonical_theme_hint"] == "전력설비"


def test_royalroader_sample_html_extracts_top_stocks_and_normalizes_codes():
    provider = RoyalroaderCaptureProvider()
    payload = provider.parse_payload(FIXTURE.read_text(encoding="utf-8"), trade_date="2026-05-29")
    top_stocks = payload["themes"][0]["top_stocks"]

    assert [stock["stock_code"] for stock in top_stocks] == ["000001", "000002"]
    assert top_stocks[0]["stock_name"] == "전력리더"
    assert top_stocks[0]["change_rate"] == 12.3
    assert top_stocks[0]["turnover"] == 1234567890.0
    assert payload["themes"][0]["members"][0]["stock_code"] == "000001"


def test_capture_output_is_loader_compatible(tmp_path):
    payload = parse_royalroader_payload(FIXTURE.read_text(encoding="utf-8"), trade_date="2026-05-29")
    path = tmp_path / "royalroader_2026-05-29.json"
    import json

    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = load_external_theme_benchmark(path)
    assert loaded["themes"][0]["top_stocks"][0]["stock_code"] == "000001"


def test_royalroader_parse_failure_raises_parse_failed():
    with pytest.raises(CaptureError) as exc:
        parse_royalroader_payload("<html><body>no benchmark data</body></html>", trade_date="2026-05-29")

    assert exc.value.code == "PARSE_FAILED"
