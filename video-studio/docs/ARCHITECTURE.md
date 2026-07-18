# Video Studio — Architecture

> Target: one local Flask app on `http://localhost:5180`, premium dark UI, a single resumable
> job system, orchestrating the existing engines (ProPainter, faster-whisper, XTTS, Wav2Lip,
> fal.ai, ComfyUI) via subprocess. **Local-only, 127.0.0.1, no hosting.**

---

## 1. High-level shape

```
┌──────────────────────────────────────────────────────────────────────┐
│  Browser  →  http://localhost:5180   (premium dark UI, vanilla JS SPA) │
│  Tabs: Library · New Project · Transcript & Script · Subtitle Recovery ·│
│        Dubbing & Lip Sync · DubSync Repair · Captions · QA Review ·     │
│        Exports · Ads Factory · Power Tools                              │
└───────────────┬──────────────────────────────────────────────────────┘
                │  JSON over HTTP (poll /api/jobs + /api/job/<id>)
┌───────────────▼──────────────────────────────────────────────────────┐
│  video-studio/app/server.py   (Flask, based on autoVSL/dashboard)      │
│  ┌────────────────┐  ┌──────────────────┐  ┌───────────────────────┐  │
│  │ Job Manager    │  │ Library / Project │  │ Config (config.json)  │  │
│  │ (persistent,   │  │ index             │  │ all machine paths     │  │
│  │  resumable,    │  └──────────────────┘  └───────────────────────┘  │
│  │  GPU-arbitrated)│                                                    │
│  └───────┬────────┘  ┌──────────────────┐  ┌───────────────────────┐  │
│          │           │ Cost ledger      │  │ Trash / restore        │  │
│          │           │ (fal_spend.json) │  │ (.trash + index.json)  │  │
│          │           └──────────────────┘  └───────────────────────┘  │
└──────────┼─────────────────────────────────────────────────────────── ┘
           │ subprocess.Popen (stdout streamed → jobs/<id>.log)
   ┌───────┴───────────────────────────────────────────────────────────┐
   │  Engine wrappers  →  EXISTING scripts + venvs (unchanged)          │
   │                                                                    │
   │  Subtitle Recovery → subtitle-studio/{erase_subs,subclean}.py      │
   │                      (autoVSL/.venv) · tools/ProPainter · tools/vsr│
   │  Captions          → subtitle-studio/recaption.py                  │
   │                      (course_pipeline/.venv → faster-whisper)      │
   │  Dubbing/Lip Sync  → autoVSL/dashboard/{dub,local_dub}.py +        │
   │                      dubbing-studio/lipsync.py (XTTS, Wav2Lip,     │
   │                      GFPGAN/CodeFormer) · fal.ai via autoVSL/.env  │
   │  Ads Factory       → autoVSL/scripts/* (edge-tts, fal.ai, ffmpeg,  │
   │                      ComfyUI @127.0.0.1:8188, build-vsl via Claude)│
   │  QA Review         → /api/qc/* (ffprobe, frame sampling, AI review)│
   │  Shared binaries   → ffmpeg (winget), Claude CLI (spell-fix/copy)  │
   └────────────────────────────────────────────────────────────────── ┘
```

**Why this shape:** it is the `autoVSL/dashboard` architecture (already proven) plus (a) a
persistent/resumable job store, (b) a central config for machine paths, and (c) engine wrappers
that call the *existing* scripts. No engine is reimplemented; nothing in the source projects moves.

---

## 2. Component inventory

### 2.1 Web server — `video-studio/app/server.py`
- **Framework:** Flask, single app, `static_folder=None` (serves pages/media manually), based on
  `autoVSL/dashboard/server.py` (2271 lines, already implements 43 endpoints).
- **Bind:** `host=127.0.0.1`, `port=int(os.environ.get("PORT", 5180))`, `debug=False`.
- **Concurrency:** one daemon thread per job (as today) — Flask stays responsive while
  subprocesses run; long work never blocks the request thread.

### 2.2 Job Manager (new: persistent + resumable) — `video-studio/app/jobs.py`
Replaces the in-memory `jobs` dict in both existing apps.
- **State:** `jobs/jobs.json` (index) + `jobs/<id>.log` (streamed output). Write-through on every
  transition → survives restart.
- **Job record:**
  ```json
  {"id","project_id","tab","action","engine","status",
   "pct","stage","started","ended","returncode","pid",
   "cost","resumable","resume_hint"}
  ```
  `status ∈ queued|running|done|failed|stopped|interrupted`.
- **Runner:** `subprocess.Popen(cmd, stdout=PIPE, stderr=STDOUT, text, bufsize=1)`; each line
  appended to `jobs/<id>.log`; `job_progress()` parses tqdm `NN%` / `a/b` for the pct + stage.
- **Resume on startup:** dead `running` jobs → `interrupted`; if `resume_hint` cache exists,
  expose **Resume** (re-invoke engine, which reuses the cache).
- **GPU arbitration (ported from subtitle-studio):** global `GPU_LOCK` + `foreign_erase_running()`
  / `wait_for_gpu()` (PowerShell `Get-CimInstance` poll) so only one GPU job runs and Video Studio
  yields to any legacy app still erasing. `_awake_keeper()` keeps Windows awake mid-job.
- **Stop:** `taskkill /PID <pid> /T /F` on the whole tree (as today).

**Resumability tiers (safest → hardest):**
1. **Free** — engines already checkpoint: ProPainter `files/.erase-cache-<stem>/frames_all`
   (720-frame chunks) and whisper `words.json`. Resume = re-run; the engine skips finished work.
2. **Cheap** — cloud dub reuse: `fal_spend.json` `cloned_stems` skips re-cloning a voice; workdir
   reuse skips finished stages.
3. **New** — checkpoint any remaining long engine at stage boundaries if needed later.

### 2.3 Config — `video-studio/config.json`
Single home for every machine path currently hardcoded across three servers:
```json
{
  "port": 5180,
  "venvs": {
    "cv":      "autoVSL/.venv/Scripts/python.exe",
    "whisper": "course_pipeline/.venv/Scripts/python.exe",
    "vsr":     "tools/vsr/.venv/Scripts/python.exe",
    "dub":     "dubbing-studio/venv/Scripts/python.exe"
  },
  "engines": {
    "erase":    "subtitle-studio/erase_subs.py",
    "subclean": "subtitle-studio/subclean.py",
    "recaption":"subtitle-studio/recaption.py",
    "dub":      "autoVSL/dashboard/dub.py",
    "local_dub":"autoVSL/dashboard/local_dub.py",
    "lipsync":  "dubbing-studio/lipsync.py"
  },
  "weights": {"propainter":"tools/ProPainter","wav2lip":"tools/Wav2Lip","codeformer":"CodeFormer"},
  "ffmpeg":  "%LOCALAPPDATA%/.../Gyan.FFmpeg/.../bin",
  "claude":  "~/.local/bin/claude.exe",
  "comfyui": "127.0.0.1:8188",
  "fal_env": "autoVSL/.env",
  "exports": "~/Desktop/Video Studio"
}
```
All paths absolute at runtime; `config.json` is the only file to edit when the machine changes.

### 2.4 Library / Project index — `video-studio/library.json`
Unifies the two file lists (`autoVSL/uploads`, `subtitle-studio/files`). Entry:
`{id, name, source_path, thumb, tags, stage, jobs[], outputs[]}`. Read-only over the originals in
early phases (no file moves); Video Studio owns new uploads under its own `uploads/`.

### 2.5 Cost ledger & Trash (reused as-is)
- `output/fal_spend.json` + `estimate_dub_cost()` / `record_spend()`; `confirm_cost` gate on every
  paid action; live `$` estimates in the UI.
- `.trash/` + `index.json`, multi-piece bundle delete, `POST /api/trash/restore`.

### 2.6 Front-end — `video-studio/app/static/`
Vanilla-JS SPA (no build step), one page per tab, restyled into a **premium dark theme**
(shared CSS: near-black surfaces, one accent, soft elevation, generous spacing, live job cards
with progress bars + stage labels + `$` cost). Polls `/api/jobs` (list) and `/api/job/<id>`
(incremental via `next_offset`). Reuses the existing `eraser/subtitles/dubbing/qc/creator/studio`
pages as tab bodies, plus new pages for Library / New Project / Transcript & Script /
DubSync Repair / Exports / Power Tools.

---

## 3. Data flow (representative jobs)

**Subtitle Recovery (AI erase → captions), fully local & resumable:**
```
UI box/auto → POST /api/clean-subs → Job(queued) → GPU_LOCK
  → erase_subs.py (ProPainter, chunked, cache=files/.erase-cache-<stem>)   [resumable]
  → backup original → replace source → write <stem>.box.json
  → recaption.py (faster-whisper words.json → lines.json → ASS burn over band)
  → output/<stem>/captioned.mp4 → exports/
```

**Dubbing (local voice) + lip-sync:**
```
Edit script → POST /api/run{action:dub,engine:local}
  → local_dub.py: XTTS clone+synth → fit to length → normalize → mux
  → lipsync.py: Wav2Lip + GFPGAN/CodeFormer  (GPU_LOCK)
  → write per-dub provenance record  → output → exports/
```

**DubSync Repair (new):**
```
Pick existing dub → read provenance → choose fix
  (re-sync | re-fit length | re-normalize | re-mux)
  → reuse lipsync.py / fit_audio_to_duration / mux  → new output version
```

**Ads Factory (mixed local/cloud):**
```
Product/research/banks → build-vsl (Claude CLI) → generate-vo (edge-tts, free)
  → generate-video (fal.ai $  OR  ComfyUI local free) → assemble (ffmpeg) → output
```

---

## 4. Tab → endpoint map (target)

| Tab | Primary endpoints | Engines |
|---|---|---|
| Library | `GET /api/library`, `GET /api/thumb/<id>`, `GET /media/<path>` | — |
| New Project | `POST /api/upload`, `POST /api/project` | — |
| Transcript & Script | `POST /api/run{transcribe}`, `GET/POST /api/script/<stem>`, `POST /api/aifix/<stem>` | faster-whisper, Claude |
| Subtitle Recovery | `POST /api/clean-preview`, `POST /api/clean-subs`, `POST /api/clean-restore` | erase_subs, subclean, vsr |
| Dubbing & Lip Sync | `POST /api/run{dub}`, `GET /api/script/<stem>`, `POST /api/script/<stem>` | dub, local_dub, lipsync |
| DubSync Repair | `POST /api/dubsync/repair` *(new)* | lipsync, fit_audio, mux |
| Captions | `POST /api/run{recaption}`, `GET/POST /api/captions/<stem>`, `POST /api/aifix/<stem>` | recaption, Claude |
| QA Review | `GET /api/qc/{videos,probe,frames}`, `POST /api/qc/{review,ai-review,remove-subs}` | ffprobe, Claude |
| Exports | `POST /api/output-to-desktop`, `POST /api/output-rename`, `DELETE /api/output` | ffmpeg |
| Ads Factory | `POST /api/build-vsl`, `POST /api/run{generate-vo,generate-video,assemble}`, `POST /api/copywrite`, `POST /api/studio/run` | edge-tts, fal.ai, ComfyUI, Claude |
| Power Tools | `GET /api/selftest`, `GET /api/spend`, trash APIs, `POST /api/chat`, GPU status | — |
| (all) | `GET /api/jobs`, `GET /api/job/<id>`, `POST /api/job/<id>/stop` | job manager |

---

## 5. Cross-cutting constraints (design rules)

1. **Single GPU (4 GB):** every GPU engine goes through `GPU_LOCK`; the runner also polls for a
   *foreign* erase (legacy app) and waits. Never run two GPU jobs at once.
2. **Dependency pins are load-bearing:** `torch==2.5.1`, `numpy<2` (1.26.4), `opencv==4.10.0.84`,
   `coqui-tts==0.25.1`, `gradio` (dubbing only). → **keep separate venvs, call via subprocess.**
   Do not attempt a single merged environment early.
3. **One secret location:** fal.ai key stays in `autoVSL/.env`; the runner injects it into the
   subprocess env for cloud actions (as dubbing-studio already does).
4. **Money is always gated:** no paid fal.ai action runs without `confirm_cost`; every spend is
   estimated up front and appended to the ledger.
5. **Windows realities:** `PYTHONUTF8=1`, ffmpeg path prepended to `PATH`, `taskkill /T /F` for
   cancel, `SetThreadExecutionState` keep-awake, `secure_filename` fallback for non-ASCII names.
6. **Everything reversible:** additive migration; originals never modified; rollback = restart
   subtitle-studio on 5180.

---

## 6. Directory layout (target)

```
video-studio/
├── docs/            PROJECT-INVENTORY.md · MIGRATION-PLAN.md · ARCHITECTURE.md
├── config.json      all machine paths (the only per-machine file)
├── library.json     unified project index
├── app/
│   ├── server.py    Flask, port 5180 (from autoVSL/dashboard, re-pathed)
│   ├── jobs.py      persistent + resumable + GPU-arbitrated job manager
│   ├── engines/     thin subprocess wrappers → existing scripts (no engine code)
│   └── static/      premium dark UI, one page per tab
├── jobs/            jobs.json + <id>.log         (persisted job state)
├── uploads/         videos uploaded via Video Studio
├── output/          renders + fal_spend.json ledger
└── .trash/          soft-deletes + index.json
```

Engines, weights, venvs, and the fal.ai key **stay where they are** in `autoVSL/`,
`subtitle-studio/`, `dubbing-studio/`, `tools/`, `course_pipeline/`, and `CodeFormer/`;
Video Studio references them through `config.json`.

**Mapping to the brief's suggested structure** (`VIDEO-STUDIO-UNIFIED-BRIEF.md` §6):

| Brief | This layout | Why |
|---|---|---|
| `app.py` | `app/server.py` | same role; kept beside its modules |
| `core/` | `app/jobs.py` + `config.json` | job queue + machine config |
| `pipelines/` | `app/engines/` | thin subprocess wrappers → existing scripts |
| `static/` + `templates/` | `app/static/` | vanilla-JS pages are self-contained (no Jinja templates needed) |
| `projects/` | `library.json` + `uploads/` | index over originals first (no file moves), owns new uploads |
| `outputs/` | `output/` | matches the existing dashboard convention + spend ledger |
| `docs/` | `docs/` | same |
