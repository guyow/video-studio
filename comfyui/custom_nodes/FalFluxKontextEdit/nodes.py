import os
import tempfile
from pathlib import Path

import numpy as np
import torch
from PIL import Image


NODE_NAME = "FalFluxKontextEdit"
MODEL_ID = "fal-ai/flux-pro/kontext"


def _require_fal():
    try:
        import fal_client
    except ImportError as exc:
        raise RuntimeError(
            "fal-client is not installed. Run: pip install -r requirements.txt"
        ) from exc

    if not os.getenv("FAL_KEY"):
        raise RuntimeError(
            "FAL_KEY is not set. Set it before starting ComfyUI."
        )
    return fal_client


def _tensor_to_pil(image_tensor: torch.Tensor) -> Image.Image:
    """Convert one ComfyUI IMAGE tensor [H,W,C] in 0..1 to RGB PIL."""
    image = image_tensor.detach().cpu().clamp(0, 1).numpy()
    image = (image * 255.0).round().astype(np.uint8)
    if image.shape[-1] == 4:
        return Image.fromarray(image, mode="RGBA").convert("RGB")
    return Image.fromarray(image, mode="RGB")


def _upload_image(fal_client, image_tensor: torch.Tensor, tmpdir: str, name: str) -> str:
    pil = _tensor_to_pil(image_tensor)
    path = Path(tmpdir) / f"{name}.png"
    pil.save(path, format="PNG")
    return fal_client.upload_file(str(path))


def _url_to_tensor(url: str) -> torch.Tensor:
    import io
    import urllib.request

    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()

    image = Image.open(io.BytesIO(data)).convert("RGB")
    arr = np.asarray(image).astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None, ...]


class FalFluxKontextEdit:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "source_image": ("IMAGE",),
                "prompt": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": (
                            "Create a premium UGC product advertisement. "
                            "Preserve the product packaging exactly as provided. "
                            "Keep the logo, printed text, colors, proportions, and package shape unchanged. "
                            "Create a realistic lifestyle scene around the product with natural lighting and shadows."
                        ),
                    },
                ),
                "aspect_ratio": (
                    [
                        "21:9", "16:9", "4:3", "3:2", "1:1",
                        "2:3", "3:4", "9:16", "9:21", "match_input",
                    ],
                    {"default": "match_input"},
                ),
                "num_images": (
                    "INT",
                    {"default": 1, "min": 1, "max": 4, "step": 1},
                ),
                "output_format": (["jpeg", "png"], {"default": "jpeg"}),
                "guidance_scale": (
                    "FLOAT",
                    {"default": 3.5, "min": 1.0, "max": 20.0, "step": 0.1},
                ),
                "safety_tolerance": (
                    [str(i) for i in range(1, 7)],
                    {"default": "2"},
                ),
                "enhance_prompt": ("BOOLEAN", {"default": False}),
            },
            "optional": {
                "seed": ("INT", {"default": 0, "min": 0, "max": 2147483647}),
            },
        }

    RETURN_TYPES = ("IMAGE", "IMAGE", "IMAGE", "IMAGE")
    RETURN_NAMES = (
        "candidate_1",
        "candidate_2",
        "candidate_3",
        "candidate_4",
    )
    FUNCTION = "generate"
    CATEGORY = "fal.ai/Flux"

    def generate(
        self,
        source_image,
        prompt,
        aspect_ratio,
        num_images,
        output_format,
        guidance_scale,
        safety_tolerance,
        enhance_prompt,
        seed=0,
    ):
        fal_client = _require_fal()

        with tempfile.TemporaryDirectory(prefix="comfy_fal_flux_") as tmpdir:
            image_url = _upload_image(
                fal_client, source_image[0], tmpdir, "source"
            )

            arguments = {
                "prompt": prompt.strip(),
                "image_url": image_url,
                "num_images": int(num_images),
                "output_format": output_format,
                "guidance_scale": float(guidance_scale),
                "safety_tolerance": str(safety_tolerance),
                "enhance_prompt": bool(enhance_prompt),
            }

            # aspect_ratio "match_input" → omit so Kontext uses source dims
            if aspect_ratio != "match_input":
                arguments["aspect_ratio"] = aspect_ratio

            # seed=0 → omit so fal picks random
            if int(seed) > 0:
                arguments["seed"] = int(seed)

            result = fal_client.subscribe(
                MODEL_ID,
                arguments=arguments,
            )

            output_files = result.get("images") or []
            if not output_files:
                raise RuntimeError(
                    f"fal.ai returned no images: {result}"
                )

            outputs = []
            for item in output_files[:4]:
                url = item.get("url") if isinstance(item, dict) else None
                if not url:
                    raise RuntimeError(
                        f"Unexpected fal.ai image output: {item}"
                    )
                outputs.append(_url_to_tensor(url))

        # Keep stable four output sockets; pad with first if fewer.
        first = outputs[0]
        while len(outputs) < 4:
            outputs.append(first)

        if len(output_files) > 4:
            print(
                f"[FalFluxKontextEdit] warning: fal returned "
                f"{len(output_files)} images, node only exposes 4"
            )

        return tuple(outputs[:4])


NODE_CLASS_MAPPINGS = {
    NODE_NAME: FalFluxKontextEdit,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    NODE_NAME: "Fal Flux Kontext Edit",
}
