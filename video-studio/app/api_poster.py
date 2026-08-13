#!/usr/bin/env python3
"""Poster Studio — HTTP surface (single static poster generation, MODE B).

A Blueprint for the same reason api_sequence.py / api_batches.py are: server.py
is already huge and a new blueprint can't disturb any existing route.

Pipeline (per slug, under output/generated-brand-poster/<slug>/):
  upload   → product.<ext> (+ object.<ext>)
  plan     → planner engine runs SYNCHRONOUSLY → plan.json / brief.txt
  generate → gen (or gen+composite) as a background job (gpu=False)
  composite→ Pillow composite of one raw (free, no cost gate)
  item     → plan + raws + finals + manifest for the gallery

Money discipline: /estimate returns the price; /generate 402s until it carries
confirm_cost, exactly like every other paid path in this app.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Blueprint, abort, jsonify, request
from werkzeug.utils import secure_filename

sys.path.insert(0, str(Path(__file__).resolve().parent / "engines"))
import image_edit  # noqa: E402  (MODELS + cost_per_image, import-safe)

bp = Blueprint("poster", __name__)

# injected by init() so this module never imports server.py (which imports it)
ROOT: Path = Path(".")
CV_PY: str = ""
FAL_ENV: Path = Path(".")
CLAUDE: str = ""
PLANNER_PY: Path = Path(".")
GEN_PY: Path = Path(".")
COMPOSITOR_DIR: Path = Path(".")
BRAND_DIR: Path = Path(".")
LAYOUT_JSON: Path = Path(".")
PROMPT_FILE: Path = Path(".")
_jobs_create = None
_run_job = None
_record_spend = None

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
RAW_RE = re.compile(r"^raw-\d+\.png$")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_UPLOAD = 25 * 1024 * 1024

CLAUDE_MODELS = ["sonnet", "opus", "haiku"]
GEMINI_MODELS = ["google/gemini-2.5-flash", "google/gemini-2.5-pro"]
GEMINI_COST = 0.03

ASPECTS = [
    {"key": "ig_feed", "fal": "4:5", "w": 1080, "h": 1350},
    {"key": "tiktok", "fal": "9:16", "w": 1080, "h": 1920},
]


def init(root, cv_py, fal_env, claude_exe, planner_py, gen_py, compositor_dir,
         brand_dir, layout_json, prompt_file, jobs_create, run_job, record_spend):
    global ROOT, CV_PY, FAL_ENV, CLAUDE, PLANNER_PY, GEN_PY
    global COMPOSITOR_DIR, BRAND_DIR, LAYOUT_JSON, PROMPT_FILE
    global _jobs_create, _run_job, _record_spend
    ROOT = Path(root)
    CV_PY = cv_py or ""
    FAL_ENV = Path(fal_env) if fal_env else Path(".")
    CLAUDE = claude_exe or ""
    PLANNER_PY = Path(planner_py)
    GEN_PY = Path(gen_py)
    COMPOSITOR_DIR = Path(compositor_dir)
    BRAND_DIR = Path(brand_dir)
    LAYOUT_JSON = Path(layout_json)
    PROMPT_FILE = Path(prompt_file)
    _jobs_create = jobs_create
    _run_job = run_job
    _record_spend = record_spend


# ---------------------------------------------------------------- helpers

def _workdir(slug: str) -> Path:
    if not SLUG_RE.match(slug or ""):
        abort(400, "bad poster slug")
    d = ROOT / "output" / "generated-brand-poster" / slug
    if not d.is_dir():
        abort(404, f"no poster {slug!r}")
    return d


def _find(workdir: Path, stem: str) -> Path | None:
    for ext in IMAGE_EXTS:
        p = workdir / f"{stem}{ext}"
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def _plan_provider(workdir: Path) -> str:
    p = workdir / "plan.json"
    if not p.is_file():
        return "claude"
    try:
        return (json.loads(p.read_text(encoding="utf-8")) or {}).get("provider") or "claude"
    except Exception:
        return "claude"


def _gen_models() -> list[dict]:
    out = []
    for key, m in image_edit.MODELS.items():
        out.append({"key": key, "label": m["label"], "cost": m["cost"],
                    "resolutions": m.get("resolutions", [])})
    return out


# ---------------------------------------------------------------- routes

@bp.get("/api/poster/config")
def config():
    health = {
        "layout_json": LAYOUT_JSON.is_file(),
        "brand_dir": BRAND_DIR.is_dir(),
        "wordmark": (BRAND_DIR / "brand-kit" / "01-liit-wordmark-brand-logo.png").is_file(),
        "planner_prompt": PROMPT_FILE.is_file(),
        "compositor": (COMPOSITOR_DIR / "layout_compositor.py").is_file(),
    }
    health["ok"] = all(health.values())
    return jsonify({
        "providers": {
            "claude": {"models": CLAUDE_MODELS},
            "gemini": {"models": GEMINI_MODELS},
        },
        "gen_models": _gen_models(),
        "aspects": ASPECTS,
        "resolutions": ["0.5K", "1K", "2K", "4K"],
        "defaults": {"gen_model": "nano-banana", "aspect": "ig_feed",
                     "num": 4, "resolution": "1K"},
        "health": health,
    })


@bp.post("/api/poster/upload")
def upload():
    product = request.files.get("product")
    obj = request.files.get("object")
    if not product or not product.filename:
        abort(400, "drop a product photo")

    slug = f"poster-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() % 10 ** 4):04d}"
    workdir = ROOT / "output" / "generated-brand-poster" / slug
    workdir.mkdir(parents=True, exist_ok=True)

    def save(f, stem: str) -> str:
        ext = Path(f.filename).suffix.lower()
        if ext not in IMAGE_EXTS:
            abort(400, f"unsupported image type {ext!r}")
        dest = workdir / f"{stem}{ext}"
        f.save(str(dest))
        if dest.stat().st_size > MAX_UPLOAD:
            dest.unlink(missing_ok=True)
            abort(413, "image too large (25 MB max)")
        return dest.name

    pname = save(product, "product")
    oname = save(obj, "object") if (obj and obj.filename) else ""

    return jsonify({
        "slug": slug,
        "product": f"output/generated-brand-poster/{slug}/{pname}",
        "object": f"output/generated-brand-poster/{slug}/{oname}" if oname else "",
    })


@bp.post("/api/poster/plan")
def plan():
    b = request.get_json(force=True) or {}
    slug = b.get("slug") or ""
    workdir = _workdir(slug)
    product_path = _find(workdir, "product")
    if not product_path:
        abort(400, "upload a product photo first")

    provider = b.get("provider") or "claude"
    if provider not in ("claude", "gemini"):
        abort(400, f"unknown provider {provider!r}")
    model = (b.get("model") or "").strip()
    if provider == "claude":
        if model not in CLAUDE_MODELS:
            model = "sonnet"
        if not CLAUDE:
            abort(503, "the claude CLI was not found on this machine")
    else:
        if model not in GEMINI_MODELS:
            model = GEMINI_MODELS[0]

    brief = (b.get("brief") or "").strip()
    (workdir / "brief.txt").write_text(brief, encoding="utf-8")
    object_path = _find(workdir, "object")

    cmd = [CV_PY or sys.executable, "-u", str(PLANNER_PY),
           "--provider", provider, "--model", model,
           "--product-image", str(product_path),
           "--brief-file", str(workdir / "brief.txt"),
           "--layout-json", str(LAYOUT_JSON),
           "--prompt-file", str(PROMPT_FILE),
           "--out-dir", str(workdir)]
    if object_path:
        cmd += ["--additional-object", str(object_path)]
    if provider == "claude":
        cmd += ["--claude-exe", CLAUDE]
    if FAL_ENV.is_file():
        cmd += ["--env-file", str(FAL_ENV)]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)   # allow a nested headless run
    env["PYTHONUTF8"] = "1"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300, env=env)
    except subprocess.TimeoutExpired:
        abort(504, "the planner took too long to answer")

    out = (r.stdout or "") + "\n" + (r.stderr or "")
    if r.returncode != 0:
        abort(502, f"planner failed: {out[-600:]}")

    result = None
    for line in reversed(out.splitlines()):
        if line.startswith("RESULT:"):
            result = line.split("RESULT:", 1)[1].strip()
            break
    planner_cost = 0.0
    for line in out.splitlines():
        if line.startswith("PLANNER_COST:"):
            try:
                planner_cost = float(line.split("PLANNER_COST:", 1)[1].strip())
            except ValueError:
                pass

    plan_path = Path(result) if result else (workdir / "plan.json")
    if not plan_path.is_file():
        abort(502, "the planner produced no plan.json")
    plan_data = json.loads(plan_path.read_text(encoding="utf-8"))

    if planner_cost > 0 and _record_spend:
        try:
            _record_spend(slug, {"this_run": planner_cost,
                                 "summary": f"Poster planner ({provider})",
                                 "engine": f"planner-{provider}"})
        except Exception:
            pass

    return jsonify({"plan": plan_data, "planner_cost": planner_cost})


@bp.post("/api/poster/estimate")
def estimate():
    b = request.get_json(force=True) or {}
    gen_model = b.get("gen_model") or "nano-banana"
    if gen_model not in image_edit.MODELS:
        abort(400, f"unknown model {gen_model!r}")
    num = max(1, min(4, int(b.get("num") or 1)))
    resolution = b.get("resolution") or "1K"
    provider = b.get("provider") or "claude"
    planner = GEMINI_COST if provider == "gemini" else 0.0
    gen = round(image_edit.cost_per_image(gen_model, resolution) * num, 4)
    parts = [f"{image_edit.MODELS[gen_model]['label'].split(' — ')[0]} · "
             f"{num} image(s) @ {resolution} ≈ ${gen:.3f}"]
    if planner:
        parts.append("planner $0.03")
    return jsonify({"this_run": round(gen + planner, 4), "gen": gen,
                    "planner": planner, "summary": " · ".join(parts)})


@bp.post("/api/poster/generate")
def generate():
    b = request.get_json(force=True) or {}
    slug = b.get("slug") or ""
    workdir = _workdir(slug)
    product_path = _find(workdir, "product")
    if not product_path:
        abort(400, "upload a product photo first")
    plan_path = workdir / "plan.json"
    if not plan_path.is_file():
        abort(400, "run the planner first")

    gen_model = b.get("gen_model") or "nano-banana"
    if gen_model not in image_edit.MODELS:
        abort(400, f"unknown model {gen_model!r}")
    aspect = b.get("aspect") or "ig_feed"
    if aspect not in ("ig_feed", "tiktok"):
        abort(400, "bad aspect")
    num = max(1, min(4, int(b.get("num") or 1)))
    seed = int(b.get("seed") or 0)
    resolution = b.get("resolution") or "1K"
    scrim = bool(b.get("scrim"))
    editable = bool(b.get("editable"))

    provider = _plan_provider(workdir)
    planner_flat = GEMINI_COST if provider == "gemini" else 0.0
    gen_cost = round(image_edit.cost_per_image(gen_model, resolution) * num, 4)
    est = {"this_run": round(gen_cost + planner_flat, 4), "gen": gen_cost,
           "planner": planner_flat,
           "summary": f"{image_edit.MODELS[gen_model]['label'].split(' — ')[0]} · "
                      f"{num} image(s) @ {resolution} ≈ ${gen_cost:.3f}"
                      + (" + planner $0.03" if planner_flat else "")}

    if not b.get("confirm_cost"):
        return jsonify({"needs_confirm": True, "estimate": est}), 402

    object_path = _find(workdir, "object")
    stage = "gen" if editable else "all"
    cmd = [CV_PY or sys.executable, "-u", str(GEN_PY),
           "--stage", stage,
           "--workdir", str(workdir),
           "--product-image", str(product_path),
           "--plan", str(plan_path),
           "--gen-model", gen_model,
           "--aspect", aspect,
           "--num", str(num),
           "--seed", str(seed),
           "--resolution", resolution,
           "--layout-json", str(LAYOUT_JSON),
           "--brand-dir", str(BRAND_DIR),
           "--env-file", str(FAL_ENV)]
    if object_path:
        cmd += ["--additional-object", str(object_path)]
    if scrim:
        cmd += ["--scrim"]

    label = f"🖼 Poster {'gen (raws)' if editable else 'make'} — {slug}"
    job_id = _jobs_create("poster-gen", slug, label, gpu=False)
    threading.Thread(target=_gen_worker,
                     args=(job_id, cmd, slug, gen_cost, gen_model, resolution),
                     daemon=True).start()
    return jsonify({"job_id": job_id, "estimate": est})


def _gen_worker(job_id, cmd, slug, gen_cost, gen_model, resolution):
    _run_job(job_id, cmd)
    from jobs import jobs
    job = jobs.get(job_id) or {}
    if job.get("status") != "done":
        return
    if _record_spend:
        try:
            _record_spend(slug, {"this_run": gen_cost,
                                 "summary": f"Poster gen {gen_model} · {resolution}",
                                 "engine": f"fal-{gen_model}"})
        except Exception:
            pass


@bp.post("/api/poster/composite")
def composite():
    b = request.get_json(force=True) or {}
    slug = b.get("slug") or ""
    workdir = _workdir(slug)
    plan_path = workdir / "plan.json"
    if not plan_path.is_file():
        abort(400, "run the planner first")

    raw = (b.get("raw") or "").strip()
    if not RAW_RE.match(raw) or Path(raw).name != raw:
        abort(400, "pick a raw image to composite")
    if not (workdir / raw).is_file():
        abort(404, f"no such raw: {raw}")
    aspect = b.get("aspect") or "ig_feed"
    if aspect not in ("ig_feed", "tiktok"):
        abort(400, "bad aspect")
    scrim = bool(b.get("scrim"))

    cmd = [CV_PY or sys.executable, "-u", str(GEN_PY),
           "--stage", "composite",
           "--workdir", str(workdir),
           "--plan", str(plan_path),
           "--composite-raw", raw,
           "--aspect", aspect,
           "--layout-json", str(LAYOUT_JSON),
           "--brand-dir", str(BRAND_DIR)]
    if scrim:
        cmd += ["--scrim"]

    job_id = _jobs_create("poster-composite", slug, f"🧩 Composite — {slug}", gpu=False)
    threading.Thread(target=_run_job, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id})


@bp.get("/api/poster/item/<slug>")
def item(slug):
    workdir = _workdir(slug)
    plan = {}
    p = workdir / "plan.json"
    if p.is_file():
        try:
            plan = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            plan = {}
    manifest = {}
    m = workdir / "manifest.json"
    if m.is_file():
        try:
            manifest = json.loads(m.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}

    base = f"output/generated-brand-poster/{slug}"

    def listing(pattern: str) -> list[dict]:
        out = []
        for f in sorted(workdir.glob(pattern)):
            if f.stat().st_size <= 0:
                continue
            out.append({"name": f.name, "url": f"/media/{base}/{f.name}"})
        return out

    return jsonify({"slug": slug, "plan": plan,
                    "raws": listing("raw-*.png"),
                    "finals": listing("final-*.jpg"),
                    "manifest": manifest})
