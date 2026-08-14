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

MOTION_TYPES = ("push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down", "drift", "static")
WPS = 2.5  # spoken words per second, for timing shots to the narration

# ── UGC realism ─────────────────────────────────────────────────────────────
# The AI look comes from three things: too-perfect lighting, static tripod
# framing, and waxy skin. Real UGC is handheld, messy, phone-shot. Every shot
# prompt is composed as:  CHARACTER + scene action + REALISM  — so the face
# stays consistent and the phone-camera look is never left to chance. The
# anti-gloss terms also go in the NEGATIVE, where the sampler actually obeys.
UGC_CHARACTER = ("A woman in her late 30s, shoulder-length wavy light-brown hair with subtle "
                 "blonde ends, pearl stud earrings, cream ribbed oversized knit sweater, visible "
                 "skin texture and pores, faint under-eye shadows, no makeup look.")
UGC_REALISM = ("Shot on iPhone 15 front camera, vertical 9:16, handheld with subtle natural camera "
               "shake, amateur framing slightly off-center, natural window light with soft shadows, "
               "slight motion blur on movement, realistic skin texture, muted colors, mild sensor "
               "grain, cluttered lived-in home in background, candid documentary UGC style, not "
               "cinematic, no bokeh, no studio lighting.")
UGC_NEGATIVE = ("cinematic, film still, studio lighting, softbox, rim light, bokeh, shallow depth of "
                "field, waxy skin, airbrushed, plastic skin, beauty filter, flawless complexion, "
                "perfect symmetry, model pose, posing for camera, locked-off tripod shot, color "
                "graded, teal and orange, HDR, oversaturated, glossy, stock photo, 3d render, cgi, "
                "text, watermark, logo")
# The reference ladder — a full pain -> shift -> relief arc, already messy and candid.
# Scene 6 is deliberately un-staged: AI models love turning product moments into
# commercials, so she eats it while doing something else.
UGC_SCENES = [
    # NB: bed / bathroom framings kept tripping Veo & Sora person-safety filters —
    # these two beats now play in living-room / kitchen settings instead.
    "Sitting on the edge of a couch wrapped in a blanket scrolling her phone, dull expression, morning light through curtains, blinks slowly",
    "Leaning on a cluttered kitchen counter with a glass of water, sighs, tired eyes, dishes stacked behind her",
    "Sitting on the floor surrounded by a real messy laundry pile, hand on forehead, clothes actually wrinkled and tangled",
    "In kitchen mid-task, closes eyes and pinches bridge of nose, dishes stacked in sink behind her",
    "At laptop gripping a coffee mug, jaw tight, rubbing one eye, papers and charger cable on table",
    "Picks up a small red gummy from a jar on the counter, casual, eats it while doing something else - not presenting it to camera",
    "Journaling at a table, small natural smile, hair slightly messy, plant leaves clipping frame edge",
    "Making the bed with quick efficient movements, slight smile, one pillow still on the floor",
    "Folding laundry from the same pile, relaxed, folds are imperfect",
    "Laughing mid-conversation across a kitchen counter, caught mid-gesture, other person blurry in foreground",
]
UGC_BEATS = ["pain_mirror", "pain_mirror", "agitation", "agitation", "agitation",
             "hope", "proof", "proof", "calm", "resolution"]
UGC_MOTION = ["static", "drift", "static", "drift", "push_in",
              "push_in", "drift", "static", "drift", "static"]


def ugc_prompt(scene: str) -> str:
    """CHARACTER + scene action + REALISM — the composition that kills the AI look."""
    return f"{UGC_CHARACTER} {scene.strip().rstrip('.')}. {UGC_REALISM}"


def log(m: str) -> None:
    print(m, flush=True)


def die(m: str) -> None:
    print(f"ERROR: {m}", flush=True)
    sys.exit(1)


def run(cmd: list[str], label: str, timeout: int | None = None, stdin: str | None = None,
        cwd: Path | None = None) -> str:
    log(f"  {label}...")
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, input=stdin,
                       cwd=str(cwd) if cwd else None)
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

# UGC mode: Claude writes ONLY the scene action; the engine wraps it in the
# character + realism blocks. Asking a model for "cinematic" anything is what
# produces the AI look, so the vocabulary is banned from its half of the prompt.
UGC_STORYBOARD_PROMPT = """You are directing REAL-LOOKING UGC b-roll for a short vertical ad. The footage must look like a normal woman filmed it on her phone at home - NOT an ad, NOT cinematic.

Turn the SCRIPT below into a shot list, one shot per beat, in order. {shots_rule}

For each shot write ONLY the `prompt` as a SCENE ACTION LINE: what she is physically doing, where, and the mess around her. 8-20 words.
- Do NOT describe her appearance, the camera, the lens, the lighting, the film look, or the colour grade. Those are added automatically - if you write them the shot is ruined.
- Banned words in your scene lines: cinematic, beautiful, stunning, perfect, elegant, golden hour, bokeh, studio, professional, model.
- Real life only: dishes in the sink, unmade bed, laundry on the floor, charger cables, plant leaves clipping the frame, a half-drunk mug. Candid - she is never posing for the camera.
- If a beat is about the product, she uses it CASUALLY while doing something else (picks a small red gummy from a jar and eats it mid-task) - never presents it to camera, never a commercial product shot.
- `duration_s` MUST be between 2.0 and 3.0 - fast cuts. Long AI shots drift into uncanny.
- Choose `motion.type` from: push_in, pull_out, pan_left, pan_right, tilt_up, tilt_down, drift, static. Prefer static and drift (a phone is handheld, not on a slider). `motion.intensity` 0.03-0.12.
- Tag `emotional_beat` (pain_mirror|agitation|hope|proof|calm|desire|resolution) and `product_moment` (before_state|during|after_state|product_hero|lifestyle).
- No text or words anywhere in the image - captions get added later in the editor.

Good scene lines, for calibration:
  "Lying in bed under a duvet scrolling her phone, dull expression, blinks slowly"
  "In kitchen mid-task, closes eyes and pinches bridge of nose, dishes stacked in sink behind her"
  "Folding laundry from the same pile, relaxed, folds are imperfect"

Respond with ONLY a JSON object: {{"style": {{"summary": "...", "pacing": "..."}}, "shots": [{{"title": "...", "prompt": "...", "motion": {{"type": "static", "intensity": 0.06}}, "duration_s": 2.5, "emotional_beat": "", "product_moment": "", "beat_tags": [], "avatar_fit": []}}]}}
No markdown, no code fences, no commentary.

SCRIPT:
{script}
"""


CLONE_PROMPT = """You are reverse-engineering a UGC ad that ALREADY WORKS, then rebuilding it for a different product.

The keyframes of the reference video are on disk - READ THEM with the Read tool before answering:
{frames}
{ref_words}
Step 1 - study the reference: how it opens, how many shots, how fast it cuts, the settings and wardrobe, what the person is physically doing in each beat, where the product appears, how it closes.

Step 2 - rebuild that SAME structure for the NEW SCRIPT below. Same shot count and rhythm, same kind of settings and energy, same beat order - but the content follows the new script. This is modelling a winner, NOT copying it: never reproduce the reference's on-screen text, logos, or exact wording.

For each shot write ONLY the `prompt` as a SCENE ACTION LINE: what she is physically doing, where, and the mess around her. 8-20 words.
- Do NOT describe her appearance, the camera, the lens, the lighting, or the film look - those are added automatically. Writing them ruins the shot.
- Banned words: cinematic, beautiful, stunning, perfect, elegant, golden hour, bokeh, studio, professional, model.
- Real life only: dishes in the sink, unmade bed, laundry on the floor, charger cables, a half-drunk mug. She is never posing for the camera.
- Product beats stay CASUAL - she uses it while doing something else, never presents it to camera.
- `duration_s` 2.0-3.0 (fast cuts), matched to how long that beat's words take to say.
- `motion.type` from: push_in, pull_out, pan_left, pan_right, tilt_up, tilt_down, drift, static. Prefer static and drift. `motion.intensity` 0.03-0.12.
- Tag `emotional_beat` (pain_mirror|agitation|hope|proof|calm|desire|resolution) and `product_moment` (before_state|during|after_state|product_hero|lifestyle).
- No text or words in the image - captions are added later in the editor.

Respond with ONLY a JSON object: {{"style": {{"summary": "what makes the reference work", "pacing": "...", "structure": "the beat order you copied"}}, "shots": [{{"title": "...", "prompt": "...", "motion": {{"type": "static", "intensity": 0.06}}, "duration_s": 2.5, "emotional_beat": "", "product_moment": "", "beat_tags": [], "avatar_fit": []}}]}}
No markdown, no code fences, no commentary.

NEW SCRIPT (this is what the new video must say and show):
{script}
"""


PRODUCT_BLOCK = """
=== THE REAL PRODUCT (this is what the ad is selling) ===
{name_line}The actual product photo(s) are on disk - READ THEM with the Read tool before you write a single shot:
{images}{inspo}
Write the product beats around the object you can SEE in those photos, not a generic version of it: its real size in a hand, how it is opened or held, where it would sit in a normal home. Never describe it wrongly (do not invent a different colour, shape, container or label).

On EVERY shot add `"product_in_shot": true` or `false` - true only where the object is actually visible in frame. Exactly {n_shots} shot(s) should be true; those are the ones that get painted from the real photo, so choose the beats where seeing it matters most (the turn, the routine moment, the payoff). Everywhere else it is false.
"""

INSPO_BLOCK = """
These are LOOK references only - match their mood, colour and energy, never copy their content or text:
{images}"""


TAGS_PROMPT = """Write the ON-SCREEN TEXT for a UGC ad — the white sticker-text boxes viewers read while it plays.

Most people watch with the sound OFF, so these tags must carry the STORY on their own: read top to bottom they should make someone understand the whole before -> shift -> after arc without hearing a word.

Style (copy this exactly - it is the native TikTok look):
- 1 or 2 SHORT lines per shot, each line its own box. Line 1 is often a label ("Me:", "Day 1:", "2 weeks later:"), line 2 the beat ("Before Fairy Flame", "still exhausted").
- Max ~5 words a line. Lower case or sentence case, casual, first person, like a real person typed it.
- No hashtags, no emojis, no quote marks, no ALL CAPS shouting, no exclamation spam.
- Do NOT narrate what is visibly happening ("folding laundry"). Say what she is FEELING or what has CHANGED.
- The product beat is the turn - name it plainly once (e.g. "then I tried Fairy Flame").
- The last shot is the payoff + a soft nudge to the link below. Never write a URL.
- No medical claims, never promise a high; "clearer", "lighter", "like myself again" are the register.

Here are the shots in order (id · seconds · emotional beat · what happens):
{shots}
{script_block}
Respond with ONLY a JSON object mapping every shot id to its lines:
{{"s1": ["Me:", "before Fairy Flame"], "s2": ["still foggy by 10am"]}}
Every shot id must appear. No markdown, no code fences, no commentary.
"""


def cmd_tags(a: argparse.Namespace) -> int:
    """Write the on-screen story tags into an existing recipe's shots."""
    recipe_path = Path(a.recipe).resolve()
    if not recipe_path.is_file():
        die(f"no recipe at {recipe_path}")
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    shots = recipe.get("shots") or []
    if not shots:
        die("the recipe has no shots")
    lines = []
    for s in shots:
        scene = s.get("prompt", "")
        scene = scene.split("no makeup look. ")[-1].split(". Shot on iPhone")[0]
        lines.append(f"  {s['id']} · {s.get('duration_s')}s · {s.get('emotional_beat') or '-'} · "
                     f"{(s.get('title') or scene)[:70]}")
    script = (a.script or recipe.get("script") or "").strip()
    script_block = f"\nThe voiceover says:\n{script[:1200]}\n" if script else ""
    prompt = TAGS_PROMPT.format(shots="\n".join(lines), script_block=script_block)

    log(f"writing on-screen story tags for {len(shots)} shots...")
    out = run([a.claude or "claude", "-p", "--model", a.model or "sonnet",
               "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch"],
              "Claude tags", timeout=600, stdin=prompt)
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        die(f"Claude did not return JSON:\n{out[:300]}")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        die(f"could not parse tag JSON: {e}")

    n = 0
    for s in shots:
        v = data.get(s["id"])
        if not v:
            continue
        s["tag"] = [str(x).strip() for x in (v if isinstance(v, list) else [v]) if str(x).strip()][:2]
        n += 1
    recipe_path.write_text(json.dumps(recipe, indent=1), encoding="utf-8")
    log("")
    log(f"OK {n} shots tagged — the story reads with the sound off:")
    for s in shots:
        if s.get("tag"):
            log(f"  {s['id']:<4} {' / '.join(s['tag'])}")
    return 0


def extract_keyframes(video: Path, out_dir: Path, n: int = 6) -> list[Path]:
    """Evenly sample n frames across the reference so Claude can see its whole arc."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = probe_sec(video)
    if dur <= 0:
        die(f"could not read the reference video: {video}")
    frames = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        dest = out_dir / f"ref-{i + 1:02d}.jpg"
        ff(["-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
            "-vf", "scale='min(768,iw)':-2", "-q:v", "3", str(dest)], f"keyframe {i + 1}/{n}")
        if dest.is_file():
            frames.append(dest)
    if not frames:
        die("could not extract any keyframes from the reference video")
    return frames


def _stage_images(paths, dest_dir: Path, kind: str) -> list[Path]:
    """Copy the user's reference images into the batch so Claude (cwd=batch) can read them."""
    out: list[Path] = []
    for i, p in enumerate(paths or [], 1):
        src = Path(p).resolve()
        if not src.is_file():
            log(f"  (skipping missing {kind} image: {src})")
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{kind}-{i:02d}{src.suffix.lower() or '.png'}"
        try:
            shutil.copyfile(src, dest)
        except OSError as exc:
            log(f"  (could not stage {src.name}: {exc})")
            continue
        out.append(dest)
    return out


def cmd_storyboard(a: argparse.Namespace) -> int:
    script = Path(a.script).read_text(encoding="utf-8", errors="replace").strip() if Path(a.script).is_file() else a.script
    if not script or len(script.split()) < 5:
        die("script is empty or too short")
    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)

    ugc = bool(getattr(a, "ugc", False)) or a.preset == "ugc10"
    dur_lo, dur_hi = (2.0, 3.0) if ugc else (2.0, 12.0)
    # with 2-3s cuts, cover the narration: words/2.5 wps / ~2.5s per shot
    words = len(script.split())
    want = a.shots or (max(4, min(14, round(words / WPS / 2.5))) if ugc else 0)

    if a.preset == "ugc10":
        # the reference ladder, verbatim — no Claude, fully deterministic
        n = min(want or len(UGC_SCENES), len(UGC_SCENES))
        # sample ACROSS the ladder (not the first n) so the pain -> shift -> relief
        # arc survives a short script; scene 6 (the casual product beat) is forced in.
        if n >= len(UGC_SCENES):
            picks = list(range(len(UGC_SCENES)))
        else:
            picks = sorted({round(i * (len(UGC_SCENES) - 1) / (n - 1)) for i in range(n)} | {5})
            while len(picks) > n:                      # trimming keeps 0, 5 and the last
                picks.remove(next(p for p in picks if p not in (0, 5, picks[-1])))
        log(f"UGC preset: {len(picks)} scenes from the reference ladder (no Claude needed)")
        shots, style = [], {"summary": "handheld iPhone UGC, messy lived-in home, candid documentary",
                            "pacing": "fast 2-3s cuts"}
        for i, idx in enumerate(picks, 1):
            shots.append({
                "id": f"s{i}", "title": UGC_SCENES[idx][:60], "prompt": ugc_prompt(UGC_SCENES[idx]),
                "negative": UGC_NEGATIVE, "style_ref": "",
                "motion": {"type": UGC_MOTION[idx], "intensity": 0.06},
                "duration_s": 2.5, "emotional_beat": UGC_BEATS[idx],
                "product_moment": "product_hero" if idx == 5 else ("before_state" if idx < 5 else "after_state"),
                "product_in_shot": idx == 5,      # scene 6 is the casual product beat
                "beat_tags": ["ugc", "handheld"], "avatar_fit": [],
            })
    else:
        ref = Path(a.ref).resolve() if getattr(a, "ref", None) else None
        claude = a.claude or "claude"
        tools = ["--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch"]

        # The user's real object + any look references, shown to Claude directly.
        # They are copied INTO the batch first: the CLI runs with cwd=work and only
        # reads freely inside it, so an absolute path to _uploads would be refused.
        prods = _stage_images(getattr(a, "product", None), work / "product", "product")
        inspos = _stage_images(getattr(a, "inspiration", None), work / "product", "inspiration")
        product_block = ""
        if prods:
            nm = (getattr(a, "product_name", "") or "").strip()
            product_block = PRODUCT_BLOCK.format(
                name_line=(f"It is: {nm}\n" if nm else ""),
                images="\n".join(f"  {p}" for p in prods),
                inspo=(INSPO_BLOCK.format(images="\n".join(f"  {p}" for p in inspos)) if inspos else ""),
                n_shots=max(1, min(6, int(getattr(a, "product_shots", 0) or 2))))
        elif inspos:
            product_block = "\n=== LOOK REFERENCES ===" + INSPO_BLOCK.format(
                images="\n".join(f"  {p}" for p in inspos)) + "\n"

        if ref and ref.is_file():
            # CLONE a proven UGC ad: read its visual DNA + structure, rebuild for this script
            log(f"studying the reference video: {ref.name}")
            frames = extract_keyframes(ref, work / "refs", n=max(4, min(10, a.frames or 6)))
            ref_words = ""
            rt = (a.ref_script or "").strip()
            if rt:
                ref_words = ("\nThe reference says (its transcript - study its hook and pacing, "
                             f"do NOT reuse its words):\n{rt[:1500]}\n")
            prompt = CLONE_PROMPT.format(
                frames="\n".join(f"  {f}" for f in frames), ref_words=ref_words, script=script)
            tools = ["--allowedTools", "Read"] + tools
            log("rebuilding its structure for your script (Claude vision)...")
        else:
            shots_rule = (f"Aim for about {want} shots total (merge or split beats to land near that)."
                          if want else "Use as many shots as the beats need (typically 4-10).")
            tmpl = UGC_STORYBOARD_PROMPT if ugc else STORYBOARD_PROMPT
            prompt = (tmpl.format(shots_rule=shots_rule, script=script) if ugc else
                      tmpl.format(shots_rule=shots_rule, brand_block=(BRAND_BLOCK if a.brand else ""), script=script))
            log(f"storyboarding the script with Claude{' (UGC realism)' if ugc else ''}...")

        if product_block:
            prompt += "\n" + product_block
            if "--allowedTools" not in tools:
                tools = ["--allowedTools", "Read"] + tools
            log(f"showing Claude your product ({len(prods)} photo(s))"
                + (f" + {len(inspos)} look reference(s)" if inspos else "") + "...")

        out = run([claude, "-p", "--model", a.model or "sonnet", *tools],
                  "Claude storyboard", timeout=900, stdin=prompt, cwd=work)
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            die(f"Claude did not return JSON:\n{out[:400]}")
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            die(f"could not parse Claude JSON: {e}")

        raw = data.get("shots") or []
        style = data.get("style") or {}
        shots = []
        for i, s in enumerate(raw, 1):
            p = (s.get("prompt") or "").strip()
            if not p:
                continue
            mo = s.get("motion") or {}
            mtype = mo.get("type") if mo.get("type") in MOTION_TYPES else ("static" if ugc else "push_in")
            try:
                inten = max(0.03, min(0.12 if ugc else 0.35, float(mo.get("intensity", 0.06 if ugc else 0.12))))
            except (TypeError, ValueError):
                inten = 0.06 if ugc else 0.12
            try:
                dur = max(dur_lo, min(dur_hi, float(s.get("duration_s", 2.5 if ugc else 4.0))))
            except (TypeError, ValueError):
                dur = 2.5 if ugc else 4.0
            neg = (s.get("negative") or "").strip()
            shots.append({
                "id": f"s{i}", "title": (s.get("title") or f"Shot {i}")[:80],
                # UGC: wrap the scene action in the character + realism blocks
                "prompt": ugc_prompt(p) if ugc else p,
                "negative": (UGC_NEGATIVE + (", " + neg if neg else "")) if ugc else neg,
                "style_ref": "", "motion": {"type": mtype, "intensity": inten},
                "duration_s": round(dur, 1),
                "emotional_beat": s.get("emotional_beat") or "", "product_moment": s.get("product_moment") or "",
                # true = this beat gets painted from the real product photo
                "product_in_shot": bool(s.get("product_in_shot")),
                "beat_tags": (s.get("beat_tags") or []) + (["ugc"] if ugc else []),
                "avatar_fit": s.get("avatar_fit") or [],
            })
        if not shots:
            die("Claude returned no usable shots")

    recipe = {
        "batch": work.name, "created": time.time(), "brief": f"[from script] {script[:120]}",
        "brand": bool(a.brand), "aspect": a.aspect, "from_script": True, "ugc": ugc,
        "script": script, "references": [], "frames": [],
        "style": style, "shots": shots,
    }
    (work / "recipe.json").write_text(json.dumps(recipe, indent=1), encoding="utf-8")
    (work / "script.txt").write_text(script + "\n", encoding="utf-8")
    total = sum(s["duration_s"] for s in shots)
    log("")
    log(f"OK storyboard: {len(shots)} shots, ~{total:.0f}s of footage planned")
    hero = [s["id"] for s in shots if s.get("product_in_shot")]
    if hero:
        log(f"your product is in: {', '.join(hero)} — those shots get painted from the real photo")
    log("BATCH: " + work.name)
    return 0


# ─────────────────────────────────────────────── assemble (clips + VO -> video)
def _shot_num(sid: str) -> tuple[int, str]:
    """Natural order: s2 before s10 (plain string sort puts s10 second)."""
    m = re.search(r"(\d+)", sid or "")
    return (int(m.group(1)) if m else 10**6, sid or "")


def _ordered_clips(batch: Path) -> list[Path]:
    root = batch.parents[2]                       # output/broll/<batch> -> repo root
    gen = batch / "generated.json"
    clips: list[Path] = []
    if gen.is_file():
        try:
            data = json.loads(gen.read_text(encoding="utf-8"))
            for e in sorted(data.get("generated") or [],
                            key=lambda x: _shot_num(x.get("shot_id", ""))):
                raw = e.get("file") or ""
                f = Path(raw)
                if not f.is_absolute():           # engines write rel-to-root or absolute
                    f = root / raw
                if f.is_file():
                    clips.append(f)
        except json.JSONDecodeError:
            pass
    if not clips:                                  # fallback: whatever is in clips/
        cdir = batch / "clips"
        if cdir.is_dir():
            clips = sorted(cdir.glob("*.mp4"), key=lambda p: _shot_num(p.stem))
    return clips


def banner_card(img: Path, dest: Path, w: int, h: int, seconds: float) -> Path:
    """A still end card at the video's exact size: the banner over a blurred fill of itself."""
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
          f"gblur=sigma=28,setsar=1[bg];"
          f"[1:v]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
          f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30,format=yuv420p")
    ff(["-loop", "1", "-t", f"{seconds:.2f}", "-i", str(img),
        "-loop", "1", "-t", f"{seconds:.2f}", "-i", str(img),
        "-filter_complex", vf, "-c:v", "libx264", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", "30", str(dest)], "build the offer end card")
    return dest


def banner_flash(video: Path, img: Path, dest: Path, w: int, h: int,
                 at: float, seconds: float) -> Path:
    """Punch the banner over the footage for a beat, centred, ~78% wide."""
    bw = int(w * 0.78) // 2 * 2
    vf = (f"[1:v]scale={bw}:-2[b];"
          f"[0:v][b]overlay=(W-w)/2:(H-h)/2:enable='between(t,{at:.2f},{at + seconds:.2f})'")
    ff(["-i", str(video), "-i", str(img), "-filter_complex", vf,
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30", str(dest)],
       "flash the offer banner")
    return dest


def has_audio(p: Path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return "audio" in (r.stdout or "")


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
    # Narration is a BONUS, never a blocker: the shot clips are already paid for,
    # so any voice problem downgrades to a silent cut instead of losing the video.
    voice_src = a.voice or a.ref_video
    if script and voice_src and a.ds_py and a.ds_app:
        if a.voice or has_audio(Path(a.ref_video)):
            cmd = [a.ds_py, a.ds_app, "--cli", "--script", script, "--no-fit",
                   "--device", "auto", "--language", "en"]
            if a.ref_video:
                cmd += ["--video", a.ref_video]
            if a.voice:
                cmd += ["--reference", a.voice]
            log("narrating the script with the cloned voice (XTTS)...")
            try:
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
                    log("  narration produced no audio — assembling the video silent")
            except SystemExit:
                vo = None
                log("  narration failed — assembling the video SILENT so the shots aren't lost")
                log("  (add a voice later: pick a Voice Bank voice, or use a reference clip that has speech)")
        else:
            log(f"  reference '{Path(a.ref_video).name}' has no audio track — nothing to clone a voice from.")
            log("  assembling silent; pick a Voice Bank voice to narrate it.")

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

    # 2b) the offer banner, flashed over the middle of the footage
    banner = Path(a.banner).resolve() if getattr(a, "banner", None) else None
    if banner and not banner.is_file():
        log(f"  (banner image not found: {banner} — skipping it)")
        banner = None
    bsec = max(0.5, min(6.0, float(getattr(a, "banner_seconds", 0) or 2.5)))
    bmode = getattr(a, "banner_mode", "end") or "end"
    if banner and bmode == "flash":
        flashed = out_dir / "_broll-flash.mp4"
        at = max(0.0, vid_sec * 0.55 - bsec / 2)
        log(f"flashing the offer banner at {at:.1f}s for {bsec:.1f}s")
        banner_flash(silent, banner, flashed, w, h, at, bsec)
        silent.unlink(missing_ok=True)
        flashed.rename(silent)
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

    # 3b) the offer end card, held after the last shot (the narration keeps
    #     playing under it, so it lands as a closing frame, not a dead tail)
    if banner and bmode == "end":
        card = banner_card(banner, out_dir / "_banner-card.mp4", w, h, bsec)
        joined = out_dir / "_broll-carded.mp4"
        ff(["-i", str(silent), "-i", str(card),
            "-filter_complex", f"[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30", str(joined)],
           f"append the {bsec:.1f}s offer end card")
        silent.unlink(missing_ok=True)
        card.unlink(missing_ok=True)
        joined.rename(silent)
        vid_sec = probe_sec(silent)
        log(f"offer end card added — {bsec:.1f}s")

    # 4) mux narration (build to the LONGER of voice/footage, never -shortest)
    #    or ship silent. The end card sits past the narration, so the target
    #    length is the video's, not the voice's.
    out = out_dir / "clip.mp4"
    if vo and vo_sec:
        target = max(vo_sec, vid_sec)
        ff(["-i", str(silent), "-i", str(vo), "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-filter:a", "apad", "-c:a", "aac", "-b:a", "192k",
            "-t", f"{target:.3f}", str(out)], "mux narration")
        guard_len(out, target)
        final = target
    else:
        ff(["-i", str(silent), "-c", "copy", str(out)], "finalize (silent)")
        final = vid_sec
    silent.unlink(missing_ok=True)

    (out_dir / "i2v.json").write_text(json.dumps({
        "name": out_dir.name, "prompt": (recipe.get("brief") or "b-roll video from script"),
        "model": "broll-video", "model_label": "Script -> AI B-Roll video",
        "aspect": recipe.get("aspect", "9:16"), "seconds": round(final, 2),
        "kind": "broll-video", "shots": len(clips), "narrated": bool(vo),
        "product": (recipe.get("product") or {}).get("name") or "",
        "product_stills": (recipe.get("product") or {}).get("stills") or 0,
        "banner": (banner.name if banner else ""), "banner_mode": (bmode if banner else ""),
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
    sb.add_argument("--shots", type=int, default=0, help="target shot count (0 = auto)")
    sb.add_argument("--brand", action="store_true")
    sb.add_argument("--ugc", action="store_true",
                    help="UGC realism: handheld phone look, consistent character, 2-3s fast cuts")
    sb.add_argument("--preset", default="", choices=("", "ugc10"),
                    help="ugc10 = the 10-scene reference ladder, verbatim (skips Claude)")
    sb.add_argument("--ref", help="a proven UGC video to model: its structure/pacing is rebuilt for this script")
    sb.add_argument("--ref-script", dest="ref_script", default="",
                    help="the reference's transcript (its hook/pacing informs the rebuild)")
    sb.add_argument("--frames", type=int, default=6, help="keyframes to sample from the reference")
    sb.add_argument("--product", action="append", default=[],
                    help="photo of YOUR real product — Claude writes the beats around it (repeatable)")
    sb.add_argument("--inspiration", action="append", default=[],
                    help="look/mood reference image, never copied (repeatable)")
    sb.add_argument("--product-name", dest="product_name", default="",
                    help="what the product is, in words")
    sb.add_argument("--product-shots", dest="product_shots", type=int, default=2,
                    help="how many shots should actually show the product (1-6)")
    sb.add_argument("--claude", help="path to the claude CLI")
    sb.add_argument("--model", default="sonnet", choices=("sonnet", "opus", "haiku"))

    tg = sub.add_parser("tags")
    tg.add_argument("--recipe", required=True, help="recipe.json to write on-screen tags into")
    tg.add_argument("--script", default="")
    tg.add_argument("--claude")
    tg.add_argument("--model", default="sonnet")

    asm = sub.add_parser("assemble")
    asm.add_argument("--batch", required=True, help="output/broll/<batch> (already generated)")
    asm.add_argument("--out-dir", dest="out_dir", required=True, help="output/i2v/<slug>")
    asm.add_argument("--script", default="", help="override; else uses the batch's script.txt")
    asm.add_argument("--ref-video", dest="ref_video", help="video to clone the narration voice from")
    asm.add_argument("--voice", help="reference voice wav (Voice Bank) instead of --ref-video")
    asm.add_argument("--banner", help="offer / sale banner image to show with the video")
    asm.add_argument("--banner-mode", dest="banner_mode", default="end",
                     choices=("end", "flash"),
                     help="end = a closing card after the last shot; flash = punched over the middle")
    asm.add_argument("--banner-seconds", dest="banner_seconds", type=float, default=2.5,
                     help="how long the banner is on screen (0.5-6)")
    asm.add_argument("--ds-py", dest="ds_py", help="dubbing venv python (for narration)")
    asm.add_argument("--ds-app", dest="ds_app", help="dubbing-studio/app.py (for narration)")

    a = ap.parse_args()
    if a.mode == "storyboard":
        return cmd_storyboard(a)
    if a.mode == "tags":
        return cmd_tags(a)
    return cmd_assemble(a)


if __name__ == "__main__":
    raise SystemExit(main())
