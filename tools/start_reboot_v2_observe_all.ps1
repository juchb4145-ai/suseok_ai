[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$DbPath = "",
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000,
    [string]$Token = "",
    [int]$CoreStartupTimeoutSec = 90,
    [int]$GatewayStartupTimeoutSec = 240,
    [double]$GatewayIntervalSec = 2.0,
    [double]$GatewayPollWaitSec = 0.5,
    [switch]$NoAutoLogin,
    [switch]$NoRequireLogin,
    [switch]$SkipGateway,
    [switch]$StopExisting,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Utf8Encoding = New-Object System.Text.UTF8Encoding -ArgumentList $false
[Console]::OutputEncoding = $Utf8Encoding
$OutputEncoding = $Utf8Encoding

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

$CoreScript = Join-Path $ProjectRoot "tools\start_reboot_v2_observe.ps1"
if (-not (Test-Path $CoreScript)) {
    throw "Core observe script not found: $CoreScript"
}

$LogDir = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$CoreUrlHost = if ($BindHost -in @("0.0.0.0", "::", "[::]")) { "127.0.0.1" } else { $BindHost }
$CoreUrl = "http://${CoreUrlHost}:$Port"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$CoreOut = Join-Path $LogDir "reboot_v2_observe_core_$Timestamp.out.log"
$CoreErr = Join-Path $LogDir "reboot_v2_observe_core_$Timestamp.err.log"
$GatewayOut = Join-Path $LogDir "reboot_v2_observe_gateway_$Timestamp.out.log"
$GatewayErr = Join-Path $LogDir "reboot_v2_observe_gateway_$Timestamp.err.log"
$PidFile = Join-Path $LogDir "reboot_v2_observe_$Timestamp.pid.json"

function Write-Step([string]$Message) {
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $Message)
}

function Invoke-CoreApi(
    [ValidateSet("GET", "POST")] [string]$Method,
    [string]$Path,
    [int]$TimeoutSec = 5
) {
    $uri = "$CoreUrl$Path"
    $headers = @{
        "X-Local-Token" = $Token
        "Authorization" = "Bearer $Token"
    }
    if ($Method -eq "POST") {
        return Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -TimeoutSec $TimeoutSec
    }
    return Invoke-RestMethod -Method Get -Uri $uri -Headers $headers -TimeoutSec $TimeoutSec
}

function Test-CoreReady {
    try {
        $health = Invoke-CoreApi -Method GET -Path "/health" -TimeoutSec 2
        if ($health.ok) {
            return $true
        }
    } catch {
        try {
            [void](Invoke-CoreApi -Method GET -Path "/api/gateway/status" -TimeoutSec 2)
            return $true
        } catch {
            return $false
        }
    }
    return $false
}

function Wait-CoreReady([int]$TimeoutSec) {
    $deadline = (Get-Date).AddSeconds([Math]::Max(1, $TimeoutSec))
    while ((Get-Date) -lt $deadline) {
        if (Test-CoreReady) {
            return $true
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Get-PortListenerPids([int]$LocalPort) {
    @(Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Get-GatewayProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -match "python|pythonw|py") -and ($_.CommandLine -match "apps[\\/]kiwoom_gateway.py|kiwoom_gateway.py")
    })
}

function Assert-NoUnsafeOrderEnv {
    $required = [ordered]@{
        TRADING_MODE = "OBSERVE"
        TRADING_ALLOW_LIVE = "0"
        TRADING_SEND_ORDER_ALLOWED = "false"
        TRADING_RUNTIME_MODE = "OBSERVE"
        TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS = "0"
        TRADING_RUNTIME_ALLOW_LIVE_ORDERS = "0"
        TRADING_ORDER_MANAGER_ENABLED = "0"
        TRADING_ORDER_MANAGER_OBSERVE_ONLY = "true"
        TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND = "false"
        TRADING_ORDER_INTENT_ENABLED = "false"
        TRADING_ALLOW_LIVE_SIM_ORDERS = "0"
        TRADING_ENTRY_ALLOW_DRY_RUN_INTENTS = "0"
        TRADING_EXIT_ALLOW_DRY_RUN_SELL_INTENTS = "0"
    }
    foreach ($key in $required.Keys) {
        $value = [Environment]::GetEnvironmentVariable($key)
        if ($value -ne $required[$key]) {
            throw "Unsafe flag mismatch before Gateway start: $key expected $($required[$key]) got $value"
        }
    }
}

function Set-GatewayObserveEnv {
    $env:PYTHONIOENCODING = "utf-8"
    $env:TRADING_CORE_TOKEN = $Token
    $env:TRADING_DB_PATH = $DbPath
    $env:TRADING_MODE = "OBSERVE"
    $env:TRADING_ALLOW_LIVE = "0"
    $env:TRADING_SEND_ORDER_ALLOWED = "false"
    $env:TRADING_RUNTIME_MODE = "OBSERVE"
    $env:TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS = "0"
    $env:TRADING_RUNTIME_ALLOW_LIVE_ORDERS = "0"
    $env:TRADING_ORDER_MANAGER_ENABLED = "0"
    $env:TRADING_ORDER_MANAGER_OBSERVE_ONLY = "true"
    $env:TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND = "false"
    $env:TRADING_ORDER_INTENT_ENABLED = "false"
    $env:TRADING_ALLOW_LIVE_SIM_ORDERS = "0"
    $env:TRADING_ENTRY_ALLOW_DRY_RUN_INTENTS = "0"
    $env:TRADING_EXIT_ALLOW_DRY_RUN_SELL_INTENTS = "0"
}

function Test-Py32Available {
    try {
        $output = & py -3.9-32 -c "import sys; print(sys.executable)" 2>$null
        return [bool]$output
    } catch {
        return $false
    }
}

function Wait-GatewayReady([int]$TimeoutSec, [bool]$RequireLogin) {
    $deadline = (Get-Date).AddSeconds([Math]::Max(1, $TimeoutSec))
    $last = $null
    while ((Get-Date) -lt $deadline) {
        try {
            $last = Invoke-CoreApi -Method GET -Path "/api/gateway/status" -TimeoutSec 5
            $heartbeatOk = [bool]$last.heartbeat_ok
            $connected = [bool]$last.connected
            $loggedIn = [bool]$last.kiwoom_logged_in
            if ($connected -and $heartbeatOk -and ((-not $RequireLogin) -or $loggedIn)) {
                return $last
            }
        } catch {
            $last = $null
        }
        Start-Sleep -Seconds 3
    }
    if ($last) {
        return $last
    }
    return $null
}

Write-Step "Reboot V2 OBSERVE all-in-one startup"
Write-Step "ProjectRoot=$ProjectRoot"
Write-Step "CoreScript=$CoreScript"
Write-Step "DbPath=$DbPath"
Write-Step "CoreUrl=$CoreUrl"
Write-Step "LogDir=$LogDir"

if ($StopExisting) {
    $stopScript = Join-Path $ProjectRoot "tools\stop_market_close.ps1"
    if (Test-Path $stopScript) {
        Write-Step "Stopping existing Core/Gateway processes for port $Port"
        if (-not $DryRun) {
            & powershell -NoProfile -ExecutionPolicy Bypass -File $stopScript -ProjectRoot $ProjectRoot -BindHost $BindHost -Port $Port -Token $Token | Out-Host
        }
    }
}

$listeners = Get-PortListenerPids -LocalPort $Port
if ((-not $DryRun) -and @($listeners).Count -gt 0) {
    throw "Core port $Port is already in use by pid(s): $(@($listeners) -join ', '). Stop it first or run with another -Port."
}

$existingGateways = Get-GatewayProcesses
if ((-not $DryRun) -and (-not $SkipGateway) -and @($existingGateways).Count -gt 0) {
    $ids = @($existingGateways | Select-Object -ExpandProperty ProcessId)
    throw "Kiwoom Gateway already running pid(s): $($ids -join ', '). Stop it first or run with -SkipGateway."
}

$coreArgs = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $CoreScript,
    "-ProjectRoot", $ProjectRoot,
    "-DbPath", $DbPath,
    "-BindHost", $BindHost,
    "-Port", "$Port",
    "-Token", $Token
)
$gatewayArgs = @(
    "-3.9-32",
    "apps\kiwoom_gateway.py",
    "--core-url", $CoreUrl,
    "--token", $Token,
    "--interval-sec", "$GatewayIntervalSec",
    "--poll-wait-sec", "$GatewayPollWaitSec",
    "--threaded-login"
)
if ($NoAutoLogin) {
    $gatewayArgs += "--no-auto-login"
} else {
    $gatewayArgs += "--auto-login"
}

if ($DryRun) {
    [pscustomobject]@{
        dry_run = $true
        core_command = "powershell " + ($coreArgs -join " ")
        gateway_command = if ($SkipGateway) { "" } else { "py " + ($gatewayArgs -join " ") }
        core_stdout = $CoreOut
        core_stderr = $CoreErr
        gateway_stdout = $GatewayOut
        gateway_stderr = $GatewayErr
    } | ConvertTo-Json -Depth 4
    return
}

Write-Step "Starting Core via tools/start_reboot_v2_observe.ps1"
$coreProcess = Start-Process `
    -FilePath "powershell" `
    -ArgumentList $coreArgs `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput $CoreOut `
    -RedirectStandardError $CoreErr `
    -WindowStyle Hidden `
    -PassThru

if (-not (Wait-CoreReady -TimeoutSec $CoreStartupTimeoutSec)) {
    throw "Core did not become ready within $CoreStartupTimeoutSec sec. See $CoreErr and $CoreOut"
}
Write-Step "Core ready pid=$($coreProcess.Id)"

$gatewayProcess = $null
$gatewayStatus = $null
if (-not $SkipGateway) {
    if (-not (Test-Py32Available)) {
        throw "py -3.9-32 is not available. Install/configure 32-bit Python before starting Kiwoom Gateway."
    }
    Set-GatewayObserveEnv
    Assert-NoUnsafeOrderEnv
    Write-Step "Starting 32-bit Kiwoom Gateway"
    $gatewayProcess = Start-Process `
        -FilePath "py" `
        -ArgumentList $gatewayArgs `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $GatewayOut `
        -RedirectStandardError $GatewayErr `
        -WindowStyle Hidden `
        -PassThru
    $requireGatewayLogin = ((-not [bool]$NoRequireLogin) -and (-not [bool]$NoAutoLogin))
    $gatewayStatus = Wait-GatewayReady -TimeoutSec $GatewayStartupTimeoutSec -RequireLogin $requireGatewayLogin
    if (-not $gatewayStatus) {
        throw "Gateway did not report heartbeat within $GatewayStartupTimeoutSec sec. See $GatewayErr and $GatewayOut"
    }
    if ((-not [bool]$NoRequireLogin) -and (-not [bool]$NoAutoLogin) -and (-not [bool]$gatewayStatus.kiwoom_logged_in)) {
        throw "Gateway heartbeat is present but Kiwoom login is not complete. Complete login or rerun with -NoRequireLogin."
    }
    Write-Step "Gateway ready pid=$($gatewayProcess.Id) logged_in=$($gatewayStatus.kiwoom_logged_in)"
}

$commandStatus = Invoke-CoreApi -Method GET -Path "/api/gateway/commands/status" -TimeoutSec 5
$runtimeStatus = Invoke-CoreApi -Method GET -Path "/api/runtime/status" -TimeoutSec 5

$summary = [pscustomobject]@{
    started = $true
    core_url = $CoreUrl
    db_path = $DbPath
    core_pid = $coreProcess.Id
    gateway_pid = if ($gatewayProcess) { $gatewayProcess.Id } else { $null }
    gateway_logged_in = if ($gatewayStatus) { [bool]$gatewayStatus.kiwoom_logged_in } else { $false }
    gateway_broker_env = if ($gatewayStatus) { $gatewayStatus.last_heartbeat_payload.broker_env } else { "" }
    order_command_last_at = $commandStatus.last_order_command_at
    command_total_count = $commandStatus.total_count
    runtime_status = $runtimeStatus.status
    runtime_mode = $runtimeStatus.mode
    core_stdout = $CoreOut
    core_stderr = $CoreErr
    gateway_stdout = $GatewayOut
    gateway_stderr = $GatewayErr
}
$summary | ConvertTo-Json -Depth 6 | Set-Content -Path $PidFile -Encoding UTF8
$summary | ConvertTo-Json -Depth 6
