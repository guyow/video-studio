#!/usr/bin/env python3
"""Segment Lipsync — re-run a PAID fal.ai lip-sync on marked time ranges only.

The repair loop used to force a choice between a free-but-soft local Wav2Lip
pass or re-billing the WHOLE video on fal (a 2-range fix on a 289s video cost
the full $14+ again on sync-v2). This engine slices just the marked ranges out
of the dub (+ a little padding), lip-syncs each slice on fal — billing only the
slice seconds — and composites the synced frames back over the dub with a short
crossfade at each boundary. Audio is the dub's own VO, untouched.

Output is a new versioned take (final.mp4 is never modified), same contract as
frame_swap.py / dubsync_repair.py.

  segment_lipsync.py --work <dub workdir> --ranges "3.2-4.85,12.0-12.4" \
      --tier latentsync --rate 0.005 [--pad 0.35] [--fade 4] \
      [--env-file <autoVSL>/.env] [--fake-url <local mp4>]

--fake-url skips fal entirely and "returns" the given clip for every range —
the free dry-run that exercises slice -> composite -> versions.json end to end.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from visual_repair import die, probe, open_reader, _read_exact          # noqa: E402
from t2v_fal import subscribe_deadline, load_env                        # noqa: E402

# tier -> fal endpoint (mirror of scripts/script-swap.py LIPSYNC_ENDPOINTS —
# keep the two in sync when adding tiers)
LIPSYNC_ENDPOINTS = {
    "sync3": "fal-ai/sync-lipsync/v3",
    "pro": "fal-ai/sync-lipsync/v2/pro",
    "standard": "fal-ai/sync-lipsync/v2",
    "hummingbird": "fal-ai/tavus/hummingbird-lipsync/v0",   # bills min 15s/call
    "veed": "veed/lipsync",
    "latentsync": "fal-ai/latentsync",
    "musetalk": "fal-ai/musetalk",
}


def ff(args: list[str], label: str) -> None:
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        die(f"{label} failed: {(r.stderr or '')[-300:]}")


def parse_ranges(spec: str, dur: float) -> list[tuple[float, float]]:
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
        out.append((max(0.0, a), min(dur, b)))
    if not out:
        die("no valid ranges given")
    out.sort()
    return out


def pad_and_merge(ranges: list[tuple[float, float]], pad: float,
                  dur: float) -> list[tuple[float, float]]:
    """Pad each range (blend room for the crossfade) and merge overlaps so a
    frame is never billed or composited twice."""
    padded = [(max(0.0, a - pad), min(dur, b + pad)) for a, b in ranges]
    merged: list[list[float]] = []
    for a, b in padded:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def lipsync_segment(idx: int, t0: float, t1: float, final: Path, vo: Path,
                    seg_dir: Path, tier: str, fake_url: str | None) -> Path:
    """Slice [t0,t1] of video+VO, lip-sync it on fal, return the synced clip."""
    dur = t1 - t0
    face = seg_dir / f"seg{idx}-face.mp4"
    audio = seg_dir / f"seg{idx}-audio.wav"
    synced = seg_dir / f"seg{idx}-synced.mp4"
    # re-encoded cuts: -ss before -i is fast but keyframe-snapped; accurate cut
    # matters here because the frames must line back up with the dub
    ff(["-ss", f"{t0:.3f}", "-i", str(final), "-t", f"{dur:.3f}",
        "-an", "-c:v", "libx264", "-crf", "16", "-preset", "veryfast", str(face)],
       f"slice video {t0:.2f}-{t1:.2f}")
    ff(["-ss", f"{t0:.3f}", "-i", str(vo), "-t", f"{dur:.3f}",
        "-ac", "1", "-ar", "44100", str(audio)],
       f"slice audio {t0:.2f}-{t1:.2f}")

    if fake_url:                                     # free dry-run
        ff(["-i", str(fake_url), "-t", f"{dur:.3f}", "-an",
            "-c:v", "libx264", "-crf", "16", "-preset", "veryfast", str(synced)],
           "fake-url stand-in")
        return synced

    import fal_client
    import httpx

    print(f"  uploading segment {idx + 1} ({dur:.1f}s) ...", flush=True)
    video_url = fal_client.upload_file(str(face))
    audio_url = fal_client.upload_file(str(audio))
    if tier == "musetalk":                           # musetalk names the field differently
        arguments = {"source_video_url": video_url, "audio_url": audio_url}
    else:
        arguments = {"video_url": video_url, "audio_url": audio_url}
        if tier in ("pro", "standard", "sync3"):
            arguments["sync_mode"] = "cut_off"       # audio == video length here, harmless
    print(f"  calling {LIPSYNC_ENDPOINTS[tier]} ...", flush=True)
    try:
        res = subscribe_deadline(fal_client, LIPSYNC_ENDPOINTS[tier], arguments, 900)
    except Exception as e:                           # noqa: BLE001
        die(f"fal.ai lip-sync failed on segment {idx + 1}: {str(e)[:300]}")
    v = res.get("video") or {}
    url = v.get("url") if isinstance(v, dict) else v
    if not url:
        die(f"no video in lipsync response: {str(res)[:180]}")
    tmp = synced.with_suffix(".part.mp4")
    with httpx.Client(follow_redirects=True, timeout=300) as c:
        r = c.get(url)
        r.raise_for_status()
        tmp.write_bytes(r.content)
    tmp.replace(synced)
    return synced


def open_seg_reader(path: Path, fps_str: str, w: int, h: int,
                    want_dur: float) -> subprocess.Popen:
    """Raw BGR reader normalized to the dub's fps/size; if fal returned a
    slightly different length, retime so the frames line back up."""
    vf = f"fps={fps_str},scale={w}:{h}"
    try:
        ds = probe(path)["dur"]
    except SystemExit:
        ds = 0.0
    if ds and abs(ds - want_dur) > 0.15:
        print(f"  (synced clip is {ds:.2f}s vs {want_dur:.2f}s wanted — retiming)", flush=True)
        vf = f"setpts=PTS*{want_dur / ds:.6f},{vf}"
    return subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-vf", vf, "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"],
        stdout=subprocess.PIPE, bufsize=10 ** 7)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--ranges", required=True, help='seconds: "start-end,start-end,..."')
    ap.add_argument("--tier", required=True, choices=sorted(LIPSYNC_ENDPOINTS))
    ap.add_argument("--rate", type=float, default=0.0, help="USD per billed second (for SPENT)")
    ap.add_argument("--min-bill", type=float, default=0.0,
                    help="minimum billed seconds per fal call (hummingbird: 15)")
    ap.add_argument("--pad", type=float, default=0.35, help="context seconds around each range")
    ap.add_argument("--fade", type=int, default=4, help="crossfade frames at each edge")
    ap.add_argument("--env-file", dest="env_file")
    ap.add_argument("--fake-url", dest="fake_url", help="dev: local mp4 instead of a fal call")
    a = ap.parse_args()

    work = Path(a.work).resolve()
    final = work / "final.mp4"
    vo = work / "new-vo.mp3"
    if not final.is_file():
        die("workdir needs final.mp4 — dub first")
    if not vo.is_file():
        die("workdir needs new-vo.mp3 (the dub's VO) — re-run the dub")

    pf = probe(final)
    W, H, fps = pf["w"], pf["h"], pf["fps"]
    dur = pf["dur"]

    ranges = pad_and_merge(parse_ranges(a.ranges, dur), a.pad, dur)
    bill_secs = sum(max(b - t, a.min_bill) for t, b in ranges)
    print(f"re-lipsync {len(ranges)} segment(s) on {a.tier}: "
          + ", ".join(f"{t:.2f}s-{b:.2f}s" for t, b in ranges)
          + f" — ~{bill_secs:.1f}s billed instead of the full {dur:.0f}s video", flush=True)

    spent = 0.0
    if not a.fake_url:
        load_env(Path(a.env_file) if a.env_file else None)
        if not os.environ.get("FAL_KEY"):
            die("FAL_KEY not set — add it to autoVSL/.env")
        # free probe before any paid call (same guard as every other engine)
        import fal_client
        try:
            fal_client.upload(b"ok", "text/plain")
        except Exception as exc:                     # noqa: BLE001
            msg = str(exc).lower()
            if any(w in msg for w in ("401", "402", "403", "locked", "balance",
                                      "exhaust", "unauthor")):
                die(f"STOPPED BEFORE SPENDING — fal.ai account problem: {exc}")

    seg_dir = work / "segsync"
    seg_dir.mkdir(exist_ok=True)
    synced_clips: list[tuple[float, float, Path]] = []
    try:
        for i, (t0, t1) in enumerate(ranges):
            print(f"\n=== segment {i + 1}/{len(ranges)}: {t0:.2f}s-{t1:.2f}s ===", flush=True)
            clip = lipsync_segment(i, t0, t1, final, vo, seg_dir, a.tier, a.fake_url)
            spent += max(t1 - t0, a.min_bill) * a.rate
            synced_clips.append((t0, t1, clip))
    finally:
        print("", flush=True)
        print("SPENT: " + json.dumps({"usd": round(spent, 3), "made": len(synced_clips),
                                      "failed": len(ranges) - len(synced_clips)}), flush=True)

    # ---- composite the synced segments back over the dub -----------------------
    print("=== stage: composite ===", flush=True)
    n_fin = int(round(dur * fps)) if not pf["nb"] else pf["nb"]
    frame_ranges = []                                # (ja, jb, reader) per segment
    for t0, t1, clip in synced_clips:
        ja = max(0, int(round(t0 * fps)))
        jb = min(n_fin - 1, int(round(t1 * fps)))
        frame_ranges.append([ja, jb, open_seg_reader(clip, pf["fps_str"], W, H, t1 - t0),
                             None])                  # [ja, jb, reader, last_frame]

    fade = max(1, a.fade)

    def alpha_for(j: int, ja: int, jb: int) -> float:
        return min(1.0, (j - ja + 1) / fade, (jb - j + 1) / fade)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = work / f"repair-segsync-{stamp}.mp4"
    fsize = W * H * 3
    writer = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
         "-r", pf["fps_str"], "-i", "pipe:0",
         "-i", str(final),
         "-map", "0:v:0", "-map", "1:a:0?",
         "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p",
         "-c:a", "copy", str(out_path)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10 ** 7)
    fin_reader = open_reader(final, pf["fps_str"], W, H)

    replaced = 0
    n_out = 0
    try:
        j = 0
        while True:
            raw_f = _read_exact(fin_reader.stdout, fsize)
            if raw_f is None:
                break                                # end of the dub
            fin_frame = np.frombuffer(raw_f, np.uint8).reshape(H, W, 3)
            out = fin_frame
            for seg in frame_ranges:
                ja, jb, reader, last = seg
                if ja <= j <= jb:
                    raw_s = _read_exact(reader.stdout, fsize)
                    if raw_s is not None:
                        seg[3] = last = np.frombuffer(raw_s, np.uint8).reshape(H, W, 3)
                    if last is not None:             # hold last frame if the clip ran short
                        al = alpha_for(j, ja, jb)
                        replaced += 1
                        if al >= 1.0:
                            out = last
                        else:
                            out = np.clip(fin_frame.astype(np.float32) * (1 - al)
                                          + last.astype(np.float32) * al, 0, 255).astype(np.uint8)
                    break
            writer.stdin.write(np.ascontiguousarray(out).tobytes())
            n_out += 1
            if j % 100 == 0:
                print(f"{int(90 * j / max(1, n_fin))}%| composite {j}", flush=True)
            j += 1
        writer.stdin.close()
        rc = writer.wait()
        if rc != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
            err = (writer.stderr.read() or b"").decode("utf-8", "replace")[-400:]
            out_path.unlink(missing_ok=True)
            die("encode failed: " + err)
    finally:
        for seg in frame_ranges:
            try:
                seg[2].kill()
            except Exception:                        # noqa: BLE001
                pass
        try:
            fin_reader.kill()
        except Exception:                            # noqa: BLE001
            pass

    # frame-count guard: same contract as frame_swap — never ship a broken take
    chk = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(out_path)],
        capture_output=True, text=True)
    n_chk = int((chk.stdout or "0").strip() or 0)
    if abs(n_chk - n_out) > 1:
        out_path.unlink(missing_ok=True)
        die(f"output frame count {n_chk} != composited {n_out} — take deleted, not delivered broken")

    versions_file = work / "versions.json"
    try:
        versions = json.loads(versions_file.read_text(encoding="utf-8")) if versions_file.is_file() else {}
    except Exception:                                # noqa: BLE001
        versions = {}
    if not isinstance(versions, dict):               # duo_run once wrote a list here
        versions = {}
    versions[out_path.name] = {"created": time.time(), "repair": "relipsync-segment",
                               "tier": a.tier, "ranges": a.ranges}
    versions_file.write_text(json.dumps(versions, indent=1), encoding="utf-8")

    print(f"replaced {replaced} frame(s) across {len(synced_clips)} segment(s); "
          "everything else is the dub untouched", flush=True)
    print(f"RESULT: output/script-swap/{work.name}/{out_path.name}", flush=True)


if __name__ == "__main__":
    main()
