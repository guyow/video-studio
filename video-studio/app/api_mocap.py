#!/usr/bin/env python3
"""Motion Capture — MediaPipe + Kalidokit + VRM avatar, all in the browser.

The heavy lifting (face/pose/hand tracking, avatar rigging, compositing,
recording) happens client-side in motion-capture.html — free and local. This
blueprint is only the storage half:

  * VRM avatar library (list/upload) — avatars live in static/ because the
    /media/ route whitelists extensions and would 403 a .vrm;
  * receive the recorded webm and finalize it to mp4 through the job system
    (engines/mocap_finalize.py: H.264 encode + audio mux back from the source);
  * list / delete / export the finished takes in output/mocap/.

A Blueprint for the same reason api_sequence.py is one: server.py is huge and
a new blueprint can't disturb any existing route.
"""
from __future__ import annotations

import random
import re
import subprocess
import threading
import time
from pathlib import Path

from flask import Blueprint, abort, jsonify, request
from werkzeug.utils import secure_filename

# single source of truth for engines/prices/script path — the timeline's
from api_sequence import FACESWAP_PY, FS_ENGINES, _fs_estimate

bp = Blueprint("mocap", __name__)

# injected by init() so this module never imports server.py (which imports it)
ROOT: Path = Path(".")
ENGINE: Path = Path(".")
JOBS_CREATE = None
RUN_JOB = None
PY: str = "python"
UPLOADS: Path = Path(".")
STATIC: Path = Path(".")
EXPORTS_DIR = None          # callable → Path (settings can change it at runtime)
SOFT_DELETE = None
FF = None                   # ff_tool("ffmpeg"|"ffprobe")
FAL_ENV: Path = Path(".")
RECORD_SPEND = None
COMFY_URL: str = "127.0.0.1:8188"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,120}$")
AVATAR_MAX_MB = 100
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# XR Animator — the pro full-body mocap desktop app (webcam → VRM in real time,
# exports BVH/VRMA for Blender/Unreal). Installed by the motion-capture setup.
XR_ANIMATOR_DIR = Path.home() / "Claude/Projects/Video AI editing/tools/XR-Animator"


def init(root: Path, engine: Path, jobs_create, run_job, py_exe: str,
         uploads: Path, static_dir: Path, exports_dir=None, soft_delete=None,
         ff_tool=None, fal_env=None, record_spend=None, comfy_url: str = ""):
    global ROOT, ENGINE, JOBS_CREATE, RUN_JOB, PY, UPLOADS, STATIC, EXPORTS_DIR, SOFT_DELETE
    global FF, FAL_ENV, RECORD_SPEND, COMFY_URL
    ROOT = Path(root)
    ENGINE = Path(engine)
    JOBS_CREATE = jobs_create
    RUN_JOB = run_job
    PY = py_exe or "python"
    UPLOADS = Path(uploads)
    STATIC = Path(static_dir)
    EXPORTS_DIR = exports_dir
    SOFT_DELETE = soft_delete
    FF = ff_tool
    FAL_ENV = Path(fal_env) if fal_env else Path(".")
    RECORD_SPEND = record_spend
    COMFY_URL = comfy_url or "127.0.0.1:8188"


def out_dir() -> Path:
    return ROOT / "output" / "mocap"


def avatars_dir() -> Path:
    return STATIC / "models" / "avatars"


def _safe_out(name: str) -> Path:
    name = Path(name or "").name
    if not name or not NAME_RE.match(name):
        abort(400, "bad file name")
    p = (out_dir() / name).resolve()
    if not str(p).startswith(str(out_dir().resolve())) or not p.is_file():
        abort(404, "no such take")
    return p


# ---------------------------------------------------------------- avatars

@bp.get("/api/mocap/avatars")
def list_avatars():
    d = avatars_dir()
    items = []
    if d.is_dir():
        for p in sorted(d.glob("*.vrm")):
            items.append({"name": p.name,
                          "url": f"/static/models/avatars/{p.name}",
                          "size": p.stat().st_size})
    return jsonify({"avatars": items})


@bp.post("/api/mocap/avatar")
def upload_avatar():
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "no file")
    if Path(f.filename).suffix.lower() != ".vrm":
        abort(400, "avatars must be .vrm files (grab one free on VRoid Hub)")
    base = secure_filename(Path(f.filename).stem).strip(".-_") or f"avatar-{int(time.time())}"
    d = avatars_dir()
    d.mkdir(parents=True, exist_ok=True)
    name, n = f"{base}.vrm", 2
    while (d / name).exists():
        name = f"{base}-{n}.vrm"
        n += 1
    f.save(d / name)
    size = (d / name).stat().st_size
    if size > AVATAR_MAX_MB * 1048576:
        (d / name).unlink(missing_ok=True)
        abort(400, f"avatar too big (>{AVATAR_MAX_MB}MB) — decimate it in VRoid first")
    return jsonify({"name": name, "url": f"/static/models/avatars/{name}", "size": size})


# ---------------------------------------------------------------- recordings

@bp.post("/api/mocap/rec")
def receive_recording():
    """The browser sends the recorded canvas webm; we finalize to mp4 as a job."""
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "no recording")
    mode = (request.form.get("mode") or "video").strip()
    source = Path(request.form.get("source") or "").name  # uploads/<name>, optional

    slug = f"mocap-{time.strftime('%Y%m%d-%H%M%S')}"
    raw_dir = out_dir() / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw = raw_dir / f"{slug}.webm"
    f.save(raw)
    if raw.stat().st_size < 2048:
        raw.unlink(missing_ok=True)
        abort(400, "the recording is empty — nothing was captured")

    out = out_dir() / f"{slug}.mp4"
    cmd = [PY, "-u", str(ENGINE), "--rec", str(raw), "--out", str(out)]
    if mode == "video" and source:
        src = UPLOADS / source
        if src.is_file():
            cmd += ["--audio-from", str(src)]

    label = "🕺 Motion Capture — " + ("live take" if mode == "live" else f"avatar over {source or 'video'}")
    job_id = JOBS_CREATE("mocap-finalize", slug, label, gpu=False)
    threading.Thread(target=RUN_JOB, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id, "name": out.name,
                    "url": f"/media/output/mocap/{out.name}"})


@bp.get("/api/mocap/list")
def list_takes():
    d = out_dir()
    items = []
    if d.is_dir():
        for p in sorted(d.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
            st = p.stat()
            items.append({"name": p.name, "url": f"/media/output/mocap/{p.name}",
                          "size": st.st_size, "mtime": st.st_mtime})
    return jsonify({"takes": items})


@bp.post("/api/mocap/delete")
def delete_take():
    b = request.get_json(force=True)
    p = _safe_out(b.get("name"))
    (out_dir() / "raw" / f"{p.stem}.webm").unlink(missing_ok=True)
    if SOFT_DELETE:
        return jsonify({"deleted": p.name, "trash": SOFT_DELETE(p, f"mocap-{p.stem}")})
    p.unlink()
    return jsonify({"deleted": p.name})


# ---------------------------------------------------------------- real person (AI)
# The photoreal route: a photo of a real person + the source footage → fal's
# Wan 2.2 Animate Replace (or PixVerse) makes that person deliver the original
# performance — body, lips, timing, audio. Runs through the same proven script
# the timeline uses (shot chunking, seam dissolves, audio conform), with the
# same 402 cost gate. This is the paid counterpart of the free 3D avatar.

def refs_dir() -> Path:
    return out_dir() / "refs"


@bp.get("/api/mocap/persons")
def list_persons():
    d = refs_dir()
    items = []
    if d.is_dir():
        for p in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if p.suffix.lower() in IMAGE_EXTS:
                items.append({"name": p.name,
                              "url": f"/media/output/mocap/refs/{p.name}"})
    return jsonify({"persons": items})


@bp.post("/api/mocap/person")
def upload_person():
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "no image")
    ext = Path(f.filename).suffix.lower()
    if ext not in IMAGE_EXTS:
        abort(400, "the person reference must be a jpg / png / webp photo")
    base = secure_filename(Path(f.filename).stem).strip(".-_") or f"person-{int(time.time())}"
    d = refs_dir()
    d.mkdir(parents=True, exist_ok=True)
    name, n = f"{base}{ext}", 2
    while (d / name).exists():
        name = f"{base}-{n}{ext}"
        n += 1
    f.save(d / name)
    return jsonify({"name": name, "url": f"/media/output/mocap/refs/{name}"})


@bp.post("/api/mocap/animate")
def animate_person():
    """Real-person performance transfer. 402 with the estimate until confirmed."""
    b = request.get_json(force=True)
    engine = b.get("engine") or "wan-replace"
    if engine not in FS_ENGINES:
        abort(400, f"unknown engine {engine!r}")
    if not FACESWAP_PY.is_file():
        abort(503, "the face-swap script is missing from ~/.claude/skills/video-face-swap")

    src = UPLOADS / Path(b.get("source") or "").name
    if not b.get("source") or not src.is_file():
        abort(400, "upload the source video first")
    person = refs_dir() / Path(b.get("person") or "").name
    if not b.get("person") or not person.is_file():
        abort(400, "upload a photo of the person first")

    r = subprocess.run(
        [FF("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(src)],
        capture_output=True, text=True)
    try:
        seconds = float((r.stdout or "").strip().splitlines()[0])
    except (ValueError, IndexError):
        abort(500, "could not read the source duration")
    if seconds > 120:
        abort(400, "keep it under 2 minutes per run — split longer videos first")

    # paid test: swap only the first N seconds (~$0.075/s @480p vs the full video)
    try:
        test_s = float(b.get("test_seconds") or 0)
    except (TypeError, ValueError):
        test_s = 0.0
    test_s = round(min(test_s, seconds), 2) if test_s > 0 else 0.0
    if 0 < test_s < seconds:
        seconds = test_s
    else:
        test_s = 0.0

    no_split = bool(b.get("no_split", True))

    # fal caps wan one-call runs by compute: ~1521 frames at 20 steps
    # (frames × steps ≲ 30420). Longer clip in one call → lower the steps.
    steps = 20
    if engine == "wan-replace" and no_split:
        r = subprocess.run(
            [FF("ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "default=nw=1:nk=1",
             str(src)], capture_output=True, text=True)
        try:
            n, _, d = (r.stdout or "30/1").strip().partition("/")
            fps = float(n) / float(d or 1)
        except (ValueError, ZeroDivisionError):
            fps = 30.0
        frames = seconds * min(fps, 30)      # the script caps renders at 30fps
        if frames > 1521:
            steps = max(8, min(20, int(30420 // frames)))

    resolution = b.get("resolution") or "480p"
    est = _fs_estimate(engine, seconds, resolution)
    if test_s:
        est["summary"] = f"TEST — first {test_s:g}s only · {est['summary']}"
    if steps < 20:
        est["summary"] += (f" · one call is long, so quality steps drop 20→{steps} "
                           f"to fit fal's limit — for full quality keep runs under "
                           f"~50s or use PixVerse")
    if not b.get("confirm_cost"):
        return jsonify({"needs_confirm": True, "estimate": est}), 402

    # one seed for the whole run — without it every ~8s Wan segment rolls its
    # own and the wardrobe/background reinvent themselves at each boundary
    try:
        seed = int(b.get("seed"))
    except (TypeError, ValueError):
        seed = random.randrange(2**31)

    if test_s:
        # frame-accurate trim (re-encode — a -c copy trim snaps to keyframes
        # and drifts the lip-sync downstream)
        clip = UPLOADS / f".{src.stem}-test{test_s:g}s.mp4"
        r = subprocess.run(
            [FF("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-t", f"{test_s}",
             "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
             "-c:a", "aac", "-b:a", "192k", str(clip)],
            capture_output=True, text=True)
        if r.returncode != 0 or not clip.is_file():
            abort(500, f"could not trim the test clip: {(r.stderr or '')[-300:]}")
        src = clip

    slug = (f"mocap-real-{time.strftime('%Y%m%d-%H%M%S')}"
            + (f"-test{test_s:g}s" if test_s else ""))
    out = out_dir() / f"{slug}.mp4"
    out_dir().mkdir(parents=True, exist_ok=True)
    cmd = [PY, "-u", str(FACESWAP_PY),
           "--input", str(src), "--face", str(person), "--out", str(out),
           "--engine", engine, "--resolution", est["resolution"],
           "--seed", str(seed), "--steps", str(steps),
           "--env-file", str(FAL_ENV), "--yes"]
    # continuous actor takes have no cuts to hide segment seams in — one call
    # renders the whole clip in a single pass (same price, zero seams). Send
    # no_split:false to fall back to seeded segments + dissolves if fal ever
    # rejects a long video.
    if no_split:
        cmd.append("--no-split")

    job_id = JOBS_CREATE("mocap-real", slug,
                         f"🧑 Real person — {person.stem} into {src.name} [{engine}]",
                         gpu=False)
    threading.Thread(target=_animate_worker, args=(job_id, cmd, slug, out, engine, est),
                     daemon=True).start()
    return jsonify({"job_id": job_id, "name": out.name, "estimate": est,
                    "seed": seed, "url": f"/media/output/mocap/{out.name}"})


def _animate_worker(job_id, cmd, slug, out: Path, engine, est):
    RUN_JOB(job_id, cmd)
    from jobs import jobs
    job = jobs.get(job_id) or {}
    if job.get("status") != "done" or not out.is_file():
        return
    if RECORD_SPEND:
        try:
            RECORD_SPEND(slug, {"this_run": est["usd"], "summary": est["summary"],
                                "engine": f"fal-mocap-{engine}"})
        except Exception:
            pass


@bp.post("/api/mocap/liveportrait")
def liveportrait():
    """FREE photoreal head transfer on the local GPU (LivePortrait via ComfyUI).

    Face/head/lips only — the photo's body stays still. No cost gate because
    there is no cost; the render just needs ComfyUI up and takes GPU minutes.
    """
    b = request.get_json(force=True)
    src = UPLOADS / Path(b.get("source") or "").name
    if not b.get("source") or not src.is_file():
        abort(400, "upload the source video first")
    person = refs_dir() / Path(b.get("person") or "").name
    if not b.get("person") or not person.is_file():
        abort(400, "upload a photo of the person first")

    import urllib.request
    try:
        urllib.request.urlopen(f"http://{COMFY_URL}/system_stats", timeout=3)
    except Exception:
        abort(503, "ComfyUI is not running — start it first (start-servers.bat), "
                   "then try again")

    r = subprocess.run(
        [FF("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(src)],
        capture_output=True, text=True)
    try:
        seconds = float((r.stdout or "").strip().splitlines()[0])
    except (ValueError, IndexError):
        abort(500, "could not read the source duration")
    if seconds > 90:
        abort(400, "keep it under 90s per run — the frames are held in RAM")

    slug = f"mocap-lp-{time.strftime('%Y%m%d-%H%M%S')}"
    out = out_dir() / f"{slug}.mp4"
    out_dir().mkdir(parents=True, exist_ok=True)
    cmd = [PY, "-u", str(ENGINE.parent / "mocap_liveportrait.py"),
           "--face", str(person), "--driving", str(src), "--out", str(out),
           "--comfy", COMFY_URL]
    job_id = JOBS_CREATE("mocap-lp", slug,
                         f"🎭 LivePortrait — {person.stem} performs {src.name} (free)",
                         gpu=True)
    threading.Thread(target=RUN_JOB, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id, "name": out.name,
                    "url": f"/media/output/mocap/{out.name}"})


@bp.post("/api/mocap/avatar-take")
def avatar_take():
    """Avatar Creator: raw webcam webm + a face image → the image performs you.

    Free (LivePortrait on the local GPU). The recording arrives as browser webm
    with mic audio; the engine conforms it to CFR and chains the transfer.
    """
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "no recording")
    person = refs_dir() / Path(request.form.get("person") or "").name
    if not request.form.get("person") or not person.is_file():
        abort(400, "pick or upload an avatar image first")

    import urllib.request
    try:
        urllib.request.urlopen(f"http://{COMFY_URL}/system_stats", timeout=3)
    except Exception:
        abort(503, "ComfyUI is not running — start it first (start-servers.bat), "
                   "then try again")

    slug = f"mocap-avatar-{time.strftime('%Y%m%d-%H%M%S')}"
    raw_dir = out_dir() / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw = raw_dir / f"{slug}.webm"
    f.save(raw)
    if raw.stat().st_size < 2048:
        raw.unlink(missing_ok=True)
        abort(400, "the recording is empty — nothing was captured")

    out = out_dir() / f"{slug}.mp4"
    cmd = [PY, "-u", str(ENGINE.parent / "mocap_avatar_creator.py"),
           "--rec", str(raw), "--face", str(person), "--out", str(out),
           "--comfy", COMFY_URL]
    job_id = JOBS_CREATE("mocap-avatar", slug,
                         f"🪞 Avatar Creator — {person.stem} performs your take (free)",
                         gpu=True)
    threading.Thread(target=RUN_JOB, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id, "name": out.name,
                    "url": f"/media/output/mocap/{out.name}"})


@bp.post("/api/mocap/xr-launch")
def xr_launch():
    """Launch XR Animator (desktop app) for pro full-body real-time mocap."""
    exes = list(XR_ANIMATOR_DIR.glob("**/electron.exe")) + \
           list(XR_ANIMATOR_DIR.glob("**/XR Animator.exe")) + \
           list(XR_ANIMATOR_DIR.glob("**/XR_Animator*.exe"))
    if not exes:
        abort(503, "XR Animator is not installed (expected under tools/XR-Animator)")
    exe = exes[0]
    subprocess.Popen([str(exe)], cwd=str(exe.parent),
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    return jsonify({"launched": exe.name})


@bp.post("/api/mocap/use-as-source")
def use_as_source():
    """Promote a finished take to a source video (uploads/) — e.g. record
    yourself on webcam, then drive the full-body Wan route with that take."""
    b = request.get_json(force=True)
    p = _safe_out(b.get("name"))
    UPLOADS.mkdir(exist_ok=True)
    name, n = p.name, 2
    while (UPLOADS / name).exists():
        name = f"{p.stem}-{n}{p.suffix}"
        n += 1
    import shutil
    shutil.copy2(p, UPLOADS / name)
    return jsonify({"name": name, "url": f"/media/uploads/{name}"})


@bp.post("/api/mocap/export")
def export_take():
    b = request.get_json(force=True)
    p = _safe_out(b.get("name"))
    dest_dir = Path(EXPORTS_DIR() if callable(EXPORTS_DIR) else (EXPORTS_DIR or "."))
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / p.name
    import shutil
    shutil.copy2(p, dest)
    return jsonify({"exported": str(dest)})
