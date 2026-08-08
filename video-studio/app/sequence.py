#!/usr/bin/env python3
"""Sequence document (EDL) — the source of truth for the timeline editor.

Everything else in this app consumes a file and emits a new file. That stays
true: an AI op here targets a *clip*, produces a new source, and the clip's
`src` swaps to it while `origin` remembers what it replaced. Editing is
non-destructive because nothing is ever written back over a source.

Geometry contract (the thing to keep straight):
    start           where the clip sits on the timeline, seconds
    in / out        the range taken FROM the source, seconds
    speed           playback rate; duration = (out - in) / speed

Pure module: no Flask, no config, no globals pointing at disk. Callers hand it
paths. That is what makes it testable without a server.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

SCHEMA = 1
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".flac"}

TRACK_KINDS = ("video", "text", "audio")
KEEP_HISTORY = 40           # versions retained for undo
EPS = 1e-6                  # timeline comparisons are in seconds; this is well below a frame


# ---------------------------------------------------------------- construction

def _uid(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def new_project(name: str, w: int = 1080, h: int = 1920, fps: int = 30) -> dict:
    """A blank project with the three tracks every edit starts from."""
    return {
        "schema": SCHEMA,
        "id": _uid("p"),
        "name": name or "untitled",
        "version": 1,
        "created": time.time(),
        "modified": time.time(),
        "canvas": {"w": int(w), "h": int(h), "fps": int(fps)},
        "tracks": [
            {"id": "V1", "kind": "video", "name": "Video", "muted": False, "clips": []},
            {"id": "T1", "kind": "text", "name": "Text", "muted": False, "clips": []},
            {"id": "A1", "kind": "audio", "name": "Audio", "muted": False, "clips": []},
        ],
        "markers": [],
        "meta": {},
    }


def new_clip(src: str, in_: float, out: float, start: float, **extra) -> dict:
    clip = {
        "id": _uid("c"),
        "src": src,
        "in": round(float(in_), 4),
        "out": round(float(out), 4),
        "start": round(float(start), 4),
        "speed": 1.0,
        "volume": 1.0,
        "transform": {"scale": 1.0, "x": 0.0, "y": 0.0, "rot": 0.0},
        "effects": [],
        "transition_in": None,
        "origin": {"engine": None, "parent": None},
    }
    clip.update(extra)
    return clip


def new_text_clip(text: str, start: float, dur: float, **extra) -> dict:
    clip = {
        "id": _uid("t"),
        "text": text,
        "start": round(float(start), 4),
        "dur": round(float(dur), 4),
        "style": {"size": 64, "color": "#FFFFFF", "outline": 3,
                  "font": "Arial", "bold": True, "pos": "bottom"},
    }
    clip.update(extra)
    return clip


# ---------------------------------------------------------------- geometry

def clip_dur(clip: dict) -> float:
    """Timeline duration of a clip. Text/audio clips carry `dur` directly."""
    if "dur" in clip and "out" not in clip:
        return max(0.0, float(clip.get("dur") or 0))
    speed = float(clip.get("speed") or 1.0) or 1.0
    return max(0.0, (float(clip["out"]) - float(clip["in"])) / speed)


def clip_end(clip: dict) -> float:
    return float(clip.get("start") or 0) + clip_dur(clip)


def duration(doc: dict) -> float:
    """Longest track end — the render length."""
    return max((clip_end(c) for t in doc.get("tracks", []) for c in t.get("clips", [])),
               default=0.0)


def find_track(doc: dict, track_id: str) -> dict:
    for t in doc.get("tracks", []):
        if t.get("id") == track_id:
            return t
    raise KeyError(f"no track {track_id!r}")


def find_clip(doc: dict, clip_id: str) -> tuple[dict, dict]:
    for t in doc.get("tracks", []):
        for c in t.get("clips", []):
            if c.get("id") == clip_id:
                return t, c
    raise KeyError(f"no clip {clip_id!r}")


# ---------------------------------------------------------------- validation

def validate(doc: dict) -> None:
    """Raise ValueError on anything the renderer could not honour.

    Deliberately strict: a document that reaches the renderer malformed costs a
    confusing ffmpeg error minutes later, which is much harder to read than this.
    """
    if not isinstance(doc, dict):
        raise ValueError("document must be an object")
    if int(doc.get("schema") or 0) != SCHEMA:
        raise ValueError(f"unsupported schema {doc.get('schema')!r} (expected {SCHEMA})")
    cv = doc.get("canvas") or {}
    for k in ("w", "h", "fps"):
        if int(cv.get(k) or 0) <= 0:
            raise ValueError(f"canvas.{k} must be a positive integer")
    if int(cv["w"]) % 2 or int(cv["h"]) % 2:
        raise ValueError("canvas w/h must be even (h264 requires it)")

    tracks = doc.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        raise ValueError("document needs at least one track")
    seen_tracks: set[str] = set()
    seen_clips: set[str] = set()
    for t in tracks:
        tid = t.get("id")
        if not tid or tid in seen_tracks:
            raise ValueError(f"duplicate or missing track id {tid!r}")
        seen_tracks.add(tid)
        if t.get("kind") not in TRACK_KINDS:
            raise ValueError(f"track {tid}: kind must be one of {TRACK_KINDS}")
        clips = t.get("clips")
        if not isinstance(clips, list):
            raise ValueError(f"track {tid}: clips must be a list")

        for c in clips:
            cid = c.get("id")
            if not cid or cid in seen_clips:
                raise ValueError(f"duplicate or missing clip id {cid!r}")
            seen_clips.add(cid)
            if float(c.get("start", 0)) < -EPS:
                raise ValueError(f"clip {cid}: start must be >= 0")
            if t["kind"] == "text":
                if clip_dur(c) <= 0:
                    raise ValueError(f"clip {cid}: text clip needs a positive dur")
                continue
            if not c.get("src"):
                raise ValueError(f"clip {cid}: src is required")
            if float(c.get("out", 0)) - float(c.get("in", 0)) <= EPS:
                raise ValueError(f"clip {cid}: out must be greater than in")
            if float(c.get("in", 0)) < -EPS:
                raise ValueError(f"clip {cid}: in must be >= 0")
            sp = float(c.get("speed") or 1.0)
            if not (0.1 <= sp <= 10.0):
                raise ValueError(f"clip {cid}: speed must be between 0.1 and 10")

        # overlap check per track — one track cannot show two clips at once
        ordered = sorted(clips, key=lambda c: float(c.get("start") or 0))
        for a, b in zip(ordered, ordered[1:]):
            if clip_end(a) > float(b.get("start") or 0) + EPS:
                raise ValueError(
                    f"track {tid}: clips {a['id']} and {b['id']} overlap "
                    f"({clip_end(a):.3f}s > {float(b['start']):.3f}s)")


def normalize(doc: dict) -> dict:
    """Sort clips by start and round drifting floats. Safe to call repeatedly."""
    for t in doc.get("tracks", []):
        for c in t.get("clips", []):
            for k in ("start", "in", "out", "dur"):
                if k in c and c[k] is not None:
                    c[k] = round(float(c[k]), 4)
        t["clips"] = sorted(t.get("clips", []), key=lambda c: float(c.get("start") or 0))
    return doc


# ---------------------------------------------------------------- mutations
# Each returns the mutated doc. They assume valid input and keep it valid;
# callers validate() before saving.

def add_clip(doc: dict, track_id: str, clip: dict, *, append: bool = False) -> dict:
    track = find_track(doc, track_id)
    if append:
        clip["start"] = round(max((clip_end(c) for c in track["clips"]), default=0.0), 4)
    track["clips"].append(clip)
    return normalize(doc)


def remove_clip(doc: dict, clip_id: str, *, ripple: bool = False) -> dict:
    track, clip = find_clip(doc, clip_id)
    gap = clip_dur(clip)
    at = float(clip["start"])
    track["clips"] = [c for c in track["clips"] if c["id"] != clip_id]
    if ripple:
        for c in track["clips"]:
            if float(c["start"]) >= at - EPS:
                c["start"] = round(float(c["start"]) - gap, 4)
    return normalize(doc)


def move_clip(doc: dict, clip_id: str, start: float, track_id: str | None = None) -> dict:
    src_track, clip = find_clip(doc, clip_id)
    clip["start"] = round(max(0.0, float(start)), 4)
    if track_id and track_id != src_track["id"]:
        dst = find_track(doc, track_id)
        if dst["kind"] != src_track["kind"]:
            raise ValueError(f"cannot move a {src_track['kind']} clip to a {dst['kind']} track")
        src_track["clips"] = [c for c in src_track["clips"] if c["id"] != clip_id]
        dst["clips"].append(clip)
    return normalize(doc)


def trim_clip(doc: dict, clip_id: str, *, edge: str, delta: float) -> dict:
    """Drag a clip edge by `delta` seconds. `edge` is "in" or "out".

    Trimming the head moves `in` AND `start` together so the rest of the clip
    stays put on the timeline — that is what makes a trim feel like a trim
    rather than a slide.
    """
    _, clip = find_clip(doc, clip_id)
    if "dur" in clip and "out" not in clip:            # text clip
        if edge == "out":
            clip["dur"] = round(max(0.1, clip_dur(clip) + delta), 4)
        else:
            d = min(delta, clip_dur(clip) - 0.1)
            clip["start"] = round(max(0.0, float(clip["start"]) + d), 4)
            clip["dur"] = round(clip_dur(clip) - d, 4)
        return normalize(doc)

    speed = float(clip.get("speed") or 1.0)
    if edge == "in":
        src_delta = delta * speed
        new_in = min(max(0.0, float(clip["in"]) + src_delta), float(clip["out"]) - 0.05)
        applied = (new_in - float(clip["in"])) / speed
        clip["in"] = round(new_in, 4)
        clip["start"] = round(max(0.0, float(clip["start"]) + applied), 4)
    elif edge == "out":
        new_out = max(float(clip["in"]) + 0.05, float(clip["out"]) + delta * speed)
        clip["out"] = round(new_out, 4)
    else:
        raise ValueError("edge must be 'in' or 'out'")
    return normalize(doc)


def split_clip(doc: dict, clip_id: str, at: float) -> dict:
    """Cut a clip at timeline position `at`, producing two adjacent clips."""
    track, clip = find_clip(doc, clip_id)
    start, dur = float(clip["start"]), clip_dur(clip)
    if not (start + 0.05 < at < start + dur - 0.05):
        raise ValueError("split point must fall inside the clip")
    offset = at - start
    right = json.loads(json.dumps(clip))
    right["id"] = _uid("c")
    right["start"] = round(at, 4)
    if "dur" in clip and "out" not in clip:
        clip["dur"] = round(offset, 4)
        right["dur"] = round(dur - offset, 4)
    else:
        speed = float(clip.get("speed") or 1.0)
        cut = float(clip["in"]) + offset * speed
        right["in"] = round(cut, 4)
        clip["out"] = round(cut, 4)
        right["transition_in"] = None
    track["clips"].append(right)
    return normalize(doc)


def ripple_delete_range(doc: dict, track_id: str, a: float, b: float) -> dict:
    """Remove [a,b) from a track and close the gap.

    This is the primitive behind transcript editing: delete words, and the
    video closes up behind them.
    """
    if b <= a + EPS:
        raise ValueError("range end must be after start")
    track = find_track(doc, track_id)
    gap = b - a
    out: list[dict] = []
    for c in sorted(track["clips"], key=lambda c: float(c["start"])):
        cs, ce = float(c["start"]), clip_end(c)
        if ce <= a + EPS:                                  # entirely before
            out.append(c)
        elif cs >= b - EPS:                                # entirely after — shift left
            c["start"] = round(cs - gap, 4)
            out.append(c)
        elif cs >= a - EPS and ce <= b + EPS:              # swallowed
            continue
        elif cs < a and ce > b:                            # cut a hole: keep both sides
            left = json.loads(json.dumps(c))
            right = json.loads(json.dumps(c))
            speed = float(c.get("speed") or 1.0)
            right["id"] = _uid("t" if "dur" in c and "out" not in c else "c")
            right["start"] = round(a, 4)
            if "dur" in c and "out" not in c:
                left["dur"] = round(a - cs, 4)
                right["dur"] = round(ce - b, 4)
            else:
                left["out"] = round(float(c["in"]) + (a - cs) * speed, 4)
                right["in"] = round(float(c["in"]) + (b - cs) * speed, 4)
            out.extend([left, right])
        elif cs < a:                                       # tail overlaps — trim its out
            speed = float(c.get("speed") or 1.0)
            if "dur" in c and "out" not in c:
                c["dur"] = round(a - cs, 4)
            else:
                c["out"] = round(float(c["in"]) + (a - cs) * speed, 4)
            out.append(c)
        else:                                              # head overlaps — trim its in
            speed = float(c.get("speed") or 1.0)
            if "dur" in c and "out" not in c:
                c["dur"] = round(ce - b, 4)
            else:
                c["in"] = round(float(c["in"]) + (b - cs) * speed, 4)
            c["start"] = round(a, 4)
            out.append(c)
    track["clips"] = out
    return normalize(doc)


def swap_source(doc: dict, clip_id: str, new_src: str, engine: str) -> dict:
    """Point a clip at an engine's output, remembering what it replaced.

    The AI ops all land here. `in`/`out` reset to the full new file because an
    engine returns exactly the range it was given.
    """
    _, clip = find_clip(doc, clip_id)
    clip["origin"] = {"engine": engine, "parent": clip.get("src"),
                      "parent_in": clip.get("in"), "parent_out": clip.get("out"),
                      "at": time.time()}
    dur = clip_dur(clip)
    clip["src"] = new_src
    clip["in"] = 0.0
    clip["out"] = round(dur * float(clip.get("speed") or 1.0), 4)
    return normalize(doc)


def revert_source(doc: dict, clip_id: str) -> dict:
    _, clip = find_clip(doc, clip_id)
    origin = clip.get("origin") or {}
    if not origin.get("parent"):
        raise ValueError("clip has no previous source to revert to")
    clip["src"] = origin["parent"]
    if origin.get("parent_in") is not None:
        clip["in"] = float(origin["parent_in"])
        clip["out"] = float(origin["parent_out"])
    clip["origin"] = {"engine": None, "parent": None}
    return normalize(doc)


# ---------------------------------------------------------------- persistence

def project_dir(root: Path, slug: str) -> Path:
    return Path(root) / "output" / "projects" / slug


def doc_path(root: Path, slug: str) -> Path:
    return project_dir(root, slug) / "project.json"


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path: Path, doc: dict, *, keep_history: bool = True) -> dict:
    """Atomic write with a version bump and a history snapshot for undo.

    Atomic because a half-written project.json is a lost edit session; the
    os.replace below is the only moment the live file changes.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalize(doc)
    validate(doc)

    if keep_history and path.is_file():
        hist = path.parent / "history"
        hist.mkdir(exist_ok=True)
        try:
            prev = load(path)
            (hist / f"v{int(prev.get('version') or 0):06d}.json").write_text(
                json.dumps(prev, indent=1), encoding="utf-8")
            snaps = sorted(hist.glob("v*.json"))
            for old in snaps[:-KEEP_HISTORY]:
                old.unlink(missing_ok=True)
        except Exception:
            pass          # history is a convenience; never block a save on it

    doc["version"] = int(doc.get("version") or 0) + 1
    doc["modified"] = time.time()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return doc


def undo(path: Path) -> dict:
    """Restore the most recent history snapshot."""
    path = Path(path)
    hist = path.parent / "history"
    snaps = sorted(hist.glob("v*.json")) if hist.is_dir() else []
    if not snaps:
        raise ValueError("nothing to undo")
    prev = json.loads(snaps[-1].read_text(encoding="utf-8"))
    snaps[-1].unlink(missing_ok=True)
    current = load(path) if path.is_file() else {}
    prev["version"] = int(current.get("version") or prev.get("version") or 1) + 1
    prev["modified"] = time.time()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(prev, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return prev


def sources(doc: dict) -> list[str]:
    """Every distinct source file the document references."""
    seen: list[str] = []
    for t in doc.get("tracks", []):
        for c in t.get("clips", []):
            s = c.get("src")
            if s and s not in seen:
                seen.append(s)
    return seen
