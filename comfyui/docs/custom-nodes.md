# Custom nodes

Custom nodes extend ComfyUI's node graph. Each one is a self-contained
Python package, usually distributed via GitHub.

## How nodes are managed here

We do **not** use ComfyUI Manager. Instead, every custom node is declared
in `custom_nodes.txt` (one URL per line) and cloned by `install.bat` into
`ComfyUI/custom_nodes/`.

This gives us:

- **Reproducibility.** The list of nodes is a tracked file. New machine,
  same nodes.
- **Version pinning.** Lines support `@<tag_or_branch>` for pinning.
- **No surprise updates.** `install.bat` only clones if the folder is
  missing. To update, you do it explicitly.

## Format

```
# Comments start with #
https://github.com/owner/repo
https://github.com/owner/repo@v1.2.3
https://github.com/owner/repo@my-fork-branch
```

## Adding a node

1. Find the GitHub URL of the node.
2. Add a line to `custom_nodes.txt`.
3. Run `install.bat`. It clones the node, then `pip install -r
   ComfyUI/custom_nodes/<name>/requirements.txt` (future revision).
4. Restart ComfyUI via `start.bat`.

## Removing a node

1. Delete the line from `custom_nodes.txt`.
2. Delete `ComfyUI/custom_nodes/<name>\`.
3. Restart ComfyUI.

## Pinning a node to a version

Replace the URL line with `<url>@<tag>`:

```
https://github.com/cubiq/ComfyUI_IPAdapter_plus@v1.0.0
```

To upgrade later, edit the tag and re-clone (delete folder first, then
re-run `install.bat`).

## Trust considerations

Each custom node runs arbitrary Python inside the venv. A malicious node
has the same access as the ComfyUI process. Before adding a node:

- Check the author and the issue tracker.
- Read the node's `requirements.txt` — it can pull arbitrary PyPI packages.
- Prefer nodes that are actively maintained and widely used.

The `custom_nodes.txt` file is the single source of truth for the
node supply chain in this app. Treat changes to it like dependency
changes in any other project.
