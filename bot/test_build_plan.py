"""End-to-end tests for build_plan.py -- the whole mechanical half of a screener
run, from a fetch payload to a finished plan.

These use a synthetic payload shaped exactly like fetch_analysis_data.py's real
output. No fetch, no TradingView, no database.
"""

import pytest

import build_plan
import persistence
import decision_policy
import setup_types

ATR = 2.0


def _bars(prices):
    half = ATR * 0.5
    return [{"date": "2026-06-%02d" % (i + 1), "open": p, "high": p + half,
             "low": p - half, "close": p} for i, p in enumerate(prices)]


def payload(**overrides):
    # A long flat base, then a climb, with a three-touch wall overhead and real
    # structural lows underneath: the ordinary shape of a breakout candidate.
    prices = [100.0] * 12 + [100.5, 101.0, 101.5, 102.0, 102.5, 103.0,
                              103.5, 104.0, 104.5, 105.0]
    data = {
        "ticker": "TEST",
        "atr14": ATR,
        "sma20": 98.0, "sma50": 95.0,
        "dist_sma20_atr": 1.1,
        "current_price": 105.0,
        "rs_20d_vs_spy": 3.4, "rs_5d_vs_spy": 1.1,
        "volume_pct_of_avg": 130.0,
        "earnings_days_out": 25, "earnings_verified": True,
        "market_regime_formula": {"regime": "pullback_in_uptrend"},
        "recent_bars_40": _bars(prices),
        # Two walls on purpose: the near one at 110 is what a breakout has to
        # close above (rule 11), the far one at 130 is what there is to sell
        # into. A payload with only a distant wall has no tradeable setup at
        # all -- the first draft of this fixture had exactly that and every
        # assertion below failed for that reason rather than for a real one.
        "wall_chains": [
            {"is_wall": True, "top": 110.0, "bottom": 109.2,
             "touches": [{"date": "2026-03-02", "price": 109.2},
                         {"date": "2026-03-20", "price": 109.7},
                         {"date": "2026-04-10", "price": 110.0}]},
            {"is_wall": True, "top": 130.0, "bottom": 129.1,
             "touches": [{"date": "2026-01-08", "price": 129.1},
                         {"date": "2026-01-29", "price": 129.6},
                         {"date": "2026-02-14", "price": 130.0}]},
        ],
        # Both below the current price of 105 on purpose. A structural low ABOVE
        # where the stock trades today has already failed, and pick_stop now
        # skips it -- the first draft of this fixture used 106.0 and the stop
        # silently vanished once that guard landed.
        "swing_lows_recent": [{"date": "2026-05-20", "price": 99.0},
                               {"date": "2026-06-05", "price": 103.0}],
        "account": {"portfolio_heat": {"heat_pct": 3.1, "cap_pct": 6.0}},
    }
    data.update(overrides)
    return data


class TestTheChainHoldsTogether:
    def test_a_plan_comes_out_complete(self):
        plan = build_plan.build(payload())
        setup = plan["primary_setup"]
        assert setup["type"] in setup_types.SETUP_TYPES
        assert setup["trigger"] is not None
        assert setup["stop"] is not None
        assert setup["stop"] < setup["trigger"]
        assert setup["atr_at_build"] == ATR

    def test_the_setup_type_is_always_a_word_save_thesis_will_accept(self):
        # A label this chain invents would be refused at write time and the
        # whole run would be lost.
        plan = build_plan.build(payload())
        assert setup_types.require(plan["primary_setup"]["type"]) == plan["primary_setup"]["type"]

    def test_the_stop_carries_its_basis_level_for_the_rule_24_check(self):
        plan = build_plan.build(payload())
        setup = plan["primary_setup"]
        assert setup["stop_basis_level"] is not None
        assert setup["stop"] < setup["stop_basis_level"]

    def test_the_decision_ceiling_is_reported_never_decided(self):
        plan = build_plan.build(payload())
        assert plan["max_allowed_decision"] in decision_policy.ALL_DECISIONS

    def test_no_qualifying_target_caps_the_decision_at_no_trade(self):
        # decision_policy: "nowhere to sell" outranks everything else, because a
        # hostile regime describes a trade you should not take YET while no
        # target describes a trade that does not exist.
        plan = build_plan.build(payload(wall_chains=[]))
        assert plan["primary_setup"]["targets"] == []
        assert plan["max_allowed_decision"] == decision_policy.NO_TRADE

    def test_a_hostile_regime_caps_the_decision_at_watchlist(self):
        plan = build_plan.build(payload(
            market_regime_formula={"regime": "risk_off"}))
        assert plan["max_allowed_decision"] == decision_policy.WATCHLIST
        assert "regime_against" in plan["rejection_reasons"]


class TestRuleFifteenWindow:
    def test_a_reversal_is_scored_on_five_days_not_twenty(self):
        # A stock that fell under its averages and closed back above them.
        prices = [104, 102, 101, 100.5, 102, 104.5]
        plan = build_plan.build(payload(
            recent_bars_40=_bars(prices), sma20=102.0, sma50=103.0,
            current_price=104.5,
            # No structural low inside the recent range, so the SMA reclaim is
            # the only reversal shape that can match. With one in range this
            # same data is ALSO a genuine failed breakdown of that level -- both
            # readings are reversals and both score on 5 days, but the test
            # should test one thing.
            swing_lows_recent=[{"date": "2026-04-01", "price": 90.0}],
            wall_chains=[{"is_wall": True, "top": 140.0, "bottom": 139.0,
                          "touches": [{"date": "2026-01-01", "price": 140.0}] * 3}],
        ))
        assert plan["setup_call"]["setup_type"] == "Reclaim"
        assert plan["setup_call"]["rs_window_days"] == 5
        assert plan["setup_call"]["rs_delta_pct"] == 1.1     # the 5-day figure

    def test_a_trend_setup_is_scored_on_twenty_days(self):
        plan = build_plan.build(payload())
        assert plan["setup_call"]["rs_window_days"] == 20
        assert plan["setup_call"]["rs_delta_pct"] == 3.4


class TestOverride:
    def test_an_override_without_a_reason_is_refused(self):
        # Same rule as a market-state override (rule 23): an override with no
        # stated reason is not allowed. That is the whole guard.
        with pytest.raises(ValueError, match="written reason"):
            build_plan.build(payload(), setup_type_override="Pullback")

    def test_an_override_with_a_reason_is_applied_and_stays_visible(self):
        plan = build_plan.build(payload(), setup_type_override="Pullback",
                                 override_reason="the wall is a stale gap edge")
        assert plan["primary_setup"]["type"] == "Pullback"
        assert "OVERRIDDEN" in plan["setup_call"]["note"]
        assert "stale gap edge" in plan["setup_call"]["note"]


class TestDisclosureFlags:
    def test_flags_are_derived_from_the_facts_not_chosen(self):
        plan = build_plan.build(payload(
            market_regime_formula={"regime": "neutral_choppy"},
            volume_pct_of_avg=80.0, earnings_verified=False,
            account={"portfolio_heat": {"heat_pct": 7.0, "cap_pct": 6.0}},
        ))
        flags = plan["summary_inputs"]["disclosure_flags"]
        assert {"regime", "volume", "event", "heat"} <= set(flags)

    def test_a_clean_setup_raises_no_spurious_flags(self):
        plan = build_plan.build(payload())
        assert "heat" not in plan["summary_inputs"]["disclosure_flags"]
        assert "event" not in plan["summary_inputs"]["disclosure_flags"]

    def test_every_flag_has_a_line_to_print(self):
        # A flag with no matching sentence would vanish silently from the
        # message instead of warning anybody.
        import summary_text
        plan = build_plan.build(payload(
            market_regime_formula={"regime": "neutral_choppy"},
            volume_pct_of_avg=80.0, earnings_verified=False,
            account={"portfolio_heat": {"heat_pct": 7.0, "cap_pct": 6.0}},
        ))
        for flag in plan["summary_inputs"]["disclosure_flags"]:
            assert flag in summary_text.DISCLOSURE_LINES, flag


class TestMissingData:
    def test_no_atr_produces_an_honest_empty_plan_not_a_crash(self):
        plan = build_plan.build(payload(atr14=None))
        assert plan["primary_setup"]["trigger"] is None
        assert plan["primary_setup"]["stop"] is None
        assert plan["max_allowed_decision"] == decision_policy.NO_TRADE

    def test_no_bars_is_not_a_crash(self):
        plan = build_plan.build(payload(recent_bars_40=[]))
        assert plan["primary_setup"]["type"] is None

    def test_every_stop_records_which_kind_of_structure_backs_it(self):
        # Recorded on every trade from 2026-08-10 so the shadow book can one day
        # answer whether stops on recent structure beat stops on old structure.
        setup = build_plan.build(payload())["primary_setup"]
        assert setup["stop"] is not None
        assert setup["stop_basis_kind"] in {
            "recent_higher_low", "consolidation_shelf", "gap_edge",
            "major_swing_low", "no_structure_distance_only",
        }

    def test_the_nearest_structure_wins_over_an_older_deeper_one(self):
        # Rule 2's own test: price does not have to pass through a five-month-old
        # low to reach a trigger it is already far above. That is exactly the
        # stop MMM was given on 2026-08-10 -- 12% away, from February.
        plan = build_plan.build(payload(
            swing_lows_recent=[{"date": "2026-02-18", "price": 60.0},
                                {"date": "2026-07-28", "price": 103.0}]))
        assert plan["primary_setup"]["stop_basis_level"] == 103.0


class TestTheMessageCanBeBuiltFromThePlan:
    def test_summary_inputs_feed_straight_into_the_template(self):
        import summary_text
        plan = build_plan.build(payload())
        text = summary_text.build(
            "Buy Only If Confirmed",
            thesis_sentence="משפט.", alternate=None,
            qty=100, cost_usd=10000.0, heat_after_pct=4.0, heat_cap_pct=6.0,
            cash_available_usd=50000.0, **plan["summary_inputs"],
        )
        assert "TEST" in text
        assert text.count(summary_text.SEP) == 6


class TestBuyNowNeedsAFiredTrigger:
    """decision_policy deliberately does not rank Buy Now against Buy Only If
    Confirmed -- it is not given the one fact that separates them. build_plan IS
    given it. On the first live run, ORCL came back with a ceiling of "Buy Now"
    beside a rejection reason of "trigger_not_fired": the same answer
    disagreeing with itself, and a trap for whoever read it next."""

    def test_an_unfired_trigger_caps_at_buy_only_if_confirmed(self):
        plan = build_plan.build(payload())          # price 105, trigger 110
        assert "trigger_not_fired" in plan["rejection_reasons"]
        assert plan["max_allowed_decision"] == decision_policy.BUY_IF_CONFIRMED

    def test_a_fired_trigger_still_allows_buy_now(self):
        # A settled daily close at or above the trigger -- the same standard
        # MONITOR_v2 uses for a green, never an intraday touch.
        plan = build_plan.build(payload(current_price=112.0))
        assert "trigger_not_fired" not in plan["rejection_reasons"]
        assert plan["max_allowed_decision"] == decision_policy.BUY_NOW

    def test_the_cap_never_strengthens_a_weaker_ceiling(self):
        plan = build_plan.build(payload(
            current_price=112.0, market_regime_formula={"regime": "risk_off"}))
        assert plan["max_allowed_decision"] == decision_policy.WATCHLIST


class TestPotentialBelowTheTargets:
    """Seen live on ORCL, 2026-08-09: the movement potential came out at 167.40
    while target 2 sat at 189.18. Both numbers are honest -- rule 17's potential
    is the base's own measured move, the targets are resistance levels overhead
    -- but printed one under the other they read as a contradiction, and this
    system's reader is explicitly a beginner."""

    def test_the_clash_is_noted_not_hidden(self):
        plan = build_plan.build(payload())
        pot = plan["potential"]
        if pot["price"] is not None and plan["primary_setup"]["targets"]:
            furthest = plan["primary_setup"]["targets"][-1]["price"]
            if pot["price"] < furthest:
                assert pot["note"] and "different questions" in pot["note"]

    def test_the_number_itself_is_never_stretched_to_cover_the_targets(self):
        # Making the potential reach the targets would be inventing a number --
        # rule 1's "no invented price levels" applies to this field too.
        plan = build_plan.build(payload())
        pot = plan["potential"]
        if pot["price"] is not None:
            assert pot["price"] == pytest.approx(
                plan["primary_setup"]["trigger"] + (pot["base_high"] - pot["base_low"]))


class TestRulesTheFirstVersionMissed:
    """A full re-read of all 30 rules on 2026-08-09, at the owner's insistence,
    after he asked whether "the two nearest levels that pay 2-to-1" was really
    the rule. It was not, and five more gaps came out of the same pass."""

    def test_rule_7_the_alternate_gets_its_own_target_scan(self):
        # "Every setup shown gets this analysis independently -- Alternate is
        # not exempt just because it's second or still pending."
        plan = build_plan.build(payload(), alt_trigger=104.0, alt_stop=99.0,
                                 alt_type="Pullback")
        alt = plan["alternate_setup"]
        assert alt is not None
        assert alt["type"] == "Pullback"
        assert "targets" in alt and "checkpoints" in alt
        assert alt["trigger"] == 104.0 and alt["stop"] == 99.0

    def test_rule_7_a_missing_alternate_is_named_not_ignored(self):
        plan = build_plan.build(payload())
        assert plan["alternate_setup"] is None
        assert "rule 5 requires two setups" in plan["alternate_note"]

    def test_rule_7_the_alternate_is_scanned_from_its_own_entry(self):
        # The rule's own example: the identical level fails from a high entry
        # and passes from a deeper one.
        high = build_plan.build(payload(), alt_trigger=125.0, alt_stop=99.0)
        deep = build_plan.build(payload(), alt_trigger=104.0, alt_stop=99.0)
        assert high["alternate_setup"]["targets"] != deep["alternate_setup"]["targets"]

    def test_rule_8_a_core_holding_is_exempt_from_everything(self):
        plan = build_plan.build(payload(sleeve="core"))
        assert plan["core_exempt"] is True
        assert plan["primary_setup"] is None
        assert "exempt from rules 1-7" in plan["note"]

    def test_rule_8_a_swing_holding_is_not_exempt(self):
        assert "core_exempt" not in build_plan.build(payload(sleeve="swing"))

    def test_rule_15_a_reversal_reports_both_windows(self):
        # "a reversal widget or report with only one RS number is itself an
        # incomplete render"
        plan = build_plan.build(payload(
            recent_bars_40=_bars([104, 102, 101, 100.5, 102, 104.5]),
            sma20=102.0, sma50=103.0, current_price=104.5,
            swing_lows_recent=[{"date": "2026-04-01", "price": 90.0}]))
        both = plan["setup_call"]["rs_both_windows"]
        assert both is not None
        assert both["rs_5d_vs_spy"] == 1.1 and both["rs_20d_vs_spy"] == 3.4

    def test_rule_15_a_trend_setup_reports_only_its_own_window(self):
        plan = build_plan.build(payload())
        assert plan["setup_call"]["rs_both_windows"] is None

    def test_rule_16_an_empty_scan_cites_the_window_it_searched(self):
        # "'No data above this level' is a claim that must be earned."
        # Keep the near wall so a setup and a stop still exist -- drop only the
        # far one, so the scan actually RUNS and finds nothing. With no walls at
        # all there is no setup either, and the note would be empty for a
        # different reason entirely.
        plan = build_plan.build(payload(
            wall_chains=[{"is_wall": True, "top": 110.0, "bottom": 109.2,
                          "touches": [{"date": "2026-03-02", "price": 109.2},
                                      {"date": "2026-03-20", "price": 109.7},
                                      {"date": "2026-04-10", "price": 110.0}]}],
            coverage={"date_start": "2024-08-09", "date_end": "2026-08-09"}))
        note = plan["target_note"]
        assert note and "2024-08-09 to 2026-08-09" in note

    def test_rule_26_the_healthy_uptrend_figure_is_surfaced(self):
        plan = build_plan.build(payload(
            market_regime_formula={"regime": "healthy_uptrend"}))
        assert plan["setup_call"]["setup_type"] == "Breakout"
        assert "-0.12R" in plan["rule_26_disclosure"]

    def test_rule_26_does_not_apply_to_other_setup_types(self):
        # Its scope is stated and narrow: Breakout and Retest only.
        assert build_plan._rule_26_disclosure("Reclaim", "healthy_uptrend") is None

    def test_rule_26_does_not_apply_in_other_regimes(self):
        assert build_plan._rule_26_disclosure("Breakout", "neutral_choppy") is None


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """An isolated database -- rule 25 is the only part of this module that
    reads one, and it must never touch the real trading_new.db."""
    monkeypatch.setattr(persistence, "DB_PATH", tmp_path / "test.db")
    persistence.init_db()
    return tmp_path / "test.db"


class TestRule25IsSwitchedOff:
    """Rule 25's past-lesson injection was switched off on 2026-08-10, on the
    owner's reasoning: a lesson is written from ONE closed trade, and feeding it
    back into a decision means the shadow book can never separate "the rules
    worked" from "the owner remembered something". Collecting clean data now is
    worth more than a hint today.

    Rule 26 survives the same argument because it rests on 305 backtested trades
    rather than one."""

    def test_no_past_lessons_ride_along_with_the_plan(self):
        assert "past_lessons" not in build_plan.build(payload())

    def test_the_plan_never_touches_the_journal_tables(self):
        # The strong form: it is not merely absent from the output, the lookup
        # does not happen at all.
        assert not hasattr(build_plan, "_past_lessons")

    def test_rule_26_is_untouched_because_its_sample_is_real(self):
        plan = build_plan.build(payload(
            market_regime_formula={"regime": "healthy_uptrend"}))
        assert "-0.12R" in plan["rule_26_disclosure"]

    def test_the_screener_prompt_tells_the_model_not_to_look_them_up(self):
        import process_queue
        out = process_queue._SCREENER_PROMPT_TEMPLATE.format(
            ticker="X", update_id=1, date="2026-08-10")
        assert "Do NOT look up past lessons" in out
