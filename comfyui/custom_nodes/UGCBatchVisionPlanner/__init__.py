"""UGC Batch Vision Planner — batched version of UGCVisionPlanner.

Iterates over the batch dimension of product_image and produces a list of N
structured prompts (one per image) instead of a single text. Per-image
behavior is identical to UGCVisionPlanner; only the output shape changes.
"""

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
