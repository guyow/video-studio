#!/usr/bin/env python3
"""Poster Studio — gen + composite (MODE B / NEW_GENERATION).

Three stages:
  gen       — fal.ai Nano Banana edit paints N background-only images (raws).
  composite — pure-Pillow liitt compositor overlays headline/subtitle/body +
              wordmark on each raw → final-NN.jpg.
  all       — gen then composite every raw in one process.

    python poster_gen.py --stage all --workdir <out> --product-image <jpg> \\
        --plan <plan.json> --gen-model nano-banana-2 --aspect ig_feed \\
        --num 2 --seed 42 --resolution 1K --layout-json <brand/liitt_layout...> \\
        --brand-dir <comfyui/brand> --env-file <autoVSL>/.env

Runs under the cv venv (fal_client + httpx + Pillow present). The compositor
lives in comfyui/custom_nodes/LiittCompositor (read-only reuse).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
ENGINES_DIR = HERE.parent
REPO_ROOT = HERE.parents[3]                       # repo root (contains autoVSL, comfyui, video-studio)
COMPOSITOR_DIR = REPO_ROOT / "comfyui" / "custom_nodes" / "LiittCompositor"

sys.path.insert(0, str(COMPOSITOR_DIR))
sys.path.insert(0, str(ENGINES_DIR))

from layout_compositor import render_layout, parse_chosen_layout  # noqa: E402
from image_edit import MODELS, cost_per_image, load_env           # noqa: E402

ASPECT_MAP = {
    "ig_feed": {"fal": "4:5", "canvas": "ig_feed"},
    "tiktok": {"fal": "9:16", "canvas": "tiktok"},
}

RAW_RE = re.compile(r"^raw-(\d+)\.png$")


def log(m: str) -> None:
    print(m, flush=True)


def load_plan(plan_path: Path) -> dict:
    if not plan_path.is_file():
        raise SystemExit(f"plan.json not found: {plan_path}")
    return json.loads(plan_path.read_text(encoding="utf-8"))


def load_manifest(workdir: Path) -> dict:
    p = workdir / "manifest.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def write_manifest(workdir: Path, m: dict) -> Path:
    p = workdir / "manifest.json"
    p.write_text(json.dumps(m, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def load_overrides(workdir: Path, explicit: str = "") -> dict | None:
    """Return the parsed layout-overrides dict, or None if missing/malformed.

    Path resolution: explicit (when supplied) wins, else <workdir>/layout-overrides.json.
    Malformed JSON → log a WARNING and degrade to template (return None), never crash.
    """
    path = Path(explicit) if explicit else (workdir / "layout-overrides.json")
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log(f"WARNING: layout-overrides.json at {path} could not be parsed: {e} — "
            f"falling back to template")
        return None
    if not isinstance(data, dict):
        log(f"WARNING: layout-overrides.json at {path} is not an object — "
            f"falling back to template")
        return None
    return data


def stage_gen(args, plan: dict) -> list[str]:
    load_env(Path(args.env_file) if args.env_file else None)
    if not os.environ.get("FAL_KEY"):
        raise SystemExit("FAL_KEY not set — add it to autoVSL/.env")
    import fal_client
    import httpx

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    model = MODELS[args.gen_model]
    num = max(1, min(4, int(args.num)))

    product_image = Path(args.product_image)
    if not product_image.is_file():
        raise SystemExit(f"product image not found: {product_image}")

    image_urls = []
    if args.reference_image and Path(args.reference_image).is_file():
        # MODE A: the reference goes first — the plan's [IMAGE SCENE] speaks
        # of "Image 1 = reference composition, Image 2 = product".
        image_urls.append(fal_client.upload_file(str(args.reference_image)))
        log(f"including reference poster: {Path(args.reference_image).name}")
    image_urls.append(fal_client.upload_file(str(product_image)))
    if args.additional_object and Path(args.additional_object).is_file():
        image_urls.append(fal_client.upload_file(str(args.additional_object)))

    fal_aspect = ASPECT_MAP[args.aspect]["fal"]
    resolution = args.resolution if args.resolution in model["resolutions"] else None

    arguments = {
        "prompt": plan.get("prompt") or "",
        "image_urls": image_urls,
        "num_images": num,
        "output_format": "png",
        "aspect_ratio": fal_aspect,
    }
    if resolution:
        arguments["resolution"] = resolution
    if args.seed and int(args.seed) > 0:
        arguments["seed"] = int(args.seed)
    if args.gen_model == "nano-banana-2":
        arguments["safety_tolerance"] = "4"
        arguments["limit_generations"] = True
        arguments["enable_web_search"] = False

    est = round(cost_per_image(args.gen_model, resolution or "1K") * num, 4)
    log(f"generating {num} image(s) on {model['edit']} @ {fal_aspect} ({resolution or 'native'}) ~${est:.3f} ...")

    res = fal_client.subscribe(model["edit"], arguments=arguments, with_logs=False)
    images = res.get("images") or []
    if not images:
        raise SystemExit(f"fal.ai returned no images: {str(res)[:400]}")

    # ACCUMULATE: figure out the starting index from existing raws so this
    # generate's images don't overwrite previous ones. User can compare
    # multiple aspects / seeds side-by-side in the gallery.
    existing_nums = []
    for f in workdir.glob("raw-*.png"):
        m = RAW_RE.match(f.name)
        if m and f.stat().st_size > 0:
            existing_nums.append(int(m.group(1)))
    offset = max(existing_nums) if existing_nums else 0

    raws = []
    with httpx.Client(follow_redirects=True, timeout=300) as c:
        for i, img in enumerate(images, 1):
            url = img.get("url") if isinstance(img, dict) else img
            if not url:
                continue
            dest = workdir / f"raw-{offset + i:02d}.png"
            r = c.get(url)
            r.raise_for_status()
            dest.write_bytes(r.content)
            raws.append(dest.name)
            log(f"  wrote {dest.name} ({len(r.content) // 1024} KB)")

    if not raws:
        raise SystemExit("fal returned images but none had a usable url")
    return raws


def _raw_index(name: str) -> int | None:
    m = RAW_RE.match(name)
    return int(m.group(1)) if m else None


def resolve_chosen_layout(plan: dict, layout_json: Path) -> str:
    """Return a valid [CHOSEN LAYOUT] string; fall back to the first template."""
    chosen = plan.get("chosen_layout") or ""
    tid, lpos = parse_chosen_layout(chosen)
    if tid is not None and lpos is not None:
        return f"[CHOSEN LAYOUT]\n{tid} | logo_position: {lpos}"
    data = json.loads(layout_json.read_text(encoding="utf-8"))
    templates = data.get("templates") or {}
    if not templates:
        raise SystemExit("layout JSON has no templates — cannot synthesize a chosen layout")
    first_name = next(iter(templates))
    first = templates[first_name]
    lpos = first.get("logo_position_default", "top_center")
    log(f"WARNING: no [CHOSEN LAYOUT] in plan — falling back to {first_name} / {lpos}")
    return f"[CHOSEN LAYOUT]\n{first_name} | logo_position: {lpos}"


def stage_composite(args, plan: dict, raws: list[str]) -> list[str]:
    from PIL import Image

    workdir = Path(args.workdir)
    canvas = ASPECT_MAP[args.aspect]["canvas"]
    chosen_layout = resolve_chosen_layout(plan, Path(args.layout_json))
    overrides = load_overrides(workdir, args.overrides)
    if overrides:
        log(f"  applying layout-overrides ({len(overrides.get('zones') or {})} zone(s))")

    if args.composite_raw:
        raw_names = [args.composite_raw]
    else:
        raw_names = sorted(
            p.name for p in workdir.glob("raw-*.png") if p.stat().st_size > 0
        )
        if raws:
            raw_names = raws

    finals = []
    for name in raw_names:
        raw_path = workdir / name
        if not raw_path.is_file():
            log(f"  skip missing raw: {name}")
            continue
        idx = _raw_index(name) or (len(finals) + 1)
        bg = Image.open(raw_path).convert("RGB")
        out = render_layout(
            bg_image=bg,
            chosen_layout=chosen_layout,
            copy_elements=plan.get("copy_elements") or "",
            layout_json_path=str(args.layout_json),
            canvas_preset=canvas,
            brand_dir=str(args.brand_dir),
            scrim=bool(args.scrim),
            overrides=overrides,
        )
        dest = workdir / f"final-{idx:02d}.jpg"
        out.save(dest, "JPEG", quality=95, optimize=True)
        finals.append(dest.name)
        log(f"  composited {dest.name} ({out.size[0]}x{out.size[1]})")
    return finals


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", required=True, choices=["gen", "composite", "all"])
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--product-image", default="")
    ap.add_argument("--additional-object", default="")
    ap.add_argument("--reference-image", default="")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--gen-model", default="nano-banana",
                    choices=list(MODELS.keys()))
    ap.add_argument("--aspect", default="ig_feed", choices=["ig_feed", "tiktok"])
    ap.add_argument("--num", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--resolution", default="1K")
    ap.add_argument("--scrim", action="store_true")
    ap.add_argument("--composite-raw", default="")
    ap.add_argument("--layout-json", required=True)
    ap.add_argument("--brand-dir", required=True)
    ap.add_argument("--overrides", default="")
    ap.add_argument("--env-file", default="")
    args = ap.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    plan = load_plan(Path(args.plan))

    manifest = load_manifest(workdir)
    manifest.setdefault("created", time.time())
    manifest["provider"] = plan.get("provider")
    manifest["gen_model"] = args.gen_model
    manifest["seed"] = int(args.seed or 0)
    manifest["num"] = max(1, min(4, int(args.num)))
    manifest["aspect"] = args.aspect
    manifest["resolution"] = args.resolution
    manifest["scrim"] = bool(args.scrim)
    manifest["plan"] = str(Path(args.plan).resolve())

    model = MODELS[args.gen_model]
    resolution = args.resolution if args.resolution in model["resolutions"] else None
    gen_cost = round(cost_per_image(args.gen_model, resolution or "1K") * manifest["num"], 4)
    manifest["gen_cost"] = gen_cost
    manifest["total_cost"] = gen_cost

    raws = manifest.get("raws") or []
    finals = manifest.get("finals") or []

    if args.stage in ("gen", "all"):
        raws = stage_gen(args, plan)
        # ACCUMULATE: re-glob after generate so manifest["raws"] is the
        # union of pre-existing + new (none overwritten anymore).
        manifest["raws"] = sorted(
            p.name for p in workdir.glob("raw-*.png")
            if p.stat().st_size > 0
        )

    if args.stage in ("composite", "all"):
        finals = stage_composite(args, plan, raws if args.stage == "all" else [])

    manifest["raws"] = sorted(
        p.name for p in workdir.glob("raw-*.png") if p.stat().st_size > 0
    )
    manifest["finals"] = finals
    manifest["total_cost"] = manifest.get("gen_cost", gen_cost)
    manifest_path = write_manifest(workdir, manifest)

    log(f"gen_cost ~${manifest['gen_cost']:.3f} · raws={len(raws)} · finals={len(finals)}")
    print(f"RESULT: {manifest_path.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
