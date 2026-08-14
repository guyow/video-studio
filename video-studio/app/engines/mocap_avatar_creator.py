#!/usr/bin/env python3
"""Avatar Creator — your webcam take drives an uploaded face image.

    python mocap_avatar_creator.py --rec take.webm --face avatar.jpg --out out.mp4

The Motion Capture tab records you raw on the webcam (video + mic). This pass:

  1. conforms the browser webm to a clean 30fps CFR mp4 (MediaRecorder writes
     variable frame timing, which would drift the lip-sync in the transfer);
  2. hands it to the LivePortrait engine as the driving video — the uploaded
     image performs your take: head, expressions, blinks, lips, your voice.

Free and local (ComfyUI on the 4GB card).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

FFMPEG_BIN = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1.2-full_build/bin"
)


def ff(name: str) -> str:
    exe = FFMPEG_BIN / f"{name}.exe"
    return str(exe) if exe.is_file() else name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rec", required=True, help="raw webcam webm from the browser")
    ap.add_argument("--face", required=True, help="the avatar image to animate")
    ap.add_argument("--out", required=True)
    ap.add_argument("--comfy", default="127.0.0.1:8188")
    a = ap.parse_args()

    rec, out = Path(a.rec), Path(a.out)
    if not rec.is_file():
        print(f"ERROR: no such recording {rec}", flush=True)
        return 1
    work = out.parent / f".{out.stem}-work"
    work.mkdir(parents=True, exist_ok=True)
    driving = work / "driving.mp4"

    print("conforming the webcam take to 30fps…", flush=True)
    r = subprocess.run(
        [ff("ffmpeg"), "-y", "-v", "error", "-i", str(rec),
         "-vf", "scale='min(1280,iw)':-2,fps=30",
         "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
         "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         str(driving)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not driving.is_file():
        print(f"ERROR: conform failed:\n{(r.stderr or '')[-500:]}", flush=True)
        return 1

    lp = Path(__file__).resolve().parent / "mocap_liveportrait.py"
    p = subprocess.Popen(
        [sys.executable, "-u", str(lp), "--face", a.face, "--driving", str(driving),
         "--out", str(out), "--comfy", a.comfy],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    for line in p.stdout:
        print(line.rstrip(), flush=True)
    rc = p.wait()
    if rc == 0:
        driving.unlink(missing_ok=True)
        try:
            work.rmdir()
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
