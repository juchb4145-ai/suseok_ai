import json
from pathlib import Path

import pytest

from trading.theme_engine.benchmark.loader import load_external_theme_benchmark


FIXTURE_DIR = Path("tests/fixtures/theme_benchmark")
JSON_FIXTURE = FIXTURE_DIR / "sample_royalroader.json"
CSV_FIXTURE = FIXTURE_DIR / "sample_royalroader.csv"


def test_load_external_theme_benchmark_json():
    payload = load_external_theme_benchmark(JSON_FIXTURE)

    assert payload["source"] == "royalroader"
    assert payload["captured_at"] == "2026-05-31T13:00:00+09:00"
    assert payload["trade_date"] == "2026-05-29"
    assert payload["ranking_basis"] == "change_rate"
    assert payload["themes"][0]["external_theme_name"] == "전력설비"
    assert payload["themes"][0]["top_stocks"]
    assert payload["themes"][0]["members"]


def test_load_external_theme_benchmark_csv():
    payload = load_external_theme_benchmark(CSV_FIXTURE)

    assert payload["source"] == "royalroader"
    assert payload["trade_date"] == "2026-05-29"
    assert payload["ranking_basis"] == "change_rate"
    assert len(payload["themes"]) == 2
    assert payload["themes"][0]["external_theme_name"] == "전력설비"
    assert len(payload["themes"][0]["members"]) == 3


def test_loader_normalizes_stock_codes_and_sorts_top_stocks():
    payload = load_external_theme_benchmark(CSV_FIXTURE)
    first_theme = payload["themes"][0]
    second_theme = payload["themes"][1]

    assert [stock["stock_code"] for stock in first_theme["top_stocks"]] == ["000001", "000002"]
    assert [stock["rank"] for stock in first_theme["top_stocks"]] == [1, 2]
    assert second_theme["top_stocks"][0]["stock_code"] == "005930"
    assert first_theme["top_stocks"][0]["change_rate"] == 12.3
    assert first_theme["top_stocks"][0]["turnover"] == 1234567890.0


def test_loader_generates_members_from_csv_rows():
    payload = load_external_theme_benchmark(CSV_FIXTURE)
    members = payload["themes"][0]["members"]

    assert [member["stock_code"] for member in members] == ["000002", "000001", "000003"]
    assert all(set(member) == {"stock_code", "stock_name"} for member in members)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"trade_date": "2026-05-29", "themes": []}, "missing source"),
        ({"source": "royalroader", "themes": []}, "missing trade_date"),
        ({"source": "royalroader", "trade_date": "2026-05-29", "themes": {}}, "themes must be list"),
        (
            {"source": "royalroader", "trade_date": "2026-05-29", "themes": [{"rank": 1, "members": [{"stock_code": "000001"}]}]},
            "missing external_theme_name",
        ),
        (
            {"source": "royalroader", "trade_date": "2026-05-29", "themes": [{"external_theme_name": "전력설비", "rank": 1}]},
            "top_stocks or members required",
        ),
        (
            {
                "source": "royalroader",
                "trade_date": "2026-05-29",
                "themes": [{"external_theme_name": "전력설비", "rank": 1, "members": [{"stock_code": "bad"}]}],
            },
            "invalid stock_code",
        ),
        (
            {
                "source": "royalroader",
                "trade_date": "2026-05-29",
                "themes": [
                    {"external_theme_name": "전력설비", "rank": 1, "members": [{"stock_code": "000001"}]},
                    {"external_theme_name": "AI반도체", "rank": 1, "members": [{"stock_code": "000002"}]},
                ],
            },
            "duplicate theme rank",
        ),
    ],
)
def test_loader_invalid_schema_raises_value_error(tmp_path, payload, message):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_external_theme_benchmark(path)


def test_json_and_csv_normalize_to_same_theme_structure():
    json_payload = load_external_theme_benchmark(JSON_FIXTURE)
    csv_payload = load_external_theme_benchmark(CSV_FIXTURE)

    assert set(json_payload) == set(csv_payload)
    assert [_theme_shape(theme) for theme in json_payload["themes"]] == [
        _theme_shape(theme) for theme in csv_payload["themes"]
    ]
    assert _comparison_view(json_payload) == _comparison_view(csv_payload)


def _theme_shape(theme):
    return {
        "theme_keys": set(theme),
        "top_stock_keys": [set(stock) for stock in theme["top_stocks"]],
        "member_keys": [set(member) for member in theme["members"]],
    }


def _comparison_view(payload):
    return [
        {
            "external_theme_name": theme["external_theme_name"],
            "canonical_theme_hint": theme["canonical_theme_hint"],
            "rank": theme["rank"],
            "top_stock_codes": [stock["stock_code"] for stock in theme["top_stocks"]],
            "member_codes": [member["stock_code"] for member in theme["members"]],
        }
        for theme in payload["themes"]
    ]
