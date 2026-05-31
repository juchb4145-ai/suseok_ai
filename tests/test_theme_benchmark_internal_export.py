import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "theme_engine" / "furiosa_ai.json"


def test_fixture_replay_exports_internal_benchmark_snapshot(tmp_path):
    db_path = tmp_path / "theme_replay.sqlite3"
    out_path = tmp_path / "reports" / "theme_benchmark" / "internal_2026-05-29.json"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "trading.theme_engine.benchmark.replay",
            "--fixture",
            str(FIXTURE),
            "--db",
            str(db_path),
            "--trade-date",
            "2026-05-29",
            "--out",
            str(out_path),
        ],
        capture_output=True,
        cwd=ROOT,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert out_path.exists()

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["source"] == "internal_dynamic_theme_engine"
    assert payload["trade_date"] == "2026-05-29"
    assert payload["ranking_basis"] == "theme_score"
    assert payload["themes"]

    theme = payload["themes"][0]
    assert theme["top_stocks"]
    assert theme["members"]
    assert theme["leader_code"] == theme["top_stocks"][0]["stock_code"]
    assert "reason_codes" in theme
    assert "snapshot_quality" in theme
