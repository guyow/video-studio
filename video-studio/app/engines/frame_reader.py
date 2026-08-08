#!/usr/bin/env python3
"""Frame Reader — watch a winning UGC clip and write the script back out of it.

Upload a video that is already working (yours or a competitor's) and this reads
it frame by frame and returns the shot-by-shot script that produced it: what is
on screen in every scene, how it was shot, what was said over it, what the
on-screen text was, and what each beat is *doing* for retention.

The point is cloning. The output is not a description — it is a shooting
document plus, per scene, an image-to-video prompt, so a scene can go straight
into /image-to-video (or B-Roll Factory) and be rebuilt with a new product.

    read      video -> cut detection -> keyframes -> whisper VO -> Claude vision
              -> read.json (structured scenes) + script.md (readable shot list)

Cut detection is ffmpeg's scene filter, so scene boundaries are the real edit
points, not an arbitrary grid — that is what makes the pacing numbers (shot
count, average shot length) trustworthy. Uniform samples fill in when a clip is
one long unbroken take.

Everything is local and free except the Claude read, which runs on the `claude`
CLI (the user's Claude Code subscription) — no API key, same as B-Roll Factory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

APP_DIR = Path(__file__).resolve().parent.parent           # video-studio/app
VS_ROOT = APP_DIR.parent                                    # video-studio/
CONFIG = json.loads((VS_ROOT / "config.json").read_text(encoding="utf-8"))
ROOT = Path(CONFIG["autovsl_root"])                         # data root (autoVSL repo)
OUT_ROOT = ROOT / "output" / "frame-reads"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

# Scene-change sensitivity. 0.30 is the sweet spot for short-form ad edits: it
# catches hard cuts and most whip transitions without firing on a fast pan.
SCENE_THRESHOLD = 0.30
# A "shot" shorter than this is almost always a transition frame, not a beat.
MIN_SHOT_S = 0.45
# Frames are read by a vision model, not displayed — 768px long edge is plenty
# and keeps a 40-frame read fast.
FRAME_MAX_PX = 768


def log(msg: str) -> None:
    print(msg, flush=True)


def slugify(s: str, fallback: str = "read", limit: int = 28) -> str:
    """Short on purpose: ad creatives arrive with 100-char filenames, and the
    batch name is a directory that everything below it nests inside. Windows
    still caps most paths at 260 chars."""
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:limit].strip("-") or fallback


# ----------------------------------------------------------------- ffmpeg utils

FFMPEG_BIN = (Path(os.environ.get("LOCALAPPDATA", "")) /
              "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
              "/ffmpeg-8.1.2-full_build/bin")


def ff(name: str) -> str:
    exe = FFMPEG_BIN / f"{name}.exe"
    return str(exe) if exe.is_file() else name


def run(cmd: list[str], what: str) -> None:
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        tail = "\n".join((r.stderr or "").strip().splitlines()[-12:])
        raise RuntimeError(f"{what} failed (rc={r.returncode}):\n{tail}")


def probe(path: Path) -> dict:
    r = subprocess.run([ff("ffprobe"), "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def media_info(path: Path) -> dict:
    p = probe(path)
    v = next((s for s in p.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in p.get("streams", []) if s.get("codec_type") == "audio"), None)
    try:
        dur = float((p.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    fps = 0.0
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    return {"w": w, "h": h, "duration": dur, "fps": round(fps, 2),
            "codec": v.get("codec_name"), "has_audio": a is not None,
            "aspect": aspect_label(w, h)}


def aspect_label(w: int, h: int) -> str:
    if not w or not h:
        return "unknown"
    r = w / h
    for label, target in (("9:16", 9 / 16), ("1:1", 1.0), ("4:5", 4 / 5),
                          ("16:9", 16 / 9), ("4:3", 4 / 3)):
        if abs(r - target) < 0.06:
            return label
    return f"{r:.2f}:1"


# --------------------------------------------------------------- cut detection

def detect_cuts(src: Path, duration: float) -> list[float]:
    """Timestamps (seconds) where ffmpeg sees a scene change."""
    r = subprocess.run(
        [ff("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(src),
         "-filter:v", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo",
         "-an", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    times = []
    for m in re.finditer(r"pts_time:([0-9.]+)", r.stderr or ""):
        try:
            t = float(m.group(1))
        except ValueError:
            continue
        if 0.05 < t < duration - 0.05:
            times.append(round(t, 3))
    return sorted(set(times))


def build_shots(duration: float, cuts: list[float], max_shots: int) -> list[dict]:
    """Turn cut points into shot spans, filling long unbroken takes with splits.

    A one-take talking head has no cuts at all — but it still has beats, so it is
    sliced on a ~4s grid rather than handed to the model as a single blob.
    """
    bounds = [0.0] + [c for c in cuts if MIN_SHOT_S < c < duration] + [duration]
    bounds = sorted(set(round(b, 3) for b in bounds))

    # drop boundaries that would make a sub-minimum shot (transition artefacts)
    cleaned = [bounds[0]]
    for b in bounds[1:]:
        if b - cleaned[-1] >= MIN_SHOT_S:
            cleaned.append(b)
    if cleaned[-1] < duration - 0.01:
        cleaned[-1] = round(duration, 3)

    # split any take longer than 6s so a static talking head still gets beats
    split: list[float] = [cleaned[0]]
    for b in cleaned[1:]:
        start = split[-1]
        span = b - start
        if span > 6.0:
            pieces = int(span // 4.0) + 1
            for k in range(1, pieces):
                split.append(round(start + span * k / pieces, 3))
        split.append(round(b, 3))
    split = sorted(set(split))

    shots = [{"n": i + 1, "t_start": split[i], "t_end": split[i + 1],
              "duration": round(split[i + 1] - split[i], 2)}
             for i in range(len(split) - 1)]

    # too many shots for one vision read — merge the shortest neighbours first
    while len(shots) > max_shots:
        i = min(range(len(shots) - 1), key=lambda k: shots[k]["duration"] + shots[k + 1]["duration"])
        merged = {"n": 0, "t_start": shots[i]["t_start"], "t_end": shots[i + 1]["t_end"]}
        merged["duration"] = round(merged["t_end"] - merged["t_start"], 2)
        shots[i:i + 2] = [merged]
    for i, s in enumerate(shots, 1):
        s["n"] = i
    return shots


def frames_for_shot(shot: dict, budget: int) -> list[float]:
    """Where to sample inside a shot: 1 frame for a flash, up to 3 for a long beat."""
    d = shot["duration"]
    a, b = shot["t_start"], shot["t_end"]
    n = 1 if d < 1.2 else (2 if d < 3.5 else 3)
    n = max(1, min(n, budget))
    if n == 1:
        return [a + d / 2]
    return [a + d * (i + 0.5) / n for i in range(n)]


def extract_frames(src: Path, shots: list[dict], out_dir: Path, max_frames: int) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    budget = max_frames
    total = 0
    for s in shots:
        per = max(1, budget // max(1, (len(shots) - s["n"] + 1)))
        s["frames"] = []
        for t in frames_for_shot(s, per):
            dest = out_dir / f"s{s['n']:03d}_t{t:07.2f}.jpg"
            try:
                run([ff("ffmpeg"), "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(src),
                     "-vf", f"scale='min({FRAME_MAX_PX},iw)':-2:flags=lanczos",
                     "-frames:v", "1", "-q:v", "3", str(dest)],
                    f"extracting frame at {t:.2f}s")
            except RuntimeError:
                continue
            if dest.is_file() and dest.stat().st_size > 0:
                s["frames"].append({"path": str(dest), "t": round(t, 2)})
                total += 1
                budget -= 1
    return total


# ------------------------------------------------------------------ transcript

def transcribe(src: Path, work: Path, whisper_py: str | None,
               whisper_script: str | None, model: str = "distil-large-v3") -> list[dict]:
    """Spoken lines with timings, via the course_pipeline whisper venv. Optional —
    a silent clip or a missing venv just means the read has no VO column."""
    if not whisper_py or not whisper_script:
        log("  (no whisper venv passed — skipping the voice-over)")
        return []
    if not Path(whisper_py).is_file() or not Path(whisper_script).is_file():
        log("  (whisper venv not found — skipping the voice-over)")
        return []
    tdir = work / "_audio"
    tdir.mkdir(parents=True, exist_ok=True)

    # transcribe.py names its output after the INPUT stem, and downloaded ad
    # creatives carry ~100-char filenames. Nested under the batch dir that blew
    # past Windows' 260-char path limit and whisper failed with WinError 3 —
    # silently, because a missing VO is survivable. Feed it a short-named link
    # instead (hardlink is instant and costs no disk; copy only if that fails).
    short = tdir / f"src{src.suffix.lower()}"
    if not short.exists():
        try:
            os.link(src, short)
        except OSError:
            shutil.copy2(src, short)

    r = subprocess.run(
        # word timings cost a little extra decode time and buy the thing that makes
        # a shot list usable: each scene gets ITS words, not the whole sentence that
        # happens to straddle the cut.
        [str(whisper_py), str(whisper_script), str(short), "--out", str(tdir),
         "--model", model, "--word-timestamps"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
    jf = tdir / "transcripts" / f"{short.stem}.json"
    if not jf.is_file():
        tail = "\n".join((r.stdout or r.stderr or "").strip().splitlines()[-4:])
        log("  !! TRANSCRIPTION FAILED — the read will have no voice-over.")
        log(f"     {tail[:400]}")
        short.unlink(missing_ok=True)
        return []
    try:
        data = json.loads(jf.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    finally:
        short.unlink(missing_ok=True)      # the transcript is what we wanted, not the copy
    return [{"start": s.get("start", 0.0), "end": s.get("end", 0.0),
             "text": (s.get("text") or "").strip(),
             "words": [{"start": w.get("start", 0.0), "end": w.get("end", 0.0),
                        "word": w.get("word", "")}
                       for w in (s.get("words") or [])]}
            for s in data.get("segments", []) if (s.get("text") or "").strip()]


def spoken_in(segs: list[dict], a: float, b: float) -> str:
    """What is said *during this scene*.

    With word timings a sentence that runs across a cut is split at the cut, so
    each scene shows its own words instead of every scene repeating the whole
    sentence. Without them (older transcripts, whisper skipped) it falls back to
    segment overlap.
    """
    words = [w for s in segs for w in (s.get("words") or [])]
    if words:
        picked = [w["word"] for w in words
                  if a - 0.02 <= (w["start"] + w["end"]) / 2 < b - 0.02]
        return " ".join(t.strip() for t in picked if t.strip()).strip()
    hits = [s["text"] for s in segs if s["end"] > a + 0.05 and s["start"] < b - 0.05]
    return " ".join(hits).strip()


# ---------------------------------------------------------------- Claude vision

def claude_exe() -> str:
    exe = shutil.which("claude")
    if exe:
        return exe
    for p in (Path.home() / ".local/bin/claude.exe", Path.home() / ".local/bin/claude"):
        if p.exists():
            return str(p)
    raise SystemExit("claude CLI not found — install Claude Code or add it to PATH.")


def ask_claude(prompt: str, cwd: Path, model: str = "sonnet", timeout: int = 2400) -> dict:
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env["PYTHONUTF8"] = "1"
    if FFMPEG_BIN.is_dir():
        env["PATH"] = str(FFMPEG_BIN) + os.pathsep + env.get("PATH", "")
    r = subprocess.run(
        [claude_exe(), "-p", "--model", model, "--allowedTools", "Read",
         "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch,Task"],
        input=prompt, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, cwd=str(cwd), env=env)
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        raise RuntimeError(f"claude CLI failed (rc={r.returncode}): {(r.stderr or '')[:400]}")
    if out.startswith("```"):
        out = out.split("```")[1]
        out = out[4:] if out.lower().startswith("json") else out
    start, end = out.find("{"), out.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError(f"Claude did not return JSON. First 300 chars:\n{out[:300]}")
    return json.loads(out[start:end + 1])


READ_PROMPT = """You are a commercial director doing a teardown of a short-form ad \
that is already performing. Another director has to reshoot this exact structure for \
a different product tomorrow, and your teardown is all they get.

Read EVERY image file listed below — they are real JPEGs on disk, one or more per \
scene, pulled at the timestamps shown. Look at them. Do not guess from the timings.

VIDEO
{specs}

SCENES (cut points came from real scene detection, so these are the actual edit points)
{scenes}

VOICE-OVER / DIALOGUE (whisper, with timings)
{transcript}

{brief_block}{cast_block}
TASK
Return the shot-by-shot script that would let someone rebuild this video. For every \
scene, describe what is actually in the frames — the subject, what they are doing, \
where they are, what they are holding, how it is lit and shot. Read any on-screen \
text out of the frames literally, character for character, including emoji. If a \
scene has no on-screen text, say "".

First settle the CAST: every person who appears on camera, described ONCE, in full. \
After that, refer to them by their id in every scene instead of re-describing them. \
A reshoot needs one consistent person per role, not a new description per cut.

Judge the edit as a director: what each beat is doing for retention, why the cut \
lands where it does, and what makes this one work rather than scroll past.

Return exactly ONE JSON object and nothing else. No prose, no markdown fence.

{{
  "summary": {{
    "one_line": "what this ad is, in one sentence",
    "format": "e.g. UGC talking head + product demo | pack-an-order | founder story | \
street interview | before-after | listicle overlay",
    "hook_type": "what the first 2 seconds do to stop the scroll",
    "product": "the product as far as you can tell from the frames",
    "target_viewer": "who this is aimed at, from the casting, setting and language",
    "pacing": "how the cutting rhythm behaves across the clip",
    "audio": "music/sfx/voice mix as far as the visuals imply",
    "on_screen_text_style": "font weight, placement, colour, animation of the captions",
    "production_level": "phone-shot UGC | lightly produced | studio",
    "why_it_works": "2-4 sentences, the honest read on why this converts",
    "weakest_link": "the beat most likely to lose the viewer, and why"
  }},
  "cast": [
    {{
      "id": "A",
      "role": "what they are in the ad, e.g. 'the customer / on-camera talent', \
'the hands', 'the partner in the background'",
      "appearance": "one locked physical description a casting director could match: \
apparent age range, build, hair (length, colour, style), skin tone, facial hair, \
distinguishing features. This is the ONE place they get described.",
      "wardrobe": "what they wear, and whether it changes across the video",
      "on_camera": "face | hands only | body, no face | voice only",
      "scenes": [1, 2, 5]
    }}
  ],
  "scenes": [
    {{
      "n": 1,
      "label": "short name for the beat, e.g. 'Hook — she catches you scrolling'",
      "characters": ["A"],
      "function": "hook | problem | agitation | product reveal | demo | proof | \
social proof | objection | offer | cta | b-roll bridge",
      "shot_type": "ECU | CU | MCU | MS | WS | overhead | POV | insert",
      "angle": "eye level | low | high | overhead | dutch",
      "camera": "handheld static | handheld drift | push in | pull out | pan | tilt | \
gimbal walk | locked off — say which",
      "subject": "who or what is in frame — name people by their cast id (\\"A holds \
the pouch up to the lens\\"), never by re-describing their appearance",
      "action": "what physically happens during this scene",
      "setting": "where it is, from the frames",
      "props": ["objects visible in frame"],
      "wardrobe": "what the talent is wearing, if a person is on screen",
      "lighting": "source, direction, quality",
      "color": "palette and grade",
      "mood": "the feeling of the frame",
      "on_screen_text": "literal text burned into the frame, or \\"\\"",
      "spoken": "what is said over this scene (use the transcript above; \\"\\" if silent)",
      "cut_in": "how this scene starts: hard cut | match cut | whip | jump cut | \
speed ramp | opens the video",
      "audio_note": "sfx or music behaviour implied here",
      "retention_note": "what this beat is doing to keep the viewer, in one line",
      "how_to_shoot": "shooting instruction for the reshoot: framing, lens feel, \
distance, movement, direction to talent — concrete enough to hand to a creator",
      "i2v_prompt": "a single image-to-video prompt that would regenerate this scene \
with a different product: subject, framing, lighting, camera move, 20-45 words, prose. \
If a cast member is on screen, OPEN the prompt with their locked appearance from the \
cast list, worded the same way every time, so every regenerated scene shows the same \
person."
    }}
  ],
  "vo_script": "the full spoken script as one clean block of text, punctuation fixed, \
ready to hand to a voice actor. Empty string if the video has no speech.",
  "text_overlays": ["every distinct on-screen text card, in order"],
  "reshoot_brief": "a short paragraph telling a creator how to shoot their own version \
of this: the structure to keep, and what they should change for their own product"
}}

HARD RULES
- One object in "scenes" per scene listed above, same "n", same order. Do not merge \
or invent scenes.
- "on_screen_text" is a transcription, not a summary. Copy what is written. If you \
cannot read it clearly, write what you can and add [unclear].
- "spoken" must come from the transcript block, matched by the scene's timing. Never \
invent dialogue that is not in the transcript.
- Describe what is in the frames. If something is not visible, say so instead of \
filling it in with a plausible guess.
- "i2v_prompt" is prose for a video model, not tags, and must not mention brand names \
or legible text — image models cannot render either.
- The cast is settled ONCE. A person's "appearance" wording is reused verbatim \
wherever they appear — in i2v_prompt and in how_to_shoot. Never give the same person \
two different descriptions in two scenes; that is the single most common way a \
teardown becomes unusable for a reshoot.
- "characters" lists the cast ids visible in that scene, in order of prominence. \
Empty list for a product-only insert, a graphic card, or an empty room.
- A pair of hands with no face is still a cast member. So is someone who only appears \
in the background. Being unsure of a face is fine — say so in "appearance" — but do \
not silently split one person into two ids, or merge two people into one.
"""

BRIEF_BLOCK = """WHAT THE USER IS PLANNING
{brief}

Keep the teardown honest, but where you write "how_to_shoot" and "reshoot_brief", \
aim them at that plan.
"""

# Told how many people are actually in the clip, the model stops hedging between
# "is this the same woman?" and "is this a second actress?" across cuts — which is
# what makes the cast ids, and therefore the i2v prompts, stay consistent.
CAST_BLOCK = """
CAST — HOW MANY PEOPLE ARE IN THIS VIDEO
The user has told you: {n_text}

Treat that as ground truth about the shoot, not a guess to second-guess. Resolve \
every frame against it: if two shots could be the same person or two people, and the \
count says one, it is one person shot differently — say how the framing or wardrobe \
changed rather than inventing a second cast member. If the count says more than one, \
work out which is which and keep them straight for the whole video.
"""

CAST_COUNTS = {
    1: "there is exactly ONE person on camera in this entire video. Every face, "
       "every pair of hands, every voice belongs to that same person.",
    2: "there are exactly TWO people on camera across this video.",
    3: "there are exactly THREE people on camera across this video.",
}


def cmd_read(a: argparse.Namespace) -> None:
    src = Path(a.video).resolve()
    if not src.is_file():
        raise SystemExit(f"video not found: {src}")
    if src.suffix.lower() not in VIDEO_EXTS:
        raise SystemExit(f"not a video: {src.name}")

    info = media_info(src)
    if info["duration"] <= 0:
        raise SystemExit("could not read a duration out of that file — is it a valid video?")

    batch = a.batch or f"{slugify(src.stem, 'read')}-{time.strftime('%m%d-%H%M%S')}"
    work = OUT_ROOT / batch
    work.mkdir(parents=True, exist_ok=True)
    frames_dir = work / "frames"

    log(f"Read: {batch}")
    log(f"Source: {src.name} — {info['w']}x{info['h']} ({info['aspect']}), "
        f"{info['duration']:.1f}s, {info['fps']}fps")

    log("=== stage: cuts ===")
    cuts = detect_cuts(src, info["duration"])
    log(f"Scene detection found {len(cuts)} cut(s)")

    shots = build_shots(info["duration"], cuts, a.max_scenes)
    log(f"{len(shots)} scene(s) to read "
        f"(avg {info['duration'] / max(1, len(shots)):.1f}s per scene)")

    log("=== stage: frames ===")
    got = extract_frames(src, shots, frames_dir, a.max_frames)
    if not got:
        raise SystemExit("could not pull a single frame out of that video.")
    log(f"Pulled {got} frame(s) at {FRAME_MAX_PX}px")

    segs: list[dict] = []
    if info["has_audio"] and not a.no_transcribe:
        log("=== stage: transcribe ===")
        log("Transcribing the voice-over locally (whisper)...")
        try:
            segs = transcribe(src, work, a.whisper_python, a.whisper_script, a.whisper_model)
        except subprocess.TimeoutExpired:
            log("  (transcription timed out — continuing without VO)")
        log(f"  {len(segs)} spoken segment(s)")
    elif not info["has_audio"]:
        log("(no audio track — reading picture only)")

    for s in shots:
        s["spoken"] = spoken_in(segs, s["t_start"], s["t_end"])

    scene_lines = []
    for s in shots:
        frames = "\n".join(f"    - {f['path']}   (t={f['t']}s)" for f in s["frames"]) \
                 or "    - (no frame could be pulled from this scene)"
        line = (f"  Scene {s['n']}: {s['t_start']:.2f}s -> {s['t_end']:.2f}s "
                f"({s['duration']:.2f}s)\n{frames}")
        if s["spoken"]:
            line += f'\n    spoken here: "{s["spoken"]}"'
        scene_lines.append(line)

    tr_block = "\n".join(f"  [{s['start']:.2f}-{s['end']:.2f}] {s['text']}" for s in segs) \
               or "  (no speech detected — this clip carries itself on picture and text)"

    specs = (f"  file: {src.name}\n"
             f"  {info['w']}x{info['h']} ({info['aspect']}), {info['duration']:.2f}s, "
             f"{info['fps']}fps\n"
             f"  {len(cuts)} detected cut(s), {len(shots)} scene(s), "
             f"average shot {info['duration'] / max(1, len(shots)):.2f}s")

    cast_block = ""
    if a.characters and a.characters > 0:
        n_text = CAST_COUNTS.get(a.characters,
                                 f"there are exactly {a.characters} people on camera "
                                 "across this video.")
        cast_block = CAST_BLOCK.format(n_text=n_text)
        log(f"Cast locked to {a.characters} person(s) on camera")

    prompt = READ_PROMPT.format(
        specs=specs,
        scenes="\n".join(scene_lines),
        transcript=tr_block,
        brief_block=BRIEF_BLOCK.format(brief=a.brief.strip()) if (a.brief or "").strip() else "",
        cast_block=cast_block)

    log("=== stage: read ===")
    log(f"Asking Claude to watch {got} frame(s) across {len(shots)} scene(s) "
        f"and write the script (a few minutes)...")
    data = ask_claude(prompt, cwd=work, model=a.model)

    # settle the cast first — scene entries are validated against these ids so a
    # stray "Person C" can't sneak in and break the one-person-per-role promise
    cast_out = []
    for i, m in enumerate(data.get("cast") or []):
        cid = str(m.get("id") or "").strip() or chr(65 + i)
        cast_out.append({
            "id": cid,
            "role": (m.get("role") or "").strip(),
            "appearance": (m.get("appearance") or "").strip(),
            "wardrobe": (m.get("wardrobe") or "").strip(),
            "on_camera": (m.get("on_camera") or "").strip(),
            "scenes": [int(x) for x in (m.get("scenes") or []) if str(x).isdigit()],
        })
    if a.characters and a.characters > 0 and len(cast_out) != a.characters:
        log(f"  note: you said {a.characters} on camera, the read returned "
            f"{len(cast_out)} — keeping what it saw, check the cast panel")
    known_ids = {m["id"] for m in cast_out}

    scenes_out = []
    by_n = {int(s.get("n", 0)): s for s in (data.get("scenes") or []) if str(s.get("n", "")).strip()}
    for s in shots:
        c = by_n.get(s["n"], {})
        scenes_out.append({
            "n": s["n"],
            "characters": [str(x).strip() for x in (c.get("characters") or [])
                           if str(x).strip() in known_ids],
            "t_start": s["t_start"], "t_end": s["t_end"], "duration": s["duration"],
            "frames": [{"path": rel_to_root(Path(f["path"])), "t": f["t"]} for f in s["frames"]],
            "label": (c.get("label") or f"Scene {s['n']}").strip(),
            "function": (c.get("function") or "").strip(),
            "shot_type": (c.get("shot_type") or "").strip(),
            "angle": (c.get("angle") or "").strip(),
            "camera": (c.get("camera") or "").strip(),
            "subject": (c.get("subject") or "").strip(),
            "action": (c.get("action") or "").strip(),
            "setting": (c.get("setting") or "").strip(),
            "props": [str(p) for p in (c.get("props") or [])][:12],
            "wardrobe": (c.get("wardrobe") or "").strip(),
            "lighting": (c.get("lighting") or "").strip(),
            "color": (c.get("color") or "").strip(),
            "mood": (c.get("mood") or "").strip(),
            "on_screen_text": (c.get("on_screen_text") or "").strip(),
            # the transcript is ground truth for speech; the model only fills a gap
            "spoken": s["spoken"] or (c.get("spoken") or "").strip(),
            "cut_in": (c.get("cut_in") or "").strip(),
            "audio_note": (c.get("audio_note") or "").strip(),
            "retention_note": (c.get("retention_note") or "").strip(),
            "how_to_shoot": (c.get("how_to_shoot") or "").strip(),
            "i2v_prompt": (c.get("i2v_prompt") or "").strip(),
        })

    summary = data.get("summary") or {}
    vo = (data.get("vo_script") or "").strip()
    if not vo and segs:
        vo = " ".join(s["text"] for s in segs).strip()

    read = {
        "batch": batch,
        "source": {"name": src.name, "path": rel_to_root(src), **info},
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "model": a.model,
        "stats": {
            "cuts": len(cuts),
            "scenes": len(shots),
            "frames_read": got,
            "avg_shot_s": round(info["duration"] / max(1, len(shots)), 2),
            "duration_s": round(info["duration"], 2),
            "words": len(vo.split()) if vo else 0,
            "wpm": round(len(vo.split()) / (info["duration"] / 60), 1) if vo and info["duration"] else 0,
        },
        "summary": {k: (str(summary.get(k) or "").strip()) for k in (
            "one_line", "format", "hook_type", "product", "target_viewer", "pacing",
            "audio", "on_screen_text_style", "production_level", "why_it_works",
            "weakest_link")},
        "cast": cast_out,
        "characters_told": a.characters or 0,
        "scenes": scenes_out,
        "vo_script": vo,
        "text_overlays": [str(t) for t in (data.get("text_overlays") or [])][:40],
        "reshoot_brief": (data.get("reshoot_brief") or "").strip(),
        # word timings did their job at scene-splitting time; keep the file readable
        "transcript": [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in segs],
        "brief": (a.brief or "").strip(),
    }

    (work / "read.json").write_text(json.dumps(read, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    (work / "script.md").write_text(render_markdown(read), encoding="utf-8")
    (work / "source.txt").write_text(str(src), encoding="utf-8")

    log("")
    log(f"Wrote {work / 'read.json'}")
    log(f"Wrote {work / 'script.md'}")
    log(f"BATCH {batch}")


def rel_to_root(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def render_markdown(r: dict) -> str:
    s, st = r["summary"], r["stats"]
    L: list[str] = []
    L.append(f"# Frame read — {r['source']['name']}")
    L.append("")
    L.append(f"*{r['created']} · {st['duration_s']}s · {st['scenes']} scenes · "
             f"{st['cuts']} cuts · avg shot {st['avg_shot_s']}s"
             + (f" · {st['wpm']} wpm" if st["wpm"] else "") + "*")
    L.append("")
    if s.get("one_line"):
        L.append(f"**{s['one_line']}**")
        L.append("")
    for label, key in (("Format", "format"), ("Hook", "hook_type"), ("Product", "product"),
                       ("Target viewer", "target_viewer"), ("Pacing", "pacing"),
                       ("Audio", "audio"), ("Caption style", "on_screen_text_style"),
                       ("Production level", "production_level")):
        if s.get(key):
            L.append(f"- **{label}:** {s[key]}")
    L.append("")
    if s.get("why_it_works"):
        L.append("## Why it works")
        L.append("")
        L.append(s["why_it_works"])
        L.append("")
    if s.get("weakest_link"):
        L.append(f"**Weakest link:** {s['weakest_link']}")
        L.append("")

    if r.get("cast"):
        L.append("## Cast")
        L.append("")
        L.append("*Described once here; the shot list refers back to these ids so a "
                 "reshoot casts one consistent person per role.*")
        L.append("")
        for m in r["cast"]:
            head = f"**{m['id']}"
            if m.get("role"):
                head += f" — {m['role']}"
            head += "**"
            if m.get("on_camera"):
                head += f"  ·  *{m['on_camera']}*"
            L.append(head)
            if m.get("appearance"):
                L.append(f"- **Appearance:** {m['appearance']}")
            if m.get("wardrobe"):
                L.append(f"- **Wardrobe:** {m['wardrobe']}")
            if m.get("scenes"):
                L.append(f"- **Appears in:** scenes {', '.join(str(x) for x in m['scenes'])}")
            L.append("")

    L.append("## Shot list")
    L.append("")
    for c in r["scenes"]:
        L.append(f"### {c['n']}. {c['label']}  ·  {c['t_start']:.2f}–{c['t_end']:.2f}s "
                 f"({c['duration']:.2f}s)")
        L.append("")
        head = " · ".join(x for x in (c["shot_type"], c["angle"], c["camera"]) if x)
        if head:
            L.append(f"`{head}`")
            L.append("")
        if c.get("characters"):
            L.append(f"- **Who's in it:** {', '.join(c['characters'])}")
        for label, key in (("Function", "function"), ("Subject", "subject"),
                           ("Action", "action"), ("Setting", "setting"),
                           ("Wardrobe", "wardrobe"), ("Lighting", "lighting"),
                           ("Colour", "color"), ("Mood", "mood"),
                           ("Cut in", "cut_in"), ("Audio", "audio_note")):
            if c.get(key):
                L.append(f"- **{label}:** {c[key]}")
        if c.get("props"):
            L.append(f"- **Props:** {', '.join(c['props'])}")
        if c.get("on_screen_text"):
            L.append(f"- **On-screen text:** `{c['on_screen_text']}`")
        if c.get("spoken"):
            L.append(f"- **Spoken:** “{c['spoken']}”")
        if c.get("retention_note"):
            L.append(f"- **Retention:** {c['retention_note']}")
        L.append("")
        if c.get("how_to_shoot"):
            L.append(f"**Shoot it:** {c['how_to_shoot']}")
            L.append("")
        if c.get("i2v_prompt"):
            L.append(f"**Image→Video prompt:** `{c['i2v_prompt']}`")
            L.append("")

    if r.get("text_overlays"):
        L.append("## On-screen text, in order")
        L.append("")
        for t in r["text_overlays"]:
            L.append(f"1. {t}")
        L.append("")
    if r.get("vo_script"):
        L.append("## Full voice-over")
        L.append("")
        L.append(r["vo_script"])
        L.append("")
    if r.get("reshoot_brief"):
        L.append("## Reshoot brief")
        L.append("")
        L.append(r["reshoot_brief"])
        L.append("")
    return "\n".join(L)


def cmd_health(a: argparse.Namespace) -> None:
    out = {
        "ffmpeg": Path(ff("ffmpeg")).is_file() or shutil.which("ffmpeg") is not None,
        "claude": bool(shutil.which("claude")
                       or (Path.home() / ".local/bin/claude.exe").exists()
                       or (Path.home() / ".local/bin/claude").exists()),
        "whisper": Path(CONFIG["venvs"]["whisper"]).is_file(),
        "out_root": str(OUT_ROOT),
    }
    print(json.dumps(out))


def main() -> None:
    ap = argparse.ArgumentParser(description="Frame Reader — winning video -> shot-by-shot script")
    sub = ap.add_subparsers(dest="cmd", required=True)

    rd = sub.add_parser("read", help="read a video into a scene-by-scene script")
    rd.add_argument("--video", required=True)
    rd.add_argument("--batch", help="output folder name under output/frame-reads/")
    rd.add_argument("--brief", default="", help="what the user plans to do with the read")
    rd.add_argument("--characters", type=int, default=0,
                    help="how many people are on camera (0 = let the reader work it out). "
                         "Locking this keeps one consistent person per role across every "
                         "scene instead of a fresh description per cut.")
    rd.add_argument("--max-scenes", type=int, default=24)
    rd.add_argument("--max-frames", type=int, default=40)
    rd.add_argument("--model", default="sonnet", choices=["sonnet", "opus", "haiku"])
    rd.add_argument("--no-transcribe", action="store_true")
    rd.add_argument("--whisper-python", default=CONFIG["venvs"].get("whisper"))
    rd.add_argument("--whisper-script",
                    default=str(Path(CONFIG["course_pipeline"]) / "transcribe.py"))
    rd.add_argument("--whisper-model", default="distil-large-v3")
    rd.set_defaults(func=cmd_read)

    hl = sub.add_parser("health", help="what the reader can do right now")
    hl.set_defaults(func=cmd_health)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
