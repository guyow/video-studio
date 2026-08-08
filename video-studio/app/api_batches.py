#!/usr/bin/env python3
"""Ad Batches — the three-layer batch system (Template / Copy / Production).

A batch is a planned set of ad assets. Layer 1 picks or defines a template
(identity, scene, format — the reusable shell). Layer 2 writes the copy and
must pass compliance + a named approval. Layer 3 (production) is locked until
that approval exists — enforced here, not just in the UI.

A Blueprint for the same reason api_sequence.py is one: server.py is ~6,200
lines and a new blueprint can't disturb any existing route.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from flask import Blueprint, abort, jsonify, request

bp = Blueprint("batches", __name__)

# injected by init() so this module never imports server.py (which imports it)
ROOT: Path = Path(".")
CLAUDE: str = ""

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


def init(root: Path, claude_exe: str = ""):
    global ROOT, CLAUDE
    ROOT = Path(root)
    CLAUDE = claude_exe or ""


# ---------------------------------------------------------------- storage

def batches_dir() -> Path:
    return ROOT / "output" / "ad-batches"


def templates_file() -> Path:
    return ROOT / "banks" / "ad-templates.json"


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_templates() -> dict:
    try:
        return json.loads(templates_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def batch_file(slug: str) -> Path:
    if not SLUG_RE.match(slug or ""):
        abort(400, "bad batch slug")
    return batches_dir() / slug / "batch.json"


def load_batch(slug: str) -> dict:
    p = batch_file(slug)
    if not p.is_file():
        abort(404, f"no batch {slug!r}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        abort(500, f"batch file for {slug!r} is corrupt")


def save_batch(b: dict) -> None:
    b["updated"] = time.time()
    _atomic_write(batch_file(b["slug"]), b)


# ---------------------------------------------------------------- compliance scanner
# Mirrors ~/.claude/skills/meta-safe-mushroom-language — keep the two in sync.

BLOCK_WORDS = [
    (r"psychedelic", "restricted vocabulary"),
    (r"psilocybin", "restricted vocabulary"),
    (r"magic\s+mushrooms?", "restricted vocabulary"),
    (r"\bshrooms?\b", "restricted vocabulary"),   # \b so plain "mushrooms" stays low-risk
    (r"micro-?dos\w*", 'restricted — use "micro-flame" instead'),
    (r"\btripp?(y|ing)?\b", "restricted vocabulary"),
    (r"\b(get|getting|got|so)\s+high\b", "promises intoxication"),
    (r"\b(baked|faded|blazed|stoned)\b", "drug slang"),
]
WARN_WORDS = [
    (r"\bhigh\b", 'check context — "high" is fine descriptively, never as the effect'),
    (r"\b(cure[sd]?|treat(s|ing|ment)?|heal[sd]?)\b",
     "possible disease-treatment claim — personal-experience framing only"),
    (r"\b(depression|ptsd|anxiety|adhd)\b",
     "naming a condition — never promise to treat it"),
    (r"\b(wellness|journey|holistic)\b", "offer-doc banned word"),
]
URL_WORDS = [
    (r"(https?://|www\.|\.com\b|\.co\b|dot\s+com)", 'spoken/written URL — use "the link is down below"'),
]


def scan_copy(copy: dict) -> dict:
    """Local, free, instant word scan over hook + script + CTA."""
    fields = {"hook": copy.get("hook") or "", "script": copy.get("script") or "",
              "cta": copy.get("cta") or ""}
    blocks, warns = [], []
    for field, text in fields.items():
        for pat, why in BLOCK_WORDS:
            for m in re.finditer(pat, text, re.IGNORECASE):
                blocks.append({"field": field, "match": m.group(0), "why": why})
        for pat, why in WARN_WORDS:
            for m in re.finditer(pat, text, re.IGNORECASE):
                warns.append({"field": field, "match": m.group(0), "why": why})
    # the URL rule only applies where words get spoken/printed on the ad itself
    for field in ("script", "cta"):
        for pat, why in URL_WORDS:
            for m in re.finditer(pat, fields[field], re.IGNORECASE):
                blocks.append({"field": field, "match": m.group(0), "why": why})
    return {
        "ran": time.time(), "blocks": blocks, "warns": warns,
        "clean": not blocks,
        # checklist items the scanner can decide by itself
        "auto_checks": {
            "microflame": not any("micro" in b["match"].lower() for b in blocks),
            "no_disease_claims": not any("condition" in w["why"] or "disease" in w["why"]
                                         for w in warns),
            "no_spoken_url": not any("URL" in b["why"] for b in blocks),
        },
    }


AI_CHECK_PROMPT = """You are the Meta-ads compliance reviewer for liitt Fairy Flame \
(a legal mushroom-extract gummy). Review this ad copy for Meta/Facebook ad review risk.

RULES (hard):
- Never these words: psychedelic, psilocybin, magic mushroom(s), shrooms, microdose/microdosing, \
trip/trippy/tripping, high/getting high, drug slang (baked, faded, blazed, stoned).
- Plain "mushroom" in a functional/culinary sense is fine. "Magic mushroom" never.
- No disease-treatment claims (treat/cure depression, PTSD, anxiety). \
Personal-experience framing is fine ("for the days you feel flat").
- No promising a high or intoxication — sell the state-shift, not a buzz.
- Never speak/write a URL in the script or CTA — point at "the link below" instead.
- Also banned: wellness, journey, holistic.

HOOK:
{hook}

SCRIPT:
{script}

CTA:
{cta}

Respond ONLY with JSON:
{{"pass": true/false, "risk": "low"/"medium"/"high",
  "issues": [{{"quote": "exact offending text", "why": "one line", "fix": "safe replacement"}}],
  "notes": "one-line overall read"}}"""


def _claude_json(prompt: str, timeout: int = 240) -> dict:
    if not CLAUDE:
        abort(503, "the claude CLI was not found on this machine")
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)       # allow a nested headless run
    env["PYTHONUTF8"] = "1"
    try:
        r = subprocess.run(
            [CLAUDE, "-p", "--model", "sonnet",
             "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch,Task"],
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
        abort(502, f"claude returned malformed JSON ({exc}): {out[s:s + 300]}")


# ---------------------------------------------------------------- batch shape

TEMPLATE_FIELDS = ["format", "aspect_ratio", "duration", "identity", "scene",
                   "wardrobe", "product_shown", "logo_placement", "reference_images"]
BRAND_LOCK = ["palette", "logo_composited", "product_shape", "identity_owned"]
PROD_STEPS = ["generate", "face_swap", "logo_composite", "export", "filed"]
REPRO_FIELDS = ["workflow_file", "model", "loras", "seed", "sampler"]


def new_batch(shortname: str, body: dict) -> dict:
    date = time.strftime("%Y-%m-%d")
    slug = f"batch-{date}-{shortname}"
    now = time.time()
    return {
        "id": f"BATCH-{date}-{shortname}", "slug": slug,
        "created": now, "updated": now,
        "meta": {
            "requested_by": body.get("requested_by") or "",
            "goal": body.get("goal") or "",
            "quantity": body.get("quantity") or "",
            "deadline": body.get("deadline") or "",
        },
        "template": {"ref": None, "is_new": False,
                     **{k: "" for k in TEMPLATE_FIELDS},
                     "brand_lock": {k: False for k in BRAND_LOCK}},
        "copy": {"hook_source": "", "hook": "", "script": "", "cta": "",
                 "manual_checks": {"age_gate": False, "stim_warnings": False,
                                   "claims_true": False},
                 "scan": None, "ai_check": None,
                 "approved_by": "", "approved_date": ""},
        "production": {"assigned_to": "", "started": "",
                       "steps": {k: {"status": "todo", "notes": ""} for k in PROD_STEPS},
                       "repro": {k: "" for k in REPRO_FIELDS},
                       "jobs": [], "outputs": "", "version": ""},
        "review": {"verdict": "", "reviewed_by": "", "date": "",
                   "failed_layer": "", "notes": "", "graduate": False},
    }


def batch_status(b: dict) -> str:
    r = b.get("review", {})
    if r.get("verdict") == "accepted":
        return "accepted"
    if r.get("verdict") == "rejected":
        return "rejected"
    if b.get("copy", {}).get("approved_by"):
        steps = b.get("production", {}).get("steps", {})
        if all(s.get("status") == "done" for s in steps.values()):
            return "review"
        if any(s.get("status") != "todo" for s in steps.values()):
            return "production"
        return "approved"
    c = b.get("copy", {})
    if c.get("hook") or c.get("script"):
        return "copy"
    if b.get("template", {}).get("ref") or b.get("template", {}).get("is_new"):
        return "template"
    return "draft"


def summary(b: dict) -> dict:
    return {"id": b["id"], "slug": b["slug"], "status": batch_status(b),
            "goal": b["meta"].get("goal", ""), "quantity": b["meta"].get("quantity", ""),
            "deadline": b["meta"].get("deadline", ""),
            "template": b["template"].get("ref") or ("NEW" if b["template"].get("is_new") else ""),
            "hook": (b["copy"].get("hook") or "")[:80],
            "created": b["created"], "updated": b["updated"]}


# ---------------------------------------------------------------- routes

@bp.get("/api/batches")
def list_batches():
    out = []
    d = batches_dir()
    if d.is_dir():
        for sub in d.iterdir():
            p = sub / "batch.json"
            if p.is_file():
                try:
                    out.append(summary(json.loads(p.read_text(encoding="utf-8"))))
                except Exception:
                    pass
    out.sort(key=lambda x: x["updated"], reverse=True)
    tpls = load_templates()
    return jsonify({"batches": out,
                    "templates": sorted(tpls.values(), key=lambda t: t.get("label", ""))})


@bp.post("/api/batches")
def create_batch():
    body = request.get_json(force=True) or {}
    shortname = re.sub(r"[^a-z0-9]+", "-", (body.get("shortname") or "").lower()).strip("-")[:40]
    if not shortname:
        abort(400, "give the batch a short name")
    b = new_batch(shortname, body)
    if batch_file(b["slug"]).is_file():
        abort(409, f"batch {b['id']} already exists today — pick another name")
    tpl_ref = (body.get("template") or "").strip()
    if tpl_ref:
        tpl = load_templates().get(tpl_ref)
        if not tpl:
            abort(404, f"unknown template {tpl_ref!r}")
        b["template"].update({k: tpl.get(k, "") for k in TEMPLATE_FIELDS})
        b["template"]["ref"] = tpl_ref
    save_batch(b)
    return jsonify({"batch": b, "status": batch_status(b)})


@bp.get("/api/batches/<slug>")
def get_batch(slug):
    b = load_batch(slug)
    return jsonify({"batch": b, "status": batch_status(b)})


@bp.post("/api/batches/<slug>")
def update_batch(slug):
    """Partial update. Layer 3 stays locked until copy is approved — enforced here."""
    b = load_batch(slug)
    body = request.get_json(force=True) or {}
    if "production" in body and not b["copy"].get("approved_by"):
        abort(403, "production is locked until the copy layer is approved")
    for section in ("meta", "template", "copy", "production", "review"):
        if section not in body:
            continue
        patch = body[section]
        if not isinstance(patch, dict):
            abort(400, f"{section} must be an object")
        # editing copy after approval voids the approval (the approved text changed)
        if section == "copy" and b["copy"].get("approved_by"):
            texty = {"hook", "script", "cta"}
            if any(k in patch and patch[k] != b["copy"].get(k) for k in texty):
                b["copy"]["approved_by"] = ""
                b["copy"]["approved_date"] = ""
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(b[section].get(k), dict):
                b[section][k].update(v)
            else:
                b[section][k] = v
    save_batch(b)
    return jsonify({"batch": b, "status": batch_status(b)})


@bp.post("/api/batches/<slug>/delete")
def delete_batch(slug):
    p = batch_file(slug)
    if not p.is_file():
        abort(404, f"no batch {slug!r}")
    for f in p.parent.iterdir():
        f.unlink()
    p.parent.rmdir()
    return jsonify({"deleted": slug})


@bp.post("/api/batches/<slug>/scan")
def scan_batch(slug):
    b = load_batch(slug)
    result = scan_copy(b["copy"])
    b["copy"]["scan"] = result
    save_batch(b)
    return jsonify({"scan": result, "status": batch_status(b)})


@bp.post("/api/batches/<slug>/ai-check")
def ai_check_batch(slug):
    b = load_batch(slug)
    c = b["copy"]
    if not (c.get("hook") or c.get("script")):
        abort(400, "write the hook/script first")
    verdict = _claude_json(AI_CHECK_PROMPT.format(
        hook=c.get("hook") or "(none)", script=c.get("script") or "(none)",
        cta=c.get("cta") or "(none)"))
    verdict["ran"] = time.time()
    b["copy"]["ai_check"] = verdict
    save_batch(b)
    return jsonify({"ai_check": verdict, "status": batch_status(b)})


@bp.post("/api/batches/<slug>/approve")
def approve_batch(slug):
    b = load_batch(slug)
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        abort(400, "approval needs a name — that's the point of the box")
    scan = b["copy"].get("scan")
    if scan is None or not scan.get("clean"):
        if not body.get("force"):
            abort(409, "copy hasn't passed a clean compliance scan — scan first "
                       "(or approve with force to override)")
    b["copy"]["approved_by"] = name
    b["copy"]["approved_date"] = time.strftime("%Y-%m-%d")
    if not b["production"].get("started"):
        b["production"]["started"] = ""
    save_batch(b)
    return jsonify({"batch": b, "status": batch_status(b)})


@bp.post("/api/batches/<slug>/review")
def review_batch(slug):
    b = load_batch(slug)
    body = request.get_json(force=True) or {}
    verdict = body.get("verdict")
    if verdict not in ("accepted", "rejected"):
        abort(400, "verdict must be accepted or rejected")
    if verdict == "rejected" and body.get("failed_layer") not in ("template", "copy", "production"):
        abort(400, "a rejection must name the layer that failed — that's the point of the form")
    b["review"] = {"verdict": verdict,
                   "reviewed_by": (body.get("reviewed_by") or "").strip(),
                   "date": time.strftime("%Y-%m-%d"),
                   "failed_layer": body.get("failed_layer") or "",
                   "notes": body.get("notes") or "",
                   "graduate": bool(body.get("graduate"))}
    graduated = None
    if verdict == "accepted" and body.get("graduate") and b["template"].get("is_new"):
        tpls = load_templates()
        tpl_id = (body.get("template_id") or b["id"]).upper()
        tpls[tpl_id] = {"id": tpl_id,
                        "label": body.get("template_label") or tpl_id,
                        **{k: b["template"].get(k, "") for k in TEMPLATE_FIELDS},
                        "graduated_from": b["id"],
                        "created": time.strftime("%Y-%m-%d")}
        _atomic_write(templates_file(), tpls)
        b["template"]["ref"] = tpl_id
        b["template"]["is_new"] = False
        graduated = tpl_id
    save_batch(b)
    return jsonify({"batch": b, "status": batch_status(b), "graduated": graduated})


# ---------------------------------------------------------------- brief → template

BRIEF_PROMPT = """You are the creative-ops lead for liitt / Fairy Flame (a legal \
mushroom-extract gummy; brand palette dark purple / black / gold; product is a \
flame-shaped gummy; the logo is ALWAYS composited in post, never AI-generated; \
spokespeople are character LoRAs the brand owns).

Below is a client/creative BRIEF. Extract a reusable ad TEMPLATE definition from it. \
Where the brief doesn't say, infer the most sensible value from context — never leave \
a field empty if a reasonable inference exists. Keep values short (one line each).

BRIEF:
{brief}

Respond ONLY with JSON:
{{"label": "short human name for this template",
  "id_suggestion": "UPPERCASE-SLUG-01",
  "format": "static image / short-form video / VSL cut",
  "aspect_ratio": "9:16 / 1:1 / 4:5",
  "duration": "e.g. 30s, or empty for statics",
  "identity": "which spokesperson/character the brief implies",
  "scene": "scene / setting",
  "wardrobe": "wardrobe / props",
  "product_shown": "gummy / pouch / none",
  "logo_placement": "where the logo gets composited",
  "reference_images": "any references the brief mentions",
  "goal_suggestion": "what a batch built on this template should achieve",
  "copy_angle": "one line on the copy angle the brief implies",
  "notes": "anything important in the brief that does not fit the fields above"}}"""

BRIEF_EXTS = {".pdf", ".txt", ".md"}


def _brief_text_from_upload() -> str:
    """Text of the uploaded brief — multipart file (pdf/txt/md) or JSON {text}."""
    f = request.files.get("file")
    if f and f.filename:
        suffix = Path(f.filename).suffix.lower()
        if suffix not in BRIEF_EXTS:
            abort(400, f"unsupported brief type {suffix!r} — send a PDF, txt, or md")
        briefs = ROOT / "uploads" / "briefs"
        briefs.mkdir(parents=True, exist_ok=True)
        from werkzeug.utils import secure_filename
        dest = briefs / (time.strftime("%Y%m%d-%H%M%S-") + secure_filename(f.filename))
        f.save(dest)
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                abort(503, "pypdf is not installed in the server venv")
            try:
                reader = PdfReader(str(dest))
                text = "\n".join((page.extract_text() or "") for page in reader.pages[:25])
            except Exception as exc:
                abort(400, f"could not read that PDF: {exc}")
            if not text.strip():
                abort(400, "that PDF has no extractable text (scanned image?) — "
                           "paste the brief text instead")
            return text
        return dest.read_text(encoding="utf-8", errors="replace")
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        abort(400, "send a brief file or paste its text")
    return text


@bp.post("/api/batches/brief-analyze")
def brief_analyze():
    """Brief (PDF/text) → Claude → proposed Layer-1 template, ready to save/edit."""
    text = _brief_text_from_upload()[:24000]
    proposal = _claude_json(BRIEF_PROMPT.format(brief=text))
    tpl = {k: str(proposal.get(k) or "") for k in TEMPLATE_FIELDS}
    return jsonify({"template": tpl,
                    "label": str(proposal.get("label") or ""),
                    "id_suggestion": str(proposal.get("id_suggestion") or ""),
                    "goal_suggestion": str(proposal.get("goal_suggestion") or ""),
                    "copy_angle": str(proposal.get("copy_angle") or ""),
                    "notes": str(proposal.get("notes") or "")})


@bp.post("/api/batches/templates")
def save_template():
    body = request.get_json(force=True) or {}
    tpl_id = re.sub(r"[^A-Z0-9-]+", "-", (body.get("id") or "").upper()).strip("-")
    if not tpl_id:
        abort(400, "template needs an id (e.g. PRINCE-GARDEN-01)")
    tpls = load_templates()
    tpls[tpl_id] = {"id": tpl_id, "label": body.get("label") or tpl_id,
                    **{k: body.get(k, "") for k in TEMPLATE_FIELDS},
                    "created": tpls.get(tpl_id, {}).get("created") or time.strftime("%Y-%m-%d")}
    _atomic_write(templates_file(), tpls)
    return jsonify({"template": tpls[tpl_id]})


@bp.post("/api/batches/templates/<tpl_id>/delete")
def delete_template(tpl_id):
    tpls = load_templates()
    if tpl_id not in tpls:
        abort(404, f"no template {tpl_id!r}")
    del tpls[tpl_id]
    _atomic_write(templates_file(), tpls)
    return jsonify({"deleted": tpl_id})


# ---------------------------------------------------------------- markdown export

def _cb(v):  # checkbox
    return "[x]" if v else "[ ]"


def export_md(b: dict) -> str:
    t, c, p, r = b["template"], b["copy"], b["production"], b["review"]
    bl = t.get("brand_lock", {})
    mc = c.get("manual_checks", {})
    ac = (c.get("scan") or {}).get("auto_checks", {})
    steps = p.get("steps", {})
    step_label = {"generate": "Generate / render", "face_swap": "Face replacement",
                  "logo_composite": "Logo composite", "export": "Export at required specs",
                  "filed": "Filed in asset library"}
    lines = [
        f"# {b['id']}",
        "",
        f"**Batch ID:** {b['id']}",
        f"**Date:** {time.strftime('%Y-%m-%d', time.localtime(b['created']))}",
        f"**Requested by:** {b['meta'].get('requested_by', '')}",
        f"**Goal of this batch:** {b['meta'].get('goal', '')}",
        f"**Quantity:** {b['meta'].get('quantity', '')}",
        f"**Deadline:** {b['meta'].get('deadline', '')}",
        "", "---", "", "## LAYER 1 — TEMPLATE", "",
        f"**Template used:** {t.get('ref') or ('NEW' if t.get('is_new') else '')}",
        "",
        "| Field | Value |", "|---|---|",
        f"| Format | {t.get('format', '')} |",
        f"| Aspect ratio | {t.get('aspect_ratio', '')} |",
        f"| Duration | {t.get('duration', '')} |",
        f"| Spokesperson identity | {t.get('identity', '')} |",
        f"| Scene / setting | {t.get('scene', '')} |",
        f"| Wardrobe / props | {t.get('wardrobe', '')} |",
        f"| Product shown | {t.get('product_shown', '')} |",
        f"| Logo placement | {t.get('logo_placement', '')} |",
        f"| Reference images | {t.get('reference_images', '')} |",
        "", "**Brand lock check:**",
        f"- {_cb(bl.get('palette'))} Palette matches (dark purple / black / gold)",
        f"- {_cb(bl.get('logo_composited'))} Logo is composited, not generated",
        f"- {_cb(bl.get('product_shape'))} Product shape correct (flame-shaped gummy)",
        f"- {_cb(bl.get('identity_owned'))} Spokesperson is an identity we own",
        "", "---", "", "## LAYER 2 — COPY", "",
        f"**Hook source:** {c.get('hook_source', '')}",
        "", f"**Hook:** {c.get('hook', '')}",
        "", f"**Script / body:**", "", c.get("script", ""),
        "", f"**CTA:** {c.get('cta', '')}",
        "", "**Compliance check:**",
        f"- {_cb(ac.get('microflame'))} \"micro-flame\" used, not \"microdosing\"",
        f"- {_cb(ac.get('no_disease_claims'))} No disease or treatment claims",
        f"- {_cb(mc.get('age_gate'))} Age-gate / 18+ handling correct for destination",
        f"- {_cb(mc.get('stim_warnings'))} Kava and caffeine warnings respected where required",
        f"- {_cb(mc.get('claims_true'))} Guarantee and lab-report claims match what is actually true",
        f"- {_cb(ac.get('no_spoken_url'))} No spoken/written URL in script or CTA",
        "",
        f"**Approved by:** {c.get('approved_by', '')} **Date:** {c.get('approved_date', '')}",
        "", "---", "", "## LAYER 3 — PRODUCTION", "",
        f"**Assigned to:** {p.get('assigned_to', '')}",
        f"**Started:** {p.get('started', '')}",
        "", "| Step | Status | Notes |", "|---|---|---|",
    ]
    for k in PROD_STEPS:
        s = steps.get(k, {})
        lines.append(f"| {step_label[k]} | {s.get('status', '')} | {s.get('notes', '')} |")
    rp = p.get("repro", {})
    lines += [
        "", "**Reproducibility record**", "",
        "| Field | Value |", "|---|---|",
        f"| Workflow file (git path / commit) | {rp.get('workflow_file', '')} |",
        f"| Model + version | {rp.get('model', '')} |",
        f"| LoRA(s) used + strength | {rp.get('loras', '')} |",
        f"| Seed | {rp.get('seed', '')} |",
        f"| Sampler settings | {rp.get('sampler', '')} |",
    ]
    if p.get("jobs"):
        lines += ["", "**Linked jobs:**"] + [
            f"- `{j.get('id')}` {j.get('label', '')} — {j.get('status', '')}" for j in p["jobs"]]
    lines += [
        "", f"**Output files:** {p.get('outputs', '')}",
        f"**Version:** {p.get('version', '')}",
        "", "---", "", "## REVIEW", "",
        f"**Accepted / rejected:** {r.get('verdict', '')}",
        f"**Reviewed by:** {r.get('reviewed_by', '')} **Date:** {r.get('date', '')}",
        f"**If rejected — which layer failed?** {r.get('failed_layer', '')}",
        "", f"**Notes for next batch:** {r.get('notes', '')}",
        "",
        f"**Does the template graduate into the library?** "
        f"{'☑ Yes' if r.get('graduate') else '☐ No'}",
        "",
    ]
    return "\n".join(lines)


@bp.post("/api/batches/<slug>/export")
def export_batch(slug):
    b = load_batch(slug)
    md = export_md(b)
    out = batch_file(slug).parent / f"{b['id']}.md"
    out.write_text(md, encoding="utf-8")
    return jsonify({"path": str(out), "markdown": md})
