# autoVSL

Create VSLs cheaply — VO free, video from ~$1.60 via API or $0 manual.

> **New here? Start with [`docs/AGENTS.md`](docs/AGENTS.md).**
> autoVSL is a multi-agent ad factory. Five skills in `.claude/skills/` chain
> research → brand → script → finished video: **prospector** and **bloodhound**
> (research), **brand-platform-builder** (brand voice), **vsl** (copywriter),
> **vsl-editor** (production engine). `docs/AGENTS.md` maps how they hand off.
> The section below is the low-level generation tooling the editor builds on.

## What's ready

| Step | Tool | Cost | Status |
|------|------|------|--------|
| VO (8 lines) | `./scripts/generate-vo.sh` | **$0** | **Done** ✓ |
| Video (8 shots) | `./scripts/generate-video.sh` (fal.ai) | **~$1.60** | Script ready |
| Video (free) | Hailuo website manual | **$0** | Prompts ready |
| Music | Suno free / Pixabay | **$0** | Manual |
| Assemble | `./scripts/assemble-vsl.sh` | **$0** | Script ready |

**Kling is NOT the cheapest API.** Wan 2.2 on fal.ai is ~$0.20/clip vs Kling ~$0.35–0.56/clip.

## Cheapest automated pipeline

```bash
# 1. VO — free (already done)
./scripts/generate-vo.sh fairy-flame

# 2. Video — ~$1.60 via API (or $0 manual on Hailuo website)
cp .env.example .env          # add FAL_KEY from fal.ai
./scripts/generate-video.sh --list-models    # compare pricing
./scripts/generate-video.sh fairy-flame --shot 1   # test 1 clip (free credits)
./scripts/generate-video.sh fairy-flame          # all 8 shots

# 3. Music — free manual (Suno or Pixabay → media/music/background.mp3)

# 4. Assemble
./scripts/check-media.sh fairy-flame
./scripts/assemble-vsl.sh fairy-flame
```

## Free video (no API key)

```bash
./scripts/print-prompts.sh fairy-flame
# Paste into Hailuo (hailuoai.video) — ~3 free clips/day
```

## Palmier (optional, edit only)

Palmier's **editor + MCP import/sequence is free**. Only generation costs money.

If you prefer Palmier over ffmpeg for the final cut:
1. Open Palmier → new 9:16 project
2. In Cursor Agent: "Build from timeline.json — **import only, do not generate**"

## Project structure

```
autoVSL/
├── docs/
│   ├── DEV-README.md         # System architecture & dev handoff (share with team)
│   └── free-generation.md    # Full free/cheap generation guide
├── scripts/
│   ├── generate-vo.py        # Free VO (Edge TTS)
│   ├── generate-video.py     # Video via fal.ai API
│   ├── print-prompts.sh      # Dump video/music prompts
│   ├── check-media.sh        # Verify files exist
│   └── assemble-vsl.sh       # Free ffmpeg export
├── vsls/fairy-flame/         # First VSL (reference implementation)
└── output/                   # Final MP4s land here
```

**For developers:** see [docs/DEV-README.md](docs/DEV-README.md) for architecture, ClickUp integration plan, and phased roadmap.
