#!/usr/bin/env bash
# Verify all media files referenced in timeline.json exist
set -euo pipefail

SLUG="${1:-fairy-flame}"
# pwd -W gives a Windows-style path under Git Bash (needed by Windows Python)
ROOT="$(cd "$(dirname "$0")/.." && (pwd -W 2>/dev/null || pwd))"
VSL_DIR="$ROOT/vsls/$SLUG"
TIMELINE="$VSL_DIR/timeline.json"
export PYTHONUTF8=1
PY="$ROOT/.venv/bin/python"; [[ -x "$PY" ]] || PY="$ROOT/.venv/Scripts/python.exe"; [[ -x "$PY" ]] || PY=python3

if [[ ! -f "$TIMELINE" ]]; then
  echo "Missing timeline: $TIMELINE" >&2
  exit 1
fi

"$PY" -c "
import json, os, sys

root = '$ROOT'
vsl_dir = '$VSL_DIR'
with open('$TIMELINE') as f:
    t = json.load(f)

media_root = os.path.join(root, t['media_root'])
missing = []
found = []

# Music
music_file = t.get('music', {}).get('file')
if music_file:
    path = os.path.join(media_root, music_file)
    (found if os.path.isfile(path) else missing).append(path)

# Segments
for seg in t.get('segments', []):
    for key in ('video', 'vo'):
        if key in seg:
            path = os.path.join(media_root, seg[key])
            (found if os.path.isfile(path) else missing).append(path)

print(f'VSL: {t.get(\"name\", \"$SLUG\")}')
print(f'Media root: {media_root}')
print()
print(f'Found: {len(found)}')
for p in found:
    print(f'  ✓ {os.path.relpath(p, root)}')
print()
print(f'Missing: {len(missing)}')
for p in missing:
    print(f'  ✗ {os.path.relpath(p, root)}')

sys.exit(1 if missing else 0)
"
