# Stage 4 — Start Creating Winning Scripts

**Goal:** match each of the top 10 angles to the best proven script formula from
the swipe file, then rewrite that winning script for our product/avatar — one at
a time.

## Inputs
- `products/<slug>/angles/<avatar>-angles.md` (top 10 angles)
- `framework/swipe-file/` (proven short-form ad scripts — the Short Form Ads
  Swipe File)
- Copywriter principles (`../3-angles/copywriter-principles-prompt.md`) — must
  already be installed in the session

## Outputs (in `products/<slug>/scripts/`)
- One file per script: `<nn>-<angle-slug>.md` containing:
  - The angle headline + authority figure
  - Which swipe-file script/formula it was matched to (and why)
  - The full rewritten script
  - The core idea being dialed home
  - Emotional arc map (Fear → Hope → Belief → Action beats)
- `manifest.json` updated with script list + status per script

## Process

### 4.1 Angle Matching
Run `angle-matching-prompt.md` with the full swipe file pasted in. Output: for
each of the 10 ad ideas, the best proven script formula to rewrite it from,
numbered.

### 4.2 Rewrite, one at a time
Run `rewrite-prompt.md`. Rewrite each idea against its matched winning script,
one at a time. Keep each script a similar length to the winning flow. Review
each before moving to the next — sharpen, don't rubber-stamp.

## Done when
- Every approved angle has a finished script file
- Each script passes: one core idea, avatar language, ~6.5 FK score, emotional
  arc complete, Say-It-Out-Loud test
