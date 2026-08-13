#!/usr/bin/env python3
"""Local dub pipeline: clone the speaker's voice and speak the edited script entirely
offline with Coqui XTTS v2 (the standalone dubbing-studio tool), then optionally add a
cloud lip-sync. The local voice stage costs nothing and needs no network.

Layout mirrors the cloud dub (dub.py) so the dashboard treats both identically:
work dir = output/script-swap/<name>/, deliverable = work/final.mp4 (+ READY_DIR copy).

Usage:
  python dashboard/local_dub.py <video> --name <stem> [--lipsync none|latentsync|veed|standard|pro]

Only --lipsync != none spends money (fal.ai); the caller must gate that with approval.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent            # autoVSL/
DUBBING_STUDIO = ROOT.parent / "dubbing-studio"          # sibling local voice-clone tool
DS_PY = DUBBING_STUDIO / "venv" / "Scripts" / "python.exe"
DS_APP = DUBBING_STUDIO / "app.py"
SWAP = ROOT / "scripts" / "script-swap.py"
READY_DIR = Path("~/Desktop/liitt testimonial Ready").expanduser()
FAL_TIERS = {"latentsync", "musetalk", "veed", "standard", "pro", "hummingbird", "sync3"}

# local lip-sync — dubbing-studio's engine (Wav2Lip + optional GFPGAN restore).
# It tolerates frames where the face is small/turned/occluded (reuses the last
# good detection); the stock tools/Wav2Lip inference.py aborts on those frames.
DS_LIPSYNC = DUBBING_STUDIO / "lipsync.py"
WAV2LIP_DIR = ROOT.parent / "tools" / "Wav2Lip"
WAV2LIP_CKPT = WAV2LIP_DIR / "checkpoints" / "wav2lip_gan.pth"
FFMPEG_BIN = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1.2-full_build/bin"
)


def _w2l_env() -> dict:
    env = dict(os.environ)
    if FFMPEG_BIN.is_dir():
        env["PATH"] = str(FFMPEG_BIN) + os.pathsep + env.get("PATH", "")
    env["PYTHONUTF8"] = "1"
    return env


def _media_dur(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True, env=_w2l_env())
    try:
        return float((r.stdout or "0").strip() or 0)
    except ValueError:
        return 0.0


def _speech_end(wav: Path) -> float:
    """When the voice actually stops (XTTS pads with silence to the video length)."""
    r = subprocess.run(["ffmpeg", "-i", str(wav), "-af", "silencedetect=n=-35dB:d=0.8",
                        "-f", "null", "-"], capture_output=True, text=True, env=_w2l_env())
    dur = _media_dur(wav)
    starts = re.findall(r"silence_start: ([0-9.]+)", r.stderr or "")
    ends = re.findall(r"silence_end: ([0-9.]+)", r.stderr or "")
    if starts and len(ends) < len(starts) and float(starts[-1]) > 3.0:
        return float(starts[-1])       # the last silence runs to EOF → speech ends there
    return dur


def run_lipsync(video: Path, audio: Path, out: Path, hd: bool) -> None:
    """Local GPU lip-sync via dubbing-studio's engine. Free, offline.
    hd=True adds the GFPGAN face-restoration pass (sharp mouth).
    Like the cloud pipeline's cut_off mode, the deliverable ends when the voice
    ends — so trailing product end-cards (no face) never break face detection."""
    if not DS_LIPSYNC.is_file():
        sys.exit(f"lip-sync engine missing at {DS_LIPSYNC}")
    wav = audio
    if audio.suffix.lower() != ".wav":               # the engine expects a wav
        wav = out.parent / "lipsync-audio.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio),
                        "-vn", "-ac", "1", "-ar", "16000", str(wav)], env=_w2l_env())
        if not wav.is_file():
            sys.exit("could not extract audio for lip-sync")

    cut = min(_speech_end(wav) + 0.4, _media_dur(wav) or 1e9, _media_dur(video) or 1e9)
    vdur = _media_dur(video)
    tmp = []
    if vdur and cut < vdur - 0.5:
        log(f"  voice ends at {cut - 0.4:.1f}s of {vdur:.1f}s footage — cutting off the "
            "silent tail (end-card) like the cloud pipeline does")
        cv = out.parent / "lipsync-face-cut.mp4"
        ca = out.parent / "lipsync-audio-cut.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
                        "-t", f"{cut:.2f}", "-an", "-c:v", "libx264", "-crf", "16",
                        "-preset", "veryfast", str(cv)], env=_w2l_env())
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
                        "-t", f"{cut:.2f}", str(ca)], env=_w2l_env())
        if cv.is_file() and cv.stat().st_size and ca.is_file():
            video, wav, tmp = cv, ca, [cv, ca]
    r = subprocess.run(
        [str(DS_PY), str(DS_LIPSYNC), "--face", str(video), "--audio", str(wav),
         "--out", str(out), "--restorer", "gfpgan" if hd else "none",
         "--upscale", "1", "--fidelity", "0.7"],
        cwd=str(DUBBING_STUDIO), env=_w2l_env(),
    )
    for p in tmp:
        p.unlink(missing_ok=True)
    if r.returncode != 0 or not out.is_file():
        sys.exit("local lip-sync failed")


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--name", required=True)
    ap.add_argument("--lipsync",
                    choices=["none", "wav2lip", "wav2lip-hd", "latentsync", "musetalk",
                             "veed", "standard", "pro", "hummingbird", "sync3"],
                    default="none")
    ap.add_argument("--language", default="en")
    ap.add_argument("--keep-volume", type=float, default=0.0,
                    help="mix the original audio back in at this volume 0.0-1.0 (music/ambience)")
    ap.add_argument("--voice-ref", default=None,
                    help="clone THIS reference audio (a saved Voice Bank voice) instead of the "
                         "on-screen speaker — the video is still the visual/lip-sync base")
    args = ap.parse_args()

    video = Path(args.video).resolve()
    if not video.is_file():
        sys.exit(f"Video not found: {video}")
    if args.lipsync in FAL_TIERS:
        # paid fal lip-sync comes AFTER minutes of local XTTS — check the account
        # can pay NOW so a dead key never wastes the whole voice stage
        from fal_guard import preflight_fal
        preflight_fal("local dub + fal lip-sync")
    if not DS_PY.is_file() or not DS_APP.is_file():
        sys.exit(f"Local dubbing tool not found at {DUBBING_STUDIO} — expected venv + app.py.\n"
                 "This is the standalone XTTS voice-clone tool.")

    work = ROOT / "output" / "script-swap" / args.name
    work.mkdir(parents=True, exist_ok=True)
    script = work / "script-edited.txt"
    if not script.is_file():
        sys.exit("No edited script found — use 'Edit script' in the dashboard and save first.")

    final = work / "final.mp4"
    ready = READY_DIR / f"{args.name}-ready.mp4"
    stamp = time.strftime("%Y%m%d-%H%M%S")

    # archive any previous render so takes aren't lost
    if final.is_file():
        final.rename(work / f"final.{stamp}.mp4")
        log(f"archived previous render -> final.{stamp}.mp4")

    # --- stage 1: local voice clone + synth (XTTS v2, free, offline) --------------
    log("\n=== stage: local-voice ===")
    voice_ref = None
    if args.voice_ref:
        voice_ref = Path(args.voice_ref)
        if not voice_ref.is_file():
            sys.exit(f"voice reference not found: {voice_ref}")
        log(f"Cloning the SAVED voice ({voice_ref.name}) and speaking your script (XTTS v2)...")
    else:
        log("Cloning the on-screen speaker's voice and synthesizing your script locally (XTTS v2)...")
    ds_cmd = [str(DS_PY), str(DS_APP), "--cli", "--video", str(video),
              "--script", str(script), "--language", args.language, "--device", "auto",
              "--keep-volume", str(max(0.0, min(1.0, args.keep_volume)))]
    if voice_ref:
        ds_cmd += ["--reference", str(voice_ref)]
    proc = subprocess.run(
        ds_cmd,
        cwd=str(DUBBING_STUDIO), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    sys.stdout.write(proc.stdout or "")
    if proc.returncode != 0:
        sys.exit(f"local voice stage failed:\n{(proc.stderr or '')[-800:]}")

    dub_wav = dub_video = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("Audio:"):
            dub_wav = Path(line.split(":", 1)[1].strip())
        elif line.startswith("Video:"):
            dub_video = Path(line.split(":", 1)[1].strip())
    if not dub_video or not dub_video.is_file():
        sys.exit("local voice stage did not produce a video")
    log(f"local voice done: {dub_video.name}")

    # keep the VO in the workdir — captions (word timing) and DubSync repairs need it
    if dub_wav and dub_wav.is_file():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(dub_wav),
                        "-codec:a", "libmp3lame", "-b:a", "192k",
                        str(work / "new-vo.mp3")], env=_w2l_env())

    # --- stage 2: lip-sync ---------------------------------------------------------
    if args.lipsync == "none":
        # the XTTS tool already swapped the new audio into the video — that IS the dub
        shutil.copy2(dub_video, final)
        log("\n=== stage: mux (local, no lip-sync) ===")
        log("Kept the original mouth; swapped in the cloned-voice audio.")
    elif args.lipsync in ("wav2lip", "wav2lip-hd"):
        hd = args.lipsync == "wav2lip-hd"
        log(f"\n=== stage: lipsync (local GPU — FREE{', + GFPGAN HD' if hd else ''}) ===")
        log("Regenerating the mouth to match the cloned voice on your GPU…")
        audio_for_lip = dub_wav if dub_wav and dub_wav.is_file() else dub_video
        run_lipsync(video, audio_for_lip, final, hd)
        log("Local lip-sync done — mouth now matches the new audio.")
    else:
        if args.lipsync not in FAL_TIERS:
            sys.exit(f"unknown lip-sync tier: {args.lipsync}")
        log(f"\n=== stage: lipsync ({args.lipsync}, fal.ai) ===")
        # feed the local audio into the cloud lip-sync stage of script-swap.py
        new_vo = work / "new-vo.mp3"
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(dub_wav or dub_video),
             "-vn", "-ac", "1", "-ar", "44100", "-b:a", "192k", str(new_vo)],
        )
        if r.returncode != 0 or not new_vo.is_file():
            sys.exit("could not prepare audio for lip-sync")
        (work / "source.txt").write_text(str(video) + "\n", encoding="utf-8")

        from fal_guard import load_env
        load_env()   # FAL_KEY etc. for the script-swap child (probe already ran at startup)
        try:
            rc = subprocess.run([sys.executable, str(SWAP), "lipsync",
                                 "--name", args.name, "--tier", args.lipsync],
                                cwd=str(ROOT), timeout=1800)
        except subprocess.TimeoutExpired:
            sys.exit("cloud lip-sync timed out after 30 min — fal.ai did not respond; "
                     "try again or use the local Wav2Lip HD lip-sync")
        if rc.returncode != 0:
            sys.exit("cloud lip-sync stage failed")
        if ready.is_file():
            shutil.copy2(ready, final)

    if final.is_file():
        READY_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(final, ready)
        (work / "dub-config.json").write_text(
            json.dumps({"tts": "local-xtts", "tier": args.lipsync, "engine": "local"}),
            encoding="utf-8")
        log(f"\ndeliverable: {ready}")
        log(f"final video for dashboard playback: {final}")
        log("Done ✅")
        return 0
    sys.exit("no final video produced")


if __name__ == "__main__":
    raise SystemExit(main())
