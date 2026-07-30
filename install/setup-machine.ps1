# Video Studio - new machine setup. Run AFTER unzipping the VA package:
#   right-click -> Run with PowerShell   (or: powershell -ExecutionPolicy Bypass -File setup-machine.ps1)
# Idempotent - safe to run again after a failure; finished stages are quick.
$ErrorActionPreference = "Stop"

$Install = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root    = Split-Path -Parent $Install
Write-Host "=== Video Studio setup ===" -ForegroundColor Cyan
Write-Host "project root: $Root"

# ---------- stage 0: preflight ----------
Write-Host "`n[0/6] checking prerequisites..." -ForegroundColor Cyan
$py = $null
try { $v = & py -3.11 --version 2>$null; if ($v -match "3\.11") { $py = "py -3.11" } } catch {}
if (-not $py) {
    try { $v = & python --version 2>$null; if ($v -match "3\.11") { $py = "python" } } catch {}
}
if (-not $py) {
    Write-Host "Python 3.11 is required (3.12/3.13 break the pinned AI packages)." -ForegroundColor Yellow
    Write-Host "Install it from https://www.python.org/downloads/release/python-3119/ (check 'py launcher'), then re-run."
    exit 1
}
Write-Host "  python 3.11: OK ($py)"

$gpu = $false
try { & nvidia-smi *> $null; $gpu = ($LASTEXITCODE -eq 0) } catch {}
if ($gpu) { Write-Host "  NVIDIA GPU: OK" }
else {
    Write-Host "  NVIDIA GPU: NOT FOUND - the free local engines (dub, lip-sync, subtitle erase," -ForegroundColor Yellow
    Write-Host "  transcription) will not run on this machine. Paid fal.ai features still work." -ForegroundColor Yellow
}

$ff = $false
try { & ffmpeg -version *> $null; $ff = ($LASTEXITCODE -eq 0) } catch {}
if (-not $ff) {
    Write-Host "  ffmpeg: installing via winget..."
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
    Write-Host "  NOTE: open a NEW terminal after setup so ffmpeg is on PATH." -ForegroundColor Yellow
} else { Write-Host "  ffmpeg: OK" }

function New-Venv($dir, $req) {
    $vpy = Join-Path $dir "Scripts\python.exe"
    if (-not (Test-Path $vpy)) {
        Write-Host "  creating venv $dir"
        Invoke-Expression "$py -m venv `"$dir`""
    }
    Write-Host "  installing $req (this can take a while - big CUDA wheels)"
    & $vpy -m pip install --upgrade pip --quiet
    & $vpy -m pip install -r (Join-Path $Install $req)
    if ($LASTEXITCODE -ne 0) { throw "pip install failed for $req" }
}

# ---------- stage 1-3: the three venvs ----------
Write-Host "`n[1/6] engines venv (autoVSL\.venv)..." -ForegroundColor Cyan
New-Venv (Join-Path $Root "autoVSL\.venv") "requirements-cv.txt"

Write-Host "`n[2/6] transcription venv (course_pipeline\.venv)..." -ForegroundColor Cyan
New-Venv (Join-Path $Root "course_pipeline\.venv") "requirements-whisper.txt"

Write-Host "`n[3/6] dubbing venv (dubbing-studio\venv)..." -ForegroundColor Cyan
New-Venv (Join-Path $Root "dubbing-studio\venv") "requirements-dub.txt"
# basicsr expects torchvision's removed functional_tensor module - re-point the import
$dubPy = Join-Path $Root "dubbing-studio\venv\Scripts\python.exe"
& $dubPy -c "import pathlib,re; p=pathlib.Path(r'$Root')/'dubbing-studio/venv/Lib/site-packages/basicsr/data/degradations.py'; t=p.read_text(encoding='utf-8'); n=t.replace('from torchvision.transforms.functional_tensor import rgb_to_grayscale','from torchvision.transforms.functional import rgb_to_grayscale'); p.write_text(n,encoding='utf-8') if n!=t else None; print('  basicsr patch: OK')"

# ---------- stage 4: config for THIS machine ----------
Write-Host "`n[4/6] rewriting config paths + fresh secrets..." -ForegroundColor Cyan
$cfgPath = Join-Path $Root "video-studio\config.json"
# config.json is not in git (it holds this machine's session key + phone PIN);
# a fresh clone starts from the template and gets its own secrets below.
if (-not (Test-Path $cfgPath)) {
    Copy-Item (Join-Path $Root "video-studio\config.example.json") $cfgPath
    Write-Host "  created config.json from config.example.json"
}
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
$oldRoot = ($cfg.autovsl_root -replace "/autoVSL$", "") -replace "\\autoVSL$", ""
$newRoot = $Root -replace "\\", "/"
foreach ($prop in $cfg.PSObject.Properties) {
    if ($prop.Value -is [string]) { $prop.Value = $prop.Value.Replace($oldRoot, $newRoot) }
    elseif ($prop.Value -is [PSCustomObject]) {
        foreach ($p2 in $prop.Value.PSObject.Properties) {
            if ($p2.Value -is [string]) { $p2.Value = $p2.Value.Replace($oldRoot, $newRoot) }
        }
    }
}
$bytes = New-Object byte[] 32; (New-Object Security.Cryptography.RNGCryptoServiceProvider).GetBytes($bytes)
$cfg.secret_key = -join ($bytes | ForEach-Object { $_.ToString("x2") })
$cfg.remote_pin = "{0:D6}" -f (Get-Random -Minimum 100000 -Maximum 999999)
$cfg.exports_dir = (Join-Path ([Environment]::GetFolderPath("Desktop")) "Video Studio") -replace "\\", "/"
$cfg | ConvertTo-Json -Depth 6 | Set-Content $cfgPath -Encoding UTF8
Write-Host "  paths now under: $newRoot"
Write-Host "  phone-access PIN for this machine: $($cfg.remote_pin)"

# ---------- stage 5: fal.ai key (optional, paid features) ----------
Write-Host "`n[5/6] fal.ai key (paid cloud features - press Enter to skip)..." -ForegroundColor Cyan
$envFile = Join-Path $Root "autoVSL\.env"
if (-not (Test-Path $envFile)) {
    $key = Read-Host "  paste FAL_KEY (or Enter for local-only mode)"
    if ($key) { "FAL_KEY=$key" | Set-Content $envFile -Encoding ascii; Write-Host "  saved to autoVSL\.env" }
    else { Write-Host "  skipped - premium dub / Image-to-Video / fit-extend stay off until a key is added" }
} else { Write-Host "  autoVSL\.env already present" }

# ---------- stage 6: desktop app + smoke test ----------
Write-Host "`n[6/6] installing shortcuts + first boot..." -ForegroundColor Cyan
& (Join-Path $Root "video-studio\launcher\install-shortcuts.ps1")
$auto = Read-Host "  start the server automatically at login? (y/N)"
if ($auto -match "^[Yy]") { & (Join-Path $Root "video-studio\launcher\enable-autostart.ps1") }

& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "video-studio\launcher\start-video-studio.ps1") -ServerOnly
$ok = $false
foreach ($i in 1..30) {
    try { $r = Invoke-WebRequest -Uri "http://localhost:5180/api/settings" -UseBasicParsing -TimeoutSec 2
          if ($r.StatusCode -eq 200) { $ok = $true; break } } catch {}
    Start-Sleep -Seconds 2
}
Write-Host ""
if ($ok) {
    Write-Host "=== Video Studio is INSTALLED and RUNNING ===" -ForegroundColor Green
    Write-Host "open it with the 'Video Studio' desktop icon (or http://localhost:5180)"
    Write-Host "notes: first transcription downloads the whisper model (~1.5 GB, one time)."
    Write-Host "       the AI script rewrite needs Claude Code installed + logged in on this machine."
} else {
    Write-Host "server did not answer yet - open the 'Video Studio' desktop icon to retry," -ForegroundColor Yellow
    Write-Host "and if it still fails send install\setup.log + a screenshot back to the owner."
}
