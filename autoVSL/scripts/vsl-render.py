#!/usr/bin/env python3
"""Full VSL renderer: clone an avatar's voice, voice a new script, lip-sync onto
the avatar footage (looping to cover longer VO), and burn social-media captions.

Stages (cached; safe to re-run):
  clone     avatar audio -> voice.json
  speak     script.txt   -> vo.mp3
  lipsync   avatar+vo    -> lipsynced.mp4   (sync_mode=loop for length gap)
  caption   word-align vo -> styled .ass -> burn -> <out>/<name>.mp4

Usage:
  python3 scripts/vsl-render.py run --name founder-01 --avatar /path/cr33.mp4 \
      --out "/Users/.../litt VSL's"
  (or run individual stages: clone|speak|lipsync|caption)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLONE_ENDPOINT = "fal-ai/minimax/voice-clone"
TTS_ENDPOINT = "fal-ai/minimax/speech-02-hd"
LIPSYNC_ENDPOINTS = {"pro": "fal-ai/sync-lipsync/v2/pro", "standard": "fal-ai/sync-lipsync/v2"}
WHISPER_ENDPOINT = "fal-ai/whisper"

# Caption style (social-media clean): bold white, heavy black outline, lower-center
WORDS_PER_LINE = 3
FONT = "Arial"
# Brand always renders with its exact stylized spelling, even in all-caps captions
BRAND = "līītt"
BRAND_ALIASES = {"lit", "litt", "liit", "liitt", "leet", "lift"}
FONT_SIZE = 50
OUTLINE = 3
SHADOW = 2
MARGIN_V = 230  # px from bottom (1080 tall)
MARGIN_LR = 40


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
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def work_dir(name: str) -> Path:
    d = ROOT / "output" / "vsl" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    with httpx.Client(follow_redirects=True, timeout=600) as c:
        r = c.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)


def stage_clone(name: str, avatar: Path) -> str:
    work = work_dir(name)
    vf = work / "voice.json"
    if vf.exists():
        vid = json.loads(vf.read_text())["custom_voice_id"]
        print(f"clone: cached {vid}")
        return vid
    wav = work / "avatar-audio.wav"
    if not wav.exists():
        ff(["-i", str(avatar), "-vn", "-ac", "1", "-ar", "32000", str(wav)])
    res = subscribe(CLONE_ENDPOINT, {"audio_url": upload(wav)})
    vid = res.get("custom_voice_id") or res.get("voice_id")
    if not vid:
        raise RuntimeError(f"No voice id: {res}")
    vf.write_text(json.dumps({"custom_voice_id": vid}, indent=2))
    print(f"clone: done {vid}")
    return vid


def stage_speak(name: str, voice_id: str) -> Path:
    work = work_dir(name)
    out = work / "vo.mp3"
    if out.exists():
        print(f"speak: cached ({probe_dur(out):.1f}s)")
        return out
    text = (work / "script.txt").read_text().strip()
    res = subscribe(TTS_ENDPOINT, {
        "text": text,
        "voice_setting": {"custom_voice_id": voice_id, "speed": 1.0, "vol": 1.0, "pitch": 0},
        "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3"},
    })
    audio = res.get("audio") or {}
    url = audio.get("url") if isinstance(audio, dict) else audio
    download(url, out)
    print(f"speak: done ({probe_dur(out):.1f}s)")
    return out


def stage_lipsync(name: str, avatar: Path, vo: Path, tier: str) -> Path:
    work = work_dir(name)
    out = work / "lipsynced.mp4"
    if out.exists():
        print(f"lipsync: cached ({probe_dur(out):.1f}s)")
        return out
    vo_dur = probe_dur(vo)
    vid_dur = probe_dur(avatar)
    mode = "loop" if vo_dur > vid_dur + 0.5 else "cut_off"
    print(f"lipsync: VO {vo_dur:.1f}s vs avatar {vid_dur:.1f}s → sync_mode={mode}")
    res = subscribe(LIPSYNC_ENDPOINTS[tier], {
        "video_url": upload(avatar), "audio_url": upload(vo), "sync_mode": mode})
    v = res.get("video") or {}
    url = v.get("url") if isinstance(v, dict) else v
    download(url, out)
    print(f"lipsync: done ({probe_dur(out):.1f}s)")
    return out


def render_word(w: str) -> str:
    """Uppercase for the caption style, but always show the brand as 'līītt'."""
    import re
    core = re.sub(r"[^A-Za-z]", "", w).lower()
    if core in BRAND_ALIASES:
        m = re.match(r"^(\W*)(.*?)(\W*)$", w)
        return (m.group(1) if m else "") + BRAND + (m.group(3) if m else "")
    return w.upper()


def _ass_time(t: float) -> str:
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def build_ass(words: list[dict], out_path: Path, video_w: int, video_h: int) -> None:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{FONT},{FONT_SIZE},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{OUTLINE},{SHADOW},2,{MARGIN_LR},{MARGIN_LR},{MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    group = []
    for w in words:
        group.append(w)
        if len(group) >= WORDS_PER_LINE:
            lines.append(group)
            group = []
    if group:
        lines.append(group)

    events = []
    for grp in lines:
        start = grp[0]["start"]
        end = grp[-1]["end"]
        text = " ".join(render_word(g["text"].strip()) for g in grp)
        events.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Cap,,0,0,0,,{text}")
    out_path.write_text(header + "\n".join(events) + "\n")


def stage_caption(name: str, lipsynced: Path, vo: Path, out_dir: Path) -> Path:
    work = work_dir(name)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{name}.mp4"

    # word-level timestamps via whisper on the clean VO
    words_json = work / "words.json"
    if not words_json.exists():
        res = subscribe(WHISPER_ENDPOINT, {
            "audio_url": upload(vo), "task": "transcribe", "chunk_level": "word"})
        words_json.write_text(json.dumps(res, indent=2))
    res = json.loads(words_json.read_text())
    chunks = res.get("chunks") or []
    words = []
    for c in chunks:
        ts = c.get("timestamp") or []
        if len(ts) == 2 and ts[0] is not None and ts[1] is not None:
            words.append({"text": c.get("text", ""), "start": ts[0], "end": ts[1]})
    if not words:
        raise RuntimeError("No word timestamps from whisper")

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(lipsynced)],
        capture_output=True, text=True, check=True).stdout.strip()
    vw, vh = (int(x) for x in probe.split(","))

    ass = work / "captions.ass"
    build_ass(words, ass, vw, vh)

    # burn subtitles (escape path for ffmpeg filter)
    ass_esc = str(ass).replace(":", "\\:")
    ff(["-i", str(lipsynced), "-vf", f"subtitles='{ass_esc}'",
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-c:a", "copy", str(final)])
    print(f"caption: done → {final}")
    return final


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage", choices=["clone", "speak", "lipsync", "caption", "run"])
    p.add_argument("--name", required=True)
    p.add_argument("--avatar", help="Path to avatar video")
    p.add_argument("--out", help="Output folder for final captioned VSL")
    p.add_argument("--tier", choices=["pro", "standard"], default="pro")
    args = p.parse_args()

    load_env()
    import os
    if not os.environ.get("FAL_KEY"):
        print("Set FAL_KEY in .env", file=sys.stderr)
        return 1

    work = work_dir(args.name)
    avatar = Path(args.avatar).expanduser() if args.avatar else None
    if avatar:
        (work / "avatar.txt").write_text(str(avatar))
    elif (work / "avatar.txt").exists():
        avatar = Path((work / "avatar.txt").read_text().strip())
    out_dir = Path(args.out).expanduser() if args.out else work / "final"

    if args.stage in ("run", "clone"):
        vid = stage_clone(args.name, avatar)
    if args.stage in ("run", "speak"):
        vid = json.loads((work / "voice.json").read_text())["custom_voice_id"]
        vo = stage_speak(args.name, vid)
    if args.stage in ("run", "lipsync"):
        vo = work / "vo.mp3"
        ls = stage_lipsync(args.name, avatar, vo, args.tier)
    if args.stage in ("run", "caption"):
        ls = work / "lipsynced.mp4"
        vo = work / "vo.mp3"
        final = stage_caption(args.name, ls, vo, out_dir)
        print(f"\n✓ Final VSL: {final}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
