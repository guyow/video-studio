"""Liitt Layout Compositor — ComfyUI custom node.

Thin wrapper around layout_compositor.render_layout. Accepts an IMAGE tensor
from FalNanoBanana2Edit (background scene only), the UGCVisionPlanner prompt +
copy_elements outputs, and overlays deterministic brand text + wordmark.

Default paths resolve relative to nodes.py location:
  nodes.py             -> parents[0] = LiittCompositor/
                          parents[1] = custom_nodes/
                          parents[2] = comfyui/
                          parents[3] = video-studio/  (repo root)
  -> parents[3]/comfyui/brand/liitt_layout_templates-revised.json
  -> parents[3]/comfyui/brand/
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import torch
from PIL import Image

from .layout_compositor import CompositorError, render_layout

NODE_NAME = "LiittCompositor"

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LAYOUT_PATH = _REPO_ROOT / "brand" / "liitt_layout_templates-revised.json"
DEFAULT_BRAND_DIR = _REPO_ROOT / "brand"


# ---------------------------------------------------------------------------
# tensor <-> PIL
# ---------------------------------------------------------------------------


def _tensor_to_pil(image_tensor: torch.Tensor) -> Image.Image:
    """Convert one ComfyUI IMAGE tensor [H,W,C] in 0..1 to RGB PIL."""
    arr = image_tensor.detach().cpu().clamp(0, 1).numpy()
    arr = (arr * 255.0).round().astype(np.uint8)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    if arr.shape[-1] == 4:
        return Image.fromarray(arr, mode="RGBA").convert("RGB")
    return Image.fromarray(arr, mode="RGB")


def _pil_to_tensor(pil_img: Image.Image) -> torch.Tensor:
    """Convert RGB/RGBA PIL to ComfyUI IMAGE tensor [1,H,W,C] in 0..1."""
    if pil_img.mode == "RGBA":
        arr = np.asarray(pil_img)
    else:
        arr = np.asarray(pil_img.convert("RGB"))
    arr = arr.astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None, ...]


# ---------------------------------------------------------------------------
# node class
# ---------------------------------------------------------------------------


class LiittCompositor:
    """Composite brand text + wordmark onto an AI-generated background."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "bg_image": ("IMAGE",),
                "copy_elements": (
                    "STRING",
                    {
                        "multiline": True,
                        "forceInput": True,
                        "default": "",
                    },
                ),
                "chosen_layout": (
                    "STRING",
                    {
                        "multiline": True,
                        "forceInput": True,
                        "default": "",
                    },
                ),
                "layout_json_path": (
                    "STRING",
                    {
                        "default": str(DEFAULT_LAYOUT_PATH),
                        "multiline": False,
                    },
                ),
                "brand_dir": (
                    "STRING",
                    {
                        "default": str(DEFAULT_BRAND_DIR),
                        "multiline": False,
                    },
                ),
                "canvas_preset": (
                    ["ig_feed", "tiktok"],
                    {"default": "ig_feed"},
                ),
            },
            "optional": {
                "scrim": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("poster",)
    FUNCTION = "composite"
    CATEGORY = "UGC Poster/Composite"

    def composite(
        self,
        bg_image,
        copy_elements,
        chosen_layout,
        layout_json_path,
        brand_dir,
        canvas_preset,
        scrim=False,
    ):
        layout_p = (
            Path(layout_json_path)
            if layout_json_path and layout_json_path.strip()
            else DEFAULT_LAYOUT_PATH
        )
        brand_p = (
            Path(brand_dir) if brand_dir and brand_dir.strip() else DEFAULT_BRAND_DIR
        )

        # bg_image is a batched tensor [N,H,W,C]; use the first image.
        first = bg_image[0]
        bg_pil = _tensor_to_pil(first)

        try:
            result = render_layout(
                bg_image=bg_pil,
                chosen_layout=chosen_layout or "",
                copy_elements=copy_elements or "",
                layout_json_path=layout_p,
                canvas_preset=canvas_preset,
                brand_dir=brand_p,
                scrim=bool(scrim),
            )
        except CompositorError as e:
            raise RuntimeError(f"[LiittCompositor] {e}") from e

        return (_pil_to_tensor(result),)


NODE_CLASS_MAPPINGS = {
    NODE_NAME: LiittCompositor,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    NODE_NAME: "Liitt Layout Compositor",
}
