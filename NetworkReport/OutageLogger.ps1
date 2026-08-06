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
if (-not (Test-Path -Path $LogDir)) { New-Item -Path $LogDir -ItemType Directory -Force | Out-Null }

# CSV will record outages for each monitored target (Router, NAS, Internet)
$csvHeader = "Target,OutageNumber,OutageStart,OutageEnd,DurationSeconds,DurationFormatted,PingsSent,PacketsDropped,AvgRttMs"
Set-Content -Path $CsvFile -Value $csvHeader

# Monitoring configuration: add Router and NAS plus an Internet target
$ReportIntervalSec = 1800 # 30 minutes for summary output

$MonitoredTargets = @(
    @{ Name = 'Router';   Addr = '10.0.0.1' },
    @{ Name = 'NAS';      Addr = '10.0.0.10' },
    @{ Name = 'Printer';  Addr = '10.0.0.121' },
    @{ Name = 'Dryer';    Addr = '10.0.0.111' },
    @{ Name = 'sw1';      Addr = '10.0.0.254' },
    @{ Name = 'sw2';      Addr = '10.0.0.253' },
    @{ Name = 'Internet'; Addr = $DnsTargets[0] }
)

$MonitoredTargetsDescription = $MonitoredTargets | ForEach-Object { "$($_.Name)=$($_.Addr)" } | Sort-Object | Join-String ', '

Write-Log "========================================" "INFO"
Write-Log "OutageLogger starting" "INFO"
Write-Log "  Monitored targets: $MonitoredTargetsDescription" "INFO"
Write-Log "  DNS targets      : $($DnsTargets -join ', ')" "INFO"
Write-Log "  Poll interval    : ${PollIntervalSec}s" "INFO"
Write-Log "  Ping timeout     : ${TimeoutMs}ms" "INFO"
Write-Log "  Fail threshold   : $FailThreshold consecutive failures" "INFO"
Write-Log "  CSV log       : $CsvFile" "INFO"
Write-Log "  Console log   : $ConsoleLog" "INFO"
Write-Log "  Press Ctrl+C to stop and print summary" "INFO"
Write-Log "========================================" "INFO"

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
$lastHeartbeat      = Get-Date
$heartbeatMin       = 30
$totalOutageSec     = 0

# Initialize per-target state
$targets = @{}
foreach ($t in $MonitoredTargets) {
    $targets[$t.Name] = [PSCustomObject]@{
        Name = $t.Name
        Addr = $t.Addr
        Sent = 0
        Failures = 0
        SumRtt = 0
        ConsecutiveFailures = 0
        InOutage = $false
        OutageCount = 0
        OutageStart = $null
        OutageEnd = $null
    }
}

$nextReportTime = (Get-Date).AddSeconds($ReportIntervalSec)

# We'll create per-target Ping objects and send pings concurrently below

try {
    while ($true) {
        # Send pings concurrently to all targets using SendPingAsync so timestamps align
        $sendTasks = @{}
        foreach ($key in $targets.Keys) {
            $t = $targets[$key]
            $ping = New-Object System.Net.NetworkInformation.Ping
            try {
                $task = $ping.SendPingAsync($t.Addr, $TimeoutMs)
            } catch {
                # create a completed task with null result by using a helper Task
                $task = [System.Threading.Tasks.Task]::FromResult($null)
            }
            $sendTasks[$key] = @{ Task = $task; PingObj = $ping }
        }

        # Wait for all tasks to complete (individual tasks respect the timeout)
        $taskArray = $sendTasks.Values | ForEach-Object { $_.Task }
        try {
            if ($taskArray.Count -gt 0) { [System.Threading.Tasks.Task]::WaitAll($taskArray) }
        } catch {
            # ignore; we'll inspect individual task statuses below
        }

        # Process results for each target
        foreach ($key in $targets.Keys) {
            $t = $targets[$key]
            $t.Sent++
            $entry = $sendTasks[$key]
            $res = $null
            if ($entry.Task -and $entry.Task.Status -eq 'RanToCompletion') {
                try { $res = $entry.Task.Result } catch { $res = $null }
            }

            if ($res -and $res.Status -eq 'Success') {
                $t.SumRtt += $res.RoundtripTime
                $t.ConsecutiveFailures = 0

                if ($t.InOutage) {
                    $t.InOutage = $false
                    $outageEnd = Get-Date
                    $t.OutageEnd = $outageEnd
                    $durationSec = [Math]::Round(($outageEnd - $t.OutageStart).TotalSeconds, 1)
                    $durationFmt = ($outageEnd - $t.OutageStart).ToString("hh\:mm\:ss")
                    $totalOutageSec += $durationSec

                    $successfulPings = ($t.Sent - $t.Failures)
                    $avgRtt = if ($successfulPings -gt 0) { [Math]::Round($t.SumRtt / $successfulPings, 1) } else { 0 }

                    $csvLine = "$($t.Name),$($t.OutageCount),$($t.OutageStart.ToString('yyyy-MM-dd HH:mm:ss')),$($outageEnd.ToString('yyyy-MM-dd HH:mm:ss')),$durationSec,$durationFmt,$($t.Sent),$($t.Failures),$avgRtt"
                    Add-Content -Path $CsvFile -Value $csvLine

                    Write-Log "OUTAGE $($t.Name) #$($t.OutageCount) ENDED — Duration: $durationFmt, AvgRtt: ${avgRtt}ms, PacketsDropped: $($t.Failures) of $($t.Sent)" "SUCCESS"
                }
            } else {
                $t.Failures++
                $t.ConsecutiveFailures++

                if (-not $t.InOutage -and $t.ConsecutiveFailures -ge $FailThreshold) {
                    $t.OutageCount++
                    $t.OutageStart = (Get-Date).AddSeconds(-($FailThreshold * $PollIntervalSec))
                    $t.InOutage = $true
                    Write-Log "OUTAGE $($t.Name) #$($t.OutageCount) STARTED at $($t.OutageStart.ToString('yyyy-MM-dd HH:mm:ss'))" "ERROR"
                } elseif (-not $t.InOutage) {
                    Write-Log "Ping failed for $($t.Name) ($($t.ConsecutiveFailures)/$FailThreshold)" "WARN"
                }
            }
        }

        # Periodic 5-minute summary of stats per target
        if ((Get-Date) -ge $nextReportTime) {
            Write-Log "----- $($ReportIntervalSec/60)-minute summary -----" "INFO"
            foreach ($k in $targets.Keys) {
                $t = $targets[$k]
                $sent = $t.Sent
                $fail = $t.Failures
                $dropPct = if ($sent -gt 0) { [Math]::Round(($fail / $sent) * 100, 2) } else { 0 }
                $successful = $sent - $fail
                $avgRtt = if ($successful -gt 0) { [Math]::Round($t.SumRtt / $successful, 1) } else { 0 }
                Write-Log "$($t.Name): Pings=$sent, Drops=$fail (${dropPct}%), AvgRtt=${avgRtt}ms" "INFO"
            }
            Write-Log "----------------------------" "INFO"
            $nextReportTime = (Get-Date).AddSeconds($ReportIntervalSec)
        }

        # Heartbeat
        $minutesSince = ((Get-Date) - $lastHeartbeat).TotalMinutes
        if ($minutesSince -ge $heartbeatMin) {
            $uptimeTotal = [Math]::Round(((Get-Date) - $StartTime).TotalHours, 1)
            Write-Log "Heartbeat: monitoring for ${uptimeTotal}h" "SUCCESS"
            $lastHeartbeat = Get-Date
        }

        Start-Sleep -Seconds $PollIntervalSec
    }
}
finally {
    # Close any open outages per target
    foreach ($k in $targets.Keys) {
        $t = $targets[$k]
        if ($t.InOutage) {
            $outageEnd = Get-Date
            $t.OutageEnd = $outageEnd
            $durationSec = [Math]::Round(($outageEnd - $t.OutageStart).TotalSeconds, 1)
            $durationFmt = ($outageEnd - $t.OutageStart).ToString("hh\:mm\:ss")
            $totalOutageSec += $durationSec

            $csvLine = "$($t.Name),$($t.OutageCount),$($t.OutageStart.ToString('yyyy-MM-dd HH:mm:ss')),$($outageEnd.ToString('yyyy-MM-dd HH:mm:ss')),$durationSec,$durationFmt (ongoing),$($t.Sent),$($t.Failures),0"
            Add-Content -Path $CsvFile -Value $csvLine
            Write-Log "OUTAGE $($t.Name) #$($t.OutageCount) was still ongoing — logged with current time" "WARN"
        }
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

    # Per-target summary
    foreach ($k in $targets.Keys) {
        $t = $targets[$k]
        $sent = $t.Sent
        $fail = $t.Failures
        $dropPct = if ($sent -gt 0) { [Math]::Round(($fail / $sent) * 100, 3) } else { 0 }
        $successful = $sent - $fail
        $avgRtt = if ($successful -gt 0) { [Math]::Round($t.SumRtt / $successful, 1) } else { 0 }
        Write-Log "  $($t.Name) — Pings: $sent, Drops: $fail (${dropPct}%), AvgRtt: ${avgRtt}ms, Outages: $($t.OutageCount)" "INFO"
    }

    Write-Log "  Total downtime    : $([Math]::Round($totalOutageSec, 1))s" "INFO"
    Write-Log "  Uptime            : $uptimePercent%" "INFO"
    Write-Log "  CSV report        : $CsvFile" "INFO"
    Write-Log "========================================" "INFO"
}
