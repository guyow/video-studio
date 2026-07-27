#!/usr/bin/env python3
"""Video Studio — unified local video pipeline (based on the autoVSL dashboard).

Run:  autoVSL\\.venv\\Scripts\\python.exe video-studio\\app\\server.py
Then open http://localhost:5181  (dev port; final home is 5180)

All machine paths come from video-studio/config.json. Engines stay in their
original projects (autoVSL/, subtitle-studio/, dubbing-studio/) and are called
via subprocess — nothing in those folders is modified.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

CLAUDE_EXE = shutil.which("claude") or next(
    (str(p) for p in (Path.home() / ".local/bin/claude.exe", Path.home() / ".local/bin/claude") if p.exists()),
    None,
)

COPY_PROMPT = """You are a direct-response copywriter for short-form video ads (VSLs and UGC-style testimonials).

Rewrite the script below according to the instruction. This is SPOKEN dialogue that will be \
voice-cloned and lip-synced onto existing footage, so:
- Write natural spoken language: contractions, short sentences. No headings, emojis, hashtags, stage directions, or quotation marks.
- LENGTH IS A HARD CONSTRAINT (the video length is fixed and the voice must fit it or the lip-sync breaks): {length_rule} Count your words and land inside the range — do not go over.
- Compliance: this is a wellness/supplement product. No disease or medical claims, no cure/treat/heal language, no guaranteed outcomes. Personal experience framing ("I felt...") is fine.
{context_block}{inspiration_block}
INSTRUCTION: {instruction}

SCRIPT TO REWRITE:
{text}

Respond with ONLY the rewritten script text — no preamble, no explanation, no markdown."""

from flask import (Flask, abort, jsonify, request, send_file, send_from_directory,
                   redirect, session)
import cleanup
from werkzeug.utils import secure_filename

APP_DIR = Path(__file__).resolve().parent          # video-studio/app
VS_ROOT = APP_DIR.parent                            # video-studio/
CONFIG = json.loads((VS_ROOT / "config.json").read_text(encoding="utf-8"))

ROOT = Path(CONFIG["autovsl_root"])                 # data root: the autoVSL repo (unchanged)
ENGINES = Path(CONFIG["engines_dir"])               # autoVSL/dashboard — engine helper scripts
# subtitle-studio's erase engine supersedes the dashboard copy: better caption-band
# detection (EasyOCR/CRAFT), NaN-corruption retry, and a resume cache written next to
# the output file so an interrupted erase picks up at the last finished chunk.
ERASE_PY = Path(CONFIG["engines"]["erase"])
# subtitle-studio's caption engine: faster-whisper word timing → editable lines.json →
# bold ASS captions burned over the (erased) band. Writes output/<stem>/ next to itself.
RECAPTION_PY = Path(CONFIG["engines"]["recaption"])
SUBSTUDIO_OUT = RECAPTION_PY.parent / "output"
LIPSYNC_PY = Path(CONFIG["engines"]["lipsync"])       # dubbing-studio Wav2Lip chain
DUB_VENV_PY = Path(CONFIG["venvs"]["dub"])
DUBSYNC_REPAIR_PY = APP_DIR / "engines" / "dubsync_repair.py"
VISUAL_REPAIR_PY = APP_DIR / "engines" / "visual_repair.py"
OBJECT_REPAIR_PY = APP_DIR / "engines" / "object_repair.py"
FRAME_SWAP_PY = APP_DIR / "engines" / "frame_swap.py"
# the ONE Desktop folder for finished deliverables (replaces the scattered
# "litt VSL's" / "liitt testimonial Ready" / "Subtitle Studio" folders going forward)
EXPORTS_DIR = Path(CONFIG["exports_dir"])

# Brand Content Studio (Coffee UI Studio): brand-locked social content
BRAND_KIT_PATH = Path(CONFIG.get("brand_kit", str(ROOT / "banks" / "liitt-brand-kit.json")))
BRAND_OUT = Path(CONFIG.get("brand_out", str(ROOT / "output" / "brand-content")))
BRAND_TEMPLATES = APP_DIR / "brand_templates"
BRAND_CONTENT_PY = APP_DIR / "engines" / "brand_content.py"
COMFY_SCRIPTS = ROOT / "scripts"
COMFY_HOST = CONFIG.get("comfyui", "127.0.0.1:8188")


def load_brand_kit() -> dict:
    try:
        return json.loads(BRAND_KIT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
STATIC = APP_DIR / "static"
UPLOADS = ROOT / "uploads"
TRANSCRIPTS = UPLOADS / "transcripts"
COURSE_PIPELINE = Path(CONFIG["course_pipeline"])
TRANSCRIBE_PY = COURSE_PIPELINE / "transcribe.py"
TRANSCRIBE_VENV_PY = Path(CONFIG["venvs"]["whisper"])
MEDIA_UPLOAD_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".mp3", ".m4a", ".wav"}
BASH = CONFIG["bash"]
FFMPEG_BIN = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1.2-full_build/bin"
)

app = Flask(__name__, static_folder=None)
app.secret_key = CONFIG.get("secret_key") or ""
if not app.secret_key or app.secret_key == "dev-only-change-me":
    # A remote PIN is only real if session cookies can't be forged, and cookies are
    # only signed by secret_key. With none configured, use a random per-process key
    # so the publicly-known default can't be used to forge a logged-in session.
    # (Sessions won't survive a restart — set a stable "secret_key" in config.json
    # to keep logins across restarts.)
    import secrets as _secrets
    app.secret_key = _secrets.token_hex(32)
    if CONFIG.get("remote_pin"):
        print("WARNING: remote_pin is set but no secret_key in config.json — using a "
              "random per-process key so logins can't be forged. Set a stable "
              "'secret_key' to keep sessions across restarts.", flush=True)

# ---------------------------------------------------------------- remote access lock
# The app spends money (fal.ai) and deletes files, so once it's reachable beyond this
# PC it MUST be behind a PIN. Desktop use (localhost) bypasses the gate entirely.
REMOTE_PIN = str(CONFIG.get("remote_pin") or "")
_AUTH_OPEN = {"/login", "/api/login", "/favicon.ico", "/api/ping"}


def _is_local(addr: str) -> bool:
    return addr in ("127.0.0.1", "::1", "localhost") or (addr or "").startswith("127.")


@app.before_request
def _require_pin():
    if not REMOTE_PIN:                          # no PIN configured → no gate
        return
    if _is_local(request.remote_addr or ""):    # this PC → never prompt
        return
    if request.path in _AUTH_OPEN or request.path.startswith("/static/"):
        return
    if session.get("vs_auth"):
        return
    if request.path.startswith("/api/"):
        abort(401)                               # API callers get 401, not a redirect
    return redirect("/login?next=" + request.path)


@app.get("/login")
def login_page():
    return send_from_directory(STATIC, "login.html")


@app.post("/api/login")
def api_login():
    pin = (request.get_json(force=True) or {}).get("pin", "")
    if REMOTE_PIN and str(pin) == REMOTE_PIN:
        session.permanent = True
        session["vs_auth"] = True
        return jsonify({"ok": True})
    abort(403, "wrong PIN")


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/ping")
def api_ping():
    return jsonify({"ok": True, "app": "video-studio"})


# ---------------------------------------------------------------- job runner

from jobs import (jobs, jobs_lock, GPU_LOCK, needs_gpu, wait_for_gpu,
                  acquire_gpu, init as jobs_init, create as jobs_create)

jobs_init()  # restore persisted jobs; start the flusher + keep-awake threads

ACTIONS = {
    "check-media":    {"script": "scripts/check-media.sh",    "label": "Check media"},
    "assemble":       {"script": "scripts/assemble-vsl.sh",   "label": "Assemble VSL"},
    "generate-vo":    {"script": "scripts/generate-vo.sh",    "label": "Generate VO (free)"},
    "generate-video": {"script": "scripts/generate-video.sh", "label": "Generate video (fal.ai, costs $)"},
    "print-prompts":  {"script": "scripts/print-prompts.sh",  "label": "Print prompts"},
    "list-models":    {"script": "scripts/generate-video.sh", "label": "List video models"},
    "transcribe":     {"label": "Transcribe (local whisper)"},
    "dub":            {"label": "Dub + Lip-sync (fal.ai, costs $)"},
    "caption":        {"label": "Burn captions (free, local)"},
    "recaption":      {"label": "New subtitles from original audio (free, local)"},
}
SWAP_WORK = ROOT / "output" / "script-swap"
TRASH = ROOT / ".trash"
DESKTOP_VSLS = Path.home() / "Desktop" / "litt VSL's"

# per-clip cost mirrors scripts/generate-video.py MODELS (for UI labels + validation)
VIDEO_MODELS = {
    "seedance-480p": 0.05, "seedance-720p": 0.11, "seedance-1080p": 0.24,
    "wan-5b-720p": 0.15, "wan-480p": 0.20, "hailuo-768p": 0.27,
    "wan-580p": 0.30, "kling-turbo": 0.35, "wan-720p": 0.40,
}
MANIFEST_STAGES = ["1_product_intake", "2_avatar_research", "3_angles",
                   "4_scripts", "5_shot_list", "6_generation"]
PRODUCT_DIRS = ["avatars", "angles", "scripts", "shot-lists", "stories"]


TRASH_INDEX = TRASH / "index.json"
READY_DIR = Path.home() / "Desktop" / "liitt testimonial Ready"

# ---------------------------------------------------------------- fal.ai cost tracking
# Estimated rates (verified against fal.ai 2026-07). Exact billing lives on fal's dashboard;
# these give a close running estimate so the user can track spend per run.
FAL_SPEND_FILE = ROOT / "output" / "fal_spend.json"
TTS_RATE_PER_1K = {"f5": 0.05, "turbo": 0.06, "hd": 0.10, "local": 0.0}   # USD / 1000 chars
MINIMAX_CLONE_FEE = 1.50                                                   # one-time per voice (turbo/hd)
LIPSYNC_RATE_PER_SEC = {"latentsync": 0.005, "musetalk": 0.005, "veed": 0.0067,
                        "standard": 0.05, "pro": 0.10, "none": 0.0, "wav2lip": 0.0,
                        "wav2lip-hd": 0.0}                                  # USD / second (wav2lip* = local, free; musetalk est.)
spend_lock = threading.Lock()


def load_spend() -> dict:
    return read_json(FAL_SPEND_FILE) or {"total": 0.0, "runs": [], "cloned_stems": {}}


def estimate_dub_cost(engine: str, tts: str, tier: str, video: Path, stem: str,
                      already_cloned: dict) -> dict:
    """Estimate this dub's fal.ai cost. Returns a breakdown dict (no side effects)."""
    dur = video_duration(ffprobe_json(video)) if video and video.is_file() else 0.0
    script = SWAP_WORK / stem / "script-edited.txt"
    chars = len(script.read_text(encoding="utf-8")) if script.is_file() else 0

    voice_cost = clone_cost = 0.0
    if engine == "fal":
        voice_cost = round(chars / 1000.0 * TTS_RATE_PER_1K.get(tts, 0.0), 4)
        if tts in ("turbo", "hd") and already_cloned.get(stem) != tts:
            clone_cost = MINIMAX_CLONE_FEE            # one-time voice clone for this stem+model
    lip = tier  # both engines use the tier name for the lip-sync step; local voice is free
    lipsync_cost = round(dur * LIPSYNC_RATE_PER_SEC.get(lip, 0.0), 4)

    total = round(voice_cost + clone_cost + lipsync_cost, 4)
    parts = []
    if engine == "local":
        parts.append("voice: local XTTS (free)")
    else:
        parts.append(f"voice {tts}: ${voice_cost:.3f} ({chars} chars)")
        if clone_cost:
            parts.append(f"one-time clone: ${clone_cost:.2f}")
    if lipsync_cost:
        parts.append(f"lip-sync {lip}: ${lipsync_cost:.3f} ({dur:.0f}s)")
    elif lip == "wav2lip-hd":
        parts.append("lip-sync: Wav2Lip HD + GFPGAN (local GPU, free)")
    elif lip == "wav2lip":
        parts.append("lip-sync: Wav2Lip (local GPU, free)")
    elif lip == "none":
        parts.append("lip-sync: none (free)")
    return {"this_run": total, "chars": chars, "duration": round(dur, 1),
            "tts": tts, "tier": lip, "engine": engine,
            "clone": clone_cost, "summary": " · ".join(parts)}


def record_spend(stem: str, info: dict) -> dict:
    """Append a run's cost to the ledger and return {this_run, total}."""
    with spend_lock:
        d = load_spend()
        d["total"] = round(float(d.get("total", 0.0)) + info["this_run"], 4)
        d.setdefault("runs", []).append(
            {"ts": time.time(), "stem": stem, "cost": info["this_run"],
             "summary": info["summary"], "engine": info["engine"]})
        d["runs"] = d["runs"][-100:]
        if info.get("clone"):
            d.setdefault("cloned_stems", {})[stem] = info["tts"]
        FAL_SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
        FAL_SPEND_FILE.write_text(json.dumps(d, indent=1), encoding="utf-8")
        return {"this_run": info["this_run"], "total": d["total"]}


def soft_delete(target: Path, label: str) -> str:
    """Move a file/folder into .trash (recording where it came from, for restore)."""
    TRASH.mkdir(exist_ok=True)
    original = str(target.relative_to(ROOT)).replace("\\", "/")
    name = f"{label}-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.move(str(target), str(TRASH / name))
    idx = read_json(TRASH_INDEX) or {}
    idx[name] = {"original": original, "deleted": time.time()}
    TRASH_INDEX.write_text(json.dumps(idx, indent=1), encoding="utf-8")
    return f".trash/{name}"


def trash_state() -> list[dict]:
    idx = read_json(TRASH_INDEX) or {}
    items = []
    for name, meta in sorted(idx.items(), key=lambda kv: kv[1].get("deleted", 0), reverse=True):
        p = TRASH / name
        if p.exists():
            items.append({"name": name, "original": meta.get("original", "?"),
                          "deleted": meta.get("deleted"), "is_dir": p.is_dir()})
    return items


def safe_output_path(rel: str) -> Path:
    """Resolve a repo-relative path, requiring an .mp4 inside output/."""
    target = (ROOT / rel.replace("\\", "/")).resolve()
    if not str(target).startswith(str(ROOT / "output")) or target.suffix != ".mp4":
        abort(400, "path must be an .mp4 under output/")
    return target


def job_env() -> dict:
    env = dict(os.environ)
    if FFMPEG_BIN.is_dir():
        env["PATH"] = str(FFMPEG_BIN) + os.pathsep + env.get("PATH", "")
    env["PYTHONUTF8"] = "1"
    return env


def run_job(job_id: str, cmd: list[str]) -> None:
    job = jobs[job_id]
    job["cmd"] = [str(c) for c in cmd]   # recorded so the job can be resumed
    gpu = job["gpu"] if "gpu" in job else needs_gpu(job["cmd"])
    got_gpu = False
    try:
        if gpu:
            wait_for_gpu(job)    # yield to subtitle-studio / the old dashboard
            acquire_gpu(job)     # one Video Studio GPU job at a time (4 GB card)
            got_gpu = True
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), env=job_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        job["pid"] = proc.pid
        for line in proc.stdout:
            with jobs_lock:
                job["lines"].append(line.rstrip("\n"))
        proc.wait()
        job["returncode"] = proc.returncode
        if job["status"] == "stopped":
            pass                       # user pressed Stop — keep that status, skip the failure paths
        elif proc.returncode == 0:
            job["status"] = "done"
        elif job["action"] == "check-media":
            job["status"] = "issues"  # check-media exits 1 when files are missing — a report, not a crash
        else:
            job["status"] = "failed"
            tail = "\n".join(job["lines"][-60:])
            if "Exhausted balance" in tail or "User is locked" in tail:
                with jobs_lock:
                    job["lines"].append("")
                    job["lines"].append(">>> fal.ai BALANCE IS EMPTY — nothing was charged for this run. <<<")
                    job["lines"].append(">>> Fix: top up at fal.ai/dashboard/billing, or create YOUR OWN key at fal.ai/dashboard/keys and replace FAL_KEY=... in autoVSL/.env <<<")
            elif "401" in tail and "fal" in tail.lower():
                with jobs_lock:
                    job["lines"].append(">>> fal.ai key rejected — check FAL_KEY in autoVSL/.env <<<")
    except Exception as exc:  # surface launcher errors in the log panel
        with jobs_lock:
            job["lines"].append(f"[dashboard] failed to run: {exc}")
        if job["status"] != "stopped":
            job["status"] = "failed"
        job["returncode"] = -1
    finally:
        if got_gpu:
            GPU_LOCK.release()
        job["ended"] = time.time()


@app.post("/api/job/<job_id>/stop")
def api_job_stop(job_id):
    """User-controlled cancel: kill the job's whole process tree and mark it stopped."""
    job = jobs.get(job_id)
    if not job:
        abort(404)
    if job["status"] != "running":
        abort(400, "job is not running")
    with jobs_lock:
        job["status"] = "stopped"
        job["lines"].append("⏹ stopped by user")
    pid = job.get("pid")
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    job["ended"] = time.time()
    job["returncode"] = -9
    return jsonify({"stopped": job_id})


@app.post("/api/job/<job_id>/resume")
def api_job_resume(job_id):
    """Re-run an interrupted/failed/stopped job with its recorded command.
    Engines with checkpoint caches (ProPainter erase, whisper words.json)
    pick up where they left off."""
    job = jobs.get(job_id)
    if not job:
        abort(404)
    if job["status"] not in ("interrupted", "failed", "stopped"):
        abort(400, "only interrupted/failed/stopped jobs can be resumed")
    cmd = job.get("cmd")
    if not cmd:
        abort(400, "this job predates resume support (no command recorded)")
    # money stays gated: these engines spend fal.ai credits on every run and blind
    # resume would both silently re-charge AND skip the spend ledger (resume goes
    # through run_job, not the run_*_job wrappers). Send them back through their own
    # tab's cost-confirmation flow instead. (local_dub.py is free — allowed.)
    cmd_str = " ".join(str(c) for c in cmd)
    is_paid_cloud = (
        ("dub.py" in cmd_str and "local_dub.py" not in cmd_str)  # fal dub
        or "duo_run.py" in cmd_str                                # fal interview dub
        or "i2v_gen.py" in cmd_str                                # fal image→video
        or "generate-video" in cmd_str                            # fal text→video
    )
    if is_paid_cloud:
        abort(400, "this job spends money on fal.ai — re-run it from its own tab so the "
                   "cost is confirmed, rather than blind-resuming (which would re-charge "
                   "and skip the spend ledger)")
    new_id = jobs_create(job.get("action"), job.get("slug"),
                         f"{job.get('label') or job.get('action') or 'job'} (resumed)",
                         resumed_from=job_id, lines=[f"▶ resuming job {job_id}"])
    threading.Thread(target=run_job, args=(new_id, cmd), daemon=True).start()
    return jsonify({"job_id": new_id, "resumed_from": job_id})


def run_dub_job(job_id: str, cmd: list[str], cost_ctx: dict) -> None:
    """Run a dub, then (if it spent money) append a cost line + update the running total."""
    run_job(job_id, cmd)
    job = jobs[job_id]
    if job["status"] != "done":
        return
    try:
        info = estimate_dub_cost(cost_ctx["engine"], cost_ctx["tts"], cost_ctx["tier"],
                                 Path(cost_ctx["video"]), cost_ctx["stem"],
                                 load_spend().get("cloned_stems", {}))
        if cost_ctx.get("paid") and info["this_run"] > 0:
            res = record_spend(cost_ctx["stem"], info)
            with jobs_lock:
                job["lines"].append("")
                job["lines"].append(f"💰 This dub cost ~${res['this_run']:.3f} on fal.ai  ({info['summary']})")
                job["lines"].append(f"🧾 Total spent on fal.ai so far: ${res['total']:.2f}")
            job["cost"] = {"this_run": res["this_run"], "total": res["total"], "summary": info["summary"]}
        else:
            with jobs_lock:
                job["lines"].append("")
                job["lines"].append(f"✅ This dub was FREE (local) — {info['summary']}")
            job["cost"] = {"this_run": 0.0, "total": load_spend().get("total", 0.0),
                           "summary": info["summary"], "free": True}
    except Exception as exc:
        with jobs_lock:
            job["lines"].append(f"(cost tracking skipped: {exc})")


@app.get("/api/spend")
def api_spend():
    d = load_spend()
    return jsonify({"total": round(float(d.get("total", 0.0)), 2),
                    "runs": list(reversed(d.get("runs", [])))[:20]})


@app.post("/api/run")
def api_run():
    body = request.get_json(force=True)
    action = body.get("action")
    slug = body.get("slug", "")
    if action not in ACTIONS:
        abort(400, "unknown action")
    # slug must be a plain name — it lands on a shell command line
    if slug and not slug.replace("-", "").replace("_", "").isalnum():
        abort(400, "bad slug")

    if action == "transcribe":
        fname = Path(body.get("file", "")).name
        src = UPLOADS / fname
        if not fname or not src.is_file():
            abort(400, "file not found in uploads/")
        if not TRANSCRIBE_VENV_PY.is_file():
            abort(500, f"transcribe venv missing: {TRANSCRIBE_VENV_PY}")
        cmd = [str(TRANSCRIBE_VENV_PY), str(TRANSCRIBE_PY), str(src), "--out", str(UPLOADS)]
        job_id = jobs_create(action, fname, f"Transcribe — {fname}")
        threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
        return jsonify({"job_id": job_id})

    if action == "caption":
        stem = Path(body.get("file", "")).name
        work = SWAP_WORK / stem
        if not stem or not (work / "final.mp4").is_file():
            abort(400, "no dubbed final for that video — dub first")
        if not (work / "new-vo.mp3").is_file():
            abort(400, "no VO in the work dir — re-run the dub")
        if not TRANSCRIBE_VENV_PY.is_file():
            abort(500, "transcribe venv missing (needed for word timing)")
        cmd = [str(TRANSCRIBE_VENV_PY), str(ENGINES / "caption.py"), "--name", stem]
        job_id = jobs_create(action, stem, f"Burn captions — {stem}")
        threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
        return jsonify({"job_id": job_id})

    if action == "recaption":
        fname = Path(body.get("file", "")).name
        src = UPLOADS / fname
        if not fname or not src.is_file():
            abort(400, "file not found in uploads/")
        if not TRANSCRIBE_VENV_PY.is_file():
            abort(500, "transcribe venv missing (needed for word timing)")
        cmd = [str(TRANSCRIBE_VENV_PY), str(ENGINES / "caption.py"),
               "--video", str(src)]
        job_id = jobs_create(action, src.stem, f"New subtitles — {fname} (from original audio)")
        threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
        return jsonify({"job_id": job_id})

    if action == "dub":
        fname = Path(body.get("file", "")).name
        src = UPLOADS / fname
        if not fname or not src.is_file():
            abort(400, "file not found in uploads/")
        stem = src.stem
        if not (SWAP_WORK / stem / "script-edited.txt").is_file():
            abort(400, "no edited script saved yet — use Edit script first")

        # one dub at a time — concurrent XTTS/Wav2Lip jobs OOM the 4GB GPU and all fail
        with jobs_lock:
            busy = next((j for j in jobs.values()
                         if j["action"] == "dub" and j["status"] == "running"), None)
        if busy:
            abort(409, f"a dub is already running ({busy['slug']}) — the GPU handles one at a "
                       "time; wait for it to finish, then start the next")

        engine = body.get("engine") if body.get("engine") in ("local", "fal") else "fal"
        venv_py = Path(CONFIG["venvs"]["cv"])

        if engine == "local":
            # local XTTS voice (free); lip-sync "none"/"wav2lip" are free (local GPU),
            # any fal tier costs money
            lipsync = body.get("lipsync") if body.get("lipsync") in (
                "none", "wav2lip", "wav2lip-hd", "latentsync", "musetalk",
                "veed", "standard", "pro") else "none"
            paid = lipsync in ("latentsync", "musetalk", "veed", "standard", "pro")
            if paid and not body.get("confirm_cost"):
                abort(400, f"lip-sync '{lipsync}' runs on fal.ai and costs money — needs cost approval (confirm_cost)")
            cmd = [str(venv_py), str(ENGINES / "local_dub.py"), str(src),
                   "--name", stem, "--lipsync", lipsync]
            lang = str(body.get("language") or "en")[:5]
            if lang.replace("-", "").isalpha():
                cmd += ["--language", lang]
            try:
                keepvol = max(0.0, min(1.0, float(body.get("keep_volume") or 0.0)))
            except (TypeError, ValueError):
                keepvol = 0.0
            if keepvol:
                cmd += ["--keep-volume", str(keepvol)]
            voice_label = "on-screen voice"
            vid = Path(str(body.get("voice_id") or "")).name
            if vid:
                ref = VOICES_DIR / vid / "ref.wav"
                if not ref.is_file():
                    abort(400, "chosen voice not found in the Voice Bank")
                cmd += ["--voice-ref", str(ref)]
                voice_label = f"voice:{_voice_meta(vid).get('name') or vid}"
            elif not has_audio_stream(src):
                # e.g. a silent Image→Video clip — there's no on-screen voice to clone
                have = (VOICES_DIR.is_dir()
                        and any((d / "ref.wav").is_file() for d in VOICES_DIR.iterdir() if d.is_dir()))
                abort(400, "This video has no audio to clone a voice from"
                           + (" — pick a saved voice in the “Voice” dropdown."
                              if have else ". Add a voice in the Voices tab first, then pick it here."))
            label = (f"Local dub — {fname} ({voice_label}"
                     + (", free)" if not paid else f" + {lipsync} lip-sync $)"))
            cost_ctx = {"engine": "local", "tts": "local", "tier": lipsync,
                        "video": str(src), "stem": stem, "paid": paid}
        else:
            # cloud pipeline clones the ON-SCREEN voice from the video's audio, so a silent
            # clip (e.g. an Image→Video result) can't be dubbed here — steer to local + bank
            if not has_audio_stream(src):
                abort(400, "This clip has no audio, and the cloud engine clones the on-screen "
                           "voice. To voice a silent clip, switch to the Local engine and pick a "
                           "saved voice from the Voice Bank.")
            # cloud pipeline: always costs money
            if not body.get("confirm_cost"):
                abort(400, "FAL.AI dub spends money (voice-clone + TTS + lip-sync) — needs cost approval (confirm_cost)")
            tier = body.get("tier") if body.get("tier") in ("pro", "standard", "veed", "latentsync", "musetalk") else "pro"
            tts = body.get("tts") if body.get("tts") in ("hd", "turbo", "f5") else "hd"
            cmd = [str(venv_py), str(ENGINES / "dub.py"), str(src),
                   "--name", stem, "--tier", tier, "--tts", tts]
            if body.get("captions", True):
                cmd.append("--captions")
            label = f"FAL.AI dub — {fname} (voice:{tts}, sync:{tier}) $"
            cost_ctx = {"engine": "fal", "tts": tts, "tier": tier,
                        "video": str(src), "stem": stem, "paid": True}

        job_id = jobs_create(action, stem, label)
        threading.Thread(target=run_dub_job, args=(job_id, cmd, cost_ctx), daemon=True).start()
        return jsonify({"job_id": job_id})

    cmd = [BASH, ACTIONS[action]["script"]]
    if action == "list-models":
        cmd.append("--list-models")
    else:
        cmd.append(slug or "fairy-flame")
    if action == "assemble" and body.get("no_music"):
        cmd.append("--no-music")
    if action == "generate-video":
        if not body.get("confirm_cost"):
            abort(400, "generate-video requires confirm_cost:true (this action spends real money)")
        shot = body.get("shot")
        if shot is not None:
            if not str(shot).isdigit():
                abort(400, "bad shot")
            cmd += ["--shot", str(shot)]
        model = body.get("video_model")
        if model:
            if model not in VIDEO_MODELS:
                abort(400, f"unknown video model: {model}")
            cmd += ["--model", model]

    job_id = jobs_create(action, slug,
                         f"{ACTIONS[action]['label']} — {slug}" if slug else ACTIONS[action]["label"])
    threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id})


DUB_STAGE_LABEL = {
    "local-voice": "voice clone (XTTS)", "clone": "voice clone", "speak": "speech",
    "lipsync": "lip-sync", "hd": "HD face restore (GFPGAN)", "mux": "finishing",
    "captions": "captions",
}


def job_progress(job: dict) -> dict | None:
    """Parse a live percentage + stage from a running job's output (tqdm + stage lines)."""
    if job["status"] != "running":
        return None
    stage = pct = frac = None
    for line in job["lines"]:
        m = re.search(r"=== stage:\s*([\w-]+)", line)
        if m:
            stage = m.group(1)
    for line in reversed(job["lines"][-40:]):          # newest tqdm reading
        m = re.search(r"(\d+)%\|", line)
        if m:
            pct = int(m.group(1))
            break
        m2 = re.search(r"\b(\d+)/(\d+)\b", line)
        if m2 and frac is None:
            a, b = int(m2.group(1)), int(m2.group(2))
            if b:
                frac = round(100 * a / b)
    p = pct if pct is not None else frac
    label = DUB_STAGE_LABEL.get(stage, stage or "working")
    return {"pct": p, "stage": stage, "label": label}


@app.get("/api/jobs")
def api_jobs():
    with jobs_lock:
        out = [
            {k: v for k, v in j.items() if k != "lines"}
            | {"line_count": len(j["lines"]), "progress": job_progress(j)}
            for j in sorted(jobs.values(), key=lambda j: j["started"], reverse=True)
        ]
    return jsonify(out)


@app.get("/api/job/<job_id>")
def api_job(job_id):
    job = jobs.get(job_id)
    if not job:
        abort(404)
    offset = int(request.args.get("offset", 0))
    with jobs_lock:
        lines = job["lines"][offset:]
        return jsonify({
            "id": job["id"], "label": job["label"], "status": job["status"],
            "returncode": job["returncode"], "lines": lines,
            "next_offset": offset + len(lines),
            "cost": job.get("cost"),
        })


# ---------------------------------------------------------------- transcribe

@app.post("/api/upload")
def api_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "no file")
    orig = Path(f.filename)
    ext = orig.suffix.lower()
    if ext not in MEDIA_UPLOAD_EXTS:
        abort(400, f"unsupported type — allowed: {', '.join(sorted(MEDIA_UPLOAD_EXTS))}")
    # secure_filename strips non-ASCII (e.g. Hebrew names) — fall back to a timestamp name
    base = secure_filename(orig.stem).strip(".-_") or f"upload-{time.strftime('%Y%m%d-%H%M%S')}"
    name, n = f"{base}{ext}", 2
    while (UPLOADS / name).exists():   # NEVER overwrite — an in-flight video keeps its pipeline
        name = f"{base}-{n}{ext}"
        n += 1
    UPLOADS.mkdir(exist_ok=True)
    f.save(UPLOADS / name)
    return jsonify({"name": name, "size": (UPLOADS / name).stat().st_size,
                    "renamed": name != orig.name})


@app.post("/api/transcript-to-product")
def api_transcript_to_product():
    body = request.get_json(force=True)
    stem = Path(body.get("stem", "")).name
    slug = body.get("slug", "")
    src = TRANSCRIPTS / f"{stem}.md"
    if not stem or not src.is_file():
        abort(404, "transcript not found")
    if not (ROOT / "products" / slug).is_dir() and not (ROOT / "research" / slug).is_dir():
        abort(400, f"unknown product slug: {slug}")
    dest = ROOT / "research" / slug / "transcripts" / f"{stem}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    rel = str(dest.relative_to(ROOT)).replace("\\", "/")
    return jsonify({"copied_to": rel})


def load_bank_entry(bank: str, entry_id: str) -> dict | None:
    path = ROOT / "banks" / f"{bank}.jsonl"
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    e = json.loads(line)
                    if e.get("id") == entry_id:
                        return e
                except json.JSONDecodeError:
                    pass
    return None


def inspiration_block(refs: list) -> str:
    """Format selected bank hooks/angles/scripts as prompt inspiration."""
    parts = []
    for ref in refs[:40]:
        t = ref.get("type")
        if t == "hook":
            e = load_bank_entry("hooks", ref.get("id", ""))
            if e:
                parts.append(f"[hook {e['id']} · {e.get('hook_class', '')}] \"{e.get('text_verbatim', '')}\""
                             + (f" — visual: {e['visual']}" if e.get("visual") else ""))
        elif t == "angle":
            e = load_bank_entry("angles", ref.get("id", ""))
            if e:
                parts.append(f"[angle {e['id']} · {e.get('name', '')}] {e.get('argument', '')[:500]}")
        elif t == "script":
            p = (ROOT / str(ref.get("path", ""))).resolve()
            if str(p).startswith(str(ROOT / "products")) and p.suffix == ".md" and p.is_file():
                parts.append(f"[script {p.name}]\n{p.read_text(encoding='utf-8', errors='replace')[:1500]}")
    if not parts:
        return ""
    return ("\nPROVEN INSPIRATION FROM THE RESEARCH BANKS (adapt their structure, energy, and psychology "
            "to THIS script's product and audience — never copy competitor brand names, product names, "
            "or specific claims verbatim):\n" + "\n\n".join(parts) + "\n")


def transcript_plain_text(stem: str) -> str:
    """Plain transcript text (no timestamps) from the local whisper .json sidecar."""
    sidecar = TRANSCRIPTS / f"{stem}.json"
    data = read_json(sidecar)
    if not data:
        return ""
    return " ".join(s["text"].strip() for s in data.get("segments", []) if s.get("text", "").strip())


@app.get("/api/script/<stem>")
def api_script_get(stem):
    stem = Path(stem).name
    edited = SWAP_WORK / stem / "script-edited.txt"
    if edited.is_file():
        return jsonify({"text": edited.read_text(encoding="utf-8"), "source": "edited"})
    text = transcript_plain_text(stem)
    return jsonify({"text": text, "source": "transcript" if text else "empty"})


@app.post("/api/script/<stem>")
def api_script_save(stem):
    stem = Path(stem).name
    text = (request.get_json(force=True).get("text") or "").strip()
    if not text:
        abort(400, "empty script")
    work = SWAP_WORK / stem
    work.mkdir(parents=True, exist_ok=True)
    (work / "script-edited.txt").write_text(text + "\n", encoding="utf-8")
    return jsonify({"saved": f"output/script-swap/{stem}/script-edited.txt", "chars": len(text)})


# ---------------------------------------------------------------- subtitle cleaner

def clean_subs_worker(job_id: str, fname: str, box: dict, mode: str) -> None:
    """Clean a burned-in subtitle region (OpenCV per-frame engine); original is backed up."""
    job = jobs[job_id]

    def log(line: str) -> None:
        with jobs_lock:
            job["lines"].append(line)

    got_gpu = False
    try:
        src = UPLOADS / fname
        probe = subprocess.run(
            [ff_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, check=True)
        vw, vh = (int(n) for n in probe.stdout.strip().split(",")[:2])
        tmp = UPLOADS / f"{src.stem}.cleaning{src.suffix}"
        venv_py = Path(CONFIG["venvs"]["cv"])
        if box is None:
            # one-click mode: erase_subs.py finds the caption band itself (ProPainter AI fill)
            x = y = w = h = None
            log(f"Auto-detecting the caption region of {vw}x{vh} — mode: erase (AI inpaint, audio untouched)")
            cmd = [str(venv_py), str(ERASE_PY), str(src), str(tmp)]
        elif mode == "erase":
            x = max(0, min(int(box["x"]), vw - 4))
            y = max(0, min(int(box["y"]), vh - 4))
            w = max(4, min(int(box["w"]), vw - x))
            h = max(4, min(int(box["h"]), vh - y))
            log(f"Cleaning {w}x{h} region at ({x},{y}) of {vw}x{vh} — mode: {mode} (audio untouched)")
            # AI video inpainting (ProPainter) — reconstructs the real background behind
            # the letters from neighboring frames; slow (GPU, minutes) but the best fill
            cmd = [str(venv_py), str(ERASE_PY), str(src), str(tmp),
                   "--x", str(x), "--y", str(y), "--w", str(w), "--h", str(h)]
        else:
            x = max(0, min(int(box["x"]), vw - 4))
            y = max(0, min(int(box["y"]), vh - 4))
            w = max(4, min(int(box["w"]), vw - x))
            h = max(4, min(int(box["h"]), vh - y))
            log(f"Cleaning {w}x{h} region at ({x},{y}) of {vw}x{vh} — mode: {mode} (audio untouched)")
            cmd = [str(venv_py), str(ENGINES / "subclean.py"), str(src),
                   "--box", str(x), str(y), str(w), str(h), "--mode", mode, "--out", str(tmp)]
        # The AI erase (ProPainter) runs on the GPU. Take the SAME lock every other
        # GPU job uses, so an erase can't launch on top of a running dub/caption/i2v
        # and OOM the 4 GB card (the "everything got stuck" gridlock). The CPU-only
        # smart/blur/bar cleaner doesn't need it.
        if box is None or mode == "erase":
            wait_for_gpu(job)    # yield to a foreign erase (subtitle-studio / old dashboard)
            acquire_gpu(job)     # one Video Studio GPU job at a time
            got_gpu = True
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=job_env(), bufsize=1)
        job["pid"] = proc.pid
        for line in proc.stdout:
            log(line.rstrip("\n"))
        proc.wait()
        if proc.returncode != 0 or not tmp.is_file():
            raise RuntimeError("subtitle cleaner failed — see log above")

        originals = UPLOADS / ".originals"
        originals.mkdir(exist_ok=True)
        if not (originals / fname).exists():
            shutil.move(str(src), str(originals / fname))
            log(f"Original backed up to uploads/.originals/{fname}")
        else:
            src.unlink()  # already have the first original — this was a re-clean
        # remember the cleaned band so captions can be burned exactly over it later
        if x is None:  # auto mode — recover the box erase_subs detected from its log
            with jobs_lock:
                joined = "\n".join(job["lines"])
            m = re.search(r"captions detected at \((\d+),(\d+)\) (\d+)x(\d+)", joined)
            if m:
                x, y, w, h = (int(g) for g in m.groups())
        if x is not None:
            (originals / f"{Path(fname).stem}.box.json").write_text(
                json.dumps({"x": x, "y": y, "w": w, "h": h, "vw": vw, "vh": vh, "mode": mode}),
                encoding="utf-8")
        tmp.rename(src)
        log("Source replaced with the cleaned video.")
        if (SWAP_WORK / src.stem / "final.mp4").is_file():
            log("NOTE: an existing dub used the old (subtitled) video — re-dub to refresh it.")
        job["returncode"] = 0
        job["status"] = "done"
    except Exception as exc:
        if job["status"] == "stopped":
            log("clean cancelled — the source video was NOT modified")
        else:
            log(f"CLEAN FAILED: {exc}")
            job["returncode"] = 1
            job["status"] = "failed"
        (UPLOADS / f"{Path(fname).stem}.cleaning{Path(fname).suffix}").unlink(missing_ok=True)
    finally:
        if got_gpu:
            GPU_LOCK.release()
        job["ended"] = time.time()


def _clean_request():
    body = request.get_json(force=True)
    fname = Path(body.get("file") or "").name
    src = UPLOADS / fname
    if not fname or not src.is_file():
        abort(404, "upload not found")
    if body.get("auto"):        # one-click: auto-detect the caption band, AI-erase it
        return body, fname, src, None, "erase"
    try:
        box = {k: int(body[k]) for k in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        abort(400, "need integer x/y/w/h box")
    if box["w"] < 4 or box["h"] < 4:
        abort(400, "box too small — drag a rectangle over the subtitles")
    mode = body.get("mode") if body.get("mode") in ("smart", "blur", "bar", "erase") else "smart"
    return body, fname, src, box, mode


@app.post("/api/clean-preview")
def api_clean_preview():
    """Render one processed frame so the user can judge the method BEFORE committing."""
    body, fname, src, box, mode = _clean_request()
    t = max(0.0, float(body.get("t") or 1.0))
    out = UPLOADS / f".preview-{Path(fname).stem}.jpg"
    venv_py = Path(CONFIG["venvs"]["cv"])
    if mode == "erase":
        # approximate single-frame preview (text mask + spatial inpaint) — the real run
        # uses ProPainter video inpainting, which fills noticeably better than this
        cmd = [str(venv_py), str(ERASE_PY), str(src), str(out),
               "--x", str(box["x"]), "--y", str(box["y"]),
               "--w", str(box["w"]), "--h", str(box["h"]), "--preview-at", str(t)]
    else:
        cmd = [str(venv_py), str(ENGINES / "subclean.py"), str(src),
               "--box", str(box["x"]), str(box["y"]), str(box["w"]), str(box["h"]),
               "--mode", mode, "--preview-at", str(t), "--out", str(out)]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=job_env(), timeout=90)
    if proc.returncode != 0 or not out.is_file():
        abort(500, f"preview failed: {(proc.stdout or '')[-200:]}")
    return send_file(out, mimetype="image/jpeg", max_age=0)


@app.post("/api/clean-subs")
def api_clean_subs():
    body, fname, src, box, mode = _clean_request()
    job_id = jobs_create("clean-subs", fname, f"Remove subtitles — {fname} ({mode})")
    threading.Thread(target=clean_subs_worker, args=(job_id, fname, box, mode), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.delete("/api/upload")
def api_upload_delete():
    """Remove a video and its whole pipeline (transcript, scripts, dubs) into .trash as one bundle."""
    fname = Path(request.args.get("file") or "").name
    src = UPLOADS / fname
    if not fname or not src.is_file():
        abort(404, "upload not found")
    stem = Path(fname).stem
    with jobs_lock:  # deleting mid-job rips the work dir out from under the pipeline
        for j in jobs.values():
            if j["status"] == "running" and j["slug"] in (fname, stem):
                abort(409, f"a {j['action']} job is still running on this video — wait for it to finish")
    TRASH.mkdir(exist_ok=True)
    bundle_name = f"upload-{secure_filename(stem) or 'video'}-{time.strftime('%Y%m%d-%H%M%S')}"
    bundle = TRASH / bundle_name
    bundle.mkdir()
    pieces = {
        "video": (src, f"uploads/{fname}"),
        "transcript-md": (TRANSCRIPTS / f"{stem}.md", f"uploads/transcripts/{stem}.md"),
        "transcript-json": (TRANSCRIPTS / f"{stem}.json", f"uploads/transcripts/{stem}.json"),
        "workdir": (SWAP_WORK / stem, f"output/script-swap/{stem}"),
        "original": (UPLOADS / ".originals" / fname, f"uploads/.originals/{fname}"),
        "boxjson": (UPLOADS / ".originals" / f"{stem}.box.json", f"uploads/.originals/{stem}.box.json"),
    }
    manifest = {}
    for key, (path, original) in pieces.items():
        if path.exists():
            shutil.move(str(path), str(bundle / key))
            manifest[key] = original
    (bundle / "bundle.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    idx = read_json(TRASH_INDEX) or {}
    idx[bundle_name] = {"original": f"uploads/{fname} (+ transcript & dub work)",
                        "deleted": time.time(), "bundle": True}
    TRASH_INDEX.write_text(json.dumps(idx, indent=1), encoding="utf-8")
    return jsonify({"moved_to": f".trash/{bundle_name}", "pieces": list(manifest)})


@app.post("/api/clean-restore")
def api_clean_restore():
    """Undo a subtitle clean — put the backed-up original back as the source."""
    fname = Path(request.get_json(force=True).get("file") or "").name
    backup = UPLOADS / ".originals" / fname
    if not fname or not backup.is_file():
        abort(404, "no backup for that video")
    src = UPLOADS / fname
    if src.exists():
        src.unlink()
    shutil.move(str(backup), str(src))
    (UPLOADS / ".originals" / f"{Path(fname).stem}.box.json").unlink(missing_ok=True)
    return jsonify({"restored": fname})


# ---------------------------------------------------------------- VSL builder

BUILD_PROMPT = """You are the VSL production designer for a direct-response ad factory. \
Turn the approved script below into a production package for a 9:16 vertical video ad.

Rules:
- Break the script into 6-10 sequential shots. Each shot gets ONE voiceover line (verbatim from \
the script where possible, lightly smoothed for speech) and ONE text-to-video prompt.
- Video prompts: cinematic, concrete, filmable moments matching the VO emotionally. Describe subject, \
setting, camera, light, mood. Vertical 9:16. Real-people UGC/documentary feel unless the script implies otherwise. \
No text overlays, no brand names, no logos in the prompts.
- Compliance: wellness product — prompts and VO must not show or claim medical outcomes.
- Ground tone and audience in the product/research context provided.

{context}

SCRIPT ({script_name}):
{script}

Respond with ONLY a JSON object (no markdown fences, no commentary):
{{"name": "<short vsl title>",
 "concept": "<2-3 sentence creative rationale>",
 "negative_prompt": "<comma-separated things to avoid in video gen>",
 "shots": [{{"id": 1, "vo_text": "<spoken line>", "prompt": "<video generation prompt>", "notes": "<edit note>"}}]}}"""


def build_vsl_worker(job_id: str, vsl_slug: str, product: str, script_rel: str,
                     doc_rels: list[str]) -> None:
    job = jobs[job_id]

    def log(line: str) -> None:
        with jobs_lock:
            job["lines"].append(line)

    try:
        script_path = ROOT / "products" / product / script_rel
        script = script_path.read_text(encoding="utf-8", errors="replace")[:8000]
        context_parts = []
        for rel in doc_rels[:6]:
            p = (ROOT / rel).resolve()
            if str(p).startswith(str(ROOT)) and p.suffix == ".md" and p.is_file():
                context_parts.append(f"--- {rel} ---\n" + p.read_text(encoding="utf-8", errors="replace")[:3500])
        offer = ROOT / "products" / product / "offer.md"
        if offer.is_file():
            context_parts.insert(0, "--- product offer ---\n" + offer.read_text(encoding="utf-8", errors="replace")[:2500])
        context = ("CONTEXT:\n" + "\n\n".join(context_parts)) if context_parts else "CONTEXT: (none provided)"

        log(f"Building VSL '{vsl_slug}' from {script_rel} with {len(context_parts)} context doc(s)")
        log("Asking Claude to design the shot list (this takes a minute or two)...")

        env = job_env()
        env.pop("CLAUDECODE", None)
        result = subprocess.run(
            [CLAUDE_EXE, "-p", "--model", "opus",
             "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch,Task"],
            input=BUILD_PROMPT.format(context=context, script_name=script_rel, script=script),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, cwd=str(ROOT), env=env,
        )
        out = (result.stdout or "").strip()
        if result.returncode != 0 or not out:
            raise RuntimeError(f"claude CLI failed (rc={result.returncode}): {(result.stderr or '')[:300]}")
        if out.startswith("```"):
            out = out.split("```")[1].lstrip("json").strip()
        start, end = out.find("{"), out.rfind("}")
        # scrub mojibake from Windows console decoding (em-dashes -> U+FFFD)
        plan = json.loads(out[start:end + 1].replace("�", "-"))
        shots = plan.get("shots") or []
        if not (3 <= len(shots) <= 14):
            raise RuntimeError(f"unexpected shot count: {len(shots)}")

        vdir = ROOT / "vsls" / vsl_slug
        (vdir / "media" / "video").mkdir(parents=True)
        (vdir / "media" / "audio").mkdir(parents=True)
        (vdir / "media" / "music").mkdir(parents=True)

        (vdir / "kling-shots.json").write_text(json.dumps({
            "settings": {"negative_prompt": plan.get("negative_prompt", ""), "aspect_ratio": "9:16"},
            "shots": [{"id": s["id"], "filename": f"shot-{s['id']:02d}.mp4", "prompt": s["prompt"]}
                      for s in shots],
        }, indent=2), encoding="utf-8")

        (vdir / "elevenlabs-vo.json").write_text(json.dumps({
            "voice": "en-US-ChristopherNeural", "voice_alternate": "en-US-GuyNeural",
            "lines": [{"id": s["id"], "filename": f"vo-{s['id']:02d}.mp3", "text": s["vo_text"]}
                      for s in shots],
        }, indent=2), encoding="utf-8")

        (vdir / "timeline.json").write_text(json.dumps({
            "name": plan.get("name", vsl_slug),
            "aspect_ratio": "9:16", "fps": 30,
            "target_duration_seconds": len(shots) * 8,
            "media_root": f"vsls/{vsl_slug}/media",
            "tracks": {"video": 0, "voiceover": 1, "music": 2},
            "music": {"file": "music/background.mp3", "volume": 0.15,
                      "fade_in_seconds": 2, "fade_out_seconds": 3,
                      "duck_under_vo": True, "duck_volume": 0.08},
            "segments": [{"shot": s["id"], "video": f"video/shot-{s['id']:02d}.mp4",
                          "vo": f"audio/vo-{s['id']:02d}.mp3", "vo_text": s["vo_text"],
                          "notes": s.get("notes", "")} for s in shots],
        }, indent=2), encoding="utf-8")

        (vdir / "brief.md").write_text(
            f"# {plan.get('name', vsl_slug)}\n\n{plan.get('concept', '')}\n\n"
            f"- **Product:** {product}\n- **Script:** {script_rel}\n"
            f"- **Context docs:** {', '.join(doc_rels) or 'none'}\n"
            f"- **Built:** {time.strftime('%Y-%m-%d %H:%M')}\n", encoding="utf-8")

        log(f"Wrote {len(shots)} shots -> vsls/{vsl_slug}/ (kling-shots, elevenlabs-vo, timeline, brief)")
        log("Next: Generate VO (free) -> Generate video ($) -> Assemble (free)")
        job["returncode"] = 0
        job["status"] = "done"
    except Exception as exc:
        log(f"BUILD FAILED: {exc}")
        job["returncode"] = 1
        job["status"] = "failed"
    finally:
        job["ended"] = time.time()


@app.post("/api/build-vsl")
def api_build_vsl():
    body = request.get_json(force=True)
    product = Path(body.get("product") or "").name
    script_rel = (body.get("script") or "").replace("\\", "/")
    docs = [d for d in (body.get("docs") or []) if isinstance(d, str)]
    vsl_slug = (body.get("name") or "").strip().lower().replace(" ", "-")
    if not valid_slug(product) or not (ROOT / "products" / product).is_dir():
        abort(400, "unknown product")
    script_path = (ROOT / "products" / product / script_rel).resolve()
    if not str(script_path).startswith(str(ROOT / "products" / product)) or not script_path.is_file():
        abort(400, "script not found")
    if not vsl_slug or not valid_slug(vsl_slug):
        abort(400, "VSL name must be letters/numbers/dashes")
    if (ROOT / "vsls" / vsl_slug).exists():
        abort(409, f"vsls/{vsl_slug} already exists — pick another name")
    if not CLAUDE_EXE:
        abort(500, "claude CLI not found")

    job_id = jobs_create("build-vsl", vsl_slug, f"Build VSL — {vsl_slug}")
    threading.Thread(target=build_vsl_worker,
                     args=(job_id, vsl_slug, product, script_rel, docs), daemon=True).start()
    return jsonify({"job_id": job_id})


# ---------------------------------------------------------------- products & research CRUD

def valid_slug(slug: str) -> bool:
    return bool(slug) and slug.replace("-", "").replace("_", "").isalnum() and slug == Path(slug).name


@app.post("/api/product")
def api_product_create():
    body = request.get_json(force=True)
    slug = (body.get("slug") or "").strip().lower()
    name = (body.get("name") or "").strip() or slug
    if not valid_slug(slug):
        abort(400, "slug must be letters/numbers/dashes only (e.g. night-mode)")
    pdir = ROOT / "products" / slug
    if pdir.exists():
        abort(409, f"product '{slug}' already exists")
    for d in PRODUCT_DIRS:
        (pdir / d).mkdir(parents=True)
    manifest = {
        "product": name, "slug": slug,
        "created": time.strftime("%Y-%m-%d"), "stage": 1,
        "stages": {key: {"status": "todo"} for key in MANIFEST_STAGES},
    }
    (pdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return jsonify({"created": slug})


@app.delete("/api/product/<slug>")
def api_product_delete(slug):
    slug = Path(slug).name
    pdir = ROOT / "products" / slug
    if not pdir.is_dir():
        abort(404)
    moved = soft_delete(pdir, f"product-{slug}")
    return jsonify({"moved_to": moved})


@app.post("/api/research-doc")
def api_research_doc_create():
    body = request.get_json(force=True)
    rel = (body.get("path") or "").strip().replace("\\", "/")
    content = body.get("content") or ""
    if not rel.endswith(".md"):
        rel += ".md"
    target = (ROOT / rel).resolve()
    if not str(target).startswith(str(ROOT / "research")):
        abort(400, "path must be under research/")
    if target.exists():
        abort(409, "that file already exists — pick another name")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content or f"# {target.stem}\n\n", encoding="utf-8")
    return jsonify({"created": str(target.relative_to(ROOT)).replace("\\", "/")})


@app.delete("/api/research-doc")
def api_research_doc_delete():
    rel = (request.args.get("path") or "").replace("\\", "/")
    target = (ROOT / rel).resolve()
    if not str(target).startswith(str(ROOT / "research")) or target.suffix != ".md":
        abort(400, "path must be a .md under research/")
    if not target.is_file():
        abort(404)
    moved = soft_delete(target, f"doc-{target.stem}")
    return jsonify({"moved_to": moved})


@app.post("/api/output-to-desktop")
def api_output_to_desktop():
    body = request.get_json(force=True)
    src = safe_output_path(body.get("path") or f"output/{Path(body.get('name') or '').name}")
    if not src.is_file():
        abort(404, "output video not found")
    # flatten dub paths: output/script-swap/<name>/final.mp4 -> <name>-final.mp4
    rel_parts = src.relative_to(ROOT / "output").parts
    flat = src.name if len(rel_parts) == 1 else f"{rel_parts[-2]}-{src.name}"
    DESKTOP_VSLS.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, DESKTOP_VSLS / flat)
    return jsonify({"saved_to": str(DESKTOP_VSLS / flat)})


@app.delete("/api/output")
def api_output_delete():
    target = safe_output_path(request.args.get("path") or "")
    if not target.is_file():
        abort(404)
    moved = soft_delete(target, f"render-{target.stem}")
    return jsonify({"moved_to": moved})


@app.post("/api/output-rename")
def api_output_rename():
    body = request.get_json(force=True)
    target = safe_output_path(body.get("path") or "")
    if not target.is_file():
        abort(404)
    new_name = secure_filename(Path(body.get("new_name") or "").name)
    if not new_name:
        abort(400, "bad name")
    if not new_name.endswith(".mp4"):
        new_name += ".mp4"
    dest = target.with_name(new_name)
    if dest.exists():
        abort(409, "a video with that name already exists")
    target.rename(dest)
    return jsonify({"renamed_to": str(dest.relative_to(ROOT)).replace("\\", "/")})


@app.post("/api/trash/restore")
def api_trash_restore():
    name = Path(request.get_json(force=True).get("name") or "").name
    idx = read_json(TRASH_INDEX) or {}
    meta = idx.get(name)
    src = TRASH / name
    if not meta or not src.exists():
        abort(404, "not found in trash")
    bundle_manifest = read_json(src / "bundle.json") if src.is_dir() else None
    if bundle_manifest:  # multi-piece upload bundle — put every piece back where it lived
        primary = bundle_manifest.get("video")
        if primary and (ROOT / primary).exists():
            abort(409, f"cannot restore — {primary} already exists")
        for key, original in bundle_manifest.items():
            piece = src / key
            dest = ROOT / original
            if piece.exists() and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(piece), str(dest))
        shutil.rmtree(src, ignore_errors=True)
        idx.pop(name, None)
        TRASH_INDEX.write_text(json.dumps(idx, indent=1), encoding="utf-8")
        return jsonify({"restored_to": primary or meta["original"]})

    dest = ROOT / meta["original"]
    if dest.exists():
        abort(409, f"cannot restore — {meta['original']} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    idx.pop(name, None)
    TRASH_INDEX.write_text(json.dumps(idx, indent=1), encoding="utf-8")
    return jsonify({"restored_to": meta["original"]})


@app.delete("/api/trash")
def api_trash_purge():
    name = Path(request.args.get("name") or "").name
    idx = read_json(TRASH_INDEX) or {}
    src = TRASH / name
    if name not in idx or not src.exists():
        abort(404, "not found in trash")
    if src.is_dir():
        shutil.rmtree(src)
    else:
        src.unlink()
    idx.pop(name, None)
    TRASH_INDEX.write_text(json.dumps(idx, indent=1), encoding="utf-8")
    return jsonify({"purged": name})


# ---------------------------------------------------------------- dev chat

CHAT_SYSTEM = (
    "You are the dev assistant embedded in the autoVSL dashboard, chatting with the project owner. "
    "The working directory is the autoVSL repo: a multi-agent VSL ad factory (research banks in banks/, "
    "product pipeline in products/, scripts+VSLs in vsls/, fal.ai+ffmpeg production engine in scripts/, "
    "dashboard in dashboard/, uploads+transcripts in uploads/). "
    "You have read-only access (Read/Grep/Glob) — you cannot edit files or run commands, so when asked to "
    "change something, explain exactly what to change or suggest doing it in a Claude Code session. "
    "Be concise and concrete; this renders in a small chat panel."
)

AGENT_NOTES = ROOT / "research" / "agent-notes.md"

RESEARCH_SYSTEM = (
    "You are the RESEARCH & BRAND STRATEGIST for a direct-response ad operation selling functional-mushroom "
    "wellness products (niches: mental-health healing, microdosing culture, brain fog, mood, focus). "
    "You chat with the founder, who spends real money on ads — precision matters.\n"
    "Your knowledge base (read these before answering anything substantive):\n"
    "- banks/hooks.jsonl and banks/angles.jsonl — every PROVEN hook and angle\n"
    "- research/ (all .md docs) — niche, avatar and brand research\n"
    "- products/*/offer.md — the brand offers\n"
    "- research/agent-notes.md — facts the founder has taught you; treat as ground truth\n"
    "What you do: find NEW niches, angles and hooks (grounded in the proven ones, never duplicates); "
    "critique or sharpen script ideas for conversion; answer brand questions precisely. "
    "When the founder teaches you product facts, restate them cleanly so they can be pinned. "
    "Always propose concrete, testable hooks/angles (label them H1/H2, A1/A2). Be concise — small chat panel. "
    "Compliance: wellness supplement — no disease/cure claims."
)

chats: dict[str, dict] = {}
chats_lock = threading.Lock()


@app.post("/api/agent-note")
def api_agent_note():
    """Pin a fact/idea from the research agent chat into its permanent memory."""
    text = (request.get_json(force=True).get("text") or "").strip()
    if not text:
        abort(400, "empty note")
    AGENT_NOTES.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    with open(AGENT_NOTES, "a", encoding="utf-8") as f:
        if AGENT_NOTES.stat().st_size == 0:
            f.write("# Agent notes — facts the founder taught the research agent\n")
        f.write(f"\n## {stamp}\n{text[:4000]}\n")
    return jsonify({"saved": str(AGENT_NOTES.relative_to(ROOT)).replace("\\", "/")})


def run_chat_turn(turn_id: str, message: str, session_id: str | None, model: str,
                  mode: str = "dev") -> None:
    chat = chats[turn_id]
    cmd = [
        CLAUDE_EXE, "-p", "--model", model,
        "--output-format", "stream-json", "--verbose",
        "--allowedTools", "Read,Grep,Glob",
        "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch,Task",
        "--append-system-prompt", RESEARCH_SYSTEM if mode == "research" else CHAT_SYSTEM,
    ]
    if session_id:
        cmd += ["--resume", session_id]
    env = job_env()
    env.pop("CLAUDECODE", None)
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        killer = threading.Timer(600, proc.kill)
        killer.start()
        proc.stdin.write(message)
        proc.stdin.close()
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            with chats_lock:
                t = ev.get("type")
                if t == "system" and ev.get("subtype") == "init":
                    chat["session_id"] = ev.get("session_id") or chat["session_id"]
                elif t == "assistant":
                    for block in (ev.get("message") or {}).get("content", []):
                        if block.get("type") == "text" and block.get("text", "").strip():
                            chat["events"].append({"kind": "text", "text": block["text"]})
                        elif block.get("type") == "tool_use":
                            inp = block.get("input") or {}
                            detail = inp.get("file_path") or inp.get("pattern") or inp.get("path") or ""
                            chat["events"].append({"kind": "tool", "text": f"{block.get('name')} {detail}".strip()})
                elif t == "result":
                    chat["session_id"] = ev.get("session_id") or chat["session_id"]
                    if ev.get("subtype") != "success":
                        chat["events"].append({"kind": "error", "text": str(ev.get("result") or "chat turn failed")})
        proc.wait()
        killer.cancel()
        stderr = proc.stderr.read()
        if proc.returncode != 0 and not any(e["kind"] == "text" for e in chat["events"]):
            with chats_lock:
                chat["events"].append({"kind": "error", "text": f"claude CLI failed (rc={proc.returncode}): {stderr[:300]}"})
        chat["status"] = "done"
    except Exception as exc:
        with chats_lock:
            chat["events"].append({"kind": "error", "text": f"chat error: {exc}"})
        chat["status"] = "done"


@app.post("/api/chat")
def api_chat():
    body = request.get_json(force=True)
    message = (body.get("message") or "").strip()
    session_id = body.get("session_id") or None
    model = body.get("model") if body.get("model") in ("sonnet", "opus", "haiku") else "sonnet"
    if not message:
        abort(400, "empty message")
    if not CLAUDE_EXE:
        abort(500, "claude CLI not found")
    turn_id = uuid.uuid4().hex[:8]
    chats[turn_id] = {"id": turn_id, "status": "running", "events": [],
                      "session_id": session_id, "started": time.time()}
    mode = "research" if body.get("mode") == "research" else "dev"
    threading.Thread(target=run_chat_turn, args=(turn_id, message, session_id, model, mode),
                     daemon=True).start()
    return jsonify({"turn_id": turn_id})


@app.get("/api/chat/<turn_id>")
def api_chat_poll(turn_id):
    chat = chats.get(turn_id)
    if not chat:
        abort(404)
    offset = int(request.args.get("offset", 0))
    with chats_lock:
        events = chat["events"][offset:]
        return jsonify({"status": chat["status"], "session_id": chat["session_id"],
                        "events": events, "next_offset": offset + len(events)})


@app.post("/api/copywrite")
def api_copywrite():
    """Rewrite a script with Claude (headless claude CLI — runs on the user's subscription)."""
    body = request.get_json(force=True)
    text = (body.get("text") or "").strip()
    instruction = (body.get("instruction") or "").strip() or "Punch this up: stronger hook, more vivid and concrete language, keep it authentic."
    slug = Path(body.get("slug") or "").name
    if not text:
        abort(400, "empty script")
    if not CLAUDE_EXE:
        abort(500, "claude CLI not found — install Claude Code or add it to PATH")

    context_block = ""
    offer = ROOT / "products" / slug / "offer.md" if slug else None
    if offer and offer.is_file():
        context_block = "\nPRODUCT CONTEXT (ground claims and voice in this):\n" + \
            offer.read_text(encoding="utf-8", errors="replace")[:2500] + "\n"

    refs = body.get("bank_refs") or []
    if body.get("brand"):
        # full research awareness: every hook + angle in the banks, plus the brand offer
        for bank, t in (("hooks", "hook"), ("angles", "angle")):
            p = ROOT / "banks" / f"{bank}.jsonl"
            if p.is_file():
                for line in p.read_text(encoding="utf-8").splitlines():
                    try:
                        refs.append({"type": t, "id": json.loads(line).get("id")})
                    except (json.JSONDecodeError, AttributeError):
                        pass
        if not context_block:
            for offer in sorted((ROOT / "products").glob("*/offer.md")):
                context_block = ("\nPRODUCT/BRAND CONTEXT (ground every claim and word choice in this):\n"
                                 + offer.read_text(encoding="utf-8", errors="replace")[:2500] + "\n")
                break
        instruction += ("\nThis is a paid AD with real budget behind it — optimize for conversion: "
                        "a scroll-stopping first line, one clear promise, concrete sensory language, "
                        "and a reason to keep watching. Use the proven hooks/angles as patterns, never verbatim.")
    # length target driven by the VIDEO duration AND the ORIGINAL speaker's pace, so the dub
    # both fits the footage and talks at the same speed as the person on screen
    orig = len(text.split())
    try:
        secs = float(body.get("target_seconds") or 0)
    except (TypeError, ValueError):
        secs = 0.0
    try:
        rate = float(body.get("rate") or 0)          # original words-per-second (measured)
    except (TypeError, ValueError):
        rate = 0.0
    rate = rate if 1.0 <= rate <= 5.0 else 2.5        # clamp to a sane speaking range
    if secs > 0:
        target = max(8, round(secs * rate))
        lo, hi = round(target * 0.9), round(target * 1.05)
        length_rule = (f"the video is {secs:.0f}s long and the ORIGINAL speaker talks at "
                       f"~{rate:.1f} words/sec — MATCH THAT PACE: write {lo}-{hi} words "
                       f"(target ~{target}) so the new voice runs at the same speed and fills the "
                       f"same time. Fewer is safe; going over makes the voice rush or overrun.")
    else:
        lo, hi = round(orig * 0.9), round(orig * 1.1)
        length_rule = f"match the original length: {lo}-{hi} words (original is {orig})."
    prompt = COPY_PROMPT.format(
        length_rule=length_rule, context_block=context_block,
        inspiration_block=inspiration_block(refs[:16]),   # enough pattern coverage; keeps rewrites fast
        instruction=instruction, text=text,
    )
    env = job_env()
    env.pop("CLAUDECODE", None)  # allow nested headless run from inside a Claude Code session
    try:
        result = subprocess.run(
            [CLAUDE_EXE, "-p", "--model", "opus",
             "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch"],
            input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=240, cwd=str(ROOT), env=env,
        )
    except subprocess.TimeoutExpired:
        abort(504, "Claude took too long — try again")
    out = (result.stdout or "").strip()
    if result.returncode != 0 or not out:
        abort(502, f"claude CLI failed (rc={result.returncode}): {(result.stderr or '')[:300]}")
    return jsonify({"text": out})


def upload_active_job(name: str, stem: str) -> dict | None:
    """Find a running/last-failed pipeline job for this upload (read-only job inspection)."""
    best = None
    with jobs_lock:
        for j in jobs.values():
            if j["action"] not in ("transcribe", "dub") or j["slug"] not in (name, stem):
                continue
            if best is None or j["started"] > best["started"]:
                best = j
        if not best:
            return None
        substage = None
        if best["action"] == "dub":
            for line in reversed(best["lines"]):
                if line.startswith("=== stage:"):
                    substage = line.replace("=== stage:", "").strip(" =")
                    break
        return {"id": best["id"], "action": best["action"], "status": best["status"],
                "started": best["started"], "ended": best["ended"], "substage": substage}


def uploads_state() -> list[dict]:
    items = []
    if UPLOADS.is_dir():
        for p in sorted(UPLOADS.iterdir()):
            if p.is_file() and p.suffix.lower() in MEDIA_UPLOAD_EXTS:
                md = TRANSCRIPTS / f"{p.stem}.md"
                work = SWAP_WORK / p.stem
                final = work / "final.mp4"
                script = work / "script-edited.txt"
                vo = work / "new-vo.mp3"
                items.append({
                    "name": p.name, "stem": p.stem,
                    "size": p.stat().st_size, "mtime": p.stat().st_mtime,
                    "is_video": p.suffix.lower() not in (".mp3", ".m4a", ".wav"),
                    "transcript": md.is_file(),
                    "transcript_path": f"uploads/transcripts/{p.stem}.md" if md.is_file() else None,
                    "transcript_mtime": md.stat().st_mtime if md.is_file() else None,
                    "script_edited": script.is_file(),
                    "script_mtime": script.stat().st_mtime if script.is_file() else None,
                    "voice_cloned": (work / "voice.json").is_file(),
                    "vo_ready": vo.is_file(),
                    "vo_mtime": vo.stat().st_mtime if vo.is_file() else None,
                    "dub_final": f"output/script-swap/{p.stem}/final.mp4" if final.is_file() else None,
                    "dub_mtime": final.stat().st_mtime if final.is_file() else None,
                    "dub_versions": dub_versions(p.stem),
                    "cleaned": (UPLOADS / ".originals" / p.name).is_file(),
                    "recaptioned": f"output/recaption/{p.stem}/captioned.mp4"
                                   if (ROOT / "output" / "recaption" / p.stem / "captioned.mp4").is_file() else None,
                    "recaptioned_stale": (ROOT / "output" / "recaption" / p.stem / "captioned.mp4").is_file()
                                         and p.stat().st_mtime > (ROOT / "output" / "recaption" / p.stem / "captioned.mp4").stat().st_mtime,
                    "captioned": f"output/script-swap/{p.stem}/final-captioned.mp4"
                                 if (work / "final-captioned.mp4").is_file() else None,
                    "captioned_stale": (work / "final-captioned.mp4").is_file() and final.is_file()
                                       and final.stat().st_mtime > (work / "final-captioned.mp4").stat().st_mtime,
                    "active": upload_active_job(p.name, p.stem),
                })
    return items


def dub_versions(stem: str) -> list[dict]:
    """All dub takes for an upload (current final + archived), with the models that made them."""
    work = SWAP_WORK / stem
    if not work.is_dir():
        return []
    versions_meta = read_json(work / "versions.json") or {}
    cfg = read_json(work / "dub-config.json") or {}
    out = []
    for f in sorted(work.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.name.startswith(("new-vo", "final-captioned")):
            continue   # not takes: VO audio-carrier / captioned derivative
        meta = cfg if f.name == "final.mp4" else versions_meta.get(f.name, {})
        out.append({
            "file": f.name,
            "path": f"output/script-swap/{stem}/{f.name}",
            "mtime": f.stat().st_mtime, "size": f.stat().st_size,
            "tts": meta.get("tts"), "tier": meta.get("tier"),
            "current": f.name == "final.mp4",
        })
    return out


@app.post("/api/dub-promote")
def api_dub_promote():
    """Crown an archived dub take as the current final (and refresh the Desktop deliverable)."""
    body = request.get_json(force=True)
    stem = Path(body.get("stem") or "").name
    fname = Path(body.get("file") or "").name
    work = SWAP_WORK / stem
    src = work / fname
    if (not stem or not fname or fname.startswith(("new-vo", "final-captioned"))
            or src.suffix != ".mp4" or not src.is_file()):
        abort(404, "version not found")
    final = work / "final.mp4"
    if fname == "final.mp4":
        return jsonify({"promoted": fname, "mtime": final.stat().st_mtime})

    versions = read_json(work / "versions.json") or {}
    cfg = read_json(work / "dub-config.json") or {}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if final.is_file():  # demote the current final into the version history
        arch = work / f"final.{stamp}.mp4"
        final.rename(arch)
        versions[arch.name] = {"tts": cfg.get("tts"), "tier": cfg.get("tier"),
                               "created": arch.stat().st_mtime}
    src.rename(final)
    meta = versions.pop(fname, {})
    if meta.get("tts") or meta.get("tier"):
        (work / "dub-config.json").write_text(
            json.dumps({"tts": meta.get("tts", "hd"), "tier": meta.get("tier", "pro")}),
            encoding="utf-8")
    (work / "versions.json").write_text(json.dumps(versions, indent=1), encoding="utf-8")
    READY_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(final, READY_DIR / f"{stem}-ready.mp4")   # deliverable follows the chosen take
    return jsonify({"promoted": fname, "mtime": final.stat().st_mtime})


# ---------------------------------------------------------------- state APIs

def read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def vsl_state(slug: str) -> dict:
    vsl_dir = ROOT / "vsls" / slug
    timeline = read_json(vsl_dir / "timeline.json") or {}
    media_root = ROOT / timeline.get("media_root", f"vsls/{slug}/media")
    segments = []
    for seg in timeline.get("segments", []):
        segments.append({
            "shot": seg.get("shot"),
            "vo_text": seg.get("vo_text", ""),
            "notes": seg.get("notes", ""),
            "video": seg.get("video"),
            "vo": seg.get("vo"),
            "video_ok": bool(seg.get("video")) and (media_root / seg["video"]).is_file(),
            "vo_ok": bool(seg.get("vo")) and (media_root / seg["vo"]).is_file(),
        })
    music_file = (timeline.get("music") or {}).get("file")
    output = ROOT / "output" / f"{slug}.mp4"
    return {
        "slug": slug,
        "name": timeline.get("name", slug),
        "media_root": timeline.get("media_root", f"vsls/{slug}/media"),
        "target_duration": timeline.get("target_duration_seconds"),
        "aspect_ratio": timeline.get("aspect_ratio"),
        "segments": segments,
        "music_ok": bool(music_file) and (media_root / music_file).is_file(),
        "music_file": music_file,
        "has_prompts": (vsl_dir / "kling-shots.json").is_file(),
        "output_exists": output.is_file(),
        "output_mtime": output.stat().st_mtime if output.is_file() else None,
        "output_size": output.stat().st_size if output.is_file() else None,
    }


@app.get("/api/overview")
def api_overview():
    products = []
    for mf in sorted((ROOT / "products").glob("*/manifest.json")):
        m = read_json(mf) or {}
        slug = m.get("slug", mf.parent.name)
        stages = []
        for key, st in (m.get("stages") or {}).items():
            stages.append({"key": key, "status": (st or {}).get("status", "?")})
        scripts = [
            {"file": s.get("file"), "status": s.get("status", "?"), "angle": s.get("angle")}
            for s in ((m.get("stages") or {}).get("4_scripts", {}) or {}).get("scripts", [])
        ]
        products.append({
            "slug": slug, "product": m.get("product", slug),
            "stage": m.get("stage"), "stages": stages, "scripts": scripts,
        })

    vsls = [vsl_state(p.name) for p in sorted((ROOT / "vsls").iterdir()) if (p / "timeline.json").is_file()]

    banks = {}
    for bank in ("hooks", "angles", "pain-points", "broll"):
        path = ROOT / "banks" / f"{bank}.jsonl"
        n = 0
        if path.is_file():
            with open(path, encoding="utf-8") as f:
                n = sum(1 for line in f if line.strip())
        banks[bank] = n

    outputs = []
    for p in sorted((ROOT / "output").glob("*.mp4")):
        outputs.append({
            "kind": "vsl", "group": None, "name": p.name,
            "path": f"output/{p.name}",
            "size": p.stat().st_size, "mtime": p.stat().st_mtime,
            "current": True, "on_desktop": (DESKTOP_VSLS / p.name).is_file(),
        })
    if SWAP_WORK.is_dir():
        for d in sorted(SWAP_WORK.iterdir()):
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
                current = f.name == "final.mp4"
                outputs.append({
                    "kind": "dub", "group": d.name, "name": f.name,
                    "path": f"output/script-swap/{d.name}/{f.name}",
                    "size": f.stat().st_size, "mtime": f.stat().st_mtime,
                    "current": current,
                    "on_desktop": (READY_DIR / f"{d.name}-ready.mp4").is_file() if current
                                  else (DESKTOP_VSLS / f"{d.name}-{f.name}").is_file(),
                })

    for p in sorted((ROOT / "output" / "edits").glob("*.mp4")) if (ROOT / "output" / "edits").is_dir() else []:
        outputs.append({"kind": "vsl", "group": None, "name": "✂ " + p.name,
                        "path": f"output/edits/{p.name}", "size": p.stat().st_size,
                        "mtime": p.stat().st_mtime, "current": True,
                        "on_desktop": (DESKTOP_VSLS / p.name).is_file()})

    research = sorted(
        str(p.relative_to(ROOT)).replace("\\", "/")
        for p in (ROOT / "research").rglob("*.md")
        if "raw" not in p.parts
    )

    return jsonify({"products": products, "vsls": vsls, "banks": banks,
                    "outputs": outputs, "research": research,
                    "uploads": uploads_state(), "trash": trash_state(),
                    "fal_spend": round(float(load_spend().get("total", 0.0)), 2)})


@app.get("/api/bank/<name>")
def api_bank(name):
    if name not in ("hooks", "angles", "pain-points", "broll"):
        abort(404)
    path = ROOT / "banks" / f"{name}.jsonl"
    entries = []
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return jsonify(entries)


@app.get("/api/file")
def api_file():
    """Return a text file from inside the repo (markdown/json/txt only)."""
    rel = request.args.get("path", "")
    target = (ROOT / rel).resolve()
    if not str(target).startswith(str(ROOT)) or target.suffix not in (".md", ".json", ".txt", ".jsonl"):
        abort(403)
    if not target.is_file():
        abort(404)
    return jsonify({"path": rel, "content": target.read_text(encoding="utf-8", errors="replace")})


@app.get("/media/<path:rel>")
def media(rel):
    target = (ROOT / rel).resolve()
    if not str(target).startswith(str(ROOT)) or target.suffix.lower() not in (
            ".mp4", ".mov", ".mkv", ".webm", ".m4v", ".mp3", ".m4a", ".wav", ".png", ".jpg", ".jpeg"):
        abort(403)
    if not target.is_file():
        abort(404)
    return send_file(target, conditional=True)  # conditional=True → Range support for <video>


# ---------------------------------------------------------------- quality control

QC_DIR = ROOT / "output" / "qc"
QC_REVIEWS = QC_DIR / "reviews.json"
QC_CACHE = QC_DIR / "cache"
NOSUBS_DIR = ROOT / "output" / "nosubs"
QC_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
qc_lock = threading.Lock()

QC_PROMPT = """You are a meticulous QC reviewer for AI-generated and AI-lip-synced direct-response video ads. \
These videos must look like real people filmed on a phone — a viewer noticing anything fake kills the ad.

Use the Read tool to view EVERY image listed below before answering.

Video: {rel}
Specs: {specs}

SPREAD frames (chronological, evenly spaced across the whole video):
{spread}

BURST frames (consecutive, ~0.12s apart, taken mid-speech — compare them to judge mouth articulation \
and lip-sync artifacts frame-to-frame):
{burst}

Assess harshly:
1. mouth — lip-sync artifact check: warped/blurry mouth or teeth, teeth smearing or changing shape, jaw \
morphing, a soft low-res "patch" around the mouth that mismatches the rest of the face, frozen or \
repeating mouth shapes across the burst frames, over-articulation.
2. realism — does the person look real: plastic/over-smooth skin, dead or misaligned eyes, hair edge \
artifacts, malformed hands/fingers, body proportions, background warping or objects morphing between \
frames, uncanny AI tells.
3. quality — technical: sharpness, compression blockiness, banding, ghosting, exposure/color shifts \
between frames, upscaling softness. Judge against the specs above.
4. text — burned-in subtitles/captions/watermarks/on-screen text: present or not, where (top/middle/bottom), \
and any garbled or misspelled AI-generated text.

Respond with ONLY a JSON object (no markdown fences, no commentary):
{{"mouth": {{"score": <1-10>, "issues": ["<specific issue + which frame>"]}},
 "realism": {{"score": <1-10>, "issues": []}},
 "quality": {{"score": <1-10>, "issues": []}},
 "text": {{"subtitles_present": true/false, "location": "<top|middle|bottom|none>", "issues": []}},
 "overall": {{"verdict": "pass"|"borderline"|"fail", "summary": "<2-3 sentences>", "fix_suggestions": ["<action>"]}}}}
Scores: 10 flawless · 8-9 minor nits · 6-7 visible on a close look · 4-5 obvious problems · 1-3 unusable. \
Note: you cannot hear audio, so judge lip-sync from visual mouth artifacts only — audio timing is checked by a human."""


def ff_tool(name: str) -> str:
    exe = FFMPEG_BIN / f"{name}.exe"
    return str(exe) if exe.is_file() else name


def safe_video_path(rel: str) -> Path:
    target = (ROOT / rel.replace("\\", "/")).resolve()
    if not str(target).startswith(str(ROOT)) or target.suffix.lower() not in QC_VIDEO_EXTS:
        abort(400, "path must be a video inside the repo")
    if not target.is_file():
        abort(404, "video not found")
    return target


def ffprobe_json(path: Path) -> dict:
    r = subprocess.run(
        [ff_tool("ffprobe"), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, env=job_env(),
    )
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def video_duration(probe: dict) -> float:
    try:
        return float((probe.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def has_audio_stream(path: Path) -> bool:
    """True if the file carries an audio track. A silent clip (e.g. an Image→Video
    result) still has a video stream but no audio → False. A probe FAILURE returns
    no streams at all → treat as unknown (True) so a transient ffprobe hiccup can't
    wrongly block a normal video from being dubbed."""
    streams = ffprobe_json(path).get("streams")
    if not streams:
        return True
    return any(s.get("codec_type") == "audio" for s in streams)


def qc_cache_dir(src: Path, tag: str) -> Path:
    rel = str(src.relative_to(ROOT))
    key = hashlib.md5(f"{rel}|{int(src.stat().st_mtime)}|{tag}".encode()).hexdigest()[:12]
    return QC_CACHE / key


def extract_spread_frames(src: Path, count: int) -> list[dict]:
    """Evenly spaced full frames -> [{path, t}], cached per file mtime."""
    probe = ffprobe_json(src)
    dur = video_duration(probe)
    if dur <= 0:
        return []
    outdir = qc_cache_dir(src, f"spread{count}")
    outdir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i in range(count):
        ts = dur * (i + 1) / (count + 1)
        out = outdir / f"f-{i + 1:02d}.jpg"
        if not out.is_file():
            subprocess.run(
                [ff_tool("ffmpeg"), "-y", "-ss", f"{ts:.3f}", "-i", str(src),
                 "-frames:v", "1", "-q:v", "3", str(out)],
                capture_output=True, timeout=120, env=job_env(),
            )
        if out.is_file():
            frames.append({"path": str(out.relative_to(ROOT)).replace("\\", "/"), "t": round(ts, 2)})
    return frames


def extract_burst_frames(src: Path, at: float, count: int = 6) -> list[str]:
    """Consecutive frames (fps=8) starting at `at` seconds — for mouth-articulation review."""
    outdir = qc_cache_dir(src, f"burst{count}@{at:.1f}")
    outdir.mkdir(parents=True, exist_ok=True)
    if not any(outdir.glob("b-*.jpg")):
        subprocess.run(
            [ff_tool("ffmpeg"), "-y", "-ss", f"{at:.3f}", "-i", str(src),
             "-vf", "fps=8", "-frames:v", str(count), "-q:v", "3", str(outdir / "b-%02d.jpg")],
            capture_output=True, timeout=120, env=job_env(),
        )
    return [str(p.relative_to(ROOT)).replace("\\", "/") for p in sorted(outdir.glob("b-*.jpg"))]


def qc_store() -> dict:
    return read_json(QC_REVIEWS) or {}


def qc_save(store: dict) -> None:
    QC_DIR.mkdir(parents=True, exist_ok=True)
    QC_REVIEWS.write_text(json.dumps(store, indent=1), encoding="utf-8")


@app.get("/api/qc/videos")
def api_qc_videos():
    vids = []

    def add(kind, group, p, models=""):
        vids.append({"kind": kind, "group": group, "name": p.name,
                     "path": str(p.relative_to(ROOT)).replace("\\", "/"),
                     "size": p.stat().st_size, "mtime": p.stat().st_mtime,
                     "models": models})

    if SWAP_WORK.is_dir():
        for d in sorted(SWAP_WORK.iterdir()):
            if d.is_dir():
                cfg = read_json(d / "dub-config.json") or {}
                models = " · ".join(x for x in (
                    ("voice: " + cfg["tts"]) if cfg.get("tts") else "",
                    ("lips: " + cfg["tier"]) if cfg.get("tier") else "",
                    ("engine: " + cfg["engine"]) if cfg.get("engine") else "") if x)
                for f in sorted(d.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
                    add("dub", d.name, f, models if f.name == "final.mp4" else "")
    for p in sorted((ROOT / "output").glob("*.mp4")):
        add("vsl", None, p)
    if NOSUBS_DIR.is_dir():
        for p in sorted(NOSUBS_DIR.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
            add("nosubs", None, p)
    if UPLOADS.is_dir():
        for p in sorted(UPLOADS.iterdir()):
            if p.is_file() and p.suffix.lower() in QC_VIDEO_EXTS:
                add("source", None, p)

    return jsonify({"videos": vids, "reviews": qc_store()})


@app.get("/api/qc/models")
def api_qc_models():
    """Scoreboard: how each voice+lip-sync combo actually performs, from QC reviews."""
    reviews = qc_store()
    combos: dict[str, dict] = {}
    if SWAP_WORK.is_dir():
        for d in SWAP_WORK.iterdir():
            if not d.is_dir():
                continue
            cfg = read_json(d / "dub-config.json") or {}
            if not cfg.get("tts"):
                continue
            combo = f"{cfg.get('tts')} + {cfg.get('tier', '?')}"
            rel = f"output/script-swap/{d.name}/final.mp4"
            r = reviews.get(rel) or {}
            c = combos.setdefault(combo, {"combo": combo, "videos": 0, "reviewed": 0,
                                          "passes": 0, "fails": 0, "scores": []})
            c["videos"] += 1
            ai = r.get("ai") or {}
            sc = [(ai.get(k) or {}).get("score") for k in ("mouth", "realism", "quality")]
            sc = [s for s in sc if isinstance(s, (int, float))]
            if sc:
                c["reviewed"] += 1
                c["scores"].append(sum(sc) / len(sc))
            if r.get("verdict") == "pass":
                c["passes"] += 1
            elif r.get("verdict") == "fail":
                c["fails"] += 1
    out = []
    for c in combos.values():
        avg = round(sum(c["scores"]) / len(c["scores"]), 1) if c["scores"] else None
        out.append({"combo": c["combo"], "videos": c["videos"], "reviewed": c["reviewed"],
                    "avg_score": avg, "passes": c["passes"], "fails": c["fails"]})
    out.sort(key=lambda x: (-(x["avg_score"] or 0), -x["videos"]))
    return jsonify(out)


@app.get("/api/qc/probe")
def api_qc_probe():
    src = safe_video_path(request.args.get("path", ""))
    return jsonify(ffprobe_json(src))


@app.get("/api/qc/frames")
def api_qc_frames():
    src = safe_video_path(request.args.get("path", ""))
    count = min(max(int(request.args.get("count", 10)), 4), 24)
    return jsonify({"frames": extract_spread_frames(src, count)})


@app.post("/api/qc/review")
def api_qc_review():
    body = request.get_json(force=True)
    src = safe_video_path(body.get("path", ""))
    rel = str(src.relative_to(ROOT)).replace("\\", "/")
    with qc_lock:
        store = qc_store()
        entry = store.get(rel) or {}
        entry["checks"] = {k: v for k, v in (body.get("checks") or {}).items()
                           if k in ("lip_sync", "dubbing", "quality", "realism") and v in ("pass", "fail", "na")}
        entry["notes"] = (body.get("notes") or "")[:2000]
        entry["verdict"] = body.get("verdict") if body.get("verdict") in ("pass", "fail", "pending") else "pending"
        entry["updated"] = time.time()
        store[rel] = entry
        qc_save(store)
    return jsonify({"saved": rel})


def qc_ai_worker(job_id: str, rel: str) -> None:
    job = jobs[job_id]

    def log(line: str) -> None:
        with jobs_lock:
            job["lines"].append(line)

    try:
        src = ROOT / rel
        probe = ffprobe_json(src)
        dur = video_duration(probe)
        v = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
        kbps = int(probe.get("format", {}).get("bit_rate") or 0) // 1000
        specs = (f"{v.get('width', '?')}x{v.get('height', '?')} · {v.get('r_frame_rate', '?')} fps · "
                 f"{dur:.1f}s · ~{kbps} kb/s total · codec {v.get('codec_name', '?')}")
        log(f"Probing done: {specs}")

        log("Extracting 6 spread frames + 6 burst frames (mid-speech)...")
        spread = extract_spread_frames(src, 6)
        burst = extract_burst_frames(src, max(0.0, dur * 0.4))
        if not spread:
            raise RuntimeError("could not extract frames (ffmpeg failed or zero duration)")

        prompt = QC_PROMPT.format(
            rel=rel, specs=specs,
            spread="\n".join(f"- {f['path']}  (t={f['t']}s)" for f in spread),
            burst="\n".join(f"- {p}" for p in burst) or "(none — video too short)",
        )
        log("Asking Claude to review the frames (takes a minute or two)...")
        env = job_env()
        env.pop("CLAUDECODE", None)
        result = subprocess.run(
            [CLAUDE_EXE, "-p", "--model", "opus", "--allowedTools", "Read",
             "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch,Task"],
            input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, cwd=str(ROOT), env=env,
        )
        out = (result.stdout or "").strip()
        if result.returncode != 0 or not out:
            raise RuntimeError(f"claude CLI failed (rc={result.returncode}): {(result.stderr or '')[:300]}")
        if out.startswith("```"):
            out = out.split("```")[1].lstrip("json").strip()
        start, end = out.find("{"), out.rfind("}")
        review = json.loads(out[start:end + 1].replace("�", "-"))

        review["reviewed"] = time.time()
        review["frames"] = [f["path"] for f in spread] + burst
        with qc_lock:
            store = qc_store()
            entry = store.get(rel) or {}
            entry["ai"] = review
            store[rel] = entry
            qc_save(store)

        o = review.get("overall") or {}
        log(f"Verdict: {str(o.get('verdict', '?')).upper()} — {o.get('summary', '')}")
        for k in ("mouth", "realism", "quality"):
            log(f"  {k}: {(review.get(k) or {}).get('score', '?')}/10")
        job["returncode"] = 0
        job["status"] = "done"
    except Exception as exc:
        log(f"AI REVIEW FAILED: {exc}")
        job["returncode"] = 1
        job["status"] = "failed"
    finally:
        job["ended"] = time.time()


@app.post("/api/qc/ai-review")
def api_qc_ai_review():
    src = safe_video_path(request.get_json(force=True).get("path", ""))
    if not CLAUDE_EXE:
        abort(500, "claude CLI not found")
    rel = str(src.relative_to(ROOT)).replace("\\", "/")
    job_id = jobs_create("qc-ai", src.stem, f"AI QC review — {src.name}")
    threading.Thread(target=qc_ai_worker, args=(job_id, rel), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.post("/api/qc/remove-subs")
def api_qc_remove_subs():
    """Clear subtitles: strip embedded tracks, or remove/blur a burned-in region, or crop the bottom."""
    body = request.get_json(force=True)
    src = safe_video_path(body.get("path", ""))
    method = body.get("method")
    if method not in ("strip", "erase", "delogo", "blur", "crop"):
        abort(400, "method must be strip | erase | delogo | blur | crop")

    NOSUBS_DIR.mkdir(parents=True, exist_ok=True)
    out = NOSUBS_DIR / f"{src.stem}-{method}.mp4"
    if out.exists():
        out = NOSUBS_DIR / f"{src.stem}-{method}-{time.strftime('%H%M%S')}.mp4"

    ffmpeg = ff_tool("ffmpeg")
    if method == "strip":
        cmd = [ffmpeg, "-y", "-i", str(src), "-map", "0", "-map", "-0:s?",
               "-map", "-0:d?", "-c", "copy", str(out)]
    elif method == "erase":
        # per-frame text detection + inpaint of only the letter pixels (erase_subs.py);
        # without a box the script auto-detects where the captions sit
        with jobs_lock:
            if any(j["action"] == "remove-subs" and j["status"] == "running" for j in jobs.values()):
                abort(409, "another subtitle-removal job is already running — the GPU can only handle "
                           "one at a time; wait for it to finish")
        venv_py = Path(CONFIG["venvs"]["cv"])
        cmd = [str(venv_py), str(ERASE_PY), str(src), str(out)]
        coords = [body.get(k) for k in ("x", "y", "w", "h")]
        if all(c is not None for c in coords):
            try:
                x, y, w, h = (int(c) for c in coords)
            except (TypeError, ValueError):
                abort(400, "x/y/w/h must be integers")
            if w < 8 or h < 8:
                abort(400, "search box too small — draw a bigger box, or use the automatic button")
            cmd += ["--x", str(x), "--y", str(y), "--w", str(w), "--h", str(h)]
    else:
        probe = ffprobe_json(src)
        v = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
        W, H = int(v.get("width") or 0), int(v.get("height") or 0)
        if not W or not H:
            abort(400, "could not read video dimensions")
        if method == "crop":
            bottom = int(body.get("bottom") or 0)
            if not (0 < bottom < H - 16):
                abort(400, f"bottom must be between 1 and {H - 16}")
            vf = f"crop=iw:ih-{bottom}:0:0"
        else:
            try:
                x, y, w, h = (int(body.get(k) or 0) for k in ("x", "y", "w", "h"))
            except (TypeError, ValueError):
                abort(400, "x/y/w/h must be integers")
            # clamp: delogo requires the box strictly inside the frame
            x = min(max(1, x), W - 12)
            y = min(max(1, y), H - 12)
            w = min(max(8, w), W - x - 2)
            h = min(max(8, h), H - y - 2)
            if method == "delogo":
                vf = f"delogo=x={x}:y={y}:w={w}:h={h}"
            else:
                r = max(2, min(w, h) // 4)
                vf = f"[0:v]crop={w}:{h}:{x}:{y},boxblur={r}:2[b];[0:v][b]overlay={x}:{y}"
        filt = ["-filter_complex", vf] if method == "blur" else ["-vf", vf]
        cmd = [ffmpeg, "-y", "-i", str(src), *filt,
               "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
               "-c:a", "copy", "-movflags", "+faststart", str(out)]

    job_id = jobs_create("remove-subs", src.stem, f"Clear subtitles ({method}) — {src.name}")
    threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id, "output": str(out.relative_to(ROOT)).replace("\\", "/")})


@app.post("/api/edit")
def api_edit():
    """Cut & zoom editor: trim [start,end] and optional center zoom -> a NEW file in output/edits/."""
    b = request.get_json(force=True)
    src = safe_video_path(b.get("path", ""))
    try:
        start, end = float(b.get("start") or 0), float(b.get("end") or 0)
        zoom = max(1.0, min(3.0, float(b.get("zoom") or 1)))
    except (TypeError, ValueError):
        abort(400, "start/end/zoom must be numbers")
    if end <= start:
        abort(400, "end must be after start")
    out_dir = ROOT / "output" / "edits"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem}-cut-{time.strftime('%H%M%S')}.mp4"
    cmd = [ff_tool("ffmpeg"), "-y", "-ss", str(start), "-to", str(end), "-i", str(src)]
    if zoom > 1:
        cmd += ["-vf", f"crop=iw/{zoom}:ih/{zoom}:(iw-iw/{zoom})/2:(ih-ih/{zoom})/2,scale=iw*{zoom}:ih*{zoom}"]
    cmd += ["-c:v", "libx264", "-crf", "16", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", str(out)]
    job_id = jobs_create("edit", src.stem,
                         f"✂ Edit — {src.name} ({start:.0f}-{end:.0f}s{', zoom ×' + str(zoom) if zoom > 1 else ''})")
    threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id, "output": str(out.relative_to(ROOT)).replace("\\", "/")})


# ---------------------------------------------------------------- creator library

LIBRARY_FILE = ROOT / "output" / "library.json"
library_lock = threading.Lock()
CREATOR_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def library_meta() -> dict:
    return read_json(LIBRARY_FILE) or {}


def qc_verdict_for(rel_path: str) -> str | None:
    """QC verdict (pass/fail/pending) recorded for a deliverable, if any."""
    r = (read_json(QC_REVIEWS) or {}).get(rel_path) or {}
    return r.get("verdict")


@app.get("/api/creator/library")
def api_creator_library():
    """Everything the Creator page needs in one call: videos + pipeline state + tags."""
    meta = library_meta()
    vids = []
    if UPLOADS.is_dir():
        for p in sorted(UPLOADS.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not p.is_file() or p.suffix.lower() not in CREATOR_VIDEO_EXTS:
                continue
            stem, work = p.stem, SWAP_WORK / p.stem
            final = work / "final.mp4"
            m = meta.get(p.name) or {}
            orig_words = len(transcript_plain_text(stem).split())  # original speaker's word count → pace
            captioned = ((work / "final-captioned.mp4").is_file()
                         or (SUBSTUDIO_OUT / stem / "captioned.mp4").is_file())
            vids.append({
                "name": p.name, "stem": stem, "size": p.stat().st_size, "mtime": p.stat().st_mtime,
                "orig_words": orig_words,
                "cleaned": (UPLOADS / ".originals" / p.name).is_file(),
                "transcript": (TRANSCRIPTS / f"{stem}.md").is_file(),
                "script": (work / "script-edited.txt").is_file(),
                "dub": f"output/script-swap/{stem}/final.mp4" if final.is_file() else None,
                "dub_mtime": final.stat().st_mtime if final.is_file() else None,
                "captioned": captioned,
                "exported": (work / ".exported").is_file(),
                "qc_verdict": qc_verdict_for(f"output/script-swap/{stem}/final.mp4"),
                "duo": (work / "duo-config.json").is_file(),
                "title": m.get("title") or "", "character": m.get("character") or "",
                "tags": m.get("tags") or [], "no_subs": bool(m.get("no_subs")),
                "approved": m.get("approved") or {},
            })
    return jsonify({
        "videos": vids,
        "characters": sorted({v["character"] for v in vids if v["character"]}),
        "tags": sorted({t for v in vids for t in v["tags"]}),
        "fal_spend": round(float(load_spend().get("total", 0.0)), 2),
    })


@app.post("/api/creator/meta")
def api_creator_meta():
    b = request.get_json(force=True)
    fname = Path(b.get("name") or "").name
    if not fname or not (UPLOADS / fname).is_file():
        abort(404, "upload not found")
    with library_lock:
        m = library_meta()
        e = m.get(fname) or {}
        if "title" in b:
            e["title"] = (b.get("title") or "").strip()[:80]
        if "character" in b:
            e["character"] = (b.get("character") or "").strip()[:40]
        if "tags" in b:
            e["tags"] = [t.strip()[:24] for t in (b.get("tags") or []) if isinstance(t, str) and t.strip()][:12]
        if "no_subs" in b:
            e["no_subs"] = bool(b.get("no_subs"))
        if "approved" in b and isinstance(b.get("approved"), dict):
            e["approved"] = {k: bool(v) for k, v in b["approved"].items()
                             if k in ("clean", "script", "dub")}
        m[fname] = e
        LIBRARY_FILE.parent.mkdir(parents=True, exist_ok=True)
        LIBRARY_FILE.write_text(json.dumps(m, indent=1), encoding="utf-8")
    return jsonify({"saved": fname})


@app.get("/api/thumb/<path:name>")
def api_thumb(name):
    """Cached poster frame for an upload (so the library shows real thumbnails)."""
    src = UPLOADS / Path(name).name
    if not src.is_file():
        abort(404)
    out = QC_CACHE / "thumbs" / f"{src.stem}-{int(src.stat().st_mtime)}.jpg"
    if not out.is_file():
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [ff_tool("ffmpeg"), "-y", "-loglevel", "error", "-ss", "1", "-i", str(src),
             "-frames:v", "1", "-vf", "scale=270:-2", "-q:v", "4", str(out)],
            env=job_env(), timeout=60)
    if not out.is_file():
        abort(500, "thumbnail failed")
    return send_file(out, mimetype="image/jpeg", max_age=3600)


# ------------------------------------------------ captions (subtitle-studio engine)

CAPTION_VIDEO_EXTS = QC_VIDEO_EXTS   # same accepted video types as QC


@app.post("/api/recaption")
def api_recaption():
    """Caption a video with subtitle-studio's recaption engine.
    mode: captions (whisper + burn) | burn-lines (re-burn edited lines.json) |
    cover (hide the old band, then captions) | no-captions (cover only)."""
    body = request.get_json(force=True)
    rel = (body.get("path") or "").replace("\\", "/")
    src = (ROOT / rel).resolve()
    if not str(src).startswith(str(ROOT)) or src.suffix.lower() not in CAPTION_VIDEO_EXTS:
        abort(400, "path must be a video inside the data root")
    if not src.is_file():
        abort(404, "video not found")
    mode = body.get("mode", "captions")
    flags = {"captions": [],
             "burn-lines": ["--burn-lines"],
             "cover": ["--cover", "--cover-style", body.get("style", "blur")],
             "no-captions": ["--no-captions"]}.get(mode)
    if flags is None:
        abort(400, "bad mode")
    if not TRANSCRIBE_VENV_PY.is_file():
        abort(500, f"whisper venv missing: {TRANSCRIBE_VENV_PY}")
    cmd = [str(TRANSCRIBE_VENV_PY), str(RECAPTION_PY), str(src)] + flags
    # only full caption runs transcribe; re-burn/cover are pure ffmpeg
    job_id = jobs_create("recaption", src.stem, f"Captions ({mode}) — {src.name}",
                         gpu=(mode == "captions"))
    threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.get("/api/captions/<stem>")
def api_captions_get(stem):
    stem = Path(stem).name
    work = SUBSTUDIO_OUT / stem
    cap = work / "captioned.mp4"
    return jsonify({"lines": read_json(work / "lines.json") or [],
                    "captioned": cap.is_file(),
                    "captioned_mtime": cap.stat().st_mtime if cap.is_file() else None})


@app.post("/api/captions/<stem>")
def api_captions_save(stem):
    stem = Path(stem).name
    lines = request.get_json(force=True).get("lines")
    if not isinstance(lines, list) or not lines:
        abort(400, "no lines")
    work = SUBSTUDIO_OUT / stem
    work.mkdir(parents=True, exist_ok=True)
    (work / "lines.json").write_text(json.dumps(lines, indent=1), encoding="utf-8")
    return jsonify({"saved": len(lines)})


@app.get("/captioned/<stem>")
def captioned_video(stem):
    """Serve the burned result (it lives in subtitle-studio's output/, outside ROOT)."""
    stem = Path(stem).name
    f = SUBSTUDIO_OUT / stem / "captioned.mp4"
    if not f.is_file():
        abort(404)
    return send_file(f, mimetype="video/mp4", conditional=True)


@app.post("/api/aifix/<stem>")
def api_aifix(stem):
    """Proofread caption lines with the local Claude CLI (free): fixes speech-to-text
    mishearings using context. Keeps line count/order/timing. (Ported from subtitle-studio.)"""
    stem = Path(stem).name
    body = request.get_json(force=True) or {}
    lines = body.get("lines")
    if not lines:
        lines = read_json(SUBSTUDIO_OUT / stem / "lines.json")
        if not lines:
            abort(404, "no captions to fix — caption the video first")
    if not CLAUDE_EXE:
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
        r = subprocess.run([CLAUDE_EXE, "-p", "--model", "haiku"], input=prompt,
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
        abort(500, f"AI returned {len(fixed) if isinstance(fixed, list) else '?'} lines, "
                   f"expected {len(lines)} — try again")
    changed = sum(1 for a, b in zip(texts, fixed) if str(a).strip() != str(b).strip())
    new_lines = [{**ln, "text": str(fixed[k])} for k, ln in enumerate(lines)]
    (SUBSTUDIO_OUT / stem).mkdir(parents=True, exist_ok=True)
    (SUBSTUDIO_OUT / stem / "lines.json").write_text(
        json.dumps(new_lines, indent=1), encoding="utf-8")
    return jsonify({"lines": new_lines, "changed": changed})


# ------------------------------------------------ DubSync Repair (Phase 4, net-new)

@app.get("/api/dubs")
def api_dubs():
    """Every dub workdir with its provenance + version history."""
    dubs = []
    if SWAP_WORK.is_dir():
        for d in sorted(SWAP_WORK.iterdir()):
            final = d / "final.mp4"
            if not d.is_dir() or not final.is_file():
                continue
            src_txt = d / "source.txt"
            source = src_txt.read_text(encoding="utf-8").strip() if src_txt.is_file() else ""
            versions = read_json(d / "versions.json") or {}
            takes = []
            for p in sorted(d.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
                if p.name.startswith(("new-vo", "final-captioned")) or p.name == "final.mp4":
                    continue
                if p.stat().st_size == 0:      # failed/aborted runs leave empty files
                    continue
                takes.append({"file": p.name, "mtime": p.stat().st_mtime,
                              "size": p.stat().st_size,
                              "repair": (versions.get(p.name) or {}).get("repair")})
            dubs.append({
                "stem": d.name,
                "final_mtime": final.stat().st_mtime,
                "final_size": final.stat().st_size,
                "has_vo": (d / "new-vo.mp3").is_file(),
                "has_source": bool(source) and Path(source).is_file(),
                "takes": takes[:12],
            })
    dubs.sort(key=lambda x: x["final_mtime"], reverse=True)
    return jsonify({"dubs": dubs})


@app.post("/api/dubsync/repair")
def api_dubsync_repair():
    """Fix a finished dub without re-dubbing: remux | refit | renorm | relipsync.
    Everything is local and free; output is a new versioned take (final.mp4 untouched)."""
    body = request.get_json(force=True)
    stem = Path(body.get("stem") or "").name
    action = body.get("action")
    if action not in ("remux", "refit", "renorm", "relipsync", "visual", "object", "swap"):
        abort(400, "bad action")
    work = SWAP_WORK / stem
    if not stem or not (work / "final.mp4").is_file():
        abort(404, "no such dub")
    venv_py = Path(CONFIG["venvs"]["cv"])
    if action == "swap":
        ranges = body.get("ranges") or []
        parts = []
        for rr in ranges:
            try:
                s0, s1 = float(rr["start"]), float(rr["end"])
            except (KeyError, TypeError, ValueError):
                abort(400, "each range needs numeric start/end seconds")
            if s1 <= s0:
                abort(400, f"range {s0:.2f}-{s1:.2f}: end must be after start")
            parts.append(f"{s0:.3f}-{s1:.3f}")
        if not parts:
            abort(400, "mark at least one time range first")
        cmd = [str(venv_py), str(FRAME_SWAP_PY), "--work", str(work),
               "--ranges", ",".join(parts)]
        if body.get("fade"):
            cmd += ["--fade", str(int(body["fade"]))]
    elif action == "object":
        samples = body.get("samples") or []
        if not any(s.get("obj") for s in samples):
            abort(400, "object repair needs at least one object box — describe the object in the chat first")
        rois = work / "object-rois.json"
        rois.write_text(json.dumps({"samples": samples}, indent=1), encoding="utf-8")
        cmd = [str(venv_py), str(OBJECT_REPAIR_PY), "--work", str(work), "--rois", str(rois)]
        if body.get("thresh"):
            cmd += ["--thresh", str(float(body["thresh"]))]
        if body.get("color_fix"):
            cmd.append("--color-fix")
    elif action == "visual":
        box = body.get("box") or {}
        try:
            bx, by, bw, bh = (int(box[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            abort(400, "visual repair needs box {x,y,w,h} over the lips")
        if bw < 16 or bh < 16:
            abort(400, "protected box is too small")
        cmd = [str(venv_py), str(VISUAL_REPAIR_PY), "--work", str(work),
               "--box", str(bx), str(by), str(bw), str(bh)]
        if not body.get("color_fix", True):
            cmd.append("--no-color-fix")
        if body.get("track"):
            cmd.append("--track")
        if body.get("encoder") == "nvenc":
            cmd += ["--encoder", "nvenc"]
    else:
        cmd = [str(venv_py), str(DUBSYNC_REPAIR_PY), action, "--work", str(work)]
    if action == "relipsync":
        restorer = body.get("restorer", "gfpgan")
        if restorer not in ("gfpgan", "codeformer", "none"):
            abort(400, "bad restorer")
        if not DUB_VENV_PY.is_file():
            abort(500, f"dubbing-studio venv missing: {DUB_VENV_PY}")
        cmd += ["--restorer", restorer, "--upscale", str(int(body.get("upscale") or 1)),
                "--fidelity", str(float(body.get("fidelity") or 0.7)),
                "--dub-python", str(DUB_VENV_PY), "--lipsync", str(LIPSYNC_PY)]
    labels = {"remux": "Re-mux voice", "refit": "Re-fit voice length",
              "renorm": "Re-normalize loudness", "relipsync": "Re-run lip-sync",
              "visual": "Fix visuals from original",
              "object": "Fix damaged object (keep the dub)",
              "swap": "Swap marked moments with original"}
    job_id = jobs_create("dubsync-repair", stem, f"DubSync Repair ({labels[action]}) — {stem}",
                         gpu=(action == "relipsync"))   # only Wav2Lip needs the GPU
    threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.post("/api/dubsync/visual-preview")
def api_dubsync_visual_preview():
    """Synchronous single-frame preview of the visual repair: ORIGINAL | DUBBED |
    REPAIRED | DIFF strip, so the user can check box placement + alignment first."""
    body = request.get_json(force=True)
    stem = Path(body.get("stem") or "").name
    work = SWAP_WORK / stem
    if not stem or not (work / "final.mp4").is_file():
        abort(404, "no such dub")
    box = body.get("box") or {}
    try:
        bx, by, bw, bh = (int(box[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        abort(400, "needs box {x,y,w,h}")
    try:
        at = float(body.get("at") or 1.0)
    except (TypeError, ValueError):
        at = 1.0
    cmd = [str(Path(CONFIG["venvs"]["cv"])), str(VISUAL_REPAIR_PY), "--work", str(work),
           "--box", str(bx), str(by), str(bw), str(bh), "--preview-at", f"{at:.3f}"]
    if not body.get("color_fix", True):
        cmd.append("--no-color-fix")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=90, env=job_env())
    except subprocess.TimeoutExpired:
        abort(504, "preview took too long")
    out = (r.stdout or "")
    if r.returncode != 0:
        err = next((ln for ln in out.splitlines() if ln.startswith("ERROR:")), out[-300:])
        abort(500, err)
    align = {}
    for ln in out.splitlines():
        if ln.startswith("ALIGN:"):
            try:
                align = json.loads(ln[6:].strip())
            except json.JSONDecodeError:
                pass
    warn = next((ln for ln in out.splitlines() if ln.startswith("WARNING:")), None)
    return jsonify({"img": f"/media/output/script-swap/{stem}/preview-visual.jpg",
                    "align": align, "warning": warn, "ts": time.time()})


ADVISE_PROMPT = """You are the repair advisor inside a local video tool. A user dubbed a video \
with AI lip-sync and something looks wrong. You decide which repair to run and locate things \
in the frames, so the tool can fix the video with zero drawing from the user.

First, Read these {n_frames} image files — frames from the DUBBED video (each {iw}x{ih} pixels):
{frame_list}

Available repairs:
- "object"    THE DEFAULT when the complaint names a specific thing that got warped/deformed \
(a cup, glasses, a hand, jewelry...). Keeps the dub and its lip-sync 100% untouched and restores \
ONLY that object's damaged pixels from the original video. Needs a box around the OBJECT in \
every frame where it is visible.
- "visual"    Restore every pixel from the ORIGINAL video except the lip region. Use only when \
the damage is broad (background/whole face) — it can disturb the lip-sync elsewhere.
- "relipsync" Redo the mouth movement with local Wav2Lip (for a bad/unsynced mouth itself). \
{relipsync_ok}
- "refit"     Time-stretch the voice to end exactly with the video (audio drift/overrun). {vo_ok}
- "remux"     Put the voice back onto the untouched original video (no lip animation at all). {vo_ok}
- "renorm"    Fix loudness (too quiet / too hot).

User's complaint (may be empty → just locate the lips and default to "visual"):
{complaint}

Reply with ONLY this JSON, no other text:
{{"action": "object|visual|relipsync|refit|remux|renorm",
  "boxes": [{{"x":..,"y":..,"w":..,"h":..}} or null, ...one per frame, the LIPS+CHIN...],
  "object_boxes": [{{"x":..,"y":..,"w":..,"h":..}} or null, ...one per frame, the NAMED OBJECT...],
  "track": true/false,
  "explanation": "1-2 friendly sentences telling the user what you found and what you'll do"}}

boxes = a tight pixel box around the speaker's LIPS + CHIN in each frame (mouth area only, not \
the whole face; null if no face). object_boxes = a tight box around the object the user named \
(null per frame where it isn't visible; use null for ALL frames if no object was named). \
All coordinates in the {iw}x{ih} pixels of these images. \
track = true if the speaker's mouth is at clearly different positions across the frames."""


ADVISE_FRACS = (0.08, 0.22, 0.36, 0.50, 0.64, 0.78, 0.92)


def _advise_frames(work: Path, final: Path):
    """Extract sample frames (≤960px tall) for the vision call.
    Returns (paths, frame_indices, w, h, scale)."""
    pr = subprocess.run([str(FFMPEG_BIN / "ffprobe.exe") if (FFMPEG_BIN / "ffprobe.exe").is_file() else "ffprobe",
                         "-v", "error", "-show_entries", "format=duration",
                         "-select_streams", "v:0",
                         "-show_entries", "stream=width,height,r_frame_rate",
                         "-of", "json", str(final)],
                        capture_output=True, text=True, env=job_env())
    d = json.loads(pr.stdout or "{}")
    W = int(d["streams"][0]["width"]); H = int(d["streams"][0]["height"])
    num, den = (int(x) for x in d["streams"][0]["r_frame_rate"].split("/"))
    fps = num / max(1, den)
    dur = float(d["format"].get("duration") or 10)
    scale = min(1.0, 960.0 / H)
    iw, ih = int(W * scale) // 2 * 2, int(H * scale) // 2 * 2
    paths, indices = [], []
    for i, frac in enumerate(ADVISE_FRACS):
        t = dur * frac
        p = work / f"advise-{i + 1}.jpg"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}",
                        "-i", str(final), "-frames:v", "1", "-vf", f"scale={iw}:{ih}", str(p)],
                       capture_output=True, env=job_env())
        if p.is_file():
            paths.append(p)
            indices.append(int(round(t * fps)))
    return paths, indices, iw, ih, scale


@app.post("/api/dubsync/advise")
def api_dubsync_advise():
    """The no-drawing flow: the user describes what's wrong (or nothing at all);
    local Claude LOOKS at sample frames, locates the lips, picks the repair, and
    the server returns a ready-to-run config + a preview strip."""
    body = request.get_json(force=True)
    stem = Path(body.get("stem") or "").name
    work = SWAP_WORK / stem
    final = work / "final.mp4"
    if not stem or not final.is_file():
        abort(404, "no such dub")
    if not CLAUDE_EXE:
        abort(500, "local Claude CLI not found — describe-and-fix needs it (draw the box instead)")
    complaint = (body.get("text") or "").strip() or "(none — locate the lips, default to visual)"

    frames, indices, iw, ih, scale = _advise_frames(work, final)
    if not frames:
        abort(500, "could not extract sample frames")
    has_vo = (work / "new-vo.mp3").is_file()
    src_txt = work / "source.txt"
    has_source = src_txt.is_file() and Path(src_txt.read_text(encoding="utf-8").strip()).is_file()
    prompt = ADVISE_PROMPT.format(
        iw=iw, ih=ih, n_frames=len(frames),
        frame_list="\n".join(f"  {p}" for p in frames),
        relipsync_ok="" if has_vo and has_source else "(NOT available for this dub)",
        vo_ok="" if has_vo and has_source else "(NOT available for this dub)",
        complaint=complaint)
    env = job_env()
    env.pop("CLAUDECODE", None)
    try:
        r = subprocess.run([CLAUDE_EXE, "-p", "--model", "sonnet", "--allowedTools", "Read"],
                           input=prompt, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180, cwd=str(work), env=env)
    except subprocess.TimeoutExpired:
        abort(504, "the advisor took too long — try again")
    out = r.stdout or ""
    i, j2 = out.find("{"), out.rfind("}")
    if r.returncode != 0 or i < 0 or j2 <= i:
        abort(502, f"advisor failed: {(r.stderr or out)[:300]}")
    try:
        plan = json.loads(out[i:j2 + 1])
    except json.JSONDecodeError:
        abort(502, "advisor reply was not valid JSON — try again")

    action = plan.get("action") if plan.get("action") in (
        "object", "visual", "relipsync", "refit", "remux", "renorm") else "visual"
    if action in ("relipsync", "refit", "remux") and not (has_vo and has_source):
        action = "visual" if has_source else "renorm"
    if action == "object" and not has_source:
        action = "renorm"

    obj_raw = plan.get("object_boxes") or []
    if action == "object":
        if not any(isinstance(b, dict) for b in obj_raw):
            action = "visual"       # nothing located → fall back to the broad repair
    boxes = [b for b in (plan.get("boxes") or []) if isinstance(b, dict)]
    box = None
    track = bool(plan.get("track"))
    if boxes:
        W, H = int(iw / scale), int(ih / scale)
        if track and len(boxes) > 1:
            # tracked box follows the face per frame → size it like ONE lip box
            # (the median), not the union of every position (that keeps too much dub)
            med = lambda k: sorted(b[k] for b in boxes)[len(boxes) // 2]
            w0, h0 = med("w") / scale, med("h") / scale
            cx = med("x") / scale + w0 / 2
            cy = med("y") / scale + h0 / 2
            x0, y0, x1, y1 = cx - w0 / 2, cy - h0 / 2, cx + w0 / 2, cy + h0 / 2
        else:
            x0 = min(b["x"] for b in boxes) / scale
            y0 = min(b["y"] for b in boxes) / scale
            x1 = max(b["x"] + b["w"] for b in boxes) / scale
            y1 = max(b["y"] + b["h"] for b in boxes) / scale
        px, py = (x1 - x0) * 0.2, (y1 - y0) * 0.2
        bx = max(0, int(x0 - px)); by = max(0, int(y0 - py))
        box = {"x": bx, "y": by,
               "w": max(24, min(W - bx, int(x1 - x0 + 2 * px))),
               "h": max(24, min(H - by, int(y1 - y0 + 2 * py)))}
    if action == "visual" and not box:
        abort(502, "the advisor could not locate the lips — try drawing the box")

    result = {"action": action, "box": box, "track": bool(plan.get("track")),
              "explanation": plan.get("explanation") or "", "ts": time.time()}

    if action == "object":
        # per-sample obj + lips boxes (scaled to full-res) with their frame indices —
        # exactly what object_repair.py needs
        lips_raw = plan.get("boxes") or []
        samples = []
        for k, j in enumerate(indices):
            def up(b):
                if not isinstance(b, dict):
                    return None
                return {"x": int(b["x"] / scale), "y": int(b["y"] / scale),
                        "w": int(b["w"] / scale), "h": int(b["h"] / scale)}
            samples.append({"j": j,
                            "obj": up(obj_raw[k]) if k < len(obj_raw) else None,
                            "lips": up(lips_raw[k]) if k < len(lips_raw) else None})
        result["samples"] = samples
        rois = work / "object-rois.json"
        rois.write_text(json.dumps({"samples": samples}, indent=1), encoding="utf-8")
        cmd = [str(Path(CONFIG["venvs"]["cv"])), str(OBJECT_REPAIR_PY), "--work", str(work),
               "--rois", str(rois), "--preview-at", "-1"]
        try:
            pr2 = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=280, env=job_env())
            if pr2.returncode == 0:
                result["img"] = f"/media/output/script-swap/{stem}/preview-object.jpg"
                for ln in pr2.stdout.splitlines():
                    if ln.startswith("ALIGN:"):
                        try:
                            result["align"] = json.loads(ln[6:].strip())
                        except json.JSONDecodeError:
                            pass
            else:
                err = next((ln for ln in (pr2.stdout or "").splitlines()
                            if ln.startswith("ERROR:")), "")
                result["warning"] = err or "preview failed — you can still run the repair"
        except subprocess.TimeoutExpired:
            result["warning"] = "preview took too long — you can still run the repair"
        return jsonify(result)

    if action == "visual" and box and has_source:
        cmd = [str(Path(CONFIG["venvs"]["cv"])), str(VISUAL_REPAIR_PY), "--work", str(work),
               "--box", str(box["x"]), str(box["y"]), str(box["w"]), str(box["h"]),
               "--preview-at", "2.0"]
        try:
            pr2 = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=90, env=job_env())
            if pr2.returncode == 0:
                result["img"] = f"/media/output/script-swap/{stem}/preview-visual.jpg"
                for ln in pr2.stdout.splitlines():
                    if ln.startswith("ALIGN:"):
                        try:
                            result["align"] = json.loads(ln[6:].strip())
                        except json.JSONDecodeError:
                            pass
        except subprocess.TimeoutExpired:
            pass
    return jsonify(result)


def _sparse_thumbs(path: Path, side: int = 64, rate: str = "0.5") -> "np.ndarray | None":
    """Tiny grayscale thumbnails every 1/rate seconds — a cheap content fingerprint."""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-vf", f"fps={rate},scale={side}:{side}", "-pix_fmt", "gray",
         "-f", "rawvideo", "pipe:1"],
        capture_output=True, env=job_env())
    n = len(r.stdout) // (side * side)
    if n == 0:
        return None
    import numpy as np
    return np.frombuffer(r.stdout[:n * side * side], np.uint8).reshape(n, side, side)


def _video_meta(path: Path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, env=job_env())
    try:
        d = json.loads(r.stdout)
        return (int(d["streams"][0]["width"]), int(d["streams"][0]["height"]),
                float(d["format"].get("duration") or 0))
    except Exception:
        return None


def _find_original(dubbed: Path):
    """Content-match the dubbed video against the uploads library.
    Returns (path, score) of the best candidate, or (None, best_score)."""
    import numpy as np
    meta = _video_meta(dubbed)
    if not meta:
        return None, 0.0
    W, H, dur = meta
    thumbs_d = _sparse_thumbs(dubbed)
    if thumbs_d is None:
        return None, 0.0
    flat_d = thumbs_d.reshape(thumbs_d.shape[0], -1).astype(np.float32)
    flat_d -= flat_d.mean(axis=1, keepdims=True)
    norm = np.linalg.norm(flat_d, axis=1, keepdims=True)
    flat_d /= np.maximum(norm, 1e-6)

    exts = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
    cands = sorted((p for p in UPLOADS.iterdir()
                    if p.is_file() and p.suffix.lower() in exts),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:40]
    hits = []            # (score, bitrate, path)
    best_score = 0.0
    for cand in cands:
        m = _video_meta(cand)
        # the original must match resolution and be at least as long (dubs get tail-truncated)
        if not m or m[0] != W or m[1] != H or m[2] < dur - 1.0:
            continue
        thumbs_c = _sparse_thumbs(cand)
        if thumbs_c is None:
            continue
        k = min(thumbs_d.shape[0], thumbs_c.shape[0])
        if k < 2:
            continue
        flat_c = thumbs_c[:k].reshape(k, -1).astype(np.float32)
        flat_c -= flat_c.mean(axis=1, keepdims=True)
        nc = np.linalg.norm(flat_c, axis=1, keepdims=True)
        flat_c /= np.maximum(nc, 1e-6)
        score = float((flat_d[:k] * flat_c).sum(axis=1).mean())
        best_score = max(best_score, score)
        if score >= 0.80:
            hits.append((score, cand.stat().st_size / max(1.0, m[2]), cand))
    if not hits:
        return None, best_score
    # among near-equal matches (duplicate copies of the same footage — anything
    # within a 0.05 score band), take the highest-bitrate one: repairs should
    # pull the sharpest pixels available
    hits.sort(key=lambda t: (round(t[0] * 20), t[1]), reverse=True)
    return hits[0][2], hits[0][0]


@app.post("/api/dubsync/upload")
def api_dubsync_upload():
    """Drag & drop repair: drop the damaged (dubbed) video and the backend FINDS its
    original automatically by content-matching against the uploads library. If no
    confident match exists, the caller is asked to supply the original too."""
    dubbed = request.files.get("dubbed")
    original = request.files.get("original")
    if not dubbed or not dubbed.filename:
        abort(400, "drop the damaged video")
    if Path(dubbed.filename).suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"):
        abort(400, f"unsupported type: {dubbed.filename}")
    if original and original.filename and \
            Path(original.filename).suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"):
        abort(400, f"unsupported type: {original.filename}")

    base = secure_filename(Path(dubbed.filename).stem).strip(".-_") or f"repair-{time.strftime('%Y%m%d-%H%M%S')}"
    stem, n = base, 2
    while (SWAP_WORK / stem).exists():
        stem = f"{base}-{n}"
        n += 1
    work = SWAP_WORK / stem
    work.mkdir(parents=True)
    dubbed.save(work / "final.mp4")

    if original and original.filename:                       # manual pair (fallback path)
        UPLOADS.mkdir(exist_ok=True)
        oname = secure_filename(Path(original.filename).name) or f"{stem}-original.mp4"
        opath, n = UPLOADS / oname, 2
        while opath.exists():
            opath = UPLOADS / f"{Path(oname).stem}-{n}{Path(oname).suffix}"
            n += 1
        original.save(opath)
        (work / "source.txt").write_text(str(opath), encoding="utf-8")
        return jsonify({"stem": stem, "source": opath.name, "auto": False})

    match, score = _find_original(work / "final.mp4")
    if match is None:
        shutil.rmtree(work, ignore_errors=True)              # nothing usable was created
        abort(422, f"couldn't find the original in your library (best match {score:.0%}) "
                   "— drop the original video too")
    (work / "source.txt").write_text(str(match), encoding="utf-8")
    return jsonify({"stem": stem, "source": match.name, "auto": True,
                    "score": round(score, 3)})


# ------------------------------------------------ Exports (Phase 5: one deliverables view)

def _export_item(p: Path, kind: str, label: str) -> dict:
    st = p.stat()
    inside_root = str(p).startswith(str(ROOT))
    rel = str(p.relative_to(ROOT)).replace("\\", "/") if inside_root else None
    return {"kind": kind, "label": label, "mtime": st.st_mtime, "size": st.st_size,
            "path": rel,                                    # repo-rel (actions work on this)
            "view": f"/media/{rel}" if rel else f"/captioned/{p.parent.name}",
            "stem": p.parent.name if not inside_root else p.stem}


# ------------------------------------------------ Clone Winner (scale a proven ad)

CLONE_PROMPT = """You are a direct-response copywriter. Below is a WINNING ad script — a \
short-form video ad that is already performing (a proven testimonial/VSL). Your job is to \
write a NEW script that clones what makes it win, so we can produce a fresh variant of the ad.

KEEP (this is why it converts — preserve the underlying machine):
- The same structure and beats in the same order (hook → problem/story → product intro → benefits → close/CTA).
- The same angle and emotional logic; the same product and the same kind of claims.
- The same spoken, first-person UGC style: contractions, short sentences, natural talk.

CHANGE (it must read as a DIFFERENT person telling their own version — never a light paraphrase):
- Rewrite every sentence with fresh wording; a new opening hook line with the same hook mechanic.
- New concrete details, sensory specifics, and personal moments (invent plausible ones).
- Do not reuse distinctive phrases from the original.

HARD RULES:
- LENGTH IS A HARD CONSTRAINT (the footage length is fixed; the voice must fit or the lip-sync breaks): {length_rule} Count your words and land inside the range — never go over.
- Compliance: wellness/supplement product — no disease or medical claims, no cure/treat/heal language, no guaranteed outcomes. Personal experience framing ("I felt…") is fine.
- No headings, emojis, hashtags, stage directions, or quotation marks — spoken dialogue only.
{steer_block}
WINNING SCRIPT (the one to clone):
{text}

Respond with ONLY the new script text — no preamble, no explanation, no markdown."""


def _probe_seconds(path: Path) -> float:
    try:
        r = subprocess.run([ff_tool("ffprobe"), "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, env=job_env(), timeout=30)
        return float((r.stdout or "0").strip() or 0)
    except (ValueError, subprocess.TimeoutExpired):
        return 0.0


def _winner_script(work: Path) -> str:
    for name in ("script-edited.txt", "transcript.txt"):
        p = work / name
        if p.is_file():
            t = p.read_text(encoding="utf-8", errors="replace").strip()
            if t:
                return t
    return ""


@app.get("/api/clone/winners")
def api_clone_winners():
    """Finished dubs that can serve as the winning template (have a final + a script)."""
    out = []
    if SWAP_WORK.is_dir():
        for d in SWAP_WORK.iterdir():
            final = d / "final.mp4"
            if not d.is_dir() or not final.is_file():
                continue
            script = _winner_script(d)
            if not script:
                continue
            src_txt = d / "source.txt"
            source = src_txt.read_text(encoding="utf-8").strip() if src_txt.is_file() else ""
            info = read_json(d / "clone-info.json") or {}
            out.append({
                "stem": d.name, "mtime": final.stat().st_mtime,
                "script": script, "words": len(script.split()),
                "source": Path(source).name if source else None,
                "has_source": bool(source) and Path(source).is_file(),
                "has_voice": (d / "voice.json").is_file(),
                "is_clone": bool(info), "cloned_from": info.get("winner"),
            })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify({"winners": out})


@app.get("/api/clone/actors")
def api_clone_actors():
    """Actor footage choices: every video in the uploads library."""
    exts = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
    vids = []
    if UPLOADS.is_dir():
        for p in UPLOADS.iterdir():
            if p.is_file() and p.suffix.lower() in exts and p.stat().st_size > 0:
                vids.append({"name": p.name, "size": p.stat().st_size,
                             "mtime": p.stat().st_mtime})
    vids.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify({"actors": vids})


def _clone_actor_video(winner_work: Path, actor: str) -> Path:
    """Resolve the actor choice to a video path. 'same' → the winner's original footage."""
    if actor == "same":
        src_txt = winner_work / "source.txt"
        if not src_txt.is_file():
            abort(400, "this winner has no source.txt — pick an actor video instead")
        video = Path(src_txt.read_text(encoding="utf-8").strip())
        if not video.is_file():
            abort(400, f"the winner's original footage is missing: {video.name}")
        return video
    video = UPLOADS / Path(actor).name
    if not video.is_file():
        abort(400, f"actor video not found in uploads: {actor}")
    return video


@app.post("/api/clone/script")
def api_clone_script():
    """Generate a similar (winning-pattern) script sized to the chosen actor footage."""
    body = request.get_json(force=True)
    winner = Path(body.get("winner") or "").name
    work = SWAP_WORK / winner
    if not winner or not work.is_dir():
        abort(404, "no such winner")
    text = _winner_script(work)
    if not text:
        abort(400, "the winner has no script/transcript to clone")
    if not CLAUDE_EXE:
        abort(500, "claude CLI not found — install Claude Code or add it to PATH")

    video = _clone_actor_video(work, body.get("actor") or "same")
    secs = _probe_seconds(video)
    win_secs = _probe_seconds(work / "final.mp4")
    rate = (len(text.split()) / win_secs) if win_secs > 2 else 2.5
    rate = rate if 1.0 <= rate <= 5.0 else 2.5
    if secs > 2:
        target = max(8, round(secs * rate))
        lo, hi = round(target * 0.9), round(target * 1.05)
        length_rule = (f"the footage is {secs:.0f}s and the delivery pace is ~{rate:.1f} "
                       f"words/sec — write {lo}-{hi} words (target ~{target}).")
    else:
        n = len(text.split())
        length_rule = f"match the original length: {round(n*0.9)}-{round(n*1.05)} words."

    steer = (body.get("steer") or "").strip()
    steer_block = f"\nEXTRA DIRECTION FROM THE MARKETER: {steer}\n" if steer else ""
    prompt = CLONE_PROMPT.format(length_rule=length_rule, steer_block=steer_block, text=text)
    env = job_env()
    env.pop("CLAUDECODE", None)
    try:
        result = subprocess.run(
            [CLAUDE_EXE, "-p", "--model", "opus",
             "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch"],
            input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=240, cwd=str(ROOT), env=env)
    except subprocess.TimeoutExpired:
        abort(504, "Claude took too long — try again")
    out = (result.stdout or "").strip()
    if result.returncode != 0 or not out:
        abort(502, f"claude CLI failed (rc={result.returncode}): {(result.stderr or '')[:300]}")
    return jsonify({"script": out, "words": len(out.split()),
                    "seconds": round(secs, 1), "rate": round(rate, 2)})


@app.post("/api/clone/run")
def api_clone_run():
    """Create the clone workdir and run the full dub chain on it (same GPU/cost gates as Dubbing)."""
    body = request.get_json(force=True)
    winner = Path(body.get("winner") or "").name
    work = SWAP_WORK / winner
    if not winner or not work.is_dir():
        abort(404, "no such winner")
    script = (body.get("script") or "").strip()
    if len(script.split()) < 8:
        abort(400, "the clone script is empty/too short — generate or paste one first")
    actor = body.get("actor") or "same"
    video = _clone_actor_video(work, actor)

    with jobs_lock:
        busy = next((j for j in jobs.values()
                     if j["action"] == "dub" and j["status"] == "running"), None)
    if busy:
        abort(409, f"a dub is already running ({busy['slug']}) — the GPU handles one at a time")

    n = 2
    while (SWAP_WORK / f"{winner}-v{n}").exists():
        n += 1
    stem = f"{winner}-v{n}"
    clone_work = SWAP_WORK / stem
    clone_work.mkdir(parents=True)
    (clone_work / "script-edited.txt").write_text(script + "\n", encoding="utf-8")
    (clone_work / "source.txt").write_text(str(video) + "\n", encoding="utf-8")
    (clone_work / "clone-info.json").write_text(json.dumps({
        "winner": winner, "actor": "same" if actor == "same" else video.name,
        "created": time.time()}, indent=1), encoding="utf-8")

    engine = body.get("engine") if body.get("engine") in ("local", "fal") else "local"
    venv_py = Path(CONFIG["venvs"]["cv"])
    if engine == "local":
        lipsync = body.get("lipsync") if body.get("lipsync") in (
            "none", "wav2lip", "wav2lip-hd") else "wav2lip-hd"
        cmd = [str(venv_py), str(ENGINES / "local_dub.py"), str(video),
               "--name", stem, "--lipsync", lipsync]
        label = f"Clone winner — {stem} (local XTTS + {lipsync}, FREE)"
        cost_ctx = {"engine": "local", "tts": "local", "tier": lipsync,
                    "video": str(video), "stem": stem, "paid": False}
    else:
        if not body.get("confirm_cost"):
            shutil.rmtree(clone_work, ignore_errors=True)
            abort(400, "FAL.AI clone spends money (voice + TTS + lip-sync) — needs cost approval (confirm_cost)")
        # same actor → reuse the winner's paid voice clone (skips the $1.50 clone fee)
        if actor == "same" and (work / "voice.json").is_file():
            shutil.copy2(work / "voice.json", clone_work / "voice.json")
        tier = body.get("tier") if body.get("tier") in ("pro", "standard", "veed", "latentsync") else "standard"
        tts = body.get("tts") if body.get("tts") in ("hd", "turbo", "f5") else "hd"
        cmd = [str(venv_py), str(ENGINES / "dub.py"), str(video),
               "--name", stem, "--tier", tier, "--tts", tts]
        label = f"Clone winner — {stem} (fal {tts}/{tier}) $"
        cost_ctx = {"engine": "fal", "tts": tts, "tier": tier,
                    "video": str(video), "stem": stem, "paid": True}

    job_id = jobs_create("dub", stem, label)
    threading.Thread(target=run_dub_job, args=(job_id, cmd, cost_ctx), daemon=True).start()
    return jsonify({"job_id": job_id, "stem": stem})


@app.get("/api/clone/list")
def api_clone_list():
    """All clones made so far, with their state (running / ready / failed)."""
    clones = []
    if SWAP_WORK.is_dir():
        for d in SWAP_WORK.iterdir():
            info = read_json(d / "clone-info.json")
            if not d.is_dir() or not info:
                continue
            final = d / "final.mp4"
            with jobs_lock:
                cand = [j for j in jobs.values()
                        if j["slug"] == d.name and j["action"] == "dub"]
                job = max(cand, key=lambda j: j["started"]) if cand else None
            clones.append({
                "stem": d.name, "winner": info.get("winner"), "actor": info.get("actor"),
                "created": info.get("created"),
                "ready": final.is_file() and final.stat().st_size > 0,
                "final_mtime": final.stat().st_mtime if final.is_file() else None,
                "job": {"id": job["id"], "status": job["status"]} if job else None,
            })
    clones.sort(key=lambda x: x.get("created") or 0, reverse=True)
    return jsonify({"clones": clones})


# ─── Duo — two-speaker interview dubbing ─────────────────────────────────────
# Diarize (who speaks when) → user reviews/edits labeled turns → clone BOTH
# voices, re-voice each line on the original timeline, sync.so active-speaker
# lip-sync so each face only moves during its own lines.

def _duo_estimate(stem: str, cfg: dict, tier: str) -> dict:
    """Cost of a duo run: up to 2 voice clones + per-char HD TTS + lip-sync tier."""
    chars = sum(len(s.get("text", "")) for s in cfg.get("segments", []))
    src = Path(cfg.get("source", ""))
    dur = video_duration(ffprobe_json(src)) if src.is_file() else 0.0
    cloned = (SWAP_WORK / stem / "voices.json").is_file()   # duo caches both voice ids
    clone_cost = 0.0 if cloned else 2 * MINIMAX_CLONE_FEE
    tts_cost = round(chars / 1000.0 * TTS_RATE_PER_1K["hd"], 4)
    lip_rate = LIPSYNC_RATE_PER_SEC.get("pro" if tier == "pro" else "standard", 0.0)
    lip_cost = round(dur * lip_rate, 4)
    total = round(clone_cost + tts_cost + lip_cost, 4)
    parts = []
    if clone_cost:
        parts.append(f"2 voice clones: ${clone_cost:.2f}")
    else:
        parts.append("voices already cloned (cached)")
    parts.append(f"TTS hd: ${tts_cost:.3f} ({chars} chars)")
    parts.append(f"lip-sync {tier}: ${lip_cost:.3f} ({dur:.0f}s, active-speaker)")
    return {"this_run": total, "chars": chars, "duration": round(dur, 1),
            "engine": "fal-duo", "tier": tier, "summary": " · ".join(parts)}


def run_duo_job(job_id: str, cmd: list[str], stem: str, est: dict) -> None:
    """Run the duo dub, then append its cost to the ledger like other paid dubs."""
    run_job(job_id, cmd)
    job = jobs[job_id]
    if job["status"] != "done":
        return
    try:
        res = record_spend(stem, est)
        with jobs_lock:
            job["lines"].append("")
            job["lines"].append(f"💰 This interview dub cost ~${res['this_run']:.3f} on fal.ai  ({est['summary']})")
            job["lines"].append(f"🧾 Total spent on fal.ai so far: ${res['total']:.2f}")
        job["cost"] = {"this_run": res["this_run"], "total": res["total"], "summary": est["summary"]}
    except Exception as exc:                                  # noqa: BLE001
        with jobs_lock:
            job["lines"].append(f"(cost tracking skipped: {exc})")


@app.get("/api/duo/config")
def api_duo_config_get():
    stem = Path(request.args.get("file", "")).stem
    if not stem:
        abort(400, "file required")
    work = SWAP_WORK / stem
    cfg = read_json(work / "duo-config.json")
    sidecar = TRANSCRIPTS / f"{stem}.json"
    return jsonify({
        "stem": stem,
        "config": cfg or None,
        "has_transcript": sidecar.is_file(),
        "voices_cloned": (work / "voices.json").is_file(),
    })


@app.post("/api/duo/config")
def api_duo_config_save():
    body = request.get_json(force=True)
    stem = Path(body.get("file", "")).stem
    cfg = body.get("config") or {}
    if not stem:
        abort(400, "file required")
    segs = cfg.get("segments") or []
    speakers = cfg.get("speakers") or {}
    if len(speakers) < 2 or len(segs) < 2:
        abort(400, "need 2 speakers and at least 2 lines")
    for s in segs:
        if s.get("speaker") not in speakers:
            abort(400, f"line at {s.get('start')}s has unknown speaker {s.get('speaker')!r}")
        if not (s.get("text") or "").strip():
            abort(400, f"line at {s.get('start')}s is empty")
        if float(s.get("end", 0)) <= float(s.get("start", -1)):
            abort(400, f"line at {s.get('start')}s has a bad time window")
    for k, sp in speakers.items():
        if not sp.get("ref_windows"):
            abort(400, f"speaker {sp.get('label', k)} has no voice-reference windows")
    work = SWAP_WORK / stem
    work.mkdir(parents=True, exist_ok=True)
    (work / "duo-config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "segments": len(segs)})


@app.post("/api/duo/diarize")
def api_duo_diarize():
    body = request.get_json(force=True)
    fname = Path(body.get("file", "")).name
    src = UPLOADS / fname
    if not fname or not src.is_file():
        abort(400, "file not found in uploads/")
    if not DUB_VENV_PY.is_file():
        abort(500, f"dubbing venv missing: {DUB_VENV_PY}")
    stem = src.stem
    work = SWAP_WORK / stem
    work.mkdir(parents=True, exist_ok=True)
    cmd = [str(DUB_VENV_PY), str(ENGINES / "diarize.py"),
           "--video", str(src),
           "--transcript", str(TRANSCRIPTS / f"{stem}.json"),
           "--work", str(work),
           "--whisper-python", str(TRANSCRIBE_VENV_PY),
           "--whisper-script", str(TRANSCRIBE_PY),
           "--uploads", str(UPLOADS)]
    job_id = jobs_create("diarize", stem, f"Detect speakers — {fname}")
    threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.post("/api/duo/run")
def api_duo_run():
    body = request.get_json(force=True)
    fname = Path(body.get("file", "")).name
    stem = Path(fname).stem
    tier = body.get("tier", "pro")
    if tier not in ("pro", "standard"):
        abort(400, "tier must be pro or standard")
    work = SWAP_WORK / stem
    cfg = read_json(work / "duo-config.json")
    if not cfg:
        abort(400, "no duo-config yet — run Detect speakers first")

    est = _duo_estimate(stem, cfg, tier)
    if not body.get("confirm_cost"):
        return jsonify({"needs_confirm": True, "estimate": est}), 402

    # one dub at a time — same GPU/cloud discipline as single-speaker dubs
    with jobs_lock:
        busy = next((j for j in jobs.values()
                     if j["action"] in ("dub", "duo") and j["status"] == "running"), None)
    if busy:
        abort(409, f"another dub is already running ({busy['slug']}) — wait for it to finish")

    venv_py = Path(CONFIG["venvs"]["cv"])
    cmd = [str(venv_py), str(ENGINES / "duo_run.py"), "--name", stem, "--tier", tier]
    job_id = jobs_create("duo", stem, f"Interview dub (2 voices) — {stem} [{tier}]")
    threading.Thread(target=run_duo_job, args=(job_id, cmd, stem, est), daemon=True).start()
    return jsonify({"job_id": job_id, "estimate": est})


@app.get("/api/exports")
def api_exports():
    """Every finished deliverable across the pipeline, one list."""
    items = []
    out = ROOT / "output"
    for p in sorted(out.glob("*.mp4")):
        items.append(_export_item(p, "vsl", p.stem))
    edits = out / "edits"
    if edits.is_dir():
        for p in sorted(edits.glob("*.mp4")):
            items.append(_export_item(p, "edit", p.stem))
    if SWAP_WORK.is_dir():
        for d in sorted(SWAP_WORK.iterdir()):
            if (d / "final.mp4").is_file():
                items.append(_export_item(d / "final.mp4", "dub", d.name))
            if (d / "final-captioned.mp4").is_file():
                items.append(_export_item(d / "final-captioned.mp4", "dub-captioned",
                                          f"{d.name} (captioned)"))
    if SUBSTUDIO_OUT.is_dir():
        for d in sorted(SUBSTUDIO_OUT.iterdir()):
            if (d / "captioned.mp4").is_file():
                items.append(_export_item(d / "captioned.mp4", "captioned",
                                          f"{d.name} (subtitle studio)"))
    if I2V_OUT.is_dir():
        for d in sorted(I2V_OUT.iterdir()):
            if d.is_dir() and d.name != "_uploads" and (d / "clip.mp4").is_file():
                items.append(_export_item(d / "clip.mp4", "i2v", f"{d.name} (image→video)"))
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify({"items": items, "exports_dir": str(EXPORTS_DIR)})


@app.post("/api/exports/send")
def api_exports_send():
    """Copy a deliverable into the ONE Desktop exports folder (flat, unique name)."""
    body = request.get_json(force=True)
    kind = body.get("kind")
    if kind == "captioned":
        stem = Path(body.get("stem") or "").name
        src = SUBSTUDIO_OUT / stem / "captioned.mp4"
        flat = f"{stem}-captioned.mp4"
    else:
        rel = (body.get("path") or "").replace("\\", "/")
        src = (ROOT / rel).resolve()
        if not str(src).startswith(str(ROOT / "output")) or src.suffix != ".mp4":
            abort(400, "path must be an .mp4 under output/")
        parts = src.relative_to(ROOT / "output").parts
        flat = src.name if len(parts) == 1 else f"{parts[-2]}-{src.name}"
    if not src.is_file():
        abort(404, "deliverable not found")
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    dest, n = EXPORTS_DIR / flat, 2
    while dest.exists():                       # never clobber an earlier export
        dest = EXPORTS_DIR / f"{Path(flat).stem}-{n}.mp4"
        n += 1
    shutil.copy2(src, dest)

    # Creator page extras: mark the workdir as exported (drives the Deliver badge)
    # and optionally queue the heavy workdir for auto-cleanup once it's safe.
    stem = Path(body.get("stem") or "").name
    queued = None
    if stem and (SWAP_WORK / stem).is_dir():
        (SWAP_WORK / stem / ".exported").write_text(str(dest), encoding="utf-8")
        if body.get("auto_cleanup"):
            delay = float(CONFIG.get("auto_cleanup", {}).get("delay_hours", 24))
            queued = cleanup.enqueue(stem, delay)
    return jsonify({"saved_to": str(dest), "cleanup": queued})


# ------------------------------------------------ Voice Bank
# Clone a voice from any library video, save it as a named voice, then reuse
# it to dub OTHER characters. Extraction is free/local (ffmpeg); the saved
# reference feeds XTTS on the local dub path (--voice-ref).

VOICES_DIR = ROOT / "output" / "voices"
VOICE_REF_SECONDS = 20          # a longer clean reference → a richer clone


def _voice_meta(vid: str) -> dict:
    return read_json(VOICES_DIR / vid / "voice.json") or {}


@app.get("/api/voices")
def api_voices():
    out = []
    if VOICES_DIR.is_dir():
        for d in sorted(VOICES_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            info = read_json(d / "voice.json")
            if not d.is_dir() or not info or not (d / "ref.wav").is_file():
                continue
            out.append({
                "id": d.name, "name": info.get("name") or d.name,
                "source": info.get("source"), "created": info.get("created"),
                "sample": f"output/voices/{d.name}/sample.mp3" if (d / "sample.mp3").is_file() else None,
            })
    return jsonify({"voices": out})


@app.post("/api/voices/create")
def api_voices_create():
    """Extract a clean voice reference from a library video and save it as a voice."""
    b = request.get_json(force=True)
    fname = Path(b.get("file") or "").name
    src = UPLOADS / fname
    if not fname or not src.is_file():
        abort(400, "pick a library video first")
    name = (b.get("name") or Path(fname).stem).strip()[:40] or Path(fname).stem
    vid = secure_filename(name).strip(".-_").lower() or "voice"
    base, n = vid, 2
    while (VOICES_DIR / vid).exists():
        vid = f"{base}-{n}"
        n += 1
    work = VOICES_DIR / vid
    work.mkdir(parents=True, exist_ok=True)
    ref = work / "ref.wav"
    sample = work / "sample.mp3"
    env = job_env()
    # full mono 16k, then take up to VOICE_REF_SECONDS from ~1s in as the clone reference
    full = work / "_full.wav"
    r = subprocess.run([ff_tool("ffmpeg"), "-y", "-loglevel", "error", "-i", str(src),
                        "-vn", "-ac", "1", "-ar", "16000", str(full)], env=env)
    if r.returncode != 0 or not full.is_file():
        shutil.rmtree(work, ignore_errors=True)
        abort(400, "that video has no usable audio to clone from")
    dur = video_duration(ffprobe_json(full)) or 0.0
    start = 1.0 if dur > VOICE_REF_SECONDS + 2 else 0.0
    clip = min(VOICE_REF_SECONDS, max(2.0, dur - start))
    subprocess.run([ff_tool("ffmpeg"), "-y", "-loglevel", "error", "-ss", str(start), "-t", str(clip),
                    "-i", str(full), "-af", "highpass=f=60,dynaudnorm",
                    "-ac", "1", "-ar", "16000", str(ref)], env=env)
    # short mp3 preview (what the voice actually sounds like)
    subprocess.run([ff_tool("ffmpeg"), "-y", "-loglevel", "error", "-t", "8", "-i", str(ref),
                    "-codec:a", "libmp3lame", "-b:a", "128k", str(sample)], env=env)
    full.unlink(missing_ok=True)
    if not ref.is_file() or ref.stat().st_size == 0:
        shutil.rmtree(work, ignore_errors=True)
        abort(400, "could not build a voice reference (need a couple of seconds of clear speech)")
    (work / "voice.json").write_text(json.dumps(
        {"name": name, "source": fname, "created": time.time(),
         "ref_seconds": round(clip, 1)}, indent=2), encoding="utf-8")
    return jsonify({"id": vid, "name": name,
                    "sample": f"output/voices/{vid}/sample.mp3"})


@app.post("/api/voices/rename")
def api_voices_rename():
    b = request.get_json(force=True)
    vid = Path(b.get("id") or "").name
    name = (b.get("name") or "").strip()[:40]
    vf = VOICES_DIR / vid / "voice.json"
    if not name or not vf.is_file():
        abort(404, "voice not found")
    info = read_json(vf)
    info["name"] = name
    vf.write_text(json.dumps(info, indent=2), encoding="utf-8")
    return jsonify({"id": vid, "name": name})


@app.post("/api/voices/delete")
def api_voices_delete():
    vid = Path(request.get_json(force=True).get("id") or "").name
    d = VOICES_DIR / vid
    if not d.is_dir():
        abort(404, "voice not found")
    label = soft_delete(d, f"voice-{vid}")
    return jsonify({"deleted": vid, "trash": label})


@app.get("/voices")
def voices_page():
    return send_from_directory(STATIC, "voices.html")


# ------------------------------------------------ Image -> Video (fal.ai)
# Upload a still + a prompt → a ~30s clip. fal models cap at 5-10s, so the
# engine chains segments (last frame → next seed). Cost-gated like dubbing.

I2V_ENGINE = APP_DIR / "engines" / "i2v_gen.py"
I2V_OUT = ROOT / "output" / "i2v"
I2V_UPLOADS = I2V_OUT / "_uploads"
I2V_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
FAL_ENV_FILE = ROOT / ".env"

# mirror of the engine's MODELS (label + rough per-segment $ for the estimate)
I2V_MODELS = {
    "kling-2.1":     {"label": "Kling 2.1 Standard — balanced (recommended)", "seg": 10, "cost_per_seg": 0.45},
    "kling-2.1-pro": {"label": "Kling 2.1 Pro — best quality (pricey)",        "seg": 10, "cost_per_seg": 0.95},
    "hailuo-02":     {"label": "MiniMax Hailuo 02 — great motion",             "seg": 10, "cost_per_seg": 0.48},
    "wan-2.2":       {"label": "Wan 2.2 — budget",                             "seg": 5,  "cost_per_seg": 0.20},
}


def _i2v_estimate(model_key: str, seconds: int) -> dict:
    m = I2V_MODELS.get(model_key) or I2V_MODELS["kling-2.1"]
    n = max(1, round(seconds / m["seg"]))
    total = round(n * m["cost_per_seg"], 2)
    real = n * m["seg"]
    return {"this_run": total, "segments": n, "seg_len": m["seg"], "seconds": real,
            "engine": "fal-i2v", "model": model_key,
            "summary": f"{n} × {m['seg']}s on {m['label'].split(' — ')[0]} ≈ ${total:.2f} (~{real}s clip)"}


@app.get("/api/i2v/models")
def api_i2v_models():
    return jsonify({"models": [{"key": k, **v} for k, v in I2V_MODELS.items()]})


@app.post("/api/i2v/upload")
def api_i2v_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "no image")
    ext = Path(f.filename).suffix.lower()
    if ext not in I2V_IMG_EXTS:
        abort(400, f"image must be one of: {', '.join(sorted(I2V_IMG_EXTS))}")
    I2V_UPLOADS.mkdir(parents=True, exist_ok=True)
    base = secure_filename(Path(f.filename).stem).strip(".-_") or "img"
    name = f"{base}-{time.strftime('%H%M%S')}{ext}"
    f.save(I2V_UPLOADS / name)
    return jsonify({"image": f"output/i2v/_uploads/{name}", "name": name})


@app.post("/api/i2v/estimate")
def api_i2v_estimate():
    b = request.get_json(force=True)
    return jsonify(_i2v_estimate(b.get("model", "kling-2.1"), int(b.get("seconds", 30))))


def run_i2v_job(job_id: str, cmd: list[str], slug: str, est: dict) -> None:
    run_job(job_id, cmd)
    job = jobs[job_id]
    if job["status"] != "done":
        return
    try:
        res = record_spend(slug, est)
        with jobs_lock:
            job["lines"].append("")
            job["lines"].append(f"💰 This clip cost ~${res['this_run']:.2f} on fal.ai  ({est['summary']})")
            job["lines"].append(f"🧾 Total spent on fal.ai so far: ${res['total']:.2f}")
        job["cost"] = {"this_run": res["this_run"], "total": res["total"], "summary": est["summary"]}
    except Exception as exc:                      # noqa: BLE001
        with jobs_lock:
            job["lines"].append(f"(cost tracking skipped: {exc})")


@app.post("/api/i2v/run")
def api_i2v_run():
    b = request.get_json(force=True)
    rel = (b.get("image") or "").replace("\\", "/")
    prompt = (b.get("prompt") or "").strip()
    model = b.get("model", "kling-2.1")
    aspect = b.get("aspect", "9:16")
    seconds = int(b.get("seconds", 30))
    if model not in I2V_MODELS:
        abort(400, "unknown model")
    if aspect not in ("9:16", "16:9", "1:1"):
        abort(400, "bad aspect")
    if not (5 <= seconds <= 60):
        abort(400, "seconds must be 5-60")
    if len(prompt) < 3:
        abort(400, "write a prompt describing the motion / scene")
    img = (ROOT / rel).resolve()
    if not str(img).startswith(str(I2V_UPLOADS.resolve())) or not img.is_file():
        abort(400, "upload an image first")

    est = _i2v_estimate(model, seconds)
    if not b.get("confirm_cost"):
        return jsonify({"needs_confirm": True, "estimate": est}), 402

    slug = f"{img.stem}-{time.strftime('%H%M%S')}"
    work = I2V_OUT / slug
    work.mkdir(parents=True, exist_ok=True)
    cv_py = Path(CONFIG["venvs"]["cv"])
    cmd = [str(cv_py), str(I2V_ENGINE), "--image", str(img), "--prompt", prompt,
           "--out", str(work), "--name", slug, "--model", model,
           "--aspect", aspect, "--seconds", str(seconds), "--env-file", str(FAL_ENV_FILE)]
    job_id = jobs_create("i2v", slug, f"Image→Video — {slug} [{model}]")
    threading.Thread(target=run_i2v_job, args=(job_id, cmd, slug, est), daemon=True).start()
    return jsonify({"job_id": job_id, "slug": slug, "estimate": est})


@app.get("/api/i2v/list")
def api_i2v_list():
    items = []
    if I2V_OUT.is_dir():
        for d in sorted(I2V_OUT.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not d.is_dir() or d.name == "_uploads":
                continue
            info = read_json(d / "i2v.json") or {}
            clip = d / "clip.mp4"
            with jobs_lock:
                cand = [j for j in jobs.values() if j["slug"] == d.name and j["action"] == "i2v"]
                job = max(cand, key=lambda j: j["started"]) if cand else None
            items.append({
                "slug": d.name, "prompt": info.get("prompt"), "model_label": info.get("model_label"),
                "aspect": info.get("aspect"), "seconds": info.get("seconds"),
                "est_cost": info.get("est_cost"),
                "ready": clip.is_file() and clip.stat().st_size > 0,
                "clip": f"output/i2v/{d.name}/clip.mp4" if clip.is_file() else None,
                "clip_mtime": clip.stat().st_mtime if clip.is_file() else None,
                "job": {"id": job["id"], "status": job["status"]} if job else None,
            })
    return jsonify({"items": items})


# ── Fit video to script (AI-extend) ─────────────────────────────────────────
# Grow a video to match a LONGER rewritten script: measure the script's real
# spoken length (XTTS, free), then AI-generate the missing seconds from the last
# frame (fal.ai, cost-gated) and prove the final length matches. See fit_extend.py.
FIT_ENGINE = APP_DIR / "engines" / "fit_extend.py"


def _fit_work(stem: str) -> Path:
    work = (SWAP_WORK / stem / "fit").resolve()
    if not str(work).startswith(str(SWAP_WORK.resolve())):
        abort(400, "bad stem")
    return work


def _fit_need(gap: float, seg: int) -> int:
    """Seconds to request from i2v: the gap rounded UP to a whole segment, so the
    generated footage is never short of the gap (a segment multiple also makes the
    engine's round(seconds/seg) exact)."""
    n = max(1, int(gap // seg) + (1 if gap % seg > 0.01 else 0))
    return n * seg


@app.post("/api/fit/analyze")
def api_fit_analyze():
    b = request.get_json(force=True)
    name = (b.get("file") or "").strip()
    if not name:
        abort(400, "no file")
    src = (UPLOADS / name).resolve()
    if not str(src).startswith(str(UPLOADS.resolve())) or not src.is_file():
        abort(400, "video not found")
    if not has_audio_stream(src):
        abort(400, "This video has no audio to clone a voice from — fit it after a dub, "
                   "or use a clip that has speech.")
    stem = Path(name).stem
    script = SWAP_WORK / stem / "script-edited.txt"
    if not script.is_file() or not script.read_text(encoding="utf-8").strip():
        abort(400, "Save a script first (right panel → Script), then Fit.")
    work = _fit_work(stem)
    work.mkdir(parents=True, exist_ok=True)
    cv_py = CONFIG["venvs"]["cv"]
    ds_app = str(Path(CONFIG["dubbing_studio"]) / "app.py")
    cmd = [cv_py, str(FIT_ENGINE), "--mode", "analyze", "--source", str(src),
           "--stem", stem, "--work", str(work), "--script", str(script),
           "--ds-py", str(DUB_VENV_PY), "--ds-app", ds_app, "--language", "en"]
    job_id = jobs_create("fit", stem, f"Fit analysis — {stem}", gpu=True)
    threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id, "stem": stem})


@app.get("/api/fit/plan/<path:stem>")
def api_fit_plan(stem):
    work = _fit_work(stem)
    return jsonify({"plan": read_json(work / "plan.json") or {},
                    "fit": read_json(work / "fit.json") or {}})


def run_fit_job(job_id: str, cmd: list[str], stem: str, est: dict) -> None:
    run_job(job_id, cmd)
    job = jobs[job_id]
    if job["status"] != "done":
        return
    try:
        res = record_spend(stem, est)
        with jobs_lock:
            job["lines"].append("")
            job["lines"].append(f"💰 This fit cost ~${res['this_run']:.2f} on fal.ai  ({est['summary']})")
            job["lines"].append(f"🧾 Total spent on fal.ai so far: ${res['total']:.2f}")
        job["cost"] = {"this_run": res["this_run"], "total": res["total"], "summary": est["summary"]}
    except Exception as exc:                      # noqa: BLE001
        with jobs_lock:
            job["lines"].append(f"(cost tracking skipped: {exc})")


@app.post("/api/fit/run")
def api_fit_run():
    b = request.get_json(force=True)
    name = (b.get("file") or "").strip()
    src = (UPLOADS / name).resolve()
    if not str(src).startswith(str(UPLOADS.resolve())) or not src.is_file():
        abort(400, "video not found")
    stem = Path(name).stem
    work = _fit_work(stem)
    plan = read_json(work / "plan.json") or {}
    if not plan:
        abort(400, "run Analyze first")
    if not plan.get("needs_extend"):
        abort(400, "video is already long enough for the script — no extension needed")
    model = b.get("model", "kling-2.1")
    aspect = b.get("aspect", "9:16")
    if model not in I2V_MODELS:
        abort(400, "unknown model")
    if aspect not in ("9:16", "16:9", "1:1"):
        abort(400, "bad aspect")
    seg = I2V_MODELS[model]["seg"]
    need = _fit_need(float(plan.get("gap") or 0), seg)
    est = _i2v_estimate(model, need)
    if not b.get("confirm_cost"):
        return jsonify({"needs_confirm": True, "estimate": est, "plan": plan}), 402
    prompt = (b.get("prompt") or "").strip()
    cv_py = CONFIG["venvs"]["cv"]
    cmd = [cv_py, str(FIT_ENGINE), "--mode", "extend", "--source", str(src),
           "--stem", stem, "--work", str(work), "--cv-py", cv_py, "--i2v", str(I2V_ENGINE),
           "--env-file", str(FAL_ENV_FILE), "--model", model, "--aspect", aspect,
           "--seconds", str(need), "--seg", str(seg), "--uploads", str(UPLOADS)]
    if prompt:
        cmd += ["--prompt", prompt]
    job_id = jobs_create("fit-extend", stem, f"Fit to script — {stem} [{model}]")
    threading.Thread(target=run_fit_job, args=(job_id, cmd, stem, est), daemon=True).start()
    return jsonify({"job_id": job_id, "stem": stem, "estimate": est})


# ── Text-to-Video: continue a copied clip ───────────────────────────────────
# Keep a chosen segment of an uploaded video, then AI-generate footage that
# continues from it (fal.ai). Reuses the i2v estimate, cost gate, spend ledger,
# and output dir, so the result shows under Media like any Image->Video clip.
T2V_ENGINE = APP_DIR / "engines" / "t2v_continue.py"


def run_t2v_job(job_id: str, cmd: list[str], slug: str, est: dict) -> None:
    run_job(job_id, cmd)
    job = jobs[job_id]
    if job["status"] != "done":
        return
    try:
        res = record_spend(slug, est)
        with jobs_lock:
            job["lines"].append("")
            job["lines"].append(f"💰 This clip cost ~${res['this_run']:.2f} on fal.ai  ({est['summary']})")
            job["lines"].append(f"🧾 Total spent on fal.ai so far: ${res['total']:.2f}")
        job["cost"] = {"this_run": res["this_run"], "total": res["total"], "summary": est["summary"]}
    except Exception as exc:                      # noqa: BLE001
        with jobs_lock:
            job["lines"].append(f"(cost tracking skipped: {exc})")


@app.post("/api/t2v/continue")
def api_t2v_continue():
    b = request.get_json(force=True)
    name = (b.get("file") or "").strip()
    src = (UPLOADS / name).resolve()
    if not str(src).startswith(str(UPLOADS.resolve())) or not src.is_file():
        abort(400, "pick a source video from your library first")
    prompt = (b.get("prompt") or "").strip()
    if len(prompt) < 3:
        abort(400, "write what the continuation should show / do")
    model = b.get("model", "kling-2.1")
    aspect = b.get("aspect", "9:16")
    if model not in I2V_MODELS:
        abort(400, "unknown model")
    if aspect not in ("9:16", "16:9", "1:1"):
        abort(400, "bad aspect")
    seconds = int(b.get("seconds", 10))
    if not (5 <= seconds <= 60):
        abort(400, "generate 5-60 seconds")
    try:
        start = max(0.0, float(b.get("start") or 0))
        end = float(b.get("end") or 0)
    except (TypeError, ValueError):
        abort(400, "bad start/end times")
    est = _i2v_estimate(model, seconds)
    if not b.get("confirm_cost"):
        return jsonify({"needs_confirm": True, "estimate": est}), 402
    slug = f"{src.stem}-cont-{time.strftime('%H%M%S')}"
    work = I2V_OUT / slug
    work.mkdir(parents=True, exist_ok=True)
    cv_py = CONFIG["venvs"]["cv"]
    seg = I2V_MODELS[model]["seg"]
    cmd = [cv_py, str(T2V_ENGINE), "--source", str(src), "--start", str(start),
           "--end", str(end), "--work", str(work), "--name", slug, "--cv-py", cv_py,
           "--i2v", str(I2V_ENGINE), "--env-file", str(FAL_ENV_FILE), "--model", model,
           "--aspect", aspect, "--seconds", str(seconds), "--seg", str(seg), "--prompt", prompt]
    job_id = jobs_create("t2v", slug, f"Text->Video continue - {slug} [{model}]")
    threading.Thread(target=run_t2v_job, args=(job_id, cmd, slug, est), daemon=True).start()
    return jsonify({"job_id": job_id, "slug": slug, "estimate": est})


# ── Script -> AI b-roll VIDEO (upload -> transcribe -> new script -> video) ──
# One button, three chained steps on the existing B-Roll Factory:
#   storyboard (Claude: script -> shot list)  ->  generate (a clip per shot)
#   -> assemble (concat + cloned-voice narration) -> output/i2v/<slug> (Media).
BROLLVID_ENGINE = APP_DIR / "engines" / "broll_video.py"


def run_chain_job(job_id: str, steps: list[tuple[str, list[str]]], est: dict | None = None,
                  slug: str = "") -> None:
    """Run several engine steps inside ONE job. run_job marks the job done after
    each command, so intermediate successes are flipped back to running."""
    last = len(steps) - 1
    for i, (label, cmd) in enumerate(steps):
        with jobs_lock:
            jobs[job_id]["lines"].append("")
            jobs[job_id]["lines"].append(f"=== step {i + 1}/{len(steps)}: {label} ===")
        run_job(job_id, cmd)
        if jobs[job_id]["status"] != "done":
            return                     # failed / stopped — leave the state as-is
        if i != last:
            with jobs_lock:
                jobs[job_id]["status"] = "running"
                jobs[job_id]["ended"] = None
    if est:
        try:
            res = record_spend(slug, est)
            with jobs_lock:
                jobs[job_id]["lines"].append("")
                jobs[job_id]["lines"].append(f"💰 ~${res['this_run']:.2f} on fal.ai ({est['summary']})")
            jobs[job_id]["cost"] = {"this_run": res["this_run"], "total": res["total"],
                                    "summary": est["summary"]}
        except Exception as exc:                      # noqa: BLE001
            with jobs_lock:
                jobs[job_id]["lines"].append(f"(cost tracking skipped: {exc})")


@app.post("/api/brollvid/run")
def api_brollvid_run():
    b = request.get_json(force=True)
    script = (b.get("script") or "").strip()
    if len(script.split()) < 5:
        abort(400, "write (or transcribe) a script first")
    if not CLAUDE_EXE:
        abort(500, "the Claude CLI isn't available — it writes the shot list")
    aspect = b.get("aspect", "9:16")
    if aspect not in ("9:16", "1:1", "16:9"):
        abort(400, "bad aspect")
    motion = b.get("motion", "ken")
    if motion not in ("ken", "anim", "ltx", "fal"):
        abort(400, "bad motion engine")
    shots = max(2, min(12, int(b.get("shots") or 6)))
    # optional reference video (from the library) — clones its voice for narration
    ref = None
    name = (b.get("file") or "").strip()
    if name:
        cand = (UPLOADS / name).resolve()
        if not str(cand).startswith(str(UPLOADS.resolve())) or not cand.is_file():
            abort(400, "reference video not found")
        ref = cand

    est = None
    if motion == "fal":
        # priced before the shot list exists: assume ~1 segment per shot
        m = I2V_MODELS.get(b.get("fal_model", "kling-2.1")) or I2V_MODELS["kling-2.1"]
        total = round(shots * m["cost_per_seg"], 2)
        est = {"this_run": total, "engine": "fal-i2v", "model": b.get("fal_model", "kling-2.1"),
               "summary": f"~{shots} shots on {m['label'].split(' — ')[0]} ≈ ${total:.2f} (approx — "
                          f"the real shot list is written first)"}
        if not b.get("confirm_cost"):
            return jsonify({"needs_confirm": True, "estimate": est}), 402

    stamp = time.strftime("%m%d-%H%M%S")
    batch = f"script-{stamp}"
    work = BROLL_OUT / batch
    work.mkdir(parents=True, exist_ok=True)
    (work / "script.txt").write_text(script + "\n", encoding="utf-8")
    out_dir = I2V_OUT / f"brollvid-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    cv_py = _cv_py()

    steps: list[tuple[str, list[str]]] = [
        ("storyboard — Claude turns the script into a shot list",
         [cv_py, str(BROLLVID_ENGINE), "storyboard", "--script", str(work / "script.txt"),
          "--work", str(work), "--aspect", aspect, "--shots", str(shots),
          "--claude", str(CLAUDE_EXE), "--model", "sonnet"]
         + (["--brand"] if b.get("brand", True) else [])
         + (["--ugc"] if b.get("ugc", True) else [])
         + (["--preset", "ugc10"] if b.get("preset") == "ugc10" else [])),
        ("generate — render a clip for every shot",
         [cv_py, str(BROLL_ENGINE), "generate", "--recipe", str(work / "recipe.json"),
          "--motion", motion, "--style", "auto", "--aspect", aspect, "--no-bank"]
         + (["--fal-model", b.get("fal_model", "kling-2.1")] if motion == "fal" else [])),
        ("assemble — stitch the shots and narrate the script",
         [cv_py, str(BROLLVID_ENGINE), "assemble", "--batch", str(work),
          "--out-dir", str(out_dir), "--ds-py", str(DUB_VENV_PY),
          "--ds-app", str(Path(CONFIG["dubbing_studio"]) / "app.py")]
         + (["--ref-video", str(ref)] if ref else [])),
    ]
    job_id = jobs_create("brollvid", batch, f"Script → b-roll video — {batch} [{motion}]")
    threading.Thread(target=run_chain_job, args=(job_id, steps, est, batch), daemon=True).start()
    return jsonify({"job_id": job_id, "batch": batch, "slug": out_dir.name, "estimate": est})


@app.get("/image-to-video")
def image_to_video_page():
    return send_from_directory(STATIC, "image-to-video.html")


# ------------------------------------------------ Settings + auto-cleanup

CONFIG_FILE = VS_ROOT / "config.json"
config_lock = threading.Lock()


@app.get("/api/settings")
def api_settings_get():
    ac = CONFIG.get("auto_cleanup") or {}
    return jsonify({
        "auto_cleanup": {"enabled": bool(ac.get("enabled", False)),
                         "delay_hours": float(ac.get("delay_hours", 24)),
                         "min_free_space_gb": float(ac.get("min_free_space_gb", 20))},
        "exports_dir": str(EXPORTS_DIR),
        "remote_pin_set": bool(CONFIG.get("remote_pin")),
        "lan_access": bool(CONFIG.get("lan_access")),
        "port": CONFIG.get("port"),
    })


@app.post("/api/settings")
def api_settings_save():
    global EXPORTS_DIR
    b = request.get_json(force=True)
    with config_lock:
        if isinstance(b.get("auto_cleanup"), dict):
            ac = CONFIG.setdefault("auto_cleanup", {})
            src = b["auto_cleanup"]
            if "enabled" in src:
                ac["enabled"] = bool(src["enabled"])
            if "delay_hours" in src:
                d = float(src["delay_hours"])
                if not (0 <= d <= 168):
                    abort(400, "delay_hours must be 0-168")
                ac["delay_hours"] = d
            if "min_free_space_gb" in src:
                ac["min_free_space_gb"] = max(0.0, float(src["min_free_space_gb"]))
        if b.get("exports_dir"):
            p = Path(b["exports_dir"]).expanduser()
            if not p.is_absolute():
                abort(400, "exports_dir must be an absolute path")
            p.mkdir(parents=True, exist_ok=True)
            CONFIG["exports_dir"] = str(p)
            EXPORTS_DIR = p
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_FILE)
    return api_settings_get()


@app.get("/api/cleanup/queue")
def api_cleanup_queue_list():
    return jsonify({"items": cleanup.list_queue()})


@app.post("/api/cleanup/queue")
def api_cleanup_queue_add():
    b = request.get_json(force=True)
    stem = Path(b.get("stem") or "").name
    if not stem or not (SWAP_WORK / stem).is_dir():
        abort(404, "no workdir for that video")
    delay = float(b.get("delay_hours", CONFIG.get("auto_cleanup", {}).get("delay_hours", 24)))
    return jsonify({"queued": cleanup.enqueue(stem, delay)})


@app.post("/api/cleanup/cancel")
def api_cleanup_cancel():
    b = request.get_json(force=True)
    stem = Path(b.get("stem") or "").name
    return jsonify({"cancelled": cleanup.cancel(stem)})


def _cleanup_is_busy(stem: str) -> bool:
    with jobs_lock:
        return any(j["status"] == "running" and j.get("slug") in (stem, f"{stem}.mp4")
                   for j in jobs.values())


def _cleanup_has_export(stem: str) -> bool:
    return EXPORTS_DIR.is_dir() and any(EXPORTS_DIR.glob(f"{stem}*.mp4"))


def _cleanup_trash(stem: str) -> str:
    return soft_delete(SWAP_WORK / stem, f"workdir-{stem}")


cleanup.init(ROOT / "output" / ".cleanup-queue.json", ROOT / "output" / ".cleanup-log.json")
cleanup.start_daemon(lambda: CONFIG.get("auto_cleanup") or {},
                     _cleanup_is_busy, _cleanup_has_export, _cleanup_trash)


# ------------------------------------------------ Brand Content Studio (Coffee UI Studio)

import urllib.request as _urlreq   # noqa: E402

BRAND_COPY_PROMPT = """You are a senior direct-response brand copywriter for the premium brand \
described below. Write the ON-IMAGE copy for ONE social ad. Output STRICT JSON only.

BRAND: {brand_name}. PRODUCT (use these names EXACTLY, never invent or alter): brand is "{brand}", \
product is "{product}". Refer to the active only as "{actives}". Price/offer available: {offer}.

VOICE: premium, intimate, warm (A24 cinematic), restrained — never clinical, never hype, never \
stoner culture. COMPLIANCE (hard): "supports" framing ONLY; NO medical/disease claims (no cure, \
treat, heal, prevent, diagnose, guaranteed results); personal-experience framing ("I felt…") is ok. \
NEVER use these words: {banned}. "glow" means inner light returning, never skin/beauty.

CREATIVE BRIEF for this ad: {brief}
{inspiration}
Return ONLY this JSON (no markdown, no commentary):
{{"eyebrow": "3-6 word symptom/callout, no period",
  "headline": "the emotional hook, 4-10 words",
  "subhead": "one sentence, turns toward relief with 'supports' framing, names the product once",
  "cta": "2-4 word action",
  "price_line": "short offer line (e.g. '30 gummies · from $69 · 60-day guarantee')"}}"""


def _brand_compliance_errors(kit: dict, copy: dict) -> list[str]:
    comp = kit.get("compliance", {})
    banned = [w.lower() for w in comp.get("banned_words", [])]
    claims_re = re.compile(comp.get("banned_claims_regex", r"(?!x)x"), re.I)
    blob = " ".join(str(copy.get(k, "")) for k in ("eyebrow", "headline", "subhead", "cta", "price_line"))
    low = blob.lower()
    errs = [f"banned word '{w}'" for w in banned if re.search(rf"\b{re.escape(w)}\b", low)]
    m = claims_re.search(blob)
    if m:
        errs.append(f"medical/absolute claim '{m.group(0)}'")
    return errs


@app.get("/api/brand/health")
def api_brand_health():
    """Plain-language preflight: ComfyUI reachable? fonts present? wordmark ready?"""
    kit = load_brand_kit()
    checks = []
    # ComfyUI
    comfy_ok = False
    try:
        with _urlreq.urlopen(f"http://{COMFY_HOST}/system_stats", timeout=4) as r:
            comfy_ok = r.status == 200
    except Exception:
        comfy_ok = False
    checks.append({"name": "ComfyUI image engine", "ok": comfy_ok,
                   "fix": None if comfy_ok else "Start ComfyUI: run C:\\ComfyUI_windows_portable\\run_nvidia_lowvram.bat, then retry."})
    # fonts
    fonts = kit.get("fonts", {})
    font_missing = [role for role, s in fonts.items()
                    if not (BRAND_KIT_PATH.parent / s.get("file", "")).is_file()]
    checks.append({"name": "Brand fonts", "ok": not font_missing,
                   "fix": None if not font_missing else f"Missing font files: {font_missing}"})
    # wordmark
    wm = kit.get("wordmark", {})
    wm_file = wm.get("user_override") or wm.get("gold_on_dark", "")
    wm_ok = bool(wm_file) and (BRAND_KIT_PATH.parent / wm_file).is_file()
    checks.append({"name": "liitt wordmark", "ok": wm_ok, "approved": wm.get("approved", False),
                   "fix": None if wm_ok else "Wordmark PNG not found — regenerate it."})
    healthy = all(c["ok"] for c in checks)
    return jsonify({"healthy": healthy, "checks": checks, "brand": kit.get("brand", "")})


@app.get("/api/brand/formats")
def api_brand_formats():
    kit = load_brand_kit()
    formats = []
    if BRAND_TEMPLATES.is_dir():
        for f in sorted(BRAND_TEMPLATES.glob("*.json")):
            try:
                t = json.loads(f.read_text(encoding="utf-8"))
                formats.append({"template": f.stem, "id": t.get("id"), "label": t.get("label"),
                                "platform": t.get("platform"), "default_preset": t.get("default_preset")})
            except Exception:
                pass
    return jsonify({"formats": formats,
                    "platforms": kit.get("platforms", {}),
                    "presets": [{"id": p["id"], "label": p.get("label")} for p in kit.get("presets", [])],
                    "wordmark_approved": kit.get("wordmark", {}).get("approved", False)})


@app.post("/api/brand/copy")
def api_brand_copy():
    """Generate structured, compliant on-image copy via the local Claude CLI."""
    kit = load_brand_kit()
    if not CLAUDE_EXE:
        abort(500, "local Claude CLI not found — copy generation needs it")
    body = request.get_json(force=True)
    tpl_name = Path(body.get("template") or "").name
    tpl_path = BRAND_TEMPLATES / f"{tpl_name}.json"
    if not tpl_path.is_file():
        abort(404, "unknown template")
    tpl = json.loads(tpl_path.read_text(encoding="utf-8"))
    brief = (body.get("brief") or tpl.get("copy_brief") or "").strip()

    prod = kit.get("product", {})
    comp = kit.get("compliance", {})
    refs = []
    for hid in (body.get("hooks") or []):
        refs.append({"type": "hook", "id": hid})
    for aid in (body.get("angles") or []):
        refs.append({"type": "angle", "id": aid})
    insp = inspiration_block(refs) if refs else ""

    prompt = BRAND_COPY_PROMPT.format(
        brand_name=kit.get("brand", "liitt"), brand=prod.get("brand", "liitt"),
        product=prod.get("name", "Fairy Flame"), actives=prod.get("actives_phrase", "microdose gummies"),
        offer=", ".join(f"{k} {v}" for k, v in prod.get("prices", {}).items()) or "see site",
        banned=", ".join(comp.get("banned_words", [])), brief=brief, inspiration=insp)

    env = job_env()
    env.pop("CLAUDECODE", None)
    last_err = ""
    for attempt in range(3):
        try:
            r = subprocess.run([CLAUDE_EXE, "-p", "--model", "sonnet"], input=prompt,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=180, cwd=str(ROOT), env=env)
        except subprocess.TimeoutExpired:
            abort(504, "copywriter took too long — try again")
        out = r.stdout or ""
        i, j2 = out.find("{"), out.rfind("}")
        if i < 0 or j2 <= i:
            last_err = "no JSON returned"
            continue
        try:
            copy = json.loads(out[i:j2 + 1])
        except json.JSONDecodeError:
            last_err = "invalid JSON"
            continue
        errs = _brand_compliance_errors(kit, copy)
        if errs:
            last_err = "; ".join(errs)
            prompt = prompt + f"\n\nYour previous attempt violated compliance ({last_err}). Rewrite, fixing it."
            continue
        return jsonify({"copy": copy, "compliant": True})
    abort(502, f"could not get compliant copy after 3 tries: {last_err}")


@app.post("/api/brand/generate")
def api_brand_generate():
    """Run the full brief→imagery→composite pipeline as a background job."""
    kit = load_brand_kit()
    body = request.get_json(force=True)
    tpl_name = Path(body.get("template") or "").name
    tpl_path = BRAND_TEMPLATES / f"{tpl_name}.json"
    if not tpl_path.is_file():
        abort(404, "unknown template")
    tpl = json.loads(tpl_path.read_text(encoding="utf-8"))
    copy = body.get("copy")
    if not isinstance(copy, dict) or not copy.get("headline"):
        abort(400, "need generated copy (call /api/brand/copy first)")
    errs = _brand_compliance_errors(kit, copy)
    if errs:
        abort(400, "copy fails compliance: " + "; ".join(errs))
    platform = tpl.get("platform")
    if platform not in kit.get("platforms", {}):
        abort(400, "template platform not in brand kit")

    campaign = secure_filename(body.get("campaign") or f"camp-{time.strftime('%Y%m%d-%H%M%S')}")
    BRAND_OUT.mkdir(parents=True, exist_ok=True)
    (BRAND_OUT / campaign).mkdir(exist_ok=True)
    content_file = BRAND_OUT / campaign / "_copy.json"
    content_file.write_text(json.dumps(copy, indent=1), encoding="utf-8")

    cmd = [str(Path(CONFIG["venvs"]["cv"])), str(BRAND_CONTENT_PY),
           "--kit", str(BRAND_KIT_PATH), "--platform", platform,
           "--template", str(tpl_path), "--content", str(content_file),
           "--campaign", campaign, "--out-dir", str(BRAND_OUT),
           "--scripts-dir", str(COMFY_SCRIPTS), "--comfy-url", COMFY_HOST,
           "--seed", str(int(body.get("seed") or 1))]
    if body.get("preset"):
        cmd += ["--preset", str(body["preset"])]
    if body.get("bg_prompt"):
        cmd += ["--bg-prompt", str(body["bg_prompt"])]

    job_id = jobs_create("brand-content", campaign,
                         f"Brand ad ({tpl.get('label', tpl_name)}) — {campaign}",
                         gpu=True)   # ComfyUI generate + upscale hold the GPU
    threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id, "campaign": campaign})


@app.get("/api/brand/campaigns")
def api_brand_campaigns():
    camps = []
    if BRAND_OUT.is_dir():
        for d in sorted(BRAND_OUT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            imgs = sorted([p for p in d.glob("*.jpg")] + [p for p in d.glob("*.png")],
                          key=lambda p: p.stat().st_mtime, reverse=True)
            imgs = [p for p in imgs if not p.name.startswith(("background-", "_"))]
            if not imgs:
                continue
            camps.append({"campaign": d.name, "mtime": d.stat().st_mtime,
                          "images": [f"/brand-out/{d.name}/{p.name}" for p in imgs]})
    return jsonify({"campaigns": camps})


@app.get("/brand-out/<path:rel>")
def brand_out(rel):
    target = (BRAND_OUT / rel).resolve()
    if not str(target).startswith(str(BRAND_OUT.resolve())) or target.suffix.lower() not in (
            ".jpg", ".jpeg", ".png"):
        abort(403)
    if not target.is_file():
        abort(404)
    return send_file(target, conditional=True)


@app.get("/api/brand/wordmark")
def api_brand_wordmark():
    kit = load_brand_kit()
    wm = kit.get("wordmark", {})
    rel = wm.get("gold_on_dark", "")
    return jsonify({"img": f"/media/banks/{rel}" if rel else None,
                    "approved": wm.get("approved", False)})


@app.post("/api/brand/wordmark/approve")
def api_brand_wordmark_approve():
    kit = load_brand_kit()
    kit.setdefault("wordmark", {})["approved"] = True
    BRAND_KIT_PATH.write_text(json.dumps(kit, indent=2), encoding="utf-8")
    return jsonify({"approved": True})


@app.post("/api/brand/wordmark/upload")
def api_brand_wordmark_upload():
    """Install the OFFICIAL logo (e.g. exported from the Figma brand kit) as the
    locked wordmark override — from then on every post uses this exact file."""
    f = request.files.get("logo")
    if not f or not f.filename:
        abort(400, "no file")
    if Path(f.filename).suffix.lower() not in (".png", ".webp"):
        abort(400, "PNG (transparent background) expected")
    dest_dir = BRAND_KIT_PATH.parent / "brand-assets" / "wordmark"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "official-logo.png"
    f.save(dest)
    kit = load_brand_kit()
    wm = kit.setdefault("wordmark", {})
    wm["user_override"] = "brand-assets/wordmark/official-logo.png"
    wm["approved"] = True
    BRAND_KIT_PATH.write_text(json.dumps(kit, indent=2), encoding="utf-8")
    return jsonify({"installed": True, "path": str(dest)})


@app.get("/brand-studio")
def brand_studio_page():
    return send_from_directory(STATIC, "brand-studio.html")


# ---------------------------------------------------------------- static
# The Creator IS the app: one page, the whole workflow. Every old tab route
# 302s into the Creator modal at the right step (?v= carries the video).
# The old pages stay reachable as "labs" under Power Tools for edge tooling.

@app.get("/")
def index():
    return send_from_directory(STATIC, "creator-studio.html")


@app.get("/settings")
def settings_page():
    return send_from_directory(STATIC, "settings.html")


@app.post("/api/extract-audio")
def api_extract_audio():
    """Editor Audio tool: pull the audio track out of a library video as an mp3.
    Fast, synchronous ffmpeg — the file lands in output/edits/ (shows in Exports)."""
    fname = Path((request.get_json(force=True) or {}).get("file") or "").name
    src = UPLOADS / fname
    if not fname or not src.is_file():
        abort(404, "upload not found")
    if not has_audio_stream(src):
        abort(400, "this video has no audio track to extract")
    out_dir = ROOT / "output" / "edits"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{src.stem}-audio.mp3"
    r = subprocess.run([ff_tool("ffmpeg"), "-y", "-loglevel", "error", "-i", str(src),
                        "-vn", "-codec:a", "libmp3lame", "-b:a", "192k", str(dest)],
                       env=job_env(), timeout=300)
    if r.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        abort(500, "audio extraction failed")
    rel = str(dest.relative_to(ROOT)).replace("\\", "/")
    return jsonify({"audio": rel, "size": dest.stat().st_size})


ASPECT_RATIOS = {"9:16": 9 / 16, "1:1": 1.0, "16:9": 16 / 9}


@app.post("/api/export-aspects")
def api_export_aspects():
    """Stage-15 of the VSL pipeline: cut the finished master into platform
    aspect ratios (center-crop-to-fill) — 9:16 / 1:1 / 16:9. Synchronous
    ffmpeg per aspect; results land in output/edits/ (visible in Exports)."""
    b = request.get_json(force=True)
    stem = Path(b.get("stem") or "").name
    aspects = [a for a in (b.get("aspects") or []) if a in ASPECT_RATIOS]
    use_captioned = bool(b.get("captioned"))
    work = SWAP_WORK / stem
    src = work / ("final-captioned.mp4" if use_captioned else "final.mp4")
    if use_captioned and not src.is_file():
        src = SUBSTUDIO_OUT / stem / "captioned.mp4"
    if not stem or not src.is_file():
        abort(404, "master not found — dub (and caption) first")
    if not aspects:
        abort(400, "pick at least one aspect ratio")
    out_dir = ROOT / "output" / "edits"
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for a in aspects:
        ar = ASPECT_RATIOS[a]
        tag = a.replace(":", "x")
        dest = out_dir / f"{stem}-{tag}{'-captioned' if use_captioned else ''}.mp4"
        # crop to fill the target ratio (center), keep even dimensions
        vf = (f"crop='if(gt(iw/ih,{ar}),ih*{ar},iw)':'if(gt(iw/ih,{ar}),ih,iw/{ar})',"
              "crop=trunc(iw/2)*2:trunc(ih/2)*2")
        r = subprocess.run([ff_tool("ffmpeg"), "-y", "-loglevel", "error", "-i", str(src),
                            "-vf", vf, "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(dest)],
                           env=job_env(), timeout=600)
        if r.returncode == 0 and dest.is_file() and dest.stat().st_size:
            made.append({"aspect": a, "path": str(dest.relative_to(ROOT)).replace("\\", "/"),
                         "size": dest.stat().st_size})
    if not made:
        abort(500, "aspect export failed")
    return jsonify({"masters": made})


@app.get("/editor")
def editor_page():
    # CapCut-style editor shell (beta) — a second skin over the same APIs;
    # "/" (the one-page Creator) remains the default until the user promotes this.
    return send_from_directory(STATIC, "editor.html")


_LEGACY_STEP = {
    "/library": None, "/new": "__new__",
    "/transcript": "script",
    "/subtitles": "source", "/eraser": "source", "/recovery": "source",
    "/dubbing": "dub",
    "/captions": "captions",
    "/dubsync": "fix", "/qc": "fix",
    "/clone": "deliver",
}


def _legacy_redirect(step):
    def handler():
        from urllib.parse import urlencode
        q = {}
        v = request.args.get("v") or request.args.get("file")
        if v:
            q["v"] = v
        if step == "__new__":
            q["new"] = "1"
        elif step:
            q["step"] = step
        return redirect("/" + ("?" + urlencode(q) if q else ""), code=302)
    return handler


for _path, _step in _LEGACY_STEP.items():
    app.add_url_rule(_path, f"legacy_{_path.strip('/')}", _legacy_redirect(_step))

_LABS = {"/qc-lab": "qc.html", "/dubsync-lab": "dubsync.html",
         "/clone-lab": "clone.html", "/subtitles-lab": "subtitles.html",
         "/dubbing-lab": "dubbing.html", "/transcript-lab": "transcript.html"}


def _lab_page(fname):
    def handler():
        return send_from_directory(STATIC, fname)
    return handler


for _path, _file in _LABS.items():
    app.add_url_rule(_path, f"lab_{_path.strip('/')}", _lab_page(_file))


@app.get("/exports")
def exports_page():
    return send_from_directory(STATIC, "exports.html")


@app.get("/tools")
def tools_page():
    return send_from_directory(STATIC, "tools.html")


@app.get("/remote")
def remote_page():
    return send_from_directory(STATIC, "remote.html")


@app.get("/mission")
def mission_page():
    return send_from_directory(STATIC, "index.html")


@app.get("/creator")
def creator_page():
    # Ads Factory (the original autoVSL creator flows) — distinct from "/"
    return send_from_directory(STATIC, "creator.html")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC, name)


# ---------------------------------------------------------------- ComfyUI Studio
# Local, free image tools (generate / upscale / inpaint / keyframe) driven by
# scripts/comfyui_studio.py against a running ComfyUI (127.0.0.1:8188).
STUDIO_OUT = ROOT / "output" / "comfyui-studio"
STUDIO_IN = STUDIO_OUT / "_in"


@app.get("/studio")
def studio_page():
    return send_from_directory(STATIC, "studio.html")


@app.get("/studio-out/<path:name>")
def studio_out(name):
    return send_from_directory(STUDIO_OUT, name)


@app.get("/api/studio/brand")
def studio_brand():
    """liitt / Fairy Flame brand kit as generation presets (banks/liitt-brand-kit.json)."""
    f = ROOT / "banks" / "liitt-brand-kit.json"
    if not f.is_file():
        return jsonify({"presets": [], "style_suffix": "", "negative_prompt": ""})
    return send_file(str(f), mimetype="application/json")


@app.post("/api/studio/upload")
def studio_upload():
    f = request.files.get("image")
    if not f:
        abort(400, "no image uploaded")
    STUDIO_IN.mkdir(parents=True, exist_ok=True)
    ext = Path(f.filename or "img.png").suffix.lower() or ".png"
    dest = STUDIO_IN / f"up_{uuid.uuid4().hex[:8]}{ext}"
    f.save(str(dest))
    return jsonify({"path": str(dest)})


@app.post("/api/studio/run")
def studio_run():
    b = request.get_json(force=True) or {}
    mode = b.get("mode")
    if mode not in ("generate", "upscale", "inpaint", "keyframe"):
        abort(400, "bad mode")
    venv_py = Path(CONFIG["venvs"]["cv"])
    script = ROOT / "scripts" / "comfyui_studio.py"
    cmd = [str(venv_py), str(script), mode]
    if mode == "generate":
        cmd += [b.get("prompt", ""),
                "--count", str(int(b.get("count", 4))),
                "--width", str(int(b.get("width", 512))),
                "--height", str(int(b.get("height", 768)))]
        if b.get("negative"):
            cmd += ["--negative", b["negative"]]
    elif mode == "upscale":
        cmd += [b.get("image", "")]
    elif mode == "inpaint":
        cmd += [b.get("image", ""), b.get("prompt", "")]
        if b.get("mask"):
            cmd += ["--mask", b["mask"]]
    elif mode == "keyframe":
        cmd += [b.get("image", ""), b.get("prompt", ""),
                "--width", str(int(b.get("width", 512))),
                "--height", str(int(b.get("height", 768))),
                "--strength", str(float(b.get("strength", 0.8)))]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=2400)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "results": [], "log": "timed out (>40 min)"})
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    results = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("→"):   # the "→ <path>" result lines
            p = line[1:].strip()
            try:
                rel = str(Path(p).resolve().relative_to(STUDIO_OUT.resolve()))
                results.append("/studio-out/" + rel.replace("\\", "/"))
            except Exception:
                pass
    return jsonify({"ok": bool(r.returncode == 0 and results),
                    "results": results, "log": out[-3000:]})


# ------------------------------------------------ B-Roll Factory
# Reference b-roll in → shot recipe → ComfyUI still → motion → tagged bank entry.
# Free and local end to end except the `fal` motion path, which is cost-gated
# exactly like /api/i2v/run. Engine: app/engines/broll_factory.py.

BROLL_ENGINE = APP_DIR / "engines" / "broll_factory.py"
BROLL_OUT = ROOT / "output" / "broll"
BROLL_REFS = BROLL_OUT / "_refs"
BROLL_BANK = Path(CONFIG.get("banks_dir") or (ROOT / "banks")) / "broll.jsonl"
BROLL_ASSETS = ROOT / "assets" / "broll"
BROLL_REF_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v",
                  ".jpg", ".jpeg", ".png", ".webp"}
BROLL_MOTIONS = ("push_in", "pull_out", "pan_left", "pan_right",
                 "tilt_up", "tilt_down", "drift", "static")
# Dirs a /broll-file/ URL may read from. Everything else is refused.
BROLL_SERVE_ROOTS = (BROLL_OUT, BROLL_ASSETS)


def _cv_py() -> str:
    return str(Path(CONFIG["venvs"]["cv"]))


def rel_from_root(p: Path) -> str:
    """Repo-relative, forward-slashed. Falls back to the absolute path for anything
    outside ROOT so a stray reference still round-trips instead of raising."""
    try:
        return str(Path(p).resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def _broll_batch_dir(batch: str) -> Path:
    d = (BROLL_OUT / batch).resolve()
    if not str(d).startswith(str(BROLL_OUT.resolve())) or d == BROLL_OUT.resolve():
        abort(400, "bad batch")
    return d


def _broll_rows() -> list[dict]:
    if not BROLL_BANK.is_file():
        return []
    rows = []
    for line in BROLL_BANK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _broll_write_rows(rows: list[dict]) -> None:
    tmp = BROLL_BANK.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                   encoding="utf-8")
    tmp.replace(BROLL_BANK)


def _broll_url(rel: str | None) -> str | None:
    """Map a repo-relative bank path to a servable URL, or None if unreadable."""
    if not rel:
        return None
    p = (ROOT / str(rel).replace("\\", "/")).resolve()
    for base in BROLL_SERVE_ROOTS:
        b = base.resolve()
        if str(p).startswith(str(b)) and p.is_file():
            return "/broll-file/" + str(p.relative_to(ROOT.resolve())).replace("\\", "/")
    return None


@app.get("/broll")
def page_broll():
    return send_from_directory(STATIC, "broll.html")


@app.get("/broll-file/<path:rel>")
def broll_file(rel: str):
    p = (ROOT / rel.replace("\\", "/")).resolve()
    if not any(str(p).startswith(str(b.resolve())) for b in BROLL_SERVE_ROOTS):
        abort(403, "outside the b-roll dirs")
    if not p.is_file():
        abort(404)
    return send_file(str(p), conditional=True)


@app.get("/api/broll/health")
def api_broll_health():
    """What the factory can do right now — ComfyUI reachability and installed models."""
    try:
        r = subprocess.run([_cv_py(), str(BROLL_ENGINE), "health"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=45, env=job_env())
        data = json.loads((r.stdout or "{}").strip().splitlines()[-1])
    except Exception as exc:                                   # noqa: BLE001
        return jsonify({"comfyui": False, "error": str(exc)[:200]})
    data["claude"] = bool(CLAUDE_EXE)
    return jsonify(data)


@app.post("/api/broll/upload")
def api_broll_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "no file")
    ext = Path(f.filename).suffix.lower()
    if ext not in BROLL_REF_EXTS:
        abort(400, f"reference must be one of: {', '.join(sorted(BROLL_REF_EXTS))}")
    BROLL_REFS.mkdir(parents=True, exist_ok=True)
    base = secure_filename(Path(f.filename).stem).strip(".-_") or "ref"
    name = f"{base}-{time.strftime('%H%M%S')}{ext}"
    f.save(BROLL_REFS / name)
    return jsonify({"name": name, "path": f"output/broll/_refs/{name}"})


@app.get("/api/broll/refs")
def api_broll_refs():
    """Uploaded references plus anything already in the library, so a clip that is
    on the box can be used as inspiration without re-uploading it."""
    refs = []
    if BROLL_REFS.is_dir():
        for p in sorted(BROLL_REFS.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.is_file() and p.suffix.lower() in BROLL_REF_EXTS:
                refs.append({"name": p.name, "path": f"output/broll/_refs/{p.name}",
                             "kind": "upload", "size": p.stat().st_size})
    lib = []
    if UPLOADS.is_dir():
        for p in sorted(UPLOADS.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.is_file() and p.suffix.lower() in BROLL_REF_EXTS:
                lib.append({"name": p.name, "path": f"uploads/{p.name}",
                            "kind": "library", "size": p.stat().st_size})
    return jsonify({"refs": refs, "library": lib[:60]})


@app.post("/api/broll/analyze")
def api_broll_analyze():
    b = request.get_json(force=True)
    rels = [str(r).replace("\\", "/") for r in (b.get("refs") or []) if str(r).strip()]
    if not rels:
        abort(400, "pick at least one reference clip or still")
    if not CLAUDE_EXE:
        abort(500, "claude CLI not found — install Claude Code or add it to PATH")
    srcs = []
    for rel in rels:
        p = (ROOT / rel).resolve()
        ok = (str(p).startswith(str(BROLL_REFS.resolve()))
              or str(p).startswith(str(UPLOADS.resolve()))
              or str(p).startswith(str(BROLL_ASSETS.resolve())))
        if not ok or not p.is_file():
            abort(400, f"reference not readable: {rel}")
        srcs.append(p)

    shots = max(1, min(12, int(b.get("shots", 6))))
    brief = (b.get("brief") or "").strip()
    aspect = b.get("aspect", "9:16")
    if aspect not in ("9:16", "1:1", "16:9"):
        abort(400, "bad aspect")
    batch = secure_filename(b.get("batch") or "").strip(".-_") or None

    cmd = [_cv_py(), str(BROLL_ENGINE), "analyze", "--shots", str(shots),
           "--aspect", aspect, "--frames-per-ref", str(max(2, min(10, int(b.get("frames", 6))))),
           "--model", b.get("model") if b.get("model") in ("sonnet", "opus", "haiku") else "sonnet"]
    for s in srcs:
        cmd += ["--ref", str(s)]
    if brief:
        cmd += ["--brief", brief]
    if b.get("brand"):
        cmd.append("--brand")
    if batch:
        cmd += ["--batch", batch]

    slug = batch or Path(srcs[0]).stem[:24]
    job_id = jobs_create("broll-analyze", slug, f"B-roll analyze — {len(srcs)} reference(s)")
    threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.get("/api/broll/batches")
def api_broll_batches():
    items = []
    if BROLL_OUT.is_dir():
        for d in sorted(BROLL_OUT.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not d.is_dir() or d.name.startswith("_") or d.name == "singles":
                continue
            recipe = read_json(d / "recipe.json")
            if not recipe:
                continue
            gen = read_json(d / "generated.json") or {}
            clips = sorted((d / "clips").glob("*.mp4")) if (d / "clips").is_dir() else []
            items.append({
                "batch": d.name,
                "brief": recipe.get("brief"),
                "created": recipe.get("created"),
                "aspect": recipe.get("aspect"),
                "brand": recipe.get("brand"),
                "style": recipe.get("style") or {},
                "shot_count": len(recipe.get("shots") or []),
                "clip_count": len(clips),
                "failed": gen.get("failed") or [],
                "references": [r.get("name") for r in (recipe.get("references") or [])],
            })
    return jsonify({"items": items})


@app.get("/api/broll/recipe/<batch>")
def api_broll_recipe_get(batch: str):
    d = _broll_batch_dir(batch)
    recipe = read_json(d / "recipe.json")
    if not recipe:
        abort(404, "no recipe for that batch")
    gen = read_json(d / "generated.json") or {}
    done = {e.get("shot_id"): e for e in (gen.get("generated") or [])}
    for s in recipe.get("shots") or []:
        made = done.get(s["id"])
        clip = d / "clips" / f"{s['id']}.mp4"
        still = d / "stills" / f"{s['id']}.png"
        s["clip_url"] = _broll_url(rel_from_root(clip)) if clip.is_file() else None
        s["still_url"] = _broll_url(rel_from_root(still)) if still.is_file() else None
        s["bank_id"] = (made or {}).get("id")
        s["style_ref_url"] = _broll_url(rel_from_root(Path(s["style_ref"]))) \
            if s.get("style_ref") else None
    for f in recipe.get("frames") or []:
        f["url"] = _broll_url(rel_from_root(Path(f["path"])))
    return jsonify(recipe)


@app.post("/api/broll/recipe/<batch>")
def api_broll_recipe_save(batch: str):
    """Persist hand-edits to the shot list before generating."""
    d = _broll_batch_dir(batch)
    path = d / "recipe.json"
    recipe = read_json(path)
    if not recipe:
        abort(404, "no recipe for that batch")
    incoming = (request.get_json(force=True) or {}).get("shots")
    if not isinstance(incoming, list) or not incoming:
        abort(400, "send a non-empty shots array")
    by_id = {s["id"]: s for s in recipe.get("shots") or []}
    kept = []
    for s in incoming:
        base = by_id.get(s.get("id"))
        if not base:
            continue
        mo = s.get("motion") or {}
        if mo.get("type") in BROLL_MOTIONS:
            base["motion"]["type"] = mo["type"]
        try:
            base["motion"]["intensity"] = max(0.03, min(0.35, float(mo.get(
                "intensity", base["motion"]["intensity"]))))
        except (TypeError, ValueError):
            pass
        try:
            base["duration_s"] = max(2.0, min(12.0, float(s.get("duration_s",
                                                                base["duration_s"]))))
        except (TypeError, ValueError):
            pass
        for k in ("title", "prompt", "negative", "emotional_beat", "product_moment"):
            if isinstance(s.get(k), str):
                base[k] = s[k].strip()
        for k in ("beat_tags", "avatar_fit"):
            if isinstance(s.get(k), list):
                base[k] = [str(t).strip() for t in s[k] if str(t).strip()]
        kept.append(base)
    if not kept:
        abort(400, "none of those shot ids exist in this batch")
    recipe["shots"] = kept
    path.write_text(json.dumps(recipe, indent=2, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True, "shots": len(kept)})


def _broll_fal_estimate(recipe: dict, shot_ids: list[str], model: str) -> dict:
    shots = [s for s in (recipe.get("shots") or [])
             if not shot_ids or s["id"] in shot_ids]
    m = I2V_MODELS.get(model) or I2V_MODELS["kling-2.1"]
    segs = sum(max(1, round(float(s.get("duration_s") or 4.0) / m["seg"])) for s in shots)
    total = round(segs * m["cost_per_seg"], 2)
    return {"this_run": total, "segments": segs, "shots": len(shots),
            "engine": "fal-i2v", "model": model,
            "summary": f"{len(shots)} shot(s), {segs} × {m['seg']}s on "
                       f"{m['label'].split(' — ')[0]} ≈ ${total:.2f}"}


@app.post("/api/broll/generate")
def api_broll_generate():
    b = request.get_json(force=True)
    batch = b.get("batch") or ""
    d = _broll_batch_dir(batch)
    recipe = read_json(d / "recipe.json")
    if not recipe:
        abort(404, "no recipe for that batch")

    motion = b.get("motion", "ken")
    if motion not in ("ken", "anim", "fal", "ltx"):
        abort(400, "motion must be ken, anim, fal or ltx")
    style = b.get("style", "auto")
    if style not in ("auto", "ipadapter", "controlnet", "text"):
        abort(400, "bad style mode")
    ids = [str(s).strip() for s in (b.get("shots") or []) if str(s).strip()]
    known = {s["id"] for s in recipe.get("shots") or []}
    unknown = [i for i in ids if i not in known]
    if unknown:
        abort(400, f"unknown shot id(s): {', '.join(unknown)}")

    est = None
    if motion == "fal":
        model = b.get("fal_model", "kling-2.1")
        if model not in I2V_MODELS:
            abort(400, "unknown fal model")
        est = _broll_fal_estimate(recipe, ids, model)
        if not b.get("confirm_cost"):
            return jsonify({"needs_confirm": True, "estimate": est}), 402

    cmd = [_cv_py(), str(BROLL_ENGINE), "generate", "--recipe", str(d / "recipe.json"),
           "--motion", motion, "--style", style,
           "--steps", str(max(8, min(50, int(b.get("steps", 26))))),
           "--fps", str(max(12, min(60, int(b.get("fps", 30))))),
           "--grain", str(max(0, min(20, int(b.get("grain", 4)))))]
    if ids:
        cmd += ["--shots", ",".join(ids)]
    if b.get("seed"):
        cmd += ["--seed", str(int(b["seed"]))]
    if b.get("no_upscale"):
        cmd.append("--no-upscale")
    if b.get("no_bank"):
        cmd.append("--no-bank")
    if motion == "fal":
        cmd += ["--fal-model", b.get("fal_model", "kling-2.1")]

    label = f"B-roll generate — {batch} [{motion}]"
    job_id = jobs_create("broll-gen", batch, label)
    if est:
        threading.Thread(target=run_i2v_job, args=(job_id, cmd, batch, est),
                         daemon=True).start()
    else:
        threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id, "estimate": est})


@app.post("/api/broll/animate")
def api_broll_animate():
    """Animate a still that already exists — the six FLUX stills in the bank are all
    tagged 'ken-burns source' and were never given a camera move."""
    b = request.get_json(force=True)
    rel = (b.get("still") or "").replace("\\", "/")
    p = (ROOT / rel).resolve()
    if not any(str(p).startswith(str(base.resolve())) for base in BROLL_SERVE_ROOTS) \
            or not p.is_file():
        abort(400, "still must be an image inside assets/broll or output/broll")
    mtype = b.get("motion_type", "push_in")
    if mtype not in BROLL_MOTIONS:
        abort(400, "bad motion type")
    engine = b.get("engine", "ken")
    if engine not in ("ken", "ltx"):
        abort(400, "engine must be ken or ltx")
    if engine == "ltx" and len((b.get("prompt") or "").strip()) < 3:
        abort(400, "the ltx engine needs a prompt describing the scene/motion")
    aspect = b.get("aspect", "9:16")
    if aspect not in ("9:16", "1:1", "16:9"):
        abort(400, "bad aspect")
    out = BROLL_OUT / "singles" / f"{p.stem}-{engine}-{mtype}-{time.strftime('%H%M%S')}.mp4"
    cmd = [_cv_py(), str(BROLL_ENGINE), "motion", "--still", str(p), "--out", str(out),
           "--engine", engine, "--motion-type", mtype, "--aspect", aspect,
           "--intensity", str(max(0.03, min(0.35, float(b.get("intensity", 0.12))))),
           "--duration", str(max(2.0, min(12.0, float(b.get("duration", 4.0))))),
           "--fps", str(max(12, min(60, int(b.get("fps", 30))))),
           "--grain", str(max(0, min(20, int(b.get("grain", 4)))))]
    if engine == "ltx":
        cmd += ["--prompt", b["prompt"].strip()]
    if b.get("no_upscale"):
        cmd.append("--no-upscale")
    job_id = jobs_create("broll-motion", p.stem,
                         f"Animate still — {p.name} [{engine}:{mtype}]")
    threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id, "out": rel_from_root(out)})


@app.get("/api/broll/bank")
def api_broll_bank():
    rows = []
    for r in _broll_rows():
        f = r.get("file")
        url = _broll_url(f)
        is_video = str(f or "").lower().endswith((".mp4", ".mov", ".webm", ".mkv"))
        rows.append({**r, "url": url, "still_url": _broll_url(r.get("still")),
                     "is_video": is_video, "missing": url is None})
    return jsonify({"rows": rows, "count": len(rows)})


@app.post("/api/broll/bank/update")
def api_broll_bank_update():
    """Edit the tags/quality/status of one bank row, or retire it."""
    b = request.get_json(force=True)
    bid = (b.get("id") or "").strip()
    if not bid:
        abort(400, "no id")
    rows = _broll_rows()
    hit = next((r for r in rows if r.get("id") == bid), None)
    if not hit:
        abort(404, "no such bank id")
    for k in ("shot", "emotional_beat", "product_moment", "quality", "status", "rights"):
        if isinstance(b.get(k), str) and b[k].strip():
            hit[k] = b[k].strip()
    for k in ("beat_tags", "avatar_fit"):
        if isinstance(b.get(k), list):
            hit[k] = [str(t).strip() for t in b[k] if str(t).strip()]
    _broll_write_rows(rows)
    return jsonify({"ok": True, "row": hit})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", CONFIG.get("port", 5181)))
    # bind to all interfaces (phone access) only when lan_access is on AND a PIN is set
    lan = bool(CONFIG.get("lan_access")) and bool(REMOTE_PIN)
    host = "0.0.0.0" if lan else "127.0.0.1"
    print(f"Video Studio -> http://localhost:{port}  (data root: {ROOT})")
    if lan:
        print(f"  phone/LAN access ON -> http://<this-PC-IP>:{port}  (PIN required)")
    app.run(host=host, port=port, debug=False, threaded=True)
