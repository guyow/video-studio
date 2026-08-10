"""Fal Nano Banana 2 Edit (Batch) — batch-aware wrapper around fal-ai/nano-banana-2/edit.

INPUTS (batch dimension lives on TWO slots):
  - product_image:            IMAGE single  [1, H, W, C]   (broadcast)
  - reference_image:          IMAGE batch   [B, H, W, C]   (B = batch size)
  - additional_object_image:  IMAGE single  [1, H, W, C]   (optional, broadcast)
  - prompts:                  STRING list   length up to B (one prompt per reference)

OUTPUT:
  - images: IMAGE batch [B, H, W, C] — one generated image per iteration,
    index-aligned with reference_image[i] and prompts[i].

Per-iteration behavior is identical to (single) FalNanoBanana2Edit — same
fal model, same arguments, same defaults. Only the loop dimension changes:
the loop is over (reference_image[i], prompt[i]). Both single-image inputs
are uploaded once and reused per iteration to avoid redundant uploads.

If num_images > 1 is requested per iteration, we only surface the FIRST
fal.ai output per iteration (one-to-one pairing rule). This keeps the
output tensor batch-aligned with the input batch. For multi-candidate
generation, use the original single-image FalNanoBanana2Edit node.

NOTE: prompt_mode default is "truncate" (not "strict") because the common
flow is: planner produces a single prompt for one product description,
and the batch is the reference design list. So batch size = reference
batch size, NOT prompts list size. Truncate mode handles this gracefully.
"""

import os
import tempfile
from pathlib import Path

import numpy as np
import torch
from PIL import Image


NODE_NAME = "FalNanoBanana2EditBatch"
MODEL_ID = "fal-ai/nano-banana-2/edit"


def _require_fal():
    try:
        import fal_client
    except ImportError as exc:
        raise RuntimeError(
            "fal-client is not installed. Run: pip install -r requirements.txt"
        ) from exc

    if not os.getenv("FAL_KEY"):
        raise RuntimeError("FAL_KEY is not set. Set it before starting ComfyUI.")
    return fal_client


def _tensor_to_pil(image_tensor):
    """Convert one ComfyUI IMAGE tensor [H,W,C] in 0..1 to RGB PIL."""
    image = image_tensor.detach().cpu().clamp(0, 1).numpy()
    image = (image * 255.0).round().astype(np.uint8)
    if image.shape[-1] == 4:
        return Image.fromarray(image, mode="RGBA").convert("RGB")
    return Image.fromarray(image, mode="RGB")


def _upload_image(fal_client, image_tensor, tmpdir, name):
    pil = _tensor_to_pil(image_tensor)
    path = Path(tmpdir) / f"{name}.png"
    pil.save(path, format="PNG")
    return fal_client.upload_file(str(path))


def _url_to_tensor(url):
    import io
    import urllib.request

    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()

    image = Image.open(io.BytesIO(data)).convert("RGB")
    arr = np.asarray(image).astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None, ...]


def _normalize_prompts(prompts):
    """Coerce prompts (str | list[str]) to a list[str]."""
    if isinstance(prompts, str):
        return [prompts]
    if isinstance(prompts, list):
        out = []
        for item in prompts:
            if isinstance(item, list):
                out.extend(str(x) for x in item)
            else:
                out.append(str(item))
        return out
    return [str(prompts)]


def _as_4d_image(tensor, name):
    """Accept either [H,W,C] (1-image) or [B,H,W,C] (batch) and return the tensor."""
    if not isinstance(tensor, torch.Tensor):
        raise RuntimeError(
            f"{name} must be a torch.Tensor, got {type(tensor).__name__}"
        )
    if tensor.dim() == 3:
        tensor = tensor.unsqueeze(0)
    if tensor.dim() != 4:
        raise RuntimeError(
            f"{name} must be 3D [H,W,C] or 4D [B,H,W,C], got shape {tuple(tensor.shape)}"
        )
    return tensor


class FalNanoBanana2EditBatch:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "product_image": ("IMAGE",),
                "reference_image": ("IMAGE",),
                "prompts": (
                    "STRING",
                    {"forceInput": True, "multiline": True, "io": True},
                ),
                "resolution": (
                    ["0.5K", "1K", "2K", "4K"],
                    {"default": "1K"},
                ),
                "aspect_ratio": (
                    [
                        "auto",
                        "21:9",
                        "16:9",
                        "3:2",
                        "4:3",
                        "5:4",
                        "1:1",
                        "4:5",
                        "3:4",
                        "2:3",
                        "9:16",
                        "4:1",
                        "1:4",
                        "8:1",
                        "1:8",
                    ],
                    {"default": "auto"},
                ),
                "limit_generations": ("BOOLEAN", {"default": True}),
                "enable_web_search": ("BOOLEAN", {"default": False}),
                "thinking_level": (
                    ["none", "minimal", "high"],
                    {"default": "none"},
                ),
                "safety_tolerance": (
                    [str(i) for i in range(1, 7)],
                    {"default": "4"},
                ),
            },
            "optional": {
                "additional_object_image": (
                    "IMAGE",
                    {"forceInput": True, "multiline": True, "io": True},
                ),
                "audio_url": ("STRING", {"default": ""}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 2147483647}),
                "prompt_mode": (
                    ["truncate", "pad", "strict"],
                    {"default": "truncate"},
                ),
                "fallback_prompt": (
                    "STRING",
                    {
                        "default": (
                            "Create a premium UGC product advertisement. "
                            "Preserve the product packaging exactly as "
                            "provided."
                        ),
                        "multiline": True,
                    },
                ),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "generate_batch"
    CATEGORY = "fal.ai/NanoBanana"

    def generate_batch(
        self,
        product_image,
        reference_image,
        prompts,
        resolution,
        aspect_ratio,
        limit_generations,
        enable_web_search,
        thinking_level,
        safety_tolerance,
        additional_object_image=None,
        audio_url="",
        seed=0,
        prompt_mode="truncate",
        fallback_prompt="",
    ):
        fal_client = _require_fal()

        # --- Validate shapes ---
        product_image = _as_4d_image(product_image, "product_image")
        reference_image = _as_4d_image(reference_image, "reference_image")

        ref_batch = int(reference_image.shape[0])
        if ref_batch < 1:
            raise RuntimeError("reference_image batch is empty (B=0)")

        prompts_list = _normalize_prompts(prompts)
        n_prompts = len(prompts_list)

        if prompt_mode == "strict":
            if ref_batch != n_prompts:
                raise RuntimeError(
                    f"FalNanoBanana2EditBatch (strict): reference_image "
                    f"batch size {ref_batch} does not match prompts list "
                    f"length {n_prompts}. Use prompt_mode='pad' or "
                    f"'truncate' to bypass."
                )
        elif prompt_mode == "pad":
            if n_prompts < ref_batch:
                fb = fallback_prompt.strip() if fallback_prompt.strip() else (
                    "Create a premium UGC product poster."
                )
                prompts_list = list(prompts_list) + [fb] * (ref_batch - n_prompts)
            elif n_prompts > ref_batch:
                prompts_list = prompts_list[:ref_batch]
        elif prompt_mode == "truncate":
            if n_prompts > ref_batch:
                prompts_list = prompts_list[:ref_batch]
            elif n_prompts < ref_batch:
                fb = fallback_prompt.strip() if fallback_prompt.strip() else (
                    "Create a premium UGC product poster."
                )
                prompts_list = list(prompts_list) + [fb] * (ref_batch - n_prompts)
        else:
            raise ValueError(f"Unknown prompt_mode: {prompt_mode!r}")

        # --- Upload reused images once ---
        with tempfile.TemporaryDirectory(prefix="comfy_fal_nanobanana2_batch_") as tmpdir:
            product_url = _upload_image(
                fal_client, product_image[0], tmpdir, "product"
            )

            additional_url = None
            if additional_object_image is not None:
                aoi = _as_4d_image(additional_object_image, "additional_object_image")
                additional_url = _upload_image(
                    fal_client, aoi[0], tmpdir, "additional"
                )

            # --- Per-reference loop ---
            output_tensors = []
            for i in range(ref_batch):
                ref_url = _upload_image(
                    fal_client, reference_image[i], tmpdir, f"ref_{i:03d}"
                )

                images = [product_url, ref_url]
                labels = [
                    "Image 1 is the product to be advertised / re-contextualized.",
                    "Image 2 is a brand design / reference to integrate "
                    "(logo, packaging, layout, or visual reference).",
                ]

                if additional_url is not None:
                    images.append(additional_url)
                    labels.append(
                        "Image 3 is an additional real product / object that "
                        "should be placed naturally in the scene."
                    )

                final_prompt = "\n".join(
                    [
                        *labels,
                        "",
                        "Use the reference images according to their descriptions above.",
                        "The actual product must remain recognizable and commercially accurate.",
                        "Do not redesign the package, logo, brand identity, or printed claims.",
                        prompts_list[i].strip(),
                    ]
                )

                arguments = {
                    "prompt": final_prompt,
                    "image_urls": images,
                    "num_images": 1,  # one-to-one pairing rule
                    "resolution": resolution,
                    "aspect_ratio": aspect_ratio,
                    "limit_generations": bool(limit_generations),
                    "enable_web_search": bool(enable_web_search),
                    "safety_tolerance": str(safety_tolerance),
                }

                if thinking_level and thinking_level != "none":
                    arguments["thinking_level"] = thinking_level

                if audio_url.strip():
                    arguments["audio_url"] = audio_url.strip()

                if int(seed) > 0:
                    arguments["seed"] = int(seed)

                result = fal_client.subscribe(
                    MODEL_ID,
                    arguments=arguments,
                )

                output_files = result.get("images") or []
                if not output_files:
                    raise RuntimeError(
                        f"fal.ai returned no images for iteration {i}: {result}"
                    )

                first = output_files[0]
                url = first.get("url") if isinstance(first, dict) else None
                if not url:
                    raise RuntimeError(
                        f"Unexpected fal.ai image output at iteration {i}: {first}"
                    )

                output_tensors.append(_url_to_tensor(url))

            if not output_tensors:
                raise RuntimeError("No outputs were produced.")

            batch = torch.cat(output_tensors, dim=0)
            return (batch,)


NODE_CLASS_MAPPINGS = {
    NODE_NAME: FalNanoBanana2EditBatch,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    NODE_NAME: "Fal Nano Banana 2 Edit (Batch)",
}
