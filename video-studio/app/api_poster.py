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


@bp.after_request
def _no_cache(resp):
    """All /api/poster/* responses must be fresh — listing and preview URLs
    return paths to on-disk files that get overwritten in place when the user
    regenerates. Without no-store the browser serves the stale cached
    listing / image even though the file content changed."""
    resp.headers["Cache-Control"] = "no-store"
    return resp


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


@bp.post("/api/poster/copy")
def copy():
    b = request.get_json(force=True) or {}
    slug = b.get("slug") or ""
    workdir = _workdir(slug)
    plan_path = workdir / "plan.json"
    if not plan_path.is_file():
        abort(400, "run the planner first")

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    elements = []
    for line in (plan.get("copy_elements") or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            elements.append(obj)

    defaults = {
        "HEADLINE": {"font_family": "Bricolage Grotesque", "font_weight": "bold", "color": "#C9C3DA"},
        "SUBTITLE": {"font_family": "Hanken Grotesk", "font_weight": "medium", "color": "#FFC233"},
        "BODY": {"font_family": "Newsreader", "font_weight": "regular", "color": "#8E899F"},
    }

    for key, default in defaults.items():
        value = (b.get(key.lower()) or "").strip()
        el = next((e for e in elements if e.get("type") == key), None)
        if value:
            if el is None:
                elements.append({"type": key, "text": value, **default})
            else:
                el["text"] = value
        elif el is not None:
            elements.remove(el)

    if "copy_elements_original" not in plan:
        plan["copy_elements_original"] = plan.get("copy_elements") or ""

    plan["copy_elements"] = "\n".join(json.dumps(e, ensure_ascii=False) for e in elements)
    plan_path.write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding="utf-8")
    (workdir / "copy-elements.jsonl").write_text(plan["copy_elements"], encoding="utf-8")

    return jsonify({"ok": True, "copy_elements": plan["copy_elements"]})


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


# ---------------------------------------------------------------- layout editor

LAYOUT_ALLOWED_ZONES = {"headline", "subheadline", "body", "logo"}
LAYOUT_ALIGN_VALUES = {"left", "center", "right"}
LAYOUT_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _layout_compositor_module():
    """FIX-1: lazily import layout_compositor so PIL only loads when needed.

    Returns the imported module. Aborts 503 if the file is missing.
    """
    if not (COMPOSITOR_DIR / "layout_compositor.py").is_file():
        abort(503, f"compositor module missing at {COMPOSITOR_DIR}")
    sys.path.insert(0, str(COMPOSITOR_DIR))
    try:
        import layout_compositor  # noqa: E402
    except Exception as e:
        abort(503, f"could not import layout_compositor: {e}")
    return layout_compositor


def _layout_logo_positions(layout_data: dict) -> list[str]:
    keys = [k for k in (layout_data.get("logo_positions") or {}).keys()
            if not k.startswith("_")]
    return sorted(keys)


def _resolve_layout_defaults(workdir: Path, layout_data: dict) -> dict:
    """Return resolved (template_id, logo_position, template_zones, presets).

    Mirrors poster_gen.resolve_chosen_layout: if plan.json has no valid
    [CHOSEN LAYOUT], fall back to the first template + logo_position_default.
    """
    templates = layout_data.get("templates") or {}
    logo_positions = _layout_logo_positions(layout_data)

    plan = {}
    p = workdir / "plan.json"
    if p.is_file():
        try:
            plan = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            plan = {}
    lc = _layout_compositor_module()
    chosen = plan.get("chosen_layout") or ""
    template_id, logo_position = lc.parse_chosen_layout(chosen)

    if template_id is None or template_id not in templates:
        template_id = next(iter(templates), None)
        if template_id is None:
            abort(502, "layout JSON has no templates")
        template = templates[template_id]
        logo_position = template.get("logo_position_default", "top_center")
    else:
        template = templates[template_id]
        if logo_position is None:
            logo_position = template.get("logo_position_default", "top_center")

    return {
        "template_id": template_id,
        "logo_position": logo_position,
        "logo_positions": logo_positions,
        "template": template,
    }


def _validate_box(box) -> str | None:
    if not isinstance(box, list) or len(box) != 4:
        return "box must be a list of 4 numbers [x0,y0,x1,y1]"
    try:
        floats = [float(v) for v in box]
    except (TypeError, ValueError):
        return "box values must be numeric"
    if not all(0.0 <= v <= 1.0 for v in floats):
        return "box values must be in [0, 1]"
    if not (floats[0] < floats[2] and floats[1] < floats[3]):
        return "box must satisfy x0<x1 and y0<y1"
    return None


def _validate_zone(key: str, zd) -> str | None:
    if not isinstance(zd, dict):
        return f"zones.{key} must be an object"
    if "box" in zd:
        err = _validate_box(zd["box"])
        if err:
            return f"zones.{key}.{err}"
    if "align" in zd:
        if zd["align"] not in LAYOUT_ALIGN_VALUES:
            return (f"zones.{key}.align must be one of "
                    f"{sorted(LAYOUT_ALIGN_VALUES)}")
    if "color" in zd:
        if (not isinstance(zd["color"], str)
                or not LAYOUT_HEX_RE.match(zd["color"])):
            return f"zones.{key}.color must be a #RRGGBB hex string"
    if "height_px" in zd:
        hpx = zd["height_px"]
        if (not isinstance(hpx, int) or isinstance(hpx, bool)
                or not (40 <= hpx <= 400)):
            return f"zones.{key}.height_px must be an int in 40..400"
    if "hidden" in zd:
        if not isinstance(zd["hidden"], bool):
            return f"zones.{key}.hidden must be a bool (true or false)"
    return None


def _validate_overrides(overrides, logo_positions_keys: list[str]) -> str | None:
    if not isinstance(overrides, dict):
        return "overrides must be an object"
    if "_version" in overrides and overrides["_version"] != 1:
        return "_version must be 1"
    if "logo_position" in overrides:
        lp = overrides["logo_position"]
        if (not isinstance(lp, str) or lp not in logo_positions_keys):
            return (f"logo_position must be one of "
                    f"{sorted(logo_positions_keys)}")
    if "collision_avoid" in overrides:
        if not isinstance(overrides["collision_avoid"], bool):
            return "collision_avoid must be a bool"
    zones = overrides.get("zones") or {}
    if not isinstance(zones, dict):
        return "zones must be an object"
    for key, zd in zones.items():
        if key not in LAYOUT_ALLOWED_ZONES:
            return (f"unknown zone {key!r}; allowed: "
                    f"{sorted(LAYOUT_ALLOWED_ZONES)}")
        err = _validate_zone(key, zd)
        if err:
            return err
    return None


def _atomic_write_json(path: Path, data: dict) -> None:
    """FIX-6: write via temp file + os.replace so a crash mid-write can't
    leave a half-written layout-overrides.json on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def _load_overrides_file(workdir: Path) -> dict | None:
    """Load <workdir>/layout-overrides.json. Malformed → None, never crash."""
    path = workdir / "layout-overrides.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _zone_defaults_from_template(template: dict) -> dict:
    """Pull box + align defaults out of the template for each allowed zone.

    Skips the body zone when the template says body:NO. Never includes the
    product zone (not draggable). Logo box comes from the template's resolved
    logo_position preset (not a fixed template default).
    """
    out = {}
    for key in ("headline", "subheadline", "body"):
        spec = template.get(key) or {}
        if key == "body" and not spec.get("supported", True):
            continue
        box = spec.get("box")
        if isinstance(box, list) and len(box) == 4:
            out[key] = {"box": list(box),
                        "align": spec.get("align", "left")}
    return out


def _logo_box_default(layout_data: dict, logo_position: str) -> list | None:
    """Resolve the relative [x0,y0,x1,y1] box for the current logo preset."""
    lp = (layout_data.get("logo_positions") or {}).get(logo_position)
    if isinstance(lp, list) and len(lp) == 4:
        return list(lp)
    return None


@bp.get("/api/poster/layout/<slug>")
def get_layout(slug):
    workdir = _workdir(slug)
    if not LAYOUT_JSON.is_file():
        abort(503, f"layout JSON missing at {LAYOUT_JSON}")
    try:
        layout_data = json.loads(LAYOUT_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        abort(503, f"could not parse layout JSON: {e}")

    resolved = _resolve_layout_defaults(workdir, layout_data)
    return jsonify({
        "slug": slug,
        "template_id": resolved["template_id"],
        "logo_position": resolved["logo_position"],
        "logo_positions": resolved["logo_positions"],
        "zone_defaults": _zone_defaults_from_template(resolved["template"]),
        "logo_box_default": _logo_box_default(layout_data, resolved["logo_position"]),
        "overrides": _load_overrides_file(workdir),
    })


@bp.post("/api/poster/layout")
def post_layout():
    b = request.get_json(force=True) or {}
    slug = b.get("slug") or ""
    workdir = _workdir(slug)
    overrides = b.get("overrides") or {}

    if not LAYOUT_JSON.is_file():
        abort(503, f"layout JSON missing at {LAYOUT_JSON}")
    try:
        layout_data = json.loads(LAYOUT_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        abort(503, f"could not parse layout JSON: {e}")
    logo_positions_keys = _layout_logo_positions(layout_data)

    err = _validate_overrides(overrides, logo_positions_keys)
    if err:
        abort(400, err)

    # Reset = empty zones object → delete the file (absence rule returns
    # everything to the template).
    zones = overrides.get("zones") or {}
    has_any_field = any(
        k in overrides for k in ("logo_position", "collision_avoid", "_version")
    ) or bool(zones)
    if not has_any_field:
        path = workdir / "layout-overrides.json"
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            abort(500, f"could not delete overrides: {e}")
        return jsonify({"ok": True, "reset": True})

    payload = dict(overrides)
    payload.setdefault("_version", 1)
    if "zones" not in payload:
        payload["zones"] = {}

    try:
        _atomic_write_json(workdir / "layout-overrides.json", payload)
    except OSError as e:
        abort(500, f"could not write overrides: {e}")
    return jsonify({"ok": True, "overrides": payload})


@bp.post("/api/poster/preview")
def preview():
    b = request.get_json(force=True) or {}
    slug = b.get("slug") or ""
    workdir = _workdir(slug)
    raw = (b.get("raw") or "").strip()
    if not RAW_RE.match(raw) or Path(raw).name != raw:
        abort(400, "pick a raw image to preview")
    aspect = b.get("aspect") or "ig_feed"
    if aspect not in ("ig_feed", "tiktok"):
        abort(400, "bad aspect")
    scrim = bool(b.get("scrim"))

    raw_path = workdir / raw
    if not raw_path.is_file() or raw_path.stat().st_size <= 0:
        abort(404, f"raw missing or empty: {raw}")
    plan_path = workdir / "plan.json"
    if not plan_path.is_file():
        abort(400, "run the planner first")

    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as e:
        abort(500, f"could not parse plan.json: {e}")

    overrides = _load_overrides_file(workdir)

    if not LAYOUT_JSON.is_file():
        abort(503, f"layout JSON missing at {LAYOUT_JSON}")
    try:
        layout_data = json.loads(LAYOUT_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        abort(503, f"could not parse layout JSON: {e}")
    resolved = _resolve_layout_defaults(workdir, layout_data)
    chosen_layout = (f"[CHOSEN LAYOUT]\n{resolved['template_id']} "
                     f"| logo_position: {resolved['logo_position']}")
    canvas_preset = "ig_feed" if aspect == "ig_feed" else "tiktok"

    lc = _layout_compositor_module()  # FIX-1 lazy import

    from PIL import Image

    bg = Image.open(raw_path).convert("RGB")
    out = lc.render_layout(
        bg_image=bg,
        chosen_layout=chosen_layout,
        copy_elements=plan.get("copy_elements") or "",
        layout_json_path=str(LAYOUT_JSON),
        canvas_preset=canvas_preset,
        brand_dir=str(BRAND_DIR),
        scrim=False,                 # editor draws its own outlines
        overrides=overrides,
    )

    # Downscale to ≤540px on the long edge for fast reload.
    W, H = out.size
    long_edge = max(W, H)
    if long_edge > 540:
        scale = 540 / long_edge
        out = out.resize((max(1, int(W * scale)), max(1, int(H * scale))),
                         Image.LANCZOS)

    import hashlib
    h = hashlib.sha1(
        f"{slug}|{raw}|{aspect}|{int(bool(scrim))}|"
        f"{json.dumps(overrides or {}, sort_keys=True, ensure_ascii=False)}"
        .encode("utf-8")
    ).hexdigest()[:12]
    dest = workdir / f"preview-{h}.png"
    out.save(dest, "PNG", optimize=True)

    return jsonify({
        "url": f"/media/output/generated-brand-poster/{slug}/{dest.name}",
        "hash": h,
    })
