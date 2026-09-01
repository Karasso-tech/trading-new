"""Task-Scheduler entry point for the automated open-position status check
(2026-07-16). Twice-daily companion to trigger_auto_monitor.py, but for
positions that are already FILLED (persistence.get_open_positions()) rather
than pending theses -- gives a short "where do I stand / what should I do"
Telegram summary instead of a full /playbook report.

Two modes, each its own Task Scheduler job:
- `midday`: fires near today's real NYSE session midpoint (see
  market_hours.midday_window_now -- derived from the actual open/close, so
  early-close days shift it automatically instead of firing at a stale clock
  time). Local Task Scheduler trigger time is only a best guess, same
  reasoning as trigger_auto_monitor.py: this script's own gate is
  authoritative.
- `eod`: fires in the same post-close grace window trigger_auto_monitor.py's
  post_close mode already uses (market_hours.market_closed_recently()).

Enqueues a synthetic '/positions' message -- same code path as a user
manually typing it in Telegram, see process_queue.py's
_handle_position_status -- with a negative, timestamp-derived update_id so it
can never collide with a real Telegram update_id. Then runs process_queue.py
and WAITS for it (subprocess.run, not Popen), for the identical reason
trigger_auto_monitor.py does: this script's only job per invocation is to run
once and exit, so a fire-and-forget Popen risks Task Scheduler tearing down
the child early.
"""

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import persistence
from market_hours import market_closed_recently, midday_window_now

BOT_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["midday", "eod"])
    args = parser.parse_args()

    if args.mode == "eod":
        gate_ok, label = market_closed_recently(), "eod"
    else:
        gate_ok, label = midday_window_now(), "midday"

    if not gate_ok:
        print(f"Not in the {label} window -- skipping automated /positions check.")
        return

    update_id = -int(datetime.now(timezone.utc).timestamp())
    ok = persistence.enqueue_message(
        update_id=update_id, from_id="scheduled", chat_id="scheduled",
        message_type="text", message_text="/positions",
        raw_update={"synthetic": True, "source": f"trigger_position_status.py:{label}"},
    )
    if not ok:
        print(f"update_id {update_id} already exists -- skipping (should not happen).")
        return

    print(f"Enqueued synthetic /positions ({label}, update_id={update_id}), running process_queue.py...")
    result = subprocess.run(
        ["python", str(BOT_DIR / "process_queue.py")],
        cwd=str(BOT_DIR.parent), shell=True,
    )
    print(f"process_queue.py exited with code {result.returncode}.")


if __name__ == "__main__":
    main()
