#!/usr/bin/env python3
"""Fit video to script — extend a video so its length matches the rewritten script.

The problem: you rewrite a VSL script LONGER than the original footage. A normal
dub then has to squeeze the longer voice into the short video (time-stretch), which
drifts. This engine does the opposite — it grows the VIDEO to the script:

  analyze  (free, local, GPU):
     synthesize the script with XTTS at its NATURAL length (--no-fit) and MEASURE
     it. That real spoken duration is the provable target. Writes new-vo.mp3 +
     plan.json {source_sec, target_sec, gap, needs_extend}.

  extend   (paid, fal.ai — cost-gated by the server):
     grab the video's LAST FRAME, have fal.ai (i2v_gen.py) generate the missing
     seconds of continuation from it, concat source+generated, mux the measured
     voice, and VERIFY the final length equals the target to the frame (guard_len).
     Writes extended.mp4 (+ a copy in uploads/ so it's a first-class library clip)
     and fit.json (the proof record).

The project's -shortest ban applies: we build to a known duration with -t and a
post-encode duration guard, never -shortest.

Runs under the cv venv (same as i2v_gen). It shells out to the dub venv for XTTS
and to i2v_gen.py for fal generation, so it only needs stdlib + ffmpeg on PATH.
"""
import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", flush=True)
    sys.exit(1)


def run(cmd: list[str], label: str, timeout: int | None = None) -> str:
    log(f"  {label}…")
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if r.returncode != 0:
        die(f"{label} failed:\n{(r.stderr or r.stdout or '')[-1200:]}")
    return r.stdout or ""


def ff(args: list[str], label: str) -> None:
    run(["ffmpeg", "-y", "-loglevel", "error", *args], label)


def probe_sec(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return float((r.stdout or "0").strip() or 0)
    except ValueError:
        return 0.0


def probe_wh(path: Path) -> tuple[int, int]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        w, h = (r.stdout or "").strip().split("x")[:2]
        return int(w), int(h)
    except (ValueError, IndexError):
        die(f"could not read resolution of {path.name}")


def last_frame(video: Path, dest: Path) -> None:
    """Grab a still from the last 0.2s of the clip (the i2v seed). Mirrors
    i2v_gen.last_frame, with a reverse fallback if -sseof lands past EOF."""
    try:
        ff(["-sseof", "-0.2", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(dest)],
           "grab last frame")
    except SystemExit:
        ff(["-i", str(video), "-vf", "reverse", "-frames:v", "1", "-q:v", "2", str(dest)],
           "grab last frame (fallback)")
    if not dest.is_file():
        die("could not extract the video's last frame")


def guard_len(out: Path, expected: float, tol: float = 0.7) -> None:
    got = probe_sec(out)
    if abs(got - expected) > tol:
        out.unlink(missing_ok=True)
        die(f"fitted length {got:.2f}s != target {expected:.2f}s — deleted, not delivered off-length")


# ─────────────────────────────────────────────── analyze
def do_analyze(a: argparse.Namespace) -> None:
    source = Path(a.source).resolve()
    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    script = Path(a.script).resolve()
    if not source.is_file():
        die(f"source video not found: {source}")
    if not script.is_file() or not script.read_text(encoding="utf-8").strip():
        die("no script yet — save a script first, then fit")

    source_sec = probe_sec(source)
    log(f"source video: {source_sec:.2f}s")

    cmd = [a.ds_py, a.ds_app, "--cli", "--video", str(source), "--script", str(script),
           "--no-fit", "--device", "auto", "--language", a.language or "en"]
    if a.reference:
        cmd += ["--reference", a.reference]
    log("synthesizing the script at its natural spoken length (XTTS)…")
    stdout = run(cmd, "XTTS synthesis", timeout=3600)

    wav = None
    for line in stdout.splitlines():
        s = line.strip()
        if s.lower().startswith("audio:"):
            wav = Path(s.split(":", 1)[1].strip())
    if not wav or not wav.is_file():
        die("XTTS did not report an audio file — synthesis may have failed (see log above)")

    target_sec = probe_sec(wav)
    vo = work / "new-vo.mp3"
    ff(["-i", str(wav), "-c:a", "libmp3lame", "-b:a", "192k", str(vo)], "save voice track")

    gap = round(target_sec - source_sec, 2)
    needs = gap > 0.4
    plan = {"source_sec": round(source_sec, 2), "target_sec": round(target_sec, 2),
            "gap": gap, "needs_extend": needs, "vo": "new-vo.mp3",
            "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    (work / "plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")

    log("")
    log(f"📏 source {source_sec:.1f}s → script needs {target_sec:.1f}s "
        f"→ {'gap ' + format(gap, '.1f') + 's to generate' if needs else 'already long enough'}")
    log("PLAN: " + json.dumps(plan))


# ─────────────────────────────────────────────── extend
def do_extend(a: argparse.Namespace) -> None:
    source = Path(a.source).resolve()
    work = Path(a.work).resolve()
    plan_file = work / "plan.json"
    if not plan_file.is_file():
        die("no analysis found — run Analyze first")
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    target_sec = float(plan["target_sec"])
    gap = float(plan["gap"])
    vo = work / "new-vo.mp3"
    if not vo.is_file():
        die("no voice track (new-vo.mp3) — re-run Analyze")
    if not plan.get("needs_extend"):
        die("video is already long enough for the script — no extension needed")

    seg = int(a.seg)
    need = int(a.seconds)                    # server rounded gap up to a segment multiple
    log(f"target {target_sec:.1f}s · source {plan['source_sec']}s · generating ~{need}s "
        f"({need // seg} × {seg}s on {a.model}) to cover the {gap:.1f}s gap")

    seed = work / "seed.jpg"
    last_frame(source, seed)

    gen_dir = work / "gen"
    gen_dir.mkdir(parents=True, exist_ok=True)
    log("generating continuation footage on fal.ai (this is the paid step)…")
    run([a.cv_py, a.i2v, "--image", str(seed), "--prompt", a.prompt,
         "--out", str(gen_dir), "--name", f"{Path(a.source).stem}-ext",
         "--model", a.model, "--aspect", a.aspect, "--seconds", str(need),
         "--env-file", a.env_file], "fal.ai generation", timeout=3600)
    gen = gen_dir / "clip.mp4"
    if not gen.is_file() or gen.stat().st_size == 0:
        die("generation produced no clip — nothing charged is usable")
    gen_sec = probe_sec(gen)
    log(f"generated {gen_sec:.1f}s of new footage")

    w, h = probe_wh(source)
    norm = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1")
    ext_silent = work / "ext-silent.mp4"
    ff(["-i", str(source), "-i", str(gen), "-filter_complex",
        f"[0:v]{norm}[v0];[1:v]{norm}[v1];[v0][v1]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30",
        str(ext_silent)], "concat source + generated")

    # mux the measured voice; pad audio, cap the whole file to the target length
    # (never -shortest), then verify the fit held.
    extended = work / "extended.mp4"
    ff(["-i", str(ext_silent), "-i", str(vo), "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-filter:a", "apad", "-c:a", "aac", "-b:a", "192k",
        "-t", f"{target_sec:.3f}", str(extended)], "mux voice + fit to length")
    guard_len(extended, target_sec)
    final_sec = probe_sec(extended)

    fitted_rel = None
    if a.uploads:
        dest = Path(a.uploads) / f"{Path(a.source).stem}-fitted.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        ff(["-i", str(extended), "-c", "copy", str(dest)], "publish fitted clip to library")
        fitted_rel = f"uploads/{dest.name}"

    proof = {"source_sec": plan["source_sec"], "target_sec": round(target_sec, 2),
             "gap": round(gap, 2), "generated_sec": round(gen_sec, 2),
             "final_sec": round(final_sec, 2), "model": a.model,
             "fitted": fitted_rel,
             "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    (work / "fit.json").write_text(json.dumps(proof, indent=1), encoding="utf-8")

    log("")
    log(f"✅ fitted: source {plan['source_sec']}s → script {target_sec:.1f}s "
        f"→ final video {final_sec:.1f}s  (matches to {abs(final_sec - target_sec):.2f}s)")
    if fitted_rel:
        log(f"📁 added to your library as {Path(fitted_rel).name} — dub / lip-sync it next")
    log("FIT: " + json.dumps(proof))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("analyze", "extend"), required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--stem", required=True)
    ap.add_argument("--work", required=True)
    # analyze
    ap.add_argument("--script")
    ap.add_argument("--ds-py", dest="ds_py")
    ap.add_argument("--ds-app", dest="ds_app")
    ap.add_argument("--language", default="en")
    ap.add_argument("--reference")
    # extend
    ap.add_argument("--cv-py", dest="cv_py")
    ap.add_argument("--i2v")
    ap.add_argument("--env-file", dest="env_file")
    ap.add_argument("--model", default="kling-2.1")
    ap.add_argument("--aspect", default="9:16")
    ap.add_argument("--seconds", type=int, default=10)
    ap.add_argument("--seg", type=int, default=10)
    ap.add_argument("--prompt", default="the same person continues speaking naturally to camera, "
                    "subtle natural head and hand movement, identical setting and lighting")
    ap.add_argument("--uploads")
    a = ap.parse_args()

    if a.mode == "analyze":
        if not (a.script and a.ds_py and a.ds_app):
            die("analyze needs --script, --ds-py, --ds-app")
        do_analyze(a)
    else:
        if not (a.cv_py and a.i2v and a.env_file):
            die("extend needs --cv-py, --i2v, --env-file")
        do_extend(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
