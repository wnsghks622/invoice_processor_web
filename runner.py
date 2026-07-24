"""Run a vendored core/ module as a subprocess and stream its stdout as Server-Sent Events.

Runs `python -m core.<module>` with cwd = the web app folder, so the package imports and the
app's config/.env resolve. stdin is /dev/null-equivalent (DEVNULL) so the headless-guarded
input() calls in the scripts never block.

Only ONE job runs at a time (module-level lock): two processors over the same drop folder
would record every invoice twice. A second request streams a busy message instead. If the
client disconnects mid-run (tab closed, connection dropped), the child process is terminated -
otherwise the UI re-enables its buttons while an invisible job keeps mutating data."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from queue import Queue, Empty

import config


def _sse(event: str, data: str) -> str:
    payload = "".join(f"data: {line}\n" for line in (data.splitlines() or [""]))
    return f"event: {event}\n{payload}\n"


_job_lock = threading.Lock()
_current_job = None      # module name of the running job, for the busy message


def stream_module(module: str, *args: str):
    """Generator yielding SSE frames for `python -m core.<module> args...`, then a final
    'done' event with the exit code. See the module docstring for the locking rules."""
    global _current_job
    if not _job_lock.acquire(blocking=False):
        yield _sse("log", f"[!] Another job ({_current_job or '?'}) is still running - "
                          f"only one can run at a time.")
        yield _sse("log", "    Wait for it to finish, then try again.")
        yield _sse("done", "1")
        return
    _current_job = module
    proc = None
    try:
        cmd = [sys.executable, "-u", "-m", f"core.{module}", *[str(a) for a in args]]
        shown = " ".join(str(a) for a in args)
        yield _sse("log", f"$ python -m core.{module} {shown}".rstrip())

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(config.HERE),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                # We decode as UTF-8, but a child Python writing to a pipe defaults to the ANSI
                # codepage (cp1252) on Windows - force its stdio to UTF-8 so names don't mojibake.
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        except OSError as e:
            yield _sse("log", f"ERROR: could not start process: {e}")
            yield _sse("done", "1")
            return

        q: Queue = Queue()

        def pump():
            assert proc.stdout is not None
            for line in proc.stdout:
                q.put(line.rstrip("\n"))
            q.put(None)

        threading.Thread(target=pump, daemon=True).start()

        while True:
            try:
                line = q.get(timeout=0.5)
            except Empty:
                yield ": keepalive\n\n"
                continue
            if line is None:
                break
            yield _sse("log", line)

        yield _sse("done", str(proc.wait()))
    except GeneratorExit:
        # Client went away mid-run. Stop the child so the freed lock can't let a second
        # copy race the abandoned one.
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        raise
    finally:
        _current_job = None
        _job_lock.release()
