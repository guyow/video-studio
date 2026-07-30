# Stage 2 — Find The Big & Easy Avatars

**Goal:** find the lowest-hanging-fruit buyer segments AND the one "monster
market" avatar that represents 10–20x more people than the others, then distill
each into a Kindergarten Simple Pitch a copywriter could write an ad from in
5 minutes.

## Inputs
- `products/<slug>/offer.md` (+ stories) — the session must already be primed
  with the Stage 1 kickoff prompt

## Outputs (in `products/<slug>/avatars/`)
- `research.md` — full output of the lowest-hanging-fruit + monster-market prompts,
  including Reddit source links
- `<avatar-slug>.md` — one Kindergarten Simple Pitch file per avatar
  (top 3 low-hanging fruit + 1 monster avatar = usually 4 files)
- `manifest.json` updated: avatar list + which avatar is chosen for Stage 3

## Process

### 2.1 Lowest-hanging-fruit avatars
Run `lowest-hanging-fruit-prompt.md`. Produces the top 3 easiest-to-convert
segments plus 1 mass-market avatar, each ranked by Eugene Schwartz market
sophistication level and market size.

### 2.2 Monster market
Run `monster-market-prompt.md`. This is live research — actual Reddit thread
searches with engagement counts and real customer language. (Future: research
agent runs this with Apify/reddit-mcp tools.)

### 2.3 Kindergarten Simple Pitch
For each avatar worth pursuing, run `kindergarten-pitch-prompt.md`. Save each
result as its own file in `avatars/`.

**Success test (from the framework):** Could a copywriter write an ad in
5 minutes from this pitch? If no, redo it.

### 2.4 Choose the avatar
Operator (or later, an eval agent) picks which avatar to target first and
records it in `manifest.json` as `chosen_avatar`.

## Done when
- `research.md` has ranked segments with real customer language and sources
- Every pursued avatar has a Kindergarten Simple Pitch that passes the 5-minute test
- `chosen_avatar` is set
