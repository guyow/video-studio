#!/usr/bin/env python3
"""Erase an object from a still — locally, free, and provably local.

This is the accurate path (see docs/ACCURATE-EDITING-PLAN.md). Unlike Nano
Banana, which re-renders the whole frame every call, LaMa is a true inpainting
model: it takes image + mask and only writes inside the mask. We then composite
anyway, so "the rest of the photo is untouched" is a guarantee, not a hope.

Weights: tools/vsr/backend/models/big-lama/big-lama.pt (TorchScript) — already
on this machine, the same one Subtitle Studio's eraser uses. Nothing is
uploaded, nothing is charged.

Mask sources, all local:
  box     the rectangle you dragged, grown a little (best for caption bars,
          logos, stickers, watermarks — flat graphics with hard edges)
  object  GrabCut seeded with that rectangle, so the mask hugs the object
          instead of taking the whole box (best for a cup on a table)
  brush   exactly the strokes you painted

Only a region around the mask is sent through the network (padded ROI), which
keeps it fast on big images and means untouched pixels are never even resampled.

  python local_erase.py --image v00.jpg --work <dir> --mode box \
      --box 0.12,0.70,0.77,0.12 --grow 10
  python local_erase.py ... --mask-only          # just the preview, no torch
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import cv2
import numpy as np

LAMA_PT = Path("C:/Users/guyas/Claude/Projects/Video AI editing/tools/vsr/backend/"
               "models/big-lama/big-lama.pt")
ROI_MARGIN = 96          # context fed to LaMa around the mask
MOD = 8                  # the network needs sides divisible by 8


def log(m: str) -> None:
    print(m, flush=True)


# ── mask building (no torch needed) ─────────────────────────────────────────

def parse_box(s: str, W: int, H: int):
    x, y, w, h = (float(v) for v in s.split(","))
    X, Y = int(round(x * W)), int(round(y * H))
    BW, BH = int(round(w * W)), int(round(h * H))
    X, Y = max(0, min(X, W - 1)), max(0, min(Y, H - 1))
    return X, Y, max(1, min(BW, W - X)), max(1, min(BH, H - Y))


def parse_strokes(s: str, W: int, H: int):
    out = []
    for part in filter(None, (p.strip() for p in s.split(";"))):
        x, y, r = (float(v) for v in part.split(","))
        out.append((int(round(x * W)), int(round(y * H)),
                    max(1, int(round(r * max(W, H))))))
    return out


def build_mask(img, mode: str, box=None, strokes=None, grow: int = 8,
               mask_file=None) -> np.ndarray:
    H, W = img.shape[:2]
    mask = np.zeros((H, W), np.uint8)

    if mode == "file":
        if not mask_file or not Path(mask_file).is_file():
            sys.exit("mode 'file' needs an existing --mask-file")
        m = cv2.imread(str(mask_file), cv2.IMREAD_GRAYSCALE)
        if m is None:
            sys.exit(f"could not read mask: {mask_file}")
        if m.shape[:2] != (H, W):
            m = cv2.resize(m, (W, H), interpolation=cv2.INTER_NEAREST)
        mask = ((m > 127) * 255).astype(np.uint8)

    if strokes:
        for (x, y, r) in strokes:
            cv2.circle(mask, (x, y), r, 255, -1)

    if box is not None:
        X, Y, BW, BH = box
        if mode == "object":
            # GrabCut seeded with the rectangle: keeps the object, drops the
            # background that happened to be inside the box.
            gc = np.zeros((H, W), np.uint8)
            bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
            try:
                cv2.grabCut(img, gc, (X, Y, BW, BH), bgd, fgd, 5, cv2.GC_INIT_WITH_RECT)
                obj = np.where((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
                keep = np.zeros((H, W), np.uint8)
                keep[Y:Y + BH, X:X + BW] = 255
                obj = cv2.bitwise_and(obj, keep)
                if obj.sum() < 0.02 * 255 * BW * BH:        # grabcut found ~nothing
                    log("  (grabcut found no object — falling back to the box)")
                    obj[Y:Y + BH, X:X + BW] = 255
                mask = cv2.bitwise_or(mask, obj)
            except cv2.error as exc:                        # noqa: PERF203
                log(f"  (grabcut failed: {exc} — using the box)")
                mask[Y:Y + BH, X:X + BW] = 255
        else:
            mask[Y:Y + BH, X:X + BW] = 255

    if not mask.any():
        sys.exit("empty mask — drag a box or paint a stroke first")

    # close gaps, then grow: an inpaint mask must cover the object's soft edge
    # and its shadow/anti-aliasing, or you get a ghost outline left behind.
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
    if grow > 0:
        k = 2 * int(grow) + 1
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    return mask


def overlay_preview(img, mask) -> np.ndarray:
    """What will be erased, painted red — shown before anything runs."""
    vis = img.copy()
    red = np.zeros_like(img)
    red[:, :, 2] = 255
    m3 = (mask > 127)[:, :, None]
    vis = np.where(m3, cv2.addWeighted(vis, 0.45, red, 0.55, 0), vis)
    cont, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(vis, cont, -1, (255, 255, 255), 2)
    return vis


# ── inpainting ──────────────────────────────────────────────────────────────

def pad_to_mod(a: np.ndarray, mod: int = MOD) -> np.ndarray:
    h, w = a.shape[-2:]
    return np.pad(a, ((0, 0), (0, 0), (0, (mod - h % mod) % mod), (0, (mod - w % mod) % mod)),
                  mode="symmetric")


def lama_inpaint(img, mask, device: str = "cpu"):
    """Run big-lama on a padded ROI around the mask; returns (result, roi, raw_delta).

    raw_delta = how much the model itself changed OUTSIDE the mask, before we
    composite. LaMa should be 0 here; we measure it instead of assuming it.
    """
    import torch

    H, W = img.shape[:2]
    ys, xs = np.where(mask > 127)
    y0, y1 = max(0, ys.min() - ROI_MARGIN), min(H, ys.max() + 1 + ROI_MARGIN)
    x0, x1 = max(0, xs.min() - ROI_MARGIN), min(W, xs.max() + 1 + ROI_MARGIN)
    roi, mroi = img[y0:y1, x0:x1], mask[y0:y1, x0:x1]
    rh, rw = roi.shape[:2]
    log(f"  inpainting a {rw}x{rh} region (mask covers "
        f"{100.0 * (mroi > 127).mean():.1f}% of it)")

    model = torch.jit.load(str(LAMA_PT), map_location=device).eval()
    x = pad_to_mod(roi[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0)
    m = pad_to_mod((mroi > 127)[None, None].astype(np.float32))
    with torch.no_grad():
        out = model(torch.from_numpy(x).to(device), torch.from_numpy(m).to(device))
    filled = (out[0].permute(1, 2, 0).cpu().numpy()[:rh, :rw] * 255).clip(0, 255)
    filled = filled.astype(np.uint8)[:, :, ::-1]                       # RGB → BGR

    outside = (mroi <= 127)
    raw_delta = float(np.abs(filled[outside].astype(np.int16)
                             - roi[outside].astype(np.int16)).max()) if outside.any() else 0.0

    result = img.copy()
    keep = (mroi > 127)[:, :, None]
    result[y0:y1, x0:x1] = np.where(keep, filled, roi)                 # enforce mask-only
    return result, (x0, y0, x1, y1), raw_delta


def next_version(work: Path) -> int:
    best = -1
    for p in work.glob("v[0-9][0-9].*"):
        m = re.match(r"v(\d\d)$", p.stem)
        if m:
            best = max(best, int(m.group(1)))
    return best + 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--mode", default="box", choices=["box", "object", "brush", "file"])
    ap.add_argument("--mask-file", help="use this mask instead of building one "
                                        "(e.g. the AI mask from mask_edit.py)")
    ap.add_argument("--box", help="x,y,w,h normalised 0-1")
    ap.add_argument("--strokes", help="x,y,r;x,y,r;... normalised")
    ap.add_argument("--grow", type=int, default=8)
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    ap.add_argument("--mask-only", action="store_true",
                    help="just write the mask + preview (no torch, instant)")
    ap.add_argument("--user-text", default="")
    args = ap.parse_args()

    src = Path(args.image)
    work = Path(args.work)
    if not src.is_file():
        sys.exit(f"image not found: {src}")
    work.mkdir(parents=True, exist_ok=True)

    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if img is None:
        sys.exit(f"could not read image: {src}")
    H, W = img.shape[:2]

    box = parse_box(args.box, W, H) if args.box else None
    strokes = parse_strokes(args.strokes, W, H) if args.strokes else None
    if args.mode == "brush" and not strokes:
        sys.exit("brush mode needs --strokes")
    if args.mode in ("box", "object") and not box:
        sys.exit(f"{args.mode} mode needs --box")

    t0 = time.time()
    mask = build_mask(img, args.mode, box, strokes, args.grow, args.mask_file)
    cover = 100.0 * (mask > 127).mean()
    log(f"Local erase: {src.name} ({W}x{H})  ·  mode {args.mode}  ·  "
        f"mask covers {cover:.1f}% of the picture")

    if args.mask_only:
        cv2.imwrite(str(work / "_mask.png"), mask)
        cv2.imwrite(str(work / "_mask-preview.jpg"), overlay_preview(img, mask),
                    [cv2.IMWRITE_JPEG_QUALITY, 88])
        log(f"  preview written in {time.time() - t0:.2f}s (free, no model run)")
        print(json.dumps({"coverage": round(cover, 2), "mask_only": True}))
        return 0

    if not LAMA_PT.is_file():
        sys.exit(f"big-lama weights not found at {LAMA_PT}")

    result, roi, raw_delta = lama_inpaint(img, mask, args.device)

    # PROOF: outside the mask must be byte-identical to the source
    outside = (mask <= 127)
    changed = int(np.count_nonzero(np.any(result != img, axis=2) & outside))
    total_outside = int(np.count_nonzero(outside))
    pct_outside = 100.0 * changed / max(1, total_outside)

    idx = next_version(work)
    dest = work / f"v{idx:02d}.png"
    cv2.imwrite(str(dest), result)
    cv2.imwrite(str(work / f"thumb-v{idx:02d}.jpg"),
                cv2.resize(result, (480, int(480 * H / W))) if W >= H
                else cv2.resize(result, (int(480 * W / H), 480)),
                [cv2.IMWRITE_JPEG_QUALITY, 86])

    hist_file = work / "edits.json"
    try:
        hist = json.loads(hist_file.read_text(encoding="utf-8"))
    except Exception:                                                  # noqa: BLE001
        hist = []
    hist.append({
        "version": f"v{idx:02d}", "file": dest.name, "alts": [],
        "parent": src.name, "mode": "erase-local",
        "user_text": args.user_text or f"erased ({args.mode} mask)",
        "prompt": None, "model": "big-lama (local)", "model_label": "LaMa — local, free",
        "aspect": "auto", "resolution": None, "format": "png",
        "protected": True, "est_cost": 0.0, "created": time.time(),
        "mask": {"mode": args.mode, "grow": args.grow, "coverage": round(cover, 2)},
        "proof": {"pixels_changed_outside_mask": changed,
                  "pct_outside_changed": round(pct_outside, 6),
                  "model_raw_max_delta_outside": raw_delta},
    })
    hist_file.write_text(json.dumps(hist, indent=2), encoding="utf-8")

    log(f"  ROI {roi}  ·  model's own drift outside the mask: {raw_delta:.0f}/255")
    log(f"  ✅ PROOF: {changed} of {total_outside} pixels outside the mask changed "
        f"({pct_outside:.4f}%) — the rest of the photo is byte-identical")
    log(f"deliverable: {dest}   ({time.time() - t0:.1f}s, $0.00)")
    log("Done ✅")
    print(json.dumps({"version": f"v{idx:02d}", "file": dest.name,
                      "pixels_changed_outside_mask": changed,
                      "pct_outside_changed": round(pct_outside, 6),
                      "coverage": round(cover, 2), "cost": 0.0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
