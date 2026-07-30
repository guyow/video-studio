# Build a CODE-ONLY zip to hand a developer.
#
# Includes: every source file, config, doc and requirements pin.
# Excludes: your videos and renders, model weights, venvs, git history, job
#           logs, course material - and the fal.ai key. Nothing private or
#           multi-gigabyte leaves the machine.
#
# The result is a few MB: enough to read, run and modify the app, and
# install\setup-machine.ps1 rebuilds the environments from the pinned
# requirements on their side.
$ErrorActionPreference = "Stop"

$Install = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root    = Split-Path -Parent $Install
$Parent  = Split-Path -Parent $Root
$Name    = Split-Path -Leaf $Root
$OutZip  = Join-Path ([Environment]::GetFolderPath("Desktop")) "VideoStudio-DEV-code.zip"

Write-Host "packaging CODE ONLY from '$Name'"
Write-Host "  -> $OutZip"

if (Test-Path $OutZip) { Remove-Item $OutZip -Force }

$excludes = @(
    # --- environments & build junk (rebuilt from install\requirements-*.txt) ---
    "*/.venv/*", "*/venv/*", "*/python_embeded/*",
    "*/__pycache__/*", "*.pyc", "*.pyo", "*/node_modules/*", "*/.pytest_cache/*",
    "*/.git/*", "*/.github/*",
    # --- secrets ---
    "*/.env", "*/*.env",
    # --- the user's content: sources, renders, workspaces, logs ---
    "*/uploads/*", "*/output/*", "*/outputs/*", "*/files/*", "*/jobs/*",
    "*/comfyui-output/*", "*/vsls/*", "*/.trash/*", "*/temp/*", "*/results/*",
    "*/courses/*",
    # --- model weights & bulky third-party trees ---
    "*/models/*", "*/checkpoints/*", "*/weights/*", "*/tools/vsr/*",
    "*/ComfyUI*/*", "*/gfpgan/*", "*/CodeFormer/*", "*/ProPainter/*",
    # --- any stray media / binary model file, wherever it hides ---
    "*.mp4", "*.mov", "*.mkv", "*.webm", "*.m4v", "*.avi",
    "*.mp3", "*.wav", "*.m4a", "*.flac",
    "*.pth", "*.ckpt", "*.safetensors", "*.pt", "*.bin", "*.onnx", "*.tar.gz",
    "*.psd", "*.zip"
) | ForEach-Object { "--exclude"; $_ }

& tar.exe -a -c -f $OutZip @excludes -C $Parent $Name
if ($LASTEXITCODE -ne 0) { throw "tar failed with exit code $LASTEXITCODE" }

$mb = [math]::Round((Get-Item $OutZip).Length / 1MB, 1)
Write-Host ""
Write-Host "done: $OutZip  ($mb MB)"
Write-Host "Tell the developer: read video-studio\README.md + docs\ARCHITECTURE.md,"
Write-Host "then run install\setup-machine.ps1 to build the 3 venvs from the pinned"
Write-Host "requirements. They supply their own FAL_KEY for the paid features."
