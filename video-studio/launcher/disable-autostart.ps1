# Remove the login autostart task.
$ErrorActionPreference = "SilentlyContinue"
Unregister-ScheduledTask -TaskName "Video Studio Server" -Confirm:$false
Write-Host "autostart DISABLED (the desktop icon still works as before)"
