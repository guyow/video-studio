# Output Schemas — Banks, Matrix, Reports

All banks live as JSONL (one entry per line) with a markdown index, under `banks/`. JSONL so the scriptwriter/editor agents can grep/filter programmatically; markdown index for humans. Every entry has a stable `id` for cross-referencing.

## §1 — Hook Bank (`banks/hooks.jsonl`)

```json
{
  "id": "hk-0042",
  "text_verbatim": "If your 'calm' gummies just make you sleepy, watch this",
  "hook_class": "negative",            
  "visual": "hand tossing competitor bottle in trash",
  "text_overlay": "NOT another melatonin trap",
  "awareness_level": 3,
  "sophistication_stage": 4,
  "avatar": "burnt-out-optimizer",
  "angle_ref": "an-0017",
  "source": "meta_ad_library",
  "source_url": "https://...",
  "evidence": "longevity_74d_9variants",
  "status": "untested",                 
  "our_results": null,
  "created": "2026-07-07",
  "tags": ["sleep", "anti-melatonin", "ugc-style"]
}
```
- `hook_class`: callout | pattern_interrupt | bold_claim | question | story_open | negative | demonstration | testimonial_open | us_vs_them | trend_jack
- `status` lifecycle: untested → testing → proven | killed. Only prospector Mode 4/5 or explicit human input changes status. Proven entries sort first in scriptwriter queries.
- Original hooks we write also get banked (source: "internal") — same schema.
- **Trend entries (bloodhound):** hook entries written by bloodhound use `hook_class: "trend_jack"` and add one extra field — `"expires"` (ISO date, typically +7 to +14 days after `created`). No other extra fields are permitted. Mode 5 sweeps entries past `expires` to `status: "killed"`; scriptwriter queries skip unexpired-check-failing trend entries.

## §2 — Angle Bank (`banks/angles.jsonl`)

```json
{
  "id": "an-0017",
  "name": "The Melatonin Hangover Enemy",
  "class": "enemy",
  "argument": "Calm gummies fail because melatonin sedates instead of regulating; [mechanism] works with your cortisol curve instead of knocking you out.",
  "awareness_entry": 3,
  "sophistication_stage": 4,
  "avatar": "burnt-out-optimizer",
  "pain_ref": "pp-0006",
  "mechanism": "[named mechanism]",
  "compliance_notes": "no disease claims; 'supports' language only",
  "evidence": ["competitor X scaling 3 concepts on this 60d+", "VoC cluster: 31 quotes"],
  "hooks": ["hk-0042", "hk-0051"],
  "status": "testing",
  "our_results": "CTR 2.1% vs acct avg 1.4%, batch 2026-06-A",
  "tags": ["sleep", "mechanism-flip"]
}
```
- **Trend entries (bloodhound):** angle entries written by bloodhound use `class: "trend_jack"` and add the same single extra field `"expires"` (ISO date, +7 to +14 days). Swept to `killed` by Mode 5 once expired.

## §3 — Winner Teardown (report, `research/[brand]/teardowns/td-[slug].md`)

```markdown
# Teardown: [Advertiser] — "[hook line]"
- Source: [ad library URL] | First seen: [date] | Longevity: [n] days | Variants: [n]
- Diagnosis: awareness entry [n], sophistication stage [n], angle class [x], avatar [guess]

| Beat (Kell map) | Time | Transcript/description | Technique |
|---|---|---|---|
| Hook | 0-3s | | visual: / audio: / text: |
| Lead / Big Promise | | | |
| Mechanism / Story | | | |
| Proof stack | | | |
| Offer | | | |
| CTA | | | |

## Why it's winning
[2-4 sentences — the transplantable pattern, not the surface]

## What we take (structure) / what we never take (substance)
- Take: [...]
- Never: [their footage, their copy verbatim, their testimonials]

→ Bank entries created: [ids]
```

## §4 — Pain-Point Matrix (`banks/pain-points.jsonl`)

```json
{
  "id": "pp-0006",
  "name": "wired-but-tired 3pm crash",
  "avatar": "burnt-out-optimizer",
  "peak_moment": "3pm at desk, post-2nd-coffee",
  "failed_solutions": ["more coffee", "melatonin at night", "energy drinks"],
  "cost": {"functional": "afternoon output collapses", "emotional": "self-blame, 'why can't I cope'", "social": "snappy with kids at dinner"},
  "identity_dimension": "wants to be the energized parent/operator, fears becoming the burnt-out cliché",
  "dream_state": "even energy till 9pm, present at dinner",
  "voc_quotes": [{"text": "...", "url": "...", "type": "pain", "intensity": 3}],
  "angles": ["an-0017"],
  "awareness_distribution": "mostly level 2-3",
  "tags": ["energy", "sleep-adjacent"]
}
```

## §5 — B-Roll Library (`banks/broll.jsonl`)

Tagged so the editor agent auto-pulls by beat, and the scriptwriter can plan edits against real inventory.

```json
{
  "id": "br-0113",
  "file": "assets/broll/kitchen-morning-pour-01.mp4",
  "duration_s": 14,
  "usable_segments": [[2.0, 6.5], [9.0, 13.0]],
  "shot": "slow push-in, gummy pour into palm, warm morning kitchen light",
  "emotional_beat": "ritual/hope",
  "product_moment": "consumption",
  "beat_tags": ["mechanism_reveal", "cta_underlay"],
  "avatar_fit": ["burnt-out-optimizer"],
  "brand": "fairy-flame",
  "rights": "owned",
  "quality": "A",
  "status": "available"
}
```
- `emotional_beat` vocabulary (shared with scriptwriter's edit plans): pain_mirror | agitation | discovery | ritual/hope | transformation | proof | belonging | urgency
- `product_moment`: unboxing | consumption | texture | packaging_hero | lifestyle | before_state | after_state | none
- **Wishlist entries** use the same schema with `"status": "wishlist"` and no file — created when a script needs a beat the library lacks. Wishlist resolution order: (1) prospector Mode 3 hunts reference examples of the shot working in winners, (2) generation agent creates it, (3) shoot list for real capture.
- `rights`: owned | licensed | reference_only. **reference_only clips never enter a published edit** — they exist to brief generation/shooting.

## §6 — Deep-Dive Report skeleton (`research/[brand]/deep-dive-[date].md`)

1. Executive read (5 bullets max: stage/awareness diagnosis, biggest VoC cluster, top white-space angle, top 3 winners found, recommended first tests)
2. Awareness & sophistication diagnosis with evidence
3. VoC synthesis: pain clusters ranked (link to pp- entries), quote highlights
4. Competitor ad census table (advertiser | active ads | longest runner | dominant angle class | mechanism)
5. Winner teardown links (td- files)
6. Angle map: ranked angles (an- entries) with white-space callouts
7. Test queue: prioritized batch recommendations for the scriptwriter (angle + hook + awareness entry + avatar per batch)
8. Bank delta: entries created this run

## §7 — Cross-referencing discipline

pain-point → angles → hooks → (scripts cite hook/angle ids) → results update statuses. The scriptwriter MUST cite bank ids in its script frontmatter; the Mode 4 loop closes when ad results flow back to those ids. This is what makes the banks compound instead of rot.
