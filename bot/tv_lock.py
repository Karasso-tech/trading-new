"""Single machine-wide lock guarding the TradingView connection (2026-08-02).

Why this module exists: only ONE process can hold the TradingView bridge at a
time -- a second one dies immediately with `EADDRINUSE ... 127.0.0.1:9223`,
which is a hard crash, not a graceful wait. Found real while adding the nightly
shadow-book job: it was first scheduled at 23:40, twenty minutes after the
post-close /monitorall run starts, and a /monitorall that runs long would have
killed it (or been killed by it) with nothing but a stack trace in a log file
nobody reads.

process_queue.py already had exactly this lock, correct and atomic, but private
to itself -- so every OTHER TradingView user in this project (score_shadow.py,
and the nightly thesis refresh) was outside it. Rather than copy the logic (the
drift failure mode CONSISTENCY_RULES.md's own header warns about), the
implementation moved here and process_queue.py now imports it. The lock FILE
path is unchanged, so an already-running process_queue.py from before this
change still interlocks correctly with anything started after it.

The atomicity note below is load-bearing, carried over verbatim from
process_queue.py's own history -- see acquire()'s docstring.
"""

from __future__ import annotations

import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

# Unchanged from process_queue.py's original constant -- the path IS the
# cross-process contract, so it must never be "tidied" to a new filename.
LOCK_FILE = Path(__file__).resolve().parent / ".process_queue.lock"


def is_locked() -> bool:
    """True only if the PID written in the lock file is a process that's still
    actually running -- a stale file left behind by a crash is not a lock."""
    if not LOCK_FILE.exists():
        return False
    try:
        pid = int(LOCK_FILE.read_text().strip())
    except (ValueError, OSError):
        return False
    result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
    return str(pid) in result.stdout


def acquire() -> bool:
    """Found in the 2026-07-30 full-system checkup: this used to check
    `is_locked()` and only then write the PID file as two separate steps --
    two callers launched close together could both see "not locked" before
    either one finished writing, and both would proceed to run against the same
    TradingView connection at once. `os.O_CREAT | os.O_EXCL` makes the
    create-if-absent check a single atomic OS call -- at most one of two
    simultaneous callers can ever win it, no gap for a second one to slip
    through."""
    if LOCK_FILE.exists() and not is_locked():
        # Lock file left behind by a process that's no longer running (crash,
        # kill) -- clear it before trying to take over. If another instance
        # races to do the same thing, the O_EXCL open below is still the real
        # gate: only one of them can win it either way.
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    return True


def release() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def acquire_waiting(timeout_seconds: int, poll_seconds: int = 30) -> bool:
    """Keep trying to take the lock until `timeout_seconds` elapses. For the
    scheduled overnight jobs, which have hours of runway and should wait for a
    long /monitorall rather than die -- unlike process_queue.py itself, where a
    second instance means a duplicate message and should give up immediately.
    Returns False if the wait ran out, so the caller can log a real reason
    instead of crashing."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        if acquire():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_seconds)


@contextmanager
def held(timeout_seconds: int, poll_seconds: int = 30):
    """`with tv_lock.held(...) as got:` -- always releases, including on an
    exception, so a crashing job can't leave the next night's run blocked
    behind a lock file it will then have to time out on."""
    got = acquire_waiting(timeout_seconds, poll_seconds)
    try:
        yield got
    finally:
        if got:
            release()
