# Architecture

This document describes the folder layout, data flow, and how the install
contracts in `manifest.json` map to on-disk reality.

## Layers

```
┌────────────────────────────────────────────────────────────┐
│  install.bat (provisioner)                                 │
│    - creates venv_comfyui/                                 │
│    - clones ComfyUI/                                       │
│    - pip installs core deps + custom node deps             │
└────────────────────────────────────────────────────────────┘
                         ↓
┌────────────────────────────────────────────────────────────┐
│  ComfyUI/  (core source, git-cloned, gitignored)           │
│    - main.py / comfy/ package                              │
│    - models/ (symlink or extra_model_paths into ../models) │
│    - output/  (symlink or extra_model_paths into ../output)│
│    - input/   (symlink or extra_model_paths into ../input) │
│    - custom_nodes/  (cloned from custom_nodes.txt)         │
└────────────────────────────────────────────────────────────┘
                         ↓
┌────────────────────────────────────────────────────────────┐
│  start.bat (launcher)                                      │
│    - activates venv_comfyui/Scripts/activate.bat           │
│    - runs `python -m comfyui --listen 127.0.0.1 --port X`  │
└────────────────────────────────────────────────────────────┘
```

## Why models/ lives at the repo root, not inside ComfyUI/

ComfyUI's stock layout puts `models/` inside the cloned source. We hoist it
to the repo root for three reasons:

1. **Reproducibility.** `ComfyUI/` is gitignored and re-cloned; `models/`
   is the user-owned asset layer and stays put across reinstalls.
2. **Reusability.** Same model files can be wired into multiple apps later
   via `extra_model_paths.yaml`.
3. **Cleaner git status.** Models are big binaries — keeping them at the
   root with `.gitignore` matches the rest of the OS (HF cache, ComfyUI
   Manager) and avoids re-ignoring patterns per-node.

`install.bat` will populate `ComfyUI/extra_model_paths.yaml` to point
ComfyUI at the repo-root `models/` (handled in a future revision when
deps are installed).

## Data flow

```
User drops model file
       │
       ▼
  models/checkpoints/foo.safetensors
       │
       ▼
  ComfyUI reads via extra_model_paths.yaml
       │
       ▼
  Workflow runs in venv_comfyui
       │
       ▼
  output/{workflow-name}/{timestamp}.png
```

## Security boundaries

| Layer             | Trust    | Notes                                   |
|-------------------|----------|-----------------------------------------|
| `.env`            | secret   | gitignored, never logged                |
| `models/`         | untrusted| large binaries, gitignored              |
| `output/`         | untrusted| user-generated, gitignored              |
| `ComfyUI/`        | trusted  | pinned to upstream `master` by manifest |
| `custom_nodes/`   | mixed    | each node is its own supply chain       |
| `venv_comfyui/`   | trusted  | installed by install.bat from PyPI      |

The `0.0.0.0` vs `127.0.0.1` bind decision is in `start.bat` and is the
single biggest security switch — review it before exposing the service.
