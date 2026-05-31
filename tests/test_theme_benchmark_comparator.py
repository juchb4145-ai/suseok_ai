from trading.theme_engine.benchmark.comparator import compare_theme_benchmarks
from trading.theme_engine.models import CanonicalTheme
from trading.theme_engine.repository import ThemeEngineRepository
from storage.db import TradingDatabase


def test_compare_theme_benchmarks_exact_theme_name_match():
    result = compare_theme_benchmarks(
        _external_snapshot([_external_theme("전력설비")]),
        _internal_snapshot([_internal_theme("power_grid", "전력설비")]),
    )

    assert result["summary"]["matched_theme_count"] == 1
    assert result["themes"][0]["match_method"] == "normalized_theme_name"
    assert result["themes"][0]["leader_match"] is True
    assert result["themes"][0]["mismatch_reasons"] == []


def test_compare_theme_benchmarks_canonical_theme_hint_match():
    result = compare_theme_benchmarks(
        _external_snapshot([_external_theme("반도체 장비", hint="반도체 소부장")]),
        _internal_snapshot([_internal_theme("semiconductor_parts", "반도체 소부장")]),
    )

    assert result["themes"][0]["internal_theme_id"] == "semiconductor_parts"
    assert result["themes"][0]["match_method"] == "canonical_theme_hint"


def test_compare_theme_benchmarks_alias_map_match():
    result = compare_theme_benchmarks(
        _external_snapshot([_external_theme("전력 장비")]),
        _internal_snapshot([_internal_theme("power_grid", "전력설비")]),
        alias_map={"전력 장비": "power_grid"},
    )

    assert result["themes"][0]["internal_theme_id"] == "power_grid"
    assert result["themes"][0]["match_method"] == "alias_map"


def test_compare_theme_benchmarks_repository_alias_match(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    repo = ThemeEngineRepository(db)
    repo.upsert_canonical_theme(CanonicalTheme(theme_id="semiconductor_parts", canonical_name="반도체 소부장", display_name="반도체 소부장"))
    repo.upsert_alias("semiconductor_parts", "반도체 장비", source="royalroader")

    result = compare_theme_benchmarks(
        _external_snapshot([_external_theme("반도체 장비")]),
        _internal_snapshot([_internal_theme("semiconductor_parts", "반도체 소부장")]),
        repository=repo,
    )

    assert result["themes"][0]["internal_theme_id"] == "semiconductor_parts"
    assert result["themes"][0]["match_method"] == "theme_aliases"
    db.close()


def test_compare_theme_benchmarks_missing_internal_theme():
    result = compare_theme_benchmarks(
        _external_snapshot([_external_theme("우주항공")]),
        _internal_snapshot([]),
    )

    assert result["summary"]["missing_external_theme_count"] == 1
    assert result["missing_external_themes"][0]["mismatch_reasons"] == [
        "INTERNAL_THEME_MISSING",
        "THEME_ALIAS_MISSING",
    ]
    assert result["alias_candidates"]


def test_compare_theme_benchmarks_internal_only_theme():
    result = compare_theme_benchmarks(
        _external_snapshot([_external_theme("전력설비")]),
        _internal_snapshot(
            [
                _internal_theme("power_grid", "전력설비"),
                _internal_theme("battery", "2차전지", rank=2),
            ]
        ),
    )

    assert result["summary"]["internal_only_theme_count"] == 1
    assert result["internal_only_themes"][0]["internal_theme_id"] == "battery"
    assert result["internal_only_themes"][0]["mismatch_reasons"] == ["EXTERNAL_THEME_MISSING"]


def test_compare_theme_benchmarks_overlap_metrics():
    result = compare_theme_benchmarks(
        _external_snapshot(
            [
                _external_theme(
                    "전력설비",
                    members=["000001", "000002", "000003"],
                    top_stocks=["000001", "000002", "000003", "000004", "000005"],
                )
            ]
        ),
        _internal_snapshot(
            [
                _internal_theme(
                    "power_grid",
                    "전력설비",
                    members=["000002", "000003", "000004"],
                    top_stocks=["000003", "000004", "000005", "000006", "000007"],
                    leader_code="000003",
                )
            ]
        ),
    )

    theme = result["themes"][0]
    assert theme["member_overlap_count"] == 2
    assert theme["member_jaccard_score"] == 0.5
    assert theme["top5_overlap_count"] == 3
    assert theme["top5_overlap_ratio"] == 0.6
    assert theme["matched_stocks"] == ["000002", "000003"]
    assert theme["external_only_stocks"] == ["000001"]
    assert theme["internal_only_stocks"] == ["000004"]


def test_compare_theme_benchmarks_leader_rank_delta_and_mismatch():
    result = compare_theme_benchmarks(
        _external_snapshot([_external_theme("전력설비", top_stocks=["000001", "000002"])]),
        _internal_snapshot(
            [
                _internal_theme(
                    "power_grid",
                    "전력설비",
                    top_stocks=["000002", "000001"],
                    leader_code="000002",
                )
            ]
        ),
    )

    theme = result["themes"][0]
    assert theme["leader_match"] is False
    assert theme["leader_rank_delta"] == 1
    assert "LEADER_MISMATCH" in theme["mismatch_reasons"]


def test_compare_theme_benchmarks_low_overlap_reasons():
    result = compare_theme_benchmarks(
        _external_snapshot(
            [
                _external_theme(
                    "전력설비",
                    members=["000001", "000002", "000003"],
                    top_stocks=["000001", "000002", "000003", "000004", "000005"],
                )
            ]
        ),
        _internal_snapshot(
            [
                _internal_theme(
                    "power_grid",
                    "전력설비",
                    members=["000006", "000007"],
                    top_stocks=["000006", "000007"],
                    leader_code="000006",
                )
            ]
        ),
    )

    reasons = result["themes"][0]["mismatch_reasons"]
    assert "LOW_TOP5_OVERLAP" in reasons
    assert "LOW_MEMBER_OVERLAP" in reasons


def _external_snapshot(themes):
    return {
        "source": "royalroader",
        "trade_date": "2026-05-29",
        "ranking_basis": "change_rate",
        "themes": themes,
    }


def _internal_snapshot(themes):
    return {
        "source": "internal_dynamic_theme_engine",
        "trade_date": "2026-05-29",
        "ranking_basis": "theme_score",
        "themes": themes,
    }


def _external_theme(
    name,
    *,
    hint="",
    rank=1,
    members=None,
    top_stocks=None,
):
    members = list(members or ["000001", "000002", "000003"])
    top_stocks = list(top_stocks or ["000001", "000002"])
    return {
        "external_theme_name": name,
        "canonical_theme_hint": hint,
        "rank": rank,
        "score": 0.0,
        "top_stocks": _stocks(top_stocks),
        "members": [{"stock_code": code, "stock_name": f"stock-{code}"} for code in members],
    }


def _internal_theme(
    theme_id,
    name,
    *,
    rank=1,
    members=None,
    top_stocks=None,
    leader_code="000001",
):
    members = list(members or ["000001", "000002", "000003"])
    top_stocks = list(top_stocks or ["000001", "000002"])
    return {
        "theme_id": theme_id,
        "theme_name": name,
        "rank": rank,
        "theme_score": 80.0,
        "weighted_return_pct": 3.0,
        "turnover": 1000000000.0,
        "breadth": 0.8,
        "leader_code": leader_code,
        "top_stocks": _stocks(top_stocks),
        "members": [{"stock_code": code, "stock_name": f"stock-{code}", "active": True} for code in members],
        "reason_codes": [],
        "snapshot_quality": {},
    }


def _stocks(codes):
    return [
        {
            "stock_code": code,
            "stock_name": f"stock-{code}",
            "rank": rank,
            "change_rate": 1.0,
            "turnover": 1000.0,
        }
        for rank, code in enumerate(codes, start=1)
    ]
