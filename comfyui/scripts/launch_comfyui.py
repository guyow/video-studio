# ============================================================
# ComfyUI launcher (Python side)
#
# Responsibilities:
#   1. Load .env via python-dotenv (secrets stay out of code)
#   2. Validate the install (.venv, ComfyUI source, main.py)
#   3. cd into ComfyUI/ and exec its main.py with secure defaults
#
# Default bind: 127.0.0.1 (localhost only). To expose on LAN,
# set COMFYUI_LISTEN=0.0.0.0 in .env (or edit DEFAULT_LISTEN below).
# ============================================================

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    print(
        "[launch_comfyui] python-dotenv is not installed.\n"
        "                Run install.bat, or: pip install python-dotenv",
        file=sys.stderr,
    )
    sys.exit(1)

# ---- Constants ----
APP_ROOT = Path(__file__).resolve().parent.parent
COMFYUI_DIR = APP_ROOT / "ComfyUI"
MAIN_PY = COMFYUI_DIR / "main.py"

DEFAULT_LISTEN = "127.0.0.1"
DEFAULT_PORT = 8188


def _die(msg: str, code: int = 1) -> None:
    print(f"[launch_comfyui] {msg}", file=sys.stderr)
    sys.exit(code)


def main() -> None:
    # ---- 1. Load .env (if present) ----
    env_file = APP_ROOT / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)
        print(f"[launch_comfyui] Loaded env from {env_file}")
    else:
        print(
            f"[launch_comfyui] No .env found at {env_file}. "
            "Copy .env.example to .env to set HF_TOKEN, etc."
        )

    # ---- 2. Validate install ----
    if not (APP_ROOT / ".venv").is_dir():
        _die("venv .venv/ not found. Run install.bat first.")
    if not MAIN_PY.is_file():
        _die(f"ComfyUI main.py not found at {MAIN_PY}. Run install.bat first.")

    # ---- 3. Resolve launch params (env > defaults) ----
    listen = os.getenv("COMFYUI_LISTEN", DEFAULT_LISTEN)
    port = os.getenv("COMFYUI_PORT", str(DEFAULT_PORT))

    # Forward any extra ComfyUI flags from COMFYUI_EXTRA_ARGS
    extra = os.getenv("COMFYUI_EXTRA_ARGS", "").split()

    cmd = [
        sys.executable,
        str(MAIN_PY),
        "--listen", listen,
        "--port", port,
        *extra,
    ]

    print(
        f"[launch_comfyui] Starting ComfyUI on http://{listen}:{port}\n"
        f"                 CWD: {COMFYUI_DIR}\n"
        f"                 Cmd: {' '.join(cmd)}\n"
        f"                 Press Ctrl+C to stop."
    )

    # ---- 4. Hand off to ComfyUI ----
    os.chdir(COMFYUI_DIR)
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        _die(f"Failed to exec ComfyUI: {e}")


if __name__ == "__main__":
    main()
