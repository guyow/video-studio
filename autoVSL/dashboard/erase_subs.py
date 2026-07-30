#!/usr/bin/env python3
"""Erase burned-in subtitles with AI video inpainting (ProPainter) — motion-compensated,
so the fill behind the letters is reconstructed from what the moving scene actually shows.

Pipeline:
  1. detect caption text per frame (white core + dark outline, letter-sized components)
  2. crop the caption band, run ProPainter (GPU) with the per-frame masks
  3. paste ONLY the masked letter pixels back (feathered edge) — every other pixel is the
     original; encode visually lossless (libx264 CRF 12, preset slow); audio stream-copied
Falls back to classic per-frame spatial inpainting if ProPainter is unavailable or fails.

Usage:
  python erase_subs.py <in.mp4> <out.mp4> --x 29 --y 896 --w 662 --h 282
                       [--thresh 190] [--sat 70] [--engine auto|propainter|classic]
"""
import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

PROPAINTER_DIR = Path(__file__).resolve().parent.parent.parent / "tools" / "ProPainter"


def text_mask(roi, thresh, sat, box_w, box_h):
    """uint8 mask (0/255) of subtitle letter strokes + their dark outline."""
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    S, V = hsv[:, :, 1], hsv[:, :, 2]
    core = ((V >= thresh) & (S <= sat)).astype(np.uint8) * 255
    core = cv2.morphologyEx(core, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    if not core.any():
        return None
    n, labels, stats, _ = cv2.connectedComponentsWithStats(core, 8)
    keep = np.zeros_like(core)
    for i in range(1, n):
        _, _, cw, ch, area = stats[i]
        # letters are small-ish; big bright blobs (pillows, windows, clothing) are not text
        if area >= 8 and ch <= 0.5 * box_h and cw <= 0.5 * box_w:
            keep[labels == i] = 255
    if not keep.any():
        return None
    # grow to swallow the dark border and anti-aliased edges around the letters
    return cv2.dilate(keep, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 13)))


def auto_detect_box(src, thresh, sat, W, H, total):
    """Find the caption region automatically. Captions are WIDE, SHORT, DENSE bands of
    letter-sized components that sit in the same rows across many frames — bright wallpaper
    dots, pillows and clothing highlights don't form that shape."""
    cap = cv2.VideoCapture(src)
    samples = min(24, max(8, total // 30))
    heat = np.zeros((H, W), np.uint16)
    hits = 0
    for i in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (i + 0.5) / samples))
        ok, frame = cap.read()
        if not ok:
            continue
        # strict letter-size cap: components taller than ~6% of the frame are not captions
        m = text_mask(frame, thresh, sat, int(W * 0.5), int(H * 0.12))
        if m is not None:
            heat += (m > 0).astype(np.uint16)
            hits += 1
    cap.release()
    if not hits:
        return None
    strong = heat >= max(3, int(hits * 0.25))
    # caption rows are covered widely; sparse speckle rows (wallpaper) are not
    rows = np.where(strong.sum(axis=1) >= W * 0.06)[0]
    if not len(rows):
        return None
    # group contiguous rows (30px gap tolerance joins two-line captions)
    bands, start, prev = [], int(rows[0]), int(rows[0])
    for r in rows[1:]:
        if r - prev > 30:
            bands.append((start, prev))
            start = int(r)
        prev = int(r)
    bands.append((start, prev))
    bands = [b for b in bands if b[1] - b[0] <= H * 0.25]
    if not bands:
        return None
    y0, y1 = max(bands, key=lambda b: int(strong[b[0]:b[1] + 1].sum()))
    xs = np.where(strong[y0:y1 + 1].any(axis=0))[0]
    pad = 16
    x0 = max(0, int(xs.min()) - pad)
    x1 = min(W, int(xs.max()) + pad)
    y0 = max(0, y0 - pad)
    y1 = min(H, y1 + pad)
    return x0, y0, x1 - x0, y1 - y0


def open_encoder(src, out, W, H, fps):
    return subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", f"{fps}", "-i", "pipe:0",
         "-i", src,
         "-map", "0:v", "-map", "1:a?",
         "-c:v", "libx264", "-crf", "12", "-preset", "slow", "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-movflags", "+faststart", out],
        stdin=subprocess.PIPE,
    )


def _pp_once(band_dir, mask_dir, out_dir):
    """One ProPainter inference run; returns (ok, frames_dir, log_tail)."""
    proc = subprocess.Popen(
        [sys.executable, "inference_propainter.py",
         "-i", str(band_dir), "-m", str(mask_dir), "-o", str(out_dir),
         "--save_frames", "--subvideo_length", "30",
         "--neighbor_length", "8", "--raft_iter", "12", "--mask_dilation", "4"],
        cwd=str(PROPAINTER_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    # tqdm writes \r-updated lines — surface a snapshot every few seconds so the job log moves
    buf, latest, tail, last = "", "", [], 0.0
    while True:
        chunk = proc.stdout.read(256)
        if not chunk:
            break
        buf += chunk
        parts = re.split(r"[\r\n]", buf)
        buf = parts.pop()
        for p in parts:
            if p.strip():
                latest = p.strip()
                tail.append(latest)
                del tail[:-12]
        if latest and time.time() - last > 5:
            print(f"  pp: {latest[:140]}", flush=True)
            last = time.time()
    proc.wait()
    frames_dir = out_dir / band_dir.name / "frames"
    return proc.returncode == 0, frames_dir, tail


def _looks_corrupted(out_pngs, mask_frames, start):
    """NaN output under GPU pressure renders the masked area black — catch it."""
    checked = bad = 0
    for j in range(0, len(out_pngs), max(1, len(out_pngs) // 5)):
        m = cv2.imread(str(mask_frames[start + j]), cv2.IMREAD_GRAYSCALE)
        if m is None or int((m > 0).sum()) < 500:
            continue
        img = cv2.imread(str(out_pngs[j]))
        if img is None:
            return True
        sel = m > 0
        black = float(((img < 10).all(axis=2) & sel).sum()) / float(sel.sum())
        checked += 1
        if black > 0.5:
            bad += 1
    return checked > 0 and bad * 2 > checked


def run_propainter(band_dir, mask_dir, out_dir, total, chunk=240):
    """Run ProPainter over the band in RAM-sized chunks; returns the merged frames dir or None."""
    if not (PROPAINTER_DIR / "inference_propainter.py").is_file():
        print("ProPainter not installed — falling back to classic inpaint", flush=True)
        return None
    band_frames = sorted(band_dir.glob("*.png"))
    mask_frames = sorted(mask_dir.glob("*.png"))
    merged = out_dir / "frames_all"
    merged.mkdir(parents=True, exist_ok=True)
    n_chunks = (total + chunk - 1) // chunk
    print(f"running ProPainter AI inpainting on the caption band (GPU, {n_chunks} chunk(s))...", flush=True)
    for ci, start in enumerate(range(0, total, chunk)):
        end = min(total, start + chunk)
        cdir = out_dir / f"c{ci}"
        cband, cmask = cdir / "band", cdir / "band_mask"
        cband.mkdir(parents=True, exist_ok=True)
        cmask.mkdir(parents=True, exist_ok=True)
        for j in range(start, end):  # hardlink (no copy) into the chunk dirs
            (cband / band_frames[j].name).hardlink_to(band_frames[j])
            (cmask / mask_frames[j].name).hardlink_to(mask_frames[j])
        print(f"  chunk {ci + 1}/{n_chunks}: frames {start}-{end - 1}", flush=True)
        outs = None
        for attempt in (1, 2):
            ok, fdir, tail = _pp_once(cband, cmask, cdir / "out")
            got = sorted(fdir.glob("*.png")) if fdir.is_dir() else []
            if ok and len(got) == end - start and not _looks_corrupted(got, mask_frames, start):
                outs = got
                break
            print(f"  chunk {ci + 1} attempt {attempt} bad (rc ok={ok}, {len(got)} frames, or black/NaN "
                  "output — GPU busy with something else?) — retrying" if attempt == 1 else
                  f"ProPainter failed on chunk {ci + 1}.\n" + "\n".join(tail[-8:]), flush=True)
            shutil.rmtree(cdir / "out", ignore_errors=True)
        if outs is None:
            return None
        for j, p in enumerate(outs):
            p.replace(merged / f"{start + j:05d}.png")
        shutil.rmtree(cdir, ignore_errors=True)
    print(f"ProPainter done: {total} band frames reconstructed", flush=True)
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--x", type=int, default=-1, help="caption box (omit all four to auto-detect)")
    ap.add_argument("--y", type=int, default=-1)
    ap.add_argument("--w", type=int, default=-1)
    ap.add_argument("--h", type=int, default=-1)
    ap.add_argument("--thresh", type=int, default=190, help="min brightness (V) of caption text")
    ap.add_argument("--sat", type=int, default=70, help="max saturation of caption text")
    ap.add_argument("--engine", choices=("auto", "propainter", "classic"), default="auto")
    ap.add_argument("--preview-at", type=float, default=None,
                    help="write ONE processed frame (spatial-inpaint approximation) as an image to <out> and exit")
    a = ap.parse_args()
    # ProPainter runs with cwd=PROPAINTER_DIR — all paths handed around must be absolute
    a.src = str(Path(a.src).resolve())
    a.out = str(Path(a.out).resolve())

    cap = cv2.VideoCapture(a.src)
    if not cap.isOpened():
        sys.exit(f"cannot open {a.src}")
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0

    bx, by, bw, bh = a.x, a.y, a.w, a.h
    if min(bx, by, bw, bh) < 0:
        print("no box given — auto-detecting the caption region...", flush=True)
        found = auto_detect_box(a.src, a.thresh, a.sat, W, H, total)
        if found is None:
            print("no burned-in captions detected — output is an untouched copy", flush=True)
            r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", a.src, "-c", "copy", a.out])
            sys.exit(0 if r.returncode == 0 else "copy failed")
        bx, by, bw, bh = found
        print(f"captions detected at ({bx},{by}) {bw}x{bh}", flush=True)

    # clamp the box, then snap its size UP to multiples of 16 — ProPainter silently outputs
    # black garbage when the crop width is not divisible by 16 (verified empirically)
    x = max(0, min(bx, W - 16))
    y = max(0, min(by, H - 16))
    w = min((max(16, bw) + 15) // 16 * 16, W // 16 * 16)
    h = min((max(16, bh) + 15) // 16 * 16, H // 16 * 16)
    x = min(x, W - w)
    y = min(y, H - h)
    print(f"video {W}x{H} @ {fps:.2f}fps, {total} frames — erasing text in box ({x},{y}) {w}x{h}", flush=True)

    if a.preview_at is not None:
        # quick approximation for the dashboard preview pane — mask + spatial inpaint of one
        # frame; the full run reconstructs the fill with ProPainter (video-aware, cleaner)
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, a.preview_at) * 1000)
        ok, frame = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
        cap.release()
        if not ok:
            sys.exit("cannot read a frame for preview")
        roi = frame[y:y + h, x:x + w]
        m = text_mask(roi, a.thresh, a.sat, w, h)
        if m is not None:
            fill = cv2.inpaint(roi, m, 4, cv2.INPAINT_TELEA)
            alpha = cv2.GaussianBlur(m.astype(np.float32) / 255.0, (7, 7), 0)[:, :, None]
            frame[y:y + h, x:x + w] = (alpha * fill + (1 - alpha) * roi).astype(np.uint8)
        if not cv2.imwrite(a.out, frame):
            sys.exit(f"cannot write preview to {a.out}")
        print("preview written (single-frame approximation — the full AI erase fills cleaner)", flush=True)
        return

    work = Path(tempfile.mkdtemp(prefix="erase-", dir=str(Path(a.out).parent)))
    band_dir = work / "band"
    mask_dir = work / "band_mask"
    band_dir.mkdir()
    mask_dir.mkdir()

    # pass 1: detect text, dump band crops + masks for the inpainter
    masks = []
    text_frames = 0
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        roi = frame[y:y + h, x:x + w]
        m = text_mask(roi, a.thresh, a.sat, w, h)
        if m is None:
            m = np.zeros((h, w), np.uint8)
        else:
            text_frames += 1
        masks.append(m)
        cv2.imwrite(str(band_dir / f"{i:05d}.png"), roi)
        cv2.imwrite(str(mask_dir / f"{i:05d}.png"), m)
        i += 1
        if i % 200 == 0:
            print(f"  scan {i}/{total} frames ({text_frames} with text)", flush=True)
    cap.release()
    total = i
    print(f"scan done: text found on {text_frames}/{total} frames", flush=True)

    if not text_frames:
        shutil.rmtree(work, ignore_errors=True)
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", a.src, "-c", "copy", a.out])
        sys.exit(0 if r.returncode == 0 else "copy failed")

    frames_dir = None
    if a.engine in ("auto", "propainter"):
        frames_dir = run_propainter(band_dir, mask_dir, work / "pp", total)
        if frames_dir is None and a.engine == "propainter":
            shutil.rmtree(work, ignore_errors=True)
            sys.exit("ProPainter engine requested but failed")

    # pass 2: composite the reconstructed letter pixels back and encode
    cap = cv2.VideoCapture(a.src)
    enc = open_encoder(a.src, a.out, W, H, fps)
    for i in range(total):
        ok, frame = cap.read()
        if not ok:
            break
        m = masks[i]
        if m.any():
            roi = frame[y:y + h, x:x + w]
            if frames_dir is not None:
                fill = cv2.imread(str(frames_dir / f"{i:05d}.png"))
                if fill is None or fill.shape[:2] != (h, w):
                    fill = cv2.inpaint(roi, m, 4, cv2.INPAINT_TELEA)
            else:
                fill = cv2.inpaint(roi, m, 4, cv2.INPAINT_TELEA)
            # feathered paste: only the letter pixels change, with a soft 2-3px edge
            alpha = cv2.GaussianBlur(m.astype(np.float32) / 255.0, (7, 7), 0)[:, :, None]
            frame[y:y + h, x:x + w] = (alpha * fill + (1 - alpha) * roi).astype(np.uint8)
        enc.stdin.write(frame.tobytes())
        if (i + 1) % 200 == 0:
            print(f"  encode {i + 1}/{total} frames", flush=True)
    cap.release()
    enc.stdin.close()
    enc.wait()
    shutil.rmtree(work, ignore_errors=True)
    if enc.returncode != 0:
        sys.exit("ffmpeg encoder failed")
    engine = "ProPainter AI" if frames_dir is not None else "classic inpaint"
    print(f"done: {total} frames, text erased on {text_frames} via {engine} "
          f"(visually lossless encode, CRF 12) -> {a.out}", flush=True)


if __name__ == "__main__":
    main()
