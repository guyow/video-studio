# Video Studio — Project Inventory

> Read-only survey of the existing projects under
> `C:\Users\guyas\Claude\Projects\Video AI editing\`, taken **2026-07-16** as the
> factual basis for the migration into one unified **Video Studio** app on
> `http://localhost:5180`.
>
> **Nothing in Subtitle Studio or autoVSL was modified to produce this document.**

---

## 0. TL;DR — what already exists

| Existing app | Port | Stack | Maturity | Role in Video Studio |
|---|---|---|---|---|
| **autoVSL / `dashboard/`** | **5170** | Flask + multi-page vanilla-JS SPA | **Highest** — already multi-tab, has a job runner, cost tracking, trash, QC | **The migration base / skeleton for Video Studio** |
| **subtitle-studio** | **5180** | Flask + single-file SPA | High — best subtitle-erase engine, resume cache | Folds into **Subtitle Recovery** + **Captions** |
| **dubbing-studio** | 7860 | Gradio | Medium — standalone voice-clone tool | Folds into **Dubbing & Lip Sync** (+ **DubSync Repair**) |

**Key insight:** the unified app the brief describes is roughly **80% already built** inside
`autoVSL/dashboard`. The safest path is to *adopt that dashboard as Video Studio*, rebind it
to port 5180, and pull in subtitle-studio's superior erase engine and dubbing-studio's
voice-clone engine — **not** a from-scratch rebuild.

> **Source of truth:** `docs/VIDEO-STUDIO-UNIFIED-BRIEF.md` (this folder). Its 11 target
> modules and safe rules are reflected throughout; every claim below is verified against the
> actual code on disk.

---

## 1. autoVSL — the ad factory + existing dashboard

**Path:** `autoVSL/`  ·  **Git repo:** yes  ·  **venv:** `autoVSL/.venv` (Flask, OpenCV, torch, easyocr, fal-client, edge-tts)

### 1a. `autoVSL/dashboard/` — the strongest asset
A Flask control panel (`server.py`, **2271 lines**) that already presents a multi-tab dark UI
and a working background-job system. **This is the closest thing to "Video Studio" that exists.**

**Pages served (all under one Flask app):**

| Route | Static file | What it is | Maps to Video Studio section |
|---|---|---|---|
| `/` , `/mission` | `index.html` (86 KB) | Overview / mission control | **Library** + dashboard home |
| `/creator` | `creator.html` (39 KB) | Creator library, per-clip metadata | **Ads Factory** + **Library** |
| `/qc` | `qc.html` (34 KB) | QC review: probe, frames, AI review, remove-subs | **QA Review** |
| `/subtitles` | `subtitles.html` (11 KB) | Subtitle workflow | **Subtitle Recovery** / **Captions** |
| `/eraser` | `eraser.html` (17 KB) | Box-draw subtitle eraser | **Subtitle Recovery** |
| `/dubbing` | `dubbing.html` (27 KB) | Dub + lip-sync controls | **Dubbing & Lip Sync** |
| `/studio` | `studio.html` (10 KB) | ComfyUI still/video studio | **Ads Factory** / **Power Tools** |

**Backend modules in `dashboard/`:** `server.py`, `caption.py` (220), `dub.py` (155),
`local_dub.py` (205), `erase_subs.py` (347), `subclean.py` (153).

**Job system (already present):** in-memory `jobs: dict[str,dict]` guarded by `jobs_lock`;
each job = a daemon `threading.Thread` wrapping `subprocess.Popen`, streaming stdout into
`job["lines"]`. Job record: `{id, action, slug, label, status, lines[], returncode, started, ended, pid, cost}`.
Statuses: `running / done / failed / stopped / issues`. Live progress parsed from tqdm output
(`job_progress()`). **Not persisted to disk → not resumable across restarts** (this is the main
gap to close for the brief's "single resumable local job system").

**Endpoints (43 total):**
- Jobs: `POST /api/run`, `GET /api/jobs`, `GET /api/job/<id>`, `POST /api/job/<id>/stop`
- Upload/media: `POST /api/upload`, `DELETE /api/upload`, `GET /api/file`, `GET /media/<path>`, `GET /api/thumb/<name>`
- Transcribe/script: `POST /api/transcript-to-product`, `GET/POST /api/script/<stem>`
- Subtitle clean: `POST /api/clean-preview`, `POST /api/clean-subs`, `POST /api/clean-restore`
- VSL build: `POST /api/build-vsl`
- Products/research CRUD: `POST /api/product`, `DELETE /api/product/<slug>`, `POST/DELETE /api/research-doc`
- Output/exports: `POST /api/output-to-desktop`, `DELETE /api/output`, `POST /api/output-rename`
- Trash: `POST /api/trash/restore`, `DELETE /api/trash`
- Dev/AI: `POST /api/agent-note`, `POST /api/chat`, `GET /api/chat/<turn_id>`, `POST /api/copywrite`, `POST /api/dub-promote`
- Overview/banks: `GET /api/overview`, `GET /api/bank/<name>`
- **QC:** `GET /api/qc/videos`, `GET /api/qc/models`, `GET /api/qc/probe`, `GET /api/qc/frames`, `POST /api/qc/review`, `POST /api/qc/ai-review`, `POST /api/qc/remove-subs`
- Editing: `POST /api/edit`
- Creator: `GET /api/creator/library`, `POST /api/creator/meta`, `GET /api/thumb/<path>`
- Cost: `GET /api/spend`
- Studio (ComfyUI): `GET /studio-out/<name>`, `GET /api/studio/brand`, `POST /api/studio/upload`, `POST /api/studio/run`
- **Port:** `PORT` env, default **5170**, `host=127.0.0.1`, `debug=False`.

**Cost tracking (reusable as-is):** `output/fal_spend.json` ledger, `estimate_dub_cost()`,
`record_spend()`, per-clip model price table (`VIDEO_MODELS`), TTS/lip-sync rate tables, and
explicit `confirm_cost` gating on every paid action.

**Trash/restore (reusable as-is):** `soft_delete()` → `.trash/` with `index.json`; multi-piece
"bundle" delete of a video + transcript + dub workdir; `POST /api/trash/restore` puts every
piece back.

### 1b. autoVSL generation pipeline (the "Ads Factory" core)
- `scripts/generate-vo.py` — free VO via **edge-tts**
- `scripts/generate-video.py` — video via **fal.ai** (`FAL_KEY`); model table seedance/wan/hailuo/kling ($0.05–$0.40/clip)
- `scripts/generate-video-local.py` — **local ComfyUI** render (free, GPU); `COMFYUI_URL=127.0.0.1:8188`
- `scripts/comfyui_*.py`, `scripts/vsl-render.py`, `assemble-vsl.sh` (free ffmpeg export)
- `scripts/script-swap.py`, `script-swap-duo.py` — **fal.ai voice-clone + lip-sync** (called by dubbing-studio too)
- Content model: `products/<slug>/` (manifest + avatars/angles/scripts/shot-lists/stories),
  `research/<slug>/`, `banks/*.jsonl` (hooks/angles/scripts research banks), `vsls/<slug>/`
  (kling-shots.json, elevenlabs-vo.json, timeline.json, brief.md), `output/`, `uploads/`.
- `.claude/skills/` — 5 chained agents (prospector, bloodhound, brand-platform-builder, vsl, vsl-editor).

### 1c. Secrets / external services
- `autoVSL/.env` holds **`FAL_KEY`** (fal.ai) + `VSL_MODEL` + ComfyUI settings. **This is the
  single source of the fal.ai key** — dubbing-studio and subtitle-studio both reach into it.
- No OpenAI / ElevenLabs / translation keys. Local AI = the **Claude CLI** (`~/.local/bin/claude.exe`).

---

## 2. subtitle-studio — best-in-class subtitle removal

**Path:** `subtitle-studio/`  ·  **Port:** **5180** (the target port for Video Studio)  ·
**No venv of its own** — borrows three sibling venvs.

**`server.py` (Flask, single file):** same job pattern as the dashboard (in-memory `jobs` +
threads + subprocess), plus extras the dashboard lacks:
- `GPU_LOCK` — serializes the one 4 GB GPU (one ProPainter/VSR run at a time).
- `_awake_keeper()` — `SetThreadExecutionState` keeps Windows awake mid-job.
- `foreign_erase_running()` / `wait_for_gpu()` — cross-process GPU arbitration (waits when the
  autoVSL dashboard is also erasing).
- `_kill_engines()` on startup — sweeps orphaned ProPainter/erase workers.
- `/api/selftest` — checks ffmpeg, both venvs, ProPainter, Claude CLI, GPU, disk.
- `/api/aifix/<stem>` — free caption spell-fix via local Claude CLI (haiku).

**Endpoints:** `/`, `/api/state`, `/api/job/<id>` (+ `/stop`), `/media/<path>`, `/api/upload`,
`/api/file` (DELETE), `/api/clean-preview`, `/api/clean`, `/api/restore`, `/api/recaption`,
`/api/rename`, `/api/tags`, `/api/thumb/<stem>`, `/api/aifix/<stem>`, `/api/selftest`,
`/api/boxcaption`, `/api/captions/<stem>` (GET/POST), `/api/auto`.

**Engines (the reason to keep this project):**
- `erase_subs.py` — **ProPainter** AI video-inpaint on GPU. Auto-detects the caption band
  (EasyOCR/CRAFT → brightness fallback), 720-frame chunks, `--fp16`, crop snapped to ×16,
  NaN/black-corruption retry, and a **persistent resume cache** `files/.erase-cache-<stem>/frames_all`
  → an interrupted erase resumes from the last finished chunk. **This is the resumability model
  Video Studio should generalize.**
- `recaption.py` — **faster-whisper** (`distil-large-v3`, word timestamps, cached in `words.json`)
  → editable `lines.json` → bold ASS captions burned exactly over the erased band. Flags for
  words-only, burn-lines (re-burn edits), cover blur/box, no-captions.
- `subclean.py` — fast non-AI OpenCV cleaner (smart inpaint / blur / dark bar).
- Also invokes `tools/vsr/.venv` "magic erase" (video-subtitle-remover, STTN inpaint).

**Front-end:** one self-contained `static/index.html` (~569 lines vanilla JS). Cards per video,
manual-box remove modal, caption-edit modal with AI spell-fix.

**Data model:** `files/` (library; cleaned in place), `files/.originals/<name>` +
`<stem>.box.json` (backup + caption box), `files/.erase-cache-<stem>/` (resume),
`output/<stem>/` (captioned.mp4, captions.ass, lines.json, words.json, script.txt),
`.trash/`, deliverables → `~/Desktop/Subtitle Studio/`. `tags.json` is the only live config.

**Note:** `st.json` / `j.json` / `fin.json` in this folder are **debug dumps of in-memory job
state**, not config — no code reads them. Same for `bm.json / fin.json / j.json / mj.json / st.json`
at the parent level. Safe to ignore/delete.

---

## 3. dubbing-studio — voice-clone dubbing (standalone)

**Path:** `dubbing-studio/`  ·  **Port:** 7860 (Gradio default)  ·  **venv:** `dubbing-studio/venv`

- **`app.py` (Gradio, 56 KB):** user pastes the target-language script (it does **not**
  transcribe/translate); pipeline = extract reference clip → **Coqui XTTS v2** voice clone +
  synth → time-stretch to video length (pitch-preserved) → normalize −16 LUFS → mux.
  17 XTTS languages. Jobs are a streaming generator — **not resumable, no job records** (only a
  timestamped output file + `outputs/fal_spend.json`).
- **`lipsync.py`:** local **Wav2Lip GAN** + **GFPGAN v1.4** or **CodeFormer** face restore.
  Weights live **outside** this folder in `tools/Wav2Lip` and `CodeFormer/` (shared install).
- Cloud engines (`fal_voice` = f5-tts, `fal_lipsync` = LatentSync) **shell out to
  `autoVSL/.venv` + `autoVSL/scripts/script-swap.py`** and read the key from `autoVSL/.env`.
- **Load-bearing pins:** `torch==2.5.1`, `numpy<2` (1.26.4), `opencv==4.10.0.84`,
  `coqui-tts==0.25.1`, `gradio==5.49.1`. These constrain the merged dependency set.

> The dashboard's `dub.py` / `local_dub.py` already reimplement most of this against the shared
> job system. dubbing-studio's unique value is the **standalone XTTS engine + Wav2Lip chain**;
> those become the Dubbing engine, but its Gradio UI is superseded by `dubbing.html`.

---

## 4. Shared / supporting assets

| Asset | Path | Used by | Notes |
|---|---|---|---|
| **autoVSL venv** | `autoVSL/.venv` | dashboard, subtitle-studio erase/clean, dub | Flask, cv2, torch, easyocr, fal-client, edge-tts |
| **whisper venv** | `course_pipeline/.venv` | recaption/caption (faster-whisper) | `transcribe.py` + `.venv`; wires cuBLAS/cuDNN DLLs |
| **ProPainter** | `tools/ProPainter/` | AI erase | model + weights; the quality subtitle-erase path |
| **VSR magic erase** | `tools/vsr/.venv` + `backend/main.py` | subtitle-studio | STTN inpaint, ~5 fps |
| **Wav2Lip** | `tools/Wav2Lip/` | lipsync | `wav2lip_gan.pth`, `gfpgan_weights/` |
| **CodeFormer** | `CodeFormer/` | lipsync | face restore weights |
| **ComfyUI** | `ComfyUI_windows_portable/` | local video/still gen | `127.0.0.1:8188`; `autoVSL/comfyui-output` symlink |
| **ffmpeg** | winget Gyan.FFmpeg 8.1.2 | all | hardcoded LOCALAPPDATA path in every server |
| **Claude CLI** | `~/.local/bin/claude.exe` | copywrite, VSL build, spell-fix | free local AI |
| **fal.ai key** | `autoVSL/.env` (`FAL_KEY`) | all cloud paths | single source of truth |
| **video-studio/** | `video-studio/` | — | **empty except `docs/`** — the target home, created 2026-07-16 |

### Out of scope (present in the folder, not part of the merge)

| Folder / file | What it is | Why excluded |
|---|---|---|
| `cmux/` | macOS Xcode app (terminal multiplexer) | Unrelated to video tooling |
| `courses/` | Marketing-course video content (sections 01–07) | Content, not code; consumed by course_pipeline |
| `course_pipeline/` (scripts) | Course transcription pipeline | Only its `.venv` (faster-whisper) is reused |
| `output/` (root) | Course-pipeline transcription outputs | Data, not code |
| `liitt-local-tool.zip/.tar.gz`, root `.mp3/.mp4/.json` files | Archives, voice samples, debug job-state dumps | No code reads them |

---

## 5. Feature coverage vs. the 11 required modules

| Required module | Already exists? | Where | Gap to build |
|---|---|---|---|
| **Library** | Partial | dashboard `/api/creator/library`, `/api/overview`; subtitle-studio `/api/state` | Unify the two file lists into one library view |
| **New Project** | Partial | dashboard `POST /api/product` + `/api/upload` | Wizard tying upload → project record |
| **Transcript & Script** | **Yes** | dashboard `/api/transcript-to-product`, `GET/POST /api/script/<stem>`; subtitle-studio whisper `words.json` → `script.txt` + `/api/aifix` spell-fix | Dedicated tab: transcribe → verify/spell-fix → edit/translate script |
| **Subtitle Recovery** | **Yes (best)** | subtitle-studio engines + dashboard `eraser`/`subtitles` | Merge; pick ProPainter/VSR/box engines |
| **Dubbing & Lip Sync** | **Yes** | dashboard `dubbing` + `dub.py`/`local_dub.py`; dubbing-studio XTTS/Wav2Lip | Consolidate two implementations |
| **DubSync Repair** | **No** | — (building blocks in `lipsync.py`: re-sync, `fit_audio_to_duration`, `mux`) | New feature + per-dub provenance record |
| **Captions** | **Yes** | subtitle-studio `recaption.py`; dashboard `caption.py` | Merge; keep editable lines.json |
| **QA Review** | **Yes** | dashboard `qc.html` + `/api/qc/*` | Wire to unified library |
| **Exports** | **Yes** | dashboard `/api/output-to-desktop`, rename, delete | Unified exports view |
| **Ads Factory** | **Yes** | autoVSL scripts + dashboard `creator`/`studio`/`build-vsl`/`copywrite` | Surface as one tab |
| **Power Tools** | Partial | subtitle-studio `/api/selftest`, dashboard trash/chat/spend/GPU tools | Collect into one tab |

**Bottom line:** 8 of 11 modules have working implementations today; only **DubSync Repair** is
net-new. **Library**, **New Project**, and **Power Tools** are mostly assembly of existing endpoints.

---

## 6. Cross-project coupling to resolve (risk register)

1. **Port 5180 is owned by subtitle-studio.** Video Studio wants it → subtitle-studio must be
   stopped/retired (or moved) before Video Studio can bind 5180.
2. **Hardcoded Windows paths everywhere** — ffmpeg (LOCALAPPDATA), venvs, ProPainter, Wav2Lip,
   Claude CLI, Desktop deliverable dirs. Centralize into one config.
3. **Three separate venvs** (autoVSL, course_pipeline, tools/vsr) + dubbing-studio's own.
   Merged app either keeps calling them as subprocesses (safe) or consolidates (risky pins:
   torch 2.5.1 / numpy<2 / opencv 4.10).
4. **Single 4 GB GPU** — only subtitle-studio has GPU arbitration (`GPU_LOCK` + foreign-erase
   polling). The unified job system must adopt this or concurrent jobs will OOM.
5. **fal.ai key lives in `autoVSL/.env`** — keep that as the shared secret location, or the
   cloud dub/video paths break.
6. **Job state is in-memory** in both Flask apps → nothing survives a restart. The brief's
   "resumable job system" requires persisting jobs + reusing engine-level resume caches.
7. **Deliverables scatter to multiple Desktop folders** (`Subtitle Studio/`, `litt VSL's/`,
   `liitt testimonial Ready/`). Unify under one exports root.
