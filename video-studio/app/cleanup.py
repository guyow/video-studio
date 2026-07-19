"""Auto-cleanup queue: after a video is exported, its heavy workdir is queued
and — once the delay passes and every safety check holds — soft-deleted into
.trash/ (reversible). Keeps the disk light without ever hard-deleting work.

The daemon is deliberately paranoid; a queued entry is only swept when:
  • auto-cleanup is enabled in config
  • the entry is past due
  • an exported copy of the deliverable exists (the Desktop copy is insurance)
  • no job is currently running for that video
Everything swept (or skipped) is appended to the cleanup log.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

_lock = threading.Lock()
_queue_file: Path | None = None
_log_file: Path | None = None

SWEEP_EVERY = 10 * 60          # daemon wake interval (seconds)


def init(queue_file: Path, log_file: Path) -> None:
    global _queue_file, _log_file
    _queue_file = queue_file
    _log_file = log_file


def _load() -> dict:
    try:
        return json.loads(_queue_file.read_text(encoding="utf-8"))
    except Exception:                                     # noqa: BLE001
        return {}


def _save(q: dict) -> None:
    _queue_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = _queue_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, indent=1), encoding="utf-8")
    tmp.replace(_queue_file)


def _log(entry: dict) -> None:
    try:
        log = json.loads(_log_file.read_text(encoding="utf-8")) if _log_file.is_file() else []
    except Exception:                                     # noqa: BLE001
        log = []
    log.append({**entry, "ts": time.time()})
    _log_file.write_text(json.dumps(log[-200:], indent=1), encoding="utf-8")


def enqueue(stem: str, delay_hours: float) -> dict:
    with _lock:
        q = _load()
        q[stem] = {"queued": time.time(), "due": time.time() + delay_hours * 3600,
                   "status": "pending"}
        _save(q)
        return q[stem]


def cancel(stem: str) -> bool:
    with _lock:
        q = _load()
        if stem in q:
            del q[stem]
            _save(q)
            return True
        return False


def list_queue() -> list[dict]:
    q = _load()
    now = time.time()
    return [{"stem": s, "queued": e["queued"], "due": e["due"],
             "seconds_left": max(0, int(e["due"] - now)), "status": e.get("status", "pending")}
            for s, e in sorted(q.items(), key=lambda kv: kv[1]["due"])]


def start_daemon(get_cfg: Callable[[], dict],
                 is_busy: Callable[[str], bool],
                 has_export: Callable[[str], bool],
                 do_trash: Callable[[str], str]) -> None:
    """get_cfg() → auto_cleanup config dict · is_busy(stem) · has_export(stem)
    · do_trash(stem) → trash label (raises on failure)."""

    def sweep() -> None:
        cfg = get_cfg()
        if not cfg.get("enabled"):
            return
        now = time.time()
        with _lock:
            q = _load()
            due = [s for s, e in q.items() if e["due"] <= now and e.get("status") == "pending"]
        for stem in due:
            try:
                if is_busy(stem):
                    _log({"stem": stem, "action": "skip", "reason": "job running"})
                    continue
                if not has_export(stem):
                    _log({"stem": stem, "action": "skip", "reason": "no exported copy found — kept"})
                    with _lock:
                        q = _load()
                        if stem in q:
                            q[stem]["status"] = "blocked-no-export"
                            _save(q)
                    continue
                label = do_trash(stem)
                with _lock:
                    q = _load()
                    q.pop(stem, None)
                    _save(q)
                _log({"stem": stem, "action": "cleaned", "trash": label})
            except Exception as exc:                      # noqa: BLE001
                _log({"stem": stem, "action": "error", "error": str(exc)})

    def loop() -> None:
        while True:
            try:
                sweep()
            except Exception:                             # noqa: BLE001
                pass
            time.sleep(SWEEP_EVERY)

    threading.Thread(target=loop, daemon=True, name="cleanup-daemon").start()
