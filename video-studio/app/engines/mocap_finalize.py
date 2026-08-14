#!/usr/bin/env python3
"""Motion Capture finalize — browser webm recording → delivered mp4.

    python mocap_finalize.py --rec <rec.webm> --out <out.mp4> [--audio-from <src>]

The mocap tab records the composited canvas with MediaRecorder, which yields
VP8/VP9 webm with, at best, opus mic audio — and often a broken duration header.
This pass makes it a real deliverable:

  * one clean H.264/AAC encode (yuv420p, faststart, even dimensions);
  * audio priority: the recording's own track (live mic) wins; otherwise the
    original source video's audio is muxed back in (--audio-from) — the canvas
    was recorded in real time while that source played, so the clocks line up;
  * verify the output actually has frames before calling it delivered.
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


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def probe(path: Path, entries: str, stream: str = "v:0") -> str:
    r = run([ff("ffprobe"), "-v", "error", "-select_streams", stream,
             "-show_entries", entries, "-of", "default=nw=1:nk=1", str(path)])
    return (r.stdout or "").strip().splitlines()[0] if (r.stdout or "").strip() else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rec", required=True, help="the recorded webm")
    ap.add_argument("--out", required=True, help="final mp4 path")
    ap.add_argument("--audio-from", default="", help="mux audio from this source video")
    a = ap.parse_args()

    rec, out = Path(a.rec), Path(a.out)
    if not rec.is_file():
        print(f"ERROR: no such recording {rec}", flush=True)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)

    rec_audio = bool(probe(rec, "stream=index", "a:0"))
    src = Path(a.audio_from) if a.audio_from else None
    src_audio = bool(src and src.is_file() and probe(src, "stream=index", "a:0"))

    cmd = [ff("ffmpeg"), "-y", "-v", "error", "-i", str(rec)]
    if rec_audio:
        print("audio: keeping the recording's own track (mic)", flush=True)
        cmd += ["-map", "0:v", "-map", "0:a"]
    elif src_audio:
        print(f"audio: muxing back from source {src.name}", flush=True)
        cmd += ["-i", str(src), "-map", "0:v", "-map", "1:a", "-shortest"]
    else:
        print("audio: none (silent take)", flush=True)
        cmd += ["-map", "0:v", "-an"]

    cmd += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
            "-pix_fmt", "yuv420p"]
    if rec_audio or src_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    cmd += ["-movflags", "+faststart", str(out)]

    print("encoding…", flush=True)
    r = run(cmd)
    if r.returncode != 0 or not out.is_file():
        print(f"ERROR: encode failed:\n{(r.stderr or '')[-1000:]}", flush=True)
        return 1

    dur_s = probe(out, "format=duration", "v:0") or "0"
    try:
        dur = float(dur_s)
    except ValueError:
        dur = 0.0
    if dur < 0.2:
        out.unlink(missing_ok=True)
        print("ERROR: output has no frames — take deleted, not delivered", flush=True)
        return 1

    print(f"verify: {dur:.2f}s", flush=True)
    print(f"RESULT: {out.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
