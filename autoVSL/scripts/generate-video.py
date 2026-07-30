#!/usr/bin/env python3
"""Generate VSL video shots via fal.ai API (cheapest: Wan 2.2, NOT Kling)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Pricing per video-second (fal.ai, March 2026)
# "family" controls which argument schema generate_shot() sends —
# fal endpoints from different vendors accept different fields.
# Seedance pricing is token-based ($1/M tokens, tokens = h*w*24fps*sec/1024);
# cost_per_sec below is the converted equivalent.
MODELS = {
    "seedance-480p": {
        "endpoint": "fal-ai/bytedance/seedance/v1/pro/fast/text-to-video",
        "cost_per_sec": 0.010,  # ~$0.05/clip (5s)
        "resolution": "480p",
        "family": "seedance",
        "duration": 5,
        "note": "Seedance Pro Fast — cheapest usable clip on fal",
    },
    "seedance-720p": {
        "endpoint": "fal-ai/bytedance/seedance/v1/pro/fast/text-to-video",
        "cost_per_sec": 0.022,  # ~$0.11/clip (5s)
        "resolution": "720p",
        "family": "seedance",
        "duration": 5,
        "note": "Seedance Pro Fast 720p — best quality-per-dollar",
    },
    "seedance-1080p": {
        "endpoint": "fal-ai/bytedance/seedance/v1/pro/fast/text-to-video",
        "cost_per_sec": 0.049,  # ~$0.24/clip (5s)
        "resolution": "1080p",
        "family": "seedance",
        "duration": 5,
        "note": "Seedance Pro Fast 1080p — premium quality, still cheap",
    },
    "wan-5b-720p": {
        "endpoint": "fal-ai/wan/v2.2-5b/text-to-video",
        "cost_per_sec": 0.03,  # flat $0.15/clip (5s)
        "resolution": "720p",
        "family": "wan",
        "duration": 5,
        "note": "Cheapest 720p — $0.15/clip flat, smaller model",
    },
    "wan-480p": {
        "endpoint": "fal-ai/wan/v2.2-a14b/text-to-video",
        "cost_per_sec": 0.04,
        "resolution": "480p",
        "family": "wan",
        "duration": 5,
        "note": "Cheapest draft quality",
    },
    "hailuo-768p": {
        "endpoint": "fal-ai/minimax/hailuo-02/standard/text-to-video",
        "cost_per_sec": 0.045,
        "resolution": "768p",
        "family": "hailuo",
        "duration": 6,  # Hailuo minimum is 6s
        "note": "Hailuo 02 — great quality, but 16:9 only (not vertical)",
    },
    "wan-580p": {
        "endpoint": "fal-ai/wan/v2.2-a14b/text-to-video",
        "cost_per_sec": 0.06,
        "resolution": "580p",
        "family": "wan",
        "duration": 5,
        "note": "Balanced quality/cost",
    },
    "kling-turbo": {
        "endpoint": "fal-ai/kling-video/v2.5-turbo/pro/text-to-video",
        "cost_per_sec": 0.07,
        "resolution": "720p",
        "family": "kling",
        "duration": 5,
        "note": "Kling 2.5 Turbo — best motion for the price",
    },
    "wan-720p": {
        "endpoint": "fal-ai/wan/v2.2-a14b/text-to-video",
        "cost_per_sec": 0.08,
        "resolution": "720p",
        "family": "wan",
        "duration": 5,
        "note": "Best Wan quality on fal",
    },
}

# Swap the default without touching code: set VSL_MODEL in .env
# (e.g. VSL_MODEL=kling-turbo), or pass --model on the command line.
FALLBACK_MODEL = "seedance-720p"
DURATION_SEC = 5  # ~81 frames at 16fps
NUM_FRAMES = 81


def load_env() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


def estimate_cost(model_key: str, num_shots: int) -> float:
    m = MODELS[model_key]
    return m["cost_per_sec"] * m["duration"] * num_shots


def generate_shot(
    model: dict,
    prompt: str,
    negative_prompt: str,
    aspect_ratio: str,
) -> str:
    import fal_client

    family = model["family"]
    if family == "wan":
        arguments = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "resolution": model["resolution"],
            "aspect_ratio": aspect_ratio,
            "num_frames": NUM_FRAMES,
            "frames_per_second": 16,
            "enable_safety_checker": False,
            "enable_output_safety_checker": False,
        }
    elif family == "kling":
        arguments = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "duration": str(model["duration"]),
            "aspect_ratio": aspect_ratio,
        }
    elif family == "hailuo":
        arguments = {
            "prompt": prompt,
            "duration": str(model["duration"]),
            "prompt_optimizer": True,
        }
    elif family == "seedance":
        arguments = {
            "prompt": prompt,
            "resolution": model["resolution"],
            "aspect_ratio": aspect_ratio,
            "duration": model["duration"],
            "enable_safety_checker": False,
        }
    else:
        raise ValueError(f"Unknown model family: {family}")

    result = fal_client.subscribe(
        model["endpoint"],
        arguments=arguments,
        with_logs=True,
    )
    return result["video"]["url"]


def download(url: str, dest: Path) -> None:
    import httpx

    with httpx.Client(follow_redirects=True, timeout=120) as client:
        resp = client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)


def main() -> int:
    load_env()  # early, so VSL_MODEL from .env can set the default
    default_model = os.environ.get("VSL_MODEL", FALLBACK_MODEL)
    if default_model not in MODELS:
        print(f"VSL_MODEL={default_model} is not a known preset, using {FALLBACK_MODEL}", file=sys.stderr)
        default_model = FALLBACK_MODEL

    parser = argparse.ArgumentParser(
        description="Generate VSL video shots via fal.ai (Wan = cheapest, not Kling)"
    )
    parser.add_argument("slug", nargs="?", default="fairy-flame")
    parser.add_argument(
        "--model",
        choices=list(MODELS.keys()),
        default=default_model,
        help=f"Model preset (default: {default_model}; set VSL_MODEL in .env to change)",
    )
    parser.add_argument("--shot", type=int, help="Generate only this shot number (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Show cost estimate, don't generate")
    parser.add_argument("--list-models", action="store_true", help="Show model pricing")
    args = parser.parse_args()

    if args.list_models:
        print("Model pricing (full VSL = 8 shots):\n")
        for key, m in MODELS.items():
            total = estimate_cost(key, 8)
            per_clip = m["cost_per_sec"] * m["duration"]
            marker = " *default*" if key == default_model else ""
            print(
                f"  {key:14} ${per_clip:.2f}/clip ({m['duration']}s)  → 8 shots ≈ ${total:.2f}  ({m['note']}){marker}"
            )
        print("\nFree option: fal.ai signup credits (test 1-3 clips free)")
        print("Truly free: Hailuo website manual tier (~3 clips/day, no API)")
        return 0

    root = Path(__file__).resolve().parent.parent
    shots_file = root / "vsls" / args.slug / "kling-shots.json"
    out_dir = root / "vsls" / args.slug / "media" / "video"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not shots_file.exists():
        print(f"Missing {shots_file}", file=sys.stderr)
        return 1

    with shots_file.open() as f:
        data = json.load(f)

    settings = data["settings"]
    shots = data["shots"]
    if args.shot:
        shots = [s for s in shots if s["id"] == args.shot]
        if not shots:
            print(f"Shot {args.shot} not found", file=sys.stderr)
            return 1

    model = MODELS[args.model]
    cost = estimate_cost(args.model, len(shots))

    print(f"VSL: {args.slug}")
    print(f"Model: {args.model} ({model['note']})")
    print(f"Shots: {len(shots)} × {model['duration']}s = ~${cost:.2f}\n")

    if args.dry_run:
        for s in shots:
            print(f"  shot-{s['id']:02d} → {s['filename']}  ${model['cost_per_sec'] * model['duration']:.2f}")
        print(f"\nTotal: ~${cost:.2f}")
        print("Run without --dry-run to generate (needs FAL_KEY)")
        return 0

    if not os.environ.get("FAL_KEY"):
        print("Set FAL_KEY in .env or environment.", file=sys.stderr)
        print("Get free credits: https://fal.ai/dashboard/keys", file=sys.stderr)
        return 1

    os.environ["FAL_KEY"] = os.environ["FAL_KEY"]  # fal-client reads this

    for shot in shots:
        dest = out_dir / shot["filename"]
        if dest.exists():
            print(f"  shot-{shot['id']:02d} … skip (exists)")
            continue

        print(f"  shot-{shot['id']:02d} … generating", end="", flush=True)
        try:
            url = generate_shot(
                model,
                shot["prompt"],
                settings.get("negative_prompt", ""),
                settings.get("aspect_ratio", "9:16"),
            )
            download(url, dest)
            print(f" → {shot['filename']}")
        except Exception as e:
            print(f" FAILED: {e}", file=sys.stderr)
            return 1

    print(f"\n✓ Done. Files in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
