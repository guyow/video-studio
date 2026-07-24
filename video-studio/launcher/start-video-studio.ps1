# Video Studio launcher — boots the server hidden (if it isn't already running)
# and opens the UI in its own app window. Safe to run twice: an already-healthy
# server is reused, never doubled.
$ErrorActionPreference = "SilentlyContinue"

$Launcher = Split-Path -Parent $MyInvocation.MyCommand.Path   # ...\video-studio\launcher
$Repo     = Split-Path -Parent (Split-Path -Parent $Launcher) # project root
$PyExe    = Join-Path $Repo "autoVSL\.venv\Scripts\python.exe"
$Server   = Join-Path $Repo "video-studio\app\server.py"
$Url      = "http://localhost:5180"

function Test-Health {
    try {
        $r = Invoke-WebRequest -Uri "$Url/api/settings" -UseBasicParsing -TimeoutSec 2
        return $r.StatusCode -eq 200
    } catch { return $false }
}
function Test-PortBound {
    $null -ne (Get-NetTCPConnection -LocalPort 5180 -State Listen -ErrorAction SilentlyContinue)
}

if (-not (Test-Health)) {
    # port already bound but not healthy yet = a server is BOOTING — never
    # double-spawn into that race, just wait for it below
    if (-not (Test-PortBound)) {
        if (-not (Test-Path $PyExe)) {
            [System.Windows.Forms.MessageBox]::Show("Video Studio venv not found:`n$PyExe", "Video Studio") | Out-Null
            exit 1
        }
        Start-Process -FilePath $PyExe -ArgumentList "`"$Server`"" -WorkingDirectory $Repo -WindowStyle Hidden
    }
    # wait for the server to come up (first boot loads config + job store)
    $deadline = (Get-Date).AddSeconds(45)
    while (-not (Test-Health)) {
        if ((Get-Date) -gt $deadline) { break }
        Start-Sleep -Milliseconds 700
    }
}

# open as a standalone app window (no browser chrome, own taskbar entry)
$edge   = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
$chrome = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe") |
          Where-Object { Test-Path $_ } | Select-Object -First 1
if (Test-Path $edge) {
    Start-Process $edge -ArgumentList "--app=$Url", "--window-size=1480,920"
} elseif ($chrome) {
    Start-Process $chrome -ArgumentList "--app=$Url", "--window-size=1480,920"
} else {
    Start-Process $Url    # plain default browser as a last resort
}
