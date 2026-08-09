#!/usr/bin/env python3
"""Run one generative model for the timeline editor and write a clip to disk.

    python seq_generate.py --model wan-2.7-edit --prompt "make it night, neon"
        --video work/clip.mp4 --out output/aiclips/foo --name foo
        --refs a.jpg,b.jpg --resolution 720p --seconds 6 --env-file <autoVSL>/.env

Runs under the cv venv (fal_client + httpx). Prints `RESULT: <abs path>` on the
last line so the caller can pick the file up without guessing.

The balance probe up front matters: starting a paid render that dies halfway
still costs money and leaves nothing behind.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ai_models import MODELS, estimate  # noqa: E402


def load_env(path: Path) -> None:
    if not path or not Path(path).is_file():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def build_args(m: dict, a, video_url, image_url, ref_urls) -> dict:
    """Only send fields the endpoint actually declares — fal rejects extras."""
    args: dict = {"prompt": a.prompt}
    mode = m["mode"]

    if mode == "v2v":
        args["video_url"] = video_url
        if m["endpoint"].startswith("google/"):
            return args                      # gemini edit takes prompt + video only
        if m["resolutions"]:
            args["resolution"] = a.resolution
        if ref_urls and m["refs"]:
            if "happy-horse" in m["endpoint"]:
                args["reference_image_urls"] = ref_urls[: m["refs"]]
            else:
                args["reference_image_url"] = ref_urls[0]
        if "wan" in m["endpoint"]:
            args["audio_setting"] = "origin"  # keep the clip's own audio bed
    elif mode == "i2v":
        args["image_url"] = image_url
        if m["resolutions"]:
            args["resolution"] = a.resolution
        if a.seconds:
            args["duration"] = (str(int(a.seconds)) if "seedance" in m["endpoint"]
                                else int(a.seconds))
    elif mode == "ref2v":
        args["reference_image_urls"] = ref_urls[: m["refs"]] or ([image_url] if image_url else [])
        if m["resolutions"]:
            args["resolution"] = a.resolution
        if a.seconds:
            args["duration"] = (str(int(a.seconds)) if "seedance" in m["endpoint"]
                                else int(a.seconds))
    else:                                     # t2v
        if m["resolutions"]:
            args["resolution"] = a.resolution
        if a.seconds:
            args["duration"] = (str(int(a.seconds)) if "seedance" in m["endpoint"]
                                else int(a.seconds))
    return args


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--prompt", default="")
    ap.add_argument("--video", default="")
    ap.add_argument("--image", default="")
    ap.add_argument("--refs", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", default="clip")
    ap.add_argument("--resolution", default="720p")
    ap.add_argument("--seconds", type=float, default=0)
    ap.add_argument("--env-file", default="")
    a = ap.parse_args()

    if a.model not in MODELS:
        print(f"ERROR: unknown model {a.model!r}", flush=True)
        return 1
    m = MODELS[a.model]

    load_env(Path(a.env_file)) if a.env_file else None
    if not os.environ.get("FAL_KEY"):
        print("ERROR: FAL_KEY not set — add it to autoVSL/.env", flush=True)
        return 1

    import fal_client
    import httpx

    est = estimate(a.model, a.seconds or 5, a.resolution)
    print(f"model  {m['label']}  ({m['endpoint']})", flush=True)
    print(f"cost   {est['summary']}", flush=True)
    if not est["verified"]:
        print("       NOTE: fal published no rate for this endpoint — the estimate "
              "above is inferred. The invoice is authoritative.", flush=True)

    try:                                     # fail before spending, not during
        fal_client.upload(b"ok", "text/plain")
    except Exception as exc:
        msg = str(exc).lower()
        if any(w in msg for w in ("403", "locked", "balance", "exhaust", "unauthor")):
            print(f"ERROR: fal.ai account problem (balance / FAL_KEY): {exc}", flush=True)
            return 1
        print(f"[warn] balance probe inconclusive: {exc}", flush=True)

    video_url = image_url = None
    ref_urls: list[str] = []
    if a.video:
        print("uploading source clip…", flush=True)
        video_url = fal_client.upload_file(str(Path(a.video).resolve()))
    if a.image:
        print("uploading image…", flush=True)
        image_url = fal_client.upload_file(str(Path(a.image).resolve()))
    for r in [x for x in a.refs.split(",") if x.strip()]:
        p = Path(r.strip()).resolve()
        if p.is_file():
            print(f"uploading reference {p.name}…", flush=True)
            ref_urls.append(fal_client.upload_file(str(p)))

    args = build_args(m, a, video_url, image_url, ref_urls)
    print("arguments: " + json.dumps({k: (v[:60] + "…" if isinstance(v, str) and len(v) > 60 else v)
                                      for k, v in args.items()}), flush=True)

    try:
        res = fal_client.subscribe(m["endpoint"], arguments=args, with_logs=False)
    except Exception as exc:
        print(f"ERROR: fal call failed: {exc}", flush=True)
        return 1

    url = None
    if isinstance(res, dict):
        v = res.get("video") or res.get("videos") or res.get("output")
        if isinstance(v, dict):
            url = v.get("url")
        elif isinstance(v, list) and v:
            url = v[0].get("url") if isinstance(v[0], dict) else v[0]
        elif isinstance(v, str):
            url = v
        url = url or res.get("url")
    if not url:
        print(f"ERROR: no video in response: {str(res)[:400]}", flush=True)
        return 1

    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{a.name}.mp4"
    print(f"downloading {url[:80]}…", flush=True)
    with httpx.Client(follow_redirects=True, timeout=900) as c:
        with c.stream("GET", url) as r:
            r.raise_for_status()
            with open(out_file, "wb") as fh:
                for chunk in r.iter_bytes(1 << 16):
                    fh.write(chunk)

    if not out_file.is_file() or out_file.stat().st_size < 10000:
        print("ERROR: downloaded file is empty or truncated", flush=True)
        return 1

    (out_dir / f"{a.name}.json").write_text(json.dumps({
        "model": a.model, "endpoint": m["endpoint"], "prompt": a.prompt,
        "estimate": est, "arguments": {k: v for k, v in args.items() if k != "prompt"},
    }, indent=1), encoding="utf-8")

    print(f"size   {out_file.stat().st_size/1048576:.1f} MB", flush=True)
    print(f"RESULT: {out_file.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
