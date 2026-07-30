#!/usr/bin/env bash
# Assemble a VSL locally with ffmpeg — free, no Palmier generation needed.
set -euo pipefail

SLUG="${1:-fairy-flame}"
NO_MUSIC="${NO_MUSIC:-0}"
if [[ "${2:-}" == "--no-music" ]]; then NO_MUSIC=1; fi
# pwd -W gives a Windows-style path under Git Bash (needed by Windows Python)
ROOT="$(cd "$(dirname "$0")/.." && (pwd -W 2>/dev/null || pwd))"
export PYTHONUTF8=1
VSL_DIR="$ROOT/vsls/$SLUG"
MEDIA="$VSL_DIR/media"
OUT_DIR="$ROOT/output"
OUT_FILE="$OUT_DIR/$SLUG.mp4"
WORK="$OUT_DIR/.work-$SLUG"
PY="$ROOT/.venv/bin/python"; [[ -x "$PY" ]] || PY="$ROOT/.venv/Scripts/python.exe"; [[ -x "$PY" ]] || PY=python3

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found. Install: brew install ffmpeg" >&2
  exit 1
fi

if [[ "$NO_MUSIC" == "1" ]]; then
  "$PY" - "$SLUG" "$ROOT" <<'CHECK'
import json, sys
from pathlib import Path
slug, root = sys.argv[1:3]
root = Path(root)
with open(root / "vsls" / slug / "timeline.json") as f:
    t = json.load(f)
media_root = root / t["media_root"]
missing = []
for seg in t["segments"]:
    for key in ("video", "vo"):
        p = media_root / seg[key]
        if not p.is_file():
            missing.append(str(p))
if missing:
    print("Missing:", *missing, sep="\n  ")
    sys.exit(1)
print("Video + VO ready (no music)")
CHECK
else
  "$ROOT/scripts/check-media.sh" "$SLUG" || {
    echo "Fix missing media first." >&2
    exit 1
  }
fi

mkdir -p "$OUT_DIR" "$WORK"
rm -f "$WORK"/*

"$PY" - "$SLUG" "$ROOT" "$WORK" "$OUT_FILE" "$NO_MUSIC" <<'PY'
import json, subprocess, sys, shutil
from pathlib import Path

slug, root, work, out_file, no_music = sys.argv[1:6]
root = Path(root)
work = Path(work)
skip_music = no_music == "1"

with open(root / "vsls" / slug / "timeline.json") as f:
    timeline = json.load(f)

media_root = root / timeline["media_root"]
segments = timeline["segments"]
music_cfg = timeline.get("music", {})
xfade = 0.4  # seconds crossfade between shots

# Step 1: trim each video clip to match its VO duration
trimmed = []
for i, seg in enumerate(segments):
    video = media_root / seg["video"]
    vo = media_root / seg["vo"]
    seg_out = work / f"seg-{i:02d}.mp4"

    # Get VO duration
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(vo)],
        capture_output=True, text=True, check=True,
    )
    dur = float(probe.stdout.strip()) + 0.3  # small breathing room

    subprocess.run([
        "ffmpeg", "-y", "-i", str(video), "-i", str(vo),
        "-t", str(dur),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2",
        "-shortest",
        str(seg_out),
    ], check=True, capture_output=True)
    trimmed.append(seg_out)
    print(f"  segment {i+1}: {dur:.1f}s")

# Step 2: concat segments with crossfade
if len(trimmed) == 1:
    video_only = trimmed[0]
else:
    # Build xfade filter chain
    inputs = []
    for t in trimmed:
        inputs.extend(["-i", str(t)])

    # Simple concat (xfade across many clips gets complex; concat is reliable)
    concat_list = work / "concat.txt"
    with concat_list.open("w") as f:
        for t in trimmed:
            f.write(f"file '{t}'\n")

    video_only = work / "video-track.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", str(video_only),
    ], check=True, capture_output=True)
    print("  concatenated video+vo track")

# Step 3: mix background music under video (optional)
music = media_root / music_cfg["file"]
if skip_music or not music.is_file():
    shutil.copy2(video_only, out_file)
    print("  skipped background music")
else:
    music_vol = music_cfg.get("volume", 0.15)
    subprocess.run([
        "ffmpeg", "-y",
        "-i", str(video_only),
        "-i", str(music),
        "-filter_complex",
        f"[1:a]volume={music_vol}[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-t", str(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_only)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()),
        str(out_file),
    ], check=True, capture_output=True)

print(f"\n✓ Exported: {out_file}")
PY

echo "Cost: \$0 (local ffmpeg)"
