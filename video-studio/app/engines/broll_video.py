#!/usr/bin/env python3
"""Script -> AI b-roll VIDEO. Orchestrates the existing B-Roll Factory into a
finished, narrated video — without touching broll_factory.py.

Two modes (each launched as a cost-appropriate server job):

  storyboard  a SCRIPT (+ optional reference video for voice/style) -> Claude
              turns it into a shot-list recipe.json in a new batch, one shot per
              narration beat, durations timed to the words. The recipe uses the
              EXACT broll_factory schema, so the existing /broll recipe review +
              Generate step render and produce the shot clips unchanged (free
              local Ken-Burns/AnimateDiff/LTX, or paid fal — cost-gated there).

  assemble    once the batch's shots are generated: clone the reference video's
              voice and narrate the SCRIPT (XTTS, free/local), concat the shot
              clips in order, pad to the narration length, mux the voiceover, and
              write output/i2v/<slug>/clip.mp4 + i2v.json so the finished video
              shows up under Media like any AI clip. No -shortest; length-guarded.

Runs under the cv venv; shells the `claude` CLI (storyboard) and the dubbing
venv's app.py (assemble narration). stdlib + ffmpeg only.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

MOTION_TYPES = ("push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down", "drift", "static")
WPS = 2.5  # spoken words per second, for timing shots to the narration


def log(m: str) -> None:
    print(m, flush=True)


def die(m: str) -> None:
    print(f"ERROR: {m}", flush=True)
    sys.exit(1)


def run(cmd: list[str], label: str, timeout: int | None = None, stdin: str | None = None) -> str:
    log(f"  {label}...")
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, input=stdin)
    if r.returncode != 0:
        die(f"{label} failed:\n{(r.stderr or r.stdout or '')[-1400:]}")
    return r.stdout or ""


def ff(args: list[str], label: str) -> None:
    run(["ffmpeg", "-y", "-loglevel", "error", *args], label)


def probe_sec(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    try:
        return float((r.stdout or "0").strip() or 0)
    except ValueError:
        return 0.0


def probe_wh(p: Path) -> tuple[int, int]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0:s=x", str(p)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        w, h = (r.stdout or "").strip().split("x")[:2]
        return int(w), int(h)
    except (ValueError, IndexError):
        return (1080, 1920)


def guard_len(out: Path, expected: float, tol: float = 0.8) -> None:
    got = probe_sec(out)
    if abs(got - expected) > tol:
        out.unlink(missing_ok=True)
        die(f"assembled length {got:.2f}s != narration {expected:.2f}s — deleted, not delivered off-length")


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40] or "script"


# ─────────────────────────────────────────────── storyboard (script -> recipe)
STORYBOARD_PROMPT = """You are a short-form video director. Turn the SCRIPT below into a shot list for AI-generated B-ROLL that plays UNDER the script as narration (no on-screen talking head — cutaway footage, product shots, lifestyle, abstract mood).

Rules:
- One shot per sentence / beat, in order. {shots_rule}
- Each shot's `duration_s` MUST roughly equal the time to speak that beat at ~2.5 words/sec (so the footage matches the voiceover). Keep 2.0–12.0.
- `prompt` is an SD1.5 image prompt: comma-separated visual tags, 25–45 words, describing ONE concrete frame (subject, setting, light, lens, mood). No text/words in the image.
- Choose a `motion.type` from: push_in, pull_out, pan_left, pan_right, tilt_up, tilt_down, drift, static — and `motion.intensity` 0.03–0.35.
- Tag `emotional_beat` (pain_mirror|agitation|hope|proof|calm|desire|resolution) and `product_moment` (before_state|during|after_state|product_hero|lifestyle).
{brand_block}
Respond with ONLY a JSON object: {{"style": {{"summary": "...", "pacing": "..."}}, "shots": [{{"title": "...", "prompt": "...", "negative": "", "motion": {{"type": "push_in", "intensity": 0.12}}, "duration_s": 4.0, "emotional_beat": "", "product_moment": "", "beat_tags": [], "avatar_fit": []}}]}}
No markdown, no code fences, no commentary.

SCRIPT:
{script}
"""

BRAND_BLOCK = """- Brand = liitt / Fairy Flame (premium mushroom-gummy wellness, A24-cinematic, warm, NOT stoner culture). Deep indigo "before" world; gold glow = the after-state; deep-magenta flame-shaped gummy is the hero. Avoid candy-bright and trippy cliches.
"""


def cmd_storyboard(a: argparse.Namespace) -> int:
    script = Path(a.script).read_text(encoding="utf-8", errors="replace").strip() if Path(a.script).is_file() else a.script
    if not script or len(script.split()) < 5:
        die("script is empty or too short")
    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)

    shots_rule = f"Aim for about {a.shots} shots total (merge or split beats to land near that)." if a.shots else "Use as many shots as the beats need (typically 4–10)."
    prompt = STORYBOARD_PROMPT.format(
        shots_rule=shots_rule, brand_block=(BRAND_BLOCK if a.brand else ""), script=script)

    claude = a.claude or "claude"
    log("storyboarding the script with Claude...")
    out = run([claude, "-p", "--model", a.model or "sonnet",
               "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch"],
              "Claude storyboard", timeout=900, stdin=prompt)
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        die(f"Claude did not return JSON:\n{out[:400]}")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        die(f"could not parse Claude JSON: {e}")

    raw = data.get("shots") or []
    shots = []
    for i, s in enumerate(raw, 1):
        p = (s.get("prompt") or "").strip()
        if not p:
            continue
        mo = s.get("motion") or {}
        mtype = mo.get("type") if mo.get("type") in MOTION_TYPES else "push_in"
        try:
            inten = max(0.03, min(0.35, float(mo.get("intensity", 0.12))))
        except (TypeError, ValueError):
            inten = 0.12
        try:
            dur = max(2.0, min(12.0, float(s.get("duration_s", 4.0))))
        except (TypeError, ValueError):
            dur = 4.0
        shots.append({
            "id": f"s{i}", "title": (s.get("title") or f"Shot {i}")[:80], "prompt": p,
            "negative": (s.get("negative") or "").strip(), "style_ref": "",
            "motion": {"type": mtype, "intensity": inten}, "duration_s": round(dur, 1),
            "emotional_beat": s.get("emotional_beat") or "", "product_moment": s.get("product_moment") or "",
            "beat_tags": s.get("beat_tags") or [], "avatar_fit": s.get("avatar_fit") or [],
        })
    if not shots:
        die("Claude returned no usable shots")

    recipe = {
        "batch": work.name, "created": time.time(), "brief": f"[from script] {script[:120]}",
        "brand": bool(a.brand), "aspect": a.aspect, "from_script": True,
        "script": script, "references": [], "frames": [],
        "style": data.get("style") or {}, "shots": shots,
    }
    (work / "recipe.json").write_text(json.dumps(recipe, indent=1), encoding="utf-8")
    (work / "script.txt").write_text(script + "\n", encoding="utf-8")
    total = sum(s["duration_s"] for s in shots)
    log("")
    log(f"OK storyboard: {len(shots)} shots, ~{total:.0f}s of footage planned")
    log("BATCH: " + work.name)
    return 0


# ─────────────────────────────────────────────── assemble (clips + VO -> video)
def _ordered_clips(batch: Path) -> list[Path]:
    root = batch.parents[2]                       # output/broll/<batch> -> repo root
    gen = batch / "generated.json"
    clips: list[Path] = []
    if gen.is_file():
        try:
            data = json.loads(gen.read_text(encoding="utf-8"))
            for e in sorted(data.get("generated") or [], key=lambda x: x.get("shot_id", "")):
                f = root / (e.get("file") or "")
                if f.is_file():
                    clips.append(f)
        except json.JSONDecodeError:
            pass
    if not clips:                                  # fallback: whatever is in clips/
        cdir = batch / "clips"
        if cdir.is_dir():
            clips = sorted(cdir.glob("*.mp4"), key=lambda p: p.stem)
    return clips


def cmd_assemble(a: argparse.Namespace) -> int:
    batch = Path(a.batch).resolve()
    if not batch.is_dir():
        die(f"no such batch: {batch}")
    recipe = json.loads((batch / "recipe.json").read_text(encoding="utf-8")) if (batch / "recipe.json").is_file() else {}
    script = a.script or (batch / "script.txt").read_text(encoding="utf-8").strip() if (batch / "script.txt").is_file() else a.script
    clips = _ordered_clips(batch)
    if not clips:
        die("no generated shot clips yet — run Generate on this batch first")
    log(f"assembling {len(clips)} shot clip(s)")

    out_dir = Path(a.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    w, h = probe_wh(clips[0])

    # 1) narration (optional): clone the reference video's voice, speak the script
    vo = None
    vo_sec = 0.0
    if script and (a.ref_video or a.voice) and a.ds_py and a.ds_app:
        cmd = [a.ds_py, a.ds_app, "--cli", "--script", script, "--no-fit", "--device", "auto", "--language", "en"]
        if a.ref_video:
            cmd += ["--video", a.ref_video]
        if a.voice:
            cmd += ["--reference", a.voice]
        log("narrating the script with the cloned voice (XTTS)...")
        so = run(cmd, "XTTS narration", timeout=3600)
        wav = None
        for line in so.splitlines():
            if line.strip().lower().startswith("audio:"):
                wav = Path(line.split(":", 1)[1].strip())
        if wav and wav.is_file():
            vo = out_dir / "vo.mp3"
            ff(["-i", str(wav), "-c:a", "libmp3lame", "-b:a", "192k", str(vo)], "save narration")
            vo_sec = probe_sec(vo)
        else:
            log("  (narration produced no audio — assembling silent b-roll)")

    # 2) normalize + concat the shots in order
    norm = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1")
    inputs: list[str] = []
    filt: list[str] = []
    for i, c in enumerate(clips):
        inputs += ["-i", str(c)]
        filt.append(f"[{i}:v]{norm}[v{i}]")
    concat = "".join(f"[v{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[v]"
    silent = out_dir / "_broll-silent.mp4"
    ff([*inputs, "-filter_complex", ";".join(filt) + ";" + concat, "-map", "[v]",
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30", str(silent)],
       "concat shots")
    vid_sec = probe_sec(silent)

    # 3) if the footage is shorter than the narration, hold the last frame to fit
    if vo_sec and vid_sec + 0.05 < vo_sec:
        pad = round(vo_sec - vid_sec + 0.1, 2)
        padded = out_dir / "_broll-padded.mp4"
        ff(["-i", str(silent), "-vf", f"tpad=stop_mode=clone:stop_duration={pad}",
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30", str(padded)],
           f"hold last frame +{pad}s to cover narration")
        silent.unlink(missing_ok=True)
        padded.rename(silent)
        vid_sec = probe_sec(silent)

    # 4) mux narration (build to the VO length, never -shortest) or ship silent
    out = out_dir / "clip.mp4"
    if vo and vo_sec:
        ff(["-i", str(silent), "-i", str(vo), "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-filter:a", "apad", "-c:a", "aac", "-b:a", "192k",
            "-t", f"{vo_sec:.3f}", str(out)], "mux narration")
        guard_len(out, vo_sec)
        final = vo_sec
    else:
        ff(["-i", str(silent), "-c", "copy", str(out)], "finalize (silent)")
        final = vid_sec
    silent.unlink(missing_ok=True)

    (out_dir / "i2v.json").write_text(json.dumps({
        "name": out_dir.name, "prompt": (recipe.get("brief") or "b-roll video from script"),
        "model": "broll-video", "model_label": "Script -> AI B-Roll video",
        "aspect": recipe.get("aspect", "9:16"), "seconds": round(final, 2),
        "kind": "broll-video", "shots": len(clips), "narrated": bool(vo),
        "batch": batch.name, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, indent=1), encoding="utf-8")

    log("")
    log(f"OK assembled {len(clips)} shots -> {final:.1f}s {'narrated' if vo else 'silent'} video")
    log(f"deliverable: {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)

    sb = sub.add_parser("storyboard")
    sb.add_argument("--script", required=True, help="script text or a path to a .txt")
    sb.add_argument("--work", required=True, help="batch dir: output/broll/<batch>")
    sb.add_argument("--aspect", default="9:16", choices=("9:16", "1:1", "16:9"))
    sb.add_argument("--shots", type=int, default=0, help="target shot count (0 = let Claude decide)")
    sb.add_argument("--brand", action="store_true")
    sb.add_argument("--claude", help="path to the claude CLI")
    sb.add_argument("--model", default="sonnet", choices=("sonnet", "opus", "haiku"))

    asm = sub.add_parser("assemble")
    asm.add_argument("--batch", required=True, help="output/broll/<batch> (already generated)")
    asm.add_argument("--out-dir", dest="out_dir", required=True, help="output/i2v/<slug>")
    asm.add_argument("--script", default="", help="override; else uses the batch's script.txt")
    asm.add_argument("--ref-video", dest="ref_video", help="video to clone the narration voice from")
    asm.add_argument("--voice", help="reference voice wav (Voice Bank) instead of --ref-video")
    asm.add_argument("--ds-py", dest="ds_py", help="dubbing venv python (for narration)")
    asm.add_argument("--ds-app", dest="ds_app", help="dubbing-studio/app.py (for narration)")

    a = ap.parse_args()
    if a.mode == "storyboard":
        return cmd_storyboard(a)
    return cmd_assemble(a)


if __name__ == "__main__":
    raise SystemExit(main())
