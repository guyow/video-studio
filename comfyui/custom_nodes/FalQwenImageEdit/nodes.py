import os
import tempfile
from pathlib import Path

import numpy as np
import torch
from PIL import Image


NODE_NAME = "FalQwenImageEdit"
MODEL_ID = "fal-ai/qwen-image-2/edit"


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


class FalQwenImageEdit:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "product_image": ("IMAGE",),
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
                "negative_prompt": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": (
                            "distorted packaging, changed logo, misspelled text, "
                            "warped product, duplicate product, deformed object, low quality"
                        ),
                    },
                ),
                "num_images": (
                    "INT",
                    {"default": 4, "min": 1, "max": 4, "step": 1},
                ),
                "output_format": (
                    ["png", "jpeg", "webp"],
                    {"default": "png"},
                ),
                "enable_prompt_expansion": ("BOOLEAN", {"default": True}),
                "enable_safety_checker": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                "reference_design": ("IMAGE",),
                "additional_object": ("IMAGE",),
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
    CATEGORY = "fal.ai/Qwen"

    def generate(
        self,
        product_image,
        prompt,
        negative_prompt,
        num_images,
        output_format,
        enable_prompt_expansion,
        enable_safety_checker,
        reference_design=None,
        additional_object=None,
    ):
        fal_client = _require_fal()

        # V1 uses the first image from each connected ComfyUI batch.
        images = [product_image[0]]
        labels = ["Image 1 is the actual product packaging/product."]

        if reference_design is not None:
            images.append(reference_design[0])
            labels.append(
                "Image 2 is a visual reference for design, composition, lighting, and style."
            )

        if additional_object is not None:
            images.append(additional_object[0])
            labels.append(
                f"Image {len(images)} is an additional real product/object "
                "that should be placed naturally in the scene."
            )

        if len(images) > 3:
            raise ValueError(
                "Qwen Image 2 Edit accepts at most 3 reference images."
            )

        final_prompt = "\n".join(
            [
                *labels,
                "",
                "Use the reference images according to their descriptions above.",
                "The actual product must remain recognizable and commercially accurate.",
                "Do not redesign the package, logo, brand identity, or printed claims.",
                prompt.strip(),
            ]
        )

        with tempfile.TemporaryDirectory(prefix="comfy_fal_qwen_") as tmpdir:
            image_urls = [
                _upload_image(
                    fal_client, image, tmpdir, f"input_{index}"
                )
                for index, image in enumerate(images, start=1)
            ]

            arguments = {
                "prompt": final_prompt,
                "negative_prompt": negative_prompt.strip(),
                "image_urls": image_urls,
                "num_images": int(num_images),
                "output_format": output_format,
                "enable_prompt_expansion": bool(enable_prompt_expansion),
                "enable_safety_checker": bool(enable_safety_checker),
            }

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

        # Keep stable four output sockets.
        first = outputs[0]
        while len(outputs) < 4:
            outputs.append(first)

        return tuple(outputs[:4])


NODE_CLASS_MAPPINGS = {
    NODE_NAME: FalQwenImageEdit,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    NODE_NAME: "Fal Qwen Image 2 Edit",
}
