# Subtitle Studio

Standalone local tool for removing burned-in (hard) subtitles from videos and
burning fresh ones — **100% free, 100% offline**. There is no fal.ai, no API
key, and no network call anywhere in this folder; nothing here can ever charge
money.

## Features

1. **Remove subtitles** — drag a box over the subtitles, pick a method, preview one
   frame, then apply to the whole video:
   - **✨ AI erase (default)** — ProPainter video inpainting on the local GPU. Detects the
     letter pixels frame-by-frame and rebuilds the *real background* behind them from
     neighboring frames. Best quality; a few minutes per video.
   - **🧠 Smart fill** — fast per-frame spatial inpaint of the whole box.
   - **🌫 Blur** — dissolves the box area.
   - **🖤 Caption bar** — dark backdrop, made to sit under new subtitles.

   The original video is always backed up to `files/.originals/` (one-click restore).

2. **New subtitles** — transcribes the video's *own audio* locally (faster-whisper on
   GPU, word-level timestamps) and burns bold white word-timed captions **exactly over
   the erased band**. Output: `output/<name>/captioned.mp4` plus a copy on your
   Desktop in `Subtitle Studio/`.

## Run

```
autoVSL\.venv\Scripts\python.exe subtitle-studio\server.py
```

then open http://localhost:5180 — or start it from the Claude dashboard launcher
(config name `subtitle-studio`).

## Dependencies (already installed on this machine — nothing new to install)

- `autoVSL/.venv` — flask, opencv, numpy, torch (used for the server + erase engines)
- `course_pipeline/.venv` — faster-whisper (used for word timing)
- `tools/ProPainter` — AI inpainting model + weights
- ffmpeg (winget Gyan.FFmpeg)

## Notes

- The AI erase targets bright/white caption text with dark outlines (the classic
  social-video style). Dark text on a white sticker is a graphic, not a caption —
  use the bar/blur mode on those, or draw the box tightly around real subtitles.
- Don't run two AI-erase jobs at the same time — the 4 GB GPU can't fit both.
- `files/`, `output/`, `.trash/` are working folders; safe to delete old items.
