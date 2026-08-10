#!/usr/bin/env python3
"""Background Swap — key out a green screen and composite the actor over a real
scene (photo or looping video), entirely local via ffmpeg.

Workdir layout (same contract as dubsync_repair.py):
    final.mp4       the current dubbed output (input to the swap)
    bg-*.mp4        NEW versioned takes — final.mp4 is never touched; promote
                    the take you like from the Fix step

Modes:
    --work DIR --background FILE            take mode (after dub: new take)
    --input V --background F --output O     standalone file-to-file mode
    --input V --background F --replace --backup-dir D
                                            pre-dub mode: key the SOURCE in
                                            place; the untouched original is
                                            backed up to D first (same
                                            .originals convention as clean-subs,
                                            restorable via /api/clean-restore)
    ... --preview OUT.png [--at SECONDS]    single-frame composite for tuning,
                                            no video is written

Realism levers (all optional):
    --key-color auto|0xRRGGBB   auto samples the frame corners — real screens
                                are never pure 0x00FF00
    --similarity 0.15           how far from the key still counts as green.
                                Keep under ~0.25: chromakey ignores brightness,
                                so big values start eating whites (shirts, teeth)
    --blend 0.05                edge softness
    --bg-blur 6                 gaussian blur on the background; fakes camera
                                depth of field and hides key imperfections
    --no-despill                keep the green light bounce (default: remove it)
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def die(msg: str) -> None:
    print(f"[background-swap] ERROR: {msg}", flush=True)
    sys.exit(1)


def run(cmd: list) -> None:
    print("[ffmpeg] " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True)


def probe_duration(path: Path) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(out.strip())


def probe_resolution(path: Path) -> tuple[int, int]:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)])
    w, h = out.decode().strip().splitlines()[0].split("x")[:2]
    return int(w), int(h)


def detect_key_color(video: Path, tmp_dir: Path) -> str:
    """Average the green-dominant 24x24 corner patches of the first frame."""
    default = "0x00FF00"
    frame = tmp_dir / "bg-keyprobe.png"
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video),
                        "-frames:v", "1", "-update", "1", str(frame)], check=True)
        try:
            import cv2
            img = cv2.imread(str(frame))          # BGR
            h, w = img.shape[:2]
            corners = [img[:24, :24], img[:24, w - 24:], img[h - 24:, :24], img[h - 24:, w - 24:]]
            patches = []
            for c in corners:
                b, g, r = (float(c[..., i].mean()) for i in range(3))
                if g > r * 1.2 and g > b * 1.2 and g > 60:
                    patches.append((r, g, b))
        except ImportError:
            from PIL import Image
            img = Image.open(frame).convert("RGB")
            w, h = img.size
            patches = []
            for x0, y0 in [(0, 0), (w - 24, 0), (0, h - 24), (w - 24, h - 24)]:
                px = [img.getpixel((x, y)) for x in range(x0, x0 + 24) for y in range(y0, y0 + 24)]
                r, g, b = (sum(p[i] for p in px) / len(px) for i in range(3))
                if g > r * 1.2 and g > b * 1.2 and g > 60:
                    patches.append((r, g, b))
        if not patches:
            print(f"[background-swap] no green-dominant corner found; using {default}", flush=True)
            return default
        r, g, b = (int(sum(p[i] for p in patches) / len(patches)) for i in range(3))
        return f"0x{r:02X}{g:02X}{b:02X}"
    except Exception as exc:
        print(f"[background-swap] key auto-detect failed ({exc}); using {default}", flush=True)
        return default


def build_filter(key: str, a, w: int, h: int) -> str:
    bg = f"[1:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    if a.bg_blur:
        bg += f",gblur=sigma={a.bg_blur}"
    bg += ",setsar=1[bg]"
    fg = f"[0:v]chromakey={key}:{a.similarity}:{a.blend}"
    if not a.no_despill:
        fg += ",despill=type=green"
    fg += "[fg]"
    return f"{bg};{fg};[bg][fg]overlay=shortest=1:format=auto,format=yuv420p[out]"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work")
    ap.add_argument("--input")
    ap.add_argument("--output")
    ap.add_argument("--background", required=True)
    ap.add_argument("--key-color", default="auto")
    ap.add_argument("--similarity", type=float, default=0.15)
    ap.add_argument("--blend", type=float, default=0.05)
    ap.add_argument("--bg-blur", type=float, default=6)
    ap.add_argument("--no-despill", action="store_true")
    ap.add_argument("--preview")
    ap.add_argument("--at", type=float, default=1.0)
    ap.add_argument("--replace", action="store_true")
    ap.add_argument("--backup-dir")
    a = ap.parse_args()

    bg = Path(a.background)
    if not bg.is_file():
        die(f"background not found: {bg}")

    if a.work:
        work = Path(a.work)
        src = work / "final.mp4"
        if not src.is_file():
            die("no final.mp4 in the workdir — run the Dub step first")
        out = work / ("bg-preview.png" if a.preview
                      else f"bg-{time.strftime('%Y%m%d-%H%M%S')}.mp4")
        if a.preview:
            a.preview = str(out)
    else:
        if not a.input or not (a.output or a.preview or a.replace):
            die("standalone mode needs --input and --output / --replace (or --preview)")
        src = Path(a.input)
        if not src.is_file():
            die(f"input not found: {src}")
        if a.replace and not a.preview:
            if not a.backup_dir:
                die("--replace needs --backup-dir")
            # same container/extension so the in-place swap keeps the filename;
            # rendered inside the (dot-)backup dir so the library never lists it
            out = Path(a.backup_dir) / f"bg-tmp-{src.name}"
        else:
            out = Path(a.preview or a.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    key = a.key_color
    if key == "auto":
        key = detect_key_color(src, out.parent)
        print(f"[background-swap] key color: {key}", flush=True)

    w, h = probe_resolution(src)
    filt = build_filter(key, a, w, h)
    bg_is_video = bg.suffix.lower() in VIDEO_EXTS

    if a.preview:
        frame = out.parent / "bg-srcframe.png"
        run(["ffmpeg", "-y", "-v", "error", "-ss", str(a.at), "-i", src,
             "-frames:v", "1", "-update", "1", frame])
        run(["ffmpeg", "-y", "-v", "error", "-i", frame, "-i", bg,
             "-filter_complex", filt, "-map", "[out]",
             "-frames:v", "1", "-update", "1", out])
        print(f"[background-swap] preview -> {out}", flush=True)
        return

    bg_input = ["-stream_loop", "-1", "-i", bg] if bg_is_video else ["-loop", "1", "-i", bg]
    run(["ffmpeg", "-y", "-i", src, *bg_input,
         "-filter_complex", filt, "-map", "[out]", "-map", "0:a?",
         "-c:v", "libx264", "-crf", "18", "-preset", "medium",
         "-c:a", "aac", "-shortest", out])

    expected, got = probe_duration(src), probe_duration(out)
    if abs(got - expected) > 0.5:
        out.unlink(missing_ok=True)
        die(f"output {got:.2f}s != source {expected:.2f}s — take deleted, not delivered truncated")

    if not a.work and a.replace:
        import os
        import shutil
        backup_dir = Path(a.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / src.name
        if not backup.exists():                       # never clobber an older original
            shutil.copy2(src, backup)
            print(f"[background-swap] original backed up -> {backup}", flush=True)
        os.replace(out, src)
        print(f"[background-swap] source replaced in place -> {src}", flush=True)
        return

    if a.work:
        versions_file = Path(a.work) / "versions.json"
        try:
            versions = json.loads(versions_file.read_text(encoding="utf-8")) if versions_file.is_file() else {}
        except Exception:
            versions = {}
        versions[out.name] = {"created": time.time(), "repair": "background",
                              "background": bg.name, "key": key,
                              "similarity": a.similarity, "bg_blur": a.bg_blur}
        versions_file.write_text(json.dumps(versions, indent=1), encoding="utf-8")

    print(f"[background-swap] done -> {out}", flush=True)


if __name__ == "__main__":
    main()
