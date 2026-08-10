# AGENTS.md — Video Studio (Workspace Overview)

*Generated 2026-08-09 from `project-orientation` skill. Read-only scan, no code modified.*

## TL;DR

**Video Studio** is a local-first Windows video ad production system for the **liitt / Fairy Flame** brand. It takes raw UGC/testimonial footage, transcribes it, rewrites the script via Claude, voice-clones + dubs, lip-syncs, repairs visual AI artifacts, burns captions, and exports finished ads. One Flask web app on `http://localhost:5180`, 13 tabs, with optional ComfyUI/Brand Studio for static image ads.

**This is a consolidation workspace.** Five separate apps (`video-studio/`, `autoVSL/`, `dubbing-studio/`, `subtitle-studio/`, `course_pipeline/`) plus vendored engines (`tools/`, `ComfyUI/`) all coexist; the Flask app in `video-studio/app/server.py` is the canonical entry point that orchestrates the rest. The repo has a `comfyui/` branch (current) that adds a new ComfyUI sandbox folder with custom fal.ai nodes.

---

## 1. What this is, in one breath

A single-process Flask app that turns UGC ad footage → transcribed → script-rewritten by Claude → voice-cloned (local XTTS, free) OR paid fal.ai dub → lip-synced (Wav2Lip/GFPGAN local OR fal sync-lipsync) → visual-artifact-repaired → caption-burned → exported. Plus a parallel **Clone Winner** flow (rebuild proven ads in fresh variants) and a **Brand Studio** for static image ads via ComfyUI SD1.5.

**Design philosophy:** local engines by default (free, runs on a 4 GB RTX 3050 Ti), paid fal.ai cloud models behind explicit cost-confirmation gates with a spend tracker. The RTX 3050 Ti constraint is real — every engine is engineered to fit ~3.5 GB VRAM.

---

## 2. Architecture in one diagram

```
┌──────────────────────────────────────────────────────────────┐
│  Browser: http://localhost:5180                              │
│  Phone:    http://<lan-ip>:5180/remote   (PIN-gated)         │
└──────────────────┬───────────────────────────────────────────┘
                   │ 6-digit PIN session cookie
                   ▼
┌──────────────────────────────────────────────────────────────┐
│  video-studio/app/server.py  (~3,700 LOC, all routes)        │
│    ├─ jobs.py       persistent job store + GPU lock          │
│    └─ engines/      24 Python engines (see §5)               │
└──┬────────┬────────┬────────┬──────────┬──────────────────────┘
   │        │        │        │          │
   │        │        │        │          └─► comfyui/  (NEW, branch comfyui)
   ▼        ▼        ▼        ▼              ComfyUI 127.0.0.1:8188
┌──────┐ ┌──────┐ ┌──────┐ ┌──────────┐   + 4 custom fal.ai nodes
│autoVSL│ │dub  │ │sub  │ │ course   │
│/dash- │ │bing-│ │title-│ │ _pipeline│   ┌─ FalFluxKontextEdit
│board  │ │studio│ │studio│ │ .venv    │   ├─ FalNanoBanana2Edit
│       │ │/venv │ │/vsr  │ │(faster-  │   ├─ FalQwenImageEdit
│local_ │ │     │ │      │ │ whisper) │   └─ UGCVisionPlanner
│dub.py │ │XTTS │ │ProPa-│ └──────────┘
│dub.py │ │Wav2-│ │inter │
│caption│ │Lip  │ │      │       ┌─ tools/Wav2Lip/  (vendored, patched)
│script-│ │GFP- │ │      │       ├─ tools/ProPainter/  (inpaint)
│swap.py│ │GAN  │ │      │       └─ tools/CodeFormer/  (face restore)
└──────┘ └──────┘ └──────┘
   │         │         │         │            ┌─ ComfyUI  (vendored, optional)
   └─────────┴─────────┴─────────┴────────────┘
                  ┌───────────────────┐
                  │ autoVSL/.env      │   FAL_KEY (paid cloud)
                  │ config.json       │   paths, ports, PIN, venvs
                  └───────────────────┘
```

---

## 3. Directory layout — what each top-level folder is FOR

| Path | What it is | Version controlled? | Runs on? |
|---|---|---|---|
| **`video-studio/`** | **THE APP.** Flask server + 24 engines + 13 static HTML tabs + desktop launcher | ⚠️ root repo (not its own git, lives in `video-studio/`) | port 5180 |
| `autoVSL/` | Data root + original pipeline. Holds `uploads/`, `output/`, `banks/`, `dashboard/` (engine wrappers called by server), `scripts/` (fal.ai pipeline + ComfyUI client) | git repo, 31 uncommitted changes historically | data + helper scripts |
| `dubbing-studio/` | Standalone local voice-clone tool (Coqui XTTS v2, own venv). `lipsync.py` is reused by the app | not a repo | sometimes run standalone |
| `subtitle-studio/` | Subtitle eraser (`erase_subs.py` ProPainter) + re-captioner (`recaption.py`). Engines reused by the app | not a repo | sometimes run standalone |
| `course_pipeline/` | faster-whisper venv + `transcribe.py` + `distill.py` + `prompts/` | not a repo | whisper venv |
| `tools/Wav2Lip/` | Wav2Lip + GFPGAN weights, `inference.py` **locally patched** (see §8 gotcha #2) | vendored | called by dub engines |
| `tools/ProPainter/`, `tools/CodeFormer/` | inpainting / face-restore weights | vendored | called by erase + brand |
| `comfyui/` | **NEW (branch `comfyui`).** Self-contained ComfyUI sandbox: venv, custom fal.ai nodes, workflow JSONs, install/start/stop bats | untracked, partially committed in `598d6aa`+`a521182` | port 8188, local |
| `ComfyUI/` | (referenced in old docs as `ComfyUI_windows_portable/`) ComfyUI for Brand Studio (SD1.5, cu126, --lowvram) | not present in this checkout — see open item §7 | port 8188 |
| `install/` | 4 venv requirements files + `setup-machine.ps1` (one-shot setup) + packaging scripts | committed | setup-only |
| `docs/` | `GIVE-TO-VA.md` only — minimal | committed | — |

**Active app:** `video-studio/app/server.py` (port 5180). Everything else is either data, an engine called by the server, a fallback standalone, or vendored weights.

---

## 4. The web app (port 5180)

### 4.1 Auth & remote
- 6-digit PIN session cookie (`config.json → remote_pin`, default `000000`); localhost bypasses.
- LAN bind (`lan_access: true`) → `/remote` is a mobile dashboard (jobs, spend, quick launches).

### 4.2 Job system (the backbone)
- `POST /api/run` → `jobs[id]` dict → background thread → `run_job()` streams stdout into `job["lines"]`.
- **Persistent across restarts** (`jobs/jobs.json`).
- **Resumable** — `POST /api/job/<id>/resume` re-runs the recorded cmd. Engines are stage-cached per workdir, so they pick up where they left off (no re-paying for a paid clone).
- **Stoppable** — `/stop` kills the process tree.
- **GPU-arbitrated** — one GPU job at a time via `GPU_LOCK` + cross-app semaphore.
- **Money gate** — every fal.ai action needs `confirm_cost: true`. `run_dub_job()` appends estimated cost + running total; `/api/spend` shows it.

### 4.3 The 13 tabs

| Tab | Route | What it does |
|---|---|---|
| Library | `/library` | uploads browser, thumbnails, pipeline status per video |
| New Project | `/new` | upload + kick off transcribe |
| Transcript & Script | `/transcript` | edit transcript, Claude rewrite, save `script-edited.txt` |
| Subtitle Recovery | `/subtitles` | erase burned-in subs (ProPainter) |
| Dubbing & Lip Sync | `/dubbing` | local XTTS+Wav2Lip (free) OR fal.ai (paid, cost-gated) |
| **Clone Winner** | `/clone` | mass-produce variants of a proven ad |
| DubSync Repair | `/dubsync` | 6-action repair suite (frame swap, object, visual, relipsync, refit, renorm, remux) + chat advisor |
| Captions | `/captions` | word-timed bold ASS captions burned over old caption band |
| QA Review | `/qc` | frame prober, side-by-side |
| Exports | `/exports` | every deliverable in one list, copy to Desktop |
| Brand Studio | `/brand-studio` | static image ads (ComfyUI SD1.5 + RealESRGAN + compositor) |
| Ads Factory | `/creator` | original autoVSL dashboard flows (hooks/angles banks, VSL assembly) |
| Power Tools | `/tools` | one-off utilities (VSR upscale etc.) |

### 4.4 The dub workdir — central data structure
Everything revolves around `autoVSL/output/script-swap/<stem>/`:
```
final.mp4               ← current deliverable
final-captioned.mp4     ← caption pass output
source.txt              ← path to original footage
script-edited.txt       ← script that was spoken
transcript.txt          ← original speech
new-vo.mp3              ← generated VO
voice.json              ← MiniMax paid voice-clone id (reusable)
dub-config.json         ← which engine/tier made it
versions.json           ← metadata for every repair take
repair-*.mp4            ← repair takes (never overwrite final until promoted)
clone-info.json         ← present on clones: {winner, actor, created}
caption-words.json      ← cached whisper word timing
```
A workdir with `final.mp4` appears in **every** tab. New features create/extend workdirs, not new storage. Deliverables also copy to `~/Desktop/liitt testimonial Ready/<stem>-ready[-captioned].mp4`.

---

## 5. Engines (24 files, `video-studio/app/engines/`)

| File | LOC | Purpose |
|---|---|---|
| `broll_factory.py` | 50,785 | b-roll video factory (large, complex) |
| `broll_video.py` | 42,378 | b-roll video generation |
| `frame_reader.py` | 37,647 | frame extraction utilities |
| `fit_extend.py` | 30,598 | fit/extend image to aspect |
| `object_repair.py` | 23,972 | DubSync object repair (NCC tracking, face-tracked lip mask, damage gate) |
| `visual_repair.py` | 28,553 | DubSync visual repair (inverse of object) |
| `sequence_render.py` | 19,707 | sequence → rendered video |
| `image_edit.py` | 17,813 | fal image-edit integration |
| `compositor.py` | 14,187 | Pillow brand compositor (deterministic, hard-fails on missing tokens) |
| `product_still.py` | 11,626 | product still generation |
| `mask_edit.py` | 13,256 | mask-based image edit |
| `local_erase.py` | 12,609 | local subtitle erase (ProPainter wrapper) |
| `i2v_gen.py` | 11,305 | image → video (fal) |
| `smart_crop.py` | 12,075 | smart crop |
| `dubsync_repair.py` | 8,305 | relipsync/refit/renorm/remux |
| `ai_models.py` | 8,260 | fal model registry |
| `t2v_fal.py` | 12,469 | text → video (fal) |
| `t2v_continue.py` | 8,439 | text → video continue |
| `tag_overlay.py` | 7,595 | story-tag overlay (TikTok-style on-screen text) |
| `frame_swap.py` | 7,081 | DubSync frame swap (1:1 frame replacement, 4-frame crossfade) |
| `seq_generate.py` | 7,464 | sequence generation |
| `seq_extend.py` | 5,913 | sequence extension |
| `seq_avatar_gen.py` | 3,594 | sequence avatar gen |
| `seq_transcribe.py` | 3,437 | sequence transcription |
| `brand_content.py` | 5,923 | brand content pipeline (ComfyUI → ESRGAN → compositor) |

### 5.1 Sister engine files in `autoVSL/dashboard/`
These are the **engine entry points called by server.py** (the `video-studio/app/engines/` files are reusable libraries, these are the CLI entrypoints):
- `local_dub.py` (12,036 LOC) — **free dub chain** (XTTS voice → Wav2Lip/GFPGAN lip-sync)
- `dub.py` (7,301 LOC) — **paid fal.ai dub chain**
- `caption.py` (11,543 LOC) — whisper word timing → ASS captions
- `erase_subs.py`, `diarize.py`, `duo_run.py`, `subclean.py` — subtitle + dual-speaker helpers
- `server.py` (110,788 LOC!) — the **original autoVSL dashboard server** (still runnable, but port 5180 belongs to video-studio/)

### 5.2 External scripts in `autoVSL/scripts/`
- `script-swap.py` — original fal.ai pipeline (whisper / MiniMax voice clone / TTS / sync-lipsync), stage-cached per workdir
- ComfyUI client + workflow builders

---

## 6. The new ComfyUI sandbox (`comfyui/`) — branch `comfyui` focus

This is the **delta on this branch** that the user is working on. It is a self-contained, file-only ComfyUI install with 4 custom fal.ai image-edit nodes.

### 6.1 What it is
- **v1 skeleton**, files only — no deps installed, no ComfyUI source cloned, no models downloaded. Run `install.bat` when ready.
- Python 3.13 via `uv` (system Python is 3.11, intentional).
- Bind: `127.0.0.1:8188` by default.
- Models, outputs, inputs, venv all gitignored.
- `.env` for `HF_TOKEN` (and `CIVITAI_TOKEN` if needed), gitignored.

### 6.2 Folder layout
```
comfyui/
├── install.bat              # provisions venv + clones ComfyUI + installs deps (6 steps)
├── start.bat                # launches on 127.0.0.1:8188
├── stop.bat                 # kills ComfyUI
├── manifest.json            # pinned versions + layout contract
├── requirements.txt         # record of runtime deps (installed by install.bat)
├── custom_nodes.txt         # list of custom nodes to clone (all commented out)
├── sync_custom_nodes.bat    # syncs custom_nodes/ into ComfyUI/custom_nodes/
├── scripts/launch_comfyui.py # python launcher (python-dotenv for .env loading)
├── .env.example / .env      # HF_TOKEN, etc.
├── docs/                    # architecture.md, custom-nodes.md, INSTALL.md, troubleshooting.md
├── models/                  # checkpoints/loras/vae/controlnet/embeddings/upscale_models/
├── output/  input/  user/   # all gitignored
├── custom_nodes/            # 4 custom fal.ai nodes (see below)
│   ├── FalFluxKontextEdit/    (3 files, 189 LOC nodes.py)
│   ├── FalNanoBanana2Edit/    (3 files, 243 LOC nodes.py)
│   ├── FalQwenImageEdit/      (4 files incl. sample_layout.json, 211 LOC nodes.py)
│   └── UGCVisionPlanner/      (2 files, 397 LOC nodes.py)  ← intent-aware prompt engineer
├── api_workflows/           # ComfyUI workflow JSONs (newest, untracked)
│   ├── new_generate_workflow.json              (NanoBanana2 + UGCVisionPlanner)
│   ├── new_generate_same_content_workflow.json
│   └── full_replace_workflow.json
├── ComfyUI/                 # gitignored, target of install.bat clone
└── .venv/                   # gitignored, target of install.bat venv
```

### 6.3 The 4 custom fal.ai nodes (all in `comfyui/custom_nodes/`)
1. **FalFluxKontextEdit** — fal.ai Flux Kontext image editor wrapper
2. **FalNanoBanana2Edit** — Google's Gemini 2.5 Flash image generation (NanoBanana2 = `google/gemini-2.5-flash-image-preview`?), with `num_images`, `resolution`, `aspect_ratio`, `limit_generations`, `enable_web_search`, `thinking_level`, `safety_tolerance`, `audio_url`, `seed`, `product_image`, `additional_object`
3. **FalQwenImageEdit** — Qwen-based image edit (has `sample_layout.json`)
4. **UGCVisionPlanner** — **the load-bearing one**: intent-aware prompt engineer that takes `product_description` + product image + additional object image, uses `google/gemini-2.5-flash` (text model, T=0.35) to plan the visual, then feeds the prompt into a downstream image-gen node (e.g. NanoBanana2). Used in the 3 workflow JSONs in `api_workflows/`.

### 6.4 The 3 untracked API workflow JSONs (`comfyui/api_workflows/`)
- `new_generate_workflow.json` — UGCVisionPlanner → FalNanoBanana2Edit
- `new_generate_same_content_workflow.json` — same content regen
- `full_replace_workflow.json` — full scene replace

These are the **production-ready ComfyUI API call payloads** the app will POST to `127.0.0.1:8188/prompt` once ComfyUI is installed and the custom nodes are loaded.

### 6.5 Git state of `comfyui/`
- Files committed in `598d6aa` ("Init setup comfyui"): `.env.example`, `.gitignore`, `README.md`, `custom_nodes.txt`, `docs/`, `install.bat`, `manifest.json`, `requirements.txt`, `start.bat`, `stop.bat`, `scripts/launch_comfyui.py`, `user/.gitkeep`
- Files committed in `a521182` ("Added custom fal ai nodes"): the 4 custom_nodes/ subdirs + `sync_custom_nodes.bat` + `sync_custom_nodes.sh`
- Files **untracked** (current): `ComfyUI/` (cloned source), `api_workflows/` (workflow JSONs), `.venv/`, `.env`, plus probably the venv, models, output, input
- The `video-studio/app/` got a HUGE merge in `6c40e9f` (current HEAD): `api_batches.py` (+631 LOC), `api_sequence.py` (+1337 LOC), plus 11 new engine files (ai_models, fit_extend, frame_reader, image_edit, i2v_gen, local_erase, mask_edit, product_still, seq_*, smart_crop, t2v_*), `sequence.py` (+447 LOC), `server.py` (+1223 LOC), and new HTML tab `static/batches.html` (+693 LOC). This is the integration work wiring ComfyUI workflows into the Flask app.

### 6.6 Install flow (file-only, NOT run)
Per `install.bat` and `manifest.json`:
1. Sanity checks: `uv`, `git`, `.env`
2. Create `.venv` with Python 3.13 via `uv`
3. Clone `https://github.com/Comfy-Org/ComfyUI.git` into `ComfyUI/`
4. Install PyTorch (CUDA 13.0) + ComfyUI deps
5. Install launcher utilities (python-dotenv)
6. Clone each custom node from `custom_nodes.txt` (currently all commented out — the 4 nodes in `custom_nodes/` are user-written, not cloned)

**Known host quirks** (from `manifest.json`):
- PYTHONPATH contamination → all Python calls prefixed with `set PYTHONPATH=`
- System Python is 3.11; venv uses 3.13
- ComfyUI main entry is `main.py`, not `python -m comfyui`

---

## 7. Tech stack (cross-cutting)

| Layer | Stack |
|---|---|
| Web | Flask (vanilla, no build step), vanilla HTML/JS/CSS for the 13 tabs |
| Lang | Python 3.11 (app) + Python 3.13 (comfyui venv) |
| LLM | **headless `claude` CLI** (user's subscription) for script rewriting + vision advisor. **No Anthropic API key.** |
| Local voices | Coqui XTTS v2 |
| Local lip-sync | Wav2Lip (patched) + GFPGAN v1.4 |
| Local erase | ProPainter (inpainting) |
| Local transcribe | faster-whisper (CUDA, cu126) |
| Local face restore | CodeFormer (optional) |
| Cloud | fal.ai (`FAL_KEY` in `autoVSL/.env`) — paid dub, paid image gen, paid edit |
| Image gen | ComfyUI + SD1.5 (4 GB VRAM) + RealESRGAN ×4 → Pillow compositor |
| GPU | RTX 3050 Ti 4 GB (one job at a time, `--lowvram` everywhere) |
| OS | Windows 10 only (launcher, ffmpeg path, Smart App Control gotcha) |
| ffmpeg | WinGet Gyan build, prepended to PATH by `job_env()` in server.py |

### 7.1 Env vars / secrets
| Where | What |
|---|---|
| `autoVSL/.env` (not in git) | `FAL_KEY` (paid cloud) |
| `video-studio/config.json` (not in git) | all paths, venvs, ports, `remote_pin`, `secret_key`, `lan_access`, `comfyui` URL, `brand_kit`, `banks_dir`, `brand_out`, `auto_cleanup` |
| `comfyui/.env` (not in git) | `HF_TOKEN`, `CIVITAI_TOKEN` |

### 7.2 The 4 venvs
| Venv | Python | What it runs |
|---|---|---|
| `autoVSL/.venv` ("cv") | 3.11 | Flask server + repair engines + image-edit engines |
| `course_pipeline/.venv` ("whisper") | 3.11 | faster-whisper CUDA transcription |
| `dubbing-studio/venv` ("dub") | 3.11 | torch 2.7.1+cu126 + TTS + Wav2Lip |
| `tools/vsr/.venv` | 3.11 | video super-resolution (Power Tools) |
| `comfyui/.venv` | 3.13 | ComfyUI (new) |

---

## 8. Hard-won gotchas (don't re-learn these the hard way)

1. **`-shortest` is BANNED.** ffmpeg silently drops trailing frames (660→657). Use `-frames:v N` + post-encode ffprobe count guard. Every engine that touches frames verifies output count == input and deletes the take on mismatch.
2. **Wav2Lip `inference.py` is locally patched** (lines 89–100 in `tools/Wav2Lip/inference.py`). Stock aborts on face-less frames (ad end-cards, distance shots). Patch reuses nearest detection, raises only if NO frame has a face.
3. **faster-whisper word alignment crashes on XTTS audio** (IndexError in `find_alignment` on silence). `caption.py` retries and falls back to segment-level timing. **The fallback is load-bearing.**
4. **XTTS pads its output to the video duration** — don't assume VO length == speech length.
5. **One dub at a time** — concurrent XTTS/Wav2Lip OOM the 4 GB card and *all* fail. The 409 guard in the dub/clone endpoints is load-bearing.
6. **Windows Smart App Control** blocks unsigned DLLs (llvmlite → numba) with `WinError 4551`. It MUST stay off. Re-enabling breaks the dub venv.
7. **ComfyUI portable ships cu130 torch;** must downgrade to cu126 for RTX 30-series. Launch with `--lowvram`.
8. **Headless `claude` CLI from inside a Claude Code session:** pop `CLAUDECODE` from env before `subprocess.run([CLAUDE_EXE, "-p", ...])` or the nested run refuses.
9. **0-byte mp4s are failed takes** — every listing filters `st_size == 0`.
10. **Paths contain spaces** — "Video AI editing" (old path) and "newvsl" (current). Always `str(Path(...))` into subprocess arg lists, never string-concatenate commands.
11. **Brand Studio: SD1.5 cannot render text.** The model never draws brand text. The pipeline goes ComfyUI → RealESRGAN ×4 → Pillow `compositor.py` which overlays text with hard-fail on missing brand-kit tokens (never silently substitutes).
12. **MSYS env-var trap** (this session, not the project): bash snapshots Windows env at launch. `setx` after bash started is invisible. Either `export FAL_KEY=...` in the launching bash, or close+reopen bash.

---

## 9. State of the project

### 9.1 Done & verified
- Full dub pipeline (local + paid)
- All 6 DubSync repair actions + chat advisor + auto-find original
- Captions, Clone Winner E2E (`1783501512600.publer.com-2-v2` produced for $0 locally)
- Brand Studio 1080×1080
- Remote phone access, spend tracking, job persistence/resume
- The `master` branch state: app stable, all 13 tabs working

### 9.2 The current branch (`comfyui`) delta
- **NEW (committed in `598d6aa`):** `comfyui/` sandbox skeleton (install/start/stop/manifest/docs/.env.example)
- **NEW (committed in `a521182`):** 4 custom fal.ai ComfyUI nodes (FalFluxKontextEdit, FalNanoBanana2Edit, FalQwenImageEdit, UGCVisionPlanner) + `sync_custom_nodes` scripts
- **NEW (merged in `6c40e9f`):** ~1223 LOC added to `server.py`, +447 LOC `sequence.py`, +1337 LOC `api_sequence.py`, +631 LOC `api_batches.py`, 11 new engine files (~3,500 LOC total), new `batches.html` tab. This is the Flask-app integration wiring ComfyUI workflows into the UI.
- **NEW (untracked):** `comfyui/api_workflows/` — 3 production-ready workflow JSONs; `comfyui/ComfyUI/` — empty (will be cloned by install.bat); `comfyui/.venv/` and `comfyui/.env` — not yet provisioned.

### 9.3 Open / next (from `PROJECT-SUMMARY.md`, plus inferred)
- ~~Put `video-studio/` under git~~ — done (root repo)
- ~~Commit autoVSL's 31 pending changes~~ — done (per `c0d3733` "Update from Video Studio session")
- **Run `comfyui/install.bat`** to provision the venv + clone ComfyUI + install deps. Currently file-only, no execution. Per `Phased-deliver` work-style rule, this needs explicit user go-ahead.
- **Drop model files** into `comfyui/models/checkpoints/`, `loras/`, etc.
- **Uncomment custom nodes in `comfyui/custom_nodes.txt`** (currently all commented out — the 4 nodes in `custom_nodes/` are user-written, not auto-cloned)
- **Wire `api_workflows/*.json`** into the new `api_sequence.py` / `api_batches.py` routes (likely already done in the merge, needs verification)
- The `ComfyUI_windows_portable/` referenced in old docs is **not present in this checkout** — the new `comfyui/ComfyUI/` is the new pattern
- Brand Studio phase 2/3: more formats (4:5, 9:16), carousels, hook-refresh agents
- Clone Winner batch mode ("make 5 variants")
- Old caption ghosting: during VO pauses the source's burned captions can faintly show between new lines (shipped in the winner; full-band cover option in `caption.py` would eliminate it)
- Asset extraction: off-white logo + 8 mood flame icons from brand PDF (blocked on asset)

### 9.4 Landmines specific to this session / current state
- **No `autoVSL/.env` in checkout** — `FAL_KEY` is empty, paid features are off. Per memory's MSYS env-var trap, the user sets it via `export FAL_KEY=...` in launching bash, or `setx` followed by close+reopen all bash terminals.
- **No `video-studio/config.json` in checkout** — only `config.example.json` with `<REPO_ROOT>` placeholders. Setup must run.
- **ComfyUI not yet installed** — `comfyui/ComfyUI/` is the target dir, currently empty. The app's `comfyui` URL in `config.json` points at `127.0.0.1:8188`.
- **`custom_nodes.txt` is empty** (all entries commented out) — the 4 nodes in `custom_nodes/` are user-written, not auto-cloned. `sync_custom_nodes.bat` copies them into `ComfyUI/custom_nodes/` manually.
- **Branch `comfyui` is 1 commit ahead of `origin/comfyui`** after the merge. Not pushed.
- **`master` has 1 extra remote branch** `origin/fix/readme-architecture-link` — likely a fix branch not merged.

---

## 10. Where to look for what

- **What is this project?** → `README.md` (root) + `PROJECT-SUMMARY.md` (177 lines, best overview)
- **What are the 13 tabs?** → `PROJECT-SUMMARY.md` §4 + `video-studio/app/static/*.html` (one file per tab)
- **How do the engines fit together?** → `PROJECT-SUMMARY.md` §6 (DubSync repair suite, 6 actions)
- **How is the workdir structured?** → `PROJECT-SUMMARY.md` §3 (the file tree)
- **What are the gotchas?** → `PROJECT-SUMMARY.md` §8 (10 items) + `SETUP.md` "Notes for developers" (7 items)
- **What's new on branch `comfyui`?** → this doc §6 + §9.2
- **What does the ComfyUI install do?** → `comfyui/docs/INSTALL.md` (286 lines) + `comfyui/manifest.json`
- **What does the new batches tab do?** → `video-studio/app/static/batches.html` (693 LOC) + `video-studio/app/api_batches.py` (631 LOC)
- **What does the new sequence flow do?** → `video-studio/app/sequence.py` (447 LOC) + `api_sequence.py` (1337 LOC) + `engines/sequence_render.py` (471 LOC)

---

## 11. Session-safe scratch

- **Branch:** `comfyui` (current) ← merged with `master` in `6c40e9f` (2026-08-09 15:05)
- **Working dir:** `C:\Kerjaan\newvsl\video-studio`
- **Untracked:** `comfyui/ComfyUI/`, `comfyui/api_workflows/`
- **Recent direction:** adding ComfyUI sandbox + 4 fal.ai custom nodes + 3 production workflow JSONs + 2 new Flask routes (`api_sequence.py`, `api_batches.py`) + new `batches.html` tab
- **Open work:** provision comfyui venv (gated on user go-ahead), wire workflows into routes (verify done in merge), drop model files
