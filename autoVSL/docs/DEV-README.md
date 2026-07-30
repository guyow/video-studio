# autoVSL — System Architecture & Dev Handoff

**Audience:** Developers joining the VSL automation project  
**Status:** Phase 1 complete (generation pipeline works). ClickUp is manual. API sync is Phase 2.  
**Last updated:** June 2026

---

## Executive summary

autoVSL is an automated Video Sales Letter (VSL) production system. It turns structured scripts and shot lists into finished 9:16 vertical videos using cheap/free AI generation APIs and local ffmpeg assembly.

**Proven:** The Fairy Flame VSL was generated end-to-end — 8 VO lines (free), 8 video shots (~$1.60 via fal.ai), assembled locally to `output/fairy-flame.mp4`.

**Architecture decision:** Do **not** build a custom UI yet. **ClickUp** is the pipeline UI (statuses, avatars, product shots, spokesperson assets, performance data). **This repo** is the execution engine. **Cursor** is the operator console for now; dedicated agents plug in later via ClickUp API.

---

## The 3-layer model

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: ClickUp (Pipeline UI — manual for now)            │
│  • One task = one VSL                                       │
│  • 7-step statuses                                          │
│  • Custom fields: avatar, product, hook, script, metrics    │
│  • Attachments: product shots, spokesperson refs, final MP4 │
└──────────────────────────┬──────────────────────────────────┘
                           │  Phase 2: sync-clickup.py
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Layer 2: autoVSL repo (Execution Engine)                   │
│  • vsls/<slug>/ — generation artifacts per VSL              │
│  • scripts/ — VO, video, assemble                           │
│  • output/ — final MP4 exports                              │
└──────────────────────────┬──────────────────────────────────┘
                           │  Phase 3: agent APIs
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: Agents (future)                                   │
│  • Research agent → avatar briefs → ClickUp                 │
│  • VSL writer agent → script + shot list → ClickUp          │
│  • Media buyer agent → performance updates → ClickUp        │
│  • Operator (Cursor) → runs generation steps                │
└─────────────────────────────────────────────────────────────┘
```

| Layer | Tool | Responsibility |
|-------|------|----------------|
| Pipeline UI | **ClickUp** | Visibility, organization, human input (product shots, spokesperson, intel) |
| Execution | **autoVSL** | Generate VO, video, assemble MP4 |
| Agents | **Cursor** (now), bots (later) | Research, write, optimize, report |

**ClickUp is the UI. This repo is the factory. Agents are the workers.**

---

## Why ClickUp (not a custom UI)

| Approach | Verdict |
|----------|---------|
| Custom web UI now | Too early — weeks of dev, duplicates ClickUp |
| Cursor + scripts only | Works solo, no team visibility |
| **ClickUp + autoVSL** | **Chosen** — pipeline visible, API-ready, manual asset uploads |
| Custom UI later (Phase 4) | Only if ClickUp feels limiting for ops |

ClickUp was chosen because the team needs to manually add:

- Product shots and packaging references
- Spokesperson photo/video references
- Customer avatar research and intel
- Performance metrics from ad tests
- Notes and approvals at each step

That human layer stays in ClickUp. Code handles repeatable generation.

---

## 7-step VSL pipeline (ClickUp mapping)

Each VSL = **one ClickUp task** moving through these statuses. Exact step names may be refined when the full Notion system is migrated — this is the working template.

| Step | ClickUp status | What lives here | autoVSL artifact |
|------|----------------|-----------------|------------------|
| 1 | **Research** | Customer avatar, pain points, objections, competitor refs | — (ClickUp only for now) |
| 2 | **Hook / Angle** | Opening hook variants, emotional angle | Custom field |
| 3 | **Script** | Full VO script, line by line | → `elevenlabs-vo.json` |
| 4 | **Shot List** | Per-shot video prompts, negative prompt, style notes | → `kling-shots.json` |
| 5 | **Generating** | Media checklist: VO ✓, shots ✓, music ✓ | Scripts run here |
| 6 | **Review** | Draft MP4 attached, revision notes | → `output/<slug>.mp4` |
| 7 | **Live / Testing** | Ad links, CPA, CTR, hook winner, iterations | Media buyer agent (future) |

### Recommended ClickUp structure

```
Space: VSL Production
├── List: Customer Avatars          ← reusable research (not per-VSL)
│   └── Task: "Burned-out dad 40s"  ← avatar profile, pains, language
├── List: Spokesperson Assets       ← reference photos/videos per talent
│   └── Task: "Talent A — warm dad energy"
├── List: Product Assets            ← packaging, product shots, logos
│   └── Task: "Fairy Flame gummies"
└── List: VSL Pipeline              ← active production
    └── Task: "Fairy Flame — 30-day microdose"
        ├── Status: Research → … → Live
        ├── Linked: Avatar task, Product task, Spokesperson task
        └── Custom fields: slug, hook, CTA, aspect ratio, generation cost
```

---

## What's built today (Phase 1)

### Working scripts

| Script | Purpose | Cost |
|--------|---------|------|
| `scripts/generate-vo.sh` | VO from Edge TTS (free) | $0 |
| `scripts/generate-video.sh` | Video shots via fal.ai (Wan 2.2 default) | ~$0.20/clip |
| `scripts/assemble-vsl.sh` | ffmpeg assembly (VO + video, optional music) | $0 |
| `scripts/check-media.sh` | Verify all media files exist | — |
| `scripts/print-prompts.sh` | Dump prompts for manual generation | — |

### Per-VSL folder structure

```
vsls/<slug>/
├── brief.md              # Human-readable summary
├── elevenlabs-vo.json    # VO lines → vo-01.mp3 … vo-08.mp3
├── kling-shots.json      # Shot prompts → shot-01.mp4 … shot-08.mp4
├── suno-music.txt        # Music prompt (optional)
├── timeline.json         # Assembly spec (tracks, segments, edit rules)
└── media/
    ├── video/
    ├── audio/
    └── music/
```

### Reference VSL

`vsls/fairy-flame/` — complete example. Output at `output/fairy-flame.mp4`.

### Environment

```bash
cp .env.example .env
# FAL_KEY=...  (fal.ai — video generation only)
```

### Quick run (today's workflow)

```bash
./scripts/generate-vo.sh <slug>
./scripts/generate-video.sh <slug>          # needs FAL_KEY + credits
./scripts/assemble-vsl.sh <slug> --no-music # or with music if present
```

See root [README.md](../README.md) and [free-generation.md](./free-generation.md) for full commands.

---

## Data flow (current — manual)

```
1. Team fills ClickUp task (avatar, script, shots, product/spokesperson refs)
2. Operator copies data into vsls/<slug>/ JSON files
3. Operator runs generation scripts
4. Operator uploads output/fairy-flame.mp4 back to ClickUp for review
5. Media buyer updates performance fields in ClickUp when live
```

## Data flow (Phase 2 — automated sync)

```
1. ClickUp task created / updated
2. sync-clickup.py pulls task → populates vsls/<slug>/
3. Operator or agent runs: generate-vo → generate-video → assemble
4. sync-clickup.py pushes: status, MP4 path, cost, timestamps → ClickUp
```

---

## Planned repo structure

```
autoVSL/
├── docs/
│   ├── DEV-README.md          ← this file
│   └── free-generation.md
├── vsls/<slug>/               ← one folder per VSL (generation source of truth)
├── avatars/                   ← Phase 2: reusable avatar JSON synced from ClickUp
├── pipeline/
│   └── schema.json            ← Phase 2: 7-step field definitions
├── scripts/
│   ├── generate-vo.py / .sh
│   ├── generate-video.py / .sh
│   ├── assemble-vsl.sh
│   ├── check-media.sh
│   ├── print-prompts.sh
│   └── sync-clickup.py        ← Phase 2 (not built yet)
├── output/                    ← final MP4 exports
├── templates/vsl-template/    ← copy for new VSLs
└── .cursor/rules/             ← Cursor agent instructions
```

---

## Phased build plan

### Phase 1 — Done ✓

- [x] Per-VSL JSON schema (script, shots, timeline)
- [x] Free VO generation (Edge TTS)
- [x] Cheap video generation (fal.ai Wan 2.2)
- [x] Local ffmpeg assembly
- [x] First end-to-end VSL (fairy-flame)
- [x] ClickUp chosen as pipeline UI (manual setup by ops team)

### Phase 2 — ClickUp sync (next dev work)

- [ ] Define `pipeline/schema.json` — maps ClickUp custom field IDs → repo JSON keys
- [ ] `scripts/sync-clickup.py`:
  - **Pull:** ClickUp task → create/update `vsls/<slug>/`
  - **Push:** Update task status, attach MP4, write generation cost + notes
- [ ] Cursor rule: *"Start VSL from ClickUp task `<id>`"*
- [ ] Document ClickUp custom fields ops team must create

**Estimated effort:** 2–3 days  
**Dependencies:** ClickUp API token, list/field IDs from manual ClickUp setup

### Phase 3 — Agent integrations

- [ ] Research agent → creates/updates avatar tasks in ClickUp
- [ ] VSL writer agent → fills script + shot list from avatar brief
- [ ] Media buyer agent → reads/writes performance custom fields on live tasks
- [ ] Webhook or polling: task status change → trigger generation step

**Estimated effort:** per agent, 1–3 days each  
**Dependencies:** Phase 2 sync, agent framework (Cursor Automations or external)

### Phase 4 — Light UI (optional, only if needed)

- [ ] Simple dashboard: pipeline view + "Run step 5" buttons
- [ ] Still reads/writes ClickUp — not a second source of truth

**Estimated effort:** 1–2 weeks  
**Trigger:** ClickUp + Cursor feels too slow for daily ops

---

## Generation cost reference

| Asset | Default tool | Cost (8-shot VSL) |
|-------|--------------|-------------------|
| VO | Edge TTS | $0 |
| Video | fal.ai Wan 480p | ~$1.60 |
| Video (quality) | fal.ai Wan 720p or Kling turbo | ~$3–4 |
| Music | Suno free / Pixabay | $0 |
| Assembly | ffmpeg | $0 |
| Palmier Generate | **Avoid** | ~$30+ |

**Do not use Palmier's generate tools** — editor/MCP import is free; generation is expensive.

---

## Agent integration (future)

All agents share **ClickUp as the message bus**:

```
Research Agent
  → writes avatar brief to ClickUp "Customer Avatars" list

VSL Writer Agent
  → reads avatar task
  → writes script + shot list to VSL Pipeline task (steps 3–4)

Operator / Cursor
  → syncs task → runs generate-vo, generate-video, assemble

Media Buyer Agent
  → reads ad performance
  → updates ClickUp custom fields (CPA, CTR, winning hook)
  → comments "regenerate shot 3 with hook B" on task
```

No agent talks directly to another agent. ClickUp + this repo are the shared interfaces.

---

## ClickUp setup checklist (ops team — manual)

Before Phase 2 dev work, ops should create:

- [ ] Space: **VSL Production**
- [ ] List: **VSL Pipeline** with 7 statuses (Research → Live)
- [ ] List: **Customer Avatars**
- [ ] List: **Spokesperson Assets**
- [ ] List: **Product Assets**
- [ ] Custom fields on VSL Pipeline tasks:
  - `slug` (text) — matches `vsls/<slug>/` folder name
  - `product` (text)
  - `avatar` (relationship → Customer Avatars)
  - `hook` (text)
  - `cta_url` (URL)
  - `vo_script` (long text) — or attachment
  - `shot_list` (long text) — or attachment
  - `generation_cost` (number)
  - `cpa` / `ctr` / `hook_winner` (for live tasks)
- [ ] Document field IDs for dev (ClickUp → Settings → copy field IDs)

---

## Key technical decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Pipeline UI | ClickUp (manual) | Team adds product/spokesperson assets; visible pipeline; API later |
| Custom UI | Deferred | ClickUp covers visibility; avoid duplicate build |
| VO generation | Edge TTS | Free, no API key, good enough quality |
| Video generation | fal.ai Wan 2.2 | Cheapest API (~$0.20/clip); Kling is 2–3× more |
| Assembly | ffmpeg local | Free; Palmier optional for visual editing only |
| Source of truth (generation) | `vsls/<slug>/` JSON | Version-controlled, scriptable, agent-readable |
| Source of truth (workflow) | ClickUp | Human-friendly, attachments, team collaboration |

---

## What dev should build next

1. **Wait for ClickUp structure** — ops finishes manual setup, shares list ID + custom field IDs
2. **Receive 7-step system doc** — map exact Notion fields → ClickUp → JSON schema
3. **Build `pipeline/schema.json`** — field mapping contract
4. **Build `scripts/sync-clickup.py`** — pull task → `vsls/`, push status + output
5. **Add tests** for sync round-trip with a dummy ClickUp task

Do **not** build a custom UI until Phase 2 sync is live and the team has used it for 2+ weeks.

---

## Related docs

- [../README.md](../README.md) — quick start, run commands
- [free-generation.md](./free-generation.md) — cost breakdown, free vs paid generation options
- `vsls/fairy-flame/` — reference implementation
- `.cursor/rules/vsl-workflow.mdc` — Cursor agent behavior

---

## Script-writing framework (added July 2026)

The full VSL script-writing system is now codified in
[`framework/`](../framework/README.md): 5 stages (product intake → avatar
research → angles → scripts → shot list), exact prompts as files, per-product
working data in `products/<slug>/` tracked by `manifest.json`. Run it with the
`/vsl` skill (`.claude/skills/vsl/SKILL.md`). Stage 5 hands off to the
generation pipeline (`vsls/<slug>/`) documented above. This answers question 1
below — the framework upstream of "Research/Hook/Script/Shot List" statuses now
lives in this repo, not just Notion.

---

## Questions for product owner before Phase 2

1. ~~Paste full **7-step system** from Notion — exact step names and fields~~ ✓ codified in `framework/`
2. Confirm ClickUp workspace URL and who creates the lists
3. Expected volume: VSLs per week/month?
4. Team size: solo operator vs multiple people in pipeline?
5. Should avatar research live in ClickUp only, or also sync to `avatars/` in repo?
