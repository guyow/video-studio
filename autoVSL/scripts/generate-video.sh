#!/usr/bin/env bash
# Generate video shots via fal.ai API (cheapest model, not Kling)
set -euo pipefail

# pwd -W gives a Windows-style path under Git Bash (needed by Windows Python)
ROOT="$(cd "$(dirname "$0")/.." && (pwd -W 2>/dev/null || pwd))"
VENV="$ROOT/.venv"
export PYTHONUTF8=1

# Windows venvs use Scripts/, Unix uses bin/
PY="$VENV/bin/python"; [[ -x "$PY" ]] || PY="$VENV/Scripts/python.exe"
if [[ ! -x "$PY" ]]; then
  echo "Creating virtual environment..."
  (python3 -m venv "$VENV" 2>/dev/null || py -m venv "$VENV")
  PY="$VENV/bin/python"; [[ -x "$PY" ]] || PY="$VENV/Scripts/python.exe"
  "$PY" -m pip install -q -r "$ROOT/requirements.txt"
fi

exec "$PY" "$ROOT/scripts/generate-video.py" "$@"
