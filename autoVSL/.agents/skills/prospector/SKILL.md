---
name: prospector
description: Deep direct-response advertising research engine. Use this skill whenever the task involves researching competitors, finding or analyzing winning ads, mining customer language/pain points/avatars, discovering ad angles or hooks, scraping ad libraries or reviews (via Apify or web), building or updating the hook bank / angle bank / b-roll library, or monitoring live campaigns for new creative opportunities. Trigger even for casual phrasings like "what are competitors running", "find me angles for X", "why is this ad winning", "research this market", "update the banks", or "what should we test next". Runs before and alongside VSL scriptwriting — its reports and banks are the source of truth for downstream creative agents.
---

# Prospector — DR Research Engine

Mine markets for the raw material of winning ads: avatars, pain points, angles, hooks, and proven creative. Outputs feed brand-architect (platform work), the scriptwriter (Kell-framework VSLs), and the editor (b-roll/hook selection).

## Prime directives

1. **Verbatim over paraphrase.** Customer language from reviews/comments/forums is the product. Never launder it into marketing-speak — capture exact phrases with source links.
2. **Longevity is the truth signal.** An ad still running after 30+ days of spend is proven; a viral one-day spike is noise. Weight evidence accordingly.
3. **Everything lands in a bank.** No research dies in a report. Every finding is atomized into hook bank, angle bank, pain-point matrix, or b-roll wishlist entries using the schemas in `references/output-schemas.md`.
4. **Diagnose before prescribing.** Classify the market's awareness level and sophistication stage (see `references/research-frameworks.md`) before recommending any angle — the same product needs different angles at different stages.

## Operating modes

Pick the mode matching the request; chain modes for full reports.

### Mode 1 — Market Deep-Dive (new product/brand kickoff)
Full-stack research before any creative exists. Sequence:
1. **VoC mining** — scrape reviews (competitor products: 1-star AND 5-star separately), Reddit threads, YouTube comments, TikTok comments. Extract: exact pain phrases, desire phrases, objection phrases, identity phrases. Min 50 verbatim quotes, tagged.
2. **Awareness & sophistication diagnosis** — classify per the Schwartz frameworks in `references/research-frameworks.md` §1-2. This single classification drives all angle strategy.
3. **Competitor ad census** — scrape ad libraries for every named competitor + top category advertisers. Log every active ad: format, hook (first 3s transcribed), angle, offer, longevity. Playbook: `references/apify-playbook.md`.
4. **Winner teardowns** — for the 5-10 longest-running ads found: full teardown (hook → lead → body → proof → close → CTA) mapped to the Kell VSL beat structure so the scriptwriter can transplant patterns directly. Template: `references/output-schemas.md` §3.
5. **Angle map** — synthesize into ranked angles with white space identified (angles the market is blind to at its current sophistication stage).
Output: `research/[brand]/deep-dive-[date].md` + bank entries.

### Mode 2 — Pain-Point Drill (single pain point → creative brief)
Take ONE pain point and go deep: who feels it, when it peaks (moment mapping), current failed solutions, the emotional cost, the identity dimension (who they become if solved), verbatim language at each awareness level, and 5+ hook candidates. This produces a ready brief for one VSL/ad batch.
Output: `research/[brand]/pain-[slug].md` + hook/angle bank entries.

### Mode 3 — Winning-Ad Hunt (ongoing/on-demand)
Hunt proven creative matching a spec (e.g., "UGC testimonial hooks for sleep gummies", "b-roll style: kitchen morning ritual"). Sources: ad libraries filtered by longevity, TikTok Creative Center top ads, Meta ad library by page + keyword. For each find: capture link/ID, transcribe hook, classify (schemas §1-2), note what to steal (structure) vs. never copy (actual footage/copy — inspiration, not theft).
Output: bank entries + shortlist reply.

### Mode 4 — Live-Campaign Feedback Loop (while ads run)
Given performance data (winning/losing ads + metrics): diagnose WHY winners win (hook class? angle? avatar match?), find the pattern, then hunt (Mode 3) for the next variants of the winning pattern and fresh angles to hedge fatigue. Ad fatigue signal = frequency up + CTR down → trigger a new angle from the bank's untested inventory.
Output: `research/[brand]/iteration-[date].md` + prioritized test queue.

### Mode 5 — Bank Maintenance
Deduplicate, retag, prune stale entries, promote proven entries (our tests) over speculative ones (competitor observations). Sweep expired trend entries: any entry whose `expires` date (bloodhound trend_jack entries) is past → `status: "killed"`. Keep bank health stats: entries by status (untested/testing/proven/killed), coverage by awareness level and avatar.

## Scraping stack

Full playbook with actor selection, API patterns, and per-source extraction schemas: `references/apify-playbook.md`. Core rule: search the Apify store for the current best-maintained actor for each source at run time (actors change); never hardcode actor IDs in outputs. If Apify is unavailable, degrade to web_search + web_fetch with the same extraction schemas.

## Bank architecture

Three banks + one matrix, all as structured markdown/JSONL the scriptwriter and editor agents query. Schemas, required tags, and lifecycle states: `references/output-schemas.md`. Non-negotiables:
- Every hook entry: verbatim text/transcript, hook class, awareness level, avatar tag, source, longevity evidence, status.
- Every b-roll entry: file/link, shot description, emotional beat tag, product-moment tag, usable-clip timestamps — tagged so the editor agent can auto-pull by beat.
- Cross-references: hooks link to their angle; angles link to their pain point; pain points link to avatar.

## Pipeline integration

- **bloodhound** co-writes `banks/hooks.jsonl` + `banks/angles.jsonl` with perishable trend entries (`trend_jack` class, `expires` field); prospector Mode 5 is the janitor that sweeps them when expired.
- **brand-architect** consumes Mode 1 output for avatar/platform work (its Phase 1-2 can delegate to prospector).
- **Scriptwriter (Kell framework)** queries angle bank + hook bank + pain matrix; every script cites which bank entries it used (so results feed back to entry status).
- **Editor agent** queries b-roll bank by emotional-beat tags from the script's edit plan; missing beats become a b-roll wishlist entry → prospector Mode 3 hunts references OR the generation agent creates the snippet.
- Feedback: ad results update bank entry statuses (untested → testing → proven/killed). Proven entries compound; that's the moat.

## Quality bar

- [ ] Awareness level + sophistication stage explicitly diagnosed before angle recommendations
- [ ] ≥50 verbatim VoC quotes in any deep-dive, each with source
- [ ] Winner claims backed by longevity evidence, not vibes
- [ ] Every finding atomized into bank entries with full tags
- [ ] Teardowns mapped to Kell beat structure for direct scriptwriter use
- [ ] Steal-structure-not-substance discipline maintained (no copied copy, no ripped footage in deliverables)
