#!/usr/bin/env python3
"""Subtitle Studio — standalone local hard-subtitle remover + re-captioner.

100% local and FREE. No fal.ai, no API keys, no network calls — only ffmpeg,
OpenCV, ProPainter (local GPU) and faster-whisper (local GPU).

Run:  autoVSL/.venv/Scripts/python.exe subtitle-studio/server.py
      (needs the autoVSL venv for flask/cv2/torch and the course_pipeline venv
       for whisper — both already installed on this machine)
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
FILES = ROOT / "files"
ORIGINALS = FILES / ".originals"
OUTPUT = ROOT / "output"
TRASH = ROOT / ".trash"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}

# engines run in the existing venvs (no new installs):
#   cv2/torch/ProPainter -> autoVSL venv ; faster-whisper -> course_pipeline venv
CV_PY = ROOT.parent / "autoVSL" / ".venv" / "Scripts" / "python.exe"
WHISPER_PY = ROOT.parent / "course_pipeline" / ".venv" / "Scripts" / "python.exe"
FFMPEG_BIN = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1.2-full_build/bin"
)
CLAUDE_EXE = Path.home() / ".local" / "bin" / "claude.exe"   # local Claude CLI (free AI spell-fix)
VSR_DIR = ROOT.parent / "tools" / "vsr"                       # video-subtitle-remover (magic erase)
VSR_PY = VSR_DIR / ".venv" / "Scripts" / "python.exe"
TAGS_FILE = ROOT / "tags.json"
THUMBS = OUTPUT / ".thumbs"


def load_tags() -> dict:
    try:
        return json.loads(TAGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_tags(t: dict) -> None:
    TAGS_FILE.write_text(json.dumps(t, indent=1), encoding="utf-8")


def _busy_stems() -> set:
    """Stems that currently have a running job — their temp must NOT be touched."""
    with jobs_lock:
        return {Path(j.get("file") or "").stem for j in jobs.values()
                if j.get("status") == "running" and j.get("file")}


def _temp_paths(stem: str | None = None):
    """Erase scratch files that are always safe to delete (never the source, backup, or
    deliverable): <stem>.cleaning.*, .lama-pass*-<stem>.*, .preview-<stem>.jpg, erase-* dirs.
    Pass a stem to target one video, or None for every leftover in files/."""
    if not FILES.is_dir():
        return []
    s = glob.escape(stem) if stem else "*"
    pats = [f"{s}.cleaning*", f".lama-pass*-{s}.*", f".lama-pass*-{s}.cleaning*",
            f".preview-{s}.jpg", "erase-*"]
    out, seen = [], set()
    for pat in pats:
        for p in FILES.glob(pat):
            if p not in seen:
                seen.add(p); out.append(p)
    return out


def clean_temp(stem: str | None = None, skip_busy: bool = True) -> int:
    """Delete erase intermediates; returns bytes freed. Skips temp for a running job."""
    busy = _busy_stems() if skip_busy else set()
    now = time.time()
    freed = 0
    for p in _temp_paths(stem):
        # a temp file's name embeds its video stem; skip if that video is mid-job
        if skip_busy and any(b and b in p.name for b in busy):
            continue
        try:
            # never delete something being actively written (guards orphaned engine
            # processes not tracked in the jobs dict, e.g. after a server restart)
            if now - p.stat().st_mtime < 90:
                continue
        except Exception:
            pass
        try:
            if p.is_dir():
                freed += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file():
                freed += p.stat().st_size
                p.unlink()
        except Exception:
            pass
    return freed


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

app = Flask(__name__, static_folder=None)
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
GPU_LOCK = threading.Lock()  # the 4GB GPU fits exactly one ProPainter run


def job_env() -> dict:
    env = dict(os.environ)
    if FFMPEG_BIN.is_dir():
        env["PATH"] = str(FFMPEG_BIN) + os.pathsep + env.get("PATH", "")
    env["PYTHONUTF8"] = "1"
    return env


def ffmpeg_exe(name: str) -> str:
    p = FFMPEG_BIN / f"{name}.exe"
    return str(p) if p.is_file() else name


def _awake_keeper() -> None:
    """Stop Windows from sleeping mid-job (an overnight run once took 91 min because
    the machine dozed off). Resets the idle timer every 50s while any job runs;
    must run on one persistent thread — SetThreadExecutionState is per-thread."""
    import ctypes
    while True:
        with jobs_lock:
            busy = any(j["status"] == "running" for j in jobs.values())
        if busy:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(0x00000001)  # ES_SYSTEM_REQUIRED
            except Exception:
                pass
        time.sleep(50)


def new_job(action: str, label: str, file: str = "") -> dict:
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"id": job_id, "action": action, "label": label, "file": file,
                    "status": "running", "lines": [], "returncode": None,
                    "started": time.time(), "ended": None}
    return jobs[job_id]


def guard_busy(name: str, mode: str = "") -> None:
    """Refuse duplicate work: never two jobs on one file, never two AI erases (4GB GPU)."""
    with jobs_lock:
        for j in jobs.values():
            if j["status"] != "running":
                continue
            if j.get("file") == name:
                abort(409, f"a {j['action']} job is already running on {name} — wait for it")
            if mode == "erase" and "(erase)" in j["label"]:
                abort(409, "an AI erase is already running — the GPU fits only one at a time")


def stream_cmd(job: dict, cmd: list[str], cwd: Path | None = None) -> int:
    proc = subprocess.Popen(cmd, cwd=str(cwd or ROOT), env=job_env(),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    job["pid"] = proc.pid
    for line in proc.stdout:
        with jobs_lock:
            job["lines"].append(line.rstrip("\n"))
    rc = proc.wait()
    job["pid"] = None
    if job.get("stop"):
        raise RuntimeError("stopped by user")
    return rc


def acquire_gpu(job: dict) -> None:
    """Take the GPU lock, staying responsive to a user stop while queued."""
    while not GPU_LOCK.acquire(timeout=5):
        if job.get("stop"):
            raise RuntimeError("stopped by user")


def log_line(job: dict, line: str) -> None:
    with jobs_lock:
        job["lines"].append(line)


def foreign_erase_running() -> bool:
    """True if ANOTHER app (e.g. the autoVSL dashboard) has ProPainter on the GPU."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and "
             "($_.CommandLine -match 'inference_propainter' -or "
             "($_.CommandLine -match 'erase_subs' -and $_.CommandLine -notmatch 'subtitle-studio')) }).Count"],
            capture_output=True, text=True, timeout=30)
        return int((r.stdout or "0").strip() or 0) > 0
    except Exception:
        return False


def wait_for_gpu(job: dict, max_wait: int = 3600) -> None:
    waited = 0
    while foreign_erase_running():
        if job.get("stop"):
            raise RuntimeError("stopped by user")
        if waited == 0:
            log_line(job, "GPU is busy with another AI erase (other app) — waiting for it to finish…")
        elif waited % 120 == 0:
            log_line(job, f"…still waiting for the GPU ({waited // 60} min)")
        time.sleep(20)
        waited += 20
        if waited >= max_wait:
            raise RuntimeError("gave up waiting for the GPU after an hour — try again later")


def finish(job: dict, ok: bool, note: str = "") -> None:
    if note:
        with jobs_lock:
            job["lines"].append(note)
    job["returncode"] = 0 if ok else 1
    job["status"] = "done" if ok else ("stopped" if job.get("stop") else "failed")
    job["ended"] = time.time()


# ---------------------------------------------------------------- state

@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html", max_age=0)


@app.get("/api/state")
def api_state():
    tags = load_tags()
    items = []
    if FILES.is_dir():
        for p in sorted(FILES.iterdir()):
            if not (p.is_file() and p.suffix.lower() in VIDEO_EXTS):
                continue
            captioned = OUTPUT / p.stem / "captioned.mp4"
            items.append({
                "name": p.name, "stem": p.stem,
                "size_mb": round(p.stat().st_size / 1e6, 1),
                "mtime": p.stat().st_mtime,
                "cleaned": (ORIGINALS / p.name).is_file(),
                "box": json.loads((ORIGINALS / f"{p.stem}.box.json").read_text(encoding="utf-8"))
                       if (ORIGINALS / f"{p.stem}.box.json").is_file() else None,
                "captioned": f"output/{p.stem}/captioned.mp4" if captioned.is_file() else None,
                "captioned_stale": captioned.is_file()
                                   and p.stat().st_mtime > captioned.stat().st_mtime,
                "editable": (OUTPUT / p.stem / "lines.json").is_file(),
                "tags": tags.get(p.stem, []),
            })
    with jobs_lock:
        active = [{"id": j["id"], "action": j["action"], "label": j["label"],
                   "status": j["status"], "started": j["started"], "file": j.get("file"),
                   "tail": next((ln for ln in reversed(j["lines"]) if ln.strip()), "")}
                  for j in jobs.values()]
    return jsonify({"files": items, "jobs": sorted(active, key=lambda j: -j["started"])})


@app.get("/api/job/<job_id>")
def api_job(job_id):
    job = jobs.get(job_id)
    if not job:
        abort(404)
    with jobs_lock:
        return jsonify(job)


@app.post("/api/job/<job_id>/stop")
def api_job_stop(job_id):
    """Stop a running job: flag it, then kill its engine process tree."""
    job = jobs.get(job_id)
    if not job or job["status"] != "running":
        abort(404, "no running job with that id")
    job["stop"] = True
    log_line(job, "⏹ stop requested — killing the engine…")
    pid = job.get("pid")
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    _kill_engines()  # the tracked pid's launcher can respawn a worker — sweep them all
    # half-written temp output is useless — remove ALL of it (.cleaning + .lama-pass*);
    # the source video is untouched, it is only swapped at the very END of a successful run
    name = job.get("file") or ""
    if name:
        clean_temp(Path(name).stem, skip_busy=False)
    return jsonify({"stopping": job_id})


@app.get("/media/<path:rel>")
def media(rel):
    target = (ROOT / rel).resolve()
    if not str(target).startswith(str(ROOT)) or target.suffix.lower() not in (
            ".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".jpg", ".png"):
        abort(403)
    if not target.is_file():
        abort(404)
    return send_file(target, conditional=True)


# ---------------------------------------------------------------- upload / delete

@app.post("/api/upload")
def api_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "no file")
    name = secure_filename(f.filename) or f"video-{int(time.time())}.mp4"
    if Path(name).suffix.lower() not in VIDEO_EXTS:
        abort(400, "not a video file")
    FILES.mkdir(exist_ok=True)
    dest = FILES / name
    while dest.exists():  # never overwrite — a job may be using the existing file
        stem, suf = dest.stem, dest.suffix
        n = stem.rsplit("-", 1)
        stem = f"{n[0]}-{int(n[1]) + 1}" if len(n) == 2 and n[1].isdigit() else f"{stem}-2"
        dest = FILES / f"{stem}{suf}"
    f.save(dest)
    return jsonify({"name": dest.name})


@app.delete("/api/file")
def api_delete():
    name = Path(request.args.get("file") or "").name
    src = FILES / name
    if not name or not src.is_file():
        abort(404)
    guard_busy(name)
    TRASH.mkdir(exist_ok=True)
    bundle = TRASH / f"{src.stem}-{time.strftime('%Y%m%d-%H%M%S')}"
    bundle.mkdir()
    for piece in (src, ORIGINALS / name, ORIGINALS / f"{src.stem}.box.json", OUTPUT / src.stem):
        if piece.exists():
            shutil.move(str(piece), str(bundle / piece.name))
    return jsonify({"trashed": bundle.name})


# ---------------------------------------------------------------- storage / cleanup

@app.get("/api/storage")
def api_storage():
    """Report where disk is going so the user can decide what to reclaim. MB per category."""
    temp = 0
    for p in _temp_paths():
        if p.is_dir():
            temp += _dir_size(p)
        elif p.is_file():
            temp += p.stat().st_size
    files_total = _dir_size(FILES)
    originals = _dir_size(ORIGINALS)
    thumbs = _dir_size(THUMBS)
    mb = lambda b: round(b / 1e6, 1)
    try:
        free_gb = round(shutil.disk_usage(ROOT).free / 1e9, 1)
    except Exception:
        free_gb = None
    return jsonify({
        "sources_mb": mb(max(0, files_total - originals - temp)),  # files/ minus backups & temp
        "originals_mb": mb(originals),
        "output_mb": mb(max(0, _dir_size(OUTPUT) - thumbs)),
        "thumbs_mb": mb(thumbs),
        "trash_mb": mb(_dir_size(TRASH)),
        "temp_mb": mb(temp),                       # reclaimable scratch (safe to delete)
        "free_gb": free_gb,
    })


@app.post("/api/cleanup")
def api_cleanup():
    """Reclaim disk. Always removes leaked temp (safe); empties .trash only if asked.
    NEVER touches source videos, .originals backups, or output/ deliverables."""
    want_trash = bool((request.get_json(silent=True) or {}).get("trash"))
    temp_freed = clean_temp(None, skip_busy=True)   # skip any video mid-job
    trash_freed = 0
    if want_trash and TRASH.is_dir():
        trash_freed = _dir_size(TRASH)
        for item in TRASH.iterdir():
            try:
                shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink()
            except Exception:
                pass
    return jsonify({"temp_mb": round(temp_freed / 1e6, 1),
                    "trash_mb": round(trash_freed / 1e6, 1),
                    "total_mb": round((temp_freed + trash_freed) / 1e6, 1)})


# ---------------------------------------------------------------- subtitle removal

def _box_request():
    body = request.get_json(force=True)
    name = Path(body.get("file") or "").name
    src = FILES / name
    if not name or not src.is_file():
        abort(404, "file not found")
    try:
        box = {k: int(body[k]) for k in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        abort(400, "need integer x/y/w/h box")
    if box["w"] < 4 or box["h"] < 4:
        abort(400, "box too small — drag a rectangle over the subtitles")
    mode = body.get("mode") if body.get("mode") in ("erase", "smart", "blur", "bar") else "erase"
    return body, name, src, box, mode


def _engine_cmd(src: Path, out: Path, box: dict, mode: str, preview_at: float | None) -> list[str]:
    if mode == "erase":
        cmd = [str(CV_PY), str(ROOT / "erase_subs.py"), str(src), str(out),
               "--x", str(box["x"]), "--y", str(box["y"]),
               "--w", str(box["w"]), "--h", str(box["h"])]
        if preview_at is not None:
            cmd += ["--preview-at", str(preview_at)]
    else:
        cmd = [str(CV_PY), str(ROOT / "subclean.py"), str(src),
               "--box", str(box["x"]), str(box["y"]), str(box["w"]), str(box["h"]),
               "--mode", mode, "--out", str(out)]
        if preview_at is not None:
            cmd += ["--preview-at", str(preview_at)]
    return cmd


@app.post("/api/clean-preview")
def api_clean_preview():
    body, name, src, box, mode = _box_request()
    t = max(0.0, float(body.get("t") or 1.0))
    out = FILES / f".preview-{src.stem}.jpg"
    proc = subprocess.run(_engine_cmd(src, out, box, mode, t),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=job_env(), timeout=120)
    if proc.returncode != 0 or not out.is_file():
        abort(500, f"preview failed: {(proc.stdout or '')[-300:]}")
    return send_file(out, mimetype="image/jpeg", max_age=0)


@app.post("/api/clean")
def api_clean():
    body, name, src, box, mode = _box_request()
    guard_busy(name, mode)
    job = new_job("clean", f"Remove subtitles — {name} ({mode})", name)

    def worker():
        try:
            tmp = FILES / f"{src.stem}.cleaning{src.suffix}"
            if mode == "erase":
                acquire_gpu(job)
                try:
                    wait_for_gpu(job)
                    rc = stream_cmd(job, _engine_cmd(src, tmp, box, mode, None))
                finally:
                    GPU_LOCK.release()
            else:
                rc = stream_cmd(job, _engine_cmd(src, tmp, box, mode, None))
            if rc != 0 or not tmp.is_file():
                raise RuntimeError("engine failed — see log above")
            backup_and_swap(src, tmp, {**box, "mode": mode})
            finish(job, True, f"Done — original backed up to files/.originals/{name}")
        except Exception as exc:
            finish(job, False, f"FAILED: {exc}")
        finally:
            clean_temp(src.stem, skip_busy=False)   # never leave erase scratch behind

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job_id": job["id"]})


def backup_and_swap(src: Path, tmp: Path, box: dict) -> None:
    """Replace src with the cleaned tmp, keeping the FIRST original + the box for captions."""
    probe = subprocess.run(
        [ffmpeg_exe("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(src)],
        capture_output=True, text=True, check=True, env=job_env())
    vw, vh = (int(n) for n in probe.stdout.strip().split(",")[:2])
    ORIGINALS.mkdir(exist_ok=True)
    if not (ORIGINALS / src.name).exists():
        shutil.move(str(src), str(ORIGINALS / src.name))
    else:
        src.unlink()  # re-clean: keep the FIRST original
    (ORIGINALS / f"{src.stem}.box.json").write_text(
        json.dumps({**box, "vw": vw, "vh": vh}), encoding="utf-8")
    tmp.rename(src)


@app.post("/api/restore")
def api_restore():
    name = Path(request.get_json(force=True).get("file") or "").name
    backup = ORIGINALS / name
    if not name or not backup.is_file():
        abort(404, "no backup for that video")
    guard_busy(name)
    src = FILES / name
    if src.exists():
        src.unlink()
    shutil.move(str(backup), str(src))
    (ORIGINALS / f"{Path(name).stem}.box.json").unlink(missing_ok=True)
    return jsonify({"restored": name})


# ---------------------------------------------------------------- new subtitles

@app.post("/api/recaption")
def api_recaption():
    name = Path(request.get_json(force=True).get("file") or "").name
    src = FILES / name
    if not name or not src.is_file():
        abort(404, "file not found")
    if not WHISPER_PY.is_file():
        abort(500, f"whisper venv missing at {WHISPER_PY}")
    guard_busy(name)
    job = new_job("recaption", f"New subtitles — {name}", name)

    def worker():
        rc = stream_cmd(job, [str(WHISPER_PY), str(ROOT / "recaption.py"), str(src)])
        finish(job, rc == 0)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job_id": job["id"]})


# ---------------------------------------------------------------- organize: rename / tags / thumbs

@app.post("/api/rename")
def api_rename():
    """Rename a video (e.g. to a character name) — moves its backups, box and outputs too."""
    body = request.get_json(force=True)
    name = Path(body.get("file") or "").name
    src = FILES / name
    if not name or not src.is_file():
        abort(404, "file not found")
    new_stem = secure_filename(Path(body.get("new") or "").stem).strip(". ")
    if not new_stem:
        abort(400, "give a valid new name")
    guard_busy(name)
    dest = FILES / f"{new_stem}{src.suffix.lower()}"
    if dest.exists():
        abort(409, f"{dest.name} already exists")
    old_stem = src.stem
    shutil.move(str(src), str(dest))
    for a, b in ((ORIGINALS / name, ORIGINALS / dest.name),
                 (ORIGINALS / f"{old_stem}.box.json", ORIGINALS / f"{new_stem}.box.json"),
                 (OUTPUT / old_stem, OUTPUT / new_stem),
                 (THUMBS / f"{old_stem}.jpg", THUMBS / f"{new_stem}.jpg")):
        if a.exists():
            shutil.move(str(a), str(b))
    tags = load_tags()
    if old_stem in tags:
        tags[new_stem] = tags.pop(old_stem)
        save_tags(tags)
    return jsonify({"name": dest.name})


@app.post("/api/tags")
def api_tags():
    body = request.get_json(force=True)
    name = Path(body.get("file") or "").name
    stem = Path(name).stem
    raw = body.get("tags") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    clean = [t.strip().lower() for t in raw if t.strip()][:12]
    tags = load_tags()
    if clean:
        tags[stem] = clean
    else:
        tags.pop(stem, None)
    save_tags(tags)
    return jsonify({"tags": clean})


@app.get("/api/thumb/<stem>")
def api_thumb(stem):
    stem = Path(stem).name
    src = next((p for p in FILES.iterdir()
                if p.is_file() and p.stem == stem and p.suffix.lower() in VIDEO_EXTS), None)
    if src is None:
        abort(404)
    THUMBS.mkdir(parents=True, exist_ok=True)
    th = THUMBS / f"{stem}.jpg"
    if not th.is_file() or th.stat().st_mtime < src.stat().st_mtime:
        subprocess.run([ffmpeg_exe("ffmpeg"), "-y", "-loglevel", "error", "-ss", "1",
                        "-i", str(src), "-frames:v", "1", "-vf", "scale=200:-2", str(th)],
                       capture_output=True, env=job_env(), timeout=60)
    if not th.is_file():
        abort(404)
    return send_file(th, mimetype="image/jpeg", max_age=300)


# ---------------------------------------------------------------- AI spell-fix (local Claude CLI, free)

@app.post("/api/aifix/<stem>")
def api_aifix(stem):
    """Proofread the caption lines with the local Claude CLI: fixes speech-to-text
    misspellings/mishearings using context. Keeps line count/order/timing. Returns the
    corrected lines for review — burning happens only when the user saves."""
    stem = Path(stem).name
    body = request.get_json(force=True) or {}
    lines = body.get("lines")
    if not lines:
        lf = OUTPUT / stem / "lines.json"
        if not lf.is_file():
            abort(404, "no captions to fix — caption the video first")
        lines = json.loads(lf.read_text(encoding="utf-8"))
    exe = CLAUDE_EXE if CLAUDE_EXE.is_file() else shutil.which("claude")
    if not exe:
        abort(500, "local Claude CLI not found — AI fix unavailable")
    texts = [str(ln.get("text", "")) for ln in lines]
    prompt = (
        "You are a subtitle proofreader. Below is a JSON array of subtitle lines from "
        "speech-to-text; they are short ALL-CAPS lines shown in sequence, so read them as one "
        "continuous script to infer intended words. Fix ONLY transcription errors: misheard or "
        "misspelled words, broken punctuation, nonsense fragments. Do NOT rephrase, do NOT "
        "change style, keep ALL-CAPS, keep the SAME number of lines in the SAME order (each "
        "line keeps its timing). Reply with ONLY the corrected JSON array — no commentary, no "
        "code fences.\n\n" + json.dumps(texts, ensure_ascii=False)
    )
    env = job_env()
    env.pop("CLAUDECODE", None)   # nested-run guard for the CLI
    try:
        r = subprocess.run([str(exe), "-p", "--model", "haiku"], input=prompt,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired:
        abort(504, "AI took too long — try again")
    out = r.stdout or ""
    i, j2 = out.find("["), out.rfind("]")
    if i < 0 or j2 <= i:
        abort(500, f"AI reply unusable: {out[:150]}")
    try:
        fixed = json.loads(out[i:j2 + 1])
    except Exception:
        abort(500, "AI reply was not valid JSON — try again")
    if not isinstance(fixed, list) or len(fixed) != len(lines):
        abort(500, f"AI returned {len(fixed) if isinstance(fixed, list) else '?'} lines, expected {len(lines)} — try again")
    changed = sum(1 for a, b in zip(texts, fixed) if str(a).strip() != str(b).strip())
    new_lines = [{**ln, "text": str(fixed[k])} for k, ln in enumerate(lines)]
    (OUTPUT / stem).mkdir(parents=True, exist_ok=True)
    (OUTPUT / stem / "lines.json").write_text(json.dumps(new_lines, indent=1), encoding="utf-8")
    return jsonify({"lines": new_lines, "changed": changed})


# ---------------------------------------------------------------- self-test

@app.get("/api/selftest")
def api_selftest():
    """Verify every moving part of the studio so problems surface HERE, not mid-job."""
    checks = []

    def add(name, ok, info=""):
        checks.append({"name": name, "ok": bool(ok), "info": str(info)[:160]})

    def run_ok(cmd, timeout=90, **kw):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=job_env(), timeout=timeout, **kw)
            return r.returncode == 0, (r.stdout or r.stderr or "").strip().splitlines()[:1]
        except Exception as e:
            return False, [str(e)[:120]]

    ok, info = run_ok([ffmpeg_exe("ffmpeg"), "-version"], 20)
    add("ffmpeg (video encoding)", ok, info[0] if info else "")
    ok, info = run_ok([ffmpeg_exe("ffprobe"), "-version"], 20)
    add("ffprobe (video info)", ok, "")
    if CV_PY.is_file():
        ok, info = run_ok([str(CV_PY), "-c",
                           "import cv2,torch,easyocr;print('cuda' if torch.cuda.is_available() else 'CPU ONLY')"], 120)
        add("vision engine (OpenCV+Torch+EasyOCR)", ok, info[0] if info else "")
    else:
        add("vision engine venv", False, f"missing {CV_PY}")
    if WHISPER_PY.is_file():
        ok, info = run_ok([str(WHISPER_PY), "-c", "import faster_whisper;print('ok')"], 90)
        add("transcription engine (Whisper)", ok, "")
    else:
        add("transcription venv", False, f"missing {WHISPER_PY}")
    add("magic erase engine (VSR)", VSR_PY.is_file()
        and any((VSR_DIR / "backend" / "models").rglob("*.pth")), str(VSR_DIR))
    exe = CLAUDE_EXE if CLAUDE_EXE.is_file() else shutil.which("claude")
    add("AI spell-fix (Claude CLI)", bool(exe), str(exe or "not found"))
    ok, info = run_ok(["nvidia-smi", "--query-gpu=memory.total,memory.used",
                       "--format=csv,noheader"], 20)
    add("GPU", ok, info[0] if info else "")
    try:
        free_gb = shutil.disk_usage(ROOT).free / 1e9
        add("disk space", free_gb > 5, f"{free_gb:.0f} GB free")
    except Exception as e:
        add("disk space", False, e)
    try:
        t = FILES / ".writetest"
        t.write_text("x")
        t.unlink()
        add("folders writable", True, "")
    except Exception as e:
        add("folders writable", False, e)
    with jobs_lock:
        busy = sum(1 for j in jobs.values() if j["status"] == "running")
    add("job queue", True, f"{busy} running")
    return jsonify({"checks": checks, "ok": all(c["ok"] for c in checks)})


# ---------------------------------------------------------------- box & caption (fast)

@app.post("/api/boxcaption")
def api_boxcaption():
    """FAST alternative to AI erase: auto-detect the old subtitle band, cover it with a
    solid box, and burn the new (audio-transcribed) captions on top of that box.
    No ProPainter — finishes in ~1 minute."""
    body = request.get_json(force=True)
    name = Path(body.get("file") or "").name
    style = body.get("style") if body.get("style") in ("blur", "box", "magic", "sttn") else "magic"
    want_caps = body.get("captions", True)
    src = FILES / name
    if not name or not src.is_file():
        abort(404, "file not found")
    if not WHISPER_PY.is_file():
        abort(500, f"whisper venv missing at {WHISPER_PY}")
    if style == "sttn" and not VSR_PY.is_file():
        abort(500, "STTN engine (VSR) not installed")
    guard_busy(name, "erase" if style in ("magic", "sttn") else "")
    label = ("Magic erase" if style == "magic" else
             "STTN erase" if style == "sttn" else f"Cover ({style})") + \
            (" + new captions" if want_caps else " only")
    job = new_job("boxcaption", f"{label} — {name}", name)

    def worker():
        try:
            log_line(job, "=== step 1/2 — detecting where the old subtitles are")
            ORIGINALS.mkdir(exist_ok=True)
            box_json = ORIGINALS / f"{src.stem}.box.json"
            rc = stream_cmd(job, [str(CV_PY), str(ROOT / "erase_subs.py"), str(src),
                                  str(box_json), "--detect-only"])
            if rc != 0:
                raise RuntimeError("detection failed — see log above")

            if style in ("magic", "sttn"):
                # magic = OUR LAMA eraser: GENERATES the fill per frame — works even when
                #   captions cover the area on every frame (static talking-heads).
                # sttn  = VSR temporal engine: copies real background from other frames —
                #   fastest + literal, but ghosts when the background is never revealed.
                box = json.loads(box_json.read_text(encoding="utf-8"))
                if box.get("none"):
                    log_line(job, "no burned-in subtitles found — skipping erase")
                else:
                    pad = 14
                    ymin = max(0, box["y"] - pad); ymax = min(box["vh"], box["y"] + box["h"] + pad)
                    xmin = max(0, box["x"] - pad); xmax = min(box["vw"], box["x"] + box["w"] + pad)
                    tmp = FILES / f"{src.stem}.cleaning{src.suffix}"
                    log_line(job, f"=== step 2/{'3' if want_caps else '2'} — {style} erase "
                                  f"(band y{ymin}-{ymax})")
                    if style == "magic":
                        cmd = [str(CV_PY), "-u", str(ROOT / "lama_erase.py"), str(src), str(tmp),
                               "--x", str(xmin), "--y", str(ymin),
                               "--w", str(xmax - xmin), "--h", str(ymax - ymin)]
                        cmd_cwd = None
                    else:
                        cmd = [str(VSR_PY), "-u", "backend/main.py",
                               "-i", str(src), "-o", str(tmp),
                               "--inpaint-mode", "sttn-auto",
                               "-c", str(ymin), str(ymax), str(xmin), str(xmax)]
                        cmd_cwd = VSR_DIR
                    acquire_gpu(job)
                    try:
                        wait_for_gpu(job)
                        rc = stream_cmd(job, cmd, cwd=cmd_cwd)
                    finally:
                        GPU_LOCK.release()
                    if rc != 0 or not tmp.is_file():
                        raise RuntimeError("magic erase failed — see log above")
                    backup_and_swap(src, tmp, {"x": box["x"], "y": box["y"],
                                               "w": box["w"], "h": box["h"], "mode": "erase"})
                    log_line(job, f"original backed up to files/.originals/{name}")
                if want_caps:
                    log_line(job, "=== final step — burning new audio-synced captions")
                    rc = stream_cmd(job, [str(WHISPER_PY), str(ROOT / "recaption.py"), str(src)])
                    if rc != 0:
                        raise RuntimeError("caption burn failed — see log above")
                else:
                    # remove-only: the cleaned source IS the result — publish it
                    out_dir = OUTPUT / src.stem
                    out_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, out_dir / "captioned.mp4")
                    dest = Path.home() / "Desktop" / "Subtitle Studio"
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest / f"{src.stem}-clean.mp4")
                    log_line(job, f"clean video -> Desktop/Subtitle Studio/{src.stem}-clean.mp4")
                finish(job, True, "✔ magic erase complete")
                return

            cmd = [str(WHISPER_PY), str(ROOT / "recaption.py"), str(src), "--cover", "--cover-style", style]
            if want_caps:
                log_line(job, f"=== step 2/2 — covering that band ({style}) and burning new captions")
            else:
                cmd.append("--no-captions")
                log_line(job, f"=== step 2/2 — covering that band ({style}), NO new captions")
            rc = stream_cmd(job, cmd)
            finish(job, rc == 0)
        except Exception as exc:
            finish(job, False, f"FAILED: {exc}")
        finally:
            clean_temp(src.stem, skip_busy=False)   # never leave erase scratch behind

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job_id": job["id"]})


# ---------------------------------------------------------------- edit subtitles

@app.get("/api/captions/<stem>")
def api_captions_get(stem):
    """Return the editable caption lines for a video (404 if not captioned yet)."""
    lines_file = OUTPUT / Path(stem).name / "lines.json"
    if not lines_file.is_file():
        abort(404, "no captions yet — run 'Just add captions' or the pipeline first")
    return jsonify({"lines": json.loads(lines_file.read_text(encoding="utf-8"))})


@app.post("/api/captions/<stem>")
def api_captions_save(stem):
    """Save edited caption lines and re-burn them (fast — no transcription)."""
    stem = Path(stem).name
    lines = (request.get_json(force=True) or {}).get("lines")
    if not isinstance(lines, list) or not lines:
        abort(400, "need a non-empty lines array")
    src = next((p for p in FILES.iterdir()
                if p.is_file() and p.stem == stem and p.suffix.lower() in VIDEO_EXTS), None)
    if src is None:
        abort(404, "source video not found")
    guard_busy(src.name)
    (OUTPUT / stem).mkdir(parents=True, exist_ok=True)
    (OUTPUT / stem / "lines.json").write_text(json.dumps(lines, indent=1), encoding="utf-8")
    job = new_job("recaption", f"Re-burn edited subtitles — {src.name}", src.name)

    def worker():
        rc = stream_cmd(job, [str(WHISPER_PY), str(ROOT / "recaption.py"), str(src), "--burn-lines"])
        finish(job, rc == 0)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job_id": job["id"]})


# ---------------------------------------------------------------- one-click pipeline

@app.post("/api/auto")
def api_auto():
    """Upload-to-done pipeline: transcribe audio -> script, AI-erase the original
    subtitles (auto-detected box, real background restored), burn the new subtitles."""
    name = Path(request.get_json(force=True).get("file") or "").name
    src = FILES / name
    if not name or not src.is_file():
        abort(404, "file not found")
    if not WHISPER_PY.is_file():
        abort(500, f"whisper venv missing at {WHISPER_PY}")
    guard_busy(name)
    job = new_job("auto", f"Auto pipeline — {name}", name)

    def worker():
        try:
            log_line(job, "=== step 1/3 — transcribing the audio into the new-subtitle script")
            # several videos can run at once: transcribe on GPU when it's free,
            # otherwise on CPU (slower but safe) so we never fight the erase for VRAM
            got_gpu = GPU_LOCK.acquire(blocking=False)
            try:
                cmd = [str(WHISPER_PY), str(ROOT / "recaption.py"), str(src), "--words-only"]
                if not got_gpu:
                    cmd.append("--cpu")
                    log_line(job, "(GPU busy — transcribing on CPU in parallel)")
                rc = stream_cmd(job, cmd)
            finally:
                if got_gpu:
                    GPU_LOCK.release()
            if rc != 0:
                raise RuntimeError("transcription failed — see log above")

            log_line(job, "=== step 2/3 — erasing the original subtitles "
                          "(auto-detecting them, AI-restoring the real background)")
            tmp = FILES / f"{src.stem}.cleaning{src.suffix}"
            acquire_gpu(job)
            try:
                wait_for_gpu(job)
                rc = stream_cmd(job, [str(CV_PY), str(ROOT / "erase_subs.py"), str(src), str(tmp)])
            finally:
                GPU_LOCK.release()
            if rc != 0 or not tmp.is_file():
                raise RuntimeError("subtitle erase failed — see log above")
            if any("no burned-in captions detected" in ln for ln in job["lines"]):
                tmp.unlink(missing_ok=True)
                log_line(job, "no burned-in subtitles found — video untouched, "
                              "new subtitles go lower-center")
            else:
                m = None
                for ln in reversed(job["lines"]):
                    m = re.search(r"erasing text in box \((\d+),(\d+)\) (\d+)x(\d+)", ln)
                    if m:
                        break
                box = ({"x": int(m[1]), "y": int(m[2]), "w": int(m[3]), "h": int(m[4])}
                       if m else {"x": 0, "y": 0, "w": 4, "h": 4})
                backup_and_swap(src, tmp, {**box, "mode": "erase"})
                log_line(job, f"original backed up to files/.originals/{name}")

            log_line(job, "=== step 3/3 — burning the new subtitles over the cleaned video")
            rc = stream_cmd(job, [str(WHISPER_PY), str(ROOT / "recaption.py"), str(src), "--trust-cache"])
            if rc != 0:
                raise RuntimeError("caption burn failed — see log above")
            finish(job, True, "✔ pipeline complete — play the result or grab it from Desktop/Subtitle Studio")
        except Exception as exc:
            finish(job, False, f"PIPELINE FAILED: {exc}")
        finally:
            clean_temp(src.stem, skip_busy=False)   # never leave erase scratch behind

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job_id": job["id"]})


def _kill_engines() -> None:
    """Kill EVERY subtitle-studio erase / ProPainter worker by command-line match.
    Reliable where killing a single tracked pid is not: the erase runs through a venv
    python shim AND an internal retry loop, so killing one worker lets the launcher
    respawn it. The GPU lock guarantees only one erase runs at a time, so a broad
    sweep can never hit a second legitimate job. Used by stop AND at startup (a server
    restart otherwise orphans a running worker that keeps hogging the GPU)."""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and "
             "($_.CommandLine -match 'subtitle-studio.erase_subs' -or "
             "$_.CommandLine -match 'subtitle-studio.lama_erase' -or "
             "($_.CommandLine -match 'inference_propainter' -and $_.CommandLine -match 'subtitle-studio')) } "
             "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
            capture_output=True, timeout=30)
    except Exception:
        pass


if __name__ == "__main__":
    FILES.mkdir(exist_ok=True)
    OUTPUT.mkdir(exist_ok=True)
    _kill_engines()
    freed = clean_temp(None, skip_busy=False)   # sweep scratch left by a prior crash/kill
    if freed:
        print(f"startup: reclaimed {freed/1e6:.0f} MB of leftover erase scratch")
    threading.Thread(target=_awake_keeper, daemon=True).start()
    # 5181 is Subtitle Studio's OWN port — 5180 kept getting taken by other sessions'
    # servers, which silently killed running jobs and served 404s in our UI
    port = int(os.environ.get("PORT", 5181))
    print(f"Subtitle Studio -> http://localhost:{port}  (local & free — no API keys, no fal.ai)")
    app.run(host="127.0.0.1", port=port, debug=False)
