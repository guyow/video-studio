# Video Studio UX/UI — Comfortable Work + Hermes Stage Supervision

> Prepared by Hermes Agent · 2026-08-16 · For Claude Code implementation.
> This is the practical spec for HOW the studio UI should change so that
> (a) working in it feels comfortable, and (b) Hermes can supervise every
> production stage in real time.
> Companion docs: PLAN-studio-improvements.md (engine work),
> PLAN-hermes-integration.md (API/agent work), PLAN-uxui.md (IA overview).

---

## 1. The one idea: every job is a visible stage pipeline

Today a job is a black box: `running → done` + a log. Nobody (founder OR
Hermes) can see *which* stage it is on, what it produced so far, or where it
failed — until it's over.

**Change:** every production job exposes a **stage map** that the UI renders
as a stepper AND Hermes reads as a supervision feed. One contract, two
consumers.

### 1.1 Stage-State contract (API)

`GET /api/job/<id>` gains:

```json
{
  "stages": [
    {"key": "plan",      "label": "Plan & cost",     "status": "done",    "started": 1786..., "finished": 1786..., "artifact": null,  "meta": {"cost_est": 2.05}},
    {"key": "voice",     "label": "Voice (clone/TTS)","status": "running", "started": 1786..., "finished": null,   "artifact": "output/script-swap/x/new-vo.mp3"},
    {"key": "lipsync",   "label": "Lip-sync",         "status": "pending", "started": null,    "finished": null,   "artifact": null},
    {"key": "captions",  "label": "Karaoke captions", "status": "pending", "started": null,    "finished": null,   "artifact": null},
    {"key": "qa",        "label": "QA verdict",       "status": "pending", "started": null,    "finished": null,   "artifact": null,  "meta": {"verdict": null}},
    {"key": "export",    "label": "Export",           "status": "pending", "started": null,    "finished": null,   "artifact": "output/.../final-captioned.mp4"}
  ]
}
```

- **Status values:** `pending · running · done · failed · skipped · warned`.
- Engines write stage transitions (one line: `stage <key> <status>` to the
  job) — same mechanism as today's log lines, structured.
- Stage maps are **stable per job type**: dub/clone =
  `plan → voice → lipsync → captions → qa → export`; build-vsl =
  `plan → vo → video → assemble → qa`; caption = `plan → captions → qa`.
- **Supervision hook:** `POST /api/job/<id>/stages/<key>/note` — anyone
  (Hermes) can attach a short annotation to a stage, rendered in the UI:
  `{"note": "lip sync drifts at ~12s — relipsync recommended (free)"}`.

## 2. UI changes

### 2.1 Job cards (Mission Control + Tools → Jobs) — P0

Every job card becomes:
```
┌──────────────────────────────────────────────┐
│ 🎬 Founder_of_liitt-v5 · dub · fal/latentsync │  $2.05
│ [●plan][▶voice][○lipsync][○captions][○qa][○export]  ← stepper strip
│ ⚠ fal OK · next: lip-sync (~40s) · [▶ expand]        ← "what's next"
└──────────────────────────────────────────────┘
```
- Stepper strip: colored dots with connector line, one row, tiny.
- **"What's next" hint**: the UI computes the next actionable stage from the
  stage map (the single most useful line for comfort).
- Auto-refresh via fetch polling every 5s (no full page reloads); the strip
  updates in place. Click card → detail panel (2.2).

### 2.2 Job detail panel — P0

Expandable side panel / section with:
- Full stage list: per stage — status dot, label, duration, artifact link
  (play/download), log slice, note (if any), [Retry] for failed/warned
  stages (retry re-queues ONLY that stage when safe — e.g. relipsync).
- QA verdict card at the `qa` stage: PASS/FAIL + checks + fix suggestions.
- Error banner per failed stage: the fix in one line
  (`fal blocked → top up or replace key in autoVSL/.env`).
- Cost line: estimate (plan) vs actual (per stage where known).

### 2.3 Agent supervision panel (`/agent`) — P0

A **stage-feed view** for Hermes supervision, readable by the founder too:
- Live feed of stage transitions: `14:02 lipsync done (41s) · 14:03 captions
  running …` — one line per transition, newest first.
- Hermes annotations appear inline at the right stage (e.g. QA note,
  "voice sounds thin — try tts=f5 on retry").
- **Attention flags**: red for failed stage needing a decision, amber for
  warned (QA borderline, spend over estimate), blue for a Hermes question
  ("approve relipsync? free").
- Pending **Hermes plans** (from PLAN-hermes-integration 3.1) with
  Approve/Reject live in this panel.

### 2.4 Clone Lab stepper = the stage map — P0

The Clone Lab wizard (PLAN-uxui.md 4.1) does NOT end at "run" — after run,
the wizard becomes the **live stage viewer** (same component as 2.2), so
producing a VSL is one continuous 5-step → 6-stage experience with zero
context switches.

### 2.5 Comfort details (cheap, high value) — P1

- **Colors are information**: pending gray · running blue pulse ·
  done green · failed red · warned amber · skipped dashed. Same palette in
  cards, steppers and feed — learn once, read everywhere.
- **Keyboard**: `j/k` move between cards, `Enter` expand, `r` retry stage,
  `a` open Agent panel.
- **No dead ends**: every "failed" state offers [Retry] or [Fix] (a link to
  the exact fix) — never a bare red block.
- **Sticky "what's next"**: the most important comfort rule — every screen
  answers "what should I do now?" at a glance.
- Artifacts always linkable (play/download) as soon as they exist — you can
  watch the VO while lip-sync still runs.

## 3. Hermes supervision loop (how I watch the stages)

1. **Watch**: Hermes drains job stage transitions (via `/api/events` from
   PLAN-hermes-integration, or the watcher poll as fallback).
2. **Act at stage boundaries** (stage hooks):
   - `voice done` → nothing (automatic).
   - `lipsync done` → Hermes runs `dubsync/advise`; if a fix is needed,
     posts a note + blue attention flag ("relipsync recommended, free").
   - `captions done` → Hermes pulls the captioned file, checks word-sync
     drift; notes any issue.
   - `qa` → Hermes runs its own checks (qa_vsl.py: duration/9:16/banned
     words/wpm) and writes the verdict + comparison to the studio's QA card.
   - any `failed` → Hermes reads the stage error, posts the one-line fix,
     and (if free/cheap) proposes a retry plan via the plans API.
3. **Report**: stage-level summaries go to Telegram (compact, one line per
   transition, only the interesting ones — not spam).
4. **Escalate**: spend over estimate, QA fail, or a stage stuck >5 min →
   Hermes pings with options, waits for the founder's call.

## 4. Priority order

| Prio | Item |
|---|---|
| P0 | 1.1 stage-state contract + engines write stages · 2.1 job cards stepper · 2.2 detail panel · 2.3 agent panel · 2.4 clone stepper→stage viewer |
| P1 | 2.5 comfort details · 3.2 advise/QA stage hooks · 3.4 stage-level Telegram reports |
| P2 | retry-by-stage safety matrix · keyboard nav polish |

## 5. Acceptance criteria

- [ ] A dub job shows 6 stages; each transitions live (pending→running→done)
      in the UI without a page reload
- [ ] Founder can see "what's next" on every card and retry a failed stage
- [ ] Hermes receives stage transitions and posts a QA verdict at the `qa`
      stage (visible in the UI + Telegram)
- [ ] A failed stage always shows a one-line fix + [Retry]/[Fix] action
- [ ] The Clone Lab wizard and the live stage viewer are the same component

## 6. Files to touch
- `video-studio/app/server.py` — stage-state in jobs, stage note endpoint,
  stage transitions from engines
- `video-studio/app/engines/*.py` — write `stage <key> <status>` lines
  (dub.py, local_dub.py, caption.py, dubsync_repair.py, qa hooks)
- `video-studio/app/static/js/vs-core.js` — Stepper, StageStrip, QaCard
  shared components
- `video-studio/app/static/index.html`, `tools.html`, `agent.html`,
  `clone-lab.html` — card/detail/panel integration
- VPS: `~/.hermes/scripts/studio_watch.py` + new `stage_watch.py` —
  Hermes-side stage hooks (3.2)
