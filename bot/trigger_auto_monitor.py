"""Task-Scheduler entry point for the automated /monitorall scan (2026-07-11).

Task Scheduler (2026-07-12) fires this at 18:30/20:30/22:30 Israel local time,
Mon-Fri -- the user's own stated real-world mapping of NYSE hours (9:30am-4:00pm ET)
to local time, first run 2h after actual open. That local-time trigger is still just
a best guess, not authoritative: Israel and US DST transitions don't land on the same
calendar dates, so the true ET-equivalent local time can drift by up to an hour for a
few weeks a year, and a plain daily Windows trigger has no NYSE-holiday awareness at
all. So this script remains the single source of truth for "is the market ACTUALLY
open right now", via a real NYSE trading calendar + America/New_York zoneinfo (same
approach as tv_data.py's _session_open_utc_ts, built for the identical DST-drift
failure mode) -- a safe, cheap no-op whenever the local-time guess is wrong or lands
on a holiday/weekend.

Enqueues a synthetic '/monitorall' message -- same code path as a user manually
typing it in Telegram, see process_queue.py's _handle_automonitor -- with a negative,
timestamp-derived update_id so it can never collide with a real Telegram update_id.
Then runs process_queue.py and WAITS for it (subprocess.run, not Popen): unlike
ack_listener.py's fire-and-forget spawn (safe there because ack_listener is itself a
long-running always-on process), this script's only job per invocation is to run
once and exit -- if it exited immediately after a fire-and-forget Popen, Task
Scheduler could tear down the child process with it.

Post-close mode (2026-07-15): `python trigger_auto_monitor.py post_close`, run by a
separate Task Scheduler job (TradingNewAutoMonitorClose, ~23:20 Israel time Mon-Fri,
~20min after the real 4pm ET close) fires a fourth /monitorall scan specifically to
capture the actual daily-close candle -- the three intraday runs above (18:30/20:30/
22:30 IL) are timed ~30min before the close and never see it. Uses
`_market_closed_recently()` instead of `_market_is_open_now()`: same NYSE-calendar/
zoneinfo approach, but checks now falls within `_POST_CLOSE_GRACE_MINUTES` after
today's close, not before it. Same "script gate is authoritative, not the local
trigger time" principle -- a safe no-op if Israel/US DST drift or a delayed
Scheduler run pushes the real invocation outside the grace window.

Late post-close mode (2026-08-08): `python trigger_auto_monitor.py post_close_late`.
Same synthetic `/monitorall` as post_close, but gated on `market_closed_today()` --
"today was a real session and it is over", with no 30-minute grace ceiling. This is
the run that now closes out the night: refresh_pending.py chains it once its rebuilds
have landed, so the last list the user reads is a scan of the ALREADY-refreshed
theses rather than of the pre-refresh numbers (user's request, 2026-08-08). The old
30-minute-grace post_close job survives only as a late fallback for the night the
refresh dies before it gets here -- it self-skips if a /monitorall already ran after
today's close, see persistence.monitorall_ran_since().

Strict-open mode (2026-07-31): `python trigger_auto_monitor.py strict_open`, run by
a separate Task Scheduler job (TradingNewAutoMonitorStrictOpen, ~17:00 Israel time
Mon-Fri, the current best-effort local-time mapping of 10:00 ET = open + 30min)
fires a fifth /monitorall scan -- a synthetic `/monitorall_strict` message, not
`/monitorall` -- to catch same-day triggers that fire right at the open, which the
first regular run (2h later, 18:30 IL) misses entirely. Uses
`open_plus_30_window_now()`: same real-NYSE-schedule gate pattern as the others,
checking now falls within that function's tolerance window of today's session open
+ 30min. `process_queue.py`'s `_handle_automonitor(strict=True)` applies an extra
5-min-candle confirmation gate for this run only (MONITOR_v2.md's strict-open
bullet) -- the open is naturally noisy, so this run needs stronger confirmation
before flagging a ticker than the calmer, later regular runs do.
"""

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import persistence
from market_hours import market_is_open_now as _market_is_open_now
from market_hours import market_closed_recently as _market_closed_recently
from market_hours import market_closed_today as _market_closed_today
from market_hours import todays_close_utc as _todays_close_utc
from market_hours import open_plus_30_window_now as _open_plus_30_window_now

BOT_DIR = Path(__file__).resolve().parent


def _fallback_should_stand_down() -> bool:
    """True when the scheduled late job must NOT run its scan (2026-08-08).

    Two reasons, and only the fallback job checks them -- the chained call at
    the end of refresh_pending.py is the authoritative run and always goes:

    1. The scan already happened after today's close. That is the normal night:
       the refresh finished and chained it hours ago. Running it again is ~20
       minutes of duplicate work and a second, identical report.
    2. Screener rebuilds are still sitting in the queue, which means the refresh
       is still working. Scanning now would read a half-rewritten list, and
       worse, it would make the refresh's own chained scan look like a duplicate
       and get itself skipped -- so the user's last list of the night would be
       the mid-refresh one. Standing down leaves the good run to happen.
    """
    close_utc = _todays_close_utc()
    if close_utc and persistence.monitorall_ran_since(close_utc.isoformat()):
        print("A /monitorall already ran after today's close -- fallback stands down.")
        return True
    still_queued = persistence.tickers_already_queued_for_screener()
    if still_queued:
        print(f"The nightly refresh is still rebuilding ({len(still_queued)} queued) "
              "-- fallback stands down, its own scan will follow.")
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", default="intraday",
                        choices=["intraday", "post_close", "post_close_late", "strict_open"])
    parser.add_argument("--fallback", action="store_true",
                        help="scheduled safety-net run: stand down if the chained scan "
                             "already happened, or if the nightly refresh is still working")
    args = parser.parse_args()

    if args.mode == "post_close":
        gate_ok, label = _market_closed_recently(), "post-close"
    elif args.mode == "post_close_late":
        gate_ok, label = _market_closed_today(), "post-close-late"
        if gate_ok and args.fallback and _fallback_should_stand_down():
            return
    elif args.mode == "strict_open":
        gate_ok, label = _open_plus_30_window_now(), "strict-open"
    else:
        gate_ok, label = _market_is_open_now(), "intraday"

    if not gate_ok:
        print(f"Not in the {label} window -- skipping automated /monitorall scan.")
        return

    message_text = "/monitorall_strict" if label == "strict-open" else "/monitorall"
    update_id = -int(datetime.now(timezone.utc).timestamp())
    ok = persistence.enqueue_message(
        update_id=update_id, from_id="scheduled", chat_id="scheduled",
        message_type="text", message_text=message_text,
        raw_update={"synthetic": True, "source": f"trigger_auto_monitor.py:{label}"},
    )
    if not ok:
        print(f"update_id {update_id} already exists -- skipping (should not happen).")
        return

    print(f"Enqueued synthetic {message_text} ({label}, update_id={update_id}), running process_queue.py...")
    result = subprocess.run(
        ["python", str(BOT_DIR / "process_queue.py")],
        cwd=str(BOT_DIR.parent), shell=True,
    )
    print(f"process_queue.py exited with code {result.returncode}.")


if __name__ == "__main__":
    main()
