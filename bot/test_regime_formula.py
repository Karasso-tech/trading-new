"""Unit tests for regime_formula.py (2026-07-20, CONSISTENCY_RULES.md rule 23).

Synthetic inputs only -- proves the scoring arithmetic itself is correct and
reproducible, same "arithmetic, not a live-data check" scope as
test_indicators_core.py.
"""

import pytest

from regime_formula import (
    IndexSnapshot,
    classify_regime,
    describe_regime_he,
    find_swing_highs,
    find_swing_lows,
)


def _snapshot(price, sma20, sma50, sma150, swing_highs, swing_lows, lookback_low):
    return IndexSnapshot(
        price=price, sma20=sma20, sma50=sma50, sma150=sma150,
        swing_highs=swing_highs, swing_lows=swing_lows, lookback_low=lookback_low,
    )


class TestFindSwingHighsLows:
    def test_finds_a_simple_ascending_sequence(self):
        # 3 bars either side required; construct 3 clean pivots.
        highs = [1, 2, 1, 2, 3, 2, 3, 4, 3, 2, 1]
        result = find_swing_highs(highs, n_pivot=1)
        assert result  # at least detects the local peaks

    def test_finds_a_simple_descending_low_sequence(self):
        lows = [5, 4, 5, 3, 2, 3, 1, 0, 1, 2, 3]
        result = find_swing_lows(lows, n_pivot=1)
        assert result

    def test_empty_input_returns_empty(self):
        assert find_swing_highs([]) == []
        assert find_swing_lows([]) == []


class TestClassifyRegime:
    def test_clean_uptrend_both_indexes_scores_risk_on(self):
        uptrend = _snapshot(
            price=110, sma20=105, sma50=100, sma150=95,
            swing_highs=[100, 103, 106], swing_lows=[95, 98, 101], lookback_low=80,
        )
        result = classify_regime(uptrend, uptrend)
        assert result.regime == "risk_on"
        assert result.score == 11
        assert result.components["agreement"] == 1

    def test_clean_downtrend_both_indexes_scores_risk_off_not_overstated(self):
        # Score is very negative (-9) but price hasn't broken the lookback low --
        # must NOT be overstated as structure_break.
        downtrend = _snapshot(
            price=90, sma20=95, sma50=100, sma150=105,
            swing_highs=[106, 103, 100], swing_lows=[101, 98, 95], lookback_low=80,
        )
        result = classify_regime(downtrend, downtrend)
        assert result.regime == "risk_off"
        assert result.structure_break_confirmed is False

    def test_severe_downtrend_with_a_real_break_confirms_structure_break(self):
        broken = _snapshot(
            price=70, sma20=95, sma50=100, sma150=105,
            swing_highs=[106, 103, 100], swing_lows=[101, 98, 95], lookback_low=75,
        )
        result = classify_regime(broken, broken)
        assert result.regime == "structure_break"
        assert result.structure_break_confirmed is True

    def test_disagreement_between_indexes_pulls_toward_neutral_choppy(self):
        uptrend = _snapshot(
            price=110, sma20=105, sma50=100, sma150=95,
            swing_highs=[100, 103, 106], swing_lows=[95, 98, 101], lookback_low=80,
        )
        downtrend = _snapshot(
            price=90, sma20=95, sma50=100, sma150=105,
            swing_highs=[106, 103, 100], swing_lows=[101, 98, 95], lookback_low=80,
        )
        result = classify_regime(uptrend, downtrend)
        assert result.components["agreement"] == -1
        assert result.regime == "neutral_choppy"

    def test_same_inputs_always_produce_the_same_result(self):
        # The entire point of rule 23 -- reproducibility.
        snap = _snapshot(
            price=100, sma20=99, sma50=98, sma150=97,
            swing_highs=[95, 97], swing_lows=[90, 92], lookback_low=80,
        )
        r1 = classify_regime(snap, snap)
        r2 = classify_regime(snap, snap)
        assert r1.regime == r2.regime
        assert r1.score == r2.score

    def test_mixed_ma_stack_is_not_forced_to_an_extreme(self):
        # Price above sma20 but sma20 below sma50 (mixed) -- score should sit
        # somewhere in the middle, not swing to a full +3 or -3.
        mixed = _snapshot(
            price=101, sma20=100, sma50=102, sma150=98,
            swing_highs=[95, 96], swing_lows=[90, 89], lookback_low=70,
        )
        result = classify_regime(mixed, mixed)
        assert -3 < result.components["spy_ma_stack"] < 3 or result.components["spy_ma_stack"] in (-1, 1)


class TestRegimeAdviceHe:
    """2026-08-03: market condition became advisory (explicit user direction --
    "a caution sign with a recommendation, not something absolute, since we
    don't know how it impacts the trades"). The wording must say which of the
    two it is, every time, and never claim a size change it no longer makes."""

    def test_choppy_is_advice_not_a_rule(self):
        text = describe_regime_he("neutral_choppy", score=-1)
        assert "המלצה (לא חוק" in text
        assert "לא מקטין את גודל הקנייה" in text

    def test_risk_off_says_it_is_a_rule(self):
        text = describe_regime_he("risk_off", score=-5)
        assert "כן חוק" in text
        assert "המלצה (לא חוק" not in text

    def test_structure_break_says_it_is_a_rule(self):
        assert "כן חוק" in describe_regime_he("structure_break")

    def test_supportive_regimes_still_get_a_note(self):
        for regime in ("risk_on", "healthy_uptrend", "pullback_in_uptrend"):
            text = describe_regime_he(regime)
            assert "מצב השוק כרגע" in text
            assert "המלצה (לא חוק" in text

    def test_unknown_label_returns_nothing_rather_than_inventing_advice(self):
        assert describe_regime_he("") == ""
        assert describe_regime_he("not_a_regime") == ""

    def test_every_classifiable_regime_has_advice(self):
        # A label the formula can return but the advice table doesn't cover
        # would silently drop the whole block from that day's report.
        for regime in ("risk_on", "healthy_uptrend", "pullback_in_uptrend",
                       "neutral_choppy", "risk_off", "structure_break"):
            assert describe_regime_he(regime) != ""
