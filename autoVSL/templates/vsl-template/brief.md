# VSL Brief — [Product Name]

**Format:** 9:16 vertical, ~60s  
**Product:**  
**CTA:**  
**Mood:**

## Pipeline

1. Generate video shots (Kling or Palmier built-in)
2. Generate VO (ElevenLabs)
3. Generate music (Suno)
4. Import + sequence in Palmier via Cursor

## Negative prompt (video)

```
neon, fast cuts, glossy skin, fake smiles, stock footage energy, hard sell, before/after split screen, EDM
```

## Media folders

- `media/video/` — shot-01.mp4, shot-02.mp4, ...
- `media/audio/` — vo-01.mp3, vo-02.mp3, ...
- `media/music/` — background.mp3

## Build command

> Build the [Product] VSL from `vsls/SLUG/timeline.json`. Palmier is open.
