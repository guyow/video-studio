#!/usr/bin/env python3
"""Generative video models available to the timeline editor.

Every endpoint id and every price below was read from fal's live registry and
OpenAPI on 2026-08-08 — not from memory. Where fal published no price override
for an endpoint, `price_verified` is False and the rate is an inference from a
sibling endpoint in the same family; the UI must label those as unconfirmed.
fal's invoice is always the authority.

Modes:
    v2v    edit an existing clip   (video_url + prompt)     -> the "Aleph" job
    i2v    a still becomes motion  (image_url + prompt)
    ref2v  reference images steer a new shot
    t2v    pure text to video
"""
from __future__ import annotations

MODELS: dict[str, dict] = {

    # ── video-to-video: the clip op. Send a clip, describe the change. ──
    "gemini-omni-edit": {
        "label": "Gemini Omni Flash — Edit",
        "endpoint": "google/gemini-omni-flash/edit",
        "mode": "v2v", "usd_per_sec": 0.13, "price_verified": True,
        "resolutions": [], "refs": 0, "max_sec": 30,
        "note": "Prompt + video only. Cheapest true edit model. Google, added 2026-06-30.",
    },
    "wan-2.7-edit": {
        "label": "Wan 2.7 — Edit video",
        "endpoint": "fal-ai/wan/v2.7/edit-video",
        "mode": "v2v", "usd_per_sec": 0.10, "price_verified": False,
        "resolutions": ["720p", "1080p"], "refs": 1, "max_sec": 10,
        "note": "Takes one reference image + audio_setting. Price inferred from Wan 2.7 i2v "
                "($0.10/s 720p) — fal published no rate for this endpoint.",
    },
    "happy-horse-edit": {
        "label": "Happy Horse — Video edit",
        "endpoint": "alibaba/happy-horse/video-edit",
        "mode": "v2v", "usd_per_sec": 0.28, "price_verified": True,
        "resolutions": ["720p", "1080p"], "refs": 4, "max_sec": 15,
        "note": "Multiple reference images. Billed input+output ($0.14 each); 1080p is $0.56/s.",
    },

    # ── image-to-video: an inspiration still becomes a shot ──
    "wan-2.7-i2v": {
        "label": "Wan 2.7 — Image to video",
        "endpoint": "fal-ai/wan/v2.7/image-to-video",
        "mode": "i2v", "usd_per_sec": 0.10, "price_verified": True,
        "resolutions": ["720p", "1080p"], "refs": 0, "max_sec": 10,
        "note": "Cheapest of the current generation.",
    },
    "happy-horse-i2v": {
        "label": "Happy Horse 1.1 — Image to video",
        "endpoint": "alibaba/happy-horse/v1.1/image-to-video",
        "mode": "i2v", "usd_per_sec": 0.14, "price_verified": True,
        "resolutions": ["720p", "1080p"], "refs": 0, "max_sec": 15,
        "note": "3-15s. 1080p is $0.18/s.",
    },
    "gemini-omni-i2v": {
        "label": "Gemini Omni Flash — Image to video",
        "endpoint": "google/gemini-omni-flash/image-to-video",
        "mode": "i2v", "usd_per_sec": 0.13, "price_verified": True,
        "resolutions": [], "refs": 0, "max_sec": 30,
        "note": "Token-billed; ~$0.13/s at 720p.",
    },
    "seedance-2.0-fast-i2v": {
        "label": "Seedance 2.0 Fast — Image to video",
        "endpoint": "bytedance/seedance-2.0/fast/image-to-video",
        "mode": "i2v", "usd_per_sec": 0.2419, "price_verified": True,
        "resolutions": ["720p"], "refs": 0, "max_sec": 15,
        "note": "The cheaper half of Seedance 2.0.",
    },
    "seedance-2.0-i2v": {
        "label": "Seedance 2.0 — Image to video",
        "endpoint": "bytedance/seedance-2.0/image-to-video",
        "mode": "i2v", "usd_per_sec": 0.3034, "price_verified": True,
        "resolutions": ["480p", "720p", "1080p", "4k"], "refs": 0, "max_sec": 15,
        "note": "1080p jumps to $0.682/s — a 15s 1080p shot is ~$10.",
    },
    "seedance-2.5-i2v": {
        "label": "Seedance 2.5 — Image to video (newest)",
        "endpoint": "bytedance/seedance-2.5/image-to-video",
        "mode": "i2v", "usd_per_sec": 0.473, "price_verified": True,
        "resolutions": ["480p", "720p"], "refs": 0, "max_sec": 30,
        "note": "Newer than 2.0 and dearer. 480p is ~$0.22/s.",
    },

    # ── reference-to-video: inspiration images steer a fresh shot ──
    "seedance-2.5-ref": {
        "label": "Seedance 2.5 — Reference to video",
        "endpoint": "bytedance/seedance-2.5/reference-to-video",
        "mode": "ref2v", "usd_per_sec": 0.473, "price_verified": True,
        "resolutions": ["480p", "720p"], "refs": 50, "max_sec": 30,
        "note": "Up to 50 references; holds character/set/palette across a 30s take.",
    },
    "wan-2.7-ref": {
        "label": "Wan 2.7 — Reference to video",
        "endpoint": "fal-ai/wan/v2.7/reference-to-video",
        "mode": "ref2v", "usd_per_sec": 0.10, "price_verified": False,
        "resolutions": ["720p", "1080p"], "refs": 4, "max_sec": 10,
        "note": "Price inferred from Wan 2.7 i2v.",
    },
    "happy-horse-ref": {
        "label": "Happy Horse 1.1 — Reference to video",
        "endpoint": "alibaba/happy-horse/v1.1/reference-to-video",
        "mode": "ref2v", "usd_per_sec": 0.14, "price_verified": True,
        "resolutions": ["720p", "1080p"], "refs": 4, "max_sec": 15,
        "note": "",
    },

    # ── text-to-video ──
    "wan-2.7-t2v": {
        "label": "Wan 2.7 — Text to video",
        "endpoint": "fal-ai/wan/v2.7/text-to-video",
        "mode": "t2v", "usd_per_sec": 0.10, "price_verified": True,
        "resolutions": ["720p", "1080p"], "refs": 0, "max_sec": 10,
        "note": "Cheapest text-to-video here.",
    },
    "seedance-2.0-fast-t2v": {
        "label": "Seedance 2.0 Fast — Text to video",
        "endpoint": "bytedance/seedance-2.0/fast/text-to-video",
        "mode": "t2v", "usd_per_sec": 0.2419, "price_verified": True,
        "resolutions": ["720p"], "refs": 0, "max_sec": 15,
        "note": "",
    },
    "seedance-2.0-t2v": {
        "label": "Seedance 2.0 — Text to video",
        "endpoint": "bytedance/seedance-2.0/text-to-video",
        "mode": "t2v", "usd_per_sec": 0.3034, "price_verified": True,
        "resolutions": ["480p", "720p", "1080p", "4k"], "refs": 0, "max_sec": 15,
        "note": "",
    },
    "happy-horse-t2v": {
        "label": "Happy Horse 1.1 — Text to video",
        "endpoint": "alibaba/happy-horse/v1.1/text-to-video",
        "mode": "t2v", "usd_per_sec": 0.14, "price_verified": True,
        "resolutions": ["720p", "1080p"], "refs": 0, "max_sec": 15,
        "note": "",
    },
}

# 1080p costs meaningfully more on the families that publish a second rate.
HD_MULTIPLIER = {
    "happy-horse-edit": 2.0, "happy-horse-i2v": 1.286, "happy-horse-ref": 1.286,
    "happy-horse-t2v": 1.286, "wan-2.7-i2v": 1.5, "wan-2.7-t2v": 1.5,
    "wan-2.7-edit": 1.5, "wan-2.7-ref": 1.5,
    "seedance-2.0-i2v": 2.248, "seedance-2.0-t2v": 2.248,
}


def estimate(model_key: str, seconds: float, resolution: str = "720p") -> dict:
    """Rough USD for a run. Labelled an estimate everywhere it is shown."""
    m = MODELS[model_key]
    rate = float(m["usd_per_sec"])
    if resolution == "1080p":
        rate *= HD_MULTIPLIER.get(model_key, 1.0)
    total = round(rate * max(0.0, float(seconds)), 3)
    return {
        "usd": total, "rate": round(rate, 4), "seconds": round(float(seconds), 2),
        "resolution": resolution, "model": model_key, "endpoint": m["endpoint"],
        "verified": bool(m["price_verified"]),
        "summary": f"{m['label']} · {seconds:.1f}s @ {resolution} ≈ ${total:.2f}"
                   + ("" if m["price_verified"] else "  (rate UNCONFIRMED)"),
    }


def public_list() -> list[dict]:
    out = []
    for k, m in MODELS.items():
        out.append({"key": k, "label": m["label"], "mode": m["mode"],
                    "endpoint": m["endpoint"], "usd_per_sec": m["usd_per_sec"],
                    "price_verified": m["price_verified"], "refs": m["refs"],
                    "resolutions": m["resolutions"], "max_sec": m["max_sec"],
                    "hd_mult": HD_MULTIPLIER.get(k, 1.0), "note": m["note"]})
    out.sort(key=lambda x: (x["mode"], x["usd_per_sec"]))
    return out
