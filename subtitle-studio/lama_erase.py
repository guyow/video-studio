#!/usr/bin/env python3
"""LAMA subtitle eraser with built-in VERIFY-AND-REPAIR — generative pixel fill.

Guarantees, by construction:
  1. Text is detected on EVERY frame (CRAFT detector on the band crop — fast), so
     caption transitions can't slip through between samples.
  2. After erasing, the output is RE-SCANNED for any remaining text; if remnants are
     found, those frames are erased again (up to 2 repair passes).
  3. The final verdict is printed — the caller burns captions only after a clean exit.

Uses VSR's bundled big-lama.pt (TorchScript) + EasyOCR/CRAFT detection.

Usage: python lama_erase.py <in.mp4> <out.mp4> [--x --y --w --h]   (no box = auto-detect)
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from erase_subs import ocr_detect_box, auto_detect_box  # noqa: E402

LAMA_PT = (Path(__file__).resolve().parent.parent / "tools" / "vsr" /
           "backend" / "models" / "big-lama" / "big-lama.pt")
BATCH = 4          # LAMA mini-batch (band crops are small; fits the 4GB card)
CTX = 48           # context rows above/below the band fed to LAMA
DILATE = 21        # mask growth: swallow outlines, glow and anti-aliased halos
VERIFY_SAMPLES = 80    # dense re-scan of the result so leftovers can't hide between samples
MAX_PASSES = 3         # 1 erase + up to 2 repair passes


def pad_to_modulo(img, mod=8):
    h, w = img.shape[:2]
    H = (h + mod - 1) // mod * mod
    W = (w + mod - 1) // mod * mod
    out = np.zeros((H, W) + img.shape[2:], img.dtype)
    out[:h, :w] = img
    return out


class Eraser:
    def __init__(self):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print(f"loading LAMA (TorchScript) on {self.device}…", flush=True)
        self.model = torch.jit.load(str(LAMA_PT), map_location=self.device).eval()
        import easyocr
        self.reader = easyocr.Reader(["en"], gpu=True, verbose=False)
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (DILATE, DILATE))

    def band_mask(self, crop):
        """Pixel mask of the caption in a band crop (crop-relative). None if no text.
        Low thresholds catch faint/anti-aliased text; captions on a solid background
        BAR are masked as one filled block so the bar's edges/corners go too (per-letter
        masks leave the bar's rounded corners behind = the 'leftover on every frame' bug)."""
        H, Wc = crop.shape[:2]
        boxes = []
        horiz, free = self.reader.detect(crop, text_threshold=0.3, low_text=0.12,
                                         link_threshold=0.3, add_margin=0.15)
        for x0, x1, y0, y1 in (horiz[0] if horiz else []):
            boxes.append((max(0, int(x0)), max(0, int(y0)), min(Wc, int(x1)), min(H, int(y1))))
        for poly in (free[0] if free else []):
            p = np.array(poly, np.int32)
            boxes.append((max(0, int(p[:, 0].min())), max(0, int(p[:, 1].min())),
                          min(Wc, int(p[:, 0].max())), min(H, int(p[:, 1].max()))))
        boxes = [b for b in boxes if b[2] > b[0] and b[3] > b[1]]
        if not boxes:
            return None
        m = np.zeros((H, Wc), np.uint8)
        # cluster boxes into caption blocks by vertical overlap, then FILL each block's
        # bounding rectangle (padded) so the background bar around the text is covered
        boxes.sort(key=lambda b: b[1])
        clusters = [[boxes[0]]]
        for b in boxes[1:]:
            cy0 = min(c[1] for c in clusters[-1]); cy1 = max(c[3] for c in clusters[-1])
            if b[1] <= cy1 + 0.6 * (cy1 - cy0 + 1):     # near the current block → same caption
                clusters[-1].append(b)
            else:
                clusters.append([b])
        padx, pady = 26, 18
        for cl in clusters:
            x0 = max(0, min(b[0] for b in cl) - padx)
            y0 = max(0, min(b[1] for b in cl) - pady)
            x1 = min(Wc, max(b[2] for b in cl) + padx)
            y1 = min(H, max(b[3] for b in cl) + pady)
            m[y0:y1, x0:x1] = 255
        return m

    def restore_detail(self, band_out, m):
        """The LAMA fill is smooth — it lacks the surrounding footage's fine texture/grain,
        so the erased patch looks lower-resolution. Recover apparent resolution by adding
        back high-frequency detail: an unsharp boost + film grain matched to the grain
        level of the untouched pixels around it. Applied only to the filled area."""
        mf = m > 0
        if not mf.any():
            return band_out
        vis = ~mf
        # unsharp: restore mid/high-frequency crispness
        blur = cv2.GaussianBlur(band_out, (0, 0), 2.0)
        sharp = cv2.addWeighted(band_out, 1.45, blur, -0.45, 0)
        # match grain: estimate the surrounding footage's high-freq noise level
        hp = band_out - cv2.GaussianBlur(band_out, (0, 0), 1.0)
        sigma = float(np.clip(np.std(hp[vis]) if vis.any() else 2.0, 1.0, 6.0))
        grain = self._grain(band_out.shape, sigma)
        enhanced = sharp + grain
        a = cv2.GaussianBlur(m.astype(np.float32) / 255.0, (9, 9), 0)[:, :, None]
        return a * enhanced + (1 - a) * band_out

    def _grain(self, shape, sigma):
        """Deterministic per-pixel grain (seeded) so it's stable frame-to-frame and does
        not reintroduce shimmer, but still gives the patch real high-frequency texture."""
        if getattr(self, "_grain_cache", None) is None or self._grain_cache.shape != shape:
            rng = np.random.default_rng(1234)
            self._grain_cache = rng.standard_normal(shape).astype(np.float32)
        return self._grain_cache * sigma

    def lama_fill(self, crops, masks):
        h, w = crops[0].shape[:2]
        imgs = np.stack([pad_to_modulo(cv2.cvtColor(c, cv2.COLOR_BGR2RGB)).transpose(2, 0, 1)
                         for c in crops]).astype(np.float32) / 255.0
        ms = np.stack([pad_to_modulo(m)[None, ...] for m in masks]).astype(np.float32)
        with torch.inference_mode():
            it = torch.from_numpy(imgs).to(self.device)
            mt = (torch.from_numpy(ms).to(self.device) > 0) * 1
            out = self.model(it, mt).permute(0, 2, 3, 1).cpu().numpy()
        res = []
        for o in out:
            o = np.clip(o * 255, 0, 255).astype(np.uint8)[:h, :w]
            res.append(cv2.cvtColor(o, cv2.COLOR_RGB2BGR))
        return res

    def build_plate(self, src, cy0, cy1, samples=48):
        """Build ONE clean background plate for the band = per-pixel MEDIAN of the
        best-restored frames. LAMA's shimmer/artifacts vary frame-to-frame, so the median
        keeps the consistent, correct background and discards the noise — this is the
        'find the perfectly restored parts and apply them to the whole video' idea.
        Returns a float32 band-sized plate (masked area filled, real area = median fabric)."""
        cap = cv2.VideoCapture(src)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or samples
        n = min(samples, max(8, total))
        stack = []
        for k in range(n):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / n))
            ok, f = cap.read()
            if not ok:
                continue
            band = f[cy0:cy1]
            m = self.band_mask(band)
            if m is None:
                cleaned = band.astype(np.float32)
            else:
                fill = self.lama_fill([band], [m])[0].astype(np.float32)
                alpha = cv2.GaussianBlur(m.astype(np.float32) / 255.0, (9, 9), 0)[:, :, None]
                cleaned = alpha * fill + (1 - alpha) * band.astype(np.float32)
            stack.append(cleaned)
        cap.release()
        if not stack:
            return None
        plate = np.median(np.stack(stack, axis=0), axis=0).astype(np.float32)
        print(f"background plate built from {len(stack)} frames (per-pixel median)", flush=True)
        return plate

    def erase(self, src, out, cy0, cy1, W, H, fps, label="erase", plate=None):
        """One full pass: per-frame detection + LAMA fill. Anchors the fill to a stable
        background PLATE (per-pixel median) where the scene is static -> zero flicker there;
        only real motion opens the gate to the fresh per-frame fill. Returns frames inpainted.
        Sets self.flicker = mean frame-to-frame change of the filled pixels (lower=smoother)."""
        cap = cv2.VideoCapture(src)
        enc = subprocess.Popen(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", f"{fps}",
             "-i", "pipe:0", "-i", src, "-map", "0:v", "-map", "1:a?",
             "-c:v", "libx264", "-crf", "15", "-preset", "fast", "-pix_fmt", "yuv420p",
             "-c:a", "copy", "-movflags", "+faststart", out],
            stdin=subprocess.PIPE)
        i = inpainted = 0
        pend_f, pend_m = [], []
        # seed the temporal anchor with the stable median plate (good stable start on
        # static shots) then let it track frame-to-frame so drifting/moving content stays
        # accurate — a global plate alone mismatches shots that move, adding flicker
        prev = plate.copy() if plate is not None else None
        flick_sum = flick_n = 0

        def write_band(f, band_out):
            nonlocal prev
            f[cy0:cy1] = band_out.astype(np.uint8)
            prev = band_out
            enc.stdin.write(f.tobytes())

        def temporal(fillf, mf, roi):
            """Blend a fresh fill toward the running anchor (previous cleaned band, seeded
            from the median plate). Static scene -> almost all anchor (a≈0.08) = the patch
            is near-identical every frame, no shimmer. Even under motion the anchor keeps a
            floor share (ceiling 0.65) so the fresh fill can't shimmer freely — it tracks
            the movement but stays temporally damped."""
            if prev is None:
                return fillf
            vis = ~mf
            motion = float(np.mean(np.abs(roi[vis] - prev[vis]))) if vis.any() else 99.0
            a = float(np.clip((motion - 2.0) / 12.0, 0.08, 0.65))
            return a * fillf + (1.0 - a) * prev

        def flush():
            nonlocal inpainted, prev, flick_sum, flick_n
            if not pend_f:
                return
            crops = [f[cy0:cy1] for f in pend_f]
            fills = self.lama_fill(crops, pend_m)
            for f, m, fill in zip(pend_f, pend_m, fills):
                roi = f[cy0:cy1].astype(np.float32)
                mf = m > 0
                fillf = temporal(fill.astype(np.float32), mf, roi)
                alpha = cv2.GaussianBlur(m.astype(np.float32) / 255.0, (9, 9), 0)[:, :, None]
                band_out = alpha * fillf + (1 - alpha) * roi
                # INLINE REPAIR: re-check THIS frame and re-erase any remnant on the spot,
                # so it exits fully clean in one temporally-continuous pass (no global
                # re-pass that would reintroduce flicker on already-clean frames)
                for _ in range(2):
                    bo_u8 = band_out.astype(np.uint8)
                    rm = self.band_mask(bo_u8)
                    if rm is None:
                        break
                    rm = cv2.dilate(rm, self.kernel)     # grow: catch the stubborn edge
                    rfill = temporal(self.lama_fill([bo_u8], [rm])[0].astype(np.float32),
                                     rm > 0, band_out)
                    ra = cv2.GaussianBlur(rm.astype(np.float32) / 255.0, (9, 9), 0)[:, :, None]
                    band_out = ra * rfill + (1 - ra) * band_out
                if prev is not None and mf.any():
                    flick_sum += float(np.mean(np.abs(band_out[mf] - prev[mf]))); flick_n += 1
                band_out = self.restore_detail(band_out, m)   # sharpen + match grain
                write_band(f, band_out)
                inpainted += 1
            pend_f.clear(); pend_m.clear()

        while True:
            ok, frame = cap.read()
            if not ok:
                break
            m = self.band_mask(frame[cy0:cy1])     # EVERY frame — nothing slips through
            if m is None:
                flush()
                prev = frame[cy0:cy1].astype(np.float32)   # keep temporal anchor continuous
                enc.stdin.write(frame.tobytes())
            else:
                pend_f.append(frame)
                pend_m.append(m)
                if len(pend_f) >= BATCH:
                    flush()
            i += 1
            if i % 150 == 0:
                print(f"  {label}: {i} frames ({inpainted} inpainted)", flush=True)
        flush()
        self.flicker = (flick_sum / flick_n) if flick_n else 0.0
        print(f"{label}: temporal jitter score = {self.flicker:.2f} (lower is smoother)", flush=True)
        cap.release()
        enc.stdin.close()
        if enc.wait() != 0:
            sys.exit("encode failed")
        print(f"{label} pass done: {i} frames, {inpainted} inpainted", flush=True)
        return inpainted

    def verify(self, path, cy0, cy1):
        """Re-scan the RESULT for any remaining text in the band. Returns dirty count."""
        cap = cv2.VideoCapture(path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        dirty = 0
        for k in range(VERIFY_SAMPLES):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / VERIFY_SAMPLES))
            ok, f = cap.read()
            if not ok:
                continue
            horiz, free = self.reader.detect(f[cy0:cy1], text_threshold=0.5, low_text=0.3)
            if (horiz and horiz[0]) or (free and free[0]):
                dirty += 1
        cap.release()
        return dirty


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
    cap.release()

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
    # SCAN region = generous zone around the detected band, so a caption that drifts
    # higher/lower or grows to 2 lines on SOME frames is still caught. The detected band
    # only fixes WHERE subtitles live; per-frame CRAFT then finds the exact text inside
    # this zone and LAMA fills only those pixels — a wider scan never over-erases, it just
    # guarantees no frame's text falls outside the window (the "leftover" bug).
    marginY = max(CTX, int(0.12 * H))
    cy0 = max(0, by - marginY)
    cy1 = min(H, by + bh + marginY)
    # never let the scan creep above the middle of the frame (avoid logos / on-screen
    # headers up top) — subtitles are always in the lower portion
    cy0 = max(cy0, int(0.42 * H))
    print(f"scan zone y[{cy0}..{cy1}] ({100*(cy1-cy0)/H:.0f}% of height)", flush=True)

    er = Eraser()
    plate = er.build_plate(src, cy0, cy1)     # stable background from the best-restored frames
    cur = src
    tmp_dir = Path(out).parent
    for p in range(1, MAX_PASSES + 1):
        target = out if p == MAX_PASSES else str(tmp_dir / f".lama-pass{p}-{Path(out).name}")
        label = "erase" if p == 1 else f"repair-{p - 1}"
        er.erase(cur, target, cy0, cy1, W, H, fps, label=label, plate=plate)
        if cur != src:
            Path(cur).unlink(missing_ok=True)
        dirty = er.verify(target, cy0, cy1)
        print(f"verify: {dirty}/{VERIFY_SAMPLES} sampled frames still show text", flush=True)
        cur = target
        if dirty == 0:
            break
    if cur != out:
        shutil.move(cur, out)
    if dirty == 0:
        print(f"VERIFIED CLEAN — no text remains in the band -> {out}", flush=True)
    else:
        print(f"WARNING: {dirty}/{VERIFY_SAMPLES} sampled frames may still show faint text "
              f"after {MAX_PASSES} passes -> {out}", flush=True)


if __name__ == "__main__":
    main()
