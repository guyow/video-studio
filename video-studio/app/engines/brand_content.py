#!/usr/bin/env python3
"""Brand Content orchestrator — brief → AI imagery → brand composite.

Runs under autoVSL/.venv (cv venv). Pipeline for ONE deliverable:
  1. ComfyUI `generate` a small background at the platform aspect (brand style_suffix
     + negative_prompt), then `upscale` 4x  — via the existing comfyui_studio.py CLI.
  2. compositor.render_template overlays the brand text + wordmark deterministically.
The model paints ONLY background imagery; all brand text/logo/colour come from the
compositor, so every post is pixel-consistent.

Copy ({eyebrow,headline,subhead,cta,price_line}) is generated upstream by the server's
/api/brand/copy (it owns the Claude CLI + bank helpers) and passed in as --content json.

Emits `=== stage: <name>` and `NN%|` lines so the Video Studio job queue shows progress.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compositor import BrandKit, render_template  # noqa: E402

SCRIPTS = None  # set from brand kit's autovsl root at runtime


def die(msg: str) -> None:
    print(f"ERROR: {msg}", flush=True)
    sys.exit(1)


def run_comfy(studio_py: Path, args: list[str], env_url: str) -> list[Path]:
    """Invoke comfyui_studio.py; return the `→` result paths it prints."""
    import os
    env = dict(os.environ, COMFYUI_URL=env_url, PYTHONUTF8="1")
    proc = subprocess.Popen([sys.executable, str(studio_py), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env)
    paths = []
    for line in proc.stdout:
        line = line.rstrip()
        print("   " + line, flush=True)
        if "→" in line:
            p = Path(line.split("→", 1)[1].strip())
            if p.suffix.lower() in (".png", ".jpg", ".jpeg"):
                paths.append(p)
    if proc.wait() != 0:
        die("ComfyUI step failed — is it running? (launch run_nvidia_lowvram.bat)")
    return paths


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", required=True)
    ap.add_argument("--platform", required=True)          # e.g. meta-1x1
    ap.add_argument("--template", required=True)          # brand_templates/*.json
    ap.add_argument("--content", required=True)           # json file {eyebrow,headline,...}
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--out-dir", required=True)           # brand-content root
    ap.add_argument("--bg-prompt", default=None)          # explicit background prompt
    ap.add_argument("--preset", default=None)             # or a brand-kit preset id
    ap.add_argument("--comfy-url", default="127.0.0.1:8188")
    ap.add_argument("--scripts-dir", required=True)       # autoVSL/scripts
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-upscale", action="store_true")
    a = ap.parse_args()

    kit = BrandKit(a.kit)
    probs = kit.preflight()
    if probs:
        die("brand kit not ready: " + "; ".join(probs))
    template = json.loads(Path(a.template).read_text(encoding="utf-8"))
    content = json.loads(Path(a.content).read_text(encoding="utf-8"))
    plat = kit.data["platforms"][a.platform]
    studio_py = Path(a.scripts_dir) / "comfyui_studio.py"

    # background prompt: explicit, or a named preset, or the template's default preset
    bg_prompt = a.bg_prompt
    if not bg_prompt:
        pid = a.preset or template.get("default_preset")
        preset = next((p for p in kit.data.get("presets", []) if p["id"] == pid), None)
        if not preset:
            die(f"no background prompt and preset '{pid}' not found in brand kit")
        bg_prompt = preset["prompt"]
    full_prompt = f"{bg_prompt}, {kit.data.get('style_suffix','')}"
    negative = kit.data.get("negative_prompt", "")

    camp_dir = Path(a.out_dir) / a.campaign
    camp_dir.mkdir(parents=True, exist_ok=True)
    work = camp_dir / "_work"
    work.mkdir(exist_ok=True)

    print("=== stage: imagery", flush=True)
    print("10%| generating background imagery on the GPU…", flush=True)
    gen = run_comfy(studio_py,
                    ["generate", full_prompt, "--count", "1",
                     "--width", str(plat["gen_w"]), "--height", str(plat["gen_h"]),
                     "--seed", str(a.seed), "--negative", negative,
                     "--out", f"{a.campaign}_bg"],
                    a.comfy_url)
    if not gen:
        die("no image produced by ComfyUI generate")
    bg = gen[0]

    if not a.no_upscale:
        print("55%| upscaling 4x (Real-ESRGAN)…", flush=True)
        up = run_comfy(studio_py, ["upscale", str(bg), "--out", f"{a.campaign}_bg4x"], a.comfy_url)
        if up:
            bg = up[0]

    print("=== stage: composite", flush=True)
    print("85%| compositing brand layout (fonts, colours, wordmark)…", flush=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = camp_dir / f"{a.platform}-{template.get('id','ad')}-{stamp}.jpg"
    render_template(kit, {**template, "platform": a.platform}, content, bg, out_path)

    # keep the raw background too, for reuse / re-composite
    try:
        (camp_dir / f"background-{stamp}{bg.suffix}").write_bytes(bg.read_bytes())
    except Exception:
        pass

    report = {"campaign": a.campaign, "platform": a.platform,
              "template": template.get("id"), "content": content,
              "bg_prompt": bg_prompt, "seed": a.seed, "created": time.time()}
    (camp_dir / f"{out_path.stem}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")

    print("100%| done", flush=True)
    print(f"RESULT: {out_path}", flush=True)


if __name__ == "__main__":
    main()
