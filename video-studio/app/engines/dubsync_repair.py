#!/usr/bin/env python3
"""DubSync Repair — fix a finished dub without re-cloning the voice.

Operates on an existing dub workdir (autoVSL/output/script-swap/<stem>/), whose
files are the provenance record written at dub time:
    source.txt      absolute path of the source (cleaned) video
    new-vo.mp3      the synthesized voice
    final.mp4       the current dubbed output

Actions (each writes a NEW versioned file — final.mp4 is never touched; promote
the repair via the dashboard's existing promote endpoint):
    remux       put the voice back onto the source video (no lip change; instant)
    refit       time-stretch the voice (pitch-preserved) to exactly the video
                length, then remux — fixes end-of-video drift/overrun
    renorm      re-normalize final.mp4's loudness to -16 LUFS (audio re-encode only)
    relipsync   redo the lip-sync locally: Wav2Lip + GFPGAN/CodeFormer on the
                source video + existing voice (free, GPU) — fixes a bad sync
                without paying for a new dub

Run under any python with ffmpeg on PATH; relipsync spawns the dubbing-studio
venv (torch pins live there).
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def die(msg: str) -> None:
    print(f"ERROR: {msg}", flush=True)
    sys.exit(1)


def run(cmd: list[str], label: str) -> None:
    print(f"  {label}…", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        die(f"{label} failed:\n{(r.stderr or r.stdout or '')[-800:]}")


def duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return float(r.stdout.strip())
    except ValueError:
        die(f"could not read duration of {path.name}")


def guard_len(out: Path, expected: float, tol: float = 0.5) -> None:
    """The dub's video length must be preserved. If the muxed output drifts from the
    source duration by more than `tol`, something truncated it — delete the take
    rather than deliver a short video (the project's -shortest ban, applied here)."""
    got = duration(out)
    if abs(got - expected) > tol:
        out.unlink(missing_ok=True)
        die(f"output {got:.2f}s != source {expected:.2f}s — take deleted, not delivered truncated")


def atempo_chain(factor: float) -> str:
    """ffmpeg atempo accepts 0.5–100 per stage; chain stages for slow-downs."""
    parts = []
    while factor < 0.5:
        parts.append("atempo=0.5")
        factor /= 0.5
    parts.append(f"atempo={factor:.6f}")
    return ",".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("remux", "refit", "renorm", "relipsync"))
    ap.add_argument("--work", required=True, help="dub workdir (script-swap/<stem>)")
    ap.add_argument("--restorer", choices=("gfpgan", "codeformer", "none"), default="gfpgan")
    ap.add_argument("--upscale", type=int, default=1)
    ap.add_argument("--fidelity", type=float, default=0.7)
    ap.add_argument("--dub-python", help="dubbing-studio venv python (relipsync)")
    ap.add_argument("--lipsync", help="path to dubbing-studio/lipsync.py (relipsync)")
    a = ap.parse_args()

    work = Path(a.work).resolve()
    if not work.is_dir():
        die(f"no such workdir: {work}")
    stem = work.name
    final = work / "final.mp4"
    vo = work / "new-vo.mp3"
    src_txt = work / "source.txt"
    source = Path(src_txt.read_text(encoding="utf-8").strip()) if src_txt.is_file() else None

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = work / f"repair-{a.action}-{stamp}.mp4"

    if a.action in ("remux", "refit", "relipsync"):
        if not vo.is_file():
            die("no new-vo.mp3 in the workdir — this dub can't be repaired here")
        if source is None or not source.is_file():
            die("source video not found (source.txt missing or stale) — re-dub instead")

    if a.action == "remux":
        dv = duration(source)
        print(f"re-muxing {vo.name} onto {source.name} (video untouched, {dv:.2f}s)", flush=True)
        # NO -shortest: it would truncate the video down to a shorter voice. Instead pad
        # the audio with trailing silence (apad) and cap the whole output to the video's
        # own duration (-t dv) so the full video is always preserved.
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-i", str(vo),
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
             "-filter:a", "apad", "-c:a", "aac", "-b:a", "192k",
             "-t", f"{dv:.3f}", str(out)], "remux")
        guard_len(out, dv)

    elif a.action == "refit":
        dv, da = duration(source), duration(vo)
        factor = da / dv
        print(f"video {dv:.2f}s, voice {da:.2f}s -> stretch factor {factor:.4f} "
              f"({'speed up' if factor > 1 else 'slow down'}, pitch preserved)", flush=True)
        fitted = work / f"new-vo-fitted-{stamp}.m4a"
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(vo),
             "-filter:a", atempo_chain(factor), "-c:a", "aac", "-b:a", "192k",
             str(fitted)], "time-stretch voice")
        # fitted voice is ~video length; cap to the video's duration (never -shortest,
        # which could still shave the video) and verify the length was preserved.
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-i", str(fitted),
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "copy",
             "-t", f"{dv:.3f}", str(out)], "remux fitted voice")
        guard_len(out, dv)

    elif a.action == "renorm":
        if not final.is_file():
            die("no final.mp4 to normalize")
        print("re-normalizing loudness to -16 LUFS (video stream copied)", flush=True)
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(final),
             "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
             "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-c:a", "aac", "-b:a", "192k", str(out)], "loudnorm")

    elif a.action == "relipsync":
        if not (a.dub_python and a.lipsync):
            die("relipsync needs --dub-python and --lipsync")
        wav = work / f"new-vo-{stamp}.wav"
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(vo),
             "-ar", "44100", "-ac", "1", str(wav)], "voice -> wav for Wav2Lip")
        cmd = [a.dub_python, a.lipsync, "--face", str(source), "--audio", str(wav),
               "--out", str(out), "--restorer", a.restorer,
               "--upscale", str(a.upscale), "--fidelity", str(a.fidelity)]
        if a.restorer == "none":
            cmd.append("--no-enhance")
        print(f"re-running local lip-sync ({a.restorer}) on {source.name} — free, GPU", flush=True)
        proc = subprocess.Popen(cmd, cwd=str(Path(a.lipsync).parent),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        for line in proc.stdout:
            print(line.rstrip(), flush=True)
        if proc.wait() != 0 or not out.is_file():
            die("lip-sync failed — see the log above")
        wav.unlink(missing_ok=True)

    if not out.is_file():
        die("no output produced")
    # register the repair in the version history the dashboard already uses
    versions_file = work / "versions.json"
    try:
        versions = json.loads(versions_file.read_text(encoding="utf-8")) if versions_file.is_file() else {}
    except Exception:
        versions = {}
    versions[out.name] = {"created": time.time(), "repair": a.action,
                          "restorer": a.restorer if a.action == "relipsync" else None}
    versions_file.write_text(json.dumps(versions, indent=1), encoding="utf-8")
    print(f"RESULT: output/script-swap/{stem}/{out.name}", flush=True)


if __name__ == "__main__":
    main()
