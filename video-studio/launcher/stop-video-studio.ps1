# Stop Video Studio - kills every python whose command line runs THIS app's
# server.py (the venv shim parent AND the real interpreter child; on Windows the
# venv python.exe spawns the base python as a child, and Flask's debug reloader
# can add another pair - killing only one respawns/orphans the other).
$ErrorActionPreference = "SilentlyContinue"

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
