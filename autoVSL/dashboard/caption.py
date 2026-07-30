#!/usr/bin/env python3
"""Burn word-timed social captions onto a dubbed video — free and local.

Word timestamps come from LOCAL faster-whisper on the new VO (which speaks the
edited script), so no fal cost. Caption style mirrors scripts/vsl-render.py
(bold white, heavy black outline, lower-center, brand rendered as 'līītt').

Usage: python dashboard/caption.py --name <stem>          # caption a dubbed final
       python dashboard/caption.py --video uploads/x.mp4  # caption an upload from its ORIGINAL audio
--name reads  output/script-swap/<stem>/{final.mp4,new-vo.mp3}
       writes output/script-swap/<stem>/final-captioned.mp4 + Desktop <stem>-ready-captioned.mp4
--video times words from the video's own audio track (no dub needed — the free
       "remove subs → burn fresh subtitles" path); writes output/recaption/<stem>/captioned.mp4
       + Desktop <stem>-recaptioned.mp4
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
READY_DIR = Path.home() / "Desktop" / "liitt testimonial Ready"

# House caption style — pro social captions: Arial Black scaled to the video,
# thick outline + soft shadow, active word popped in liitt gold (karaoke).
WORDS_PER_LINE = 3
FONT = "Arial Black"                 # heavy social-caption face; ships with Windows
HILITE = "&H0042C5F5"                # liitt gold #F5C542 (ASS is &H00BBGGRR)
BRAND = "līītt"
BRAND_ALIASES = {"lit", "litt", "liit", "liitt", "leet", "lift"}


def cap_size(video_h: int) -> int:
    """Caption font size scaled to the video (≈4.2% of height; 1920 → 81px)."""
    return max(36, round(video_h * 0.042))


def _register_cuda_dlls() -> None:
    """Make pip-installed NVIDIA cuBLAS/cuDNN DLLs visible to ctranslate2 (Windows)."""
    if sys.platform != "win32":
        return
    import os
    site = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for sub in ("cublas", "cudnn"):
        bin_dir = site / sub / "bin"
        if bin_dir.is_dir():
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def words_from_vo(vo: Path, work: Path) -> list[dict]:
    cache = work / "caption-words.json"
    if cache.is_file() and cache.stat().st_mtime > vo.stat().st_mtime:
        print("word timing: cached")
        return json.loads(cache.read_text(encoding="utf-8"))

    _register_cuda_dlls()
    from faster_whisper import WhisperModel

    model = None
    for device, compute in (("cuda", "int8_float16"), ("cpu", "int8")):
        try:
            print(f"word timing: loading whisper on {device}…", flush=True)
            model = WhisperModel("distil-large-v3", device=device, compute_type=compute)
            break
        except Exception as e:
            print(f"  {device} unavailable ({str(e)[:120]})")
    if model is None:
        raise RuntimeError("could not load whisper on any device")

    # prime with the made script — the VO speaks exactly this text, so priming
    # pulls ambiguous words toward the script's wording
    script = work / "script-edited.txt"
    prompt = script.read_text(encoding="utf-8", errors="replace")[:800] if script.is_file() else None
    words = []
    for vad in (True, False):
        try:
            segments, _ = model.transcribe(str(vo), word_timestamps=True, vad_filter=vad,
                                           beam_size=5, initial_prompt=prompt)
            words = [{"text": w.word, "start": round(w.start, 3), "end": round(w.end, 3)}
                     for seg in segments for w in (seg.words or [])]
            break
        except IndexError as e:
            # faster-whisper's word aligner crashes on some audio (empty final
            # chunk after VAD); flipping the VAD reshuffles the chunks past it
            print(f"word timing: aligner crashed with vad_filter={vad} ({e}) — retrying", flush=True)
    if not words:
        # the aligner refuses this audio entirely (e.g. XTTS time-stretch artifacts) —
        # segment-level timing never runs the aligner; spread each segment's words evenly
        print("word timing: falling back to segment-level timing", flush=True)
        segments, _ = model.transcribe(str(vo), word_timestamps=False, vad_filter=False,
                                       beam_size=5, initial_prompt=prompt)
        for seg in segments:
            toks = seg.text.split()
            if not toks:
                continue
            step = (seg.end - seg.start) / len(toks)
            words += [{"text": " " + t, "start": round(seg.start + i * step, 3),
                       "end": round(seg.start + (i + 1) * step, 3)}
                      for i, t in enumerate(toks)]
    if not words:
        raise RuntimeError("no word timestamps produced")
    cache.write_text(json.dumps(words), encoding="utf-8")
    print(f"word timing: {len(words)} words")
    return words


def render_word(w: str) -> str:
    core = re.sub(r"[^A-Za-z]", "", w).lower()
    if core in BRAND_ALIASES:
        m = re.match(r"^(\W*)(.*?)(\W*)$", w)
        return (m.group(1) if m else "") + BRAND + (m.group(3) if m else "")
    return w.upper()


def _ass_time(t: float) -> str:
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def band_margin(stem: str, video_h: int) -> int | None:
    """If upload <stem> had subtitles erased, MarginV that puts captions over that band."""
    box_file = ROOT / "uploads" / ".originals" / f"{stem}.box.json"
    if not box_file.is_file():
        return None
    box = json.loads(box_file.read_text(encoding="utf-8"))
    size = cap_size(video_h)
    scale = video_h / box.get("vh", video_h)   # dub output may differ in resolution
    y, h = box["y"] * scale, box["h"] * scale
    margin = int(video_h - (y + h) + max(0, (h - size * 1.4) / 2))
    return max(30, min(margin, video_h - size - 30))


def cleaned_band_margin(name: str, video_h: int) -> int | None:
    """Band margin for a dub work dir (its source upload is recorded in source.txt)."""
    src_file = ROOT / "output" / "script-swap" / name / "source.txt"
    if not src_file.is_file():
        return None
    return band_margin(Path(src_file.read_text(encoding="utf-8").strip()).stem, video_h)


def build_ass(words: list[dict], out_path: Path, video_w: int, video_h: int,
              margin_v: int | None = None) -> None:
    size = cap_size(video_h)
    outline = max(3, round(size / 13))
    shadow = max(2, round(size / 18))
    m_lr = round(video_w * 0.055)
    margin_v = margin_v if margin_v is not None else max(60, round(video_h * 0.12))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{FONT},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,{outline},{shadow},2,{m_lr},{m_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines, group = [], []
    for w in words:
        group.append(w)
        if len(group) >= WORDS_PER_LINE:
            lines.append(group)
            group = []
    if group:
        lines.append(group)
    # one event per active word: the full line each time, the spoken word popped
    # in liitt gold. Windows chain word-start → next-word-start, so no flicker.
    events: list[str] = []
    for g in lines:
        toks = [render_word(w["text"].strip()) for w in g]
        if len(toks) == 1:
            events.append(f"Dialogue: 0,{_ass_time(g[0]['start'])},{_ass_time(g[-1]['end'])},Cap,,0,0,0,,{toks[0]}")
            continue
        for i, w in enumerate(g):
            s = g[0]["start"] if i == 0 else w["start"]
            e = g[-1]["end"] if i == len(g) - 1 else g[i + 1]["start"]
            if e <= s:
                continue
            parts = [("{\\1c" + HILITE + "\\fscx108\\fscy108}" + t + "{\\r}") if j == i else t
                     for j, t in enumerate(toks)]
            events.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Cap,,0,0,0,," + " ".join(parts))
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", help="script-swap work dir name (caption a dubbed final)")
    ap.add_argument("--video", help="caption an uploads video directly — word timing from its ORIGINAL audio")
    args = ap.parse_args()
    if bool(args.name) == bool(args.video):
        sys.exit("pass exactly one of --name or --video")

    if args.video:
        final = Path(args.video).resolve()
        if not final.is_file():
            sys.exit(f"no such video: {final}")
        name = final.stem
        work = ROOT / "output" / "recaption" / name
        work.mkdir(parents=True, exist_ok=True)
        vo = final  # whisper reads the audio track straight out of the video
        out = work / "captioned.mp4"
        deliverable_name = f"{name}-recaptioned.mp4"
        get_margin = band_margin
    else:
        name = args.name
        work = ROOT / "output" / "script-swap" / name
        final = work / "final.mp4"
        vo = work / "new-vo.mp3"
        if not final.is_file():
            sys.exit("no dubbed final.mp4 — run the dub first")
        if not vo.is_file():
            sys.exit("no new-vo.mp3 in the work dir")
        out = work / "final-captioned.mp4"
        deliverable_name = f"{name}-ready-captioned.mp4"
        get_margin = cleaned_band_margin

    words = words_from_vo(vo, work)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(final)],
        capture_output=True, text=True, check=True).stdout.strip()
    vw, vh = (int(x) for x in probe.split(",")[:2])

    ass = work / "captions.ass"
    band = get_margin(name, vh)
    if band is not None:
        print(f"captions: placing over the erased subtitle band (MarginV={band})")
    build_ass(words, ass, vw, vh, margin_v=band)
    print(f"captions: {ass.name} built for {vw}x{vh}")

    # subtitles filter path escaping (Windows: forward slashes + escaped colon)
    ass_esc = str(ass).replace("\\", "/").replace(":", "\\:")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(final), "-vf", f"subtitles='{ass_esc}'",
         "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-c:a", "copy", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not out.is_file():
        sys.exit(f"ffmpeg burn failed: {(proc.stderr or '')[-400:]}")

    READY_DIR.mkdir(parents=True, exist_ok=True)
    deliverable = READY_DIR / deliverable_name
    import shutil
    shutil.copy2(out, deliverable)
    print(f"captioned video -> {out}")
    print(f"deliverable -> {deliverable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
