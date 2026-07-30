# Save all code changes to GitHub in one step.
# Guards first: nothing over 5 MB and nothing secret-looking gets committed, so a
# stray video or key can't be published even if a .gitignore rule is missed.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "Video Studio -> GitHub" -ForegroundColor Cyan

$changes = git status --porcelain
if (-not $changes) {
    Write-Host "Nothing changed - GitHub is already up to date." -ForegroundColor Green
    exit 0
}

git add -A | Out-Null
$staged = git diff --cached --name-only | Where-Object { $_ }
if (-not $staged) { Write-Host "Nothing to save." -ForegroundColor Green; exit 0 }

# guard 1: big files
$big = @()
foreach ($f in $staged) {
    if (Test-Path -LiteralPath $f -PathType Leaf) {
        $mb = (Get-Item -LiteralPath $f).Length / 1MB
        if ($mb -gt 5) { $big += "$([math]::Round($mb,1)) MB  $f" }
    }
}
# guard 2: obvious secret files
$secret = $staged | Where-Object { $_ -match '(^|/)\.env$|config\.json$|\.pem$|id_rsa' }

if ($big -or $secret) {
    Write-Host ""
    Write-Host "STOPPED - these should not go to GitHub:" -ForegroundColor Yellow
    $big     | ForEach-Object { Write-Host "  big file : $_" }
    $secret  | ForEach-Object { Write-Host "  secret   : $_" }
    Write-Host ""
    Write-Host "Nothing was committed. Ask Claude to sort this out." -ForegroundColor Yellow
    git reset -q
    exit 1
}

Write-Host ""
Write-Host "Saving $($staged.Count) file(s):" -ForegroundColor Cyan
$staged | Select-Object -First 12 | ForEach-Object { Write-Host "  $_" }
if ($staged.Count -gt 12) { Write-Host "  ... and $($staged.Count - 12) more" }

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
git commit -q -m "Update from Video Studio session - $stamp"
git push -q origin master
Write-Host ""
Write-Host "Saved and pushed to https://github.com/guyow/video-studio" -ForegroundColor Green
