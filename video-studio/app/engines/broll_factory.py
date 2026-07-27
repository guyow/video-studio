#!/usr/bin/env python3
"""B-Roll Factory — turn reference b-roll into new, tagged b-roll clips.

Three modes, all driven from the Video Studio /broll tab (or standalone CLI):

  analyze   reference clip(s)/still(s) -> keyframes -> Claude vision -> recipe.json
            The recipe is a shot list: SD1.5 prompt, motion move, duration, and
            the bank tags (emotional_beat / product_moment / beat_tags / avatar_fit)
            so a generated clip drops straight into banks/broll.jsonl.

  generate  recipe.json -> for each shot: ComfyUI still -> upscale -> motion -> clip
            -> bank entry. Free and local except the `fal` motion path.

  motion    one still -> one clip. Standalone, so the six FLUX stills already in
            banks/broll.jsonl (all tagged "ken-burns source") can be animated too.

Motion engines
  ken   ffmpeg zoompan over a supersampled still. Free, no GPU, full 1080x1920,
        eased push/pull/pan/tilt/drift. The default — most ad b-roll IS this.
  anim  AnimateDiff (SD1.5 motion module) at a 4GB-safe resolution, then motion-
        interpolated, upscaled and ping-ponged out to the target duration.
  fal   shells the existing i2v_gen.py chaining engine. Real motion, costs money;
        the server cost-gates it before this ever runs.

Style matching
  If ComfyUI has IPAdapter installed, each shot's chosen reference frame is used
  as an image-prompt so the generated still inherits the reference's palette,
  grade and feel. Without IPAdapter the engine degrades to text-prompt-only
  (optionally + ControlNet tile for composition) and says so in the log.

Everything runs on the local GPU except `--motion fal`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zlib
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

APP_DIR = Path(__file__).resolve().parent.parent          # video-studio/app
VS_ROOT = APP_DIR.parent                                   # video-studio/
CONFIG = json.loads((VS_ROOT / "config.json").read_text(encoding="utf-8"))
ROOT = Path(CONFIG["autovsl_root"])                        # data root (autoVSL repo)
BANKS = Path(CONFIG.get("banks_dir") or (ROOT / "banks"))
BROLL_BANK = BANKS / "broll.jsonl"
BRAND_KIT = Path(CONFIG.get("brand_kit") or (BANKS / "liitt-brand-kit.json"))
OUT_ROOT = ROOT / "output" / "broll"
COMFY_URL = CONFIG.get("comfyui", "127.0.0.1:8188")

sys.path.insert(0, str(ROOT / "scripts"))
from comfyui_client import ComfyUIClient, ComfyUIError          # noqa: E402
from comfyui_workflows import (build_txt2img, build_upscale,     # noqa: E402
                               build_controlnet_keyframe, build_animatediff,
                               build_ltx2_i2v)

BASE_NEGATIVE = ("text, watermark, signature, logo, low quality, blurry, jpeg artifacts, "
                 "deformed hands, extra fingers, extra limbs, duplicate subject, "
                 "collage, split screen, borders, frame")

# SD1.5 is trained at 512px — these stay inside the range that behaves on a 4 GB card
# and does not duplicate subjects at high aspect ratios.
GEN_DIMS = {"9:16": (448, 800), "1:1": (640, 640), "16:9": (800, 448)}
OUT_DIMS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}
# LTX-2.3 render dims: divisible by 64 (pass 1 samples at half size, /32 latents).
# Kept modest — the 22B model already crawls on this machine at any size.
LTX_DIMS = {"9:16": (576, 1024), "1:1": (768, 768), "16:9": (1024, 576)}
LTX_FPS = 25
LTX_MODELS = {
    "checkpoint":   "ltx-2.3-22b-dev-fp8.safetensors",
    "text_encoder": "gemma_3_12B_it_fp4_mixed.safetensors",
    "upsampler":    "ltx-2.3-spatial-upscaler-x2-1.1.safetensors",
}
# Optional distilled LoRA — either name unlocks the fast 8+3-step schedule.
# The blueprint ships rank-384; ComfyUI's template browser wants the newer 1.1.
LTX_LORAS = (
    "ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors",
    "ltx-2.3-22b-distilled-lora-384.safetensors",
)
MOTION_TYPES = ("push_in", "pull_out", "pan_left", "pan_right",
                "tilt_up", "tilt_down", "drift", "static")
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def log(msg: str) -> None:
    print(msg, flush=True)


def slugify(s: str, fallback: str = "shot") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:48] or fallback


# ----------------------------------------------------------------- ffmpeg utils

FFMPEG_BIN = (Path(os.environ.get("LOCALAPPDATA", "")) /
              "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
              "/ffmpeg-8.1.2-full_build/bin")


def ff(name: str) -> str:
    exe = FFMPEG_BIN / f"{name}.exe"
    return str(exe) if exe.is_file() else name


def run(cmd: list[str], what: str) -> None:
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        tail = "\n".join((r.stderr or "").strip().splitlines()[-12:])
        raise RuntimeError(f"{what} failed (rc={r.returncode}):\n{tail}")


def probe(path: Path) -> dict:
    r = subprocess.run([ff("ffprobe"), "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def media_info(path: Path) -> dict:
    p = probe(path)
    v = next((s for s in p.get("streams", []) if s.get("codec_type") == "video"), {})
    try:
        dur = float((p.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return {"w": int(v.get("width") or 0), "h": int(v.get("height") or 0),
            "duration": dur, "codec": v.get("codec_name")}


def frame_count(path: Path) -> int:
    r = subprocess.run([ff("ffprobe"), "-v", "error", "-select_streams", "v:0",
                        "-count_frames", "-show_entries", "stream=nb_read_frames",
                        "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return int((r.stdout or "0").strip().split(",")[0])
    except ValueError:
        return 0


# --------------------------------------------------------------- brand + bank

def load_brand(enabled: bool) -> dict:
    if not enabled or not BRAND_KIT.is_file():
        return {"suffix": "", "negative": ""}
    try:
        kit = json.loads(BRAND_KIT.read_text(encoding="utf-8"))
    except Exception as exc:                                   # noqa: BLE001
        log(f"  (brand kit unreadable, ignoring: {exc})")
        return {"suffix": "", "negative": ""}
    return {"suffix": (kit.get("style_suffix") or "").strip(),
            "negative": (kit.get("negative_prompt") or "").strip(),
            "brand": (kit.get("brand") or {}).get("slug") if isinstance(kit.get("brand"), dict)
                     else kit.get("brand")}


def bank_rows() -> list[dict]:
    if not BROLL_BANK.is_file():
        return []
    rows = []
    for line in BROLL_BANK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def next_bank_id(rows: list[dict]) -> int:
    n = 0
    for r in rows:
        m = re.match(r"br-(\d+)$", str(r.get("id") or ""))
        if m:
            n = max(n, int(m.group(1)))
    return n + 1


def bank_append(entries: list[dict]) -> None:
    if not entries:
        return
    BROLL_BANK.parent.mkdir(parents=True, exist_ok=True)
    existing = BROLL_BANK.read_text(encoding="utf-8") if BROLL_BANK.is_file() else ""
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    with open(BROLL_BANK, "a", encoding="utf-8") as f:
        if sep:
            f.write(sep)
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def rel_to_root(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


# --------------------------------------------------------------- ComfyUI layer

def comfy() -> ComfyUIClient:
    c = ComfyUIClient(COMFY_URL, timeout=30)
    if not c.ping():
        raise SystemExit(
            f"ComfyUI is not running at {COMFY_URL}.\n"
            "Start it first: C:\\ComfyUI_windows_portable\\run_nvidia_gpu.bat  "
            "(no VRAM flag — that is the right launcher for mixed workloads on the 4 GB card).")
    return c


def comfy_caps(c: ComfyUIClient) -> dict:
    """What this ComfyUI can actually do right now."""
    caps = {"ipadapter": False, "ipadapter_models": [], "clip_vision": [],
            "controlnets": [], "upscalers": [], "checkpoints": [], "motion_modules": []}
    try:
        caps["checkpoints"] = c.checkpoints()
    except Exception:                                          # noqa: BLE001
        pass
    for key, fn in (("controlnets", c.controlnets), ("upscalers", c.upscale_models),
                    ("motion_modules", c.motion_modules)):
        try:
            caps[key] = fn()
        except Exception:                                      # noqa: BLE001
            caps[key] = []
    try:
        info = c._get_json("/object_info/IPAdapterUnifiedLoader")
        if info:
            caps["ipadapter"] = True
    except Exception:                                          # noqa: BLE001
        pass
    if not caps["ipadapter"]:
        try:
            caps["ipadapter_models"] = c._combo("IPAdapterModelLoader", "ipadapter_file")
            caps["ipadapter"] = bool(caps["ipadapter_models"])
        except Exception:                                      # noqa: BLE001
            pass
    try:
        caps["clip_vision"] = c.clip_visions()
    except Exception:                                          # noqa: BLE001
        pass
    # LTX-2.3 video: needs the 22B checkpoint + gemma text encoder + the x2
    # latent upsampler, all served by native nodes on this ComfyUI (0.27+).
    try:
        text_encoders = c._combo("LTXAVTextEncoderLoader", "text_encoder")
        latent_ups = c._combo("LatentUpscaleModelLoader", "model_name")
        loras = c._combo("LoraLoaderModelOnly", "lora_name")
    except Exception:                                          # noqa: BLE001
        text_encoders, latent_ups, loras = [], [], []
    caps["ltx"] = {
        "checkpoint": LTX_MODELS["checkpoint"] in caps["checkpoints"],
        "text_encoder": LTX_MODELS["text_encoder"] in text_encoders,
        "upsampler": LTX_MODELS["upsampler"] in latent_ups,
        # the installed LoRA's actual name (or None) — truthy check still works
        "lora": next((l for l in LTX_LORAS if l in loras), None),
    }
    caps["ltx"]["ready"] = all(caps["ltx"][k] for k in ("checkpoint", "text_encoder", "upsampler"))
    return caps


def build_ipadapter_still(*, checkpoint: str, ref_image: str, positive: str, negative: str,
                          width: int, height: int, seed: int, steps: int, cfg: float,
                          weight: float, filename_prefix: str) -> dict:
    """SD1.5 txt2img with the reference frame as an image prompt (IPAdapter unified).

    IPAdapterUnifiedLoader resolves the adapter + CLIP-Vision pair by preset name,
    so this survives whichever of the sd15 model files the user installed.
    """
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": positive, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "LoadImage", "inputs": {"image": ref_image}},
        "5": {"class_type": "IPAdapterUnifiedLoader",
              "inputs": {"model": ["1", 0], "preset": "STANDARD (medium strength)"}},
        "6": {"class_type": "IPAdapterAdvanced",
              "inputs": {"model": ["5", 0], "ipadapter": ["5", 1], "image": ["4", 0],
                         "weight": weight, "weight_type": "style transfer",
                         "combine_embeds": "concat", "start_at": 0.0, "end_at": 1.0,
                         "embeds_scaling": "V only"}},
        "7": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "8": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "euler",
                         "scheduler": "normal", "denoise": 1.0, "model": ["6", 0],
                         "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["7", 0]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["1", 2]}},
        "10": {"class_type": "SaveImage",
               "inputs": {"images": ["9", 0], "filename_prefix": filename_prefix}},
    }


def comfy_fetch_one(c: ComfyUIClient, files: list[dict], dest: Path) -> Path:
    if not files:
        raise RuntimeError("ComfyUI returned no output files")
    c.download(files[0], dest)
    return dest


# ------------------------------------------------------------------- reference

def extract_keyframes(src: Path, out_dir: Path, count: int, tag: str) -> list[dict]:
    """Evenly spread frames from a reference clip, downscaled for vision reading."""
    out_dir.mkdir(parents=True, exist_ok=True)
    info = media_info(src)
    dur = info["duration"]
    frames: list[dict] = []
    if dur <= 0:                                  # a still, not a clip
        dest = out_dir / f"{tag}_still.jpg"
        run([ff("ffmpeg"), "-y", "-v", "error", "-i", str(src),
             "-vf", "scale='min(768,iw)':-2:flags=lanczos", "-frames:v", "1", str(dest)],
            f"reading {src.name}")
        return [{"path": str(dest), "t": 0.0, "src": src.name}]
    for i in range(count):
        t = dur * (i + 0.5) / count
        dest = out_dir / f"{tag}_t{t:05.1f}.jpg"
        try:
            run([ff("ffmpeg"), "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(src),
                 "-vf", "scale='min(768,iw)':-2:flags=lanczos", "-frames:v", "1", str(dest)],
                f"extracting frame at {t:.1f}s")
        except RuntimeError:
            continue
        if dest.is_file() and dest.stat().st_size > 0:
            frames.append({"path": str(dest), "t": round(t, 2), "src": src.name})
    return frames


ANALYZE_PROMPT = """You are a b-roll art director for short-form vertical ads.

Below are keyframes pulled from REFERENCE b-roll the user wants to draw inspiration \
from. Read every image file listed — they are real files on disk.

REFERENCE FRAMES
{frames}

REFERENCE SPECS
{specs}

BRIEF FROM THE USER
{brief}
{brand_block}
TASK
Design {n} NEW b-roll shots that would sit inside the same reel as these references: \
same visual language, grade, lighting and energy — but DIFFERENT subjects. Do not \
re-describe the reference footage back to me; invent new shots in its style.

Return exactly ONE JSON object and nothing else. No prose, no markdown fence.

{{
  "style": {{
    "summary": "one sentence describing the look ACTUALLY PRESENT in the frames above",
    "palette": ["#rrggbb", "#rrggbb", "#rrggbb"],
    "lighting": "short phrase",
    "camera": "short phrase (lens, height, distance habits)",
    "grade": "short phrase (contrast, saturation, film emulation)",
    "pacing": "short phrase (how long shots hold, how much movement)"
  }},
  "shots": [
    {{
      "title": "3-6 word shot name",
      "prompt": "Stable Diffusion 1.5 POSITIVE prompt as COMMA-SEPARATED TAGS, not prose. 25-45 words. Concrete subject, framing, distance, lens, lighting, palette, film grade.",
      "negative": "extra comma-separated negatives specific to this shot, or empty string",
      "style_ref": "absolute path of the ONE reference frame above whose look this shot should match",
      "motion": {{"type": "one of: {motions}", "intensity": 0.12}},
      "duration_s": 4.0,
      "emotional_beat": "pain_mirror | agitation | hope | proof | calm | desire | resolution",
      "product_moment": "before_state | during | after_state | product_hero | lifestyle",
      "beat_tags": ["short_snake_case_tags"],
      "avatar_fit": ["who this shot speaks to, kebab-case"]
    }}
  ]
}}

HARD RULES
- SD 1.5 renders the prompt. It CANNOT do legible text, logos, branded packaging, or \
reliable hands. Never ask for any of those. Prefer faces, bodies, environments, \
textures, objects, food, light.
- Every shot must have a DIFFERENT subject and a different framing distance. No two \
shots may be variations of the same image.
- motion.type must suit the composition: push_in on a face or object, pan on a \
landscape or a table, tilt_up on a standing figure, drift on texture, static only \
when the frame is already busy.
- motion.intensity is 0.05 (barely moving) to 0.25 (assertive). Match the reference's \
pacing.
- duration_s between 3.0 and 6.0.
- style_ref must be one of the exact absolute paths listed above.
"""

BRAND_BLOCK = """
BRAND CONSTRAINT
These shots are for a brand whose house style is: {suffix}

Split the two influences cleanly — do not let the brand rewrite what you saw:
- The "style" object you return must describe the REFERENCE FRAMES honestly, even \
where they clash with the brand. If the reference is bright daylight and the brand \
is dark indigo, say bright daylight. That field is a reading, not a wish.
- The brand wins on palette, grade and mood. The reference wins on composition, \
framing, subject distance, camera behaviour and pacing. Take structure from the \
reference, colour from the brand.
- Do not repeat the brand style words verbatim in the shot prompts — the engine \
appends them automatically.
"""


def claude_exe() -> str:
    exe = shutil.which("claude")
    if exe:
        return exe
    for p in (Path.home() / ".local/bin/claude.exe", Path.home() / ".local/bin/claude"):
        if p.exists():
            return str(p)
    raise SystemExit("claude CLI not found — install Claude Code or add it to PATH.")


def ask_claude(prompt: str, cwd: Path, model: str = "sonnet", timeout: int = 900) -> dict:
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env["PYTHONUTF8"] = "1"
    if FFMPEG_BIN.is_dir():
        env["PATH"] = str(FFMPEG_BIN) + os.pathsep + env.get("PATH", "")
    r = subprocess.run(
        [claude_exe(), "-p", "--model", model, "--allowedTools", "Read",
         "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch,Task"],
        input=prompt, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, cwd=str(cwd), env=env)
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        raise RuntimeError(f"claude CLI failed (rc={r.returncode}): {(r.stderr or '')[:400]}")
    if out.startswith("```"):
        out = out.split("```")[1]
        out = out[4:] if out.lower().startswith("json") else out
    start, end = out.find("{"), out.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError(f"Claude did not return JSON. First 300 chars:\n{out[:300]}")
    return json.loads(out[start:end + 1])


def cmd_analyze(a: argparse.Namespace) -> None:
    refs = [Path(r).resolve() for r in a.ref]
    missing = [r for r in refs if not r.is_file()]
    if missing:
        raise SystemExit("reference not found: " + ", ".join(str(m) for m in missing))

    batch = a.batch or f"{slugify(a.brief, 'broll')}-{time.strftime('%m%d-%H%M%S')}"
    work = OUT_ROOT / batch
    frames_dir = work / "refs"
    frames_dir.mkdir(parents=True, exist_ok=True)

    log(f"Batch: {batch}")
    log(f"Reading {len(refs)} reference file(s)...")
    frames: list[dict] = []
    specs: list[str] = []
    for i, r in enumerate(refs, 1):
        info = media_info(r)
        kind = "clip" if r.suffix.lower() in VIDEO_EXTS else "still"
        specs.append(f"- {r.name}: {kind}, {info['w']}x{info['h']}"
                     + (f", {info['duration']:.1f}s" if info["duration"] > 0 else ""))
        got = extract_keyframes(r, frames_dir, a.frames_per_ref, f"ref{i}")
        log(f"  {r.name} → {len(got)} keyframe(s)")
        frames.extend(got)
    if not frames:
        raise SystemExit("Could not read a single frame out of the references.")

    brand = load_brand(a.brand)
    prompt = ANALYZE_PROMPT.format(
        frames="\n".join(f"- {f['path']}   (from {f['src']}, t={f['t']}s)" for f in frames),
        specs="\n".join(specs),
        brief=a.brief.strip() or "(none — infer the intent from the references)",
        brand_block=BRAND_BLOCK.format(suffix=brand["suffix"]) if brand["suffix"] else "",
        n=a.shots, motions=" | ".join(MOTION_TYPES))

    log(f"Asking Claude to read {len(frames)} frames and design {a.shots} shots "
        f"(this takes a minute or two)...")
    data = ask_claude(prompt, cwd=work, model=a.model)

    known = {f["path"] for f in frames}
    shots = []
    for i, s in enumerate(data.get("shots") or [], 1):
        mo = s.get("motion") or {}
        mtype = mo.get("type") if mo.get("type") in MOTION_TYPES else "push_in"
        try:
            intensity = float(mo.get("intensity", 0.12))
        except (TypeError, ValueError):
            intensity = 0.12
        try:
            dur = float(s.get("duration_s", 4.0))
        except (TypeError, ValueError):
            dur = 4.0
        ref = s.get("style_ref")
        if ref not in known:
            ref = frames[(i - 1) % len(frames)]["path"]
        shots.append({
            "id": f"s{i}",
            "title": (s.get("title") or f"Shot {i}").strip(),
            "prompt": (s.get("prompt") or "").strip(),
            "negative": (s.get("negative") or "").strip(),
            "style_ref": ref,
            "motion": {"type": mtype, "intensity": max(0.03, min(0.35, intensity))},
            "duration_s": max(2.0, min(12.0, dur)),
            "emotional_beat": s.get("emotional_beat") or "",
            "product_moment": s.get("product_moment") or "",
            "beat_tags": [t for t in (s.get("beat_tags") or []) if isinstance(t, str)],
            "avatar_fit": [t for t in (s.get("avatar_fit") or []) if isinstance(t, str)],
        })
    shots = [s for s in shots if s["prompt"]]
    if not shots:
        raise SystemExit("Claude returned a recipe with no usable shots.")

    recipe = {
        "batch": batch, "created": time.time(), "brief": a.brief,
        "brand": bool(a.brand), "aspect": a.aspect,
        "references": [{"file": str(r), "name": r.name} for r in refs],
        "frames": frames,
        "style": data.get("style") or {},
        "shots": shots,
    }
    (work / "recipe.json").write_text(json.dumps(recipe, indent=2, ensure_ascii=False),
                                      encoding="utf-8")
    st = recipe["style"]
    log("")
    log(f"Style read: {st.get('summary', '?')}")
    log(f"  lighting: {st.get('lighting', '?')}   camera: {st.get('camera', '?')}")
    log(f"  grade: {st.get('grade', '?')}   pacing: {st.get('pacing', '?')}")
    log("")
    for s in shots:
        log(f"  {s['id']}  {s['title']}  [{s['motion']['type']} "
            f"{s['motion']['intensity']:.2f} · {s['duration_s']:.1f}s · {s['emotional_beat']}]")
    log("")
    log(f"→ {work / 'recipe.json'}")


# ---------------------------------------------------------------------- motion

def _ease(n: int) -> str:
    """Smoothstep on the output-frame counter, for zoompan expressions.
    No commas — commas would terminate the filter argument."""
    m = max(1, n - 1)
    p = f"(on/{m})"
    return f"({p}*{p}*(3-2*{p}))"


def ken_burns(still: Path, out: Path, *, duration: float, fps: int, mtype: str,
              intensity: float, width: int, height: int, grain: int) -> None:
    """Programmatic camera move over a still. Free, no GPU, full output resolution.

    The still is supersampled to 2x the output before zoompan so the filter's
    integer pixel stepping lands at half-pixel steps on the output — that is what
    keeps a slow 0.05-intensity drift from juddering.
    """
    n = max(2, int(round(duration * fps)))
    e = _ease(n)
    amp = max(0.01, intensity)
    ss_w, ss_h = width * 2, height * 2

    if mtype == "push_in":
        z, x, y = f"1+{amp}*{e}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif mtype == "pull_out":
        z, x, y = f"{1 + amp}-{amp}*{e}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif mtype == "pan_right":
        z, x, y = f"{1 + amp}", f"(iw-iw/zoom)*{e}", "(ih-ih/zoom)/2"
    elif mtype == "pan_left":
        z, x, y = f"{1 + amp}", f"(iw-iw/zoom)*(1-{e})", "(ih-ih/zoom)/2"
    elif mtype == "tilt_down":
        z, x, y = f"{1 + amp}", "(iw-iw/zoom)/2", f"(ih-ih/zoom)*{e}"
    elif mtype == "tilt_up":
        z, x, y = f"{1 + amp}", "(iw-iw/zoom)/2", f"(ih-ih/zoom)*(1-{e})"
    elif mtype == "drift":
        z = f"1+{amp}*{e}"
        x, y = f"(iw-iw/zoom)*(0.35+0.30*{e})", f"(ih-ih/zoom)*(0.62-0.24*{e})"
    else:                                                       # static
        z, x, y = "1.0005", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"

    # cover-then-crop, never force-scale: a still that isn't exactly the target
    # aspect must be center-cropped like the exporter does, not stretched.
    chain = (f"scale={ss_w}:{ss_h}:force_original_aspect_ratio=increase:flags=lanczos,"
             f"crop={ss_w}:{ss_h},"
             f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={width}x{height}:fps={fps}")
    if grain > 0:
        chain += f",noise=alls={grain}:allf=t+u"
    chain += ",format=yuv420p"

    out.parent.mkdir(parents=True, exist_ok=True)
    run([ff("ffmpeg"), "-y", "-v", "error", "-loop", "1", "-i", str(still),
         "-vf", chain, "-frames:v", str(n), "-r", str(fps),
         "-c:v", "libx264", "-crf", "18", "-preset", "medium",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(out)],
        "ken-burns render")
    got = frame_count(out)
    if got < n - 1:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"ken-burns produced {got} frames, expected {n}")


def animatediff_clip(c: ComfyUIClient, *, checkpoint: str, motion_module: str,
                     positive: str, negative: str, out: Path, duration: float, fps: int,
                     width: int, height: int, seed: int, steps: int, work: Path) -> None:
    """AnimateDiff at a 4GB-safe resolution, then interpolated, upscaled and
    ping-ponged out to the requested duration."""
    # 16 frames @ 8 fps = 2s of real generated motion; anything larger OOMs on 4 GB.
    gen_w = 256 if width < height else 448
    gen_h = 448 if width < height else 256
    if width == height:
        gen_w = gen_h = 320
    log(f"    AnimateDiff {gen_w}x{gen_h} × 16f (this is the slow path on a 4 GB card)")
    wf = build_animatediff(checkpoint=checkpoint, motion_module=motion_module,
                           positive=positive, negative=negative,
                           width=gen_w, height=gen_h, num_frames=16, fps=8,
                           seed=seed, steps=steps, filename_prefix="broll/anim")
    files = c.run(wf, max_wait=1800)
    raw = work / "anim_raw.mp4"
    comfy_fetch_one(c, files, raw)

    mid = work / "anim_smooth.mp4"
    run([ff("ffmpeg"), "-y", "-v", "error", "-i", str(raw),
         "-vf", (f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1,"
                 f"scale={width}:{height}:flags=lanczos,unsharp=5:5:0.6:5:5:0.0,format=yuv420p"),
         "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-an", str(mid)],
        "motion interpolation")

    pp = work / "anim_pingpong.mp4"
    run([ff("ffmpeg"), "-y", "-v", "error", "-i", str(mid),
         "-filter_complex", "[0:v]split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1[v]",
         "-map", "[v]", "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-an", str(pp)],
        "ping-pong")

    n = max(2, int(round(duration * fps)))
    out.parent.mkdir(parents=True, exist_ok=True)
    run([ff("ffmpeg"), "-y", "-v", "error", "-stream_loop", "-1", "-i", str(pp),
         "-frames:v", str(n), "-r", str(fps),
         "-c:v", "libx264", "-crf", "18", "-preset", "medium",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(out)],
        "loop to duration")
    for tmp in (raw, mid, pp):
        tmp.unlink(missing_ok=True)


def fal_clip(still: Path, out: Path, *, prompt: str, aspect: str, seconds: int,
             model: str, work: Path) -> None:
    """Shell the existing fal.ai image→video engine. The server cost-gates this."""
    engine = APP_DIR / "engines" / "i2v_gen.py"
    if not engine.is_file():
        raise RuntimeError(f"i2v engine missing at {engine}")
    stage = work / "fal"
    stage.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(engine), "--image", str(still), "--prompt", prompt,
           "--out", str(stage), "--name", out.stem, "--model", model,
           "--aspect", aspect, "--seconds", str(seconds),
           "--env-file", str(ROOT / ".env")]
    log(f"    fal.ai {model} — {seconds}s (this one costs money)")
    r = subprocess.run([str(x) for x in cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    for line in (r.stdout or "").splitlines():
        log(f"      {line}")
    if r.returncode != 0:
        tail = "\n".join((r.stderr or "").strip().splitlines()[-10:])
        raise RuntimeError(f"fal i2v failed (rc={r.returncode}):\n{tail}")
    made = next((p for p in sorted(stage.rglob("*.mp4"),
                                   key=lambda x: x.stat().st_mtime, reverse=True)), None)
    if not made:
        raise RuntimeError("fal i2v produced no mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(made), str(out))


def _ltx_length(duration: float) -> int:
    """Frame count for LTX: 8k+1 at 25 fps, floor 9, cap ~9.6s."""
    n = int(round((duration * LTX_FPS - 1) / 8)) * 8 + 1
    return max(9, min(241, n))


def ltx_clip(c: ComfyUIClient, caps: dict, still: Path, out: Path, *, prompt: str,
             negative: str, duration: float, aspect: str, seed: int, work: Path,
             fps_out: int) -> None:
    """LTX-2.3 image→video on the local GPU: two-pass sampling with the
    spatial-upscaler-x2 doubling the latent between passes. Free but VERY slow
    on this machine — the 22B fp8 weights stream from disk on every step."""
    ltx = caps.get("ltx") or {}
    if not ltx.get("ready"):
        missing = [k for k in ("checkpoint", "text_encoder", "upsampler") if not ltx.get(k)]
        raise RuntimeError(f"LTX-2.3 models missing in ComfyUI: {', '.join(missing)}")
    w, h = LTX_DIMS[aspect]
    length = _ltx_length(duration)
    lora = ltx.get("lora") or None
    log(f"    LTX-2.3 {w}x{h} × {length}f @ {LTX_FPS}fps"
        + (" + distilled LoRA (fast 8+3-step schedule)" if lora else
           " (no distilled LoRA — full 24-step base sampling w/ CFG, ~2.5x slower;"
           " install the LoRA for the fast path)"))
    log("    ⚠ 22B model on a 4 GB card: weights stream from RAM/disk — this can take"
        " tens of minutes to hours per clip. Ken Burns and fal stay the fast paths.")
    name = c.upload_image(still)
    wf = build_ltx2_i2v(
        checkpoint=LTX_MODELS["checkpoint"], text_encoder=LTX_MODELS["text_encoder"],
        upscale_model=LTX_MODELS["upsampler"], image_name=name,
        positive=prompt, negative=negative, width=w, height=h, length=length,
        fps=LTX_FPS, seed=seed, lora=lora, filename_prefix="broll/ltx")
    files = c.run(wf, poll=5.0, max_wait=4 * 3600)
    vids = [f for f in files if str(f.get("filename", "")).lower().endswith(".mp4")]
    if not vids:
        raise RuntimeError(f"LTX run produced no mp4 (outputs: "
                           f"{[f.get('filename') for f in files][:4]})")
    raw = work / "ltx_raw.mp4"
    c.download(vids[0], raw)

    ow, oh = OUT_DIMS[aspect]
    out.parent.mkdir(parents=True, exist_ok=True)
    run([ff("ffmpeg"), "-y", "-v", "error", "-i", str(raw),
         "-vf", (f"scale={ow}:{oh}:force_original_aspect_ratio=increase:flags=lanczos,"
                 f"crop={ow}:{oh},fps={fps_out},format=yuv420p"),
         "-c:v", "libx264", "-crf", "18", "-preset", "medium",
         "-movflags", "+faststart", "-an", str(out)],
        "LTX finishing pass")
    raw.unlink(missing_ok=True)


# -------------------------------------------------------------------- generate

def make_still(c: ComfyUIClient, caps: dict, shot: dict, *, checkpoint: str, positive: str,
               negative: str, gen_w: int, gen_h: int, seed: int, steps: int, cfg: float,
               style_mode: str, ip_weight: float, cnet_strength: float,
               dest: Path) -> str:
    """Generate one still. Returns the style path actually taken."""
    ref = shot.get("style_ref")
    have_ref = bool(ref) and Path(ref).is_file()

    if style_mode == "ipadapter" and have_ref:
        name = c.upload_image(Path(ref))
        wf = build_ipadapter_still(checkpoint=checkpoint, ref_image=name, positive=positive,
                                   negative=negative, width=gen_w, height=gen_h, seed=seed,
                                   steps=steps, cfg=cfg, weight=ip_weight,
                                   filename_prefix="broll/still")
        comfy_fetch_one(c, c.run(wf, max_wait=1200), dest)
        return "ipadapter"

    if style_mode == "controlnet" and have_ref and caps["controlnets"]:
        name = c.upload_image(Path(ref))
        wf = build_controlnet_keyframe(
            checkpoint=checkpoint, controlnet=caps["controlnets"][0], control_image=name,
            positive=positive, negative=negative, width=gen_w, height=gen_h, seed=seed,
            steps=steps, cfg=cfg, strength=cnet_strength, filename_prefix="broll/still")
        comfy_fetch_one(c, c.run(wf, max_wait=1200), dest)
        return "controlnet"

    wf = build_txt2img(checkpoint=checkpoint, positive=positive, negative=negative,
                       width=gen_w, height=gen_h, seed=seed, steps=steps, cfg=cfg,
                       filename_prefix="broll/still")
    comfy_fetch_one(c, c.run(wf, max_wait=1200), dest)
    return "text"


def upscale_still(c: ComfyUIClient, caps: dict, src: Path, dest: Path,
                  target_w: int, target_h: int) -> str:
    """ESRGAN 4x so the Ken Burns move has pixels to eat into. Falls back to
    lanczos — a failed upscale must never kill the shot."""
    if caps["upscalers"]:
        try:
            name = c.upload_image(src)
            wf = build_upscale(image_name=name, upscale_model=caps["upscalers"][0],
                               filename_prefix="broll/up")
            big = src.with_name(src.stem + "_4x.png")
            comfy_fetch_one(c, c.run(wf, max_wait=900), big)
            run([ff("ffmpeg"), "-y", "-v", "error", "-i", str(big),
                 "-vf", f"scale={target_w * 2}:{target_h * 2}:"
                        f"force_original_aspect_ratio=increase:flags=lanczos,"
                        f"crop={target_w * 2}:{target_h * 2}", str(dest)],
                "fit upscaled still")
            big.unlink(missing_ok=True)
            return "esrgan"
        except Exception as exc:                              # noqa: BLE001
            log(f"    (ESRGAN upscale failed — {str(exc)[:120]}; using lanczos)")
    run([ff("ffmpeg"), "-y", "-v", "error", "-i", str(src),
         "-vf", f"scale={target_w * 2}:{target_h * 2}:flags=lanczos", str(dest)],
        "lanczos upscale")
    return "lanczos"


def cmd_generate(a: argparse.Namespace) -> None:
    recipe_path = Path(a.recipe).resolve()
    if not recipe_path.is_file():
        raise SystemExit(f"recipe not found: {recipe_path}")
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    work = recipe_path.parent
    batch = recipe.get("batch") or work.name
    aspect = a.aspect or recipe.get("aspect") or "9:16"
    if aspect not in GEN_DIMS:
        raise SystemExit(f"aspect must be one of {', '.join(GEN_DIMS)}")
    gen_w, gen_h = GEN_DIMS[aspect]
    out_w, out_h = OUT_DIMS[aspect]

    shots = recipe.get("shots") or []
    if a.shots:
        want = {s.strip() for s in a.shots.split(",") if s.strip()}
        shots = [s for s in shots if s["id"] in want]
    if not shots:
        raise SystemExit("no shots selected")

    brand = load_brand(recipe.get("brand") or a.brand)
    c = comfy()
    caps = comfy_caps(c)
    if not caps["checkpoints"]:
        raise SystemExit("ComfyUI has no checkpoints in models/checkpoints.")
    checkpoint = a.checkpoint or caps["checkpoints"][0]

    style_mode = a.style
    if style_mode == "auto":
        style_mode = "ipadapter" if caps["ipadapter"] else "text"
    if style_mode == "ipadapter" and not caps["ipadapter"]:
        log("  IPAdapter is not installed in ComfyUI — falling back to text-prompt styling.")
        log("  (Install it for true reference matching: ComfyUI-Manager → "
            "'ComfyUI_IPAdapter_plus', then the sd15 adapter + CLIP-ViT-H encoder.)")
        style_mode = "text"

    motion_engine = a.motion
    if motion_engine == "anim" and not caps["motion_modules"]:
        log("  No AnimateDiff motion module installed — using ken-burns instead.")
        motion_engine = "ken"
    if motion_engine == "ltx" and not (caps.get("ltx") or {}).get("ready"):
        miss = [k for k in ("checkpoint", "text_encoder", "upsampler")
                if not (caps.get("ltx") or {}).get(k)]
        log(f"  LTX-2.3 models missing ({', '.join(miss)}) — using ken-burns instead.")
        motion_engine = "ken"

    log(f"Batch {batch} · {len(shots)} shot(s) · {aspect} {out_w}x{out_h} · "
        f"still={style_mode} · motion={motion_engine} · {checkpoint}")

    stills_dir, clips_dir = work / "stills", work / "clips"
    stills_dir.mkdir(parents=True, exist_ok=True)
    clips_dir.mkdir(parents=True, exist_ok=True)

    rows = bank_rows()
    next_id = next_bank_id(rows)
    made: list[dict] = []
    failed: list[str] = []

    for idx, shot in enumerate(shots):
        sid = shot["id"]
        # zlib.crc32, not hash(): str hashing is salted per process (PYTHONHASHSEED),
        # so hash() would hand the same shot a different image on every re-run.
        seed = a.seed + idx if a.seed else (zlib.crc32(f"{batch}:{sid}".encode()) % 2_000_000_000)
        positive = shot["prompt"]
        if brand["suffix"]:
            positive = f"{positive}, {brand['suffix']}"
        negative = ", ".join(x for x in (shot.get("negative"), brand["negative"],
                                         BASE_NEGATIVE) if x)
        t0 = time.time()
        log("")
        log(f"[{idx + 1}/{len(shots)}] {sid} — {shot['title']}")
        try:
            raw = stills_dir / f"{sid}_raw.png"
            used = make_still(c, caps, shot, checkpoint=checkpoint, positive=positive,
                              negative=negative, gen_w=gen_w, gen_h=gen_h, seed=seed,
                              steps=a.steps, cfg=a.cfg, style_mode=style_mode,
                              ip_weight=a.ip_weight, cnet_strength=a.cnet_strength,
                              dest=raw)
            log(f"    still {gen_w}x{gen_h} via {used} ({time.time() - t0:.0f}s)")

            still = stills_dir / f"{sid}.png"
            if a.no_upscale:
                run([ff("ffmpeg"), "-y", "-v", "error", "-i", str(raw),
                     "-vf", f"scale={out_w * 2}:{out_h * 2}:"
                            f"force_original_aspect_ratio=increase:flags=lanczos,"
                            f"crop={out_w * 2}:{out_h * 2}", str(still)],
                    "resize still")
            else:
                how = upscale_still(c, caps, raw, still, out_w, out_h)
                log(f"    upscaled via {how} → {out_w * 2}x{out_h * 2}")

            clip = clips_dir / f"{sid}.mp4"
            dur = float(shot.get("duration_s") or 4.0)
            mo = shot.get("motion") or {}
            if motion_engine == "ken":
                ken_burns(still, clip, duration=dur, fps=a.fps,
                          mtype=mo.get("type", "push_in"),
                          intensity=float(mo.get("intensity", 0.12)),
                          width=out_w, height=out_h, grain=a.grain)
            elif motion_engine == "anim":
                animatediff_clip(c, checkpoint=checkpoint,
                                 motion_module=caps["motion_modules"][0],
                                 positive=positive, negative=negative, out=clip,
                                 duration=dur, fps=a.fps, width=out_w, height=out_h,
                                 seed=seed, steps=max(16, a.steps - 6), work=work)
            elif motion_engine == "ltx":
                # LTX animates the shot's OWN still, so the motion prompt is the
                # shot prompt + the recipe's camera move spelled out in words.
                move = {"push_in": "slow cinematic push in", "pull_out": "slow pull back",
                        "pan_left": "slow pan left", "pan_right": "slow pan right",
                        "tilt_up": "slow tilt up", "tilt_down": "slow tilt down",
                        "drift": "gentle handheld drift", "static": "locked-off static shot"
                        }.get(mo.get("type", "push_in"), "slow cinematic push in")
                ltx_clip(c, caps, still, clip,
                         prompt=f"{shot['prompt']}, {move}, subtle natural motion",
                         negative=negative, duration=dur, aspect=aspect, seed=seed,
                         work=work, fps_out=a.fps)
            else:
                fal_clip(still, clip, prompt=shot["prompt"], aspect=aspect,
                         seconds=int(round(dur)), model=a.fal_model, work=work)

            info = media_info(clip)
            log(f"    clip {info['w']}x{info['h']} · {info['duration']:.1f}s "
                f"({time.time() - t0:.0f}s total) → {clip}")

            entry = {
                "id": f"br-{next_id:04d}",
                "file": rel_to_root(clip),
                "still": rel_to_root(still),
                "duration_s": round(info["duration"], 2),
                "usable_segments": None,
                "shot": f"{shot['title']} — {shot['prompt'][:140]}",
                "emotional_beat": shot.get("emotional_beat") or "",
                "product_moment": shot.get("product_moment") or "",
                "beat_tags": shot.get("beat_tags") or [],
                "avatar_fit": shot.get("avatar_fit") or [],
                "brand": brand.get("brand") or "fairy-flame",
                "rights": "owned",
                "quality": "B",
                "status": "available",
                "source": "broll-factory",
                "batch": batch,
                "shot_id": sid,
                "motion": mo,
                "prompt": positive,
                "style_mode": used,
                "motion_engine": motion_engine,
                "created": time.time(),
            }
            made.append(entry)
            next_id += 1
        except Exception as exc:                              # noqa: BLE001
            log(f"    FAILED: {exc}")
            failed.append(sid)

    if made and not a.no_bank:
        bank_append(made)
        log("")
        log(f"Banked {len(made)} clip(s) → {BROLL_BANK}")
    (work / "generated.json").write_text(
        json.dumps({"batch": batch, "generated": made, "failed": failed,
                    "motion_engine": motion_engine, "style_mode": style_mode,
                    "when": time.time()}, indent=2, ensure_ascii=False), encoding="utf-8")

    log("")
    log(f"Done: {len(made)} clip(s) made"
        + (f", {len(failed)} failed ({', '.join(failed)})" if failed else "")
        + (" — $0, all local." if motion_engine != "fal" else ""))
    if failed and not made:
        raise SystemExit(1)


def cmd_motion(a: argparse.Namespace) -> None:
    """Animate one still that already exists (e.g. the FLUX stills in the bank)."""
    still = Path(a.still).resolve()
    if not still.is_file():
        raise SystemExit(f"still not found: {still}")
    aspect = a.aspect
    out_w, out_h = OUT_DIMS[aspect]
    out = Path(a.out).resolve() if a.out else (
        OUT_ROOT / "singles" / f"{still.stem}-{a.motion_type}-{time.strftime('%H%M%S')}.mp4")
    work = out.parent
    work.mkdir(parents=True, exist_ok=True)

    if a.engine == "ltx":
        if not a.prompt:
            raise SystemExit("--engine ltx needs --prompt describing the scene/motion")
        c = comfy()
        caps = comfy_caps(c)
        seed = zlib.crc32(still.name.encode()) % 2_000_000_000
        ltx_clip(c, caps, still, out, prompt=a.prompt,
                 negative=BASE_NEGATIVE, duration=a.duration, aspect=aspect,
                 seed=seed, work=work, fps_out=a.fps)
        info = media_info(out)
        log(f"→ {out}  ({info['w']}x{info['h']}, {info['duration']:.1f}s)")
        return

    src = still
    if not a.no_upscale:
        c = comfy()
        caps = comfy_caps(c)
        src = work / f"{still.stem}_big.png"
        how = upscale_still(c, caps, still, src, out_w, out_h)
        log(f"upscaled via {how}")

    log(f"{a.motion_type} @ {a.intensity:.2f} · {a.duration:.1f}s · {out_w}x{out_h}")
    ken_burns(src, out, duration=a.duration, fps=a.fps, mtype=a.motion_type,
              intensity=a.intensity, width=out_w, height=out_h, grain=a.grain)
    if src != still:
        src.unlink(missing_ok=True)
    info = media_info(out)
    log(f"→ {out}  ({info['w']}x{info['h']}, {info['duration']:.1f}s)")


def cmd_health(a: argparse.Namespace) -> None:
    c = ComfyUIClient(COMFY_URL, timeout=10)
    if not c.ping():
        print(json.dumps({"comfyui": False, "url": COMFY_URL}))
        return
    caps = comfy_caps(c)
    caps["comfyui"] = True
    caps["url"] = COMFY_URL
    caps["bank_rows"] = len(bank_rows())
    print(json.dumps(caps))


def main() -> int:
    p = argparse.ArgumentParser(description="B-Roll Factory — reference in, tagged b-roll out")
    sub = p.add_subparsers(dest="mode", required=True)

    an = sub.add_parser("analyze", help="reference clips -> recipe.json")
    an.add_argument("--ref", action="append", required=True, help="reference clip or still (repeatable)")
    an.add_argument("--brief", default="", help="what you want out of it")
    an.add_argument("--shots", type=int, default=6)
    an.add_argument("--frames-per-ref", type=int, default=6)
    an.add_argument("--aspect", default="9:16", choices=list(GEN_DIMS))
    an.add_argument("--brand", action="store_true", help="apply the liitt brand kit style")
    an.add_argument("--batch", default=None)
    an.add_argument("--model", default="sonnet", choices=["sonnet", "opus", "haiku"])

    ge = sub.add_parser("generate", help="recipe.json -> stills -> clips -> bank")
    ge.add_argument("--recipe", required=True)
    ge.add_argument("--shots", default="", help="comma-separated shot ids (default: all)")
    ge.add_argument("--motion", default="ken", choices=["ken", "anim", "fal", "ltx"])
    ge.add_argument("--style", default="auto", choices=["auto", "ipadapter", "controlnet", "text"])
    ge.add_argument("--aspect", default=None, choices=list(GEN_DIMS))
    ge.add_argument("--steps", type=int, default=26)
    ge.add_argument("--cfg", type=float, default=7.0)
    ge.add_argument("--seed", type=int, default=0, help="0 = derive per shot from the batch name")
    ge.add_argument("--fps", type=int, default=30)
    ge.add_argument("--grain", type=int, default=4, help="film grain strength, 0 = off")
    ge.add_argument("--ip-weight", type=float, default=0.7)
    ge.add_argument("--cnet-strength", type=float, default=0.55)
    ge.add_argument("--checkpoint", default=None)
    ge.add_argument("--fal-model", default="kling-2.1")
    ge.add_argument("--brand", action="store_true")
    ge.add_argument("--no-upscale", action="store_true")
    ge.add_argument("--no-bank", action="store_true", help="don't append to broll.jsonl")

    mo = sub.add_parser("motion", help="one still -> one clip")
    mo.add_argument("--still", required=True)
    mo.add_argument("--out", default=None)
    mo.add_argument("--engine", default="ken", choices=["ken", "ltx"])
    mo.add_argument("--prompt", default="", help="scene/motion description (ltx engine)")
    mo.add_argument("--motion-type", default="push_in", choices=list(MOTION_TYPES))
    mo.add_argument("--intensity", type=float, default=0.12)
    mo.add_argument("--duration", type=float, default=4.0)
    mo.add_argument("--fps", type=int, default=30)
    mo.add_argument("--grain", type=int, default=4)
    mo.add_argument("--aspect", default="9:16", choices=list(OUT_DIMS))
    mo.add_argument("--no-upscale", action="store_true")

    sub.add_parser("health", help="what ComfyUI can do right now (JSON)")

    a = p.parse_args()
    try:
        {"analyze": cmd_analyze, "generate": cmd_generate,
         "motion": cmd_motion, "health": cmd_health}[a.mode](a)
    except ComfyUIError as exc:
        print(f"ComfyUI error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
