# INSTALL.md — ComfyUI Setup Guide

This guide covers two paths:

- **Path A — Automatic:** run `install.bat` and let it provision everything.
- **Path B — Manual:** run each step by hand, skipping anything you've
  already done. Use this if `install.bat` doesn't fit your environment,
  or if you want fine-grained control (e.g. you already have PyTorch at
  a specific CUDA version).

Both paths converge to the same end state: a working ComfyUI install at
`http://127.0.0.1:8188`, driven by `start.bat` + `scripts/launch_comfyui.py`.

---

## Prerequisites

Before either path, you need:

| Tool | Why | Already installed? |
|---|---|---|
| **Git for Windows** | clones ComfyUI + custom nodes | `git --version` |
| **uv** (Python package manager) | creates a clean Python 3.13 venv | `uv --version` |
| **NVIDIA GPU driver** (Win10/11) | lets CUDA talk to your GPU | `nvidia-smi` |
| **Visual C++ Build Tools** (optional) | some custom nodes compile C extensions | only if a custom node needs it |

`install.bat` does **not** install Git, uv, or the NVIDIA driver — those
are system-level. If `git`/`uv` aren't on PATH, install them first.

---

## Path A — Automatic (`install.bat`)

```bash
cd C:\Kerjaan\comfyui-research\comfyui
./install.bat
```

`install.bat` is **idempotent**: it skips anything that already exists.

| Step | What it does | Skipped if... |
|---|---|---|
| 0 | Sanity-checks `uv` and `git` on PATH | (always runs) |
| 0.5 | Copies `.env.example` → `.env` if missing | `.env` already exists |
| 1 | `uv venv --python 3.13 .venv` | `.venv/` already exists |
| 2 | `git clone --depth 1 https://github.com/Comfy-Org/ComfyUI.git` | `ComfyUI/` already exists |
| 3 | `pip install --upgrade pip` | (always runs, fast) |
| 4 | `pip install torch torchvision torchaudio --index-url .../cu121` | (see note below) |
| 5 | `pip install -r ComfyUI/requirements.txt` + `-r requirements.txt` | (see note below) |
| 6 | Clones each URL from `custom_nodes.txt` | folder already exists |

**Note on steps 4 & 5 (pip):** `install.bat` does not currently check
whether PyTorch/ComfyUI deps are already installed at the right version.
If you already have them, `pip install` will either no-op (same version)
or upgrade/downgrade (different version — may re-download the ~1.9 GB
PyTorch wheel). If you want to **fully skip** these, use Path B.

After install finishes:
1. Edit `.env` with your real `FAL_KEY`, `HF_TOKEN`, etc.
2. Drop model files into `models/checkpoints/`, `models/loras/`, etc.
3. Run `start.bat`.

---

## Path B — Manual (skip what's done)

Run only the steps you need. Every step has a one-liner and a
"skip this if..." check.

### 1. Create the venv (skip if `.venv/` already exists)

```bash
cd C:\Kerjaan\comfyui-research\comfyui
uv venv --python 3.13 .venv
```

Verify:
```bash
.venv\Scripts\python.exe --version    # Python 3.13.x
.venv\Scripts\python.exe -c "import sys; print(sys.executable)"
# should print ...\comfyui\.venv\Scripts\python.exe
```

### 2. Clone ComfyUI (skip if `ComfyUI/main.py` already exists)

```bash
git clone --depth 1 https://github.com/Comfy-Org/ComfyUI.git ComfyUI
```

Verify:
```bash
ls ComfyUI/main.py                      # file exists
cat ComfyUI/requirements.txt | head -5  # numpy, Pillow, av, ...
```

### 3. Clear PYTHONPATH contamination (this host only)

> **Why:** this machine has `PYTHONPATH` set globally to the Hermes venv
> path. Every `pip install` and `python` call below MUST unset it, or
> packages will land in the wrong site-packages.

```bash
set PYTHONPATH=
.venv\Scripts\activate.bat
set PYTHONPATH=
```

From here on, prefix every Python call with `set PYTHONPATH=` (Windows)
or `PYTHONPATH=` (bash). Or activate the venv and stay in that shell.

### 4. Upgrade pip (always)

```bash
python -m pip install --upgrade pip
```

### 5. Install PyTorch (skip if torch is already at the right CUDA build)

**Skip this if:** `.venv\Scripts\python.exe -c "import torch; print(torch.__version__, torch.cuda.is_available())"` prints something like `2.x.x+cu121 True`.

**For NVIDIA + CUDA 12.1:**
```bash
python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
```

**For NVIDIA + CUDA 12.4 / 12.6 / 13.0:** swap `cu121` for the matching tag at https://pytorch.org/get-started/locally/.

**For AMD (ROCm):** swap the index-url per https://pytorch.org/get-started/locally/.

**For CPU only (testing):** drop the `--index-url` flag entirely.

Verify:
```bash
python -c "import torch; print(torch.__version__, 'cuda:', torch.cuda.is_available())"
# Expected: 2.x.x+cu121 cuda: True
```

### 6. Install ComfyUI deps (skip if you just want to re-run install.bat)

**Skip this if:** `pip show comfy-cli` (or any other ComfyUI dep) returns
the version pinned in `ComfyUI/requirements.txt`.

```bash
python -m pip install -r ComfyUI\requirements.txt
```

### 7. Install launcher utilities (always — small)

```bash
python -m pip install -r requirements.txt
```

This installs `python-dotenv`, which `scripts/launch_comfyui.py` needs.

### 8. Clone custom nodes (skip if `ComfyUI/custom_nodes/<name>/` already exists with `__init__.py`)

For each URL in `custom_nodes.txt`:
```bash
git clone <url> ComfyUI\custom_nodes\<name>
```

Or in a loop (bash):
```bash
while IFS= read -r line; do
  case "$line" in \#*|"") continue;; esac
  name=$(basename "$line" .git)
  if [ -d "ComfyUI/custom_nodes/$name" ]; then
    echo "skip $name (already exists)"
  else
    git clone "$line" "ComfyUI/custom_nodes/$name"
  fi
done < custom_nodes.txt
```

**Trap:** a partial clone (only `.git/` present, no `__init__.py`) will
make ComfyUI log `(IMPORT FAILED)`. Check inside the folder before
re-cloning — if it's incomplete, `rm -rf` it and re-clone.

### 9. Wire the repo-root `models/` into ComfyUI (recommended)

By default ComfyUI looks for models inside `ComfyUI/models/`. Our layout
keeps models at the repo root (`comfyui/models/`) so they survive
re-clones. To point ComfyUI at the repo-root models folder, write
`ComfyUI/extra_model_paths.yaml`:

```bash
cp ComfyUI\extra_model_paths.yaml.example ComfyUI\extra_model_paths.yaml
```

Then edit `extra_model_paths.yaml` so it includes:
```yaml
my_models:
  base_path: C:\Kerjaan\comfyui-research\comfyui\models
  checkpoints: checkpoints
  loras: loras
  vae: vae
  controlnet: controlnet
  embeddings: embeddings
  upscale_models: upscale_models
```

After this, files dropped into `comfyui/models/checkpoints/foo.safetensors`
are visible to ComfyUI's loader. Restart ComfyUI to pick up the change.

### 10. (Optional) Drop model files

Place checkpoints/LoRAs/VAEs into the matching `models/<type>/` folder.
The directory layout is:

```
models\
├── checkpoints\    # SD 1.5, SDXL, Flux, Hunyuan, etc.
├── loras\          # LoRAs
├── vae\            # VAE files
├── controlnet\     # ControlNet models
├── embeddings\     # textual inversions
├── upscale_models\ # ESRGAN, etc.
├── clip\           # text encoders (Flux, Hunyuan)
├── clip_vision\    # CLIP vision encoders (IPAdapter)
└── diffusion_models\  # some architectures store the base here
```

**For large downloads** (multi-GB safetensors), use a single foreground
`curl` with no `-C -` (host quirk: resume appends corrupted bytes).
See `docs/troubleshooting.md` § curl corruption if a download looks wrong.

### 11. Verify the install (smoke test)

Before running `start.bat`, confirm the venv + ComfyUI are aligned:

```bash
.venv\Scripts\python.exe -c "import torch, comfy; print('torch', torch.__version__, '| comfy', comfy.__version__)"
```

If `comfy` fails to import, the ComfyUI source is missing or the
`ComfyUI/` path isn't on `sys.path` (the launcher `cd`s into it, so
`comfy` resolves from there). Run from the repo root.

### 12. Run

```bash
./start.bat
```

You should see:
```
[launch_comfyui] Loaded env from ...\.env
[launch_comfyui] Starting ComfyUI on http://127.0.0.1:8188
```

Open `http://127.0.0.1:8188` in a browser. If a model is in
`models/checkpoints/`, it will appear in the checkpoint loader dropdown.

---

## After install

| Task | Where |
|---|---|
| Update ComfyUI | `cd ComfyUI && git pull` |
| Add a custom node | edit `custom_nodes.txt`, re-clone, restart |
| Update a custom node | `cd ComfyUI/custom_nodes/<name> && git pull`, restart |
| Add a model | drop into `models/<type>/`, restart |
| Edit secrets | edit `.env`, restart |
| Expose on LAN | edit `scripts/launch_comfyui.py` → `DEFAULT_LISTEN = "0.0.0.0"` |

---

## Uninstall / cleanup

- **Remove the venv:** `rm -rf .venv` (deps gone, source stays)
- **Remove ComfyUI source:** `rm -rf ComfyUI` (next install re-clones)
- **Remove a custom node:** `rm -rf ComfyUI\custom_nodes\<name>` + delete its line in `custom_nodes.txt`
- **Remove everything (fresh start):** `rm -rf .venv ComfyUI ComfyUI\custom_nodes\* .env` then re-run `install.bat`

Models, outputs, inputs, and `user/` are preserved across reinstalls
unless you delete them.

---

## See also

- `docs/architecture.md` — folder layout, data flow, security boundaries
- `docs/custom-nodes.md` — how to add/remove/pin custom nodes
- `docs/troubleshooting.md` — common failures (CUDA, port, OOM, HF 401)
- `README.md` — top-level project overview
