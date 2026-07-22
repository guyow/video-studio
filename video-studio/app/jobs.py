"""Video Studio job store — persistent, resumable, GPU-arbitrated.

Wraps the in-memory `jobs` dict the dashboard code already uses, adding:
  * write-through persistence:  jobs/jobs.json (index) + jobs/<id>.log (output)
  * load-on-startup:            jobs that were `running` when the server died
                                come back as `interrupted` (resumable if a cmd
                                was recorded)
  * GPU arbitration:            one GPU job at a time (4 GB card), yielding to
                                a foreign erase run by subtitle-studio or the
                                old dashboard (ported from subtitle-studio)
  * keep-awake:                 Windows can't sleep while a job runs

The dashboard's request handlers keep mutating job dicts directly; a flusher
thread snapshots state every 2 s, so a crash loses at most ~2 s of log lines.
"""
import json
import subprocess
import threading
import time
import uuid
from pathlib import Path

JOBS_DIR = Path(__file__).resolve().parent.parent / "jobs"
INDEX_FILE = JOBS_DIR / "jobs.json"
KEEP_JOBS = 300          # retention: newest N jobs survive in the index
FLUSH_EVERY = 2.0        # seconds between persistence sweeps

# engine scripts that need the GPU — detected from the job's command line
GPU_MARKERS = ("transcribe.py", "caption.py", "recaption.py", "erase_subs.py", "local_dub.py")

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
GPU_LOCK = threading.Lock()  # the 4GB GPU fits exactly one AI engine run
_meta: dict[str, dict] = {}  # flusher bookkeeping, kept out of the job dicts


# ---------------------------------------------------------------- creation

def create(action: str, slug: str, label: str, **extra) -> str:
    """Register a new running job and return its id.

    Every job insertion goes through here so the `jobs` dict is only mutated
    under `jobs_lock` — the readers (_write_index/_flush_logs/_awake_keeper and
    the /api/jobs handler) iterate it under the same lock, so this closes the
    "dictionary changed size during iteration" race. `extra` carries the
    occasional optional key (e.g. gpu=True, resumed_from=<id>).
    """
    job_id = uuid.uuid4().hex[:8]
    job = {
        "id": job_id, "action": action, "slug": slug, "label": label,
        "status": "running", "lines": [], "returncode": None,
        "started": time.time(), "ended": None,
    }
    job.update(extra)
    with jobs_lock:
        jobs[job_id] = job
    return job_id


# ---------------------------------------------------------------- persistence

def _index_view(job: dict) -> dict:
    """Everything but the log lines and private (underscore) keys."""
    return {k: v for k, v in job.items() if k != "lines" and not k.startswith("_")}


def _write_index() -> None:
    with jobs_lock:
        entries = sorted(jobs.values(), key=lambda j: j.get("started") or 0)[-KEEP_JOBS:]
        data = [_index_view(j) for j in entries]
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(INDEX_FILE)


def _flush_logs() -> bool:
    """Append any new log lines to jobs/<id>.log. Returns True if anything changed."""
    changed = False
    with jobs_lock:
        snapshot = [(j, list(j.get("lines") or [])) for j in jobs.values()]
    for job, lines in snapshot:
        m = _meta.setdefault(job["id"], {"flushed": 0, "sig": None})
        if len(lines) > m["flushed"]:
            JOBS_DIR.mkdir(parents=True, exist_ok=True)
            with open(JOBS_DIR / f"{job['id']}.log", "a", encoding="utf-8") as f:
                f.write("\n".join(lines[m["flushed"]:]) + "\n")
            m["flushed"] = len(lines)
            changed = True
        sig = (job.get("status"), job.get("ended"), job.get("returncode"))
        if m["sig"] != sig:
            m["sig"] = sig
            changed = True
    return changed


def _flusher() -> None:
    while True:
        try:
            if _flush_logs():
                _write_index()
        except Exception:
            pass  # persistence must never take a job down
        time.sleep(FLUSH_EVERY)


def load_persisted() -> None:
    """Bring back every stored job; dead `running` jobs become `interrupted`."""
    try:
        entries = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    for e in entries:
        jid = e.get("id")
        if not jid or jid in jobs:
            continue
        log = JOBS_DIR / f"{jid}.log"
        try:
            lines = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
        except Exception:
            lines = []
        e["lines"] = lines
        _meta[jid] = {"flushed": len(lines), "sig": None}
        if e.get("status") == "running":
            e["status"] = "interrupted"
            e["ended"] = e.get("ended") or time.time()
            note = "⏸ server restarted while this job was running"
            note += " — press Resume to re-run (engines with caches pick up where they left off)" \
                if e.get("cmd") else ""
            e["lines"].append(note)
        jobs[jid] = e
    # prune orphaned logs beyond retention
    keep = {j["id"] for j in jobs.values()}
    for log in JOBS_DIR.glob("*.log"):
        if log.stem not in keep:
            try:
                log.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------- GPU arbitration
# Ported from subtitle-studio/server.py so Video Studio never fights the old
# apps (or itself) for the single 4 GB GPU.

def needs_gpu(cmd: list[str]) -> bool:
    return any(m in part for part in cmd for m in GPU_MARKERS)


def _log(job: dict, line: str) -> None:
    with jobs_lock:
        job["lines"].append(line)


def _stopped(job: dict) -> bool:
    return job.get("status") == "stopped"


def foreign_erase_running() -> bool:
    """True if ANOTHER app (subtitle-studio / old dashboard) has an AI erase on the GPU.
    Our own GPU jobs serialize through GPU_LOCK, which is held before launch —
    so at acquisition time any matching process is foreign."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and "
             "($_.CommandLine -match 'inference_propainter' -or "
             "$_.CommandLine -match 'erase_subs') }).Count"],
            capture_output=True, text=True, timeout=30)
        return int((r.stdout or "0").strip() or 0) > 0
    except Exception:
        return False


def wait_for_gpu(job: dict, max_wait: int = 3600) -> None:
    """Yield to a foreign AI erase, staying responsive to a user Stop."""
    waited = 0
    while foreign_erase_running():
        if _stopped(job):
            raise RuntimeError("stopped by user")
        if waited == 0:
            _log(job, "GPU is busy with another AI erase (other app) — waiting for it to finish…")
        elif waited % 120 == 0:
            _log(job, f"…still waiting for the GPU ({waited // 60} min)")
        time.sleep(20)
        waited += 20
        if waited >= max_wait:
            raise RuntimeError("gave up waiting for the GPU after an hour — try again later")


def acquire_gpu(job: dict) -> None:
    """Take the GPU lock, staying responsive to a user Stop while queued."""
    if not GPU_LOCK.acquire(blocking=False):
        _log(job, "waiting for another Video Studio GPU job to finish…")
    else:
        return
    while not GPU_LOCK.acquire(timeout=5):
        if _stopped(job):
            raise RuntimeError("stopped by user")


def _awake_keeper() -> None:
    """Stop Windows from sleeping mid-job. Resets the idle timer every 50 s while
    any job runs; must stay on one thread — SetThreadExecutionState is per-thread."""
    import ctypes
    while True:
        with jobs_lock:
            busy = any(j.get("status") == "running" for j in jobs.values())
        if busy:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(0x00000001)  # ES_SYSTEM_REQUIRED
            except Exception:
                pass
        time.sleep(50)


def init() -> None:
    """Call once at server startup: restore persisted jobs, start the flusher
    and keep-awake threads."""
    load_persisted()
    threading.Thread(target=_flusher, daemon=True).start()
    threading.Thread(target=_awake_keeper, daemon=True).start()
