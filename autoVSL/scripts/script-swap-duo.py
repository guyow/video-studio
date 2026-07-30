#!/usr/bin/env python3
"""Two-voice script-swap: clone BOTH speakers, re-voice their edited lines,
reassemble a time-anchored audio track, and lip-sync with sync.so active
speaker detection so each face syncs only during its own lines.

Driven by a duo-config.json (see output/script-swap/<name>/duo-config.json):
  speakers: per-speaker reference audio windows (clean solo segments)
  segments: ordered lines {start, speaker, text} anchored to original timing

Usage:
  python3 scripts/script-swap-duo.py clone   --name testimonial-01
  python3 scripts/script-swap-duo.py speak   --name testimonial-01
  python3 scripts/script-swap-duo.py assemble --name testimonial-01
  python3 scripts/script-swap-duo.py lipsync --name testimonial-01 [--tier pro]
  python3 scripts/script-swap-duo.py run     --name testimonial-01
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
READY_DIR = Path("~/Desktop/liitt testimonial Ready").expanduser()

CLONE_ENDPOINT = "fal-ai/minimax/voice-clone"
TTS_ENDPOINT = "fal-ai/minimax/speech-02-hd"
LIPSYNC_ENDPOINTS = {"pro": "fal-ai/sync-lipsync/v2/pro", "standard": "fal-ai/sync-lipsync/v2"}
GAP_PAD = 0.12  # min silence between back-to-back lines


def load_env() -> None:
    import os

    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def ff(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", *args], check=True, capture_output=True)


def probe_dur(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def work_dir(name: str) -> Path:
    return ROOT / "output" / "script-swap" / name


def load_cfg(name: str) -> dict:
    return json.loads((work_dir(name) / "duo-config.json").read_text())


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
        r = client.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)


def build_reference(cfg: dict, name: str, speaker: str) -> Path:
    """Concatenate a speaker's clean solo windows into one reference wav."""
    work = work_dir(name)
    ref = work / f"ref-{speaker}.wav"
    if ref.exists():
        return ref
    src = Path(cfg["source"])
    windows = cfg["speakers"][speaker]["ref_windows"]
    parts = []
    for i, (a, b) in enumerate(windows):
        part = work / f"ref-{speaker}-{i}.wav"
        ff(["-ss", str(a), "-to", str(b), "-i", str(src), "-vn", "-ac", "1", "-ar", "32000", str(part)])
        parts.append(part)
    if len(parts) == 1:
        parts[0].rename(ref)
    else:
        inputs = []
        for p in parts:
            inputs.extend(["-i", str(p)])
        streams = "".join(f"[{i}:a]" for i in range(len(parts)))
        filt = f"{streams}concat=n={len(parts)}:v=0:a=1[out]"
        ff([*inputs, "-filter_complex", filt, "-map", "[out]", "-ar", "32000", "-ac", "1", str(ref)])
    return ref


def stage_clone(cfg: dict, name: str) -> dict:
    work = work_dir(name)
    voices_file = work / "voices.json"
    if voices_file.exists():
        voices = json.loads(voices_file.read_text())
        print(f"clone: cached {voices}")
        return voices
    voices = {}
    for speaker in cfg["speakers"]:
        ref = build_reference(cfg, name, speaker)
        print(f"clone: speaker {speaker} from {ref.name} ({probe_dur(ref):.1f}s)")
        res = subscribe(CLONE_ENDPOINT, {"audio_url": upload(ref)})
        vid = res.get("custom_voice_id") or res.get("voice_id")
        if not vid:
            raise RuntimeError(f"No voice id for {speaker}: {res}")
        voices[speaker] = vid
    voices_file.write_text(json.dumps(voices, indent=2))
    print(f"clone: done {voices}")
    return voices


def render_tts(text: str, voice_id: str, speed: float, dest: Path) -> None:
    res = subscribe(TTS_ENDPOINT, {
        "text": text,
        "voice_setting": {"custom_voice_id": voice_id, "speed": round(speed, 3), "vol": 1.0, "pitch": 0},
        "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3"},
    })
    audio = res.get("audio") or {}
    url = audio.get("url") if isinstance(audio, dict) else audio
    download(url, dest)


def stage_speak(cfg: dict, name: str, voices: dict) -> list[Path]:
    """Render each line, pre-correcting TTS speed so its length lands near the
    original window — keeps later time-stretch gentle and the audio natural."""
    work = work_dir(name)
    lines_dir = work / "lines"
    lines_dir.mkdir(exist_ok=True)
    outputs = []
    for i, seg in enumerate(cfg["segments"]):
        dest = lines_dir / f"line-{i:02d}.mp3"
        target = seg.get("end", 0) - seg.get("start", 0) if "end" in seg else None
        if not dest.exists():
            render_tts(seg["text"], voices[seg["speaker"]], 1.0, dest)
            # Speed pre-correction: if natural length is >15% off the window,
            # re-render at a MiniMax speed that lands closer (range 0.5-2.0).
            if target and target > 0.5:
                raw = probe_dur(dest)
                ratio = raw / target
                if ratio > 1.15 or ratio < 0.87:
                    speed = max(0.5, min(2.0, ratio))
                    print(f"  line {i:02d} [{seg['speaker']}] {raw:.2f}s vs {target:.2f}s → re-render speed x{speed:.2f}")
                    render_tts(seg["text"], voices[seg["speaker"]], speed, dest)
            print(f"  line {i:02d} [{seg['speaker']}] → {dest.name}")
        outputs.append(dest)
    return outputs


def stage_budget(cfg: dict, name: str) -> None:
    """Print a per-segment length budget so edited lines fit naturally (~2.7 wps)."""
    print(f"\nLength budget for {name} — write each line near the target word count:\n")
    print(f"  {'seg':>3}  {'spk':>3}  {'window':>7}  {'words':>6}   text")
    for i, seg in enumerate(cfg["segments"]):
        win = seg["end"] - seg["start"]
        lo, hi = int(win * 2.3), int(win * 3.0)
        words = len(seg["text"].split())
        flag = "" if lo <= words <= hi + 1 else "  <-- adjust length"
        print(f"  {i:>3}  {seg['speaker']:>3}  {win:6.2f}s  {lo:>2}-{hi:<2}  now:{words:>2}{flag}")
    print("\n(window = seconds that mouth is moving in the original; ~2.3-3.0 words/sec)")


def atempo_chain(factor: float) -> str:
    """Decompose a tempo factor into a chain of atempo filters (each in [0.5, 2.0])."""
    factor = max(0.25, min(4.0, factor))
    steps = []
    f = factor
    while f > 2.0:
        steps.append(2.0)
        f /= 2.0
    while f < 0.5:
        steps.append(0.5)
        f /= 0.5
    steps.append(f)
    return ",".join(f"atempo={s:.4f}" for s in steps)


def fit_line_to_window(src: Path, dest: Path, target: float) -> tuple[float, float]:
    """Time-stretch src so its duration == target seconds. Returns (raw_dur, tempo)."""
    raw = probe_dur(src)
    tempo = raw / target  # >1 speeds up (raw longer than window), <1 slows down
    ff(["-i", str(src), "-filter:a", atempo_chain(tempo), "-ar", "32000", "-ac", "1", str(dest)])
    # Pad/trim to exact target so downstream placement is precise
    fitted = probe_dur(dest)
    if fitted < target - 0.02:
        tmp = dest.with_suffix(".pad.wav")
        ff(["-i", str(dest), "-af", f"apad=pad_dur={target - fitted:.3f}", str(tmp)])
        tmp.replace(dest)
    elif fitted > target + 0.02:
        tmp = dest.with_suffix(".trim.wav")
        ff(["-i", str(dest), "-t", f"{target:.3f}", str(tmp)])
        tmp.replace(dest)
    return raw, tempo


def stage_assemble(cfg: dict, name: str, lines: list[Path]) -> Path:
    """Fit each line to its ORIGINAL [start,end] window, then place at its start.
    This locks the new audio to the exact time the mouth is moving in the source."""
    work = work_dir(name)
    out = work / "combined-vo.wav"
    total_video = probe_dur(Path(cfg["source"]))
    segs = cfg["segments"]

    inputs = []
    delays = []
    print("assemble: fitting each line to its original window")
    for i, (seg, line) in enumerate(zip(segs, lines)):
        target = seg["end"] - seg["start"]
        fitted = work / "lines" / f"line-{i:02d}-fit.wav"
        raw, tempo = fit_line_to_window(line, fitted, target)
        flag = "  <-- heavy stretch, consider rewording" if (tempo > 1.6 or tempo < 0.7) else ""
        print(f"  line {i:02d} [{seg['speaker']}] window {target:4.2f}s  raw {raw:4.2f}s  tempo x{tempo:.2f}{flag}")
        delays.append(seg["start"])
        inputs.extend(["-i", str(fitted)])

    parts = []
    for i, start in enumerate(delays):
        ms = int(start * 1000)
        parts.append(f"[{i}:a]adelay={ms}|{ms}[a{i}]")
    mix_in = "".join(f"[a{i}]" for i in range(len(delays)))
    n = len(delays)
    filt = ";".join(parts) + f";{mix_in}amix=inputs={n}:normalize=0[out]"

    ff([*inputs, "-filter_complex", filt, "-map", "[out]",
        "-ar", "32000", "-ac", "1", str(out)])
    print(f"assemble: combined VO {probe_dur(out):.1f}s (video {total_video:.1f}s)")
    return out


def stage_lipsync(cfg: dict, name: str, vo: Path, tier: str) -> Path:
    READY_DIR.mkdir(parents=True, exist_ok=True)
    out = READY_DIR / f"{name}-ready.mp4"
    src = Path(cfg["source"])
    res = subscribe(LIPSYNC_ENDPOINTS[tier], {
        "video_url": upload(src),
        "audio_url": upload(vo),
        "sync_mode": "cut_off",
    })
    v = res.get("video") or {}
    url = v.get("url") if isinstance(v, dict) else v
    download(url, out)
    print(f"lipsync: done → {out}")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage", choices=["clone", "speak", "assemble", "lipsync", "run", "budget"])
    p.add_argument("--name", required=True)
    p.add_argument("--tier", choices=["pro", "standard"], default="pro")
    args = p.parse_args()

    load_env()
    import os

    if not os.environ.get("FAL_KEY"):
        print("Set FAL_KEY in .env", file=sys.stderr)
        return 1

    cfg = load_cfg(args.name)
    work = work_dir(args.name)

    if args.stage == "budget":
        stage_budget(cfg, args.name)
        return 0
    if args.stage in ("run", "clone"):
        voices = stage_clone(cfg, args.name)
    if args.stage in ("run", "speak"):
        voices = json.loads((work / "voices.json").read_text())
        lines = stage_speak(cfg, args.name, voices)
    if args.stage in ("run", "assemble"):
        lines = [work / "lines" / f"line-{i:02d}.mp3" for i in range(len(cfg["segments"]))]
        vo = stage_assemble(cfg, args.name, lines)
    if args.stage in ("run", "lipsync"):
        vo = work / "combined-vo.wav"
        out = stage_lipsync(cfg, args.name, vo, args.tier)
        print(f"\n✓ Final: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
