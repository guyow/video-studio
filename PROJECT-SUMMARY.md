# Video Studio — Developer Handoff

*Workspace: `C:\Users\guyas\Claude\Projects\Video AI editing\` · Last updated: 2026-07-18*

## What this is

A local-first **video ad production system** for the liitt / Fairy Flame brand. It takes raw UGC/testimonial footage and produces ready-to-launch ads: transcribe → rewrite the script → voice-clone + dub → lip-sync → repair visual AI artifacts → burn captions → export. A newer layer clones proven "winner" ads into fresh variants, and a Brand Studio generates static image ads. Everything runs through one Flask web app on **http://localhost:5180** (phone access on the LAN behind a PIN).

Design philosophy throughout: **free/local engines by default** (XTTS, Wav2Lip, GFPGAN, OpenCV on a 4 GB RTX 3050 Ti), with paid fal.ai cloud engines behind explicit cost-confirmation gates and a spend tracker.

---

## 1. Repository / directory map

| Path | What it is | Version control |
|---|---|---|
| `video-studio/` | **The app.** Flask server + engines + 15 static HTML tabs | ⚠️ **NOT a git repo** |
| `autoVSL/` | Data root + original pipeline (uploads, workdirs, banks, fal.ai scripts, dashboard engine helpers) | git repo, **31 uncommitted changes** |
| `dubbing-studio/` | Standalone local voice-clone tool (Coqui XTTS v2, own venv) | not a repo |
| `subtitle-studio/` | Subtitle eraser / re-captioner (engines reused by the app) | not a repo |
| `tools/Wav2Lip/` | Wav2Lip + GFPGAN weights; `inference.py` is locally patched | vendored |
| `tools/ProPainter/`, `CodeFormer/` | inpainting / face-restore weights | vendored |
| `course_pipeline/` | faster-whisper venv (transcription) | — |
| `ComfyUI_windows_portable/` | ComfyUI for Brand Studio imagery (`--lowvram`, torch cu126) | portable install |

**⚠️ First job for a dev: put `video-studio/` under git and commit the autoVSL changes.** Nothing is version-controlled where it matters most.

### Key files in `video-studio/`
- `app/server.py` (~3,700 lines) — all routes, job orchestration, LLM calls. The heart.
- `app/jobs.py` — persistent job store (`jobs/jobs.json`), GPU lock, keep-awake.
- `app/engines/` — `visual_repair.py`, `object_repair.py`, `frame_swap.py`, `dubsync_repair.py`, `brand_content.py`, `compositor.py`.
- `app/static/` — one HTML file per tab + `vs-nav.js` (shared shell nav) + `vs.css`.
- `config.json` — **all paths, venvs, ports, PIN live here.** No hardcoded paths in engines.
- `docs/MIGRATION-PLAN.md` — running changelog of every feature with dates.

### External engine helpers (called by the app, live in autoVSL)
- `autoVSL/dashboard/local_dub.py` — free dub chain: XTTS voice → Wav2Lip/GFPGAN lip-sync.
- `autoVSL/dashboard/dub.py` — paid fal.ai dub chain (wraps `autoVSL/scripts/script-swap.py`).
- `autoVSL/dashboard/caption.py` — whisper word timing → ASS captions burned over the old subtitle band.
- `autoVSL/scripts/script-swap.py` — the original fal.ai pipeline (whisper / MiniMax voice clone / TTS / sync-lipsync), stage-cached per workdir.

---

## 2. Runtime & environments

| Venv (see `config.json → venvs`) | Used for |
|---|---|
| `autoVSL/.venv` (**"cv"**) — Python + OpenCV + numpy + Flask + Pillow | runs the server AND all repair engines |
| `course_pipeline/.venv` (**"whisper"**) — faster-whisper CUDA | transcription + caption word timing |
| `dubbing-studio/venv` (**"dub"**) — torch + TTS + Wav2Lip deps | XTTS synth, lip-sync inference |
| `tools/vsr/.venv` | video super-resolution (Power Tools) |

- **ffmpeg**: WinGet Gyan build; `job_env()` in server.py prepends it to PATH for every subprocess. Engines must never assume ffmpeg is on the system PATH.
- **GPU**: 4 GB RTX 3050 Ti. Hard constraints baked in: one GPU job at a time (`GPU_LOCK` + cross-app arbitration file), small Wav2Lip batches, ComfyUI `--lowvram`, SD1.5 generation at ≤576px then ESRGAN-upscale.
- **External services**: fal.ai (`autoVSL/.env → FAL_KEY`) for paid dubs; **headless `claude` CLI** (user's Claude subscription) for all LLM work — script rewriting, clone-script generation, and the DubSync vision advisor. No Anthropic API key involved.
- **Launch**: `.claude/launch.json` defines the `video-studio` dev server (the app is started via `autoVSL/.venv` python running `app/server.py`). Port **5180** is owned by Video Studio.

---

## 3. The web app

### Auth / remote
- Session-gated with a 6-digit PIN (`config.json → remote_pin`, currently `196941`); localhost bypasses the gate; LAN bind (`lan_access: true`) so a phone on the Wi-Fi can drive everything via `/remote` (mobile dashboard: jobs, spend, quick launches).

### Job system (the backbone)
- Every long operation is a **job**: `POST /api/run` (or a feature endpoint) → `jobs[id]` dict → background thread → `run_job()` streams stdout lines into `job["lines"]`.
- Persistent across restarts (`jobs/jobs.json`), resumable (`POST /api/job/<id>/resume` re-runs the recorded cmd — engines are stage-cached so they pick up where they left off), stoppable (`/stop` kills the process tree).
- GPU jobs `wait_for_gpu()` + acquire a lock so subtitle-studio / dubbing tools never fight over VRAM.
- **Money gate**: any fal.ai action requires `confirm_cost: true` in the request body, and `run_dub_job()` appends the estimated cost + running total (`/api/spend`) after each paid run. Cloud dubs are deliberately excluded from blind resume.

### The dub workdir — the central data structure
Everything revolves around `autoVSL/output/script-swap/<stem>/`:

```
final.mp4               ← the current deliverable (promote archives the old one as final.<ts>.mp4)
final-captioned.mp4     ← caption pass output
source.txt              ← absolute path to the original footage in uploads/
script-edited.txt       ← the script that was spoken
transcript.txt          ← original speech
new-vo.mp3              ← the generated voice-over (needed by captions + repairs)
voice.json              ← MiniMax paid voice-clone id (reusable, skips re-clone fee)
dub-config.json         ← which engine/tier made it
versions.json           ← metadata for every repair take
repair-*.mp4            ← repair takes (never overwrite final until promoted)
clone-info.json         ← present only on clones: {winner, actor, created}
caption-words.json      ← cached whisper word timing
```

A workdir with `final.mp4` automatically appears in **every** tab (DubSync, Captions, Exports, Clone Winner). New features should create/extend workdirs rather than invent new storage. Deliverables also copy to `~/Desktop/liitt testimonial Ready/<stem>-ready[-captioned].mp4`.

---

## 4. Tabs & features (13 tabs + login + remote)

| Tab | Route | What it does |
|---|---|---|
| Library | `/library` | uploads browser, thumbnails (`/api/thumb/...`), pipeline status per video |
| New Project | `/new` | upload + kick off transcribe |
| Transcript & Script | `/transcript` | edit transcript, **Claude rewrite** (`COPY_PROMPT`, length fitted to footage duration × measured words/sec), save `script-edited.txt` |
| Subtitle Recovery | `/subtitles` | erase burned-in subs (subtitle-studio engine, ProPainter-backed) |
| Dubbing & Lip Sync | `/dubbing` | run the dub: **local** (XTTS + Wav2Lip, free) or **fal.ai** (MiniMax/F5 voice + sync/veed/latentsync tiers, `confirm_cost`) |
| **Clone Winner** | `/clone` | scale a proven ad — see §5 |
| DubSync Repair | `/dubsync` | the repair suite — see §6 |
| Captions | `/captions` | word-timed bold ASS captions burned over the old caption band |
| QA Review | `/qc` | frame prober / side-by-side inspection |
| Exports | `/exports` | every deliverable in one list, copy to Desktop export dir |
| Brand Studio | `/brand-studio` | static image ads — see §7 |
| Ads Factory | `/creator` | the original autoVSL dashboard flows (hooks/angles banks, VSL assembly) |
| Power Tools | `/tools` | one-off utilities (VSR upscale etc.) |

---

## 5. Clone Winner (newest feature, 2026-07-18)

**Purpose**: take a winning ad and mass-produce variants — same winning structure/angle, fresh wording, same or different actor.

Flow (`app/static/clone.html`):
1. `GET /api/clone/winners` — every workdir with a final + script.
2. `GET /api/clone/actors` — every uploads video (actor choices).
3. `POST /api/clone/script` — headless `claude -p` with `CLONE_PROMPT`: keeps beats/angle/claims, rewrites everything else; word count = chosen-footage duration × the winner's measured speaking pace.
4. `POST /api/clone/run` — seeds a new workdir `<winner>-vN` (script-edited.txt + source.txt + clone-info.json; same-actor fal runs copy `voice.json` to skip the $1.50 clone fee), then launches `local_dub.py` (default, free) or `dub.py` (fal, cost-gated) with `--name <clone-stem>`. Registered as action `"dub"` so it inherits the GPU guard + spend tracking.
5. `GET /api/clone/list` — clone gallery with live job status.

Note: the server's generic `dub` action derives the workdir stem from the upload filename; the clone endpoints bypass it and call the engine scripts directly with `--name` — that's deliberate.

**Verified E2E**: `1783501512600.publer.com-2-v2` was produced fully locally ($0) — generated variant script, XTTS voice, Wav2Lip HD sync of 718 frames, captions — deliverables on the Desktop.

---

## 6. DubSync Repair suite (the deepest subsystem)

Fixes finished dubs without paying for a re-dub. Every repair writes a **take** (`repair-*.mp4`); the user promotes a take to `final.mp4` (old final archived). Six actions on `POST /api/dubsync/repair`:

1. **swap** (`frame_swap.py`) — user marks exact time ranges in the player (⏺/⏹); those frames are replaced 1:1 with the aligned original, 4-frame crossfade at edges, dub audio kept. The "I can see the bug, just obey me" tool.
2. **object** (`object_repair.py`) — "fix the cup but don't touch the lip-sync": dub is the base, restores ONLY the damaged object's pixels from the original. NCC tracking anchored to Claude-vision keyframes, face-tracked lip placement, diff-hysteresis damage mask, damage-based mouth-protection gate, ±2-frame temporal smoothing. **Read the invariants comment block before touching it** — every rule in there was earned by a real failure (tracker drift into background, interpolated lips on a walking subject, box-IoU gating misses).
3. **visual** (`visual_repair.py`) — inverse: original is the base, keeps only the dub's lip region (drawn/tracked box).
4. **relipsync / refit / renorm / remux** (`dubsync_repair.py`) — re-run local lip-sync, time-stretch VO, loudness, remux.
5. **Chat advisor** (`POST /api/dubsync/advise`) — user types "the cup gets warped when he drinks"; Claude vision reads 7 sampled frames and returns action + per-frame object/lip boxes that drive the engines. No manual box drawing.
6. **One-video drop** (`POST /api/dubsync/upload`) — drop only the damaged video; the backend content-fingerprints it (64px gray thumbs @0.5fps, ZNCC) against uploads, picks the match — preferring the **highest-bitrate** duplicate — and wires `source.txt`. 422 + asks for the original if <80% confidence.

**Frame-count integrity rule (project-wide)**: ffmpeg `-shortest` silently dropped trailing frames (660→657) — it is banned; encoders use `-frames:v N`, readers die loudly on early EOF, and every engine ffprobe-verifies output frame count == input, deleting the take on mismatch. Keep this guarantee in any new engine.

---

## 7. Brand Studio (static image ads)

SD1.5 on 4 GB VRAM cannot render text → **the model never draws brand text**. Pipeline (`brand_content.py`): ComfyUI generates background/hero imagery small (≤576px, brand style/negative prompts) → RealESRGAN ×4 tiled → downscale to exact platform px (1080×1080 first) → **`compositor.py`** (Pillow, deterministic) overlays eyebrow/headline/CTA/price + the **locked liitt wordmark PNG** using brand-kit hex/font tokens (hard-fails on missing tokens, never silently substitutes). Assets: `autoVSL/banks/brand-assets/{fonts,wordmark}/`, kit at `banks/liitt-brand-kit.json`. Copy generation reuses the banks (hooks/angles jsonl) + compliance validator. `GET /api/brand/health` preflights ComfyUI/fonts/wordmark.

---

## 8. Hard-won gotchas (do not re-learn these the hard way)

1. **`-shortest` is banned** (see §6). Always `-frames:v` + post-encode ffprobe count guard.
2. **Stock Wav2Lip `inference.py` aborted on face-less frames** (ad footage ends with product end-cards; distance shots). Patched locally: reuses the nearest detection, raises only if NO frame has a face. Additionally `local_dub.py` cuts video+audio at **speech end** (ffmpeg silencedetect) before lip-sync — replicating fal's `cut_off`.
3. **faster-whisper word alignment crashes on XTTS audio** (IndexError in `find_alignment` on silence). `caption.py` retries and falls back to segment-level timing.
4. **XTTS pads/fits its output to the video duration** — don't assume VO length == speech length.
5. **One dub at a time** — concurrent XTTS/Wav2Lip OOM the 4 GB card and *all* fail. The 409 guard in the dub/clone endpoints is load-bearing.
6. **Windows Smart App Control** blocked unsigned DLLs (llvmlite → numba) with WinError 4551 — it's OFF now; re-enabling breaks the dub venv.
7. **ComfyUI**: portable install shipped cu130 torch; downgraded to cu126; must launch with `--lowvram` (a launcher bat exists).
8. **Headless claude CLI from inside a Claude Code session**: pop `CLAUDECODE` from env before `subprocess.run([CLAUDE_EXE, "-p", ...])` or the nested run refuses.
9. **`0-byte mp4s`** are failed takes — every listing filters `st_size == 0`.
10. **Paths contain spaces** ("Video AI editing") — always `str(Path)` into subprocess arg lists, never string-concatenate commands.

---

## 9. Current state & open items

**Working and verified**: full dub pipeline (both engines), all six repair actions, chat advisor, auto-find original, captions, clone winner E2E, brand studio 1080×1080, remote phone access, spend tracking, job persistence/resume.

**Open / next**:
- Put `video-studio/` under git; commit autoVSL's 31 pending changes (includes the Wav2Lip patch, local_dub fixes, caption fallback).
- Task backlog: extract off-white logo + 8 mood flame icons from the brand PDF (blocked on asset).
- Brand Studio phase 2/3: more formats (4:5, 9:16), carousels, hook-refresh agents.
- Clone Winner: batch mode ("make 5 variants") would be a natural next step — the endpoints already support sequential runs.
- Old caption ghosting: during VO pauses the source's original burned captions can faintly show between new caption lines (pre-existing behavior, shipped in the winner; a full-band cover option in `caption.py` would eliminate it).

**Production files**: launch-ready ads land in `~/Desktop/liitt testimonial Ready/`; the cup-video ad (`1783501512600.publer.com-2-ready-captioned.mp4`) and its first clone (`...-2-v2-ready-captioned.mp4`) are there now.
