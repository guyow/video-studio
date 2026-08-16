#!/usr/bin/env python3
"""Dub QA verdict — the studio-side port of the VPS qa_vsl checks.

Mechanical checks on a finished dub: duration 30-120s, portrait 9:16, audio
present, talking pace 100-200 wpm, banned words (Meta-safe + offer-doc list).
Runs under the whisper venv. Reuses caption.py's caption-words.json cache when
it's fresh so the chained caption -> qa run never transcribes twice.

Verdict -> qa.json in the workdir (or next to --video). A FAIL is a report,
not a crash: the exit code stays 0 unless the checks themselves can't run.

Usage:  qa_vsl.py --work <output/script-swap/<stem>>   (dub workdir)
        qa_vsl.py --video <path.mp4> [--out <qa.json>] (any video)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

DUR_MIN, DUR_MAX = 30.0, 120.0
WPM_MIN, WPM_MAX = 100.0, 200.0
PORTRAIT = 9 / 16          # target w/h
ASPECT_TOL = 0.04

# hard fails: the offer-doc list + the Meta-restricted product words the
# meta-safe playbook exists to keep out of spoken scripts
BANNED = ("wellness", "journey", "holistic",
          "psychedelic", "psychedelics", "psilocybin", "shroom", "shrooms",
          "magic mushroom", "magic mushrooms",
          "microdose", "microdoses", "microdosing")
# ambiguous on their own — flagged as warnings, never fail the take
WARN_WORDS = ("trip", "tripping", "high")


def ffprobe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        sys.exit(f"ffprobe failed on {path.name}: {(r.stderr or '')[:200]}")
    return json.loads(r.stdout or "{}")


def transcribe_words(audio: Path) -> list[dict]:
    """faster-whisper word timing (same cuda-dll + cpu-fallback dance as caption.py)."""
    if sys.platform == "win32":
        import os
        site = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
        for sub in ("cublas", "cudnn"):
            bin_dir = site / sub / "bin"
            if bin_dir.is_dir():
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    from faster_whisper import WhisperModel
    model = None
    for device, compute in (("cuda", "int8_float16"), ("cpu", "int8")):
        try:
            print(f"qa: loading whisper on {device}…", flush=True)
            model = WhisperModel("distil-large-v3", device=device, compute_type=compute)
            break
        except Exception as e:  # noqa: BLE001
            print(f"  {device} unavailable ({str(e)[:120]})")
    if model is None:
        sys.exit("qa: could not load whisper on any device")
    segments, _ = model.transcribe(str(audio), word_timestamps=True,
                                   vad_filter=True, beam_size=5)
    return [{"text": w.word, "start": round(w.start, 3), "end": round(w.end, 3)}
            for seg in segments for w in (seg.words or [])]


def get_words(work: Path | None, video: Path) -> list[dict]:
    if work:
        cache = work / "caption-words.json"
        vo = work / "new-vo.mp3"
        ref = vo if vo.is_file() else video
        if cache.is_file() and cache.stat().st_mtime > ref.stat().st_mtime:
            print("qa: word timing from caption cache")
            return json.loads(cache.read_text(encoding="utf-8"))
        if vo.is_file():
            return transcribe_words(vo)
    return transcribe_words(video)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", help="dub workdir (output/script-swap/<stem>)")
    ap.add_argument("--video", help="check this file instead of the workdir's final")
    ap.add_argument("--out", help="where to write qa.json (default: workdir/qa.json)")
    args = ap.parse_args()

    print("=== stage: qa ===", flush=True)
    work = Path(args.work).resolve() if args.work else None
    if args.video:
        video = Path(args.video).resolve()
    elif work:
        # prefer the deliverable: captioned final when it's current, else final
        cap, fin = work / "final-captioned.mp4", work / "final.mp4"
        video = cap if (cap.is_file() and fin.is_file()
                        and cap.stat().st_mtime >= fin.stat().st_mtime) else fin
    else:
        sys.exit("pass --work or --video")
    if not video.is_file():
        sys.exit(f"video not found: {video}")
    print(f"qa: checking {video.name}")

    info = ffprobe(video)
    fmt = info.get("format") or {}
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    dur = float(fmt.get("duration") or 0)
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)

    checks: list[dict] = []

    def check(key: str, ok: bool, value, fix: str = "", warn: bool = False) -> None:
        checks.append({"key": key, "pass": bool(ok), "value": value,
                       **({"fix": fix} if fix and not ok else {}),
                       **({"warn": True} if warn else {})})
        mark = "PASS" if ok else ("WARN" if warn else "FAIL")
        print(f"QA {key}: {mark} — {value}" + (f"  ({fix})" if fix and not ok else ""),
              flush=True)

    check("duration", DUR_MIN <= dur <= DUR_MAX, f"{dur:.1f}s",
          f"target {DUR_MIN:.0f}-{DUR_MAX:.0f}s — trim or extend the script")
    ratio = (w / h) if h else 0
    check("aspect", abs(ratio - PORTRAIT) <= ASPECT_TOL, f"{w}x{h}",
          "deliverable must be portrait 9:16 — run smart-crop / re-export")
    check("audio", has_audio, "audio stream present" if has_audio else "NO audio stream",
          "the dub lost its audio — re-run the mux")

    words: list[dict] = []
    try:
        words = get_words(work, video)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        check("pace", False, f"transcription failed: {str(e)[:120]}",
              "re-run QA once the GPU is free", warn=True)
    text = "".join(wd.get("text", "") for wd in words)
    if words:
        span = max(0.5, (words[-1].get("end") or dur) - (words[0].get("start") or 0))
        wpm = len(words) / span * 60.0
        check("pace", WPM_MIN <= wpm <= WPM_MAX, f"{wpm:.0f} wpm ({len(words)} words / {span:.0f}s)",
              "slow the script down" if wpm > WPM_MAX else "the delivery drags — tighten the script")

        low = " " + re.sub(r"[^a-z ]", " ", text.lower()) + " "
        hits = sorted({b for b in BANNED
                       if re.search(rf"(?<![a-z]){re.escape(b)}(?![a-z])", low)})
        check("banned-words", not hits,
              ("none found" if not hits else "found: " + ", ".join(hits)),
              "rewrite with the meta-safe wordplay list — these words get the ad rejected")
        warns = sorted({b for b in WARN_WORDS
                        if re.search(rf"(?<![a-z]){re.escape(b)}(?![a-z])", low)})
        if warns:
            check("risky-words", True, "context-check these: " + ", ".join(warns), warn=True)

    failed = [c for c in checks if not c["pass"] and not c.get("warn")]
    verdict = {"pass": not failed, "checks": checks, "video": video.name,
               "duration": round(dur, 1), "size": f"{w}x{h}",
               "transcript": text.strip()[:2000], "checked_at": time.time()}
    out = Path(args.out) if args.out else (work / "qa.json" if work
                                           else video.with_suffix(".qa.json"))
    out.write_text(json.dumps(verdict, indent=1), encoding="utf-8")
    print(f"\nQA: {'PASS' if verdict['pass'] else f'FAIL ({len(failed)} issue(s))'}  -> {out}",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
