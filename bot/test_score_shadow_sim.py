import persistence
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
@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(persistence, "DB_PATH", db_path)
    persistence.init_db()
    return db_path


class TestRunOneEndToEnd:
    """The path that had no test at all, and paid for it.

    `is_fully_closed` was added to trade_sim.SimResult -- the engine's result --
    and score_shadow hands run_one a DIFFERENT type, TradeSim. So
    `int(sim.is_fully_closed)` was an AttributeError waiting on the first row of
    the next nightly scan, and the whole suite passed, because nothing exercised
    run_one. Every unit below it was covered; the wiring between them was not.

    These stub the fetch and the clock, so the test is real wiring over
    synthetic bars: no network, no TradingView, no writes outside the temp DB.
    """

    def _bars(self, closes):
        """Bars starting on the build date -- score_shadow ignores anything
        before it, so bars from a fixed epoch would make every case read as
        never_fired regardless of what the closes say."""
        from datetime import datetime, timezone
        built = datetime.fromisoformat(self._candidate["built_at"][:10])
        start = built.replace(tzinfo=timezone.utc).timestamp()
        return [{"time": start + i * 86400, "open": c, "high": c + 1,
                  "low": c - 1, "close": c} for i, c in enumerate(closes)]

    def _thesis(self, temp_db):
        persistence.save_thesis(
            ticker="ABC", status="pending", source="SCREENER_v3",
            primary_setup={"type": "Breakout", "trigger": 100.0, "stop": 95.0,
                            "atr_at_build": 2.0,
                            "targets": [{"price": 115.0, "pct": "40", "status": "pass"}]},
            rubric_grade="B", market_regime_at_build="healthy_uptrend",
            decision="Buy Only If Confirmed")
        return persistence.get_shadow_candidates()[0]

    def _run(self, monkeypatch, closes):
        score_shadow.clear_bars_cache()
        monkeypatch.setattr(score_shadow, "_fetch_bars",
                             lambda ticker: _async(self._bars(closes)))
        monkeypatch.setattr(score_shadow, "_spy_bars", lambda: [])
        return score_shadow.run_one(self._candidate)

    def test_a_finished_trade_is_written_as_finished(self, temp_db, monkeypatch):
        self._candidate = self._thesis(temp_db)
        row_id = self._run(monkeypatch, [99, 101, 100, 96, 90, 88])
        assert row_id is not None
        with persistence._db() as conn:
            row = conn.execute("SELECT * FROM shadow_outcomes WHERE id=?", (row_id,)).fetchone()
        assert row["resolution"] == "stop"
        assert row["is_fully_closed"] == 1
        assert row["setup_side"] == "primary"

    def test_a_running_trade_is_not(self, temp_db, monkeypatch):
        self._candidate = self._thesis(temp_db)
        row_id = self._run(monkeypatch, [99, 101, 103, 104, 105, 106])
        with persistence._db() as conn:
            row = conn.execute("SELECT * FROM shadow_outcomes WHERE id=?", (row_id,)).fetchone()
        assert row["is_fully_closed"] == 0

    def test_a_non_firing_idea_records_how_near_it_came(self, temp_db, monkeypatch):
        # The four columns added the same day, written through the real path
        # rather than checked on the pure function alone.
        self._candidate = self._thesis(temp_db)
        row_id = self._run(monkeypatch, [95, 96, 99.5, 97, 96])
        with persistence._db() as conn:
            row = conn.execute("SELECT * FROM shadow_outcomes WHERE id=?", (row_id,)).fetchone()
        assert row["resolution"] in ("never_fired", "expired_never_fired")
        assert row["closest_approach_pct"] == pytest.approx(-0.5, abs=0.01)
        assert row["closest_approach_atr"] == pytest.approx(-0.25, abs=0.01)
        assert row["closest_approach_date"] is not None
        assert row["move_without_entry_pct"] == pytest.approx(1.05, abs=0.01)

    def test_bars_are_fetched_once_per_ticker(self, temp_db, monkeypatch):
        # 244 candidates sit on 61 tickers. Without the cache that is four times
        # the network work for identical data.
        self._candidate = self._thesis(temp_db)
        calls = []
        canned = self._bars([99, 101, 103])

        score_shadow.clear_bars_cache()
        monkeypatch.setattr(score_shadow, "_spy_bars", lambda: [])
        monkeypatch.setattr(score_shadow, "_fetch_bars",
                             lambda ticker: (calls.append(ticker),
                                             _async(canned))[1])
        score_shadow.run_one(self._candidate)
        second = dict(self._candidate)
        second["checked_date"] = "later"
        score_shadow.run_one(second)
        assert calls == ["ABC"]


async def _coro(value):
    return value


def _async(value):
    return _coro(value)

class TestTheGradeAtBuildAndTheGradeAtFireAreTwoThings:
    """CONSISTENCY_RULES.md rule 27's own worked example, made checkable.

    "Buy 100 / stop 97 / target 108 fires and closes at 102. The real order is
    5 of risk against 6 of reward, but it was still scored 8-against-3, still
    graded B, and still printed an order." The live system was fixed for this;
    the shadow book had no field for the second number at all, so a build-time
    grade and a fire-time grade shared one column and no analysis could say
    which of the two had failed.
    """

    INPUTS = {"rr": 2.67, "target_atr_multiple": 4.0, "regime": "healthy_uptrend",
               "rs_delta_pct": 3.0, "dist_sma20_atr": 0.4, "earnings_days_out": 40}

    def test_the_same_setup_grades_lower_once_the_fill_is_known(self):
        planned, planned_rr = score_shadow.regrade_at_fire(
            self.INPUTS, entry=100.0, stop=97.0, target=108.0, atr_at_build=2.0)
        filled, filled_rr = score_shadow.regrade_at_fire(
            self.INPUTS, entry=102.0, stop=97.0, target=108.0, atr_at_build=2.0)
        assert planned == "A" and filled == "B"
        assert planned_rr == pytest.approx(2.67, abs=0.01)
        assert filled_rr == pytest.approx(1.20, abs=0.01)

    def test_only_the_two_price_criteria_move(self):
        # Regime, relative strength and the event window are the build-time
        # values: this book stores no per-date history of them, and inventing
        # one would be worse than reusing a stated one. That limit is why the
        # field is called "at fire" and not "today".
        grade, _ = score_shadow.regrade_at_fire(
            dict(self.INPUTS, regime="risk_off", rs_delta_pct=-5.0),
            entry=100.0, stop=97.0, target=108.0, atr_at_build=2.0)
        # rr and target_atr both still pass; rs now fails; regime is reported,
        # not scored -- so 4 of 5.
        assert grade == "B"

    @pytest.mark.parametrize("why,kwargs", [
        ("no stored inputs", dict(rubric_inputs=None, entry=102.0, stop=97.0,
                                   target=108.0, atr_at_build=2.0)),
        ("never fired", dict(rubric_inputs=INPUTS, entry=None, stop=97.0,
                              target=108.0, atr_at_build=2.0)),
        ("gapped through the stop", dict(rubric_inputs=INPUTS, entry=96.0, stop=97.0,
                                          target=108.0, atr_at_build=2.0)),
        ("no target", dict(rubric_inputs=INPUTS, entry=102.0, stop=97.0,
                            target=None, atr_at_build=2.0)),
    ])
    def test_it_says_nothing_rather_than_guessing(self, why, kwargs):
        # None reads as "not measured". A letter here would read as a judgement
        # nobody made.
        assert score_shadow.regrade_at_fire(**kwargs) == (None, None), why

class TestTheAlternateIsMeasuredToo:
    """Rule 5 has demanded two setups in every report since the beginning, and
    only the Primary was ever simulated -- so the shadow book was learning from
    half of what the system produces.

    Rule 7 records two real cases where the half nobody measured was the better
    trade: ANET's 179.80 level failed from a 179.80 entry and paid 2.54:1 from
    the Alternate's ~162, and MU's identical target went from 1.78:1 to 8.19:1.
    """

    NUMERIC_ALT = {"type": "Pullback", "trigger": 95.0, "stop": 92.0,
                    "atr_at_build": 2.0,
                    "targets": [{"price": 104.0, "pct": "40", "status": "pass"}]}

    def _bars(self, closes, built):
        from datetime import datetime, timezone
        start = datetime.fromisoformat(built[:10]).replace(tzinfo=timezone.utc).timestamp()
        return [{"time": start + i * 86400, "open": c, "high": c + 1,
                  "low": c - 1, "close": c} for i, c in enumerate(closes)]

    def _build(self, alternate):
        persistence.save_thesis(
            ticker="ABC", status="pending", source="SCREENER_v3",
            primary_setup={"type": "Breakout", "trigger": 100.0, "stop": 95.0,
                            "atr_at_build": 2.0,
                            "targets": [{"price": 115.0, "pct": "40", "status": "pass"}]},
            alternate_setup=alternate)
        return persistence.get_shadow_candidates()[0]

    def _run(self, monkeypatch, candidate, closes):
        score_shadow.clear_bars_cache()
        bars = self._bars(closes, candidate["built_at"])
        monkeypatch.setattr(score_shadow, "_fetch_bars", lambda t: _async(bars))
        monkeypatch.setattr(score_shadow, "_spy_bars", lambda: [])
        score_shadow.run_one(candidate)
        with persistence._db() as conn:
            return {r["setup_side"]: dict(r) for r in conn.execute(
                "SELECT * FROM shadow_outcomes ORDER BY id")}

    def test_both_setups_get_their_own_row(self, temp_db, monkeypatch):
        rows = self._run(monkeypatch, self._build(self.NUMERIC_ALT),
                          [96, 99, 101, 104, 103])
        assert set(rows) == {"primary", "alternate"}
        # and they are genuinely different trades: different triggers, so the
        # Alternate entered earlier and the Primary later
        assert rows["primary"]["trigger"] == 100.0
        assert rows["alternate"]["trigger"] == 95.0
        assert rows["alternate"]["entry"] != rows["primary"]["entry"]

    def test_a_prose_alternate_is_recorded_as_untestable_not_as_a_miss(
        self, temp_db, monkeypatch
    ):
        # Rule 5 explicitly allows a second setup with no cited level yet. Those
        # cannot fire, and counting them as "never fired" would fold every
        # undefined Alternate into the same bucket as real plans price never
        # reached -- dragging down the exact statistic the book exists to make.
        prose = {"type": "Pullback", "trigger": "a deeper flush, level not yet formed"}
        rows = self._run(monkeypatch, self._build(prose), [96, 99, 101, 104])
        assert rows["alternate"]["resolution"] == score_shadow.UNTESTABLE
        assert rows["alternate"]["resolution"] != "never_fired"
        assert "no numeric trigger" in rows["alternate"]["sim_note"]

    @pytest.mark.parametrize("missing,alt", [
        ("no alternate at all", None),
        ("no stop", {"type": "Pullback", "trigger": 95.0,
                      "targets": [{"price": 104.0}]}),
        # save_thesis already refuses to store a non-numeric target price, so
        # the storable version of "no priced target" is an empty list -- which
        # is also the common one: a real Alternate with a trigger and a stop and
        # nowhere identified to sell yet.
        ("no priced target", {"type": "Pullback", "trigger": 95.0, "stop": 92.0,
                               "targets": []}),
    ])
    def test_two_out_of_three_is_not_a_plan(self, temp_db, monkeypatch, missing, alt):
        rows = self._run(monkeypatch, self._build(alt), [96, 99, 101, 104])
        assert rows["alternate"]["resolution"] == score_shadow.UNTESTABLE, missing

    def test_an_untestable_alternate_is_not_asked_again_tomorrow(self, temp_db, monkeypatch):
        # It cannot become testable: this build's Alternate is fixed. Re-asking
        # nightly is how the book filled with repeats in the first place.
        self._run(monkeypatch, self._build(None), [96, 99, 101, 104])
        remaining = persistence.get_shadow_candidates()
        assert remaining and "alternate" in remaining[0]["finished_setup_sides"]

