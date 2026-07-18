#!/usr/bin/env python3
"""Visual Repair — restore lip-sync visual damage from the ORIGINAL video. No AI, free.

The lip-sync/restoration AI corrupts pixels it should never touch (objects near the
mouth, background warps, whole-face "beautification"). The original source video is
pixel-perfect ground truth everywhere EXCEPT the mouth. So:

  1. ALIGN   — match every dubbed frame to its source frame (the finals are shorter
               than their sources, so the mapping is estimated, never assumed):
               ZNCC on downscaled grayscale (mouth box masked out), anchor frames at
               high-motion moments, model fit (identity / offset / retime) with
               held-out validation, per-frame refinement, monotone smoothing.
               If confidence is too low the tool REFUSES — a misaligned composite
               is worse than the artifact.
  2. COMPOSITE — every output frame = the original frame, with ONLY the protected
               mouth/jaw region kept from the dub (feathered ellipse mask +
               low-frequency color transfer so no seam shows). Audio = the dub's.

Runs on CPU under autoVSL/.venv (cv2 + numpy + scipy). Writes a versioned take
(repair-visual-<stamp>.mp4) — final.mp4 is never touched.

Modes:
  full run:     visual_repair.py --work <dir> --box X Y W H [options]
  preview:      ... --preview-at SECONDS   (writes preview-visual.jpg strip + exits)
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

LOW_SIDE = 96            # long side of the alignment thumbnails
MAX_ANCHORS = 40
ANCHOR_SPACING = 10
RESID_LIMIT = 1.5        # median |residual| (frames) above which the model fit is distrusted
ZNCC_FLOOR = 0.6         # below this mean confidence we refuse to composite
ARTIFACT_THRESH = 20.0   # worst-block gray-level diff outside the box that flags a frame


def die(msg: str) -> None:
    print(f"ERROR: {msg}", flush=True)
    sys.exit(1)


# ---------------------------------------------------------------- probing / decode

def probe(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        die(f"ffprobe failed on {path.name}: {(r.stderr or '')[-200:]}")
    d = json.loads(r.stdout)
    s = d["streams"][0]
    num, den = (int(x) for x in s["r_frame_rate"].split("/"))
    return {"w": int(s["width"]), "h": int(s["height"]),
            "fps": num / max(1, den), "fps_str": s["r_frame_rate"],
            "nb": int(s.get("nb_frames") or 0),
            "dur": float(d["format"].get("duration") or 0)}


def _read_exact(pipe, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def decode_lowres(path: Path, fps_str: str, lw: int, lh: int,
                  ss: float | None = None, t: float | None = None) -> np.ndarray:
    """Decode a whole video (or a window) to [n, lh, lw] uint8 grayscale at a pinned fps."""
    cmd = ["ffmpeg", "-v", "error"]
    if ss is not None:
        cmd += ["-ss", f"{max(0.0, ss):.3f}"]
    cmd += ["-i", str(path)]
    if t is not None:
        cmd += ["-t", f"{t:.3f}"]
    cmd += ["-vf", f"fps={fps_str},scale={lw}:{lh}", "-pix_fmt", "gray",
            "-f", "rawvideo", "pipe:1"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        die(f"low-res decode failed on {path.name}: {(r.stderr or b'').decode('utf-8', 'replace')[-200:]}")
    n = len(r.stdout) // (lw * lh)
    return np.frombuffer(r.stdout[:n * lw * lh], dtype=np.uint8).reshape(n, lh, lw)


def open_reader(path: Path, fps_str: str, w: int, h: int) -> subprocess.Popen:
    """Streaming full-res BGR reader at a pinned fps (normalizes VFR + rotation)."""
    return subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-vf", f"fps={fps_str},scale={w}:{h}", "-pix_fmt", "bgr24",
         "-f", "rawvideo", "pipe:1"],
        stdout=subprocess.PIPE, bufsize=10 ** 7)


def decode_one(path: Path, at: float, w: int, h: int) -> np.ndarray | None:
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, at):.3f}", "-i", str(path),
         "-frames:v", "1", "-vf", f"scale={w}:{h}", "-pix_fmt", "bgr24",
         "-f", "rawvideo", "pipe:1"],
        capture_output=True)
    if len(r.stdout) < w * h * 3:
        return None
    return np.frombuffer(r.stdout[:w * h * 3], dtype=np.uint8).reshape(h, w, 3).copy()


# ---------------------------------------------------------------- alignment

def normalize_rows(frames: np.ndarray, keep: np.ndarray) -> np.ndarray:
    """[n,h,w] uint8 → [n,npix] float32, masked to `keep`, zero-mean unit-norm rows (for ZNCC)."""
    flat = frames.reshape(frames.shape[0], -1)[:, keep].astype(np.float32)
    flat -= flat.mean(axis=1, keepdims=True)
    norm = np.linalg.norm(flat, axis=1, keepdims=True)
    norm[norm < 1e-6] = 1e-6
    return flat / norm


def pick_anchors(low_final: np.ndarray) -> list[int]:
    """Frames with the most motion make the sharpest match peaks."""
    n = low_final.shape[0]
    act = np.abs(np.diff(low_final.astype(np.int16), axis=0)).mean(axis=(1, 2))
    order = np.argsort(act)[::-1]
    anchors, taken = [], np.zeros(n, bool)
    for j in order:
        j = int(j)
        if taken[max(0, j - ANCHOR_SPACING):j + ANCHOR_SPACING].any():
            continue
        anchors.append(j)
        taken[j] = True
        if len(anchors) >= MAX_ANCHORS:
            break
    return sorted(anchors)


def fit_mapping(low_src: np.ndarray, low_final: np.ndarray, keep: np.ndarray) -> tuple[np.ndarray, dict]:
    """Estimate the monotone map: final frame j → source frame i. Returns (map, info)."""
    from scipy.signal import medfilt
    from scipy.stats import theilslopes

    n_src, n_fin = low_src.shape[0], low_final.shape[0]
    S = normalize_rows(low_src, keep)
    anchors = pick_anchors(low_final)
    A = normalize_rows(low_final[anchors], keep)

    matches, weights = [], []
    for k, j in enumerate(anchors):
        scores = S @ A[k]
        i = int(np.argmax(scores))
        top = float(scores[i])
        scores[max(0, i - 3):i + 4] = -1           # peak sharpness vs next-best elsewhere
        ratio = top / max(1e-6, float(scores.max()))
        matches.append((j, i, top))
        weights.append(ratio)
    js = np.array([m[0] for m in matches], float)
    is_ = np.array([m[1] for m in matches], float)
    top_scores = np.array([m[2] for m in matches])

    good = top_scores > 0.5                        # ignore hopeless anchors in the fit
    if good.sum() < 6:
        return _perframe_map(S, low_final, keep, n_src, n_fin), {"model": "per-frame", "note": "few good anchors"}
    jg, ig = js[good], is_[good]

    hold = np.zeros(len(jg), bool)
    hold[::5] = True                               # ~20% held out for validation
    jf, if_ = jg[~hold], ig[~hold]

    cands = {"identity": (1.0, 0.0), "offset": (1.0, float(np.median(if_ - jf)))}
    if len(jf) >= 4:
        slope, intercept, _, _ = theilslopes(if_, jf)
        cands["retime"] = (float(slope), float(intercept))

    def resid(a, b):
        return np.median(np.abs(a * jg + b - ig))

    scored = sorted(((resid(a, b), name, a, b) for name, (a, b) in cands.items()))
    best_r, model, a, b = scored[0]
    for r, name, aa, bb in scored:                 # prefer the simplest model within 0.5 fr
        if r <= best_r + 0.5 and name in ("identity", "offset"):
            best_r, model, a, b = r, name, aa, bb
            break

    held_ok = int(np.sum(np.abs(a * jg[hold] + b - ig[hold]) <= 3)) if hold.any() else 0
    held_n = int(hold.sum())
    info = {"model": model, "a": round(a, 6), "b": round(b, 3),
            "median_resid": round(float(best_r), 2),
            "heldout_ok": f"{held_ok}/{held_n}", "anchors": len(anchors)}

    if best_r > RESID_LIMIT or (held_n >= 3 and held_ok < held_n * 0.8):
        print(f"model fit rejected ({info}) — falling back to per-frame search", flush=True)
        return _perframe_map(S, low_final, keep, n_src, n_fin), {**info, "model": "per-frame"}

    # per-frame refinement ±2 around the model, then smooth + enforce monotone
    F = normalize_rows(low_final, keep)
    mapping = np.empty(n_fin, np.int64)
    conf = np.empty(n_fin, np.float32)
    for j in range(n_fin):
        p = int(round(a * j + b))
        lo, hi = max(0, p - 2), min(n_src, p + 3)
        if lo >= hi:
            lo, hi = min(max(0, p), n_src - 1), min(max(0, p), n_src - 1) + 1
        scores = S[lo:hi] @ F[j]
        mapping[j] = lo + int(np.argmax(scores))
        conf[j] = float(scores.max())
        if j % 200 == 0:
            print(f"align {j}/{n_fin}", flush=True)
    mapping = medfilt(mapping, 5).astype(np.int64)
    mapping = np.maximum.accumulate(mapping)
    np.clip(mapping, 0, n_src - 1, out=mapping)
    info["mean_zncc"] = round(float(conf.mean()), 3)
    if info["mean_zncc"] < ZNCC_FLOOR:
        die(f"alignment confidence too low (mean ZNCC {info['mean_zncc']} < {ZNCC_FLOOR}) — "
            "refusing to composite. The source in source.txt may not be the video this dub was made from.")
    return mapping, info


def _perframe_map(S, low_final, keep, n_src, n_fin) -> np.ndarray:
    """Monotone local search fallback (handles piecewise retimes)."""
    from scipy.signal import medfilt
    F = normalize_rows(low_final, keep)
    step = n_src / max(1, n_fin)
    mapping = np.empty(n_fin, np.int64)
    confs = []
    prev = 0
    for j in range(n_fin):
        lo = max(prev, int(j * step) - 10)
        hi = min(n_src, max(lo + 1, int(j * step) + int(step) + 6))
        scores = S[lo:hi] @ F[j]
        mapping[j] = prev = lo + int(np.argmax(scores))
        confs.append(float(scores.max()))
        if j % 200 == 0:
            print(f"align {j}/{n_fin}", flush=True)
    mean_c = float(np.mean(confs))
    print(f"per-frame alignment mean ZNCC {mean_c:.3f}", flush=True)
    if mean_c < ZNCC_FLOOR:
        die(f"alignment confidence too low (mean ZNCC {mean_c:.2f} < {ZNCC_FLOOR}) — refusing to composite.")
    mapping = medfilt(mapping, 5).astype(np.int64)
    return np.maximum.accumulate(np.clip(mapping, 0, n_src - 1))


# ---------------------------------------------------------------- compositing

def build_mask(box: tuple[int, int, int, int], W: int, H: int, feather: int):
    """Feathered ellipse inscribed in the box. Returns (roi slice coords, float32 alpha [rh,rw,1])."""
    x, y, w, h = box
    pad = feather * 2
    r0, r1 = max(0, y - pad), min(H, y + h + pad)
    c0, c1 = max(0, x - pad), min(W, x + w + pad)
    m = np.zeros((r1 - r0, c1 - c0), np.float32)
    cv2.ellipse(m, ((x - c0) + w // 2, (y - r0) + h // 2), (w // 2, h // 2),
                0, 0, 360, 1.0, -1)
    k = feather * 2 + 1
    m = cv2.GaussianBlur(m, (k | 1, k | 1), feather / 2.0)
    return (r0, r1, c0, c1), m[:, :, None]


def composite_roi(base: np.ndarray, final: np.ndarray, roi, alpha: np.ndarray,
                  color_fix: bool, sigma: float) -> None:
    """Blend the final's protected region onto the base (source) frame, in place."""
    r0, r1, c0, c1 = roi
    s = base[r0:r1, c0:c1].astype(np.float32)
    f = final[r0:r1, c0:c1].astype(np.float32)
    if color_fix:  # low-frequency difference transfer: fixes AI color shift + shadow gradients
        f += cv2.GaussianBlur(s, (0, 0), sigma) - cv2.GaussianBlur(f, (0, 0), sigma)
    out = s * (1.0 - alpha) + np.clip(f, 0, 255) * alpha
    base[r0:r1, c0:c1] = np.clip(out, 0, 255).astype(np.uint8)


def heatmap(src: np.ndarray, fin: np.ndarray, box) -> np.ndarray:
    d = cv2.absdiff(cv2.cvtColor(src, cv2.COLOR_BGR2GRAY), cv2.cvtColor(fin, cv2.COLOR_BGR2GRAY))
    d = cv2.applyColorMap(cv2.convertScaleAbs(d, alpha=3.0), cv2.COLORMAP_JET)
    x, y, w, h = box
    cv2.rectangle(d, (x, y), (x + w, y + h), (255, 255, 255), 2)
    return d


# ---------------------------------------------------------------- auto lip-box

def suggest_box(work: Path, source: Path, final: Path) -> None:
    """Find the lip region WITHOUT the user drawing anything.

    Insight: the dub re-draws the lips in (almost) EVERY frame, so lip pixels
    differ from the original persistently. Artifacts (a mangled cup, background
    warps) only differ in SOME frames. New burned captions also persist, but
    they live in the bottom band, which is excluded. → the largest blob of
    persistently-changed pixels above the caption band is the mouth."""
    pf = probe(final)
    W, H = pf["w"], pf["h"]
    lw = LOW_SIDE if W >= H else max(16, round(LOW_SIDE * W / H))
    lh = LOW_SIDE if H > W else max(16, round(LOW_SIDE * H / W))
    lw, lh = lw - lw % 2, lh - lh % 2

    print("analyzing where the dub persistently differs from the original…", flush=True)
    low_src = decode_lowres(source, pf["fps_str"], lw, lh)
    low_fin = decode_lowres(final, pf["fps_str"], lw, lh)
    if low_src.shape[0] == 0 or low_fin.shape[0] == 0:
        die("decode produced no frames")

    # alignment without a mask (the mouth is small; ZNCC tolerates it)
    keep = np.ones(lh * lw, bool)
    mapping, info = fit_mapping(low_src, low_fin, keep)
    print(f"alignment: {json.dumps(info)}", flush=True)

    n_fin = low_fin.shape[0]
    samples = range(0, n_fin, max(1, n_fin // 200))
    persist = np.zeros((lh, lw), np.float32)
    for j in samples:
        d = np.abs(low_fin[j].astype(np.int16) - low_src[mapping[j]].astype(np.int16))
        persist += (d > 20)
    persist /= max(1, len(list(samples)))

    cand = (persist > 0.6).astype(np.uint8)
    cand = cv2.dilate(cand, np.ones((3, 3), np.uint8))
    n_lbl, lbl, stats, _ = cv2.connectedComponentsWithStats(cand)
    if n_lbl < 2:
        die("could not auto-detect the lip region — the dub barely differs persistently "
            "from the original. Draw the box, or describe where the lips are.")
    # rank blobs: burned captions are wide-and-short text bands low in the frame —
    # reject those; the mouth is a compact blob in the upper 3/4
    best, best_area = None, 0
    for k in range(1, n_lbl):
        x, y, w, h = (stats[k, cv2.CC_STAT_LEFT], stats[k, cv2.CC_STAT_TOP],
                      stats[k, cv2.CC_STAT_WIDTH], stats[k, cv2.CC_STAT_HEIGHT])
        area = int(stats[k, cv2.CC_STAT_AREA])
        aspect = w / max(1, h)
        center_y = (y + h / 2) / lh
        if aspect > 3.5 or center_y > 0.75:            # text-shaped or caption-band → skip
            continue
        frac = area / (lh * lw)
        if not 0.0005 <= frac <= 0.25:
            continue
        if area > best_area:
            best, best_area = (x, y, w, h), area
    if best is None:
        die("auto-detect only found caption-shaped change regions — the lips may be hidden. "
            "Draw the box, or describe where the lips are.")
    x, y, w, h = best
    # scale to full res + pad 25%
    sx, sy = W / lw, H / lh
    px, py = int(w * sx * 0.25), int(h * sy * 0.25)
    bx = max(0, int(x * sx) - px)
    by = max(0, int(y * sy) - py)
    bw = min(W - bx, int(w * sx) + 2 * px)
    bh = min(H - by, int(h * sy) + 2 * py)
    box = {"x": bx, "y": by, "w": bw, "h": bh}
    print(f"BOX: {json.dumps(box)}", flush=True)

    # preview strip at the highest-motion sampled moment, with the suggested box
    act = np.abs(np.diff(low_fin.astype(np.int16), axis=0)).mean(axis=(1, 2))
    j_star = int(np.argmax(act[: n_fin - 1]))
    at = j_star / pf["fps"]
    do_preview(work, source, final, (bx, by, bw, bh), at, True,
               max(8, min(40, round(0.15 * min(bw, bh)))))


# ---------------------------------------------------------------- main paths

def do_preview(work: Path, source: Path, final: Path, box, at: float,
               color_fix: bool, feather: int) -> None:
    pf, ps = probe(final), probe(source)
    at = min(max(0.1, at), max(0.1, pf["dur"] - 0.2))
    lw = LOW_SIDE if pf["w"] >= pf["h"] else max(16, round(LOW_SIDE * pf["w"] / pf["h"]))
    lh = LOW_SIDE if pf["h"] > pf["w"] else max(16, round(LOW_SIDE * pf["h"] / pf["w"]))
    lw, lh = lw - lw % 2, lh - lh % 2

    # local alignment: search the source around both hypotheses (truncation / retime)
    guesses = [at, at * (ps["dur"] / max(0.1, pf["dur"]))]
    win0 = max(0.0, min(guesses) - 1.5)
    win1 = max(guesses) + 1.5
    low_win = decode_lowres(source, pf["fps_str"], lw, lh, ss=win0, t=win1 - win0)
    if low_win.shape[0] == 0:
        die("could not decode a source window for the preview")
    fin_low = decode_lowres(final, pf["fps_str"], lw, lh, ss=at, t=1.2 / pf["fps"])
    if fin_low.shape[0] == 0:
        die("could not decode the final frame for the preview")
    sx, sy = lw / pf["w"], lh / pf["h"]
    keep = np.ones(lh * lw, bool).reshape(lh, lw)
    bx, by, bw, bh = box
    keep[int(by * sy):int((by + bh) * sy) + 1, int(bx * sx):int((bx + bw) * sx) + 1] = False
    keep = keep.reshape(-1)
    S = normalize_rows(low_win, keep)
    f = normalize_rows(fin_low[:1], keep)[0]
    scores = S @ f
    idx = int(np.argmax(scores))
    t_src = win0 + idx / pf["fps"]
    zncc = float(scores.max())
    model = "truncation" if abs(t_src - at) < abs(t_src - guesses[1]) else "retime"
    print(f"ALIGN: {json.dumps({'zncc': round(zncc, 3), 'src_time': round(t_src, 3), 'final_time': round(at, 3), 'looks_like': model})}", flush=True)

    src_f = decode_one(source, t_src, pf["w"], pf["h"])
    fin_f = decode_one(final, at, pf["w"], pf["h"])
    if src_f is None or fin_f is None:
        die("could not decode full-res preview frames")
    comp = src_f.copy()
    roi, alpha = build_mask(box, pf["w"], pf["h"], feather)
    composite_roi(comp, fin_f, roi, alpha, color_fix, sigma=max(4.0, min(bw, bh) / 4.0))
    hm = heatmap(src_f, fin_f, box)

    ph = 480
    panels = []
    for img, label in ((src_f, "ORIGINAL"), (fin_f, "DUBBED (artifacts)"),
                       (comp, "REPAIRED"), (hm, "DIFF (white box = kept lips)")):
        p = cv2.resize(img, (round(pf["w"] * ph / pf["h"]), ph))
        cv2.putText(p, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 0, 0), 4)
        cv2.putText(p, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 1)
        panels.append(p)
    strip = np.hstack(panels)
    out = work / "preview-visual.jpg"
    cv2.imwrite(str(out), strip, [cv2.IMWRITE_JPEG_QUALITY, 88])
    print(f"PREVIEW: output/script-swap/{work.name}/preview-visual.jpg", flush=True)
    if zncc < ZNCC_FLOOR:
        print(f"WARNING: low match confidence ({zncc:.2f}) at this moment — try another timestamp", flush=True)


def do_repair(work: Path, source: Path, final: Path, box, feather: int,
              color_fix: bool, track: bool, encoder: str) -> None:
    pf, ps = probe(final), probe(source)
    W, H = pf["w"], pf["h"]
    lw = LOW_SIDE if W >= H else max(16, round(LOW_SIDE * W / H))
    lh = LOW_SIDE if H > W else max(16, round(LOW_SIDE * H / W))
    lw, lh = lw - lw % 2, lh - lh % 2

    print("=== stage: align", flush=True)
    print(f"decoding thumbnails ({lw}x{lh}) of both videos…", flush=True)
    low_src = decode_lowres(source, pf["fps_str"], lw, lh)
    low_fin = decode_lowres(final, pf["fps_str"], lw, lh)
    n_src, n_fin = low_src.shape[0], low_fin.shape[0]
    print(f"source {n_src} frames, final {n_fin} frames @ {pf['fps_str']} fps", flush=True)
    if n_src == 0 or n_fin == 0:
        die("decode produced no frames")

    sx, sy = lw / W, lh / H
    bx, by, bw, bh = box
    keep2d = np.ones((lh, lw), bool)
    keep2d[int(by * sy):int((by + bh) * sy) + 1, int(bx * sx):int((bx + bw) * sx) + 1] = False
    keep = keep2d.reshape(-1)

    mapping, info = fit_mapping(low_src, low_fin, keep)
    print(f"alignment: {json.dumps(info)}", flush=True)

    # artifact report (pre-repair): LOCALIZED damage detector — the worst 8px block
    # outside the mouth box (a frame-wide mean would dilute a mangled cup to nothing)
    B = 8
    bh_, bw_ = (lh // B) * B, (lw // B) * B
    diffs = np.empty(n_fin, np.float32)
    for j in range(n_fin):
        d = np.abs(low_fin[j].astype(np.int16) - low_src[mapping[j]].astype(np.int16)).astype(np.float32)
        d[~keep2d] = 0
        diffs[j] = d[:bh_, :bw_].reshape(bh_ // B, B, bw_ // B, B).mean(axis=(1, 3)).max()
    flagged = int((diffs > ARTIFACT_THRESH).sum())
    print(f"artifact scan: {flagged} of {n_fin} frames differ visibly from the original outside "
          f"the lip box (worst block {diffs.max():.0f}, incl. any re-burned captions)", flush=True)

    track_off = np.zeros((n_fin, 2), np.int32)
    if track:
        tpl = low_src[mapping[0], int(by * sy):int((by + bh) * sy) + 1,
                      int(bx * sx):int((bx + bw) * sx) + 1].astype(np.float32)
        from scipy.signal import medfilt
        dxs, dys = np.zeros(n_fin), np.zeros(n_fin)
        for j in range(n_fin):
            res = cv2.matchTemplate(low_src[mapping[j]].astype(np.float32), tpl, cv2.TM_CCOEFF_NORMED)
            _, _, _, loc = cv2.minMaxLoc(res)
            dxs[j] = loc[0] - bx * sx
            dys[j] = loc[1] - by * sy
        track_off[:, 0] = (medfilt(dxs, 9) / sx).astype(np.int32)
        track_off[:, 1] = (medfilt(dys, 9) / sy).astype(np.int32)
        print(f"tracking: max drift {np.abs(track_off).max()} px", flush=True)

    del low_src, low_fin

    print("=== stage: composite", flush=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = work / f"repair-visual-{stamp}.mp4"
    sigma = max(4.0, min(bw, bh) / 4.0)
    base_roi, base_alpha = build_mask(box, W, H, feather)

    vcodec = (["-c:v", "h264_nvenc", "-cq", "19", "-preset", "p4"] if encoder == "nvenc"
              else ["-c:v", "libx264", "-crf", "17", "-preset", "medium"])
    writer = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
         "-r", pf["fps_str"], "-i", "pipe:0",
         "-i", str(final),
         "-map", "0:v:0", "-map", "1:a:0?",
         *vcodec, "-pix_fmt", "yuv420p", "-frames:v", str(n_fin), "-c:a", "copy",
         str(out_path)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10 ** 7)

    src_reader = open_reader(source, pf["fps_str"], W, H)
    fin_reader = open_reader(final, pf["fps_str"], W, H)
    fsize = W * H * 3
    src_idx, src_frame = -1, None
    try:
        for j in range(n_fin):
            raw_f = _read_exact(fin_reader.stdout, fsize)
            if raw_f is None:
                die(f"dub reader ended early at frame {j}/{n_fin} — aborting (no silent truncation)")
            fin_frame = np.frombuffer(raw_f, np.uint8).reshape(H, W, 3)
            target = int(mapping[j])
            while src_idx < target:
                raw_s = _read_exact(src_reader.stdout, fsize)
                if raw_s is None:
                    break
                src_idx += 1
                src_frame = np.frombuffer(raw_s, np.uint8).reshape(H, W, 3)
            if src_frame is None:
                out = fin_frame.copy()          # no source available — pass through
            else:
                out = src_frame.copy()
                if track and (track_off[j] != 0).any():
                    dx, dy = int(track_off[j][0]), int(track_off[j][1])
                    tb = (min(max(0, bx + dx), W - bw), min(max(0, by + dy), H - bh), bw, bh)
                    roi, alpha = build_mask(tb, W, H, feather)
                else:
                    roi, alpha = base_roi, base_alpha
                composite_roi(out, fin_frame, roi, alpha, color_fix, sigma)
            writer.stdin.write(out.tobytes())
            if j % 25 == 0:
                pct = 20 + int(78 * j / max(1, n_fin))
                print(f"{pct}%| composite {j}/{n_fin}", flush=True)
        writer.stdin.close()
        rc = writer.wait()
        if rc != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
            err = (writer.stderr.read() or b"").decode("utf-8", "replace")[-400:]
            out_path.unlink(missing_ok=True)
            die("encode failed: " + err)
    finally:
        for p in (src_reader, fin_reader):
            try:
                p.kill()
            except Exception:
                pass

    # hard guarantee: output has EXACTLY the dub's frame count (the old -shortest
    # mux behavior silently dropped trailing frames — 657 out of 660)
    chk = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(out_path)],
        capture_output=True, text=True)
    n_out = int((chk.stdout or "0").strip() or 0)
    if n_out != n_fin:
        out_path.unlink(missing_ok=True)
        die(f"output frame count {n_out} != dub {n_fin} — take deleted, not delivered broken")

    report = {"alignment": info, "frames": n_fin,
              "frames_with_artifacts": flagged,
              "artifact_threshold": ARTIFACT_THRESH,
              "mean_outside_diff": round(float(diffs.mean()), 2),
              "worst_outside_diff": round(float(diffs.max()), 2),
              "box": {"x": bx, "y": by, "w": bw, "h": bh},
              "color_fix": color_fix, "track": track, "created": time.time()}
    (work / "repair-visual-report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")

    versions_file = work / "versions.json"
    try:
        versions = json.loads(versions_file.read_text(encoding="utf-8")) if versions_file.is_file() else {}
    except Exception:
        versions = {}
    versions[out_path.name] = {"created": time.time(), "repair": "visual"}
    versions_file.write_text(json.dumps(versions, indent=1), encoding="utf-8")

    print(f"100%| composite {n_fin}/{n_fin}", flush=True)
    print(f"repaired {flagged} damaged frame(s); every pixel outside the lip region is now the original", flush=True)
    print(f"RESULT: output/script-swap/{work.name}/{out_path.name}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--box", nargs=4, type=int, default=None, metavar=("X", "Y", "W", "H"))
    ap.add_argument("--suggest-box", action="store_true",
                    help="auto-detect the lip region (no drawing) and write a preview")
    ap.add_argument("--feather", type=int, default=0, help="0 = auto")
    ap.add_argument("--no-color-fix", action="store_true")
    ap.add_argument("--track", action="store_true")
    ap.add_argument("--encoder", choices=("x264", "nvenc"), default="x264")
    ap.add_argument("--preview-at", type=float, default=None)
    a = ap.parse_args()

    work = Path(a.work).resolve()
    final = work / "final.mp4"
    if not final.is_file():
        die("no final.mp4 in the workdir")
    src_txt = work / "source.txt"
    if not src_txt.is_file():
        die("no source.txt — this dub has no recorded source video")
    source = Path(src_txt.read_text(encoding="utf-8").strip())
    if not source.is_file():
        die(f"source video missing: {source}")
    if a.suggest_box:
        suggest_box(work, source, final)
        return
    if not a.box:
        die("--box is required (or use --suggest-box)")
    box = tuple(a.box)
    if box[2] < 16 or box[3] < 16:
        die("protected box is too small")
    feather = a.feather or max(8, min(40, round(0.15 * min(box[2], box[3]))))

    if a.preview_at is not None:
        do_preview(work, source, final, box, a.preview_at, not a.no_color_fix, feather)
    else:
        do_repair(work, source, final, box, feather, not a.no_color_fix, a.track, a.encoder)


if __name__ == "__main__":
    main()
