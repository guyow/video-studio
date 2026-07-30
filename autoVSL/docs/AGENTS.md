# autoVSL — Agent System Map

This project is a **multi-agent ad factory**. Five skills live in `.claude/skills/`,
each invocable by name in any session. This doc explains what each does and how they
hand off, so a "director" session can chain them end to end.

## The five agents

| Agent | Role | Reads | Writes |
|-------|------|-------|--------|
| **prospector** | Research by **longevity** — proven winners (30+ days of spend), competitor teardowns, customer language, hook/angle/b-roll banks | ad libraries, reviews (web/Apify) | `banks/` + `research/` reports |
| **bloodhound** | Research by **velocity** — what's trending NOW (sounds, formats, memes, cultural moments); perishable, dated | live social/trend signals | `banks/` (expiry-dated `trend_jack` entries) + `research/trends/` reports |
| **brand-platform-builder** | Brand identity, voice, positioning, naming, mascot, avatar | product brief | brand platform + brand kit |
| **vsl** | Copywriter — turns research + brand voice into scripts, avatars, angles, shot lists | research, brand voice | `products/<slug>/` scripts |
| **vsl-editor** | Production — turns a script + source/avatar footage into a finished captioned video | script + video | finished MP4s |

## How they chain (the pipeline)

```
  prospector  ──┐   (proven angles, hooks, customer language → banks)
                ├─►  vsl (copywriter)  ──►  vsl-editor  ──►  finished ad
  bloodhound  ──┘        ▲                   (this session's engine)
  (trending now)         │
  brand-platform-builder ┘   (brand voice + positioning feed the writer's tone)
```

1. **Research** — `prospector` (proven, durable) and `bloodhound` (fresh, perishable)
   feed angles, hooks, and real customer language into the banks. Run these first
   and to refresh.
2. **Brand** — `brand-platform-builder` sets voice/positioning once per product; the
   writer pulls tone from it.
3. **Write** — `vsl` runs its staged framework (intake → avatar → angles → scripts →
   shot list) using the research + brand voice, and drops finished scripts in
   `products/<slug>/`.
4. **Produce** — `vsl-editor` takes a finished script plus footage (a source
   testimonial to repurpose, or an avatar talking-head) and outputs a finished,
   captioned MP4 (voice-clone → new VO → lip-sync → captions).

## Shared data contract (how handoffs actually work)

Agents don't call each other directly — they hand off through files:
- `banks/` — the shared JSONL banks (hooks, angles, pain-points, b-roll) prospector and bloodhound write and the writer/editor query; schemas in `.claude/skills/prospector/references/output-schemas.md`
- `research/` — prospector/bloodhound reports, teardowns, and raw scrape dumps (`research/raw/`, gitignored)
- `products/<slug>/` — the writer's per-product working data + manifest
- `framework/`, `templates/` — the writer's playbook and starting points
- `scripts/`, `output/` — the editor's engine and render artifacts
- Finished deliverables land in the user's Desktop folders
  (`liitt testimonial Ready/`, `litt VSL's/`)

## Running them together (director session)

In one session you can invoke each by name (e.g. "/prospector …", "/vsl …") or just
describe the goal and let the router pick. A typical full run:
1. `prospector` — refresh angle/hook banks for the product.
2. `bloodhound` — check for a trend to ride (optional, time-sensitive).
3. `brand-platform-builder` — if the brand voice isn't set yet.
4. `vsl` — write the script(s) from the research + brand voice.
5. `vsl-editor` — render the winning script onto an avatar/testimonial, with captions.

Each agent's own `SKILL.md` is the source of truth for its commands and rules.
The editor's hard-won production rules (always stop for the text edit, per-video
triage, timing/caption mechanics, fal gotchas) live in
`.claude/skills/vsl-editor/SKILL.md`.
