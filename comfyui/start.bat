@echo off
REM ============================================================
REM  ComfyUI launcher
REM  ---- Only works after install.bat has been run ----
REM
REM  1. Activates .venv
REM  2. Clears inherited PYTHONPATH (Hermes venv contamination)
REM  3. Calls scripts/launch_comfyui.py which:
REM     - loads .env via python-dotenv
REM     - launches ComfyUI bound to 127.0.0.1 by default
REM
REM  To expose on LAN (trusted networks only), edit
REM  COMFYUI_LISTEN in scripts/launch_comfyui.py.
REM ============================================================

setlocal

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv\ not found. Run install.bat first.
    pause
    exit /b 1
)

if not exist "ComfyUI\main.py" (
    echo [ERROR] ComfyUI\main.py not found. Run install.bat first.
    pause
    exit /b 1
)

REM Clear PYTHONPATH to avoid Hermes venv contamination on this host
set "PYTHONPATH="
.venv\Scripts\python.exe scripts\launch_comfyui.py

endlocal
