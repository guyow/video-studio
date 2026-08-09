#!/usr/bin/env python3
"""Image editing via fal.ai Nano Banana (Google's Gemini image models).

Replace an object, erase an object, add something, restyle, or re-frame a
picture into another aspect ratio — all by describing it in words. Nano Banana
is prompt-driven (there is NO inpaint mask), so the caller composes a precise
instruction and this engine just runs it.

Every run writes a NEW version into the workdir (v00 = the untouched original,
v01, v02, ... = edits) and appends a row to edits.json, so nothing is ever
overwritten and any version can be used as the base for the next edit.

Runs under the cv venv (fal_client + httpx + Pillow present).

  python image_edit.py --work output/images/my-shot --mode erase \
      --prompt "Remove the coffee cup ..." --base v00.png \
      --model nano-banana --num 1 --aspect auto --format png \
      --env-file <autoVSL>/.env

Schemas verified against fal.ai's OpenAPI 2026-08-03:
  fal-ai/nano-banana[/edit]      prompt, image_urls, num_images, aspect_ratio,
                                 output_format, safety_tolerance, seed
  fal-ai/nano-banana-2[/edit]    + resolution (0.5K/1K/2K/4K), thinking_level
  fal-ai/nano-banana-pro[/edit]  + resolution (1K/2K/4K), system_prompt
  output: {description, images:[{url,...}]}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# ── model registry ──────────────────────────────────────────────────────────
# cost = USD per image at 1K; resolution multipliers below. Prices are fal's
# published rates (2026-08) — ESTIMATES for the UI, fal's invoice is the truth.
MODELS = {
    "nano-banana": {
        "label": "Nano Banana — fast & cheapest (recommended)",
        "gen": "fal-ai/nano-banana",
        "edit": "fal-ai/nano-banana/edit",
        "cost": 0.039,
        "resolutions": [],                       # model has no resolution param
    },
    "nano-banana-2": {
        "label": "Nano Banana 2 — sharper, still cheap",
        "gen": "fal-ai/nano-banana-2",
        "edit": "fal-ai/nano-banana-2/edit",
        "cost": 0.08,
        "resolutions": ["0.5K", "1K", "2K", "4K"],
    },
    "nano-banana-pro": {
        "label": "Nano Banana Pro — best quality (pricey)",
        "gen": "fal-ai/nano-banana-pro",
        "edit": "fal-ai/nano-banana-pro/edit",
        "cost": 0.15,
        "resolutions": ["1K", "2K", "4K"],
    },
}

RES_MULT = {
    "nano-banana-2":   {"0.5K": 0.75, "1K": 1.0, "2K": 1.5, "4K": 2.0},
    "nano-banana-pro": {"1K": 1.0, "2K": 1.0, "4K": 2.0},
}

ASPECTS = ["auto", "21:9", "16:9", "3:2", "4:3", "5:4", "1:1",
           "4:5", "3:4", "2:3", "9:16"]

# flat grey the model is told to treat as empty canvas when re-framing
PAD_FILL = (128, 128, 128)


def cost_per_image(model: str, resolution: str = "1K") -> float:
    m = MODELS.get(model) or MODELS["nano-banana"]
    mult = RES_MULT.get(model, {}).get(resolution, 1.0)
    return round(m["cost"] * mult, 4)


def log(m: str) -> None:
    print(m, flush=True)


def load_env(env_file: Path | None) -> None:
    if not env_file or not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def next_version(work: Path) -> int:
    """Highest existing v## in the workdir, plus one."""
    best = -1
    for p in work.glob("v[0-9][0-9].*"):
        m = re.match(r"v(\d\d)$", p.stem)
        if m:
            best = max(best, int(m.group(1)))
    return best + 1


def pad_to_aspect(src: Path, dest: Path, aspect: str, focus=(0.5, 0.5)) -> tuple:
    """Place the image on a larger flat-grey canvas of the target ratio.

    Nothing is cropped — the model is asked to paint the grey areas in. The
    focus point decides where the original sits on the new canvas. Returns
    (dest, paste_box, canvas_size) so the original can be stamped back on
    afterwards (see protect_original).
    """
    from PIL import Image

    im = Image.open(src).convert("RGB")
    w, h = im.size
    a, b = aspect.split(":")
    ar = float(a) / float(b)
    if w / h > ar:                                # too wide → grow height
        tw, th = w, max(h + 1, round(w / ar))
    else:                                         # too tall → grow width
        tw, th = max(w + 1, round(h * ar)), h
    x, y = round((tw - w) * focus[0]), round((th - h) * focus[1])
    canvas = Image.new("RGB", (tw, th), PAD_FILL)
    canvas.paste(im, (x, y))
    canvas.save(dest, "PNG")
    log(f"  padded canvas for re-frame: {w}x{h} -> {tw}x{th} ({aspect}), "
        f"original pinned at {x},{y}")
    return dest, (x, y, w, h), (tw, th)


def protect_original(result: Path, original: Path, box, canvas, blend: int = 0) -> None:
    """Stamp the ORIGINAL pixels back over the model's output.

    Nano Banana re-renders the whole picture rather than patching it, so a
    re-frame would otherwise come back with the subject subtly redrawn (and at
    a different resolution). We scale its output back to the padded canvas and
    paste the untouched original into the exact rectangle it occupied — so the
    only AI pixels in the final image are the newly-invented edges.

    blend > 0 feathers that many pixels at the seam (costs strictness at the
    very border); blend = 0 is bit-exact.
    """
    from PIL import Image

    res = Image.open(result).convert("RGB")
    if res.size != tuple(canvas):
        res = res.resize(tuple(canvas), Image.LANCZOS)
    orig = Image.open(original).convert("RGB")
    x, y, w, h = box
    if orig.size != (w, h):                       # defensive: never distort the source
        orig = orig.resize((w, h), Image.LANCZOS)

    if blend > 0:
        mask = Image.new("L", (w, h), 255)
        for i in range(blend):                    # linear alpha ramp around the edge
            v = int(255 * (i + 1) / (blend + 1))
            for pt in ((i, i, w - 1 - i, i), (i, h - 1 - i, w - 1 - i, h - 1 - i),
                       (i, i, i, h - 1 - i), (w - 1 - i, i, w - 1 - i, h - 1 - i)):
                Image.Image.paste(mask, v, pt[:2] + (pt[2] + 1, pt[3] + 1))
        res.paste(orig, (x, y), mask)
    else:
        res.paste(orig, (x, y))

    ext = result.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        res.save(result, "JPEG", quality=95, optimize=True)
    elif ext == ".webp":
        res.save(result, "WEBP", quality=95)
    else:
        res.save(result, "PNG", optimize=True)
    log(f"  🔒 original stamped back at {x},{y} ({w}x{h}) — only the new edges are AI"
        + (f", {blend}px seam blend" if blend else ", pixel-exact"))


def make_thumb(src: Path, dest: Path, box: int = 480) -> None:
    from PIL import Image

    im = Image.open(src)
    if im.mode in ("RGBA", "P"):
        im = im.convert("RGB")
    im.thumbnail((box, box))
    im.save(dest, "JPEG", quality=86)


def protect_outside(result: Path, original: Path, region, blend: int = 16) -> None:
    """Keep the model's work ONLY inside the region the user pointed at.

    Same problem as re-framing: Nano Banana re-renders the whole frame, so an
    "erase the cup" edit also quietly redraws the face, the hand, the grain and
    the resolution. Here we scale its output back to the source size and
    composite it through a feathered mask — inside the box we take the model,
    everywhere else we take the untouched original.

    region is (x, y, w, h) normalised 0-1 against the ORIGINAL image.
    """
    from PIL import Image, ImageDraw, ImageFilter

    res = Image.open(result).convert("RGB")
    orig = Image.open(original).convert("RGB")
    if res.size != orig.size:
        res = res.resize(orig.size, Image.LANCZOS)
    w, h = orig.size
    x0, y0 = int(region[0] * w), int(region[1] * h)
    x1, y1 = int((region[0] + region[2]) * w), int((region[1] + region[3]) * h)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, max(x1, x0 + 1)), min(h, max(y1, y0 + 1))

    # Grow the kept area before feathering. If the mask faded exactly on the
    # object's edge, the original would bleed back in and you'd see a ghost of
    # the thing you just erased plus a visible rectangle; growing by twice the
    # blend radius puts the whole fade on clean background instead.
    grow = max(2 * blend, 12)
    gx0, gy0 = max(0, x0 - grow), max(0, y0 - grow)
    gx1, gy1 = min(w, x1 + grow), min(h, y1 + grow)

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rectangle([gx0, gy0, gx1, gy1], fill=255)
    if blend > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(blend))
    out = Image.composite(res, orig, mask)       # white → model, black → original

    ext = result.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        out.save(result, "JPEG", quality=95, optimize=True)
    elif ext == ".webp":
        out.save(result, "WEBP", quality=95)
    else:
        out.save(result, "PNG", optimize=True)
    log(f"  🔒 only {gx0},{gy0}-{gx1},{gy1} kept from the model "
        f"(your box {x0},{y0}-{x1},{y1} grown {grow}px, {blend}px feather) — "
        f"the rest of the photo is your original, untouched")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work", required=True, help="the image's workdir")
    ap.add_argument("--mode", required=True,
                    choices=["replace", "erase", "add", "style", "reframe", "generate"])
    ap.add_argument("--prompt", required=True, help="the FINAL composed instruction")
    ap.add_argument("--base", help="base image (relative to --work, or absolute)")
    ap.add_argument("--ref", action="append", default=[],
                    help="extra reference image (repeatable)")
    ap.add_argument("--model", default="nano-banana", choices=list(MODELS))
    ap.add_argument("--num", type=int, default=1)
    ap.add_argument("--aspect", default="auto", choices=ASPECTS)
    ap.add_argument("--resolution", default="1K")
    ap.add_argument("--format", default="png", choices=["png", "jpeg", "webp"])
    ap.add_argument("--focus", default="0.5,0.5", help="x,y 0-1 anchor for --mode reframe")
    ap.add_argument("--no-pad", action="store_true",
                    help="re-frame using the model's native aspect only (no grey canvas)")
    ap.add_argument("--no-protect", action="store_true",
                    help="re-frame: let the model re-render the original area too (NOT strict)")
    ap.add_argument("--seam-blend", type=int, default=0,
                    help="re-frame: feather N px where the original meets the new edges (0 = exact)")
    ap.add_argument("--protect-region",
                    help="x,y,w,h normalised — keep the model's work ONLY inside this box "
                         "and restore the untouched original everywhere else")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--user-text", default="", help="what the user typed (for edits.json)")
    ap.add_argument("--env-file")
    args = ap.parse_args()

    load_env(Path(args.env_file) if args.env_file else None)
    if not os.environ.get("FAL_KEY"):
        sys.exit("FAL_KEY not set — add it to autoVSL/.env")

    import fal_client
    import httpx

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    model = MODELS[args.model]
    resolution = args.resolution if args.resolution in model["resolutions"] else None

    # resolve inputs
    def resolve(p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else (work / q)

    inputs: list[Path] = []
    base: Path | None = None
    if args.mode != "generate":
        if not args.base:
            sys.exit("--base is required for every mode except generate")
        base = resolve(args.base)
        if not base.is_file():
            sys.exit(f"base image not found: {base}")
        inputs.append(base)
    for r in args.ref:
        rp = resolve(r)
        if not rp.is_file():
            sys.exit(f"reference image not found: {rp}")
        inputs.append(rp)

    log(f"Image edit: {work.name}  ·  mode {args.mode}  ·  {model['label'].split(' — ')[0]}")
    log(f"prompt: {args.prompt}")

    # cheap pre-flight: fails fast on a locked / out-of-balance account so we
    # never start a paid edit that cannot finish.
    try:
        fal_client.upload(b"ok", "text/plain")
    except Exception as exc:                                  # noqa: BLE001
        msg = str(exc).lower()
        if any(w in msg for w in ("403", "locked", "balance", "exhaust", "unauthor")):
            sys.exit(f"fal.ai account problem (check balance / FAL_KEY): {exc}")

    # re-frame: put the original on a bigger canvas so nothing gets cropped
    pad_box = pad_canvas = None
    if args.mode == "reframe" and base is not None and not args.no_pad:
        if args.aspect == "auto":
            sys.exit("re-frame needs a real aspect ratio (not 'auto')")
        try:
            fx, fy = (float(v) for v in args.focus.split(","))
        except ValueError:
            fx, fy = 0.5, 0.5
        padded, pad_box, pad_canvas = pad_to_aspect(
            base, work / "_reframe-src.png", args.aspect,
            (min(max(fx, 0), 1), min(max(fy, 0), 1)))
        inputs[0] = padded

    # every other mode: keep the model's work only inside the box the user drew
    keep_box = None
    if args.protect_region and base is not None and args.mode != "reframe":
        try:
            keep_box = tuple(float(v) for v in args.protect_region.split(","))
            if len(keep_box) != 4 or keep_box[2] <= 0 or keep_box[3] <= 0:
                raise ValueError
        except ValueError:
            sys.exit("--protect-region must be x,y,w,h normalised 0-1")

    log("uploading input image(s) ...")
    image_urls = [fal_client.upload_file(str(p)) for p in inputs]

    endpoint = model["edit"] if image_urls else model["gen"]
    arguments: dict = {
        "prompt": args.prompt,
        "num_images": max(1, min(4, args.num)),
        "output_format": args.format,
        "aspect_ratio": args.aspect,
    }
    if image_urls:
        arguments["image_urls"] = image_urls
    if resolution:
        arguments["resolution"] = resolution
    if args.seed is not None:
        arguments["seed"] = args.seed

    est = round(cost_per_image(args.model, resolution or "1K") * arguments["num_images"], 4)
    log(f"generating on fal.ai ({endpoint}) ~${est:.3f} ...")
    try:
        res = fal_client.subscribe(endpoint, arguments=arguments, with_logs=False)
    except Exception as exc:                                  # noqa: BLE001
        sys.exit(f"fal.ai edit failed: {exc}")

    images = res.get("images") or []
    if not images:
        sys.exit(f"no image returned: {str(res)[:400]}")
    if res.get("description"):
        log(f"model said: {str(res['description'])[:300]}")

    ext = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}[args.format]
    idx = next_version(work)
    written: list[str] = []
    with httpx.Client(follow_redirects=True, timeout=300) as c:
        for i, img in enumerate(images):
            url = img.get("url") if isinstance(img, dict) else img
            if not url:
                continue
            name = f"v{idx:02d}{ext}" if i == 0 else f"v{idx:02d}-alt{i}{ext}"
            dest = work / name
            r = c.get(url)
            r.raise_for_status()
            dest.write_bytes(r.content)
            # STRICT modes: the model only gets to keep the pixels it was asked for
            if pad_box and not args.no_protect:
                protect_original(dest, base, pad_box, pad_canvas, max(0, args.seam_blend))
            elif keep_box and not args.no_protect:
                protect_outside(dest, base, keep_box, args.seam_blend or 16)
            if i == 0:
                make_thumb(dest, work / f"thumb-v{idx:02d}.jpg")
            written.append(name)
            log(f"  wrote {name} ({len(r.content) // 1024} KB)")

    if not written:
        sys.exit("fal returned images but none had a usable url")

    (work / "_reframe-src.png").unlink(missing_ok=True)

    # append this edit to the version history
    hist_file = work / "edits.json"
    try:
        hist = json.loads(hist_file.read_text(encoding="utf-8"))
    except Exception:                                         # noqa: BLE001
        hist = []
    hist.append({
        "version": f"v{idx:02d}", "file": written[0], "alts": written[1:],
        "parent": Path(args.base).name if args.base else None,
        "mode": args.mode, "user_text": args.user_text, "prompt": args.prompt,
        "model": args.model, "model_label": model["label"],
        "aspect": args.aspect, "resolution": resolution, "format": args.format,
        "protected": bool((pad_box or keep_box) and not args.no_protect),
        "est_cost": est, "created": time.time(),
        "refs": [Path(r).name for r in args.ref],
    })
    hist_file.write_text(json.dumps(hist, indent=2), encoding="utf-8")

    log(f"\ndeliverable: {work / written[0]}")
    log(f"estimated fal.ai cost: ~${est:.3f}")
    log("Done ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
