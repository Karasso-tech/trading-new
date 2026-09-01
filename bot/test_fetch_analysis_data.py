"""Unit tests for fetch_analysis_data.py's starter-staleness folding
(_with_starter_staleness, 2026-07-22).

Context: /playbook and /positions need to tell the user whether a
entry_type='starter' position has sat unconfirmed for a while, without
asking the claude -p report-writing session to do date arithmetic itself
(same principle as /pending's days_pending, computed server-side with the
real NYSE trading-day calendar). This is the one Category A piece of the
starter-confirmation feature -- whether it's actually confirmed for a full
add is a Category B judgment left to STRATEGY_v3.md (see that file's
"אישור Starter" rule), not computed here.
"""

from datetime import datetime, timedelta, timezone

import fetch_analysis_data as fad


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=n)).isoformat()


class TestWithStarterStaleness:
    def test_none_open_position_passes_through_unchanged(self):
        assert fad._with_starter_staleness(None) is None

    def test_full_position_is_left_untouched(self):
        op = {"entry_type": "full", "entry_date": _days_ago(30)}
        result = fad._with_starter_staleness(op)
        assert "days_since_starter" not in result
        assert "starter_stale" not in result

    def test_starter_filled_today_is_not_yet_stale(self):
        # Deliberately NOT asserting an exact count: count_trading_days is
        # inclusive of both ends, so "today" is 1 on a weekday and 0 on a
        # weekend -- hardcoding 1 made this whole suite fail every Saturday and
        # Sunday (found real 2026-08-02). Same class of bug as the hardcoded
        # fixture dates fixed in the 2026-07-30 checkup: a test that depends on
        # which day it happens to run is a broken test, not a real finding.
        op = {"entry_type": "starter", "entry_date": _days_ago(0)}
        result = fad._with_starter_staleness(op)
        assert result["days_since_starter"] <= 1
        assert result["starter_stale"] is False

    def test_starter_filled_a_month_ago_is_stale(self):
        op = {"entry_type": "starter", "entry_date": _days_ago(30)}
        result = fad._with_starter_staleness(op)
        assert result["days_since_starter"] >= fad.STARTER_STALE_TRADING_DAYS
        assert result["starter_stale"] is True


def _epoch(date_str: str) -> float:
    return datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc).timestamp()


class TestClassifyTouch:
    def test_body_cut_when_body_straddles_level(self):
        bar = {"time": _epoch("2026-01-01"), "open": 10, "high": 13, "low": 9, "close": 12}
        assert fad._classify_touch(bar, level=11, tol=0.5) == "body_cut"

    def test_wick_cut_when_only_high_low_range_crosses_level(self):
        bar = {"time": _epoch("2026-01-01"), "open": 10, "high": 12, "low": 9, "close": 10.5}
        assert fad._classify_touch(bar, level=11, tol=0.5) == "wick_cut"

    def test_touch_when_high_lands_within_tolerance_without_crossing(self):
        bar = {"time": _epoch("2026-01-01"), "open": 10, "high": 10.9, "low": 9.5, "close": 10.2}
        assert fad._classify_touch(bar, level=11, tol=0.15) == "touch"

    def test_none_when_nothing_near_the_level(self):
        bar = {"time": _epoch("2026-01-01"), "open": 10, "high": 10.5, "low": 9.8, "close": 10.2}
        assert fad._classify_touch(bar, level=20, tol=1) is None


class TestTouchesSinceFormation:
    def test_counts_only_bars_after_formation_date_by_kind(self):
        wall = {"top": 100.0, "bottom": 98.0, "touches": [{"date": "2026-01-01", "price": 100.0}]}
        atr = 2.0  # tol = max(100*0.03, 0.5*2) = 3.0
        bars = [
            # before formation -- would be a body_cut if counted, must be excluded
            {"time": _epoch("2025-12-31"), "open": 95, "high": 105, "low": 90, "close": 105},
            # after formation: body_cut
            {"time": _epoch("2026-01-02"), "open": 99, "high": 102, "low": 98, "close": 101},
            # after formation: wick_cut (level inside high/low, body doesn't cross)
            {"time": _epoch("2026-01-03"), "open": 98.5, "high": 101, "low": 98, "close": 99},
            # after formation: touch (high just under level, within tol, no crossing)
            {"time": _epoch("2026-01-04"), "open": 95, "high": 99.9, "low": 94, "close": 96},
            # after formation: nothing near the level
            {"time": _epoch("2026-01-05"), "open": 45, "high": 50, "low": 40, "close": 48},
        ]
        result = fad._touches_since_formation(bars, wall, atr)
        assert result == {"body_cut": 1, "wick_cut": 1, "touch": 1}

    def test_empty_touches_returns_zero_counts(self):
        wall = {"top": 100.0, "bottom": 98.0, "touches": []}
        result = fad._touches_since_formation([], wall, atr=2.0)
        assert result == {"body_cut": 0, "wick_cut": 0, "touch": 0}

    def test_formation_date_uses_latest_touch_not_list_order(self):
        # _swing_highs sorts touches by PRICE, not date -- so the highest-priced
        # touch (list-last) can easily be an EARLIER date than a lower-priced one
        # earlier in the list. Formation date must be the true latest date (2026-02-01
        # here), not touches[-1]'s date (2026-01-01), or a bar on 2026-01-15 would be
        # wrongly excluded as "before formation" when it's actually after.
        wall = {
            "top": 100.0, "bottom": 98.0,
            "touches": [
                {"date": "2026-02-01", "price": 98.0},   # lower price, but LATEST date
                {"date": "2026-01-01", "price": 100.0},  # highest price, list-last
            ],
        }
        atr = 2.0
        bars = [
            {"time": _epoch("2026-01-15"), "open": 99, "high": 102, "low": 98, "close": 101},
        ]
        result = fad._touches_since_formation(bars, wall, atr)
        assert result == {"body_cut": 0, "wick_cut": 0, "touch": 0}, \
            "bar predates the true (latest) formation date and must be excluded"
