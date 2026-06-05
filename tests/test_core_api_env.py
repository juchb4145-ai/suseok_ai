import os

from apps.core_api import load_project_env


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
