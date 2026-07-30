#!/usr/bin/env python3
"""Subtitle region cleaner v2 — per-frame OpenCV processing (replaces ffmpeg delogo).

Modes:
  smart  cv2.inpaint (Telea) — reconstructs the region from its surroundings
  blur   strong gaussian blur on the region
  bar    semi-transparent dark caption backdrop (new captions sit on top)

Usage:
  preview:  subclean.py <video> --box X Y W H --mode smart --preview-at 2.0 --out frame.jpg
  full run: subclean.py <video> --box X Y W H --mode smart --out cleaned.mp4
Audio is copied untouched on full runs.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

PAD = 8  # inpaint context margin around the box


def ffmpeg_exe(name: str) -> str:
    local = (Path(os.environ.get("LOCALAPPDATA", ""))
             / "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
             / "ffmpeg-8.1.2-full_build/bin" / f"{name}.exe")
    return str(local) if local.is_file() else name


def probe(video: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        [ffmpeg_exe("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True).stdout.strip().split(",")
    num, _, den = out[2].partition("/")
    return int(out[0]), int(out[1]), float(num) / float(den or 1)


def clamp_box(x: int, y: int, w: int, h: int, vw: int, vh: int) -> tuple[int, int, int, int]:
    x = max(0, min(x, vw - 4))
    y = max(0, min(y, vh - 4))
    return x, y, max(4, min(w, vw - x)), max(4, min(h, vh - y))


def _dissolve(roi: np.ndarray) -> np.ndarray:
    """Downscale-blur-upscale: bold caption text cannot survive a 24x downscale."""
    h, w = roi.shape[:2]
    small = cv2.resize(roi, (max(2, w // 24), max(2, h // 24)), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (5, 5), 0)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)


def process_frame(frame: np.ndarray, box: tuple[int, int, int, int], mode: str) -> np.ndarray:
    x, y, w, h = box
    if mode == "smart":
        # inpaint only a padded ROI — fast and keeps context local
        x0, y0 = max(0, x - PAD * 6), max(0, y - PAD * 6)
        x1, y1 = min(frame.shape[1], x + w + PAD * 6), min(frame.shape[0], y + h + PAD * 6)
        roi = frame[y0:y1, x0:x1]
        mask = np.zeros(roi.shape[:2], np.uint8)
        mask[y - y0:y - y0 + h, x - x0:x - x0 + w] = 255
        frame[y0:y1, x0:x1] = cv2.inpaint(roi, mask, 7, cv2.INPAINT_TELEA)
    elif mode == "blur":
        frame[y:y + h, x:x + w] = _dissolve(frame[y:y + h, x:x + w])
    elif mode == "bar":
        dissolved = _dissolve(frame[y:y + h, x:x + w])
        dark = np.zeros_like(dissolved)
        frame[y:y + h, x:x + w] = cv2.addWeighted(dissolved, 0.30, dark, 0.70, 0)
    return frame


def render_preview(video: Path, box, mode: str, at: float, out: Path) -> None:
    tmp = out.with_suffix(".src.png")
    subprocess.run([ffmpeg_exe("ffmpeg"), "-y", "-ss", str(at), "-i", str(video),
                    "-frames:v", "1", str(tmp)], capture_output=True, check=True)
    frame = cv2.imread(str(tmp))
    tmp.unlink(missing_ok=True)
    if frame is None:
        sys.exit("could not extract preview frame")
    frame = process_frame(frame, box, mode)
    cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
    print(f"preview -> {out}")


def render_full(video: Path, box, mode: str, out: Path) -> None:
    vw, vh, fps = probe(video)
    dec = subprocess.Popen(
        [ffmpeg_exe("ffmpeg"), "-v", "error", "-i", str(video),
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, bufsize=vw * vh * 3 * 4)
    tmp_v = out.with_suffix(".video.mp4")
    enc = subprocess.Popen(
        [ffmpeg_exe("ffmpeg"), "-y", "-v", "error",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{vw}x{vh}", "-r", f"{fps:.4f}", "-i", "-",
         "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-pix_fmt", "yuv420p", str(tmp_v)],
        stdin=subprocess.PIPE)
    n, size = 0, vw * vh * 3
    while True:
        raw = dec.stdout.read(size)
        if len(raw) < size:
            break
        frame = np.frombuffer(raw, np.uint8).reshape(vh, vw, 3).copy()
        enc.stdin.write(process_frame(frame, box, mode).tobytes())
        n += 1
        if n % 150 == 0:
            print(f"  {n} frames…", flush=True)
    dec.stdout.close()
    enc.stdin.close()
    dec.wait()
    if enc.wait() != 0:
        sys.exit("video encode failed")
    print(f"  {n} frames processed ({mode})")
    # mux the untouched original audio back in
    r = subprocess.run([ffmpeg_exe("ffmpeg"), "-y", "-v", "error", "-i", str(tmp_v),
                        "-i", str(video), "-map", "0:v:0", "-map", "1:a:0?",
                        "-c", "copy", str(out)], capture_output=True, text=True)
    tmp_v.unlink(missing_ok=True)
    if r.returncode != 0 or not out.is_file():
        sys.exit(f"audio mux failed: {(r.stderr or '')[-300:]}")
    print(f"cleaned -> {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--box", nargs=4, type=int, required=True, metavar=("X", "Y", "W", "H"))
    ap.add_argument("--mode", choices=["smart", "blur", "bar"], default="smart")
    ap.add_argument("--preview-at", type=float, help="render a single preview frame at this second")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    video, out = Path(args.video), Path(args.out)
    if not video.is_file():
        sys.exit(f"not found: {video}")
    vw, vh, _ = probe(video)
    box = clamp_box(*args.box, vw, vh)
    print(f"{args.mode}: box {box} in {vw}x{vh}")

    if args.preview_at is not None:
        render_preview(video, box, args.mode, args.preview_at, out)
    else:
        render_full(video, box, args.mode, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
