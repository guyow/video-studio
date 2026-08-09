# Plan: a real AI video editor inside Video Studio

Written 2026-08-08. STATUS same day: P0–P1 built (`/timeline`), P2 partially
(text→ASS, audio lanes, per-clip fades; no ducking yet), P3 largely built
(edit-by-transcript ✓, silence removal ✓, auto-split ✓, generative clip ops via
fal ✓, chat-driven editing ✓; clip-scoped legacy engines + auto-reframe still
open), P4–P5 not started (transitions/keyframes/presets). See the
`timeline-editor` memory for verified results and machine gotchas.

Decision taken up front: **staged AI-first → full NLE**, general-purpose (ad cuts, VSL
assembly, winner refresh all sit on one core). It extends `/editor` in this app rather
than starting a new project — port discipline, the one-app unification, and all 18
engines are already here.

---

## 1. Where we actually stand

The AI half is genuinely strong. The editor half does not exist.

**Built and real:** ~6,200-line `app/server.py`, ~130 endpoints, 18 engines — erase
(LaMa), dub (XTTS + fal), voice bank, captions (ASS), i2v, t2v, b-roll factory,
frame-reader (cut detection + vision), image editor, object repair, frame swap,
fit-extend, smart crop, face swap.

**The gap.** `/editor` looks like an editor and isn't one. `editor-app.js:560`
`renderTimeline()` draws six *fixed* lanes — FX/JOB, VIDEO, B-ROLL, VOICE, CAPTIONS,
DELIVER — and each lane renders **one full-width bar** describing job state for the
single selected video. The filmstrip is 12 static frames from `/api/qc/frames`. It is a
pipeline dashboard shaped like a timeline.

The only editing operation in the entire codebase is `/api/edit` (`server.py:2172`):
one trim `[start,end]` plus an optional center zoom, written to a new file.

So there is:

- no sequence/clip model — nothing can hold "clip B follows clip A"
- no multi-clip, no tracks, no split, no ripple, no reorder
- no transitions, no overlay compositing, no keyframes
- no render graph — every feature writes a whole new file end-to-end

That last point is the important one, and it's also the opportunity.

## 2. The one architectural idea

> **A sequence document (EDL) is the single source of truth. Every AI engine stays
> exactly as it is — it just becomes an operation on a clip.**

Today every engine consumes a file and emits a new file. That is normally an obstacle to
building an editor. Here it isn't, because in an EDL world an AI op targets a clip,
produces a new source file, and the clip's `src` pointer swaps to it. The original stays
on disk. Editing becomes non-destructive **by construction**, and all 18 engines get
adopted without rewriting one of them.

That is why this is worth building on top of what exists rather than starting over.

### The document

`output/projects/<slug>/project.json`

```json
{
  "id": "ad-flame-07", "name": "Flame hook test", "version": 7,
  "canvas": { "w": 1080, "h": 1920, "fps": 30 },
  "tracks": [
    { "id": "V1", "kind": "video", "clips": [
      { "id": "c1", "src": "uploads/raw-a.mp4",
        "in": 2.0, "out": 7.5, "start": 0.0, "speed": 1.0,
        "transform": { "scale": 1, "x": 0, "y": 0, "rot": 0 },
        "volume": 1.0, "effects": [], "transition_in": null,
        "origin": { "engine": null, "parent": null } }
    ]},
    { "id": "T1", "kind": "text",  "clips": [] },
    { "id": "A1", "kind": "audio", "clips": [] }
  ],
  "markers": [], "meta": {}
}
```

Invariants: `start` is timeline position, `in`/`out` are the source range, clip duration
is `(out - in) / speed`. `origin` records which engine produced a swapped-in source and
what it replaced, so any AI op is one click to revert.

## 3. Preview — real-time in the browser

The preview is what decides whether this feels like an editor or like a batch tool. It
must be real-time and scrubbable, which means no server round-trip per frame.

- **Proxies.** On import, transcode to 540p (vertical) / 720p h264 with a dense keyframe
  interval for fast seeking, CRF 26, cached in `output/proxies/`. Also extract audio
  peaks for waveform drawing.
- **Playback engine.** A pool of 2–3 `<video>` elements. The current clip plays in A
  while the next is preloaded *and pre-seeked* in B; at the boundary we swap which one is
  visible and call `play()`. That is what makes multi-clip playback gapless.
- **Overlays** (text, captions, stickers) draw to a `<canvas>` above the video, driven by
  rAF from the active element's `currentTime` mapped to timeline time.
- **Transforms** (scale/pan/crop) are CSS transforms on the video element — free, and
  GPU-composited by the browser.
- **Audio tracks** are separate `<audio>` elements slaved to the master clock, with drift
  correction whenever they exceed ~60 ms.

**Honest limit:** DOM preview cannot reproduce ffmpeg's transitions and blend effects
exactly. For those we render a short segment server-side (2–3 s around the transition),
cache it, and play that. Anywhere preview is approximate, the UI says so.

## 4. Render — EDL → ffmpeg, with a segment cache

`app/engines/sequence_render.py` compiles the document to a `filter_complex`:

- per clip: `trim` → `setpts` (speed) → `scale`/`crop`/`pad` to canvas
- joins: `concat`, with `xfade` where a transition is set
- overlays: `overlay` gated by `enable='between(t,…)'`
- **text: render the text/caption tracks to an ASS file and use the `subtitles` filter** —
  reusing `recaption.py`'s `build_ass` and the House Bold template. `drawtext` is worse
  and we already own better.
- audio: `atrim`/`adelay`/`volume` per clip → `amix`, with `sidechaincompress` for
  ducking VO over music

**Segment cache is the performance story.** Hash each clip's `(src, in, out, speed,
transform, effects, canvas, fps)`, render it once to
`output/projects/<slug>/cache/<hash>.mp4` normalised to identical codec/fps/pix_fmt/
timebase, then build the final with the concat *demuxer* and stream copy. Change one
clip, re-render one clip. On a 4 GB machine this is the difference between a usable
editor and a slideshow.

Two invariants carried over from this repo's own scar tissue:

- **never `-shortest`** — it silently dropped trailing frames before; use `-frames:v n`
  plus an ffprobe frame-count guard that deletes a bad take
- every cached segment must be byte-compatible or concat-copy will break; normalise
  aggressively at segment render time, not at the end

## 5. The AI layer — what makes it *an AI editor*

These are timeline-native, which is the part no other tool here does. Each takes a clip
or a range and returns an EDL patch.

1. **Edit by transcript.** Whisper word timings → transcript panel → delete words and the
   video ripple-deletes with them. Every dependency already exists. This is the single
   highest-value feature in the plan.
2. **Silence and filler removal.** `silencedetect` (already used in the dub pipeline)
   proposes cuts as a reviewable diff — never applied blindly.
3. **Auto-split on import.** `frame_reader.py` already does cut detection. Drop in a
   winning ad and get it back as separate clips. This is what makes "refresh a winner"
   fast.
4. **Auto-reframe.** `smart_crop.py` per clip to 9:16 / 1:1 / 16:9, stored as EDL
   transform keyframes rather than a new file — so it costs nothing and stays editable.
5. **Auto b-roll placement.** Script beats + the b-roll bank → Claude proposes placements
   → inserted on V2 for review.
6. **Every existing engine as a clip op** — erase, dub, voice, face swap, object repair,
   fit-extend — applied to one clip, result swapped into `src`, original preserved.

## 6. Phases

Each phase is independently shippable. P0 and P1 are the whole game; if they land, the
rest is additive.

| Phase | What | Done when |
|---|---|---|
| **P0** | `app/sequence.py` (schema, atomic versioned save, undo stack, validation), `sequence_render.py` (compiler + segment cache + frame guards), `/api/seq/*` CRUD, proxy generator, golden-file render tests | a hand-written 3-clip EDL renders frame-exact, asserted by ffprobe |
| **P1** | The real timeline: clip tracks, drag, trim handles, split at playhead, ripple delete, snapping, zoom, multi-select. Playback engine (A/B pool + canvas + audio sync). Undo/redo, autosave. Old pipeline lanes kept as a collapsible strip — we don't lose job visibility | import 3 clips, cut a 30 s ad, scrub smoothly, export matches preview |
| **P2** | Text track → ASS path, caption track bound to the transcript, audio tracks with music/VO, fades, auto-duck | a fully captioned ad with a music bed exports correctly |
| **P3** | The AI layer from §5 — transcript editing, silence cuts, auto-split, clip-scoped engines, auto-reframe | paste a winner → auto-split → swap voice → re-caption → export, without leaving the page |
| **P4** | Full NLE: transitions, keyframed transforms (Ken Burns, zoom punches), effect stack, speed ramps, multi-track compositing | multi-layer edit with transitions renders as previewed |
| **P5** | Presets per workflow (ad cut / VSL assembly / winner refresh) and a Claude "assemble a first cut from this script + this footage" that emits an EDL for you to edit | first cut generated, then hand-edited |

Rough weight: P1 is the largest single piece (the playback engine is the hard part), P0
and P3 next, P2 and P5 smallest.

## 7. Risks, named

- **Preview/export divergence.** Mitigated by rendered preview segments for transitions
  and by labelling approximate preview honestly. Cannot be fully eliminated.
- **A/V sync drift in JS playback.** The hardest engineering in P1. Needs a real master
  clock and periodic correction, not naive `play()` calls.
- **`server.py` is already 6,185 lines.** Adding ~40 endpoints inline makes it worse.
  Sequence endpoints should go in a new `app/api/sequence.py` Blueprint — a *new*
  blueprint adds no risk to existing routes, unlike the deferred split of old ones.
- **4 GB GPU.** Renders are ffmpeg/CPU-bound; GPU ops stay behind the existing
  `GPU_LOCK`. The segment cache is what keeps iteration tolerable.
- **Scope drift into P4 too early.** Transitions and keyframes are the fun part and the
  wrong part to start. The EDL + playback engine must be solid first or they get built
  twice.

## 8. Recommendation

Build **P0 first, and build it with tests** — the EDL schema and the render compiler are
the contract everything else depends on, and they're the cheapest thing to get wrong
now and the most expensive to change later. P0 ships with no visible UI change, which
feels unsatisfying; it is still the right order.

Then P1, where it starts looking like an editor, and P3, where it starts being one no
one else has.
