#!/usr/bin/env python3
"""Image -> Video via fal.ai. Upload a still + a prompt, get a clip.

No single fal.ai model renders 30s in one shot (they top out at ~5-10s), so
we generate the clip in SEGMENTS and chain them: the last frame of each
segment becomes the input image for the next, so a 30s clip stays continuous
from your one uploaded picture. Segments are concatenated with ffmpeg (exact
frame counts, never -shortest).

Runs under the cv venv (fal_client + httpx present); ffmpeg on PATH.

  python i2v_gen.py --image pic.jpg --prompt "slow push-in, cinematic" \
      --out output/i2v/my-clip --name my-clip --model kling-2.1 \
      --aspect 9:16 --seconds 30 --env-file <autoVSL>/.env
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ── model registry ──────────────────────────────────────────────────────────
# seg: the per-segment length (s) we request; cost: rough USD per segment
# (labelled ESTIMATE in the UI — fal's real invoice is authoritative).
MODELS = {
    "kling-2.1": {
        "label": "Kling 2.1 Standard — balanced (recommended)",
        "endpoint": "fal-ai/kling-video/v2.1/standard/image-to-video",
        "seg": 10, "durations": ["5", "10"], "cost_per_seg": 0.45,
        "dur_param": "duration", "dur_str": True,
    },
    "kling-2.1-pro": {
        "label": "Kling 2.1 Pro — best quality (pricey)",
        "endpoint": "fal-ai/kling-video/v2.1/pro/image-to-video",
        "seg": 10, "durations": ["5", "10"], "cost_per_seg": 0.95,
        "dur_param": "duration", "dur_str": True,
    },
    "hailuo-02": {
        "label": "MiniMax Hailuo 02 — great motion",
        "endpoint": "fal-ai/minimax/hailuo-02/standard/image-to-video",
        "seg": 10, "durations": ["6", "10"], "cost_per_seg": 0.48,
        "dur_param": "duration", "dur_str": True,
    },
    "wan-2.2": {
        "label": "Wan 2.2 — budget",
        "endpoint": "fal-ai/wan/v2.2-a14b/image-to-video",
        "seg": 5, "durations": ["5"], "cost_per_seg": 0.20,
        "dur_param": "duration", "dur_str": True,
    },
}

NEG = "blurry, distorted, warped face, low quality, watermark, text artifacts"


def log(m: str) -> None:
    # The Windows console is cp1252 by default, so any non-ASCII in a message
    # (emoji, en/em dashes, middots) raises UnicodeEncodeError. That would kill
    # the process AFTER the paid fal.ai call has already succeeded, throwing
    # away generated footage the user has been billed for. Never let a log line
    # do that.
    try:
        print(m, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(m.encode(enc, "replace").decode(enc, "replace"), flush=True)


def load_env(env_file: Path) -> None:
    if not env_file or not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def ff(args: list[str]) -> None:
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + (r.stderr or "")[-400:])


def probe_dur(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                       capture_output=True, text=True)
    try:
        return float((r.stdout or "0").strip())
    except ValueError:
        return 0.0


def last_frame(video: Path, dest: Path) -> None:
    """The TRUE final frame — the visual seed for the next segment.

    `-update 1` rewrites the same file for every decoded frame, so the one left on
    disk is the last. `-frames:v 1` grabbed the FIRST frame after the seek instead,
    i.e. ~0.2s before the end, so every segment restarted from a moment that had
    already played — a visible jump back at each join."""
    ff(["-sseof", "-0.6", "-i", str(video), "-update", "1", "-q:v", "2", str(dest)])
    if not dest.is_file():                       # fallback: first frame of a reversed copy
        ff(["-i", str(video), "-vf", "reverse", "-frames:v", "1", "-q:v", "2", str(dest)])


def assemble(seg_files: list[Path], final: Path, blend: float) -> None:
    """Join the chained segments into one clip.

    Each segment starts from the previous one's last frame, so the geometry already
    lines up — what gives a join away is the encode/exposure step at the cut. A few
    frames of dissolve absorb it. With blend=0 we instead drop each segment's first
    frame, which is a re-render of the seed and would otherwise hold for two frames
    (a small but visible stutter)."""
    if len(seg_files) == 1:
        ff(["-i", str(seg_files[0]), "-c:v", "libx264", "-crf", "18",
            "-pix_fmt", "yuv420p", "-r", "30", str(final)])
        return

    inputs = []
    for p in seg_files:
        inputs += ["-i", str(p)]
    norm = "fps=30,setsar=1,format=yuv420p"
    parts = []
    for i in range(len(seg_files)):
        drop = "" if (blend > 0 or i == 0) else "trim=start_frame=1,setpts=PTS-STARTPTS,"
        parts.append(f"[{i}:v]{drop}{norm}[a{i}]")
    if blend > 0:
        cur, run_len = "[a0]", probe_dur(seg_files[0])
        for i in range(1, len(seg_files)):
            offset = max(0.0, run_len - blend - 1 / 30)
            parts.append(f"{cur}[a{i}]xfade=transition=fade:duration={blend:.4f}"
                         f":offset={offset:.4f}[x{i}]")
            cur = f"[x{i}]"
            run_len = run_len + probe_dur(seg_files[i]) - blend
        filt = ";".join(parts) + f";{cur}null[v]"
        log(f"  joining {len(seg_files)} segments with a {blend * 30:.0f}-frame dissolve")
    else:
        streams = "".join(f"[a{i}]" for i in range(len(seg_files)))
        filt = ";".join(parts) + f";{streams}concat=n={len(seg_files)}:v=1:a=0[v]"
    ff([*inputs, "-filter_complex", filt, "-map", "[v]",
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30", str(final)])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--out", required=True, help="workdir")
    ap.add_argument("--name", required=True)
    ap.add_argument("--model", default="kling-2.1", choices=list(MODELS))
    ap.add_argument("--aspect", default="9:16", choices=["9:16", "16:9", "1:1"])
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--env-file")
    ap.add_argument("--blend", type=float, default=0.12,
                    help="seconds of dissolve between chained segments (0 = hard cut)")
    args = ap.parse_args()

    load_env(Path(args.env_file) if args.env_file else None)
    if not os.environ.get("FAL_KEY"):
        sys.exit("FAL_KEY not set — add it to autoVSL/.env")

    import fal_client
    import httpx

    # cheap pre-flight: a tiny upload fails fast if the account is locked / out of
    # balance, so we never start a paid render that can't complete.
    try:
        fal_client.upload(b"ok", "text/plain")
    except Exception as exc:                      # noqa: BLE001
        msg = str(exc).lower()
        if any(w in msg for w in ("403", "locked", "balance", "exhaust", "unauthor")):
            sys.exit(f"fal.ai account problem (check balance / FAL_KEY): {exc}")

    model = MODELS[args.model]
    seg_len = model["seg"]
    n_seg = max(1, round(args.seconds / seg_len))
    work = Path(args.out)
    work.mkdir(parents=True, exist_ok=True)
    src_img = Path(args.image)
    if not src_img.is_file():
        sys.exit(f"image not found: {src_img}")

    log(f"Image -> Video: {args.name}")
    log(f"model {model['label']}  ·  {n_seg} segment(s) x {seg_len}s ~= {n_seg * seg_len}s"
        f"  ·  {args.aspect}")
    log(f"prompt: {args.prompt}")

    dur_val = str(seg_len) if model["dur_str"] else seg_len
    if str(seg_len) not in model["durations"]:
        dur_val = model["durations"][-1]

    seg_files: list[Path] = []
    cur_img = src_img
    total_cost = 0.0

    for i in range(n_seg):
        log(f"\n=== stage: segment {i + 1}/{n_seg} ===")
        log(f"  uploading seed image ({cur_img.name}) ...")
        image_url = fal_client.upload_file(str(cur_img))
        log(f"  generating {seg_len}s on fal.ai ({model['endpoint']}) ...")
        arguments = {
            "prompt": args.prompt,
            "image_url": image_url,
            model["dur_param"]: dur_val,
            "aspect_ratio": args.aspect,
            "negative_prompt": NEG,
        }
        try:
            res = fal_client.subscribe(model["endpoint"], arguments=arguments, with_logs=False)
        except Exception as exc:                 # noqa: BLE001
            sys.exit(f"fal.ai generation failed on segment {i + 1}: {exc}")
        video = res.get("video") or {}
        url = video.get("url") if isinstance(video, dict) else video
        if not url:
            sys.exit(f"no video returned for segment {i + 1}: {str(res)[:300]}")
        seg_path = work / f"seg-{i:02d}.mp4"
        with httpx.Client(follow_redirects=True, timeout=600) as c:
            r = c.get(url)
            r.raise_for_status()
            seg_path.write_bytes(r.content)
        got = probe_dur(seg_path)
        total_cost += model["cost_per_seg"]
        log(f"  segment {i + 1} done: {seg_path.name} ({got:.1f}s)  ~${model['cost_per_seg']:.2f}")
        seg_files.append(seg_path)
        if i < n_seg - 1:                        # seed the next segment
            cur_img = work / f"seed-{i + 1:02d}.jpg"
            last_frame(seg_path, cur_img)

    log("\n=== stage: assemble ===")
    final = work / "clip.mp4"
    assemble(seg_files, final, max(0.0, min(args.blend, seg_len * 0.3)))
    if not final.is_file() or final.stat().st_size == 0:
        sys.exit("assembly produced no output")

    dur = probe_dur(final)
    (work / "i2v.json").write_text(json.dumps({
        "name": args.name, "prompt": args.prompt, "model": args.model,
        "model_label": model["label"], "aspect": args.aspect,
        "segments": n_seg, "seg_len": seg_len, "seconds": round(dur, 1),
        "est_cost": round(total_cost, 2), "created": time.time(),
        "source_image": src_img.name,
    }, indent=2), encoding="utf-8")

    # keep the seed of the whole clip as a poster; drop intermediate seeds
    for p in work.glob("seed-*.jpg"):
        p.unlink(missing_ok=True)

    log(f"\ndeliverable: {final}  ({dur:.1f}s)")
    log(f"estimated fal.ai cost: ~${total_cost:.2f}")
    log("Done ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
