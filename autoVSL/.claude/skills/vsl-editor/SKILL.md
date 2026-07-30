---
name: vsl-editor
description: Produce finished videos from a script + a source/avatar video — voice-clone, new voiceover, lip-sync, and burned social captions. Use for repurposing testimonials (swap the words a person says) and for rendering VSLs from a written script onto an avatar. Usage - describe the job and provide the source video(s) + script; the skill runs the fal.ai + ffmpeg pipeline in scripts/.
---

# VSL Editor — Media Production Engine

You turn WORDS + FOOTAGE into a FINISHED VIDEO. The upstream skills (`prospector`
→ `brand-platform-builder` → `vsl`) produce research, brand voice, and scripts.
You are the last stage: take a script and a source video and output a captioned MP4.

Everything runs on the project's `.venv`, `ffmpeg`, and a `FAL_KEY` in `.env`.
The engine is three Python scripts in `scripts/`. Read this whole file before running.

## Two workflows

### A. Testimonial repurposing — make a real person say new words
Source: an existing testimonial video. Goal: keep the person/footage, swap the script.
- **Single speaker:** `scripts/script-swap.py` (stages: transcribe → clone → speak → lipsync)
- **Two+ speakers:** `scripts/script-swap-duo.py` (clone each voice, time-anchor each line to the original speaker's turn, sync with active-speaker detection)

### B. VSL render — make an avatar deliver a written script (with captions)
Source: an avatar talking-head video + a written script. Goal: a finished captioned VSL.
- `scripts/vsl-render.py` (stages: clone → speak → lipsync → caption)

## THE CARDINAL RULE (never break)
**Always STOP and hand the transcript to the user for editing before generating any
voice or lip-sync.** For testimonials: transcribe first, show the text, WAIT for the
user's rewritten script. The whole point is swapping in new marketing copy — never
render the original words. (You may run `clone` in parallel while they edit — it does
not depend on the script.)

## Per-video triage (do this BEFORE rendering every source video)
Extract ~6 frames across the video and run diarization, then classify:
1. **Speaker count** — 1 → `script-swap.py`; 2+ → `script-swap-duo.py`.
2. **Mouth visible?** — if the speaker is masked / mouth hidden the whole time,
   SKIP lip-sync entirely: clone + new VO + `ffmpeg` mux audio over the original
   (no lip-sync cost, no artifacts).
3. **Old product on screen?** — if the source shows old/branded product, lip-sync
   the new VO onto a PRODUCT-FREE window of the footage (trim to a clean segment
   with `ffmpeg -ss <start> -t <len>` first, then lip-sync that clip). Verify the
   window is clean by extracting frames from it.

## Commands (single-speaker testimonial)
```bash
.venv/bin/python scripts/script-swap.py transcribe <video> --name <job>   # then STOP for edit
# user edits output/script-swap/<job>/script-edited.txt
.venv/bin/python scripts/script-swap.py clone <video> --name <job>        # $1.50, one-time per person
.venv/bin/python scripts/script-swap.py speak --name <job>                # new VO in cloned voice
.venv/bin/python scripts/script-swap.py lipsync <video> --name <job> --tier pro   # or standard
```
Finished testimonials land in `~/Desktop/liitt testimonial Ready/<job>-ready.mp4`.
`lipsync` skips if the output already exists — delete it to force a re-render.

## Commands (two-speaker testimonial)
Build `output/script-swap/<job>/duo-config.json` (see script-swap-duo.py header):
per-speaker `ref_windows` (clean solo audio, **≥10s total per speaker** — stitch
multiple windows) and `segments` [{start, end, speaker, text}] anchored to the
original diarized turns. Then:
```bash
.venv/bin/python scripts/script-swap-duo.py clone   --name <job>
.venv/bin/python scripts/script-swap-duo.py budget   --name <job>   # per-segment word targets so edits fit
.venv/bin/python scripts/script-swap-duo.py speak   --name <job>
.venv/bin/python scripts/script-swap-duo.py assemble --name <job>
.venv/bin/python scripts/script-swap-duo.py lipsync  --name <job> --tier pro
```

## Commands (full VSL from avatar + script)
```bash
# write the script to output/vsl/<name>/script.txt first
.venv/bin/python scripts/vsl-render.py clone   --name <name> --avatar <video>
.venv/bin/python scripts/vsl-render.py speak   --name <name>          # check VO length
.venv/bin/python scripts/vsl-render.py lipsync --name <name> --tier pro   # loop mode auto-used if VO>video
.venv/bin/python scripts/vsl-render.py caption --name <name> --out "<final folder>"
```
Finished VSLs land in the `--out` folder as `<name>.mp4`.

## Timing mechanism (lip-sync quality)
New VO lines rarely match the original mouth-movement duration → drift. The duo
pipeline fixes this by **fitting each line to its original [start,end] window**:
MiniMax `speed` pre-correction to land near target, then light `atempo`. Use the
`budget` command to give the user per-segment word counts so their edits fit
naturally (a line far off its window stretches badly). Same principle applies if a
single-speaker line drifts.

## Captions (VSL render)
- Word timestamps come from `fal-ai/whisper` on the VO (near-perfect since the
  script text is known). Cached in `output/vsl/<name>/words.json`.
- Style: bold white, heavy black outline, lower-center, ~3 words at a line (ASS
  subtitle burned with `ffmpeg`). Tune size/position in `vsl-render.py` constants.
- **Re-burning captions costs $0** — it reads cached `words.json` + local ffmpeg.
  To fix wording/spelling, edit `words.json` (keep timestamps) and re-run `caption`.
- **Brand spelling:** the caption renderer forces the brand to render as `līītt`
  (see `BRAND`/`BRAND_ALIASES` in vsl-render.py), lowercase even inside all-caps
  captions, and auto-corrects Whisper mishears (lit/litt/leet).

## Cost reference (fal.ai)
- Voice clone: **$1.50** one-time per person (reused forever; requires ≥10s ref audio).
- TTS (MiniMax speech-02-hd): ~$0.03–0.20 per video.
- Lip-sync sync.so: **pro $5/min, standard $3/min** of FINAL video (~$0.08/sec pro).
- Whisper transcribe/align: pennies.
- A ~30s testimonial re-edit ≈ $1.50 (pro). A 2-min VSL ≈ ~$11 (mostly lip-sync).

## Gotchas (learned the hard way)
- **fal balance "User is locked / Exhausted balance":** sometimes a STALE lock right
  after a top-up — retry a cheap call; if it succeeds, the lock cleared. If it stays
  locked, the balance is genuinely out. Small top-ups drain fast at pro $5/min.
- **MiniMax voice-clone needs ≥10s reference audio** — stitch multiple clean windows
  (concat with the audio FILTER, not `-c copy`; WAV headers break stream-copy concat).
- **MiniMax audio_setting sample_rate must be an integer** (32000), not a string.
- **Killing a lip-sync mid-run can leave a stale output file** → later runs see it as
  "cached" and skip. Delete the `-ready.mp4` when re-rendering with changed text.
- **Long script + short avatar:** VO longer than the footage → `sync_mode=loop`
  (auto in vsl-render.py). The footage visibly repeats; mask seams with punch-in
  zooms / B-roll for polish, or use a longer avatar take.
- Disk fills with frame extractions and temp WAVs — clean scratchpad/work dirs.

## B-roll bank integration

The b-roll inventory lives in `banks/broll.jsonl` (schema: `.claude/skills/prospector/references/output-schemas.md` §5). Before generating or sourcing new footage:

1. **Check inventory first.** Query `banks/broll.jsonl` by the edit plan's `emotional_beat` and `product_moment` tags (use the §5 vocabulary exactly). Prefer `quality: "A"` and matching `brand`/`avatar_fit`.
2. **Rights hard rule:** `rights: "reference_only"` clips NEVER enter a rendered output — they exist to brief generation or shooting. Only `owned`/`licensed` clips are cuttable.
3. **Missing beat → wishlist.** If no entry covers a beat, append a wishlist entry (same schema, `"status": "wishlist"`, no `file`) with the shot description and tags. Resolution order: prospector Mode 3 reference hunt → generation → shoot list.
4. **Bank what you make.** New usable clips (generated or delivered) get an entry immediately — file path under `assets/broll/`, usable segments, beat tags, rights — so inventory compounds across jobs.

## Handoff / where this sits
Inputs come from the `vsl` skill (script) and the user (source/avatar video, edits).
This skill is the production stage. See `docs/AGENTS.md` for how the four agents chain.
