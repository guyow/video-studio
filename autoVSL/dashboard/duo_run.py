#!/usr/bin/env python3
"""Run the two-speaker interview dub end-to-end and land the result where the
rest of Video Studio expects it (workdir final.mp4 → DubSync/Captions/Exports).

Wraps scripts/script-swap-duo.py (clone both voices → per-line TTS → time-anchored
assembly → sync.so lip-sync with active-speaker detection), then:
  • archives any existing final.mp4 as final.<ts>.mp4 (+ versions.json entry)
  • copies the finished video into the workdir as final.mp4
  • writes dub-config.json {engine: fal-duo, tier}

Run under the autoVSL venv (fal_client + httpx present):
  python duo_run.py --name <stem> --tier pro|standard
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent            # autoVSL/
DUO_SCRIPT = ROOT / "scripts" / "script-swap-duo.py"
READY_DIR = Path("~/Desktop/liitt testimonial Ready").expanduser()


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", required=True)
    ap.add_argument("--tier", choices=["pro", "standard"], default="pro")
    args = ap.parse_args()

    # the interview dub is the most expensive pipeline — never start it on a dead key
    from fal_guard import preflight_fal
    preflight_fal("interview dub")

    work = ROOT / "output" / "script-swap" / args.name
    cfg_file = work / "duo-config.json"
    if not cfg_file.is_file():
        sys.exit("duo-config.json missing — run speaker detection first")
    cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
    n_seg = len(cfg.get("segments", []))
    labels = {k: v.get("label", f"Speaker {k}") for k, v in cfg.get("speakers", {}).items()}
    log(f"Interview dub: {args.name} — {n_seg} lines, "
        f"{' / '.join(labels.values())}, sync tier: {args.tier}")

    r = subprocess.run([sys.executable, str(DUO_SCRIPT), "run",
                        "--name", args.name, "--tier", args.tier], cwd=str(ROOT))
    if r.returncode != 0:
        sys.exit("interview dub failed — see the log above")

    ready = READY_DIR / f"{args.name}-ready.mp4"
    if not ready.is_file() or ready.stat().st_size == 0:
        sys.exit(f"pipeline finished but deliverable missing: {ready}")

    final = work / "final.mp4"
    if final.is_file():
        ts = time.strftime("%Y%m%d-%H%M%S")
        archived = work / f"final.{ts}.mp4"
        final.rename(archived)
        versions = work / "versions.json"
        hist = json.loads(versions.read_text(encoding="utf-8")) if versions.is_file() else []
        hist.append({"file": archived.name, "archived_at": time.time(),
                     "note": "replaced by interview (duo) dub"})
        versions.write_text(json.dumps(hist, indent=2), encoding="utf-8")
        log(f"previous final archived as {archived.name}")

    shutil.copy2(ready, final)
    (work / "dub-config.json").write_text(json.dumps(
        {"engine": "fal-duo", "tier": args.tier, "tts": "hd",
         "speakers": labels, "segments": n_seg, "ts": time.time()}, indent=2),
        encoding="utf-8")

    log("")
    log(f"deliverable: {ready}")
    log(f"final video for dashboard playback: {final}")
    log("Done ✅  (both voices cloned, each face lip-synced only during its own lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
