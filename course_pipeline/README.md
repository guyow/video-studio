# Course Transcription & Knowledge-Extraction Pipeline

Turns folders of course videos into (1) clean timestamped transcripts and
(2) distilled, agent-ready `SKILL.md` knowledge documents. Everything runs
locally and free: audio extraction via ffmpeg, transcription via
faster-whisper, distillation via the `claude` CLI on your subscription
(no API key, nothing large ever uploaded anywhere).

## Folder structure

```
course_pipeline/
  transcribe.py        video/audio -> transcripts
  distill.py           transcripts -> SKILL.md per course
  requirements.txt
  .venv/               (created by setup)

output/
  transcripts/
    <Course Name>/               mirrors your course folder structure
      01 - Lesson.md             readable transcript with [HH:MM:SS] timestamps
      01 - Lesson.json           raw segments (+ words) for tooling/RAG later
  skills/
    <Course Name>/
      SKILL.md                   distilled frameworks, principles, playbooks, quotes
      notes/                     per-video condensed notes (long courses only)
  .cache/audio/                  temporary 16kHz wavs (deleted after each file)
```

## One-time setup

```powershell
# Windows
py -3.11 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
# optional, NVIDIA GPU only:
.venv\Scripts\pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

```bash
# macOS
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

No system ffmpeg needed — the script uses the binary bundled with
`imageio-ffmpeg` if ffmpeg isn't on PATH. The Whisper model (~1.5 GB for
distil-large-v3) downloads automatically on first run and is cached.

## Running on a new course folder

```powershell
# 1. Transcribe (point at the folder that contains one subfolder per course)
.venv\Scripts\python transcribe.py "D:\Courses"

# 2. Distill into SKILL.md files
.venv\Scripts\python distill.py
```

That's it. Both steps are idempotent:
- `transcribe.py` skips any video that already has a completed `.json` sidecar.
- `distill.py` skips any course whose `SKILL.md` is newer than all its transcripts
  (so adding one new video to a course re-distills just that course).
- Interrupting mid-run is safe; finished files are kept. `--force` redoes everything.

Useful flags:

| Flag | What it does |
|---|---|
| `transcribe.py --model large-v3` | multilingual / max accuracy (distil-large-v3 is English-only) |
| `transcribe.py --language en` | skip per-file language detection |
| `transcribe.py --word-timestamps` | per-word timing in the .json (slower) |
| `transcribe.py --device cpu` | force CPU (default: try GPU, fall back) |
| `distill.py --course "Name"` | distill a single course |
| `distill.py --model opus` | higher-quality distillation pass |

## Model choice: speed vs accuracy

| Model | Accuracy | Notes |
|---|---|---|
| **distil-large-v3** (default) | ~large-v3 within ~1% WER on English | ~6x faster than large-v3. English-only. Best choice for English courses. |
| **large-v3** | best | Use for non-English courses or heavily accented audio. |
| medium / small | noticeably worse on jargon | Only if you're desperate for speed on CPU. |

Long files are handled natively by faster-whisper (it streams internally with
voice-activity detection) — no manual chunking needed, and a progress bar
tracks position in the audio.

## Notes

- Extracted audio is 16 kHz mono WAV (~110 MB per hour), created next to nothing
  and deleted after each file — a 500 MB video never gets copied or uploaded.
- `distill.py` uses `claude -p` headless mode. Long courses are map-reduced:
  each video is condensed to dense notes (cached in `skills/<course>/notes/`),
  then one synthesis call produces `SKILL.md`.
- The `.json` sidecars keep every segment (and optionally every word) with
  timestamps, so you can build embeddings/RAG later without re-transcribing.
