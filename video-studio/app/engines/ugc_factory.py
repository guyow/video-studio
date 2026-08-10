#!/usr/bin/env python3
"""UGC Factory — analyze a reference UGC ad into a deep-dive teardown.

Mode `analyze` (the Phase-1 ingest step, driven from the /ugc tab):

  video -> transcript (faster-whisper, reused if the sidecar already exists)
        -> spread keyframes (ffmpeg)
        -> Claude vision teardown (hook / beats / avatar / mechanism / format)
        -> output/ugc/<stem>/{deepdive.md, narrative.md, transcript.txt, analysis.json}

The teardown follows the prospector deep-dive format the research bank already
uses, so a finished analysis reads like one entry of a competitor ad census.
Free end to end: local whisper + local ffmpeg + the claude CLI.

  python ugc_factory.py analyze --video <path> [--model opus] [--frames 12]
      [--whisper-python <venv-py> --whisper-script <transcribe.py> --uploads <dir>]
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

APP_DIR = Path(__file__).resolve().parent.parent          # video-studio/app
VS_ROOT = APP_DIR.parent                                   # video-studio/
CONFIG = json.loads((VS_ROOT / "config.json").read_text(encoding="utf-8"))
ROOT = Path(CONFIG["autovsl_root"])                        # data root (autoVSL repo)
OUT_ROOT = ROOT / "output" / "ugc"

FFMPEG_BIN = (Path(os.environ.get("LOCALAPPDATA", "")) /
              "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
              "/ffmpeg-8.1.2-full_build/bin")


def ff(name: str) -> str:
    exe = FFMPEG_BIN / f"{name}.exe"
    return str(exe) if exe.is_file() else name


def log(msg: str) -> None:
    print(msg, flush=True)


def ffprobe_json(path: Path) -> dict:
    r = subprocess.run(
        [ff("ffprobe"), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def video_specs(probe: dict) -> tuple[float, str]:
    dur = 0.0
    try:
        dur = float(probe.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        pass
    v = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
    w, h = v.get("width") or 0, v.get("height") or 0
    if w and h:
        ratio = w / h
        aspect = min((("9:16", 9 / 16), ("1:1", 1.0), ("4:5", 0.8), ("16:9", 16 / 9)),
                     key=lambda a: abs(ratio - a[1]))[0]
    else:
        aspect = "?"
    return dur, f"{w}x{h} ({aspect}) · {dur:.1f}s · codec {v.get('codec_name', '?')}"


def claude_exe() -> str:
    exe = shutil.which("claude")
    if exe:
        return exe
    for p in (Path.home() / ".local/bin/claude.exe", Path.home() / ".local/bin/claude"):
        if p.exists():
            return str(p)
    raise SystemExit("claude CLI not found — install Claude Code or add it to PATH.")


def ask_claude(prompt: str, cwd: Path, model: str, timeout: int = 900) -> str:
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    r = subprocess.run(
        [claude_exe(), "-p", "--model", model, "--allowedTools", "Read",
         "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch,Task"],
        input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, cwd=str(cwd), env=env)
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        raise RuntimeError(f"claude CLI failed (rc={r.returncode}): {(r.stderr or '')[:400]}")
    return out


# ------------------------------------------------------------------- transcript

def ensure_transcript(video: Path, transcripts: Path, whisper_python: str,
                      whisper_script: str, uploads: str) -> Path | None:
    """Return the whisper .json sidecar, running faster-whisper if it's missing."""
    sidecar = transcripts / f"{video.stem}.json"
    if sidecar.is_file():
        log(f"Transcript found: {sidecar.name}")
        return sidecar
    if not (whisper_python and whisper_script):
        log("No transcript and no whisper configured — analyzing visuals only.")
        return None
    log("Transcribing (faster-whisper, local GPU)…")
    r = subprocess.run([whisper_python, whisper_script, str(video), "--out", uploads],
                       cwd=str(ROOT))
    if r.returncode != 0 or not sidecar.is_file():
        log("WARNING: transcription failed — analyzing visuals only.")
        return None
    return sidecar


def transcript_lines(sidecar: Path | None) -> str:
    if not sidecar:
        return "(no speech / transcript unavailable)"
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return "(transcript unreadable)"
    lines = []
    for s in data.get("segments", []):
        t = s.get("text", "").strip()
        if t:
            lines.append(f"[{float(s.get('start', 0)):6.1f}s] {t}")
    return "\n".join(lines) or "(no speech detected)"


# ---------------------------------------------------------------------- frames

def extract_frames(video: Path, outdir: Path, count: int, dur: float) -> list[dict]:
    outdir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i in range(count):
        ts = dur * (i + 0.5) / count if dur > 0 else i
        out = outdir / f"f-{i + 1:02d}.jpg"
        if not out.is_file():
            subprocess.run([ff("ffmpeg"), "-y", "-loglevel", "error",
                            "-ss", f"{ts:.3f}", "-i", str(video),
                            "-frames:v", "1", "-q:v", "3", str(out)],
                           capture_output=True, timeout=120)
        if out.is_file():
            frames.append({"path": f"frames/{out.name}", "t": round(ts, 2)})
    return frames


# ---------------------------------------------------------------------- prompt

DEEPDIVE_PROMPT = """You are "prospector", a direct-response ad analyst. You are given one \
short-form UGC/VSL-style video ad to tear down: its full timed transcript and {nframes} \
keyframes sampled evenly across it. Read EVERY frame image with the Read tool (paths below, \
relative to the current directory), then write the teardown.

VIDEO SPECS: {specs}

KEYFRAMES (Read each one):
{frames}

TIMED TRANSCRIPT:
{transcript}

Respond with EXACTLY this structure — a markdown document, then one fenced json block, \
nothing else. No preamble.

# Deep-Dive — <short title you give this ad>

## 1. Executive read
3-6 bullets: what this ad is, who it targets, why it likely works (or doesn't), the single most stealable thing in it.

## 2. Format & production
Format class (talking-head UGC / silent meme montage / b-roll+VO / mixed…), spokesperson (who appears on camera, how many distinct people), production cost read (what was actually needed to shoot this), on-screen text usage, subtitle style if visible, brand/end-card treatment.

## 3. Beat structure
A numbered beat-by-beat map with timestamps: hook, problem/agitation, failed solutions, mechanism/root-cause reveal, product intro, proof/social-proof, ease/format close, CTA. Quote the load-bearing lines verbatim. Note which beats are missing — absence is information.

## 4. Avatar & awareness
Who the ad speaks to (age, life stage, pain cluster, identity language), awareness level (1-5) and sophistication stage (1-5) with one-line justification each.

## 5. Mechanism & claims
The core mechanism/argument, named ingredients or features, numeric anchors, and how claims are hedged for compliance (exact hedge language if present).

## 6. Narrative summary
One flowing paragraph (120-200 words) retelling the ad's story arc in plain language — this is the seed a copywriter would rewrite from, so capture the emotional logic, not just the facts.

## 7. What to steal / what to skip
Concrete, ranked: structures, phrases, and format choices worth cloning for our own product, and what to leave behind (with why).

```json
{{"title": "<short ad title>", "format_class": "<one of: talking-head-ugc | silent-montage | broll-vo | mixed | other>",
 "duration_s": <number>, "aspect": "<9:16|1:1|16:9|4:5>", "shots_estimate": <number of distinct shots/scenes>,
 "speakers_on_camera": <number>, "hook_text": "<verbatim opening hook line or on-screen text>",
 "awareness_level": <1-5>, "sophistication_stage": <1-5>,
 "avatar": "<one-line avatar label>", "mechanism": "<one-line core mechanism>",
 "beats": ["<beat name>", …], "has_subtitles": <true|false>, "has_end_card": <true|false>}}
```"""


# --------------------------------------------------------------------- analyze

def cmd_analyze(a: argparse.Namespace) -> None:
    video = Path(a.video).resolve()
    if not video.is_file():
        raise SystemExit(f"video not found: {video}")
    work = OUT_ROOT / video.stem
    work.mkdir(parents=True, exist_ok=True)

    probe = ffprobe_json(video)
    dur, specs = video_specs(probe)
    log(f"Analyzing: {video.name}  ({specs})")

    sidecar = ensure_transcript(video, Path(a.transcripts), a.whisper_python,
                                a.whisper_script, a.uploads)
    transcript = transcript_lines(sidecar)
    (work / "transcript.txt").write_text(
        re.sub(r"^\[\s*[\d.]+s\] ", "", transcript, flags=re.M) + "\n", encoding="utf-8")

    log(f"Extracting {a.frames} keyframes…")
    frames = extract_frames(video, work / "frames", a.frames, dur)
    if not frames:
        raise SystemExit("could not extract a single frame (ffmpeg failed?)")

    prompt = DEEPDIVE_PROMPT.format(
        nframes=len(frames), specs=specs,
        frames="\n".join(f"- {f['path']}   (t={f['t']}s)" for f in frames),
        transcript=transcript[:12000])
    log(f"Asking Claude ({a.model}) to read {len(frames)} frames and write the teardown "
        "(takes a couple of minutes)…")
    out = ask_claude(prompt, cwd=work, model=a.model)
    (work / "raw-claude.txt").write_text(out, encoding="utf-8")

    # split off the trailing fenced json meta block; everything before it is the doc
    meta = {}
    m = re.search(r"```json\s*(\{.*?\})\s*```\s*$", out, re.S)
    if m:
        try:
            meta = json.loads(m.group(1))
        except json.JSONDecodeError:
            log("WARNING: meta json block did not parse — keeping the markdown anyway.")
        out = out[:m.start()].rstrip() + "\n"
    # tolerate a chatty preamble or a ```markdown wrapper — the doc starts at "# "
    h = re.search(r"^#\s", out, re.M)
    if h:
        out = out[h.start():].strip()
        if out.endswith("```"):
            out = out[:-3].rstrip()
        out += "\n"
    else:
        # e.g. the video is a test pattern / not an ad — Claude explains instead
        log("Claude declined to write a teardown. Its reply:")
        log("  " + out.strip().replace("\n", "\n  ")[:1200])
        raise SystemExit("No teardown produced (full reply in raw-claude.txt).")

    (work / "deepdive.md").write_text(out, encoding="utf-8")

    # narrative.md = section 6, extracted so the recompose step can seed from it alone
    nm = re.search(r"##\s*6\.\s*Narrative summary\s*\n(.*?)(?=\n##\s|\Z)", out, re.S)
    narrative = (nm.group(1).strip() if nm else "") or "(narrative section missing — see deepdive.md)"
    (work / "narrative.md").write_text(narrative + "\n", encoding="utf-8")

    meta.update({"video": video.name, "analyzed": time.time(),
                 "duration_s": meta.get("duration_s") or round(dur, 1),
                 "frames": [f["path"] for f in frames],
                 "model": a.model, "transcript": bool(sidecar)})
    (work / "analysis.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                                        encoding="utf-8")

    log("")
    log(f"Teardown: {meta.get('title', video.stem)}")
    log(f"  format: {meta.get('format_class', '?')} · avatar: {meta.get('avatar', '?')}")
    log(f"  hook: {str(meta.get('hook_text', '?'))[:90]}")
    log(f"  awareness {meta.get('awareness_level', '?')}/5 · "
        f"sophistication {meta.get('sophistication_stage', '?')}/5 · "
        f"~{meta.get('shots_estimate', '?')} shots")
    log("")
    log(f"→ {work / 'deepdive.md'}")
    log(f"→ {work / 'narrative.md'}")
    log(f"→ {work / 'transcript.txt'}")


# -------------------------------------------------------------- variant script

VARIANT_PROMPT = """You are a direct-response copywriter. Below is an APPROVED spoken UGC ad \
script. Write variant #{index} of {total}: a fresh take that could run alongside the original \
in the same campaign.
- NEW opening hook — attack from a different angle than the original's first line (and than \
earlier variants would obviously take: vary between question, bold claim, confession, "POV", \
contrarian, story-drop).
- Keep the SAME beat structure, emotional logic, and product moments — this script is approved; \
you are re-skinning it, not rethinking it.
- Same length: {lo}-{hi} words (count them — the visuals are already cut to this length).
- Spoken language: contractions, short sentences. No headings, emojis, hashtags, stage \
directions, or quotation marks.
- Compliance: wellness/supplement product — no disease/medical claims, no cure/treat/heal \
language, no guaranteed outcomes; personal-experience framing is fine.
{learnings}
APPROVED SCRIPT:
{script}

Respond with ONLY the variant script text — no preamble, no explanation, no markdown."""


def learnings_block(path: Path, limit: int = 12) -> str:
    """Past approve/reject verdicts → prompt guidance, newest last."""
    if not path.is_file():
        return ""
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    rows = rows[-limit:]
    if not rows:
        return ""
    out = []
    for r in rows:
        verdict = str(r.get("verdict", "")).upper()
        reason = (r.get("reason") or "").strip()
        hook = (r.get("params", {}).get("hook") or "").strip()
        bits = [b for b in (f'hook "{hook[:80]}"' if hook else "", reason[:160]) if b]
        out.append(f"- {verdict}: " + (" — ".join(bits) if bits else "(no notes)"))
    return ("\nWHAT THE USER HAS APPROVED/REJECTED BEFORE (write toward the approved "
            "patterns, away from the rejected ones):\n" + "\n".join(out) + "\n")


def cmd_variant_script(a: argparse.Namespace) -> None:
    base = Path(a.base)
    script = base.read_text(encoding="utf-8", errors="replace").strip() if base.is_file() else ""
    if len(script.split()) < 5:
        raise SystemExit(f"base script missing or too short: {base}")
    words = len(script.split())
    prompt = VARIANT_PROMPT.format(
        index=a.index, total=a.total, lo=int(words * 0.9), hi=int(words * 1.1),
        learnings=learnings_block(Path(a.learnings)) if a.learnings else "",
        script=script)
    out_dir = Path(a.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"Writing script variant {a.index}/{a.total} ({a.model})…")
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    r = subprocess.run([claude_exe(), "-p", "--model", a.model], input=prompt,
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=600, cwd=str(out_dir), env=env)
    text = (r.stdout or "").strip()
    if r.returncode != 0 or len(text.split()) < 5:
        raise SystemExit(f"claude CLI failed (rc={r.returncode}): {(r.stderr or '')[:300]}")
    (out_dir / "script.txt").write_text(text + "\n", encoding="utf-8")
    # stuff the script into the recipe so the tags step can read the voiceover
    recipe_path = out_dir / "recipe.json"
    if recipe_path.is_file():
        try:
            recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
            recipe["script"] = text
            recipe_path.write_text(json.dumps(recipe, indent=1, ensure_ascii=False),
                                   encoding="utf-8")
        except json.JSONDecodeError:
            pass
    log("")
    log(text)
    log("")
    log(f"→ {out_dir / 'script.txt'} ({len(text.split())} words, base was {words})")


# -------------------------------------------------------------------- finalize

def cmd_finalize(a: argparse.Namespace) -> None:
    """Give the assembled preview a stable, unique deliverable name and record it
    on the analysis, so captioning and the UI never collide across previews."""
    out_dir = Path(a.out_dir).resolve()
    slug = out_dir.name
    src = out_dir / ("clip-tagged.mp4" if a.prefer_tagged and (out_dir / "clip-tagged.mp4").is_file()
                     else "clip.mp4")
    if not src.is_file():
        raise SystemExit(f"no assembled clip in {out_dir}")
    final = out_dir / f"{slug}.mp4"
    shutil.copy2(src, final)
    log(f"preview deliverable: {final}")

    work = OUT_ROOT / a.stem
    work.mkdir(parents=True, exist_ok=True)
    rc_path = work / "recompose.json"
    rc = {}
    if rc_path.is_file():
        try:
            rc = json.loads(rc_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            rc = {}
    previews = rc.setdefault("previews", [])
    previews.append({"slug": slug, "file": f"output/i2v/{slug}/{slug}.mp4",
                     "tagged": src.name == "clip-tagged.mp4",
                     "label": a.label or "preview",
                     "created": time.strftime("%Y-%m-%d %H:%M:%S")})
    rc["last_preview"] = slug
    rc_path.write_text(json.dumps(rc, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"recorded on output/ugc/{a.stem}/recompose.json ({len(previews)} preview(s))")


# ------------------------------------------------------------------------ mute

def cmd_mute(a: argparse.Namespace) -> None:
    """Sound-off twins: every deliverable also as a muted file, so each preview/
    variant ships as sound/no-sound x subs/no-subs."""
    out_dir = Path(a.out_dir).resolve()
    slug = out_dir.name
    made = 0
    pairs = [(out_dir / f"{slug}.mp4", out_dir / f"{slug}-mute.mp4")]
    cap = ROOT / "output" / "recaption" / slug / "captioned.mp4"
    if cap.is_file():
        pairs.append((cap, out_dir / f"{slug}-caption-mute.mp4"))
    for src, dst in pairs:
        if not src.is_file():
            continue
        r = subprocess.run([ff("ffmpeg"), "-y", "-loglevel", "error", "-i", str(src),
                            "-an", "-c:v", "copy", str(dst)], capture_output=True, timeout=600)
        if r.returncode == 0 and dst.is_file():
            log(f"muted twin: {dst.name}")
            made += 1
    if not made:
        log("no deliverables to mute (video had no sound to begin with?) — nothing to do")


def main() -> int:
    p = argparse.ArgumentParser(description="UGC Factory — reference ad in, teardown out")
    sub = p.add_subparsers(dest="cmd", required=True)

    an = sub.add_parser("analyze", help="video -> deepdive.md + narrative.md + transcript.txt")
    an.add_argument("--video", required=True)
    an.add_argument("--frames", type=int, default=12)
    an.add_argument("--model", default="opus", choices=["sonnet", "opus", "haiku"])
    an.add_argument("--transcripts", default=str(ROOT / "uploads" / "transcripts"))
    an.add_argument("--uploads", default=str(ROOT / "uploads"))
    an.add_argument("--whisper-python", default="")
    an.add_argument("--whisper-script", default="")
    an.set_defaults(func=cmd_analyze)

    vs = sub.add_parser("variant-script", help="approved script -> fresh-hook variant")
    vs.add_argument("--base", required=True, help="approved script .txt")
    vs.add_argument("--out-dir", dest="out_dir", required=True,
                    help="variant batch dir (writes script.txt, updates recipe.json)")
    vs.add_argument("--index", type=int, required=True)
    vs.add_argument("--total", type=int, required=True)
    vs.add_argument("--model", default="sonnet", choices=["sonnet", "opus", "haiku"])
    vs.add_argument("--learnings", default="", help="banks/ugc-learnings.jsonl")
    vs.set_defaults(func=cmd_variant_script)

    fi = sub.add_parser("finalize", help="stable-name the assembled preview + record it")
    fi.add_argument("--out-dir", dest="out_dir", required=True, help="output/i2v/<slug>")
    fi.add_argument("--stem", required=True, help="the analysis this preview belongs to")
    fi.add_argument("--prefer-tagged", dest="prefer_tagged", action="store_true")
    fi.add_argument("--label", default="", help="e.g. 'variant 3/5' — shown in the tab")
    fi.set_defaults(func=cmd_finalize)

    mu = sub.add_parser("mute", help="write sound-off twins of the deliverables")
    mu.add_argument("--out-dir", dest="out_dir", required=True, help="output/i2v/<slug>")
    mu.set_defaults(func=cmd_mute)

    a = p.parse_args()
    a.func(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
