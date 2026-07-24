# Build the give-to-a-VA package: one ZIP of the whole project INCLUDING model
# weights (Wav2Lip, XTTS, erase engine) but EXCLUDING venvs (not relocatable),
# private content (uploads/outputs/exports), git history, and secrets (.env).
# The VA unzips it anywhere and runs install\setup-machine.ps1.
$ErrorActionPreference = "Stop"

$Install = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root    = Split-Path -Parent $Install
$Parent  = Split-Path -Parent $Root
$Name    = Split-Path -Leaf $Root
$OutZip  = Join-Path ([Environment]::GetFolderPath("Desktop")) "VideoStudio-VA-package.zip"

Write-Host "packaging '$Name' -> $OutZip"
Write-Host "(includes model weights ~3 GB - this takes a few minutes)"

if (Test-Path $OutZip) { Remove-Item $OutZip -Force }

# bsdtar ships with Windows; much faster than Compress-Archive on big trees
$excludes = @(
    "*/.venv/*", "*/venv/*",            # python venvs - rebuilt by setup-machine.ps1
    "*/.git/*", "*/.github/*",          # repo history
    "*/__pycache__/*", "*.pyc",
    "*/output/*", "*/outputs/*",         # renders, dubs, workspaces (private)
    "*/comfyui-output/*",
    "*/uploads/*",                       # source videos (private)
    "*/files/*",                         # subtitle-studio inputs (private)
    "*/jobs/*",                          # job logs
    "*/.trash/*", "*/temp/*",
    "*/.env",                            # FAL_KEY - never ship a paid API key
    "*/ComfyUI*/*",                      # optional local image AI - too big; VA can skip
    "*/courses/*",                       # the user's PAID course videos - never ship
    "*/tools/vsr/*",                     # legacy subtitle remover - unused by the app
    "*/vsls/*",                          # produced ad videos (private)
    "*.tar.gz"                           # stray archives
) | ForEach-Object { "--exclude"; $_ }

& tar.exe -a -c -f $OutZip @excludes -C $Parent $Name
if ($LASTEXITCODE -ne 0) { throw "tar failed with exit code $LASTEXITCODE" }

$size = [math]::Round((Get-Item $OutZip).Length / 1GB, 2)
Write-Host "done: $OutZip ($size GB)"
Write-Host "send this to the VA + docs\GIVE-TO-VA.md has their instructions"
