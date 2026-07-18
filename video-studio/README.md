# Video Studio

One unified local-first app for the whole video pipeline — subtitle removal, transcripts,
dubbing, lip-sync repair, captions, QA, exports, and the Ads Factory.

**Run:**
```
autoVSL\.venv\Scripts\python.exe video-studio\app\server.py
```
then open **http://localhost:5180**. (Or start the `video-studio` entry in `.claude/launch.json`.)

## Layout

| Path | What |
|---|---|
| `app/server.py` | Flask server (based on the autoVSL dashboard, re-pathed via config) |
| `app/jobs.py` | persistent + resumable + GPU-arbitrated job store |
| `app/engines/dubsync_repair.py` | DubSync Repair engine (remux / refit / renorm / relipsync) |
| `app/static/` | the 11-tab UI (`vs.css` + `vs-nav.js` = shared shell) |
| `config.json` | **every machine path** — the only file to edit when paths change |
| `jobs/` | persisted job index + per-job logs (survive restarts) |
| `docs/` | brief, inventory, migration plan, architecture |

## Where the engines live (called in place, never copied)

- **Erase / captions:** `subtitle-studio/erase_subs.py`, `recaption.py` (ProPainter, whisper)
- **Dubbing / lip-sync:** `autoVSL/dashboard/{dub,local_dub}.py`, `dubbing-studio/lipsync.py`
- **Ads Factory:** `autoVSL/scripts/*` (edge-tts, fal.ai, ComfyUI, ffmpeg)
- **Data root:** the `autoVSL/` repo (uploads, products, banks, output) — config `autovsl_root`
- **fal.ai key:** stays in `autoVSL/.env`

## The original apps (fallbacks, untouched)

- subtitle-studio — start via launch.json (`autoPort` moves it off 5180)
- autoVSL dashboard — port 5170
- dubbing-studio (Gradio) — port 7860

Exports land in **Desktop\Video Studio** (config `exports_dir`).
