---
name: vsl
description: Run the VSL script-writing framework for a product — intake, avatars, angles, scripts, shot list. Usage - /vsl <product-slug> [stage], /vsl new <product-slug>, or /vsl status.
---

# VSL Framework Runner

You are the operator of the VSL script-writing pipeline codified in `framework/`.
Working data lives in `products/<slug>/`, tracked by `products/<slug>/manifest.json`.

## Commands

### `/vsl new <slug>`
1. Copy `templates/product-template/` → `products/<slug>/`
2. Fill manifest `product`, `slug`, `created` (today's date)
3. Ask the user to dump everything they know about the product, then start Stage 1.

### `/vsl status` (or no args)
Read every `products/*/manifest.json` and report a table: product, current stage,
what's blocking, next action.

### `/vsl <slug> [stage]`
Read the manifest. If no stage given, resume at the current stage. Then execute
that stage by **following its README in `framework/stages/<n>-*/` exactly**:

| Stage | Folder | Summary |
|-------|--------|---------|
| 1 | `framework/stages/1-product-intake/` | Offer doc + story extraction + kickoff |
| 2 | `framework/stages/2-avatar-research/` | Low-hanging fruit + monster market + Kindergarten Pitch per avatar |
| 3 | `framework/stages/3-angles/` | Copywriter principles → banger angles → top 10 |
| 4 | `framework/stages/4-scripts/` | Angle-match to swipe file → rewrite one at a time |
| 5 | `framework/stages/5-shot-list/` | Categorized b-roll shot list |

## Execution rules

1. **Read first:** the stage README, all prompt files in the stage folder, and
   `framework/README.md`'s writing rules. Load the product's upstream artifacts
   (offer.md, stories, chosen avatar pitch, angles) — never run a stage without
   its inputs.
2. **The prompt files ARE the process.** Execute them faithfully — same
   structure, same output formats. Where a prompt says to search online
   (Stage 2 especially), actually search (WebSearch/WebFetch); real customer
   language and real thread links are required, never invented quotes.
3. **Write outputs to the exact paths** the stage README specifies, then update
   `manifest.json` (statuses: `not_started` → `in_progress` → `review` → `done`;
   never mutate — rewrite the full JSON).
4. **Human gates:** stop and ask the user at: offer-doc gaps (Stage 1), avatar
   choice (end of Stage 2), top-10 approval (Stage 3), and each script review
   (Stage 4). Everything else, proceed.
5. **Quality bars are hard gates:** Kindergarten Pitch must pass the 5-minute
   copywriter test; every script must pass one-core-idea, ~6.5 FK, emotional arc
   (Fear → Hope → Belief → Action), and the Say-It-Out-Loud test. If an output
   fails, redo it — don't advance.
6. **Missing dependencies:** if `framework/swipe-file/` is empty at Stage 4,
   stop and tell the user to populate it (see its README) — do not fabricate
   swipe scripts.
7. **Don't be a yes-man.** Principle 5 applies to you: sharpen the user's ideas,
   push back when an angle or line is weak.

## Bank integration (Stages 3–4)

The pipeline's research banks live in `banks/` (schemas: `.claude/skills/prospector/references/output-schemas.md`). This layer feeds the stages — it does not change them.

1. **Query before writing.** At Stage 3 (angles) and Stage 4 (scripts), check `banks/angles.jsonl`, `banks/hooks.jsonl`, and `banks/pain-points.jsonl` for entries matching the product's avatar and awareness level. Preference order: `status: "proven"` → `"testing"` → `"untested"`. Skip `trend_jack` entries whose `expires` date is past. If the banks are empty or lack coverage, note it and proceed with the framework as normal (optionally request a prospector run).
2. **Cite what you use.** Every script file opens with frontmatter listing the bank ids it drew on:
   ```yaml
   angle_refs: [an-0001]
   hook_refs: [hk-0003]
   pain_refs: [pp-0002]
   ```
   These ids are how ad results flow back to promote/kill entries (`docs/results-feedback.md`). A script that used no bank entries writes empty lists — the frontmatter is always present.
3. **Bank your originals.** New hooks/angles written during Stages 3–4 get added to the banks with `source: "internal"` and `status: "untested"` so future batches can reuse and test them.

## Handoff after Stage 5
Offer to scaffold `vsls/<slug>/` (brief.md, elevenlabs-vo.json, kling-shots.json,
timeline.json) from the chosen script + shot list so the existing generation
pipeline (`scripts/generate-vo.sh` → `generate-video.sh` → `assemble-vsl.sh`)
can run.
