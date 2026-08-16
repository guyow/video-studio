# Hermes Agent — Video Studio Integration Plan

> Prepared by Hermes Agent · 2026-08-16 · For Claude Code implementation.
> Goal: make Hermes a first-class agent inside the studio — it sees
> everything, proposes execution plans, the founder approves, Hermes
> executes via the API and advises in real time.

---

## 1. What exists today (verified live)

| Capability | Status |
|---|---|
| Studio API + PIN auth (`POST /api/login`) | ✅ works |
| Hermes watcher (VPS cron, polls /api/overview+jobs+exports+winners+spend every 10 min, reports to Telegram) | ✅ works |
| Telegram chat loop (founder ↔ Hermes) | ✅ works |
| Hermes reads studio code from the GitHub clone on the VPS | ✅ works (repo slightly stale — see T6.1 in improvements plan) |
| Hermes pulls outputs via `/media/...` for QA (qa_vsl.py + faster-whisper) | ✅ works |
| **Execution-plan approval loop** | ❌ missing (founder must say "go" in chat only) |
| **Studio pushing events to Hermes** | ❌ missing (Hermes polls) |

## 2. Target architecture

```
┌─────────────┐   POST /api/plans (create)    ┌──────────────────┐
│  Hermes     │ ─────────────────────────────▶ │  Video Studio    │
│  (VPS)      │   GET /api/plans (poll status) │  (Windows)       │
│             │ ◀───────────────────────────── │                  │
│  watcher    │   POST /api/events (webhook)   │  dashboard /agent│
│  cron+agent │ ◀───────────────────────────── │  shows plans +   │
│             │   GET /api/fal/status          │  approve/reject  │
│  Telegram   │ ──────────────────────────────▶│                  │
└─────────────┘   /api/jobs, /media, /api/...  └──────────────────┘
```

**Rule (already agreed with the founder):** no production action (especially
any paid step) runs before an execution plan exists AND the founder approved
it. Hermes enforces this on its side; the studio enforces it on the API side
(paid endpoints require an approved plan id).

## 3. Studio-side work (Claude Code)

### 3.1 Plans API (P0)
- `POST /api/plans` — body: `{title, steps: [{step, cost?, engine?}],
  cost_estimate, asks, ref?}` → `{id}` · status=pending · author=hermes.
- `GET /api/plans` — list; filters `?status=pending|approved|rejected|done`.
- `POST /api/plans/<id>/approve` / `/reject` — body `{comment?}`;
  approve requires the founder session (dashboard button) or a signed
  request from Hermes's Telegram flow.
- `POST /api/plans/<id>/status` — Hermes marks done/executing (optional).
- **Enforcement**: `clone/run`, `generate-video`, `i2v/run` and any other
  paid endpoint REQUIRE `plan_id` in the body when `confirm_cost:true`;
  reject 403 if the plan isn't approved. (Keep a `bypass` for founder
  dashboard runs.)

### 3.2 Events webhook (P1, replaces polling)
- `POST /api/events` — append event `{type, at, payload}` (job done/failed,
  dub complete, spend delta, plan created/approved, QA verdict).
- `GET /api/events?since=<id>` — Hermes drains the queue (crash-safe:
  events persist in `data/events.jsonl`, id = line number).
- Optional push: `config.json: webhook_url` → studio POSTs to Hermes's
  gateway endpoint; polling stays as fallback.

### 3.3 Fal health (P0)
- `GET /api/fal/status` — validates the key, returns
  `{valid, message, key_masked, checked_at}` (see improvements T1.1).
- Hermes runs this before every paid plan and surfaces it in the plan card.

### 3.4 Agent identity (P2)
- Optional `AGENT_TOKEN` in config.json: Hermes calls the API with
  `Authorization: Bearer <token>` instead of the shared PIN — makes plan
  authorship + approval enforcement auditable.

## 4. Hermes-side work (VPS)

### 4.1 Plan-first execution (P0) — new behavior, already agreed
Before any production action:
1. Hermes writes the execution plan: `POST /api/plans` (studio) +
   posts the same plan to Telegram (home channel).
2. Hermes waits for `status=approved` (polls `GET /api/plans/<id>` or
   receives the webhook event).
3. On approve → execute → report result + QA verdict back to Telegram
   and as an event.
4. On reject → read the comment, revise the plan, re-submit.

### 4.2 Watcher upgrade (P1)
- Switch from polling every 10 min to draining `/api/events?since=<id>`
  every 2 min + keep `/api/overview` snapshot on failures.
- Add fal status + spend delta to every report (already partly there).

### 4.3 Auto-QA on new outputs (P1)
- When a dub/VSL output appears (event), Hermes pulls it via `/media/`,
  runs `qa_vsl.py --transcribe` + banned-words + pace checks, and posts a
  QA verdict card to Telegram + writes it back via `POST /api/agent-note`
  or the plan's status.

### 4.4 Chat advice loop (P0, mostly exists)
- Founder messages Hermes in Telegram while working → Hermes checks live
  studio state (`/api/jobs`, `/api/overview`) and answers with
  facts+recommendations. Keep the responses short and actionable.

## 5. Security & guardrails

- **Approval gate**: paid endpoints reject unapproved plans (3.1).
- **Spend cap**: `config.json: spend_cap_daily` — the studio refuses paid
  runs above the cap; Hermes alerts on deltas (watcher already does).
- **No secrets in plans**: Hermes plans never include keys/PINs; fal status
  returns only masked key info.
- **Read-only defaults**: Hermes's non-plan API surface stays read-only
  (jobs, media, dubs, winners); the only writes are plans, approvals
  (founder-side), agent-notes and job execution it was approved for.

## 6. Acceptance criteria

- [ ] Hermes can create a plan via the API; it appears in `/agent` with a
      nav badge; founder approves/rejects with a comment
- [ ] A paid `clone/run` WITHOUT an approved `plan_id` returns 403
- [ ] Job completion/spend/QA events flow to Hermes (webhook or drain)
- [ ] Hermes produces a full VSL with zero chat "go" pings after the plan
      approval (dub → captions → QA → report)
- [ ] fal blocked → plan card shows the fix; no run is attempted

## 7. Priority order

1. **P0**: Plans API + approval gate (3.1, 3.3) + Hermes plan-first flow (4.1)
2. **P1**: Events API + watcher drain (3.2, 4.2) + auto-QA (4.3)
3. **P2**: AGENT_TOKEN identity (3.4) · spend cap UI (UX/UI plan 4.6)

## 8. Files to touch
- Studio: `video-studio/app/server.py` (plans, events, fal/status, gate),
  `video-studio/app/static/agent.html` (panel, see UX/UI plan 4.2),
  `video-studio/app/static/vs-nav.js` (badge)
- Hermes (VPS): `~/.hermes/scripts/studio_watch.py` (events drain),
  `~/.hermes/scripts/` new `plan_exec.py` (plan-first executor),
  `~/marketing-workspace/scripts/qa_vsl.py` (auto-QA hook)
