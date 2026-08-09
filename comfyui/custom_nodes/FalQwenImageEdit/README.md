# FalQwenImageEdit — ComfyUI custom node

Minimal ComfyUI custom node for calling fal.ai's `fal-ai/qwen-image-2/edit`.

## V1 inputs

- Required: product image
- Optional: reference design
- Optional: additional object/product image
- 1–4 generated candidates
- PNG/JPEG/WEBP
- Prompt expansion and safety checker controls

Qwen Image 2 Edit accepts 1–3 reference images. This node maps them as:

1. Product
2. Reference design
3. Additional object

## Install

Copy `FalQwenImageEdit` into:

```text
ComfyUI/custom_nodes/FalQwenImageEdit/
```

Install dependencies with the Python environment used by ComfyUI:

```bash
pip install -r ComfyUI/custom_nodes/FalQwenImageEdit/requirements.txt
```

Set your fal.ai key before starting ComfyUI.

Linux/macOS:

```bash
export FAL_KEY="YOUR_FAL_KEY"
python main.py
```

Windows PowerShell:

```powershell
$env:FAL_KEY="YOUR_FAL_KEY"
python main.py
```

For the Windows portable build, use its embedded Python:

```powershell
.\python_embeded\python.exe -m pip install -r .\ComfyUI\custom_nodes\FalQwenImageEditequirements.txt
```

## First workflow

Import `workflow_v1_product_to_4_candidates.json`.

Graph:

```text
Load Product Image
        |
        v
Fal Qwen Image 2 Edit
   |    |    |    |
   v    v    v    v
 Save Save Save Save
```

The optional reference/object sockets are intentionally not connected in V1.

## Important

- Inputs are uploaded to fal's CDN before inference.
- fal CDN URLs are publicly accessible while retained.
- V1 uses only the first image from a ComfyUI batch.
- The node waits synchronously for fal.ai to finish.
- This is a prototype, not yet a production job-queue implementation.
- Product preservation is prompt-based in V1. If packaging/logo fidelity is not good enough, V2 should add segmentation/masking and composite the original product over the generated scene.
