#!/usr/bin/env python3
"""Mask-native editing on fal.ai — describe it, then change ONLY that.

Phase 2+3 of docs/ACCURATE-EDITING-PLAN.md. Two tasks:

  --task mask   EVF-SAM turns words ("the coffee cup") into a pixel-accurate
                mask. ~$0.005. Nothing is edited — you get a mask + preview to
                approve first.
  --task fill   A mask-native inpainting model rewrites ONLY the masked pixels
                (FLUX.1 [pro] Fill, or Bria GenFill / Eraser).

Unlike Nano Banana — which has no mask input at all and re-renders the whole
frame every call — these models take image + mask. We then composite through
the mask locally anyway and MEASURE what the model did outside it, so "the rest
of the photo is untouched" is a number in the log, not a promise.

  python mask_edit.py --work DIR --image v00.jpg --task mask \
      --text "the coffee cup" --expand 6 --env-file <autoVSL>/.env
  python mask_edit.py --work DIR --image v00.jpg --task fill \
      --text "a jar of liitt gummies" --mask _mask.png --model flux-fill ...

Schemas verified against fal's OpenAPI 2026-08-03:
  fal-ai/evf-sam          prompt, image_url, mask_only, expand_mask, fill_holes,
                          blur_mask, revert_mask, use_grounding_dino
  fal-ai/flux-pro/v1/fill prompt, image_url, mask_url, num_images, output_format,
                          safety_tolerance, seed
  fal-ai/bria/genfill     prompt, image_url, mask_url, negative_prompt, num_images, seed
  fal-ai/bria/eraser      image_url, mask_url, mask_type, preserve_alpha
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import cv2
import numpy as np

SAM_ENDPOINT = "fal-ai/evf-sam"
SAM_COST = 0.005

FILL_MODELS = {
    "flux-fill": {
        "label": "FLUX.1 [pro] Fill — best quality inpainting",
        "endpoint": "fal-ai/flux-pro/v1/fill",
        "per_mp": 0.05, "flat": 0.0, "wants": "prompt",
    },
    "bria-genfill": {
        "label": "Bria GenFill — cheap, commercially licensed",
        "endpoint": "fal-ai/bria/genfill",
        "per_mp": 0.0, "flat": 0.04, "wants": "prompt",
    },
    "bria-eraser": {
        "label": "Bria Eraser — cloud object removal",
        "endpoint": "fal-ai/bria/eraser",
        "per_mp": 0.0, "flat": 0.04, "wants": "nothing",
    },
}


def log(m: str) -> None:
    print(m, flush=True)


def load_env(env_file) -> None:
    p = Path(env_file) if env_file else None
    if not p or not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def fill_cost(model: str, w: int, h: int) -> float:
    m = FILL_MODELS[model]
    return round(m["flat"] + m["per_mp"] * (w * h / 1_000_000.0), 4)


def next_version(work: Path) -> int:
    best = -1
    for p in work.glob("v[0-9][0-9].*"):
        mm = re.match(r"v(\d\d)$", p.stem)
        if mm:
            best = max(best, int(mm.group(1)))
    return best + 1


def overlay_preview(img, mask):
    vis = img.copy()
    red = np.zeros_like(img)
    red[:, :, 2] = 255
    vis = np.where((mask > 127)[:, :, None], cv2.addWeighted(vis, .45, red, .55, 0), vis)
    cont, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(vis, cont, -1, (255, 255, 255), 2)
    return vis


def preflight(fal_client) -> None:
    try:
        fal_client.upload(b"ok", "text/plain")
    except Exception as exc:                                  # noqa: BLE001
        msg = str(exc).lower()
        if any(w in msg for w in ("403", "locked", "balance", "exhaust", "unauthor")):
            sys.exit(f"fal.ai account problem (check balance / FAL_KEY): {exc}")


def download(url: str, dest: Path) -> None:
    import httpx
    with httpx.Client(follow_redirects=True, timeout=300) as c:
        r = c.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)


# ── task: text → mask ───────────────────────────────────────────────────────

def task_mask(args, work: Path, img, fal_client) -> int:
    H, W = img.shape[:2]
    src = Path(args.image)
    log(f"Finding “{args.text}” with EVF-SAM (~${SAM_COST:.3f}) ...")
    image_url = fal_client.upload_file(str(src))
    res = fal_client.subscribe(SAM_ENDPOINT, arguments={
        "prompt": args.text,
        "image_url": image_url,
        "mask_only": True,               # give us the mask, not a cut-out
        "fill_holes": True,
        "expand_mask": max(0, int(args.expand)),
        "use_grounding_dino": True,      # better at "the X" phrasing
    }, with_logs=False)

    url = None
    for key in ("image", "mask", "output"):
        v = res.get(key)
        if isinstance(v, dict) and v.get("url"):
            url = v["url"]
            break
        if isinstance(v, str) and v.startswith("http"):
            url = v
            break
    if not url:
        sys.exit(f"EVF-SAM returned no mask: {str(res)[:300]}")

    raw = work / "_mask-raw.png"
    download(url, raw)
    m = cv2.imread(str(raw), cv2.IMREAD_GRAYSCALE)
    raw.unlink(missing_ok=True)
    if m is None:
        sys.exit("could not decode the mask EVF-SAM returned")
    if m.shape[:2] != (H, W):
        m = cv2.resize(m, (W, H), interpolation=cv2.INTER_NEAREST)
    mask = ((m > 127) * 255).astype(np.uint8)

    if args.grow > 0:                     # an inpaint mask must cover the soft edge
        k = 2 * int(args.grow) + 1
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))

    cover = 100.0 * (mask > 127).mean()
    if cover < 0.01:
        sys.exit(f"EVF-SAM found nothing matching “{args.text}” — try different words, "
                 f"or draw the box by hand")
    cv2.imwrite(str(work / "_mask.png"), mask)
    cv2.imwrite(str(work / "_mask-preview.jpg"), overlay_preview(img, mask),
                [cv2.IMWRITE_JPEG_QUALITY, 88])
    log(f"  mask covers {cover:.1f}% of the picture")
    log("Done ✅")
    print(json.dumps({"coverage": round(cover, 2), "cost": SAM_COST, "text": args.text}))
    return 0


# ── task: mask-native fill ──────────────────────────────────────────────────

def task_fill(args, work: Path, img, fal_client) -> int:
    H, W = img.shape[:2]
    src = Path(args.image)
    model = FILL_MODELS[args.model]

    mask_path = Path(args.mask)
    if not mask_path.is_absolute():
        mask_path = work / mask_path
    if not mask_path.is_file():
        sys.exit(f"mask not found: {mask_path} — make one first (draw, brush or describe it)")
    mask = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)
    if mask is None:
        sys.exit("could not read the mask")
    if mask.shape[:2] != (H, W):
        mask = cv2.resize(mask, (W, H), interpolation=cv2.INTER_NEAREST)
    mask = ((mask > 127) * 255).astype(np.uint8)
    if not mask.any():
        sys.exit("the mask is empty")

    est = fill_cost(args.model, W, H)
    log(f"Mask-native {'erase' if model['wants'] == 'nothing' else 'fill'}: "
        f"{model['label']}  (~${est:.3f})")
    log(f"  mask covers {100.0 * (mask > 127).mean():.1f}% of a {W}x{H} picture")
    if model["wants"] == "prompt":
        log(f"  prompt: {args.text}")

    clean_mask = work / "_mask-upload.png"
    cv2.imwrite(str(clean_mask), mask)
    image_url = fal_client.upload_file(str(src))
    mask_url = fal_client.upload_file(str(clean_mask))
    clean_mask.unlink(missing_ok=True)

    arguments = {"image_url": image_url, "mask_url": mask_url}
    if model["wants"] == "prompt":
        arguments["prompt"] = args.text
        arguments["num_images"] = 1
    if args.seed is not None:
        arguments["seed"] = args.seed
    if args.model == "flux-fill":
        arguments["output_format"] = "png"

    try:
        res = fal_client.subscribe(model["endpoint"], arguments=arguments, with_logs=False)
    except Exception as exc:                                  # noqa: BLE001
        sys.exit(f"fal.ai fill failed: {exc}")

    url = None
    imgs = res.get("images")
    if isinstance(imgs, list) and imgs:
        url = imgs[0].get("url") if isinstance(imgs[0], dict) else imgs[0]
    if not url:
        v = res.get("image")
        url = v.get("url") if isinstance(v, dict) else (v if isinstance(v, str) else None)
    if not url:
        sys.exit(f"no image returned: {str(res)[:300]}")

    got = work / "_fill-raw.png"
    download(url, got)
    out = cv2.imread(str(got), cv2.IMREAD_COLOR)
    got.unlink(missing_ok=True)
    if out is None:
        sys.exit("could not decode the returned image")
    if out.shape[:2] != (H, W):
        log(f"  model returned {out.shape[1]}x{out.shape[0]} — scaling back to {W}x{H}")
        out = cv2.resize(out, (W, H), interpolation=cv2.INTER_LANCZOS4)

    # how much did the model touch OUTSIDE the mask, before we correct it?
    outside = mask <= 127
    raw_delta = float(np.abs(out[outside].astype(np.int16)
                             - img[outside].astype(np.int16)).max()) if outside.any() else 0.0

    result = np.where((mask > 127)[:, :, None], out, img)     # enforce mask-only
    changed = int(np.count_nonzero(np.any(result != img, axis=2) & outside))
    total_outside = int(np.count_nonzero(outside))

    idx = next_version(work)
    dest = work / f"v{idx:02d}.png"
    cv2.imwrite(str(dest), result)
    th = cv2.resize(result, (480, int(480 * H / W))) if W >= H \
        else cv2.resize(result, (int(480 * W / H), 480))
    cv2.imwrite(str(work / f"thumb-v{idx:02d}.jpg"), th, [cv2.IMWRITE_JPEG_QUALITY, 86])

    hist_file = work / "edits.json"
    try:
        hist = json.loads(hist_file.read_text(encoding="utf-8"))
    except Exception:                                         # noqa: BLE001
        hist = []
    hist.append({
        "version": f"v{idx:02d}", "file": dest.name, "alts": [], "parent": src.name,
        "mode": "erase-mask" if model["wants"] == "nothing" else "fill-mask",
        "user_text": args.text, "prompt": args.text,
        "model": args.model, "model_label": model["label"],
        "aspect": "auto", "resolution": None, "format": "png",
        "protected": True, "est_cost": est, "created": time.time(),
        "proof": {"pixels_changed_outside_mask": changed,
                  "pct_outside_changed": round(100.0 * changed / max(1, total_outside), 6),
                  "model_raw_max_delta_outside": raw_delta},
    })
    hist_file.write_text(json.dumps(hist, indent=2), encoding="utf-8")

    log(f"  model's own drift outside the mask: {raw_delta:.0f}/255")
    log(f"  ✅ PROOF: {changed} of {total_outside} pixels outside the mask changed — "
        f"the rest of the photo is byte-identical")
    log(f"deliverable: {dest}")
    log(f"estimated fal.ai cost: ~${est:.3f}")
    log("Done ✅")
    print(json.dumps({"version": f"v{idx:02d}", "file": dest.name, "cost": est,
                      "pixels_changed_outside_mask": changed,
                      "model_raw_max_delta_outside": raw_delta}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--task", required=True, choices=["mask", "fill"])
    ap.add_argument("--text", default="")
    ap.add_argument("--mask", default="_mask.png")
    ap.add_argument("--model", default="flux-fill", choices=list(FILL_MODELS))
    ap.add_argument("--expand", type=int, default=0, help="EVF-SAM's own mask expansion")
    ap.add_argument("--grow", type=int, default=6, help="extra dilation after the mask arrives")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--env-file")
    args = ap.parse_args()

    load_env(args.env_file)
    if not os.environ.get("FAL_KEY"):
        sys.exit("FAL_KEY not set — add it to autoVSL/.env")
    if args.task == "mask" and len(args.text.strip()) < 2:
        sys.exit("say what to find, e.g. “the coffee cup”")
    if args.task == "fill" and FILL_MODELS[args.model]["wants"] == "prompt" \
            and len(args.text.strip()) < 2:
        sys.exit("say what should replace it")

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    img = cv2.imread(str(Path(args.image)), cv2.IMREAD_COLOR)
    if img is None:
        sys.exit(f"could not read image: {args.image}")

    import fal_client
    preflight(fal_client)
    return task_mask(args, work, img, fal_client) if args.task == "mask" \
        else task_fill(args, work, img, fal_client)


if __name__ == "__main__":
    raise SystemExit(main())
