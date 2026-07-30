---
name: bloodhound
description: Always-on viral/trend intelligence for the ad pipeline. Use this skill for anything about what's trending NOW — trending sounds, hashtags, memes, viral video formats, new UGC/editing styles, current events or cultural moments we could ride as ad angles (trend-jacking), a "daily scent report", "what's blowing up", "can we jack this", or "is there a new format we should copy". Complements prospector: prospector proves things by LONGEVITY (30+ days of spend); bloodhound hunts by VELOCITY (share/engagement acceleration in the last 24-72h). Every bloodhound output is perishable and carries an expiry date.
---

# Bloodhound — Trend & Velocity Intelligence

Hunt fast-moving formats, sounds, memes, and cultural moments the pipeline can leverage before they peak. Prospector mines what's *proven*; bloodhound smells what's *moving*.

## Prime directives

1. **Velocity is the truth signal.** Evidence = acceleration in the last 24–72h (view/share/comment growth, sound adoption rate, creator pile-on). A format that's been stable for months is prospector's territory, not yours.
2. **Everything expires.** Every idea, hook, and angle you produce carries an `expires` date (ISO, typically +7 to +14 days from creation). No evergreen claims — that's prospector's job. If an insight looks durable, hand it to prospector for longevity validation instead of banking it yourself.
3. **Every idea lands on a brand + avatar.** A trend without a mapped brand, avatar, and concrete hook sketch is trivia, not intelligence.
4. **Risk note required.** Every leverage idea includes a one-line risk assessment. Hard rule: never jack tragedies, health scares, or anything that conflicts with the brand's compliance constraints — no exceptions, regardless of velocity.

## Modes

### Mode A — Daily Scent Report → `research/trends/scent-[date].md`
Scan, in order:
1. TikTok Creative Center (trends, trending sounds/hashtags, top ads by region/industry)
2. Platform-native viral formats in and adjacent to our categories (TikTok/Instagram/YouTube Shorts)
3. Current events / news with cultural momentum (web search: what is everyone talking about today)

Output: a dated report with **3–5 leverage ideas**. Each idea:
- The trend/moment, with velocity evidence (numbers + timeframe)
- Which of our brands + which avatar it serves
- A concrete hook sketch (first-3-seconds level of specificity)
- Risk note
- `expires` date

### Mode B — Format Watch → `research/trends/formats/fmt-[slug].md`
Detect new NATIVE ad/content formats gaining traction (UGC styles, editing patterns, structural gimmicks — e.g., "fake podcast clip", "text-message storytime", "green-screen reaction stack"). For each: describe the format precisely enough that the **editor agent can replicate the structure** — shot pattern, pacing, text overlay conventions, sound usage, first-3-seconds mechanics — plus 2–3 observed examples with velocity evidence and which of our beats/avatars it could carry.

### Mode C — Event-Jack (on demand, breaking moment)
Given a breaking event/cultural moment, produce rapidly:
1. **Fit** — which brand/avatar could ride this, and the identity-level reason it resonates (not just topical adjacency)
2. **Risk** — brand safety, sensitivity, compliance conflicts. Apply the hard rule (directive 4). If risk fails, say "pass" and stop — a fast no is a valid output.
3. **Angle + hook candidates** — 2–5, banked with short expiry (+7d default; breaking moments decay fastest)

## Bank integration

Write hooks and angles into the SAME banks as prospector — `banks/hooks.jsonl` and `banks/angles.jsonl` — using the schemas in `.claude/skills/prospector/references/output-schemas.md` **verbatim**, with exactly two bloodhound-specific markers:
- `"expires"`: ISO date, +7 to +14 days from `created`
- class `"trend_jack"` (`hook_class` and/or angle `class`)

Do not invent any other fields. Expired entries are swept to `status: "killed"` by prospector Mode 5 — bloodhound never deletes bank entries itself.

## Scraping & sources

Reuse `.claude/skills/prospector/references/apify-playbook.md` — bloodhound favors §2 (TikTok Creative Center + TikTok scraping) and §6 (Instagram), plus news/current-events scanning via web search. CLI helper: `scripts/apify_run.sh` (APIFY_TOKEN from env). If Apify is unavailable, degrade to web search/fetch with the same evidence discipline (velocity numbers + source links, never vibes).

Save raw pulls to `research/raw/` (gitignored) per playbook hygiene.

## Quality bar (check before delivering)

- [ ] Every idea names the brand + avatar it serves
- [ ] Every idea has a concrete hook sketch, not just a trend description
- [ ] Every idea has a risk note; tragedy/health-scare/compliance-conflict jacks rejected
- [ ] Every bank entry has an `expires` date and `trend_jack` class
- [ ] Velocity evidence cited (numbers + timeframe + source), no evergreen claims
- [ ] Format Watch descriptions are precise enough for the editor to replicate structure without seeing the examples
