# Stage 3 — Pick Your Avatar & Generate Ad Ideas

**Goal:** from the chosen avatar's Kindergarten Simple Pitch, generate a big
list of proven-pattern angles, then cut to the 10 best with assigned
spokesperson/authority figures.

## Inputs
- `products/<slug>/avatars/<chosen-avatar>.md` (Kindergarten Simple Pitch)
- `framework/angle-library/winning-angles.md` (proven angle patterns)

## Outputs
- `products/<slug>/angles/<avatar-slug>-angles.md` — all generated angles +
  the final top 10 with authority figures
- `manifest.json` updated with the top-10 angle list

## Process (run these 3 prompts in order, in a fresh session)

1. **Prime the session:** paste the chosen avatar's Kindergarten Simple Pitch.
2. **`copywriter-principles-prompt.md`** — installs the 10 copywriting
   principles for the session. Every later stage reuses these same principles.
3. **`banger-angle-generator-prompt.md`** — paste the winning-angles library;
   AI studies the psychology and writes headline ideas for OUR avatar, each with
   the authority figure the avatar would most trust.
4. **`pick-10-best-prompt.md`** — cut to the 10 best, grouped by authority
   figure (3-6 figures max, gender + age listed).

## Angle rules (hard constraints)
- Angles must be PROBLEMS our product actually solves.
- Only use angles the avatar DEFINITELY already knows about — nothing the
  audience is clueless about or has never heard of.

## Done when
- Top 10 angles exist, each with: headline, authority figure (2-3 word title,
  age, gender), and 3 bullet points on what it's about
- Operator approves the 10 (or swaps in alternates)
