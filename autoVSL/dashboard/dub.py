#!/usr/bin/env python3
"""Dashboard dub pipeline: voice-clone + speak edited script + lip-sync.

Thin orchestrator around scripts/script-swap.py stages (clone -> speak -> lipsync).
Skips the paid fal whisper stage by seeding the work dir with the free local
transcript, and invalidates stale cached VO/lipsync when the script was re-edited.

Usage: python dashboard/dub.py <video-path> --name <stem> [--tier pro|standard]
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SWAP = ROOT / "scripts" / "script-swap.py"
READY_DIR = Path("~/Desktop/liitt testimonial Ready").expanduser()


def run_stage(stage: str, extra: list[str]) -> None:
    print(f"\n=== stage: {stage} ===", flush=True)
    try:
        # hard cap: fal.ai calls occasionally hang forever — fail loudly instead of stalling the queue
        r = subprocess.run([sys.executable, str(SWAP), stage, *extra],
                           cwd=str(ROOT), timeout=1800)
    except subprocess.TimeoutExpired:
        sys.exit(f"stage '{stage}' timed out after 30 min — fal.ai did not respond. "
                 "Nothing more will be charged for this run; try again (or use the local engine).")
    if r.returncode != 0:
        # the stage already printed its own error — keep it the last thing in the log
        sys.exit(f"stage '{stage}' failed — see the error above. "
                 "Finished stages are cached and won't re-bill on the next run.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--name", required=True)
    ap.add_argument("--tier", choices=["sync3", "pro", "standard", "hummingbird",
                                       "veed", "latentsync", "musetalk"], default="pro")
    ap.add_argument("--tts", choices=["hd", "turbo", "hd25", "turbo25", "f5", "chatterbox"],
                    default="hd")
    ap.add_argument("--captions", action="store_true",
                    help="burn word-timed captions from the script after lip-sync (free, local)")
    args = ap.parse_args()

    video = Path(args.video).resolve()
    if not video.is_file():
        sys.exit(f"Video not found: {video}")
    work = ROOT / "output" / "script-swap" / args.name
    work.mkdir(parents=True, exist_ok=True)

    script = work / "script-edited.txt"
    if not script.is_file():
        sys.exit("No edited script found — use 'Edit script' in the dashboard and save first.")

    # fail fast with a clear message if the fal account can't pay (free probe, no charge)
    from fal_guard import preflight_fal
    preflight_fal("dub")

    # Seed transcript.txt from the free local transcript so nothing ever
    # triggers the paid fal whisper stage for this job.
    transcript = work / "transcript.txt"
    local_md = ROOT / "uploads" / "transcripts" / f"{Path(video).stem}.md"
    if not transcript.is_file() and local_md.is_file():
        transcript.write_text(local_md.read_text(encoding="utf-8"), encoding="utf-8")

    # Invalidate stale caches: script edited or TTS model changed -> redo VO + videos;
    # lip-sync tier changed -> redo videos only. dub-config.json records the last run's models.
    vo = work / "new-vo.mp3"
    ready = READY_DIR / f"{args.name}-ready.mp4"
    final = work / "final.mp4"
    cfg_file = work / "dub-config.json"
    prev = {}
    if cfg_file.is_file():
        try:
            prev = json.loads(cfg_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    stamp = time.strftime("%Y%m%d-%H%M%S")

    versions_file = work / "versions.json"

    def archive_videos(reason: str) -> None:
        """Archive the current render as a named version (with the models that made it)."""
        if not (ready.is_file() or final.is_file()):
            return
        print(f"{reason} -> archiving old video(s) (.{stamp})")
        try:
            versions = json.loads(versions_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            versions = {}
        meta = {"tts": prev.get("tts", "hd"), "tier": prev.get("tier", "pro")}
        if final.is_file():
            dest = work / f"final.{stamp}.mp4"
            final.rename(dest)
            versions[dest.name] = {**meta, "created": dest.stat().st_mtime}
            if ready.is_file():
                ready.unlink()          # identical to the archived final — drop the duplicate
        elif ready.is_file():
            dest = work / f"ready.{stamp}.mp4"
            shutil.move(str(ready), dest)
            versions[dest.name] = {**meta, "created": dest.stat().st_mtime}
        versions_file.write_text(json.dumps(versions, indent=1), encoding="utf-8")

    script_changed = vo.is_file() and script.stat().st_mtime > vo.stat().st_mtime
    tts_changed = vo.is_file() and prev.get("tts", "hd") != args.tts
    if script_changed or tts_changed:
        reason = "script changed" if script_changed else f"voice model changed ({prev.get('tts', 'hd')} -> {args.tts})"
        print(f"{reason} -> regenerating VO")
        vo.rename(work / f"new-vo.{stamp}.mp3")
        archive_videos(reason)
    elif prev.get("tier", "pro") != args.tier and (ready.is_file() or final.is_file()):
        archive_videos(f"lip-sync tier changed ({prev.get('tier', 'pro')} -> {args.tier})")

    if args.tts not in ("f5", "chatterbox"):    # zero-shot voices clone free in the speak stage
        run_stage("clone", [str(video), "--name", args.name])   # cached after first run (voice.json)
    run_stage("speak", [str(video), "--name", args.name, "--tts", args.tts])
    run_stage("lipsync", ["--name", args.name, "--tier", args.tier])
    cfg_file.write_text(json.dumps({"tts": args.tts, "tier": args.tier}), encoding="utf-8")

    if ready.is_file():
        shutil.copy2(ready, final)
        print(f"\nfinal video copied for dashboard playback: {final}")
        print(f"deliverable: {ready}")

    if args.captions and final.is_file():
        # chain the free caption burn (runs under the course venv for faster-whisper)
        caption_py = Path(__file__).resolve().parent / "caption.py"
        venv_py = ROOT.parent / "course_pipeline" / ".venv" / "Scripts" / "python.exe"
        if venv_py.is_file():
            print("\n=== stage: captions ===", flush=True)
            r = subprocess.run([str(venv_py), str(caption_py), "--name", args.name])
            if r.returncode != 0:
                print("caption stage failed — the uncaptioned final is still good; "
                      "use the dashboard's caption button to retry", file=sys.stderr)
        else:
            print("captions skipped: course_pipeline venv not found", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
