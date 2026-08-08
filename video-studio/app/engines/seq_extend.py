#!/usr/bin/env python3
"""Seamless video extend — the forward→reverse boomerang (ping-pong) method.

    python seq_extend.py --src <video> --out <out.mp4> --seconds 30

Free and local. Alternates forward / reversed copies of the clip until the
target length is reached, then trims exactly. The three fixes that make the
seam invisible (learned the hard way, see the seamless-loop skill):

  1. drop the duplicated turnaround frame at every junction (otherwise the
     video "sticks" for one frame at each bounce);
  2. one clean re-encode over the whole chain — no per-segment codec drift;
  3. never -shortest: cut with -t and verify the output duration with ffprobe,
     refusing to deliver a short take.

Audio: the boomerang trick is for PICTURE. Reversed audio sounds wrong, so the
original audio is forward-looped to the target length. For dub work that audio
gets replaced anyway.
"""
from __future__ import annotations

import argparse
import math
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
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=float, required=True)
    a = ap.parse_args()

    src, out = Path(a.src), Path(a.out)
    if not src.is_file():
        print(f"ERROR: no such file {src}", flush=True)
        return 1

    dur_s = probe(src, "format=duration", "v:0") or "0"
    try:
        dur = float(dur_s)
    except ValueError:
        dur = 0.0
    if dur <= 0.2:
        print("ERROR: could not read the source duration", flush=True)
        return 1
    if dur > 90:
        print("ERROR: the free loop extend is for clips up to 90s (the reverse "
              "pass holds frames in memory). Trim the clip first, or use an AI "
              "extend model.", flush=True)
        return 1
    target = max(1.0, float(a.seconds))
    if target <= dur + 0.2:
        print(f"ERROR: the clip is already {dur:.1f}s — nothing to extend to "
              f"{target:.1f}s", flush=True)
        return 1

    fr = probe(src, "stream=r_frame_rate", "v:0") or "30/1"
    try:
        n, _, d = fr.partition("/")
        fps = float(n) / float(d or 1)
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    frame = 1.0 / max(1.0, fps)
    has_audio = bool(probe(src, "stream=index", "a:0"))

    work = out.parent / f".{out.stem}-work"
    work.mkdir(parents=True, exist_ok=True)
    rev = work / "rev.mp4"
    print(f"source {dur:.2f}s @ {fps:.2f}fps → target {target:.2f}s", flush=True)

    # one reversed copy, silent (audio is handled separately)
    print("building the reversed pass…", flush=True)
    r = run([ff("ffmpeg"), "-y", "-v", "error", "-i", str(src),
             "-vf", "reverse", "-an",
             "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", str(rev)])
    if r.returncode != 0 or not rev.is_file():
        print(f"ERROR: reverse pass failed:\n{(r.stderr or '')[-800:]}", flush=True)
        return 1

    # ping-pong chain: fwd, rev, fwd, rev … — every segment after the first
    # skips its first frame so the turnaround frame never doubles up
    reps = math.ceil(target / dur) + 1
    lst = work / "chain.txt"
    lines = []
    for i in range(reps):
        piece = src if i % 2 == 0 else rev
        lines.append(f"file '{piece.as_posix()}'")
        if i > 0:
            lines.append(f"inpoint {frame:.6f}")
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"chaining {reps} passes…", flush=True)
    cmd = [ff("ffmpeg"), "-y", "-v", "error",
           "-f", "concat", "-safe", "0", "-i", str(lst)]
    if has_audio:
        cmd += ["-stream_loop", "-1", "-i", str(src)]          # forward audio, looped
        cmd += ["-map", "0:v", "-map", "1:a"]
    cmd += ["-t", f"{target:.4f}",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
            "-pix_fmt", "yuv420p"]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    cmd += ["-movflags", "+faststart", str(out)]
    r = run(cmd)
    if r.returncode != 0 or not out.is_file():
        print(f"ERROR: chain encode failed:\n{(r.stderr or '')[-1000:]}", flush=True)
        return 1

    got_s = probe(out, "format=duration", "v:0") or "0"
    try:
        got = float(got_s)
    except ValueError:
        got = 0.0
    print(f"verify: {got:.2f}s (target {target:.2f}s)", flush=True)
    if abs(got - target) > max(0.5, target * 0.03):
        out.unlink(missing_ok=True)
        print("ERROR: output length mismatch — take deleted, not delivered", flush=True)
        return 1

    # tidy the workdir
    for p in (rev, lst):
        p.unlink(missing_ok=True)
    try:
        work.rmdir()
    except OSError:
        pass

    print(f"RESULT: {out.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
