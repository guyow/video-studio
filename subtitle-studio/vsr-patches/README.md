# VSR patches

`tools/vsr` is a clone of https://github.com/YaoFANGUK/video-subtitle-remover (v1.4.0)
and is NOT tracked in this repo (1.7 GB with models). Two local bug fixes are required
for it to work here — apply after any fresh clone:

```
cd tools/vsr
git apply ../../subtitle-studio/vsr-patches/vsr-fixes.patch
```

1. **sttn_auto_inpaint.py** — f-string used Python-3.12-only nested same quotes;
   crashes on Python 3.11.
2. **video_io.py (FramePrefetcher)** — EOF deadlock: a single end-sentinel is consumed
   once, then the next `read()` blocks forever (hits when container frame-count metadata
   overreports). Fixed with a persistent `_eof` flag.

Setup after clone: create `.venv` (py 3.11), install
`torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu121`,
then `pip install -r requirements.txt paddlepaddle`. Models ship inside the repo
(`backend/models/`). See `subtitle-studio/DEV-HANDOFF.md` §3 for the CLI.
