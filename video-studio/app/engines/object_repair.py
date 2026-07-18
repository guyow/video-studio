#!/usr/bin/env python3
"""Object Repair — keep the dub 100%, restore ONLY a damaged object from the original.

The inverse of visual_repair: there, the original is the base and the dub supplies the
lips. Here the DUB is the base (its lip-sync is good and must never be touched) and we
surgically restore the pixels the AI damaged around one OBJECT the user names (e.g. a
cup that warps whenever it nears the mouth). The original video is ground truth.

Pipeline (parameters from measured footage):
  1. align dub↔source (reuse visual_repair.fit_mapping; identity expected — hard-warn otherwise)
  2. track the object: NCC template matching at half-res on the ORIGINAL (clean),
     keyframed by Claude-vision boxes; score<0.5 → interpolation fallback + wider corridor
  3. damage mask per frame, ONLY inside the tracked ROI corridor:
     |orig−dub| (blur σ2, channel-max) seed τ=25 → hysteresis grow to 12 → morphology →
     keep components overlapping ROI → clip to ROI+40px corridor
  4. mouth protection: subtract the interpolated lip ellipse from the mask UNLESS the
     object genuinely occludes the mouth (IoU gate 0.3 open / 0.15 close, 4-frame hold)
  5. temporal OR over ±2 frames (kills flicker; doubles as entry/exit ramp), feather σ6
  6. composite original over dub where masked; encode crf16, audio copied from dub,
     NO -shortest; output frame count is verified == dub frame count or the take is deleted

ROIs come from a JSON file (written by the /api/dubsync/advise vision call):
  {"samples":[{"j": <frame>, "obj": {x,y,w,h}|null, "lips": {x,y,w,h}|null}, ...]}
"""
import argparse
import json
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from visual_repair import (die, probe, decode_lowres, open_reader, _read_exact,
                           decode_one, fit_mapping)

SEED_T = 25.0        # damage seed threshold (channel-max gray levels, blurred)
GROW_T = 12.0        # hysteresis growth threshold
CORRIDOR = 40        # px the mask may extend beyond the tracked ROI
ROI_GATE_DIL = 16    # px ROI dilation when testing component overlap
MASK_DIL = 9         # final mask dilation radius
FEATHER_S = 6.0      # feather sigma
# mouth gate: share of the lip ellipse covered by cup-connected damage. The mask is
# corridor-clipped to the object, so only true cup-at-face frames produce coverage —
# low thresholds are safe (a distant cup can never reach the lips).
GATE_OPEN, GATE_CLOSE, GATE_HOLD = 0.15, 0.05, 4
NCC_FLOOR = 0.5
TEMPORAL = 2         # OR over ±TEMPORAL frames


# ---------------------------------------------------------------- roi helpers

def load_samples(path: Path, n_fin: int):
    d = json.loads(path.read_text(encoding="utf-8"))
    samples = sorted((s for s in d.get("samples", []) if s.get("obj")), key=lambda s: s["j"])
    lips = sorted((s for s in d.get("samples", []) if s.get("lips")), key=lambda s: s["j"])
    if not samples:
        die("no object boxes in the ROI file — the advisor could not locate the object")
    return samples, lips


def interp_boxes(samples, key: str, n_fin: int) -> np.ndarray:
    """Per-frame [x,y,w,h] by linear interpolation between sample frames."""
    out = np.zeros((n_fin, 4), np.float32)
    pts = [(s["j"], s[key]) for s in samples if s.get(key)]
    js = [p[0] for p in pts]
    for j in range(n_fin):
        if j <= js[0]:
            b = pts[0][1]
        elif j >= js[-1]:
            b = pts[-1][1]
        else:
            k = next(i for i in range(len(js) - 1) if js[i] <= j <= js[i + 1])
            t = (j - js[k]) / max(1, js[k + 1] - js[k])
            b0, b1 = pts[k][1], pts[k + 1][1]
            b = {c: b0[c] + t * (b1[c] - b0[c]) for c in ("x", "y", "w", "h")}
        out[j] = (b["x"], b["y"], b["w"], b["h"])
    return out


def iou(a, b) -> float:
    ax0, ay0, ax1, ay1 = a[0], a[1], a[0] + a[2], a[1] + a[3]
    bx0, by0, bx1, by1 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    ix = max(0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0, min(ay1, by1) - max(ay0, by0))
    inter = ix * iy
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


# ---------------------------------------------------------------- tracking

def track_object(source: Path, pf: dict, samples, n_fin: int, key: str = "obj",
                 low: np.ndarray | None = None):
    """NCC template tracking at half-res on the ORIGINAL, keyframed by vision boxes.
    Returns (boxes [n,4] full-res, low_score_frames set, low array for reuse)."""
    W, H = pf["w"], pf["h"]
    hw, hh = W // 2, H // 2
    if low is None:
        low = decode_lowres(source, pf["fps_str"], hw, hh)   # grayscale half-res, full video
    n_src = low.shape[0]
    interp = interp_boxes(samples, key, n_fin)

    keyframes = {s["j"]: s[key] for s in samples if s.get(key)}
    key_js = sorted(keyframes)

    pos = interp.copy()          # start from interpolation; NCC only REFINES locally
    low_score = []
    tpl = None
    MAX_DEV = 60                 # half-res px a match may deviate from the vision path
    for j in range(n_fin):
        if j >= n_src:
            break
        # refresh template at each keyframe (from the clean ORIGINAL, padded 1.2x)
        near = min(key_js, key=lambda kk: abs(kk - j))
        if tpl is None or j in keyframes:
            b = keyframes[near]
            pad_w, pad_h = b["w"] * 0.1, b["h"] * 0.1
            x0 = int(max(0, (b["x"] - pad_w) / 2)); y0 = int(max(0, (b["y"] - pad_h) / 2))
            x1 = int(min(hw, (b["x"] + b["w"] + pad_w) / 2)); y1 = int(min(hh, (b["y"] + b["h"] + pad_h) / 2))
            kj = min(near, n_src - 1)
            tpl = low[kj, y0:y1, x0:x1].astype(np.float32)
        th, tw = tpl.shape
        if th < 8 or tw < 8:
            continue
        # search window is anchored to the INTERPOLATED path — a false match can
        # never walk the tracker away (the earlier prev-match anchoring drifted
        # into the bright background and stuck there)
        cx, cy = interp[j, 0] / 2, interp[j, 1] / 2
        sx0 = int(max(0, cx - 72)); sy0 = int(max(0, cy - 72))
        sx1 = int(min(hw, cx + tw + 72)); sy1 = int(min(hh, cy + th + 72))
        win = low[j, sy0:sy1, sx0:sx1].astype(np.float32)
        if win.shape[0] <= th or win.shape[1] <= tw:
            continue
        res = cv2.matchTemplate(win, tpl, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        x_half, y_half = sx0 + loc[0], sy0 + loc[1]
        dev = max(abs(x_half - cx), abs(y_half - cy))
        if score >= NCC_FLOOR and dev <= MAX_DEV:
            pos[j, 0], pos[j, 1] = x_half * 2, y_half * 2
        else:
            low_score.append(j)          # keep interpolated position; corridor widened later
        if j % 200 == 0:
            print(f"track {j}/{n_fin}", flush=True)

    # smooth trajectory (median-5) — no jump clamp needed: deviation is bounded above
    from scipy.signal import medfilt
    for c in (0, 1):
        pos[:, c] = medfilt(pos[:, c], 5)
    return pos, set(low_score), low


# ---------------------------------------------------------------- mask builder

def damage_mask(orig: np.ndarray, dub: np.ndarray, box, corridor_extra: int,
                seed_t: float = SEED_T, lip_box=None):
    """Binary damage mask inside the ROI corridor. Returns None fast-path if clean.
    The component KEEP-rule always tests against the OBJECT box (so mouth-only
    diffs are dropped), but when the object is interacting with the lips the
    corridor bounds widen to their union — a merged cup-mouth blob survives whole."""
    H, W = orig.shape[:2]
    x, y, w, h = (int(v) for v in box)
    obx, oby, obw, obh = x, y, w, h              # keep-rule anchor: the object itself
    if lip_box is not None:
        lx, ly, lw_, lh_ = (int(v) for v in lip_box)
        interacting = not (x > lx + lw_ + 60 or lx > x + w + 60 or
                           y > ly + lh_ + 60 or ly > y + h + 60)
        if interacting:
            x0u, y0u = min(x, lx), min(y, ly)
            x1u, y1u = max(x + w, lx + lw_), max(y + h, ly + lh_)
            x, y, w, h = x0u, y0u, x1u - x0u, y1u - y0u
    pad = CORRIDOR + corridor_extra
    rx0, ry0 = max(0, x - pad), max(0, y - pad)
    rx1, ry1 = min(W, x + w + pad), min(H, y + h + pad)
    if rx1 - rx0 < 8 or ry1 - ry0 < 8:
        return None
    o = cv2.GaussianBlur(orig[ry0:ry1, rx0:rx1], (0, 0), 2)
    d = cv2.GaussianBlur(dub[ry0:ry1, rx0:rx1], (0, 0), 2)
    diff = np.abs(o.astype(np.int16) - d.astype(np.int16)).max(axis=2).astype(np.float32)
    seed = (diff > seed_t).astype(np.uint8)
    if not seed.any():
        return None
    # hysteresis: grow seed into connected pixels above GROW_T
    grow = (diff > GROW_T).astype(np.uint8)
    k3 = np.ones((3, 3), np.uint8)
    m = seed
    for _ in range(12):
        m2 = cv2.dilate(m, k3) & grow
        if (m2 == m).all():
            break
        m = m2
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k3)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
    # keep components that genuinely touch the (dilated) OBJECT box — never the
    # widened corridor, so a talking mouth alone can't sneak in
    roi = np.zeros_like(m)
    gx0 = max(0, obx - ROI_GATE_DIL - rx0); gy0 = max(0, oby - ROI_GATE_DIL - ry0)
    gx1 = min(rx1, obx + obw + ROI_GATE_DIL) - rx0; gy1 = min(ry1, oby + obh + ROI_GATE_DIL) - ry0
    roi[gy0:gy1, gx0:gx1] = 1
    n_lbl, lbl, stats, _ = cv2.connectedComponentsWithStats(m)
    keep = np.zeros_like(m)
    for k in range(1, n_lbl):
        comp = lbl == k
        ov = int((comp & (roi > 0)).sum())
        if ov >= max(30, 0.2 * stats[k, cv2.CC_STAT_AREA]):
            keep[comp] = 1
    if not keep.any():
        return None
    keep = cv2.dilate(keep, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (MASK_DIL * 2 + 1,) * 2))
    full = np.zeros((H, W), np.uint8)
    full[ry0:ry1, rx0:rx1] = keep
    return full


def lip_ellipse_mask(shape, box) -> np.ndarray:
    m = np.zeros(shape[:2], np.uint8)
    x, y, w, h = (int(v) for v in box)
    if w > 4 and h > 4:
        cv2.ellipse(m, (x + w // 2, y + h // 2), (int(w * 0.65), int(h * 0.65)), 0, 0, 360, 1, -1)
    return m


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--rois", required=True, help="JSON file with per-sample obj/lips boxes")
    ap.add_argument("--thresh", type=float, default=SEED_T)
    ap.add_argument("--color-fix", action="store_true")
    ap.add_argument("--preview-at", type=float, default=None,
                    help="seconds (or -1 = worst damage frame): write strip + exit")
    a = ap.parse_args()

    work = Path(a.work).resolve()
    final = work / "final.mp4"
    src_txt = work / "source.txt"
    if not final.is_file() or not src_txt.is_file():
        die("workdir needs final.mp4 + source.txt")
    source = Path(src_txt.read_text(encoding="utf-8").strip())
    if not source.is_file():
        die(f"source video missing: {source}")

    pf, ps = probe(final), probe(source)
    W, H = pf["w"], pf["h"]

    print("=== stage: align", flush=True)
    lw = 96 if W >= H else max(16, round(96 * W / H))
    lh = 96 if H > W else max(16, round(96 * H / W))
    lw, lh = lw - lw % 2, lh - lh % 2
    low_src = decode_lowres(source, pf["fps_str"], lw, lh)
    low_fin = decode_lowres(final, pf["fps_str"], lw, lh)
    n_fin = low_fin.shape[0]
    keep_all = np.ones(lh * lw, bool)
    mapping, info = fit_mapping(low_src, low_fin, keep_all)
    print(f"alignment: {json.dumps(info)}", flush=True)
    if info.get("model") not in ("identity", "offset") or abs(info.get("b", 0)) > 1.5:
        print("WARNING: alignment is not near-identity — object repair assumes same-take footage", flush=True)

    samples, _ = load_samples(Path(a.rois), n_fin)
    all_samples = json.loads(Path(a.rois).read_text(encoding="utf-8"))["samples"]
    obj_boxes_i = interp_boxes([s for s in all_samples if s.get("obj")], "obj", n_fin)
    lips_available = any(s.get("lips") for s in all_samples)
    lip_boxes = interp_boxes([s for s in all_samples if s.get("lips")], "lips", n_fin) \
        if lips_available else None
    del low_fin

    print("=== stage: track", flush=True)
    boxes, low_score, low_half = track_object(source, pf, samples, n_fin, "obj")
    # carry interpolated w/h (NCC refines position only)
    boxes[:, 2:] = obj_boxes_i[:, 2:]
    print(f"object tracked; {len(low_score)} low-confidence frame(s) fell back to interpolation", flush=True)
    if lips_available:
        # lips can't be NCC-tracked directly (the dub redraws them every frame) —
        # track the FACE (eyes/nose are stable in both videos) and carry the lips
        # as the same relative offset inside the tracked face
        face_samples = []
        for s in all_samples:
            lb = s.get("lips")
            if not lb:
                continue
            fw, fh = lb["w"] * 2.4, lb["h"] * 2.8
            face_samples.append({"j": s["j"], "face": {
                "x": lb["x"] + lb["w"] / 2 - fw / 2,
                "y": lb["y"] + lb["h"] / 2 - fh * 0.62,   # face extends mostly above the lips
                "w": fw, "h": fh}})
        faces, face_low, _ = track_object(source, pf, face_samples, n_fin, "face", low_half)
        face_i = interp_boxes(face_samples, "face", n_fin)
        lip_boxes = lip_boxes.copy()
        lip_boxes[:, 0] += faces[:, 0] - face_i[:, 0]   # shift lips by the face's
        lip_boxes[:, 1] += faces[:, 1] - face_i[:, 1]   # tracked-vs-interpolated delta
        print(f"face tracked for lip placement; {len(face_low)} low-confidence frame(s)", flush=True)
    del low_src, low_half

    # mouth-protection gate is computed per frame DURING compositing, from the
    # damage mask itself: it opens when the cup-connected damage covers a large
    # share of the lip ellipse (i.e. the AI genuinely fought the object at the
    # mouth). Box-IoU gating proved too blunt — partial box overlap missed the
    # forced-open-mouth case while the object clearly occluded the lips.
    gate_state = {"open": False, "hold": 0, "opened_frames": 0}

    if a.preview_at is not None:
        _preview(a, work, source, final, pf, mapping, boxes, lip_boxes, low_score, n_fin)
        return

    print("=== stage: composite", flush=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = work / f"repair-object-{stamp}.mp4"
    fsize = W * H * 3

    src_reader = open_reader(source, pf["fps_str"], W, H)
    fin_reader = open_reader(final, pf["fps_str"], W, H)

    writer = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
         "-r", pf["fps_str"], "-i", "pipe:0",
         "-i", str(final),
         "-map", "0:v:0", "-map", "1:a:0?",
         "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p",
         "-frames:v", str(n_fin), "-c:a", "copy",
         str(out_path)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10 ** 7)

    # streaming with ±TEMPORAL look-ahead: deque holds (dub, orig, binmask, j)
    ring: deque = deque()
    touched, mask_px = 0, 0
    src_idx, src_frame = -1, None

    def read_pair(j):
        nonlocal src_idx, src_frame
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
        return fin_frame, src_frame

    def emit(center):
        nonlocal touched, mask_px
        j = center[3]
        merged = None
        for (_, _, bm, jj) in ring:
            if abs(jj - j) <= TEMPORAL and bm is not None:
                merged = bm if merged is None else (merged | bm)
        dub_f, orig_f = center[0], center[1]
        out = dub_f
        if merged is not None and orig_f is not None:
            m = merged.copy()
            if lip_boxes is not None:
                lm = lip_ellipse_mask((H, W), lip_boxes[j])
                lip_area = max(1, int(lm.sum()))
                cover = int((m & lm).sum()) / lip_area
                if gate_state["open"]:
                    gate_state["hold"] += 1
                    if cover < GATE_CLOSE and gate_state["hold"] >= GATE_HOLD:
                        gate_state["open"] = False
                elif cover > GATE_OPEN:
                    gate_state["open"], gate_state["hold"] = True, 0
                if gate_state["open"]:
                    gate_state["opened_frames"] += 1
                else:
                    m[lm > 0] = 0        # dub mouth is untouchable
            if m.any():
                alpha = cv2.GaussianBlur(m.astype(np.float32), (0, 0), FEATHER_S)[:, :, None]
                np.clip(alpha, 0, 1, out=alpha)
                o = orig_f.astype(np.float32)
                if a.color_fix:
                    o += cv2.GaussianBlur(dub_f.astype(np.float32), (0, 0), 20) \
                         - cv2.GaussianBlur(o, (0, 0), 20)
                out = np.clip(dub_f.astype(np.float32) * (1 - alpha) + o * alpha,
                              0, 255).astype(np.uint8)
                touched += 1
                mask_px += int(merged.sum())
        writer.stdin.write(np.ascontiguousarray(out).tobytes())
        if j % 25 == 0:
            pct = 25 + int(73 * j / max(1, n_fin))
            print(f"{pct}%| composite {j}/{n_fin}", flush=True)

    try:
        for j in range(n_fin):
            dub_f, orig_f = read_pair(j)
            extra = 20 if j in low_score else 0
            lb = lip_boxes[j] if lip_boxes is not None else None
            bm = damage_mask(orig_f, dub_f, boxes[j], extra, a.thresh, lb) if orig_f is not None else None
            ring.append((dub_f.copy(), None if orig_f is None else orig_f.copy(), bm, j))
            if len(ring) > 2 * TEMPORAL + 1:
                ring.popleft()
            if j >= TEMPORAL:
                emit(ring[len(ring) - 1 - TEMPORAL])
        for k in range(TEMPORAL):
            emit(ring[len(ring) - TEMPORAL + k])
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

    # hard guarantee: output has EXACTLY the dub's frame count
    chk = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(out_path)],
        capture_output=True, text=True)
    n_out = int((chk.stdout or "0").strip() or 0)
    if n_out != n_fin:
        out_path.unlink(missing_ok=True)
        die(f"output frame count {n_out} != dub {n_fin} — take deleted, not delivered broken")

    report = {"alignment": info, "frames": n_fin, "frames_touched": touched,
              "mean_mask_px": round(mask_px / max(1, touched)),
              "low_confidence_track_frames": sorted(low_score)[:40],
              "gate_open_frames": gate_state["opened_frames"], "thresh": a.thresh,
              "created": time.time()}
    (work / "repair-object-report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")

    versions_file = work / "versions.json"
    try:
        versions = json.loads(versions_file.read_text(encoding="utf-8")) if versions_file.is_file() else {}
    except Exception:
        versions = {}
    versions[out_path.name] = {"created": time.time(), "repair": "object"}
    versions_file.write_text(json.dumps(versions, indent=1), encoding="utf-8")

    print(f"100%| composite {n_fin}/{n_fin}", flush=True)
    print(f"restored the object on {touched} frame(s); the dub (and its lip-sync) is untouched everywhere else", flush=True)
    print(f"RESULT: output/script-swap/{work.name}/{out_path.name}", flush=True)


def _preview(a, work, source, final, pf, mapping, boxes, lip_boxes, low_score, n_fin):
    """Single-frame strip: orig | dub | repaired | mask heatmap."""
    W, H = pf["w"], pf["h"]
    if a.preview_at is not None and a.preview_at >= 0:
        j = min(n_fin - 1, int(round(a.preview_at * pf["fps"])))
    else:
        # worst-damage frame inside the ROI corridor: scan a coarse sample
        best_j, best_v = 0, -1.0
        for j in range(0, n_fin, max(1, n_fin // 80)):
            of = decode_one(source, int(mapping[j]) / pf["fps"], W, H)
            df = decode_one(final, j / pf["fps"], W, H)
            if of is None or df is None:
                continue
            bm = damage_mask(of, df, boxes[j], 0, a.thresh,
                             lip_boxes[j] if lip_boxes is not None else None)
            v = float(bm.sum()) if bm is not None else 0.0
            if v > best_v:
                best_j, best_v = j, v
        j = best_j
    of = decode_one(source, int(mapping[j]) / pf["fps"], W, H)
    df = decode_one(final, j / pf["fps"], W, H)
    if of is None or df is None:
        die("could not decode preview frames")
    bm = damage_mask(of, df, boxes[j], 20 if j in low_score else 0, a.thresh,
                     lip_boxes[j] if lip_boxes is not None else None)
    rep = df.copy()
    heat = np.zeros((H, W), np.uint8)
    gate_open = False
    if bm is not None:
        m = bm.copy()
        if lip_boxes is not None:
            lm = lip_ellipse_mask((H, W), lip_boxes[j])
            cover = int((m & lm).sum()) / max(1, int(lm.sum()))
            gate_open = cover > GATE_OPEN
            if not gate_open:
                m[lm > 0] = 0
        alpha = cv2.GaussianBlur(m.astype(np.float32), (0, 0), FEATHER_S)[:, :, None]
        rep = np.clip(df.astype(np.float32) * (1 - alpha) + of.astype(np.float32) * alpha,
                      0, 255).astype(np.uint8)
        heat = (m * 255)
    hm = cv2.applyColorMap(heat, cv2.COLORMAP_JET)
    x, y, w, h = (int(v) for v in boxes[j])
    cv2.rectangle(hm, (x, y), (x + w, y + h), (255, 255, 255), 2)
    ph = 480
    panels = []
    for img, label in ((of, "ORIGINAL"), (df, "DUB (damaged)"), (rep, "REPAIRED"),
                       (hm, f"MASK f{j} (box = tracked object)")):
        p = cv2.resize(img, (round(W * ph / H), ph))
        cv2.putText(p, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 0, 0), 4)
        cv2.putText(p, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 1)
        panels.append(p)
    out = work / "preview-object.jpg"
    cv2.imwrite(str(out), np.hstack(panels), [cv2.IMWRITE_JPEG_QUALITY, 88])
    print(f"ALIGN: {json.dumps({'frame': int(j), 'gate_open': gate_open})}", flush=True)
    print(f"PREVIEW: output/script-swap/{work.name}/preview-object.jpg", flush=True)


if __name__ == "__main__":
    main()
