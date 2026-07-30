@echo off
REM One-click launcher for the local stack (run after any reboot).
REM Starts: ComfyUI (127.0.0.1:8188) + autoVSL Mission Control (127.0.0.1:5170).
REM The dubbing Gradio tool (7860) is NOT started on purpose — idle XTTS holds
REM 1-2 GB VRAM and slows ComfyUI. Start it only when needed:
REM   dubbing-studio\venv\Scripts\python.exe dubbing-studio\app.py

echo Starting ComfyUI on http://127.0.0.1:8188 ...
start "ComfyUI" /min cmd /c "cd /d C:\ComfyUI_windows_portable && .\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build"

echo Starting autoVSL Mission Control on http://127.0.0.1:5170 ...
start "autoVSL Dashboard" /min cmd /c "cd /d "%~dp0autoVSL" && .venv\Scripts\python.exe dashboard\server.py"

echo.
echo Both launching (minimized windows). Give them ~30s, then open:
echo   http://127.0.0.1:5170        (Mission Control - /studio, /dubbing, /eraser, /qc)
echo   http://127.0.0.1:8188        (ComfyUI)
pause
