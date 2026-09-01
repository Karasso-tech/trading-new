"""Detects a dead or hung ack_listener.py and restarts it (2026-07-14).

Found real, 2026-07-13: ack_listener.py hung inside a getUpdates() call with no
client-side timeout after a network blip -- not crashed, just permanently stuck,
for 9+ hours overnight with zero log output and no way for anything to notice on
its own. That specific cause is fixed at the source (telegram_send.py now passes
timeout= on every request), but this watchdog is the general safety net: it
doesn't matter WHY the listener stops making progress, only that it has, and that
someone/something needs to restart it. Meant to run on a schedule (Windows Task
Scheduler, every few minutes) -- see run_ack_listener_watchdog.bat.

Two independent signals, either one triggers a restart:
1. PID file missing, or the PID in it isn't a live process (dead).
2. Heartbeat file missing, or older than STALE_SECONDS (hung -- process is alive
   per (1) but not making progress; ack_listener.py writes a fresh heartbeat at
   the top of every poll loop iteration, BEFORE the network call that can hang).

Deliberately does NOT try to distinguish "genuinely idle, no new Telegram
messages" from "stuck" via the log file alone -- both look like log silence, only
the heartbeat (written every ~3s regardless of whether there were any messages)
tells them apart.
"""

import subprocess
import sys
import time
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent
PID_FILE = BOT_DIR / ".ack_listener.pid"
HEARTBEAT_FILE = BOT_DIR / ".ack_listener_heartbeat"
STALE_SECONDS = 180  # generous margin over POLL_SECONDS=3 + the 15s HTTP timeout


def _pid_alive(pid: int) -> bool:
    result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
    return str(pid) in result.stdout


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _heartbeat_age_seconds() -> float | None:
    try:
        return time.time() - int(HEARTBEAT_FILE.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _kill_if_alive(pid: int) -> None:
    if _pid_alive(pid):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


def _restart() -> None:
    subprocess.Popen(
        ["python", str(BOT_DIR / "ack_listener.py")],
        cwd=str(BOT_DIR.parent), shell=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )


def main() -> None:
    pid = _read_pid()
    age = _heartbeat_age_seconds()

    if pid is None:
        print("No PID file -- ack_listener.py has never started (or the file was removed). Starting it.")
        _restart()
        return

    if not _pid_alive(pid):
        print(f"PID {pid} is not running (dead) -- restarting.")
        _restart()
        return

    if age is None:
        print(f"PID {pid} is alive but no heartbeat file exists -- restarting to be safe.")
        _kill_if_alive(pid)
        _restart()
        return

    if age > STALE_SECONDS:
        print(f"PID {pid} is alive but heartbeat is {age:.0f}s old (> {STALE_SECONDS}s) -- hung, killing and restarting.")
        _kill_if_alive(pid)
        _restart()
        return

    print(f"OK: PID {pid} alive, heartbeat {age:.0f}s old -- healthy.")


if __name__ == "__main__":
    sys.exit(main())
