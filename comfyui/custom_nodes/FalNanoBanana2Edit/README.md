# FalNanoBanana2Edit — ComfyUI custom node

ComfyUI wrapper for fal.ai's `fal-ai/nano-banana-2/edit`.
Multi-image edit: product image + optional reference design + optional additional object.

## Inputs

- **Required**: `product_image`
- **Optional**: `reference_design` (brand/design to integrate), `additional_object` (real product/object photo, e.g. the actual candy inside a package)
- **Knobs**: `num_images` (1-4), `resolution` (0.5K/1K/2K/4K), `aspect_ratio` (auto or explicit), `limit_generations`, `enable_web_search`, `thinking_level` (none/minimal/high), `safety_tolerance` (1-6)
- **Optional strings**: `audio_url` (forwarded as-is to fal if non-empty), `seed` (0 = random)

## Image roles in the prompt

When you connect `reference_design` and/or `additional_object`, the node labels them
in the prompt so the model can disambiguate:

- Image 1: the product to be advertised / re-contextualized
- Image 2: brand design / visual reference (if `reference_design` connected)
- Image 3: additional real product / object to place in the scene (if `additional_object` connected)

Order in `image_urls` is preserved — nano-banana-2 uses positional references.

## Aspect ratio

`auto` is the default. The model uses the source image dimensions. Any other value
forces the model to crop/extend to that ratio.

## Install

Copy `FalNanoBanana2Edit` into `ComfyUI/custom_nodes/`, then:

```bash
pip install -r ComfyUI/custom_nodes/FalNanoBanana2Edit/requirements.txt
```

Set `FAL_KEY` in the shell that launches ComfyUI and restart.

## V1 caveats

- Synchronous call to fal; no async/queue.
- `num_images` hard-capped at 4 (model limit).
- All uploaded images are publicly accessible on fal's CDN while retained.
