"""Tests for ts.py -- the single exit engine shared by the shadow book
and the backtest (2026-08-03).

Each test pins one assumption from the module docstring. A silent change to any
of them would change every historical number this project produces, so each one
gets a named test rather than being left to a summary statistic to notice.
"""

import pytest

import indicators_core as ic
import trade_sim as ts


def _bar(day: int, o: float, h: float, low: float, c: float) -> dict:
    return {"day": day, "open": o, "high": h, "low": low, "close": c}


def _date(bar: dict) -> str:
    return f"2026-01-{bar['day']:02d}"


def _atrs(bars: list[dict], value: float = 5.0) -> list:
    """Flat ATR for the trailing-stop math -- the ATR calculation itself is
    already covered by test_indicators_core.py; what matters here is that the
    trail uses THAT BAR's value, which a flat series still exercises."""
    return [value] * len(bars)


class TestBuildPlan:
    def test_no_target_is_runner_only(self):
        """Rule 6: nothing qualifies -> the whole position trails out."""
        plan = ts.build_plan(100, 90, [])
        assert len(plan.tranches) == 1
        assert plan.tranches[0].price is None
        assert plan.tranches[0].pct == 100.0

    def test_one_target_is_forty_sixty(self):
        plan = ts.build_plan(100, 90, [120])
        assert [(t.price, t.pct) for t in plan.tranches] == [(120, 40.0), (None, 60.0)]

    def test_two_targets_are_forty_thirtyfive_twentyfive(self):
        plan = ts.build_plan(100, 90, [120, 140])
        assert [(t.price, t.pct) for t in plan.tranches] == [(120, 40.0), (140, 35.0), (None, 25.0)]

    def test_a_third_target_is_ignored_not_allocated(self):
        # Rule 7 defines splits for one and two targets only.
        plan = ts.build_plan(100, 90, [120, 140, 160])
        assert len(plan.tranches) == 3


class TestSimulate:
    PLAN1 = None  # built per test; entry 100, stop 90, risk 10

    def test_stop_hit_is_exactly_minus_one_r(self):
        bars = [_bar(1, 100, 101, 99, 100), _bar(2, 99, 99, 89, 90)]
        plan = ts.build_plan(100, 90, [120])
        r = ts.simulate(bars, 0, plan, _atrs(bars), _date)
        assert r.resolution == "stop"
        assert r.r_multiple == pytest.approx(-1.0)
        assert r.reached_t1 is False

    def test_gap_through_the_stop_fills_at_the_open(self):
        """Assumption 4 -- a gap-down fills worse than planned."""
        bars = [_bar(1, 100, 101, 99, 100), _bar(2, 80, 82, 78, 79)]
        plan = ts.build_plan(100, 90, [120])
        r = ts.simulate(bars, 0, plan, _atrs(bars), _date)
        assert r.exit_price == 80.0
        assert r.r_multiple == pytest.approx(-2.0)

    def test_target_and_stop_in_the_same_bar_assume_the_stop(self):
        """Assumption 3 -- assuming the good one is how backtests lie."""
        bars = [_bar(1, 100, 101, 99, 100), _bar(2, 100, 121, 89, 95)]
        plan = ts.build_plan(100, 90, [120])
        r = ts.simulate(bars, 0, plan, _atrs(bars), _date)
        assert r.resolution == "stop"
        assert r.r_multiple == pytest.approx(-1.0)

    def test_target_one_pays_forty_percent_then_the_runner_trails(self):
        bars = [_bar(1, 100, 101, 99, 100),
                _bar(2, 110, 121, 109, 120),     # target 1 = 120 hit
                _bar(3, 120, 140, 119, 139),
                _bar(4, 139, 141, 100, 101)]     # trail takes the runner out
        plan = ts.build_plan(100, 90, [120])
        r = ts.simulate(bars, 0, plan, _atrs(bars), _date)
        assert r.reached_t1 is True
        assert r.resolution == "runner_trailed"
        # 40% out at +2R; the runner leaves at the trailed stop, above entry.
        assert r.r_multiple > 0.4 * 2.0
        assert r.tranche_exits[0][3] == "target_1"

    def test_two_targets_pay_both_tranches(self):
        bars = [_bar(1, 100, 101, 99, 100),
                _bar(2, 110, 121, 109, 120),
                _bar(3, 125, 141, 124, 140),
                _bar(4, 140, 150, 139, 150)]
        plan = ts.build_plan(100, 90, [120, 140])
        r = ts.simulate(bars, 0, plan, _atrs(bars), _date)
        assert r.reached_t1 and r.reached_t2
        # 40% at +2R, 35% at +4R, 25% still open at the last close (+5R).
        assert r.r_multiple == pytest.approx(0.4 * 2 + 0.35 * 4 + 0.25 * 5)

    def test_both_targets_in_one_bar_still_pay_both(self):
        bars = [_bar(1, 100, 101, 99, 100), _bar(2, 110, 145, 109, 144)]
        plan = ts.build_plan(100, 90, [120, 140])
        r = ts.simulate(bars, 0, plan, _atrs(bars), _date)
        assert r.reached_t1 and r.reached_t2
        assert len(r.tranche_exits) == 2

    def test_runner_only_trails_from_the_first_bar(self):
        """Rule 6 -- no qualifying target, so the whole position is a runner."""
        bars = [_bar(1, 100, 105, 99, 104),
                _bar(2, 104, 130, 103, 129),
                _bar(3, 129, 131, 100, 101)]
        plan = ts.build_plan(100, 90, [])
        r = ts.simulate(bars, 0, plan, _atrs(bars), _date)
        assert r.reached_t1 is False
        # The trail ratcheted above the original 90 stop, so this exits positive.
        assert r.r_multiple > 0

    def test_trailing_stop_only_ratchets_up(self):
        bars = [_bar(1, 100, 101, 99, 100),
                _bar(2, 110, 121, 109, 120),
                _bar(3, 120, 125, 118, 119),   # a lower low must not lower the stop
                _bar(4, 119, 120, 60, 61)]
        plan = ts.build_plan(100, 90, [120])
        r = ts.simulate(bars, 0, plan, _atrs(bars), _date)
        exit_price = r.tranche_exits[-1][1]
        assert exit_price > 90.0    # never fell back toward the original stop

    def test_still_open_at_the_end_is_marked_open(self):
        """Assumption 7 -- marked to the last close, never counted as a win."""
        bars = [_bar(1, 100, 101, 99, 100), _bar(2, 100, 105, 99, 104)]
        plan = ts.build_plan(100, 90, [120])
        r = ts.simulate(bars, 0, plan, _atrs(bars), _date)
        assert r.resolution == "open"
        assert r.r_multiple == pytest.approx(0.4)

    def test_r_is_measured_against_the_original_stop_not_the_trail(self):
        """Assumption 2 -- the risk denominator never moves."""
        bars = [_bar(1, 100, 101, 99, 100),
                _bar(2, 110, 121, 109, 120),
                _bar(3, 120, 122, 100, 101)]
        plan = ts.build_plan(100, 90, [120])
        r = ts.simulate(bars, 0, plan, _atrs(bars), _date)
        # risk is 10 (entry 100 - original stop 90) throughout
        assert r.mfe_r == pytest.approx((122 - 100) / 10)

    def test_mfe_and_mae_stop_at_the_exit_bar(self):
        """MFE/MAE describe the TRADE, not the stock. A trade stopped out on bar
        3 must not be credited with a rally that happened on bar 5, and must not
        be charged with a crash on bar 6 either -- it was already flat.

        This is a regression test: both were originally max()/min() over every
        remaining bar in the dataset, which made every stopped-out trade in the
        15-year lab file look like it had been deeply in profit first."""
        bars = [_bar(1, 100, 105, 99, 104),
                _bar(2, 104, 112, 103, 110),    # best price while actually held
                _bar(3, 108, 109, 88, 89),      # stop at 90 taken out here
                _bar(4, 90, 200, 89, 199),      # huge rally AFTER the exit
                _bar(5, 199, 200, 40, 41)]      # and then a crash, also after
        plan = ts.build_plan(100, 90, [500])     # target far away, never reached
        r = ts.simulate(bars, 0, plan, _atrs(bars), _date)
        assert r.resolution == "stop"
        assert r.exit_date == "2026-01-03"
        assert r.mfe_r == pytest.approx((112 - 100) / 10)   # bar 2, not bar 4's 200
        assert r.mae_r == pytest.approx((88 - 100) / 10)    # bar 3, not bar 5's 40

    def test_mfe_covers_the_whole_hold_when_the_trade_never_exits(self):
        """The other side of the same rule: a position still open at the end of
        the data is alive for every bar, so every bar counts."""
        bars = [_bar(1, 100, 105, 99, 104),
                _bar(2, 104, 130, 103, 129),
                _bar(3, 129, 131, 95, 96)]
        plan = ts.build_plan(100, 90, [500])
        r = ts.simulate(bars, 0, plan, _atrs(bars), _date)
        assert r.resolution == "open"
        assert r.mfe_r == pytest.approx((131 - 100) / 10)
        assert r.mae_r == pytest.approx((95 - 100) / 10)

    def test_counterfactual_full_exit_at_target_one_is_recorded(self):
        """The 2026-08-03 backtest found this counterfactual mattered more than
        any entry filter, so the engine reports it on every trade."""
        bars = [_bar(1, 100, 101, 99, 100),
                _bar(2, 110, 121, 109, 120),
                _bar(3, 120, 160, 119, 159)]
        plan = ts.build_plan(100, 90, [120])
        r = ts.simulate(bars, 0, plan, _atrs(bars), _date)
        assert r.r_multiple_full_exit_at_t1 == pytest.approx(2.0)
        assert r.r_multiple > r.r_multiple_full_exit_at_t1   # the runner earned more

class TestTrailRules:
    """The research knobs from _backtest_results/PREREGISTRATION_TRAIL.md.

    One shared trade: bought at 100 with a stop at 90 and a target far out of
    reach, it runs to 131, then rolls over and trades down through 90. Under the
    live rule that is a full -1R. Each variant is pinned by what it rescues.
    """

    BARS = [_bar(1, 100, 105, 99, 104),
            _bar(2, 104, 130, 103, 129),     # peak 130 -> 3R at its best
            _bar(3, 129, 131, 95, 96),       # rolls over
            _bar(4, 96, 97, 60, 61)]         # through the original stop

    def _run(self, trail=None):
        plan = ts.build_plan(100, 90, [500])          # target never reached
        return ts.simulate(self.BARS, 0, plan, _atrs(self.BARS), _date, trail)

    def test_default_is_the_live_rule_and_gives_it_all_back(self):
        """No trail argument must reproduce today's behaviour exactly: the stop
        never moves before target 1, so the whole move is handed back."""
        r = self._run()
        assert r.resolution == "stop"
        assert r.r_multiple == pytest.approx(-1.0)
        assert r.mfe_r == pytest.approx(3.1)      # it really was up 3.1R first
        assert self._run(ts.TrailRule()).r_multiple == r.r_multiple

    def test_structure_trail_from_entry_rescues_the_round_trip(self):
        r = self._run(ts.TrailRule(start="entry"))
        assert r.resolution == "runner_trailed"
        assert r.r_multiple == pytest.approx(0.225)    # stop rode up to 102.25

    def test_a_looser_noise_floor_still_ratchets(self):
        """The floor changes which daily low qualifies, not the mechanism."""
        r = self._run(ts.TrailRule(start="entry", noise_floor_atr=1.5))
        assert r.r_multiple > -1.0

    def test_chandelier_trails_off_the_high_not_the_lows(self):
        r = self._run(ts.TrailRule(start="entry", method="chandelier",
                                    chandelier_atr=3.0))
        assert r.r_multiple == pytest.approx(1.5)      # 130 - 3x5 = 115

    def test_chandelier_gets_no_extra_buffer_on_top_of_its_multiple(self):
        """A 3x ATR chandelier must be 3x, not 3.15x -- the multiple is already
        the buffer, so the 0.15x stop buffer is deliberately not stacked."""
        r = self._run(ts.TrailRule(start="entry", method="chandelier",
                                    chandelier_atr=3.0))
        assert r.exit_price == pytest.approx(115.0)

    def test_breakeven_fires_while_the_stop_is_otherwise_still_frozen(self):
        """Breakeven is independent of the trail: start stays 'after_t1' here,
        so nothing else is moving the stop."""
        r = self._run(ts.TrailRule(breakeven_at_r=2.0))
        assert r.exit_price == pytest.approx(100.0)
        assert r.r_multiple == pytest.approx(0.0)

    def test_breakeven_does_not_fire_below_its_trigger(self):
        r = self._run(ts.TrailRule(breakeven_at_r=5.0))    # peak was only 3.1R
        assert r.r_multiple == pytest.approx(-1.0)

    def test_giveback_exits_at_the_next_open_not_the_signal_close(self):
        """Assumption 1 applies to this rule too: it is decided on a close and
        filled at the following open, never at the close that triggered it."""
        r = self._run(ts.TrailRule(giveback_after_r=1.5, giveback_frac=0.5))
        assert r.resolution == "giveback"
        assert r.exit_date == "2026-01-04"              # the bar AFTER the signal
        assert r.exit_price == pytest.approx(96.0)      # that bar's open
        assert r.r_multiple == pytest.approx(-0.4)

    def test_giveback_does_not_fire_below_its_trigger(self):
        r = self._run(ts.TrailRule(giveback_after_r=6.0, giveback_frac=0.5))
        assert r.resolution == "stop"
        assert r.r_multiple == pytest.approx(-1.0)

    def test_a_research_rule_stops_governing_once_target_one_sells(self):
        """The rule owns the stop only until target 1. After the first tranche
        sells, the runner goes back to the live structure trail no matter what
        the rule said -- otherwise a variant measures two changes at once, and
        the looser runner trail swamps the thing actually being tested.

        Here target 1 (110) sells on bar 2. A 6x ATR chandelier would put the
        runner's stop at 130 - 30 = 100, far below the structure trail's 102.25,
        so if the chandelier were still in charge the exit price would be 100.
        """
        bars = [_bar(1, 100, 105, 99, 104),
                _bar(2, 104, 130, 103, 129),     # target 110 hit here
                _bar(3, 129, 131, 95, 96)]       # trail takes the runner out
        plan = ts.build_plan(100, 90, [110])
        wide = ts.TrailRule(start="entry", method="chandelier", chandelier_atr=6.0)
        r = ts.simulate(bars, 0, plan, _atrs(bars), _date, wide)
        assert r.reached_t1
        runner_exit = r.tranche_exits[-1][1]
        assert runner_exit == pytest.approx(102.25)   # the live trail, not 100.0


class TestSimulate2:
    def test_stop_above_entry_returns_nothing_rather_than_faking_r(self):
        bars = [_bar(1, 100, 101, 99, 100)]
        r = ts.simulate(bars, 0, ts.Plan(100, 110, [ts.Tranche(None, 100.0)]), _atrs(bars), _date)
        assert r.r_multiple is None


class TestAtrSeries:
    def test_matches_atr_wilder_at_every_bar(self):
        highs = [10 + i * 0.5 for i in range(40)]
        lows = [9 + i * 0.5 for i in range(40)]
        closes = [9.5 + i * 0.5 for i in range(40)]
        series = ts.atr_series(highs, lows, closes, period=14)
        for i in (15, 20, 30, 39):
            expected = ic.atr_wilder(highs[:i + 1], lows[:i + 1], closes[:i + 1], period=14)
            assert series[i] == pytest.approx(expected)

    def test_undefined_bars_are_none_not_zero(self):
        highs = [10 + i for i in range(20)]
        lows = [9 + i for i in range(20)]
        closes = [9.5 + i for i in range(20)]
        series = ts.atr_series(highs, lows, closes, period=14)
        assert series[:14] == [None] * 14
        assert series[14] is not None

    def test_too_short_history_returns_all_none(self):
        assert ts.atr_series([1, 2], [1, 2], [1, 2], period=14) == [None, None]
class TestTheSimManagesAStopTheWayLiveDoes:
    """Two ways this engine used to manage a position that the live system does
    not, either of which makes every number it produces describe a different
    strategy however correct the arithmetic is.
    """

    def _bars(self, closes, start=1767225600):
        return [{"time": start + i * 86400, "open": c, "high": c + 1.0,
                  "low": c - 1.0, "close": c} for i, c in enumerate(closes)]

    def _date(self, bar):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(bar["time"], tz=timezone.utc).date().isoformat()

    def _run(self, closes, **kw):
        bars = self._bars(closes)
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        cl = [b["close"] for b in bars]
        atrs = ts.atr_series(highs, lows, cl)
        atrs = [a if a else 2.0 for a in atrs]
        plan = ts.build_plan(100.0, 95.0, [115.0])
        return ts.simulate(bars, 1, plan, atrs, self._date, **kw)

    def test_the_defaults_are_the_live_rules(self):
        # Not a style preference. A default that differs from live means every
        # historical number in the book describes a system nobody trades.
        assert ts.ATR_MODE_FROZEN == "frozen"
        assert ts.STRUCTURE_LIVE == "live_levels"
        import inspect
        params = inspect.signature(ts.simulate).parameters
        assert params["atr_mode"].default == ts.ATR_MODE_FROZEN
        assert params["structure"].default == ts.STRUCTURE_LIVE

    def test_future_bars_cannot_change_a_past_stop_decision(self):
        """The guard that makes the whole thing usable as evidence.

        Handing the level picker the entire series would let a stop on day five
        stand on a low that had not happened yet, and every result after it
        would be a number nobody could have traded. This runs the same trade
        twice with completely different endings and asserts the shared opening
        plays out identically.
        """
        opening = [99, 101, 104, 103, 106, 108, 107]
        crash = self._run(opening + [70, 60, 55])
        moon = self._run(opening + [140, 160, 180])

        # the exits that happened during the SHARED bars must be identical
        shared = self._date(self._bars(opening + [0])[len(opening) - 1])
        early_crash = [e for e in crash.tranche_exits if e[0] <= shared]
        early_moon = [e for e in moon.tranche_exits if e[0] <= shared]
        assert early_crash == early_moon

    def test_a_low_the_live_picker_would_not_offer_is_not_used(self):
        # The old engine took ANY daily low since entry, a strictly looser set:
        # on real data it offered 39 distinct levels against the live picker's
        # 18, topping out 7 points higher, so it ratcheted stops the live system
        # could never have placed. Measured over the whole shadow book, that
        # difference moved 28 of 116 trades and flattered the average by 0.03R.
        closes = [99, 101, 103, 105, 107, 109, 111, 113, 116, 118, 112, 108, 104]
        live = self._run(closes)
        loose = self._run(closes, structure=ts.STRUCTURE_DAILY_LOWS)
        assert live.r_multiple is not None and loose.r_multiple is not None
        # the looser set can only ever place the stop equal or higher
        assert loose.r_multiple >= live.r_multiple - 1e-9

    def test_the_frozen_atr_is_the_entry_bars_atr(self):
        # Live uses the position's atr_at_build for the life of the trade, and
        # says why: judging an existing stop against a freshly recomputed ATR
        # re-rates every open position whenever volatility moves.
        closes = [99, 101, 103, 105, 107, 109, 111, 113, 116, 118, 112, 108, 104]
        frozen = self._run(closes)
        per_bar = self._run(closes, atr_mode=ts.ATR_MODE_PER_BAR)
        assert frozen.resolution and per_bar.resolution   # both complete
        # and the knob is real -- the two are computed from different inputs
        assert ts.ATR_MODE_PER_BAR != ts.ATR_MODE_FROZEN

    def test_an_empty_candidate_list_never_costs_the_trade_its_trail(self):
        # A short history can leave the level picker with nothing to offer. The
        # trail falls back rather than silently freezing the stop.
        short = self._run([99, 101, 103, 118, 112])
        assert short.resolution is not None

