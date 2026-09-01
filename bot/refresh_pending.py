"""Nightly refresh of waiting (pending) theses -- 2026-08-02.

The problem this fixes, in one line: a thesis's trigger, stop and targets are
written once at /screener time and then frozen forever, while the chart keeps
moving. Nothing ever rebuilt them. The live effect, all found in the real DB:

  * ~26 of 55 stored theses had a trigger saved as a sentence, not a number, so
    nothing mechanical could measure them at all -- not the rubric, not the
    shadow book, not the stale-trigger check.
  * A trigger that fired kept producing fresh-looking "🟢 trigger fired!"
    headlines for days (MSFT: fired 2026-07-16 at 389.03, still shouting on
    2026-07-30 with price at 451).
  * /pending's own age flag fires at 10 trading days and then does nothing at
    all -- no rebuild, no expiry, just a mark.

What this script does NOT do: judge anything. It runs a purely mechanical check
over every pending row, decides which ones are worth rebuilding, and enqueues a
real `/screener TICKER` run for each -- the same synthetic-message path
trigger_auto_monitor.py already uses, so each rebuild is the full, unmodified
screener analysis, one at a time, never a cut-down version.

Nothing is ever deleted automatically. Theses that look dead are REPORTED with
the exact `/drop` command to send, per the user's own instruction: he drops
them, not the script.

2026-08-03, three changes all driven by the same worry -- a nightly rewrite the
user cannot see, cannot judge, and cannot undo:
  * Age alone no longer triggers a rebuild ("if nothing is broke don't fix it").
    See the note above classify() for why nothing is lost by dropping it.
  * A rebuild can no longer move an idea off the waiting list by itself. A
    rebuilt thesis that comes back "No Trade" stays pending and is reported with
    its /drop line -- same posture this script already took for dead ideas.
    (The rule lives in persistence.save_thesis; a genuinely NEW No Trade still
    goes cold as rule 29 intends.)
  * Every overwrite is now archived first (persistence.thesis_history) and the
    before/after is printed in the nightly message, so a rewrite that made an
    idea worse is visible the next morning instead of invisible forever.

Ordering: rebuild candidates go closest-to-trigger first, so if the clock runs
out the ones nearest to actually mattering are the ones that got done.

2026-08-08, the user's own call: this run moved from 00:15 to 23:20 -- ahead of
the night's post-close /monitorall instead of behind it -- and now STARTS that
scan itself when it is done (see _run_post_close_scan). Before, the last full
list of the night was built from pre-refresh numbers and could show a buy level
this script was minutes away from moving. The cost is that the buy list lands
around midnight-01:00 rather than 23:20, and that classify() now reads a price
from the 22:30 scan (~30min pre-close) rather than the settled close -- it only
decides WHETHER to rebuild, though; the rebuild itself fetches fresh data.
classify() is unchanged by the reorder; see the note there for why a full-atr
threshold already keeps a just-fired entry safe from being rebuilt out from
under the scan.

2026-08-11, the user's call, about the OTHER half of "nothing is deleted
automatically": an idea whose price sits under its own stop was reported with
the same full /drop line every night until he acted, which is how a section
stops being read and a newly dead idea hides among the old ones. It is now told
loudly once, counted quietly after that, and after
persistence.DEAD_NIGHTS_BEFORE_COLD nights with no answer it moves to the cold
shelf by itself -- off the list he reads, still not rebuilt tonight, still not
deleted, and looked at again by the cold re-check in a few trading days, queued
last so it never takes the night's hours from an idea that can still be bought.
See _handle_dead.

Usage:
  python bot/refresh_pending.py            # the real nightly run
  python bot/refresh_pending.py --dry-run  # print the plan, enqueue nothing
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import persistence
import tv_lock
from market_hours import market_closed_today, next_session_open_utc, todays_close_utc
from telegram_send import send_text, escape_html

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = PROJECT_ROOT / "refresh_pending.log"

# Age alone is no longer a rebuild reason (2026-08-03, on the user's own call:
# "if nothing is broke don't fix it"). It used to be -- REBUILD_AFTER_TRADING_DAYS
# = 5 -- and it was the single biggest source of rewrites: an idea whose trigger
# was still a real number, still un-fired, and still sitting within 1 ATR of
# price got its whole plan replaced purely because a calendar counter passed 5.
#
# Why removing it loses nothing:
#   * Anything actually wrong with the levels is already caught by the three
#     remaining reasons below (unmeasurable trigger / fired-and-stale / drifted
#     away). If price has NOT moved a full ATR in five days, the stored trigger
#     still describes the same trade it described when it was written.
#   * The things age catches that price does not -- a decayed setup, a flipped
#     regime, earnings walking closer -- are re-checked LIVE on every monitor
#     scan against today's numbers (rule 27, the WGMI fix). That protection does
#     not require overwriting the stored plan, and never did.
#   * Each rebuild is a full ~25min screener run. Spending the night on ideas
#     that were fine meant the genuinely broken ones hit the pre-open cutoff and
#     never got rebuilt at all -- the age rule was crowding out the real work.
#
# `--all` still rebuilds everything on demand, for the one case that legitimately
# invalidates every stored grade at once: a change to the scoring rules.

# Price has drifted this far from the stored trigger, in ATRs, so the trigger no
# longer describes the trade being waited for. Same threshold /pending's own
# "moved" flag already uses, reused rather than invented separately.
MOVED_ATR_MULTIPLE = 1.0

# Stop starting NEW rebuilds this long before the opening bell. Each one is a
# full ~25min screener run; the point is that whatever the user reads pre-open
# is finished, not mid-rewrite.
STOP_BEFORE_OPEN_HOURS = 2

# How long to wait for another TradingView job (the shadow book, a /monitorall)
# to finish before giving up on draining tonight's rebuilds.
_LOCK_WAIT_SECONDS = 90 * 60


def _log(message: str) -> None:
    """Prints only. The LOG_FILE.open() that used to follow this print killed
    every scheduled run (found 2026-08-04): the .bat already held
    refresh_pending.log open via `>>`, a Windows cmd redirect doesn't share that
    handle, and this raised PermissionError on the FIRST log line of main() --
    right after printing the plan, before a single thesis was rebuilt. Two
    nights of "20 pending: 7 to rebuild" followed by nothing. bot/run_task.py
    now owns the log file and captures this stdout into it."""
    print(f"{datetime.now(timezone.utc).isoformat()}  {message}")


@dataclass
class Verdict:
    ticker: str
    action: str          # "rebuild" | "keep" | "looks_dead"
    reason: str          # short, stable token -- for the log and the message
    distance_atr: Optional[float]  # |price - trigger| / atr, None when unknowable


def _numeric(value) -> Optional[float]:
    """A stored trigger/stop is sometimes a descriptive sentence rather than a
    number (26 of 55 real rows). Never coerce or parse a number out of prose --
    an unmeasurable level is exactly the signal that the thesis needs rebuilding,
    so returning None here IS the useful answer."""
    if isinstance(value, (int, float)):
        return float(value)
    return None


def classify(row: dict) -> Verdict:
    """Pure decision over one /pending row -- no DB, no fetch, unit-testable.

    Every "rebuild" here means the stored plan is measurably WRONG, not merely
    old (see the age note above): its trigger cannot be measured at all, it
    already fired long ago, or price has walked a full ATR away from it. A thesis
    that is simply unchanged and still near its trigger is kept, however long it
    has been waiting.

    Order matters: the checks run cheapest-and-most-certain first, so a thesis
    that is both stale AND drifted reports the more fundamental reason."""
    ticker = row.get("ticker", "?")
    setup = row.get("primary_setup") or {}
    trigger = _numeric(setup.get("trigger"))
    stop = _numeric(setup.get("stop"))
    atr = _numeric(setup.get("atr_at_build"))
    price = _numeric(row.get("latest_price"))

    distance_atr = None
    if trigger is not None and price is not None and atr:
        distance_atr = abs(price - trigger) / atr

    if trigger is None:
        return Verdict(ticker, "rebuild", "no_numeric_trigger", None)

    # A price that has fallen through the thesis's own stop has invalidated it.
    # Reported, never auto-dropped -- and never rebuilt either, since rebuilding
    # a broken idea just manufactures a new reason to look at a losing chart.
    if stop is not None and price is not None and price < stop:
        return Verdict(ticker, "looks_dead", "price_below_stop", distance_atr)

    # No place to sell into = nothing measurable at all (2026-08-08, found on a
    # live run). BTCUSD and ONDS both confirmed their triggers and both came
    # back "can't be scored, no target saved" -- rule 27's live re-grade needs a
    # target, so these produce no grade, no reward:risk and no order, night
    # after night, while still sitting on the waiting list looking like live
    # ideas. Distance-from-trigger, the only thing this script used to look at,
    # can never catch that. Capped by the same counter as the cold list -- see
    # the queueing loop in main() -- so a ticker that never finds a target is
    # not re-screened forever.
    targets = setup.get("targets") or []
    first_target = targets[0] if targets and isinstance(targets[0], dict) else None
    if first_target is None or _numeric(first_target.get("price")) is None:
        return Verdict(ticker, "rebuild", "no_target_to_sell_into", distance_atr)

    age = persistence.get_trigger_fired_age(ticker)
    if age and age.get("stale"):
        return Verdict(ticker, "rebuild", "trigger_fired_and_went_stale", distance_atr)

    # Distance is deliberately unsigned, and stayed unsigned when this run moved
    # ahead of the night's /monitorall (2026-08-08). The reorder raised the
    # question "won't this rebuild an entry that just fired, before the scan can
    # report it?" -- it can't: MOVED_ATR_MULTIPLE is a FULL atr, and an entry
    # that just crossed its trigger is a fraction of one past it, so it is kept.
    # What does get rebuilt from above is an idea that ran a whole atr past the
    # entry, and that one has to be: the reward is gone and the stop is now far
    # below, which is a different trade needing a new plan, not the stored one.
    if distance_atr is not None and distance_atr >= MOVED_ATR_MULTIPLE:
        return Verdict(ticker, "rebuild", "price_moved_away_from_trigger", distance_atr)

    return Verdict(ticker, "keep", "still_current", distance_atr)


def plan(rows: list[dict]) -> tuple[list[Verdict], list[Verdict], list[Verdict]]:
    """(rebuild, keep, looks_dead) -- rebuild sorted closest-to-trigger first so
    a clock cutoff drops the least relevant ones. Unknown distance sorts last:
    it can't be shown to be near, so it doesn't get to jump the queue."""
    verdicts = [classify(r) for r in rows]
    rebuild = [v for v in verdicts if v.action == "rebuild"]
    rebuild.sort(key=lambda v: (v.distance_atr is None, v.distance_atr or 0.0))
    return (rebuild,
            [v for v in verdicts if v.action == "keep"],
            [v for v in verdicts if v.action == "looks_dead"])


def session_skip_reason() -> Optional[str]:
    """Why tonight's refresh must not run, or None to go ahead (2026-08-08).

    The real NYSE calendar, not the Task Scheduler's Mon-Fri guess -- Mon-Fri
    still fires on Thanksgiving, Good Friday and every other market holiday,
    days that produced no new prices at all, where a rebuild would rewrite
    stored theses off the same closes they already hold. Same
    authoritative-gate principle as trigger_auto_monitor.py: the local trigger
    time is a guess, the calendar is the truth.
    """
    close_utc = todays_close_utc()
    if close_utc is None:
        return "today was not an NYSE trading day -- nothing new to refresh, skipping"
    if not market_closed_today():
        # Not a no-op worth passing over quietly: it means the trigger time has
        # drifted ahead of the real close (Israel and US DST transitions land on
        # different dates), so tonight's refresh is being lost, not skipped.
        return (f"WARNING: today's session has NOT closed yet (close was "
                f"{close_utc.isoformat()}) -- skipping rather than rebuilding off a "
                "live candle. Check the scheduled trigger time.")
    return None


def _cutoff_utc() -> datetime:
    return next_session_open_utc() - timedelta(hours=STOP_BEFORE_OPEN_HOURS)


def _enqueue_rebuild(ticker: str, index: int) -> bool:
    """One synthetic '/screener TICKER' message, exactly like
    _handle_screener_batch's fan-out. Negative, timestamp-derived update_id so
    it can never collide with a real Telegram update (always positive)."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return persistence.enqueue_message(
        update_id=-(now_ms + index), from_id="refresh_pending", chat_id="refresh_pending",
        message_type="text", message_text=f"/screener {ticker}",
        raw_update={"synthetic": True, "source": "refresh_pending"},
    )


def _drain_queue() -> None:
    """Run process_queue.py once and WAIT for it -- same subprocess.run (not
    Popen) reasoning as trigger_auto_monitor.py: this script's whole job is one
    invocation, so a fire-and-forget child could be torn down with it when Task
    Scheduler reaps the parent.

    The wait first: process_queue.py gives up IMMEDIATELY if it can't take the
    TradingView lock (correct for its own use -- a second instance there means a
    duplicate reply). So launching it while the shadow book still holds the
    bridge would leave every rebuild sitting unprocessed in the queue with
    nothing reporting that it happened. Waiting for the lock to clear first
    turns that into a normal delay. Losing a race here is harmless anyway: the
    messages stay queued and the next run drains them."""
    waited = 0
    while tv_lock.is_locked() and waited < _LOCK_WAIT_SECONDS:
        time.sleep(30)
        waited += 30
    if tv_lock.is_locked():
        _log(f"TradingView still busy after {waited // 60}min -- rebuilds stay queued for the next run")
        return
    subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "process_queue.py")],
                   cwd=str(PROJECT_ROOT), check=False)


def _run_post_close_scan() -> None:
    """Fire the night's post-close /monitorall, now that the rebuilds have
    landed (2026-08-08, user's request).

    The order used to be scan-then-refresh, which meant the last full list of
    the night was built from pre-refresh numbers: it could still be showing a
    buy level the refresh was about to move. Running the scan last makes that
    list the actual orders for the next session.

    Chained here rather than given its own Task Scheduler time because this run
    has no fixed length -- 40 to 90 minutes depending on how many theses need a
    full screener rebuild. A clock trigger would either fire while the refresh
    still held the TradingView lock (process_queue.py gives up immediately when
    it can't take it, so the scan would silently not happen) or sit idle for an
    hour on a short night.

    Never fatal: the refresh's own work is already saved and reported by the
    time we get here, so a scan that fails must not turn a successful refresh
    into a failed task."""
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "trigger_auto_monitor.py"),
             "post_close_late"],
            cwd=str(PROJECT_ROOT), check=False)
        _log(f"post-close scan finished, exit={result.returncode}")
    except Exception as exc:  # noqa: BLE001 -- see docstring
        _log(f"WARNING: post-close scan could not be started: {exc!r}")


def _handle_dead(dead: list[Verdict], live: list[Verdict]
                  ) -> tuple[list[Verdict], list[Verdict], list[Verdict]]:
    """Book-keeping for ideas whose price sits under their own stop, and the
    split that decides how loudly each one is reported (2026-08-11).

    The problem this fixes: a dead idea was reported with the same full /drop
    line every single night until the user acted, and a message that repeats
    verbatim is a message nobody reads -- so a newly dead idea hid among the old
    ones. Now the first night is loud, the nights after are a one-line count,
    and after DEAD_NIGHTS_BEFORE_COLD nights with no answer the row moves to the
    shelf (cold) on its own.

    Shelving is not deleting and not rebuilding: the row keeps everything it
    has, leaves the list the user reads, and the cold list looks at it again in
    a few trading days -- queued last, so it can never take the night's hours
    from an idea that can still be bought. Returns (first night, still dead,
    shelved tonight)."""
    for v in live:
        persistence.clear_dead_nights(v.ticker)

    first, still, shelved = [], [], []
    for v in dead:
        nights = persistence.bump_dead_night(v.ticker)
        if nights >= persistence.DEAD_NIGHTS_BEFORE_COLD:
            persistence.set_cold(v.ticker)
            shelved.append(v)
        elif nights <= 1:
            first.append(v)
        else:
            still.append(v)
    if shelved:
        _log(f"{len(shelved)} idea(s) under their own stop for "
             f"{persistence.DEAD_NIGHTS_BEFORE_COLD} nights -- moved to the cold shelf")
    return first, still, shelved


def _fmt_price(value: Optional[float]) -> str:
    """A price for the message, or '?' when the stored value was prose rather
    than a number -- never a fabricated figure, and never a bare 'None'."""
    return f"{value:.2f}" if isinstance(value, (int, float)) else "?"


def _change_lines(changes: list[dict]) -> list[str]:
    """One line per rewritten idea: what it was, what it is now. Only the fields
    that ACTUALLY changed are named, so an unchanged grade doesn't add noise to
    a line that is really about a moved trigger.

    This exists because a rewrite the user cannot see is a rewrite the user
    cannot judge -- the whole worry that prompted the 2026-08-03 changes."""
    lines = []
    for change in changes:
        before, after = change["before"], change["after"]
        parts = []
        if before.get("grade") != after.get("grade"):
            parts.append(f"ציון {escape_html(before.get('grade') or '?')} ← "
                          f"<b>{escape_html(after.get('grade') or '?')}</b>")
        if before.get("trigger") != after.get("trigger"):
            parts.append(f"קנייה {_fmt_price(before.get('trigger'))} ← "
                          f"<b>{_fmt_price(after.get('trigger'))}</b>")
        if before.get("stop") != after.get("stop"):
            parts.append(f"סטופ {_fmt_price(before.get('stop'))} ← "
                          f"<b>{_fmt_price(after.get('stop'))}</b>")
        if before.get("decision") != after.get("decision"):
            parts.append(f"החלטה {escape_html(before.get('decision') or '?')} ← "
                          f"<b>{escape_html(after.get('decision') or '?')}</b>")
        detail = " | ".join(parts) if parts else "בלי שינוי במספרים"
        lines.append(f"• <b>{escape_html(change['ticker'])}</b>: {detail}")
    return lines


def _report(rebuilt: list[str], skipped: list[Verdict], dead: list[Verdict], kept: int,
            changes: Optional[list[dict]] = None,
            no_target_exhausted: Optional[list[Verdict]] = None,
            dead_still: Optional[list[Verdict]] = None,
            shelved: Optional[list[Verdict]] = None) -> None:
    """One Telegram message, in the same plain-language register as every other
    user-facing text in this system (SCREENER_v3.md section ח's own rule: the
    reader is a beginner, not a professional). No rule numbers, no field names."""
    changes = changes or []
    lines = ["🌙 <b>רענון לילי של הרעיונות הממתינים</b>", ""]
    if rebuilt:
        lines.append(f"🔄 נבנו מחדש עם מחירים עדכניים (<b>{len(rebuilt)}</b>): "
                      + ", ".join(escape_html(t) for t in rebuilt))
    if changes:
        lines.append("")
        lines.append("<b>מה השתנה בפועל (לפני ← אחרי):</b>")
        lines.extend(_change_lines(changes))
        lines.append("")
        lines.append("הגרסה הישנה של כל רעיון נשמרה — שום דבר לא נמחק.")
        # An idea the rebuild turned into a No Trade is no longer moved off the
        # list automatically (see save_thesis) -- so it has to be SAID, or it
        # would just sit there quietly with a verdict nobody noticed.
        no_trade = [c for c in changes
                    if (c["after"].get("decision") == "No Trade"
                        and c["before"].get("decision") != "No Trade")]
        if no_trade:
            lines.append("")
            lines.append(f"⚠️ <b>אחרי הבנייה מחדש כבר לא נראים כמו עסקה ({len(no_trade)})</b> — "
                          "השארתי אותם ברשימה, ההחלטה שלך:")
            for c in no_trade:
                lines.append(f"<code>/drop {escape_html(c['ticker'])} כבר לא נראה טוב</code>")
    if kept:
        lines.append(f"✅ נשארו כמו שהם, עדיין מעודכנים: <b>{kept}</b>")
    if skipped:
        lines.append("")
        lines.append(f"⏳ <b>לא הספקתי לבנות מחדש ({len(skipped)}):</b> "
                      + ", ".join(escape_html(v.ticker) for v in skipped))
        lines.append("הם עדיין ברשימה עם המספרים הישנים. אם הם כבר לא מעניינים אותך, שלח:")
        for v in skipped:
            lines.append(f"<code>/drop {escape_html(v.ticker)} לא רלוונטי</code>")
    # Three different volumes for the same fact, by how long it has been true.
    # The first night is the news; nights two to four are a reminder; the fifth
    # is the shelf. Repeating the loud version nightly is what made this section
    # unreadable in the first place.
    if dead:
        lines.append("")
        lines.append(f"🔴 <b>נראים גמורים ({len(dead)})</b> — המחיר ירד מתחת לרמה שבה הרעיון מתבטל:")
        for v in dead:
            lines.append(f"<code>/drop {escape_html(v.ticker)} ירד מתחת לסטופ</code>")
        lines.append("לא מחקתי כלום בעצמי — ההחלטה שלך.")
    if dead_still:
        lines.append("")
        lines.append(f"🔴 <b>{len(dead_still)} עדיין נראים גמורים</b>: "
                      + ", ".join(escape_html(v.ticker) for v in dead_still))
    if shelved:
        lines.append("")
        lines.append(f"📦 <b>הועברו למדף ({len(shelved)})</b>: "
                      + ", ".join(escape_html(v.ticker) for v in shelved))
        lines.append(f"הם היו מתחת לסטופ {persistence.DEAD_NIGHTS_BEFORE_COLD} לילות ברצף, אז הורדתי "
                      "אותם מרשימת ההמתנה. לא נמחק כלום — אבדוק אותם שוב בעוד כמה ימי מסחר, "
                      "אחרונים בתור, כדי שלא ייקחו זמן מרעיונות חיים. אם הם חזרו להיות עסקה, "
                      "הם יחזרו לרשימה לבד.")
    for v in (no_target_exhausted or []):
        lines.append("")
        lines.append(f"🎯 <b>{escape_html(v.ticker)} — אין לאן למכור</b>: בתוכנית השמורה אין יעד, "
                      f"ולכן אי אפשר לחשב לה ציון או יחס סיכון/סיכוי. ביקשתי בנייה מחדש "
                      f"{persistence.COLD_MAX_RECHECKS} פעמים ועדיין אין יעד — לא אבקש שוב:")
        lines.append(f"<code>/drop {escape_html(v.ticker)} אין יעד למכור לתוכו</code>")

    exhausted = persistence.get_exhausted_cold()
    if exhausted:
        lines.append("")
        lines.append(f"🧊 <b>נבדקו שוב {persistence.COLD_MAX_RECHECKS} פעמים ועדיין אין להם לאן למכור "
                      f"({len(exhausted)})</b> — לא אבדוק אותם יותר לבד:")
        for row in exhausted:
            lines.append(f"<code>/drop {escape_html(row['ticker'])} אין יעד גם אחרי בדיקות חוזרות</code>")
    if not (rebuilt or skipped or dead or dead_still or shelved or no_target_exhausted):
        lines.append("אין מה לעדכן — כל הרעיונות הממתינים עדיין מעודכנים.")
    resp = send_text("\n".join(lines))
    if not resp.get("ok"):
        _log(f"WARNING: Telegram summary failed to send: {resp}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and send nothing, enqueue nothing")
    parser.add_argument("--force", action="store_true",
                        help="run even when today was not a real trading session -- "
                             "for running this by hand, off-hours or on a weekend")
    parser.add_argument("--all", action="store_true",
                        help="rebuild EVERY pending thesis, not just the stale ones -- "
                             "for a one-off after a scoring change, when every stored "
                             "grade was produced by rules that no longer apply")
    args = parser.parse_args()

    # Real NYSE calendar, not the Task Scheduler's Mon-Fri guess (2026-08-08,
    # user's request: "only at the end of days the market is open"). Mon-Fri
    # still fires on Thanksgiving, Good Friday and the rest -- days with no new
    # prices at all, where a rebuild would rewrite stored theses off the same
    # closes they already hold. Same authoritative-gate principle as
    # trigger_auto_monitor.py: the local trigger time is a guess, the calendar
    # is the truth. --dry-run and --force are deliberately exempt.
    if not (args.dry_run or args.force):
        reason = session_skip_reason()
        if reason:
            _log(reason)
            return

    # Taken BEFORE any rebuild is enqueued: everything archived from here on is
    # this run's doing, which is what the before/after message reports on.
    run_started_at = datetime.now(timezone.utc).isoformat()

    rows = persistence.get_pending_report_rows()
    if not rows:
        _log("no pending theses -- nothing to refresh")
        return

    rebuild, keep, dead = plan(rows)
    if args.all:
        # A scoring change makes every stored grade stale by definition, even on
        # a thesis whose prices are still perfectly current -- the letter was
        # produced by rules that no longer exist. Dead ideas stay out: rebuilding
        # one just manufactures a fresh reason to look at a broken chart.
        rebuild = rebuild + keep
        rebuild.sort(key=lambda v: (v.distance_atr is None, v.distance_atr or 0.0))
        keep = []
        _log(f"--all: rebuilding every live pending thesis ({len(rebuild)}), "
             f"{len(dead)} dead ones still excluded")
    _log(f"{len(rows)} pending: {len(rebuild)} to rebuild, {len(keep)} current, {len(dead)} look dead")
    for v in rebuild + keep + dead:
        _log(f"  {v.ticker:6s} {v.action:10s} {v.reason}"
             + (f" ({v.distance_atr:.2f} ATR from trigger)" if v.distance_atr is not None else ""))

    if args.dry_run:
        _log("dry run -- nothing enqueued, nothing sent")
        return

    dead_new, dead_still, shelved = _handle_dead(dead, live=[v for v in rebuild + keep])

    # Cold list, rule 29 (2026-08-03). A "No Trade" idea left the active list
    # because it had nowhere to sell -- which is usually about where price is
    # standing, not about the company, so it is re-screened every few trading
    # days rather than forgotten. Appended AFTER the stale rebuilds so live
    # ideas always get the clock first if the night runs short.
    cold_due = persistence.get_cold_recheck_candidates()
    if cold_due:
        _log(f"cold list: {len(cold_due)} due for a re-check "
             f"(every {persistence.COLD_RECHECK_TRADING_DAYS} trading days, "
             f"max {persistence.COLD_MAX_RECHECKS} tries)")
        rebuild = rebuild + [Verdict(row["ticker"], "rebuild", "cold_recheck", None)
                              for row in cold_due]

    # A ticker already waiting in the queue from an earlier run (a long batch
    # still draining, or a one-off full rescan overlapping tonight's scheduled
    # job) must never be queued a second time -- that's two 25-minute runs and
    # two contradictory reports for one idea.
    already_queued = persistence.tickers_already_queued_for_screener()
    if already_queued:
        before = len(rebuild)
        rebuild = [v for v in rebuild if v.ticker.upper() not in already_queued]
        _log(f"{before - len(rebuild)} ticker(s) already queued from an earlier run -- skipping them")

    # A thesis with nowhere to sell into is capped exactly like a cold one
    # (rule 29's own reasoning, same counter): each attempt is a full ~25 minute
    # screener run, and a chart that genuinely has no level above it will keep
    # coming back without a target however many times it is asked. After the
    # cap it is reported with a /drop line and left alone, never auto-deleted.
    no_target_exhausted = []
    if any(v.reason == "no_target_to_sell_into" for v in rebuild):
        still_worth_asking = []
        for v in rebuild:
            if v.reason != "no_target_to_sell_into":
                still_worth_asking.append(v)
                continue
            tries = (persistence.get_thesis(v.ticker) or {}).get("cold_rechecks") or 0
            if tries >= persistence.COLD_MAX_RECHECKS:
                no_target_exhausted.append(v)
            else:
                still_worth_asking.append(v)
        if no_target_exhausted:
            _log(f"{len(no_target_exhausted)} ticker(s) asked {persistence.COLD_MAX_RECHECKS} times "
                 "and still have no target -- not rebuilding again")
        rebuild = still_worth_asking

    cutoff = _cutoff_utc()
    rebuilt, skipped = [], []
    for i, verdict in enumerate(rebuild):
        if datetime.now(timezone.utc) >= cutoff:
            # Everything from here on is left untouched and named in the message
            # -- silently dropping it would read as "all done" when it wasn't.
            skipped = rebuild[i:]
            _log(f"cutoff reached ({cutoff.isoformat()}) -- {len(skipped)} not rebuilt")
            break
        if _enqueue_rebuild(verdict.ticker, i):
            rebuilt.append(verdict.ticker)
            if verdict.reason in ("cold_recheck", "no_target_to_sell_into"):
                # Counted at QUEUE time, not on completion: a run that dies
                # partway must still consume its attempt, or a ticker that
                # reliably fails to analyse would be retried forever.
                persistence.bump_cold_recheck(verdict.ticker)
        else:
            _log(f"enqueue collision for {verdict.ticker} -- skipped (should not happen)")
            skipped.append(verdict)

    # Drain whenever there IS screener work waiting, not only when this run put
    # it there. Found immediately in practice: a previous run enqueued 22
    # rebuilds and then died before draining, and the next run correctly skipped
    # re-enqueueing them (already_queued above) -- which, with a `if rebuilt`
    # condition here, also meant it never drained them either. The work would
    # have sat in the queue until some unrelated Telegram message happened to
    # trigger process_queue.py.
    if rebuilt or persistence.tickers_already_queued_for_screener():
        _drain_queue()

    # Read AFTER the drain: the rebuilds only actually land while process_queue
    # runs, so asking any earlier would report an empty diff every time.
    # Unfiltered by ticker on purpose -- a rebuild queued by an earlier run and
    # drained by this one is still a change the user saw happen tonight.
    changes = persistence.get_thesis_changes_since(run_started_at)
    for c in changes:
        _log(f"  changed {c['ticker']}: {c['before']} -> {c['after']}")

    _report(rebuilt, skipped, dead_new, kept=len(keep), changes=changes,
            no_target_exhausted=no_target_exhausted,
            dead_still=dead_still, shelved=shelved)
    _run_post_close_scan()
    _log("done")


if __name__ == "__main__":
    main()
