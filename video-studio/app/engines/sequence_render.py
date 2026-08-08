#!/usr/bin/env python3
"""Render a sequence document (EDL) to a video file.

    python sequence_render.py --project <project.json> --out <final.mp4>
                              [--range A:B] [--scale 0.5] [--force]

Design, and why:

* **Per-clip segment cache.** Each clip renders once to
  `cache/<hash>.mp4`, keyed on everything that affects its pixels. Change one
  clip, re-render one clip. On a 4 GB machine this is the difference between an
  editor and a slideshow.
* **Every segment is normalised identically** (same codec, fps, pixel format,
  timebase, sample rate, channel layout) so the final join can use the concat
  *demuxer* with stream copy — no generation loss, no re-encode.
* **Silent sources still get an audio track.** concat refuses to join segments
  whose stream layout differs, so a video with no audio gets `anullsrc`.
* **Never `-shortest`.** It silently dropped trailing frames in this repo
  before. Segments are cut with `-t` and every render is verified by counting
  frames with ffprobe; a short take is deleted rather than cached.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sequence as seq  # noqa: E402

FFMPEG_BIN = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1.2-full_build/bin"
)


def ff(name: str) -> str:
    exe = FFMPEG_BIN / f"{name}.exe"
    return str(exe) if exe.is_file() else name


def run(cmd: list[str], *, quiet: bool = False) -> subprocess.CompletedProcess:
    if not quiet:
        print("+", " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd[:4]), "…", flush=True)
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def probe(path: Path, entries: str, stream: str = "v:0") -> str:
    r = run([ff("ffprobe"), "-v", "error", "-select_streams", stream,
             "-show_entries", entries, "-of", "default=nw=1:nk=1", str(path)], quiet=True)
    return (r.stdout or "").strip()


def has_audio(path: Path) -> bool:
    return bool(probe(path, "stream=index", "a:0"))


def frame_count(path: Path) -> int:
    """Real frame count. -count_frames is slow but it is the only honest answer."""
    out = probe(path, "stream=nb_read_frames", "v:0")
    if out.isdigit():
        return int(out)
    r = run([ff("ffprobe"), "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1",
             str(path)], quiet=True)
    out = (r.stdout or "").strip()
    return int(out) if out.isdigit() else 0


def media_dur(path: Path) -> float:
    out = probe(path, "format=duration", "v:0")
    if not out:
        r = run([ff("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)], quiet=True)
        out = (r.stdout or "").strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- segment build

def clip_hash(clip: dict, canvas: dict, src: Path, scale: float) -> str:
    """Key the cache on everything that changes the pixels — including the
    source file's mtime+size, so replacing a file busts its segments."""
    try:
        st = src.stat()
        stamp = f"{int(st.st_mtime)}:{st.st_size}"
    except OSError:
        stamp = "missing"
    payload = {
        "src": clip.get("src"), "stamp": stamp,
        "in": clip.get("in"), "out": clip.get("out"),
        "speed": clip.get("speed"), "volume": clip.get("volume"),
        "transform": clip.get("transform"), "effects": clip.get("effects"),
        "canvas": canvas, "scale": scale, "v": 3,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def video_filter(clip: dict, w: int, h: int, fps: int) -> str:
    """Fit the source into the canvas, then apply the clip's transform.

    Order matters: scale-to-fit and pad first so `scale`/`x`/`y` always mean the
    same thing regardless of the source's own aspect ratio.
    """
    tr = clip.get("transform") or {}
    zoom = max(0.05, float(tr.get("scale") or 1.0))
    dx, dy = float(tr.get("x") or 0), float(tr.get("y") or 0)
    rot = float(tr.get("rot") or 0)
    speed = float(clip.get("speed") or 1.0)

    parts = [
        f"scale={w}:{h}:force_original_aspect_ratio=decrease",
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
    ]
    if abs(zoom - 1.0) > 1e-3 or abs(dx) > 1e-3 or abs(dy) > 1e-3:
        zw, zh = f"iw*{zoom:.4f}", f"ih*{zoom:.4f}"
        parts += [f"scale={zw}:{zh}",
                  f"crop={w}:{h}:(iw-{w})/2+({dx:.2f}):(ih-{h})/2+({dy:.2f})"]
    if abs(rot) > 1e-3:
        parts.append(f"rotate={rot}*PI/180:c=black")
    if abs(speed - 1.0) > 1e-3:
        parts.append(f"setpts=PTS/{speed:.4f}")
    parts.append(f"fps={fps}")
    # fades live inside the segment, so they stay cache-friendly (effects is
    # part of the clip hash) and never complicate the concat join
    dur = seq.clip_dur(clip)
    for e in clip.get("effects") or []:
        d = min(float(e.get("d") or 0), dur)
        if d <= 0:
            continue
        if e.get("type") == "fade_in":
            parts.append(f"fade=t=in:st=0:d={d:.3f}")
        elif e.get("type") == "fade_out":
            parts.append(f"fade=t=out:st={max(0.0, dur - d):.3f}:d={d:.3f}")
    return ",".join(parts)


def audio_filter(clip: dict) -> str:
    speed = float(clip.get("speed") or 1.0)
    vol = float(clip.get("volume") if clip.get("volume") is not None else 1.0)
    parts = []
    # atempo only accepts 0.5–2.0 per instance, so chain it for extreme speeds
    rest = speed
    while rest > 2.0 + 1e-6:
        parts.append("atempo=2.0")
        rest /= 2.0
    while rest < 0.5 - 1e-6:
        parts.append("atempo=0.5")
        rest /= 0.5
    if abs(rest - 1.0) > 1e-3:
        parts.append(f"atempo={rest:.4f}")
    if abs(vol - 1.0) > 1e-3:
        parts.append(f"volume={vol:.4f}")
    dur = seq.clip_dur(clip)
    for e in clip.get("effects") or []:
        d = min(float(e.get("d") or 0), dur)
        if d <= 0:
            continue
        if e.get("type") == "fade_in":
            parts.append(f"afade=t=in:st=0:d={d:.3f}")
        elif e.get("type") == "fade_out":
            parts.append(f"afade=t=out:st={max(0.0, dur - d):.3f}:d={d:.3f}")
    parts += ["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
    return ",".join(parts)


def render_segment(clip: dict, canvas: dict, root: Path, cache: Path,
                   scale: float, force: bool) -> Path:
    src = (root / clip["src"]).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"source missing: {clip['src']}")

    w = int(canvas["w"] * scale) // 2 * 2
    h = int(canvas["h"] * scale) // 2 * 2
    fps = int(canvas["fps"])
    out = cache / f"{clip_hash(clip, {'w': w, 'h': h, 'fps': fps}, src, scale)}.mp4"
    dur = seq.clip_dur(clip)
    want = max(1, round(dur * fps))

    if out.is_file() and not force:
        if frame_count(out) >= want - 1:
            print(f"  cache hit  {clip['id']} -> {out.name}", flush=True)
            return out
        out.unlink(missing_ok=True)      # short/corrupt take: never trust it

    src_dur = (float(clip["out"]) - float(clip["in"]))
    cmd = [ff("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{float(clip['in']):.4f}", "-t", f"{src_dur:.4f}", "-i", str(src)]

    vf = video_filter(clip, w, h, fps)
    if has_audio(src):
        cmd += ["-filter_complex",
                f"[0:v]{vf}[v];[0:a]{audio_filter(clip)}[a]",
                "-map", "[v]", "-map", "[a]"]
    else:
        # a segment with no audio cannot concat with one that has audio
        cmd += ["-f", "lavfi", "-t", f"{dur:.4f}", "-i", "anullsrc=r=48000:cl=stereo",
                "-filter_complex", f"[0:v]{vf}[v]",
                "-map", "[v]", "-map", "1:a"]

    cmd += ["-t", f"{dur:.4f}",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-video_track_timescale", "90000",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(out)]

    r = run(cmd)
    if r.returncode != 0 or not out.is_file():
        raise RuntimeError(f"segment render failed for {clip['id']}:\n{r.stderr[-1500:]}")

    got = frame_count(out)
    if got < want - 2:                      # the -shortest class of bug, caught here
        out.unlink(missing_ok=True)
        raise RuntimeError(
            f"segment {clip['id']} came out short: {got} frames, expected ~{want}. "
            f"Take deleted rather than cached.")
    print(f"  rendered   {clip['id']} -> {out.name} ({got}f)", flush=True)
    return out


# ---------------------------------------------------------------- text -> ASS

def ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def hex_to_ass(color: str) -> str:
    c = (color or "#FFFFFF").lstrip("#")
    if len(c) != 6:
        return "&H00FFFFFF"
    return f"&H00{c[4:6]}{c[2:4]}{c[0:2]}".upper()      # ASS is BBGGRR


def build_ass(doc: dict, path: Path, scale: float) -> bool:
    """Text tracks become a subtitle file — reusing the caption pipeline this
    repo already trusts, instead of a pile of drawtext filters."""
    clips = [c for t in doc["tracks"] if t["kind"] == "text" and not t.get("muted")
             for c in t["clips"]]
    if not clips:
        return False
    w = int(doc["canvas"]["w"] * scale); h = int(doc["canvas"]["h"] * scale)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
        "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
    ]
    events = []
    for i, c in enumerate(clips):
        st = c.get("style") or {}
        size = int(float(st.get("size") or 64) * scale)
        align = {"top": 8, "middle": 5, "bottom": 2}.get(st.get("pos") or "bottom", 2)
        margin = int(h * 0.08)
        lines.append(
            f"Style: s{i},{st.get('font') or 'Arial'},{size},{hex_to_ass(st.get('color'))},"
            f"&H000000FF,&H00000000,&H00000000,{-1 if st.get('bold', True) else 0},0,0,0,"
            f"100,100,0,0,1,{int(float(st.get('outline') or 3) * scale)},0,{align},"
            f"{int(w*0.06)},{int(w*0.06)},{margin},1")
        text = str(c.get("text") or "").replace("\n", "\\N").replace("{", "(").replace("}", ")")
        events.append(f"Dialogue: 0,{ass_time(float(c['start']))},"
                      f"{ass_time(seq.clip_end(c))},s{i},,0,0,0,,{text}")
    lines += ["", "[Events]",
              "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text"]
    lines += events
    path.write_text("\n".join(lines), encoding="utf-8")
    return True


# ---------------------------------------------------------------- assembly

def concat_segments(segs: list[Path], out: Path, work: Path) -> None:
    """Join with the concat demuxer and stream copy — no re-encode."""
    lst = work / "concat.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in segs), encoding="utf-8")
    r = run([ff("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c", "copy", "-movflags", "+faststart", str(out)])
    if r.returncode != 0 or not out.is_file():
        raise RuntimeError(f"concat failed:\n{r.stderr[-1500:]}")


def build_gap_filler(dur: float, canvas: dict, scale: float, work: Path, idx: int) -> Path:
    """Black with silence — a gap on the video track is a real thing to render."""
    w = int(canvas["w"] * scale) // 2 * 2
    h = int(canvas["h"] * scale) // 2 * 2
    fps = int(canvas["fps"])
    out = work / f"gap{idx}.mp4"
    r = run([ff("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={fps}",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
             "-t", f"{dur:.4f}",
             "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", "-video_track_timescale", "90000",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", str(out)])
    if r.returncode != 0:
        raise RuntimeError(f"gap render failed:\n{r.stderr[-800:]}")
    return out


def render(project: Path, out: Path, rng: tuple[float, float] | None,
           scale: float, force: bool) -> Path:
    doc = seq.load(project)
    seq.validate(doc)
    root = Path(doc.get("meta", {}).get("root") or project.parent.parent.parent.parent)
    work = project.parent
    cache = work / "cache"
    cache.mkdir(parents=True, exist_ok=True)

    canvas = doc["canvas"]
    total = seq.duration(doc)
    if total <= 0:
        raise RuntimeError("nothing to render — the timeline is empty")
    a, b = rng if rng else (0.0, total)
    b = min(b, total)
    print(f"render {doc.get('name')} v{doc.get('version')} · {a:.2f}–{b:.2f}s "
          f"· {canvas['w']}x{canvas['h']}@{canvas['fps']} · scale {scale}", flush=True)

    vtracks = [t for t in doc["tracks"] if t["kind"] == "video" and not t.get("muted")]
    if not vtracks or not any(t["clips"] for t in vtracks):
        raise RuntimeError("no video clips to render")
    base = vtracks[0]                       # V1 is the spine; upper tracks are P4 work

    # ---- 1. segments, with gaps filled, clipped to the requested range
    segs: list[Path] = []
    cursor = a
    gap_i = 0
    for clip in sorted(base["clips"], key=lambda c: float(c["start"])):
        cs, ce = float(clip["start"]), seq.clip_end(clip)
        if ce <= a or cs >= b:
            continue
        if cs > cursor + 0.01:
            segs.append(build_gap_filler(min(cs, b) - cursor, canvas, scale, work, gap_i))
            gap_i += 1
            cursor = cs
        piece = json.loads(json.dumps(clip))
        speed = float(clip.get("speed") or 1.0)
        if cs < a:                          # partial head
            piece["in"] = round(float(clip["in"]) + (a - cs) * speed, 4)
            piece["start"] = a
        if ce > b:                          # partial tail
            piece["out"] = round(float(piece["in"]) + (b - max(cs, a)) * speed, 4)
        segs.append(render_segment(piece, canvas, root, cache, scale, force))
        cursor = min(ce, b)
    if not segs:
        raise RuntimeError("the requested range contains no clips")
    if cursor < b - 0.01:
        segs.append(build_gap_filler(b - cursor, canvas, scale, work, gap_i))

    joined = work / "_joined.mp4"
    concat_segments(segs, joined, work)

    # ---- 2. audio tracks + text, one pass (only if there is anything to add)
    atracks = [t for t in doc["tracks"] if t["kind"] == "audio" and not t.get("muted")
               and t["clips"]]
    ass = work / "_text.ass"
    has_text = build_ass(doc, ass, scale)

    if not atracks and not has_text:
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(joined), str(out))
        print(f"OK {out}", flush=True)
        return verify(out, b - a, int(canvas["fps"]))

    cmd = [ff("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-i", str(joined)]
    inputs, amix = 1, ["[0:a]"]
    for t in atracks:
        for c in t["clips"]:
            src = (root / c["src"]).resolve()
            if not src.is_file():
                continue
            cmd += ["-i", str(src)]
            inputs += 1
    filters, idx = [], 1
    for t in atracks:
        for c in t["clips"]:
            src = (root / c["src"]).resolve()
            if not src.is_file():
                continue
            delay = max(0, int((float(c["start"]) - a) * 1000))
            vol = float(c.get("volume") if c.get("volume") is not None else 1.0)
            filters.append(
                f"[{idx}:a]atrim=start={float(c.get('in') or 0):.4f}:"
                f"end={float(c.get('in') or 0) + seq.clip_dur(c):.4f},"
                f"asetpts=PTS-STARTPTS,volume={vol:.3f},"
                f"adelay={delay}|{delay},aresample=48000[a{idx}]")
            amix.append(f"[a{idx}]")
            idx += 1

    if len(amix) > 1:
        filters.append(f"{''.join(amix)}amix=inputs={len(amix)}:"
                       f"duration=first:dropout_transition=0,alimiter=limit=0.95[aout]")
        amap = "[aout]"
    else:
        amap = "0:a"

    if has_text:
        esc = ass.as_posix().replace(":", "\\:")
        filters.append(f"[0:v]subtitles='{esc}'[vout]")
        vmap = "[vout]"
    else:
        vmap = "0:v"

    cmd += ["-filter_complex", ";".join(filters), "-map", vmap, "-map", amap]
    if not has_text:
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(out)]

    out.parent.mkdir(parents=True, exist_ok=True)
    r = run(cmd)
    if r.returncode != 0 or not out.is_file():
        raise RuntimeError(f"final pass failed:\n{r.stderr[-2000:]}")
    joined.unlink(missing_ok=True)
    print(f"OK {out}", flush=True)
    return verify(out, b - a, int(canvas["fps"]))


def verify(out: Path, expect_sec: float, fps: int) -> Path:
    """Prove the render is the length it claims. A silently-short export is the
    failure mode this repo has been bitten by before."""
    got = media_dur(out)
    frames = frame_count(out)
    want = round(expect_sec * fps)
    print(f"verify: {got:.2f}s / {frames} frames (expected ~{expect_sec:.2f}s / ~{want})",
          flush=True)
    if expect_sec > 0.5 and abs(got - expect_sec) > max(0.5, expect_sec * 0.03):
        raise RuntimeError(
            f"OUTPUT LENGTH MISMATCH: {got:.2f}s vs expected {expect_sec:.2f}s — "
            f"not delivering a bad take")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--range", default="")
    ap.add_argument("--scale", type=float, default=1.0, help="0.5 for a fast draft")
    ap.add_argument("--force", action="store_true", help="ignore the segment cache")
    a = ap.parse_args()

    rng = None
    if a.range:
        lo, _, hi = a.range.partition(":")
        rng = (float(lo or 0), float(hi or 1e9))
    try:
        render(Path(a.project).resolve(), Path(a.out).resolve(), rng, a.scale, a.force)
    except Exception as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
