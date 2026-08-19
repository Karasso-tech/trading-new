"""Tests for the open-position trailing stop (level_picker.trail_stop and
bot/trail_stop.py).

This decides where real money exits, so the two hard properties get their own
named tests: it never moves down, and it measures against the ATR frozen at
entry rather than today's.
"""

import pytest

import level_picker as lp
import trail_stop

ATR = 2.0

LOWS = [
    {"date": "2026-07-01", "price": 90.0},
    {"date": "2026-07-20", "price": 96.0},
    {"date": "2026-08-01", "price": 103.0},
]


class TestTrailStop:
    def test_it_moves_up_to_the_highest_low_that_clears_the_floor(self):
        # Price 106, ATR 2 -> the noise floor is 1.4. A stop under the 103 low
        # sits at 102.7, which is 3.3 away: clear. Old stop 95.7.
        r = lp.trail_stop(current_price=106.0, current_stop=95.7,
                           atr_at_build=ATR, swing_lows=LOWS)
        assert r.moved is True
        assert r.basis_level == 103.0
        assert r.stop == pytest.approx(102.7)

    def test_the_buffer_sits_under_the_level_never_on_it(self):
        r = lp.trail_stop(current_price=106.0, current_stop=95.7,
                           atr_at_build=ATR, swing_lows=LOWS)
        assert r.stop == pytest.approx(r.basis_level - lp.STOP_BUFFER_ATR * ATR)

    def test_a_low_too_close_to_todays_price_is_skipped(self):
        # Price 104: a stop under the 103 low would be 102.7, only 1.3 away --
        # inside the 1.4 noise band. It falls back to the next low down.
        r = lp.trail_stop(current_price=104.0, current_stop=95.0,
                           atr_at_build=ATR, swing_lows=LOWS)
        assert r.basis_level == 96.0
        assert r.stop == pytest.approx(95.7)

    def test_it_never_moves_down(self):
        # The whole point. A trail that loosens is not a trail, and
        # persistence.update_current_stop rejects it at the database anyway.
        r = lp.trail_stop(current_price=106.0, current_stop=104.0,
                           atr_at_build=ATR, swing_lows=LOWS)
        assert r.moved is False
        assert r.stop == 104.0

    def test_nothing_new_means_the_stop_stays_put_with_a_reason(self):
        r = lp.trail_stop(current_price=106.0, current_stop=104.0,
                           atr_at_build=ATR, swing_lows=[])
        assert r.moved is False
        assert r.stop == 104.0
        assert "stays exactly where it is" in r.reason

    def test_no_build_time_atr_leaves_the_stop_alone(self):
        # Guessing an ATR here would move a real stop on a made-up number.
        r = lp.trail_stop(current_price=106.0, current_stop=95.0,
                           atr_at_build=0, swing_lows=LOWS)
        assert r.moved is False
        assert r.stop == 95.0


class TestFrozenAtrIsUsed:
    """`atr_at_build` is frozen at entry on purpose. Judging an existing stop
    against a freshly recomputed ATR is the exact recurring error report_lint
    was written to catch (AMZN/LLY/CRM/UPS, twice in July 2026), and it quietly
    re-rates every open position whenever volatility moves."""

    def _payload(self, atr_at_build, live_atr):
        return {
            "ticker": "TEST", "current_price": 106.0, "atr14": live_atr,
            "swing_lows_recent": LOWS,
            "open_position": {
                "current_stop": 95.7, "entry_price": 100.0, "initial_stop": 95.7,
                "entry_setup": {"atr_at_build": atr_at_build},
                "tranche_plan": {"tranches": [
                    {"label": "target_1", "filled_qty": 40, "status": "filled"}]},
            },
        }

    def test_the_frozen_atr_wins_over_the_live_one(self):
        out = trail_stop.compute(self._payload(atr_at_build=2.0, live_atr=9.0))
        assert out["atr_used"] == 2.0
        assert "frozen at entry" in out["atr_source"]
        assert out["stop_should_be"] == pytest.approx(102.7)

    def test_a_bigger_live_atr_would_have_given_a_different_answer(self):
        # Proof the distinction is not cosmetic: on the live ATR the 103 low
        # sits inside the noise band and the stop would not move at all.
        loose = lp.trail_stop(current_price=106.0, current_stop=95.7,
                               atr_at_build=9.0, swing_lows=LOWS)
        assert loose.moved is False

    def test_a_position_with_no_frozen_atr_says_which_one_it_used(self):
        out = trail_stop.compute(self._payload(atr_at_build=None, live_atr=2.0))
        assert out["atr_used"] == 2.0
        assert "no atr_at_build on file" in out["atr_source"]

    def test_no_open_position_is_an_honest_error_not_a_stop(self):
        out = trail_stop.compute({"ticker": "TEST", "current_price": 106.0})
        assert "error" in out
        assert "stop_should_be" not in out


class TestOutputShape:
    def test_it_reports_both_numbers_and_the_level_behind_them(self):
        out = trail_stop.compute({
            "ticker": "TEST", "current_price": 106.0, "atr14": 2.0,
            "swing_lows_recent": LOWS,
            "open_position": {"current_stop": 95.7, "entry_price": 100.0,
                               "initial_stop": 95.7,
                               "entry_setup": {"atr_at_build": 2.0},
                               "tranche_plan": {"tranches": [
                                   {"label": "target_1", "filled_qty": 40,
                                    "status": "filled"}]}},
        })
        assert out["stop_now"] == 95.7
        assert out["stop_should_be"] == pytest.approx(102.7)
        assert out["stop_basis_level"] == 103.0
        assert out["stop_basis_date"] == "2026-08-01"
        assert out["moved"] is True


class TestOlderLevelsAreFlaggedNotHidden:
    """STRATEGY_v3 asks for "real NEW structure since entry". On the first live
    run, ASTS came back wanting its stop lifted from 54.78 to 66.53 on a low
    dated 2026-04-29 -- three months BEFORE the position was opened on 07-31.

    That may still be the right level. It is emphatically not the same claim as
    "the trade has made a new higher low", and a reader deciding whether to move
    a real stop needs to know which one they are looking at."""

    def _payload(self, basis_date, entry_date):
        return {
            "ticker": "ASTS", "current_price": 71.94, "atr14": 6.39,
            "swing_lows_recent": [{"date": basis_date, "price": 67.49}],
            "open_position": {
                "current_stop": 54.78, "entry_price": 58.86, "initial_stop": 51.84,
                "entry_date": entry_date,
                "entry_setup": {"atr_at_build": 6.39},
                # The real ASTS shape: target 1 sold, runner still open. Only a
                # position past target 1 may trail at all.
                "tranche_plan": {"tranches": [
                    {"label": "target_1", "filled_qty": 84, "status": "filled"},
                    {"label": "runner", "filled_qty": 45, "status": "partial"}]},
            },
        }

    def test_a_pre_entry_low_is_flagged(self):
        out = trail_stop.compute(self._payload("2026-04-29", "2026-07-31"))
        assert out["moved"] is True
        assert out["basis_after_entry"] is False
        assert "BEFORE this position was opened" in out["caution"]

    def test_a_low_made_since_entry_carries_no_caution(self):
        out = trail_stop.compute(self._payload("2026-08-04", "2026-07-31"))
        assert out["basis_after_entry"] is True
        assert "caution" not in out

    def test_the_number_is_identical_either_way(self):
        # The flag is disclosure, never a different answer -- same posture as
        # every other rule 19-22 style disclosure in this system.
        old = trail_stop.compute(self._payload("2026-04-29", "2026-07-31"))
        new = trail_stop.compute(self._payload("2026-08-04", "2026-07-31"))
        assert old["stop_should_be"] == new["stop_should_be"]

    def test_the_distance_from_price_is_reported(self):
        # 71.94 - 66.53 = 5.41 on an ATR of 6.39 -- 0.85x, only just clear of
        # the 0.7 floor. Tight, and worth being able to see.
        out = trail_stop.compute(self._payload("2026-08-04", "2026-07-31"))
        assert out["distance_atr"] == pytest.approx(0.847, abs=0.01)


class TestNoTrailingBeforeTargetOne:
    """This project's own measured result: twelve pre-registered ways to lock in
    profit before the first target were tested, and ALL TWELVE lost money, with
    tighter consistently worse (private backtest notes, point 7). The same five
    years found the runner tranche to be the entire edge -- +40.6R with it
    against -33.3R without -- and an early-tightened stop is exactly how a
    runner gets killed before it can run.

    The first version of trail_stop, written the same day, had no such guard.
    Run against the real book it recommended lifting the stop on three full-size
    positions that had not reached target 1: NBIS, BE and NOW. The owner asked
    whether he should act on it, which is the only reason it was caught before a
    real stop moved."""

    def test_a_full_size_position_never_trails(self):
        r = lp.trail_stop(current_price=106.0, current_stop=95.0,
                           atr_at_build=ATR, swing_lows=LOWS, past_target_1=False)
        assert r.moved is False
        assert r.stop == 95.0
        assert "has not reached target 1" in r.reason

    def test_the_same_position_would_have_trailed_after_target_1(self):
        # Proof the guard is what stopped it, not the levels.
        r = lp.trail_stop(current_price=106.0, current_stop=95.0,
                           atr_at_build=ATR, swing_lows=LOWS, past_target_1=True)
        assert r.moved is True

    def _payload(self, tranches):
        return {
            "ticker": "TEST", "current_price": 106.0, "atr14": ATR,
            "swing_lows_recent": LOWS,
            "open_position": {
                "current_stop": 95.0, "entry_price": 100.0, "initial_stop": 95.0,
                "entry_date": "2026-07-01",
                "entry_setup": {"atr_at_build": ATR},
                "tranche_plan": {"tranches": tranches},
            },
        }

    def test_an_unfilled_target_reads_as_not_past_it(self):
        out = trail_stop.compute(self._payload(
            [{"label": "target_1", "filled_qty": 0, "status": "open"},
             {"label": "runner", "filled_qty": 0, "status": "open"}]))
        assert out["past_target_1"] is False
        assert out["moved"] is False

    def test_a_filled_target_unlocks_the_trail(self):
        # The real ASTS shape: target_1 filled, runner partially trimmed.
        out = trail_stop.compute(self._payload(
            [{"label": "target_1", "filled_qty": 84, "status": "filled"},
             {"label": "runner", "filled_qty": 45, "status": "partial"}]))
        assert out["past_target_1"] is True
        assert out["moved"] is True

    def test_a_runner_fill_alone_does_not_count_as_target_1(self):
        # A discretionary runner trim is not the same event as selling the
        # first planned target.
        out = trail_stop.compute(self._payload(
            [{"label": "target_1", "filled_qty": 0, "status": "open"},
             {"label": "runner", "filled_qty": 10, "status": "partial"}]))
        assert out["past_target_1"] is False

    def test_a_position_with_no_tranche_plan_is_treated_as_not_past_it(self):
        # The cautious default. Assuming otherwise would trail a stop on a
        # position nobody can confirm has taken profit.
        out = trail_stop.compute({
            "ticker": "TEST", "current_price": 106.0, "atr14": ATR,
            "swing_lows_recent": LOWS,
            "open_position": {"current_stop": 95.0, "entry_price": 100.0,
                               "initial_stop": 95.0, "entry_date": "2026-07-01",
                               "entry_setup": {"atr_at_build": ATR}},
        })
        assert out["past_target_1"] is False
        assert out["moved"] is False
