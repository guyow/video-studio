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
    --key-color auto|0xRRGGBB   auto samples the frame borders — real screens
                                are never pure 0x00FF00
    --similarity 0.15           how far from the key still counts as green.
                                Keep under ~0.25: chromakey ignores brightness,
                                so big values start eating whites (shirts, teeth)
    --blend 0.05                edge softness
    --fill-holes 2              morphological closing passes on the alpha mask —
                                fills small transparent holes that open up INSIDE
                                the actor when their colours drift near the key
    --bg-blur 6                 gaussian blur on the background; fakes camera
                                depth of field and hides key imperfections
    --no-despill                keep the green light bounce (default: remove it)

Reverse mode (--reverse): person → GREEN SCREEN instead of scene → person.
    If the input already has a transparent background (alpha channel, e.g. a
    .webm/.mov from an AI background remover) it is flattened onto solid green
    with plain ffmpeg. If it is normal footage, person_matte.py (local AI,
    RobustVideoMatting) removes the real background and paints green.
    --green-color 0x00FF00      the screen colour painted behind the person
    --background is not needed in this mode.
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


def _border_patch_origins(w: int, h: int, s: int = 24) -> list:
    """8 patches along the frame border: 4 corners + 4 edge midpoints. The old
    corners-only probe fell back to pure 0x00FF00 whenever the actor (or a
    vignette) covered a corner — the key landed far from the real screen colour,
    the user cranked similarity to compensate, and the actor went transparent."""
    return [(0, 0), (w - s, 0), (0, h - s), (w - s, h - s),
            (w // 2 - s // 2, 0), (w // 2 - s // 2, h - s),
            (0, h // 2 - s // 2), (w - s, h // 2 - s // 2)]


def detect_key_color(video: Path, tmp_dir: Path) -> str:
    """Average the green-dominant 24x24 border patches of the first frame."""
    default = "0x00FF00"
    frame = tmp_dir / "bg-keyprobe.png"
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video),
                        "-frames:v", "1", "-update", "1", str(frame)], check=True)
        try:
            import cv2
            img = cv2.imread(str(frame))          # BGR
            h, w = img.shape[:2]
            patches = []
            for x0, y0 in _border_patch_origins(w, h):
                c = img[y0:y0 + 24, x0:x0 + 24]
                b, g, r = (float(c[..., i].mean()) for i in range(3))
                if g > r * 1.2 and g > b * 1.2 and g > 60:
                    patches.append((r, g, b))
        except ImportError:
            from PIL import Image
            img = Image.open(frame).convert("RGB")
            w, h = img.size
            patches = []
            for x0, y0 in _border_patch_origins(w, h):
                px = [img.getpixel((x, y)) for x in range(x0, x0 + 24) for y in range(y0, y0 + 24)]
                r, g, b = (sum(p[i] for p in px) / len(px) for i in range(3))
                if g > r * 1.2 and g > b * 1.2 and g > 60:
                    patches.append((r, g, b))
        if not patches:
            print(f"[background-swap] no green-dominant border patch found; using {default}", flush=True)
            return default
        r, g, b = (int(sum(p[i] for p in patches) / len(patches)) for i in range(3))
        return f"0x{r:02X}{g:02X}{b:02X}"
    except Exception as exc:
        print(f"[background-swap] key auto-detect failed ({exc}); using {default}", flush=True)
        return default


def build_filter(key: str, a, w: int, h: int, mask: bool = False,
                 ai: bool = False) -> str:
    bg = f"[1:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    if a.bg_blur:
        bg += f",gblur=sigma={a.bg_blur}"
    bg += ",setsar=1[bg]"
    if ai:
        # full AI key: the person matte IS the alpha — no chromakey at all, so
        # nothing colour-based can ever eat the person (works without a green
        # screen too); the light blur feathers the edge like a real camera edge
        fg = (f"[2:v]format=gray,scale={w}:{h},gblur=sigma=1.2[a];"
              f"[0:v][a]alphamerge")
    else:
        fg = f"[0:v]chromakey={key}:{a.similarity}:{a.blend}"
        holes = max(0, min(int(a.fill_holes or 0), 6))
        if mask or holes:
            fg += "[keyed];[keyed]split[k][m];[m]alphaextract[ca];"
            cur = "[ca]"
            if mask:
                # the AI person mask (input 2) wins over the chromakey: wherever
                # the person is, alpha is forced opaque — the key can only
                # remove screen
                fg += (f"[2:v]format=gray,scale={w}:{h}[pm];"
                       f"{cur}[pm]blend=all_mode=lighten[cm];")
                cur = "[cm]"
            # closing fills small transparent holes inside the actor; the light
            # blur feathers the edge so the cutout reads as a real camera edge
            ops = (["dilation"] * holes + ["erosion"] * holes) if holes else []
            ops.append("gblur=sigma=1.2")
            fg += f"{cur}{','.join(ops)}[a];[k][a]alphamerge"
    if not a.no_despill:
        fg += ",despill=type=green"
    fg += "[fg]"
    return f"{bg};{fg};[bg][fg]overlay=shortest=1:format=auto,format=yuv420p[out]"


def probe_video_stream(path: Path) -> dict:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,pix_fmt:stream_tags=alpha_mode",
        "-of", "json", str(path)])
    return (json.loads(out).get("streams") or [{}])[0]


ALPHA_PIX_FMTS = {"rgba", "argb", "bgra", "abgr", "ya8", "ya16le", "ya16be",
                  "gbrap", "gbrap10le", "gbrap12le", "gbrap16le", "pal8"}


def alpha_input_args(path: Path) -> list | None:
    """Decoder args for a source with a transparent background, or None if the
    source has no alpha. VP8/VP9 .webm hides its alpha in side data — ffprobe
    reports plain yuv420p + an alpha_mode tag, and the native decoder DROPS the
    alpha, so those must be forced through libvpx."""
    st = probe_video_stream(path)
    fmt = st.get("pix_fmt", "")
    codec = st.get("codec_name", "")
    if (st.get("tags") or {}).get("alpha_mode") == "1":
        dec = "libvpx-vp9" if codec == "vp9" else "libvpx"
        return ["-c:v", dec]
    if fmt.startswith("yuva") or fmt in ALPHA_PIX_FMTS:
        return []
    return None


def norm_color(s: str) -> str:
    s = (s or "").strip().lstrip("#")
    if s.lower().startswith("0x"):
        s = s[2:]
    if len(s) != 6 or any(c not in "0123456789abcdefABCDEF" for c in s):
        die(f"bad --green-color {s!r} — use 0xRRGGBB, e.g. 0x00FF00")
    return "0x" + s.upper()


def render_reverse(src: Path, out: Path, a) -> None:
    """Person → green screen. Alpha sources are flattened with ffmpeg; opaque
    sources go through person_matte.py (local AI background removal)."""
    green = norm_color(a.green_color)
    dec = alpha_input_args(src)
    if dec is None:
        matte = Path(__file__).with_name("person_matte.py")
        print("[background-swap] no alpha channel — AI matting via person_matte.py", flush=True)
        cmd = [sys.executable, str(matte), "--input", str(src), "--green-color", green]
        cmd += ["--preview", str(a.preview), "--at", str(a.at)] if a.preview \
            else ["--output", str(out)]
        run(cmd)
        return
    w, h = probe_resolution(src)
    filt = (f"color=c={green}:s={w}x{h},setsar=1[bg];"
            f"[bg][0:v]overlay=shortest=1:format=auto,format=yuv420p[out]")
    if a.preview:
        run(["ffmpeg", "-y", "-v", "error", "-ss", str(a.at), *dec, "-i", src,
             "-filter_complex", filt, "-map", "[out]",
             "-frames:v", "1", "-update", "1", a.preview])
        print(f"[background-swap] preview -> {a.preview}", flush=True)
        return
    run(["ffmpeg", "-y", *dec, "-i", src,
         "-filter_complex", filt, "-map", "[out]", "-map", "0:a?",
         "-c:v", "libx264", "-crf", "18", "-preset", "medium",
         "-c:a", "aac", "-shortest", out])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work")
    ap.add_argument("--input")
    ap.add_argument("--output")
    ap.add_argument("--background")
    ap.add_argument("--key-color", default="auto")
    ap.add_argument("--similarity", type=float, default=0.15)
    ap.add_argument("--blend", type=float, default=0.05)
    ap.add_argument("--fill-holes", type=int, default=2)
    ap.add_argument("--protect-person", action="store_true")
    ap.add_argument("--ai-key", action="store_true")
    ap.add_argument("--bg-blur", type=float, default=6)
    ap.add_argument("--no-despill", action="store_true")
    ap.add_argument("--reverse", action="store_true")
    ap.add_argument("--green-color", default="0x00FF00")
    ap.add_argument("--preview")
    ap.add_argument("--at", type=float, default=1.0)
    ap.add_argument("--replace", action="store_true")
    ap.add_argument("--backup-dir")
    ap.add_argument("--promote", action="store_true")
    a = ap.parse_args()

    if a.reverse:
        bg = None
    else:
        if not a.background:
            die("--background is required (or use --reverse for person → green screen)")
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

    if a.reverse:
        render_reverse(src, out, a)
        if a.preview:
            return
        key = norm_color(a.green_color)
    else:
        if a.ai_key:
            key = "ai"
        else:
            key = a.key_color
            if key == "auto":
                key = detect_key_color(src, out.parent)
                print(f"[background-swap] key color: {key}", flush=True)

        w, h = probe_resolution(src)
        mask_path = None
        if a.protect_person or a.ai_key:
            matte = Path(__file__).with_name("person_matte.py")
            if a.preview:
                mask_path = out.parent / "bg-personmask.png"
                run([sys.executable, str(matte), "--input", str(src),
                     "--emit", "mask", "--preview", str(mask_path), "--at", str(a.at)])
            else:
                # .mkv so the take-listing (*.mp4) never shows the temp mask
                mask_path = out.parent / "bg-personmask-tmp.mkv"
                print("[background-swap] building the AI person shield — "
                      "the key will not touch the person", flush=True)
                run([sys.executable, str(matte), "--input", str(src),
                     "--emit", "mask", "--output", str(mask_path)])
        filt = build_filter(key, a, w, h, mask=bool(mask_path), ai=a.ai_key)
        bg_is_video = bg.suffix.lower() in VIDEO_EXTS

        if a.preview:
            frame = out.parent / "bg-srcframe.png"
            run(["ffmpeg", "-y", "-v", "error", "-ss", str(a.at), "-i", src,
                 "-frames:v", "1", "-update", "1", frame])
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", frame, "-i", bg]
            if mask_path:
                cmd += ["-i", mask_path]
            run(cmd + ["-filter_complex", filt, "-map", "[out]",
                       "-frames:v", "1", "-update", "1", out])
            print(f"[background-swap] preview -> {out}", flush=True)
            return

        bg_input = ["-stream_loop", "-1", "-i", bg] if bg_is_video else ["-loop", "1", "-i", bg]
        cmd = ["ffmpeg", "-y", "-i", src, *bg_input]
        if mask_path:
            cmd += ["-i", mask_path]
        run(cmd + ["-filter_complex", filt, "-map", "[out]", "-map", "0:a?",
                   "-c:v", "libx264", "-crf", "18", "-preset", "medium",
                   "-c:a", "aac", "-shortest", out])
        if mask_path:
            mask_path.unlink(missing_ok=True)

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
        if a.reverse:
            versions[out.name] = {"created": time.time(), "repair": "green-screen",
                                  "green": key}
        else:
            versions[out.name] = {"created": time.time(), "repair": "background",
                                  "background": bg.name, "key": key,
                                  "similarity": a.similarity, "bg_blur": a.bg_blur,
                                  "protected": bool(a.protect_person)}
        if a.promote:
            # "Replace background" should REPLACE what the user sees: crown the
            # fresh take as final.mp4, archive the previous final as a take
            work = Path(a.work)
            final = work / "final.mp4"
            versions.pop(out.name, None)
            if final.is_file():
                arch = work / f"final.{time.strftime('%Y%m%d-%H%M%S')}.mp4"
                final.rename(arch)
                try:
                    cfg = json.loads((work / "dub-config.json").read_text(encoding="utf-8"))
                except Exception:
                    cfg = {}
                versions[arch.name] = {"tts": cfg.get("tts"), "tier": cfg.get("tier"),
                                       "created": time.time()}
                print(f"[background-swap] previous final archived -> {arch.name}", flush=True)
            out.rename(final)
            print("[background-swap] promoted -> final.mp4 — the dubbed video now "
                  "carries the new background", flush=True)
        versions_file.write_text(json.dumps(versions, indent=1), encoding="utf-8")

    print(f"[background-swap] done -> {out}", flush=True)


if __name__ == "__main__":
    main()
