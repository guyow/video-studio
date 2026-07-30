@echo off
REM Double-click me to save all code changes to GitHub.
REM Safe: refuses to commit big files (videos/weights) even if a rule is missed.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "install\push-to-github.ps1"
echo.
pause
