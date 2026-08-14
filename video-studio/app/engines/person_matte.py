#!/usr/bin/env python3
"""Person matte — remove the REAL background from normal footage with local AI
and paint a clean green screen behind the person.

Called by background_swap.py --reverse when the input has no alpha channel;
also works standalone:

    python person_matte.py --input in.mp4 --output out.mp4 [--green-color 0x00FF00]
    python person_matte.py --input in.mp4 --preview out.png [--at 1.0]

--emit picks what is written:
    green  (default)  person composited on solid green
    mask              grayscale person mask (white = person) — used by
                      background_swap.py --protect-person to force the actor
                      fully opaque while keying
    alpha             person with a real transparent background:
                      .webm (VP9 + alpha, slow encode, small) or
                      .mov (ProRes 4444 + alpha, fast encode, big file)

Model: RobustVideoMatting (mobilenetv3) — built for video, so the matte is
temporally smooth (no frame-to-frame flicker like per-frame removers) and the
hair/edge alpha is soft and natural, not a hard AI-looking cutout. Runs on the
GPU in fp16 (fits 4GB VRAM at the internal 512px matting resolution), falls
back to CPU automatically. First run downloads the code + weights (~15 MB)
into video-studio/weights/torch-hub; after that it is fully offline.

Audio is copied through untouched from the source.
"""
import argparse
import subprocess
import sys
from pathlib import Path

WEIGHTS = Path(__file__).resolve().parents[2] / "weights" / "torch-hub"


def log(msg: str) -> None:
    print(f"[person-matte] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[person-matte] ERROR: {msg}", flush=True)
    sys.exit(1)


def parse_color(s: str) -> tuple:
    s = (s or "").strip().lstrip("#")
    if s.lower().startswith("0x"):
        s = s[2:]
    try:
        v = int(s, 16)
    except ValueError:
        die(f"bad --green-color {s!r} — use 0xRRGGBB")
    return ((v >> 16) & 255, (v >> 8) & 255, v & 255)


def probe_fps(path: Path) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    num, den = out.decode().strip().split("/")
    return float(num) / float(den or 1)


def load_model():
    import torch
    WEIGHTS.mkdir(parents=True, exist_ok=True)
    torch.hub.set_dir(str(WEIGHTS))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    log(f"loading RobustVideoMatting (device: {device})…")
    try:
        model = torch.hub.load("PeterL1n/RobustVideoMatting", "mobilenetv3",
                               pretrained=True, trust_repo=True)
    except Exception as exc:
        die(f"could not load the matting model ({exc}) — first run needs "
            "internet to download ~15 MB into weights/torch-hub")
    model = model.eval().to(device)
    if device == "cuda":
        model = model.half()
    return model, device


class Matter:
    """One recurrent matting stream: feed frames in order, get green composites."""

    def __init__(self, w: int, h: int, green: tuple):
        import torch
        self.torch = torch
        self.model, self.device = load_model()
        self.dtype = torch.float16 if self.device == "cuda" else torch.float32
        self.ds = min(512 / max(w, h), 1.0)   # RVM's recommended downsample
        self.bg = torch.tensor(green, device=self.device,
                               dtype=self.dtype).div(255).view(1, 3, 1, 1)
        self.rec = [None] * 4

    def _to_cpu(self):
        log("GPU out of memory — falling back to CPU (slower, same quality)")
        self.torch.cuda.empty_cache()
        self.model = self.model.float().cpu()
        self.device, self.dtype = "cpu", self.torch.float32
        self.bg = self.bg.float().cpu()
        self.rec = [None] * 4

    def process(self, rgb, emit="green"):
        """HxWx3 uint8 RGB in → uint8 out: HxWx3 person-on-green ('green'),
        HxW mask ('mask'), or HxWx4 RGBA ('alpha')."""
        torch = self.torch
        for attempt in range(2):
            try:
                with torch.no_grad():
                    src = (torch.from_numpy(rgb).to(self.device)
                           .permute(2, 0, 1).unsqueeze(0).to(self.dtype).div(255))
                    fgr, pha, *self.rec = self.model(src, *self.rec,
                                                     downsample_ratio=self.ds)
                    if emit == "mask":
                        return pha[0, 0].mul(255).clamp(0, 255).byte().cpu().numpy()
                    if emit == "alpha":
                        rgba = torch.cat([fgr[0], pha[0]], dim=0)
                        return (rgba.mul(255).clamp(0, 255).byte()
                                .permute(1, 2, 0).cpu().numpy())
                    com = fgr * pha + self.bg * (1 - pha)
                    return (com[0].mul(255).clamp(0, 255).byte()
                            .permute(1, 2, 0).cpu().numpy())
            except RuntimeError as exc:
                if attempt == 0 and self.device == "cuda" \
                        and "out of memory" in str(exc).lower():
                    self._to_cpu()
                    continue
                raise


def open_video(path: Path):
    import cv2
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        die(f"could not open {path}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    return cap, w, h


def matte_preview(src: Path, out_png: Path, at: float, green: tuple,
                  emit: str = "green") -> None:
    import cv2
    cap, w, h = open_video(src)
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, at) * 1000)
    ok, frame = cap.read()
    if not ok:
        cap.set(cv2.CAP_PROP_POS_MSEC, 0)
        ok, frame = cap.read()
    cap.release()
    if not ok:
        die("could not read a frame for the preview")
    m = Matter(w, h, green)
    out = m.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), emit)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    if emit == "mask":
        cv2.imwrite(str(out_png), out)
    elif emit == "alpha":
        cv2.imwrite(str(out_png), cv2.cvtColor(out, cv2.COLOR_RGBA2BGRA))
    else:
        cv2.imwrite(str(out_png), cv2.cvtColor(out, cv2.COLOR_RGB2BGR))
    log(f"preview -> {out_png}")


def encoder_cmd(emit: str, src: Path, out: Path, w: int, h: int, fps: float) -> list:
    raw = {"green": "rgb24", "mask": "gray", "alpha": "rgba"}[emit]
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", raw, "-s", f"{w}x{h}",
           "-r", f"{fps:.6f}", "-i", "-",
           "-i", str(src), "-map", "0:v"]
    if emit == "mask":     # mute internal mask: no audio, fast encode
        return cmd + ["-an", "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                      "-pix_fmt", "yuv420p", "-shortest", str(out)]
    cmd += ["-map", "1:a?"]
    if emit == "alpha":
        if out.suffix.lower() == ".mov":   # ProRes 4444: fast, edit-friendly, big
            return cmd + ["-c:v", "prores_ks", "-profile:v", "4444",
                          "-pix_fmt", "yuva444p10le", "-c:a", "pcm_s16le",
                          "-shortest", str(out)]
        return cmd + ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                      "-b:v", "0", "-crf", "24", "-row-mt", "1", "-cpu-used", "4",
                      "-c:a", "libopus", "-shortest", str(out)]
    return cmd + ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
                  "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)]


def matte_video(src: Path, out: Path, green: tuple, emit: str = "green") -> None:
    import cv2
    cap, w, h = open_video(src)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    fps = probe_fps(src)
    m = Matter(w, h, green)
    out.parent.mkdir(parents=True, exist_ok=True)
    enc = subprocess.Popen(encoder_cmd(emit, src, out, w, h, fps),
                           stdin=subprocess.PIPE)
    n = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            enc.stdin.write(m.process(rgb, emit).tobytes())
            n += 1
            if n % 100 == 0:
                log(f"{n}/{total or '?'} frames")
    finally:
        cap.release()
        enc.stdin.close()
    if enc.wait() != 0 or n == 0:
        out.unlink(missing_ok=True)
        die("encode failed — nothing written")
    log(f"done — {n} frames -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output")
    ap.add_argument("--green-color", default="0x00FF00")
    ap.add_argument("--emit", choices=["green", "mask", "alpha"], default="green")
    ap.add_argument("--preview")
    ap.add_argument("--at", type=float, default=1.0)
    a = ap.parse_args()

    src = Path(a.input)
    if not src.is_file():
        die(f"input not found: {src}")
    green = parse_color(a.green_color)
    if a.preview:
        matte_preview(src, Path(a.preview), a.at, green, a.emit)
    elif a.output:
        matte_video(src, Path(a.output), green, a.emit)
    else:
        die("need --output or --preview")


if __name__ == "__main__":
    main()
