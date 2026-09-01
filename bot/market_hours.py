"""Shared NYSE trading-calendar gating helpers (2026-07-16).

Extracted out of trigger_auto_monitor.py so trigger_position_status.py can
reuse the exact same "is the market ACTUALLY open/closed right now" logic
instead of re-deriving it -- both scripts are Task-Scheduler entry points
whose local-time trigger is just a best guess (Israel/US DST transitions
don't land on the same calendar dates, and a plain daily Windows trigger has
no NYSE-holiday awareness at all), so a real NYSE calendar + America/New_York
zoneinfo check must be the authoritative gate in both places.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

NY_TZ = ZoneInfo("America/New_York")
_NYSE = mcal.get_calendar("NYSE")
_POST_CLOSE_GRACE_MINUTES = 30


def _todays_session():
    """Today's NYSE schedule row, or None on a weekend/holiday."""
    now_ny = datetime.now(NY_TZ)
    schedule = _NYSE.schedule(start_date=now_ny.date(), end_date=now_ny.date())
    return None if schedule.empty else schedule.iloc[0]


def next_session_open_utc() -> datetime:
    """UTC datetime of the next NYSE session open -- today's if it hasn't
    happened yet, otherwise the next real trading day's. Added 2026-08-02 for
    refresh_pending.py, which must stop starting new (slow) rebuilds a couple of
    hours before the market opens, so the list the user reads pre-open is
    settled rather than half-rewritten. Looks 10 calendar days ahead, which
    covers any weekend plus the longest NYSE holiday stretch."""
    now_utc = datetime.now(timezone.utc)
    now_ny = datetime.now(NY_TZ)
    schedule = _NYSE.schedule(start_date=now_ny.date(),
                              end_date=(now_ny + timedelta(days=10)).date())
    for _, session in schedule.iterrows():
        open_utc = session["market_open"].to_pydatetime().astimezone(timezone.utc)
        if open_utc > now_utc:
            return open_utc
    raise RuntimeError("no NYSE session open found in the next 10 days")


def market_is_open_now() -> bool:
    session = _todays_session()
    if session is None:
        return False  # weekend or holiday
    market_open = session["market_open"].to_pydatetime()
    market_close = session["market_close"].to_pydatetime()
    now_utc = datetime.now(NY_TZ).astimezone(timezone.utc)
    return market_open <= now_utc <= market_close


def is_premarket_now() -> bool:
    """True if today is a real NYSE session and now is before that session's
    open -- used to flag a live /monitor quote as premarket (thin volume, no
    30m/2h bars yet) rather than silently treating it like a regular-session
    price."""
    session = _todays_session()
    if session is None:
        return False  # weekend or holiday
    market_open = session["market_open"].to_pydatetime()
    now_utc = datetime.now(NY_TZ).astimezone(timezone.utc)
    return now_utc < market_open


def market_closed_recently() -> bool:
    session = _todays_session()
    if session is None:
        return False  # weekend or holiday
    market_close = session["market_close"].to_pydatetime()
    now_utc = datetime.now(NY_TZ).astimezone(timezone.utc)
    grace_end = market_close + timedelta(minutes=_POST_CLOSE_GRACE_MINUTES)
    return market_close <= now_utc <= grace_end


def todays_close_utc() -> Optional[datetime]:
    """UTC datetime of today's NYSE close, or None on a weekend/holiday
    (2026-08-08). Used as the "since" edge for 'has the post-close scan already
    run tonight' -- derived from the real schedule row so an early-close day
    narrows the window instead of leaving a stale clock time behind."""
    session = _todays_session()
    if session is None:
        return None
    return session["market_close"].to_pydatetime().astimezone(timezone.utc)


def market_closed_today() -> bool:
    """Same "today was a real session and it is now over" check as
    market_closed_recently(), but with no upper bound on how long ago the close
    was (2026-08-08).

    Why a second gate: the post-close /monitorall no longer runs on its own
    clock trigger. It is chained to run after refresh_pending.py finishes, so
    the user's last message of the night is a scan of the ALREADY-refreshed
    pending list. That refresh takes 40-90 minutes, which is far outside
    market_closed_recently()'s 30-minute grace window -- gating the chained run
    on that would make it a permanent no-op.

    Still bounded in practice by the NY calendar date: _todays_session() returns
    None once the clock rolls past midnight New York time, so this cannot fire
    on the following morning's stale state.
    """
    session = _todays_session()
    if session is None:
        return False  # weekend or holiday
    market_close = session["market_close"].to_pydatetime()
    now_utc = datetime.now(NY_TZ).astimezone(timezone.utc)
    return now_utc >= market_close


def midday_window_now(half_width_minutes: int = 15) -> bool:
    """True if now falls within half_width_minutes of today's real session
    midpoint (market_open + (market_close-market_open)/2). Derived from the
    actual NYSE schedule row rather than a hardcoded ET time so early-close
    days (e.g. day before Thanksgiving) shift the midpoint automatically
    instead of firing at a stale clock time."""
    session = _todays_session()
    if session is None:
        return False  # weekend or holiday
    market_open = session["market_open"].to_pydatetime()
    market_close = session["market_close"].to_pydatetime()
    midpoint = market_open + (market_close - market_open) / 2
    now_utc = datetime.now(NY_TZ).astimezone(timezone.utc)
    half_width = timedelta(minutes=half_width_minutes)
    return (midpoint - half_width) <= now_utc <= (midpoint + half_width)


_STRICT_OPEN_OFFSET_MINUTES = 30


def open_plus_30_window_now(half_width_minutes: int = 15) -> bool:
    """True if now falls within half_width_minutes of today's session open +
    30 minutes -- same real-NYSE-schedule pattern as midday_window_now (not a
    hardcoded ET clock time) so early-close days shift the target
    automatically instead of firing at a stale clock time. Backs the
    'strict-open' /monitorall scan (2026-07-31): a same-day trigger right at
    the open is otherwise missed entirely by the normal ~2h-later cadence."""
    session = _todays_session()
    if session is None:
        return False  # weekend or holiday
    market_open = session["market_open"].to_pydatetime()
    target = market_open + timedelta(minutes=_STRICT_OPEN_OFFSET_MINUTES)
    now_utc = datetime.now(NY_TZ).astimezone(timezone.utc)
    half_width = timedelta(minutes=half_width_minutes)
    return (target - half_width) <= now_utc <= (target + half_width)
