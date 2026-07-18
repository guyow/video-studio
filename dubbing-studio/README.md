# 🎙️ Local Voice-Clone Dubbing Tool

Drop in a video, paste your own script, and this tool clones the voice of the
speaker in the video and reads **your** words in that cloned voice. It outputs:

- **`dubbed_*.wav`** — the cloned-voice audio (the key hand-off for your lip-sync tool)
- **`dubbed_*.mp4`** — your original video with the new audio swapped in

It runs **fully local / offline** after a one-time model download. It does
**not** transcribe, translate, or lip-sync — you supply the exact words, and
lip-sync happens in your separate downstream tool.

Engine: **Coqui XTTS v2** via the maintained [`coqui-tts`](https://github.com/idiap/coqui-ai-TTS)
package (a drop-in replacement for the abandoned `TTS` package).

---

## One-time setup (Windows)

### 1. Install ffmpeg
```powershell
winget install Gyan.FFmpeg
```
Open a **new** terminal afterwards so the PATH update takes effect. Verify:
```powershell
ffmpeg -version
```

### 2. Create the virtual environment (Python 3.10 recommended)
```powershell
cd "dubbing-studio"
py -3.10 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

### 3. Install the CUDA-enabled PyTorch wheel FIRST
This must be installed before `requirements.txt` so you get the GPU build, not
the CPU-only one. This is the build for CUDA 12.1 wheels (works on the RTX 3050 Ti
laptop GPU with the 566.x / CUDA 12.7 driver):
```powershell
pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu121
```

> **Why torch 2.5.1 and not newer?** torch ≥ 2.6 changed `torch.load` to
> `weights_only=True` by default, which breaks XTTS checkpoint loading. 2.5.1
> avoids that headache.

Verify the GPU is visible:
```powershell
python -c "import torch; print(torch.cuda.is_available())"   # should print True
```
If it prints `False`, you'll still be able to run on CPU (slower) — the app
falls back automatically.

### 4. Install the rest
```powershell
pip install -r requirements.txt
```

### 5. Download the model (~1.9 GB) — use the resumable downloader
XTTS v2 caches into `./models/`. **Run the included downloader first** — it
resumes on a dropped connection, unlike Coqui's built-in downloader (which
restarts from scratch and will waste the whole download on a flaky link):
```powershell
python download_model.py
```
After this, the app loads the model fully offline. It also auto-accepts the
Coqui Public Model License (`COQUI_TOS_AGREED=1`) so nothing blocks on a prompt.

> You *can* skip this and let the app download on first dub, but on a slow
> connection that download has no resume — `download_model.py` is safer.

> **License note:** XTTS v2 is released under the **Coqui Public Model License
> (CPML)** — free for non-commercial use. For commercial use, obtain a license
> from Coqui.

---

## Launch the app
```powershell
python app.py
```
It opens in your browser. Then:
1. Drop your video into the upload area.
2. Paste your script.
3. Pick the script's language.
4. (Optional) Upload a custom reference voice.
5. (Optional) Nudge **"Keep original audio volume"** above 0 to keep the
   original track faintly under the dub.
6. Click **Dub**. The `.wav` and `.mp4` appear on the right with download buttons.

Outputs are also saved to the `outputs/` folder.

### Command-line mode (optional)
```powershell
python app.py --cli --video clip.mp4 --script "Hello, this is my script." --language en
# or point --script at a .txt file:
python app.py --cli --video clip.mp4 --script my_script.txt --language es --keep-volume 0.1
# force CPU:
python app.py --cli --video clip.mp4 --script my_script.txt --device cpu
```

---

## Knobs to turn if quality/speed isn't right

| Symptom | Try |
|---|---|
| Voice doesn't sound like the speaker | Upload a **custom reference** — a clean 10–15s clip of just their voice, no music/noise. Cleaner reference = better clone. |
| Reference clip grabbed the wrong part | Upload your own reference audio to override the auto-extracted clip. |
| Robotic / rushed delivery | Add punctuation to your script (commas, periods) — XTTS uses it for pacing and pauses. |
| Long pauses between sentences | Lower the gap: edit `gap = np.zeros(int(0.3 * ...))` in `synthesize()` (e.g. `0.15`). |
| Chunks sound disconnected | Increase `MAX_CHARS_PER_CHUNK` (top of `app.py`) so fewer, longer chunks are synthesized — costs more VRAM. |
| Out of VRAM on GPU | It auto-falls back to CPU. To force CPU up front, pick **cpu** in the Device selector. |
| Too slow | Use **cuda** (GPU). GPU is many times faster than CPU for XTTS. |
| Voice sounds slowed-down / sped-up | The length-match stretched it hard to fit the video. Write a script closer to the video's spoken length, or untick **"Match the dubbed voice length to the video"** (`--no-fit`). |
| Lip-sync tool wants equal-length inputs | Keep length-match **on** (default) — audio and video come out the same length. |

---

## Hardware / VRAM behavior

Designed for a **4 GB VRAM** laptop GPU (RTX 3050 Ti):

- XTTS v2 uses roughly **2–2.5 GB VRAM** during synthesis — fits comfortably in 4 GB.
- After each job the app runs `del model; gc.collect(); torch.cuda.empty_cache()`
  to release GPU memory.
- If CUDA runs out of memory mid-job, the app **catches the OOM, frees the GPU,
  and automatically retries the whole synthesis on CPU** with a clear log message.
- There's a **Device** selector (`auto` / `cuda` / `cpu`). `auto` uses the GPU
  when available.

### Windows Smart App Control note (already handled)
This machine has **Smart App Control** enabled, which blocks the unsigned
`llvmlite.dll` that ships inside `numba` (a dependency of `librosa`). That would
normally kill the whole app at import with *"An Application Control policy has
blocked this file"*. `app.py` detects this and swaps in a tiny pure-Python
stand-in for `numba` — XTTS's synthesis never uses the numba-accelerated
`librosa` functions, so there's no quality or speed loss for dubbing. **Nothing
to do here, and Smart App Control is left fully enabled** (this is not a
security bypass — it just removes the dependency on the blocked file). On a PC
without Smart App Control, the real `numba` is used automatically.

### CUDA-OOM troubleshooting
- **Close other GPU apps** (games, browsers with lots of GPU tabs, Stable
  Diffusion, etc.). Check current usage with `nvidia-smi`.
- **Pick `cpu`** in the Device selector to sidestep the GPU entirely (slower).
- **Shorten the script** or lower `MAX_CHARS_PER_CHUNK` so each generation is smaller.
- If `torch.cuda.is_available()` is `False`, you installed the CPU-only torch —
  reinstall the cu121 wheel (step 3).

---

## Where things live
- `app.py` — the Gradio app + CLI, implementing the whole pipeline.
- `requirements.txt` — pinned deps (torch installed separately, see setup).
- `models/` — XTTS v2 weights cache (git-ignored, ~1.9 GB).
- `outputs/` — generated `.wav` / `.mp4` (git-ignored).

## How it works (pipeline)
1. **Reference:** extract audio from the video with ffmpeg, skip leading
   silence, cut a ~12s mono 16 kHz clip to clone from (or use your uploaded
   reference).
2. **Clone + synthesize:** XTTS v2 clones the voice and reads your script.
   Long scripts are split into sentence chunks, synthesized, and concatenated.
3. **Length-match (for lip-sync), on by default:** the dubbed audio is
   time-stretched (pitch preserved, via ffmpeg `atempo`) to exactly match the
   video length, so the audio and video are equal-length and drop straight into
   a lip-sync tool like **fal.ai**. Turn it off (checkbox, or `--no-fit`) to keep
   the voice at its natural length instead. Big stretches (>2x) stay in tune but
   can sound unnatural — write a script roughly the length of the video for the
   best result.
4. **Outputs:** write the dubbed `.wav`, then mux it into the video with ffmpeg
   (video stream copied, not re-encoded).
