#!/usr/bin/env python3
"""Word-level transcript for the timeline editor's edit-by-transcript.

    python seq_transcribe.py --src <video> --out <words.json> [--model small]

Runs under the whisper venv (course_pipeline/.venv — faster-whisper lives there).
Writes {"language": .., "words": [{"w","s","e"}, ...]} with times in SOURCE
seconds; the server maps them onto the timeline per clip.

Hardening carried over from caption.py's scars: faster-whisper's aligner can
IndexError on odd audio under vad_filter — retry with vad off, then fall back
to segment-level timing (words spread evenly) rather than dying.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def collect(segments) -> list[dict]:
    words = []
    for seg in segments:                       # iterating runs the model
        if seg.words:
            for w in seg.words:
                t = (w.word or "").strip()
                if t:
                    words.append({"w": t, "s": round(w.start, 3), "e": round(w.end, 3)})
        elif seg.text.strip():                 # segment-level fallback: spread evenly
            toks = seg.text.split()
            step = (seg.end - seg.start) / max(1, len(toks))
            for i, t in enumerate(toks):
                words.append({"w": t, "s": round(seg.start + i * step, 3),
                              "e": round(seg.start + (i + 1) * step, 3)})
        print(f"  … {seg.end:6.1f}s", flush=True)
    return words


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="small")
    # CPU by default: this box has no cublas64_12/cudnn for ctranslate2, and a
    # CUDA failure surfaces mid-transcribe as a hard crash, not at model load.
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    a = ap.parse_args()

    src = Path(a.src)
    if not src.is_file():
        print(f"ERROR: no such file {src}", flush=True)
        return 1

    from faster_whisper import WhisperModel

    try:
        model = WhisperModel(a.model, device=a.device,
                             compute_type="int8" if a.device == "cpu" else "int8_float16")
        print(f"model {a.model} on {a.device}", flush=True)
    except Exception as exc:
        print(f"ERROR: could not load whisper model: {exc}", flush=True)
        return 1

    words, lang = None, ""
    for vad, wordts in ((True, True), (False, True), (False, False)):
        try:
            segments, info = model.transcribe(str(src), word_timestamps=wordts,
                                              vad_filter=vad)
            words = collect(segments)
            lang = info.language or ""
            break
        except Exception as exc:
            print(f"[warn] transcribe vad={vad} words={wordts} failed: {exc}", flush=True)
    if words is None:
        print("ERROR: transcription failed entirely", flush=True)
        return 1

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"language": lang, "words": words}, ensure_ascii=False),
                   encoding="utf-8")
    print(f"OK {len(words)} words ({lang}) -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
