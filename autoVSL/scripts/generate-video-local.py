#!/usr/bin/env python3
"""Generate VSL shots LOCALLY & FREE via ComfyUI (no fal.ai, no API cost).

Drop-in sibling of generate-video.py: it reads the SAME
vsls/<slug>/kling-shots.json and writes the SAME filenames into
vsls/<slug>/media/video/ — so downstream steps (check-media, assemble-vsl)
don't change. The difference is the shots are rendered on the local GPU
through a running ComfyUI instead of a paid endpoint.

  Video (experimental on 4 GB):  python generate-video-local.py fairy-flame
  Stills (reliable on 4 GB):     python generate-video-local.py fairy-flame --stills

Config (via .env or flags):
  COMFYUI_URL             default 127.0.0.1:8188
  COMFYUI_CHECKPOINT      SD1.5 .safetensors in ComfyUI/models/checkpoints
  COMFYUI_MOTION_MODULE   AnimateDiff module in models/animatediff_models
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Windows consoles default to cp1252; force UTF-8 so status glyphs don't crash.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comfyui_client import ComfyUIClient, ComfyUIError  # noqa: E402
from comfyui_workflows import build_animatediff, build_txt2img  # noqa: E402


def load_env() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def dims_for(aspect: str, base: int, stills: bool) -> tuple[int, int]:
    """Return (w, h). Portrait long-side = base for video, larger for stills."""
    long_side = base if not stills else int(base * 1.6)
    if aspect == "16:9":
        return long_side, int(long_side * 9 / 16)
    # default 9:16 vertical
    return int(long_side * 9 / 16), long_side


def main() -> int:
    load_env()
    p = argparse.ArgumentParser(description="Local ComfyUI VSL shot generator (free)")
    p.add_argument("slug", nargs="?", default="fairy-flame")
    p.add_argument("--shot", type=int, help="Only this shot id (for testing)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--stills", action="store_true",
                   help="Render one still image per shot instead of video (reliable on 4 GB)")
    p.add_argument("--url", default=os.environ.get("COMFYUI_URL", "127.0.0.1:8188"))
    p.add_argument("--checkpoint", default=os.environ.get("COMFYUI_CHECKPOINT"))
    p.add_argument("--motion-module", default=os.environ.get("COMFYUI_MOTION_MODULE"))
    p.add_argument("--frames", type=int, default=16)
    p.add_argument("--fps", type=int, default=8)
    p.add_argument("--steps", type=int, default=20)
    p.add_argument("--base", type=int, default=512, help="Long-side pixels (shrink if OOM)")
    p.add_argument("--seed", type=int, default=1)
    args = p.parse_args()

    root = Path(__file__).resolve().parent.parent
    shots_file = root / "vsls" / args.slug / "kling-shots.json"
    out_dir = root / "vsls" / args.slug / "media" / ("stills" if args.stills else "video")

    if not shots_file.exists():
        print(f"Missing {shots_file}", file=sys.stderr)
        return 1

    data = json.loads(shots_file.read_text())
    settings = data.get("settings", {})
    shots = data["shots"]
    if args.shot:
        shots = [s for s in shots if s["id"] == args.shot]
        if not shots:
            print(f"Shot {args.shot} not found", file=sys.stderr)
            return 1

    aspect = settings.get("aspect_ratio", "9:16")
    negative = settings.get("negative_prompt", "text, watermark, low quality, blurry, deformed")
    width, height = dims_for(aspect, args.base, args.stills)
    mode = "STILL image" if args.stills else f"VIDEO {args.frames}f@{args.fps}fps"

    print(f"VSL: {args.slug}  |  mode: {mode}  |  {width}x{height} ({aspect})")
    print(f"Shots: {len(shots)}  →  {out_dir}\n")

    if args.dry_run:
        for s in shots:
            print(f"  shot-{s['id']:02d} → {s['filename']}")
        print("\n(dry run — nothing generated; local GPU cost = $0)")
        return 0

    client = ComfyUIClient(args.url)
    if not client.ping():
        print(f"ComfyUI not reachable at {args.url}. Start it "
              f"(run_nvidia_lowvram.bat) then retry.", file=sys.stderr)
        return 1

    # Resolve models (auto-pick first available if unset)
    ckpt = args.checkpoint
    if not ckpt:
        cands = client.checkpoints()
        if not cands:
            print("No checkpoints installed in ComfyUI/models/checkpoints.", file=sys.stderr)
            return 1
        ckpt = cands[0]
    motion = args.motion_module
    if not args.stills and not motion:
        mm = client.motion_modules()
        if not mm:
            print("No AnimateDiff motion module in models/animatediff_models.\n"
                  "Use --stills, or install a motion module first.", file=sys.stderr)
            return 1
        motion = mm[0]
    print(f"checkpoint: {ckpt}" + ("" if args.stills else f"  |  motion: {motion}") + "\n")

    out_dir.mkdir(parents=True, exist_ok=True)
    for shot in shots:
        base_name = shot["filename"]
        if args.stills:
            dest = out_dir / (Path(base_name).stem + ".png")
        else:
            dest = out_dir / base_name
        if dest.exists():
            print(f"  shot-{shot['id']:02d} … skip (exists)")
            continue

        print(f"  shot-{shot['id']:02d} … rendering", end="", flush=True)
        try:
            if args.stills:
                wf = build_txt2img(
                    checkpoint=ckpt, positive=shot["prompt"], negative=negative,
                    width=width, height=height, seed=args.seed + shot["id"],
                    steps=max(args.steps, 25),
                    filename_prefix=f"autovsl/{args.slug}/shot{shot['id']:02d}",
                )
            else:
                wf = build_animatediff(
                    checkpoint=ckpt, motion_module=motion,
                    positive=shot["prompt"], negative=negative,
                    width=width, height=height, num_frames=args.frames,
                    fps=args.fps, steps=args.steps, seed=args.seed + shot["id"],
                    filename_prefix=f"autovsl/{args.slug}/shot{shot['id']:02d}",
                )
            files = client.run(wf)
            if not files:
                print(" FAILED: no output produced", file=sys.stderr)
                return 1
            client.download(files[-1], dest)
            print(f" → {dest.name}")
        except ComfyUIError as e:
            print(f" FAILED: {e}", file=sys.stderr)
            return 1

    print(f"\n✓ Done. Files in {out_dir}  (cost: $0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
