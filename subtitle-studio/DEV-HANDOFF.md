# Subtitle Studio — Developer Handoff

Local web app that removes burned-in (hard) subtitles from videos and burns fresh,
audio-synced captions. **100% local, zero paid APIs.** Built July 2026.

Owner workflow (marketer): drop a video → old subtitles removed invisibly → new
captions from the audio → deliverable lands on `Desktop/Subtitle Studio/`.

---

## 1. Run / deploy

- **Server**: `autoVSL\.venv\Scripts\python.exe subtitle-studio\server.py` → http://localhost:5180
- **Always run DETACHED** (`pythonw` via `Start-Process`, or the Desktop launcher
  `Start Subtitle Studio.bat`). Flask logs go to `server.err.log` (Flask logs to stderr).
- ⚠ **Stale-server trap**: if an old process still holds port 5180, a new one silently
  fails to bind and the OLD code keeps serving. Before restarting:
  `Get-NetTCPConnection -LocalPort 5180 -State Listen` → `Stop-Process` the OwningProcess
  **by PID**, loop until 0 listeners, then start. Verify a new endpoint responds after.

## 2. Architecture

```
subtitle-studio/
  server.py          Flask API + job runner (threads, in-memory jobs dict)
  static/index.html  single-page UI (vanilla JS, no build step)
  erase_subs.py      detection (EasyOCR/CRAFT + heuristics) + ProPainter erase (legacy)
  subclean.py        cheap covers: blur / bar / Telea inpaint
  recaption.py       faster-whisper word timing → ASS captions → ffmpeg burn (+cover modes)
  files/             uploaded videos (source of truth)
  files/.originals/  pre-erase backups + <stem>.box.json (detected subtitle band)
  output/<stem>/     captioned.mp4, lines.json (editable captions), words.json, script.txt
  tags.json          {stem: [tags]}
tools/vsr/           video-subtitle-remover v1.4.0 (the "magic erase" engine) + its own .venv
```

**Three python environments** (do not merge):
- `autoVSL/.venv` — Flask server, OpenCV, torch cu121, EasyOCR (detection), ProPainter deps
- `course_pipeline/.venv` — faster-whisper (transcription; CUDA via pip nvidia-cublas/cudnn)
- `tools/vsr/.venv` — VSR: torch 2.5.1+cu121, paddleocr/paddlepaddle, PySide6

Hardware: RTX 3050 Ti **4 GB VRAM** — one GPU job at a time (`GPU_LOCK` in server.py +
`wait_for_gpu()` scans for foreign ProPainter/erase processes via PowerShell).

## 3. Erase engines (quality/speed ladder)

| Engine | Where | Speed | Use |
|---|---|---|---|
| **VSR STTN "magic"** | `tools/vsr` CLI, style=`magic` (DEFAULT) | ~6–15 fps | Invisible pixel recovery. THE product. |
| frosted/box cover | recaption.py `--cover` | ~30–90 s total | Fast fallback, covers band with blur patch / black bar |
| ProPainter | erase_subs.py (legacy `/api/auto`) | 3–10 **s/frame** (hours) | Deprecated for long videos; checkpoint-resumable |

**VSR CLI** (all paths ABSOLUTE, `-o` relative crashes on `makedirs('')`):
```
cd tools/vsr
.venv/Scripts/python.exe -u backend/main.py -i <abs-in> -o <abs-out> \
    --inpaint-mode sttn-auto -c YMIN YMAX XMIN XMAX     # note: Y first!
```
Constant mask across all frames → **no flicker by construction**. `PYTHONUTF8=1` required.

**Patches applied to VSR source (re-apply if re-cloned):**
1. `backend/inpaint/sttn_auto_inpaint.py:245` — f-string nested same quotes (py3.12-only
   syntax) → doubled quotes. Find more with `python -m compileall backend`.
2. `backend/tools/video_io.py` `FramePrefetcher` — **EOF deadlock**: single `(False,None)`
   sentinel; once consumed, next `read()` blocks forever (triggered when container
   frame-count metadata overreports, e.g. `-c copy` cuts). Fixed with `self._eof` flag;
   `read()` returns `(False, None)` after EOF+empty queue.
3. Never pipe VSR stdout through `grep|tail` etc. — tqdm `\r` spam fills the pipe and
   hangs it. Stream line-by-line (server's `stream_cmd`) or redirect to a file.

## 4. Detection

`erase_subs.py --detect-only <video> <out.json>` → `{x,y,w,h,vw,vh,mode}` or `{none:true}`.
- Primary: **EasyOCR (CRAFT)** — 28 sampled frames, text boxes any colour, keeps rows
  recurring in ≥30% of text frames (temporal consistency), lower 55% of frame only,
  **full extent min/max** of band boxes (percentiles trimmed long lines → leaks), snaps to
  full width if >0.82·W. numpy int32 must be `int()`ed before `json.dumps`.
- Fallback: white-pixel heuristic (`auto_detect_box`).
- EasyOCR first run downloads models to `~/.EasyOCR`; progress bar crashes cp1252 consoles
  → `PYTHONUTF8=1` (server's `job_env()` sets it).

## 5. API surface (all JSON unless noted)

```
GET  /api/state                    files[] {name,stem,size_mb,cleaned,captioned,captioned_stale,
                                   editable,tags,box} + jobs[] {id,status,file,label,tail}
POST /api/upload                   multipart; collision-safe naming
POST /api/boxcaption               {file, style: magic|blur|box, captions: bool}  ← MAIN FLOW
POST /api/auto                     legacy ProPainter pipeline (slow)
POST /api/clean                    manual box {file,x,y,w,h,mode: erase|smart|blur|bar}
POST /api/clean-preview            same body +t → jpeg frame
POST /api/recaption                {file} captions only, video untouched
GET/POST /api/captions/<stem>      read / save+reburn editable lines [{start,end,text}]
POST /api/aifix/<stem>             {lines?} → local Claude CLI proofreads (haiku, ~30s, sync)
POST /api/rename                   {file,new} — moves backups/outputs/tags/thumb too
POST /api/tags                     {file, tags: "a, b" | [..]}
GET  /api/thumb/<stem>             cached jpeg poster (output/.thumbs)
GET  /api/selftest                 10 environment checks (~60s, sync)
POST /api/job/<id>/stop            kills engine PROCESS TREE + `_kill_engines()` sweep
POST /api/restore                  undo erase (backup → source, deletes box.json)
DELETE /api/file?file=             bundle to .trash/
GET  /media/<rel>                  range-supported video/file serving
```

**boxcaption(style=magic) flow**: OCR detect → box.json → VSR erase (+14px pad,
GPU-locked) → `backup_and_swap` (source replaced, original kept, box.json `mode:"erase"`)
→ recaption burns captions (placed over the band via ASS MarginV) or, if `captions:false`,
copies clean video to output + Desktop.

`box.json.mode` matters: `box|blur` → edit re-burns re-apply the cover (old subs still
under it); `erase` → no cover needed (source truly clean).

## 6. Captions

- Word timing: faster-whisper distil-large-v3, CUDA w/ CPU fallback (`--cpu` flag exists —
  used when GPU busy). Cache `words.json` (`--trust-cache` skips mtime check since erase
  stream-copies audio).
- `lines.json` = editable [{start,end,text}] (3 words/line default). `--burn-lines`
  re-burns from it (~7–15 s, no whisper).
- UI editor: per-line inputs, paste-full-script (redistributes across speech span),
  🤖 AI fix (POST /api/aifix — local `claude.exe -p --model haiku` via stdin, env pops
  `CLAUDECODE`, parses first `[`…last `]`, validates same line count).
- Style: ASS, Arial 50, white + black outline, ALL CAPS, MarginV from band position.

## 7. Jobs / concurrency

- In-memory `jobs` dict (lost on restart) + daemon worker threads; `stream_cmd` streams
  child stdout into `job["lines"]`, records `job["pid"]`.
- `guard_busy(name)` → 409 if a job runs on that file. `GPU_LOCK` + `acquire_gpu(job)`
  (poll-with-stop) serialize GPU work; `wait_for_gpu` also waits out OTHER apps' erase jobs.
- Stop = flag + `taskkill /T /F` + command-line sweep `_kill_engines()` (launcher shims
  respawn workers, so tree-kill alone is not enough). Sweep also runs at server startup
  (orphaned engines from a dead server keep the GPU busy).
- `_awake_keeper` thread: `SetThreadExecutionState(ES_SYSTEM_REQUIRED)` every 50 s while
  jobs run (Windows sleep once stretched a run to 91 min). Per-thread API — must stay on
  one persistent thread.
- ProPainter path checkpoints finished chunks in `files/.erase-cache-<stem>/` (resume).

## 8. UI notes (static/index.html)

- No framework, no build. Served with `max_age=0`, but browsers still cache hard —
  Ctrl+F5 after changes; verify with `curl /` if a change "didn't take".
- Embedded-browser constraints: `alert/confirm/window.open` are blocked → toast(),
  two-step armed buttons, in-page video overlay.
- 5 s poll re-renders cards but SKIPS while user is typing (tags/rename/search focus)
  and restores open `<details>`.
- Job progress: `stage(tail)` maps the last log line to a friendly status chip.

## 9. Known limitations / next ideas

- 4 GB VRAM = one GPU job at a time; magic erase ≈ 1 min per 8–10 s of 1080×1920 video.
- VSR quality on subs over *very* busy moving texture: excellent (verified over moving
  hair) but not formally validated on every content type — spot-check new content types.
- `/api/aifix` and `/api/selftest` are synchronous (30–90 s request) — fine for one user,
  convert to jobs if multi-user.
- Legacy `/api/auto` (ProPainter) could be removed once magic is fully trusted.
- Whisper punctuation sometimes splits mid-sentence ("I. LOOK") — AI fix usually corrects.

## 10. Quick smoke test

`GET /api/selftest` → 10 checks green. Then drop a short subtitled clip and hit
✨ Magic erase + captions; expect done in ~2–3 min for 20 s, deliverable on Desktop.
