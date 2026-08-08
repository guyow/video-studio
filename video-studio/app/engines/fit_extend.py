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
     generate the missing seconds from a seed frame, join, mux the measured voice,
     and VERIFY the final length equals the target to the frame (guard_len).
     Writes extended.mp4 (+ a copy in uploads/ so it's a first-class library clip)
     and fit.json (the proof record).

  join     (free):
     re-do ONLY the join from footage already generated — different blend, or a
     second try at the seam — without paying fal.ai again.

THE SEAM IS THE WHOLE GAME. A cut you can see ruins the illusion, so the join is
built in five stages, each fixing a different way the eye catches a splice:

  1. seed = the EXACT frame we cut at.  The old code seeded from "somewhere in the
     last 0.2s" while concatenating the source WHOLE — so the generated footage
     restarted from a moment that had already played: a visible ~6-frame jump back.
  2. calmest seam.  Inside the free slack (we always generate a whole segment, so
     there are spare seconds), we look back up to ~1.2s and cut on the stillest,
     sharpest frame — a motion-blurred frame makes a bad i2v seed and a bad splice.
  3. colour match.  fal's encode drifts exposure/contrast/tint/range against the
     source; measured over real frames on both sides and corrected with eq +
     colorbalance, plus a light sharpness/grain match so the texture doesn't pop.
  4. micro-dissolve.  A few frames of cross-fade over the join hides whatever step
     is left. Short enough that a talking mouth can't ghost.
  5. proof.  We measure the frame-to-frame difference across the seam against the
     motion around it and record the ratio in fit.json — a number, not a promise.

Cadence is kept at the SOURCE's frame rate (not a hardcoded 30) so the real footage
is never resampled; only the generated tail is conformed to it.

The project's -shortest ban applies: we build to a known duration with -t and a
post-encode duration guard, never -shortest.

Runs under the cv venv (same as i2v_gen). It shells out to the dub venv for XTTS
and to i2v_gen.py for fal generation. cv2/numpy are used for the seam work and
degrade gracefully (plain end-cut, no colour match) if they're missing.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", flush=True)
    sys.exit(1)


def run(cmd: list[str], label: str, timeout: int | None = None,
        stream: bool = False) -> str:
    """Run a child and return its stdout.

    stream=True echoes each line as it arrives instead of holding it until the
    child exits. The fal generation takes ~2.5 min PER SEGMENT — captured output
    left the job drawer frozen on one line for half an hour of paid work, which
    reads as a hang. Progress you can't see is progress you don't trust."""
    log(f"  {label}…")
    if not stream:
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        if r.returncode != 0:
            die(f"{label} failed:\n{(r.stderr or r.stdout or '')[-1200:]}")
        return r.stdout or ""

    out: list[str] = []
    p = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", bufsize=1)
    try:
        for line in p.stdout:                     # type: ignore[union-attr]
            out.append(line)
            line = line.rstrip()
            if line:
                log(f"    {line}")
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        die(f"{label} timed out after {timeout}s")
    if p.returncode != 0:
        die(f"{label} failed:\n{''.join(out)[-1200:]}")
    return "".join(out)


def ff(args: list[str], label: str) -> None:
    run(["ffmpeg", "-y", "-loglevel", "error", *args], label)


def probe(path: Path, entries: str, stream: bool = True) -> str:
    cmd = ["ffprobe", "-v", "error"]
    if stream:
        cmd += ["-select_streams", "v:0"]
    cmd += ["-show_entries", entries, "-of", "csv=p=0", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (r.stdout or "").strip()


def probe_sec(path: Path) -> float:
    try:
        return float(probe(path, "format=duration", stream=False).split(",")[0] or 0)
    except ValueError:
        return 0.0


def probe_wh(path: Path) -> tuple[int, int]:
    try:
        w, h = probe(path, "stream=width,height").split(",")[:2]
        return int(w), int(h)
    except (ValueError, IndexError):
        die(f"could not read resolution of {path.name}")
        raise


def probe_fps(path: Path) -> tuple[str, float]:
    """Frame rate as (ffmpeg fraction, float). We conform the generated tail to the
    SOURCE's rate — forcing everything to 30 would resample 25fps footage and add
    judder to the part of the video that was already perfect."""
    raw = probe(path, "stream=r_frame_rate").split(",")[0]
    try:
        num, den = raw.split("/")
        val = float(num) / float(den)
        if 5.0 < val < 121.0:
            return raw, val
    except (ValueError, ZeroDivisionError):
        pass
    return "30", 30.0


def _cv():
    """cv2 + numpy if this venv has them (the cv venv does). Every caller has a
    plain-ffmpeg fallback so a missing wheel degrades quality, never breaks the run."""
    try:
        import cv2                                   # noqa: PLC0415
        import numpy as np                           # noqa: PLC0415
        return cv2, np
    except Exception:                                # noqa: BLE001
        return None, None


def last_frame(video: Path, dest: Path) -> None:
    """The TRUE final frame. `-update 1` rewrites the same file for every decoded
    frame, so what survives is the last one — unlike `-frames:v 1`, which grabs the
    FIRST frame after the seek point (that was the old ~0.2s jump-back bug)."""
    try:
        ff(["-sseof", "-0.6", "-i", str(video), "-update", "1", "-q:v", "2", str(dest)],
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


# ─────────────────────────────────────────────── seam: where to cut
def pick_seam(source: Path, source_sec: float, max_back: float, fps: float,
              seed: Path) -> tuple[float, dict]:
    """Choose the cut point inside the free slack and write that exact frame as the
    i2v seed. Prefers frames that are STILL (a moving subject splices badly) and
    SHARP (motion blur seeds a mushy generation), with a mild pull toward the end so
    we never throw away footage we don't have to.

    Returns (cut_sec, info). Falls back to the true end of the clip."""
    info: dict = {"method": "end"}
    cv2, np = _cv()
    if cv2 is None or max_back < 2.0 / fps:
        last_frame(source, seed)
        return source_sec, info

    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        cap.release()
        last_frame(source, seed)
        return source_sec, info

    start = max(0.0, source_sec - min(max_back, 1.2))
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)
    grays: list = []
    crisp: list = []
    cuts: list[float] = []
    while len(grays) < 90:
        ok, frame = cap.read()
        if not ok:
            break
        h, w = frame.shape[:2]
        small = cv2.cvtColor(cv2.resize(frame, (240, max(2, int(240 * h / w)))),
                             cv2.COLOR_BGR2GRAY).astype(np.float32)
        # blur before differencing: sensor/compression grain is temporal too, and
        # ungated it swamps the real subject motion we're trying to rank frames by.
        grays.append(cv2.GaussianBlur(small, (0, 0), 1.6))
        crisp.append(small)                       # sharpness must see the real detail
        pos = cap.get(cv2.CAP_PROP_POS_FRAMES)
        cuts.append(pos / fps if pos > 0 else cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0)
    cap.release()

    if len(grays) < 4:
        last_frame(source, seed)
        return source_sec, info

    motion = [float(np.mean(np.abs(grays[i] - grays[i - 1]))) for i in range(1, len(grays))]
    sharp = [float(cv2.Laplacian(g, cv2.CV_32F).var()) for g in crisp]
    m_max = max(max(motion), 1e-6)
    s_max = max(max(sharp), 1e-6)
    n = len(grays)

    best_i, best_score = n - 1, 1e9
    for i in range(1, n):
        local = (motion[i - 1] + (motion[i] if i < len(motion) else motion[i - 1])) / (2 * m_max)
        blur = 1.0 - sharp[i] / s_max
        back = (n - 1 - i) / (n - 1)          # cost of discarding footage
        score = local + 0.6 * blur + 0.25 * back
        if score < best_score:
            best_i, best_score = i, score

    # second pass: re-read to the chosen frame so the seed is byte-for-byte the
    # frame we cut at (seeking straight to an index is unreliable on h264).
    cap = cv2.VideoCapture(str(source))
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)
    chosen = None
    for _ in range(best_i + 1):
        ok, frame = cap.read()
        if not ok:
            break
        chosen = frame
    cap.release()
    if chosen is None:
        last_frame(source, seed)
        return source_sec, info

    cv2.imwrite(str(seed), chosen, [int(cv2.IMWRITE_JPEG_QUALITY), 96])
    cut = min(source_sec, max(cuts[best_i], 2.0 / fps))
    info = {"method": "calm-frame", "trimmed_sec": round(source_sec - cut, 3),
            "candidates": n, "motion": round(motion[best_i - 1] / m_max, 3),
            "sharpness": round(sharp[best_i] / s_max, 3)}
    if source_sec - cut > 0.01:
        log(f"  seam: cutting {source_sec - cut:.2f}s early on the stillest frame "
            f"(motion {info['motion']:.2f}, sharpness {info['sharpness']:.2f})")
    return cut, info


# ─────────────────────────────────────────────── seam: make the tail match
def _sample(path: Path, at: float, span: float, count: int):
    """`count` frames spread over `span` seconds starting at `at`, as float BGR."""
    cv2, np = _cv()
    if cv2 is None:
        return []
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        return []
    out = []
    for k in range(count):
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, at + span * k / max(1, count - 1)) * 1000.0)
        ok, frame = cap.read()
        if ok:
            out.append(frame.astype(np.float32))
    cap.release()
    return out


def match_filters(source: Path, cut: float, gen: Path) -> tuple[str, dict]:
    """Measure the source's last half-second against the generated clip's first
    half-second and build the ffmpeg filter that pulls the generated footage onto
    the source's look: exposure, contrast, tint, saturation, then texture.

    Everything is clamped hard — a colour match that over-corrects is worse than
    none, and these numbers come from only a handful of frames."""
    cv2, np = _cv()
    stats: dict = {"applied": False}
    if cv2 is None:
        return "", stats

    src = _sample(source, max(0.0, cut - 0.55), 0.5, 7)
    tail = _sample(gen, 0.05, 0.5, 7)
    if len(src) < 3 or len(tail) < 3:
        return "", stats

    def desc(frames):
        arr = np.stack(frames)
        luma = arr @ np.array([0.114, 0.587, 0.299], dtype=np.float32)   # BGR weights
        hsv = [cv2.cvtColor(f.astype("uint8"), cv2.COLOR_BGR2HSV) for f in frames]
        sat = float(np.mean(np.stack(hsv)[..., 1]))
        lap = float(np.mean([cv2.Laplacian(l, cv2.CV_32F).var() for l in luma]))
        hf = float(np.mean([np.std(l - cv2.GaussianBlur(l, (0, 0), 1.2)) for l in luma]))
        return {"mean": float(luma.mean()), "std": float(luma.std()),
                "chan": [float(x) for x in arr.reshape(-1, 3).mean(axis=0)],
                "sat": sat, "lap": lap, "hf": hf}

    a, b = desc(src), desc(tail)
    clamp = lambda v, lo, hi: float(max(lo, min(hi, v)))                 # noqa: E731

    contrast = clamp(a["std"] / max(b["std"], 1e-3), 0.85, 1.18)
    brightness = clamp((a["mean"] - contrast * b["mean"]) / 255.0, -0.10, 0.10)
    saturation = clamp(a["sat"] / max(b["sat"], 1e-3), 0.85, 1.18)

    parts = []
    if abs(contrast - 1) > 0.01 or abs(brightness) > 0.004 or abs(saturation - 1) > 0.02:
        parts.append(f"eq=contrast={contrast:.4f}:brightness={brightness:.4f}"
                     f":saturation={saturation:.4f}")

    # residual per-channel tint left after the luma fix (BGR order from OpenCV).
    # float() everywhere: these come from numpy and fit.json must stay serializable.
    tint = [float(a["chan"][i] - (contrast * b["chan"][i] + brightness * 255.0)) / 255.0
            for i in range(3)]
    bm, gm, rm = (float(clamp(t, -0.09, 0.09)) for t in tint)
    if max(abs(rm), abs(gm), abs(bm)) > 0.006:
        parts.append(f"colorbalance=rm={rm:.4f}:gm={gm:.4f}:bm={bm:.4f}"
                     f":rs={rm / 2:.4f}:gs={gm / 2:.4f}:bs={bm / 2:.4f}")

    # texture: fal output is usually cleaner/softer than a phone-shot source, and a
    # sudden jump in grain or crispness reads as a cut even when colour is perfect.
    ratio = a["lap"] / max(b["lap"], 1e-3)
    if ratio > 1.35:
        amount = clamp((ratio - 1.0) * 0.45, 0.15, 0.85)
        parts.append(f"unsharp=5:5:{amount:.3f}:5:5:0")
    grain = a["hf"] - b["hf"]
    if grain > 1.2:
        parts.append(f"noise=alls={clamp(grain * 0.9, 1, 7):.0f}:allf=t+u")

    stats = {"applied": bool(parts), "contrast": round(contrast, 4),
             "brightness": round(brightness, 4), "saturation": round(saturation, 4),
             "tint": [round(rm, 4), round(gm, 4), round(bm, 4)],
             "sharpen": round(max(0.0, ratio - 1.0), 3), "grain": round(max(0.0, grain), 2),
             "filters": ",".join(parts)}
    if parts:
        log(f"  match: contrast {contrast:.3f} · brightness {brightness:+.3f} · "
            f"saturation {saturation:.3f} · tint {rm:+.3f}/{gm:+.3f}/{bm:+.3f}")
    return ",".join(parts), stats


def seam_report(video: Path, seam: float, fps: float) -> dict:
    """Proof, not a promise: how big is the frame-to-frame jump AT the join compared
    with the normal motion around it? ~1x means the splice is invisible in the data."""
    cv2, np = _cv()
    if cv2 is None:
        return {}
    frames = _sample(video, max(0.0, seam - 0.5), 1.0, 16)
    if len(frames) < 6:
        return {}
    small = [cv2.GaussianBlur(
        cv2.cvtColor(cv2.resize(f, (192, 108)), cv2.COLOR_BGR2GRAY).astype(np.float32),
        (0, 0), 1.2) for f in frames]
    diffs = [float(np.mean(np.abs(small[i] - small[i - 1]))) for i in range(1, len(small))]
    mid = len(diffs) // 2
    step = max(diffs[max(0, mid - 1):mid + 2])
    around = sorted(diffs[:max(1, mid - 1)] + diffs[mid + 2:])
    base = around[len(around) // 2] if around else step
    ratio = step / max(base, 0.4)
    verdict = "invisible" if ratio < 1.6 else ("smooth" if ratio < 2.6 else "visible")
    return {"seam_ratio": round(ratio, 2), "seam_step": round(step, 2),
            "motion_around": round(base, 2), "verdict": verdict}


# ─────────────────────────────────────────────── the join
def build_join(source: Path, gen: Path, out: Path, cut: float, blend: float,
               fps_str: str, fps: float, w: int, h: int, color: str,
               tail_speed: float = 1.0, pad_to: float = 0.0) -> float:
    """source[0..cut] + generated, joined so you can't see where. Returns the seam
    timestamp in the output.

    Scale-to-FILL (never pad): black bars appearing halfway through a video are the
    most visible seam of all, and the generated aspect can differ from the source."""
    norm = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
            f"fps={fps_str},setsar=1,format=yuv420p")
    gen_sec = probe_sec(gen)
    frame = 1.0 / fps
    blend = max(0.0, min(blend, cut * 0.4, gen_sec * 0.4))

    head = f"[0:v]trim=end={cut:.4f},setpts=PTS-STARTPTS,{norm}[v0]"
    # with a dissolve the overlap already absorbs the seed frame; with a hard cut we
    # drop it, or the generated re-render of the frame we cut on holds for 2 frames.
    tail_trim = "" if blend > 0 else "trim=start_frame=1,setpts=PTS-STARTPTS,"
    slow = f"setpts={tail_speed:.5f}*PTS," if tail_speed > 1.001 else ""
    hold = f",tpad=stop_mode=clone:stop_duration={pad_to:.3f}" if pad_to > 0.01 else ""
    tail = f"[1:v]{tail_trim}{slow}{norm}{',' + color if color else ''}{hold}[v1]"

    if blend > 0:
        offset = max(0.0, cut - blend - frame)     # keep the fade inside v0's timeline
        join = f"[v0][v1]xfade=transition=fade:duration={blend:.4f}:offset={offset:.4f}[v]"
        seam = offset + blend / 2
        log(f"  join: {blend * fps:.0f}-frame dissolve at {cut:.2f}s")
    else:
        join = "[v0][v1]concat=n=2:v=1:a=0[v]"
        seam = cut
        log(f"  join: hard cut at {cut:.2f}s (frame-exact)")

    ff(["-i", str(source), "-i", str(gen), "-filter_complex",
        f"{head};{tail};{join}", "-map", "[v]", "-c:v", "libx264", "-crf", "17",
        "-preset", "slow", "-pix_fmt", "yuv420p", "-r", fps_str, str(out)],
       "join source + generated footage")
    if not out.is_file() or out.stat().st_size == 0:
        die("the join produced no video")
    return seam


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
    stdout = run(cmd, "XTTS synthesis", timeout=3600, stream=True)

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


# ─────────────────────────────────────────────── extend / re-join
def do_extend(a: argparse.Namespace) -> None:
    source = Path(a.source).resolve()
    work = Path(a.work).resolve()
    plan_file = work / "plan.json"
    if not plan_file.is_file():
        die("no analysis found — run Analyze first")
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    target_sec = float(plan["target_sec"])
    gap = float(plan["gap"])
    source_sec = float(plan["source_sec"]) or probe_sec(source)
    vo = work / "new-vo.mp3"
    if not vo.is_file():
        die("no voice track (new-vo.mp3) — re-run Analyze")
    if not plan.get("needs_extend"):
        die("video is already long enough for the script — no extension needed")

    fps_str, fps = probe_fps(source)
    w, h = probe_wh(source)
    gen_dir = work / "gen"
    gen = Path(a.gen_file).resolve() if a.gen_file else gen_dir / "clip.mp4"
    rejoin = a.mode == "join"

    if rejoin:
        if not gen.is_file() or gen.stat().st_size == 0:
            die("no generated footage to re-join — run the paid extend first")
        log(f"re-joining already-generated footage (free) — {probe_sec(gen):.1f}s available")
        seam_room = max(0.0, source_sec + probe_sec(gen) - target_sec)
    else:
        seg = int(a.seg)
        need = int(a.seconds)                # server rounded the gap up to a segment
        n_seg = max(1, need // seg)
        # spare seconds we already paid for, minus what the generator's own segment
        # dissolves will eat — spending slack we don't have would make the clip short.
        seam_room = max(0.0, need - gap - (n_seg - 1) * a.i2v_blend)
        log(f"target {target_sec:.1f}s · source {source_sec:.1f}s · generating ~{need}s "
            f"({n_seg} × {seg}s on {a.model}) to cover the {gap:.1f}s gap")

    # 1 · where to cut — spend the spare seconds on a better seam, never on making
    #     the video too short. Keep a safety margin so the length guard still holds.
    #     On a re-join the cut is NOT re-decided: the footage was generated from the
    #     frame at the old cut, so moving it would re-open the very jump we closed.
    seed = work / "seed.jpg"
    prev = json.loads((work / "fit.json").read_text(encoding="utf-8")) \
        if rejoin and (work / "fit.json").is_file() else {}
    if prev.get("cut_sec"):
        cut = float(prev["cut_sec"])
        seam_info = dict(prev.get("seam_pick") or {}, reused=True)
        log(f"  seam: keeping the original cut at {cut:.2f}s (the footage was generated "
            f"from that frame)")
    else:
        max_back = max(0.0, min(seam_room - max(a.blend, 0.0) - 0.20, 1.2))
        cut, seam_info = pick_seam(source, source_sec, max_back, fps, seed)

    if not rejoin:
        gen_dir.mkdir(parents=True, exist_ok=True)
        log("generating continuation footage on fal.ai (this is the paid step)…")
        run([a.cv_py, a.i2v, "--image", str(seed), "--prompt", a.prompt,
             "--out", str(gen_dir), "--name", f"{source.stem}-ext",
             "--model", a.model, "--aspect", a.aspect, "--seconds", str(a.seconds),
             "--blend", str(a.i2v_blend), "--env-file", a.env_file],
            "fal.ai generation", timeout=3600, stream=True)
        if not gen.is_file() or gen.stat().st_size == 0:
            die("generation produced no clip — nothing charged is usable")
    gen_sec = probe_sec(gen)
    log(f"continuation footage: {gen_sec:.1f}s")

    # If the tail came back short, we do NOT move the cut — the footage was generated
    # from the frame at THAT cut, so moving it re-opens the jump we just closed.
    # Instead stretch the tail a hair (a continuation shot hides ±10% easily) and,
    # only in the extreme, hold the last frame rather than lose a paid render.
    blend = max(0.0, a.blend)
    tail_speed, pad = 1.0, 0.0
    want_tail = target_sec - cut + blend
    if gen_sec < want_tail - 0.02:
        tail_speed = min(want_tail / max(gen_sec, 0.1), 1.12)
        got = gen_sec * tail_speed
        log(f"  tail is {want_tail - gen_sec:.2f}s short — slowing the generated part "
            f"×{tail_speed:.3f} to land on target")
        if got < want_tail - 0.02:
            pad = want_tail - got
            log(f"  ⚠ still {pad:.2f}s short — holding the last frame for {pad:.2f}s "
                f"(the generator returned less footage than requested)")

    # 2 · make the tail look like the source, then 3 · dissolve the join away
    color, color_stats = ("", {}) if a.no_match else match_filters(source, cut, gen)
    ext_silent = work / "ext-silent.mp4"
    seam = build_join(source, gen, ext_silent, cut, blend, fps_str, fps, w, h, color,
                      tail_speed, pad)

    # mux the measured voice; pad audio, cap the whole file to the target length
    # (never -shortest), then verify the fit held.
    extended = work / "extended.mp4"
    ff(["-i", str(ext_silent), "-i", str(vo), "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-filter:a", "apad", "-c:a", "aac", "-b:a", "192k",
        "-t", f"{target_sec:.3f}", str(extended)], "mux voice + fit to length")
    guard_len(extended, target_sec)
    final_sec = probe_sec(extended)

    quality = seam_report(extended, seam, fps)
    if quality:
        log(f"  seam check: step {quality['seam_step']} vs motion {quality['motion_around']} "
            f"→ {quality['seam_ratio']}× ({quality['verdict']})")

    fitted_rel = None
    if a.uploads:
        dest = Path(a.uploads) / f"{source.stem}-fitted.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        ff(["-i", str(extended), "-c", "copy", str(dest)], "publish fitted clip to library")
        fitted_rel = f"uploads/{dest.name}"

    proof = {"source_sec": round(source_sec, 2), "target_sec": round(target_sec, 2),
             "gap": round(gap, 2), "generated_sec": round(gen_sec, 2),
             "final_sec": round(final_sec, 2), "model": a.model,
             "fitted": fitted_rel, "fps": round(fps, 3),
             "seam_sec": round(seam, 2), "cut_sec": round(cut, 4),
             "blend_sec": round(blend, 3),
             "seam_pick": seam_info, "color": color_stats, **quality,
             "tail_speed": round(tail_speed, 4), "held_sec": round(pad, 2),
             "rejoined": rejoin,
             "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    (work / "fit.json").write_text(json.dumps(proof, indent=1), encoding="utf-8")

    log("")
    log(f"✅ fitted: source {source_sec:.1f}s → script {target_sec:.1f}s "
        f"→ final video {final_sec:.1f}s  (matches to {abs(final_sec - target_sec):.2f}s)")
    if quality:
        log(f"🪄 seam at {seam:.1f}s is {quality['verdict']} ({quality['seam_ratio']}× "
            f"the surrounding motion)")
    if fitted_rel:
        log(f"📁 added to your library as {Path(fitted_rel).name} — dub / lip-sync it next")
    log("FIT: " + json.dumps(proof))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("analyze", "extend", "join"), required=True)
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
    # seam
    ap.add_argument("--blend", type=float, default=0.17,
                    help="seconds of cross-dissolve over the join (0 = frame-exact hard cut)")
    ap.add_argument("--i2v-blend", dest="i2v_blend", type=float, default=0.12,
                    help="dissolve between the generator's own chained segments")
    ap.add_argument("--no-match", dest="no_match", action="store_true",
                    help="skip the colour/texture match of the generated tail")
    ap.add_argument("--gen-file", dest="gen_file",
                    help="re-use this generated clip instead of paying fal.ai again")
    a = ap.parse_args()

    if a.mode == "analyze":
        if not (a.script and a.ds_py and a.ds_app):
            die("analyze needs --script, --ds-py, --ds-app")
        do_analyze(a)
    elif a.mode == "join":
        do_extend(a)
    else:
        if not (a.cv_py and a.i2v and a.env_file):
            die("extend needs --cv-py, --i2v, --env-file")
        do_extend(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
