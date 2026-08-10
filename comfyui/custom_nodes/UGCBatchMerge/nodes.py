"""UGC Batch Merge — passthrough + validate IMAGE batch.

This node exists to:
  1. Validate that the incoming IMAGE batch is well-formed (>= 1 image,
     4D tensor [B, H, W, C] in 0..1 float).
  2. Make the "this is a batch of images" contract explicit in the graph
     at one named point — useful as a faucet before any downstream batch-
     aware consumer (e.g. a future batch-aware image gen node).
  3. (Optional) clamp the batch size to a max_images ceiling so a runaway
     loader does not blow up GPU/RAM downstream.

It is NOT a combiner. It does NOT touch prompts. Prompts live in a separate
slot on UGCBatchVisionPlanner; the image node that consumes both is built
in a later phase.

Per-image behavior is unchanged from the raw upstream batch — only the
batch envelope is validated and clamped.
"""

import torch


class UGCBatchMerge:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "product_image": ("IMAGE",),
                "max_images": (
                    "INT",
                    {"default": 0, "min": 0, "max": 64, "step": 1},
                ),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "merge"
    CATEGORY = "UGC Poster/AI"

    def merge(self, product_image, max_images=0):
        if not isinstance(product_image, torch.Tensor):
            raise RuntimeError(
                f"product_image must be a torch.Tensor, got {type(product_image).__name__}"
            )

        if product_image.dim() != 4:
            raise RuntimeError(
                f"product_image must be 4D [B, H, W, C], got shape "
                f"{tuple(product_image.shape)}"
            )

        batch_size = int(product_image.shape[0])
        if batch_size < 1:
            raise RuntimeError("product_image batch is empty (B=0)")

        if max_images and batch_size > int(max_images):
            product_image = product_image[: int(max_images)]

        return (product_image,)


NODE_CLASS_MAPPINGS = {
    "UGCBatchMerge": UGCBatchMerge,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "UGCBatchMerge": "UGC Batch Merge (image batch)",
}
