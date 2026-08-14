# Brand Kit Spec Structure (Phase 5)

The kit is a *system*, not a style sheet. It uses a two-layer token architecture so sibling brands can re-theme on the same component foundation without touching component code.

**Before writing:** if the user has an existing design system (design.md, tokens.css, Figma variables), read it and author this spec as a theme layer on that foundation. Match their token naming conventions exactly.

## 05-brand-kit-spec.md structure

```markdown
# Brand Kit Spec — [Brand]

## 1. Primitive Tokens (raw values — brand-agnostic names)

### Color primitives
| Token | Hex | Notes |
|---|---|---|
| --color-[family]-[step] | #... | e.g. --color-indigo-900 |
(Full ramp per family: typically 1 dark base family, 1-2 accent families, 1 neutral ramp. Derive families from the brand world, not from taste — each family must trace to a world element.)

### Type primitives
| Token | Value | Role hint |
|---|---|---|
| --font-display | [family] | brand/world voice |
| --font-body | [family] | user/UI voice |
(Two-family max. Display carries the brand world; body carries usability. Note licensing/availability.)

### Space / radius / shadow character
[Scale + one sentence on the *character*: sharp vs soft, dense vs airy — and why, traced to the world]

## 2. Semantic Tokens (meaning layer — what components consume)
| Token | Maps to primitive | Meaning |
|---|---|---|
| --action-primary | --color-[x] | the ONE conversion color (buy buttons, CTAs) |
| --brand-voice | --color-[x] | headlines, world moments |
| --accent-magic | --color-[x] | delight layer, used sparingly |
| --surface-1/2/3 | ... | elevation hierarchy |
| --text-primary/secondary | ... | |
(This is the re-theming seam: a sibling brand swaps semantic mappings, keeps components.)

## 3. Logo & Mark Rules
- Primary lockup, stacked, icon-only; min sizes; clear space
- Embedded personality device (if Phase 4 chose type 3): usage rules
- Never-do list (stretching, effects, off-palette fills)

## 4. Photography / Illustration Direction
- [3-5 rules with rationale traced to brand world + avatar]
- Image-gen prompt scaffold: "[reusable prompt prefix that produces on-brand imagery]"

## 5. Packaging Hierarchy (three reads)
- **1-second shelf read:** [what the eye gets at 6 feet — mark, color block, format]
- **3-second pickup read:** [front-of-pack: promise, count, flavor, mascot moment]
- **10-second flip read:** [back: RTBs, usage ritual, compliance panel]

## 6. Claims & Compliance Zones
- Approved claim phrasings: [list, per user's stated regulatory constraints]
- Forbidden words/phrasings: [list]
- Where claims may appear vs. where world-voice only: [map]

## 7. Multi-Brand Theming Note
[One paragraph: which primitives are shared across the portfolio, which semantic mappings define THIS brand, and the exact swap procedure for spinning up a sibling theme]
```

## Quality checks specific to this deliverable

- Every color family traces to a brand-world element (no orphan aesthetics)
- Exactly one action color — if everything pops, nothing converts
- Semantic layer complete enough that a component library never references a primitive directly
- In-feed test noted: does --action-primary and the hero palette pop against white AND dark platform chrome?
- Compliance zones written before any copywriter touches the kit
