# Register the login autostart: at every logon the Video Studio SERVER boots
# silently (no window). The desktop icon then opens the UI instantly.
# Run disable-autostart.ps1 to remove it.
$ErrorActionPreference = "Stop"

$Launcher = Split-Path -Parent $MyInvocation.MyCommand.Path
$Vbs = Join-Path $Launcher "Video Studio Server.vbs"

$action   = New-ScheduledTaskAction -Execute "wscript.exe" -Argument """$Vbs"""
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# zero time limit = never kill the task's process tree; laptop-friendly battery flags
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "Video Studio Server" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Boots the Video Studio server silently at login" -Force | Out-Null
Write-Host "autostart ENABLED - the Video Studio server will boot silently at every login"
