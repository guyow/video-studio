@echo off
REM ============================================================
REM  ComfyUI stop helper
REM  Kills any python process serving ComfyUI on the default
REM  port. Use Task Manager for more control.
REM ============================================================

setlocal
set "PORT=8188"

for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    echo Killing PID %%P on port %PORT%
    taskkill /F /PID %%P
)

echo Done.
endlocal
