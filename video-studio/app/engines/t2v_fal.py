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
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# dur: how the model wants duration — "str" ('5'), "strs" ('8s'), or "int" (8)
# i2v: the same model's IMAGE-to-video endpoint. A shot that carries a `still`
# (painted from the user's real product) renders through this one instead, so
# the object on screen is theirs and not something the model invented.
MODELS = {
    "veo3": {
        "label": "Veo 3 — best quality, native audio (premium)",
        "endpoint": "fal-ai/veo3", "i2v": "fal-ai/veo3/image-to-video",
        "dur": "strs", "durations": ["4s", "6s", "8s"],
        "aspects": ["16:9", "9:16"], "cost_per_s": 0.40, "audio": True, "resolution": "1080p",
    },
    "veo3-fast": {
        "label": "Veo 3 Fast — Veo quality, cheaper (recommended)",
        "endpoint": "fal-ai/veo3/fast", "i2v": "fal-ai/veo3/fast/image-to-video",
        "dur": "strs", "durations": ["4s", "6s", "8s"],
        "aspects": ["16:9", "9:16"], "cost_per_s": 0.15, "audio": True, "resolution": "720p",
    },
    "sora-2": {
        "label": "Sora 2 — OpenAI, strong realism",
        "endpoint": "fal-ai/sora-2/text-to-video", "i2v": "fal-ai/sora-2/image-to-video",
        "dur": "int", "durations": [4, 8, 12],
        "aspects": ["16:9", "9:16"], "cost_per_s": 0.30, "audio": True, "resolution": "720p",
    },
    "kling-2.5-pro": {
        "label": "Kling 2.5 Turbo Pro — great motion",
        "endpoint": "fal-ai/kling-video/v2.5-turbo/pro/text-to-video",
        "i2v": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video", "dur": "str",
        "durations": ["5", "10"], "aspects": ["16:9", "9:16", "1:1"], "cost_per_s": 0.07,
    },
    "kling-2.1-master": {
        "label": "Kling 2.1 Master — cinematic",
        "endpoint": "fal-ai/kling-video/v2.1/master/text-to-video",
        "i2v": "fal-ai/kling-video/v2.1/master/image-to-video", "dur": "str",
        "durations": ["5", "10"], "aspects": ["16:9", "9:16", "1:1"], "cost_per_s": 0.09,
    },
    "seedance-pro": {
        "label": "Seedance 1.0 Pro — 1080p, flexible length",
        "endpoint": "fal-ai/bytedance/seedance/v1/pro/text-to-video",
        "i2v": "fal-ai/bytedance/seedance/v1/pro/image-to-video", "dur": "str",
        "durations": [str(n) for n in range(2, 13)],
        "aspects": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
        "cost_per_s": 0.12, "resolution": "1080p",
    },
    "hailuo-02": {
        "label": "MiniMax Hailuo 02 — lively motion, cheap",
        "endpoint": "fal-ai/minimax/hailuo-02/standard/text-to-video",
        "i2v": "fal-ai/minimax/hailuo-02/standard/image-to-video", "dur": "str",
        "durations": ["6", "10"], "aspects": ["16:9", "9:16", "1:1"], "cost_per_s": 0.05,
    },
    "wan-2.2": {
        "label": "Wan 2.2 — budget",
        "endpoint": "fal-ai/wan/v2.2-a14b/text-to-video",
        "i2v": "fal-ai/wan/v2.2-a14b/image-to-video", "dur": "str",
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


def classify_exc(msg: str) -> str:
    """Sort a fal exception into a retry policy bucket."""
    low = msg.lower()
    if any(w in low for w in ("content policy", "moderation", "flagged", "safety",
                              "sensitive", "prohibited", "policy violation")):
        return "moderation"
    if any(w in low for w in ("422", "validation", "unprocessable", "extra",
                              "not permitted", "unexpected")):
        return "schema"
    if any(w in low for w in ("429", "rate limit", "500", "502", "503", "504",
                              "timeout", "timed out", "connection", "temporarily",
                              "internal server", "bad gateway", "unavailable")):
        return "transient"
    return "other"


def sanitize_prompt(prompt: str) -> str:
    """Moderation fallback: neutralize the detailed person description and the
    bed/bathroom framings that trip Veo/Sora person-safety filters."""
    p = prompt
    try:
        from broll_video import UGC_CHARACTER
        p = p.replace(UGC_CHARACTER, "A woman in her late 30s speaking casually at home.")
    except ImportError:
        pass
    for bad, good in (("lying in bed under a duvet", "sitting on a couch under a blanket"),
                      ("under a duvet", "under a blanket"),
                      ("in bed ", "on the couch "),
                      ("bathroom mirror", "hallway mirror"),
                      ("messy bathroom", "hallway"),
                      ("toothbrush in hand", "coffee mug in hand")):
        p = re.sub(re.escape(bad), good, p, flags=re.I)
    return p


def subscribe_deadline(fal_client, endpoint: str, args_: dict, deadline_s: float):
    """fal_client.subscribe with a wall-clock cap — a hung queue item used to
    block the whole chain forever (subscribe has no timeout of its own)."""
    box: dict = {}

    def _call():
        try:
            box["res"] = fal_client.subscribe(endpoint, arguments=args_, with_logs=False)
        except Exception as exc:                  # noqa: BLE001
            box["exc"] = exc

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    t.join(deadline_s)
    if t.is_alive():
        raise TimeoutError(f"fal.ai gave no result within {deadline_s:.0f}s — queue item hung")
    if "exc" in box:
        raise box["exc"]
    return box["res"]


def call_shot(fal_client, endpoint: str, args_: dict, minimal: dict, deadline_s: float):
    """One shot's fal call with a per-failure-kind retry policy.

    schema (422)   -> one retry with the bare-minimum arguments (extras stripped)
    transient      -> up to 3 retries, backoff 15/45/120s + jitter
    moderation     -> no retry here (caller retries once with a sanitized prompt)
    timeout        -> no retry (the deadline already bounds the chain)
    other          -> one retry
    Returns (result | None, fail_kind | None, detail).
    """
    schema_left, other_left = 1, 1
    backoffs = [15, 45, 120]
    cur = dict(args_)
    while True:
        try:
            return subscribe_deadline(fal_client, endpoint, cur, deadline_s), None, ""
        except Exception as exc:                  # noqa: BLE001
            msg = str(exc)
            kind = "timeout" if isinstance(exc, TimeoutError) else classify_exc(msg)
            if kind == "moderation" or kind == "timeout":
                return None, kind, msg
            if kind == "schema" and schema_left:
                schema_left -= 1
                log(f"    schema rejected the extras ({msg[:100]}) — retrying minimal ...")
                cur = dict(minimal)
                continue
            if kind == "transient" and backoffs:
                wait = backoffs.pop(0) + random.uniform(0, 5)
                log(f"    transient fal error ({msg[:100]}) — retrying in {wait:.0f}s ...")
                time.sleep(wait)
                continue
            if kind == "other" and other_left:
                other_left -= 1
                log(f"    error ({msg[:100]}) — one retry ...")
                continue
            return None, kind, msg


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
    ap.add_argument("--min-success", type=float, default=0.75,
                    help="fail (exit 3) instead of assembling when fewer than this "
                         "fraction of shots rendered (default 0.75)")
    ap.add_argument("--shot-timeout", type=float, default=900,
                    help="wall-clock cap per shot in seconds (default 900)")
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
        if any(w in msg for w in ("401", "402", "403", "locked", "balance", "exhaust", "unauthor")):
            die(f"fal.ai account problem (check balance / FAL_KEY): {exc}")

    work = recipe_path.parent
    clips_dir = work / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    log(f"Text -> Video on fal.ai · {model['label']}")
    log(f"{len(shots)} shot(s) · {aspect} · {est['summary']}")

    made, failed, failures, spent = [], [], [], 0.0
    try:
        for i, s in enumerate(shots, 1):
            sid = s.get("id") or f"s{i}"
            dur_val, real = pick_duration(model, float(s.get("duration_s") or 4))
            prompt = (s.get("prompt") or "").strip()
            log(f"\n[{i}/{len(shots)}] {sid} — {(s.get('title') or prompt)[:58]}")

            # a finished clip from an earlier run is free — only failed shots re-bill
            out = clips_dir / f"{sid}.mp4"
            if out.is_file() and probe_dur(out) > 0.5:
                log(f"    cached from a previous run ({probe_dur(out):.1f}s) — $0")
                made.append({"shot_id": sid, "file": str(out), "duration_s": probe_dur(out)})
                continue

            # a shot with a `still` holds the user's REAL product: animate that exact
            # frame instead of letting the model invent the object from words.
            still = Path(s["still"]) if s.get("still") else None
            still_url = None
            if still and still.is_file() and model.get("i2v"):
                try:
                    still_url = fal_client.upload_file(str(still))
                except Exception as exc:              # noqa: BLE001
                    log(f"    (could not upload {still.name}: {str(exc)[:120]} — text-to-video instead)")
            elif still and still.is_file():
                log(f"    ({a.model} has no image-to-video endpoint — text-to-video instead)")

            endpoint = model["i2v"] if still_url else model["endpoint"]
            args_ = {"prompt": prompt, "duration": dur_val}
            minimal = {"prompt": prompt, "duration": dur_val}
            if not still_url:
                # image-to-video takes its aspect from the still itself; passing one
                # is a validation error on several of these endpoints.
                args_["aspect_ratio"] = aspect
                minimal["aspect_ratio"] = aspect
            else:
                args_["image_url"] = still_url
                minimal["image_url"] = still_url
            if model.get("resolution"):
                args_["resolution"] = model["resolution"]
            if model.get("audio"):
                args_["generate_audio"] = False        # narration is added at assembly
            # send the anti-AI-gloss negative everywhere it might help; endpoints
            # that reject it get the minimal-args retry automatically
            args_["negative_prompt"] = (s.get("negative") or NEG)[:900]
            log(f"    {real:.0f}s on {endpoint}" + ("  · your product still" if still_url else ""))

            res, kind, detail = call_shot(fal_client, endpoint, args_, minimal, a.shot_timeout)
            if res is None and kind == "moderation":
                clean = sanitize_prompt(prompt)
                if clean != prompt:
                    log("    content filter rejected the prompt — retrying with a softened scene ...")
                    args2 = dict(minimal)
                    args2["prompt"] = clean
                    res, kind, detail = call_shot(fal_client, endpoint, args2, args2, a.shot_timeout)
            if res is None:
                log(f"    FAILED ({kind}): {detail[:200]}")
                failed.append(sid)
                failures.append({"shot": sid, "kind": kind, "detail": detail[:200]})
                continue

            v = res.get("video") or {}
            url = v.get("url") if isinstance(v, dict) else v
            if not url:
                log(f"    FAILED: no video in response: {str(res)[:180]}")
                failed.append(sid)
                failures.append({"shot": sid, "kind": "no_output", "detail": str(res)[:180]})
                continue
            spent += real * model["cost_per_s"]   # billed once fal rendered it
            with httpx.Client(follow_redirects=True, timeout=900) as c:
                r = c.get(url)
                r.raise_for_status()
                tmp = out.with_suffix(".part.mp4")
                tmp.write_bytes(r.content)
                tmp.replace(out)
            log(f"    OK {out.name} ({probe_dur(out):.1f}s) ~${real * model['cost_per_s']:.2f}")
            made.append({"shot_id": sid, "file": str(out), "duration_s": probe_dur(out)})
    finally:
        # actual money spent — machine-readable so the server ledgers the truth
        # even when the run failed halfway (it used to record $0 or the estimate)
        log("")
        log("SPENT: " + json.dumps({"usd": round(spent, 2), "made": len(made),
                                    "failed": len(failed)}))
        (work / "generated.json").write_text(json.dumps({
            "batch": work.name, "generated": made, "failed": failed,
            "failures": failures, "spent_usd": round(spent, 2),
            "motion_engine": f"fal-t2v:{a.model}", "style_mode": "text-to-video",
            "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, indent=1), encoding="utf-8")

    log(f"Done: {len(made)} clip(s) made, {len(failed)} failed"
        f"{' (' + ', '.join(failed) + ')' if failed else ''} — ~${spent:.2f} on fal.ai")
    total = len(made) + len(failed)
    if not made:
        die("no clips were generated")
    if total and len(made) / total < a.min_success:
        kinds = ", ".join(sorted({f['kind'] for f in failures})) or "unknown"
        print(f"ERROR: only {len(made)}/{total} shots generated ({kinds}) — not assembling a "
              f"partial video. Finished clips are cached; re-run to retry just the failed shots.",
              flush=True)
        sys.exit(3)
    if failed:
        log(f"⚠ PARTIAL: {len(failed)} shot(s) missing — assembling with {len(made)}/{total}. "
            "Re-run to fill the gaps (finished clips are cached).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
