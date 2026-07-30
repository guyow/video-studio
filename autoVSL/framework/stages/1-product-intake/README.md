# Stage 1 — Product Intake

**Goal:** bring the AI fully up to speed on the offer before any selling ideas are generated.

## Inputs
- Everything known about the product/offer (raw docs, pages, notes)
- Founder or customer stories (voice-recorded, then transcribed)

## Outputs (in `products/<slug>/`)
- `offer.md` — the Offer Information Document, built from `offer-doc-template.md`
- `stories/<name>.md` — one file per extracted story (raw transcript + structured summary)
- `manifest.json` — created/updated with `"stage": 1` progress

## Process

### 1a. Build the Offer Information Document
Fill out `offer-doc-template.md` completely. This is the single document that gets
pasted into every downstream prompt — it must be thorough. If information is
missing, list the gaps at the top of `offer.md` under `## Open gaps` and ask the
operator to fill them.

### 1b. Run the Kickoff Prompt
Open a fresh copywriting session and run `kickoff-prompt.md` with the completed
offer doc pasted in.

### 1c. Story Extraction
Use `story-extraction.md`. First have the AI **rewrite the questions specifically
for this product and this person's situation**. Then the person voice-records
answers (talking like they're telling a friend — no filter, no script). Save the
transcript to `products/<slug>/stories/`, then paste it into the session so the
AI has the full story.

### 1d. Anything else?
Ask: "Have you updated your offer? Changed anything? Anything else the AI needs
to know?" Capture the answer in `offer.md` (changelog section at bottom).

## Done when
- `offer.md` exists with no critical gaps
- At least one story captured (or explicitly marked "no story available")
- Operator confirms the AI's play-back summary of the offer is accurate
