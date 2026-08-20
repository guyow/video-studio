# Stop Video Studio - kills every python whose command line runs THIS app's
# server.py (the venv shim parent AND the real interpreter child; on Windows the
# venv python.exe spawns the base python as a child, and Flask's debug reloader
# can add another pair - killing only one respawns/orphans the other).
param([switch]$Force)   # -Force: skip the running-jobs guard
$ErrorActionPreference = "SilentlyContinue"

# GUARD: refuse to stop while a generation is running — restarts used to kill
# other people's (paid) jobs. Jobs now survive restarts via the detached
# runner, but not stopping mid-run is still the polite default.
if (-not $Force) {
    try {
        $jobs = Invoke-RestMethod -Uri "http://localhost:5180/api/jobs" -TimeoutSec 3
        $running = @($jobs | Where-Object { $_.status -eq "running" })
        if ($running.Count -gt 0) {
            Write-Host "REFUSING to stop: $($running.Count) job(s) still running:" -ForegroundColor Yellow
            $running | ForEach-Object { Write-Host ("  - " + $_.label + "  (started " + ([DateTimeOffset]::FromUnixTimeSeconds([long]$_.started).LocalDateTime.ToString("HH:mm")) + ")") }
            Write-Host "Wait for them to finish, or re-run with -Force to stop anyway."
            exit 1
        }
    } catch {}   # server not answering = nothing to protect
}

$procs = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
         Where-Object { $_.CommandLine -match "video-studio\\app\\server\.py" }

if (-not $procs) {
    Write-Host "Video Studio wasn't running"
    exit 0
}
# parents (venv shim / reloader) first, children after - no respawn window
$ordered = $procs | Sort-Object { if ($procs.ProcessId -contains $_.ParentProcessId) { 0 } else { 1 } }
$killed = @()
foreach ($p in $ordered) {
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $killed += $p.ProcessId } catch {}
}
Write-Host ("stopped Video Studio (pid " + ($killed -join ", ") + ")")
