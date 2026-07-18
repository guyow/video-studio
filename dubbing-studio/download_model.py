"""
Robust XTTS v2 model downloader (resumable).

Coqui's built-in downloader has no resume support, so on a slow/flaky
connection a dropped byte near the end wastes the whole ~1.9 GB download.
This script uses huggingface_hub, which resumes partial downloads, and retries
a few times. It places the files exactly where coqui-tts expects them so the
app then loads instantly and offline.

Run:  python download_model.py
"""
import os
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
TARGET = (PROJECT_DIR / "models" / "tts"
          / "tts_models--multilingual--multi-dataset--xtts_v2")
TARGET.mkdir(parents=True, exist_ok=True)

REPO = "coqui/XTTS-v2"
# Files coqui-tts needs to load XTTS v2 for inference.
FILES = ["config.json", "model.pth", "vocab.json", "speakers_xtts.pth"]

from huggingface_hub import hf_hub_download  # noqa: E402


def main():
    for fname in FILES:
        dest = TARGET / fname
        if dest.exists() and dest.stat().st_size > 0:
            print(f"[skip] {fname} already present ({dest.stat().st_size/1e6:.1f} MB)")
            continue
        for attempt in range(1, 9):
            try:
                print(f"[get ] {fname} (attempt {attempt})...", flush=True)
                hf_hub_download(
                    repo_id=REPO,
                    filename=fname,
                    local_dir=str(TARGET),
                    local_dir_use_symlinks=False,
                )
                print(f"[done] {fname}", flush=True)
                break
            except Exception as e:  # network hiccup -> resume on next attempt
                print(f"[warn] {fname} failed: {e}\n       retrying (resumes)...",
                      flush=True)
                time.sleep(3)
        else:
            print(f"[FAIL] could not download {fname} after retries.")
            sys.exit(1)

    # coqui-tts writes this marker after the CPML license is accepted; create it
    # so the app never blocks on the interactive license prompt.
    (TARGET / "tos_agreed.txt").write_text(
        "I have read, understood and agreed to the Terms and Conditions.",
        encoding="utf-8",
    )
    print("\nAll model files present in:", TARGET)


if __name__ == "__main__":
    main()
