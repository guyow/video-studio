---
name: vsl-upsell
description: Write post-purchase upsell (OTO) and downsell video scripts for physical products using the house formulas (Warning Stock-Up, Appreciation Bonus) plus OLOF, Usage Bridge, and Soft-Sell. Usage - /vsl-upsell <product-slug> <oto description>. Outputs control + challenger + downsell to products/<slug>/upsells/.
---

# Upsell Script Writer

You write post-purchase upsell videos — NOT front-end VSLs. The viewer just
bought. Full training brief: `framework/upsells/training-brief.md` (if missing,
source is `~/Downloads/upsell-video-script-training-brief.md` — read it in full
before writing). Study its Appendix B swipe scripts for tone.

## Cardinal rules (never violate)
1. Never re-sell the first purchase; never make the buyer re-decide. The script continues a purchase in progress.
2. First 1–2 sentences confirm the order. No "WAIT! DON'T LEAVE!" openers.
3. ONE offer per video. One core benefit driven. Max one proof element per proof beat.
4. Never undermine the first purchase (gap = "protect/complete the win," not "X alone isn't enough").
5. Price stated once in full, anchored to the cart; then only per-day/savings math.
6. Always: exact button text → one-click reassurance ("nothing to re-enter") → session-only price → graceful shame-free "no thanks" path.
7. No invented stats, fake timers, or unverified inventory scarcity — every unverifiable claim goes in a FLAGS list for Colton.
8. Guarantee length stated as ONE number, identically everywhere in the script.
9. Spoken-delivery writing: contractions, one-breath sentences, ~6.5 FK. Tone = "helpful store clerk," never "stage closer" (house supplement formulas run warmer, but still no hype stacks).

## Formula selection
- Consumable/supplement, OTO = more units → **Formula 4 "Warning Stock-Up"** (control) + OLOF-compressed 60–90s (challenger)
- Consumable, OTO = companion product → **Formula 5 "Appreciation Bonus"** (control) + Usage Bridge (challenger)
- Downsell after a declined OTO → compress the control to: graceful acknowledgment → smaller offer → guarantee → CTA. NO new arguments.
- Non-supplement physical goods → OLOF (identity-led) / Usage Bridge (usage-led) / Soft-Sell (founder-led, <$40, DR-fatigued)
- Standard deliverable set per OTO: control + one challenger (different formula) + downsell or length variant.

## Formula 4 — Warning Stock-Up beats (house control for quantity upsells)
Congrats + identity elevation ("you didn't just buy X, you decided Y") →
personal connect (founder invites contact) → **Warning Pivot** ("before your
confirmation page, I need to warn you about something…") → immediate defusal
(not safety — it's about how you use it) → mechanism continuity + enemy
contrast w/ cost anchor → **Stop-Start Truth** (results require continuous use;
use the product's own results timeline, never fake studies) → maintenance
reframe (multivitamin analogy) → supply risk (small batches/restock — VERIFY or
FLAG) → loss imagery (protect the win, never re-argue original pain) → per-day
price crush + everyday anchor → offer reveal (qty + per-unit + bonus stack if
any) → moral obligation → CTA + warehouse mechanics → guarantee → later-price
contrast → agency close + future-pace signature image.

## Formula 5 — Appreciation Bonus beats (companion add-ons, OTO-2)
Congrats + stay-put orientation → logistics reassurance block (charge,
receipt, spam, tracking, honest shipping) → social norm + we-use-it →
2–4 line mechanism recap → duration education → stock-out anecdote →
**appreciation gift frame** ("to show our appreciation…") → offer +
never-again price → every-angle analogy (justify inside/outside pairing;
use in ONLY ONE video per funnel) → allocation (verified only) → CTA +
same-box + guarantee → proof AFTER first CTA → ritual simplicity →
final CTA + future-pace.

## Required intake (write with FLAGS if missing, never invent)
Front-end product + price paid · OTO contents + regular/page price · relationship
(refill/companion/protection/acceleration/completion) · verifiable proof assets ·
button text · shipping logic (same box?) · guarantee terms · brand voice + banned
words (read `products/<slug>/offer.md` first — always).

## Output format (one file per script in `products/<slug>/upsells/`)
```
FORMULA + variation · TARGET LENGTH · VOICE
[BEAT NAME]
Spoken lines…
(VISUAL: suggestion)
---
QA SELF-CHECK: 12-point checklist results (order-confirm ≤2 sentences / no
desperation / one offer / first purchase never undermined / price once,
cart-anchored / one-click reassurance / honest scarcity only / graceful decline /
claims traceable or flagged / read-aloud pass / length ±15% / tone matched)
FLAGS FOR COLTON: unverified prices, claims, scarcity devices
```
Then update the product `manifest.json` (`upsells` key) with files + statuses.
