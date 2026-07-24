' Silent wrapper — stops the server with zero console flash.
Dim shell, fso, here
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("Wscript.Shell")
shell.Run "powershell -NoProfile -ExecutionPolicy Bypass -File """ & here & "\stop-video-studio.ps1""", 0, False
