# ComfyUI-pdf-to-images — ComfyUI custom node

Renders every page of a PDF as a high-resolution image and returns the
pages as a single 4D `IMAGE` batch tensor `[B, H, W, C]` that plugs
straight into ComfyUI's `SaveImage` node.

The extracted pages are also written to disk under
`<input_dir>/<pdf_basename>/page_NNNN.<ext>` so they can be previewed in
any image viewer.

## Inputs

| Param | Type | Default | Note |
| --- | --- | --- | --- |
| `pdf_path` | STRING | `""` | Absolute path, or bare filename (resolved against ComfyUI's `input/` dir) |
| `dpi` | INT 72-600 | `300` | Print-quality default. 600 is high-detail (≈4× the file size) |
| `image_format` | combo `[png, jpeg, webp]` | `png` | Lossless = `png` |
| `start_page` | INT | `0` | 0-based page index |
| `max_pages` | INT | `0` | `0` = all pages |
| `jpeg_quality` | INT 1-100 | `95` | Used for `jpeg` / `webp` |
| `force_grayscale` | BOOLEAN | `False` | Saves as 1-channel `L`, then upcasts to RGB for the IMAGE socket |

## Outputs

| Socket | Type | Note |
| --- | --- | --- |
| `images` | IMAGE (list) | Each element is `[1, H, W, 3]`, ready for `SaveImage` with `is_input_list=True` (e.g. `SaveImageDataSetToFolder`) |
| `count` | INT | Total pages rendered |
| `folder_name` | STRING | Sanitized folder name (e.g. `MyPDF`), wired to `SaveImage` folder input |
| `folder_path` | STRING | Absolute path to the saved folder (e.g. `C:\...\ComfyUI\input\MyPDF`) |

`folder_name` / `folder_path` are empty strings on hard failures (bad
path, encrypted PDF) or match the sanitized PDF basename / absolute path
on success.

## Install

Drop `ComfyUI-pdf-to-images/` into `ComfyUI/custom_nodes/`, then:

```bash
pip install -r ComfyUI/custom_nodes/ComfyUI-pdf-to-images/requirements.txt
```

Restart ComfyUI so the node appears in the menu under **image**.

## Caveats

- PyMuPDF is imported lazily inside the node function. If it is missing,
  the first invocation fails with a clear `RuntimeError` instead of the
  node package refusing to load.
- Pages with mixed dimensions are skipped (PDFs with all-same-size pages
  produce a single aligned batch).
- DPI 300 is print-quality; bump to 400-600 for fine text/diagrams, drop
  to 150 to save VRAM on large pages.