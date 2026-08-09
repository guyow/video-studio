# FalFluxKontextEdit — ComfyUI custom node

ComfyUI wrapper for fal.ai's `fal-ai/flux-pro/kontext` (Flux Kontext image edit).
Accepts one source image and a prompt, returns 1–4 edited candidates.

## Differences from FalQwenImageEdit

- **Single image input** — Kontext accepts exactly one `image_url`, not up to 3 references.
- **No negative prompt** — Kontext does not support `negative_prompt`.
- **No `enable_safety_checker` toggle** — replaced by `safety_tolerance` (string 1–6).
- **`enhance_prompt`** replaces `enable_prompt_expansion`.
- **New knobs**: `guidance_scale`, `seed`, `aspect_ratio`.
- **Output format**: jpeg/png only (no webp).
- **`num_images` max is 4** (Kontext's hard limit).

## Inputs

- Required: `source_image` (1 image), `prompt`
- Knobs: aspect ratio, num_images, output_format, guidance_scale, safety_tolerance, enhance_prompt
- Optional: seed (0 = random)

## Aspect ratio "match_input"

Omitted from the fal request, so Kontext uses the source image dimensions.
Any other value (`1:1`, `16:9`, etc.) is sent explicitly and the model may crop/resize.

## Install

Copy `FalFluxKontextEdit` into `ComfyUI/custom_nodes/`, then:

```bash
pip install -r ComfyUI/custom_nodes/FalFluxKontextEdit/requirements.txt
```

Set `FAL_KEY` in the shell that launches ComfyUI and restart.

## V1 caveats

- Single-image model — multi-reference workflows need a different node.
- `num_images` hard-capped at 4 by the model.
- Synchronous call to fal; no async/queue inside the node.
