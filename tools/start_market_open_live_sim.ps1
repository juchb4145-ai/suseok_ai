[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$DbPath = "",
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000,
    [string]$Token = "",
    [int]$CoreStartupTimeoutSec = 45,
    [int]$GatewayStartupTimeoutSec = 90,
    [int]$RuntimeStartupTimeoutSec = 120,
    [ValidateSet("rest", "websocket-pilot", "websocket-experimental")]
    [string]$GatewayTransport = "websocket-pilot",
    [string]$GatewayCoreUrl = "",
    [switch]$SkipGateway,
    [switch]$SkipRuntime,
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

if (-not $GatewayCoreUrl) {
    $gatewayHost = if ($BindHost -in @("0.0.0.0", "::", "[::]")) { "127.0.0.1" } else { $BindHost }
    $GatewayCoreUrl = "http://${gatewayHost}:$Port"
}

$Python64 = Join-Path $ProjectRoot "venv_64\Scripts\python.exe"
if (-not (Test-Path $Python64)) {
    throw "64-bit Python runtime not found: $Python64"
}

$LogDir = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$env:TRADING_MODE = "OBSERVE"
$env:TRADING_RUNTIME_MODE = "DRY_RUN"
$env:TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS = "1"
$env:TRADING_RUNTIME_ALLOW_LIVE_ORDERS = "0"
$env:TRADING_GATEWAY_TRANSPORT = $GatewayTransport
$env:TRADING_KIWOOM_GATEWAY_CORE_URL = $GatewayCoreUrl
if ($GatewayTransport -eq "websocket-pilot") {
    $env:TRADING_GATEWAY_WEBSOCKET_REAL_PILOT = "1"
    $env:TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL = "1"
    $env:TRADING_GATEWAY_WEBSOCKET_FALLBACK_TO_REST = "1"
    $env:TRADING_GATEWAY_WEBSOCKET_PILOT_ALLOW_ORDER_COMMANDS = "1"
    $env:TRADING_GATEWAY_WEBSOCKET_PILOT_BLOCK_ORDER_COMMANDS = "0"
}
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
            if ($parent -and ($parent.CommandLine -like "*trading_app.api*" -or $parent.CommandLine -like "*apps.core_api*")) {
                [void]$processIds.Add([int]$parent.ProcessId)
                foreach ($childProcessId in Get-DescendantProcessIds -RootProcessId ([int]$parent.ProcessId)) {
                    [void]$processIds.Add([int]$childProcessId)
                }
            }
        }
    }

    $gatewayProcesses = Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -match "python|pythonw") -and ($_.CommandLine -match "apps[\\/]kiwoom_gateway.py|kiwoom_gateway.py")
    }
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

Push-Location $ProjectRoot
try {
    Write-Step "Market open LIVE_SIM startup settings"
    Write-Step "Core TRADING_MODE=$env:TRADING_MODE"
    Write-Step "Runtime TRADING_RUNTIME_MODE=$env:TRADING_RUNTIME_MODE"
    Write-Step "Runtime TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS=$env:TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS"
    Write-Step "Runtime TRADING_RUNTIME_ALLOW_LIVE_ORDERS=$env:TRADING_RUNTIME_ALLOW_LIVE_ORDERS"
    Write-Step "Gateway transport=$env:TRADING_GATEWAY_TRANSPORT"
    Write-Step "Gateway core URL=$env:TRADING_KIWOOM_GATEWAY_CORE_URL"
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
            trading_mode = $env:TRADING_MODE
            runtime_mode = $env:TRADING_RUNTIME_MODE
            runtime_allow_dry_run_orders = $env:TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS
            runtime_allow_live_orders = $env:TRADING_RUNTIME_ALLOW_LIVE_ORDERS
            gateway_transport = $env:TRADING_GATEWAY_TRANSPORT
            gateway_core_url = $env:TRADING_KIWOOM_GATEWAY_CORE_URL
            websocket_pilot_order_allow = $env:TRADING_GATEWAY_WEBSOCKET_PILOT_ALLOW_ORDER_COMMANDS
            websocket_pilot_order_block = $env:TRADING_GATEWAY_WEBSOCKET_PILOT_BLOCK_ORDER_COMMANDS
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
        $gatewayStart = Invoke-CoreApi -Method POST -Path "/api/gateway/kiwoom/start" -TimeoutSec 20
        Write-Step "Gateway start reason=$($gatewayStart.reason)"
        $gatewayStatus = Wait-Until -TimeoutSec $GatewayStartupTimeoutSec -Label "Kiwoom gateway readiness" -Condition {
            $status = $null
            try {
                $status = Invoke-CoreApi -Method GET -Path "/api/gateway/status" -TimeoutSec 5
            } catch {
                return $null
            }
            if ($status.connected -and $status.heartbeat_ok -and $status.kiwoom_logged_in -and $status.orderable) {
                Assert-GatewayNotRealMode $status
                if (Test-GatewaySimulationMode $status) {
                    return $status
                }
            }
            return $null
        }
        $gatewayModes = Get-GatewayBrokerModeSummary $gatewayStatus
        Write-Step "Gateway ready connected=$($gatewayStatus.connected) orderable=$($gatewayStatus.orderable) broker_env=$($gatewayModes.broker_env) account_mode=$($gatewayModes.account_mode)"
    }

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

    [pscustomobject]@{
        started = $true
        core = @{
            pid = $coreProcess.Id
            url = "http://${BindHost}:$Port"
            stdout = $coreOutLog
            stderr = $coreErrLog
            trading_mode = "OBSERVE"
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
                broker_env = [string]$gatewayModes.broker_env
                server_mode = [string]$gatewayModes.server_mode
                account_mode = [string]$gatewayModes.account_mode
                server_gubun = [string]$gatewayModes.server_gubun
            }
        } else { $null }
        db_order_execution = $dbSettings
    } | ConvertTo-Json -Depth 8
}
finally {
    Pop-Location
}
