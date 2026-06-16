[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$DbPath = "",
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000,
    [string]$Token = "",
    [int]$CoreStartupTimeoutSec = 45,
    [int]$GatewayStartupTimeoutSec = 90,
    [int]$GatewayStartupRetryCount = 1,
    [int]$GatewayDiagnosticsTailLines = 20,
    [int]$RuntimeStartupTimeoutSec = 120,
    [ValidateSet("rest", "websocket-pilot", "websocket-experimental")]
    [string]$GatewayTransport = "websocket-pilot",
    [string]$GatewayCoreUrl = "",
    [int]$RuntimeDryRunPositionAmount = 30000000,
    [switch]$RequireGatewayOrderable,
    [switch]$SkipGateway,
    [switch]$SkipRuntime,
    [switch]$SkipShadowSmallEntryPreflight,
    [switch]$WaitThemeLabStartupSnapshot,
    [switch]$DisableThemeBackfillWarmup,
    [switch]$SkipPreOpenDataWarmup,
    [int]$ThemeBackfillMaxPerCycle = 6,
    [int]$ThemeBackfillMaxPending = 10,
    [int]$ThemeBackfillTtlSec = 60,
    [int]$ThemeBackfillOpt10001BucketSec = 60,
    [int]$ThemeBackfillOpt10081BucketSec = 1800,
    [int]$ThemeBackfillMaxThemes = 8,
    [int]$ThemeBackfillMaxHitsPerTheme = 8,
    [int]$ThemeBackfillCacheTtlSec = 21600,
    [int]$ThemeBackfillCacheLimit = 500,
    [int]$PreOpenWarmupTimeoutSec = 900,
    [int]$PreOpenWarmupPollSec = 5,
    [int]$PreOpenWarmupMinBackfillSuccessCount = 80,
    [double]$PreOpenWarmupTargetPriceCoveragePct = 60.0,
    [int]$PreOpenWarmupMaxPerCycle = 6,
    [int]$PreOpenWarmupMaxPending = 10,
    [int]$PreOpenWarmupMaxThemes = 8,
    [int]$PreOpenWarmupMaxHitsPerTheme = 90,
    [switch]$AllowLiveSimWithWarnings,
    [switch]$NoStopExisting,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if (-not $ProjectRoot) {
    $ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
} else {
    $ProjectRoot = Resolve-Path $ProjectRoot
}
$ProjectRoot = [string]$ProjectRoot

if (-not $DbPath) {
    $DbPath = Join-Path $ProjectRoot "data\trader.sqlite3"
}
$DbPath = [string](Resolve-Path $DbPath)

if (-not $Token) {
    $Token = if ($env:TRADING_CORE_TOKEN) { $env:TRADING_CORE_TOKEN } else { "local-dev-token" }
}

$ThemeBackfillMaxPerCycle = [Math]::Max(1, $ThemeBackfillMaxPerCycle)
$ThemeBackfillMaxPending = [Math]::Max($ThemeBackfillMaxPerCycle, $ThemeBackfillMaxPending)
$ThemeBackfillTtlSec = [Math]::Max(10, $ThemeBackfillTtlSec)
$ThemeBackfillOpt10001BucketSec = [Math]::Max(30, $ThemeBackfillOpt10001BucketSec)
$ThemeBackfillOpt10081BucketSec = [Math]::Max(300, $ThemeBackfillOpt10081BucketSec)
$ThemeBackfillMaxThemes = [Math]::Max(0, $ThemeBackfillMaxThemes)
$ThemeBackfillMaxHitsPerTheme = [Math]::Max(0, $ThemeBackfillMaxHitsPerTheme)
$ThemeBackfillCacheTtlSec = [Math]::Max(0, $ThemeBackfillCacheTtlSec)
$ThemeBackfillCacheLimit = [Math]::Max(0, $ThemeBackfillCacheLimit)
$PreOpenWarmupTimeoutSec = [Math]::Max(0, $PreOpenWarmupTimeoutSec)
$PreOpenWarmupPollSec = [Math]::Max(1, $PreOpenWarmupPollSec)
$PreOpenWarmupMinBackfillSuccessCount = [Math]::Max(0, $PreOpenWarmupMinBackfillSuccessCount)
$PreOpenWarmupTargetPriceCoveragePct = [Math]::Min(100.0, [Math]::Max(0.0, $PreOpenWarmupTargetPriceCoveragePct))
$PreOpenWarmupMaxPerCycle = [Math]::Max(1, $PreOpenWarmupMaxPerCycle)
$PreOpenWarmupMaxPending = [Math]::Max($PreOpenWarmupMaxPerCycle, $PreOpenWarmupMaxPending)
$PreOpenWarmupMaxThemes = [Math]::Max(0, $PreOpenWarmupMaxThemes)
$PreOpenWarmupMaxHitsPerTheme = [Math]::Max(0, $PreOpenWarmupMaxHitsPerTheme)

$regularOpenAt = (Get-Date).Date.AddHours(9)
$PreOpenDataWarmupEnabled = (-not [bool]$SkipPreOpenDataWarmup) -and (-not [bool]$DisableThemeBackfillWarmup) -and ((Get-Date) -lt $regularOpenAt)
if ($PreOpenDataWarmupEnabled) {
    $ThemeBackfillMaxPerCycle = [Math]::Max($ThemeBackfillMaxPerCycle, $PreOpenWarmupMaxPerCycle)
    $ThemeBackfillMaxPending = [Math]::Max($ThemeBackfillMaxPending, $PreOpenWarmupMaxPending)
    $ThemeBackfillMaxThemes = [Math]::Max($ThemeBackfillMaxThemes, $PreOpenWarmupMaxThemes)
    $ThemeBackfillMaxHitsPerTheme = [Math]::Max($ThemeBackfillMaxHitsPerTheme, $PreOpenWarmupMaxHitsPerTheme)
}

$gatewayHost = if ($BindHost -in @("0.0.0.0", "::", "[::]")) { "127.0.0.1" } else { $BindHost }
if (-not $GatewayCoreUrl) {
    $GatewayCoreUrl = "http://${gatewayHost}:$Port"
}
$GatewayWsUrl = ""
if ($GatewayTransport -ne "rest") {
    $GatewayWsUrl = "ws://${gatewayHost}:$Port/ws/gateway/transport"
}

$Python64 = Join-Path $ProjectRoot "venv_64\Scripts\python.exe"
if (-not (Test-Path $Python64)) {
    throw "64-bit Python runtime not found: $Python64"
}

$LogDir = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$env:TRADING_MODE = "OBSERVE"
$env:TRADING_RUNTIME_ENABLED = "1"
$env:TRADING_RUNTIME_AUTO_START = "0"
$env:TRADING_RUNTIME_MODE = "DRY_RUN"
$env:TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS = "1"
$env:TRADING_RUNTIME_ALLOW_LIVE_ORDERS = "0"
$env:TRADING_RUNTIME_DRY_RUN_POSITION_AMOUNT = [string]$RuntimeDryRunPositionAmount
$env:TRADING_GATEWAY_TRANSPORT = $GatewayTransport
$env:TRADING_KIWOOM_GATEWAY_CORE_URL = $GatewayCoreUrl
if ($GatewayWsUrl) {
    $env:TRADING_GATEWAY_WS_URL = $GatewayWsUrl
}
if ($GatewayTransport -eq "websocket-pilot") {
    $env:TRADING_GATEWAY_WEBSOCKET_REAL_PILOT = "1"
    $env:TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL = "1"
    $env:TRADING_GATEWAY_WEBSOCKET_FALLBACK_TO_REST = "1"
    $env:TRADING_GATEWAY_WEBSOCKET_PILOT_ALLOW_ORDER_COMMANDS = "1"
    $env:TRADING_GATEWAY_WEBSOCKET_PILOT_BLOCK_ORDER_COMMANDS = "0"
}
$env:TRADING_SHADOW_STRATEGY_OBSERVE_ONLY = "1"
$env:TRADING_SHADOW_STRATEGY_ALLOW_APPLY = "0"
$env:TRADING_CHANGE_PROPOSAL_ALLOW_AUTO_APPLY = "0"
$env:TRADING_THEME_BACKFILL_ENABLED = if ($DisableThemeBackfillWarmup) { "0" } else { "1" }
$env:TRADING_THEME_BACKFILL_OBSERVE_ONLY = "1"
$env:TRADING_THEME_BACKFILL_MAX_PER_CYCLE = [string]$ThemeBackfillMaxPerCycle
$env:TRADING_THEME_BACKFILL_MAX_PENDING = [string]$ThemeBackfillMaxPending
$env:TRADING_THEME_BACKFILL_TTL_SEC = [string]$ThemeBackfillTtlSec
$env:TRADING_THEME_BACKFILL_OPT10001_BUCKET_SEC = [string]$ThemeBackfillOpt10001BucketSec
$env:TRADING_THEME_BACKFILL_OPT10081_BUCKET_SEC = [string]$ThemeBackfillOpt10081BucketSec
$env:TRADING_THEME_BACKFILL_ALLOW_OPT10081 = "0"
$env:TRADING_THEME_BACKFILL_ALLOW_REGULAR_SESSION = if ($PreOpenDataWarmupEnabled) { "0" } else { "1" }
$env:TRADING_THEME_BACKFILL_MAX_THEMES = [string]$ThemeBackfillMaxThemes
$env:TRADING_THEME_BACKFILL_MAX_HITS_PER_THEME = [string]$ThemeBackfillMaxHitsPerTheme
$env:TRADING_THEME_BACKFILL_CACHE_ENABLED = "1"
$env:TRADING_THEME_BACKFILL_CACHE_TTL_SEC = [string]$ThemeBackfillCacheTtlSec
$env:TRADING_THEME_BACKFILL_CACHE_LIMIT = [string]$ThemeBackfillCacheLimit
$env:TRADING_CORE_TOKEN = $Token
$env:TRADING_DB_PATH = $DbPath
$env:PYTHONIOENCODING = "utf-8"

function Write-Step([string]$Message) {
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $Message)
}

function Protect-Arg([string]$Value) {
    if ($Value -match '[\s"]') {
        return '"' + ($Value -replace '"', '\"') + '"'
    }
    return $Value
}

function Join-ProcessArgs([string[]]$ArgsList) {
    return (($ArgsList | ForEach-Object { Protect-Arg $_ }) -join " ")
}

function Invoke-CoreApi(
    [ValidateSet("GET", "POST")] [string]$Method,
    [string]$Path,
    [int]$TimeoutSec = 10
) {
    $uri = "http://${BindHost}:$Port$Path"
    $headers = @{
        "X-Local-Token" = $Token
        "Authorization" = "Bearer $Token"
    }
    if ($Method -eq "GET") {
        return Invoke-RestMethod -Method Get -Uri $uri -TimeoutSec $TimeoutSec
    }
    return Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -TimeoutSec $TimeoutSec
}

function Test-CoreHealthy {
    try {
        $health = Invoke-CoreApi -Method GET -Path "/health" -TimeoutSec 2
        return [bool]$health.ok
    } catch {
        return $false
    }
}

function Wait-Until([scriptblock]$Condition, [int]$TimeoutSec, [string]$Label) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    do {
        $result = & $Condition
        if ($result) {
            return $result
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    throw "Timed out waiting for $Label after ${TimeoutSec}s"
}

function Get-FileTailLines([string]$Path, [int]$LineCount) {
    if (-not (Test-Path $Path)) {
        return @()
    }
    try {
        return @(Get-Content -Path $Path -Tail ([Math]::Max(1, $LineCount)) -ErrorAction Stop)
    } catch {
        return @("TAIL_READ_FAILED: $($_.Exception.Message)")
    }
}

function Get-ObjectPropertyValue([object]$Object, [string]$Name) {
    if ($null -eq $Object) {
        return $null
    }
    if ($Object -is [System.Collections.IDictionary]) {
        return $Object[$Name]
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($property) {
        return $property.Value
    }
    return $null
}

function Normalize-BrokerMode([object]$Value) {
    $text = ([string]$Value).Trim().ToUpperInvariant()
    if (-not $text) {
        return "UNKNOWN"
    }
    if (@("1", "SIM", "SIMULATION", "MOCK", "PAPER", "PAPER_TRADING", "LIVE_SIM", "DEMO", "TEST") -contains $text) {
        return "SIMULATION"
    }
    if (@("0", "REAL", "LIVE", "PROD", "PRODUCTION", "LIVE_REAL") -contains $text) {
        return "REAL"
    }
    return "UNKNOWN"
}

function Get-GatewayBrokerModeSummary([object]$Status) {
    $payload = Get-ObjectPropertyValue $Status "last_heartbeat_payload"
    $serverGubun = Get-ObjectPropertyValue $payload "server_gubun"
    $serverGubunText = ""
    if ($null -ne $serverGubun) {
        $serverGubunText = [string]$serverGubun
    }
    [pscustomobject]@{
        broker_env = Normalize-BrokerMode (Get-ObjectPropertyValue $payload "broker_env")
        server_mode = Normalize-BrokerMode (Get-ObjectPropertyValue $payload "server_mode")
        account_mode = Normalize-BrokerMode (Get-ObjectPropertyValue $payload "account_mode")
        server_gubun = $serverGubunText
    }
}

function Test-GatewaySimulationMode([object]$Status) {
    $modes = Get-GatewayBrokerModeSummary $Status
    return (
        $modes.broker_env -eq "SIMULATION" -or
        $modes.server_mode -eq "SIMULATION" -or
        $modes.account_mode -eq "SIMULATION"
    )
}

function Assert-GatewayNotRealMode([object]$Status) {
    $modes = Get-GatewayBrokerModeSummary $Status
    if ($modes.broker_env -eq "REAL" -or $modes.server_mode -eq "REAL" -or $modes.account_mode -eq "REAL") {
        throw "Gateway reported REAL account/server mode; LIVE_SIM startup aborted."
    }
}

function Get-GatewayProcessSnapshot {
    $rows = @(Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -match "python|pythonw") -and ($_.CommandLine -match "apps[\\/]kiwoom_gateway.py|kiwoom_gateway.py")
    })
    return @($rows | ForEach-Object {
        [pscustomobject]@{
            pid = [int]$_.ProcessId
            name = [string]$_.Name
            command_line = [string]$_.CommandLine
        }
    })
}

function Get-GatewayStartupDiagnostics {
    $status = $null
    try {
        $status = Invoke-CoreApi -Method GET -Path "/api/gateway/status" -TimeoutSec 5
    } catch {
        $status = [pscustomobject]@{
            error = $_.Exception.Message
        }
    }
    $stdoutPath = Join-Path $LogDir "kiwoom_gateway_dashboard.out.log"
    $stderrPath = Join-Path $LogDir "kiwoom_gateway_dashboard.err.log"
    [pscustomobject]@{
        status = $status
        processes = @(Get-GatewayProcessSnapshot)
        stdout_path = $stdoutPath
        stderr_path = $stderrPath
        stdout_tail = @(Get-FileTailLines -Path $stdoutPath -LineCount $GatewayDiagnosticsTailLines)
        stderr_tail = @(Get-FileTailLines -Path $stderrPath -LineCount $GatewayDiagnosticsTailLines)
    }
}

function Write-GatewayStartupDiagnostics([object]$Diagnostics) {
    $status = Get-ObjectPropertyValue $Diagnostics "status"
    if ($status) {
        Write-Step ("Gateway diagnostic status state={0} connected={1} heartbeat_ok={2} login={3} orderable={4} heartbeat_age={5} last_error={6}" -f `
            (Get-ObjectPropertyValue $status "connection_state"), `
            (Get-ObjectPropertyValue $status "connected"), `
            (Get-ObjectPropertyValue $status "heartbeat_ok"), `
            (Get-ObjectPropertyValue $status "kiwoom_logged_in"), `
            (Get-ObjectPropertyValue $status "orderable"), `
            (Get-ObjectPropertyValue $status "heartbeat_age_sec"), `
            (Get-ObjectPropertyValue $status "last_error"))
    }
    $processes = @(Get-ObjectPropertyValue $Diagnostics "processes")
    $pids = (($processes | ForEach-Object { [string](Get-ObjectPropertyValue $_ "pid") }) -join ",")
    Write-Step "Gateway diagnostic processes count=$($processes.Count) pids=$pids"
    $stderrTail = @(Get-ObjectPropertyValue $Diagnostics "stderr_tail")
    if ($stderrTail.Count -gt 0) {
        Write-Step "Gateway stderr tail: $($stderrTail -join ' | ')"
    }
    $stdoutTail = @(Get-ObjectPropertyValue $Diagnostics "stdout_tail")
    if ($stdoutTail.Count -gt 0) {
        Write-Step "Gateway stdout tail: $($stdoutTail -join ' | ')"
    }
}

function Get-DescendantProcessIds([int]$RootProcessId) {
    $children = Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $RootProcessId }
    foreach ($child in $children) {
        [int]$child.ProcessId
        Get-DescendantProcessIds -RootProcessId ([int]$child.ProcessId)
    }
}

function Stop-ExistingTradingStack {
    if (Test-CoreHealthy) {
        try {
            Write-Step "Stopping existing runtime loop"
            Invoke-CoreApi -Method POST -Path "/api/runtime/stop" -TimeoutSec 15 | Out-Null
            Start-Sleep -Seconds 2
        } catch {
            Write-Step "Runtime stop request skipped: $($_.Exception.Message)"
        }
    }

    $processIds = New-Object System.Collections.Generic.HashSet[int]
    $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        $listenerProcessId = [int]$listener.OwningProcess
        [void]$processIds.Add($listenerProcessId)
        foreach ($childProcessId in Get-DescendantProcessIds -RootProcessId $listenerProcessId) {
            [void]$processIds.Add([int]$childProcessId)
        }
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerProcessId" -ErrorAction SilentlyContinue
        if ($proc -and $proc.ParentProcessId) {
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ParentProcessId)" -ErrorAction SilentlyContinue
            if ($parent -and ($parent.CommandLine -match "trading_app\.api|apps[\\/]core_api\.py|core_api\.py")) {
                [void]$processIds.Add([int]$parent.ProcessId)
                foreach ($childProcessId in Get-DescendantProcessIds -RootProcessId ([int]$parent.ProcessId)) {
                    [void]$processIds.Add([int]$childProcessId)
                }
            }
        }
    }

    $gatewayProcesses = Get-GatewayProcessSnapshot
    foreach ($gateway in $gatewayProcesses) {
        [void]$processIds.Add([int]$gateway.ProcessId)
    }

    $sortedProcessIds = $processIds.GetEnumerator() | ForEach-Object { [int]$_ } | Sort-Object -Descending
    foreach ($processIdToStop in $sortedProcessIds) {
        $process = Get-Process -Id $processIdToStop -ErrorAction SilentlyContinue
        if ($process) {
            Write-Step "Stopping pid=$processIdToStop name=$($process.ProcessName)"
            Stop-Process -Id $processIdToStop -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 2
}

function Assert-LiveSimDbSettings {
    $script = @'
import json
import sys
from pathlib import Path

from storage.db import TradingDatabase
from trading.strategy.runtime_settings import StrategyRuntimeSettingsRepository

db_path = Path(sys.argv[1])
db = TradingDatabase(str(db_path))
try:
    settings = StrategyRuntimeSettingsRepository(db).load()
    execution = dict(settings.value("order_execution", {}) or {})
    canary = dict(settings.value("live_sim_hybrid_ready_canary", {}) or {})
    result = {
        "db_path": str(db_path),
        "mode": str(execution.get("mode") or ""),
        "live_sim_enabled": bool(execution.get("live_sim_enabled")),
        "live_real_enabled": bool(execution.get("live_real_enabled")),
        "kill_switch_active": bool(execution.get("kill_switch_active")),
        "require_simulated_account": bool(execution.get("require_simulated_account")),
        "allowed_account_mode": str(execution.get("allowed_account_mode") or ""),
        "block_real_account": bool(execution.get("block_real_account")),
        "fail_closed_on_account_unknown": bool(execution.get("fail_closed_on_account_unknown")),
        "allowed_account_numbers_count": len(list(execution.get("allowed_account_numbers") or [])),
        "live_sim_hybrid_ready_canary": {
            "enabled": bool(canary.get("enabled")),
            "order_enabled": bool(canary.get("order_enabled")),
            "max_orders_per_day": int(canary.get("max_orders_per_day") or 0),
            "max_position_amount_krw": int(canary.get("max_position_amount_krw") or 0),
            "position_size_multiplier": float(canary.get("position_size_multiplier") or 0.0),
        },
    }
finally:
    db.close()

print(json.dumps(result, ensure_ascii=False, sort_keys=True))
if (
    result["mode"].upper() != "LIVE_SIM"
    or not result["live_sim_enabled"]
    or result["live_real_enabled"]
    or not result["require_simulated_account"]
    or result["allowed_account_mode"].upper() != "SIMULATION"
    or not result["block_real_account"]
    or not result["fail_closed_on_account_unknown"]
):
    sys.exit(2)
'@
    $output = ($script | & $Python64 - $DbPath) -join "`n"
    $exitCode = $LASTEXITCODE
    if ($output) {
        Write-Step "DB order execution: $output"
    }
    if ($exitCode -ne 0) {
        throw "DB order_execution must be LIVE_SIM with simulation-account guards enabled and live_real_enabled=false."
    }
    return $output | ConvertFrom-Json
}

function Get-ReasonCodeText([object]$Value) {
    $items = @($Value)
    if ($items.Count -eq 0) {
        return ""
    }
    return (($items | ForEach-Object { [string]$_ } | Where-Object { $_ }) -join ",")
}

function Get-LiveSimPreflightStatus {
    return Invoke-CoreApi -Method POST -Path "/api/runtime/live-sim/preflight/rebuild?include_details=true" -TimeoutSec 90
}

function Convert-LiveSimPreflightSummary([object]$Preflight) {
    $account = Get-ObjectPropertyValue $Preflight "account_mode_summary"
    $modes = Get-ObjectPropertyValue $account "normalized_modes"
    $performance = Get-ObjectPropertyValue $Preflight "performance_summary"
    $gatewayLoad = Get-ObjectPropertyValue $Preflight "gateway_load_summary"
    $backfill = Get-ObjectPropertyValue $Preflight "backfill_summary"
    $loadGuard = Get-ObjectPropertyValue $backfill "load_guard"
    $safety = Get-ObjectPropertyValue $Preflight "safety_summary"
    [pscustomobject]@{
        status = [string](Get-ObjectPropertyValue $Preflight "status")
        checked_at = [string](Get-ObjectPropertyValue $Preflight "checked_at")
        account_masked = [string](Get-ObjectPropertyValue $account "account_masked")
        simulation_confirmed = [bool](Get-ObjectPropertyValue $account "simulation_confirmed")
        real_detected = [bool](Get-ObjectPropertyValue $account "real_detected")
        broker_env = [string](Get-ObjectPropertyValue $modes "broker_env")
        account_mode = [string](Get-ObjectPropertyValue $modes "account_mode")
        live_real_enabled = [bool](Get-ObjectPropertyValue $safety "live_real_enabled")
        kill_switch_active = [bool](Get-ObjectPropertyValue $safety "kill_switch_active")
        net_expectancy = Get-ObjectPropertyValue $performance "net_expectancy"
        accepted_completed_lifecycle_count = [int](Get-ObjectPropertyValue $performance "accepted_completed_lifecycle_count")
        bad_ready_rate = Get-ObjectPropertyValue $performance "bad_ready_rate"
        gateway_queue_depth = [int](Get-ObjectPropertyValue $gatewayLoad "gateway_queue_depth")
        order_command_pending_count = [int](Get-ObjectPropertyValue $gatewayLoad "order_command_pending_count")
        backfill_pending_count = [int](Get-ObjectPropertyValue $gatewayLoad "backfill_pending_count")
        load_guard_status = [string](Get-ObjectPropertyValue $loadGuard "load_guard_status")
        tr_backfill_caused_ready_count = [int](Get-ObjectPropertyValue $backfill "tr_backfill_caused_ready_count")
        blocking_reasons = Get-ReasonCodeText (Get-ObjectPropertyValue $Preflight "blocking_reasons")
        warning_reasons = Get-ReasonCodeText (Get-ObjectPropertyValue $Preflight "warning_reasons")
        operator_message_ko = [string](Get-ObjectPropertyValue $Preflight "operator_message_ko")
        recommended_action_ko = [string](Get-ObjectPropertyValue $Preflight "recommended_action_ko")
    }
}

function Write-LiveSimPreflightSummary([object]$Preflight) {
    $summary = Convert-LiveSimPreflightSummary $Preflight
    Write-Step ("LIVE_SIM preflight status={0} account={1} sim={2} broker_env={3} account_mode={4} live_real={5} kill_switch={6}" -f `
        $summary.status, $summary.account_masked, $summary.simulation_confirmed, $summary.broker_env, $summary.account_mode, $summary.live_real_enabled, $summary.kill_switch_active)
    Write-Step ("LIVE_SIM preflight performance net={0} accepted={1} bad_ready={2} queue={3} order_pending={4} backfill_pending={5} load_guard={6}" -f `
        $summary.net_expectancy, $summary.accepted_completed_lifecycle_count, $summary.bad_ready_rate, $summary.gateway_queue_depth, $summary.order_command_pending_count, $summary.backfill_pending_count, $summary.load_guard_status)
    if ($summary.blocking_reasons) {
        Write-Step "LIVE_SIM preflight blocking=$($summary.blocking_reasons)"
    }
    if ($summary.warning_reasons) {
        Write-Step "LIVE_SIM preflight warnings=$($summary.warning_reasons)"
    }
    Write-Step "LIVE_SIM preflight message=$($summary.operator_message_ko)"
    Write-Step "LIVE_SIM preflight action=$($summary.recommended_action_ko)"
    return $summary
}

function Write-LiveSimCanaryStartupSummary([object]$DbSettings, [object]$PreflightSummary) {
    $canary = Get-ObjectPropertyValue $DbSettings "live_sim_hybrid_ready_canary"
    $preflightStatus = if ($PreflightSummary) { [string](Get-ObjectPropertyValue $PreflightSummary "status") } else { "SKIPPED" }
    $enabled = [bool](Get-ObjectPropertyValue $canary "enabled")
    $orderEnabled = [bool](Get-ObjectPropertyValue $canary "order_enabled")
    $maxOrdersPerDay = [int](Get-ObjectPropertyValue $canary "max_orders_per_day")
    $maxPositionAmount = [int](Get-ObjectPropertyValue $canary "max_position_amount_krw")
    $multiplier = [double](Get-ObjectPropertyValue $canary "position_size_multiplier")
    Write-Step ("LIVE_SIM Canary enabled={0} order_enabled={1} max_orders_per_day={2} max_position_amount_krw={3} position_size_multiplier={4} preflight={5}" -f `
        $enabled, $orderEnabled, $maxOrdersPerDay, $maxPositionAmount, $multiplier, $preflightStatus)
    if ($preflightStatus.Trim().ToUpperInvariant() -ne "GO") {
        Write-Step "LIVE_SIM Canary orders unavailable: Preflight status is not GO."
    }
    if (-not $orderEnabled) {
        Write-Step "LIVE_SIM Canary order_enabled=false; 판단만 기록하고 주문은 생성하지 않습니다."
    }
}

function Assert-LiveSimPreflightAllowsStartup([object]$Preflight) {
    $status = ([string](Get-ObjectPropertyValue $Preflight "status")).Trim().ToUpperInvariant()
    $blockingText = Get-ReasonCodeText (Get-ObjectPropertyValue $Preflight "blocking_reasons")
    $warningText = Get-ReasonCodeText (Get-ObjectPropertyValue $Preflight "warning_reasons")
    $reasonText = "$blockingText,$warningText"
    if ($status -eq "FAIL_CLOSED" -or $reasonText -match "REAL|UNKNOWN_FAIL_CLOSED|LIVE_REAL|ALLOWED_ACCOUNT_MISMATCH") {
        throw "LIVE_SIM preflight fail-closed: REAL/UNKNOWN/LIVE_REAL/account mismatch 위험은 -AllowLiveSimWithWarnings로 우회할 수 없습니다. reasons=$reasonText"
    }
    if ($status -eq "GO") {
        return
    }
    if ($status -eq "GO_WITH_WARNINGS") {
        if ($AllowLiveSimWithWarnings) {
            Write-Step "LIVE_SIM preflight GO_WITH_WARNINGS accepted by explicit -AllowLiveSimWithWarnings"
            return
        }
        throw "LIVE_SIM preflight returned GO_WITH_WARNINGS. 경고 상태로 진행하려면 -AllowLiveSimWithWarnings를 명시해야 합니다. warnings=$warningText"
    }
    throw "LIVE_SIM preflight blocked runtime startup. status=$status reasons=$reasonText"
}

function Get-ThemeLabStartupSummary {
    try {
        $snapshot = Invoke-CoreApi -Method GET -Path "/api/themelab/snapshot" -TimeoutSec 20
        $summary = Get-ObjectPropertyValue $snapshot "summary"
        $operatorView = Get-ObjectPropertyValue $snapshot "operator_view"
        $mainAction = Get-ObjectPropertyValue $operatorView "main_action"
        $themeBackfill = Get-ObjectPropertyValue $snapshot "theme_backfill_runtime"
        [pscustomobject]@{
            available = $true
            operation_status = [string](Get-ObjectPropertyValue $summary "operation_status")
            operation_message = [string](Get-ObjectPropertyValue $summary "operation_message")
            ready_count = [int](Get-ObjectPropertyValue $summary "ready_count")
            ready_small_entry_count = [int](Get-ObjectPropertyValue $summary "ready_small_entry_count")
            order_candidate_count = [int](Get-ObjectPropertyValue $summary "order_candidate_count")
            main_action = [pscustomobject]@{
                status = [string](Get-ObjectPropertyValue $mainAction "status")
                label_ko = [string](Get-ObjectPropertyValue $mainAction "label_ko")
                message_ko = [string](Get-ObjectPropertyValue $mainAction "message_ko")
            }
            theme_backfill = [pscustomobject]@{
                enabled = [bool](Get-ObjectPropertyValue $themeBackfill "enabled")
                observe_only = [bool](Get-ObjectPropertyValue $themeBackfill "observe_only")
                tr_backfill_caused_ready_count = [int](Get-ObjectPropertyValue $themeBackfill "tr_backfill_caused_ready_count")
            }
        }
    } catch {
        [pscustomobject]@{
            available = $false
            error = $_.Exception.Message
        }
    }
}

function Get-ThemeLabStartupSummarySkipped {
    [pscustomobject]@{
        available = $false
        skipped = $true
        reason = "STARTUP_SNAPSHOT_DECOUPLED"
        operation_status = "SKIPPED"
        operation_message = "ThemeLab startup snapshot is decoupled from LIVE_SIM readiness. Use -WaitThemeLabStartupSnapshot to wait for it."
        theme_backfill = [pscustomobject]@{
            enabled = [string]$env:TRADING_THEME_BACKFILL_ENABLED -eq "1"
            observe_only = [string]$env:TRADING_THEME_BACKFILL_OBSERVE_ONLY -eq "1"
            max_per_cycle = [int]$env:TRADING_THEME_BACKFILL_MAX_PER_CYCLE
            max_pending = [int]$env:TRADING_THEME_BACKFILL_MAX_PENDING
            ttl_sec = [int]$env:TRADING_THEME_BACKFILL_TTL_SEC
            opt10001_bucket_sec = [int]$env:TRADING_THEME_BACKFILL_OPT10001_BUCKET_SEC
            opt10081_bucket_sec = [int]$env:TRADING_THEME_BACKFILL_OPT10081_BUCKET_SEC
            allow_opt10081 = [string]$env:TRADING_THEME_BACKFILL_ALLOW_OPT10081 -eq "1"
            allow_regular_session = [string]$env:TRADING_THEME_BACKFILL_ALLOW_REGULAR_SESSION -eq "1"
            max_themes = [int]$env:TRADING_THEME_BACKFILL_MAX_THEMES
            max_hits_per_theme = [int]$env:TRADING_THEME_BACKFILL_MAX_HITS_PER_THEME
            cache_enabled = [string]$env:TRADING_THEME_BACKFILL_CACHE_ENABLED -eq "1"
            cache_ttl_sec = [int]$env:TRADING_THEME_BACKFILL_CACHE_TTL_SEC
            cache_limit = [int]$env:TRADING_THEME_BACKFILL_CACHE_LIMIT
        }
    }
}

function Get-ShadowSmallEntryStartupStatus {
    $statusPayload = $null
    $preflightPayload = $null
    try {
        $statusPayload = Invoke-CoreApi -Method GET -Path "/api/shadow-small-entry-ops/status" -TimeoutSec 20
        if (-not $SkipShadowSmallEntryPreflight) {
            try {
                $preflightPayload = Invoke-CoreApi -Method POST -Path "/api/shadow-small-entry-ops/preflight" -TimeoutSec 30
            } catch {
                $preflightPayload = [pscustomobject]@{
                    status = "ERROR"
                    blocking_reasons = @($_.Exception.Message)
                }
            }
        }
        [pscustomobject]@{
            available = $true
            status = [string](Get-ObjectPropertyValue $statusPayload "status")
            mode = [string](Get-ObjectPropertyValue $statusPayload "mode")
            order_enabled = [bool](Get-ObjectPropertyValue $statusPayload "order_enabled")
            preflight_status = [string](Get-ObjectPropertyValue $statusPayload "preflight_status")
            preflight_blocking_reasons = @(Get-ObjectPropertyValue $statusPayload "preflight_blocking_reasons")
            operator_message_ko = [string](Get-ObjectPropertyValue $statusPayload "operator_message_ko")
            daily_usage = Get-ObjectPropertyValue $statusPayload "today"
            limits = Get-ObjectPropertyValue $statusPayload "limits"
            audit = Get-ObjectPropertyValue $statusPayload "audit"
            startup_preflight = $preflightPayload
        }
    } catch {
        [pscustomobject]@{
            available = $false
            error = $_.Exception.Message
        }
    }
}

function Convert-ToIntOrZero([object]$Value) {
    if ($null -eq $Value) {
        return 0
    }
    $text = ([string]$Value).Trim()
    if (-not $text) {
        return 0
    }
    try {
        return [int]([double]$text)
    } catch {
        return 0
    }
}

function Convert-ToNullableDouble([object]$Value) {
    if ($null -eq $Value) {
        return $null
    }
    $text = ([string]$Value).Trim()
    if (-not $text) {
        return $null
    }
    try {
        return [double]$text
    } catch {
        return $null
    }
}

function Get-RegularOpenAt {
    return (Get-Date).Date.AddHours(9)
}

function Test-BeforeRegularOpen {
    return (Get-Date) -lt (Get-RegularOpenAt)
}

function Get-PreOpenDataWarmupSummary {
    try {
        $snapshot = Invoke-CoreApi -Method GET -Path "/api/themelab/snapshot?refresh=true" -TimeoutSec 30
        $summary = Get-ObjectPropertyValue $snapshot "summary"
        $dataQuality = Get-ObjectPropertyValue $snapshot "data_quality"
        $backfill = Get-ObjectPropertyValue $snapshot "theme_backfill_runtime"
        $coverageRatio = Convert-ToNullableDouble (Get-ObjectPropertyValue $dataQuality "theme_rank_min_price_coverage_ratio")
        $coveragePct = $null
        if ($null -ne $coverageRatio) {
            $coveragePct = [Math]::Round($coverageRatio * 100.0, 2)
        }
        $successCount = [Math]::Max(
            (Convert-ToIntOrZero (Get-ObjectPropertyValue $backfill "success_count")),
            (Convert-ToIntOrZero (Get-ObjectPropertyValue $backfill "theme_backfill_success_count"))
        )
        $queuedCount = Convert-ToIntOrZero (Get-ObjectPropertyValue $backfill "queued_count")
        $dispatchedCount = Convert-ToIntOrZero (Get-ObjectPropertyValue $backfill "dispatched_count")
        $activeCount = $queuedCount + $dispatchedCount
        $candidateCount = [Math]::Max(
            (Convert-ToIntOrZero (Get-ObjectPropertyValue $backfill "candidate_count")),
            (Convert-ToIntOrZero (Get-ObjectPropertyValue $backfill "theme_backfill_candidate_count"))
        )
        $status = [string](Get-ObjectPropertyValue $dataQuality "status")
        $coverageReady = $false
        if ($null -ne $coveragePct) {
            $coverageReady = $coveragePct -ge $PreOpenWarmupTargetPriceCoveragePct
        }
        $successReady = $successCount -ge $PreOpenWarmupMinBackfillSuccessCount
        $noBackfillNeeded = $candidateCount -eq 0 -and $activeCount -eq 0 -and $status.ToUpperInvariant() -notin @("BROKEN", "DEGRADED")
        [pscustomobject]@{
            available = $true
            completed = [bool](($coverageReady -or $successReady -or $noBackfillNeeded) -and $activeCount -eq 0)
            status = $status
            operation_status = [string](Get-ObjectPropertyValue $summary "operation_status")
            ready_count = Convert-ToIntOrZero (Get-ObjectPropertyValue $summary "ready_count")
            order_candidate_count = Convert-ToIntOrZero (Get-ObjectPropertyValue $summary "order_candidate_count")
            price_coverage_pct = $coveragePct
            target_price_coverage_pct = $PreOpenWarmupTargetPriceCoveragePct
            backfill_success_count = $successCount
            min_backfill_success_count = $PreOpenWarmupMinBackfillSuccessCount
            backfill_candidate_count = $candidateCount
            backfill_queued_count = $queuedCount
            backfill_dispatched_count = $dispatchedCount
            backfill_active_count = $activeCount
            paused_reason = [string](Get-ObjectPropertyValue $backfill "paused_reason")
            message = [string](Get-ObjectPropertyValue $dataQuality "message")
            coverage_ready = [bool]$coverageReady
            success_ready = [bool]$successReady
            no_backfill_needed = [bool]$noBackfillNeeded
        }
    } catch {
        [pscustomobject]@{
            available = $false
            completed = $false
            error = $_.Exception.Message
        }
    }
}

function Wait-PreOpenDataWarmup {
    if (-not $PreOpenDataWarmupEnabled) {
        return [pscustomobject]@{
            enabled = $false
            skipped = $true
            reason = if ([bool]$SkipPreOpenDataWarmup) { "SKIP_PRE_OPEN_DATA_WARMUP" } elseif ([bool]$DisableThemeBackfillWarmup) { "THEME_BACKFILL_DISABLED" } else { "NOT_BEFORE_REGULAR_OPEN" }
            completed = $false
        }
    }
    if ($PreOpenWarmupTimeoutSec -le 0) {
        return [pscustomobject]@{
            enabled = $true
            skipped = $true
            reason = "TIMEOUT_DISABLED"
            completed = $false
        }
    }

    $deadline = (Get-Date).AddSeconds($PreOpenWarmupTimeoutSec)
    $last = $null
    Write-Step "Pre-open ThemeLab data warmup wait started timeout=${PreOpenWarmupTimeoutSec}s target_price_coverage=${PreOpenWarmupTargetPriceCoveragePct}% min_backfill_success=$PreOpenWarmupMinBackfillSuccessCount"
    while ((Get-Date) -lt $deadline -and (Test-BeforeRegularOpen)) {
        $last = Get-PreOpenDataWarmupSummary
        if ($last.available) {
            Write-Step ("Pre-open warmup status={0} coverage={1}% success={2}/{3} active={4} candidates={5} paused={6}" -f `
                $last.status, `
                $(if ($null -ne $last.price_coverage_pct) { $last.price_coverage_pct } else { "n/a" }), `
                $last.backfill_success_count, `
                $last.min_backfill_success_count, `
                $last.backfill_active_count, `
                $last.backfill_candidate_count, `
                $last.paused_reason)
            if ($last.completed) {
                $last | Add-Member -NotePropertyName enabled -NotePropertyValue $true -Force
                $last | Add-Member -NotePropertyName stop_reason -NotePropertyValue "TARGET_REACHED" -Force
                return $last
            }
        } else {
            Write-Step "Pre-open warmup snapshot unavailable: $($last.error)"
        }
        Start-Sleep -Seconds $PreOpenWarmupPollSec
    }

    if ($null -eq $last) {
        $last = Get-PreOpenDataWarmupSummary
    }
    $stopReason = if (-not (Test-BeforeRegularOpen)) { "REGULAR_OPEN_REACHED" } else { "TIMEOUT" }
    $last | Add-Member -NotePropertyName enabled -NotePropertyValue $true -Force
    $last | Add-Member -NotePropertyName stop_reason -NotePropertyValue $stopReason -Force
    return $last
}

Push-Location $ProjectRoot
try {
    Write-Step "Market open LIVE_SIM startup settings"
    Write-Step "Core TRADING_MODE=$env:TRADING_MODE"
    Write-Step "Runtime TRADING_RUNTIME_ENABLED=$env:TRADING_RUNTIME_ENABLED auto_start=$env:TRADING_RUNTIME_AUTO_START"
    Write-Step "Runtime TRADING_RUNTIME_MODE=$env:TRADING_RUNTIME_MODE"
    Write-Step "Runtime TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS=$env:TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS"
    Write-Step "Runtime TRADING_RUNTIME_ALLOW_LIVE_ORDERS=$env:TRADING_RUNTIME_ALLOW_LIVE_ORDERS"
    Write-Step "Runtime TRADING_RUNTIME_DRY_RUN_POSITION_AMOUNT=$env:TRADING_RUNTIME_DRY_RUN_POSITION_AMOUNT"
    Write-Step "Gateway transport=$env:TRADING_GATEWAY_TRANSPORT"
    Write-Step "Gateway readiness require_orderable=$([bool]$RequireGatewayOrderable)"
    Write-Step "Gateway startup retries=$GatewayStartupRetryCount"
    Write-Step "Gateway core URL=$env:TRADING_KIWOOM_GATEWAY_CORE_URL"
    Write-Step "LIVE_SIM preflight allow_warnings=$([bool]$AllowLiveSimWithWarnings)"
    if ($env:TRADING_GATEWAY_WS_URL) {
        Write-Step "Gateway WebSocket URL=$env:TRADING_GATEWAY_WS_URL"
    }
    Write-Step "Shadow strategy observe_only=$env:TRADING_SHADOW_STRATEGY_OBSERVE_ONLY allow_apply=$env:TRADING_SHADOW_STRATEGY_ALLOW_APPLY"
    Write-Step "Change proposal auto_apply=$env:TRADING_CHANGE_PROPOSAL_ALLOW_AUTO_APPLY"
    Write-Step "Theme backfill enabled=$env:TRADING_THEME_BACKFILL_ENABLED observe_only=$env:TRADING_THEME_BACKFILL_OBSERVE_ONLY max_per_cycle=$env:TRADING_THEME_BACKFILL_MAX_PER_CYCLE max_pending=$env:TRADING_THEME_BACKFILL_MAX_PENDING ttl=$env:TRADING_THEME_BACKFILL_TTL_SEC opt10001_bucket=$env:TRADING_THEME_BACKFILL_OPT10001_BUCKET_SEC max_themes=$env:TRADING_THEME_BACKFILL_MAX_THEMES max_hits_per_theme=$env:TRADING_THEME_BACKFILL_MAX_HITS_PER_THEME cache_ttl=$env:TRADING_THEME_BACKFILL_CACHE_TTL_SEC"
    Write-Step "Pre-open data warmup enabled=$PreOpenDataWarmupEnabled skip=$([bool]$SkipPreOpenDataWarmup) timeout=$PreOpenWarmupTimeoutSec poll=$PreOpenWarmupPollSec target_coverage_pct=$PreOpenWarmupTargetPriceCoveragePct min_success=$PreOpenWarmupMinBackfillSuccessCount allow_regular_session=$env:TRADING_THEME_BACKFILL_ALLOW_REGULAR_SESSION"
    if (-not $WaitThemeLabStartupSnapshot) {
        Write-Step "ThemeLab startup snapshot wait=disabled; LIVE_SIM readiness will not wait for ThemeLab dashboard backfill diagnostics"
    }
    if ($GatewayTransport -eq "websocket-pilot") {
        Write-Step "WebSocket pilot order commands allow=$env:TRADING_GATEWAY_WEBSOCKET_PILOT_ALLOW_ORDER_COMMANDS block=$env:TRADING_GATEWAY_WEBSOCKET_PILOT_BLOCK_ORDER_COMMANDS"
    }

    $dbSettings = Assert-LiveSimDbSettings

    if ($DryRun) {
        Write-Step "DryRun requested; startup commands were not executed"
        [pscustomobject]@{
            dry_run = $true
            project_root = $ProjectRoot
            db_path = $DbPath
            host = $BindHost
            port = $Port
            dashboard_url = "http://${BindHost}:$Port/"
            themelab_url = "http://${BindHost}:$Port/themelab"
            trading_mode = $env:TRADING_MODE
            runtime_enabled = $env:TRADING_RUNTIME_ENABLED
            runtime_auto_start = $env:TRADING_RUNTIME_AUTO_START
            runtime_mode = $env:TRADING_RUNTIME_MODE
            runtime_allow_dry_run_orders = $env:TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS
            runtime_allow_live_orders = $env:TRADING_RUNTIME_ALLOW_LIVE_ORDERS
            runtime_dry_run_position_amount = $env:TRADING_RUNTIME_DRY_RUN_POSITION_AMOUNT
            gateway_transport = $env:TRADING_GATEWAY_TRANSPORT
            gateway_core_url = $env:TRADING_KIWOOM_GATEWAY_CORE_URL
            gateway_ws_url = $env:TRADING_GATEWAY_WS_URL
            gateway_require_orderable = [bool]$RequireGatewayOrderable
            gateway_startup_retry_count = $GatewayStartupRetryCount
            live_sim_preflight = @{
                enabled = $true
                dry_run_note = "Core/Gateway startup was not executed, so API preflight rebuild was not called."
                allow_warnings = [bool]$AllowLiveSimWithWarnings
            }
            websocket_pilot_order_allow = $env:TRADING_GATEWAY_WEBSOCKET_PILOT_ALLOW_ORDER_COMMANDS
            websocket_pilot_order_block = $env:TRADING_GATEWAY_WEBSOCKET_PILOT_BLOCK_ORDER_COMMANDS
            shadow_strategy_observe_only = $env:TRADING_SHADOW_STRATEGY_OBSERVE_ONLY
            shadow_strategy_allow_apply = $env:TRADING_SHADOW_STRATEGY_ALLOW_APPLY
            change_proposal_allow_auto_apply = $env:TRADING_CHANGE_PROPOSAL_ALLOW_AUTO_APPLY
            theme_backfill_enabled = $env:TRADING_THEME_BACKFILL_ENABLED
            theme_backfill_observe_only = $env:TRADING_THEME_BACKFILL_OBSERVE_ONLY
            theme_backfill_max_per_cycle = $env:TRADING_THEME_BACKFILL_MAX_PER_CYCLE
            theme_backfill_max_pending = $env:TRADING_THEME_BACKFILL_MAX_PENDING
            theme_backfill_ttl_sec = $env:TRADING_THEME_BACKFILL_TTL_SEC
            theme_backfill_opt10001_bucket_sec = $env:TRADING_THEME_BACKFILL_OPT10001_BUCKET_SEC
            theme_backfill_opt10081_bucket_sec = $env:TRADING_THEME_BACKFILL_OPT10081_BUCKET_SEC
            theme_backfill_allow_opt10081 = $env:TRADING_THEME_BACKFILL_ALLOW_OPT10081
            theme_backfill_allow_regular_session = $env:TRADING_THEME_BACKFILL_ALLOW_REGULAR_SESSION
            theme_backfill_max_themes = $env:TRADING_THEME_BACKFILL_MAX_THEMES
            theme_backfill_max_hits_per_theme = $env:TRADING_THEME_BACKFILL_MAX_HITS_PER_THEME
            theme_backfill_cache_enabled = $env:TRADING_THEME_BACKFILL_CACHE_ENABLED
            theme_backfill_cache_ttl_sec = $env:TRADING_THEME_BACKFILL_CACHE_TTL_SEC
            theme_backfill_cache_limit = $env:TRADING_THEME_BACKFILL_CACHE_LIMIT
            pre_open_data_warmup = @{
                enabled = [bool]$PreOpenDataWarmupEnabled
                skip = [bool]$SkipPreOpenDataWarmup
                timeout_sec = $PreOpenWarmupTimeoutSec
                poll_sec = $PreOpenWarmupPollSec
                min_backfill_success_count = $PreOpenWarmupMinBackfillSuccessCount
                target_price_coverage_pct = $PreOpenWarmupTargetPriceCoveragePct
                max_per_cycle = $PreOpenWarmupMaxPerCycle
                max_pending = $PreOpenWarmupMaxPending
                max_themes = $PreOpenWarmupMaxThemes
                max_hits_per_theme = $PreOpenWarmupMaxHitsPerTheme
            }
            themelab_startup_snapshot = if ($WaitThemeLabStartupSnapshot) { "wait" } else { "decoupled" }
            shadow_small_entry_preflight = if ($SkipShadowSmallEntryPreflight) { "skipped" } else { "enabled" }
            db_order_execution = $dbSettings
        } | ConvertTo-Json -Depth 6
        return
    }

    if (-not $NoStopExisting) {
        Stop-ExistingTradingStack
    }

    $existingListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($existingListener) {
        throw "Port $Port is still in use. Re-run with a free port or stop the existing listener."
    }

    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $coreOutLog = Join-Path $LogDir "market_open_core_$stamp.out.log"
    $coreErrLog = Join-Path $LogDir "market_open_core_$stamp.err.log"
    $coreArgs = @(
        "apps\core_api.py",
        "--host", $BindHost,
        "--port", [string]$Port,
        "--db", $DbPath,
        "--token", $Token,
        "--mode", "OBSERVE"
    )
    $coreProcess = Start-Process `
        -FilePath $Python64 `
        -ArgumentList (Join-ProcessArgs $coreArgs) `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $coreOutLog `
        -RedirectStandardError $coreErrLog `
        -PassThru
    Write-Step "Started core API parent pid=$($coreProcess.Id)"

    Wait-Until -TimeoutSec $CoreStartupTimeoutSec -Label "core API health" -Condition {
        if (Test-CoreHealthy) { return @{ ok = $true } }
        return $null
    } | Out-Null
    Write-Step "Core API healthy"

    $gatewayStatus = $null
    $gatewayModes = $null
    if (-not $SkipGateway) {
        $gatewayWaitLabel = if ($RequireGatewayOrderable) { "Kiwoom gateway orderable readiness" } else { "Kiwoom gateway heartbeat readiness" }
        $maxGatewayAttempts = 1 + [Math]::Max(0, $GatewayStartupRetryCount)
        for ($gatewayAttempt = 1; $gatewayAttempt -le $maxGatewayAttempts; $gatewayAttempt++) {
            $gatewayStart = Invoke-CoreApi -Method POST -Path "/api/gateway/kiwoom/start" -TimeoutSec 20
            Write-Step "Gateway start attempt=$gatewayAttempt/$maxGatewayAttempts reason=$($gatewayStart.reason)"
            if ($gatewayStart.stale_recovery -and ($gatewayStart.stale_recovery.stale -or $gatewayStart.stale_recovery.orphan)) {
                $stoppedCount = @($gatewayStart.stale_recovery.stopped_processes).Count
                $remainingCount = @($gatewayStart.stale_recovery.remaining_processes).Count
                Write-Step "Gateway stale recovery stopped=$stoppedCount remaining=$remainingCount stale=$($gatewayStart.stale_recovery.stale) orphan=$($gatewayStart.stale_recovery.orphan) state=$($gatewayStart.gateway.connection_state) heartbeat_age=$($gatewayStart.gateway.heartbeat_age_sec)"
            }
            try {
                $gatewayStatus = Wait-Until -TimeoutSec $GatewayStartupTimeoutSec -Label $gatewayWaitLabel -Condition {
                    $status = $null
                    try {
                        $status = Invoke-CoreApi -Method GET -Path "/api/gateway/status" -TimeoutSec 5
                    } catch {
                        return $null
                    }
                    if ($status.connected -and $status.heartbeat_ok) {
                        Assert-GatewayNotRealMode $status
                        if ($RequireGatewayOrderable) {
                            if ($status.kiwoom_logged_in -and $status.orderable -and (Test-GatewaySimulationMode $status)) {
                                return $status
                            }
                            return $null
                        }
                        return $status
                    }
                    return $null
                }
                break
            } catch {
                $diagnostics = Get-GatewayStartupDiagnostics
                Write-GatewayStartupDiagnostics $diagnostics
                if ($gatewayAttempt -ge $maxGatewayAttempts) {
                    throw
                }
                Write-Step "Gateway readiness retrying after timeout; requesting gateway restart"
            }
        }
        $gatewayModes = Get-GatewayBrokerModeSummary $gatewayStatus
        $gatewayReadyForOrders = [bool]($gatewayStatus.kiwoom_logged_in -and $gatewayStatus.orderable -and (Test-GatewaySimulationMode $gatewayStatus))
        if (-not $gatewayReadyForOrders -and -not $RequireGatewayOrderable) {
            Write-Step "Gateway heartbeat ready but order readiness pending; runtime will start in guarded DRY_RUN mode login=$($gatewayStatus.kiwoom_logged_in) orderable=$($gatewayStatus.orderable) broker_env=$($gatewayModes.broker_env) account_mode=$($gatewayModes.account_mode)"
        }
        Write-Step "Gateway ready connected=$($gatewayStatus.connected) heartbeat_ok=$($gatewayStatus.heartbeat_ok) orderable=$($gatewayStatus.orderable) broker_env=$($gatewayModes.broker_env) account_mode=$($gatewayModes.account_mode)"
    }

    $liveSimPreflightSummary = $null
    if (-not $SkipRuntime) {
        $liveSimPreflight = Get-LiveSimPreflightStatus
        $liveSimPreflightSummary = Write-LiveSimPreflightSummary $liveSimPreflight
        Assert-LiveSimPreflightAllowsStartup $liveSimPreflight
        Write-Step "LIVE_SIM preflight passed; runtime startup may proceed"
    } else {
        Write-Step "LIVE_SIM preflight skipped because runtime startup is skipped"
    }
    Write-LiveSimCanaryStartupSummary $dbSettings $liveSimPreflightSummary

    $runtimeStatus = $null
    if (-not $SkipRuntime) {
        $beforeRuntime = Invoke-CoreApi -Method GET -Path "/api/runtime/status" -TimeoutSec 10
        $startCycle = [int]($beforeRuntime.cycle_count)
        $runtimeStart = Invoke-CoreApi -Method POST -Path "/api/runtime/start" -TimeoutSec 20
        Write-Step "Runtime start running=$($runtimeStart.running) mode=$($runtimeStart.mode)"
        $runtimeStatus = Wait-Until -TimeoutSec $RuntimeStartupTimeoutSec -Label "runtime first LIVE_SIM-ready cycle" -Condition {
            try {
                $status = Invoke-CoreApi -Method GET -Path "/api/runtime/status" -TimeoutSec 8
                $snapshot = $status.latest_snapshot
                $liveSimReady = [bool]($snapshot.live_sim_order_sink_enabled) -or [string]($snapshot.live_sim_order_policy) -eq "LIVE_SIM_FIRST_LEG_GUARDED"
                if ($status.running -and [string]$status.mode -eq "DRY_RUN" -and [int]$status.cycle_count -gt $startCycle -and $liveSimReady) {
                    return $status
                }
            } catch {
                return $null
            }
            return $null
        }
        Write-Step "Runtime ready mode=$($runtimeStatus.mode) cycles=$($runtimeStatus.cycle_count)"
    }

    if (-not $SkipRuntime) {
        $preOpenDataWarmupStatus = Wait-PreOpenDataWarmup
        if ($preOpenDataWarmupStatus.enabled -and -not $preOpenDataWarmupStatus.skipped) {
            Write-Step "Pre-open data warmup completed=$($preOpenDataWarmupStatus.completed) stop_reason=$($preOpenDataWarmupStatus.stop_reason) coverage=$($preOpenDataWarmupStatus.price_coverage_pct)% success=$($preOpenDataWarmupStatus.backfill_success_count) active=$($preOpenDataWarmupStatus.backfill_active_count)"
        } elseif ($preOpenDataWarmupStatus.skipped) {
            Write-Step "Pre-open data warmup skipped: $($preOpenDataWarmupStatus.reason)"
        }
        Write-Step "LIVE_SIM readiness reached; ThemeLab backfill warmed or bounded by startup policy"
    } else {
        $preOpenDataWarmupStatus = [pscustomobject]@{
            enabled = [bool]$PreOpenDataWarmupEnabled
            skipped = $true
            reason = "RUNTIME_SKIPPED"
            completed = $false
        }
        Write-Step "Runtime startup skipped; ThemeLab backfill will not run until runtime starts"
    }
    $themeLabStatus = if ($WaitThemeLabStartupSnapshot) { Get-ThemeLabStartupSummary } else { Get-ThemeLabStartupSummarySkipped }
    $shadowSmallEntryStatus = Get-ShadowSmallEntryStartupStatus
    if ($themeLabStatus.available) {
        Write-Step "ThemeLab status=$($themeLabStatus.operation_status) action=$($themeLabStatus.main_action.label_ko)"
    } elseif ($themeLabStatus.skipped) {
        Write-Step "ThemeLab startup snapshot skipped: $($themeLabStatus.reason)"
    } else {
        Write-Step "ThemeLab status unavailable: $($themeLabStatus.error)"
    }
    if ($shadowSmallEntryStatus.available) {
        Write-Step "Shadow Small Entry status=$($shadowSmallEntryStatus.status) mode=$($shadowSmallEntryStatus.mode) order_enabled=$($shadowSmallEntryStatus.order_enabled) preflight=$($shadowSmallEntryStatus.preflight_status)"
    } else {
        Write-Step "Shadow Small Entry status unavailable: $($shadowSmallEntryStatus.error)"
    }

    [pscustomobject]@{
        started = $true
        core = @{
            pid = $coreProcess.Id
            url = "http://${BindHost}:$Port"
            stdout = $coreOutLog
            stderr = $coreErrLog
            trading_mode = "OBSERVE"
        }
        ui = @{
            dashboard_url = "http://${BindHost}:$Port/"
            themelab_url = "http://${BindHost}:$Port/themelab"
        }
        runtime = if ($runtimeStatus) {
            @{
                running = [bool]$runtimeStatus.running
                mode = [string]$runtimeStatus.mode
                order_policy = [string]$runtimeStatus.order_policy
                cycle_count = [int]$runtimeStatus.cycle_count
                last_error = [string]$runtimeStatus.last_error
                live_sim_order_sink_enabled = [bool]$runtimeStatus.latest_snapshot.live_sim_order_sink_enabled
                live_sim_order_policy = [string]$runtimeStatus.latest_snapshot.live_sim_order_policy
            }
        } else { $null }
        gateway = if ($gatewayStatus) {
            @{
                connected = [bool]$gatewayStatus.connected
                heartbeat_ok = [bool]$gatewayStatus.heartbeat_ok
                kiwoom_logged_in = [bool]$gatewayStatus.kiwoom_logged_in
                orderable = [bool]$gatewayStatus.orderable
                transport = [string]$env:TRADING_GATEWAY_TRANSPORT
                core_url = [string]$env:TRADING_KIWOOM_GATEWAY_CORE_URL
                ws_url = [string]$env:TRADING_GATEWAY_WS_URL
                readiness_mode = if ($RequireGatewayOrderable) { "orderable" } else { "heartbeat" }
                ready_for_orders = [bool]($gatewayStatus.kiwoom_logged_in -and $gatewayStatus.orderable -and (Test-GatewaySimulationMode $gatewayStatus))
                require_orderable = [bool]$RequireGatewayOrderable
                broker_env = [string]$gatewayModes.broker_env
                server_mode = [string]$gatewayModes.server_mode
                account_mode = [string]$gatewayModes.account_mode
                server_gubun = [string]$gatewayModes.server_gubun
            }
        } else { $null }
        themelab = $themeLabStatus
        pre_open_data_warmup = $preOpenDataWarmupStatus
        live_sim_preflight = $liveSimPreflightSummary
        live_sim_hybrid_ready_canary = Get-ObjectPropertyValue $dbSettings "live_sim_hybrid_ready_canary"
        shadow_small_entry_ops = $shadowSmallEntryStatus
        db_order_execution = $dbSettings
    } | ConvertTo-Json -Depth 8
}
finally {
    Pop-Location
}
