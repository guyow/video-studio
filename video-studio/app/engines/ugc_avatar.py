#!/usr/bin/env python3
"""UGC Factory: one avatar image -> a talking UGC clip on Google Veo 3 (fal.ai).

The avatar photo becomes the person on screen; Veo 3's native audio makes them
SAY the script (lip-synced), in the voice/emotion the user picked, inside a
scene template (selfie / selling / podcast / car / mirror / stream / static).
Optionally a product photo is first merged into the avatar image with
nano-banana edit, so the person holds the user's REAL product.

DURATION FOLLOWS THE WORDS. Veo 3 renders at most 8s per call, so a longer
script is chained: the script is split across 8s segments, each segment is
seeded with the LAST FRAME of the one before it (the i2v_gen.py trick), and
the segments are stitched into one continuous take.

With --voice-ref (a Voice-Bank ref.wav), Veo's own audio stays off — the person
mouths the words on camera and the script is spoken locally by XTTS in the
cloned voice (free), then muxed on. For chained clips this also keeps the voice
perfectly consistent across segments.

Endpoints + parameter shapes follow t2v_fal.py / product_still.py / i2v_gen.py
(verified against fal's live OpenAPI schemas). Costs are ESTIMATES.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# every fal model with an image->video endpoint (mirrors t2v_fal.py; endpoints
# verified against fal's live OpenAPI schemas).
#   audio: the model generates NATIVE speech — the avatar talks by itself.
#          Silent models still mouth the words; pair them with a Voice-Bank
#          voice (XTTS, free) or run them with voice="none".
#   dur:   how the endpoint wants duration — "str" ('5'), "strs" ('8s'), "int" (8)
#   durations: the segment lengths the endpoint accepts (chaining covers the rest)
MODELS = {
    "veo3-fast": {"label": "Google Veo 3 Fast — 720P · speaks (recommended)",
                  "endpoint": "fal-ai/veo3/fast/image-to-video", "audio": True,
                  "dur": "strs", "durations": (4, 6, 8),
                  "resolution": "720p", "cost_per_s": 0.15},
    "veo3":      {"label": "Google Veo 3 — 1080P · speaks (premium)",
                  "endpoint": "fal-ai/veo3/image-to-video", "audio": True,
                  "dur": "strs", "durations": (4, 6, 8),
                  "resolution": "1080p", "cost_per_s": 0.40},
    "sora-2":    {"label": "Sora 2 — OpenAI realism · speaks",
                  "endpoint": "fal-ai/sora-2/image-to-video", "audio": True,
                  "dur": "int", "durations": (4, 8, 12),
                  "resolution": "720p", "cost_per_s": 0.30},
    "kling-2.6-pro": {"label": "Kling 2.6 Pro — native voice at half Veo's price · speaks",
                      "endpoint": "fal-ai/kling-video/v2.6/pro/image-to-video", "audio": True,
                      "dur": "str", "durations": (5, 10), "img_key": "start_image_url",
                      "cost_per_s": 0.14, "cost_per_s_silent": 0.07},
    "kling-2.5-pro":    {"label": "Kling 2.5 Turbo Pro — great motion · silent",
                         "endpoint": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
                         "audio": False, "dur": "str", "durations": (5, 10), "cost_per_s": 0.07},
    "kling-2.1-master": {"label": "Kling 2.1 Master — cinematic · silent",
                         "endpoint": "fal-ai/kling-video/v2.1/master/image-to-video",
                         "audio": False, "dur": "str", "durations": (5, 10), "cost_per_s": 0.09},
    "seedance-pro": {"label": "Seedance 1.0 Pro — 1080P flexible · silent",
                     "endpoint": "fal-ai/bytedance/seedance/v1/pro/image-to-video",
                     "audio": False, "dur": "str", "durations": tuple(range(2, 13)),
                     "resolution": "1080p", "cost_per_s": 0.12},
    "hailuo-02": {"label": "MiniMax Hailuo 02 — lively, cheap · silent",
                  "endpoint": "fal-ai/minimax/hailuo-02/standard/image-to-video",
                  "audio": False, "dur": "str", "durations": (6, 10), "cost_per_s": 0.05},
    "wan-2.2":   {"label": "Wan 2.2 — budget · silent",
                  "endpoint": "fal-ai/wan/v2.2-a14b/image-to-video",
                  "audio": False, "dur": "str", "durations": (5,), "cost_per_s": 0.04},
}
MERGE = {"endpoint": "fal-ai/nano-banana/edit", "cost": 0.039}
NEG = "blurry, distorted, warped face, low quality, watermark, text artifacts"
WPS = 2.3            # comfortable spoken pace, words per second
MAX_SECONDS = 120    # chaining cap — no script is too long, it just chains more

TEMPLATES = {
    "selfie":  "Casual selfie video: the person holds the phone at arm's length, front-camera "
               "framing, slight natural handheld movement, everyday setting",
    "selling": "UGC creator ad: the person pitches to camera like a friend recommending a find, "
               "holding the product up to the lens when they mention it, handheld phone framing",
    "podcast": "Podcast studio: the person talks into a large desktop microphone, wearing "
               "headphones, moody studio lighting, side-on podcast framing",
    "car":     "Car talk video: the person sits in the driver's seat of a parked car talking to "
               "a phone mounted on the dashboard, daylight through the windows",
    "mirror":  "Mirror video: the person films themselves in a bedroom or bathroom mirror, phone "
               "visible in their hand, casual at-home look",
    "stream":  "Live-stream setup: webcam framing at a desk, ring-light glow, streamer room with "
               "LED accents in the background",
    "static":  "Static tripod shot: fixed camera, the person stands centred and talks straight "
               "into the lens, clean stable framing",
}

# cloud lip-sync tiers (fal.ai) — endpoints + prices proven in autoVSL's
# scripts/script-swap.py (the pipeline behind the winning dub ads)
LIPSYNC = {
    "sync":       {"endpoint": "fal-ai/sync-lipsync/v2",     "cost_per_s": 0.05,
                   "label": "sync.so v2"},
    "pro":        {"endpoint": "fal-ai/sync-lipsync/v2/pro", "cost_per_s": 0.10,
                   "label": "sync.so v2 pro"},
    "sync3":      {"endpoint": "fal-ai/sync-lipsync/v3",     "cost_per_s": 0.1333,
                   "label": "sync.so v3"},
    "veed":       {"endpoint": "veed/lipsync",               "cost_per_s": 0.0067,
                   "label": "VEED"},
    "latentsync": {"endpoint": "fal-ai/latentsync",          "cost_per_s": 0.005,
                   "label": "LatentSync"},
}

VOICES = {
    "auto":    "",
    "whisper": "in a hushed, intimate whisper",
    "rough":   "in a slightly rough, textured voice",
    "harsh":   "in a harsh, gravelly voice",
    "soft":    "in a soft, clear, gentle voice",
    "low":     "in a low, strong, deep voice",
    "high":    "in a very high-pitched voice",
    "none":    "",
}

EMOTIONS = {
    "auto": "", "happy": "happy and upbeat", "angry": "angry and intense",
    "fearful": "fearful and anxious", "surprised": "surprised and amazed",
    "disgusted": "disgusted and put off", "excited": "excited and energetic",
    "calm": "calm and relaxed", "playful": "playful and cheeky",
    "serious": "serious and matter-of-fact",
}


def log(m: str) -> None:
    print(m, flush=True)


def die(m: str) -> None:
    print(f"ERROR: {m}", flush=True)
    sys.exit(1)


def ffmpeg(args: list[str], label: str) -> None:
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        die(f"{label} failed: {(r.stderr or '')[-260:]}")


def probe_dur(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    try:
        return float((r.stdout or "0").strip() or 0)
    except ValueError:
        return 0.0


def has_audio(p: Path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return "audio" in (r.stdout or "")


def load_env(path: Path | None) -> None:
    if path and path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def plan_segments(seconds: int, durations: tuple[int, ...]) -> list[int]:
    """Cover `seconds` with the model's longest segments plus one allowed tail."""
    durs = sorted(durations)
    seg_max = durs[-1]
    seconds = max(durs[0], min(MAX_SECONDS, seconds))
    segs: list[int] = []
    left = seconds
    while left > 0:
        if left >= seg_max:
            segs.append(seg_max)
            left -= seg_max
        else:
            segs.append(next((s for s in durs if s >= left), seg_max))
            left = 0
    return segs


def fmt_duration(model: dict, n: int):
    if model["dur"] == "int":
        return int(n)
    if model["dur"] == "strs":
        return f"{n}s"
    return str(n)


def seconds_for_words(n_words: int) -> int:
    return max(2, min(MAX_SECONDS, math.ceil(n_words / WPS)))


def split_script(script: str, segs: list[int]) -> list[str]:
    """Split the script across segments, proportional to each segment's length,
    breaking on sentence boundaries where possible."""
    if len(segs) == 1:
        return [script]
    sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", script) if s.strip()]
    if len(sentences) < len(segs):                      # fall back to word chunks
        words = script.split()
        sentences = [" ".join(words[i:i + 12]) for i in range(0, len(words), 12)]
    total_s = sum(segs)
    total_w = sum(len(s.split()) for s in sentences)
    chunks: list[str] = []
    si = 0
    for gi, seg in enumerate(segs):
        want = total_w * seg / total_s
        got: list[str] = []
        n = 0
        while si < len(sentences) and (n < want * 0.8 or not got):
            if gi < len(segs) - 1 and got and n + len(sentences[si].split()) > want * 1.3:
                break
            got.append(sentences[si])
            n += len(sentences[si].split())
            si += 1
        chunks.append(" ".join(got))
    if si < len(sentences):                             # leftovers ride the last chunk
        chunks[-1] = (chunks[-1] + " " + " ".join(sentences[si:])).strip()
    return chunks


def build_prompt(a: argparse.Namespace, say: str, speak: bool, cont: bool) -> str:
    parts = [TEMPLATES.get(a.template, TEMPLATES["selfie"]) + "."]
    if cont:
        parts.append("Direct continuation of the same continuous take: the SAME person, same "
                     "outfit, same setting, same lighting, same camera framing — no cut, no "
                     "scene change.")
    if a.action.strip():
        parts.append(a.action.strip().rstrip(".") + ".")
    if not speak or not say.strip():
        parts.append("The person does not speak.")
    else:
        text = say.strip().replace('"', "'")
        voice = VOICES.get(a.voice, "")
        emo = EMOTIONS.get(a.emotion, "")
        line = f'The person looks at the camera and says{" " + voice if voice else ""}: "{text}"'
        if emo:
            line += f" — sounding {emo}"
        parts.append(line + ". Natural, accurate lip-sync to the spoken words.")
    if a.bg.strip():
        parts.append(f"Background sound: {a.bg.strip().rstrip('.')}.")
    parts.append("Realistic UGC quality. No subtitles, no captions, no on-screen text, no watermark.")
    return " ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True, help="avatar photo (the person on screen)")
    ap.add_argument("--product", help="optional product photo to merge into their hands")
    ap.add_argument("--template", default="selfie", choices=sorted(TEMPLATES))
    ap.add_argument("--action", default="", help="how the character behaves")
    ap.add_argument("--script", default="", help="what the avatar says (audio text)")
    ap.add_argument("--voice", default="auto", choices=sorted(VOICES))
    ap.add_argument("--emotion", default="auto", choices=sorted(EMOTIONS))
    ap.add_argument("--bg", default="", help="background sound description")
    ap.add_argument("--model", default="veo3-fast", choices=sorted(MODELS))
    ap.add_argument("--seconds", default="auto",
                    help="'auto' = as long as the words need; or a number (2-120)")
    ap.add_argument("--out", required=True, help="output dir: output/i2v/<slug>")
    ap.add_argument("--name", required=True)
    ap.add_argument("--env-file", dest="env_file")
    ap.add_argument("--voice-ref", dest="voice_ref", help="Voice Bank ref.wav to clone")
    ap.add_argument("--ds-py", dest="ds_py", help="dubbing venv python")
    ap.add_argument("--ds-app", dest="ds_app", help="dubbing-studio/app.py")
    ap.add_argument("--lipsync", default="sync",
                    choices=sorted(LIPSYNC) + ["gfpgan", "fast", "none"],
                    help="with --voice-ref: regenerate the mouth to match the cloned "
                         "voice. sync/pro/sync3/veed/latentsync = cloud (fal.ai, paid); "
                         "gfpgan/fast = local Wav2Lip (free); none = keep Veo's mouth "
                         "and just swap the audio")
    a = ap.parse_args()

    avatar = Path(a.image).resolve()
    if not avatar.is_file():
        die(f"avatar image not found: {avatar}")
    product = Path(a.product).resolve() if a.product else None
    if product and not product.is_file():
        die(f"product image not found: {product}")
    voice_ref = Path(a.voice_ref).resolve() if a.voice_ref else None
    if voice_ref and not voice_ref.is_file():
        die(f"voice reference not found: {voice_ref}")
    if voice_ref and not (a.ds_py and a.ds_app):
        die("--voice-ref needs --ds-py and --ds-app (dubbing-studio) to speak the script")

    model = MODELS[a.model]
    n_words = len(a.script.split())
    if str(a.seconds).strip().lower() == "auto":
        seconds = seconds_for_words(n_words) if n_words else max(model["durations"])
    else:
        try:
            seconds = max(2, min(MAX_SECONDS, int(a.seconds)))
        except ValueError:
            die("--seconds must be 'auto' or a number")
    segs = plan_segments(seconds, model["durations"])
    speak = a.voice != "none"
    chunks = split_script(a.script, segs) if (speak and a.script.strip()) else ["" for _ in segs]
    gen_audio = speak and not voice_ref and model["audio"]
    if speak and a.script.strip() and not model["audio"] and not voice_ref:
        log("NOTE: this model films SILENT video — the person mouths the words but no sound "
            "is generated. Pick a Voice-Bank voice (free) or a speaking model (Veo 3 / Sora 2) "
            "for audible speech.")
    load_env(Path(a.env_file) if a.env_file else None)
    if not os.environ.get("FAL_KEY"):
        die("FAL_KEY not set — add it to autoVSL/.env")
    import fal_client
    import httpx

    try:
        fal_client.upload(b"ok", "text/plain")
    except Exception as exc:                                  # noqa: BLE001
        msg = str(exc).lower()
        if any(w in msg for w in ("403", "locked", "balance", "exhaust", "unauthor")):
            die(f"fal.ai account problem (check balance / FAL_KEY): {exc}")

    out_dir = Path(a.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"UGC Factory · {model['label']}")
    log(f"template: {a.template} · voice: {'bank' if voice_ref else a.voice} · "
        f"emotion: {a.emotion}")
    log(f"{n_words} words → {sum(segs)}s in {len(segs)} segment(s): "
        + " + ".join(f"{s}s" for s in segs))

    # 1 · optional: put the REAL product into the avatar's hands (nano-banana)
    start_img = avatar
    if product:
        log("\nmerging your product into the avatar shot (nano-banana edit) ...")
        try:
            urls = [fal_client.upload_file(str(avatar)), fal_client.upload_file(str(product))]
            res = fal_client.subscribe(MERGE["endpoint"], arguments={
                "prompt": "Put the product from the second image naturally into the person's "
                          "hand from the first image. Keep the person's face, hair, clothing, "
                          "pose and the background EXACTLY the same — photorealistic, the "
                          "product label stays sharp and readable.",
                "image_urls": urls, "num_images": 1, "output_format": "png"},
                with_logs=False)
            images = res.get("images") or []
            url = (images[0].get("url") if isinstance(images[0], dict) else images[0]) if images else None
            if not url:
                raise RuntimeError(f"no image returned: {str(res)[:160]}")
            start_img = out_dir / "start.png"
            with httpx.Client(follow_redirects=True, timeout=300) as c:
                r = c.get(url)
                r.raise_for_status()
                start_img.write_bytes(r.content)
            log(f"    OK start.png (~${MERGE['cost']:.3f})")
        except Exception as exc:                              # noqa: BLE001
            log(f"    merge failed ({str(exc)[:160]}) — continuing with the avatar photo alone")
            start_img = avatar

    # 2 · Veo 3 image->video, segment by segment (last frame seeds the next)
    seg_files: list[Path] = []
    seed = start_img
    for i, (seg_s, say) in enumerate(zip(segs, chunks), 1):
        prompt = build_prompt(a, say, speak, cont=i > 1)
        log(f"\n[{i}/{len(segs)}] {seg_s}s on {model['endpoint']}")
        if say:
            log(f"    says: {say[:90]}")
        try:
            img_url = fal_client.upload_file(str(seed))
        except Exception as exc:                              # noqa: BLE001
            die(f"could not upload the seed image to fal.ai: {str(exc)[:200]}")
        dur_val = fmt_duration(model, seg_s)
        img_key = model.get("img_key", "image_url")   # Kling 2.6 wants start_image_url
        args_ = {"prompt": prompt, img_key: img_url, "duration": dur_val}
        if model.get("resolution"):
            args_["resolution"] = model["resolution"]
        if model["audio"]:
            args_["generate_audio"] = gen_audio
        if a.model.startswith("kling"):
            args_["negative_prompt"] = NEG
        try:
            res = fal_client.subscribe(model["endpoint"], arguments=args_, with_logs=False)
        except Exception as exc:                              # noqa: BLE001
            msg = str(exc)
            if any(w in msg.lower() for w in ("422", "validation", "unprocessable",
                                              "extra", "not permitted", "unexpected")):
                log(f"    schema rejected the extras ({msg[:120]}) — retrying minimal ...")
                try:
                    res = fal_client.subscribe(model["endpoint"], arguments={
                        "prompt": prompt, img_key: img_url, "duration": dur_val},
                        with_logs=False)
                except Exception as exc2:                     # noqa: BLE001
                    die(f"segment {i} failed: {str(exc2)[:260]}")
            else:
                die(f"segment {i} failed: {msg[:260]}")
        v = res.get("video") or {}
        url = v.get("url") if isinstance(v, dict) else v
        if not url:
            die(f"segment {i}: no video in the response: {str(res)[:200]}")
        seg_path = out_dir / f"seg{i:02d}.mp4"
        with httpx.Client(follow_redirects=True, timeout=900) as c:
            r = c.get(url)
            r.raise_for_status()
            seg_path.write_bytes(r.content)
        log(f"    OK {seg_path.name} ({probe_dur(seg_path):.1f}s)")
        seg_files.append(seg_path)
        if i < len(segs):                       # last frame becomes the next seed
            seed = out_dir / f"seed{i:02d}.png"
            ffmpeg(["-sseof", "-0.2", "-i", str(seg_path), "-frames:v", "1",
                    "-q:v", "2", str(seed)], "extract last frame")

    # 3 · stitch the segments into one continuous take
    clip = out_dir / "clip.mp4"
    if len(seg_files) == 1:
        seg_files[0].replace(clip)
    else:
        with_audio = gen_audio and all(has_audio(p) for p in seg_files)
        inputs: list[str] = []
        for p in seg_files:
            inputs += ["-i", str(p)]
        n = len(seg_files)
        if with_audio:
            filt = ("".join(f"[{i}:v][{i}:a]" for i in range(n))
                    + f"concat=n={n}:v=1:a=1[v][aud]")
            ffmpeg([*inputs, "-filter_complex", filt, "-map", "[v]", "-map", "[aud]",
                    "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30",
                    "-c:a", "aac", "-b:a", "192k", str(clip)], "stitch segments")
        else:
            filt = "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
            ffmpeg([*inputs, "-filter_complex", filt, "-map", "[v]",
                    "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30",
                    str(clip)], "stitch segments")
        for p in seg_files:
            p.unlink(missing_ok=True)
        log(f"\nstitched {n} segments → clip.mp4")
    dur = probe_dur(clip)

    # 4 · optional: speak the FULL script in the Voice-Bank voice (XTTS, free),
    #     then regenerate the MOUTH to match it (Wav2Lip + GFPGAN, local GPU, free)
    if voice_ref and a.script.strip():
        log(f"\nre-voicing with your saved voice (XTTS clone from {voice_ref.parent.name}) ...")
        try:
            so = subprocess.run(
                [a.ds_py, a.ds_app, "--cli", "--video", str(clip),
                 "--script", a.script.strip(), "--no-fit",
                 "--device", "auto", "--language", "en", "--reference", str(voice_ref)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=3600)
            wav = None
            for line in (so.stdout or "").splitlines():
                if line.strip().lower().startswith("audio:"):
                    wav = Path(line.split(":", 1)[1].strip())
            if so.returncode != 0 or not (wav and wav.is_file()):
                raise RuntimeError((so.stderr or so.stdout or "no audio produced")[-260:])
            vo_sec = probe_dur(wav)
            af = "apad"
            if vo_sec > dur + 0.15 and dur > 0:
                af = f"atempo={min(1.5, vo_sec / dur):.3f},apad"
                log(f"    voice is {vo_sec:.1f}s for {dur:.1f}s of video — speeding it to fit")
            # one voice track, fitted to the exact footage length — the lip-sync
            # engine and the fallback mux both consume this
            fitted = out_dir / "_voice.wav"
            ffmpeg(["-i", str(wav), "-filter:a", af, "-t", f"{dur:.3f}", str(fitted)],
                   "fit cloned voice")
            voiced = None
            tier = LIPSYNC.get(a.lipsync)
            if tier:                                  # cloud lip-sync (fal.ai, paid)
                ls_cost = round(dur * tier["cost_per_s"], 2)
                log(f"    lip-syncing the mouth to the cloned voice on {tier['label']} "
                    f"(fal.ai, ~${ls_cost:.2f}) ...")
                try:
                    args_ls = {"video_url": fal_client.upload_file(str(clip)),
                               "audio_url": fal_client.upload_file(str(fitted))}
                    if a.lipsync in ("sync", "pro", "sync3"):
                        args_ls["sync_mode"] = "cut_off"      # sync.so-specific option
                    res_ls = fal_client.subscribe(tier["endpoint"], arguments=args_ls,
                                                  with_logs=False)
                    v_ls = res_ls.get("video") or {}
                    url_ls = v_ls.get("url") if isinstance(v_ls, dict) else v_ls
                    if not url_ls:
                        raise RuntimeError(f"no video in the response: {str(res_ls)[:160]}")
                    cand = out_dir / "_synced.mp4"
                    with httpx.Client(follow_redirects=True, timeout=900) as c:
                        r = c.get(url_ls)
                        r.raise_for_status()
                        cand.write_bytes(r.content)
                    if not cand.stat().st_size or probe_dur(cand) < 0.5:
                        raise RuntimeError("truncated download")
                    voiced = cand
                    log("    OK — mouth regenerated to match the cloned voice")
                except Exception as exc:                      # noqa: BLE001
                    log(f"    cloud lip-sync failed ({str(exc)[:200]}) — keeping Veo's "
                        "own mouth movement (the cloned voice is still applied)")
            elif a.lipsync in ("gfpgan", "fast"):     # local Wav2Lip (free)
                log("    lip-syncing the mouth to the cloned voice (Wav2Lip"
                    + (" + GFPGAN" if a.lipsync == "gfpgan" else "")
                    + ", local GPU, free) ...")
                try:
                    ls_py = Path(a.ds_app).resolve().parent / "lipsync.py"
                    cand = out_dir / "_synced.mp4"
                    ls = subprocess.run(
                        [a.ds_py, str(ls_py), "--face", str(clip),
                         "--audio", str(fitted), "--out", str(cand)]
                        + (["--no-enhance"] if a.lipsync == "fast" else []),
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=7200)
                    if ls.returncode != 0 or not cand.is_file() or not cand.stat().st_size:
                        raise RuntimeError((ls.stderr or ls.stdout or "no output")[-260:])
                    voiced = cand
                    log("    OK — mouth regenerated to match the cloned voice")
                except Exception as exc:                      # noqa: BLE001
                    log(f"    lip-sync failed ({str(exc)[:200]}) — keeping Veo's own "
                        "mouth movement (the cloned voice is still applied)")
            if voiced is None:
                voiced = out_dir / "_voiced.mp4"
                ffmpeg(["-i", str(clip), "-i", str(fitted), "-map", "0:v:0",
                        "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                        "-t", f"{dur:.3f}", str(voiced)], "mux cloned voice")
            clip.unlink(missing_ok=True)
            voiced.rename(clip)
            fitted.unlink(missing_ok=True)
            dur = probe_dur(clip)
            log("    OK — the avatar now speaks in your saved voice (free, local)")
        except Exception as exc:                              # noqa: BLE001
            log(f"    re-voicing failed ({str(exc)[:200]}) — keeping the clip as generated")

    rate = (model.get("cost_per_s_silent") if (not gen_audio and model.get("cost_per_s_silent"))
            else model["cost_per_s"])
    est = round(sum(segs) * rate + (MERGE["cost"] if product else 0), 2)
    (out_dir / "i2v.json").write_text(json.dumps({
        "kind": "ugc", "prompt": f"[{a.template}] {a.script.strip()[:160] or a.action.strip()[:160]}",
        "model_label": model["label"], "aspect": "from image", "seconds": round(dur, 1),
        "est_cost": est, "template": a.template,
        "voice": (f"bank:{voice_ref.parent.name}" if voice_ref else a.voice),
        "emotion": a.emotion, "bg": a.bg, "segments": len(segs), "words": n_words,
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, indent=1), encoding="utf-8")
    log(f"\nDone: clip.mp4 ({dur:.1f}s, {len(segs)} segment(s)) ~${est:.2f} on fal.ai")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
