#!/usr/bin/env python3
"""
Course video transcription pipeline (local, free).

Walks a folder of course videos (nested subfolders = one folder per course),
extracts 16kHz mono audio with ffmpeg, transcribes locally with faster-whisper,
and writes per-video transcripts that mirror the course folder structure:

    <out>/transcripts/<course>/<video-name>.md     readable transcript with timestamps
    <out>/transcripts/<course>/<video-name>.json   segments (+ optional word timestamps) for tooling/RAG

Idempotent: files whose .json sidecar already exists (status=complete) are skipped.
Re-run any time; use --force to redo.

Usage:
    python transcribe.py "D:/Courses"                      # all defaults (distil-large-v3, auto device)
    python transcribe.py "D:/Courses" --model large-v3 --language he
    python transcribe.py "D:/Courses" --word-timestamps --keep-audio
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mpg", ".mpeg", ".wmv", ".flv", ".ts"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus", ".wma"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS

# Start a new paragraph block in the .md when the pause between segments exceeds
# this many seconds, or the block grows longer than BLOCK_MAX_SECONDS.
BLOCK_GAP_SECONDS = 2.5
BLOCK_MAX_SECONDS = 60.0


def find_ffmpeg() -> str:
    """Prefer ffmpeg on PATH; fall back to the binary bundled with imageio-ffmpeg."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("ffmpeg not found on PATH and imageio-ffmpeg is not installed. "
                 "Run: pip install imageio-ffmpeg  (or install ffmpeg)")


def extract_audio(ffmpeg: str, src: Path, dst: Path) -> None:
    """Extract 16kHz mono 16-bit PCM wav (what Whisper consumes natively)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part.wav")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed for {src.name}: {result.stderr.strip()[:500]}")
    tmp.replace(dst)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def hms(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _register_cuda_dlls() -> None:
    """On Windows, make pip-installed NVIDIA cuBLAS/cuDNN DLLs visible to ctranslate2."""
    if sys.platform != "win32":
        return
    import os
    site = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for sub in ("cublas", "cudnn"):
        bin_dir = site / sub / "bin"
        if bin_dir.is_dir():
            os.add_dll_directory(str(bin_dir))
            # ctranslate2 loads cuBLAS/cuDNN via the classic search order, so
            # add_dll_directory alone isn't enough — PATH must include them too.
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def load_model(model_name: str, device: str, compute_type: str):
    """Load WhisperModel, trying CUDA first when device=auto and falling back to CPU."""
    _register_cuda_dlls()
    from faster_whisper import WhisperModel

    attempts = []
    if device == "auto":
        attempts = [("cuda", compute_type if compute_type != "auto" else "int8_float16"),
                    ("cpu", compute_type if compute_type != "auto" else "int8")]
    else:
        ct = compute_type if compute_type != "auto" else ("int8_float16" if device == "cuda" else "int8")
        attempts = [(device, ct)]

    last_err = None
    for dev, ct in attempts:
        try:
            print(f"Loading model '{model_name}' on {dev} ({ct})...")
            model = WhisperModel(model_name, device=dev, compute_type=ct)
            return model, dev, ct
        except Exception as e:  # missing CUDA/cuDNN DLLs, OOM, etc.
            last_err = e
            print(f"  -> {dev} unavailable ({type(e).__name__}: {str(e)[:200]})")
    sys.exit(f"Could not load model on any device: {last_err}")


def group_segments(segments: list[dict]) -> list[dict]:
    """Group raw segments into readable paragraph blocks for the .md."""
    blocks = []
    cur = None
    for seg in segments:
        text = seg["text"].strip()
        if not text:
            continue
        gap = seg["start"] - cur["end"] if cur else 0.0
        if cur is None or gap > BLOCK_GAP_SECONDS or (seg["end"] - cur["start"]) > BLOCK_MAX_SECONDS:
            if cur:
                blocks.append(cur)
            cur = {"start": seg["start"], "end": seg["end"], "text": text}
        else:
            cur["end"] = seg["end"]
            cur["text"] += " " + text
    if cur:
        blocks.append(cur)
    return blocks


def write_markdown(md_path: Path, src: Path, rel: Path, duration: float,
                   meta: dict, segments: list[dict]) -> None:
    lines = [
        f"# {src.stem}",
        "",
        f"- **Source file:** `{rel.as_posix()}`",
        f"- **Duration:** {hms(duration)}",
        f"- **Transcribed:** {meta['transcribed_at']} — {meta['model']} ({meta['device']}, {meta['compute_type']})",
        f"- **Language:** {meta['language']} (p={meta['language_probability']:.2f})",
        "",
        "---",
        "",
    ]
    for block in group_segments(segments):
        lines.append(f"**[{hms(block['start'])}]** {block['text']}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")


def transcribe_file(model, src: Path, rel: Path, out_dir: Path, cache_dir: Path,
                    ffmpeg: str, args, meta_base: dict) -> None:
    from tqdm import tqdm

    md_path = out_dir / rel.parent / f"{src.stem}.md"
    json_path = out_dir / rel.parent / f"{src.stem}.json"
    md_path.parent.mkdir(parents=True, exist_ok=True)

    # --- audio extraction (skipped for already-small audio sources is not worth it:
    # we always normalize to 16k mono wav so Whisper never touches the big file) ---
    wav_path = cache_dir / rel.parent / f"{src.stem}.wav"
    if not wav_path.exists():
        print(f"  extracting audio -> {wav_path.name}")
        extract_audio(ffmpeg, src, wav_path)
    duration = wav_duration(wav_path)

    segments_iter, info = model.transcribe(
        str(wav_path),
        language=args.language,
        vad_filter=True,
        word_timestamps=args.word_timestamps,
        beam_size=5,
    )

    segments = []
    t0 = time.time()
    with tqdm(total=round(duration), unit="s", desc=f"  {src.stem[:40]}", leave=False) as bar:
        for seg in segments_iter:
            item = {"id": seg.id, "start": round(seg.start, 2), "end": round(seg.end, 2),
                    "text": seg.text}
            if args.word_timestamps and seg.words:
                item["words"] = [{"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word}
                                 for w in seg.words]
            segments.append(item)
            bar.n = min(round(seg.end), bar.total)
            bar.refresh()
    elapsed = time.time() - t0
    speed = duration / elapsed if elapsed > 0 else 0.0

    meta = dict(meta_base)
    meta.update({
        "source": rel.as_posix(),
        "duration_seconds": round(duration, 1),
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "transcribed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "realtime_factor": round(speed, 2),
        "status": "complete",
    })

    write_markdown(md_path, src, rel, duration, meta, segments)
    json_path.write_text(json.dumps({"meta": meta, "segments": segments},
                                    ensure_ascii=False, indent=1), encoding="utf-8")

    if not args.keep_audio:
        wav_path.unlink(missing_ok=True)
    print(f"  done in {hms(elapsed)} ({speed:.1f}x realtime) -> {md_path}")


def is_complete(json_path: Path) -> bool:
    if not json_path.exists():
        return False
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))["meta"]["status"] == "complete"
    except Exception:
        return False


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="Transcribe a folder of course videos locally with faster-whisper.")
    ap.add_argument("input", help="Folder containing course videos (nested subfolders supported)")
    ap.add_argument("--out", default=None, help="Output root (default: <this repo>/output)")
    ap.add_argument("--model", default="distil-large-v3",
                    help="faster-whisper model: distil-large-v3 (default, English), large-v3 (multilingual), medium, small...")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--compute-type", default="auto",
                    help="ctranslate2 compute type (auto: int8_float16 on cuda, int8 on cpu)")
    ap.add_argument("--language", default=None, help="Force language code (e.g. en, he). Default: auto-detect per file")
    ap.add_argument("--word-timestamps", action="store_true", help="Also store per-word timestamps in the .json (slower)")
    ap.add_argument("--keep-audio", action="store_true", help="Keep extracted .wav files in the cache")
    ap.add_argument("--force", action="store_true", help="Re-transcribe even if output already exists")
    args = ap.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    if input_path.is_file():
        input_dir = input_path.parent
        files = [input_path]
    elif input_path.is_dir():
        input_dir = input_path
        files = sorted(p for p in input_dir.rglob("*")
                       if p.suffix.lower() in MEDIA_EXTS and p.is_file()
                       and not p.name.startswith("."))  # skip macOS ._AppleDouble junk
    else:
        sys.exit(f"Not found: {input_path}")

    root = Path(args.out).resolve() if args.out else Path(__file__).resolve().parent.parent / "output"
    out_dir = root / "transcripts"
    cache_dir = root / ".cache" / "audio"
    if not files:
        sys.exit(f"No video/audio files found under {input_dir}")

    todo, skipped = [], 0
    for f in files:
        rel = f.relative_to(input_dir)
        if not args.force and is_complete(out_dir / rel.parent / f"{f.stem}.json"):
            skipped += 1
        else:
            todo.append((f, rel))

    print(f"Found {len(files)} media files ({skipped} already transcribed, {len(todo)} to do)")
    if not todo:
        print("Nothing to do.")
        return

    ffmpeg = find_ffmpeg()
    model, device, compute_type = load_model(args.model, args.device, args.compute_type)
    meta_base = {"model": args.model, "device": device, "compute_type": compute_type}

    failures = []
    for i, (f, rel) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {rel.as_posix()}")
        try:
            transcribe_file(model, f, rel, out_dir, cache_dir, ffmpeg, args, meta_base)
        except KeyboardInterrupt:
            print("\nInterrupted — progress on completed files is saved; re-run to continue.")
            raise
        except Exception as e:
            failures.append((rel.as_posix(), str(e)))
            print(f"  FAILED: {e}")

    print(f"\nDone: {len(todo) - len(failures)} transcribed, {skipped} skipped, {len(failures)} failed.")
    for name, err in failures:
        print(f"  FAILED {name}: {err[:200]}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
