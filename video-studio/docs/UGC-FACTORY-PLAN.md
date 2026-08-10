# UGC Factory + Character Bank — Implementation Plan

**Date:** 2026-08-02 · **Status:** ALL PHASES (1-4) BUILT 2026-08-02

Late additions (same day): **Character Generator** in /characters (describe or haiku-invent a
persona → FLUX-schnell portrait candidates on fal via `engines/char_gen.py`, ~$0.003/img,
cost-gated → pick → auto-register); **muted twins** (`ugc_factory.py mute` — every preview/
variant also ships sound-off, incl. a muted subtitled version; exported by finish);
**analysis delete** (`POST /api/ugc/<stem>/delete` — trashes the analysis + all its batches/
previews/captions; 🗑 button per analysis row). Fixes along the way: /media 403 (lowercase
drive letter in config — ROOT now resolved), Voice-Bank narration in assemble (XTTS CLI needs
--video; narration now runs after concat), silent-source guards (no captions without
narration), clone storyboard honors an explicit shot count, script-vs-footage length warning
in the cost dialog, busy states on all long-running buttons.

Phase 4 (verdicts → learnings, bulk variants, finish), as built:
- **Verdicts**: ✅/❌ on every preview card (reject requires a one-line reason) →
  `POST /api/ugc/<stem>/verdict` appends `banks/ugc-learnings.jsonl` (ul-XXXX ids,
  params snapshot incl. hook line). The learnings block is injected into every
  narrative/script generation prompt AND the variant-script prompt.
- **Bulk variants**: approving a preview unlocks the panel. `POST /api/ugc/<stem>/bulk`
  {count 1-20, voice same/cycle, regen_shots} → one sequential chain job. FREE by
  default: per-variant batch dirs copy recipe.json + generated.json (absolute clip
  paths → the paid clips are REUSED, zero re-render), each variant gets a fresh-hook
  script (`ugc_factory.py variant-script`, learnings-steered, length-locked to the
  approved script), its own XTTS narration (voice cycling), optional tags + subtitled
  twin. `regen_shots` re-renders visuals per variant on fal — 402 cost gate (N × run).
- **Finish**: `POST /api/ugc/<stem>/finish` exports approved previews (+ subtitled
  twins) to the Desktop exports folder (unique flat names) and soft-deletes the
  heavy workdirs (batch dirs, i2v dirs, recaption dirs) to `.trash/`. The analysis
  + recompose.json stay, with a `finished` record.
- Verified free path: verdict→bank entry, bulk guards (no batch 404, unapproved 400),
  finish export+trash round-trip. Paid preview/variant chains still await their
  first real run.

Phase 3 (recompose → preview), as built:
- `/ugc` tab gained a "Recompose → preview" panel: shots (0=auto) / aspect / model /
  voice (Voice Bank · clone-original · silent) / character / story-tags / subtitled-twin.
- **Storyboard (free)**: `POST /api/ugc/<stem>/storyboard` → `broll_video.py storyboard --ugc`
  with the original ad as `--ref` (visual DNA modelled) and an optional
  `--character-desc` from the Character Bank (new arg on broll_video.py). Editable
  shot table in-tab, saved via `POST /api/ugc/<stem>/recipe`.
- **Preview (paid, cost-gated)**: `POST /api/ugc/<stem>/preview` — 402 + estimate
  (real `t2v_fal --estimate-only`) until `confirm_cost`. Chain: t2v_fal (with
  `--character-dir` on reference models) → broll_video assemble (XTTS narration)
  → optional story tags → `ugc_factory.py finalize` (stable `<slug>.mp4` name,
  recorded in recompose.json) → optional caption.py subtitled twin at
  `output/recaption/<slug>/captioned.mp4`. Both players render in-tab.
- Reference models enforce a Character Bank character server-side (400 without one).
- Verified free path end-to-end (storyboard job, recipe edit, 402 estimate $1.40
  for 4 shots, character guard). Paid path (fal render → assemble → captions)
  NOT yet run — first real preview is the validation.

Built so far (deviations from the plan below noted):
- `/ugc` tab: analyze a library video → deep-dive teardown (deepdive.md / narrative.md /
  transcript.txt / analysis.json in `output/ugc/<stem>/`), editable in-tab, plus
  generate/regenerate of a new narrative and a duration-fitted new script
  (narrative-new.md / script-new.txt). Engine: `app/engines/ugc_factory.py`;
  raw Claude reply kept as `raw-claude.txt` for debugging.
- `/characters` tab: Character Bank at `output/characters/<id>/` (refs/ + character.json),
  create from uploaded image(s) or a picked frame of a library video, optional name with
  haiku auto-naming fallback, add-ref/rename/soft-delete. Routes live in `server.py`
  mirroring the Voice Bank (NOT a separate character_bank.py — the CRUD is small enough).
- `kling-o1-ref` added to both T2V registries (`t2v_fal.py` MODELS + server
  `T2V_FAL_MODELS`) with `reference: True`; `t2v_fal.py --character-dir` uploads a
  character's refs once and passes them as `image_urls` on every shot.
- Verified: server boots, all routes respond, Character CRUD round-trips, analyze job
  runs end-to-end (whisper → frames → Claude vision; on a non-ad test pattern Claude
  correctly declines and the reply is surfaced in the job log).

Two new menus in the shell nav (`vs-nav.js`):
- **UGC Factory** — analyze a reference UGC ad, recompose it with new script/voice/brand/character, preview one video, then bulk-generate approved variants.
- **Character Bank** — persistent registry of characters (reference images) used to keep the same person across every generated shot, mirroring the existing Voice Bank pattern.

---

## 1. Character Bank

**Storage:** `output/characters/<id>/` — mirrors `output/voices/<id>/`.
Each character folder holds:
- `refs/` — 1–7 reference images (face required; outfit/product refs optional)
- `character.json` — `{ id, name, created, source: "upload" | "video-extract" | "generated", notes, used_in: [...] }`

**Two registration paths, one bank:**
1. Directly in the Character Bank page (upload image(s), optionally name it).
2. Inside the UGC Factory flow — uploading a character image mid-flow, or extracting a clean face frame from the input video, **auto-registers** it into the bank so it's reusable later.

**Naming:** name field is optional. If empty, auto-generate a friendly name via the local Claude CLI (haiku) from the reference image — e.g. "Kitchen Mom — Blonde, 40s". User can rename anytime (`/api/characters/rename`, same as voices).

**Guardrail:** extracting a character from an uploaded video is a deliberate button ("Register this person as a character"), never automatic — cloning real people from competitor ads is a legal problem; the bank is for own footage, own talent, or synthetic characters.

**Routes** (new file `app/engines/character_bank.py`, thin route registration in `server.py`):
- `GET /api/characters` · `POST /api/characters/create` · `POST /api/characters/rename` · `POST /api/characters/delete`
- `POST /api/characters/extract` — pick best face frame from a library video (reuse QC frame sampling)
- Page: `static/characters.html` + nav entry

**Generation backend:** characters are consumed by reference-conditioned video models on fal:
- **Kling O1 reference-to-video** (`fal-ai/kling-video/o1/reference-to-video`, ~$0.112/s, up to 7 reference inputs)
- **Kling 3.0 elements** (`@Element1` prompt tagging) as it becomes available
- Add these to the `t2v_fal.py` registry with `supports_reference: true`; UGC Factory only enables the character option when such a model is selected.

Honest expectation: ~90–95% identity consistency across shots — reads as the same person in an ad, not pixel-identical under freeze-frame.

---

## 2. UGC Factory pipeline

New nav entry `/ugc` → step modules `static/js/vs-step-ugc-*.js`, engine logic in `app/engines/ugc_factory.py`, all long-running work registered through `jobs.py` (inherits persistence, GPU lock, resume, `confirm_cost` spend gating).

### Step 1 — Ingest & Deep-Dive
- Upload video (`/api/upload`)
- Transcribe: faster-whisper, word-level timing (existing `course_pipeline`)
- Frame sampling + Claude CLI vision (existing pattern from `/api/qc/ai-review`, `broll_factory.py analyze`)
- New `DEEPDIVE_PROMPT`: produces a prospector-style teardown (hook / beat structure / avatar / mechanism / claim hedging / format notes) + **narrative summary** + full transcript
- Output: editable markdown saved next to the video; user can correct before moving on

### Step 2 — Recompose
Header settings (set once, flow everywhere):
**Duration** (15s / 30s / 60s / custom) · **Shots** (auto = duration ÷ ~5–7s, manual override with inline validation against per-model segment caps) · **Aspect** (9:16 / 1:1 / 16:9, greyed per model constraints from `t2v_fal.py` registry) · **Model** · **Voice** (existing Voice Bank) · **Brand** (existing `liitt-brand-kit.json` / `compositor.py` / `tag_overlay.py`) · **Character** (optional — pick from bank, upload new → auto-register, or none)

Body:
- **Narrative**: editable, generate/regenerate from the deep-dive (reuses `CLONE_PROMPT` approach + `inspiration_block()` for hooks/angles banks)
- **Transcript/script**: manual, generated, or regenerated — length-fitted to chosen duration (script generated to fit, never trimmed after)
- **Storyboard**: editable shot table (shot N: line + visual description + character y/n), derived from script + shot count
- **Live cost estimate**: shots × seconds × per-second model rate, shown next to Generate

### Step 3 — Preview & Approve
- Generate ONE video: character shots via reference-to-video, non-character shots via T2V or b-roll bank, XTTS narration from selected voice, brand overlay/end-card, aspect-aware layout
- Render **two files: with subtitles and without** (word-timed ASS via existing `caption.py`; sub pass is a separate ffmpeg step anyway)
- User approves or rejects. **Reject asks for a one-line reason** → written to learnings (see §4)

### Step 4 — Bulk Variants
- User types N variants → total cost estimate → `confirm_cost` gate → single sequential job (one GPU job at a time is already enforced)
- **Cost lever:** variants reuse the approved video's paid character shots by default and vary the cheap layers (script/hook line, voice, captions style, b-roll selection, brand treatment); regenerating character shots is per-variant opt-in
- Every variant rendered with + without subtitles

### Step 5 — Finish
- Export to Desktop via existing `/api/exports/send`
- Auto-cleanup daemon (`cleanup.py`) soft-deletes heavy workdirs
- Learnings written (§4)

---

## 3. Files touched / created

| New | Purpose |
|---|---|
| `app/engines/ugc_factory.py` | pipeline logic + routes |
| `app/engines/character_bank.py` | bank CRUD + face-frame extraction |
| `static/ugc.html`, `static/js/vs-step-ugc-*.js` | UGC Factory UI |
| `static/characters.html` | Character Bank UI |
| `banks/ugc-learnings.jsonl` | approve/reject memory |
| `docs/UGC-FACTORY-PLAN.md` | this file |

| Modified | Change |
|---|---|
| `static/vs-nav.js` | two nav entries |
| `app/engines/t2v_fal.py` | add Kling O1 reference-to-video (+ Kling 3.0 when live), `supports_reference` flag |
| `server.py` | route registration only — new logic stays in `app/engines/` (server.py is 5k lines; do not grow it) |

**Project constraints to respect:** no `-shortest` (use `-frames:v N` + ffprobe verification) · one GPU job at a time (`GPU_LOCK`) · engines called as subprocesses across venvs, never imported · every paid fal call behind `confirm_cost` with ledger entry.

## 4. Learning memory

`banks/ugc-learnings.jsonl` — one entry per verdict:
```json
{"id":"ul-0001","date":"2026-08-02","video":"<stem>","verdict":"approved|rejected","reason":"...","params":{"duration":30,"shots":6,"aspect":"9:16","model":"kling-o1","character":"ch-0002","voice":"v-0003","hook":"..."}}
```
Generation prompts get a "past verdicts" block (same mechanism as `inspiration_block()`), so the model sees what was approved/rejected and why before writing the next script/storyboard.

## 5. Build order

1. **Phase 1 — Ingest & Deep-Dive** (mostly prompt work; immediately useful standalone; no paid calls)
2. **Phase 2 — Character Bank** (CRUD + UI + t2v_fal registry additions; cheap to build, needed by Phase 3)
3. **Phase 3 — Recompose + Preview** (wiring existing engines: script gen, storyboard, XTTS, captions, brand, reference-to-video; first paid-call surface)
4. **Phase 4 — Bulk + Learnings + Finish** (variant loop in jobs.py, learnings bank, export/cleanup wiring)

Suggested first validation before writing any UI: run the deep-dive prompt manually against one library video to confirm teardown quality is good enough to build around, and run one Kling O1 reference-to-video test shot to confirm identity consistency on real reference images.
