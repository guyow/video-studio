#!/usr/bin/env python3
"""LAMA subtitle eraser — generative fill for ALWAYS-ON subtitles over static areas.

Why this exists: the STTN engine recovers background by copying it from other frames.
When captions are on ~every frame over a static area (typical marketing talking-heads),
no frame ever reveals the background, and STTN outputs ghost mush. LAMA instead
*generates* a plausible fill per frame — the right tool for that case.

Uses VSR's bundled big-lama.pt (TorchScript) + our EasyOCR/CRAFT per-frame text masks
(VSR's own LAMA mode is blocked by a paddlepaddle 3.x inference bug on this machine).

Usage: python lama_erase.py <in.mp4> <out.mp4> [--x --y --w --h]   (no box = auto-detect)
"""
import argparse
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from erase_subs import ocr_detect_box, make_ocr_masker, auto_detect_box  # noqa: E402

LAMA_PT = (Path(__file__).resolve().parent.parent / "tools" / "vsr" /
           "backend" / "models" / "big-lama" / "big-lama.pt")
DETECT_EVERY = 3      # detect text every N frames, hold the mask in between
BATCH = 4             # LAMA mini-batch (band crops are small; 4 fits 4GB easily)
CTX = 48              # extra context above/below the band fed to LAMA


def pad_to_modulo(img, mod=8):
    h, w = img.shape[:2]
    H = (h + mod - 1) // mod * mod
    W = (w + mod - 1) // mod * mod
    if img.ndim == 3:
        out = np.zeros((H, W, img.shape[2]), img.dtype)
    else:
        out = np.zeros((H, W), img.dtype)
    out[:h, :w] = img
    return out


def lama_batch(model, device, crops_bgr, masks):
    """Inpaint a list of BGR crops with binary masks; returns BGR results."""
    h, w = crops_bgr[0].shape[:2]
    imgs = np.stack([pad_to_modulo(cv2.cvtColor(c, cv2.COLOR_BGR2RGB)).transpose(2, 0, 1)
                     for c in crops_bgr]).astype(np.float32) / 255.0
    ms = np.stack([pad_to_modulo(m)[None, ...] for m in masks]).astype(np.float32)
    with torch.inference_mode():
        it = torch.from_numpy(imgs).to(device)
        mt = (torch.from_numpy(ms).to(device) > 0) * 1
        out = model(it, mt).permute(0, 2, 3, 1).cpu().numpy()
    res = []
    for o in out:
        o = np.clip(o * 255, 0, 255).astype(np.uint8)[:h, :w]
        res.append(cv2.cvtColor(o, cv2.COLOR_RGB2BGR))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--x", type=int, default=-1)
    ap.add_argument("--y", type=int, default=-1)
    ap.add_argument("--w", type=int, default=-1)
    ap.add_argument("--h", type=int, default=-1)
    a = ap.parse_args()
    src, out = str(Path(a.src).resolve()), str(Path(a.out).resolve())

    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        sys.exit(f"cannot open {src}")
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0

    bx, by, bw, bh = a.x, a.y, a.w, a.h
    if min(bx, by, bw, bh) < 0:
        print("auto-detecting the subtitle band…", flush=True)
        found = ocr_detect_box(src, W, H, total) or auto_detect_box(src, 190, 70, W, H, total)
        if found is None:
            print("no burned-in subtitles detected — copying through", flush=True)
            r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-c", "copy", out])
            sys.exit(0 if r.returncode == 0 else "copy failed")
        bx, by, bw, bh = (int(v) for v in found)
    print(f"band ({bx},{by}) {bw}x{bh} in {W}x{H} @ {fps:.2f}fps, ~{total} frames", flush=True)

    # LAMA crop = full width of the band rows + context (full width gives LAMA texture to sample)
    cy0 = max(0, by - CTX)
    cy1 = min(H, by + bh + CTX)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"loading LAMA (TorchScript) on {device}…", flush=True)
    model = torch.jit.load(str(LAMA_PT), map_location=device).eval()
    got = make_ocr_masker(0, cy0, W, cy1 - cy0)
    if not got:
        sys.exit("EasyOCR unavailable — cannot build text masks")
    _reader, mask_of = got

    enc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", f"{fps}", "-i", "pipe:0",
         "-i", src, "-map", "0:v", "-map", "1:a?",
         "-c:v", "libx264", "-crf", "15", "-preset", "fast", "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-movflags", "+faststart", out],
        stdin=subprocess.PIPE)

    i = 0
    text_frames = 0
    last_mask = None
    pend_f, pend_m = [], []          # frames waiting for LAMA (their crops+masks)

    def flush_pending():
        nonlocal text_frames
        if not pend_f:
            return
        crops = [f[cy0:cy1, 0:W] for f in pend_f]
        fills = lama_batch(model, device, crops, pend_m)
        for f, m, fill in zip(pend_f, pend_m, fills):
            alpha = cv2.GaussianBlur(m.astype(np.float32) / 255.0, (9, 9), 0)[:, :, None]
            roi = f[cy0:cy1, 0:W]
            f[cy0:cy1, 0:W] = (alpha * fill + (1 - alpha) * roi).astype(np.uint8)
            enc.stdin.write(f.tobytes())
            text_frames += 1
        pend_f.clear(); pend_m.clear()

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % DETECT_EVERY == 0:
            m = mask_of(frame)
            # union with previous mask so caption transitions never slip through
            if m is not None and last_mask is not None:
                m = cv2.bitwise_or(m, last_mask) if i % (DETECT_EVERY * 2) else m
            last_mask = m
        m = last_mask
        if m is None or not m.any():
            flush_pending()              # keep output ordering
            enc.stdin.write(frame.tobytes())
        else:
            pend_f.append(frame)
            pend_m.append(m)
            if len(pend_f) >= BATCH:
                flush_pending()
        i += 1
        if i % 150 == 0:
            print(f"  {i}/{total} frames ({text_frames} inpainted)", flush=True)
    flush_pending()
    cap.release()
    enc.stdin.close()
    if enc.wait() != 0:
        sys.exit("encode failed")
    print(f"done: {i} frames, {text_frames} inpainted with LAMA -> {out}", flush=True)


if __name__ == "__main__":
    main()
