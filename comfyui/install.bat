@echo off
REM ============================================================
REM  ComfyUI install script
REM  ---- DO NOT RUN UNTIL YOU ARE READY ----
REM  This script will:
REM    1. Create a Python 3.13 venv via uv
REM    2. Clone ComfyUI core into .\ComfyUI\
REM    3. Install PyTorch (CUDA 13.0) + ComfyUI deps
REM    4. Install launcher utilities (python-dotenv)
REM    5. Clone each custom node from custom_nodes.txt
REM    6. Print a summary
REM ============================================================
REM  Host quirk: PYTHONPATH is contaminated on this machine, so
REM  every Python call is prefixed with `set PYTHONPATH=` to
REM  clear the inherited value before launch.
REM ============================================================

setlocal ENABLEDELAYEDEXPANSION

echo.
echo ============================================================
echo  ComfyUI install
echo  Location: %~dp0
echo ============================================================
echo.

REM ---- 0. Sanity checks ----
where uv >nul 2>nul
if errorlevel 1 (
    echo [ERROR] uv is not on PATH. Install it with:
    echo         irm https://astral.sh/uv/install.ps1 | iex
    echo         or: pip install uv
    exit /b 1
)

where git >nul 2>nul
if errorlevel 1 (
    echo [ERROR] git is not on PATH. Install Git for Windows.
    exit /b 1
)

if not exist ".env" (
    echo [WARN] .env not found. Copying from .env.example.
    copy /Y ".env.example" ".env" >nul
    echo [WARN] Edit .env with your tokens BEFORE running models.
)

REM ---- 1. venv (Python 3.13 via uv) ----
if not exist ".venv\" (
    echo [1/6] Creating venv .venv\ with Python 3.13 via uv ...
    uv venv --python 3.13 .venv
    if errorlevel 1 (
        echo [ERROR] venv creation failed.
        exit /b 1
    )
) else (
    echo [1/6] Reusing existing .venv\
)

REM Clear PYTHONPATH to avoid Hermes venv contamination on this host
set "PYTHONPATH="
call ".venv\Scripts\activate.bat"
set "PYTHONPATH="

REM ---- 2. Clone ComfyUI core ----
if not exist "ComfyUI\" (
    echo [2/6] Cloning ComfyUI core ...
    git clone --depth 1 https://github.com/Comfy-Org/ComfyUI.git ComfyUI
    if errorlevel 1 (
        echo [ERROR] git clone failed.
        exit /b 1
    )
) else (
    echo [2/6] Reusing existing ComfyUI\
)

REM ---- 3. Upgrade pip ----
echo [3/6] Upgrading pip ...
python -m ensurepip --upgrade

REM ---- 4. PyTorch (CUDA 13.0) ----
echo [4/6] Installing PyTorch (CUDA 13.0) ...
python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu130
if errorlevel 1 (
    echo [WARN] CUDA 13.0 install failed. Trying CPU fallback ...
    python -m pip install torch torchvision torchaudio
)

REM ---- 5. ComfyUI core deps + launcher utilities ----
echo [5/6] Installing ComfyUI core deps + launcher utilities ...
python -m pip install -r "ComfyUI\requirements.txt"
python -m pip install -r "requirements.txt"

REM ---- 6. Custom nodes ----
echo [6/6] Installing custom nodes from custom_nodes.txt ...
if exist "custom_nodes.txt" (
    for /f "usebackq tokens=* delims=" %%G in ("custom_nodes.txt") do (
        set "line=%%G"
        if not "!line!"=="" if "!line:~0,1!" NEQ "#" (
            echo   - Cloning !line!
            git clone "!line!" "ComfyUI\custom_nodes\%%~nxG" 2>nul
            if errorlevel 1 (
                echo     [WARN] Clone failed for !line! - may already exist
            )
        )
    )
) else (
    echo   (no custom_nodes.txt found, skipping)
)

echo.
echo ============================================================
echo  Install complete.
echo  Next: run start.bat
echo  Models go into: models\checkpoints\, models\loras\, etc.
echo ============================================================
endlocal
