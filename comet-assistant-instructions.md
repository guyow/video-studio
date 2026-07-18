# Comet Assistant Instructions — Goldy → liitt Rebrand (Testimonial Clip)

Paste this into your Comet browser assistant. It has live access to your logged-in ElevenLabs and Enhancor accounts. Script is locked — no more guessing at wording.

**Files ready in your `Video AI editing` folder:**
- `WhatsApp Video 2026-07-04 at 14.38.50.mp4` — original clip
- `speaker0_voice-sample.mp3` (14.4s) — clean isolated voice sample of Speaker 0 (says the "Goldies" lines and the "pre-rolls/flower" line)
- `speaker1_voice-sample.mp3` (10.1s) — clean isolated voice sample of Speaker 1 (says the "Banana Punch" line)
- `goldy-to-liitt_voice-sample.mp3` — ignore/delete, superseded by the two per-speaker files above (it mixed both voices together, no good for cloning)

## Step 1 — Clone both voices

1. Go to **Voices → Add Voice → Instant Voice Cloning** in ElevenLabs.
2. Upload `speaker0_voice-sample.mp3` → name it `liitt-testimonial-male-01`.
3. Repeat with `speaker1_voice-sample.mp3` → name it `liitt-testimonial-female-01`.

## Step 2 — Generate exactly these three replacement lines (nothing else)

Use **Text to Speech**, matching each line to its cloned voice. Match delivery to a casual, warm, conversational tone — not an ad-read.

| # | Timestamp | Speaker | Voice | Original | New line to generate |
|---|-----------|---------|-------|----------|------|
| 1 | 0:00.26–0:06.38 | Speaker 0 (man) | `liitt-testimonial-male-01` | "We love Goldies. We've been, uh, consumers of the Goldies products for-" | **"We love liitt. We've been, uh, consumers of the liitt products for-"** |
| 2 | 0:08.16–0:11.90 | Speaker 1 (woman) | `liitt-testimonial-female-01` | "Yep. We're really enjoying Banana Punch lately." | **"Yep. We're really enjoying Fairy Flame lately."** |
| 3 | 0:17.96–0:22.22 | Speaker 0 (man) | `liitt-testimonial-male-01` | "Yes, in nice pre-rolls or-" (+ "Yeah" + "...flower." + "However you prefer." cut with it) | **"Yes, it's yummy gummies."** |

Generate a short test of each first, check it matches the original speaker's energy, then finalize. Download all three as separate audio files.

## Step 3 — Cut one line entirely (no audio needed)

- **"Pun intended."** (0:00:13.82–0:00:14.02, Speaker 0) gets removed completely — not replaced, just cut. It only worked as a joke when the flavor was called "Banana Punch"; keep "It packs a punch." right before it, drop this line.

## Step 4 — Hand back for splicing/reassembly (don't do this in-browser)

Report back the 3 downloaded audio files and stop. I'll splice them into the original track locally with ffmpeg:
- Replace 0:00:00.26–0:00:06.38 with new line 1
- Replace 0:00:08.16–0:00:11.90 with new line 2
- Remove 0:00:13.82–0:00:14.02 entirely (close the gap)
- Replace 0:00:17.96–0:00:22.22 with new line 3 (this segment gets shorter — video will need a matching trim, see step 5)

## Step 5 — Enhancor lip-sync (after splicing is done)

1. Go to app.enhancor.ai/editor, find the **lip-sync / dub / "sync audio to video"** tool.
2. Only feed it the video segments that actually changed (the 3 windows above, trimmed a second or two wider on each side for a clean blend) plus the corresponding new spliced audio — not the full 37 seconds.
3. Report back the exact tool name/label you find, in case it's not literally called "lip sync."

## Step 6 — Confirm before publishing

Return the final short lip-synced segments to the user for review. Do not reassemble the full final video, publish, or send anywhere — that's a manual step on our end.

---

**Ground rules for the assistant:**
- Only generate the 3 lines listed above — do not inven