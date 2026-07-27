#!/usr/bin/env python3
"""Text-to-Video (continue a real clip) — keep a copied segment of an uploaded
video, then AI-generate new footage that continues from it per a new script.

Flow (all cost-gated by the server before the paid step runs):
  1. TRIM the chosen [start,end] segment of the source (kept verbatim — real
     footage, audio preserved).
  2. grab that segment's LAST FRAME as the seed.
  3. fal.ai (i2v_gen.py) generates `seconds` of new footage from the seed,
     guided by the new script/prompt.
  4. CONCAT kept-segment + generated (normalized to the source frame), pad the
     kept segment's audio with trailing silence over the generated tail, and
     verify the length (never -shortest).

Output: output/i2v/<slug>/clip.mp4 + i2v.json — so it shows up under Media in
the editor automatically, exactly like an Image->Video result.

Runs under the cv venv (shells i2v_gen.py + ffmpeg). stdlib only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def log(m: str) -> None:
    print(m, flush=True)


def die(m: str) -> None:
    print(f"ERROR: {m}", flush=True)
    sys.exit(1)


def run(cmd: list[str], label: str, timeout: int | None = None) -> str:
    log(f"  {label}...")
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if r.returncode != 0:
        die(f"{label} failed:\n{(r.stderr or r.stdout or '')[-1200:]}")
    return r.stdout or ""


def ff(args: list[str], label: str) -> None:
    run(["ffmpeg", "-y", "-loglevel", "error", *args], label)


def probe_sec(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    try:
        return float((r.stdout or "0").strip() or 0)
    except ValueError:
        return 0.0


def probe_wh(p: Path) -> tuple[int, int]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(p)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        w, h = (r.stdout or "").strip().split("x")[:2]
        return int(w), int(h)
    except (ValueError, IndexError):
        die(f"could not read resolution of {p.name}")


def has_audio(p: Path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                        "stream=codec_type", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return "audio" in (r.stdout or "")


def last_frame(video: Path, dest: Path) -> None:
    try:
        ff(["-sseof", "-0.2", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(dest)], "grab last frame")
    except SystemExit:
        ff(["-i", str(video), "-vf", "reverse", "-frames:v", "1", "-q:v", "2", str(dest)], "grab last frame (fallback)")
    if not dest.is_file():
        die("could not grab the copied segment's last frame")


def concat_continue(segment: Path, generated: Path, out: Path, w: int, h: int) -> float:
    """Normalize both to WxH/30fps, concat (video), then re-attach the kept
    segment's audio padded with silence across the generated tail. Returns the
    final duration. No -shortest anywhere."""
    norm = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1")
    silent = out.with_name("_ext-silent.mp4")
    ff(["-i", str(segment), "-i", str(generated), "-filter_complex",
        f"[0:v]{norm}[v0];[1:v]{norm}[v1];[v0][v1]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30",
        str(silent)], "concat kept segment + generated")
    total = probe_sec(silent)
    if has_audio(segment):
        # keep the segment's real audio; pad silence over the generated tail
        ff(["-i", str(silent), "-i", str(segment), "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-filter:a", "apad", "-c:a", "aac", "-b:a", "192k",
            "-t", f"{total:.3f}", str(out)], "attach kept audio + silence tail")
    else:
        ff(["-i", str(silent), "-c", "copy", str(out)], "finalize (silent source)")
    silent.unlink(missing_ok=True)
    return probe_sec(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=0.0)      # 0 = to end of source
    ap.add_argument("--work", required=True)               # output/i2v/<slug>
    ap.add_argument("--name", required=True)
    ap.add_argument("--cv-py", dest="cv_py", required=True)
    ap.add_argument("--i2v", required=True)
    ap.add_argument("--env-file", dest="env_file", required=True)
    ap.add_argument("--model", default="kling-2.1")
    ap.add_argument("--aspect", default="9:16")
    ap.add_argument("--seconds", type=int, default=10)
    ap.add_argument("--seg", type=int, default=10)
    ap.add_argument("--prompt", required=True)
    a = ap.parse_args()

    source = Path(a.source).resolve()
    if not source.is_file():
        die(f"source video not found: {source}")
    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)

    dur = probe_sec(source)
    start = max(0.0, min(a.start, max(0.0, dur - 0.2)))
    end = a.end if a.end and a.end > start else dur
    end = min(end, dur)
    keep = round(end - start, 2)
    if keep < 0.3:
        die("the copied segment is too short — pick a wider range")
    w, h = probe_wh(source)
    log(f"source {dur:.1f}s {w}x{h} - copying {start:.1f}s->{end:.1f}s ({keep:.1f}s), "
        f"generating {a.seconds}s more on {a.model}")

    # 1) trim the kept segment (re-encode so the concat inputs match cleanly)
    segment = work / "kept-segment.mp4"
    ff(["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(source),
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "192k", str(segment)], "copy the chosen segment")

    # 2) seed
    seed = work / "seed.jpg"
    last_frame(segment, seed)

    # 3) generate the continuation (paid fal step)
    gen_dir = work / "gen"
    gen_dir.mkdir(parents=True, exist_ok=True)
    log("generating the continuation on fal.ai (this is the paid step)...")
    run([a.cv_py, a.i2v, "--image", str(seed), "--prompt", a.prompt,
         "--out", str(gen_dir), "--name", f"{a.name}-gen", "--model", a.model,
         "--aspect", a.aspect, "--seconds", str(a.seconds), "--env-file", a.env_file],
        "fal.ai generation", timeout=3600)
    gen = gen_dir / "clip.mp4"
    if not gen.is_file() or gen.stat().st_size == 0:
        die("generation produced no clip")
    gen_sec = probe_sec(gen)

    # 4) stitch
    out = work / "clip.mp4"
    final = concat_continue(segment, gen, out, w, h)
    if abs(final - (keep + gen_sec)) > 1.0:
        die(f"stitched length {final:.1f}s != kept {keep:.1f}s + generated {gen_sec:.1f}s")

    (work / "i2v.json").write_text(json.dumps({
        "name": a.name, "prompt": a.prompt, "model": a.model,
        "model_label": a.model, "aspect": a.aspect, "seconds": round(final, 2),
        "kind": "t2v-continue", "kept_from": source.name,
        "kept_sec": keep, "generated_sec": round(gen_sec, 2),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, indent=1), encoding="utf-8")

    log("")
    log(f"OK kept {keep:.1f}s + generated {gen_sec:.1f}s -> {final:.1f}s clip")
    log(f"deliverable: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
