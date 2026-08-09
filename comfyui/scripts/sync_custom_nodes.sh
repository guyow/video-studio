#!/usr/bin/env bash
# ============================================================
# sync_custom_nodes.sh
#
# Mirror custom_nodes/ (repo level) → ComfyUI/custom_nodes/
# Mode: full replace — folder tujuan dihapus lalu dicopy ulang.
#       Aman karena ComfyUI/custom_nodes/ gitignored.
#
# Usage:
#   ./scripts/sync_custom_nodes.sh             # sync all
#   ./scripts/sync_custom_nodes.sh my_node      # sync one
#
# Run setelah edit/add/hapus node di custom_nodes/.
# ============================================================
set -euo pipefail

# ---- resolve paths ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_DIR="$APP_ROOT/custom_nodes"
DST_DIR="$APP_ROOT/ComfyUI/custom_nodes"

# ---- preflight ----
if [ ! -d "$SRC_DIR" ]; then
    echo "[sync] no $SRC_DIR/ — nothing to sync"
    exit 0
fi

if [ ! -d "$APP_ROOT/ComfyUI" ]; then
    echo "[sync] FATAL: $APP_ROOT/ComfyUI/ not found."
    echo "       Run install.bat first."
    exit 1
fi

mkdir -p "$DST_DIR"

# ---- collect targets ----
if [ $# -gt 0 ]; then
    # sync specific names only
    TARGETS=()
    for name in "$@"; do
        if [ ! -d "$SRC_DIR/$name" ]; then
            echo "[sync] WARN: $SRC_DIR/$name/ does not exist, skipping"
            continue
        fi
        TARGETS+=("$name")
    done
    if [ ${#TARGETS[@]} -eq 0 ]; then
        echo "[sync] no valid targets, exiting"
        exit 0
    fi
else
    # sync all subfolders of custom_nodes/
    TARGETS=()
    for dir in "$SRC_DIR"/*/; do
        [ -d "$dir" ] || continue
        name="$(basename "$dir")"
        # skip dotfiles / hidden
        [[ "$name" == .* ]] && continue
        TARGETS+=("$name")
    done
fi

if [ ${#TARGETS[@]} -eq 0 ]; then
    echo "[sync] no nodes found in $SRC_DIR/, nothing to do"
    exit 0
fi

# ---- perform sync ----
echo "[sync] Mirroring ${#TARGETS[@]} node(s) to $DST_DIR"
for name in "${TARGETS[@]}"; do
    src="$SRC_DIR/$name"
    dst="$DST_DIR/$name"

    if [ -e "$dst" ] || [ -L "$dst" ]; then
        echo "  - replace $name"
        rm -rf "$dst"
    else
        echo "  - copy   $name"
    fi

    # use cp -a to preserve timestamps, perms, structure
    cp -a "$src" "$dst"
done

echo "[sync] Done. Restart ComfyUI to pick up changes."
