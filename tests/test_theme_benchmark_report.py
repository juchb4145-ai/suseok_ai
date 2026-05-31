import csv
import json
from pathlib import Path

from trading.theme_engine.benchmark.report import write_benchmark_reports


def test_write_benchmark_reports_creates_json_csv_and_markdown(tmp_path):
    result = _result()

    paths = write_benchmark_reports(result, tmp_path / "theme_benchmark", "2026-05-29")

    json_path = Path(paths["json"])
    csv_path = Path(paths["csv"])
    md_path = Path(paths["markdown"])
    assert json_path.exists()
    assert csv_path.exists()
    assert md_path.exists()

    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded["summary"]["matched_theme_count"] == 2
    assert loaded["themes"][0]["external_theme_name"] == "전력설비"

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig", newline="")))
    assert rows[0]["trade_date"] == "2026-05-29"
    assert rows[0]["external_theme_name"] == "전력설비"
    assert rows[0]["internal_theme_id"] == "power_grid"
    assert rows[0]["mismatch_reasons"] == ""
    assert rows[1]["mismatch_reasons"] == "LOW_TOP5_OVERLAP,LOW_MEMBER_OVERLAP,LEADER_MISMATCH"

    markdown = md_path.read_text(encoding="utf-8")
    assert "# Theme Benchmark Report" in markdown
    assert "## Summary" in markdown
    assert "- matched_theme_count: 2" in markdown
    assert "전력설비" in markdown
    assert "## Top Mismatched Themes" in markdown
    assert "## Alias Candidates" in markdown
    assert "반도체 장비" in markdown


def _result():
    return {
        "source": "royalroader",
        "summary": {
            "external_theme_count": 3,
            "internal_theme_count": 3,
            "matched_theme_count": 2,
            "avg_member_jaccard": 0.55,
            "avg_top5_overlap": 0.5,
            "leader_match_rate": 0.5,
            "missing_external_theme_count": 1,
            "internal_only_theme_count": 1,
        },
        "themes": [
            {
                "external_theme_name": "전력설비",
                "canonical_theme_hint": "전력설비",
                "internal_theme_id": "power_grid",
                "internal_theme_name": "전력설비",
                "external_rank": 1,
                "internal_rank": 1,
                "member_jaccard_score": 1.0,
                "top5_overlap_ratio": 1.0,
                "leader_match": True,
                "leader_rank_delta": 0,
                "external_only_stocks": [],
                "internal_only_stocks": [],
                "mismatch_reasons": [],
            },
            {
                "external_theme_name": "반도체 장비",
                "canonical_theme_hint": "반도체 소부장",
                "internal_theme_id": "semiconductor_parts",
                "internal_theme_name": "반도체 소부장",
                "external_rank": 2,
                "internal_rank": 5,
                "member_jaccard_score": 0.1,
                "top5_overlap_ratio": 0.2,
                "leader_match": False,
                "leader_rank_delta": 2,
                "external_only_stocks": ["000010"],
                "internal_only_stocks": ["000020"],
                "mismatch_reasons": ["LOW_TOP5_OVERLAP", "LOW_MEMBER_OVERLAP", "LEADER_MISMATCH"],
            },
        ],
        "missing_external_themes": [
            {
                "external_theme_name": "우주항공",
                "canonical_theme_hint": "",
                "external_rank": 3,
                "mismatch_reasons": ["INTERNAL_THEME_MISSING", "THEME_ALIAS_MISSING"],
            }
        ],
        "internal_only_themes": [
            {
                "internal_theme_id": "battery",
                "internal_theme_name": "2차전지",
                "internal_rank": 3,
                "mismatch_reasons": ["EXTERNAL_THEME_MISSING"],
            }
        ],
        "alias_candidates": [
            {
                "external_theme_name": "우주항공",
                "canonical_theme_hint": "",
                "normalized_external_theme_name": "우주항공",
                "normalized_canonical_theme_hint": "",
                "mismatch_reasons": ["THEME_ALIAS_MISSING", "INTERNAL_THEME_MISSING"],
            }
        ],
        "external_top5_by_theme": {
            "전력설비": ["000001", "000002"],
            "반도체 장비": ["000010", "000011"],
        },
        "internal_top5_by_theme": {
            "power_grid": ["000001", "000002"],
            "semiconductor_parts": ["000020", "000011"],
        },
    }
