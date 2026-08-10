#!/usr/bin/env python3
"""Script-swap pipeline: clone a speaker's voice from a video, transcribe it,
re-voice an edited script with the clone, and lip-sync the original footage.

Stages (each cached in the work dir, safe to re-run):
  transcribe  video → transcript.txt (edit this, save as script-edited.txt)
  clone       video audio → voice.json (reusable MiniMax custom_voice_id)
  speak       script-edited.txt + voice.json → new-vo.mp3
  lipsync     original video + new-vo.mp3 → <name>-swapped.mp4

Usage:
  python3 scripts/script-swap.py run <video> [--name test] [--tier pro|standard]
  python3 scripts/script-swap.py transcribe <video> --name test
  python3 scripts/script-swap.py speak --name test
  python3 scripts/script-swap.py lipsync --name test
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
READY_DIR = Path("~/Desktop/liitt testimonial Ready").expanduser()

WHISPER_ENDPOINT = "fal-ai/whisper"
CLONE_ENDPOINT = "fal-ai/minimax/voice-clone"
# minimax models need a paid voice clone ($1.50, one-time per speaker) first;
# f5 and chatterbox zero-shot clone from a reference clip for free.
# Prices verified against fal.ai model pages on 2026-08-10.
TTS_ENDPOINTS = {
    "hd": "fal-ai/minimax/speech-02-hd",         # $0.10/1k chars
    "turbo": "fal-ai/minimax/speech-02-turbo",    # cheaper minimax
    "hd25": "fal-ai/minimax/preview/speech-2.5-hd",       # $0.06/1k — newer + cheaper than 02-hd
    "turbo25": "fal-ai/minimax/preview/speech-2.5-turbo",  # $0.04/1k
    "f5": "fal-ai/f5-tts",                        # $0.05/1k chars, no clone fee
    "chatterbox": "resemble-ai/chatterboxhd/text-to-speech",  # $0.04/1k, free zero-shot clone
}
LIPSYNC_ENDPOINTS = {
    "sync3": "fal-ai/sync-lipsync/v3",          # $8/min — sync.so's newest, top quality
    "pro": "fal-ai/sync-lipsync/v2/pro",        # ~$6/min
    "standard": "fal-ai/sync-lipsync/v2",       # ~$3/min
    "hummingbird": "fal-ai/tavus/hummingbird-lipsync/v0",  # $2.10/min (15s min) — Tavus zero-shot
    "veed": "veed/lipsync",                     # ~$0.40/min
    "latentsync": "fal-ai/latentsync",          # ~$0.20 per 40s clip
    "musetalk": "fal-ai/musetalk",              # fast real-time lip-sync, cheap compute
}
# voices that zero-shot clone from a reference clip inside the speak stage —
# they skip the paid MiniMax clone stage entirely
ZERO_SHOT_TTS = ("f5", "chatterbox")


def load_env() -> None:
    env_file = ROOT / ".env"
    if env_file.exists():
        import os

        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


def work_dir(name: str) -> Path:
    d = ROOT / "output" / "script-swap" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", *args], check=True, capture_output=True)


def extract_audio(video: Path, dest: Path) -> Path:
    """Mono 32kHz wav — clean input for both Whisper and voice cloning."""
    if not dest.exists():
        run_ffmpeg(["-i", str(video), "-vn", "-ac", "1", "-ar", "32000", str(dest)])
    return dest


def audio_duration(path: Path) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(out.strip())


def clone_sample(video: Path, work: Path) -> Path:
    """MiniMax voice-clone rejects samples under 10s (422 audio_duration_too_short).
    Short clips get looped — repeating the same speech keeps the clone natural,
    unlike padding with silence."""
    wav = extract_audio(video, work / "source-audio.wav")
    dur = audio_duration(wav)
    if dur >= 10.5:
        return wav
    looped = work / "source-audio-looped.wav"
    if not looped.exists():
        loops = max(1, int(10.5 // max(dur, 0.5)) + 1)
        print(f"clone: sample is {dur:.1f}s (< 10s minimum) — looping x{loops + 1}")
        run_ffmpeg(["-stream_loop", str(loops), "-i", str(wav), "-t", "15", str(looped)])
    return looped


def upload(path: Path) -> str:
    import fal_client

    print(f"  uploading {path.name} …", flush=True)
    return fal_client.upload_file(str(path))


def subscribe(endpoint: str, arguments: dict) -> dict:
    import fal_client

    print(f"  calling {endpoint} …", flush=True)
    return fal_client.subscribe(endpoint, arguments=arguments, with_logs=True)


def download(url: str, dest: Path) -> None:
    import httpx

    with httpx.Client(follow_redirects=True, timeout=300) as client:
        resp = client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)


def stage_transcribe(video: Path, work: Path) -> str:
    out = work / "transcript.txt"
    raw = work / "transcript-raw.json"
    if out.exists():
        print(f"transcribe: cached ({out})")
        return out.read_text()

    wav = extract_audio(video, work / "source-audio.wav")
    audio_url = upload(wav)
    result = subscribe(WHISPER_ENDPOINT, {"audio_url": audio_url, "task": "transcribe"})
    raw.write_text(json.dumps(result, indent=2))
    text = result.get("text", "").strip()
    out.write_text(text + "\n")
    print(f"transcribe: done → {out}")
    return text


def stage_clone(video: Path, work: Path) -> str:
    voice_file = work / "voice.json"
    if voice_file.exists():
        vid = json.loads(voice_file.read_text())["custom_voice_id"]
        print(f"clone: cached voice id {vid}")
        return vid

    wav = clone_sample(video, work)
    audio_url = upload(wav)
    result = subscribe(CLONE_ENDPOINT, {"audio_url": audio_url})
    vid = result.get("custom_voice_id") or result.get("voice_id")
    if not vid:
        raise RuntimeError(f"No voice id in clone response: {result}")
    voice_file.write_text(json.dumps({"custom_voice_id": vid, "raw": result}, indent=2))
    print(f"clone: done → voice id {vid}")
    return vid


def stage_speak(work: Path, voice_id: str | None, tts: str = "hd",
                video: Path | None = None) -> Path:
    out = work / "new-vo.mp3"
    script_file = work / "script-edited.txt"
    if not script_file.exists():
        raise SystemExit(
            f"Missing {script_file} — edit transcript.txt and save it as script-edited.txt"
        )
    if out.exists():
        print(f"speak: cached ({out})")
        return out

    text = script_file.read_text().strip()

    if tts in ZERO_SHOT_TTS:
        # zero-shot clone from a short reference clip of the source audio — no paid clone step
        if video is None or not video.is_file():
            raise SystemExit(f"{tts} needs the source video for the voice reference")
        wav = extract_audio(video, work / "source-audio.wav")
        ref = work / "ref-audio.wav"
        if not ref.exists():
            run_ffmpeg(["-i", str(wav), "-t", "12", str(ref)])
        if tts == "f5":
            result = subscribe(
                TTS_ENDPOINTS["f5"],
                {"gen_text": text, "ref_audio_url": upload(ref), "model_type": "F5-TTS"},
            )
            url = (result.get("audio_url") or {}).get("url")
        else:  # chatterbox: audio_url is the voice-clone reference
            result = subscribe(
                TTS_ENDPOINTS["chatterbox"],
                {"text": text, "audio_url": upload(ref), "high_quality_audio": True},
            )
            audio = result.get("audio") or {}
            url = audio.get("url") if isinstance(audio, dict) else audio
        if not url:
            raise RuntimeError(f"No audio url in {tts} response: {result}")
        tmp = work / f"new-vo.{tts}.wav"
        download(url, tmp)
        run_ffmpeg(["-i", str(tmp), "-codec:a", "libmp3lame", "-b:a", "192k", str(out)])
        tmp.unlink(missing_ok=True)
    else:
        # speech-2.5 documents the cloned id under voice_id; speech-02 keeps the
        # fal-specific custom_voice_id field
        voice_setting = {"speed": 1.0, "vol": 1.0, "pitch": 0}
        voice_setting["voice_id" if tts in ("hd25", "turbo25") else "custom_voice_id"] = voice_id
        result = subscribe(
            TTS_ENDPOINTS[tts],
            {
                "text": text,
                "voice_setting": voice_setting,
                "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3"},
            },
        )
        audio = result.get("audio") or {}
        url = audio.get("url") if isinstance(audio, dict) else audio
        if not url:
            raise RuntimeError(f"No audio url in TTS response: {result}")
        download(url, out)
    print(f"speak: done → {out}")
    return out


def stage_lipsync(video: Path, vo: Path, work: Path, name: str, tier: str) -> Path:
    READY_DIR.mkdir(parents=True, exist_ok=True)
    out = READY_DIR / f"{name}-ready.mp4"
    if out.exists():
        print(f"lipsync: cached ({out})")
        return out

    video_url = upload(video)
    audio_url = upload(vo)
    if tier == "musetalk":
        # fal-ai/musetalk names the video field differently.
        arguments = {"source_video_url": video_url, "audio_url": audio_url}
    else:
        arguments = {"video_url": video_url, "audio_url": audio_url}
        if tier in ("pro", "standard", "sync3"):
            arguments["sync_mode"] = "cut_off"   # sync.so-specific option
    result = subscribe(LIPSYNC_ENDPOINTS[tier], arguments)
    video_out = result.get("video") or {}
    url = video_out.get("url") if isinstance(video_out, dict) else video_out
    if not url:
        raise RuntimeError(f"No video url in lipsync response: {result}")
    download(url, out)
    print(f"lipsync: done → {out}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["run", "transcribe", "clone", "speak", "lipsync"])
    parser.add_argument("video", nargs="?", help="Path to source video")
    parser.add_argument("--name", default=None, help="Job name (default: video stem)")
    parser.add_argument("--tier", choices=list(LIPSYNC_ENDPOINTS), default="pro")
    parser.add_argument("--tts", choices=list(TTS_ENDPOINTS), default="hd",
                        help="Voice/TTS model for the speak stage (default: hd)")
    args = parser.parse_args()

    load_env()
    import os

    if not os.environ.get("FAL_KEY"):
        print("Set FAL_KEY in .env", file=sys.stderr)
        return 1

    video = Path(args.video).expanduser() if args.video else None
    name = args.name or (video.stem if video else None)
    if not name:
        print("Need --name (or a video path)", file=sys.stderr)
        return 1
    work = work_dir(name)

    if video:
        (work / "source.txt").write_text(str(video) + "\n")
    elif (work / "source.txt").exists():
        video = Path((work / "source.txt").read_text().strip())
    if args.stage in ("run", "transcribe", "clone", "lipsync") and (
        video is None or not video.is_file()
    ):
        print(f"Video not found: {video}", file=sys.stderr)
        return 1

    if args.stage in ("run", "transcribe"):
        text = stage_transcribe(video, work)
        print(f"\n--- transcript ---\n{text}\n------------------")
    if args.stage in ("run", "clone") and not (args.stage == "run" and args.tts in ZERO_SHOT_TTS):
        voice_id = stage_clone(video, work)   # f5/chatterbox clone zero-shot in speak — no paid clone
    if args.stage in ("run", "speak"):
        if args.tts in ZERO_SHOT_TTS:
            vo = stage_speak(work, None, args.tts, video)
        else:
            voice_id = json.loads((work / "voice.json").read_text())["custom_voice_id"]
            vo = stage_speak(work, voice_id, args.tts)
    if args.stage in ("run", "lipsync"):
        vo = work / "new-vo.mp3"
        if not vo.exists():
            raise SystemExit(f"Missing {vo} — run the speak stage first")
        out = stage_lipsync(video, vo, work, name, args.tier)
        print(f"\n✓ Final video: {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
