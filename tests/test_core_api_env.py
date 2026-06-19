import os
from pathlib import Path

from apps.core_api import load_project_env
from trading_app.dependencies import PROJECT_ROOT, resolve_trading_db_path


def test_load_project_env_sets_missing_values(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "# comment",
                "TRADING_RUNTIME_ENABLED=1",
                "TRADING_RUNTIME_AUTO_START=0",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("TRADING_RUNTIME_ENABLED", raising=False)
    monkeypatch.delenv("TRADING_RUNTIME_AUTO_START", raising=False)

    load_project_env(env_path)

    assert os.environ["TRADING_RUNTIME_ENABLED"] == "1"
    assert os.environ["TRADING_RUNTIME_AUTO_START"] == "0"


def test_load_project_env_does_not_override_existing_values(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("TRADING_RUNTIME_ENABLED=1\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_RUNTIME_ENABLED", "0")

    load_project_env(env_path)

    assert os.environ["TRADING_RUNTIME_ENABLED"] == "0"


def test_resolve_trading_db_path_uses_project_root_for_relative_env(monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", "data/trader.sqlite3")

    resolved = resolve_trading_db_path()

    assert resolved == (PROJECT_ROOT / "data" / "trader.sqlite3").resolve(strict=False)
    assert Path("data/trader.sqlite3").resolve(strict=False) != resolved or Path.cwd() == PROJECT_ROOT
