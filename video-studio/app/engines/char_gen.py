#!/usr/bin/env python3
"""Character Generator — text description -> portrait candidates on fal (FLUX).

Feeds the Character Bank: the user describes a persona (or lets Claude invent
one), this renders N candidate portraits, the user picks the one that IS the
character, and that image becomes the reference Kling O1 locks onto for every
shot. Cheap ($0.003/image on FLUX schnell) but still money — the server
cost-gates it like every fal call.

  python char_gen.py --prompt "woman in her 40s, warm smile..." --count 4 \
      --out-dir <dir> --env-file <autoVSL>/.env [--estimate-only]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ENDPOINT = "fal-ai/flux/schnell"
COST_PER_IMAGE = 0.003          # estimate — fal's invoice is the authority

# the character sheet look: a clean neutral portrait Kling O1 can lock onto,
# with the same anti-gloss realism the shot prompts use
PORTRAIT_SUFFIX = (" Chest-up portrait, facing camera, neutral warm expression, plain "
                   "soft-lit indoor background, realistic skin texture with visible pores, "
                   "no makeup look, candid smartphone photo, not cinematic, no studio "
                   "lighting, no bokeh.")


def log(m: str) -> None:
    print(m, flush=True)


def die(m: str) -> None:
    print(f"ERROR: {m}", flush=True)
    sys.exit(1)


def load_env(path: Path | None) -> None:
    if path and path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True, help="who the character is")
    ap.add_argument("--count", type=int, default=4)
    ap.add_argument("--out-dir", dest="out_dir", required=True)
    ap.add_argument("--env-file", dest="env_file")
    ap.add_argument("--estimate-only", action="store_true")
    a = ap.parse_args()

    count = max(1, min(8, a.count))
    est = {"this_run": round(count * COST_PER_IMAGE, 2), "engine": "fal-flux",
           "model": "flux-schnell", "images": count,
           "summary": f"{count} portrait candidate(s) on FLUX schnell "
                      f"= ~${count * COST_PER_IMAGE:.2f} (estimate)"}
    if a.estimate_only:
        print("ESTIMATE: " + json.dumps(est))
        return 0

    load_env(Path(a.env_file) if a.env_file else None)
    if not os.environ.get("FAL_KEY"):
        die("FAL_KEY not set — add it to autoVSL/.env")
    import fal_client
    import httpx

    out_dir = Path(a.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt = a.prompt.strip().rstrip(".") + "." + PORTRAIT_SUFFIX
    log(f"Generating {count} portrait candidate(s) — {est['summary']}")
    log(f"  prompt: {prompt[:160]}")

    made = []
    res = fal_client.subscribe(ENDPOINT, arguments={
        "prompt": prompt, "image_size": "portrait_4_3",
        "num_images": count, "enable_safety_checker": True,
    }, with_logs=False)
    images = res.get("images") or []
    with httpx.Client(follow_redirects=True, timeout=300) as c:
        for i, img in enumerate(images, 1):
            url = img.get("url") if isinstance(img, dict) else img
            if not url:
                continue
            out = out_dir / f"cand-{i:02d}.jpg"
            r = c.get(url)
            r.raise_for_status()
            out.write_bytes(r.content)
            made.append(out.name)
            log(f"  OK {out.name}")

    (out_dir / "gen.json").write_text(json.dumps(
        {"prompt": a.prompt, "count": count, "made": made,
         "cost_estimate": est["this_run"]}, indent=2, ensure_ascii=False), encoding="utf-8")
    if not made:
        die("fal returned no images")
    log("")
    log(f"Done: {len(made)} candidate(s) in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
