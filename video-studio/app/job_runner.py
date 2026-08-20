"""Detached job supervisor — keeps an engine run alive across server restarts.

The dashboard used to pipe engine output straight into the Flask process, so
killing/restarting server.py (code deploy, stale-UI restart) broke the pipe and
took the engine down with it. Instead, server.py now spawns THIS script in its
own process group; it runs the engine with output appended to jobs/<id>.out and
writes a `__VS_EXIT__ <code>` sentinel when the engine finishes. The server
tails the file — and after a restart it re-attaches to the same file and the
still-running process instead of marking the job interrupted.

Usage: python job_runner.py <out_file> -- <engine cmd...>
Exits with the engine's return code.
"""
import os
import subprocess
import sys


def main() -> int:
    out_path = sys.argv[1]
    cmd = sys.argv[sys.argv.index("--") + 1:]
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"   # file-redirected stdout must not block-buffer
    with open(out_path, "a", encoding="utf-8", errors="replace", buffering=1) as out:
        try:
            proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env)
            rc = proc.wait()
        except Exception as exc:  # noqa: BLE001 — surface launch failures in the job log
            out.write(f"[runner] failed to start: {exc}\n")
            rc = -1
        out.write(f"__VS_EXIT__ {rc}\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
