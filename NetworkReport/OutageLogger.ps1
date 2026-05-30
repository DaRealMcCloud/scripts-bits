#Requires -Version 5.1
<#
.SYNOPSIS
    Monitors internet connectivity and logs all outages to a CSV file.
    Designed to produce evidence for ISP support cases.

.DESCRIPTION
    Pings public DNS endpoints every second. When connectivity is lost, records the
    outage start time. When it recovers, records the end time and duration.
    Produces a CSV log with every outage and a summary on exit.

    Run on a machine with a direct Ethernet connection (no VPN, no failover).

.PARAMETER PollIntervalSec
    Seconds between connectivity checks. Default: 1

.PARAMETER TimeoutMs
    Ping timeout in milliseconds. Default: 2000

.PARAMETER FailThreshold
    Consecutive failed pings before declaring an outage (avoids single-packet false positives). Default: 3

.EXAMPLE
    .\OutageLogger.ps1
    Starts monitoring. Press Ctrl+C to stop and print summary.
#>

[CmdletBinding()]
param(
    [string[]]$DnsTargets = @("1.1.1.1", "8.8.8.8", "9.9.9.9"),

    [int]$PollIntervalSec = 1,
    [int]$TimeoutMs       = 2000,
    [int]$FailThreshold   = 3
)

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$LogDir     = Join-Path $ScriptDir "log"
$StartTime  = Get-Date
$DateStamp  = $StartTime.ToString("yyyyMMdd_HHmmss")
$CsvFile    = Join-Path $LogDir "OutageLog_$DateStamp.csv"
$ConsoleLog = Join-Path $LogDir "OutageLogger_$DateStamp.log"

function Write-Log {
    param(
        [string]$Message,
        [ValidateSet("INFO", "WARN", "ERROR", "SUCCESS")]
        [string]$Level = "INFO"
    )

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] [$Level] $Message"

    $color = switch ($Level) {
        "INFO"    { "Cyan" }
        "WARN"    { "Yellow" }
        "ERROR"   { "Red" }
        "SUCCESS" { "Green" }
    }

    Write-Host $line -ForegroundColor $color
    Add-Content -Path $ConsoleLog -Value $line
}

function Test-Internet {
    param(
        [string[]]$Targets,
        [int]$Timeout
    )

    foreach ($target in $Targets) {
        $null = ping.exe -n 1 -w $Timeout $target 2>$null
        if ($LASTEXITCODE -eq 0) {
            return $true
        }
    }
    return $false
}

# ---------------------------------------------------------------------------
# Initialize CSV
# ---------------------------------------------------------------------------
$csvHeader = "OutageNumber,OutageStart,OutageEnd,DurationSeconds,DurationFormatted"
Set-Content -Path $CsvFile -Value $csvHeader

Write-Log "========================================" "INFO"
Write-Log "OutageLogger starting" "INFO"
Write-Log "  DNS targets   : $($DnsTargets -join ', ')" "INFO"
Write-Log "  Poll interval : ${PollIntervalSec}s" "INFO"
Write-Log "  Ping timeout  : ${TimeoutMs}ms" "INFO"
Write-Log "  Fail threshold: $FailThreshold consecutive failures" "INFO"
Write-Log "  CSV log       : $CsvFile" "INFO"
Write-Log "  Console log   : $ConsoleLog" "INFO"
Write-Log "  Press Ctrl+C to stop and print summary" "INFO"
Write-Log "========================================" "INFO"

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
$outageCount        = 0
$outageStart        = $null
$consecutiveFailures = 0
$inOutage           = $false
$lastHeartbeat      = Get-Date
$heartbeatMin       = 30
$totalOutageSec     = 0

try {
    while ($true) {
        $connected = Test-Internet -Targets $DnsTargets -Timeout $TimeoutMs

        if ($connected) {
            # --- Connection OK ---
            if ($inOutage) {
                # Outage just ended
                $outageEnd      = Get-Date
                $durationSec    = [Math]::Round(($outageEnd - $outageStart).TotalSeconds, 1)
                $durationFmt    = ($outageEnd - $outageStart).ToString("hh\:mm\:ss")
                $totalOutageSec += $durationSec

                $csvLine = "$outageCount,$($outageStart.ToString('yyyy-MM-dd HH:mm:ss')),$($outageEnd.ToString('yyyy-MM-dd HH:mm:ss')),$durationSec,$durationFmt"
                Add-Content -Path $CsvFile -Value $csvLine

                Write-Log "OUTAGE #$outageCount ENDED — Duration: $durationFmt ($durationSec sec)" "SUCCESS"
                $inOutage = $false
                $lastHeartbeat = Get-Date
            }
            $consecutiveFailures = 0

            # Heartbeat
            $minutesSince = ((Get-Date) - $lastHeartbeat).TotalMinutes
            if ($minutesSince -ge $heartbeatMin) {
                $uptimeTotal = [Math]::Round(((Get-Date) - $StartTime).TotalHours, 1)
                Write-Log "Heartbeat: connection stable. Monitoring for ${uptimeTotal}h, $outageCount outage(s) recorded." "SUCCESS"
                $lastHeartbeat = Get-Date
            }
        } else {
            # --- Connection FAILED ---
            $consecutiveFailures++

            if (-not $inOutage -and $consecutiveFailures -ge $FailThreshold) {
                # New outage detected
                $outageCount++
                $outageStart = (Get-Date).AddSeconds(-($FailThreshold * $PollIntervalSec))
                $inOutage = $true
                Write-Log "OUTAGE #$outageCount STARTED at $($outageStart.ToString('yyyy-MM-dd HH:mm:ss'))" "ERROR"
            } elseif (-not $inOutage) {
                Write-Log "Ping failed ($consecutiveFailures/$FailThreshold)" "WARN"
            }
        }

        Start-Sleep -Seconds $PollIntervalSec
    }
}
finally {
    # Close any open outage
    if ($inOutage) {
        $outageEnd   = Get-Date
        $durationSec = [Math]::Round(($outageEnd - $outageStart).TotalSeconds, 1)
        $durationFmt = ($outageEnd - $outageStart).ToString("hh\:mm\:ss")
        $totalOutageSec += $durationSec

        $csvLine = "$outageCount,$($outageStart.ToString('yyyy-MM-dd HH:mm:ss')),$($outageEnd.ToString('yyyy-MM-dd HH:mm:ss')),$durationSec,$durationFmt (ongoing)"
        Add-Content -Path $CsvFile -Value $csvLine
        Write-Log "OUTAGE #$outageCount was still ongoing — logged with current time" "WARN"
    }

    # Print summary
    $totalRuntime    = (Get-Date) - $StartTime
    $runtimeFmt      = $totalRuntime.ToString("dd\.hh\:mm\:ss")
    $uptimePercent   = if ($totalRuntime.TotalSeconds -gt 0) {
        [Math]::Round((1 - ($totalOutageSec / $totalRuntime.TotalSeconds)) * 100, 3)
    } else { 100 }

    Write-Log "========================================" "INFO"
    Write-Log "OutageLogger Summary" "INFO"
    Write-Log "  Monitoring period : $($StartTime.ToString('yyyy-MM-dd HH:mm:ss')) to $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" "INFO"
    Write-Log "  Total runtime     : $runtimeFmt" "INFO"
    Write-Log "  Total outages     : $outageCount" "INFO"
    Write-Log "  Total downtime    : $([Math]::Round($totalOutageSec, 1))s" "INFO"
    Write-Log "  Uptime            : $uptimePercent%" "INFO"
    Write-Log "  CSV report        : $CsvFile" "INFO"
    Write-Log "========================================" "INFO"
}
