"""Tests for level_picker.py -- the stop, the targets and the potential.

Each test pins one clause of one already-written rule. That is deliberate: the
rules are the specification, and a silent change to any of them should break a
named test rather than quietly change every future trade's numbers.
"""

import pytest

import decision_policy
import level_picker as lp

ATR = 2.0

LOWS = [
    {"date": "2026-07-01", "price": 90.0},
    {"date": "2026-07-15", "price": 96.0},
    {"date": "2026-07-28", "price": 99.7},   # too close to a 100 trigger
]


class TestPickStop:
    def test_takes_the_nearest_low_that_clears_the_noise_floor(self):
        # 99.7 is only 0.3 below a 100 entry -- inside daily noise (rule 4), so
        # the next real low back is the one that counts. 96.0 - 0.3 buffer = 95.7,
        # which is 2.15x ATR away.
        r = lp.pick_stop(trigger=100.0, atr14=ATR, swing_lows=LOWS)
        assert r.basis_level == 96.0
        assert r.stop == pytest.approx(95.7)
        assert r.distance_atr >= lp.NOISE_FLOOR_ATR

    def test_the_stop_sits_below_the_level_never_at_it(self):
        # Rule 24: a stop set exactly at the low gets filled by any wick that
        # merely touches the level.
        r = lp.pick_stop(trigger=100.0, atr14=ATR, swing_lows=LOWS)
        assert r.stop < r.basis_level
        assert r.buffer == pytest.approx(lp.STOP_BUFFER_ATR * ATR)

    def test_no_usable_structure_falls_back_to_a_labelled_distance(self):
        """2026-08-10. Returning no stop at all left the setup unsizeable and
        the trade undecidable. A stop still has to exist, so it falls back to a
        plain distance -- and says so, with its own basis kind, precisely so a
        trade standing on nothing is countable later instead of looking like
        every other trade in the book."""
        r = lp.pick_stop(trigger=100.0, atr14=ATR,
                          swing_lows=[{"date": "d", "price": 99.9}])
        assert r.stop == pytest.approx(100.0 - lp.NO_STRUCTURE_ATR * ATR)
        assert r.basis_kind == lp.BASIS_NO_STRUCTURE
        assert r.basis_level is None            # never pretends a level backs it
        assert "no structure to stand on" in r.reason

    def test_lows_above_the_entry_are_ignored(self):
        r = lp.pick_stop(trigger=100.0, atr14=ATR,
                          swing_lows=[{"date": "d", "price": 120.0}] + LOWS)
        assert r.basis_level == 96.0

    def test_no_atr_is_refused_rather_than_divided_by(self):
        assert lp.pick_stop(100.0, 0.0, LOWS).stop is None


def _wall(top, touches=3, is_wall=True):
    return {"is_wall": is_wall, "top": top, "bottom": top - 0.5,
            "touches": [{"date": "2026-0%d-01" % (i + 1), "price": top}
                        for i in range(touches)]}


class TestPickTargets:
    def test_a_level_past_the_gate_becomes_a_target(self):
        # entry 100, stop 96 -> risk 4. 112 is 6x ATR up and pays 3:1.
        scan = lp.pick_targets(100.0, 96.0, ATR, [_wall(112.0)])
        assert len(scan.targets) == 1
        assert scan.targets[0].price == 112.0
        assert scan.targets[0].rr == pytest.approx(3.0)

    def test_a_level_too_close_becomes_a_checkpoint_not_nothing(self):
        # Rule 3: a level failing either check is a Checkpoint, and rule 14 says
        # it is still reported rather than silently dropped.
        scan = lp.pick_targets(100.0, 96.0, ATR, [_wall(102.0)])
        assert scan.targets == []
        assert [c.price for c in scan.checkpoints] == [102.0]

    def test_the_stricter_near_band_is_enforced(self):
        # 1.0-1.5x ATR needs 2.5:1, not 2:1. At entry 100 / stop 99, 102.5 is
        # 1.25x ATR and pays exactly 2.5 -- it passes; 102.2 pays 2.2 and does not.
        assert lp.pick_targets(100.0, 99.0, ATR, [_wall(102.5)]).targets
        assert not lp.pick_targets(100.0, 99.0, ATR, [_wall(102.2)]).targets

    def test_allocation_follows_rule_7(self):
        one = lp.pick_targets(100.0, 96.0, ATR, [_wall(112.0)])
        assert [t.pct for t in one.targets] == [40.0]
        assert one.runner_pct == 60.0
        two = lp.pick_targets(100.0, 96.0, ATR, [_wall(112.0), _wall(130.0)])
        assert [t.pct for t in two.targets] == [40.0, 35.0]
        assert two.runner_pct == 25.0

    def test_never_more_than_two_sellable_targets(self):
        scan = lp.pick_targets(100.0, 96.0, ATR,
                                [_wall(112.0), _wall(130.0), _wall(150.0)])
        assert len(scan.targets) == lp.MAX_TARGETS
        assert 150.0 in [c.price for c in scan.checkpoints]

    def test_the_wall_top_is_used_never_a_level_inside_it(self):
        # Rule 11's recursive step, satisfied by construction: the chaining ran
        # over every swing high at once, so a level that belongs to a wall is
        # inside that chain and is never offered on its own. This is the real
        # PLTR miss -- 157.78 looked valid but 163.70 was the true top.
        wall = {"is_wall": True, "bottom": 157.78, "top": 163.70,
                "touches": [{"date": "2026-05-01", "price": 157.78},
                            {"date": "2026-05-20", "price": 160.0},
                            {"date": "2026-06-02", "price": 163.70}]}
        scan = lp.pick_targets(100.0, 96.0, ATR, [wall])
        assert scan.targets[0].price == 163.70

    def test_an_empty_swing_scan_is_reported_as_UNFINISHED_not_as_no_target(self):
        """Rule 12: "skipping straight to 'no target' without checking them is
        itself a miss." Found for real on 2026-08-09 -- the first full run over
        the pending list returned 8 No Trades out of 16, every one of them from a
        scan that had only looked at swing highs, which is one source of five."""
        scan = lp.pick_targets(100.0, 96.0, ATR, [_wall(90.0)])
        assert scan.targets == []
        assert scan.complete is False
        assert "STILL UNCHECKED" in scan.note
        assert lp.SOURCE_SWING in scan.sources_checked

    def test_the_other_mechanical_sources_are_tried_when_swings_fail(self):
        # A base then a run: the measured move projects a level the swing highs
        # alone never offered.
        bars = [{"date": "2026-06-%02d" % (i + 1), "open": p, "high": p + 1,
                 "low": p - 1, "close": p}
                for i, p in enumerate([80, 80.5, 79.8, 80.2, 80.6, 79.9, 80.3,
                                        80.1, 85, 90, 95, 100])]
        scan = lp.pick_targets(100.0, 96.0, ATR, [_wall(90.0)], bars=bars,
                                swing_lows=[{"date": "d", "price": 79.8}])
        assert lp.SOURCE_MEASURED_MOVE in scan.sources_checked

    def test_a_clean_swing_target_never_drags_in_projected_levels(self):
        # The extra sources are a fallback, not clutter added to every report.
        scan = lp.pick_targets(100.0, 96.0, ATR, [_wall(112.0)], bars=[{"date": "d",
                "open": 100, "high": 101, "low": 99, "close": 100}] * 12)
        assert scan.sources_checked == [lp.SOURCE_SWING]
        assert scan.complete is True


class TestMovementPotential:
    def _bars(self, prices):
        return [{"date": "2026-06-%02d" % (i + 1), "open": p, "high": p + 0.2,
                 "low": p - 0.2, "close": p} for i, p in enumerate(prices)]

    def test_measures_the_base_before_the_move(self):
        bars = self._bars([100, 100.5, 99.8, 100.2, 100.6, 99.9, 100.3, 100.1,
                            103, 106, 109])
        p = lp.movement_potential(bars, breakout_level=110.0, atr14=ATR)
        assert p.price is not None
        assert p.base_high is not None and p.base_low is not None
        # Measured move: the base's height added on top of the breakout level.
        assert p.price == pytest.approx(110.0 + (p.base_high - p.base_low))

    def test_no_base_is_said_out_loud_not_skipped(self):
        # Rule 17: when there is genuinely nothing to measure from, that is
        # written down explicitly rather than passed over in silence.
        bars = self._bars([100, 110, 120, 130, 140, 150])
        p = lp.movement_potential(bars, breakout_level=150.0, atr14=ATR)
        assert p.price is None
        assert "no sideways base" in p.note

    def test_no_bars_is_not_a_crash(self):
        assert lp.movement_potential([], 100.0, ATR).price is None


class TestRejectionReasons:
    BASE = dict(has_target=True, rr=3.0, grade="B", regime="pullback_in_uptrend",
                rs_delta_pct=2.0, dist_sma20_atr=0.5, earnings_days_out=30,
                trigger_fired=True, stop=95.0)

    def test_a_clean_buy_has_no_reasons(self):
        assert lp.rejection_reasons(**self.BASE) == []

    def test_each_gate_emits_its_own_stable_token(self):
        cases = [
            ({"has_target": False}, "no_qualifying_target"),
            ({"regime": "risk_off"}, "regime_against"),
            ({"grade": "D"}, "grade_below_c"),
            ({"rs_delta_pct": -1.0}, "rs_weaker_than_market"),
            ({"dist_sma20_atr": 3.0}, "extended_vs_sma20"),
            ({"earnings_days_out": 3}, "earnings_inside_window"),
            ({"earnings_days_out": None}, "earnings_unverified"),
            ({"trigger_fired": False}, "trigger_not_fired"),
            ({"stop": None}, "no_usable_stop"),
        ]
        for override, token in cases:
            merged = dict(self.BASE)
            merged.update(override)
            assert token in lp.rejection_reasons(**merged), token

    def test_the_tokens_survive_the_plain_words_translator(self):
        # decision_policy.explain_reasons matches these by substring, so a
        # renamed token would silently fall through to the generic fallback and
        # the user would stop being told why anything was rejected.
        merged = dict(self.BASE)
        merged.update({"has_target": False, "regime": "risk_off", "grade": "D",
                       "rs_delta_pct": -1.0, "trigger_fired": False})
        for token in lp.rejection_reasons(**merged):
            assert decision_policy.explain_reasons([token]) != [decision_policy.FALLBACK_REASON], token


class TestStopIsNeverAboveTheCurrentPrice:
    """Found on the first live run, 2026-08-09. AMD was trading at 483.36 and
    the plan came back with a trigger of 530.13 and a stop of 492.41 -- taken
    from a 498.15 shelf dated a month earlier that price had since broken
    straight down through. A stop above where the stock is trading today is a
    stop that is violated before the trade is ever entered.

    Rule 2's own test is "does price have to pass through this level to reach
    the trigger?" The honest reading of a level price is already underneath is
    that the trade does not depend on it any more -- it already lost it."""

    REAL_AMD = [
        {"date": "2026-07-08", "price": 498.15},   # the stale shelf
        {"date": "2026-07-25", "price": 455.30},   # a low still under the price
    ]

    def test_a_low_above_the_current_price_is_skipped(self):
        r = lp.pick_stop(trigger=530.13, atr14=38.27, swing_lows=self.REAL_AMD,
                          current_price=483.36)
        assert r.basis_level == 455.30
        assert r.stop < 483.36

    def test_without_a_current_price_the_old_behaviour_is_unchanged(self):
        # The parameter is optional, and a caller that has no live price still
        # gets the nearest structure under the trigger.
        r = lp.pick_stop(trigger=530.13, atr14=38.27, swing_lows=self.REAL_AMD)
        assert r.basis_level == 498.15

    def test_only_broken_levels_available_is_said_out_loud(self):
        r = lp.pick_stop(trigger=530.13, atr14=38.27,
                          swing_lows=[{"date": "d", "price": 498.15}],
                          current_price=483.36)
        assert r.stop is None
        assert "already broken" in r.reason

    def test_a_broken_level_still_falls_back_rather_than_refusing(self):
        too_tight = lp.pick_stop(trigger=100.0, atr14=ATR,
                                  swing_lows=[{"date": "d", "price": 99.9}],
                                  current_price=99.95)
        assert too_tight.basis_kind == lp.BASIS_NO_STRUCTURE
        assert "no structure to stand on" in too_tight.reason
