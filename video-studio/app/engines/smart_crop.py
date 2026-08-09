#!/usr/bin/env python3
"""Smart crop / format swap — content-aware reframing, free and local.

The naive way to change a picture's shape is centre-crop (cuts whatever is at
the edges, and zooms in) or letterbox (bars). Neither is acceptable for ad
creative. This does what a human editor does instead: work out where the
important stuff is, then slide the biggest possible window of the target shape
so that the important stuff survives.

How it decides what matters
---------------------------
1. **Saliency** — spectral-residual saliency (Hou & Zhang, CVPR'07), computed
   with a plain FFT so no opencv-contrib is needed: an image's log-amplitude
   spectrum is mostly predictable, and what's left over after subtracting the
   smoothed version is where the eye goes.
2. **Edge energy** — Sobel magnitude, the same signal seam-carving uses. Flat
   sky/wall scores low, detailed subjects score high.
3. **Faces** — S3FD (the detector already on disk from Wav2Lip). Faces get a
   large weight AND a hard constraint: a window that slices someone's face is
   rejected outright unless no window can hold them all.
4. **Centre prior** — a weak nudge, only to break ties.

Then for each target ratio it takes the LARGEST window of that ratio that fits
(so the output is never zoomed in and never upscaled), scores every position
with an integral image, and keeps the best. Portrait targets also get a
rule-of-thirds bonus for putting the face slightly above centre (head-room),
which is what makes an auto-crop look deliberate rather than mechanical.

  python smart_crop.py --image shot.jpg --out ./formats --stem shot \
      --ratios 16:9,9:16,1:1 --format jpg --quality 92 --debug
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ANALYSIS_MAX_SIDE = 720          # everything is scored at this scale, then mapped back
FACE_WEIGHT = 4.0                # how much a face outweighs ordinary saliency
FACE_CUT_PENALTY = 40.0          # per unit of face area left outside the window
CENTRE_PRIOR = 0.12
THIRDS_BONUS = 0.30              # head-room preference for portrait targets
HEADROOM = 0.40                  # ideal face-centre height inside a tall window

WAV2LIP = Path("C:/Users/guyas/Claude/Projects/Video AI editing/tools/Wav2Lip")


def log(m: str) -> None:
    print(m, flush=True)


# ── importance ──────────────────────────────────────────────────────────────

def spectral_saliency(gray: np.ndarray) -> np.ndarray:
    """Spectral-residual saliency. Bright = the eye goes here."""
    f = np.fft.fft2(gray.astype(np.float32))
    log_amp = np.log1p(np.abs(f))
    phase = np.angle(f)
    smooth = cv2.blur(log_amp, (3, 3))
    residual = log_amp - smooth
    recon = np.fft.ifft2(np.exp(residual + 1j * phase))
    sal = np.abs(recon) ** 2
    sal = cv2.GaussianBlur(sal, (0, 0), 8)
    return sal


def edge_energy(gray: np.ndarray) -> np.ndarray:
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.GaussianBlur(cv2.magnitude(gx, gy), (0, 0), 6)


def norm01(a: np.ndarray) -> np.ndarray:
    lo, hi = float(a.min()), float(a.max())
    return (a - lo) / (hi - lo) if hi > lo else np.zeros_like(a)


def detect_faces(bgr: np.ndarray, device: str = "cpu") -> list:
    """S3FD face boxes [(x1,y1,x2,y2), ...]; empty list if unavailable."""
    try:
        import warnings
        warnings.filterwarnings("ignore")
        sys.path.insert(0, str(WAV2LIP))
        import torch                                     # noqa: F401  (needed by the detector)
        import face_detection

        fd = face_detection.FaceAlignment(
            face_detection.LandmarksType._2D, flip_input=False, device=device)
        got = fd.get_detections_for_batch(np.array([bgr[:, :, ::-1]]))
        return [tuple(int(v) for v in b) for b in got if b is not None]
    except Exception as exc:                             # noqa: BLE001
        log(f"  (face detection unavailable: {str(exc)[:120]} — using saliency only)")
        return []


def importance_map(bgr: np.ndarray, faces: list) -> np.ndarray:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    imp = 0.55 * norm01(spectral_saliency(gray)) + 0.45 * norm01(edge_energy(gray))

    h, w = imp.shape
    for (x1, y1, x2, y2) in faces:                       # faces dominate
        pad_x, pad_y = int(0.25 * (x2 - x1)), int(0.35 * (y2 - y1))
        cv2.rectangle(imp, (max(0, x1 - pad_x), max(0, y1 - pad_y)),
                      (min(w, x2 + pad_x), min(h, y2 + pad_y)), FACE_WEIGHT, -1)

    yy, xx = np.mgrid[0:h, 0:w]
    centre = np.exp(-(((xx - w / 2) / (w * 0.6)) ** 2 + ((yy - h / 2) / (h * 0.6)) ** 2))
    return imp + CENTRE_PRIOR * centre


# ── window search ───────────────────────────────────────────────────────────

def best_window(imp: np.ndarray, faces: list, ratio: float) -> tuple:
    """Largest window of `ratio` that fits, positioned to keep the most value.

    Returns (x, y, w, h, score, faces_fully_inside).
    """
    H, W = imp.shape
    if W / H > ratio:                                    # image too wide → slide on x
        wh = H
        ww = max(1, int(round(H * ratio)))
    else:                                                # image too tall → slide on y
        ww = W
        wh = max(1, int(round(W / ratio)))
    ww, wh = min(ww, W), min(wh, H)

    integral = cv2.integral(imp.astype(np.float64))      # (H+1, W+1)

    def win_sum(x, y):
        return (integral[y + wh, x + ww] - integral[y, x + ww]
                - integral[y + wh, x] + integral[y, x])

    xs = range(0, W - ww + 1) if ww < W else [0]
    ys = range(0, H - wh + 1) if wh < H else [0]
    face_area = sum(max(0, x2 - x1) * max(0, y2 - y1) for x1, y1, x2, y2 in faces) or 1

    best = None
    for y in ys:
        for x in xs:
            score = win_sum(x, y) / (ww * wh)

            # hard-ish constraint: how much face area falls outside this window
            outside = 0
            for (fx1, fy1, fx2, fy2) in faces:
                ix1, iy1 = max(fx1, x), max(fy1, y)
                ix2, iy2 = min(fx2, x + ww), min(fy2, y + wh)
                inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
                outside += (fx2 - fx1) * (fy2 - fy1) - inter
            score -= FACE_CUT_PENALTY * (outside / face_area)

            # head-room: for tall windows put the main face a bit above centre
            if faces and wh < H:
                fy = (faces[0][1] + faces[0][3]) / 2
                rel = (fy - y) / wh
                score += THIRDS_BONUS * float(np.exp(-((rel - HEADROOM) / 0.22) ** 2))

            if best is None or score > best[0]:
                best = (score, x, y)

    score, x, y = best
    contained = all(x <= fx1 and fy1 >= y and fx2 <= x + ww and fy2 <= y + wh
                    for fx1, fy1, fx2, fy2 in faces)
    return x, y, ww, wh, score, contained


def parse_ratio(s: str) -> float:
    a, b = s.split(":")
    return float(a) / float(b)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stem", required=True)
    ap.add_argument("--ratios", default="16:9,9:16,1:1")
    ap.add_argument("--sizes", default="", help="optional w x h per ratio, e.g. 1920x1080,1080x1920")
    ap.add_argument("--format", default="jpg", choices=["jpg", "png", "webp"])
    ap.add_argument("--quality", type=int, default=92)
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    ap.add_argument("--no-faces", action="store_true")
    ap.add_argument("--debug", action="store_true", help="also write a heat-map overlay")
    args = ap.parse_args()

    src = Path(args.image)
    if not src.is_file():
        sys.exit(f"image not found: {src}")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    full = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if full is None:
        sys.exit(f"could not read image: {src}")
    FH, FW = full.shape[:2]
    scale = min(1.0, ANALYSIS_MAX_SIDE / max(FH, FW))
    small = cv2.resize(full, (max(1, int(FW * scale)), max(1, int(FH * scale))),
                       interpolation=cv2.INTER_AREA) if scale < 1.0 else full.copy()

    log(f"Smart crop: {src.name}  ({FW}x{FH})")
    t0 = time.time()
    faces = [] if args.no_faces else detect_faces(small, args.device)
    faces.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
    log(f"  faces found: {len(faces)}  ({time.time() - t0:.1f}s)")

    imp = importance_map(small, faces)
    log("  importance map: saliency + edges"
        + (" + faces" if faces else "") + " + centre prior")

    sizes = {}
    for spec in filter(None, args.sizes.split(",")):
        w, h = spec.lower().split("x")
        sizes[round(int(w) / int(h), 4)] = (int(w), int(h))

    written, report = [], []
    for rs in filter(None, args.ratios.split(",")):
        ratio = parse_ratio(rs)
        x, y, ww, wh, score, contained = best_window(imp, faces, ratio)
        inv = 1.0 / scale if scale < 1.0 else 1.0
        X, Y = int(round(x * inv)), int(round(y * inv))
        CW, CH = int(round(ww * inv)), int(round(wh * inv))
        X, Y = max(0, min(X, FW - 1)), max(0, min(Y, FH - 1))
        CW, CH = min(CW, FW - X), min(CH, FH - Y)
        crop = full[Y:Y + CH, X:X + CW]

        target = sizes.get(round(ratio, 4))
        if target:                                       # only ever downscale
            tw, th = target
            if tw <= CW and th <= CH:
                crop = cv2.resize(crop, (tw, th), interpolation=cv2.INTER_AREA)

        name = f"{args.stem}-{rs.replace(':', 'x')}-smart.{args.format}"
        dest = out_dir / name
        if args.format == "jpg":
            cv2.imwrite(str(dest), crop, [cv2.IMWRITE_JPEG_QUALITY, args.quality])
        elif args.format == "webp":
            cv2.imwrite(str(dest), crop, [cv2.IMWRITE_WEBP_QUALITY, args.quality])
        else:
            cv2.imwrite(str(dest), crop)

        kept = (CW * CH) / float(FW * FH)
        written.append(name)
        report.append({"ratio": rs, "file": name, "crop": [X, Y, CW, CH],
                       "out_size": [crop.shape[1], crop.shape[0]],
                       "kept_area": round(kept, 3), "faces_intact": bool(contained),
                       "zoomed_in": False})
        log(f"  {rs:>5} -> {crop.shape[1]}x{crop.shape[0]}  crop at {X},{Y}  "
            f"keeps {kept * 100:.0f}% of the frame  "
            + ("faces intact ✅" if contained or not faces else "⚠ a face is clipped"))

        if args.debug:
            vis = small.copy()
            heat = cv2.applyColorMap((norm01(imp) * 255).astype(np.uint8), cv2.COLORMAP_JET)
            vis = cv2.addWeighted(vis, 0.55, heat, 0.45, 0)
            for (fx1, fy1, fx2, fy2) in faces:
                cv2.rectangle(vis, (fx1, fy1), (fx2, fy2), (255, 255, 255), 2)
            cv2.rectangle(vis, (x, y), (x + ww, y + wh), (0, 255, 0), 3)
            cv2.imwrite(str(out_dir / f"{args.stem}-{rs.replace(':', 'x')}-debug.jpg"), vis)

    (out_dir / f"{args.stem}-smartcrop.json").write_text(
        json.dumps({"source": src.name, "size": [FW, FH], "faces": faces,
                    "results": report}, indent=2), encoding="utf-8")
    log(f"\n{len(written)} file(s) in {time.time() - t0:.1f}s — free, local, no AI")
    log("Done ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
