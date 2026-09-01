"""Tests for setup_classifier.py.

These pin BEHAVIOUR, not the exact threshold numbers -- the thresholds are an
admitted first draft (see the module docstring) and are expected to move once
the shadow book is full enough to check them. What must not move is the shape:
a reclaim must never come back as a breakout, a trigger must never sit just
under a wall, and an unrecognisable chart must come back as "none" rather than
as a guess.
"""

import pytest

import setup_classifier as sc

ATR = 2.0


def bars(prices, gap_at=None, gap_size=0.0):
    """Daily bars from a list of closes.

    Each bar spans half an ATR either side of its body, so consecutive bars
    OVERLAP the way real ones do. That matters: a gap is defined as today's low
    above yesterday's high, and the first version of this helper drew bars only
    0.4 wide against an ATR of 2.0 -- so every ordinary day looked like a gap
    and half these tests failed for a reason that had nothing to do with the
    code under test. `gap_at` is the only way a gap appears here."""
    out = []
    half = ATR * 0.5
    for i, p in enumerate(prices):
        open_ = p
        if gap_at is not None and i == gap_at:
            open_ = prices[i - 1] + gap_size
        out.append({"date": "2026-06-%02d" % (i + 1), "open": open_,
                    "high": max(p, open_) + half, "low": min(p, open_) - half,
                    "close": p})
    return out


def wall(top, is_wall=True, touches=3):
    return {"is_wall": is_wall, "top": top, "bottom": top - 0.5,
            "touches": [{"date": "2026-05-0%d" % (i + 1), "price": top}
                        for i in range(touches)]}


class TestBreakout:
    """Both series here sit clear of their moving averages for the whole window
    on purpose. The first draft used averages the price had recently been under,
    and the classifier correctly called it a Reclaim -- the data was genuinely
    both things at once, which made the test say nothing about breakouts."""

    def test_a_wall_overhead_reads_as_a_breakout(self):
        call = sc.classify(bars=bars([100, 101, 102, 103, 104, 105]), atr14=ATR,
                            sma20=98.0, sma50=95.0, wall_chains=[wall(108.0)],
                            swing_lows=[])
        assert call.setup_type == sc.BREAKOUT
        # Rule 11: the trigger is a close above the wall's TOP.
        assert call.trigger == 108.0

    def test_an_unchained_swing_high_is_a_weaker_call(self):
        call = sc.classify(bars=bars([100, 101, 102, 103, 104, 105]), atr14=ATR,
                            sma20=98.0, sma50=95.0,
                            wall_chains=[wall(108.0, is_wall=False, touches=1)],
                            swing_lows=[])
        assert call.setup_type == sc.BREAKOUT
        assert call.confidence == "weak"


class TestReversalsWinOverBreakout:
    """A stock that just reclaimed a level or gapped is ALSO near a high.
    Calling that a plain Breakout throws away what makes it what it is -- and
    rule 15 keys the relative-strength window off exactly this distinction."""

    def test_a_reclaim_is_not_called_a_breakout(self):
        # Traded under the SMA50 and closed back above it.
        call = sc.classify(bars=bars([104, 102, 101, 100.5, 102, 104.5]), atr14=ATR,
                            sma20=102.0, sma50=103.0, wall_chains=[wall(120.0)],
                            swing_lows=[])
        assert call.setup_type == sc.RECLAIM

    def test_a_failed_breakdown_outranks_a_reclaim(self):
        # Every failed breakdown is technically a reclaim of something; the
        # more specific label is the useful one.
        call = sc.classify(bars=bars([100, 98.5, 96, 95, 97, 99]), atr14=ATR,
                            sma20=100.0, sma50=100.0, wall_chains=[wall(130.0)],
                            swing_lows=[{"date": "2026-05-20", "price": 96.0}])
        assert call.setup_type == sc.FAILED_BREAKDOWN

    def test_a_fresh_gap_that_held_is_gap_and_hold(self):
        prices = [100, 101, 105, 106, 107, 108]
        call = sc.classify(bars=bars(prices, gap_at=2, gap_size=4.0), atr14=ATR,
                            sma20=104.0, sma50=103.0, wall_chains=[wall(130.0)],
                            swing_lows=[])
        assert call.setup_type == sc.GAP_AND_HOLD

    def test_a_stale_gap_is_not_the_story_any_more(self):
        # Found on this module's first real run: a steady uptrend that had
        # gapped eight sessions back came out as Gap-and-Hold when the real
        # setup was the wall overhead.
        prices = [100, 104, 105, 106, 107, 108, 109, 110, 111, 112]
        call = sc.classify(bars=bars(prices, gap_at=1, gap_size=3.0), atr14=ATR,
                            sma20=108.0, sma50=105.0, wall_chains=[wall(120.0)],
                            swing_lows=[])
        assert call.setup_type != sc.GAP_AND_HOLD

    def test_a_gap_that_filled_is_not_a_hold(self):
        prices = [100, 105, 104, 101, 99, 98]
        call = sc.classify(bars=bars(prices, gap_at=1, gap_size=5.0), atr14=ATR,
                            sma20=103.0, sma50=103.0, wall_chains=[], swing_lows=[])
        assert call.setup_type != sc.GAP_AND_HOLD


class TestTriggerIsNeverUnderAWall:
    """Rule 11's reasoning is not specific to a breakout thesis: an entry half
    an ATR below a three-touch wall is an entry into resistance whatever the
    setup is called, and the stop sits underneath it either way."""

    def test_a_trigger_just_below_a_wall_is_raised_to_its_top(self):
        # A reclaim whose recent high is 105.2 with a wall top at 106.0 --
        # 0.4x ATR overhead.
        call = sc.classify(bars=bars([104, 102, 101, 100.5, 102, 104.5]), atr14=ATR,
                            sma20=102.0, sma50=103.0, wall_chains=[wall(106.0)],
                            swing_lows=[])
        assert call.setup_type == sc.RECLAIM
        assert call.trigger == 106.0
        assert any("raised" in e for e in call.evidence)

    def test_a_wall_with_real_room_above_is_left_alone(self):
        call = sc.classify(bars=bars([104, 102, 101, 100.5, 102, 104.5]), atr14=ATR,
                            sma20=102.0, sma50=103.0, wall_chains=[wall(130.0)],
                            swing_lows=[])
        assert call.trigger < 130.0


class TestPullbackIsLast:
    def test_an_uptrend_that_paused_reads_as_a_pullback(self):
        call = sc.classify(bars=bars([110, 111, 112, 110, 109, 108.5]), atr14=ATR,
                            sma20=108.0, sma50=105.0, wall_chains=[], swing_lows=[])
        assert call.setup_type == sc.PULLBACK

    def test_a_deep_fall_is_not_a_pullback(self):
        call = sc.classify(bars=bars([120, 118, 116, 114, 112, 110]), atr14=ATR,
                            sma20=110.0, sma50=105.0, wall_chains=[], swing_lows=[])
        assert call.setup_type != sc.PULLBACK


class TestNoMatch:
    def test_an_unrecognisable_chart_says_so_rather_than_guessing(self):
        call = sc.classify(bars=bars([100, 100, 100, 100, 100, 100]), atr14=ATR,
                            sma20=None, sma50=None, wall_chains=[], swing_lows=[])
        assert call.setup_type is None
        assert call.confidence == "none"
        assert call.note

    def test_no_data_is_not_a_crash(self):
        assert sc.classify(bars=[], atr14=ATR, sma20=None, sma50=None,
                            wall_chains=[], swing_lows=[]).setup_type is None
        assert sc.classify(bars=bars([100, 101]), atr14=0, sma20=None, sma50=None,
                            wall_chains=[], swing_lows=[]).setup_type is None


class TestRsWindow:
    def test_reversals_use_five_days_and_the_rest_use_twenty(self):
        # Rule 15: a fixed 20-day window structurally fails almost every
        # reversal thesis, because the stock is still dragging the fall it is
        # recovering from.
        for setup in (sc.RECLAIM, sc.FAILED_BREAKDOWN, sc.GAP_AND_HOLD):
            assert sc.rs_window_days(setup) == 5
        for setup in (sc.BREAKOUT, sc.RETEST, sc.PULLBACK):
            assert sc.rs_window_days(setup) == 20

    def test_every_setup_type_is_covered_by_the_window_rule(self):
        import setup_types
        for name in setup_types.SETUP_TYPES:
            assert sc.rs_window_days(name) in (5, 20)

    def test_the_classifier_only_ever_emits_official_names(self):
        # A label this module invents would be rejected by save_thesis and the
        # whole run would be lost.
        import setup_types
        emitted = {sc.BREAKOUT, sc.RETEST, sc.PULLBACK, sc.RECLAIM,
                   sc.FAILED_BREAKDOWN, sc.GAP_AND_HOLD}
        assert emitted == set(setup_types.SETUP_TYPES)


class TestFailedBreakdownNeedsTheRightOrder:
    """Found on the first live run, 2026-08-09: four tickers out of five came
    back Failed Breakdown, including MSFT -- which had run from 381 to 500 in
    ten days and was failing nothing at all. The old test asked only "did some
    bar dip under this level" and "is the close above it now", which is true of
    ANY stock that has rallied through an old level. The order was never
    checked."""

    # MSFT's real bars, 2026-07-23 to 2026-08-07, and the real April swing low
    # the rally passed straight through on its way up.
    MSFT_CLOSES = [381.58, 381.70, 389.10, 393.35, 390.54, 451.10, 464.72,
                    487.65, 492.81, 487.46, 499.86, 499.99]
    MSFT_LOW = {"date": "2026-04-23", "price": 411.41}

    def test_a_rally_up_through_an_old_level_is_not_a_failed_breakdown(self):
        call = sc.classify(bars=bars(self.MSFT_CLOSES), atr14=15.81,
                            sma20=430.0, sma50=400.0,
                            wall_chains=[wall(520.0)], swing_lows=[self.MSFT_LOW])
        assert call.setup_type != sc.FAILED_BREAKDOWN

    def test_a_real_break_and_reclaim_still_matches(self):
        # Above 100, closes below it, closes back above -- in that order.
        call = sc.classify(bars=bars([104, 103, 98, 97, 98.5, 101]), atr14=ATR,
                            sma20=101.0, sma50=101.0, wall_chains=[wall(130.0)],
                            swing_lows=[{"date": "2026-05-01", "price": 100.0}])
        assert call.setup_type == sc.FAILED_BREAKDOWN
        assert any("closed below it" in e for e in call.evidence)

    def test_a_wick_under_the_level_is_not_a_break(self):
        # It takes a CLOSE below to break a level -- the same daily-close
        # standard the rest of this system uses for a confirmed trigger.
        b = bars([104, 103, 102, 103, 104, 105])
        b[2]["low"] = 95.0                       # a deep wick, but the close held
        call = sc.classify(bars=b, atr14=ATR, sma20=101.0, sma50=101.0,
                            wall_chains=[wall(130.0)],
                            swing_lows=[{"date": "2026-05-01", "price": 100.0}])
        assert call.setup_type != sc.FAILED_BREAKDOWN

    def test_a_level_far_below_the_recent_range_is_ignored(self):
        # An ancient low the stock is nowhere near is not the level any current
        # thesis depends on (rule 2).
        call = sc.classify(bars=bars([104, 103, 102, 103, 104, 105]), atr14=ATR,
                            sma20=101.0, sma50=101.0, wall_chains=[wall(130.0)],
                            swing_lows=[{"date": "2026-01-01", "price": 40.0}])
        assert call.setup_type != sc.FAILED_BREAKDOWN
