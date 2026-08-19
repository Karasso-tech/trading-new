"""Unit tests for score_shadow.py's full-plan simulation (2026-08-03 upgrade).

Pure function, synthetic bars only -- no fetch, no DB, same scope as
test_score_shadow.py's own tests of the older capture-only path.

Each test below pins ONE of the assumptions listed in score_shadow.py's own
assumptions block. That is deliberate: the assumptions are what make this
usable as backtest evidence, so a silent change to any of them should break a
named test rather than quietly change every historical number.
"""

from datetime import datetime, timezone

import pytest

import score_shadow
from score_shadow import SIM_VERSION, extract_plan, simulate_trade


def _ohlc(date_iso: str, o: float, h: float, low: float, c: float) -> dict:
    ts = datetime.fromisoformat(date_iso).replace(tzinfo=timezone.utc).timestamp()
    return {"time": ts, "open": o, "high": h, "low": low, "close": c}


PLAN = {"trigger": 100.0, "stop": 90.0, "atr_at_build": 5.0, "setup_type": "Breakout",
        "targets": [{"price": 120.0, "pct": 40.0}]}

FIRES = _ohlc("2026-01-02", 99, 101, 98, 100.5)     # closes above the 100 trigger
ENTRY = _ohlc("2026-01-05", 100, 101, 99, 100)      # entry at the open: 100, risk 10


class TestExtractPlan:
    def test_reads_the_stored_setup_shape(self):
        plan = extract_plan({"type": "Breakout", "trigger": 100.0, "stop": 90.0,
                              "atr_at_build": 5.0,
                              "targets": [{"price": "120.00", "pct": "40"}]})
        assert plan["trigger"] == 100.0
        assert plan["setup_type"] == "Breakout"
        assert plan["targets"] == [{"price": 120.0, "pct": 40.0}]

    def test_free_text_trigger_is_not_guessed_at(self):
        plan = extract_plan({"trigger": "no order ready; trigger set after the candle forms"})
        assert plan["trigger"] is None

    def test_targets_are_sorted_and_capped_at_two(self):
        plan = extract_plan({"trigger": 10.0, "targets": [
            {"price": 30.0, "pct": 25}, {"price": 20.0, "pct": 40}, {"price": 25.0, "pct": 35}]})
        assert [t["price"] for t in plan["targets"]] == [20.0, 25.0]

    def test_missing_setup_is_empty_not_an_error(self):
        assert extract_plan(None)["trigger"] is None


class TestSimulateTrade:
    def test_never_fired(self):
        sim = simulate_trade([_ohlc("2026-01-02", 95, 96, 94, 95)], PLAN, "2026-01-01")
        assert sim.fired is False
        assert sim.resolution == "never_fired"
        assert sim.r_multiple_planned is None

    def test_entry_is_the_next_open_not_the_trigger(self):
        """Assumption 2: a daily-close confirmation can only be acted on the
        next morning, and the gap between trigger and that open is a real cost
        of the rule -- measured, not hidden."""
        bars = [FIRES, _ohlc("2026-01-05", 105, 106, 104, 105)]
        sim = simulate_trade(bars, PLAN, "2026-01-01")
        assert sim.fired is True
        assert sim.entry == 105.0
        assert sim.entry_gap_pct == pytest.approx(5.0)
        # Assumption 3: the stop is NOT re-derived from the higher entry --
        # a gap-up genuinely does widen real risk.
        assert sim.risk_per_share == pytest.approx(15.0)

    def test_stop_hit_is_minus_one_r(self):
        bars = [FIRES, ENTRY, _ohlc("2026-01-06", 99, 99, 89, 90)]
        sim = simulate_trade(bars, PLAN, "2026-01-01")
        assert sim.resolution == "stop"
        assert sim.r_multiple_simple == pytest.approx(-1.0)
        assert sim.r_multiple_planned == pytest.approx(-1.0)
        assert sim.bars_held == 2

    def test_target_pays_the_planned_tranche_then_rides_at_breakeven(self):
        """Assumption 7: after target 1 the remainder rides with the stop at
        breakeven -- a fixed stand-in for the real trailing rule, which needs
        per-bar structure judgment that cannot be simulated."""
        bars = [FIRES, ENTRY,
                _ohlc("2026-01-06", 110, 121, 109, 120),   # target 120 hit
                _ohlc("2026-01-07", 120, 130, 119, 130)]   # runner rides on
        sim = simulate_trade(bars, PLAN, "2026-01-01")
        assert sim.resolution == "target_1"
        assert sim.r_multiple_simple == pytest.approx(2.0)
        assert sim.r_multiple_planned == pytest.approx(0.4 * 2.0 + 0.6 * 3.0)
        assert sim.mfe_r == pytest.approx(3.0)

    def test_stop_and_target_in_the_same_bar_assume_the_stop(self):
        """Assumption 4 -- a daily bar cannot say which came first, and
        assuming the good one is how backtests lie."""
        bars = [FIRES, ENTRY, _ohlc("2026-01-06", 100, 121, 89, 95)]
        sim = simulate_trade(bars, PLAN, "2026-01-01")
        assert sim.resolution == "stop"
        assert sim.r_multiple_simple == pytest.approx(-1.0)

    def test_gap_through_the_stop_fills_at_the_open(self):
        """Assumption 5 -- a gap-down fills worse than planned."""
        bars = [FIRES, ENTRY, _ohlc("2026-01-06", 80, 82, 78, 79)]
        sim = simulate_trade(bars, PLAN, "2026-01-01")
        assert sim.exit_price == 80.0
        assert sim.r_multiple_simple == pytest.approx(-2.0)

    def test_still_open_is_marked_open_not_counted_as_a_win(self):
        """Assumption 8 -- marked to the last close and flagged, never
        silently scored as a winner or dropped from the sample."""
        bars = [FIRES, ENTRY, _ohlc("2026-01-06", 101, 105, 100, 104)]
        sim = simulate_trade(bars, PLAN, "2026-01-01")
        assert sim.resolution == "open"
        assert sim.r_multiple_simple == pytest.approx(0.4)

    def test_fired_on_the_last_bar_has_no_entry_yet(self):
        sim = simulate_trade([FIRES], PLAN, "2026-01-01")
        assert sim.fired is True
        assert sim.entry is None
        assert sim.resolution == "open"
        assert "no entry bar yet" in sim.note

    def test_stop_above_entry_leaves_r_uncomputable_rather_than_faked(self):
        plan = {"trigger": 100.0, "stop": 110.0, "targets": []}
        bars = [FIRES, _ohlc("2026-01-05", 105, 106, 104, 105)]
        sim = simulate_trade(bars, plan, "2026-01-01")
        assert sim.r_multiple_planned is None
        assert "R cannot be computed" in sim.note

    def test_two_targets_pay_both_tranches(self):
        plan = {"trigger": 100.0, "stop": 90.0,
                "targets": [{"price": 120.0, "pct": 40.0}, {"price": 140.0, "pct": 35.0}]}
        bars = [FIRES, ENTRY,
                _ohlc("2026-01-06", 110, 121, 109, 120),
                _ohlc("2026-01-07", 125, 141, 124, 140),
                _ohlc("2026-01-08", 140, 150, 139, 150)]
        sim = simulate_trade(bars, plan, "2026-01-01")
        assert sim.resolution == "target_2"
        assert sim.r_multiple_planned == pytest.approx(0.4 * 2.0 + 0.35 * 4.0 + 0.25 * 5.0)

    def test_r_is_measured_in_risk_units_not_percent(self):
        """The whole reason for the upgrade: on the first real run the F-graded
        rejects showed the biggest percentage moves purely because F-graded
        tickers are the jumpy ones. In R, a calm winner and a wild winner with
        the same plan score identically."""
        calm = {"trigger": 100.0, "stop": 99.0, "targets": [{"price": 102.0, "pct": 40.0}]}
        wild = {"trigger": 100.0, "stop": 80.0, "targets": [{"price": 140.0, "pct": 40.0}]}
        calm_bars = [FIRES, _ohlc("2026-01-05", 100, 100.5, 99.5, 100),
                      _ohlc("2026-01-06", 100, 102.5, 100, 102)]
        wild_bars = [FIRES, _ohlc("2026-01-05", 100, 105, 95, 100),
                      _ohlc("2026-01-06", 100, 141, 100, 140)]
        assert simulate_trade(calm_bars, calm, "2026-01-01").r_multiple_simple == pytest.approx(2.0)
        assert simulate_trade(wild_bars, wild, "2026-01-01").r_multiple_simple == pytest.approx(2.0)

    def test_bars_before_date_built_are_ignored(self):
        """Assumption 1 -- a setup cannot fire on price action that predates
        the thesis that defined it."""
        bars = [_ohlc("2025-12-01", 150, 160, 140, 155), _ohlc("2026-01-02", 95, 96, 94, 95)]
        assert simulate_trade(bars, PLAN, "2026-01-01").fired is False

    def test_sim_version_is_recorded(self):
        # Stored on every row so a later change to these assumptions is
        # identifiable instead of quietly mixed into the old data.
        assert isinstance(SIM_VERSION, str) and SIM_VERSION


# --- the 2026-08-09 columns -------------------------------------------------
#
# A shadow row said what an idea DID and never what the market did while it did
# it, so "+0.05R on average" could not be read in either direction. These cover
# the yardstick, the fire clock and the plan's own stated reward:risk.

def _bar(day, close, high=None, low=None, open_=None):
    from datetime import datetime, timezone
    ts = int(datetime(2026, 1, day, tzinfo=timezone.utc).timestamp())
    return {"time": ts, "open": open_ if open_ is not None else close,
            "high": high if high is not None else close,
            "low": low if low is not None else close, "close": close}


SPY_BARS = [_bar(5, 100.0), _bar(6, 101.0), _bar(7, 102.0), _bar(8, 99.0), _bar(9, 110.0)]


class TestBenchmarkReturn:
    def test_measures_the_trades_own_window_not_the_whole_history(self):
        # 2026-01-06 close 101 -> 2026-01-08 close 99 is -1.98%. The 110 on the
        # last bar is outside the window and must not leak in.
        pct = score_shadow.benchmark_return(SPY_BARS, "2026-01-06", "2026-01-08")
        assert pct == pytest.approx((99 - 101) / 101 * 100)

    def test_an_open_trade_marks_to_the_last_available_bar(self):
        pct = score_shadow.benchmark_return(SPY_BARS, "2026-01-06", None)
        assert pct == pytest.approx((110 - 101) / 101 * 100)

    def test_no_bars_in_the_window_is_none_not_zero(self):
        # None reads as "not measured"; 0.0 would read as "the market went
        # nowhere", which is a different and false claim.
        assert score_shadow.benchmark_return(SPY_BARS, "2027-01-01", None) is None
        assert score_shadow.benchmark_return([], "2026-01-06", None) is None

    def test_an_end_before_the_start_is_none(self):
        assert score_shadow.benchmark_return(SPY_BARS, "2026-01-08", "2026-01-06") is None


class TestTradingDaysBetween:
    def test_counts_real_bars_not_calendar_days(self):
        # 5th to 8th is four bars, i.e. three trading days apart. A weekend or a
        # holiday is already absent from the bars, so it cannot be counted.
        assert score_shadow.trading_days_between(SPY_BARS, "2026-01-05", "2026-01-08") == 3

    def test_same_day_is_zero(self):
        assert score_shadow.trading_days_between(SPY_BARS, "2026-01-05", "2026-01-05") == 0

    def test_missing_input_is_none(self):
        assert score_shadow.trading_days_between(SPY_BARS, "2026-01-05", None) is None
        assert score_shadow.trading_days_between([], "2026-01-05", "2026-01-08") is None


class TestRrAtBuild:
    def test_uses_the_first_sellable_target_not_the_furthest(self):
        # rule 3 gates on the nearest sellable level and rule 7 sells the first
        # tranche there; quoting the far one states a reward never claimed.
        plan = {"trigger": 100.0, "stop": 90.0,
                "targets": [{"price": 125.0}, {"price": 200.0}]}
        assert score_shadow.rr_at_build(plan) == pytest.approx(2.5)

    def test_no_target_or_no_stop_is_none_never_a_guess(self):
        assert score_shadow.rr_at_build({"trigger": 100.0, "stop": 90.0, "targets": []}) is None
        assert score_shadow.rr_at_build({"trigger": 100.0, "targets": [{"price": 125.0}]}) is None

    def test_a_stop_above_the_trigger_is_none_not_negative(self):
        plan = {"trigger": 90.0, "stop": 100.0, "targets": [{"price": 125.0}]}
        assert score_shadow.rr_at_build(plan) is None
