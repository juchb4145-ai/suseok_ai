from pathlib import Path


SCRIPT = Path("tools/start_market_open_live_sim.ps1")


def test_market_open_live_sim_script_declares_runtime_safety_envs():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "[int]$RuntimeDryRunPositionAmount = 30000000" in text
    assert "$env:TRADING_MODE = \"OBSERVE\"" in text
    assert "$env:TRADING_RUNTIME_ENABLED = \"1\"" in text
    assert "$env:TRADING_RUNTIME_AUTO_START = \"0\"" in text
    assert "$env:TRADING_RUNTIME_MODE = \"DRY_RUN\"" in text
    assert "$env:TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS = \"1\"" in text
    assert "$env:TRADING_RUNTIME_ALLOW_LIVE_ORDERS = \"0\"" in text
    assert "$env:TRADING_RUNTIME_DRY_RUN_POSITION_AMOUNT" in text
    assert "$env:TRADING_SHADOW_STRATEGY_OBSERVE_ONLY = \"1\"" in text
    assert "$env:TRADING_SHADOW_STRATEGY_ALLOW_APPLY = \"0\"" in text
    assert "$env:TRADING_CHANGE_PROPOSAL_ALLOW_AUTO_APPLY = \"0\"" in text
    assert "$env:TRADING_THEME_BACKFILL_OBSERVE_ONLY = \"1\"" in text


def test_market_open_live_sim_script_reports_new_operator_surfaces():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "/api/themelab/snapshot" in text
    assert "/api/shadow-small-entry-ops/status" in text
    assert "/api/shadow-small-entry-ops/preflight" in text
    assert "themelab_url" in text
    assert "dashboard_url" in text
    assert "shadow_small_entry_ops" in text
    assert "theme_backfill" in text


def test_market_open_live_sim_script_configures_websocket_pilot_url():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "$GatewayWsUrl = \"ws://${gatewayHost}:$Port/ws/gateway/transport\"" in text
    assert "$env:TRADING_GATEWAY_WS_URL = $GatewayWsUrl" in text
    assert "gateway_ws_url" in text
