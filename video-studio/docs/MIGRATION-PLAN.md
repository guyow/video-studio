# Video Studio — Migration Plan (safest path first)

> Goal (per `docs/VIDEO-STUDIO-UNIFIED-BRIEF.md`): one unified local app **Video Studio** on
> `http://localhost:5180` with tabs
> **Library · New Project · Transcript & Script · Subtitle Recovery · Dubbing & Lip Sync ·
> DubSync Repair · Captions · QA Review · Exports · Ads Factory · Power Tools**, a premium
> dark UI, and a single **resumable** local job system.
>
> Main workflow: Upload → Analyze → Verify transcript → Remove subtitles → Restore background
> → Edit/translate script → Dub → Shot-level lip-sync → DubSync Repair → Captions → QA → Export.
>
> **Guiding principle:** adopt what works, wrap what's risky, rebuild nothing that already runs.
> **Constraint honored throughout:** *do not modify or delete anything in `subtitle-studio` or
> `autoVSL`* until the new app has proven itself. Every step below is additive.

---

## The core decision: adopt, don't rebuild

`autoVSL/dashboard` is already a multi-tab Flask dashboard with a job runner, cost tracking,
trash/restore, and 8 of the 11 required tabs in some form. The safest migration is therefore:

> **Video Studio = a copy of `autoVSL/dashboard`, rebased into `video-studio/`, rebound to port
> 5180, with subtitle-studio's erase/caption engines and dubbing-studio's XTTS/Wav2Lip engine
> wired in as subprocess back-ends.**

This keeps every proven engine in place, changes no source project, and lets the two old apps
keep running as fallbacks until Video Studio is trusted.

---

## Phase 0 — Freeze & snapshot (no code, ~30 min)

1. **Do not touch** `subtitle-studio/` or `autoVSL/`. They stay runnable as fallbacks.
2. Record the current run commands (from READMEs):
   - subtitle-studio: `autoVSL\.venv\Scripts\python.exe subtitle-studio\server.py` → :5180
   - dashboard: `autoVSL\.venv\Scripts\python.exe autoVSL\dashboard\server.py` → :5170
   - dubbing-studio: `dubbing-studio\venv\Scripts\python.exe dubbing-studio\app.py` → :7860
3. Confirm shared assets exist (ProPainter, Wav2Lip, CodeFormer, tools/vsr, course_pipeline/.venv,
   ComfyUI, ffmpeg, Claude CLI, `autoVSL/.env` with `FAL_KEY`). subtitle-studio's `/api/selftest`
   already checks most of these — run it and keep the output.
4. **Deliverable:** this docs set (done) + a one-line note of which app owns which port today.

**Risk: none** — read-only.

---

## Phase 1 — Scaffold Video Studio next to the originals (~half day)

1. Create the app skeleton under `video-studio/` (new folder, already has `docs/`):
   ```
   video-studio/
     app/            server.py  (copied from autoVSL/dashboard, then re-pathed)
     app/engines/    thin subprocess wrappers → existing scripts (NO engine code copied)
     app/static/     the 11-tab dark UI
     config.json     all machine paths in ONE place (venvs, ffmpeg, weights, .env, desktop)
     jobs/           persisted job store (jobs.json + per-job log files)   ← resumability
     library.json    the unified project index
   ```
2. **Copy** (never move) `autoVSL/dashboard/server.py` into `video-studio/app/server.py`.
   Change only: (a) `PORT` default → **5180**, (b) all `ROOT`/path constants → read from
   `config.json`, (c) leave every engine call pointing at the *existing* scripts in
   `autoVSL/` and `subtitle-studio/` via absolute paths.
3. Bring the 11-tab shell online with the **existing** dashboard pages mapped in (see routing
   table below). Missing tabs render "coming soon" placeholders at first.
4. **Run Video Studio on a temporary port (e.g. 5181)** while validating, so it never fights
   subtitle-studio for 5180 yet.

**Risk: low** — additive; originals untouched; port conflict avoided by using 5181 during dev.

### Tab → existing asset routing

| Tab | Serve now from | Backend calls (unchanged engines) |
|---|---|---|
| Library | new `library.json` view over `autoVSL/uploads` + `subtitle-studio/files` | `/api/creator/library`, `/api/state` (read) |
| New Project | new wizard | `/api/upload`, `/api/product` |
| Transcript & Script | new tab (transcribe → verify → edit/translate) | whisper via `recaption.py --words-only`, `/api/transcript-to-product`, `GET/POST /api/script/<stem>`, `/api/aifix` |
| Subtitle Recovery | dashboard `eraser.html` + subtitle-studio modal | `subtitle-studio/erase_subs.py`, `recaption.py`, `subclean.py`, `tools/vsr` |
| Dubbing & Lip Sync | dashboard `dubbing.html` | `autoVSL/dashboard/dub.py`, `local_dub.py`; `dubbing-studio/lipsync.py` |
| DubSync Repair | **new** page | reuse `lipsync.py` (re-sync), `fit_audio_to_duration`, `mux` |
| Captions | subtitle-studio caption modal | `recaption.py` / `caption.py` |
| QA Review | dashboard `qc.html` | `/api/qc/*` |
| Exports | new exports view | `/api/output-to-desktop`, `/api/output-rename`, `DELETE /api/output` |
| Ads Factory | dashboard `creator.html` + `studio.html` | `build-vsl`, `generate-vo/video`, `assemble`, `copywrite`, ComfyUI |
| Power Tools | new collector page | `/api/selftest`, `/api/spend`, trash/restore, GPU tools, `/api/chat` |

---

## Phase 2 — The single resumable job system (~1–2 days) — *the heart of the brief*

Both existing apps use the same in-memory pattern (`jobs` dict + daemon thread + `subprocess.Popen`
streaming stdout). Neither survives a restart. Unify and persist:

1. **One job store** (`video-studio/jobs/`):
   - `jobs.json` — index of every job `{id, tab, action, project, status, started, ended, pct, stage, cost}`.
   - `jobs/<id>.log` — the streamed output (replaces the in-memory `lines[]`).
   - Write-through on every state change so a crash/restart loses nothing.
2. **Resume on startup:**
   - Load `jobs.json`; any `running` job whose process is dead → mark `interrupted` (not failed).
   - For engines with **checkpoint caches**, offer **Resume**: subtitle-studio's
     `files/.erase-cache-<stem>/frames_all` (ProPainter chunks) and whisper `words.json` already
     make erase + transcription resumable — expose a "Resume" button that re-invokes the engine,
     which picks up the cache. This is the safest resumability win (no engine changes needed).
   - Cloud dubs: reuse the fal spend ledger + workdir so a re-run skips already-cloned voices.
3. **GPU arbitration:** port subtitle-studio's `GPU_LOCK` + `foreign_erase_running()` /
   `wait_for_gpu()` into the unified runner so only one GPU job runs at a time, and Video Studio
   yields if an old app is still erasing. Keep `_awake_keeper()`.
4. **Job model fields to standardize:** `id, project_id, tab, action, engine, status
   (queued/running/done/failed/stopped/interrupted), pct, stage, started, ended, returncode,
   cost, resumable(bool), resume_hint(path)`.

**Risk: medium** — the one genuinely new subsystem. Mitigated by reusing existing engine-level
caches rather than inventing new checkpointing.

---

## Phase 3 — Consolidate the engines behind stable wrappers (~2–3 days)

Wrap, don't rewrite. Each wrapper is a thin function that builds the subprocess command against
the *existing* script + venv from `config.json`.

1. **Subtitle Recovery** — wrap `subtitle-studio/erase_subs.py` (ProPainter, resumable),
   `subclean.py` (fast), `tools/vsr` (magic erase), `recaption.py` (`--cover` modes). Prefer
   subtitle-studio's engines over the dashboard's copies (better detection + resume cache).
2. **Transcript & Script** — wrap whisper transcription (`recaption.py --words-only` →
   `words.json`/`script.txt`) for the verify step, plus `/api/script/<stem>` (edit/translate)
   and the free Claude spell-fix (`/api/aifix`). This is the brief's "Analyze → Verify
   transcript → Edit/translate script" stage, surfaced as its own tab.
3. **Captions** — wrap `recaption.py` (`--words-only`, `--burn-lines`) + keep editable
   `lines.json` + the free Claude spell-fix (`/api/aifix`).
4. **Dubbing & Lip Sync** — pick ONE implementation. Recommend the dashboard's `local_dub.py` +
   `dub.py` (already on the shared job system) for the pipeline, calling `dubbing-studio/lipsync.py`
   for the local Wav2Lip+GFPGAN/CodeFormer chain. Keep `confirm_cost` gating + spend ledger.
5. **Ads Factory** — reuse autoVSL scripts verbatim via subprocess (`generate-vo`, `generate-video`,
   `assemble`, `build-vsl`, ComfyUI local). No changes.
6. **QA Review** — reuse `/api/qc/*` as-is.

**Risk: low–medium** — engines unchanged; only the call sites move.

---

## Phase 4 — DubSync Repair (the one new feature) (~2 days)

No existing repair feature; the building blocks exist in `dubbing-studio`:
- **Re-sync only** (fix bad lip-sync without re-cloning voice): call `lipsync.lipsync_video(video, wav)`
  with a different restorer/upscale on an existing dub pair.
- **Re-fit / re-normalize** drift: `fit_audio_to_duration()` + `normalize_output_loudness()`.
- **Re-mux** corrected audio: `mux_audio_into_video()`.
- **Prerequisite:** add a **per-dub provenance record** (source video, script, reference,
  engine settings, output paths) written at dub time — today only timestamped filenames + the
  spend ledger exist. DubSync Repair reads that record to know what to re-run.

**Risk: medium** — new code, but composed entirely from proven functions.

---

## Phase 5 — Unify Library / New Project / Exports / Power Tools (~2 days)

1. **Library** — one `library.json` project index unifying `autoVSL/uploads` + `subtitle-studio/files`;
   each entry = `{id, name, source_path, thumb, tags, jobs[], outputs[], stage}`. Read-only over the
   originals at first (no file moves).
2. **New Project** — wizard: upload → create library entry → optional first action.
3. **Exports** — one view over all outputs + the `/api/output-to-desktop` family; unify the
   scattered Desktop folders under one `exports/` root going forward.
4. **Power Tools** — collect `/api/selftest`, `/api/spend`, trash/restore, GPU status, dev chat,
   `agent-note`, and bulk ops.

**Risk: low** — mostly UI assembly over endpoints that already exist.

---

## Phase 6 — Cutover to port 5180 (~half day)

Only after Phases 1–5 pass real use on the temp port:
1. Stop subtitle-studio (it owns 5180). Keep its folder intact as a fallback.
2. Flip Video Studio's `PORT` to **5180**; register it as the launcher entry that used to point
   at subtitle-studio.
3. Smoke-test each tab end-to-end on 5180.
4. Keep autoVSL dashboard (5170) and dubbing-studio (7860) alive for one grace period, then
   retire from the launcher (folders stay on disk).

**Risk: low** — reversible; originals never deleted.

---

## What we explicitly do NOT do first (deferred)

- ❌ Merging the four venvs into one (torch 2.5.1 / numpy<2 / opencv 4.10 pins are load-bearing).
  Keep calling existing venvs as subprocesses. Consolidate later, if ever.
- ❌ Rewriting engines (ProPainter, XTTS, Wav2Lip, whisper) — reuse via subprocess.
- ❌ Deleting or moving files out of subtitle-studio / autoVSL / dubbing-studio.
- ❌ A framework rewrite (React/Vite). The vanilla-JS dark UI already works; keep it, restyle it.
- ❌ Cloud/hosting. Stays 100% local on 127.0.0.1.

---

## Sequenced checklist

- [x] **P0** Snapshot ports, assets, run commands (2026-07-16).
- [x] **P1** Scaffold `video-studio/`; copy dashboard `server.py`; `config.json`; run on :5181 (2026-07-16).
- [x] **P2** Persistent + resumable job store (`app/jobs.py`); GPU_LOCK + wait_for_gpu ported; resume-on-startup + `POST /api/job/<id>/resume` (cloud dubs excluded from blind resume — cost stays gated). Verified across two real restarts (2026-07-16).
- [x] **P3** *(2026-07-16)* — erase engine swapped to subtitle-studio's `erase_subs.py`
  (auto band detection + resume cache; CLI verified drop-in compatible, cache lands next to the
  output file). Transcript & Script tab built (list → transcribe → verify → edit → AI rewrite →
  save for dubbing). Captions tab built: `/api/recaption` (modes: captions / burn-lines / cover /
  no-captions) wraps `recaption.py` on the whisper venv, `GET/POST /api/captions/<stem>` edits
  `lines.json`, `/api/aifix/<stem>` ported (local Claude spell-fix), `/captioned/<stem>` serves
  results from subtitle-studio's output/. Note: Captions tab targets uploads (unique stems);
  dubbed finals (all named `final.mp4`) keep their caption path on the Dubbing page.
  Dubbing consolidation deferred to P4 alongside DubSync Repair.
- [x] **P4** *(2026-07-16)* DubSync Repair shipped: `app/engines/dubsync_repair.py` (remux /
  refit / renorm / relipsync via dubbing-studio's lipsync.py), `GET /api/dubs`,
  `POST /api/dubsync/repair`, dubsync.html UI with takes + promote (reuses `/api/dub-promote`).
  Provenance already existed in the dub workdirs (`source.txt`, `voice.json`, `versions.json`) —
  no new record needed. Repairs never touch final.mp4; each lands as a versioned take.
  Verified end-to-end with a real renorm repair (1.3 s, take listed + promotable).
- [x] **P5** *(2026-07-16)* Library (home, live pipeline-stage cards), New Project (upload →
  transcribe), Power Tools (persistent jobs w/ Stop/Resume + spend ledger), Exports
  (`GET /api/exports` aggregates VSL renders + edits + dub finals + captioned videos incl.
  subtitle-studio output; `POST /api/exports/send` copies into the ONE Desktop folder
  `Desktop\Video Studio`, never clobbering). All 11 tabs functional. Verified: 19 deliverables
  listed (1.05 GB), real send-to-Desktop copy confirmed.
- [x] **P6** *(2026-07-16)* Cutover DONE: subtitle-studio's server stopped (0 running jobs,
  folder untouched, restartable via launch.json with autoPort), `config.json` port → 5180,
  launch.json updated. All 14 pages + 7 APIs smoke-tested 200 on :5180, zero console errors.
  **Video Studio now owns http://localhost:5180.** Rollback: stop video-studio, restart
  subtitle-studio.

**Rollback at any point:** delete `video-studio/app`, restart subtitle-studio on 5180. Because
the migration is additive and path-based, nothing in the source projects ever changed.

---

## Post-migration additions

- [x] **Visual Repair** *(2026-07-16)* — `app/engines/visual_repair.py`: restores lip-sync visual
  damage (mangled objects/background/face) from the ORIGINAL video, frame-matched, no AI, free.
  ZNCC alignment on masked thumbnails (anchors → identity/offset/retime model fit → held-out
  validation → per-frame monotone fallback → hard refusal below ZNCC 0.6), feathered-ellipse
  composite keeping only the user-drawn lip region from the dub, low-frequency color transfer
  hides seams, block-level artifact report. `POST /api/dubsync/repair {action:"visual"}` +
  synchronous `POST /api/dubsync/visual-preview` (4-panel strip). UI: box-draw + preview on
  /dubsync. Verified on 1783501512600.publer.com-2: identity alignment (residual 0.0, ZNCC .977),
  575/660 frames had localized damage, repaired output matches source outside the box (diff 2.19)
  and the dub inside it; the mangled drinking-cup frame restored pixel-perfect.
- [x] **Brand Studio (Coffee UI Studio)** *(2026-07-16)*: new `/brand-studio` tab turns ComfyUI into a
  brand-locked social-content engine for liitt/Fairy Flame. Thesis: SD1.5@4GB can't render text/logos, so
  the model paints BACKGROUND only and a deterministic Pillow compositor (`app/engines/compositor.py`)
  overlays brand text/CTA/wordmark from real OFL fonts + hex tokens → pixel-identical branding.
  `app/engines/brand_content.py` orchestrates generate→ESRGAN 4x→composite via the job queue.
  Endpoints `/api/brand/{health,formats,copy,generate,campaigns,wordmark}`. Locked `liitt` twin-wick
  wordmark rendered once (user approves in-UI). Verified E2E: real 1080×1080 pain-mirror Meta ad,
  compliant copy (no banned words/claims, names Fairy Flame, "supports" framing), correct brand type +
  gold CTA (never white-on-gold) + wordmark. Raw ComfyUI stays at `/studio` (advanced). Phase 1 = Meta
  1:1; P2/P3 (4:5, 9:16, carousels, web scraper-refresh) pending.
- [x] **Voice Bank** *(2026-07-22)* — new tab `/voices`: clone the voice from any library
  video, save it as a named voice (with an audio preview), and reuse it to dub OTHER characters.
  Extraction is free/local (ffmpeg — a clean ~20s reference, no GPU). In the Dub step's local
  engine, a "Voice" picker lets you choose the on-screen speaker (default) or any saved voice;
  the saved reference feeds XTTS via `local_dub.py --voice-ref` → `dubbing-studio --reference`.
  Endpoints `/api/voices`, `/api/voices/{create,rename,delete}`; voices live in
  `output/voices/<id>/`. (Local single-dub for now; per-speaker banked voices in interview mode
  is a follow-up.)
- [x] **Image → Video** *(2026-07-19)* — new tab `/image-to-video`: upload a picture + a
  motion/scene prompt → a ~30s clip via fal.ai. fal image-to-video models only render 5-10s,
  so `engines/i2v_gen.py` chains segments (each segment's last frame seeds the next) and
  concatenates to the target length, staying continuous from the one uploaded still. Model
  registry (Kling 2.1 / Kling 2.1 Pro / Hailuo 02 / Wan 2.2) with aspect (9:16/1:1/16:9) and
  length (10/20/30s) choices, a live cost estimate, and a `confirm_cost` gate before any spend
  (recorded in the shared fal.ai ledger). Clips land in `output/i2v/<slug>/clip.mp4` and appear
  in Exports. Endpoints: `/api/i2v/{models,upload,estimate,run,list}`.
- [x] **Interview mode — two-speaker dubbing** *(2026-07-18)* — for interviewer + guest videos:
  "Detect speakers" (dubbing.html step 🎤) diarizes locally for free (`dashboard/diarize.py`:
  whisper transcript → voice embeddings (resemblyzer, MFCC fallback) → 2-means → labeled turns +
  auto clone-reference windows → `duo-config.json`); user reviews turns (chip-flip wrong labels,
  rename speakers, edit each line against its word budget), then `POST /api/duo/run` (cost-gated)
  drives the pre-existing `scripts/script-swap-duo.py`: both voices cloned, every line re-voiced
  with the right voice locked to its original window, sync.so v2 active-speaker lip-sync so each
  face moves only during its own lines. `dashboard/duo_run.py` lands the result as work/final.mp4
  so Captions/DubSync/Exports pick it up. New endpoints: `/api/duo/{config,diarize,run}`.
- [x] **Clone Winner tab** *(2026-07-18)* — scale a proven ad without guessing: pick any finished
  dub as the "winner", Claude writes a NEW script with the same winning structure/angle but fresh
  wording (length auto-fitted to the chosen footage duration × the winner's measured speaking pace),
  choose the same actor or any video from the uploads library, then the existing dub chain renders it
  (local XTTS + Wav2Lip HD free by default; fal.ai tiers behind the cost gate — same-actor fal reuses
  the winner's voice.json so the clone fee is skipped). Clones are ordinary `output/script-swap/
  <winner>-vN/` workdirs, so they appear in DubSync Repair, Captions, and Exports automatically.
  New: `static/clone.html`, `/api/clone/{winners,actors,script,run,list}`, nav entry.
- [x] **Object Repair** *(2026-07-17)* — the inverse of Visual Repair and the new DEFAULT fix:
  base = DUB untouched (lip-sync sacred), restore ONLY the damaged object's pixels from the
  original. `app/engines/object_repair.py`: NCC object tracking anchored to the vision path,
  face tracking for lip placement, diff-hysteresis damage mask in an object corridor,
  damage-based mouth-protection gate (opens only when the object truly collides with the lips),
  OR±2 temporal smoothing, exact-frame-count guarantee. Chat-driven ("fix the cup") via the
  advisor's new "object" action. Verified on the cup video: 660/660 frames, talking mouths
  byte-identical to the dub, sip frames restored to the original, smooth ramp transitions.
  Also fixed the `-shortest` frame-drop bug in visual_repair + 0-byte takes filtered.
- [x] **Visual Repair v2 — describe-and-fix + drag&drop** *(2026-07-16)*: no drawing needed.
  `POST /api/dubsync/advise` — the user types what's wrong (or nothing); local Claude (vision,
  `--allowedTools Read`) looks at 3 sample frames, locates the lips (median-sized box for moving
  speakers + track flag, union for static), picks the repair action, explains itself, and the
  server returns a ready-to-run config + preview strip. `POST /api/dubsync/upload` — drop a
  damaged video + its original straight onto the DubSync page (creates a repair workdir; no
  pipeline needed). Manual box-draw remains as fallback. Verified E2E on the cup video: complaint
  → auto lip box ≈ hand-drawn box, tracking auto-detected, cup frame restored.
