#!/usr/bin/env python3
"""Generate a face for the avatar bank.

    python seq_avatar_gen.py --prompt "woman in her 30s, warm smile" \
        --out <avatar dir>/face.png --env-file <autoVSL>/.env

Uses Nano Banana ($0.039/image — cheapest imagen on fal). The prompt is wrapped
with the reference-face rules from the face-swap skill: the swap engines want
one frontal, evenly-lit, neutral head-and-shoulders portrait — that framing is
worth more than any model parameter, so it is enforced here rather than hoped
for in the user's prompt.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

FACE_FRAME = (", photorealistic head-and-shoulders portrait, facing the camera "
              "directly, even soft studio lighting, neutral pleasant expression, "
              "plain neutral background, sharp focus, high detail, no sunglasses, "
              "no extreme angle")
# when the prompt already describes wardrobe/scene (e.g. from 🔍 Describe),
# forcing a plain background would fight it — keep only the framing rules
FACE_FRAME_LITE = (", photorealistic portrait, face clearly visible and facing "
                   "the camera, sharp focus, high detail")
SCENE_WORDS = ("wearing", "background", "shirt", "jacket", "hoodie", "dress",
               "suit", "t-shirt", "sweater", "outdoor", "indoor", "room",
               "street", "office", "kitchen", "studio", "car", "scene")


def load_env(path: Path) -> None:
    if not path or not Path(path).is_file():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--env-file", default="")
    a = ap.parse_args()

    if a.env_file:
        load_env(Path(a.env_file))
    if not os.environ.get("FAL_KEY"):
        print("ERROR: FAL_KEY not set — add it to autoVSL/.env", flush=True)
        return 1

    import fal_client
    import httpx

    ptxt = a.prompt.strip()
    frame_suffix = FACE_FRAME_LITE if any(w in ptxt.lower() for w in SCENE_WORDS) else FACE_FRAME
    prompt = ptxt + frame_suffix
    print(f"generating face: {a.prompt.strip()[:80]}", flush=True)
    try:
        res = fal_client.subscribe("fal-ai/nano-banana", arguments={
            "prompt": prompt, "num_images": 1, "aspect_ratio": "1:1"},
            with_logs=False)
    except Exception as exc:
        print(f"ERROR: fal call failed: {exc}", flush=True)
        return 1

    url = None
    imgs = (res or {}).get("images") or []
    if imgs and isinstance(imgs[0], dict):
        url = imgs[0].get("url")
    if not url:
        print(f"ERROR: no image in response: {str(res)[:300]}", flush=True)
        return 1

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=300) as c:
        r = c.get(url)
        r.raise_for_status()
        out.write_bytes(r.content)
    if out.stat().st_size < 5000:
        print("ERROR: downloaded image is suspiciously small", flush=True)
        return 1
    print(f"RESULT: {out.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
