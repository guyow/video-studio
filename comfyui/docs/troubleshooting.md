# Troubleshooting

Common failures and how to fix them.

## `python` is not recognized

Install Python 3.10–3.12 from python.org and check **Add to PATH** during
install. Verify with:

```bash
python --version
```

## `git` is not recognized

Install Git for Windows and re-open Git Bash.

## `install.bat` fails at `pip install torch`

CUDA mismatch. The script tries CUDA 12.1 first, then CPU. If both fail,
your GPU driver may be too old. Update NVIDIA drivers, or force CPU mode
by editing `install.bat` to drop the `--index-url` flag.

## ComfyUI starts but says "no models found"

You have not dropped any model files yet. Place them in:

- `models\checkpoints\` for SD / SDXL / Flux base models
- `models\loras\` for LoRAs
- `models\vae\` for VAEs
- `models\controlnet\` for ControlNet
- `models\embeddings\` for textual inversions
- `models\upscale_models\` for upscalers

Restart ComfyUI after adding models.

## Port 8188 already in use

Run `stop.bat`. If that doesn't clear it:

```bash
netstat -ano | findstr :8188
taskkill /F /PID <pid>
```

Or change the port in `start.bat` (`COMFYUI_PORT=8189`).

## "Torch not compiled with CUDA enabled"

Your installed PyTorch is CPU-only. Reinstall with CUDA:

```bash
venv_comfyui\Scripts\activate
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
```

## Custom node import error

`ComfyUI\custom_nodes\<name>\` exists but Python can't import it. Check:

1. The node's `requirements.txt` — run it manually inside the venv.
2. The node's `__init__.py` — open it and look for `print(...)` errors at
   the top (they show in the ComfyUI console).

## HuggingFace 401 / 403

Your `HF_TOKEN` is missing or invalid. Edit `.env`, set
`HF_TOKEN=hf_xxxxxxxxxxxx`, restart ComfyUI.

## Workflow errors with "X is not in the list"

A custom node used in the workflow is not installed. Add it to
`custom_nodes.txt`, run `install.bat`, restart.

## Out of VRAM

Lower the resolution, enable `--lowvram` (in `start.bat`), or close
other GPU-using apps. For Flux, 12GB VRAM is the practical minimum.

## Where to look for logs

- ComfyUI console (the terminal running `start.bat`)
- ComfyUI's `ComfyUI\logs\` folder
- The browser devtools console (F12) for UI-side errors
