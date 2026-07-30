# Stage 5 — Shot List

**Goal:** turn finished scripts into a complete, categorized b-roll shot list so
the shoot (or AI generation) knows exactly what to capture.

## Inputs
- All finished scripts in `products/<slug>/scripts/`

## Outputs
- `products/<slug>/shot-lists/<script-or-batch>-shots.md` — categorized shot
  list (props, wardrobe, hooks, problem, spokesperson, story, benefit,
  community, product shots)

## Process
Run `shot-list-prompt.md` with all scripts in the session. Use
`shot-list-example.md` as the reference structure — the output should follow the
same categories and level of detail.

## Handoff to generation
The shot list bridges to the existing pipeline two ways:
- **Real footage:** shoot list for the spokesperson/b-roll shoot; footage gets
  tagged into the b-roll database
- **AI generation:** shots become `kling-shots.json` prompts in `vsls/<slug>/`,
  then `generate-video.sh` → `assemble-vsl.sh`

## Done when
- Every script's key beats have at least one matching shot
- Shot list is categorized like the example (hooks / problem / spokesperson /
  story / benefits / product) with props + wardrobe up top
