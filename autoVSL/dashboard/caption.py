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

# House caption style — the cr_test2 reference format: bold ROUNDED sans-serif,
# white fill + heavy black outline, word-by-word karaoke, lower third, active
# word popped in liitt gold, keyword phrases held in gold persistently.
WORDS_PER_LINE = 3
FONTS_DIR = Path(__file__).resolve().parent / "fonts"
_BALOO = FONTS_DIR / "baloo-2-v23-latin-800.ttf"
# rounded face matches the reference; Arial Black stays the fallback when the
# font file is missing on a fresh checkout
FONT = "Baloo 2 ExtraBold" if _BALOO.is_file() else "Arial Black"
HILITE = "&H0042C5F5"                # liitt gold #F5C542 (ASS is &H00BBGGRR)
BRAND = "līītt"
BRAND_ALIASES = {"lit", "litt", "liit", "liitt", "leet", "lift"}


def _norm(w: str) -> str:
    return re.sub(r"[^a-z0-9]", "", w.lower())


def mark_keywords(words: list[dict], phrases: list[str]) -> None:
    """Flag the words covered by any keyword phrase (consecutive match) so the
    renderer holds them in gold — the reference's 'yellow keyword highlight'."""
    seq = [_norm(w["text"]) for w in words]
    for phrase in phrases:
        toks = [_norm(t) for t in phrase.split() if _norm(t)]
        if not toks:
            continue
        for i in range(len(seq) - len(toks) + 1):
            if seq[i:i + len(toks)] == toks:
                for k in range(i, i + len(toks)):
                    words[k]["gold"] = True


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

    def styled(g: list[dict], toks: list[str], active: int) -> str:
        """Active word: gold + popped. Keyword words: held gold. Rest: white."""
        parts = []
        for j, t in enumerate(toks):
            if j == active:
                parts.append("{\\1c" + HILITE + "\\fscx108\\fscy108}" + t + "{\\r}")
            elif g[j].get("gold"):
                parts.append("{\\1c" + HILITE + "}" + t + "{\\r}")
            else:
                parts.append(t)
        return " ".join(parts)

    for g in lines:
        toks = [render_word(w["text"].strip()) for w in g]
        if len(toks) == 1:
            events.append(f"Dialogue: 0,{_ass_time(g[0]['start'])},{_ass_time(g[-1]['end'])},Cap,,0,0,0,,"
                          + styled(g, toks, 0))
            continue
        for i, w in enumerate(g):
            s = g[0]["start"] if i == 0 else w["start"]
            e = g[-1]["end"] if i == len(g) - 1 else g[i + 1]["start"]
            if e <= s:
                continue
            events.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Cap,,0,0,0,," + styled(g, toks, i))
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def refit_text(words: list[dict], text: str) -> list[dict]:
    """Burn the USER'S exact wording instead of whisper's: keep the recognized
    word timeline, map the provided words onto it proportionally (1:1 swap when
    the counts match). The karaoke rhythm survives; the words are verbatim."""
    toks = text.split()
    if not toks or not words:
        return words
    n_rec = len(words)
    out = []
    for i, tok in enumerate(toks):
        a = min(int(i * n_rec / len(toks)), n_rec - 1)
        b = max(a + 1, min(int((i + 1) * n_rec / len(toks)) or 1, n_rec))
        out.append({"text": " " + tok, "start": words[a]["start"],
                    "end": words[b - 1]["end"]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", help="script-swap work dir name (caption a dubbed final)")
    ap.add_argument("--video", help="caption an uploads video directly — word timing from its ORIGINAL audio")
    ap.add_argument("--text", help="exact caption text (timing still comes from the audio)")
    ap.add_argument("--keywords", help="comma-separated words/phrases held in gold "
                                       "(the reference style's yellow keyword highlight)")
    args = ap.parse_args()
    if not (args.name or args.video):
        sys.exit("pass --name (dubbed final) or --video (any file; add --name to pick the workdir)")
    # stage marker for the studio's stage map (dub.py prints its own when chaining)
    print("=== stage: captions ===", flush=True)

    if args.video:
        final = Path(args.video).resolve()
        if not final.is_file():
            sys.exit(f"no such video: {final}")
        # --name alongside --video names the workdir (UGC clips are all clip.mp4)
        name = args.name or final.stem
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

    if args.text and args.video:
        # prime whisper with the intended wording (helps brand words align);
        # --name mode must NOT touch script-edited.txt — that's the dub's script
        (work / "script-edited.txt").write_text(args.text, encoding="utf-8")
    words = words_from_vo(vo, work)
    if args.text:
        words = refit_text(words, args.text.strip())
        print(f"captions: using the provided text verbatim ({len(args.text.split())} words)")
    if args.keywords:
        phrases = [p.strip() for p in args.keywords.replace("·", ",").split(",") if p.strip()]
        mark_keywords(words, phrases)
        print(f"captions: {sum(1 for w in words if w.get('gold'))} keyword words held in gold")

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

    # subtitles filter path escaping (Windows: forward slashes + escaped colon);
    # fontsdir ships the rounded Baloo 2 face with the repo — no font install
    ass_esc = str(ass).replace("\\", "/").replace(":", "\\:")
    vf = f"subtitles='{ass_esc}'"
    if _BALOO.is_file():
        fonts_esc = str(FONTS_DIR).replace("\\", "/").replace(":", "\\:")
        vf += f":fontsdir='{fonts_esc}'"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(final), "-vf", vf,
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
