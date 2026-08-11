# Video Studio

A local web app that turns a winning UGC ad into your own. Runs on your machine —
the GPU work is local and free; only the premium cloud models cost money, and every
spend asks for approval first.

**The main pipeline:** upload a UGC ad that already works → it's transcribed and its
keyframes are read to learn *why* it works (beat order, pacing, settings) → your
script is rebuilt on that proven structure as handheld UGC shots → each shot is
generated → stitched, narrated in a cloned voice, and captioned with TikTok-style
story text.

It also does: voice-clone dubbing + lip-sync (one or two speakers), subtitle removal
with background restoration, word-timed captions, image→video, script rewriting, and
multi-aspect export.

---

## Requirements

- **Windows** (the launcher and a few engine paths are Windows-specific)
- **Python 3.11** — 3.12/3.13 break the pinned AI packages
- **NVIDIA GPU, 4 GB+** for the free local engines (dub, lip-sync, subtitle erase,
  transcription). Without one, the cloud models still work.
- **ffmpeg** on PATH (setup installs it via winget)
- Optional: **ComfyUI** for local image generation; a **fal.ai** key for cloud models;
  **Claude Code** logged in for the AI script/shot-list features

## Install

```powershell
git clone https://github.com/guyow/video-studio.git
cd video-studio
powershell -ExecutionPolicy Bypass -File install\setup-machine.ps1
```

That checks your prerequisites, builds the three Python environments from the pinned
requirements in `install\`, applies the known dependency patches, creates
`video-studio\config.json` from the template with fresh secrets, installs a Desktop
shortcut, and boots the server.

## Run

```powershell
powershell -ExecutionPolicy Bypass -File video-studio\launcher\start-video-studio.ps1
```

…or double-click the **Video Studio** desktop shortcut. Then open
**http://localhost:5180**.

`launcher\enable-autostart.ps1` boots the server silently at login;
`stop-video-studio.ps1` shuts it down.

## Configure

`video-studio\config.json` is **not** in git — it holds this machine's session key
and phone-access PIN. `config.example.json` is the template; setup copies it and
generates fresh secrets. Paths in it must point at your clone.

For the paid cloud models, put your own key in `autoVSL\.env`:

```
FAL_KEY=your-key-here
```

Without it, everything local still runs; the cloud features stay off.

---

## Layout

| Path | What's there |
|---|---|
| `video-studio/app/server.py` | the Flask app — all routes |
| `video-studio/app/engines/` | pipeline engines (b-roll video, tags, fal text→video, repairs) |
| `video-studio/app/static/` | the UI (vanilla HTML/JS, no build step) |
| `video-studio/launcher/` | desktop shortcut, start/stop, login autostart |
| `autoVSL/dashboard/` | dub, caption, diarize engines |
| `autoVSL/scripts/` | ComfyUI client + workflow builders |
| `dubbing-studio/` | XTTS voice cloning + Wav2Lip lip-sync |
| `subtitle-studio/` | subtitle removal + caption burning |
| `install/` | pinned requirements + machine setup + packaging scripts |
| `PROJECT-SUMMARY.md` | architecture and developer handoff notes |
| `docs/` | operational handoff notes (e.g. giving the app to a VA) |

Jobs are persistent and GPU-arbitrated (one GPU job at a time), so a render survives
a restart. Two rules the engines follow deliberately: **`-shortest` is never used**
(it silently drops frames — build to a known duration and verify), and **every paid
call is estimated and confirmed before it runs**.

Start with `PROJECT-SUMMARY.md`.
