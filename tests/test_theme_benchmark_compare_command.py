import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NAVER_LIST = ROOT / "tests" / "fixtures" / "naver_theme" / "list.html"
NAVER_DETAIL_DIR = ROOT / "tests" / "fixtures" / "naver_theme"
TICKS = ROOT / "tests" / "fixtures" / "theme_engine" / "furiosa_ticks.json"
SAMPLE_EXTERNAL = ROOT / "tests" / "fixtures" / "theme_benchmark" / "sample_royalroader.json"


def test_compare_command_replays_fixture_and_compares_sample_external(tmp_path):
    out_dir = tmp_path / "reports" / "theme_benchmark"
    result = _run_compare(
        tmp_path,
        "--list-html",
        str(NAVER_LIST),
        "--detail-dir",
        str(NAVER_DETAIL_DIR),
        "--ticks",
        str(TICKS),
        "--db",
        str(tmp_path / "theme_replay.sqlite3"),
        "--trade-date",
        "2026-05-29",
        "--external",
        str(SAMPLE_EXTERNAL),
        "--out",
        str(out_dir),
    )

    assert result.returncode == 0, result.stderr
    assert (out_dir / "internal_2026-05-29.json").exists()
    assert (out_dir / "theme_benchmark_2026-05-29.json").exists()
    assert (out_dir / "theme_benchmark_2026-05-29.csv").exists()
    assert (out_dir / "theme_benchmark_2026-05-29.md").exists()
    payload = json.loads((out_dir / "theme_benchmark_2026-05-29.json").read_text(encoding="utf-8"))
    assert payload["summary"]["status"] == "COMPARED"


def test_compare_command_uses_existing_internal_snapshot(tmp_path):
    internal_path = tmp_path / "internal_2026-05-29.json"
    internal_path.write_text(
        json.dumps(
            {
                "source": "internal_dynamic_theme_engine",
                "trade_date": "2026-05-29",
                "ranking_basis": "theme_score",
                "themes": [
                    {
                        "theme_id": "power_grid",
                        "theme_name": "전력설비",
                        "rank": 1,
                        "theme_score": 80.0,
                        "weighted_return_pct": 1.0,
                        "turnover": 1000.0,
                        "breadth": 1.0,
                        "leader_code": "000001",
                        "top_stocks": [{"stock_code": "000001", "stock_name": "전력리더", "rank": 1}],
                        "members": [{"stock_code": "000001", "stock_name": "전력리더"}],
                        "reason_codes": [],
                        "snapshot_quality": {},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "reports" / "theme_benchmark"

    result = _run_compare(
        tmp_path,
        "--internal",
        str(internal_path),
        "--external",
        str(SAMPLE_EXTERNAL),
        "--out",
        str(out_dir),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads((out_dir / "theme_benchmark_2026-05-29.json").read_text(encoding="utf-8"))
    assert payload["summary"]["status"] == "COMPARED"


def test_compare_command_skips_when_external_missing(tmp_path):
    out_dir = tmp_path / "reports" / "theme_benchmark"
    result = _run_compare(
        tmp_path,
        "--list-html",
        str(NAVER_LIST),
        "--detail-dir",
        str(NAVER_DETAIL_DIR),
        "--ticks",
        str(TICKS),
        "--db",
        str(tmp_path / "theme_replay.sqlite3"),
        "--trade-date",
        "2026-05-29",
        "--out",
        str(out_dir),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads((out_dir / "theme_benchmark_2026-05-29.json").read_text(encoding="utf-8"))
    assert payload["summary"]["status"] == "SKIPPED"
    assert payload["summary"]["reason"] == "EXTERNAL_BENCHMARK_NOT_PROVIDED"
    assert (out_dir / "internal_2026-05-29.json").exists()
    assert (out_dir / "theme_benchmark_2026-05-29.md").exists()


def test_compare_command_does_not_fail_on_low_overlap(tmp_path):
    external_path = tmp_path / "external_low_overlap.json"
    external_path.write_text(
        json.dumps(
            {
                "source": "royalroader",
                "trade_date": "2026-05-29",
                "ranking_basis": "change_rate",
                "themes": [
                    {
                        "external_theme_name": "Furiosa External",
                        "canonical_theme_hint": "furiosa_ai",
                        "rank": 1,
                        "score": 0.0,
                        "top_stocks": [
                            {"stock_code": "999999", "stock_name": "MISS", "rank": 1},
                            {"stock_code": "999998", "stock_name": "MISS2", "rank": 2},
                        ],
                        "members": [{"stock_code": "999999", "stock_name": "MISS"}],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "reports" / "theme_benchmark"

    result = _run_compare(
        tmp_path,
        "--list-html",
        str(NAVER_LIST),
        "--detail-dir",
        str(NAVER_DETAIL_DIR),
        "--ticks",
        str(TICKS),
        "--db",
        str(tmp_path / "theme_replay.sqlite3"),
        "--trade-date",
        "2026-05-29",
        "--external",
        str(external_path),
        "--out",
        str(out_dir),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads((out_dir / "theme_benchmark_2026-05-29.json").read_text(encoding="utf-8"))
    assert payload["themes"]
    assert "LOW_TOP5_OVERLAP" in payload["themes"][0]["mismatch_reasons"]
    assert "LOW_MEMBER_OVERLAP" in payload["themes"][0]["mismatch_reasons"]


def test_compare_command_fails_on_external_parse_error(tmp_path):
    bad_external = tmp_path / "bad_external.json"
    bad_external.write_text("{bad json", encoding="utf-8")

    result = _run_compare(
        tmp_path,
        "--list-html",
        str(NAVER_LIST),
        "--detail-dir",
        str(NAVER_DETAIL_DIR),
        "--ticks",
        str(TICKS),
        "--db",
        str(tmp_path / "theme_replay.sqlite3"),
        "--trade-date",
        "2026-05-29",
        "--external",
        str(bad_external),
        "--out",
        str(tmp_path / "reports" / "theme_benchmark"),
    )

    assert result.returncode != 0


def _run_compare(tmp_path, *args):
    return subprocess.run(
        [sys.executable, "-m", "trading.theme_engine.benchmark.compare", *args],
        capture_output=True,
        cwd=ROOT,
        text=True,
    )
