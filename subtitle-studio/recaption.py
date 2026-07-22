#!/usr/bin/env python3
"""Burn NEW word-timed subtitles onto a video — 100% local and free.

Word timestamps come from LOCAL faster-whisper reading the video's own audio
track (no API, no cost). If the video's old subtitles were erased with a box
(files/.originals/<stem>.box.json), the new captions are placed exactly over
that band; otherwise they sit lower-center.

Style: bold white, heavy black outline, 3 words per line, all caps.

Usage: python recaption.py <files/video.mp4>
Writes output/<stem>/captioned.mp4  +  Desktop/Subtitle Studio/<stem>-subtitled.mp4
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
READY_DIR = Path.home() / "Desktop" / "Subtitle Studio"

WORDS_PER_LINE = 3
FONT = "Arial"
FONT_SIZE = 50
OUTLINE = 3
SHADOW = 2
MARGIN_V = 230
MARGIN_LR = 40


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


def words_from_audio(video: Path, work: Path, trust_cache: bool = False,
                     cpu_only: bool = False) -> list[dict]:
    cache = work / "words.json"
    # trust_cache: the pipeline erases subtitles AFTER transcribing — the file's mtime
    # changes but the audio is stream-copied, so the cached words are still exact
    if cache.is_file() and (trust_cache or cache.stat().st_mtime > video.stat().st_mtime):
        print("word timing: cached")
        return json.loads(cache.read_text(encoding="utf-8"))

    _register_cuda_dlls()
    from faster_whisper import WhisperModel

    model = None
    devices = (("cpu", "int8"),) if cpu_only else (("cuda", "int8_float16"), ("cpu", "int8"))
    for device, compute in devices:
        try:
            print(f"word timing: loading whisper on {device}…", flush=True)
            model = WhisperModel("distil-large-v3", device=device, compute_type=compute)
            break
        except Exception as e:
            print(f"  {device} unavailable ({str(e)[:120]})")
    if model is None:
        raise RuntimeError("could not load whisper on any device")

    segments, info = model.transcribe(str(video), word_timestamps=True, vad_filter=True, beam_size=5)
    words = [{"text": w.word, "start": round(w.start, 3), "end": round(w.end, 3)}
             for seg in segments for w in (seg.words or [])]
    if not words:
        raise RuntimeError("no speech found — nothing to caption")
    cache.write_text(json.dumps(words), encoding="utf-8")
    # remember the detected spoken language so a later --translate run knows the source
    lang = (getattr(info, "language", None) or "en").lower()
    (work / "lang.txt").write_text(lang, encoding="utf-8")
    print(f"word timing: {len(words)} words (detected language: {lang})")
    return words


def _box_for(stem: str, video_h: int, video_w: int) -> dict | None:
    """Load the saved subtitle-band box for a stem, scaled to the current resolution."""
    box_file = ROOT / "files" / ".originals" / f"{stem}.box.json"
    if not box_file.is_file():
        return None
    box = json.loads(box_file.read_text(encoding="utf-8"))
    if box.get("none") or "y" not in box:
        return None
    sx = video_w / box.get("vw", video_w)
    sy = video_h / box.get("vh", video_h)
    return {"x": int(box["x"] * sx), "y": int(box["y"] * sy),
            "w": int(box["w"] * sx), "h": int(box["h"] * sy)}


def band_margin(stem: str, video_h: int, video_w: int | None = None) -> int | None:
    """MarginV (px from the bottom) for the new captions.

    Captions belong in the lower third. We only sit them exactly on the erased band
    when that band is ALREADY in the lower part of the frame (so it looks native).
    If the old subtitles were mid-frame / high (over the face), placing the new ones
    there looks wrong — fall back to a proper lower-third position instead."""
    lower_third = int(video_h * 0.16)          # ~16% up from the bottom = standard caption line
    b = _box_for(stem, video_h, video_w or video_h)
    if b is None:
        return lower_third
    band_center = b["y"] + b["h"] / 2
    if band_center < video_h * 0.60:           # band is mid-frame or higher -> don't follow it
        return lower_third
    margin = int(video_h - (b["y"] + b["h"]) + max(0, (b["h"] - FONT_SIZE * 1.4) / 2))
    return max(lower_third, min(margin, video_h - FONT_SIZE - 30))


def _ass_time(t: float) -> str:
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


# ── Local offline translation (Argos Translate / ctranslate2 — no API, no cost) ──
# Font per script so translated captions actually render (Arial has no CJK glyphs →
# tofu boxes). Windows ships these system fonts. Upper-casing only helps Latin/Cyrillic.
_FONT_BY_LANG = {
    "zh": "Microsoft YaHei", "ja": "Yu Gothic", "ko": "Malgun Gothic",
    "ar": "Arial", "he": "Arial", "th": "Leelawadee UI", "hi": "Nirmala UI",
}
_NO_UPPERCASE = {"zh", "ja", "ko", "ar", "he", "th", "hi"}


def font_for(lang: str | None) -> str:
    return _FONT_BY_LANG.get((lang or "").lower(), FONT)


def font_for_text(text: str) -> str:
    """Pick a font from the script actually present in the text (so edited/re-burned
    non-Latin captions render too, even when the language code isn't known)."""
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF:
            return _FONT_BY_LANG["ja" if 0x3040 <= o <= 0x30FF else "zh"]
        if 0xAC00 <= o <= 0xD7A3:
            return _FONT_BY_LANG["ko"]
        if 0x0E00 <= o <= 0x0E7F:
            return _FONT_BY_LANG["th"]
        if 0x0900 <= o <= 0x097F:
            return _FONT_BY_LANG["hi"]
    return FONT


def ensure_argos(from_code: str, to_code: str) -> None:
    """Make sure the offline language packages for from→to are installed.
    Argos pivots through English, so for non-English pairs we install both legs.
    Downloading a package needs internet ONCE (cached in ~/.local/share/argos-translate);
    after that translation is fully offline."""
    import argostranslate.package as pkg
    pairs = [(from_code, to_code)]
    if "en" not in (from_code, to_code):
        pairs = [(from_code, "en"), ("en", to_code)]   # pivot via English
    installed = {(p.from_code, p.to_code) for p in pkg.get_installed_packages()}
    missing = [p for p in pairs if p not in installed]
    if not missing:
        return
    print(f"translation: fetching language package(s) {missing} (one-time download)…", flush=True)
    pkg.update_package_index()
    available = pkg.get_available_packages()
    for fc, tc in missing:
        match = next((p for p in available if p.from_code == fc and p.to_code == tc), None)
        if match is None:
            raise RuntimeError(f"no Argos package for {fc}->{tc} — that language pair isn't available offline")
        pkg.install_from_path(match.download())


def translate_text(text: str, from_code: str, to_code: str) -> str:
    import argostranslate.translate as tr
    return tr.translate(text, from_code, to_code)


def group_lines(words: list[dict], translate: tuple[str, str] | None = None) -> list[dict]:
    """Group word timings into editable caption lines: [{start, end, text}].
    If translate=(from_code, to_code) is given, each line's text is translated offline
    (keeping the original timing) before it is cased for display."""
    out, group = [], []
    for w in words:
        group.append(w)
        if len(group) >= WORDS_PER_LINE:
            out.append(group)
            group = []
    if group:
        out.append(group)

    def render(g: list[dict]) -> str:
        text = " ".join(re.sub(r"\s+", " ", w["text"].strip()) for w in g)
        if translate:
            fc, tc = translate
            text = translate_text(text, fc, tc)
            return text if tc in _NO_UPPERCASE else text.upper()
        return text.upper()

    return [{"start": g[0]["start"], "end": g[-1]["end"], "text": render(g)} for g in out]


def build_ass(lines: list[dict], out_path: Path, video_w: int, video_h: int,
              margin_v: int | None = None, font: str = FONT) -> None:
    """Write the .ass from editable caption lines ({start, end, text}). Text is burned
    verbatim — whatever the user edited is exactly what appears."""
    margin_v = margin_v if margin_v is not None else MARGIN_V
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{FONT_SIZE},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{OUTLINE},{SHADOW},2,{MARGIN_LR},{MARGIN_LR},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = [
        f"Dialogue: 0,{_ass_time(ln['start'])},{_ass_time(ln['end'])},Cap,,0,0,0,,"
        + re.sub(r"\s*\n\s*", r"\\N", ln["text"].strip())   # allow manual line breaks
        for ln in lines if ln.get("text", "").strip()
    ]
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", help="video to subtitle (word timing from its own audio)")
    ap.add_argument("--words-only", action="store_true",
                    help="transcribe to words.json + script.txt and exit (pipeline step 1)")
    ap.add_argument("--trust-cache", action="store_true",
                    help="reuse words.json even if the video file is newer (audio unchanged)")
    ap.add_argument("--cpu", action="store_true",
                    help="transcribe on CPU (used when another job holds the GPU)")
    ap.add_argument("--burn-lines", action="store_true",
                    help="skip transcription — re-burn from the edited output/<stem>/lines.json")
    ap.add_argument("--cover", action="store_true",
                    help="cover the saved subtitle band (hides old subtitles) before captions")
    ap.add_argument("--cover-style", choices=("blur", "box"), default="blur",
                    help="blur = frosted cover that blends into the video (default); box = solid black bar")
    ap.add_argument("--no-captions", action="store_true",
                    help="ONLY remove the old subtitles (cover the band) — do NOT add any new captions")
    ap.add_argument("--translate", default="", metavar="LANG",
                    help="translate the captions into this language (ISO code, e.g. es/fr/de/he/zh) "
                         "using offline Argos Translate. Empty = keep the spoken language.")
    args = ap.parse_args()

    video = Path(args.video).resolve()
    if not video.is_file():
        sys.exit(f"no such video: {video}")
    stem = video.stem
    work = ROOT / "output" / stem
    work.mkdir(parents=True, exist_ok=True)
    lines_file = work / "lines.json"

    if args.no_captions:
        # remove-only: cover the detected band, add NOTHING. No transcription, no subtitles.
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True).stdout.strip()
        vw, vh = (int(x) for x in probe.split(",")[:2])
        b = _box_for(stem, vh, vw)
        out = work / "captioned.mp4"
        if b is None:
            print("no subtitle band saved — copying through unchanged")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-c", "copy", str(out)])
        else:
            style = args.cover_style
            if style == "blur":
                x0, y0, w0, h0 = b["x"], b["y"], b["w"], b["h"]
                fc = (f"[0:v]crop={w0}:{h0}:{x0}:{y0},scale=iw/22:ih/22,"
                      f"scale={w0}:{h0}:flags=neighbor,boxblur=6[cov];[0:v][cov]overlay={x0}:{y0}[out]")
                ff = ["ffmpeg", "-y", "-i", str(video), "-filter_complex", fc,
                      "-map", "[out]", "-map", "0:a?"]
            else:
                ff = ["ffmpeg", "-y", "-i", str(video), "-vf",
                      f"drawbox=x={b['x']}:y={b['y']}:w={b['w']}:h={b['h']}:color=black:t=fill"]
            ff += ["-c:v", "libx264", "-crf", "17", "-preset", "veryfast", "-c:a", "copy", str(out)]
            print(f"removing old subtitles ({style} cover) at ({b['x']},{b['y']}) {b['w']}x{b['h']} — NO new captions")
            r = subprocess.run(ff, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0 or not out.is_file():
                sys.exit(f"cover failed: {(r.stderr or '')[-300:]}")
        READY_DIR.mkdir(parents=True, exist_ok=True)
        deliverable = READY_DIR / f"{stem}-clean.mp4"
        shutil.copy2(out, deliverable)
        print(f"clean video (subtitles removed, no captions) -> {out}")
        print(f"deliverable -> {deliverable}")
        return 0

    if args.burn_lines:
        # editor path: use the user's edited caption lines verbatim, no whisper
        if not lines_file.is_file():
            sys.exit("no lines.json to burn — caption the video once before editing")
        lines = json.loads(lines_file.read_text(encoding="utf-8"))
        print(f"re-burning {len(lines)} edited caption line(s) — no transcription")
    else:
        words = words_from_audio(video, work, trust_cache=args.trust_cache, cpu_only=args.cpu)
        if args.words_only:
            script = " ".join(re.sub(r"\s+", " ", w["text"].strip()) for w in words)
            (work / "script.txt").write_text(script + "\n", encoding="utf-8")
            print(f"script: {len(words)} words -> output/{stem}/script.txt")
            print(f"script preview: {script[:220]}{'…' if len(script) > 220 else ''}")
            return 0
        translate = None
        target = args.translate.strip().lower()
        if target:
            source = (work / "lang.txt").read_text(encoding="utf-8").strip().lower() \
                if (work / "lang.txt").is_file() else "en"
            if source == target:
                print(f"translation: spoken language is already '{target}' — no translation needed")
            else:
                print(f"translation: {source} -> {target} (offline)")
                ensure_argos(source, target)
                translate = (source, target)
        lines = group_lines(words, translate=translate)
        # write the editable caption lines (the Edit-subtitles UI reads/writes this)
        lines_file.write_text(json.dumps(lines, indent=1), encoding="utf-8")

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True).stdout.strip()
    vw, vh = (int(x) for x in probe.split(",")[:2])

    ass = work / "captions.ass"
    band = band_margin(stem, vh, vw)
    if band is not None:
        print(f"captions: placing over the subtitle band (MarginV={band})")
    # font: from the requested target language, else sniffed from the caption text
    # (keeps non-Latin captions readable on editor re-burns too)
    sample = " ".join(ln.get("text", "") for ln in lines[:8])
    cap_font = font_for(args.translate.strip()) if args.translate.strip() else font_for_text(sample)
    build_ass(lines, ass, vw, vh, margin_v=band, font=cap_font)
    print(f"captions: {ass.name} built for {vw}x{vh} (font: {cap_font})")

    out = work / "captioned.mp4"
    # subtitles filter path escaping (Windows: forward slashes + escaped colon)
    ass_esc = str(ass).replace("\\", "/").replace(":", "\\:")
    subs = f"subtitles='{ass_esc}'"
    ff = ["ffmpeg", "-y", "-i", str(video)]
    # Decide whether to re-cover the old-subtitle band. Explicit --cover (box/blur flow),
    # OR — so EDITING preserves the cover — infer it from the saved box.json method:
    #   mode box/blur => the source still has old subs under a cover, re-apply it;
    #   mode erase / none => source is already clean (or untouched), captions only.
    saved_mode = None
    bf = ROOT / "files" / ".originals" / f"{stem}.box.json"
    if bf.is_file():
        try:
            saved_mode = json.loads(bf.read_text(encoding="utf-8")).get("mode")
        except Exception:
            pass
    do_cover = args.cover or saved_mode in ("box", "blur")
    cover_style = args.cover_style if args.cover else (saved_mode if saved_mode in ("box", "blur") else "blur")
    b = _box_for(stem, vh, vw) if do_cover else None
    if args.cover and b is not None and bf.is_file():
        try:   # remember the ACTUAL style so a later edit re-burn re-applies the same cover
            d = json.loads(bf.read_text(encoding="utf-8")); d["mode"] = cover_style
            bf.write_text(json.dumps(d), encoding="utf-8")
        except Exception:
            pass
    if args.cover and b is None:
        print("cover requested but no subtitle band was detected — captions only")

    if b is None:
        ff += ["-vf", subs]
    elif cover_style == "blur":
        # frosted cover: crop the band, smear it (downscale→upscale so the old text is
        # unreadable) then overlay it back — keeps the video's colors, blends in, then captions
        x, y, w, h = b["x"], b["y"], b["w"], b["h"]
        fc = (f"[0:v]crop={w}:{h}:{x}:{y},scale=iw/22:ih/22,"
              f"scale={w}:{h}:flags=neighbor,boxblur=6[cov];"
              f"[0:v][cov]overlay={x}:{y}[bg];[bg]{subs}[out]")
        ff += ["-filter_complex", fc, "-map", "[out]", "-map", "0:a?"]
        print(f"frosted cover over old subtitles at ({x},{y}) {w}x{h}")
    else:  # solid box
        ff += ["-vf", f"drawbox=x={b['x']}:y={b['y']}:w={b['w']}:h={b['h']}:color=black:t=fill,{subs}"]
        print(f"solid cover box over old subtitles at ({b['x']},{b['y']}) {b['w']}x{b['h']}")

    ff += ["-c:v", "libx264", "-crf", "17", "-preset", "veryfast", "-c:a", "copy", str(out)]
    proc = subprocess.run(ff, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not out.is_file():
        sys.exit(f"ffmpeg burn failed: {(proc.stderr or '')[-400:]}")

    READY_DIR.mkdir(parents=True, exist_ok=True)
    lang_tag = f"-{args.translate.strip().lower()}" if args.translate.strip() else ""
    deliverable = READY_DIR / f"{stem}-subtitled{lang_tag}.mp4"
    shutil.copy2(out, deliverable)
    print(f"captioned video -> {out}")
    print(f"deliverable -> {deliverable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
