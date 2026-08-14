---
name: brand-platform-builder
description: Build a complete brand platform from a product brief using the Raindrop-agency-style method (the agency behind Grüns, Dr. Squatch, Native, Lume). Use this skill whenever the user asks for brand identity work, a brand kit, brand strategy, naming/positioning, mascot development, avatar research, brand voice, creative territories, or wants to "brand" a new or existing product — even if they only say things like "help me build the brand", "I need a brand kit", "come up with our identity", or "do the brand work like Grüns/Dr. Squatch". Outputs five deliverables — avatar research, brand platform, mascot concepts, brand kit spec (design-token compatible), and creative territories — as structured markdown files.
---

# Brand Platform Builder (Raindrop Method)

Build a DTC-grade brand platform the way Raindrop built Grüns from Seed stage: consumer-insight-driven, performance-first, and designed as a re-themeable system rather than a one-off logo.

## Core philosophy (internalize before starting)

**People don't buy your why. They buy THEIR why** — who they are and who they're hoping to become. Every deliverable in this skill must answer: *what is the customer saying about themselves when they engage with this brand?* If a deliverable can't answer that, it isn't done.

Second principle: **spark a movement, not a moment.** Creative that entertains gets watched; creative that lets the customer perform their identity gets shared and repurchased.

Third principle: **brand and performance are one discipline.** Every identity decision (name, mascot, palette, voice) must be justified by how it performs in a paid ad, on a PDP, and on a retail shelf — in that order.

Read `references/raindrop-method.md` before Phase 1 for the full framework detail, case evidence, and anti-patterns.

## Required inputs

Before starting, collect from the user (ask only for what's missing):
1. Product brief — what it is, format, price point, key ingredients/features
2. Category + 3-5 named competitors
3. Any existing brand assets or constraints (name locked? palette locked? design tokens?)
4. Primary channel (DTC paid, organic social, retail, marketplace)
5. Compliance constraints (e.g., supplement/hemp claims rules)

## The six phases

Work through phases in order. Each phase produces a file in `outputs/` (or the user's specified directory). Do not skip Phase 1 — everything downstream inherits from the avatar work.

### Phase 1 — Avatar & "Their Why" research → `01-avatar-research.md`

- Build 1-3 avatars (primary + secondary). For each: demographics sketch, day-in-the-life, current alternatives, frustrations with those alternatives.
- The core section: **Identity ladder** — who they are today → who they're hoping to become → what buying this product *says about them* to themselves and others.
- Emotional drivers ranked (aspiration, relief, belonging, status, play).
- Objection map: the top 5 reasons they won't buy, each paired with the belief that dissolves it.
- If web search is available, validate with real voice-of-customer: competitor reviews, Reddit/forum language, exact phrases customers use. Quote their words, not marketing words.
- Template in `references/deliverable-templates.md` §1.

### Phase 2 — Competitive & category analysis → `02-category-analysis.md`

- Map named competitors on two axes the user cares about (e.g., clinical↔playful, premium↔value). Identify the white space.
- Catalog category codes: what every brand in the category does visually/verbally (so the brand can keep the codes that signal legitimacy and break the ones that create sameness).
- Identify the **category entry points**: the moments/needs that trigger purchase in this category.
- Template in `references/deliverable-templates.md` §2.

### Phase 3 — Brand platform → `03-brand-platform.md`

The centerpiece. Contains:
- **Positioning statement** (for [avatar] who [their why], [brand] is the [category frame] that [unique promise], because [reason to believe]).
- **Brand world**: a named, enterable place/state — the "Enter the Grüns Zone" move. The brand world is not a tagline; it's a spatial/experiential metaphor every asset can live inside. Propose 2-3 candidates, recommend one, explain why in terms of the avatar's identity ladder.
- **Value props** ranked and phrased in customer language (from Phase 1 quotes).
- **Voice & tone**: 5 voice attributes, each with a do/don't example sentence. Include a "premium guardrail" — the line playfulness must never cross (Raindrop's Barry rule: playful edge WITHOUT losing premium feel).
- **Naming** (only if name isn't locked): 5-10 candidates scored against memorability, ownability (trademark/domain sanity check via web search if available), and the identity ladder.
- Template in `references/deliverable-templates.md` §3.

### Phase 4 — Mascot & personality asset → `04-mascot-concepts.md`

- Propose 2-3 mascot/personality-device directions. A mascot can be a character (Barry), a personified product, or a recurring device baked into the wordmark (Grüns' ü-as-smiley).
- For each: name, personality in 3 adjectives, visual description detailed enough for a designer/AI-image brief, relationship to the avatar (mirror, guide, or comic foil), and 3 example usages (ad hook, packaging moment, email sign-off).
- Include usage rules: where the mascot appears, where it never appears, premium guardrail.
- Recommend one direction and write the designer handoff brief.
- Template in `references/deliverable-templates.md` §4.

### Phase 5 — Brand kit spec → `05-brand-kit-spec.md`

- Translate the platform into a token-ready spec: primitive tokens (raw palette with hex, type families, spacing/radius character) and semantic tokens (action color, brand-voice color, surface hierarchy) so the kit drops into a tokens.css / Tailwind / Figma-variables workflow.
- If the user has an existing design system (e.g., a design.md or tokens.css), read it first and structure this spec as a theme layer on that foundation rather than a parallel system.
- Include: logo usage rules, photography/illustration direction, packaging hierarchy (front-of-pack in 3 reads: 1-second shelf read, 3-second pickup read, 10-second flip read), and compliance-safe claim phrasing zones.
- Full spec structure in `references/brand-kit-spec.md`.

### Phase 6 — Creative territories & channel sequencing → `06-creative-territories.md`

- 3-5 creative territories: each a repeatable ad *format* (not a one-off ad) expressing the brand world. For each: the hook mechanic, why the avatar shares it, a 15-second script sketch, and how it degrades gracefully from video → static → email.
- **Channel sequencing plan** (the Grüns arc): DTC paid + email to prove the machine → retail-readiness checklist (what packaging/awareness must exist before shelf) → broadcast/mass creative only after retail. Note audience-expansion moves (Grüns added Spanish-language creative with retail expansion; identify the equivalent for this brand).
- **Sub-universe hooks**: if product lines will expand (kids line, pro line, seasonal), sketch how the brand world extends without rebuilding — new avatar, same platform.
- Template in `references/deliverable-templates.md` §5.

## Quality bar (check before delivering)

- [ ] Every deliverable traces back to the avatar's identity ladder — no orphan aesthetics
- [ ] Brand world is enterable (a place/state), not just a slogan
- [ ] Mascot has a premium guardrail written down
- [ ] Brand kit spec is token-structured and re-themeable for sibling brands
- [ ] Creative territories are formats, not single ads
- [ ] All claims phrasing respects stated compliance constraints
- [ ] Customer language (real quotes) appears in the platform, not just marketing language

## Reference files

- `references/raindrop-method.md` — full framework: their-why philosophy, Grüns/Dr. Squatch case evidence, mascot theory, sequencing logic, anti-patterns. **Read before Phase 1.**
- `references/deliverable-templates.md` — copy-paste markdown templates for deliverables 1-4 and 6.
- `references/brand-kit-spec.md` — the token-layer structure for deliverable 5, with primitive/semantic split and multi-brand theming pattern.
