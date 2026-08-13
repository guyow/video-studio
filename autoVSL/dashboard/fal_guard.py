"""Shared fal.ai pre-flight: fail fast, with a clear message, BEFORE any money moves.

Every engine that is about to make a paid fal call runs preflight_fal() first.
The probe is a free 2-byte upload — it exercises auth + account status without
charging anything. One shared word-list so no engine forgets a status code
(402 was missing from several copies of this check).
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # autoVSL/

_BLOCKED_WORDS = ("401", "402", "403", "locked", "balance", "exhaust", "unauthor")

STOPPED_MSG = (
    "STOPPED BEFORE SPENDING: the fal.ai account for the key in autoVSL/.env is "
    "locked or out of balance.\nFix: create your own key at fal.ai/dashboard/keys "
    "(add billing), then replace the FAL_KEY=... line in autoVSL/.env"
)


def load_env(env_file: Path | None = None) -> None:
    """Load autoVSL/.env into os.environ (existing values win)."""
    env_file = env_file or (ROOT / ".env")
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def preflight_fal(context: str = "") -> None:
    """Free probe against fal.ai; sys.exit with a human message if the account can't pay."""
    import sys
    load_env()
    if not os.environ.get("FAL_KEY"):
        sys.exit("FAL_KEY is not set — add it to autoVSL/.env before running paid jobs")
    try:
        import fal_client
        fal_client.upload(b"ok", "text/plain")
    except Exception as e:  # noqa: BLE001 — any failure here means "do not spend"
        msg = str(e)
        low = msg.lower()
        if any(w in low for w in _BLOCKED_WORDS):
            sys.exit(STOPPED_MSG)
        sys.exit(f"fal.ai unreachable{f' ({context})' if context else ''}: {msg[:200]}")
