#!/usr/bin/env python3
"""Photoreal head performance transfer — LivePortrait on the local GPU, free.

    python mocap_liveportrait.py --face person.jpg --driving video.mp4 --out out.mp4

A photo of a real person + a driving video of an actor → the person in the
photo delivers the performance: head motion, expressions, blinks, lips. This is
Kuaishou's LivePortrait (the tech behind Douyin/WeChat portrait animation),
running through ComfyUI-LivePortraitKJ on the local card — no API cost.

Face/head only: the body in the photo stays still. For full-body transfer use
the paid Wan Animate route. The FaceAlignment cropper is used (no InsightFace,
no non-commercial license problem; the py3.13 mediapipe wheel is tasks-only so
the MediaPipe cropper can't load).

The driving video is downscaled to 512w for motion extraction (landmarks don't
need resolution) and capped at 30fps; the OUTPUT renders at the photo's own
resolution and the original audio is muxed back at the end.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

FFMPEG_BIN = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1.2-full_build/bin"
)
AUTOVSL_SCRIPTS = Path.home() / "Claude/Projects/Video AI editing/autoVSL/scripts"


def ff(name: str) -> str:
    exe = FFMPEG_BIN / f"{name}.exe"
    return str(exe) if exe.is_file() else name


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def probe(path: Path, entries: str, stream: str = "v:0") -> str:
    r = run([ff("ffprobe"), "-v", "error", "-select_streams", stream,
             "-show_entries", entries, "-of", "default=nw=1:nk=1", str(path)])
    return (r.stdout or "").strip().splitlines()[0] if (r.stdout or "").strip() else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--face", required=True, help="photo of the real person")
    ap.add_argument("--driving", required=True, help="video whose performance is copied")
    ap.add_argument("--out", required=True)
    ap.add_argument("--comfy", default="127.0.0.1:8188")
    ap.add_argument("--max-seconds", type=float, default=90.0)
    a = ap.parse_args()

    face, driving, out = Path(a.face), Path(a.driving), Path(a.out)
    if not face.is_file():
        print(f"ERROR: no such photo {face}", flush=True)
        return 1
    if not driving.is_file():
        print(f"ERROR: no such video {driving}", flush=True)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(AUTOVSL_SCRIPTS))
    try:
        from comfyui_client import ComfyUIClient, ComfyUIError
    except ImportError:
        print("ERROR: comfyui_client.py not found in autoVSL/scripts", flush=True)
        return 1

    c = ComfyUIClient(a.comfy, timeout=30)
    if not c.ping():
        print(f"ERROR: ComfyUI is not running at {a.comfy} — "
              "launch it first (start-servers.bat)", flush=True)
        return 1

    try:
        dur = float(probe(driving, "format=duration") or 0)
    except ValueError:
        dur = 0.0
    if dur <= 0.2:
        print("ERROR: could not read the driving video duration", flush=True)
        return 1
    if dur > a.max_seconds:
        print(f"ERROR: driving video is {dur:.0f}s — keep it under "
              f"{a.max_seconds:.0f}s per run (frames are held in RAM)", flush=True)
        return 1
    fr = probe(driving, "stream=r_frame_rate") or "30/1"
    try:
        n, _, d = fr.partition("/")
        fps = float(n) / float(d or 1)
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    force_rate = 30 if fps > 32 else 0
    out_fps = force_rate or fps
    has_audio = bool(probe(driving, "stream=index", "a:0"))
    print(f"driving {dur:.1f}s @ {fps:g}fps → render @ {out_fps:g}fps · "
          f"~{int(dur * out_fps)} frames", flush=True)

    # the photo drives the OUTPUT resolution — cap it so RAM survives
    work = out.parent / f".{out.stem}-work"
    work.mkdir(parents=True, exist_ok=True)
    face_small = work / "face.png"
    r = run([ff("ffmpeg"), "-y", "-v", "error", "-i", str(face),
             "-vf", "scale='min(1280,iw)':-2", str(face_small)])
    if r.returncode != 0 or not face_small.is_file():
        print(f"ERROR: could not prepare the photo:\n{(r.stderr or '')[-400:]}", flush=True)
        return 1
    face_name = c.upload_image(face_small)
    print(f"photo staged as {face_name}", flush=True)

    wf = {
        "1": {"class_type": "DownloadAndLoadLivePortraitModels",
              "inputs": {"precision": "fp16", "mode": "human"}},
        "2": {"class_type": "LivePortraitLoadFaceAlignmentCropper",
              "inputs": {"face_detector": "blazeface_back_camera",
                         "landmarkrunner_device": "CPU",   # onnx; the .pth pickle trips torch>=2.6 weights_only
                         "face_detector_device": "cuda",
                         "face_detector_dtype": "fp16",
                         "keep_model_loaded": False}},
        "3": {"class_type": "LoadImage", "inputs": {"image": face_name}},
        "4": {"class_type": "VHS_LoadVideoPath",
              "inputs": {"video": str(driving.resolve()), "force_rate": force_rate,
                         "custom_width": 512, "custom_height": 0, "frame_load_cap": 0,
                         "skip_first_frames": 0, "select_every_nth": 1, "format": "None"}},
        "5": {"class_type": "LivePortraitCropper",
              "inputs": {"pipeline": ["1", 0], "cropper": ["2", 0],
                         "source_image": ["3", 0], "dsize": 512, "scale": 2.3,
                         "vx_ratio": 0.0, "vy_ratio": -0.125, "face_index": 0,
                         "face_index_order": "large-small", "rotate": True}},
        "6": {"class_type": "LivePortraitProcess",
              "inputs": {"pipeline": ["1", 0], "crop_info": ["5", 1],
                         "source_image": ["3", 0], "driving_images": ["4", 0],
                         "lip_zero": False, "lip_zero_threshold": 0.03,
                         "stitching": True, "delta_multiplier": 1.0,
                         "mismatch_method": "constant",
                         "relative_motion_mode": "relative",
                         "driving_smooth_observation_variance": 3e-06}},
        "7": {"class_type": "LivePortraitComposite",
              "inputs": {"source_image": ["3", 0], "cropped_image": ["6", 0],
                         "liveportrait_out": ["6", 1]}},
        "8": {"class_type": "VHS_VideoCombine",
              "inputs": {"images": ["7", 0], "frame_rate": round(out_fps, 3),
                         "loop_count": 0, "filename_prefix": "mocap-lp",
                         "format": "video/h264-mp4", "pix_fmt": "yuv420p",
                         "crf": 19, "save_metadata": False, "trim_to_audio": False,
                         "pingpong": False, "save_output": True}},
    }

    print("rendering on the local GPU… (first run also loads the models)", flush=True)
    t0 = time.time()
    beat = threading.Event()

    def heartbeat():
        while not beat.wait(20):
            print(f"  …still rendering ({time.time() - t0:.0f}s)", flush=True)

    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        files = c.run(wf, poll=2.0, max_wait=3600)
    except ComfyUIError as exc:
        beat.set()
        print(f"ERROR: ComfyUI run failed: {str(exc)[:600]}", flush=True)
        return 1
    finally:
        beat.set()
    vids = [f for f in files if f["filename"].lower().endswith((".mp4", ".webm", ".mov"))]
    if not vids:
        print(f"ERROR: ComfyUI produced no video (outputs: {files})", flush=True)
        return 1
    render = work / "render.mp4"
    c.download(vids[-1], render)
    print(f"rendered in {time.time() - t0:.0f}s — muxing the original audio", flush=True)

    cmd = [ff("ffmpeg"), "-y", "-v", "error", "-i", str(render)]
    if has_audio:
        cmd += ["-i", str(driving), "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", str(out)]
    r = run(cmd)
    if r.returncode != 0 or not out.is_file():
        print(f"ERROR: audio mux failed:\n{(r.stderr or '')[-400:]}", flush=True)
        return 1

    got = float(probe(out, "format=duration") or 0)
    print(f"verify: {got:.2f}s (driving {dur:.2f}s)", flush=True)
    if got < 0.2:
        out.unlink(missing_ok=True)
        print("ERROR: empty output — take deleted, not delivered", flush=True)
        return 1
    for p in (face_small, render):
        p.unlink(missing_ok=True)
    try:
        work.rmdir()
    except OSError:
        pass
    print(f"RESULT: {out.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
