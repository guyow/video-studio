#!/usr/bin/env python3
"""autoVSL dashboard — local control panel for the ad factory pipeline.

Run:  .venv/Scripts/python.exe dashboard/server.py   (from repo root)
Then open http://localhost:5170
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
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

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
UPLOADS = ROOT / "uploads"
TRANSCRIPTS = UPLOADS / "transcripts"
COURSE_PIPELINE = ROOT.parent / "course_pipeline"
TRANSCRIBE_PY = COURSE_PIPELINE / "transcribe.py"
TRANSCRIBE_VENV_PY = COURSE_PIPELINE / ".venv" / "Scripts" / "python.exe"
MEDIA_UPLOAD_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".mp3", ".m4a", ".wav"}
BASH = r"C:\Program Files\Git\bin\bash.exe"
FFMPEG_BIN = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1.2-full_build/bin"
)

app = Flask(__name__, static_folder=None)

# ---------------------------------------------------------------- job runner

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

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
    lip = tier if engine == "fal" else tier  # both use the tier name; local voice is free
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
    try:
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
        job_id = uuid.uuid4().hex[:8]
        jobs[job_id] = {
            "id": job_id, "action": action, "slug": fname,
            "label": f"Transcribe — {fname}",
            "status": "running", "lines": [], "returncode": None,
            "started": time.time(), "ended": None,
        }
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
        cmd = [str(TRANSCRIBE_VENV_PY), str(Path(__file__).parent / "caption.py"), "--name", stem]
        job_id = uuid.uuid4().hex[:8]
        jobs[job_id] = {
            "id": job_id, "action": action, "slug": stem,
            "label": f"Burn captions — {stem}",
            "status": "running", "lines": [], "returncode": None,
            "started": time.time(), "ended": None,
        }
        threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
        return jsonify({"job_id": job_id})

    if action == "recaption":
        fname = Path(body.get("file", "")).name
        src = UPLOADS / fname
        if not fname or not src.is_file():
            abort(400, "file not found in uploads/")
        if not TRANSCRIBE_VENV_PY.is_file():
            abort(500, "transcribe venv missing (needed for word timing)")
        cmd = [str(TRANSCRIBE_VENV_PY), str(Path(__file__).parent / "caption.py"),
               "--video", str(src)]
        job_id = uuid.uuid4().hex[:8]
        jobs[job_id] = {
            "id": job_id, "action": action, "slug": src.stem,
            "label": f"New subtitles — {fname} (from original audio)",
            "status": "running", "lines": [], "returncode": None,
            "started": time.time(), "ended": None,
        }
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
        venv_py = ROOT / ".venv" / "Scripts" / "python.exe"

        if engine == "local":
            # local XTTS voice (free); lip-sync "none"/"wav2lip" are free (local GPU),
            # any fal tier costs money
            lipsync = body.get("lipsync") if body.get("lipsync") in (
                "none", "wav2lip", "wav2lip-hd", "latentsync", "musetalk",
                "veed", "standard", "pro") else "none"
            paid = lipsync in ("latentsync", "musetalk", "veed", "standard", "pro")
            if paid and not body.get("confirm_cost"):
                abort(400, f"lip-sync '{lipsync}' runs on fal.ai and costs money — needs cost approval (confirm_cost)")
            cmd = [str(venv_py), str(Path(__file__).parent / "local_dub.py"), str(src),
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
            label = (f"Local dub — {fname} (XTTS voice"
                     + (", free)" if not paid else f" + {lipsync} lip-sync $)"))
            cost_ctx = {"engine": "local", "tts": "local", "tier": lipsync,
                        "video": str(src), "stem": stem, "paid": paid}
        else:
            # cloud pipeline: always costs money
            if not body.get("confirm_cost"):
                abort(400, "FAL.AI dub spends money (voice-clone + TTS + lip-sync) — needs cost approval (confirm_cost)")
            tier = body.get("tier") if body.get("tier") in ("pro", "standard", "veed", "latentsync", "musetalk") else "pro"
            tts = body.get("tts") if body.get("tts") in ("hd", "turbo", "f5") else "hd"
            cmd = [str(venv_py), str(Path(__file__).parent / "dub.py"), str(src),
                   "--name", stem, "--tier", tier, "--tts", tts]
            if body.get("captions", True):
                cmd.append("--captions")
            label = f"FAL.AI dub — {fname} (voice:{tts}, sync:{tier}) $"
            cost_ctx = {"engine": "fal", "tts": tts, "tier": tier,
                        "video": str(src), "stem": stem, "paid": True}

        job_id = uuid.uuid4().hex[:8]
        jobs[job_id] = {
            "id": job_id, "action": action, "slug": stem,
            "label": label,
            "status": "running", "lines": [], "returncode": None,
            "started": time.time(), "ended": None,
        }
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

    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {
        "id": job_id, "action": action, "slug": slug,
        "label": f"{ACTIONS[action]['label']} — {slug}" if slug else ACTIONS[action]["label"],
        "status": "running", "lines": [], "returncode": None,
        "started": time.time(), "ended": None,
    }
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

def ffmpeg_exe(name: str) -> str:
    p = FFMPEG_BIN / f"{name}.exe"
    return str(p) if p.is_file() else name


def clean_subs_worker(job_id: str, fname: str, box: dict, mode: str) -> None:
    """Clean a burned-in subtitle region (OpenCV per-frame engine); original is backed up."""
    job = jobs[job_id]

    def log(line: str) -> None:
        with jobs_lock:
            job["lines"].append(line)

    try:
        src = UPLOADS / fname
        probe = subprocess.run(
            [ffmpeg_exe("ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, check=True)
        vw, vh = (int(n) for n in probe.stdout.strip().split(",")[:2])
        tmp = UPLOADS / f"{src.stem}.cleaning{src.suffix}"
        venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
        if box is None:
            # one-click mode: erase_subs.py finds the caption band itself (ProPainter AI fill)
            x = y = w = h = None
            log(f"Auto-detecting the caption region of {vw}x{vh} — mode: erase (AI inpaint, audio untouched)")
            cmd = [str(venv_py), str(Path(__file__).parent / "erase_subs.py"), str(src), str(tmp)]
        elif mode == "erase":
            x = max(0, min(int(box["x"]), vw - 4))
            y = max(0, min(int(box["y"]), vh - 4))
            w = max(4, min(int(box["w"]), vw - x))
            h = max(4, min(int(box["h"]), vh - y))
            log(f"Cleaning {w}x{h} region at ({x},{y}) of {vw}x{vh} — mode: {mode} (audio untouched)")
            # AI video inpainting (ProPainter) — reconstructs the real background behind
            # the letters from neighboring frames; slow (GPU, minutes) but the best fill
            cmd = [str(venv_py), str(Path(__file__).parent / "erase_subs.py"), str(src), str(tmp),
                   "--x", str(x), "--y", str(y), "--w", str(w), "--h", str(h)]
        else:
            x = max(0, min(int(box["x"]), vw - 4))
            y = max(0, min(int(box["y"]), vh - 4))
            w = max(4, min(int(box["w"]), vw - x))
            h = max(4, min(int(box["h"]), vh - y))
            log(f"Cleaning {w}x{h} region at ({x},{y}) of {vw}x{vh} — mode: {mode} (audio untouched)")
            cmd = [str(venv_py), str(Path(__file__).parent / "subclean.py"), str(src),
                   "--box", str(x), str(y), str(w), str(h), "--mode", mode, "--out", str(tmp)]
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
    venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
    if mode == "erase":
        # approximate single-frame preview (text mask + spatial inpaint) — the real run
        # uses ProPainter video inpainting, which fills noticeably better than this
        cmd = [str(venv_py), str(Path(__file__).parent / "erase_subs.py"), str(src), str(out),
               "--x", str(box["x"]), "--y", str(box["y"]),
               "--w", str(box["w"]), "--h", str(box["h"]), "--preview-at", str(t)]
    else:
        cmd = [str(venv_py), str(Path(__file__).parent / "subclean.py"), str(src),
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
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"id": job_id, "action": "clean-subs", "slug": fname,
                    "label": f"Remove subtitles — {fname} ({mode})", "status": "running",
                    "lines": [], "returncode": None, "started": time.time(), "ended": None}
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

    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"id": job_id, "action": "build-vsl", "slug": vsl_slug,
                    "label": f"Build VSL — {vsl_slug}", "status": "running",
                    "lines": [], "returncode": None, "started": time.time(), "ended": None}
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
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"id": job_id, "action": "qc-ai", "slug": src.stem,
                    "label": f"AI QC review — {src.name}", "status": "running",
                    "lines": [], "returncode": None, "started": time.time(), "ended": None}
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
        venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
        cmd = [str(venv_py), str(Path(__file__).parent / "erase_subs.py"), str(src), str(out)]
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

    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"id": job_id, "action": "remove-subs", "slug": src.stem,
                    "label": f"Clear subtitles ({method}) — {src.name}", "status": "running",
                    "lines": [], "returncode": None, "started": time.time(), "ended": None}
    threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id, "output": str(out.relative_to(ROOT)).replace("\\", "/")})


@app.post("/api/edit")
def api_edit():
    """One-pass editor render: trim [start,end] + optional zoom, speed, text overlay,
    volume — combined into a single ffmpeg job -> a NEW file in output/edits/."""
    b = request.get_json(force=True)
    src = safe_video_path(b.get("path", ""))
    try:
        start, end = float(b.get("start") or 0), float(b.get("end") or 0)
        zoom = max(1.0, min(3.0, float(b.get("zoom") or 1)))
        speed = max(0.5, min(2.0, float(b.get("speed") or 1)))
        volume = max(0.0, min(2.0, float(b.get("volume") if b.get("volume") is not None else 1)))
    except (TypeError, ValueError):
        abort(400, "start/end/zoom/speed/volume must be numbers")
    if end <= start:
        abort(400, "end must be after start")
    text = str(b.get("text") or "").strip()[:120]

    vf, af, tags = [], [], []
    if zoom > 1:
        vf.append(f"crop=trunc(iw/{zoom}/2)*2:trunc(ih/{zoom}/2)*2:(iw-iw/{zoom})/2:(ih-ih/{zoom})/2,"
                  f"scale=trunc(iw*{zoom}/2)*2:trunc(ih*{zoom}/2)*2")
        tags.append(f"zoom×{zoom:g}")
    if speed != 1:
        vf.append(f"setpts=PTS/{speed}")
        af.append(f"atempo={speed}")
        tags.append(f"speed×{speed:g}")
    if text:
        safe = text.replace("\\", "").replace("'", "’").replace(":", "\\:").replace("%", "\\%")
        pos = {"top": "y=h*0.08", "center": "y=(h-text_h)/2", "bottom": "y=h*0.85"}.get(
            str(b.get("text_pos") or "bottom"), "y=h*0.85")
        vf.append("drawtext=fontfile='C\\:/Windows/Fonts/arialbd.ttf':text='" + safe
                  + "':x=(w-text_w)/2:" + pos
                  + ":fontsize=h/16:fontcolor=white:borderw=3:bordercolor=black@0.85")
        tags.append("text")
    if volume != 1:
        af.append(f"volume={volume}")
        tags.append("mute" if volume == 0 else f"vol×{volume:g}")

    out_dir = ROOT / "output" / "edits"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem}-cut-{time.strftime('%H%M%S')}.mp4"
    cmd = [ff_tool("ffmpeg"), "-y", "-ss", str(start), "-to", str(end), "-i", str(src)]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    if af:
        cmd += ["-af", ",".join(af)]
    cmd += ["-c:v", "libx264", "-crf", "16", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", str(out)]
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"id": job_id, "action": "edit", "slug": src.stem,
                    "label": f"✂ Edit — {src.name} ({start:.0f}-{end:.0f}s"
                             + (", " + ", ".join(tags) if tags else "") + ")",
                    "status": "running", "lines": [], "returncode": None,
                    "started": time.time(), "ended": None}
    threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id, "output": str(out.relative_to(ROOT)).replace("\\", "/")})


@app.get("/api/poster")
def api_poster():
    """Cached poster frame for ANY repo video path (library grid thumbnails)."""
    src = safe_video_path(request.args.get("path", ""))
    try:
        t = max(0.0, float(request.args.get("t") or 1.0))
    except (TypeError, ValueError):
        t = 1.0
    out = qc_cache_dir(src, f"poster{t:g}") / "poster.jpg"
    if not out.is_file():
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [ff_tool("ffmpeg"), "-y", "-loglevel", "error", "-ss", f"{t:g}", "-i", str(src),
             "-frames:v", "1", "-vf", "scale=300:-2", "-q:v", "4", str(out)],
            env=job_env(), timeout=60)
    if not out.is_file():
        abort(500, "poster failed")
    return send_file(out, mimetype="image/jpeg", max_age=3600)


@app.post("/api/concat")
def api_concat():
    """Append clips: joins 2+ videos back to back (normalized to the first clip's frame)."""
    b = request.get_json(force=True)
    paths = [p for p in (b.get("paths") or []) if isinstance(p, str)]
    if len(paths) < 2:
        abort(400, "need at least two videos")
    srcs = [safe_video_path(p) for p in paths[:6]]
    probe = ffprobe_json(srcs[0])
    v0 = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
    W, H = int(v0.get("width") or 720), int(v0.get("height") or 1280)
    n = len(srcs)
    parts, maps = [], ""
    for i in range(n):
        parts.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                     f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{i}];"
                     f"[{i}:a]aresample=44100[a{i}];")
        maps += f"[v{i}][a{i}]"
    filt = "".join(parts) + f"{maps}concat=n={n}:v=1:a=1[v][a]"
    out_dir = ROOT / "output" / "edits"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"joined-{time.strftime('%H%M%S')}.mp4"
    cmd = [ff_tool("ffmpeg"), "-y"]
    for s in srcs:
        cmd += ["-i", str(s)]
    cmd += ["-filter_complex", filt, "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-crf", "17", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", str(out)]
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"id": job_id, "action": "edit", "slug": srcs[0].stem,
                    "label": f"➕ Join {n} clips → {out.name}", "status": "running",
                    "lines": [], "returncode": None, "started": time.time(), "ended": None}
    threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id, "output": str(out.relative_to(ROOT)).replace("\\", "/")})


# ---------------------------------------------------------------- video-studio (5180) engine
# The unified Video Studio app (video-studio/app/server.py) owns the richest subtitle
# pipeline (captions editor, AI-fix, fast cover, auto). We absorb it: auto-start it if
# it's down and reverse-proxy it under /subs/* so VSL Auto is one single app/origin.

VIDEO_STUDIO_PY = ROOT.parent / "video-studio" / "app" / "server.py"
_vs_lock = threading.Lock()


def _port_open(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_video_studio() -> bool:
    if _port_open(5180):
        return True
    with _vs_lock:
        if _port_open(5180):
            return True
        if not VIDEO_STUDIO_PY.is_file():
            return False
        flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0   # detached + new group
        subprocess.Popen([sys.executable, str(VIDEO_STUDIO_PY)], cwd=str(ROOT.parent),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags)
        for _ in range(40):                       # engines import torch — give it time
            time.sleep(0.5)
            if _port_open(5180):
                return True
    return False


@app.route("/subs/", defaults={"p": ""}, methods=["GET", "POST", "DELETE"])
@app.route("/subs/<path:p>", methods=["GET", "POST", "DELETE"])
def subs_proxy(p):
    if not ensure_video_studio():
        abort(502, "the Video Studio engine (port 5180) could not be started")
    import urllib.error
    import urllib.request
    url = f"http://127.0.0.1:5180/{p}"
    if request.query_string:
        url += "?" + request.query_string.decode("utf-8", "ignore")
    headers = {h: request.headers[h] for h in ("Content-Type", "Range") if request.headers.get(h)}
    data = request.get_data() if request.method in ("POST", "DELETE") else None
    req = urllib.request.Request(url, data=data, headers=headers, method=request.method)
    try:
        resp = urllib.request.urlopen(req, timeout=900)
    except urllib.error.HTTPError as e:
        return Response(e.read(), status=e.code,
                        content_type=e.headers.get("Content-Type", "text/plain"))
    except Exception as e:
        abort(502, f"Video Studio engine error: {e}")
    out = Response(resp.read(), status=resp.status,
                   content_type=resp.headers.get("Content-Type", "application/octet-stream"))
    for h in ("Content-Range", "Accept-Ranges"):
        if resp.headers.get(h):
            out.headers[h] = resp.headers[h]
    return out


# ---------------------------------------------------------------- creator library

LIBRARY_FILE = ROOT / "output" / "library.json"
library_lock = threading.Lock()
CREATOR_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def library_meta() -> dict:
    return read_json(LIBRARY_FILE) or {}


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
            vids.append({
                "name": p.name, "stem": stem, "size": p.stat().st_size, "mtime": p.stat().st_mtime,
                "orig_words": orig_words,
                "cleaned": (UPLOADS / ".originals" / p.name).is_file(),
                "transcript": (TRANSCRIPTS / f"{stem}.md").is_file(),
                "script": (work / "script-edited.txt").is_file(),
                "dub": f"output/script-swap/{stem}/final.mp4" if final.is_file() else None,
                "dub_mtime": final.stat().st_mtime if final.is_file() else None,
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


# ---------------------------------------------------------------- static

@app.get("/")
def index():
    # Creator is the front door — simple, workflow-first. Power tools live at /mission.
    return send_from_directory(STATIC, "creator.html")


@app.get("/mission")
def mission_page():
    return send_from_directory(STATIC, "index.html")


@app.get("/creator")
def creator_page():
    return send_from_directory(STATIC, "creator.html")


@app.get("/qc")
def qc_page():
    return send_from_directory(STATIC, "qc.html")


@app.get("/subtitles")
def subtitles_page():
    return send_from_directory(STATIC, "subtitles.html")


@app.get("/eraser")
def eraser_page():
    # merged into the unified Subtitles Studio
    return send_from_directory(STATIC, "subtitles.html")


@app.get("/dubbing")
def dubbing_page():
    return send_from_directory(STATIC, "dubbing.html")


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
    import uuid
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
    import subprocess
    b = request.get_json(force=True) or {}
    mode = b.get("mode")
    if mode not in ("generate", "upscale", "inpaint", "keyframe"):
        abort(400, "bad mode")
    venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5170))
    print(f"autoVSL dashboard -> http://localhost:{port}  (repo: {ROOT})")
    app.run(host="127.0.0.1", port=port, debug=False)
