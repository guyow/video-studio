"""
Local Voice-Clone Dubbing Tool
==============================================================================
Drop in a video + paste your script -> it clones the voice of the speaker in
the video and reads YOUR script in that cloned voice. Outputs a .wav of the
dubbed audio and an .mp4 with that audio swapped in.

This tool does NOT transcribe, translate, or lip-sync. You supply the exact
words; lip-sync happens in a separate downstream tool.

Engine: Coqui XTTS v2 (via the maintained `coqui-tts` package).
Runs fully local/offline after the one-time model download.

Primary interface: a Gradio web app (run `python app.py`).
A small command-line entry point is included too (`python app.py --cli ...`).
==============================================================================
"""

import os
# torch >= 2.6 defaults torch.load to weights_only=True, which rejects the
# pickled config classes inside the (trusted, locally downloaded) XTTS v2
# checkpoint — restore the old behavior for this process only.
os.environ.setdefault("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", "1")
import sys
import re
import gc
import glob
import shutil
import argparse
import subprocess
import tempfile
from pathlib import Path

# --- Paths & environment --------------------------------------------------
# Keep everything self-contained inside the project folder.
PROJECT_DIR = Path(__file__).resolve().parent
MODELS_DIR = PROJECT_DIR / "models"      # XTTS weights cache here (~1.9 GB)
OUTPUTS_DIR = PROJECT_DIR / "outputs"    # generated .wav / .mp4 land here
MODELS_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)

# Cache models INSIDE the project (instead of %LOCALAPPDATA%\tts).
os.environ.setdefault("TTS_HOME", str(MODELS_DIR))
# Auto-agree to the Coqui Public Model License so the first run does NOT block
# on an interactive y/n prompt. XTTS v2 is released under CPML (non-commercial
# unless you obtain a separate license from Coqui). By running this you accept it.
os.environ.setdefault("COQUI_TOS_AGREED", "1")

import torch  # noqa: E402  (import after env setup)


# ==========================================================================
# numba shim (Windows Smart App Control workaround)
# ==========================================================================
# coqui-tts imports `librosa`, which imports `numba`, which loads an UNSIGNED
# `llvmlite.dll`. On machines with Windows Smart App Control enabled, that DLL
# is blocked ("An Application Control policy has blocked this file",
# WinError 4551) and the whole import chain dies.
#
# numba is a JIT compiler librosa uses to speed up beat-tracking / pitch /
# note-conversion helpers — NONE of which XTTS v2's synthesis path calls
# (XTTS computes its mel-spectrograms with torch). So we can satisfy the
# import with a lightweight pure-Python stand-in and never touch the blocked
# DLL. This does NOT disable or weaken Smart App Control — it just removes the
# dependency on the unsigned binary.
#
# On a machine WITHOUT Smart App Control, the real numba imports fine and we
# use it (faster). We only fall back to the shim when the real import fails.
def _install_numba_shim():
    import types
    import functools
    import numpy as _np

    numba = types.ModuleType("numba")

    def _passthrough_decorator(*args, **kwargs):
        """Emulate @jit / @njit / @stencil (with or without arguments)."""
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]                       # used as bare @jit
        def deco(fn):
            return fn                            # used as @jit(nopython=True, ...)
        return deco

    def _vectorize(*dargs, **dkwargs):
        """Emulate @numba.vectorize -> numpy elementwise ufunc."""
        excluded = dkwargs.get("excluded", None)
        def deco(fn):
            vec = _np.vectorize(fn, excluded=excluded)
            return functools.wraps(fn)(vec)
        # allow bare @vectorize
        if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
            return deco(dargs[0])
        return deco

    def _guvectorize(*dargs, **dkwargs):
        """
        Emulate @numba.guvectorize. librosa only uses this for beat/pitch/peak
        helpers that XTTS never calls. We return a wrapper that raises a clear,
        catchable error if it is ever actually invoked, so a silent-wrong-answer
        is impossible.
        """
        def deco(fn):
            @functools.wraps(fn)
            def _unsupported(*a, **k):
                raise NotImplementedError(
                    f"numba shim: guvectorized function '{fn.__name__}' was "
                    "called, but this environment blocks numba (Smart App "
                    "Control). This librosa feature isn't used by XTTS dubbing."
                )
            return _unsupported
        return deco

    numba.jit = _passthrough_decorator
    numba.njit = _passthrough_decorator
    numba.stencil = _passthrough_decorator
    numba.vectorize = _vectorize
    numba.guvectorize = _guvectorize
    numba.prange = range
    numba.__file__ = "<numba-shim>"              # keep inspect/lazy_loader happy
    numba.__version__ = "0.0.0-shim"

    # Any other attribute (numba.uint, numba.float64, numba.config, ...) returns
    # a permissive stand-in that works as a type-cast, a value, or a decorator.
    # Dunder attributes (__file__, __path__, __spec__, ...) are NOT intercepted
    # so that inspect/importlib introspection keeps working.
    class _Universal:
        def __call__(self, *a, **k):
            if len(a) == 1 and callable(a[0]) and not k:
                return a[0]
            if len(a) == 1 and not k:
                return a[0]                      # type-cast style: numba.uint(x)
            def deco(fn):
                return fn
            return deco
        def __getitem__(self, item):
            return self                          # numba.float32[:] in signatures

    def _numba_getattr(name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)           # let real introspection fail cleanly
        return _Universal()
    numba.__getattr__ = _numba_getattr

    sys.modules["numba"] = numba


try:
    import numba  # noqa: F401  (prefer the real numba when it's allowed to load)
except Exception:
    sys.modules.pop("numba", None)               # drop the half-imported module
    _install_numba_shim()


# XTTS constants
XTTS_MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"
XTTS_SAMPLE_RATE = 24000          # XTTS synthesizes at 24 kHz
REFERENCE_SAMPLE_RATE = 16000     # clean mono reference clip
REFERENCE_SECONDS = 20            # longer, clean reference => a richer clone
MAX_CHARS_PER_CHUNK = 250         # keep each XTTS generation within a safe length

# --- Quality tuning -------------------------------------------------------
# XTTS v2 inference knobs. The coqui defaults are conservative; these reduce
# stutter / repeated-syllable artifacts and sharpen articulation while staying
# natural. Forwarded through TTS.tts(**kwargs) for the xtts_v2 model (with a
# safe fallback to plain .tts() on any version that rejects a kwarg).
XTTS_INFERENCE = dict(
    temperature=0.70,          # slight expressiveness without hallucinating
    length_penalty=1.0,
    repetition_penalty=5.0,    # up from ~2.0 default: kills repeated syllables
    top_k=50,
    top_p=0.85,
    enable_text_splitting=True,  # let XTTS sentence-split for natural prosody
)
# How many seconds of the reference XTTS uses to build the speaker embedding.
# Longer (up to ~30s) => a more faithful clone. The stock default is only 6s.
GPT_COND_LEN = 12
# Target integrated loudness for the finished dub (broadcast-ish, EBU R128).
TARGET_LUFS = -16.0

# XTTS v2 supported languages (code -> label for the dropdown)
SUPPORTED_LANGUAGES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "pl": "Polish",
    "tr": "Turkish",
    "ru": "Russian",
    "nl": "Dutch",
    "cs": "Czech",
    "ar": "Arabic",
    "zh": "Chinese",
    "ja": "Japanese",
    "hu": "Hungarian",
    "ko": "Korean",
    "hi": "Hindi",
}

# Module-level model cache so we don't reload XTTS on every click.
_TTS_MODEL = None
_TTS_DEVICE = None


# ==========================================================================
# ffmpeg helpers
# ==========================================================================
def check_ffmpeg():
    """Raise a clear error if ffmpeg / ffprobe are not on PATH."""
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise RuntimeError(
                f"'{tool}' was not found on your PATH.\n"
                "Install it on Windows with:  winget install Gyan.FFmpeg\n"
                "Then open a NEW terminal so the PATH change takes effect."
            )


def _run(cmd):
    """Run a subprocess, capturing output. Returns CompletedProcess."""
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def get_media_duration(path):
    """Return duration in seconds (float) using ffprobe, or None if unknown."""
    result = _run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return None


def extract_full_audio(video_path, out_wav):
    """Extract the full audio track from a video as 16 kHz mono WAV."""
    result = _run([
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", str(REFERENCE_SAMPLE_RATE),
        str(out_wav),
    ])
    if result.returncode != 0 or not Path(out_wav).exists():
        raise RuntimeError(
            "Could not extract audio from the video. Does it have an audio "
            f"track?\n\nffmpeg said:\n{result.stderr[-800:]}"
        )


def clean_reference(in_wav, out_wav):
    """
    Light, clone-SAFE cleanup of a voice reference: strip sub-bass rumble,
    a gentle broadband denoise, and loudness-normalize the level. Kept
    deliberately light — heavy denoise removes the speaker traits XTTS needs
    to clone well. Falls back to a plain copy if the filter chain fails, so a
    bad input can never break the dub.
    """
    res = _run([
        "ffmpeg", "-y", "-i", str(in_wav),
        "-af", "highpass=f=70,afftdn=nr=10,loudnorm=I=-18:TP=-2:LRA=11",
        "-ac", "1", "-ar", str(REFERENCE_SAMPLE_RATE),
        str(out_wav),
    ])
    if res.returncode != 0 or not Path(out_wav).exists():
        shutil.copy(str(in_wav), str(out_wav))
    return out_wav


def normalize_output_loudness(in_wav, out_wav):
    """
    Loudness-normalize the finished dub to TARGET_LUFS (EBU R128) so it sits at
    a consistent, video-ready level with safe true-peak headroom. Falls back to
    a plain copy if loudnorm fails.
    """
    res = _run([
        "ffmpeg", "-y", "-i", str(in_wav),
        "-af", f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11",
        "-ar", str(XTTS_SAMPLE_RATE),
        str(out_wav),
    ])
    if res.returncode != 0 or not Path(out_wav).exists():
        shutil.copy(str(in_wav), str(out_wav))
    return out_wav


def find_speech_onset(audio_wav):
    """
    Find the first moment of speech (skip leading silence) using ffmpeg's
    silencedetect. Returns a start offset in seconds. Falls back to 1.0s.
    """
    result = _run([
        "ffmpeg", "-i", str(audio_wav),
        "-af", "silencedetect=noise=-30dB:d=0.3",
        "-f", "null", "-",
    ])
    # silencedetect writes to stderr, e.g. "silence_end: 1.234"
    ends = re.findall(r"silence_end:\s*([0-9.]+)", result.stderr)
    starts = re.findall(r"silence_start:\s*([0-9.]+)", result.stderr)
    # If the clip opens with silence, the first silence_start is ~0 and the
    # first silence_end marks where speech begins.
    if starts and float(starts[0]) < 0.5 and ends:
        return max(0.0, float(ends[0]))
    # Otherwise there's speech from the top; skip the first second to avoid
    # any intro pop / breath.
    return 1.0


def extract_reference_clip(video_path, out_wav, log):
    """
    Build a clean ~12s mono 16 kHz reference clip for voice cloning:
      1. extract the full audio,
      2. skip leading silence,
      3. cut REFERENCE_SECONDS from the speech onset.
    Raises a clear error if the source is too short/silent to clone from.
    """
    with tempfile.TemporaryDirectory() as tmp:
        full = Path(tmp) / "full.wav"
        extract_full_audio(video_path, full)

        duration = get_media_duration(full)
        if duration is None or duration < 2.0:
            raise RuntimeError(
                "The video's audio is too short or silent to build a voice "
                "reference from (need at least ~2 seconds of speech). Upload a "
                "longer/clearer clip, or supply your own reference audio."
            )

        onset = find_speech_onset(full)
        # Make sure we don't start so late there's nothing left.
        if onset > max(0.0, duration - 2.0):
            onset = max(0.0, duration - REFERENCE_SECONDS)
        clip_len = min(REFERENCE_SECONDS, max(2.0, duration - onset))

        log(f"  reference clip: {clip_len:.1f}s starting at {onset:.1f}s "
            f"(source audio {duration:.1f}s)")

        raw_ref = Path(tmp) / "reference_raw.wav"
        result = _run([
            "ffmpeg", "-y", "-ss", f"{onset:.2f}", "-t", f"{clip_len:.2f}",
            "-i", str(full),
            "-ac", "1", "-ar", str(REFERENCE_SAMPLE_RATE),
            str(raw_ref),
        ])
        if result.returncode != 0 or not raw_ref.exists():
            raise RuntimeError(
                "Failed to cut the reference clip.\n\n"
                f"ffmpeg said:\n{result.stderr[-800:]}"
            )
        # Clone-safe cleanup (de-rumble + light denoise + level) for a better clone.
        clean_reference(raw_ref, out_wav)
        log("  reference cleaned (de-rumble + light denoise + level-matched)")


def mux_audio_into_video(video_path, audio_wav, out_video, keep_original_volume, log):
    """
    Replace the video's audio with the dubbed track.
      * keep_original_volume == 0.0  -> original audio fully removed.
      * keep_original_volume  > 0.0  -> original audio mixed back in faintly.
    We do NOT stretch either stream; both run their natural length and the
    output ends with the longer of the two (video re-muxed, not re-encoded).
    """
    vid_dur = get_media_duration(video_path)
    aud_dur = get_media_duration(audio_wav)
    if vid_dur and aud_dur:
        diff = aud_dur - vid_dur
        log(f"  length check: video {vid_dur:.1f}s, dubbed audio {aud_dur:.1f}s "
            f"(difference {diff:+.1f}s — not stretched; lip-sync tool handles timing)")

    if keep_original_volume and keep_original_volume > 0.0:
        # Mix: dubbed audio at full volume + original ducked to the slider value.
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),      # 0: original video (+audio)
            "-i", str(audio_wav),       # 1: dubbed audio
            "-filter_complex",
            f"[0:a]volume={keep_original_volume:.3f}[orig];"
            f"[1:a]volume=1.0[dub];"
            f"[orig][dub]amix=inputs=2:duration=longest:dropout_transition=0[aout]",
            "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(out_video),
        ]
    else:
        # Straight replacement: drop original audio entirely. We copy the video
        # stream (no re-encode) and encode the dubbed audio to AAC. No -shortest:
        # we deliberately let both streams run their natural length.
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),      # 0: original video
            "-i", str(audio_wav),       # 1: dubbed audio
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(out_video),
        ]

    result = _run(cmd)
    if result.returncode != 0 or not Path(out_video).exists():
        raise RuntimeError(
            "Failed to mux the dubbed audio into the video.\n\n"
            f"ffmpeg said:\n{result.stderr[-800:]}"
        )


# ==========================================================================
# Time-fit: match dubbed audio length to the video (for lip-sync)
# ==========================================================================
def _atempo_chain(tempo):
    """
    Build an ffmpeg atempo filter string for an arbitrary tempo factor.
    atempo only accepts 0.5–2.0 per instance, so we chain several to reach
    factors outside that range. atempo preserves PITCH (voice doesn't get
    chipmunky / deep) — it only changes speed. Duration scales by 1/tempo.
    """
    factors = []
    f = float(tempo)
    while f > 2.0:
        factors.append(2.0)
        f /= 2.0
    while f < 0.5:
        factors.append(0.5)
        f /= 0.5
    factors.append(f)
    return ",".join(f"atempo={x:.6f}" for x in factors)


# Only time-stretch within a natural-sounding band. Anything beyond this is
# made up with silence padding (audio too short) rather than an ugly >2x
# stretch. For lip-sync, trailing silence = a closed mouth, which is correct.
FIT_MIN_TEMPO = 0.90   # slow the voice at most to 0.90x (10% slower)
FIT_MAX_TEMPO = 1.18   # speed the voice at most to 1.18x (18% faster)


def fit_audio_to_duration(in_wav, target_seconds, out_wav, log):
    """
    Make `in_wav` last ~`target_seconds` for the downstream lip-sync tool, but
    WITHOUT wrecking the voice. Strategy:
      * small mismatch  -> gentle pitch-preserving time-stretch (atempo)
      * audio too short -> keep a natural pace, PAD the rest with silence
      * audio too long  -> gentle speed-up only; accept a small overrun beyond
                           that rather than chipmunking the voice
    Returns the path actually written (out_wav, or in_wav if unchanged).
    """
    cur = get_media_duration(in_wav)
    if not cur or cur <= 0 or not target_seconds or target_seconds <= 0:
        log("  length-match: skipped (couldn't read a duration).")
        return in_wav
    if abs(cur - target_seconds) < 0.10:
        log("  length-match: already within 0.1s — nothing to do.")
        return in_wav

    tempo = cur / target_seconds       # >1 => shorten (speed up); <1 => lengthen
    clamped = min(max(tempo, FIT_MIN_TEMPO), FIT_MAX_TEMPO)
    stretched_len = cur / clamped      # length after the clamped stretch

    if abs(clamped - tempo) < 1e-3:
        pct = (target_seconds - cur) / cur * 100.0
        log(f"  length-match: gentle {'slow-down' if tempo < 1 else 'speed-up'} "
            f"to hit {target_seconds:.1f}s (was {cur:.1f}s, {pct:+.0f}%).")
    elif tempo < FIT_MIN_TEMPO:
        pad = max(0.0, target_seconds - stretched_len)
        log(f"  length-match: script is short for a {target_seconds:.1f}s video; "
            f"keeping a natural pace (0.90x) + {pad:.1f}s trailing silence "
            f"instead of an unnatural {1/tempo:.1f}x slow-down.")
    else:  # tempo > FIT_MAX_TEMPO
        log(f"  length-match: script is long for the video; speeding up to "
            f"1.18x max (result ~{stretched_len:.1f}s vs {target_seconds:.1f}s "
            f"target) rather than chipmunking the voice.")

    # Pass 1: clamped pitch-preserving stretch. Pass 2 (apad) tops up with
    # silence to the target when the clamped stretch left us short.
    af = _atempo_chain(clamped)
    if stretched_len < target_seconds - 0.05:
        af += f",apad=whole_dur={target_seconds:.3f}"

    result = _run([
        "ffmpeg", "-y", "-i", str(in_wav),
        "-filter:a", af,
        "-ar", str(XTTS_SAMPLE_RATE),
        str(out_wav),
    ])
    if result.returncode != 0 or not Path(out_wav).exists():
        log("  ⚠ length-match failed; keeping the natural-length audio.\n"
            f"     ffmpeg said: {result.stderr[-400:]}")
        return in_wav
    return out_wav


# ==========================================================================
# Text handling
# ==========================================================================
def split_script(text):
    """
    Split the script into chunks small enough for reliable XTTS synthesis.
    Splits on sentence punctuation (Western + CJK), then greedily packs
    sentences up to MAX_CHARS_PER_CHUNK. Over-long sentences are hard-split.
    """
    text = text.strip()
    if not text:
        return []

    # Split into sentences while keeping the delimiters.
    pieces = re.split(r"(?<=[.!?。！？\n])\s*", text)
    pieces = [p.strip() for p in pieces if p and p.strip()]

    chunks = []
    current = ""
    for piece in pieces:
        # Hard-split any single sentence longer than the limit.
        while len(piece) > MAX_CHARS_PER_CHUNK:
            head, piece = piece[:MAX_CHARS_PER_CHUNK], piece[MAX_CHARS_PER_CHUNK:]
            if current:
                chunks.append(current)
                current = ""
            chunks.append(head.strip())
        if not current:
            current = piece
        elif len(current) + 1 + len(piece) <= MAX_CHARS_PER_CHUNK:
            current += " " + piece
        else:
            chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


# ==========================================================================
# XTTS model
# ==========================================================================
def pick_device(requested):
    """
    Resolve the device to use.
      requested in {"auto", "cuda", "cpu"}.
    'auto' uses CUDA when available, else CPU.
    """
    if requested == "cpu":
        return "cpu"
    if requested == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError(
                "You picked CUDA but torch.cuda.is_available() is False. "
                "Install the CUDA build of torch (see README), or pick CPU."
            )
        return "cuda"
    # auto
    return "cuda" if torch.cuda.is_available() else "cpu"


def load_tts(device, log):
    """Load (and cache) the XTTS v2 model on the given device."""
    global _TTS_MODEL, _TTS_DEVICE
    if _TTS_MODEL is not None and _TTS_DEVICE == device:
        return _TTS_MODEL

    # If a model is loaded on a different device, drop it first.
    if _TTS_MODEL is not None:
        free_gpu()

    from TTS.api import TTS  # imported lazily; heavy import

    log(f"  loading XTTS v2 on {device.upper()} "
        f"(first run downloads ~1.9 GB into ./models — please wait)...")
    model = TTS(XTTS_MODEL_NAME).to(device)

    # Use more of the reference clip for the speaker embedding (default is only
    # ~6s) and let XTTS loudness-normalize references itself. Defensive: config
    # layout varies across coqui-tts versions, so never let this break loading.
    try:
        cfg = model.synthesizer.tts_model.config
        cfg.gpt_cond_len = GPT_COND_LEN
        cfg.max_ref_len = max(getattr(cfg, "max_ref_len", 10), GPT_COND_LEN)
        cfg.sound_norm_refs = True
        log(f"  tuned: gpt_cond_len={GPT_COND_LEN}s, sound_norm_refs=on")
    except Exception:
        pass

    _TTS_MODEL = model
    _TTS_DEVICE = device
    log("  model ready.")
    return model


def free_gpu():
    """Release the model and clear CUDA memory."""
    global _TTS_MODEL, _TTS_DEVICE
    _TTS_MODEL = None
    _TTS_DEVICE = None
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def synthesize(text_chunks, reference_wav, language, device, log):
    """
    Synthesize each chunk in the cloned voice and concatenate into one track.
    Returns (numpy_waveform, sample_rate).
    Handles CUDA OOM by falling back to CPU and retrying the whole synthesis.
    """
    import numpy as np

    def _tts_call(model, text):
        """Call XTTS with the tuned inference knobs; fall back to plain .tts()
        on any coqui-tts version that rejects one of the kwargs."""
        try:
            return model.tts(text=text, speaker_wav=str(reference_wav),
                             language=language, **XTTS_INFERENCE)
        except TypeError:
            return model.tts(text=text, speaker_wav=str(reference_wav),
                             language=language)

    def _do(dev):
        model = load_tts(dev, log)
        gap = np.zeros(int(0.3 * XTTS_SAMPLE_RATE), dtype=np.float32)  # 0.3s pause
        segments = []
        for i, chunk in enumerate(text_chunks, 1):
            log(f"  synthesizing chunk {i}/{len(text_chunks)} "
                f"({len(chunk)} chars)...")
            wav = _tts_call(model, chunk)
            segments.append(np.asarray(wav, dtype=np.float32))
            segments.append(gap)
        if segments:
            segments.pop()  # drop trailing gap
        return np.concatenate(segments) if segments else np.zeros(1, dtype=np.float32)

    try:
        return _do(device), XTTS_SAMPLE_RATE
    except torch.cuda.OutOfMemoryError:
        log("  ⚠ CUDA ran out of memory. Freeing GPU and retrying on CPU "
            "(slower but reliable)...")
        free_gpu()
        return _do("cpu"), XTTS_SAMPLE_RATE
    except RuntimeError as e:
        # Some OOMs surface as generic RuntimeError with "out of memory".
        if "out of memory" in str(e).lower() and device != "cpu":
            log("  ⚠ CUDA out of memory. Freeing GPU and retrying on CPU...")
            free_gpu()
            return _do("cpu"), XTTS_SAMPLE_RATE
        raise


# ==========================================================================
# Main pipeline
# ==========================================================================
def dub_pipeline(video_path, script, language_code, reference_audio,
                 keep_original_volume, device_choice, log,
                 fit_to_video=True):
    """
    Full pipeline. `log(str)` is a callback for progress lines.
    If `fit_to_video` is True, the dubbed audio is time-stretched (pitch
    preserved) to exactly match the video length so the lip-sync tool gets
    equal-length inputs.
    Returns (audio_wav_path, video_out_path).
    """
    import soundfile as sf
    import numpy as np

    # --- validate inputs --------------------------------------------------
    check_ffmpeg()

    if not video_path:
        raise RuntimeError("Please upload a video first.")
    if not script or not script.strip():
        raise RuntimeError("The script is empty. Paste the words you want spoken.")
    if language_code not in SUPPORTED_LANGUAGES:
        raise RuntimeError(
            f"Unsupported language '{language_code}'. "
            f"Choose one of: {', '.join(SUPPORTED_LANGUAGES)}"
        )

    device = pick_device(device_choice)
    log(f"Device: {device.upper()}")
    if device == "cuda":
        try:
            name = torch.cuda.get_device_name(0)
            total = torch.cuda.get_device_properties(0).total_memory / 1e9
            log(f"GPU: {name} ({total:.1f} GB total VRAM)")
        except Exception:
            pass

    chunks = split_script(script)
    log(f"Script split into {len(chunks)} chunk(s).")

    stamp = _timestamp()
    audio_out = OUTPUTS_DIR / f"dubbed_{stamp}.wav"
    video_out = OUTPUTS_DIR / f"dubbed_{stamp}.mp4"

    with tempfile.TemporaryDirectory() as tmp:
        # --- 1. voice reference ------------------------------------------
        if reference_audio:
            log("Step 1/3: using your uploaded reference audio.")
            ref_raw = Path(tmp) / "reference_raw.wav"
            ref_wav = Path(tmp) / "reference.wav"
            # Normalize whatever they uploaded to 16 kHz mono.
            res = _run([
                "ffmpeg", "-y", "-i", str(reference_audio),
                "-ac", "1", "-ar", str(REFERENCE_SAMPLE_RATE), str(ref_raw),
            ])
            if res.returncode != 0 or not ref_raw.exists():
                raise RuntimeError(
                    "Could not read your reference audio.\n\n"
                    f"ffmpeg said:\n{res.stderr[-800:]}"
                )
            # Same clone-safe cleanup as the auto-extracted reference.
            clean_reference(ref_raw, ref_wav)
        else:
            log("Step 1/3: extracting a voice reference from the video...")
            ref_wav = Path(tmp) / "reference.wav"
            extract_reference_clip(video_path, ref_wav, log)

        # --- 2. clone + synthesize ---------------------------------------
        log("Step 2/3: cloning voice and synthesizing your script...")
        if device == "cuda":
            torch.cuda.reset_peak_memory_stats()
        waveform, sr = synthesize(chunks, ref_wav, language_code, device, log)
        raw_wav = Path(tmp) / "dubbed_raw.wav"
        sf.write(str(raw_wav), waveform, sr)
        dubbed_len = len(waveform) / sr
        log(f"  synthesized dubbed audio ({dubbed_len:.1f}s).")
        if _TTS_DEVICE == "cuda":
            peak = torch.cuda.max_memory_allocated() / 1e9
            reserved = torch.cuda.max_memory_reserved() / 1e9
            log(f"  peak VRAM this job: {peak:.2f} GB allocated "
                f"/ {reserved:.2f} GB reserved")

        # Free the GPU now that synthesis is done.
        free_gpu()

        # --- 2b. optionally match audio length to the video --------------
        vid_dur = get_media_duration(video_path)
        if fit_to_video:
            log("Matching dubbed audio length to the video (for lip-sync)...")
            fitted = fit_audio_to_duration(raw_wav, vid_dur, audio_out, log)
            if str(fitted) != str(audio_out):     # fit skipped/failed -> use raw
                shutil.copy(str(fitted), str(audio_out))
        else:
            log("Keeping the dubbed audio at its natural length "
                "(length-match off).")
            shutil.copy(str(raw_wav), str(audio_out))

        # Broadcast-level loudness on the finished dub (consistent, video-ready).
        log(f"  normalizing loudness to {TARGET_LUFS:g} LUFS...")
        norm_tmp = Path(tmp) / "dubbed_norm.wav"
        normalize_output_loudness(audio_out, norm_tmp)
        shutil.copy(str(norm_tmp), str(audio_out))

        final_len = get_media_duration(audio_out) or dubbed_len
        log(f"  wrote dubbed audio: {audio_out.name} ({final_len:.1f}s)")

        # --- 3. mux into video -------------------------------------------
        log("Step 3/3: swapping the new audio into the video...")
        mux_audio_into_video(video_path, audio_out, video_out,
                             keep_original_volume, log)
        log(f"  wrote video: {video_out.name}")

    log("Done ✅")
    return str(audio_out), str(video_out)


def _timestamp():
    """Filesystem-safe timestamp for output names."""
    import time
    return time.strftime("%Y%m%d_%H%M%S")


# ==========================================================================
# FAL.AI cloud voice engine (optional, paid) — reuses the proven autoVSL pipeline
# ==========================================================================
AUTOVSL_DIR = Path(__file__).resolve().parent.parent / "autoVSL"
FAL_F5_RATE = 0.05            # USD per 1,000 characters (fal-ai/f5-tts)
FAL_LATENTSYNC_RATE = 0.005   # USD per second (~$0.20 / 40s clip, fal-ai/latentsync)
# script-swap.py writes finished lip-syncs here (its READY_DIR).
FAL_READY_DIR = Path("~/Desktop/liitt testimonial Ready").expanduser()
SPEND_FILE = OUTPUTS_DIR / "fal_spend.json"


def _load_spend():
    import json
    try:
        return json.loads(SPEND_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"total": 0.0, "runs": []}


def _add_spend(amount, chars):
    import json
    d = _load_spend()
    d["total"] = round(float(d.get("total", 0.0)) + amount, 4)
    d.setdefault("runs", []).append(
        {"amount": round(amount, 4), "chars": chars, "ts": _timestamp()})
    SPEND_FILE.write_text(json.dumps(d, indent=1), encoding="utf-8")
    return d


def estimate_run_cost(script, engine):
    """Live estimate of what THIS run will cost."""
    chars = len(script or "")
    if not engine or engine.startswith("💻"):
        return "💵 **This run: $0.00** — Local engine (free, offline, nothing sent to the cloud)."
    est = chars / 1000.0 * FAL_F5_RATE
    return (f"💵 **This run: ~${est:.3f}** — FAL.AI f5-tts · "
            f"{chars:,} characters × ${FAL_F5_RATE:.2f} / 1,000.")


def spend_total_md():
    """Cumulative total paid to fal.ai (as tracked by this tool)."""
    d = _load_spend()
    n = len(d.get("runs", []))
    return (f"🧾 **Total paid to FAL.AI so far: ${float(d.get('total', 0.0)):.2f}** "
            f"across {n} paid run{'' if n == 1 else 's'}.  "
            f"*(estimated from f5-tts pricing; check fal.ai for the exact bill)*")


def fal_available():
    """True if the sibling autoVSL project is present to run the cloud voice."""
    return (AUTOVSL_DIR / ".venv" / "Scripts" / "python.exe").is_file() and \
           (AUTOVSL_DIR / "scripts" / "script-swap.py").is_file()


def fal_voice(video_path, script, log):
    """Clone + speak the script on fal.ai (f5-tts, zero-shot) via autoVSL. Returns an audio path.

    Costs money — the caller must have obtained cost approval before calling this.
    """
    import subprocess
    import shutil
    if not fal_available():
        raise RuntimeError(
            "FAL.AI engine needs the autoVSL project alongside this tool "
            "(expected autoVSL/.venv + autoVSL/scripts/script-swap.py). "
            "Use the Local engine, or run this from the full setup."
        )
    py = AUTOVSL_DIR / ".venv" / "Scripts" / "python.exe"
    swap = AUTOVSL_DIR / "scripts" / "script-swap.py"
    name = "gradio-" + _timestamp()
    work = AUTOVSL_DIR / "output" / "script-swap" / name
    work.mkdir(parents=True, exist_ok=True)
    (work / "script-edited.txt").write_text(script, encoding="utf-8")

    env = dict(os.environ)
    env_file = AUTOVSL_DIR / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip())
    env["PYTHONUTF8"] = "1"

    log("FAL.AI: cloning the voice + speaking your script (f5-tts, zero-shot)…")
    r = subprocess.run(
        [str(py), str(swap), "speak", str(video_path), "--name", name, "--tts", "f5"],
        cwd=str(AUTOVSL_DIR), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.stdout:
        log(r.stdout.strip()[-1200:])
    vo = work / "new-vo.mp3"
    if r.returncode != 0 or not vo.is_file():
        raise RuntimeError("FAL.AI voice failed:\n" + (r.stderr or r.stdout or "")[-800:])
    out_audio = OUTPUTS_DIR / f"dubbed_fal_{_timestamp()}.mp3"
    shutil.copy2(vo, out_audio)
    log(f"  fal.ai voice done: {out_audio.name}")
    return str(out_audio)


def estimate_lipsync_cost(video_path) -> float:
    """~USD to LatentSync-lip-sync this video (by its duration)."""
    dur = get_media_duration(video_path) if video_path else None
    return round((dur or 0.0) * FAL_LATENTSYNC_RATE, 3)


def fal_lipsync(video_path, audio_wav, log):
    """Lip-sync `video_path` to `audio_wav` via fal.ai LatentSync, using
    autoVSL's script-swap.py `lipsync` stage. Returns the output mp4 path.

    Costs money (~$0.20 / 40s) — the caller must have obtained cost approval
    before calling this. This is the route to LatentSync-grade quality that a
    4 GB laptop cannot run locally.
    """
    import subprocess
    import shutil
    if not fal_available():
        raise RuntimeError(
            "Cloud lip-sync needs the autoVSL project alongside this tool "
            "(expected autoVSL/.venv + autoVSL/scripts/script-swap.py)."
        )
    py = AUTOVSL_DIR / ".venv" / "Scripts" / "python.exe"
    swap = AUTOVSL_DIR / "scripts" / "script-swap.py"
    name = "gradio-lipsync-" + _timestamp()
    work = AUTOVSL_DIR / "output" / "script-swap" / name
    work.mkdir(parents=True, exist_ok=True)

    # LatentSync drives the video's mouth from new-vo.mp3 → our dubbed audio.
    vo = work / "new-vo.mp3"
    res = _run(["ffmpeg", "-y", "-i", str(audio_wav),
                "-codec:a", "libmp3lame", "-b:a", "192k", str(vo)])
    if res.returncode != 0 or not vo.exists():
        raise RuntimeError("Could not prepare the audio for lip-sync:\n"
                           + res.stderr[-500:])

    env = dict(os.environ)
    env_file = AUTOVSL_DIR / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip())
    env["PYTHONUTF8"] = "1"

    log("FAL.AI: lip-syncing with LatentSync (cloud GPU)…")
    r = subprocess.run(
        [str(py), str(swap), "lipsync", str(video_path),
         "--name", name, "--tier", "latentsync"],
        cwd=str(AUTOVSL_DIR), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.stdout:
        log(r.stdout.strip()[-1200:])
    ready = FAL_READY_DIR / f"{name}-ready.mp4"
    if r.returncode != 0 or not ready.is_file():
        raise RuntimeError("FAL.AI lip-sync failed:\n" + (r.stderr or r.stdout or "")[-800:])
    out_video = OUTPUTS_DIR / f"lipsync_latentsync_{_timestamp()}.mp4"
    shutil.copy2(ready, out_video)
    log(f"  LatentSync done: {out_video.name}")
    return str(out_video)


# ==========================================================================
# Gradio UI
# ==========================================================================
def _patch_gradio_schema_bug():
    """
    gradio 4.44's bundled gradio_client crashes generating the API schema when a
    JSON schema value is a bare bool (e.g. `additionalProperties: false`):
        TypeError: argument of type 'bool' is not iterable
    It's non-fatal (API docs only) but prints a scary traceback on startup.
    This is the well-known targeted fix: short-circuit bool schemas.
    """
    try:
        import gradio_client.utils as gcu

        _orig = gcu._json_schema_to_python_type

        def _safe(schema, defs=None):
            if isinstance(schema, bool):
                return "bool" if schema else "None"
            return _orig(schema, defs)

        gcu._json_schema_to_python_type = _safe

        _orig_get = gcu.get_type

        def _safe_get(schema):
            if isinstance(schema, bool):
                return "bool" if schema else "None"
            return _orig_get(schema)

        gcu.get_type = _safe_get
    except Exception:
        pass  # if internals change, just skip — the bug is only cosmetic


def _patch_gradio_launch_check():
    """
    Gradio's startup reachability check (networking.url_ok) sends an httpx HEAD
    and only accepts status 200/401/302. On this setup the server answers the
    HEAD with a different code, so the check false-negatives and launch() aborts
    with "localhost is not accessible" — even though the server is genuinely up
    (verified independently). Replace it with a check that treats ANY HTTP
    response as "reachable"; only a real connection failure counts as down.
    """
    try:
        import gradio.networking as gnet
        import httpx
        import time as _t

        def _url_ok(url):
            for _ in range(10):
                try:
                    httpx.get(url, timeout=5, verify=False)
                    return True          # got a response => server is up
                except Exception:
                    _t.sleep(0.5)
            return False

        gnet.url_ok = _url_ok
    except Exception:
        pass


def build_ui():
    _patch_gradio_schema_bug()
    _patch_gradio_launch_check()
    import gradio as gr

    lang_choices = [(f"{label} ({code})", code)
                    for code, label in SUPPORTED_LANGUAGES.items()]

    with gr.Blocks(title="Local Voice-Clone Dubbing Tool") as demo:
        gr.Markdown(
            "# 🎙️ Local Voice-Clone Dubbing Tool\n"
            "Drop in a video, paste your script, and get the speaker's cloned "
            "voice reading **your** words. Outputs a `.wav` and an `.mp4` with "
            "the new audio swapped in. Runs fully local (Coqui XTTS v2).\n\n"
            "✅ **The voice is cloned automatically from your video — you do NOT "
            "need to upload any audio.** Just drop the video + paste the script.\n\n"
            "*Optionally lip-sync the result locally too (Wav2Lip + GFPGAN, free & "
            "offline) — tick the lip-sync box below.*"
        )

        with gr.Row():
            with gr.Column(scale=1):
                video_in = gr.Video(label="1. Drop your video here (mp4, mov, ...)")
                script_in = gr.Textbox(
                    label="2. Paste your script (the exact words to speak)",
                    lines=8,
                    placeholder="Type or paste the words you want spoken, in the "
                                "language you pick on the right...",
                )
                with gr.Row():
                    language_in = gr.Dropdown(
                        choices=lang_choices, value="en",
                        label="3. Script language",
                    )
                    device_in = gr.Radio(
                        choices=["auto", "cuda", "cpu"], value="auto",
                        label="Device (auto = GPU if available)",
                    )
                fit_in = gr.Checkbox(
                    value=True,
                    label="Match the dubbed voice length to the video "
                          "(recommended for lip-sync — makes audio + video the "
                          "same length so it drops straight into fal.ai)",
                )

                with gr.Accordion("⚙️ Dub settings — engine", open=True):
                    engine_in = gr.Radio(
                        choices=["💻 Local — XTTS (free, offline)",
                                 "☁️ FAL.AI — cloud (paid)"],
                        value="💻 Local — XTTS (free, offline)",
                        label="Dubbing engine",
                        info="Local runs on your GPU for free. FAL.AI uses the cloud "
                             "(f5-tts voice clone) and costs money.",
                    )
                    approve_in = gr.Checkbox(
                        value=False,
                        label="💰 I approve fal.ai charges for this run "
                              "(required — FAL.AI won't run until this is ticked)",
                    )
                    gr.Markdown(
                        "*Local = $0. FAL.AI f5-tts ≈ $0.05 per 1,000 characters. "
                        "Nothing is ever sent to the cloud unless you pick FAL.AI **and** "
                        "tick the approval box.*"
                    )
                    cost_est_md = gr.Markdown(estimate_run_cost("", None))
                    total_spent_md = gr.Markdown(spend_total_md())

                with gr.Accordion("👄 Lip-sync the result to the dubbed voice",
                                  open=True):
                    lipsync_engine_in = gr.Radio(
                        choices=[
                            "🚫 Off",
                            "💻 Local — Wav2Lip + GFPGAN (free)",
                            "☁️ Cloud — LatentSync (fal.ai, paid, best quality)",
                        ],
                        value="🚫 Off",
                        label="Lip-sync engine",
                        info="Local runs on your 4 GB GPU (free, but soft mouth + "
                             "slow: ~30s work per 1s video). Cloud LatentSync gives "
                             "the sharp, realistic look a 4 GB GPU can't produce "
                             "locally — ~$0.20 per 40s.",
                    )
                    lipsync_restorer_in = gr.Dropdown(
                        choices=[
                            "GFPGAN — sharp (default)",
                            "CodeFormer — sharper & more identity-faithful",
                            "CodeFormer + 2× upscale — HD (slowest)",
                            "None — fastest, soft mouth",
                        ],
                        value="GFPGAN — sharp (default)",
                        label="Local face restorer (free — applies to the local engine)",
                        info="More free local models: GFPGAN and CodeFormer are two "
                             "different face restorers; CodeFormer +2× also Real-ESRGAN "
                             "upscales the whole frame. All run on your GPU.",
                    )
                    lipsync_approve_in = gr.Checkbox(
                        value=False,
                        label="💰 I approve fal.ai charges for cloud LatentSync "
                              "(required — cloud lip-sync won't run until ticked)",
                    )
                    lipsync_cost_md = gr.Markdown(
                        "*Local = $0. Cloud LatentSync ≈ $0.005/sec "
                        "(~$0.20 per 40s clip). Nothing is sent to the cloud unless "
                        "you pick Cloud **and** tick approval.*")

                dub_btn = gr.Button("🎬 Dub", variant="primary")

                with gr.Accordion("Advanced options (optional — you can ignore these)",
                                  open=False):
                    ref_in = gr.Audio(
                        label="Use a different voice instead of the video's "
                              "(leave empty to clone from the video automatically)",
                        type="filepath",
                    )
                    keepvol_in = gr.Slider(
                        minimum=0.0, maximum=1.0, value=0.0, step=0.05,
                        label="Keep original audio volume (0 = replace fully)",
                    )

            with gr.Column(scale=1):
                status_out = gr.Textbox(
                    label="Status / progress log", lines=18, interactive=False,
                )
                audio_out = gr.Audio(label="Dubbed audio (.wav)", type="filepath")
                video_out = gr.Video(label="Video with new audio (.mp4)")
                lipsync_video_out = gr.Video(
                    label="👄 Lip-synced video (.mp4) — appears when lip-sync is on")
                file_out = gr.File(label="Download files")

        # Local restorer dropdown label -> (restorer, upscale) for lipsync_video.
        _RESTORER_MAP = {
            "GFPGAN — sharp (default)": ("gfpgan", 1),
            "CodeFormer — sharper & more identity-faithful": ("codeformer", 1),
            "CodeFormer + 2× upscale — HD (slowest)": ("codeformer", 2),
            "None — fastest, soft mouth": ("none", 1),
        }

        def _run_dub(video, script, language, reference, keepvol, device, fit,
                     engine, approve, lipsync_engine, lipsync_restorer,
                     lipsync_approve):
            """Generator that streams the log to the UI, then the results.
            Yields 6-tuples: (status, audio, video, lipsync_video, files, spend)."""
            lines = []

            def log(msg):
                lines.append(msg)

            def _lipsync_stage(src_video, dubbed_audio, a, v):
                """After a dub, optionally lip-sync. Off / local Wav2Lip / cloud
                LatentSync. Yields UI updates; cloud is gated on cost approval."""
                mode = lipsync_engine or "🚫 Off"
                if mode.startswith("🚫"):
                    return

                if mode.startswith("☁"):   # cloud LatentSync (paid)
                    if not lipsync_approve:
                        log("")
                        log("⚠️ Cloud LatentSync is PAID. Tick "
                            "“I approve fal.ai charges for cloud LatentSync” and Dub "
                            "again. (Nothing sent to the cloud; no charge.)")
                        yield "\n".join(lines), a, v, None, [a, v], spend_total_md()
                        return
                    est = estimate_lipsync_cost(src_video)
                    log("")
                    log(f"👄 Cloud LatentSync approved — est ~${est:.3f} for this clip.")
                    yield "\n".join(lines), a, v, None, [a, v], spend_total_md()
                    ls = fal_lipsync(src_video, dubbed_audio, log)
                    _add_spend(est, 0)      # keep the running fal.ai total honest
                    log("Lip-sync done ✅")
                    yield "\n".join(lines), a, v, ls, [a, v, ls], spend_total_md()
                    return

                # local Wav2Lip (+ chosen free restorer)
                from lipsync import lipsync_video, lipsync_available
                ok, msg = lipsync_available()
                if not ok:
                    log("⚠ Local lip-sync skipped — " + msg)
                    yield "\n".join(lines), a, v, None, [a, v], spend_total_md()
                    return
                restorer, upscale = _RESTORER_MAP.get(lipsync_restorer, ("gfpgan", 1))
                log("")
                log(f"👄 Lip-syncing locally (Wav2Lip + {restorer}"
                    + (f", {upscale}× upscale" if upscale > 1 else "") + "). This is "
                    "SLOW on 4 GB — roughly 30s of work per 1s of video. Please wait…")
                yield "\n".join(lines), a, v, None, [a, v], spend_total_md()
                free_gpu()  # make sure XTTS isn't holding VRAM during lip-sync
                ls = str(OUTPUTS_DIR / f"lipsync_{_timestamp()}.mp4")
                lipsync_video(src_video, dubbed_audio, ls, restorer=restorer,
                              upscale=upscale, log=log)
                log("Lip-sync done ✅")
                yield "\n".join(lines), a, v, ls, [a, v, ls], spend_total_md()

            use_fal = bool(engine) and engine.startswith("☁")
            try:
                if use_fal:
                    # cost gate: never spend money without explicit approval
                    if not approve:
                        yield ("⚠️ FAL.AI is a PAID cloud engine.\n\nTick "
                               "“I approve fal.ai charges for this run” above, then "
                               "press Dub again.\n\n(Nothing was sent to the cloud; "
                               "no charge.)"), None, None, None, None, spend_total_md()
                        return
                    if not video:
                        yield "Please drop a video first.", None, None, None, None, spend_total_md()
                        return
                    if not script or not script.strip():
                        yield "The script is empty.", None, None, None, None, spend_total_md()
                        return
                    chars = len(script)
                    est = chars / 1000.0 * FAL_F5_RATE
                    log(f"FAL.AI run approved — estimated cost ~${est:.3f} "
                        f"({chars:,} chars).")
                    yield "\n".join(lines), None, None, None, None, spend_total_md()
                    a = fal_voice(video, script, log)
                    stamp = _timestamp()
                    v = str(OUTPUTS_DIR / f"dubbed_fal_{stamp}.mp4")
                    log("Swapping the fal.ai audio into your video…")
                    mux_audio_into_video(video, a, v, keepvol, log)
                    d = _add_spend(est, chars)   # record the charge → running total
                    log(f"Done ✅  (this run ~${est:.3f}; total paid to FAL.AI so far "
                        f"${float(d['total']):.2f})")
                    yield "\n".join(lines), a, v, None, [a, v], spend_total_md()
                    yield from _lipsync_stage(video, a, a, v)
                else:
                    yield "\n".join(lines + ["Starting local dub…"]), None, None, None, None, spend_total_md()
                    a, v = dub_pipeline(video, script, language, reference,
                                        keepvol, device, log, fit_to_video=fit)
                    yield "\n".join(lines), a, v, None, [a, v], spend_total_md()
                    yield from _lipsync_stage(video, a, a, v)
            except Exception as e:
                lines.append("")
                lines.append(f"❌ ERROR: {e}")
                free_gpu()  # don't leave the GPU occupied after a failure
                yield "\n".join(lines), None, None, None, None, spend_total_md()

        dub_btn.click(
            _run_dub,
            inputs=[video_in, script_in, language_in, ref_in, keepvol_in,
                    device_in, fit_in, engine_in, approve_in,
                    lipsync_engine_in, lipsync_restorer_in, lipsync_approve_in],
            outputs=[status_out, audio_out, video_out, lipsync_video_out,
                     file_out, total_spent_md],
        )

        # live per-run cost estimate as the script / engine change
        script_in.change(estimate_run_cost, [script_in, engine_in], cost_est_md)
        engine_in.change(estimate_run_cost, [script_in, engine_in], cost_est_md)

    return demo


# ==========================================================================
# CLI entry point
# ==========================================================================
def run_cli(args):
    def log(msg):
        print(msg, flush=True)

    a, v = dub_pipeline(
        video_path=args.video,
        script=Path(args.script).read_text(encoding="utf-8")
            if os.path.isfile(args.script) else args.script,
        language_code=args.language,
        reference_audio=args.reference,
        keep_original_volume=args.keep_volume,
        device_choice=args.device,
        log=log,
        fit_to_video=not args.no_fit,
    )
    print(f"\nAudio: {a}\nVideo: {v}")


def _force_utf8_stdout():
    """
    The Windows console defaults to cp1252, which can't encode characters like
    the em-dash or ✅ that appear in our log lines. Switch stdout/stderr to UTF-8
    so CLI runs don't crash on a print(). No-op if not supported.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def main():
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(description="Local voice-clone dubbing tool")
    parser.add_argument("--cli", action="store_true",
                        help="Run in command-line mode instead of the web UI.")
    parser.add_argument("--video", help="Path to the input video (CLI mode).")
    parser.add_argument("--script",
                        help="Script text, or a path to a .txt file (CLI mode).")
    parser.add_argument("--language", default="en",
                        help="Language code (default: en).")
    parser.add_argument("--reference", default=None,
                        help="Optional custom reference audio file.")
    parser.add_argument("--keep-volume", type=float, default=0.0,
                        help="Keep original audio at this volume 0.0-1.0 (default 0).")
    parser.add_argument("--device", default="auto",
                        choices=["auto", "cuda", "cpu"],
                        help="Device to run on (default: auto).")
    parser.add_argument("--no-fit", action="store_true",
                        help="Do NOT match audio length to the video "
                             "(default: audio is time-stretched to fit the video "
                             "for lip-sync).")
    parser.add_argument("--share", action="store_true",
                        help="Create a public Gradio share link.")
    args = parser.parse_args()

    if args.cli:
        if not args.video or not args.script:
            parser.error("--cli requires --video and --script")
        run_cli(args)
        return

    print("Launching Gradio app... (first run will download XTTS v2 ~1.9 GB)")
    demo = build_ui()
    demo.queue()  # allow the progress generator to stream
    try:
        demo.launch(inbrowser=True, share=args.share)
    except ValueError as e:
        if "localhost is not accessible" in str(e):
            print(
                "\nGradio couldn't reach 127.0.0.1 (a proxy/VPN/firewall is "
                "blocking loopback).\nEither fix that, or re-launch with a public "
                "share link:\n    python app.py --share\n"
            )
        raise


if __name__ == "__main__":
    main()
