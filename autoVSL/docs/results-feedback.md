# Results Feedback Loop — ad results → bank statuses

How campaign performance flows back into the banks so winners compound and losers die. Manual process for now (matches current operating mode); automate later if volume demands.

## The chain

```
script frontmatter          ad platform              manifest.json               banks/*.jsonl
angle_refs/hook_refs/  ──►  ad id + metrics  ──►  performance.results[]  ──►  status updates
pain_refs (bank ids)        (CPA, CTR, spend)      (per product)              (prospector Mode 4/5)
```

1. **Scripts cite ids** — every script's frontmatter lists the bank entries it used (`angle_refs`, `hook_refs`, `pain_refs`). This is enforced by the vsl skill's bank-integration rules. No ids = unmeasurable = not finalized.
2. **Results land in the manifest** — when an ad has enough spend to judge, append an entry to the product's `manifest.json` → `performance.results[]` (rewrite the full JSON, never mutate in place):
   ```json
   {
     "ad_id": "meta-120211234567890",
     "vsl_slug": "fairy-flame-photo-before-kids-e2h3",
     "script": "scripts/01-photo-before-kids.md",
     "angle_refs": ["an-0003"],
     "hook_refs": ["hk-0001"],
     "metrics": {"spend": 480.22, "cpa": 41.10, "ctr": 0.021, "days_running": 9},
     "verdict": "scale",
     "date": "2026-07-20"
   }
   ```
   `verdict`: scale | iterate | kill (the human/media-buyer call, with the numbers that justify it).
3. **Prospector updates statuses** — run prospector Mode 4 with the new results. It applies transitions to the cited bank entries and records evidence in `our_results`:
   - `untested → testing` — the entry shipped in a live ad
   - `testing → proven` — beat account benchmarks with meaningful spend (human confirms)
   - `testing → killed` — clearly lost, or the concept fatigued
   Only prospector Mode 4/5 (or explicit human edit) changes `status` — nothing else touches it.
4. **The loop closes** — the scriptwriter's next batch queries `proven` entries first, prospector Mode 4 hunts fresh variants of what's winning, and Mode 5 keeps the banks clean (dedupe, expiry sweep of `trend_jack` entries).

## Fatigue signal (from prospector SKILL.md Mode 4)

Frequency up + CTR down on a winner = fatigue → don't kill the angle, pull the next untested hook from the same angle's `hooks` list, or request a Mode 3 hunt for fresh variants of the winning pattern.

## Weekly rhythm (suggested)

- After each batch gets ~7 days of spend: add `performance.results[]` entries + run Mode 4.
- Weekly: Mode 5 bank maintenance (dedupe, expiry sweep, health stats by status/avatar/awareness).
