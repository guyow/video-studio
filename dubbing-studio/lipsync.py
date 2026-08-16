"""
Local lip-sync stage for the dubbing tool.
==============================================================================
Chains two on-GPU, offline models:
  1. Wav2Lip (GAN)  — drives the speaker's mouth from the dubbed audio
  2. GFPGAN v1.4    — restores the face/mouth region Wav2Lip leaves soft/blurry

Wav2Lip alone runs on 4 GB but produces a low-res (96px) mouth; GFPGAN is what
makes it look good. Both run locally — nothing is uploaded.

Public entry point:
    lipsync_video(face_video, audio_wav, out_video, enhance=True, log=print)
==============================================================================
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Reuse the existing, already-patched Wav2Lip + GFPGAN install that lives at
# <project root>/tools/Wav2Lip (shared with the autoVSL dashboard) rather than
# duplicating ~940 MB of model weights. Its inference.py is already patched for
# spaced paths + modern librosa, and its gfpgan/weights hold the aux models.
WAV2LIP_DIR = HERE.parent / "tools" / "Wav2Lip"
WAV2LIP_CKPT = WAV2LIP_DIR / "checkpoints" / "wav2lip_gan.pth"
GFPGAN_MODEL = WAV2LIP_DIR / "gfpgan_weights" / "GFPGANv1.4.pth"
# GFPGAN/facexlib look for their detection+parsing weights under
# "<cwd>/gfpgan/weights"; tools/Wav2Lip already has them, so run from there.
GFPGAN_CWD = WAV2LIP_DIR
# CodeFormer (codeformer-pip) resolves its weights from "CodeFormer/weights/*"
# RELATIVE to the CWD; the wrapper downloaded them under the project root.
CODEFORMER_CWD = HERE.parent


def lipsync_available() -> tuple[bool, str]:
    """Return (ok, message) describing whether the local models are present."""
    missing = [str(p) for p in (WAV2LIP_CKPT, GFPGAN_MODEL) if not p.is_file()]
    if missing:
        return False, "Missing lip-sync model(s):\n  " + "\n  ".join(missing)
    return True, "ok"


def _run_wav2lip(face_video, audio_wav, out_video, log,
                 pads=(0, 10, 0, 0), wav2lip_batch=16, facedet_batch=4):
    """Run Wav2Lip inference.py as a subprocess (isolates its global argparse
    + hparams). Small batch sizes keep it inside 4 GB of VRAM."""
    cmd = [
        sys.executable, "inference.py",
        "--checkpoint_path", str(WAV2LIP_CKPT),
        "--face", str(face_video),
        "--audio", str(audio_wav),
        "--outfile", str(out_video),
        "--pads", *[str(p) for p in pads],
        "--wav2lip_batch_size", str(wav2lip_batch),
        "--face_det_batch_size", str(facedet_batch),
        "--resize_factor", "1",
    ]
    log("  Wav2Lip: detecting face + syncing mouth to the dubbed audio…")
    env = dict(os.environ, PYTHONUTF8="1")
    r = subprocess.run(cmd, cwd=str(WAV2LIP_DIR), env=env,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not Path(out_video).is_file():
        tail = (r.stderr or r.stdout or "")[-1200:]
        raise RuntimeError("Wav2Lip failed:\n" + tail)


def _enhance_with_gfpgan(in_video, audio_wav, out_video, log, upscale=1):
    """Restore every frame's face with GFPGAN, then re-mux the dubbed audio."""
    import cv2
    from gfpgan import GFPGANer

    log("  GFPGAN: restoring the face/mouth detail Wav2Lip left soft…")
    # GFPGANer hardcodes a relative weights path; run from GFPGAN_CWD so the
    # pre-downloaded detection/parsing models are found (fully offline).
    prev = os.getcwd()
    frames_dir = Path(tempfile.mkdtemp(prefix="gfpgan_frames_"))
    try:
        os.chdir(GFPGAN_CWD)
        restorer = GFPGANer(model_path=str(GFPGAN_MODEL), upscale=upscale,
                            arch="clean", channel_multiplier=2, bg_upsampler=None)

        cap = cv2.VideoCapture(str(in_video))
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            _, _, restored = restorer.enhance(
                frame, has_aligned=False, only_center_face=False, paste_back=True)
            cv2.imwrite(str(frames_dir / f"{idx:06d}.png"), restored)
            idx += 1
            if idx % 25 == 0:
                log(f"    enhanced {idx}/{n or '?'} frames")
        cap.release()
        if idx == 0:
            raise RuntimeError("GFPGAN got no frames from the Wav2Lip output.")

        # Reassemble enhanced frames + the dubbed audio into the final mp4.
        cmd = [
            "ffmpeg", "-y",
            "-framerate", f"{fps}",
            "-i", str(frames_dir / "%06d.png"),
            "-i", str(audio_wav),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-shortest",
            str(out_video),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0 or not Path(out_video).is_file():
            raise RuntimeError("ffmpeg reassembly failed:\n" + (r.stderr or "")[-800:])
        log(f"    reassembled {idx} enhanced frames @ {fps:.2f} fps")
    finally:
        os.chdir(prev)
        with contextlib.suppress(Exception):
            import shutil
            shutil.rmtree(frames_dir, ignore_errors=True)


def _enhance_with_codeformer(in_video, audio_wav, out_video, log,
                             fidelity=0.7, upscale=1, bg_enhance=False):
    """Restore every frame's face with CodeFormer (optionally Real-ESRGAN
    full-frame upscale), then re-mux the dubbed audio. Loads the models once
    and reuses a single FaceRestoreHelper across frames (fast path)."""
    import cv2
    import torch
    from torchvision.transforms.functional import normalize

    log("  CodeFormer: restoring faces"
        + (f" + Real-ESRGAN {upscale}x upscale" if bg_enhance and upscale > 1 else "")
        + " (sharper, identity-faithful)…")
    prev = os.getcwd()
    frames_dir = Path(tempfile.mkdtemp(prefix="cf_frames_"))
    try:
        os.chdir(CODEFORMER_CWD)   # CodeFormer/weights/* are resolved relative to CWD
        # These globals are the models the wrapper loads once, at import.
        from codeformer.app import codeformer_net, upsampler, device
        from codeformer.basicsr.utils import img2tensor, tensor2img
        from codeformer.facelib.utils.face_restoration_helper import FaceRestoreHelper

        face_helper = FaceRestoreHelper(
            upscale, face_size=512, crop_ratio=(1, 1),
            det_model="retinaface_resnet50", save_ext="png",
            use_parse=True, device=device)
        bg_upsampler = upsampler if bg_enhance else None

        cap = cv2.VideoCapture(str(in_video))
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        # Fixed, even output size so every frame matches (libx264/yuv420p needs
        # even dims, and the image2 encoder needs all frames identical in size).
        base_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) * max(1, upscale)
        base_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) * max(1, upscale)
        tgt = (base_w - base_w % 2, base_h - base_h % 2)
        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            face_helper.clean_all()
            face_helper.read_image(frame)
            face_helper.get_face_landmarks_5(only_center_face=False, resize=640,
                                             eye_dist_threshold=5)
            face_helper.align_warp_face()
            for cropped in face_helper.cropped_faces:
                t = img2tensor(cropped / 255.0, bgr2rgb=True, float32=True)
                normalize(t, (0.5, 0.5, 0.5), (0.5, 0.5, 0.5), inplace=True)
                t = t.unsqueeze(0).to(device)
                try:
                    with torch.no_grad():
                        out = codeformer_net(t, w=fidelity, adain=True)[0]
                        restored = tensor2img(out, rgb2bgr=True, min_max=(-1, 1))
                    del out
                    torch.cuda.empty_cache()
                except RuntimeError as e:
                    log(f"    (codeformer fallback on a face: {e})")
                    restored = tensor2img(t, rgb2bgr=True, min_max=(-1, 1))
                face_helper.add_restored_face(restored.astype("uint8"))
            bg = bg_upsampler.enhance(frame, outscale=upscale)[0] if bg_upsampler else None
            face_helper.get_inverse_affine(None)
            result = face_helper.paste_faces_to_input_image(upsample_img=bg)
            if result.shape[1] != tgt[0] or result.shape[0] != tgt[1]:
                result = cv2.resize(result, tgt, interpolation=cv2.INTER_LANCZOS4)
            cv2.imwrite(str(frames_dir / f"{idx:06d}.png"), result)
            idx += 1
            if idx % 25 == 0:
                log(f"    enhanced {idx}/{n or '?'} frames")
        cap.release()
        if idx == 0:
            raise RuntimeError("CodeFormer got no frames from the Wav2Lip output.")

        cmd = [
            "ffmpeg", "-y", "-framerate", f"{fps}",
            "-i", str(frames_dir / "%06d.png"), "-i", str(audio_wav),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-shortest", str(out_video),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0 or not Path(out_video).is_file():
            raise RuntimeError("ffmpeg reassembly failed:\n" + (r.stderr or "")[-800:])
        log(f"    reassembled {idx} enhanced frames @ {fps:.2f} fps")
    finally:
        os.chdir(prev)
        with contextlib.suppress(Exception):
            import shutil
            shutil.rmtree(frames_dir, ignore_errors=True)


def lipsync_video(face_video, audio_wav, out_video, enhance=True, log=print,
                  restorer="gfpgan", upscale=1, fidelity=0.7,
                  wav2lip_batch=16, facedet_batch=4):
    """
    Lip-sync `face_video` to `audio_wav`, writing `out_video`.
      restorer="gfpgan"     -> Wav2Lip + GFPGAN            (sharp; default)
      restorer="codeformer" -> Wav2Lip + CodeFormer        (sharper/faithful;
                               upscale=2 adds Real-ESRGAN full-frame HD)
      restorer="none" or enhance=False -> Wav2Lip only     (fastest, soft mouth)
    """
    ok, msg = lipsync_available()
    if not ok:
        raise RuntimeError(msg)
    if not enhance:
        restorer = "none"

    out_video = Path(out_video)
    out_video.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        w2l_out = Path(tmp) / "wav2lip.mp4"
        _run_wav2lip(face_video, audio_wav, w2l_out, log,
                     wav2lip_batch=wav2lip_batch, facedet_batch=facedet_batch)
        if restorer == "codeformer":
            _enhance_with_codeformer(w2l_out, audio_wav, out_video, log,
                                     fidelity=fidelity, upscale=upscale,
                                     bg_enhance=upscale > 1)
        elif restorer == "gfpgan":
            _enhance_with_gfpgan(w2l_out, audio_wav, out_video, log)
        else:
            import shutil
            shutil.copy(str(w2l_out), str(out_video))
    log(f"  lip-synced video ready: {out_video.name}")
    return str(out_video)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Local Wav2Lip + GFPGAN lip-sync")
    ap.add_argument("--face", required=True, help="Input video (talking head)")
    ap.add_argument("--audio", required=True, help="Dubbed audio (.wav)")
    ap.add_argument("--out", required=True, help="Output mp4")
    ap.add_argument("--restorer", choices=["gfpgan", "codeformer", "none"],
                    default="gfpgan", help="Face restorer (default: gfpgan)")
    ap.add_argument("--upscale", type=int, default=1,
                    help="Real-ESRGAN full-frame upscale (codeformer only; e.g. 2)")
    ap.add_argument("--fidelity", type=float, default=0.7,
                    help="CodeFormer identity fidelity 0-1 (higher = more faithful)")
    ap.add_argument("--no-enhance", action="store_true", help="Wav2Lip only, skip restore")
    ap.add_argument("--wav2lip-batch", type=int, default=16,
                    help="Wav2Lip batch size (lower it on OOM; default 16 fits 4 GB)")
    ap.add_argument("--facedet-batch", type=int, default=4,
                    help="face-detector batch size (lower it on OOM; default 4)")
    a = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            s.reconfigure(encoding="utf-8")
    lipsync_video(a.face, a.audio, a.out, enhance=not a.no_enhance,
                  restorer=a.restorer, upscale=a.upscale, fidelity=a.fidelity,
                  wav2lip_batch=a.wav2lip_batch, facedet_batch=a.facedet_batch)
