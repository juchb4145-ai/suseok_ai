[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000,
    [string]$Token = "",
    [int]$RuntimeStopTimeoutSec = 15,
    [switch]$KeepCoreApi,
    [switch]$KeepGateway,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if (-not $ProjectRoot) {
    $ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
} else {
    $ProjectRoot = Resolve-Path $ProjectRoot
}
$ProjectRoot = [string]$ProjectRoot

if (-not $Token) {
    $Token = if ($env:TRADING_CORE_TOKEN) { $env:TRADING_CORE_TOKEN } else { "local-dev-token" }
}

function Write-Step([string]$Message) {
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $Message)
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

function Get-DescendantProcessIds([int]$RootProcessId) {
    $children = Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $RootProcessId }
    foreach ($child in $children) {
        [int]$child.ProcessId
        Get-DescendantProcessIds -RootProcessId ([int]$child.ProcessId)
    }
}

function Add-Target(
    [hashtable]$Targets,
    [int]$ProcessId,
    [string]$Reason
) {
    if ($ProcessId -le 0) {
        return
    }
    if ($ProcessId -eq $PID) {
        return
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $process) {
        return
    }
    if (-not $Targets.ContainsKey($ProcessId)) {
        $Targets[$ProcessId] = [pscustomobject]@{
            pid = $ProcessId
            parent_pid = [int]$process.ParentProcessId
            name = [string]$process.Name
            command_line = [string]$process.CommandLine
            reasons = @()
        }
    }
    $Targets[$ProcessId].reasons = @($Targets[$ProcessId].reasons + $Reason | Sort-Object -Unique)
}

function Get-TradingStopTargets {
    $targets = @{}

    if (-not $KeepCoreApi) {
        $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        foreach ($listener in $listeners) {
            $listenerProcessId = [int]$listener.OwningProcess
            Add-Target -Targets $targets -ProcessId $listenerProcessId -Reason "port:$Port"
            foreach ($childProcessId in Get-DescendantProcessIds -RootProcessId $listenerProcessId) {
                Add-Target -Targets $targets -ProcessId ([int]$childProcessId) -Reason "core-descendant"
            }

            $listenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerProcessId" -ErrorAction SilentlyContinue
            if ($listenerProcess -and $listenerProcess.ParentProcessId) {
                $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($listenerProcess.ParentProcessId)" -ErrorAction SilentlyContinue
                if ($parent -and ($parent.CommandLine -like "*trading_app.api*" -or $parent.CommandLine -like "*apps.core_api*")) {
                    Add-Target -Targets $targets -ProcessId ([int]$parent.ProcessId) -Reason "core-parent"
                    foreach ($childProcessId in Get-DescendantProcessIds -RootProcessId ([int]$parent.ProcessId)) {
                        Add-Target -Targets $targets -ProcessId ([int]$childProcessId) -Reason "core-parent-descendant"
                    }
                }
            }
        }
    }

    if (-not $KeepGateway) {
        $gatewayProcesses = Get-CimInstance Win32_Process | Where-Object {
            ($_.Name -match "python|pythonw") -and ($_.CommandLine -match "apps[\\/]kiwoom_gateway.py|kiwoom_gateway.py")
        }
        foreach ($gateway in $gatewayProcesses) {
            Add-Target -Targets $targets -ProcessId ([int]$gateway.ProcessId) -Reason "kiwoom-gateway"
        }
    }

    return @($targets.Values | Sort-Object -Property pid -Descending)
}

Write-Step "Market close stop sequence"
Write-Step "ProjectRoot=$ProjectRoot"
Write-Step "Core endpoint=http://${BindHost}:$Port"

$coreWasHealthy = Test-CoreHealthy
$runtimeStopResult = $null
if ($coreWasHealthy) {
    if ($DryRun) {
        Write-Step "DryRun: would request runtime stop"
    } else {
        try {
            Write-Step "Stopping runtime loop"
            $runtimeStopResult = Invoke-CoreApi -Method POST -Path "/api/runtime/stop" -TimeoutSec $RuntimeStopTimeoutSec
            Start-Sleep -Seconds 2
        } catch {
            Write-Step "Runtime stop request failed: $($_.Exception.Message)"
        }
    }
} else {
    Write-Step "Core API is not healthy; runtime stop API skipped"
}

$targets = Get-TradingStopTargets
if ($DryRun) {
    Write-Step "DryRun requested; processes were not stopped"
    [pscustomobject]@{
        dry_run = $true
        core_was_healthy = $coreWasHealthy
        would_stop_count = @($targets).Count
        targets = $targets
    } | ConvertTo-Json -Depth 8
    return
}

$stopped = @()
foreach ($target in $targets) {
    $process = Get-Process -Id ([int]$target.pid) -ErrorAction SilentlyContinue
    if ($process) {
        Write-Step "Stopping pid=$($target.pid) name=$($process.ProcessName)"
        try {
            Stop-Process -Id ([int]$target.pid) -Force -ErrorAction Stop
            $stopped += [pscustomobject]@{
                pid = [int]$target.pid
                name = [string]$process.ProcessName
                stopped = $true
                reasons = $target.reasons
                error = ""
            }
        } catch {
            $stopped += [pscustomobject]@{
                pid = [int]$target.pid
                name = [string]$process.ProcessName
                stopped = $false
                reasons = $target.reasons
                error = $_.Exception.Message
            }
        }
    }
}

Start-Sleep -Seconds 2

$remainingPortListeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort,OwningProcess
$remainingGatewayProcesses = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -match "python|pythonw") -and ($_.CommandLine -match "apps[\\/]kiwoom_gateway.py|kiwoom_gateway.py")
} | Select-Object ProcessId,ParentProcessId,Name,CommandLine
$remainingCoreProcesses = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -match "python|pythonw") -and ($_.CommandLine -match "trading_app.api|apps.core_api")
} | Select-Object ProcessId,ParentProcessId,Name,CommandLine

[pscustomobject]@{
    stopped = $true
    core_was_healthy = $coreWasHealthy
    runtime_stop = $runtimeStopResult
    stopped_processes = $stopped
    port8000_listeners_remaining = @($remainingPortListeners)
    core_processes_remaining = @($remainingCoreProcesses)
    gateway_processes_remaining = @($remainingGatewayProcesses)
} | ConvertTo-Json -Depth 8
