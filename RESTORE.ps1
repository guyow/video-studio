# RESTORE.ps1 — Rebuild Video Studio to working state
# Run from: C:\Users\guyas\Claude\Projects\Video AI editing\

param(
    [switch]$SkipVenv = $false,
    [switch]$Quick = $false
)

$ErrorActionPreference = "Stop"
$root = (Get-Item $PSScriptRoot).FullName

Write-Host @"

═══════════════════════════════════════════════════════════════════════════════
          🎬 VIDEO STUDIO RESTORE — PHASE 1: Working State
═══════════════════════════════════════════════════════════════════════════════

Target: C:\Users\guyas\Claude\Projects\Video AI editing\
Goal:   Restore all 4 Python venvs + verify system works

"@ -ForegroundColor Cyan

if (!$SkipVenv) {
    Write-Host "📦 Step 1: Creating Python Virtual Environments" -ForegroundColor Yellow
    Write-Host "This may take 5-10 minutes (torch install is slow)…`n"

    # 1. autoVSL venv (main app + OpenCV)
    Write-Host "  • autoVSL/.venv (Flask + OpenCV + numpy)" -ForegroundColor Gray
    $path = Join-Path $root "autoVSL"
    Push-Location $path
    & python -m venv .venv --clear 2>&1 | Out-Null
    & .\.venv\Scripts\pip install --upgrade pip setuptools wheel 2>&1 | Out-Null
    $pkgs = @(
        "flask", "werkzeug", "requests", "pillow", "opencv-python",
        "numpy", "scipy", "torch", "torchvision", "tqdm"
    )
    & .\.venv\Scripts\pip install $pkgs 2>&1 | Out-Null
    Write-Host "    ✓ Done" -ForegroundColor Green
    Pop-Location

    # 2. course_pipeline venv (whisper)
    Write-Host "  • course_pipeline/.venv (faster-whisper)" -ForegroundColor Gray
    $path = Join-Path $root "course_pipeline"
    Push-Location $path
    & python -m venv .venv --clear 2>&1 | Out-Null
    & .\.venv\Scripts\pip install --upgrade pip 2>&1 | Out-Null
    & .\.venv\Scripts\pip install "faster-whisper" "pydub" 2>&1 | Out-Null
    Write-Host "    ✓ Done" -ForegroundColor Green
    Pop-Location

    # 3. dubbing-studio venv (XTTS, TTS, torch) — HEAVY
    Write-Host "  • dubbing-studio/venv (torch + XTTS + TTS)" -ForegroundColor Yellow
    Write-Host "    Installing torch for cu126 (RTX 3050 Ti)..." -ForegroundColor Gray
    $path = Join-Path $root "dubbing-studio"
    Push-Location $path
    & python -m venv venv --clear 2>&1 | Out-Null
    & .\venv\Scripts\pip install --upgrade pip 2>&1 | Out-Null
    Write-Host "    Torch install may take 3-5 minutes..." -ForegroundColor Gray
    & .\venv\Scripts\pip install torch torchvision torchaudio `
        --index-url https://download.pytorch.org/whl/cu126 2>&1 | Out-Null
    Write-Host "    ✓ Torch installed" -ForegroundColor Green
    $pkgs = @(
        "TTS", "librosa", "numba", "scipy", "pydub", "tqdm", "face_detection"
    )
    & .\venv\Scripts\pip install $pkgs 2>&1 | Out-Null
    Write-Host "    ✓ Done" -ForegroundColor Green
    Pop-Location

    # 4. tools/vsr venv (optional, upscaling)
    Write-Host "  • tools/vsr/.venv (optional — video upscaling)" -ForegroundColor Gray
    $vsr_path = Join-Path $root "tools/vsr"
    if (Test-Path $vsr_path) {
        Push-Location $vsr_path
        & python -m venv .venv --clear 2>&1 | Out-Null
        & .\.venv\Scripts\pip install --upgrade pip 2>&1 | Out-Null
        Write-Host "    ✓ Created (skip deps for now)" -ForegroundColor Green
        Pop-Location
    }

    Write-Host "`n✅ All venvs ready`n" -ForegroundColor Green
}

Write-Host "📥 Step 2: Verify Model Weights Directory Structure" -ForegroundColor Yellow

$dirs = @(
    "tools/Wav2Lip/checkpoints",
    "tools/Wav2Lip/gfpgan_weights",
    "tools/ProPainter"
)
foreach ($d in $dirs) {
    $p = Join-Path $root $d
    if (!(Test-Path $p)) {
        New-Item -ItemType Directory -Force $p | Out-Null
        Write-Host "  • Created: $d" -ForegroundColor Gray
    }
}
Write-Host "✅ Directories ready`n" -ForegroundColor Green

Write-Host "⚠️  Step 3: MANUAL — Download Model Weights" -ForegroundColor Yellow
Write-Host @"
  The following files must be downloaded manually (external repos):

  1️⃣  Wav2Lip Checkpoint (370 MB)
      URL: https://github.com/Rudrabha/Wav2Lip/releases/download/v1.0/wav2lip_gan.pth
      Save to: tools/Wav2Lip/checkpoints/wav2lip_gan.pth

  2️⃣  GFPGAN v1.4 (348 MB)
      URL: https://github.com/TencentARC/GFPGAN/releases/download/v1.3.8/GFPGANv1.4.pth
      Save to: tools/Wav2Lip/gfpgan_weights/GFPGANv1.4.pth

  📦 OPTIONAL:
      • ProPainter (subtitle erase): https://github.com/sczhou/ProPainter
        Extract to: tools/ProPainter/

      • ComfyUI (Brand Studio): https://github.com/comfyui-org/ComfyUI/releases
        Download portable, extract to: ComfyUI_windows_portable/

"@ -ForegroundColor Cyan

if (!$Quick) {
    $prompt = Read-Host "Press Enter after downloading (or type 'skip' to skip verification)"
    if ($prompt -eq "skip") {
        Write-Host "`n⏭️  Skipping verification..." -ForegroundColor Yellow
        exit 0
    }
}

Write-Host "`n✅ Step 4: Verify Setup" -ForegroundColor Yellow

$checks = @{
    "autoVSL/.venv" = "Main venv (Flask + OpenCV)"
    "course_pipeline/.venv" = "Whisper venv"
    "dubbing-studio/venv" = "Dubbing venv (torch)"
    "tools/Wav2Lip/checkpoints/wav2lip_gan.pth" = "Wav2Lip checkpoint"
    "tools/Wav2Lip/gfpgan_weights/GFPGANv1.4.pth" = "GFPGAN weights"
}

$missing = @()
foreach ($check in $checks.GetEnumerator()) {
    $p = Join-Path $root $check.Key
    if (Test-Path $p) {
        Write-Host "  ✓ $($check.Value)" -ForegroundColor Green
    } else {
        Write-Host "  ✗ $($check.Value) — MISSING" -ForegroundColor Red
        $missing += $check.Key
    }
}

if ($missing.Count -gt 0) {
    Write-Host "`n❌ Missing files:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "     $_" }
    Write-Host "`n⚠️  Download the above files before starting the server.`n"
} else {
    Write-Host "`n✅ All files present!`n" -ForegroundColor Green
}

Write-Host "═══════════════════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "NEXT: Start the server" -ForegroundColor Yellow
Write-Host @"

From PowerShell:
  cd autoVSL
  .\.venv\Scripts\python app/server.py

Then open: http://localhost:5180

Test the workflow:
  1. Upload a video
  2. Transcribe it
  3. Dub with local engine (XTTS + Wav2Lip)
  4. Export to Desktop

═══════════════════════════════════════════════════════════════════════════════
"@ -ForegroundColor Cyan
