# VSL Script-Writing Framework

This directory is the **playbook**: every prompt, template, and checklist in the
VSL script-writing process, stored as files so a human, Claude Code, or a future
autonomous agent can run the exact same process.

Working data lives in `products/<slug>/` (one folder per product/offer).
Finished scripts hand off to the existing generation pipeline in `vsls/<slug>/`.

## The pipeline

| Stage | Name | Input | Output (in `products/<slug>/`) |
|-------|------|-------|-------------------------------|
| 1 | **Product Intake** | Everything known about the offer + founder/customer stories | `offer.md`, `stories/` |
| 2 | **Avatar Research** | `offer.md` + stories | `avatars/research.md`, one Kindergarten Pitch per avatar in `avatars/` |
| 3 | **Angle Generation** | Chosen avatar's Kindergarten Pitch | `angles/<avatar>-angles.md` (banger angles → top 10) |
| 4 | **Script Writing** | Top 10 angles + swipe file | `scripts/` (one file per rewritten script) |
| 5 | **Shot List** | Finished scripts | `shot-lists/` |
| → | **Generation** | Script + shot list | `vsls/<slug>/` (existing VO/video/assembly pipeline) |

Each stage folder in `stages/` contains:
- `README.md` — what the stage does, inputs, outputs, done-criteria
- The exact prompts to run, verbatim, with `{{PLACEHOLDERS}}` for product-specific values

## State tracking

`products/<slug>/manifest.json` records which stage each campaign is at, which
avatar was chosen, and which angles/scripts exist. Skills and agents read this
to know where to resume.

## How to run it

- **Today (operator-driven):** run `/vsl <stage>` in Claude Code (see
  `.claude/skills/vsl/SKILL.md`), or open the stage README and follow it manually.
- **Later (autonomous):** each stage is a self-contained contract
  (prompt files in → structured files out), so an agent can execute a stage
  end-to-end and a reviewer (human or eval) gates promotion to the next stage.

## Path to autonomy

1. **Manual** — operator runs each stage via `/vsl`, reviews every output. (now)
2. **Semi-auto** — stages 2–5 run unattended per product; human reviews at two
   gates only: avatar choice (end of stage 2) and final scripts (end of stage 4).
3. **Autonomous** — research agent feeds stage 1–2, script agent runs 3–5,
   media buyer feeds performance back into `manifest.json` to kill/iterate angles.

## Non-negotiable writing rules (apply at every stage)

These come from the copywriter principles in stage 3 and apply to ALL copy output:
- FK reading level ~6.5. Mass America. Short punchy sentences.
- Extreme specificity — real numbers, real details, believable stories.
- One core idea per campaign, dialed home relentlessly.
- Avatar's own language (pulled from research), never invented jargon.
- Emotional arc: Fear → Hope → Belief → Action.
- "Say It Out Loud" test: if it sounds weird spoken by a 55-year-old man in Ohio, rewrite it.
