# Free / Cheap Asset Generation

**Palmier generation costs money.** Use Palmier only for free timeline editing. Generate assets externally.

## TL;DR — which option?

| Approach | Cost for 8 shots | Quality | Automated in Cursor? |
|----------|------------------|---------|----------------------|
| **Hailuo website (free tier)** | **$0** | Good | No — manual copy-paste |
| **fal.ai Wan 2.2 480p (API)** | **~$1.60** | OK drafts | **Yes** — `./scripts/generate-video.sh` |
| **MiniMax Hailuo 512p (API)** | **~$0.80** | OK | Not yet (different API) |
| **Kling via API** | **~$2.80–4.50** | Best motion | Yes but **not cheapest** |
| **Palmier Generate** | **~$30+** | Best | Yes but **avoid** |

**Kling is NOT the cheapest.** Wan 2.2 and Hailuo are 2–4× cheaper. Kling wins on cinematic motion quality, not price.

---

## Cost summary

| Asset | Free option | Cheap fallback | Cost |
|-------|-------------|----------------|------|
| **VO (8 lines)** | `scripts/generate-vo.py` (Edge TTS) | ElevenLabs free tier (~10k chars/mo) | **$0** |
| **Music (60s)** | Suno free tier (manual) | Pixabay free download | **$0** |
| **Video (8 shots)** | Hailuo website free tier (manual) | `./scripts/generate-video.sh` (Wan ~$1.60) | **$0–1.60** |
| **Edit + export** | Palmier MCP (editor only) | ffmpeg `assemble-vsl.sh` | **$0** |

---

## 1. Voiceover — FREE (automated)

Uses Microsoft Edge TTS. No account, no API key.

```bash
./scripts/generate-vo.sh fairy-flame
```

Outputs `vo-01.mp3` … `vo-08.mp3` into `media/audio/`.

Try different voices:

```bash
python3 scripts/generate-vo.py fairy-flame --voice en-US-GuyNeural
python3 scripts/generate-vo.py --list-voices
```

**ElevenLabs** (optional, higher quality): free tier covers this script (~1,200 chars total). Paste lines from `./scripts/print-prompts.sh fairy-flame`.

---

## 2. Music — FREE (manual, ~2 min)

### Option A: Suno free tier (best match)
1. Go to [suno.com](https://suno.com) — free account gets daily credits
2. Paste prompt from `vsls/fairy-flame/suno-music.txt`:
   > Sparse warm piano, gentle ambient pads, slow cinematic build, no vocals, filmic mood, A24 style, 60 seconds
3. Enable **Instrumental**
4. Download → save as `media/music/background.mp3`

### Option B: Free stock music (instant)
Download a royalty-free cinematic piano track:
- [Pixabay Music](https://pixabay.com/music/search/cinematic%20piano/) — filter "piano", "ambient"
- [YouTube Audio Library](https://studio.youtube.com/) — cinematic / emotional

Save as `media/music/background.mp3`. Trim to ~60s in any editor.

---

## 3. Video — pick your path

### Option A: API in Cursor (~$1.60, automated)

Uses **Wan 2.2** on fal.ai — cheapest API option. Kling costs 2–3× more.

```bash
# See pricing comparison
./scripts/generate-video.sh --list-models

# Test with 1 shot using free signup credits
cp .env.example .env   # add your key from fal.ai/dashboard/keys
./scripts/generate-video.sh fairy-flame --shot 1 --dry-run
./scripts/generate-video.sh fairy-flame --shot 1

# Generate all 8 shots (~$1.60 at 480p)
./scripts/generate-video.sh fairy-flame
```

Models (use `--model` flag):
- `wan-480p` — **$1.60 total** (default, cheapest)
- `wan-580p` — $2.40 total
- `wan-720p` — $3.20 total
- `kling-turbo` — $2.80 total (better motion, not cheapest)

fal.ai gives **free signup credits** — enough to test 1–3 shots before paying.

### Option B: Website free tier ($0, manual)

Run `./scripts/print-prompts.sh fairy-flame` and paste into:

| Tool | Free allowance | Settings | Link |
|------|----------------|----------|------|
| **Hailuo (MiniMax)** | ~3 videos/day | 9:16, 6–10s | [hailuoai.video](https://hailuoai.video) |
| **Luma Dream Machine** | ~5 gens/day | 9:16 | [lumalabs.ai](https://lumalabs.ai/dream-machine) |
| **Kling** | signup credits | 9:16, 720p, 5–10s | [klingai.com](https://klingai.com) |
| **Pika** | limited free | 9:16 | [pika.art](https://pika.art) |

**Workflow per shot:**
1. Copy shot prompt + negative prompt
2. Set 9:16 aspect ratio, 5–10 seconds
3. Generate → download → rename to `shot-01.mp4` etc.
4. Drop in `media/video/`

### Cheap API fallback (~$2–4 total)
If free tiers run out: [fal.ai](https://fal.ai) Kling/Hailuo models at ~$0.25–0.50 per 5s clip. Still 10× cheaper than Palmier.

---

## 4. Assemble — FREE

### Option A: Palmier (free editor, no generation)
Open Palmier → new 9:16 project. **Do not use Generate buttons.**

In Cursor Agent:
> Build the Fairy Flame VSL from `vsls/fairy-flame/timeline.json`. Import only — do not generate.

### Option B: ffmpeg (fully local, no Palmier)
```bash
./scripts/assemble-vsl.sh fairy-flame
```
Outputs `output/fairy-flame.mp4`.

---

## Full free pipeline (one command chain)

```bash
# 1. VO — automated, free
pip install -r requirements.txt
python3 scripts/generate-vo.py fairy-flame

# 2. Video + music — manual (see above)
./scripts/print-prompts.sh fairy-flame

# 3. Verify
./scripts/check-media.sh fairy-flame

# 4. Assemble — pick one
./scripts/assemble-vsl.sh fairy-flame          # ffmpeg, $0
# OR tell Cursor to build in Palmier (import only)
```
