# "Install" Video Studio on this PC: Desktop + Start Menu shortcuts with the
# brand icon, pointing at the silent launcher. Run again anytime to repair.
$ErrorActionPreference = "Stop"

$Launcher = Split-Path -Parent $MyInvocation.MyCommand.Path
$Ico      = Join-Path $Launcher "video-studio.ico"
$StartVbs = Join-Path $Launcher "Video Studio.vbs"
$StopVbs  = Join-Path $Launcher "Stop Video Studio.vbs"

$Desktop   = [Environment]::GetFolderPath("Desktop")
$StartMenu = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\Video Studio"
New-Item -ItemType Directory -Force -Path $StartMenu | Out-Null

$shell = New-Object -ComObject WScript.Shell
function Make-Lnk($path, $target, $icon, $desc) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = "wscript.exe"
    $lnk.Arguments = """$target"""
    $lnk.WorkingDirectory = Split-Path -Parent $target
    $lnk.IconLocation = $icon
    $lnk.Description = $desc
    $lnk.Save()
}

Make-Lnk (Join-Path $Desktop "Video Studio.lnk")   $StartVbs $Ico "Open Video Studio (starts the server if needed)"
Make-Lnk (Join-Path $StartMenu "Video Studio.lnk") $StartVbs $Ico "Open Video Studio (starts the server if needed)"
Make-Lnk (Join-Path $StartMenu "Stop Video Studio.lnk") $StopVbs $Ico "Stop the Video Studio server"

Write-Host "installed:"
Write-Host "  $Desktop\Video Studio.lnk"
Write-Host "  $StartMenu\Video Studio.lnk"
Write-Host "  $StartMenu\Stop Video Studio.lnk"
