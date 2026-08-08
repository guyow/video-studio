#!/usr/bin/env python3
"""HTTP surface for the timeline editor.

A Blueprint rather than more routes in server.py — that file is already ~6,200
lines, and a *new* blueprint carries none of the risk of splitting the old ones.

All edit logic lives in sequence.py and runs here, on the server. The browser
sends intents ("split c3 at 4.2s"), not documents. That keeps one implementation
of ripple/trim/split instead of two that drift, and it makes undo free.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Blueprint, abort, jsonify, request
from werkzeug.utils import secure_filename

import sequence as seq
sys.path.insert(0, str(Path(__file__).resolve().parent / "engines"))
import ai_models  # noqa: E402

bp = Blueprint("seq", __name__)

# injected by init() so this module never imports server.py (which imports it)
ROOT: Path = Path(".")
PY: str = sys.executable
RENDER_PY: Path = Path(".")
GEN_PY: Path = Path(".")
CV_PY: str = ""
FAL_ENV: Path = Path(".")
CLAUDE: str = ""
WHISPER_PY: str = ""
ENGINE_DIR = Path(__file__).resolve().parent / "engines"
_jobs_create = None
_run_job = None
_ff = None
_record_spend = None

MEDIA_EXTS = seq.VIDEO_EXTS | seq.AUDIO_EXTS
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")


def init(root: Path, render_py: Path, jobs_create, run_job, ff_tool, python_exe=None,
         gen_py: Path = None, cv_py: str = "", fal_env: Path = None,
         claude_exe: str = "", record_spend=None, whisper_py: str = ""):
    global ROOT, RENDER_PY, GEN_PY, CV_PY, FAL_ENV, CLAUDE, WHISPER_PY
    global _jobs_create, _run_job, _ff, PY, _record_spend
    ROOT, RENDER_PY = Path(root), Path(render_py)
    _jobs_create, _run_job, _ff = jobs_create, run_job, ff_tool
    PY = python_exe or sys.executable
    GEN_PY = Path(gen_py) if gen_py else Path(".")
    CV_PY = cv_py or ""
    FAL_ENV = Path(fal_env) if fal_env else Path(".")
    CLAUDE = claude_exe or ""
    _record_spend = record_spend
    WHISPER_PY = whisper_py or ""


# ---------------------------------------------------------------- helpers

def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:60]
    return s or f"project-{int(time.time())}"


def proj_dir(slug: str) -> Path:
    if not SLUG_RE.match(slug or ""):
        abort(400, "bad project slug")
    return seq.project_dir(ROOT, slug)


def doc_file(slug: str) -> Path:
    p = proj_dir(slug) / "project.json"
    if not p.is_file():
        abort(404, f"no project {slug!r}")
    return p


def load_doc(slug: str) -> dict:
    return seq.load(doc_file(slug))


def save_doc(slug: str, doc: dict) -> dict:
    try:
        return seq.save(proj_dir(slug) / "project.json", doc)
    except ValueError as exc:
        abort(400, str(exc))


def safe_rel(rel: str) -> Path:
    """Resolve a repo-relative media path, refusing anything outside ROOT."""
    target = (ROOT / str(rel or "").replace("\\", "/")).resolve()
    if not str(target).startswith(str(ROOT.resolve())):
        abort(400, "path escapes the repo")
    if target.suffix.lower() not in MEDIA_EXTS:
        abort(400, f"unsupported media type {target.suffix!r}")
    return target


def ffprobe(path: Path) -> dict:
    r = subprocess.run(
        [_ff("ffprobe"), "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def media_info(rel: str) -> dict:
    path = safe_rel(rel)
    if not path.is_file():
        return {"src": rel, "missing": True}
    pr = ffprobe(path)
    v = next((s for s in pr.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in pr.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = 0.0
    try:
        dur = float(pr.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        pass
    fps = 0.0
    if v and v.get("r_frame_rate", "0/1") != "0/0":
        try:
            n, _, d = v["r_frame_rate"].partition("/")
            fps = float(n) / float(d or 1)
        except (ValueError, ZeroDivisionError):
            fps = 0.0
    return {
        "src": rel, "missing": False, "dur": round(dur, 3),
        "w": int(v.get("width") or 0) if v else 0,
        "h": int(v.get("height") or 0) if v else 0,
        "fps": round(fps, 3), "audio": bool(a),
        "kind": "audio" if not v else "video",
    }


def with_meta(doc: dict) -> dict:
    """The document plus everything the UI needs but must not persist."""
    return {
        "doc": doc,
        "duration": round(seq.duration(doc), 3),
        "sources": {s: media_info(s) for s in seq.sources(doc)},
    }


# ---------------------------------------------------------------- projects

@bp.get("/api/seq/projects")
def list_projects():
    base = ROOT / "output" / "projects"
    items = []
    if base.is_dir():
        for d in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0,
                        reverse=True):
            f = d / "project.json"
            if not f.is_file():
                continue
            try:
                doc = seq.load(f)
            except Exception:
                continue
            items.append({
                "slug": d.name, "name": doc.get("name"), "version": doc.get("version"),
                "modified": doc.get("modified"), "duration": round(seq.duration(doc), 2),
                "canvas": doc.get("canvas"),
                "clips": sum(len(t.get("clips", [])) for t in doc.get("tracks", [])),
            })
    return jsonify({"projects": items})


@bp.post("/api/seq/projects")
def create_project():
    b = request.get_json(force=True) or {}
    name = (b.get("name") or "").strip() or "Untitled edit"
    slug = slugify(b.get("slug") or name)
    if (seq.project_dir(ROOT, slug) / "project.json").is_file():
        slug = f"{slug}-{int(time.time()) % 100000}"
    try:
        doc = seq.new_project(name, int(b.get("w") or 1080), int(b.get("h") or 1920),
                              int(b.get("fps") or 30))
    except (TypeError, ValueError):
        abort(400, "w/h/fps must be integers")
    doc["meta"]["root"] = str(ROOT)
    doc["version"] = 0                       # save() bumps to 1
    save_doc(slug, doc)
    return jsonify({"slug": slug, **with_meta(load_doc(slug))})


@bp.get("/api/seq/<slug>")
def get_project(slug):
    return jsonify({"slug": slug, **with_meta(load_doc(slug))})


@bp.post("/api/seq/<slug>/delete")
def delete_project(slug):
    d = proj_dir(slug)
    if not d.is_dir():
        abort(404, "no such project")
    trash = ROOT / "output" / "projects" / ".trash" / f"{slug}-{int(time.time())}"
    trash.parent.mkdir(parents=True, exist_ok=True)
    d.rename(trash)                          # soft delete, like the rest of this app
    return jsonify({"ok": True, "trashed": str(trash.relative_to(ROOT)).replace("\\", "/")})


@bp.post("/api/seq/<slug>/undo")
def undo_project(slug):
    try:
        doc = seq.undo(doc_file(slug))
    except ValueError as exc:
        abort(400, str(exc))
    return jsonify({"slug": slug, **with_meta(doc)})


# ---------------------------------------------------------------- media

@bp.get("/api/seq/media/probe")
def probe_media():
    rel = request.args.get("src") or ""
    return jsonify(media_info(rel))


@bp.get("/api/seq/media/list")
def list_media():
    """Everything importable, newest first: uploads plus rendered outputs."""
    out = []
    for folder in ("uploads", "output/edits", "output/i2v", "output/broll"):
        base = ROOT / folder
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_file() and p.suffix.lower() in MEDIA_EXTS and not p.name.startswith("."):
                try:
                    rel = str(p.relative_to(ROOT)).replace("\\", "/")
                except ValueError:
                    continue
                out.append({"src": rel, "name": p.name, "folder": folder,
                            "size": p.stat().st_size, "mtime": p.stat().st_mtime,
                            "kind": "audio" if p.suffix.lower() in seq.AUDIO_EXTS else "video"})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify({"media": out[:400]})


@bp.post("/api/seq/media/proxy")
def make_proxy():
    """A small, densely-keyframed copy so scrubbing is instant.

    Playback in the browser seeks constantly; seeking a 1080p long-GOP master is
    what makes web editors feel broken. Proxies are the fix, and they are cheap.
    """
    b = request.get_json(force=True) or {}
    src = safe_rel(b.get("src") or "")
    if not src.is_file():
        abort(404, "source not found")
    pdir = ROOT / "output" / "proxies"
    pdir.mkdir(parents=True, exist_ok=True)
    st = src.stat()
    key = f"{src.stem}-{int(st.st_mtime)}-{st.st_size % 100000}.mp4"
    out = pdir / key
    rel = str(out.relative_to(ROOT)).replace("\\", "/")
    if out.is_file():
        return jsonify({"proxy": rel, "cached": True})

    cmd = [_ff("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
           "-vf", "scale=-2:540", "-c:v", "libx264", "-crf", "26", "-preset", "veryfast",
           "-g", "15", "-keyint_min", "15", "-sc_threshold", "0", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart", str(out)]
    job_id = _jobs_create("proxy", src.stem, f"⚡ Proxy — {src.name}", gpu=False)
    threading.Thread(target=_run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id, "proxy": rel, "cached": False})


# ---------------------------------------------------------------- edit ops

def _f(b: dict, key: str, default=None) -> float:
    try:
        v = b.get(key, default)
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        abort(400, f"{key} must be a number")


@bp.post("/api/seq/<slug>/op")
def apply_op(slug):
    """One endpoint, many intents. Free ops share _apply_op with the chat
    planner so ripple/trim/split have exactly one implementation."""
    b = request.get_json(force=True) or {}
    op = b.get("op")
    doc = load_doc(slug)
    try:
        if op in FREE_OPS:
            _apply_op(doc, b)
        elif op == "swap_source":
            seq.swap_source(doc, b.get("clip"), b.get("src") or "", b.get("engine") or "ai")
        elif op == "revert_source":
            seq.revert_source(doc, b.get("clip"))
        elif op == "canvas":
            for k in ("w", "h", "fps"):
                if b.get(k):
                    doc["canvas"][k] = int(b[k])
        else:
            abort(400, f"unknown op {op!r}")
    except KeyError as exc:
        abort(404, str(exc))
    except (TypeError, ValueError) as exc:
        abort(400, str(exc))
    save_doc(slug, doc)
    return jsonify({"slug": slug, **with_meta(load_doc(slug))})


# ---------------------------------------------------------------- render

@bp.post("/api/seq/<slug>/render")
def render_project(slug):
    b = request.get_json(force=True) or {}
    doc = load_doc(slug)
    if seq.duration(doc) <= 0:
        abort(400, "timeline is empty")

    draft = bool(b.get("draft"))
    scale = 0.5 if draft else 1.0
    out_dir = ROOT / "output" / "edits"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    out = out_dir / f"{slug}{'-draft' if draft else ''}-{stamp}.mp4"

    cmd = [PY, "-u", str(RENDER_PY), "--project", str(doc_file(slug)),
           "--out", str(out), "--scale", str(scale)]
    rng = b.get("range")
    if isinstance(rng, list) and len(rng) == 2:
        cmd += ["--range", f"{float(rng[0])}:{float(rng[1])}"]
    if b.get("force"):
        cmd.append("--force")

    job_id = _jobs_create("seq-render", slug,
                          f"🎬 Render — {doc.get('name')}{' (draft)' if draft else ''}",
                          gpu=False)
    threading.Thread(target=_run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id,
                    "output": str(out.relative_to(ROOT)).replace("\\", "/")})


# ═══════════════════════════════════════════════════════════════════
#  AI layer — Claude drives the timeline, fal models generate pixels
# ═══════════════════════════════════════════════════════════════════

# Ops Claude may apply on its own. Everything here is free and reversible
# (undo restores the previous version). Anything that spends money is NOT in
# this list — those come back as proposals the user confirms.
FREE_OPS = {"add", "add_text", "remove", "move", "trim", "split",
            "ripple_delete", "set", "track", "rename"}

CHAT_SYSTEM = """You are the editor inside a video editing app. You do not describe edits — you make them.

You receive the current timeline as JSON and a request from the user. You reply with ONE JSON object:

{
  "reply": "one or two short sentences, plain language, what you did and why",
  "ops": [ ... zero or more edit operations, applied in order ... ],
  "generate": null or { "clip": "<clip id>", "prompt": "<what the model should change>", "why": "<short>" },
  "faceswap": null or { "clip": "<clip id>", "why": "<short>" }
}

The operations you may use (all free, all undoable):
  {"op":"split","clip":"<id>","at":<timeline seconds>}
  {"op":"remove","clip":"<id>","ripple":true|false}         ripple closes the gap
  {"op":"ripple_delete","track":"V1","a":<sec>,"b":<sec>}   cut a span out and close it
  {"op":"trim","clip":"<id>","edge":"in"|"out","delta":<seconds, may be negative>}
  {"op":"move","clip":"<id>","start":<sec>}
  {"op":"add","src":"<source path from the media list>","append":true}
  {"op":"add_text","text":"<words>","start":<sec>,"dur":<sec>,"style":{"size":64,"pos":"bottom","color":"#FFFFFF"}}
  {"op":"set","clip":"<id>","patch":{"speed":1.5,"volume":0.5,"transform":{"scale":1.2}}}

Rules that matter:
- Timeline positions are SECONDS on the timeline, not offsets inside a source file.
- A clip occupies [start, start+duration). Never split outside a clip.
- Only reference clip ids that exist in the document you were given.
- If the user asks for something that costs money (restyle, regenerate, change what is
  IN the footage, "make it night", "add a dragon"), do NOT invent an op. Put it in
  "generate" naming the clip, and leave "ops" empty. The user confirms the spend.
- EXCEPTION — changing WHO is on camera: if the user wants to change, swap or replace
  the FACE / the ACTOR / the PERSON, use "faceswap", never "generate". The generate
  models cannot take a reference face, so they will charge money and fail the job.
  Leave ops and generate empty, and in "reply" tell them to pick or save an avatar
  (the new face) in the Face swap box.
- If the request is unclear or you cannot do it, return empty ops and say so plainly in "reply".
- Prefer the smallest edit that satisfies the request. Do not reorganise things you were not asked about.

Return ONLY the JSON object. No markdown fence, no commentary."""


def _doc_for_claude(doc: dict) -> dict:
    """A compact view — the full document wastes context on fields Claude never sets."""
    out = {"canvas": doc.get("canvas"), "duration": round(seq.duration(doc), 2), "tracks": []}
    for t in doc.get("tracks", []):
        clips = []
        for c in t.get("clips", []):
            e = {"id": c["id"], "start": round(float(c.get("start") or 0), 2),
                 "dur": round(seq.clip_dur(c), 2)}
            if t["kind"] == "text":
                e["text"] = c.get("text")
            else:
                e["src"] = c.get("src")
                e["source_range"] = [round(float(c.get("in") or 0), 2),
                                     round(float(c.get("out") or 0), 2)]
                if float(c.get("speed") or 1) != 1:
                    e["speed"] = c.get("speed")
            clips.append(e)
        out["tracks"].append({"id": t["id"], "kind": t["kind"],
                              "muted": bool(t.get("muted")), "clips": clips})
    return out


def _claude_json(prompt: str, timeout: int = 240) -> dict:
    if not CLAUDE:
        abort(503, "the claude CLI was not found on this machine")
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)       # allow a nested headless run
    env["PYTHONUTF8"] = "1"
    try:
        r = subprocess.run(
            [CLAUDE, "-p", "--model", "sonnet",
             "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch,Task",
             "--append-system-prompt", CHAT_SYSTEM],
            input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            cwd=str(ROOT), env=env)
    except subprocess.TimeoutExpired:
        abort(504, "claude took too long to answer")
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        abort(502, f"claude CLI failed (rc={r.returncode}): {(r.stderr or '')[:300]}")
    if out.startswith("```"):
        out = out.split("```")[1]
        out = out[4:] if out.lower().startswith("json") else out
    s, e = out.find("{"), out.rfind("}")
    if s < 0 or e <= s:
        abort(502, f"claude did not return JSON: {out[:300]}")
    try:
        return json.loads(out[s:e + 1].replace("�", "-"))
    except json.JSONDecodeError as exc:
        abort(502, f"claude returned malformed JSON ({exc}): {out[s:s+300]}")


@bp.get("/api/seq/models")
def list_models():
    return jsonify({"models": ai_models.public_list()})


@bp.post("/api/seq/<slug>/chat")
def chat(slug):
    """Talk to the editor. Free edits are applied; paid work comes back to confirm."""
    b = request.get_json(force=True) or {}
    msg = (b.get("message") or "").strip()
    if not msg:
        abort(400, "say something")
    doc = load_doc(slug)

    media = [m["src"] for m in (list_media().get_json().get("media") or [])[:60]]
    prompt = (
        f"CURRENT TIMELINE:\n{json.dumps(_doc_for_claude(doc), indent=1)}\n\n"
        f"MEDIA AVAILABLE TO ADD (use these exact paths):\n"
        + "\n".join(media[:40]) + "\n\n"
        f"PLAYHEAD IS AT: {float(b.get('playhead') or 0):.2f}s\n"
        + (f"SELECTED CLIP: {b.get('selected')}\n" if b.get("selected") else "")
        + f"\nUSER REQUEST:\n{msg}\n")

    plan = _claude_json(prompt)
    ops = plan.get("ops") or []
    applied, errors = [], []

    for o in ops if isinstance(ops, list) else []:
        if not isinstance(o, dict) or o.get("op") not in FREE_OPS:
            errors.append("refused op " + str(o.get("op") if isinstance(o, dict) else o))
            continue
        try:
            _apply_op(doc, o)
            applied.append(o)
        except Exception as exc:
            errors.append(f"{o.get('op')}: {exc}")

    if applied:
        save_doc(slug, doc)
        doc = load_doc(slug)

    gen = plan.get("generate")
    if gen and not isinstance(gen, dict):
        gen = None
    if gen and not gen.get("clip"):
        gen = None
    fs = plan.get("faceswap")
    if fs and not isinstance(fs, dict):
        fs = None
    if fs and not fs.get("clip"):
        fs = None
    # hard guard: a face/actor change must NEVER route to the generate models —
    # they take no reference face, so the money is spent and the job fails
    # (this exact misroute burned $3.90 on 2026-08-08)
    if gen and re.search(r"(face|actor|actress|person|character|spokes\w*)", msg, re.I)            and re.search(r"(change|swap|replace|new|different|another)", msg, re.I):
        fs = fs or {"clip": gen.get("clip"),
                    "why": "face/actor change — rerouted from generate to face swap"}
        gen = None

    return jsonify({
        "reply": str(plan.get("reply") or "")[:1200],
        "applied": applied, "errors": errors,
        "generate": gen, "faceswap": fs, "slug": slug, **with_meta(doc),
    })


@bp.post("/api/seq/refs/upload")
def upload_ref():
    """Inspiration images the models can be steered by."""
    f = request.files.get("image")
    if not f or not f.filename:
        abort(400, "no image")
    ext = Path(f.filename).suffix.lower()
    if ext not in IMAGE_EXTS:
        abort(400, f"unsupported image type {ext!r}")
    d = ROOT / "output" / "seq-refs"
    d.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time())}-{secure_filename(Path(f.filename).stem)[:40]}{ext}"
    f.save(str(d / name))
    return jsonify({"ref": str((d / name).relative_to(ROOT)).replace("\\", "/"),
                    "name": name})


@bp.post("/api/seq/<slug>/generate")
def generate(slug):
    """Run a generative model on a clip (or make a new one) and swap it in.

    Two-step by design: the first call answers 402 with an estimate, and only a
    call carrying confirm_cost actually spends. Same discipline as every other
    paid path in this app.
    """
    b = request.get_json(force=True) or {}
    model_key = b.get("model") or ""
    if model_key not in ai_models.MODELS:
        abort(400, f"unknown model {model_key!r}")
    m = ai_models.MODELS[model_key]
    prompt = (b.get("prompt") or "").strip()
    if not prompt:
        abort(400, "describe the change first — every model needs a prompt "
                   "(fal rejects empty ones)")

    doc = load_doc(slug)
    clip_id = b.get("clip")
    clip = None
    if clip_id:
        try:
            _, clip = seq.find_clip(doc, clip_id)
        except KeyError:
            abort(404, "no such clip")

    resolution = b.get("resolution") or "720p"
    if m["resolutions"] and resolution not in m["resolutions"]:
        resolution = m["resolutions"][0]

    if m["mode"] == "v2v":
        if not clip:
            # default: the uploaded video — the first clip on a video track
            for t_ in doc["tracks"]:
                if t_["kind"] == "video" and t_["clips"]:
                    clip = sorted(t_["clips"], key=lambda c: float(c.get("start") or 0))[0]
                    clip_id = clip["id"]
                    break
        if not clip:
            abort(400, "add a video to the timeline first")
        seconds = min(seq.clip_dur(clip), float(m["max_sec"]))
        want = float(b.get("seconds") or 0)
        if want > 0:                      # user chose to edit only the first N seconds
            seconds = min(seconds, want)
    else:
        seconds = float(b.get("seconds") or 5)
    seconds = max(1.0, min(seconds, float(m["max_sec"])))

    est = ai_models.estimate(model_key, seconds, resolution)
    if not b.get("confirm_cost"):
        return jsonify({"needs_confirm": True, "estimate": est}), 402

    work = ROOT / "output" / "ai-clips"
    work.mkdir(parents=True, exist_ok=True)
    name = f"{slug}-{model_key}-{time.strftime('%H%M%S')}"

    cmd = [CV_PY or PY, str(GEN_PY), "--model", model_key, "--prompt", prompt,
           "--out", str(work), "--name", name, "--resolution", resolution,
           "--seconds", str(int(seconds)), "--env-file", str(FAL_ENV)]

    # v2v needs the clip's exact range as a file — cut it first
    if m["mode"] == "v2v":
        cut = work / f"{name}-src.mp4"
        src = (ROOT / clip["src"]).resolve()
        r = subprocess.run(
            [_ff("ffmpeg"), "-y", "-v", "error", "-ss", str(float(clip["in"])),
             "-t", str(seconds), "-i", str(src),
             "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", "-c:a", "aac", str(cut)],
            capture_output=True, text=True)
        if r.returncode != 0 or not cut.is_file():
            abort(500, f"could not extract the clip range: {(r.stderr or '')[-300:]}")
        cmd += ["--video", str(cut)]

    refs = [x for x in (b.get("refs") or []) if isinstance(x, str)][:m["refs"] or 0]
    ref_paths = []
    for r_ in refs:
        p = (ROOT / r_).resolve()
        if p.is_file() and str(p).startswith(str(ROOT.resolve())):
            ref_paths.append(str(p))
    if ref_paths:
        cmd += ["--refs", ",".join(ref_paths)]
    if b.get("image"):
        img = (ROOT / b["image"]).resolve()
        if img.is_file():
            cmd += ["--image", str(img)]

    job_id = _jobs_create("seq-generate", slug, f"✨ {m['label']} — {slug}", gpu=False)
    threading.Thread(target=_generate_worker,
                     args=(job_id, cmd, slug, clip_id, model_key, est),
                     daemon=True).start()
    return jsonify({"job_id": job_id, "estimate": est})


def _generate_worker(job_id, cmd, slug, clip_id, model_key, est):
    """Run the model, then wire the result into the document."""
    _run_job(job_id, cmd)
    from jobs import jobs, jobs_lock
    job = jobs.get(job_id) or {}
    if job.get("status") != "done":
        return
    with jobs_lock:
        lines = list(job.get("lines") or [])
    result = next((l.split("RESULT:", 1)[1].strip()
                   for l in reversed(lines) if "RESULT:" in l), None)
    if not result:
        with jobs_lock:
            job["lines"].append("[seq] model finished but produced no RESULT path")
        return

    try:
        rel = str(Path(result).resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        with jobs_lock:
            job["lines"].append(f"[seq] result landed outside the repo: {result}")
        return

    if _record_spend:
        try:
            _record_spend(slug, {"this_run": est["usd"], "summary": est["summary"],
                                 "engine": f"fal-{model_key}"})
        except Exception:
            pass

    try:
        doc = seq.load(seq.doc_path(ROOT, slug))
        # the result is APPENDED as its own clip — the original stays on the
        # timeline untouched, so you compare them side by side and keep the
        # one you like (delete the other). No more silent replacement.
        info = media_info(rel)
        new_clip = seq.new_clip(rel, 0, info.get("dur") or 5, 0)
        new_clip["origin"] = {"engine": model_key, "parent": None}
        seq.add_clip(doc, "V1", new_clip, append=True)
        seq.save(seq.doc_path(ROOT, slug), doc)
        with jobs_lock:
            job["lines"].append(f"[seq] added to the end of the timeline: {rel}")
    except Exception as exc:
        with jobs_lock:
            job["lines"].append(f"[seq] could not update the document: {exc}")


def _apply_op(doc: dict, o: dict) -> None:
    """The op vocabulary, shared by the HTTP endpoint and the chat planner."""
    op = o.get("op")
    if op == "add":
        info = media_info(o.get("src") or "")
        if info.get("missing"):
            raise ValueError(f"no such source {o.get('src')!r}")
        dur = float(o.get("dur") or info.get("dur") or 0)
        if dur <= 0:
            raise ValueError("unknown duration")
        track = o.get("track") or ("A1" if info["kind"] == "audio" else "V1")
        start = float(o.get("start") or 0)
        seq.add_clip(doc, track,
                     seq.new_clip(info["src"], float(o.get("in") or 0),
                                  float(o.get("in") or 0) + dur, start),
                     append=bool(o.get("append", True)))
    elif op == "add_text":
        c = seq.new_text_clip(str(o.get("text") or "TEXT"),
                              float(o.get("start") or 0), float(o.get("dur") or 2))
        if isinstance(o.get("style"), dict):
            c["style"].update({k: v for k, v in o["style"].items() if k in c["style"]})
        seq.add_clip(doc, o.get("track") or "T1", c)
    elif op == "remove":
        seq.remove_clip(doc, o.get("clip"), ripple=bool(o.get("ripple")))
    elif op == "move":
        seq.move_clip(doc, o.get("clip"), float(o.get("start") or 0), o.get("track"))
    elif op == "trim":
        seq.trim_clip(doc, o.get("clip"), edge=o.get("edge") or "out",
                      delta=float(o.get("delta") or 0))
    elif op == "split":
        seq.split_clip(doc, o.get("clip"), float(o.get("at") or 0))
    elif op == "ripple_delete":
        seq.ripple_delete_range(doc, o.get("track") or "V1",
                                float(o.get("a") or 0), float(o.get("b") or 0))
    elif op == "set":
        _, clip = seq.find_clip(doc, o.get("clip"))
        patch = o.get("patch") or {}
        for k in ("speed", "volume", "dur"):
            if k in patch:
                clip[k] = float(patch[k])
        if "text" in patch:
            clip["text"] = str(patch["text"])
        if isinstance(patch.get("transform"), dict):
            clip.setdefault("transform", {}).update(
                {k: float(v) for k, v in patch["transform"].items()
                 if k in ("scale", "x", "y", "rot")})
        if "fade_in" in patch or "fade_out" in patch:
            fx = [e for e in clip.get("effects") or []
                  if e.get("type") not in ("fade_in", "fade_out")]
            fi = max(0.0, min(float(patch.get("fade_in") or 0), 5.0))
            fo = max(0.0, min(float(patch.get("fade_out") or 0), 5.0))
            if fi > 0:
                fx.append({"type": "fade_in", "d": round(fi, 2)})
            if fo > 0:
                fx.append({"type": "fade_out", "d": round(fo, 2)})
            clip["effects"] = fx
        if isinstance(patch.get("style"), dict):
            clip.setdefault("style", {}).update(patch["style"])
    elif op == "track":
        if o.get("action") == "mute":
            seq.find_track(doc, o.get("track"))["muted"] = bool(o.get("muted"))
        elif o.get("action") == "add":
            kind = o.get("kind") or "video"
            if kind not in seq.TRACK_KINDS:
                raise ValueError("bad track kind")
            prefix = {"video": "V", "text": "T", "audio": "A"}[kind]
            n = sum(1 for t in doc["tracks"] if t["kind"] == kind) + 1
            doc["tracks"].append({"id": f"{prefix}{n}", "kind": kind,
                                  "name": f"{kind.title()} {n}", "muted": False, "clips": []})
    elif op == "rename":
        doc["name"] = str(o.get("name") or doc.get("name"))[:120]
    else:
        raise ValueError(f"unknown op {op!r}")


# ═══════════════════════════════════════════════════════════════════
#  Transcript layer — edit the video by editing its words
# ═══════════════════════════════════════════════════════════════════
# Whisper produces word timings per SOURCE file; the endpoints below map them
# onto the timeline per clip (start + (word_time - in) / speed), so deleting
# words becomes an ordinary ripple_delete. One primitive, many features.

def _words_file(src_rel: str) -> Path:
    """Cache path for a source's transcript, keyed on mtime+size so a replaced
    file re-transcribes instead of serving stale words."""
    import hashlib as _h
    p = ROOT / src_rel
    try:
        st = p.stat()
        stamp = f"{int(st.st_mtime)}-{st.st_size % 100000}"
    except OSError:
        stamp = "missing"
    key = _h.sha1(f"{src_rel}:{stamp}".encode()).hexdigest()[:16]
    d = ROOT / "output" / "seq-transcripts"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{Path(src_rel).stem[:40]}-{key}.json"


@bp.post("/api/seq/<slug>/transcribe")
def transcribe(slug):
    """Launch whisper jobs for every video source that lacks a transcript."""
    b = request.get_json(force=True) or {}
    doc = load_doc(slug)
    srcs = []
    if b.get("src"):
        srcs = [b["src"]]
    else:
        for t in doc["tracks"]:
            if t["kind"] != "video":
                continue
            for c in t["clips"]:
                s = c.get("src")
                if s and s not in srcs and not _words_file(s).is_file():
                    srcs.append(s)
    started = []
    for s in srcs[:6]:
        path = safe_rel(s)
        if not path.is_file():
            continue
        cmd = [WHISPER_PY or PY, "-u", str(ENGINE_DIR / "seq_transcribe.py"),
               "--src", str(path), "--out", str(_words_file(s))]
        # gpu=False: whisper runs CPU on this box (no cublas for ctranslate2),
        # so it must not queue behind the one-at-a-time GPU lock
        jid = _jobs_create("transcribe", Path(s).stem[:30],
                           f"🎙 Transcribe — {Path(s).name}", gpu=False)
        threading.Thread(target=_run_job, args=(jid, cmd), daemon=True).start()
        started.append({"job_id": jid, "src": s})
    return jsonify({"jobs": started})


@bp.get("/api/seq/<slug>/transcript")
def transcript(slug):
    """Words per clip, in TIMELINE seconds — ready for click-to-delete."""
    doc = load_doc(slug)
    clips, missing = [], []
    for t in doc["tracks"]:
        if t["kind"] != "video":
            continue
        for c in sorted(t["clips"], key=lambda c: float(c.get("start") or 0)):
            wf = _words_file(c["src"])
            entry = {"clip": c["id"], "track": t["id"], "src": c["src"],
                     "start": c["start"], "ready": wf.is_file(), "words": []}
            if wf.is_file():
                try:
                    data = json.loads(wf.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
                sp = float(c.get("speed") or 1) or 1
                cin, cout = float(c["in"]), float(c["out"])
                for w in data.get("words", []):
                    if w["e"] <= cin + 0.01 or w["s"] >= cout - 0.01:
                        continue
                    entry["words"].append({
                        "w": w["w"],
                        "s": round(float(c["start"]) + max(0.0, w["s"] - cin) / sp, 3),
                        "e": round(float(c["start"]) + max(0.0, w["e"] - cin) / sp, 3)})
            elif c["src"] not in missing:
                missing.append(c["src"])
            clips.append(entry)
    return jsonify({"clips": clips, "missing": missing})


@bp.post("/api/seq/<slug>/silence")
def silence(slug):
    """Find (or, with apply, remove) silent spans. Free — pure ffmpeg.

    Proposals carry the doc version; apply refuses if the timeline moved since
    the scan, because stale ranges would cut the wrong footage.
    """
    b = request.get_json(force=True) or {}
    doc = load_doc(slug)

    if b.get("apply"):
        ranges = [r for r in (b.get("ranges") or []) if isinstance(r, dict)]
        if int(b.get("version") or 0) != int(doc.get("version") or 0):
            abort(409, "the timeline changed since the scan — find silences again")
        # apply right-to-left so earlier ranges keep their coordinates
        for r in sorted(ranges, key=lambda r: -float(r["a"])):
            seq.ripple_delete_range(doc, r.get("track") or "V1",
                                    float(r["a"]), float(r["b"]))
        save_doc(slug, doc)
        return jsonify({"applied": len(ranges), "slug": slug, **with_meta(load_doc(slug))})

    thresh = float(b.get("db") or -35)
    min_d = float(b.get("min") or 0.6)
    pad = float(b.get("pad") or 0.12)
    props = []
    for t in doc["tracks"]:
        if t["kind"] != "video":
            continue
        for c in t["clips"]:
            src = safe_rel(c["src"])
            sp = float(c.get("speed") or 1) or 1
            src_len = float(c["out"]) - float(c["in"])
            if not src.is_file() or src_len < 1.0:
                continue
            r = subprocess.run(
                [_ff("ffmpeg"), "-hide_banner", "-nostats",
                 "-ss", str(float(c["in"])), "-t", str(src_len), "-i", str(src),
                 "-vn", "-af", f"silencedetect=n={thresh}dB:d={min_d}",
                 "-f", "null", "-"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=180)
            txt = r.stderr or ""
            starts = [float(m) for m in re.findall(r"silence_start:\s*(-?[0-9.]+)", txt)]
            ends = [float(m) for m in re.findall(r"silence_end:\s*(-?[0-9.]+)", txt)]
            for i, s0 in enumerate(starts):
                e0 = ends[i] if i < len(ends) else src_len
                a = float(c["start"]) + (max(0.0, s0) + pad) / sp
                z = float(c["start"]) + (min(src_len, e0) - pad) / sp
                if z - a >= 0.25:
                    props.append({"track": t["id"], "clip": c["id"],
                                  "a": round(a, 3), "b": round(z, 3),
                                  "len": round(z - a, 2)})
    props.sort(key=lambda p: p["a"])
    return jsonify({"proposals": props, "version": doc.get("version")})


@bp.post("/api/seq/<slug>/autosplit")
def autosplit(slug):
    """Scene-cut detection on one clip → real splits on the timeline.

    Drop a downloaded winner in whole, auto-split it, and every shot becomes
    its own clip to reorder or regenerate.
    """
    b = request.get_json(force=True) or {}
    doc = load_doc(slug)
    try:
        track, clip = seq.find_clip(doc, b.get("clip"))
    except KeyError:
        abort(404, "no such clip")
    src = safe_rel(clip["src"])
    if not src.is_file():
        abort(404, "source missing")
    sp = float(clip.get("speed") or 1) or 1
    src_len = float(clip["out"]) - float(clip["in"])
    thresh = max(0.1, min(float(b.get("thresh") or 0.30), 0.9))

    r = subprocess.run(
        [_ff("ffmpeg"), "-hide_banner", "-nostats",
         "-ss", str(float(clip["in"])), "-t", str(src_len), "-i", str(src),
         "-vf", f"scale=320:-2,select='gt(scene,{thresh})',showinfo",
         "-an", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600)
    times = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", r.stderr or "")]

    cuts, last = [], 0.0
    for st in sorted(times):
        if st < 0.4 or st > src_len - 0.4 or st - last < 0.4:
            continue
        cuts.append(st)
        last = st

    made = []
    for st in cuts:
        tt = float(clip["start"]) + st / sp
        piece = next((c for c in track["clips"]
                      if float(c["start"]) + 0.05 < tt < seq.clip_end(c) - 0.05), None)
        if not piece:
            continue
        try:
            seq.split_clip(doc, piece["id"], tt)
            made.append(round(tt, 3))
        except ValueError:
            continue
    if made:
        save_doc(slug, doc)
        doc = load_doc(slug)
    return jsonify({"cuts": made, "slug": slug, **with_meta(doc)})


@bp.post("/api/seq/extend")
def extend_video():
    """Boomerang-extend a clip to a target length — free, local, seamless.

    The result lands in uploads/ (not a scratch folder) so it is immediately
    available to BOTH the timeline and the Creator's dub/lipsync pipeline.
    """
    b = request.get_json(force=True) or {}
    src = safe_rel(b.get("src") or "")
    if not src.is_file():
        abort(404, "source not found")
    try:
        seconds = float(b.get("seconds") or 0)
    except (TypeError, ValueError):
        abort(400, "seconds must be a number")
    if not (2 <= seconds <= 600):
        abort(400, "target must be between 2 and 600 seconds")

    up = ROOT / "uploads"
    up.mkdir(exist_ok=True)
    base = f"{src.stem[:48]}-x{int(seconds)}s"
    name, n = f"{base}.mp4", 2
    while (up / name).exists():
        name = f"{base}-{n}.mp4"
        n += 1

    cmd = [PY, "-u", str(ENGINE_DIR / "seq_extend.py"),
           "--src", str(src), "--out", str(up / name), "--seconds", str(seconds)]
    jid = _jobs_create("extend", src.stem[:30], f"⏩ Extend — {src.name} → {int(seconds)}s",
                       gpu=False)
    threading.Thread(target=_run_job, args=(jid, cmd), daemon=True).start()
    return jsonify({"job_id": jid, "name": name,
                    "src": f"uploads/{name}"})


# ═══════════════════════════════════════════════════════════════════
#  Avatar bank + face swap — change the actor, keep the performance
# ═══════════════════════════════════════════════════════════════════
# Avatars are GLOBAL (output/avatars/), not per-project — save a face once,
# reuse it in every video. The swap itself calls the video-face-swap skill's
# script, which already carries the four hard-won pipeline fixes (audio
# conform, shot-aware chunking, size lock, overlap dissolve).

FACESWAP_PY = Path.home() / ".claude" / "skills" / "video-face-swap" / "scripts" / "faceswap_video.py"

FS_ENGINES = {
    "wan-replace": {
        "label": "Wan Animate Replace — best quality, relights into the shot",
        "resolutions": ["480p", "580p", "720p"],
        "per_16_frames": {"480p": 0.04, "580p": 0.06, "720p": 0.08},
        "note": "Re-renders the WHOLE frame: wardrobe and set may change too.",
    },
    "pixverse": {
        "label": "PixVerse Swap — keeps wardrobe and set, cheaper",
        "resolutions": ["360p", "540p", "720p"],
        "per_5s": {"360p": 0.15, "540p": 0.15, "720p": 0.20},
        "note": "Face only; the original clothing and background survive.",
    },
}


def _avatars_dir() -> Path:
    d = ROOT / "output" / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _avatar_face(aid: str) -> Path:
    d = _avatars_dir() / aid
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        p = d / f"face{ext}"
        if p.is_file():
            return p
    abort(404, f"avatar {aid!r} has no face image")


@bp.get("/api/seq/avatars")
def list_avatars():
    out = []
    base = _avatars_dir()
    for d in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir() or d.name.startswith("."):
            continue
        meta = {}
        mf = d / "avatar.json"
        if mf.is_file():
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        face = next((d / f"face{e}" for e in (".png", ".jpg", ".jpeg", ".webp")
                     if (d / f"face{e}").is_file()), None)
        if not face:
            continue
        out.append({"id": d.name, "name": meta.get("name") or d.name,
                    "image": str(face.relative_to(ROOT)).replace("\\", "/"),
                    "created": meta.get("created")})
    return jsonify({"avatars": out})


@bp.post("/api/seq/avatars/create")
def create_avatar():
    """Save a face as a reusable avatar — from an uploaded image, or grabbed
    straight out of a video frame ("keep THIS actor")."""
    name = ""
    aid = f"av{int(time.time()) % 10 ** 8:08d}"
    d = _avatars_dir() / aid
    d.mkdir(parents=True, exist_ok=True)

    if request.files.get("image"):                          # route 1: image upload
        f = request.files["image"]
        name = (request.form.get("name") or "").strip()
        ext = Path(f.filename or "face.png").suffix.lower()
        if ext not in IMAGE_EXTS:
            abort(400, f"unsupported image type {ext!r}")
        f.save(str(d / f"face{ext}"))
    else:                                                   # route 2: video frame
        b = request.get_json(force=True) or {}
        name = (b.get("name") or "").strip()
        src = safe_rel(b.get("from_video") or "")
        if not src.is_file():
            abort(404, "video not found")
        at = max(0.0, float(b.get("at") or 0))
        r = subprocess.run(
            [_ff("ffmpeg"), "-y", "-v", "error", "-ss", str(at), "-i", str(src),
             "-frames:v", "1", "-q:v", "2", str(d / "face.jpg")],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not (d / "face.jpg").is_file():
            d.rmdir()
            abort(500, f"could not grab the frame: {(r.stderr or '')[-200:]}")

    (d / "avatar.json").write_text(json.dumps({
        "name": name or aid, "created": time.time()}), encoding="utf-8")
    return jsonify({"id": aid, "name": name or aid,
                    "image": str(next(d.glob("face.*")).relative_to(ROOT)).replace("\\", "/")})


@bp.post("/api/seq/avatars/delete")
def delete_avatar():
    b = request.get_json(force=True) or {}
    aid = re.sub(r"[^a-z0-9_-]", "", str(b.get("id") or ""))
    d = _avatars_dir() / aid
    if not aid or not d.is_dir():
        abort(404, "no such avatar")
    trash = _avatars_dir() / ".trash"
    trash.mkdir(exist_ok=True)
    d.rename(trash / f"{aid}-{int(time.time())}")
    return jsonify({"deleted": aid})


def _fs_estimate(engine: str, seconds: float, resolution: str) -> dict:
    e = FS_ENGINES[engine]
    if resolution not in e["resolutions"]:
        resolution = e["resolutions"][-1]
    if "per_16_frames" in e:
        blocks = math.ceil(seconds * 30 / 16)
        usd = round(blocks * e["per_16_frames"][resolution], 2)
    else:
        usd = round(math.ceil(seconds / 5) * e["per_5s"][resolution], 2)
    return {"usd": usd, "seconds": round(seconds, 2), "resolution": resolution,
            "engine": engine, "verified": True,
            "summary": f"{e['label'].split(' — ')[0]} · {seconds:.1f}s @ {resolution} ≈ ${usd:.2f}",
            "model": engine}


@bp.post("/api/seq/<slug>/faceswap")
def faceswap(slug):
    """Swap the actor's face on one clip using a banked avatar.

    Same money discipline as everything else: 402 with an estimate until the
    request carries confirm_cost. The original clip stays revertible — the
    result swaps in via the EDL's origin mechanism.
    """
    b = request.get_json(force=True) or {}
    engine = b.get("engine") or "wan-replace"
    if engine not in FS_ENGINES:
        abort(400, f"unknown engine {engine!r}")
    if not FACESWAP_PY.is_file():
        abort(503, "the face-swap script is missing from ~/.claude/skills/video-face-swap")

    doc = load_doc(slug)
    try:
        _, clip = seq.find_clip(doc, b.get("clip"))
    except KeyError:
        abort(404, "no such clip")
    face = _avatar_face(str(b.get("avatar") or ""))

    seconds = min(seq.clip_dur(clip), 120.0)
    resolution = b.get("resolution") or "720p"
    est = _fs_estimate(engine, seconds, resolution)
    if not b.get("confirm_cost"):
        return jsonify({"needs_confirm": True, "estimate": est}), 402

    work = ROOT / "output" / "ai-clips"
    work.mkdir(parents=True, exist_ok=True)
    name = f"{slug}-faceswap-{time.strftime('%H%M%S')}"

    # cut the clip's exact range first — the swap runs on just that footage
    cut = work / f"{name}-src.mp4"
    src = (ROOT / clip["src"]).resolve()
    r = subprocess.run(
        [_ff("ffmpeg"), "-y", "-v", "error", "-ss", str(float(clip["in"])),
         "-t", str(seconds), "-i", str(src),
         "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", str(cut)],
        capture_output=True, text=True)
    if r.returncode != 0 or not cut.is_file():
        abort(500, f"could not extract the clip range: {(r.stderr or '')[-300:]}")

    out = work / f"{name}.mp4"
    cmd = [CV_PY or PY, "-u", str(FACESWAP_PY),
           "--input", str(cut), "--face", str(face), "--out", str(out),
           "--engine", engine, "--resolution", est["resolution"],
           "--match-source-size", "--env-file", str(FAL_ENV), "--yes"]

    jid = _jobs_create("faceswap", slug, f"🎭 Face swap — {slug} [{engine}]", gpu=False)
    threading.Thread(target=_faceswap_worker,
                     args=(jid, cmd, slug, b.get("clip"), out, engine, est),
                     daemon=True).start()
    return jsonify({"job_id": jid, "estimate": est})


def _faceswap_worker(job_id, cmd, slug, clip_id, out: Path, engine, est):
    _run_job(job_id, cmd)
    from jobs import jobs, jobs_lock
    job = jobs.get(job_id) or {}
    if job.get("status") != "done" or not out.is_file():
        return
    if _record_spend:
        try:
            _record_spend(slug, {"this_run": est["usd"], "summary": est["summary"],
                                 "engine": f"fal-faceswap-{engine}"})
        except Exception:
            pass
    try:
        rel = str(out.relative_to(ROOT)).replace("\\", "/")
        doc = seq.load(seq.doc_path(ROOT, slug))
        seq.swap_source(doc, clip_id, rel, f"faceswap-{engine}")
        seq.save(seq.doc_path(ROOT, slug), doc)
        with jobs_lock:
            job["lines"].append(f"[seq] new face wired into the timeline: {rel}")
    except Exception as exc:
        with jobs_lock:
            job["lines"].append(f"[seq] could not update the document: {exc}")


@bp.post("/api/seq/avatars/generate")
def generate_avatar():
    """Make a brand-new face with AI and bank it as an avatar.

    Nano Banana, one 1:1 image, $0.04 — cost-gated like everything else. The
    engine wraps the prompt with the face-swap skill's reference-face framing
    (frontal, even light, neutral expression) so the result actually works as
    a swap reference, not just a pretty picture.
    """
    b = request.get_json(force=True) or {}
    prompt = (b.get("prompt") or "").strip()
    if not prompt:
        abort(400, "describe the face you want")
    est = {"usd": 0.04, "summary": f"Nano Banana avatar · 1 image ≈ $0.04",
           "model": "nano-banana", "verified": True}
    if not b.get("confirm_cost"):
        return jsonify({"needs_confirm": True, "estimate": est}), 402

    aid = f"av{int(time.time()) % 10 ** 8:08d}"
    d = _avatars_dir() / aid
    d.mkdir(parents=True, exist_ok=True)
    name = (b.get("name") or "").strip() or prompt[:40]
    (d / "avatar.json").write_text(json.dumps({
        "name": name, "created": time.time(), "prompt": prompt,
        "pending": True}), encoding="utf-8")

    cmd = [CV_PY or PY, "-u", str(ENGINE_DIR / "seq_avatar_gen.py"),
           "--prompt", prompt, "--out", str(d / "face.png"),
           "--env-file", str(FAL_ENV)]
    jid = _jobs_create("avatar-gen", aid, f"🎭 Make avatar — {name[:30]}", gpu=False)
    threading.Thread(target=_avatar_gen_worker, args=(jid, cmd, aid, name),
                     daemon=True).start()
    return jsonify({"job_id": jid, "id": aid, "estimate": est})


def _avatar_gen_worker(job_id, cmd, aid, name):
    _run_job(job_id, cmd)
    from jobs import jobs, jobs_lock
    job = jobs.get(job_id) or {}
    d = _avatars_dir() / aid
    if job.get("status") == "done" and (d / "face.png").is_file():
        (d / "avatar.json").write_text(json.dumps({
            "name": name, "created": time.time(), "generated": True}), encoding="utf-8")
        if _record_spend:
            try:
                _record_spend(aid, {"this_run": 0.04,
                                    "summary": f"avatar '{name}' (nano-banana)",
                                    "engine": "fal-nano-banana"})
            except Exception:
                pass
    else:
        # never leave a half-made avatar in the bank
        for f in d.glob("*"):
            f.unlink(missing_ok=True)
        try:
            d.rmdir()
        except OSError:
            pass
        with jobs_lock:
            job.setdefault("lines", []).append("[seq] avatar discarded (generation failed)")


DESCRIBE_PROMPT = """Read this image file with the Read tool and look at the person on screen:

  {frame}

Describe what you see in THREE parts, each a single comma-separated line:

1. face  - apparent age range, gender presentation, hair style and colour, facial
           hair, skin tone, face shape, notable features, expression and vibe.
           No names, no identity guessing.
2. wearing    - clothing: garment types, colours, style, accessories.
3. background - the setting: location type, colours, lighting, notable objects,
                indoor/outdoor, mood.

Reply with ONLY this JSON, nothing else:
{{"face": "<line>", "wearing": "<line>", "background": "<line>"}}"""


@bp.post("/api/seq/avatars/describe")
def describe_actor():
    """Look at a frame of the video and describe the actor as a face prompt.

    Free (local Claude vision). The point: see in words WHO is in the footage,
    then edit that line — "same person but older", "same vibe, different face" —
    and feed it straight into Make avatar.
    """
    if not CLAUDE:
        abort(503, "the claude CLI was not found on this machine")
    b = request.get_json(force=True) or {}
    src = safe_rel(b.get("from_video") or "")
    if not src.is_file():
        abort(404, "video not found")
    at = max(0.0, float(b.get("at") or 0))

    d = _avatars_dir() / ".describe"
    d.mkdir(exist_ok=True)
    frame = d / f"frame-{int(time.time())}.jpg"
    r = subprocess.run(
        [_ff("ffmpeg"), "-y", "-v", "error", "-ss", str(at), "-i", str(src),
         "-frames:v", "1", "-q:v", "2", str(frame)],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not frame.is_file():
        abort(500, f"could not grab the frame: {(r.stderr or '')[-200:]}")

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env["PYTHONUTF8"] = "1"
    try:
        cr = subprocess.run(
            [CLAUDE, "-p", "--model", "sonnet", "--allowedTools", "Read"],
            input=DESCRIBE_PROMPT.format(frame=str(frame)),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, cwd=str(d), env=env)
    except subprocess.TimeoutExpired:
        frame.unlink(missing_ok=True)
        abort(504, "the describer took too long — try again")
    frame.unlink(missing_ok=True)
    out = cr.stdout or ""
    i, j = out.find("{"), out.rfind("}")
    if cr.returncode != 0 or i < 0 or j <= i:
        abort(502, f"describe failed: {(cr.stderr or out)[:250]}")
    try:
        d3 = json.loads(out[i:j + 1].replace(chr(65533), "-"))
    except json.JSONDecodeError:
        abort(502, "describer reply was not valid JSON — try again")
    face = str(d3.get("face") or "").strip()
    wearing = str(d3.get("wearing") or "").strip()
    background = str(d3.get("background") or "").strip()
    if not face:
        abort(502, "no face found to describe in that frame — scrub to a clearer moment")
    combined = face
    if wearing:
        combined += ", wearing " + wearing
    if background:
        combined += ", background: " + background
    return jsonify({"face": face[:300], "wearing": wearing[:300],
                    "background": background[:300],
                    "description": combined[:700], "at": at})
