# Video Studio Improvement Plan — Fairy Founder VSL Factory

> Prepared by Hermes Agent · 2026-08-16 · For Claude Code implementation.
> Goal: turn the studio into a repeatable **real-actress VSL factory** —
> same face, cloned voice, accurate lip-sync, karaoke gold captions — with a
> Hermes approval loop and spend guardrails.
> Grounded in: live API probing, code reading (app/server.py + engines), and
> today's production attempt (Founder_of_liitt-v4 failed on fal payment).

---

## Phase 1 — Clone Winner pipeline hardening (the core flow)

**Context:** `POST /api/clone/run` (server.py:3363) dubs a new script onto an
actor video: voice clone (fal) + TTS + lip-sync tier. It worked once
(Founder_of_liitt-v2, fal hd/latentsync, $2.17 total spend), then failed:
`fal.ai refused the request (payment/permission)` — account for the key in
`autoVSL/.env` locked/out of balance. Nothing was charged (good).

### T1.1 — fal pre-flight check endpoint
- Add `GET /api/fal/status` → validates the FAL_KEY (cheap test call, e.g.
  balance/account info endpoint) and returns: `{valid, message}`.
- Call it at the START of every fal dub; abort early with a clear message
  instead of failing mid-run (today's run spent ~1-2 min before failing).
- Also surface the key's age/masked id so a stale key is easy to spot.

### T1.2 — Voice clone from actor audio (actor != "same")
- Current code only reuses the winner's `voice.json` when `actor == "same"`
  (server.py:3410). For a different actor it creates a NEW clone from the
  actor video's audio. Verify this path actually works end-to-end (the v4 run
  died before we could see). Add explicit logging: `Cloning voice from
  <actor> audio (one-time $1.50)` vs `Reusing winner voice (free)`.
- Add a `POST /api/voice/clone` endpoint to pre-clone a voice from an actor
  video and save it, so later dubs skip the fee.

### T1.3 — Cost estimate before starting
- Add `POST /api/clone/estimate` (mirror of the existing i2v estimate):
  `{winner, actor, script, engine, tier}` → `{this_run, summary}` BEFORE the
  job starts; include in the job log line 0 so the founder sees cost upfront
  (Hermes approval flow reads this).

### T1.4 — Wire into run-campaign.sh
- `scripts/run-campaign.sh` (VPS) currently orchestrates build-vsl only.
  Add a `--clone` mode: script → clone/run (fal, tier=latentsync) → captions →
  QA → report. Per brief 03 (lipsync): full automation.

### T1.5 — Tier defaults & docs
- Default `tier` for fal engine: **latentsync** (best quality/cost,
  $0.005/s ≈ $0.21 per 41s VSL). Keep `veed` ($0.0067/s) and `pro/sync3`
  ($0.10-0.13/s) available. Document in the dashboard's dub UI.

---

## Phase 2 — Lip-sync quality loop (DubSync)

**Context:** DubSync Repair exists (engines/dubsync_repair.py, endpoints
`/api/dubsync/advise|repair|visual-preview`): remux (instant), refit
(time-stretch), renorm (loudness), relipsync (local Wav2Lip + GFPGAN —
FREE redo without paying for a new dub).

### T2.1 — Auto-advise after every fal dub
- When a fal dub completes, automatically run the advise step (Claude decides
  which fix is needed) and append the recommendation to the job log:
  `DubSync advise: OK` or `DubSync advise: relipsync recommended (free)`.
- Founder can then hit one button to apply the recommended repair.

### T2.2 — relipsync verification on 4GB GPU
- Verify relipsync (Wav2Lip + GFPGAN/CodeFormer) runs on the 4GB card without
  OOM; if GFPGAN is too heavy, fall back to Wav2Lip-only (acceptable quality
  on close-ups) and note it in the job log.

### T2.3 — Repair → QA → promote loop
- After any repair take: run the same QA checks as T5.1, then auto-promote if
  clean. Never leave `final.mp4` stale (the dashboard already flags
  `captioned_stale` — extend the same pattern to repairs).

---

## Phase 3 — Captions & on-screen graphics (format fidelity)

**Context:** The target format (frame-read of cr_test2-165602.mp4) =
word-by-word karaoke captions: Arial Black, ALL CAPS, ~3 words/line, active
word in **liitt gold #F5C542**, thick outline + soft shadow; plus TikTok-style
badge callouts. Engines exist: subtitle-studio/recaption.py (100% local free)
and engines/tag_overlay.py (white rounded boxes, black text).

### T3.1 — Captions as part of the dub chain
- Today captions are a separate manual step (`action: caption` → captions the
  dub's `final.mp4` using `new-vo.mp3` word timing → `final-captioned.mp4`).
- Make it automatic: after every successful dub, queue the caption step
  (free). Deliverable = `final-captioned.mp4` + expose on `/media/`.

### T3.2 — Badge pack for the testimonial format
- Add a badge preset library to tag_overlay.py for this format:
  `2 WEEKS` (blue pill), `NO BUZZ / NO FOG / NO CRASH` (crossed-out white
  badges), `LINK BELOW` (white pill), `PROGRESS / WEEK N` (orange cards).
  Drive timing from the caption word timestamps (word → badge appears).

### T3.3 — Format template registry
- Save the podcast-testimonial format as a named template in the studio
  (`formats/testimonial-podcast.json`): shot structure (13 scenes: hook +
  insert + punch-back + week-by-week cards + crossed-claims + proof stack +
  product reveal + graphics-free CTA + tail hold), caption style, badge pack.
  Both `build-vsl` and `clone/run` can reference it.

---

## Phase 4 — Hermes integration (visibility + approval loop)

**Context:** Hermes now polls the studio every 10 min (overview/jobs/exports/
clone winners/spend) and reports to Telegram. The founder wants to SEE
execution plans and APPROVE before any production action (especially paid).

### T4.1 — Plans API (the approval "place")
- Add `POST /api/plans` (Hermes writes an execution plan:
  `{title, steps[], cost_estimate, asks}`), `GET /api/plans` (dashboard
  panel), `POST /api/plans/<id>/approve|reject` (founder, with comment).
- Hermes polls plan status before executing any paid step. Dashboard shows
  pending plans with an Approve/Reject button.
- Keep `POST /api/agent-note` for permanent facts (unchanged).

### T4.2 — Events webhook (optional, replaces polling)
- Add `POST /api/events` + a webhook target config: the studio PUSHES job
  completions/errors/spend to Hermes instead of Hermes polling. Polling
  (every 10 min) stays as fallback. Low priority.

### T4.3 — Spend reporting
- Already exposed via `/api/spend` + job cost lines. Add a daily spend digest
  endpoint (`GET /api/spend/daily`) so Hermes can alert on spend deltas.

---

## Phase 5 — QA & guardrails

### T5.1 — Auto-QA after every dub (studio-side)
- Port the VPS qa_vsl.py checks into the studio (or invoke it): duration
  30-120s, portrait 9:16, audio present, banned words
  (wellness/journey/holistic), talking pace 100-200 wpm, transcription via
  faster-whisper. Append PASS/FAIL to the job log; block "promote" on FAIL.

### T5.2 — Spend guardrails
- Pre-check fal balance before paid jobs (T1.1); fail-fast with the exact
  fix message (already good — keep the wording).
- Optional: daily spend cap setting (`config.json: spend_cap_daily`).

### T5.3 — qc-ai wiring
- The studio already runs AI QC review jobs (Claude) on outputs. Wire the
  clone flow to run qc-ai on the final-captioned output automatically.

---

## Phase 6 — Repo hygiene & known pitfalls

### T6.1 — Commit the Windows-only engines (IMPORTANT)
- The GitHub repo snapshot is missing engines that exist only on the Windows
  machine: `dub.py`, `local_dub.py`, `caption.py` (+ any new edits). This
  means the VPS copy (which Hermes reads) is stale vs. production.
- Action: commit/push the current Windows `video-studio/` + `autoVSL/` code to
  `github.com/guyow/video-studio` so both sides are in sync.

### T6.2 — Job log completeness
- On early failures (process never starts / missing venv), ensure stderr is
  captured into the job log (today's first failure had an empty log until the
  error field; make rc!=0 always append stderr tail).

### T6.3 — Mojibake scrub (exists)
- Keep the em-dash → U+FFFD scrub on Windows console output; verify it also
  covers the new endpoints.

---

## Acceptance criteria (Definition of Done)

- [ ] `clone/run` with a new script + actor video produces
      `final-captioned.mp4` (dub → captions) with karaoke gold captions
      matching the reference style, WITHOUT manual steps
- [ ] Fal runs start with a pre-flight check; payment failures abort in <5s
      with the exact fix (top-up / new key)
- [ ] `final-captioned.mp4` + QA report (PASS/FAIL) attached to every dub job
- [ ] Hermes can create a plan via API, the dashboard shows it, the founder
      approves/rejects, and Hermes only executes approved paid steps
- [ ] Repo in sync: Windows engines committed; VPS copy == GitHub
- [ ] Total cost of one VSL (41s): ~$2 (voice clone $1.50 one-time + TTS +
      latentsync ~$0.21) with zero-cost local fallback (XTTS + wav2lip-hd +
      relipsync)

## Files to touch (from code reading)
- `video-studio/app/server.py` — clone/run, plans API, fal/status, estimate
- `video-studio/app/engines/dubsync_repair.py` — relipsync/GFPGAN fallback
- `video-studio/app/engines/tag_overlay.py` — badge presets
- `subtitle-studio/recaption.py` — caption auto-run hook (or wrapper)
- `video-studio/scripts/run-campaign.sh` (VPS) — `--clone` mode
- `autoVSL/.env` — FAL_KEY (rotate/validate)
