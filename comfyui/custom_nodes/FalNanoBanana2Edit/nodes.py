import os
import tempfile
from pathlib import Path

import numpy as np
import torch
from PIL import Image

NODE_NAME = "FalNanoBanana2Edit"
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


def _tensor_to_pil(image_tensor: torch.Tensor) -> Image.Image:
    """Convert one ComfyUI IMAGE tensor [H,W,C] in 0..1 to RGB PIL."""
    image = image_tensor.detach().cpu().clamp(0, 1).numpy()
    image = (image * 255.0).round().astype(np.uint8)
    if image.shape[-1] == 4:
        return Image.fromarray(image, mode="RGBA").convert("RGB")
    return Image.fromarray(image, mode="RGB")


def _upload_image(
    fal_client, image_tensor: torch.Tensor, tmpdir: str, name: str
) -> str:
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


class FalNanoBanana2Edit:
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
                "num_images": (
                    "INT",
                    {"default": 1, "min": 1, "max": 4, "step": 1},
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
                "reference_design": ("IMAGE",),
                "additional_object": (
                    "IMAGE",
                    {"forceInput": True, "multiline": True, "io": True},
                ),
                "audio_url": ("STRING", {"default": ""}),
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
    CATEGORY = "fal.ai/NanoBanana"

    def generate(
        self,
        product_image,
        prompt,
        num_images,
        resolution,
        aspect_ratio,
        limit_generations,
        enable_web_search,
        thinking_level,
        safety_tolerance,
        reference_design=None,
        additional_object=None,
        audio_url="",
        seed=0,
    ):
        fal_client = _require_fal()

        images = [product_image[0]]
        labels = ["Image 1 is the product to be advertised / re-contextualized."]

        if reference_design is not None:
            images.append(reference_design[0])
            labels.append(
                "Image 2 is a brand design / reference to integrate "
                "(logo, packaging, layout, or visual reference)."
            )

        if additional_object is not None:
            images.append(additional_object[0])
            labels.append(
                f"Image {len(images)} is an additional real product / object "
                "that should be placed naturally in the scene "
                "(e.g. the actual candy, the contents of the package, etc.)."
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

        with tempfile.TemporaryDirectory(prefix="comfy_fal_nano_") as tmpdir:
            image_urls = [
                _upload_image(fal_client, image, tmpdir, f"input_{index}")
                for index, image in enumerate(images, start=1)
            ]

            arguments = {
                "prompt": final_prompt,
                "image_urls": image_urls,
                "num_images": int(num_images),
                "resolution": resolution,
                "aspect_ratio": aspect_ratio,
                "limit_generations": bool(limit_generations),
                "enable_web_search": bool(enable_web_search),
                "safety_tolerance": str(safety_tolerance),
            }

            if thinking_level and thinking_level != "none":
                arguments["thinking_level"] = thinking_level

            # Only forward audio_url when it looks like a real URL
            # (fal rejects placeholders like "randomize" with
            # "Invalid URL scheme ':'" — see
            # fal_client.client.FalClientHTTPError). Empty / non-URL
            # strings are dropped so the call succeeds for the image-only
            # path that this node is actually used for.
            audio_url_clean = (audio_url or "").strip()
            if audio_url_clean and audio_url_clean.lower() != "randomize":
                lowered = audio_url_clean.lower()
                if (
                    lowered.startswith("http://")
                    or lowered.startswith("https://")
                    or lowered.startswith("data:")
                ):
                    arguments["audio_url"] = audio_url_clean
                else:
                    print(
                        "[FalNanoBanana2Edit] ignoring non-URL audio_url: "
                        f"{audio_url_clean!r}"
                    )

            if int(seed) > 0:
                arguments["seed"] = int(seed)

            result = fal_client.subscribe(
                MODEL_ID,
                arguments=arguments,
            )

            output_files = result.get("images") or []
            if not output_files:
                raise RuntimeError(f"fal.ai returned no images: {result}")

            outputs = []
            for item in output_files[:4]:
                url = item.get("url") if isinstance(item, dict) else None
                if not url:
                    raise RuntimeError(f"Unexpected fal.ai image output: {item}")
                outputs.append(_url_to_tensor(url))

        # Keep stable four output sockets; pad with first if fewer.
        first = outputs[0]
        while len(outputs) < 4:
            outputs.append(first)

        if len(output_files) > 4:
            print(
                f"[FalNanoBanana2Edit] warning: fal returned "
                f"{len(output_files)} images, node only exposes 4"
            )

        return tuple(outputs[:4])


NODE_CLASS_MAPPINGS = {
    NODE_NAME: FalNanoBanana2Edit,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    NODE_NAME: "Fal Nano Banana 2 Edit",
}
