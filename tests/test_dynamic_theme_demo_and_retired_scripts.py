import json
import subprocess
import sys
from pathlib import Path


def test_demo_outputs_theme_rank_without_legacy_table(tmp_path):
    db_path = tmp_path / "demo.sqlite3"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "trading.theme_engine.demo",
            "--db",
            str(db_path),
            "--list-html",
            "tests/fixtures/naver_theme/list.html",
            "--detail-dir",
            "tests/fixtures/naver_theme",
            "--ticks",
            "tests/fixtures/theme_engine/furiosa_ticks.json",
        ],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert payload["type"] == "theme_rank"
    assert payload["themes"][0]["theme_id"] == "furiosa_ai"


def test_retired_csv_generators_only_print_retirement_message():
    for script in ["scripts/generate_theme_mappings.py", "scripts/generate_naver_theme_mappings.py"]:
        result = subprocess.run([sys.executable, script], text=True, capture_output=True)
        assert result.returncode == 1
        assert "theme_mappings.csv is retired" in result.stdout


def test_live_runtime_demo_outputs_rank_and_health(tmp_path):
    db_path = tmp_path / "live_demo.sqlite3"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "trading.theme_engine.demo_live_runtime",
            "--db",
            str(db_path),
            "--list-html",
            "tests/fixtures/naver_theme/list.html",
            "--detail-dir",
            "tests/fixtures/naver_theme",
            "--ticks",
            "tests/fixtures/theme_engine/furiosa_ticks.json",
        ],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert payload["theme_rank"]["themes"][0]["theme_id"] == "furiosa_ai"
    assert payload["runtime_health"]["data_ready"] is True
