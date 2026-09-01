"""Unit tests for the live re-grade's entry price (_score_setup, 2026-08-08).

Rule 27 re-scores a stored setup against live numbers so a decayed idea can't
keep looking tradeable. It was still measuring reward:risk from the STORED
trigger, though -- so an idea that fired and then ran was graded on a trade the
user could no longer get, and the order kept printing. These cover the fix.

Pure arithmetic over a setup dict: no fetch, no DB, no TradingView.
"""

import fetch_monitor_data as fmd

# Grade-friendly background so the entry price is the only thing under test:
# supportive regime, strong RS, price near its SMA20, no earnings in the window.
BASE = dict(atr14=2.0, dist_sma20_atr=0.5, regime="Risk-On",
            rs_5d=5.0, rs_20d=5.0, earnings_days_out=30)


def _setup(trigger=100.0, stop=97.0, target=108.0, type_="Breakout"):
    return {"type": type_, "trigger": trigger, "stop": stop,
            "atr_at_build": 2.0, "targets": [{"price": target}]}


def test_before_the_trigger_fires_the_plan_is_scored_as_planned():
    # Price still under the trigger: the entry hasn't happened, so the stored
    # plan IS what would be ordered. Nothing may move.
    r = fmd._score_setup(_setup(), live_close=98.0, **BASE)
    assert r["entry_is_live_close"] is False
    assert r["entry_used"] == 100.0
    assert r["rr"] == 8.0 / 3.0


def test_a_run_past_the_trigger_is_scored_at_the_real_entry():
    # The documented case: buy 100 / stop 97 / target 108 fires and closes at
    # 102. The real order is 5 of risk against 6 of reward, not 3 against 8.
    r = fmd._score_setup(_setup(), live_close=102.0, **BASE)
    assert r["entry_is_live_close"] is True
    assert r["planned_entry"] == 100.0
    assert r["entry_used"] == 102.0
    assert r["rr"] == 6.0 / 5.0


def test_the_worse_reward_risk_is_what_drops_the_grade():
    # The whole point: this must reach the LETTER, because the letter is what
    # MONITOR_v2's D-block reads. Same setup, same day, only the run-up differs.
    fresh = fmd._score_setup(_setup(), live_close=100.05, **BASE)
    chased = fmd._score_setup(_setup(), live_close=104.0, **BASE)
    assert fresh["criteria"]["rr"] is True and fresh["grade"] == "A"
    assert chased["criteria"]["rr"] is False and chased["grade"] == "B"


def test_a_run_up_costs_exactly_one_criterion_not_a_hard_block():
    # Deliberate, and worth pinning: R:R is 1 of 5 scored criteria, so a chase
    # drops the grade one letter rather than banning the trade outright. Only a
    # setup already weak on the other four falls to D and gets blocked. A gate
    # that fired on every confirmed breakout would just get ignored -- the
    # regime criterion was removed on 2026-08-02 for exactly that reason.
    chased = fmd._score_setup(_setup(), live_close=104.0, **BASE)
    weak = fmd._score_setup(_setup(), live_close=104.0,
                            **{**BASE, "rs_5d": -1.0, "rs_20d": -1.0,
                               "dist_sma20_atr": 3.0})
    assert chased["grade"] == "B"
    assert weak["grade"] == "D"


def test_price_past_the_target_leaves_no_reward():
    # abs() used to report this as a big reward -- the further past the target
    # price ran, the better the trade scored. There is no trade left here.
    r = fmd._score_setup(_setup(), live_close=110.0, **BASE)
    assert r["rr"] == 0.0
    assert r["criteria"]["rr"] is False
    assert r["criteria"]["target_atr"] is False


def test_target_distance_is_measured_from_the_real_entry_too():
    # Not just R:R: "is the target far enough to be worth the trip" is also a
    # question about the entry you actually get. atr14 is 2.0, so a 6.0 reward
    # is 3.0x.
    r = fmd._score_setup(_setup(), live_close=102.0, **BASE)
    assert r["target_atr_multiple"] == 3.0


def test_no_live_close_falls_back_to_the_stored_plan():
    # Missing data must never be treated as "price is at the trigger" -- it is
    # simply not knowable, so the stored plan stands.
    r = fmd._score_setup(_setup(), live_close=None, **BASE)
    assert r["entry_is_live_close"] is False
    assert r["rr"] == 8.0 / 3.0


def test_every_setup_type_fires_the_same_way():
    # Breakout/Pullback/Reclaim/Retest are all names for where the level came
    # from. All four confirm on a daily close ABOVE the trigger, so all four
    # get re-priced the same way -- a Pullback is not an order waiting below.
    for type_ in ("Breakout", "Pullback", "Reclaim", "Retest"):
        r = fmd._score_setup(_setup(type_=type_), live_close=102.0, **BASE)
        assert r["entry_used"] == 102.0, type_


def test_a_malformed_setup_is_not_repriced():
    # Target under the stop is not a short, it is bad data. Guessing a direction
    # for it would be inventing the trade -- left on the original arithmetic.
    r = fmd._score_setup(_setup(target=90.0), live_close=102.0, **BASE)
    assert r["entry_is_live_close"] is False


def test_a_setup_with_no_target_still_reports_its_reason():
    # Unchanged behaviour, guarded so the new code path can't swallow it.
    r = fmd._score_setup({"type": "Breakout", "trigger": 100.0, "stop": 97.0, "targets": []},
                         live_close=102.0, **BASE)
    assert r["grade"] is None
    assert r["reason"] == "no_target_to_score"
