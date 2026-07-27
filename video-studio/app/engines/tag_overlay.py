#!/usr/bin/env python3
"""Burn TikTok-style "tag" text boxes onto a video — white rounded boxes,
black text, stacked, exactly like the reference UGC ads use.

Text is NEVER baked into the AI generation (models mangle letters). It is
rendered here as transparent PNGs with Pillow — real rounded corners, real
kerning — and overlaid by ffmpeg for each tag's time window.

  # tags timed from a b-roll recipe (one tag per shot, using its duration)
  python tag_overlay.py --video clip.mp4 --recipe recipe.json --out tagged.mp4

  # or an explicit list
  python tag_overlay.py --video clip.mp4 --out tagged.mp4 \
      --tags '[{"start":0,"end":3,"lines":["Me:","Before Fairy Flame"]}]'

Runs under the cv venv (Pillow present); ffmpeg on PATH.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# the reference look: white box, black text, soft rounding, generous padding
BOX_FILL = (255, 255, 255, 236)
TEXT_FILL = (17, 17, 17, 255)
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\seguisb.ttf",     # Segoe UI Semibold — closest to the TikTok face
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]


def log(m: str) -> None:
    print(m, flush=True)


def die(m: str) -> None:
    print(f"ERROR: {m}", flush=True)
    sys.exit(1)


def run(cmd: list[str], label: str) -> None:
    log(f"  {label}...")
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        die(f"{label} failed:\n{(r.stderr or r.stdout or '')[-1000:]}")


def probe_wh(p: Path) -> tuple[int, int]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0:s=x", str(p)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        w, h = (r.stdout or "").strip().split("x")[:2]
        return int(w), int(h)
    except (ValueError, IndexError):
        die(f"could not read the size of {p.name}")


def probe_sec(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    try:
        return float((r.stdout or "0").strip() or 0)
    except ValueError:
        return 0.0


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for f in FONT_CANDIDATES:
        if Path(f).is_file():
            try:
                return ImageFont.truetype(f, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_tag(lines: list[str], vw: int, vh: int, dest: Path, y_frac: float = 0.16) -> None:
    """One transparent PNG: each line its own white rounded box, stacked+centred."""
    img = Image.new("RGBA", (vw, vh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    size = max(22, round(vh * 0.038))          # scales with the video
    font = load_font(size)
    pad_x, pad_y = round(size * 0.62), round(size * 0.34)
    radius = round(size * 0.55)
    gap = round(size * 0.24)

    boxes = []
    for text in lines:
        text = (text or "").strip()
        if not text:
            continue
        l, t, r, b = d.textbbox((0, 0), text, font=font)
        boxes.append((text, r - l, b - t, l, t))
    if not boxes:
        return
    total_h = sum(bh + 2 * pad_y for _, _, bh, _, _ in boxes) + gap * (len(boxes) - 1)
    y = round(vh * y_frac)
    if y + total_h > vh * 0.92:                # keep it clear of the bottom
        y = max(round(vh * 0.06), round(vh * 0.92 - total_h))

    for text, bw, bh, ox, oy in boxes:
        w = bw + 2 * pad_x
        h = bh + 2 * pad_y
        x = (vw - w) // 2
        d.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=BOX_FILL)
        d.text((x + pad_x - ox, y + pad_y - oy), text, font=font, fill=TEXT_FILL)
        y += h + gap
    img.save(dest)


def tags_from_recipe(recipe: dict, total: float) -> list[dict]:
    """One tag per shot, timed by the shots' own durations (scaled to the real
    video length so drift can't push the last tag off the end)."""
    shots = recipe.get("shots") or []
    planned = sum(float(s.get("duration_s") or 0) for s in shots) or 1.0
    scale = (total / planned) if total > 0 else 1.0
    out, t = [], 0.0
    for s in shots:
        dur = float(s.get("duration_s") or 0) * scale
        tag = s.get("tag")
        lines = ([tag] if isinstance(tag, str) else list(tag)) if tag else []
        if lines:
            out.append({"start": round(t, 2), "end": round(t + dur, 2), "lines": lines})
        t += dur
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--recipe", help="b-roll recipe.json — one tag per shot")
    ap.add_argument("--tags", help='JSON list: [{"start":0,"end":3,"lines":["Me:","..."]}]')
    ap.add_argument("--y", type=float, default=0.16, help="vertical position (0=top, 1=bottom)")
    a = ap.parse_args()

    video = Path(a.video).resolve()
    if not video.is_file():
        die(f"no such video: {video}")
    out = Path(a.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    total = probe_sec(video)
    if a.tags:
        tags = json.loads(a.tags)
    elif a.recipe:
        tags = tags_from_recipe(json.loads(Path(a.recipe).read_text(encoding="utf-8")), total)
    else:
        die("pass --tags or --recipe")
    tags = [t for t in tags if (t.get("lines") and float(t.get("end", 0)) > float(t.get("start", 0)))]
    if not tags:
        die("no tags to burn (the recipe has no 'tag' on any shot)")

    vw, vh = probe_wh(video)
    log(f"burning {len(tags)} tag(s) onto {video.name} ({vw}x{vh}, {total:.1f}s)")

    tmp = Path(tempfile.mkdtemp(prefix="tags-"))
    inputs: list[str] = ["-i", str(video)]
    filters: list[str] = []
    cur = "0:v"
    for i, t in enumerate(tags):
        png = tmp / f"tag-{i:02d}.png"
        render_tag([str(x) for x in t["lines"]], vw, vh, png, y_frac=a.y)
        if not png.is_file():
            continue
        inputs += ["-i", str(png)]
        nxt = f"v{i}"
        # fade the box in/out fast so it pops like a sticker, not a hard cut
        filters.append(
            f"[{cur}][{i + 1}:v]overlay=0:0:enable='between(t,{t['start']},{t['end']})'[{nxt}]")
        cur = nxt
        log(f"  {t['start']:>5.1f}-{t['end']:<5.1f}s  {' / '.join(str(x) for x in t['lines'])[:56]}")

    if not filters:
        die("nothing rendered")
    run(["ffmpeg", "-y", "-loglevel", "error", *inputs,
         "-filter_complex", ";".join(filters), "-map", f"[{cur}]", "-map", "0:a?",
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", str(out)],
        "burn tags")
    got = probe_sec(out)
    if abs(got - total) > 0.5:
        out.unlink(missing_ok=True)
        die(f"tagged output {got:.2f}s != source {total:.2f}s — deleted, not delivered off-length")
    log("")
    log(f"OK tagged video -> {out}  ({got:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
