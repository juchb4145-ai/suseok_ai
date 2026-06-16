from pathlib import Path


SCRIPT = Path("tools/start_market_open_live_sim.ps1")


def test_market_open_live_sim_script_declares_runtime_safety_envs():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "[int]$RuntimeDryRunPositionAmount = 30000000" in text
    assert "[switch]$RequireGatewayOrderable" in text
    assert "[switch]$AllowLiveSimWithWarnings" in text
    assert "[int]$GatewayStartupRetryCount = 1" in text
    assert "$env:TRADING_MODE = \"OBSERVE\"" in text
    assert "$env:TRADING_RUNTIME_ENABLED = \"1\"" in text
    assert "$env:TRADING_RUNTIME_AUTO_START = \"0\"" in text
    assert "$env:TRADING_RUNTIME_MODE = \"DRY_RUN\"" in text
    assert "$env:TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS = \"1\"" in text
    assert "$env:TRADING_RUNTIME_ALLOW_LIVE_ORDERS = \"0\"" in text
    assert "$env:TRADING_RUNTIME_LIVE_SIM_REQUIRE_PREFLIGHT_GO_FOR_ORDER_SINK = \"1\"" in text
    assert "$env:TRADING_RUNTIME_LIVE_SIM_ALLOW_PREFLIGHT_WARNINGS_FOR_ORDER_SINK" in text
    assert "$env:TRADING_RUNTIME_DRY_RUN_POSITION_AMOUNT" in text
    assert "$env:TRADING_SHADOW_STRATEGY_OBSERVE_ONLY = \"1\"" in text
    assert "$env:TRADING_SHADOW_STRATEGY_ALLOW_APPLY = \"0\"" in text
    assert "$env:TRADING_CHANGE_PROPOSAL_ALLOW_AUTO_APPLY = \"0\"" in text
    assert "$env:TRADING_THEME_BACKFILL_ENABLED = if ($DisableThemeBackfillWarmup) { \"0\" } else { \"1\" }" in text
    assert "$env:TRADING_THEME_BACKFILL_OBSERVE_ONLY = \"1\"" in text
    assert "$env:TRADING_THEME_BACKFILL_MAX_PER_CYCLE = [string]$ThemeBackfillMaxPerCycle" in text
    assert "$env:TRADING_THEME_BACKFILL_MAX_PENDING = [string]$ThemeBackfillMaxPending" in text
    assert "$env:TRADING_THEME_BACKFILL_TTL_SEC = [string]$ThemeBackfillTtlSec" in text
    assert "$env:TRADING_THEME_BACKFILL_OPT10001_BUCKET_SEC = [string]$ThemeBackfillOpt10001BucketSec" in text
    assert "$env:TRADING_THEME_BACKFILL_ALLOW_OPT10081 = \"0\"" in text
    assert "$env:TRADING_THEME_BACKFILL_ALLOW_REGULAR_SESSION = if ($PreOpenDataWarmupEnabled) { \"0\" } else { \"1\" }" in text
    assert "$env:TRADING_THEME_BACKFILL_MAX_THEMES = [string]$ThemeBackfillMaxThemes" in text
    assert "$env:TRADING_THEME_BACKFILL_MAX_HITS_PER_THEME = [string]$ThemeBackfillMaxHitsPerTheme" in text
    assert "$env:TRADING_THEME_BACKFILL_CACHE_ENABLED = \"1\"" in text
    assert "$env:TRADING_THEME_BACKFILL_CACHE_TTL_SEC = [string]$ThemeBackfillCacheTtlSec" in text
    assert "$env:TRADING_THEME_BACKFILL_CACHE_LIMIT = [string]$ThemeBackfillCacheLimit" in text
    assert "[switch]$SkipPreOpenDataWarmup" in text
    assert "$PreOpenDataWarmupEnabled" in text
    assert "Get-LiveSimPreflightStatus" in text
    assert "/api/runtime/live-sim/preflight/rebuild?include_details=true" in text
    assert "Assert-LiveSimPreflightAllowsStartup" in text
    assert "GO_WITH_WARNINGS" in text
    assert "FAIL_CLOSED" in text
    assert "REAL/UNKNOWN/LIVE_REAL" in text
    assert "live_sim_preflight" in text


def test_market_open_live_sim_script_reports_new_operator_surfaces():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "/api/themelab/snapshot" in text
    assert "[switch]$WaitThemeLabStartupSnapshot" in text
    assert "STARTUP_SNAPSHOT_DECOUPLED" in text
    assert "/api/shadow-small-entry-ops/status" in text
    assert "/api/shadow-small-entry-ops/preflight" in text
    assert "themelab_url" in text
    assert "dashboard_url" in text
    assert "shadow_small_entry_ops" in text
    assert "theme_backfill" in text


def test_market_open_live_sim_script_decouples_gateway_orderability_from_runtime_start():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "[int]$GatewayPreflightReadyTimeoutSec = 180" in text
    assert "Kiwoom gateway heartbeat readiness" in text
    assert "Kiwoom gateway orderable readiness" in text
    assert "function Wait-GatewayLiveSimPreflightReady" in text
    assert "Gateway heartbeat ready but order readiness pending" in text
    assert "waiting for LIVE_SIM preflight readiness" in text
    assert "Gateway preflight readiness pending" in text
    assert "Gateway LIVE_SIM preflight readiness reached" in text
    assert "DRY_RUN collection-only mode" in text
    assert "runtime first DRY_RUN collection cycle" in text
    assert "live_sim_startup_policy" in text
    assert "ready_for_orders" in text
    assert "require_orderable" in text
    assert "Get-GatewayStartupDiagnostics" in text
    assert "Gateway readiness retrying after timeout" in text
    assert "Gateway start attempt=" in text


def test_market_open_live_sim_script_configures_websocket_pilot_url():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "$GatewayWsUrl = \"ws://${gatewayHost}:$Port/ws/gateway/transport\"" in text
    assert "$env:TRADING_GATEWAY_WS_URL = $GatewayWsUrl" in text
    assert "gateway_ws_url" in text


def test_market_open_live_sim_script_waits_for_pre_open_data_warmup():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "function Wait-PreOpenDataWarmup" in text
    assert "Pre-open ThemeLab data warmup wait started" in text
    assert "/api/themelab/snapshot?refresh=true" in text
    assert "Pre-open data warmup completed=" in text
    assert "pre_open_data_warmup" in text
