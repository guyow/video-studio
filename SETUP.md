# Setup — Video Studio Local Development

This repository contains the **source code** for the Video Studio web app and its engines. The **model weights** and **Python venvs** are not included in git — they're large, binary, and platform-specific.

## Quick start (after cloning)

### 1. Create Python virtual environments

You need 4 venvs. Each one is isolated and can be created independently on your machine.

```bash
# Main venv: OpenCV, Flask, numpy, Pillow, repair engines
cd autoVSL
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-cv.txt

# Whisper/transcription venv
# NOTE: the nvidia-* packages are REQUIRED for CUDA transcription — without them
# faster-whisper fails with "Library cublas64_12.dll is not found"
cd ..\course_pipeline
python -m venv .venv
.venv\Scripts\activate
pip install faster-whisper pydub nvidia-cublas-cu12 nvidia-cudnn-cu12

# Dubbing (XTTS, Wav2Lip) venv — CUDA-heavy, watch the logs
# NOTE: torch MUST stay pinned — newer torch wheels (2.13+) fail to install on
# Windows with "WinError 206: filename too long" (their license tree exceeds MAX_PATH)
cd ..\dubbing-studio
python -m venv venv
venv\Scripts\activate
pip install torch==2.7.1 torchvision==0.22.1 torchaudio==2.7.1 --index-url https://download.pytorch.org/whl/cu126
pip install TTS librosa numba tqdm gfpgan resemblyzer
# TTS 0.22 breaks on transformers>=4.43 ("cannot import BeamSearchScorer") — pin it:
pip install transformers==4.40.2
# torch>=2.6 also rejects XTTS checkpoints (weights_only default). dubbing-studio/app.py
# already sets TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1 at the top — keep that line if you
# ever update the file.
# then patch basicsr for modern torchvision (functional_tensor was removed):
# in venv\Lib\site-packages\basicsr\data\degradations.py replace
#   from torchvision.transforms.functional_tensor import rgb_to_grayscale
# with
#   from torchvision.transforms.functional import rgb_to_grayscale

# VSR (optional, Power Tools)
cd ..\tools\vsr
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-vsr.txt
```

### 2. Download model weights

**Wav2Lip + GFPGAN** (for local lip-sync, ~1.5 GB):
```bash
# Create the directory structure
mkdir -p tools\Wav2Lip\checkpoints tools\Wav2Lip\gfpgan_weights

# Download Wav2Lip checkpoint from official repo:
# https://github.com/Rudrabha/Wav2Lip/releases/download/v1.0/wav2lip_gan.pth
# → save to tools\Wav2Lip\checkpoints\wav2lip_gan.pth

# Download GFPGAN v1.4 from:
# https://github.com/TencentARC/GFPGAN/releases/download/v1.3.8/GFPGANv1.4.pth
# → save to tools\Wav2Lip\gfpgan_weights\GFPGANv1.4.pth
```

**ProPainter** (subtitle erase, ~600 MB):
```bash
# Clone or download from: https://github.com/sczhou/ProPainter
# Place the full folder at: tools/ProPainter/
```

**CodeFormer** (optional face restore):
```bash
# pip install codeformer-pip (installs weights automatically)
# or place weights in CodeFormer/weights/ manually
```

**ComfyUI + SD1.5** (Brand Studio, ~5 GB, optional):
```bash
# Download ComfyUI portable for Windows:
# https://github.com/comfyui-org/ComfyUI/releases → ComfyUI_windows_portable
# Extract to: ComfyUI_windows_portable/
# Edit run_nvidia_lowvram.bat to match your GPU + CUDA version (cu126 for RTX 3050 Ti)
```

### 3. Configuration

Copy `video-studio/config.json.example` → `video-studio/config.json` and edit:
- `port`: 5180 (or your choice)
- `autovsl_root`: absolute path to `autoVSL/`
- `venvs`: point to the .venv paths you just created
- `remote_pin`: your 6-digit PIN for phone access
- FAL_KEY in `autoVSL/.env` (if using paid dubs)

### 4. Launch

```bash
# Start the dev server (uses autoVSL/.venv python)
.claude/launch.json defines it; or manually:
cd video-studio/app
..\..\autoVSL\.venv\Scripts\python server.py
# Visit http://localhost:5180
```

---

## Directory structure

```
Video AI editing/
├── video-studio/                    ← Flask web app + repair engines (the source code)
│   ├── app/
│   │   ├── server.py                ← ~3700 lines, all routes + job orchestration
│   │   ├── jobs.py                  ← persistent job store, GPU lock
│   │   ├── engines/                 ← repair + brand engines (Python)
│   │   └── static/                  ← 13 HTML tabs + shared CSS/JS
│   ├── config.json                  ← all paths, ports, PINs (edit to your setup)
│   └── docs/
├── autoVSL/                         ← data root + external engine scripts
│   ├── .venv/                       ← CREATED: main Python env
│   ├── dashboard/                   ← local_dub.py, dub.py (fal), caption.py
│   ├── scripts/                     ← script-swap.py (fal pipeline), ComfyUI client
│   ├── banks/                       ← brand kit, hooks/angles JSONL (small)
│   ├── output/                      ← workdirs (script-swap/<stem>/)
│   └── uploads/                     ← user footage (not in git)
├── dubbing-studio/                  ← XTTS + Wav2Lip standalone tool
│   ├── venv/                        ← CREATED: torch + TTS venv
│   └── lipsync.py                   ← lip-sync orchestrator (reused by app)
├── subtitle-studio/                 ← erase-subs engine (ProPainter wrapper)
├── course_pipeline/                 ← whisper transcription
│   └── .venv/                       ← CREATED: faster-whisper CUDA venv
├── tools/                           
│   ├── Wav2Lip/                     ← DOWNLOADED: checkpoint + weights
│   ├── ProPainter/                  ← DOWNLOADED: inpainting
│   └── CodeFormer/                  ← DOWNLOADED: face restore (optional)
├── ComfyUI_windows_portable/        ← DOWNLOADED: SD1.5 + launcher (optional)
├── PROJECT-SUMMARY.md               ← architecture + gotchas (dev handoff)
├── SETUP.md                         ← this file
└── .gitignore                       ← venvs, weights, outputs are excluded
```

---

## Notes for developers

1. **GPU arbitration is built-in** — one dub at a time, locked across all apps. Check `autoVSL/GPU_LOCK` for the cross-app semaphore.

2. **Stage caching** — engines cache stage outputs per workdir (e.g., XTTS voice goes in `new-vo.mp3`, Wav2Lip raw goes in `final.mp4`). This means `POST /api/job/<id>/resume` can pick up where it left off without re-running paid stages.

3. **No hardcoded paths** — all paths live in `config.json`. Engines read from environment/args, never assume a specific directory structure.

4. **Headless Claude CLI** — script generation and the vision advisor use `claude -p` (the user's CLI, not an API key). Inside a Claude Code session, `subprocess.run()` needs `CLAUDECODE` popped from the env, or the nested run refuses.

5. **Windows paths with spaces** — `"Video AI editing"` has spaces. Always `str(Path(...))` into subprocess args, never string-concatenate commands.

6. **Fast-Whisper word alignment bug** — `caption.py` retries and falls back to segment timing if word alignment crashes. It's been hit, so the fallback is load-bearing.

7. **Wav2Lip face detection** — patched locally to reuse nearest detection on face-less frames (ad end-cards). Stock `inference.py` aborts; the patch is in `tools/Wav2Lip/inference.py` lines 89–100.

---

## Contributing

- **Source code only** — don't commit venvs, model weights, or user data.
- **New engines** — add to `video-studio/app/engines/` and wire up the route in `server.py`.
- **Config changes** — document in `video-studio/config.json.example` and `SETUP.md`.
- **Bug reports** — see `PROJECT-SUMMARY.md` §8 for known gotchas before filing.

---

## Troubleshooting

**"Face not detected" on Wav2Lip** → the source video has a final end-card or a frame with no face. The fix is already in `tools/Wav2Lip/inference.py` (reuse nearest detection), and `local_dub.py` cuts at speech-end to avoid end-cards.

**Smart App Control blocks numba/llvmlite** (WinError 4551) → turn off Windows Smart App Control in Settings → Apps → App & browser control. The dub venv won't load otherwise.

**"Face not detected in ANY frame"** → the source truly has zero faces. Use a different video.

**ComfyUI won't launch** → check that `run_nvidia_lowvram.bat` points to the right GPU + CUDA version. The portable build ships cu130; downgrade to cu126 if you have an RTX card.

**"Module torch not found"** → the dubbing-studio venv didn't install torch. Re-run the pip command with the correct `--index-url` for your CUDA version.
