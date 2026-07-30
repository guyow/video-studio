#!/usr/bin/env python3
"""Two-speaker diarization for interview footage → duo-config.json.

Answers "who talks when" so script-swap-duo.py can clone BOTH voices and
re-voice each line with the right speaker on the original timeline.

Pipeline (all local, free):
  1. transcript sidecar (uploads/transcripts/<stem>.json) — run whisper if missing
  2. voice embedding per transcript segment (resemblyzer if available, MFCC otherwise)
  3. 2-means clustering (pure numpy, deterministic) + short-segment smoothing
  4. merge consecutive same-speaker segments into TURNS
  5. auto-pick per-speaker reference windows (longest clean turns, for voice cloning)
  6. write duo-config.json + duo-transcript.txt into the workdir

Run under the dubbing-studio venv (torch + librosa present):
  python diarize.py --video <uploads/x.mp4> --transcript <uploads/transcripts/x.json>
                    --work <output/script-swap/x> [--whisper-python P --whisper-script S
                    --uploads DIR]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np


def log(msg: str) -> None:
    print(msg, flush=True)


def ffmpeg_wav(video: Path, dest: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(dest)],
        check=True)


def load_segments(sidecar: Path) -> list[dict]:
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    segs = []
    for s in data.get("segments", []):
        text = (s.get("text") or "").strip()
        if not text:
            continue
        segs.append({"start": float(s["start"]), "end": float(s["end"]), "text": text})
    return segs


# ── embeddings ────────────────────────────────────────────────────────────────

def embed_resemblyzer(wav: np.ndarray, sr: int, segs: list[dict]) -> np.ndarray:
    from resemblyzer import VoiceEncoder
    enc = VoiceEncoder("cpu")            # tiny model; CPU keeps the GPU free
    out = []
    for s in segs:
        a, b = int(s["start"] * sr), int(s["end"] * sr)
        clip = wav[max(0, a):b]
        if len(clip) < sr // 2:          # pad ultra-short segments to 0.5s
            clip = np.pad(clip, (0, sr // 2 - len(clip)))
        out.append(enc.embed_utterance(clip))
    return np.vstack(out)


def embed_mfcc(wav: np.ndarray, sr: int, segs: list[dict]) -> np.ndarray:
    import librosa
    out = []
    for s in segs:
        a, b = int(s["start"] * sr), int(s["end"] * sr)
        clip = wav[max(0, a):b]
        if len(clip) < sr // 2:
            clip = np.pad(clip, (0, sr // 2 - len(clip)))
        m = librosa.feature.mfcc(y=clip.astype(np.float32), sr=sr, n_mfcc=20)
        v = np.concatenate([m.mean(axis=1), m.std(axis=1)])
        out.append(v / (np.linalg.norm(v) + 1e-9))
    return np.vstack(out)


# ── clustering ────────────────────────────────────────────────────────────────

def two_means(X: np.ndarray, iters: int = 100) -> tuple[np.ndarray, float]:
    """Deterministic 2-means on unit vectors. Returns (labels, separation 0..1)."""
    Xn = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    sim = Xn @ Xn.T
    i, j = np.unravel_index(np.argmin(sim), sim.shape)   # farthest pair seeds
    centers = np.stack([Xn[i], Xn[j]])
    labels = np.zeros(len(Xn), dtype=int)
    for _ in range(iters):
        d = Xn @ centers.T                               # cosine similarity
        new = d.argmax(axis=1)
        if (new == labels).all() and _ > 0:
            break
        labels = new
        for k in (0, 1):
            if (labels == k).any():
                c = Xn[labels == k].mean(axis=0)
                centers[k] = c / (np.linalg.norm(c) + 1e-9)
    d = Xn @ centers.T
    margin = float(np.mean(np.abs(d[:, 0] - d[:, 1])))   # how separable the voices are
    return labels, margin


def smooth(segs: list[dict], labels: np.ndarray) -> np.ndarray:
    """Flip isolated short segments sandwiched between the other speaker."""
    labels = labels.copy()
    for i in range(1, len(labels) - 1):
        dur = segs[i]["end"] - segs[i]["start"]
        if dur < 1.2 and labels[i - 1] == labels[i + 1] != labels[i]:
            labels[i] = labels[i - 1]
    return labels


def to_turns(segs: list[dict], labels: np.ndarray) -> list[dict]:
    turns = []
    for s, lab in zip(segs, labels):
        spk = "A" if lab == 0 else "B"
        if turns and turns[-1]["speaker"] == spk and s["start"] - turns[-1]["end"] < 1.5:
            turns[-1]["end"] = s["end"]
            turns[-1]["text"] += " " + s["text"]
        else:
            turns.append({"start": round(s["start"], 2), "end": round(s["end"], 2),
                          "speaker": spk, "text": s["text"]})
    for t in turns:
        t["end"] = round(t["end"], 2)
    return turns


def ref_windows(turns: list[dict], speaker: str, want: float = 14.0) -> list[list[float]]:
    """Longest clean turns of this speaker → clone-reference windows."""
    own = sorted((t for t in turns if t["speaker"] == speaker),
                 key=lambda t: t["end"] - t["start"], reverse=True)
    wins, total = [], 0.0
    for t in own:
        a, b = t["start"] + 0.15, t["end"] - 0.15
        if b - a < 2.0:
            continue
        wins.append([round(a, 2), round(b, 2)])
        total += b - a
        if total >= want or len(wins) >= 3:
            break
    return wins


def fmt_ts(sec: float) -> str:
    return f"{int(sec // 60)}:{sec % 60:04.1f}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video", required=True)
    ap.add_argument("--transcript", required=True, help="whisper sidecar json")
    ap.add_argument("--work", required=True, help="output/script-swap/<stem>")
    ap.add_argument("--whisper-python", help="whisper venv python (auto-transcribe if sidecar missing)")
    ap.add_argument("--whisper-script", help="course_pipeline/transcribe.py")
    ap.add_argument("--uploads", help="uploads dir (transcribe --out target)")
    args = ap.parse_args()

    video = Path(args.video)
    sidecar = Path(args.transcript)
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)

    if not video.is_file():
        sys.exit(f"video not found: {video}")

    if not sidecar.is_file():
        if not (args.whisper_python and args.whisper_script):
            sys.exit("no transcript yet — run Transcribe first")
        log("=== stage: transcribe (whisper) ===")
        log("No transcript yet — transcribing the interview first…")
        r = subprocess.run([args.whisper_python, args.whisper_script, str(video),
                            "--out", args.uploads or str(sidecar.parent.parent)])
        if r.returncode != 0 or not sidecar.is_file():
            sys.exit("transcription failed")

    log("=== stage: diarize (who speaks when) ===")
    segs = load_segments(sidecar)
    if len(segs) < 2:
        sys.exit(f"only {len(segs)} spoken segment(s) — nothing to split into two speakers")
    log(f"{len(segs)} spoken segments from the transcript")

    wav_path = work / "diarize-audio.wav"
    ffmpeg_wav(video, wav_path)
    import librosa
    wav, sr = librosa.load(str(wav_path), sr=16000, mono=True)

    try:
        X = embed_resemblyzer(wav, sr, segs)
        log("voice embeddings: resemblyzer (speaker d-vectors)")
    except Exception as exc:                               # noqa: BLE001
        log(f"resemblyzer unavailable ({exc.__class__.__name__}) — using MFCC voice stats")
        X = embed_mfcc(wav, sr, segs)

    labels, margin = two_means(X)
    labels = smooth(segs, labels)
    # Speaker A = whoever speaks first
    if labels[0] == 1:
        labels = 1 - labels
    turns = to_turns(segs, labels)

    a_time = sum(t["end"] - t["start"] for t in turns if t["speaker"] == "A")
    b_time = sum(t["end"] - t["start"] for t in turns if t["speaker"] == "B")
    log(f"speaker separation: {margin:.2f} "
        f"({'clear' if margin > 0.08 else 'LOW — review the turns and flip any that are wrong'})")
    log(f"{len(turns)} turns · Speaker A {a_time:.0f}s · Speaker B {b_time:.0f}s")

    refs = {s: ref_windows(turns, s) for s in ("A", "B")}
    for s, wins in refs.items():
        total = sum(b - a for a, b in wins)
        note = "" if total >= 8 else "  (short — voice clone may sound generic)"
        log(f"clone reference {s}: {len(wins)} window(s), {total:.1f}s{note}")

    cfg = {
        "source": str(video),
        "speakers": {
            "A": {"label": "Speaker A", "ref_windows": refs["A"]},
            "B": {"label": "Speaker B", "ref_windows": refs["B"]},
        },
        "segments": turns,
    }
    (work / "duo-config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    lines = [f"[{t['speaker']} {fmt_ts(t['start'])}–{fmt_ts(t['end'])}]  {t['text']}" for t in turns]
    (work / "duo-transcript.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    log("")
    for ln in lines:
        log("  " + ln)
    log("")
    log(f"duo-config.json ready → {work / 'duo-config.json'}")
    log("Next: review the turns in the Dubbing tab (rename speakers, fix any wrong labels), "
        "edit each line's text, then run the interview dub.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
