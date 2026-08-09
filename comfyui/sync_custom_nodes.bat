@echo off
REM ============================================================
REM  Sync custom nodes — Windows wrapper
REM  Calls scripts/sync_custom_nodes.sh via Git Bash.
REM
REM  Usage:
REM    sync_custom_nodes.bat              (sync all)
REM    sync_custom_nodes.bat my_node       (sync one)
REM ============================================================

setlocal

set "SCRIPT_DIR=%~dp0"
set "APP_ROOT=%SCRIPT_DIR%.."

REM Find Git Bash (try common locations)
where bash >nul 2>nul
if errorlevel 1 (
    echo [ERROR] bash not on PATH. Install Git for Windows.
    pause
    exit /b 1
)

REM Convert Windows path → MSYS path for bash
set "MSYS_SCRIPT=%APP_ROOT%\comfyui\scripts\sync_custom_nodes.sh"
for /f "usebackq delims=" %%P in (`bash -c "cygpath -u '%MSYS_SCRIPT%'"`) do set "MSYS_SCRIPT_UNIX=%%P"

bash "%MSYS_SCRIPT_UNIX%" %*

endlocal
