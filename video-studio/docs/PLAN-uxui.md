# Video Studio UX/UI Organization Plan

> Prepared by Hermes Agent · 2026-08-16 · For Claude Code implementation.
> Grounded in the current code: `app/static/vs-nav.js` (IA), the existing
> lab pages, and the workflow needs from PLAN-studio-improvements.md.

---

## 1. Current state (what exists)

The studio already has a workflow-based nav (vs-nav.js, "2026-08-08 cleanup"):

| Group | Pages | Purpose |
|---|---|---|
| 🎬 Create | `/` (Mission Control) | products & scripts, build VSL, activity |
| ✂ Edit | `/timeline` | the NLE |
| ✨ Generate | `/image-to-video`, `/broll`, `/image-editor`, `/frame-reader` | AI generation studios |
| 🏷 Brand & Ads | `/batches`, `/brand-studio`, `/creator`, `/mission`, `/studio` | ad factory |
| 🎙 Voices | `/voices` | voice bank |
| 🧰 Tools | `/tools`, `/exports`, `/qc-lab`, `/dubsync-lab`, `/clone-lab`, `/subtitles-lab`, `/dubbing-lab`, `/transcript-lab`, `/editor` | labs + legacy |
| ⚙ Settings | `/settings` | config |

**Gaps found (from today's production attempt):**
- The **Clone Winner (actress VSL) flow** is a raw API call — no guided UI.
- **No approval surface** — Hermes execution plans have nowhere to appear.
- **fal status is invisible** — the founder only learns of a payment failure
  from a job log after it fails.
- **QA results live in job logs** — no PASS/FAIL card.
- Captions are a separate manual step — no preview before burning.

## 2. Design principles

1. **One guided flow per job type** — the studio is a factory; each product
   type (VSL, clone dub, caption) is a stepper, not a form buried in Tools.
2. **Status at a glance** — every job card shows: stage, spinner/check,
   cost, QA verdict, and the next actionable button.
3. **Approve/reject in place** — pending Hermes plans appear in the nav as a
   badge; approve/reject without leaving the page.
4. **Errors show fixes** — payment failure → "Top up at fal.ai → or replace
   key" inline, not a raw JSON blob.
5. **Everything old stays reachable** — grouped, not removed (keep the
   existing IA promise).

## 3. Target IA

| Group | Change |
|---|---|
| 🎬 Create | + "🎭 Actress VSL" quick action (opens Clone Lab stepper) · + 🤖 Agent panel card (pending plans count) |
| ✂ Edit | unchanged |
| ✨ Generate | unchanged |
| 🏷 Brand & Ads | unchanged |
| 🎙 Voices | unchanged |
| 🧰 Tools | Clone Lab upgraded to stepper flow (see §4.1) · DubSync Lab gets auto-advise card |
| **🤖 Agent (NEW)** | `/agent` — Hermes integration panel |
| ⚙ Settings | + fal status card + spend cap field |

## 4. New/changed screens

### 4.1 Clone Lab → guided stepper (P0)
`/clone-lab` becomes a 5-step wizard:
1. **Winner** — pick from `/api/clone/winners` (cards: stem, source, voice,
   script preview)
2. **Actor** — same as winner or uploads bank (face preview)
3. **Script** — paste / `/api/clone/script` generate (word count + target
   seconds + wpm shown live)
4. **Engine & tier** — local (free) vs fal (latentsync/veed/pro) with cost
   estimate from `/api/clone/estimate` + fal health pill
5. **Review & run** — summary card → run → job progress (dub → captions →
   QA) with a **QA verdict card** at the end + "Open in Exports"

### 4.2 Agent panel (P0) — `/agent`
Sections:
- **Pending plans** — cards from `GET /api/plans?status=pending`:
  title, steps (collapsible), cost estimate, asks →
  [✅ Approve] [❌ Reject + comment] (POST `/api/plans/<id>/approve|reject`)
- **Activity feed** — Hermes events: plan created, job started/done, spend
  delta, QA verdicts (from `/api/events`)
- **Spend ledger** — current total + today's delta + fal health

### 4.3 Fal status pill (P0)
- Component rendered from `GET /api/fal/status`: green "fal OK" / red
  "fal blocked — top up or replace key" (link to `/settings#fal`).
- Placed: Tools header, Clone Lab step 4, Settings.

### 4.4 QA verdict card (P1)
- On every dub job + exports item: PASS/FAIL + checks list (duration,
  9:16, audio, banned words, wpm) — from the studio-side QA (T5.1).
- FAIL → inline fix suggestions (e.g. "pace 210 wpm → slow the script").

### 4.5 Caption preview (P1)
- In Subtitles Lab: before burning, show the karaoke captions as a timed
  preview (words highlight in liitt gold as the timeline scrubs).
- One-click burn → `final-captioned.mp4` → auto QA.

### 4.6 Settings (P2)
- fal card: key masked id, balance status, last check time, [Test now].
- Spend cap field (daily) + notifications toggle.

## 5. Key flows

**Actress VSL production (the core job):**
Create → "🎭 Actress VSL" → Clone Lab stepper → review & run →
job progress (dub → captions → QA) → exports. Founder approves the cost in
step 4 via the estimate card.

**Hermes approval loop:**
Hermes writes plan (`POST /api/plans`) → nav badge appears + Telegram ping →
founder opens `/agent` → Approve/Reject (+comment) → Hermes polls status →
executes only approved steps → result posted back as an event + Telegram.

## 6. Component specs (shared)

- **PlanCard** — title, meta (author=Hermes, created), steps list with
  cost tags, asks, footer buttons (Approve/Reject) — used in `/agent` and
  Mission Control.
- **FalPill** — status dot + label + fix link.
- **QaCard** — verdict + checklist + fix suggestions.
- **Stepper** — numbered stages, current stage highlighted, back/next,
  per-stage validation against the API.

## 7. Priorities

- **P0** (unblocks production): 4.1 Clone stepper · 4.2 Agent panel ·
  4.3 fal pill
- **P1** (quality loop): 4.4 QA card · 4.5 caption preview
- **P2** (polish): 4.6 settings cards · component theming

## 8. Files to touch
- `video-studio/app/static/vs-nav.js` — add 🤖 Agent group + badge
- `video-studio/app/static/clone-lab.html` (or new `agent.html`) — new pages
- `video-studio/app/static/js/vs-core.js` — shared components (PlanCard,
  FalPill, QaCard, Stepper)
- `video-studio/app/server.py` — serve `/agent`, plans/events endpoints
  (see PLAN-studio-improvements.md T4.1/T4.2)
