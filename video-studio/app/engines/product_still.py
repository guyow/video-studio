#!/usr/bin/env python3
"""Put YOUR REAL product into the shot stills, before any video is generated.

Text-to-video invents a product from words, so the thing on screen is never the
thing you sell. This engine fixes that upstream: for every shot the storyboard
flagged as a product beat it paints ONE still on fal.ai (Nano Banana edit),
feeding it your actual product photo as a reference and telling the model to
keep the object pixel-faithful — same shape, label, colour, proportions.

The still is written back into the recipe as `shots[i]["still"]`, and
t2v_fal.py then runs those shots through the model's IMAGE-to-video endpoint
instead of text-to-video. Everything downstream (assemble, tags, burn) is
unchanged.

  python product_still.py --recipe output/broll/<batch>/recipe.json \
      --product product.jpg [--product back.jpg] [--inspiration mood.jpg] \
      --product-name "Fairy Flame — magenta flame-shaped gummies in an amber jar" \
      --aspect 9:16 --model nano-banana --env-file <autoVSL>/.env

Runs under the cv venv (fal_client + httpx). stdlib otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                        # noqa: BLE001
        pass

# mirrors image_edit.py MODELS — kept small on purpose: a shot still only ever
# needs the cheap/fast tier, the video model re-renders it anyway.
MODELS = {
    "nano-banana":     {"endpoint": "fal-ai/nano-banana/edit",     "cost": 0.039, "res": None},
    "nano-banana-2":   {"endpoint": "fal-ai/nano-banana-2/edit",   "cost": 0.08,  "res": "1K"},
    "nano-banana-pro": {"endpoint": "fal-ai/nano-banana-pro/edit", "cost": 0.15,  "res": "1K"},
}

# The whole point of this engine. Nano Banana will happily "improve" a product
# unless it is told, flatly, that the object is not its to redesign.
LOCK = ("The product in reference image {n} is a REAL physical product. Reproduce it EXACTLY as "
        "photographed — identical shape, proportions, colour, material, label artwork, lettering "
        "and packaging. Do not redesign it, do not restyle the label, do not invent new text on "
        "it, do not change its size or colour, do not add a different product.")

PLACEMENT = ("It sits in the scene naturally, at a believable real-world size, lit by the same "
             "light as everything around it, with a matching shadow. She is using or holding it "
             "casually mid-task — never presenting it to camera, never a commercial product shot, "
             "never floating or centred like packshot.")

INSPO = ("Reference image(s) {n} are MOOD only: match their colour feel and framing energy. Never "
         "copy their content, their subject, their text or their layout.")

REALISM = ("Photorealistic still frame from a handheld iPhone video: natural window light, slight "
           "motion blur, amateur off-centre framing, visible skin texture, mild sensor grain, "
           "cluttered lived-in home. Not cinematic, no studio lighting, no bokeh, no colour grade.")

NO_TEXT = ("No added text, no captions, no watermarks, no logos anywhere except the product's own "
           "label. No borders, no collage, no split screen.")


def log(m: str) -> None:
    print(m, flush=True)


def die(m: str) -> None:
    print(f"ERROR: {m}", flush=True)
    sys.exit(1)


def load_env(path: Path | None) -> None:
    if path and path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def scene_of(shot: dict) -> str:
    """The bare scene action — the UGC character/realism wrappers are re-added here."""
    p = (shot.get("prompt") or "").strip()
    p = p.split("no makeup look. ")[-1]
    p = p.split(". Shot on iPhone")[0]
    return p.strip(" .")


def wants_product(shot: dict) -> bool:
    if shot.get("product_in_shot") is not None:
        return bool(shot.get("product_in_shot"))
    return (shot.get("product_moment") or "") in ("product_hero", "during")


def compose(shot: dict, n_prod: int, n_inspo: int, product_name: str) -> str:
    """One instruction: the real object, in this shot's scene, shot like UGC."""
    prod_ref = "1" if n_prod == 1 else f"1-{n_prod}"
    parts = [
        f"{REALISM} Vertical smartphone frame.",
        f"Scene: {scene_of(shot)}.",
        LOCK.format(n=prod_ref),
    ]
    if product_name:
        parts.append(f"The product is: {product_name}.")
    parts.append(PLACEMENT)
    if n_inspo:
        lo = n_prod + 1
        hi = n_prod + n_inspo
        parts.append(INSPO.format(n=str(lo) if lo == hi else f"{lo}-{hi}"))
    parts.append(NO_TEXT)
    return " ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--recipe", required=True, help="output/broll/<batch>/recipe.json")
    ap.add_argument("--product", action="append", default=[],
                    help="photo of the real product (repeatable, first one leads)")
    ap.add_argument("--inspiration", action="append", default=[],
                    help="mood/look reference, never copied (repeatable)")
    ap.add_argument("--product-name", dest="product_name", default="",
                    help="what it is, in words — helps the model name it correctly")
    ap.add_argument("--aspect", default="9:16")
    ap.add_argument("--model", default="nano-banana", choices=sorted(MODELS))
    ap.add_argument("--shots", default="",
                    help="comma shot ids to force (default: the ones flagged as product beats)")
    ap.add_argument("--all-shots", action="store_true",
                    help="put the product in EVERY shot (pricier, usually unnecessary)")
    ap.add_argument("--max", type=int, default=6, help="never paint more than N stills")
    ap.add_argument("--env-file", dest="env_file")
    ap.add_argument("--estimate-only", action="store_true")
    a = ap.parse_args()

    recipe_path = Path(a.recipe).resolve()
    if not recipe_path.is_file():
        die(f"no recipe at {recipe_path}")
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    shots = recipe.get("shots") or []
    if not shots:
        die("the recipe has no shots")

    products = [Path(p).resolve() for p in a.product]
    inspo = [Path(p).resolve() for p in a.inspiration]
    for p in products + inspo:
        if not p.is_file():
            die(f"reference image not found: {p}")
    if not products:
        die("--product is required — that photo is the whole point of this step")

    # which shots get the real object in them
    if a.shots:
        want = {s.strip() for s in a.shots.split(",") if s.strip()}
        picked = [s for s in shots if s.get("id") in want]
    elif a.all_shots:
        picked = list(shots)
    else:
        picked = [s for s in shots if wants_product(s)]
        if not picked:
            # the storyboard never flagged one — put it in the middle beat rather
            # than silently shipping a video with no product in it at all.
            picked = [shots[min(len(shots) - 1, max(0, len(shots) // 2))]]
            log("(no shot was flagged as a product beat — using the middle shot)")
    picked = picked[:max(1, a.max)]

    model = MODELS[a.model]
    est = round(len(picked) * model["cost"], 3)
    if a.estimate_only:
        print("ESTIMATE: " + json.dumps({
            "this_run": est, "stills": len(picked), "engine": "product-still",
            "model": a.model,
            "summary": f"{len(picked)} product still(s) on {a.model} = ~${est:.2f}"}))
        return 0

    load_env(Path(a.env_file) if a.env_file else None)
    if not os.environ.get("FAL_KEY"):
        die("FAL_KEY not set — add it to autoVSL/.env")
    import fal_client
    import httpx

    work = recipe_path.parent
    stills = work / "stills"
    stills.mkdir(parents=True, exist_ok=True)

    log(f"Real product -> shot stills · {a.model} · {len(picked)} shot(s) ~${est:.2f}")
    log(f"product photo(s): {', '.join(p.name for p in products)}"
        + (f"  ·  inspiration: {', '.join(p.name for p in inspo)}" if inspo else ""))

    log("uploading your reference image(s) once ...")
    try:
        urls = [fal_client.upload_file(str(p)) for p in products + inspo]
    except Exception as exc:                                 # noqa: BLE001
        die(f"could not upload the reference images to fal.ai: {str(exc)[:200]}")

    made, failed, spent = 0, [], 0.0
    for i, s in enumerate(picked, 1):
        sid = s.get("id") or f"s{i}"
        prompt = compose(s, len(products), len(inspo), a.product_name)
        log(f"\n[{i}/{len(picked)}] {sid} — {(s.get('title') or scene_of(s))[:58]}")
        args_ = {"prompt": prompt, "image_urls": urls, "num_images": 1,
                 "output_format": "png", "aspect_ratio": a.aspect}
        if model["res"]:
            args_["resolution"] = model["res"]
        try:
            res = fal_client.subscribe(model["endpoint"], arguments=args_, with_logs=False)
        except Exception as exc:                             # noqa: BLE001
            log(f"    FAILED: {str(exc)[:200]}")
            failed.append(sid)
            continue
        images = res.get("images") or []
        url = (images[0].get("url") if isinstance(images[0], dict) else images[0]) if images else None
        if not url:
            log(f"    FAILED: no image returned: {str(res)[:180]}")
            failed.append(sid)
            continue
        dest = stills / f"{sid}.png"
        try:
            with httpx.Client(follow_redirects=True, timeout=300) as c:
                r = c.get(url)
                r.raise_for_status()
                dest.write_bytes(r.content)
        except Exception as exc:                             # noqa: BLE001
            log(f"    FAILED to download: {str(exc)[:160]}")
            failed.append(sid)
            continue
        s["still"] = str(dest)
        s["still_source"] = "product"
        spent += model["cost"]
        made += 1
        log(f"    OK {dest.name} ({len(dest.read_bytes()) // 1024} KB) ~${model['cost']:.3f}")

    recipe["product"] = {
        "name": a.product_name, "images": [str(p) for p in products],
        "inspiration": [str(p) for p in inspo], "stills": made,
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    recipe_path.write_text(json.dumps(recipe, indent=1), encoding="utf-8")

    log("")
    if not made:
        # never kill the run: text-to-video still has a script and a shot list.
        log("WARNING: no product still could be painted — the shots will be generated from "
            "text only, so the product on screen will NOT be yours.")
        return 0
    log(f"OK {made} shot(s) now hold your real product"
        f"{' (' + str(len(failed)) + ' failed: ' + ', '.join(failed) + ')' if failed else ''}"
        f" — ~${spent:.2f} on fal.ai")
    log("these shots will render through image-to-video, the rest through text-to-video")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
