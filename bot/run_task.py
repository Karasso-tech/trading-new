"""Single entry point every Windows Task Scheduler job goes through (2026-08-04).

Two jobs, both of them about the user being able to SEE that the automation ran:

1. It owns the log file. Before this, each .bat redirected `>> foo.log` while the
   Python script it launched also opened foo.log to append. On Windows the cmd
   redirect holds that handle without sharing it, so the script's own open() died
   with PermissionError on its very first log line. TradingNewRefreshPending had
   been crashing that way every night since the .bat was written -- it printed its
   plan ("20 pending: 7 to rebuild") and then died before rebuilding a single
   thesis, while Task Scheduler showed a tidy "last result: 1" nobody was reading.
   backup_db.py hit the same wall one line later, after its copy had already
   landed, so the backup worked and the log said it failed. Now exactly one
   process opens each log: this one. The scripts only print.

2. It sends a Telegram heartbeat when the job finishes. The nightly backup and the
   shadow book are completely silent on success, so "ran and worked" and "never
   ran at all" looked identical from the outside -- which is exactly how
   refresh_pending stayed broken for two days without anyone noticing.

Usage:
    python bot/run_task.py <key>          -- key from TASKS below

The exit code is the child's, unchanged, so Task Scheduler's LastTaskResult still
means what it always meant. A Telegram failure never changes it: a heartbeat that
couldn't be delivered must not turn a successful backup into a failed task.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BOT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BOT_DIR.parent
sys.path.insert(0, str(BOT_DIR))

import run_files  # noqa: E402  -- needs sys.path set above

STATE_FILE = PROJECT_ROOT / "_task_heartbeat_state.json"

# A line the script prints starting with this prefix becomes the heartbeat's
# summary, instead of whatever its last line of output happened to be. Nothing
# is required to print one -- it's an opt-in for scripts that know which of
# their lines is the one worth reading on a phone.
_SUMMARY_PREFIX = "HEARTBEAT:"

# Telegram's cap is 4096; a heartbeat has no business being anywhere near it.
_SUMMARY_MAX_CHARS = 600


# key -> (Hebrew label, script, args, log file, quiet)
#
# quiet=True means "don't message me on every success" -- for the jobs that run
# every 5 or 30 minutes, where a per-run ping would be 288 messages a day and
# would train the user to ignore the whole channel. Those still message
# immediately on failure, and once a day with a roll-up of the quiet runs, so
# silence never means "unknown".
TASKS = {
    "automonitor": (
        "סריקה אוטומטית (אמצע יום)",
        "trigger_auto_monitor.py", [], "trigger_auto_monitor.log", False),
    "automonitor_close": (
        "סריקה אוטומטית (אחרי הסגירה)",
        "trigger_auto_monitor.py", ["post_close"], "trigger_auto_monitor.log", False),
    # Fallback only (2026-08-08): the real post-close scan is chained off the end
    # of refresh_pending. This one fires late and no-ops if that already
    # happened, so a refresh that dies still leaves the user with a scan. quiet
    # because the normal outcome IS the no-op -- a nightly "skipped" ping would
    # be noise, and a genuine failure still messages immediately.
    "automonitor_close_late": (
        "סריקה אוטומטית (גיבוי מאוחר)",
        "trigger_auto_monitor.py", ["post_close_late", "--fallback"], "trigger_auto_monitor.log", True),
    "automonitor_strict": (
        "סריקת פתיחה",
        "trigger_auto_monitor.py", ["strict_open"], "trigger_auto_monitor.log", False),
    "position_status_midday": (
        "מצב פוזיציות (אמצע יום)",
        "trigger_position_status.py", ["midday"], "trigger_position_status.log", False),
    "position_status_close": (
        "מצב פוזיציות (סוף יום)",
        "trigger_position_status.py", ["eod"], "trigger_position_status.log", False),
    "score_shadow": (
        "ספר צללים (ניקוד לילי)",
        "score_shadow.py", [], "score_shadow.log", False),
    "backup_db": (
        "גיבוי יומי",
        "backup_db.py", [], "backup_db.log", False),
    "refresh_pending": (
        "רענון לילי של הרעיונות הממתינים",
        "refresh_pending.py", [], "refresh_pending.log", False),
    "x_feed": (
        "מעקב חדשות (X)",
        "fetch_x_feed.py", [], "x_feed.log", True),
    "ack_watchdog": (
        "שומר הבוט",
        "ack_listener_watchdog.py", [], "ack_listener_watchdog.log", True),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().isoformat()


def _human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} שניות"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} דק' {secs} שנ'"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} שע' {minutes} דק'"


def _load_state() -> dict:
    """Never lets a corrupt/half-written state file stop a real task from running
    -- the roll-up counters are a convenience, the backup underneath them is not."""
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"could not save heartbeat state ({e}) -- roll-up counts may reset", file=sys.stderr)


def _local_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _summarize(output_lines: list[str], ok: bool) -> str:
    """What one line of this run is worth reading on a phone.

    An explicit HEARTBEAT: line wins. Otherwise the last non-empty line, which
    for these scripts is reliably the conclusion ("done", "OK: backed up to
    ...", "fetched=41 stored_or_deduped=12"). On a crash the last line is the
    exception itself, which is the line you want anyway."""
    for line in reversed(output_lines):
        stripped = line.strip()
        if stripped.startswith(_SUMMARY_PREFIX):
            return stripped[len(_SUMMARY_PREFIX):].strip()[:_SUMMARY_MAX_CHARS]
    for line in reversed(output_lines):
        stripped = line.strip()
        if stripped:
            return stripped[:_SUMMARY_MAX_CHARS]
    return "(בלי פלט)" if ok else "(נפל בלי להדפיס כלום)"


def _send(text: str) -> None:
    """Import is deliberately inside the function: a broken telegram_send (bad
    .env, missing requests) must not stop the task itself from running. Same
    reason the whole thing is wrapped -- this is a status ping, not the work."""
    try:
        import telegram_send
        telegram_send.send_text(text)
    except Exception as e:
        print(f"heartbeat send failed ({e}) -- task result was still written to the log", file=sys.stderr)


def _escape(text: str) -> str:
    try:
        import telegram_send
        return telegram_send.escape_html(text)
    except Exception:
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _rollup_if_new_day(key: str, label: str, state: dict) -> None:
    """One message a day for the quiet jobs, covering the day that just ended.

    Sent on the first run of a new local day rather than on a schedule of its
    own, so it needs no extra Task Scheduler entry and can't itself go missing."""
    entry = state.get(key) or {}
    previous_date = entry.get("date")
    today = _local_date()
    if not previous_date or previous_date == today:
        return
    runs = entry.get("runs", 0)
    fails = entry.get("fails", 0)
    if runs == 0 and fails == 0:
        return
    mark = "✅" if fails == 0 else "⚠️"
    _send(
        f"{mark} <b>{_escape(label)}</b> — סיכום {_escape(previous_date)}\n"
        f"רץ {runs} פעמים, {fails} כשלונות."
    )


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in TASKS:
        print(f"usage: python run_task.py <{'|'.join(TASKS)}>", file=sys.stderr)
        return 2

    key = sys.argv[1]
    label, script, args, log_name, quiet = TASKS[key]
    log_path = PROJECT_ROOT / log_name
    script_path = BOT_DIR / script

    # Housekeeping runs BEFORE the job, not after, and here rather than in each
    # script (2026-08-09). Before, so the log this run is about to append to has
    # already been rotated if it was oversized -- ack_listener.log had reached
    # 38 MB. Here, because every scheduled job funnels through this file, so one
    # call covers all of them and no new job can forget to tidy up after itself.
    # housekeeping() swallows its own errors: a failed sweep must never cost a
    # scheduled job its actual work.
    run_files.housekeeping()

    started = time.monotonic()
    output_lines: list[str] = []

    # utf-8 on both ends: these scripts print Hebrew, and the Windows console
    # default (cp1255) would raise UnicodeEncodeError inside the child and turn
    # a working job into a failed one for no reason at all.
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    # Unbuffered, or Python block-buffers stdout the moment it's a pipe instead
    # of a console -- which on refresh_pending's three-hour run meant the log
    # sat empty until the very end, and a job that hung told you nothing at all.
    # The whole reason these logs exist is watching a run in progress.
    child_env["PYTHONUNBUFFERED"] = "1"

    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n{_stamp()}  ===== run_task {key} starting =====\n")
        log.flush()
        try:
            proc = subprocess.Popen(
                [sys.executable, str(script_path), *args],
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_env,
            )
        except Exception as e:
            # Couldn't even start -- no interpreter, script missing after a move.
            # Worth a message precisely because it produces no script output at all.
            log.write(f"{_stamp()}  could not start {script}: {e}\n")
            _send(f"❌ <b>{_escape(label)}</b> — לא הצליח בכלל לרוץ\n{_escape(str(e))}")
            return 1

        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            output_lines.append(line)
            log.write(line + "\n")
            log.flush()  # a job that hangs still leaves everything it got to
        exit_code = proc.wait()
        elapsed = time.monotonic() - started
        log.write(f"{_stamp()}  ===== run_task {key} finished, exit={exit_code}, "
                  f"{elapsed:.0f}s =====\n")

    ok = exit_code == 0
    summary = _summarize(output_lines, ok)

    state = _load_state()
    if quiet:
        _rollup_if_new_day(key, label, state)
        entry = state.get(key) or {}
        if entry.get("date") != _local_date():
            entry = {"date": _local_date(), "runs": 0, "fails": 0}
        entry["runs"] = entry.get("runs", 0) + 1
        if not ok:
            entry["fails"] = entry.get("fails", 0) + 1
        state[key] = entry
        _save_state(state)

    if ok and quiet:
        return exit_code  # counted above, reported in tomorrow's roll-up

    if ok:
        _send(f"✅ <b>{_escape(label)}</b> — רץ בהצלחה ({_human_duration(elapsed)})\n"
              f"{_escape(summary)}")
    else:
        _send(f"❌ <b>{_escape(label)}</b> — נפל (קוד {exit_code}, {_human_duration(elapsed)})\n"
              f"{_escape(summary)}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
