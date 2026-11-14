<#
.SYNOPSIS
    Delta India Terminal - Windows Process Supervisor & Watchdog (TASK-P15-01)
.DESCRIPTION
    Monitors and supervises the Delta India Terminal server.
    Ensures automatic recovery upon unexpected termination, performs periodic
    HTTP health checks against /api/v2/system/mode, and rotates logs.
.PARAMETER Port
    HTTP Port (default: 5000)
.PARAMETER MaxRestarts
    Maximum restarts in a rolling window before tripping emergency halt (default: 10)
.PARAMETER HealthIntervalSec
    Interval in seconds between health probes (default: 10)
#>
param (
    [int]$Port = 5000,
    [int]$MaxRestarts = 10,
    [int]$HealthIntervalSec = 10
)

$ErrorActionPreference = "Continue"
$ScriptRoot = Split-Path -Parent -Path $MyInvocation.MyCommand.Definition
$ProjectRoot = Resolve-Path "$ScriptRoot\.."
$LogDir = "$ProjectRoot\logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

$StdoutLog = "$LogDir\service_stdout.log"
$StderrLog = "$LogDir\service_stderr.log"
$WatchdogLog = "$LogDir\watchdog.log"

function Log-Message {
    param([string]$Message, [string]$Level = "INFO")
    $timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $entry = "[$timestamp] [$Level] $Message"
    Write-Host $entry
    Add-Content -Path $WatchdogLog -Value $entry
}

Log-Message "Starting Delta Terminal Process Supervisor from $ProjectRoot"

$RestartCount = 0
$HealthUrl = "http://127.0.0.1:$Port/api/v2/system/mode"

while ($true) {
    if ($RestartCount -ge $MaxRestarts) {
        Log-Message "CRITICAL: Exceeded maximum allowed restarts ($MaxRestarts). Halting supervisor for manual intervention." "FATAL"
        break
    }

    Log-Message "Spawning application process: python app.py"
    $ProcessInfo = New-Object System.Diagnostics.ProcessStartInfo
    $ProcessInfo.FileName = "python"
    $ProcessInfo.Arguments = "app.py"
    $ProcessInfo.WorkingDirectory = $ProjectRoot
    $ProcessInfo.UseShellExecute = $false
    $ProcessInfo.RedirectStandardOutput = $true
    $ProcessInfo.RedirectStandardError = $true

    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $ProcessInfo

    # Hook stdout and stderr
    $Process.OutputDataReceived += {
        if ($_.Data) { Add-Content -Path $StdoutLog -Value $_.Data }
    }
    $Process.ErrorDataReceived += {
        if ($_.Data) { Add-Content -Path $StderrLog -Value $_.Data }
    }

    $Started = $Process.Start()
    if (-not $Started) {
        Log-Message "Failed to start python process. Retrying in 5 seconds..." "ERROR"
        Start-Sleep -Seconds 5
        $RestartCount++
        continue
    }

    $Process.BeginOutputReadLine()
    $Process.BeginErrorReadLine()
    Log-Message "Process started successfully (PID: $($Process.Id)). Beginning watchdog probe loop."

    $ConFailedProbes = 0

    while (-not $Process.HasExited) {
        Start-Sleep -Seconds $HealthIntervalSec
        try {
            $response = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 3 -ErrorAction Stop
            if ($response.status -eq "ok" -or $response.mode) {
                $ConFailedProbes = 0
            } else {
                $ConFailedProbes++
            }
        } catch {
            $ConFailedProbes++
            Log-Message "Health probe failed ($ConFailedProbes/3): $($_.Exception.Message)" "WARN"
        }

        if ($ConFailedProbes -ge 3) {
            Log-Message "Process unresponsive (3 consecutive probe failures). Killing PID $($Process.Id)..." "ERROR"
            try {
                $Process.Kill()
            } catch {
                Log-Message "Could not kill process: $($_.Exception.Message)" "WARN"
            }
            break
        }
    }

    $ExitCode = $Process.ExitCode
    Log-Message "Process PID $($Process.Id) terminated with exit code $ExitCode" "WARN"
    $RestartCount++
    Start-Sleep -Seconds 3
}
