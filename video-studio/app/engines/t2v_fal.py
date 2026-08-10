#!/usr/bin/env python3
"""Text -> Video on fal.ai. One clip per shot, straight from the prompt.

Why this exists: the b-roll Factory paints every still in ComfyUI first, so a
4 GB card (or a hung ComfyUI) takes the whole pipeline down with it - even the
paid path. This engine talks to fal.ai ONLY: prompt in, finished clip out. No
ComfyUI, no local GPU, no model downloads.

It writes clips/<sid>.mp4 into the same batch layout the b-roll assemble step
already reads, so `broll_video.py assemble` stitches + narrates them unchanged.

  python t2v_fal.py --recipe output/broll/<batch>/recipe.json --model veo3-fast \
      --aspect 9:16 --env-file <autoVSL>/.env

Model endpoints and their parameter shapes were read from fal's live OpenAPI
schemas. Costs are ESTIMATES for the confirm dialog - fal's invoice is the
authority.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# dur: how the model wants duration — "str" ('5'), "strs" ('8s'), or "int" (8)
MODELS = {
    "veo3": {
        "label": "Veo 3 — best quality, native audio (premium)",
        "endpoint": "fal-ai/veo3", "dur": "strs", "durations": ["4s", "6s", "8s"],
        "aspects": ["16:9", "9:16"], "cost_per_s": 0.40, "audio": True, "resolution": "1080p",
    },
    "veo3-fast": {
        "label": "Veo 3 Fast — Veo quality, cheaper (recommended)",
        "endpoint": "fal-ai/veo3/fast", "dur": "strs", "durations": ["4s", "6s", "8s"],
        "aspects": ["16:9", "9:16"], "cost_per_s": 0.15, "audio": True, "resolution": "720p",
    },
    "sora-2": {
        "label": "Sora 2 — OpenAI, strong realism",
        "endpoint": "fal-ai/sora-2/text-to-video", "dur": "int", "durations": [4, 8, 12],
        "aspects": ["16:9", "9:16"], "cost_per_s": 0.30, "audio": True, "resolution": "720p",
    },
    "kling-o1-ref": {
        # reference-conditioned: pass 1-7 reference images (a Character Bank folder)
        # and the SAME person/objects appear in every generated shot.
        "label": "Kling O1 Reference — persistent character from reference images",
        "endpoint": "fal-ai/kling-video/o1/reference-to-video", "dur": "str",
        "durations": ["5", "10"], "aspects": ["16:9", "9:16", "1:1"], "cost_per_s": 0.112,
        "reference": True,
    },
    "kling-2.5-pro": {
        "label": "Kling 2.5 Turbo Pro — great motion",
        "endpoint": "fal-ai/kling-video/v2.5-turbo/pro/text-to-video", "dur": "str",
        "durations": ["5", "10"], "aspects": ["16:9", "9:16", "1:1"], "cost_per_s": 0.07,
    },
    "kling-2.1-master": {
        "label": "Kling 2.1 Master — cinematic",
        "endpoint": "fal-ai/kling-video/v2.1/master/text-to-video", "dur": "str",
        "durations": ["5", "10"], "aspects": ["16:9", "9:16", "1:1"], "cost_per_s": 0.09,
    },
    "seedance-pro": {
        "label": "Seedance 1.0 Pro — 1080p, flexible length",
        "endpoint": "fal-ai/bytedance/seedance/v1/pro/text-to-video", "dur": "str",
        "durations": [str(n) for n in range(2, 13)],
        "aspects": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
        "cost_per_s": 0.12, "resolution": "1080p",
    },
    "hailuo-02": {
        "label": "MiniMax Hailuo 02 — lively motion, cheap",
        "endpoint": "fal-ai/minimax/hailuo-02/standard/text-to-video", "dur": "str",
        "durations": ["6", "10"], "aspects": ["16:9", "9:16", "1:1"], "cost_per_s": 0.05,
    },
    "wan-2.2": {
        "label": "Wan 2.2 — budget",
        "endpoint": "fal-ai/wan/v2.2-a14b/text-to-video", "dur": "str",
        "durations": ["5"], "aspects": ["16:9", "9:16", "1:1"], "cost_per_s": 0.04,
    },
}
NEG = "blurry, distorted, warped face, low quality, watermark, text artifacts"


def log(m: str) -> None:
    print(m, flush=True)


def die(m: str) -> None:
    print(f"ERROR: {m}", flush=True)
    sys.exit(1)


def probe_dur(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    try:
        return float((r.stdout or "0").strip() or 0)
    except ValueError:
        return 0.0


def load_env(path: Path | None) -> None:
    if path and path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def pick_duration(model: dict, want: float):
    """Nearest allowed duration >= want (models only take fixed lengths)."""
    opts = model["durations"]
    if model["dur"] == "int":
        nums = [int(o) for o in opts]
    else:
        nums = [float(str(o).rstrip("s")) for o in opts]
    chosen = next((n for n in sorted(nums) if n >= want - 0.4), max(nums))
    if model["dur"] == "int":
        return int(chosen), float(chosen)
    if model["dur"] == "strs":
        return f"{int(chosen)}s", float(chosen)
    return str(int(chosen)), float(chosen)


def estimate(model_key: str, shots: list[dict]) -> dict:
    m = MODELS[model_key]
    total = 0.0
    secs = 0.0
    for s in shots:
        _, real = pick_duration(m, float(s.get("duration_s") or 4))
        secs += real
        total += real * m["cost_per_s"]
    return {"this_run": round(total, 2), "shots": len(shots), "seconds": round(secs, 1),
            "engine": "fal-t2v", "model": model_key,
            "summary": f"{len(shots)} shots / {secs:.0f}s on {m['label'].split(' — ')[0]} "
                       f"= ~${total:.2f} (estimate)"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recipe", required=True, help="output/broll/<batch>/recipe.json")
    ap.add_argument("--model", default="veo3-fast", choices=sorted(MODELS))
    ap.add_argument("--aspect", default="9:16")
    ap.add_argument("--shots", default="", help="comma shot ids (default all)")
    ap.add_argument("--env-file", dest="env_file")
    ap.add_argument("--estimate-only", action="store_true")
    ap.add_argument("--character-dir", dest="character_dir",
                    help="Character Bank folder (output/characters/<id>) — its refs/ images "
                         "are passed as reference images on models that support them")
    a = ap.parse_args()

    recipe_path = Path(a.recipe).resolve()
    if not recipe_path.is_file():
        die(f"no recipe at {recipe_path}")
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    shots = recipe.get("shots") or []
    if a.shots:
        want = {s.strip() for s in a.shots.split(",") if s.strip()}
        shots = [s for s in shots if s.get("id") in want]
    if not shots:
        die("no shots in the recipe")

    model = MODELS[a.model]
    aspect = a.aspect if a.aspect in model["aspects"] else model["aspects"][0]
    est = estimate(a.model, shots)
    if a.estimate_only:
        print("ESTIMATE: " + json.dumps(est))
        return 0

    load_env(Path(a.env_file) if a.env_file else None)
    if not os.environ.get("FAL_KEY"):
        die("FAL_KEY not set — add it to autoVSL/.env")
    import fal_client
    import httpx

    # fail fast if the account is locked / out of balance, before any paid call
    try:
        fal_client.upload(b"ok", "text/plain")
    except Exception as exc:                      # noqa: BLE001
        msg = str(exc).lower()
        if any(w in msg for w in ("403", "locked", "balance", "exhaust", "unauthor")):
            die(f"fal.ai account problem (check balance / FAL_KEY): {exc}")

    # reference images (Character Bank) → uploaded once, reused for every shot
    ref_urls: list[str] = []
    if model.get("reference"):
        cdir = Path(a.character_dir or "")
        refs = sorted((cdir / "refs").glob("ref-*")) if cdir.is_dir() else []
        refs = [r for r in refs if r.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")][:7]
        if not refs:
            die(f"model '{a.model}' needs reference images — pass --character-dir "
                "pointing at a Character Bank folder (output/characters/<id>)")
        log(f"Uploading {len(refs)} character reference image(s)…")
        for r in refs:
            ref_urls.append(fal_client.upload_file(str(r)))
    elif a.character_dir:
        log(f"NOTE: model '{a.model}' ignores reference images — pick kling-o1-ref "
            "to keep the same character across shots.")

    work = recipe_path.parent
    clips_dir = work / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    log(f"Text -> Video on fal.ai · {model['label']}")
    log(f"{len(shots)} shot(s) · {aspect} · {est['summary']}")

    made, failed, spent = [], [], 0.0
    for i, s in enumerate(shots, 1):
        sid = s.get("id") or f"s{i}"
        dur_val, real = pick_duration(model, float(s.get("duration_s") or 4))
        prompt = (s.get("prompt") or "").strip()
        log(f"\n[{i}/{len(shots)}] {sid} — {(s.get('title') or prompt)[:58]}")
        log(f"    {real:.0f}s on {model['endpoint']}")
        args_ = {"prompt": prompt, "aspect_ratio": aspect, "duration": dur_val}
        if model.get("resolution"):
            args_["resolution"] = model["resolution"]
        if model.get("audio"):
            args_["generate_audio"] = False        # narration is added at assembly
        if a.model.startswith("kling"):
            args_["negative_prompt"] = (s.get("negative") or NEG)[:900]
        if ref_urls:
            args_["image_urls"] = ref_urls   # O1 reference conditioning (fal schema)
        try:
            res = fal_client.subscribe(model["endpoint"], arguments=args_, with_logs=False)
        except Exception as exc:                  # noqa: BLE001
            log(f"    FAILED: {str(exc)[:200]}")
            failed.append(sid)
            continue
        v = res.get("video") or {}
        url = v.get("url") if isinstance(v, dict) else v
        if not url:
            log(f"    FAILED: no video in response: {str(res)[:180]}")
            failed.append(sid)
            continue
        out = clips_dir / f"{sid}.mp4"
        with httpx.Client(follow_redirects=True, timeout=900) as c:
            r = c.get(url)
            r.raise_for_status()
            out.write_bytes(r.content)
        spent += real * model["cost_per_s"]
        log(f"    OK {out.name} ({probe_dur(out):.1f}s) ~${real * model['cost_per_s']:.2f}")
        made.append({"shot_id": sid, "file": str(out), "duration_s": probe_dur(out)})

    # MERGE into any existing manifest — a partial re-render (--shots s3) must not
    # orphan the other shots' clips, or assemble would drop them from the video.
    gen_path = work / "generated.json"
    prev: dict = {}
    if gen_path.is_file():
        try:
            prev = json.loads(gen_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = {}
    by_id = {e.get("shot_id"): e for e in prev.get("generated", []) if e.get("shot_id")}
    for e in made:
        by_id[e["shot_id"]] = e
    still_failed = [f for f in failed if f not in by_id]
    gen_path.write_text(json.dumps({
        "batch": work.name, "generated": list(by_id.values()), "failed": still_failed,
        "motion_engine": f"fal-t2v:{a.model}", "style_mode": "text-to-video",
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, indent=1), encoding="utf-8")

    log("")
    log(f"Done: {len(made)} clip(s) made, {len(failed)} failed"
        f"{' (' + ', '.join(failed) + ')' if failed else ''} — ~${spent:.2f} on fal.ai")
    if not made:
        die("no clips were generated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
