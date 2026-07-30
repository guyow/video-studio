#!/usr/bin/env python3
"""ComfyUI Studio — local, free image tools for autoVSL, driven through a
running ComfyUI (start it with run_nvidia_full.bat, server 127.0.0.1:8188).

Modes:
  generate  prompt  -> N image variations (SD 1.5, batch)   [FREE / local GPU]
  upscale   image   -> 4x Real-ESRGAN upscaled image         [FREE / local GPU]
  inpaint   image   -> repaint a masked region from a prompt [FREE / local GPU]
  keyframe  image   -> ControlNet-guided image (consistency) [FREE / local GPU]

Outputs land in autoVSL/output/comfyui-studio/<mode>/. Nothing is uploaded to
the cloud — this all runs on the local GPU.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comfyui_client import ComfyUIClient, ComfyUIError          # noqa: E402
from comfyui_workflows import (build_txt2img, build_upscale,     # noqa: E402
                               build_inpaint, build_controlnet_keyframe)

ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = ROOT / "output" / "comfyui-studio"


def _client(url):
    c = ComfyUIClient(url)
    if not c.ping():
        raise SystemExit(f"ComfyUI not reachable at {url}. Launch it "
                         f"(run_nvidia_full.bat) and retry.")
    return c


def _pick_checkpoint(c, requested):
    if requested:
        return requested
    cands = c.checkpoints()
    if not cands:
        raise SystemExit("No checkpoints in ComfyUI/models/checkpoints.")
    return cands[0]


def _save_all(c, files, out_dir, stem):
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for i, f in enumerate(files, 1):
        ext = Path(f["filename"]).suffix or ".png"
        dest = out_dir / (f"{stem}.{ext.lstrip('.')}" if len(files) == 1
                          else f"{stem}_{i:02d}{ext}")
        c.download(f, dest)
        saved.append(dest)
    return saved


def cmd_generate(a, c):
    ckpt = _pick_checkpoint(c, a.checkpoint)
    print(f"generate: {a.count} variation(s) @ {a.width}x{a.height} · {ckpt}")
    kwargs = {}
    if getattr(a, "negative", None):
        kwargs["negative"] = a.negative
    wf = build_txt2img(checkpoint=ckpt, positive=a.prompt, width=a.width,
                       height=a.height, seed=a.seed, steps=a.steps,
                       batch_size=a.count, filename_prefix="studio/gen", **kwargs)
    files = c.run(wf, max_wait=900)
    saved = _save_all(c, files, OUT_ROOT / "generate", a.out or f"gen_{a.seed}")
    for s in saved:
        print(f"  → {s}")


def cmd_upscale(a, c):
    name = c.upload_image(a.image)
    print(f"upscale: {Path(a.image).name} → 4x ({a.model})")
    wf = build_upscale(image_name=name, upscale_model=a.model,
                       filename_prefix="studio/upscaled")
    files = c.run(wf, max_wait=600)
    saved = _save_all(c, files, OUT_ROOT / "upscale",
                      a.out or (Path(a.image).stem + "_4x"))
    for s in saved:
        print(f"  → {s}")


def cmd_inpaint(a, c):
    ckpt = _pick_checkpoint(c, a.checkpoint)
    img = c.upload_image(a.image)
    mask = c.upload_image(a.mask) if a.mask else None
    print(f"inpaint: {Path(a.image).name} · mask={'yes' if mask else 'alpha'} · {ckpt}")
    wf = build_inpaint(checkpoint=ckpt, image_name=img, mask_name=mask,
                       positive=a.prompt, seed=a.seed, steps=a.steps,
                       denoise=a.denoise, filename_prefix="studio/inpaint")
    files = c.run(wf, max_wait=900)
    saved = _save_all(c, files, OUT_ROOT / "inpaint",
                      a.out or (Path(a.image).stem + "_inpaint"))
    for s in saved:
        print(f"  → {s}")


def cmd_keyframe(a, c):
    ckpt = _pick_checkpoint(c, a.checkpoint)
    cnets = c.controlnets()
    if not cnets:
        raise SystemExit("No ControlNet models in ComfyUI/models/controlnet yet.")
    cnet = a.controlnet or cnets[0]
    ref = c.upload_image(a.image)
    print(f"keyframe: guided by {Path(a.image).name} · controlnet={cnet} · {ckpt}")
    wf = build_controlnet_keyframe(
        checkpoint=ckpt, controlnet=cnet, control_image=ref, positive=a.prompt,
        width=a.width, height=a.height, seed=a.seed, steps=a.steps,
        strength=a.strength, filename_prefix="studio/keyframe")
    files = c.run(wf, max_wait=900)
    saved = _save_all(c, files, OUT_ROOT / "keyframe",
                      a.out or (Path(a.image).stem + "_kf"))
    for s in saved:
        print(f"  → {s}")


def main() -> int:
    p = argparse.ArgumentParser(description="ComfyUI Studio — local free image tools")
    p.add_argument("--url", default=os.environ.get("COMFYUI_URL", "127.0.0.1:8188"))
    p.add_argument("--checkpoint", default=os.environ.get("COMFYUI_CHECKPOINT"))
    sub = p.add_subparsers(dest="mode", required=True)

    g = sub.add_parser("generate"); g.add_argument("prompt")
    g.add_argument("--count", type=int, default=4); g.add_argument("--width", type=int, default=512)
    g.add_argument("--height", type=int, default=768); g.add_argument("--steps", type=int, default=25)
    g.add_argument("--seed", type=int, default=1); g.add_argument("--out")
    g.add_argument("--negative", default=None, help="Negative prompt override (brand kit)")

    u = sub.add_parser("upscale"); u.add_argument("image")
    u.add_argument("--model", default="RealESRGAN_x4plus.pth"); u.add_argument("--out")

    n = sub.add_parser("inpaint"); n.add_argument("image"); n.add_argument("prompt")
    n.add_argument("--mask", default=None); n.add_argument("--denoise", type=float, default=1.0)
    n.add_argument("--steps", type=int, default=25); n.add_argument("--seed", type=int, default=1)
    n.add_argument("--out")

    k = sub.add_parser("keyframe"); k.add_argument("image"); k.add_argument("prompt")
    k.add_argument("--controlnet", default=None); k.add_argument("--strength", type=float, default=0.8)
    k.add_argument("--width", type=int, default=512); k.add_argument("--height", type=int, default=768)
    k.add_argument("--steps", type=int, default=25); k.add_argument("--seed", type=int, default=1)
    k.add_argument("--out")

    a = p.parse_args()
    c = _client(a.url)
    try:
        {"generate": cmd_generate, "upscale": cmd_upscale,
         "inpaint": cmd_inpaint, "keyframe": cmd_keyframe}[a.mode](a, c)
    except ComfyUIError as e:
        print(f"ComfyUI error: {e}", file=sys.stderr)
        return 1
    print("✓ done (local GPU, $0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
