#!/usr/bin/env python3
"""Frame Swap — replace user-marked time ranges of the dub with the ORIGINAL frames.

The most direct repair: the user watches the dub, marks exactly WHEN the AI damage
happens (start/end, one or more ranges), and those frames are exchanged 1:1 with the
aligned original video. A short crossfade (default 4 frames) at each boundary hides
the switch. The dub's audio is kept untouched; everything outside the marked ranges
is the dub, byte-for-byte.

Usage:
  frame_swap.py --work <dub workdir> --ranges "3.20-4.85,12.00-12.40" [--fade 4]
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from visual_repair import die, probe, decode_lowres, open_reader, _read_exact, fit_mapping


def parse_ranges(spec: str, fps: float, n_fin: int):
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            a, b = (float(x) for x in part.split("-"))
        except ValueError:
            die(f"bad range '{part}' — expected start-end in seconds, e.g. 3.2-4.85")
        if b <= a:
            die(f"range '{part}': end must be after start")
        ja = max(0, int(round(a * fps)))
        jb = min(n_fin - 1, int(round(b * fps)))
        if ja <= jb:
            out.append((ja, jb))
    if not out:
        die("no valid ranges given")
    out.sort()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--ranges", required=True, help='seconds: "start-end,start-end,..."')
    ap.add_argument("--fade", type=int, default=4, help="crossfade frames at each edge")
    a = ap.parse_args()

    work = Path(a.work).resolve()
    final = work / "final.mp4"
    src_txt = work / "source.txt"
    if not final.is_file() or not src_txt.is_file():
        die("workdir needs final.mp4 + source.txt")
    source = Path(src_txt.read_text(encoding="utf-8").strip())
    if not source.is_file():
        die(f"source video missing: {source}")

    pf = probe(final)
    W, H = pf["w"], pf["h"]

    print("=== stage: align", flush=True)
    lw = 96 if W >= H else max(16, round(96 * W / H))
    lh = 96 if H > W else max(16, round(96 * H / W))
    lw, lh = lw - lw % 2, lh - lh % 2
    low_src = decode_lowres(source, pf["fps_str"], lw, lh)
    low_fin = decode_lowres(final, pf["fps_str"], lw, lh)
    n_fin = low_fin.shape[0]
    mapping, info = fit_mapping(low_src, low_fin, np.ones(lh * lw, bool))
    print(f"alignment: {json.dumps(info)}", flush=True)
    del low_src, low_fin

    ranges = parse_ranges(a.ranges, pf["fps"], n_fin)
    fade = max(1, a.fade)
    print(f"swapping {len(ranges)} range(s): "
          + ", ".join(f"frames {ja}-{jb} ({ja/pf['fps']:.2f}s-{jb/pf['fps']:.2f}s)"
                      for ja, jb in ranges), flush=True)

    def alpha_for(j: int) -> float:
        """1.0 deep inside a marked range, ramping over `fade` frames at each edge."""
        best = 0.0
        for ja, jb in ranges:
            if ja <= j <= jb:
                best = max(best, min(1.0, (j - ja + 1) / fade, (jb - j + 1) / fade))
        return best

    print("=== stage: swap", flush=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = work / f"repair-swap-{stamp}.mp4"
    fsize = W * H * 3

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

    src_reader = open_reader(source, pf["fps_str"], W, H)
    fin_reader = open_reader(final, pf["fps_str"], W, H)
    src_idx, src_frame = -1, None
    swapped = 0
    try:
        for j in range(n_fin):
            raw_f = _read_exact(fin_reader.stdout, fsize)
            if raw_f is None:
                die(f"dub reader ended early at frame {j}/{n_fin} — aborting")
            fin_frame = np.frombuffer(raw_f, np.uint8).reshape(H, W, 3)
            target = int(mapping[j])
            while src_idx < target:
                raw_s = _read_exact(src_reader.stdout, fsize)
                if raw_s is None:
                    break
                src_idx += 1
                src_frame = np.frombuffer(raw_s, np.uint8).reshape(H, W, 3)
            al = alpha_for(j)
            if al > 0 and src_frame is not None:
                swapped += 1
                if al >= 1.0:
                    out = src_frame
                else:
                    out = np.clip(fin_frame.astype(np.float32) * (1 - al)
                                  + src_frame.astype(np.float32) * al, 0, 255).astype(np.uint8)
            else:
                out = fin_frame
            writer.stdin.write(np.ascontiguousarray(out).tobytes())
            if j % 50 == 0:
                pct = 10 + int(88 * j / max(1, n_fin))
                print(f"{pct}%| swap {j}/{n_fin}", flush=True)
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

    chk = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(out_path)],
        capture_output=True, text=True)
    n_out = int((chk.stdout or "0").strip() or 0)
    if n_out != n_fin:
        out_path.unlink(missing_ok=True)
        die(f"output frame count {n_out} != dub {n_fin} — take deleted, not delivered broken")

    versions_file = work / "versions.json"
    try:
        versions = json.loads(versions_file.read_text(encoding="utf-8")) if versions_file.is_file() else {}
    except Exception:
        versions = {}
    versions[out_path.name] = {"created": time.time(), "repair": "swap",
                               "ranges": a.ranges}
    versions_file.write_text(json.dumps(versions, indent=1), encoding="utf-8")

    print(f"100%| swap {n_fin}/{n_fin}", flush=True)
    print(f"swapped {swapped} frame(s) from the original across {len(ranges)} range(s); "
          "everything else is the dub untouched", flush=True)
    print(f"RESULT: output/script-swap/{work.name}/{out_path.name}", flush=True)


if __name__ == "__main__":
    main()
