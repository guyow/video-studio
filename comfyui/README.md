# ComfyUI App

Standarized ComfyUI install with secure env defaults and full documentation.

> **Status:** v1 skeleton. This folder contains files only — no dependencies
> installed, no ComfyUI source cloned, no models downloaded. Run `install.bat`
> when you are ready to provision the runtime.

## Folder layout

```
comfyui/
├── install.bat              # provisions venv + clones ComfyUI + installs deps
├── start.bat                # launches ComfyUI on 127.0.0.1:8188
├── stop.bat                 # kills the ComfyUI process
├── manifest.json            # pinned versions and layout contract
├── requirements.txt         # record of runtime deps (installed by install.bat)
├── custom_nodes.txt         # list of custom nodes to clone
├── .env.example             # template for secrets (HF_TOKEN, etc.)
├── .gitignore               # ignore rules
├── README.md                # this file
├── docs/
│   ├── architecture.md      # folder layout, data flow
│   ├── custom-nodes.md      # how to add/remove/pin nodes
│   └── troubleshooting.md   # common failures and fixes
├── models/                  # checkpoints, loras, vae, controlnet, ...
│   ├── checkpoints/
│   ├── loras/
│   ├── vae/
│   ├── controlnet/
│   ├── embeddings/
│   └── upscale_models/
├── output/                  # generated outputs (gitignored)
├── input/                   # uploaded inputs (gitignored)
└── user/                    # workflows, user data (gitignored)
```

## Quick start (later, when you are ready)

1. Copy `.env.example` to `.env` and fill in `HF_TOKEN` (and `CIVITAI_TOKEN` if needed).
2. Open **Git Bash** (not PowerShell) at this folder.
3. Run `install.bat` — it will:
   - Create `venv_comfyui\`
   - Clone ComfyUI core into `ComfyUI\`
   - Install PyTorch (CUDA 12.1, with CPU fallback)
   - Install ComfyUI's `requirements.txt`
   - Clone each custom node from `custom_nodes.txt`
4. Drop model files into the matching subdir under `models\`.
5. Run `start.bat` → ComfyUI opens at `http://127.0.0.1:8188`.

## Security defaults

- Bound to `127.0.0.1` by default — not reachable from the LAN.
- `.env` is git-ignored. `.env.example` shows the required keys with no values.
- Models, outputs, inputs, and the venv are all git-ignored.
- To expose on LAN, edit `start.bat` and set `COMFYUI_LISTEN=0.0.0.0`
  (only do this on a trusted network).

## Installation

Two paths — see [docs/INSTALL.md](docs/INSTALL.md) for the full guide:

- **Automatic:** run `install.bat` and let it provision everything.
  Idempotent — skips folders that already exist. May re-download
  PyTorch if your venv doesn't already have it.
- **Manual:** run each step by hand, skipping anything you've already
  done. Use this if you want full control over which packages get
  installed at which version.

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md) — full install walkthrough
- [docs/architecture.md](docs/architecture.md)
- [docs/custom-nodes.md](docs/custom-nodes.md)
- [docs/troubleshooting.md](docs/troubleshooting.md)
