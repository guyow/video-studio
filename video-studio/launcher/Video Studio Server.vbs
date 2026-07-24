' Silent server-only boot (used by the login autostart task) — no window opens.
Dim shell, fso, here
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("Wscript.Shell")
shell.Run "powershell -NoProfile -ExecutionPolicy Bypass -File """ & here & "\start-video-studio.ps1"" -ServerOnly", 0, False
