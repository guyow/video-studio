"""
ComfyUI Custom Node: PDF to Batch Images
Renders every page of a PDF as a high-resolution image, saves them under
<input_dir>/<pdf_basename>/, and outputs the pages as a list of single-image
tensors [1, H, W, 3] — same convention as
ComfyUI-batching-nodes/batch_image_loader.py. Plugs straight into
SaveImage nodes that have is_input_list=True (e.g. SaveImageDataSetToFolder).
"""

import os
import re

import numpy as np
import torch
import folder_paths
from PIL import Image

NODE_NAME = "PdfToBatchImages"


def _require_pymupdf():
    """Import PyMuPDF at call time so the node package can load even before
    the dependency is installed; the user gets a clean RuntimeError on first
    invocation instead of a broken registration."""
    try:
        import pymupdf as fitz  # PyMuPDF >= 1.24
        return fitz
    except ImportError:
        pass
    try:
        import fitz  # PyMuPDF < 1.24
        return fitz
    except ImportError as exc:
        raise RuntimeError(
            "[PDF2BatchImages] PyMuPDF is not installed. "
            "Install it with: pip install -r ComfyUI/custom_nodes/"
            "ComfyUI-pdf-to-images/requirements.txt"
        ) from exc


def _sanitize_folder_name(name: str) -> str:
    """Strip the .pdf extension and replace characters that are invalid
    in Windows folder names with underscores."""
    name = os.path.splitext(name)[0]
    # Invalid on Windows: < > : " / \ | ? * and control chars.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    # Collapse runs of underscores and trim leading/trailing whitespace + dots.
    name = re.sub(r'_+', '_', name).strip(' ._')
    return name or "pdf"


def _pixmap_to_pil(pix, force_grayscale):
    """Convert a PyMuPDF pixmap to a PIL Image (RGB or L)."""
    if force_grayscale or pix.n < 3:
        return Image.frombytes("L", (pix.width, pix.height), pix.samples)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


class PdfToBatchImagesNode:
    """
    PDF -> batch of page images.

    Renders every page of a PDF at a configurable DPI (default 300, max 600)
    and returns the pages as a list of single-image tensors, one [1, H, W, 3]
    per page, in the format ComfyUI's SaveImage node (with is_input_list=True)
    expects.

    Saved files live under: <input_dir>/<pdf_basename>/page_NNNN.<ext>
    `folder_name` (sanitized basename) and `folder_path` (absolute path) are
    exposed as outputs so SaveImage can pick up the destination folder
    without retyping.
    """

    @classmethod
    def INPUT_TYPES(cls):
        input_dir = folder_paths.get_input_directory()
        return {
            "required": {
                "pdf_path": ("STRING", {
                    "multiline": False,
                    "default": "",
                }),
                "dpi": ("INT", {
                    "default": 300,
                    "min": 72,
                    "max": 600,
                    "step": 1,
                }),
                "image_format": (["png", "jpeg", "webp"], {
                    "default": "png",
                }),
            },
            "optional": {
                "start_page": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 10000,
                    "step": 1,
                }),
                "max_pages": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 10000,
                    "step": 1,
                }),
                "jpeg_quality": ("INT", {
                    "default": 95,
                    "min": 1,
                    "max": 100,
                    "step": 1,
                }),
                "force_grayscale": ("BOOLEAN", {
                    "default": False,
                }),
            },
        }

    RETURN_TYPES = ("IMAGE", "INT", "STRING", "STRING")
    RETURN_NAMES = ("images", "count", "folder_name", "folder_path")
    OUTPUT_NODE = False
    OUTPUT_IS_LIST = (True, False, False, False)

    FUNCTION = "pdf_to_images"
    CATEGORY = "image"

    def pdf_to_images(
        self,
        pdf_path,
        dpi=300,
        image_format="png",
        start_page=0,
        max_pages=0,
        jpeg_quality=95,
        force_grayscale=False,
    ):
        """Render a PDF's pages to images, save them under
        input/<pdf_name>/, and return them as a list of [1, H, W, 3] tensors."""

        # ---------- Resolve input path ----------
        if not pdf_path or not pdf_path.strip():
            print(f"[{NODE_NAME}] pdf_path is empty.")
            return ([self._empty_batch()], 0, "", "")

        pdf_path = pdf_path.strip()
        input_dir = folder_paths.get_input_directory()

        # If user typed a bare filename, resolve against ComfyUI's input dir
        # (where the upload button drops files).
        if not os.path.isabs(pdf_path) and not os.path.exists(pdf_path):
            candidate = os.path.join(input_dir, pdf_path)
            if os.path.exists(candidate):
                pdf_path = candidate

        if not os.path.isfile(pdf_path):
            print(f"[{NODE_NAME}] PDF not found: {pdf_path}")
            return ([self._empty_batch()], 0, "", "")

        if not pdf_path.lower().endswith(".pdf"):
            print(f"[{NODE_NAME}] Not a PDF file: {pdf_path}")
            return ([self._empty_batch()], 0, "", "")

        # ---------- Prepare output folder ----------
        pdf_basename = os.path.basename(pdf_path)
        folder_name = _sanitize_folder_name(pdf_basename)
        output_dir = os.path.join(input_dir, folder_name)
        os.makedirs(output_dir, exist_ok=True)

        # ---------- Open PDF ----------
        try:
            fitz = _require_pymupdf()
            doc = fitz.open(pdf_path)
        except RuntimeError as e:
            print(f"[{NODE_NAME}] {e}")
            return ([self._empty_batch()], 0, folder_name, output_dir)
        except Exception as e:
            print(f"[{NODE_NAME}] Failed to open PDF: {e}")
            return ([self._empty_batch()], 0, folder_name, output_dir)

        if doc.is_encrypted:
            print(f"[{NODE_NAME}] PDF is encrypted/password-protected: {pdf_path}")
            doc.close()
            return ([self._empty_batch()], 0, folder_name, output_dir)

        total_pages = doc.page_count
        if total_pages == 0:
            print(f"[{NODE_NAME}] PDF has no pages: {pdf_path}")
            doc.close()
            return ([self._empty_batch()], 0, folder_name, output_dir)

        # ---------- Page range ----------
        start_page = max(0, min(int(start_page), total_pages))
        if max_pages and int(max_pages) > 0:
            end_page = min(total_pages, start_page + int(max_pages))
        else:
            end_page = total_pages
        end_page = max(start_page, end_page)

        # ---------- Render loop ----------
        # PDF native resolution is 72 DPI. DPI / 72 = render scale.
        zoom = float(dpi) / 72.0
        matrix = fitz.Matrix(zoom, zoom)

        ext = image_format.lower()
        save_kwargs = {}
        if ext in ("jpeg", "webp"):
            save_kwargs["quality"] = int(jpeg_quality)

        # Output as list of [1, H, W, 3] tensors (same convention as
        # ComfyUI-batching-nodes/batch_image_loader.py). Save nodes with
        # is_input_list=True expect this shape per element.
        tensors = []  # list of [1, H, W, 3] per page
        reference_hw = None

        for page_index in range(start_page, end_page):
            try:
                page = doc.load_page(page_index)
                # alpha=False strips alpha so output is opaque.
                pix = page.get_pixmap(matrix=matrix, alpha=False)
            except Exception as e:
                print(f"[{NODE_NAME}] Failed to render page {page_index + 1}: {e}")
                continue

            img = _pixmap_to_pil(pix, force_grayscale)

            # ComfyUI IMAGE tensors are always 3-channel; convert L -> RGB.
            if img.mode == "L":
                img = img.convert("RGB")

            hw = (img.height, img.width)
            if reference_hw is None:
                reference_hw = hw
            elif hw != reference_hw:
                # PDFs sometimes have pages of different sizes; skip
                # rather than produce a misaligned batch.
                print(
                    f"[{NODE_NAME}] Skipping page {page_index + 1}: "
                    f"size {hw} != {reference_hw}"
                )
                continue

            # ---------- Save to disk ----------
            page_filename = f"page_{page_index + 1:04d}.{ext}"
            page_path = os.path.join(output_dir, page_filename)
            try:
                if ext == "jpeg":
                    img.save(page_path, "JPEG", **save_kwargs)
                elif ext == "webp":
                    img.save(page_path, "WEBP", **save_kwargs)
                else:
                    img.save(page_path, "PNG")
            except Exception as e:
                print(f"[{NODE_NAME}] Failed to save {page_filename}: {e}")

            # ---------- To ComfyUI IMAGE tensor ----------
            img_array = np.array(img).astype(np.float32) / 255.0
            img_tensor = torch.from_numpy(img_array)[None,]  # [1, H, W, 3]
            tensors.append(img_tensor)

        doc.close()

        if not tensors:
            print(f"[{NODE_NAME}] No pages rendered for: {pdf_path}")
            return ([self._empty_batch()], 0, folder_name, output_dir)

        count = len(tensors)
        sample = tensors[0]
        print(
            f"[{NODE_NAME}] Rendered {count} pages from "
            f"{pdf_basename} -> {output_dir} @ {dpi} DPI ({ext}) "
            f"[{sample.shape[2]}x{sample.shape[1]}]"
        )

        return (tensors, count, folder_name, output_dir)

    @staticmethod
    def _empty_batch():
        # 1x64x64x3 placeholder so downstream nodes don't crash on empty input.
        return torch.zeros((1, 64, 64, 3), dtype=torch.float32)


NODE_CLASS_MAPPINGS = {
    NODE_NAME: PdfToBatchImagesNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    NODE_NAME: "PDF to Batch Images",
}