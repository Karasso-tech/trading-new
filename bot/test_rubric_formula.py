"""Unit tests for rubric_formula.py (2026-07-29, CONSISTENCY_RULES.md rule 27).

Synthetic inputs only, plus one real-world regression fixture -- same
"arithmetic, not a live-data check" scope as test_regime_formula.py.
"""

from rubric_formula import RubricInputs, classify_rubric


def _inputs(rr=3.0, target_atr=2.0, regime="healthy_uptrend", rs=5.0,
            dist_sma20_atr=0.5, earnings_days_out=20):
    return RubricInputs(
        rr=rr, target_atr_multiple=target_atr, regime=regime,
        rs_delta_pct=rs, dist_sma20_atr=dist_sma20_atr,
        earnings_days_out=earnings_days_out,
    )


class TestIndividualCriteria:
    def test_all_five_scored_criteria_pass_grades_a(self):
        result = classify_rubric(_inputs())
        assert result.score == 5
        assert result.grade == "A"
        assert all(result.criteria.values())

    def test_rr_below_threshold_loses_the_point(self):
        result = classify_rubric(_inputs(rr=2.29))
        assert result.criteria["rr"] is False
        assert result.score == 4
        assert result.grade == "B"

    def test_rr_at_exact_threshold_passes(self):
        result = classify_rubric(_inputs(rr=2.3))
        assert result.criteria["rr"] is True

    def test_target_atr_below_threshold_loses_the_point(self):
        result = classify_rubric(_inputs(target_atr=1.49))
        assert result.criteria["target_atr"] is False

    def test_neutral_choppy_regime_does_not_score(self):
        # Matches the real CRDO/WGMI examples: neutral_choppy is not
        # "supportive" -- criterion 3 must be 0, not a partial point.
        result = classify_rubric(_inputs(regime="neutral_choppy"))
        assert result.criteria["regime"] is False

    def test_risk_off_regime_does_not_score(self):
        result = classify_rubric(_inputs(regime="risk_off"))
        assert result.criteria["regime"] is False

    def test_pullback_in_uptrend_is_supportive(self):
        result = classify_rubric(_inputs(regime="pullback_in_uptrend"))
        assert result.criteria["regime"] is True

    def test_negative_rs_loses_the_point(self):
        result = classify_rubric(_inputs(rs=-0.01))
        assert result.criteria["rs"] is False

    def test_zero_rs_does_not_score(self):
        # Strictly outperforming, not just tied.
        result = classify_rubric(_inputs(rs=0.0))
        assert result.criteria["rs"] is False

    def test_extended_beyond_2x_atr_loses_the_point(self):
        result = classify_rubric(_inputs(dist_sma20_atr=2.01))
        assert result.criteria["sma20_extension"] is False

    def test_extended_negative_direction_also_loses_the_point(self):
        # abs() -- deep below SMA20 is just as "extended" as far above it.
        result = classify_rubric(_inputs(dist_sma20_atr=-2.5))
        assert result.criteria["sma20_extension"] is False

    def test_unknown_earnings_date_is_a_conservative_no_point(self):
        # None = unverified, same "can't award the point" posture the real
        # CRDO report used for its own unverified earnings criterion.
        result = classify_rubric(_inputs(earnings_days_out=None))
        assert result.criteria["event"] is False

    def test_earnings_inside_the_window_loses_the_point(self):
        result = classify_rubric(_inputs(earnings_days_out=5))
        assert result.criteria["event"] is False

    def test_earnings_well_outside_the_window_scores(self):
        result = classify_rubric(_inputs(earnings_days_out=30))
        assert result.criteria["event"] is True


class TestRegimeIsReportedButNeverScored:
    """2026-08-02: regime left the score (see rubric_formula.SCORED_CRITERIA for
    the three reasons). It is still REPORTED in the criteria dict -- every
    existing reader depends on that -- so the tests below pin both halves: the
    verdict still appears, and it moves nothing."""

    def test_hostile_regime_does_not_lower_the_score(self):
        supportive = classify_rubric(_inputs(regime="healthy_uptrend"))
        hostile = classify_rubric(_inputs(regime="risk_off"))
        assert supportive.score == hostile.score == 5
        assert supportive.grade == hostile.grade == "A"

    def test_the_regime_verdict_is_still_reported(self):
        assert classify_rubric(_inputs(regime="risk_off")).criteria["regime"] is False
        assert classify_rubric(_inputs(regime="risk_on")).criteria["regime"] is True

    def test_choppy_regime_no_longer_costs_a_grade_letter(self):
        # The exact live situation this change was made for: the regime formula
        # read neutral_choppy every day from 2026-07-20, so this input used to
        # come back C and now comes back B on identical trade quality.
        result = classify_rubric(_inputs(earnings_days_out=None, regime="neutral_choppy"))
        assert result.score == 4
        assert result.grade == "B"


class TestGradeBoundaries:
    def test_four_of_five_grades_b(self):
        result = classify_rubric(_inputs(earnings_days_out=None))  # loses only "event"
        assert result.score == 4
        assert result.grade == "B"

    def test_three_of_five_grades_c(self):
        result = classify_rubric(_inputs(earnings_days_out=None, rs=-1))
        assert result.score == 3
        assert result.grade == "C"

    def test_two_of_five_grades_d(self):
        result = classify_rubric(_inputs(earnings_days_out=None, rs=-1, rr=1.0))
        assert result.score == 2
        assert result.grade == "D"

    def test_zero_of_five_is_still_d_because_there_is_no_f(self):
        # D and F blocked exactly the same things, so the scale bottoms out at
        # D -- a second failing letter carried no extra meaning.
        result = classify_rubric(_inputs(earnings_days_out=None, rs=-1, rr=1.0,
                                          target_atr=0.5, dist_sma20_atr=9.0))
        assert result.score == 0
        assert result.grade == "D"

    def test_same_inputs_always_produce_the_same_result(self):
        inputs = _inputs()
        r1 = classify_rubric(inputs)
        r2 = classify_rubric(inputs)
        assert r1.grade == r2.grade
        assert r1.score == r2.score


class TestRealWorldRegressionFixtures:
    def test_crdo_2026_07_20_primary_setup_matches_the_delivered_f_grade(self):
        # Real numbers from _decision_CRDO.json's Primary setup (Failed
        # Breakdown, target 245.9499): rr=2.10, target_atr=1.71x,
        # regime=neutral_choppy, rs_vs_spy_5d=-19.83 (reversal setup -> 5d
        # window per SCREENER_v3.md's RS-window rule), dist_sma20_atr=-1.96,
        # earnings_verified=false. The report itself hand-scored this 2/6->F
        # -- this proves the formula reproduces that by construction, not
        # just by coincidence of the grade boundary.
        # Re-baselined 2026-08-02: regime left the score. The two points this
        # setup earned (target_atr, sma20_extension) were never regime points,
        # so the score stays 2 -- out of 5 now, not 6 -- and the grade lands on
        # D instead of F. The trade is judged exactly as harshly either way:
        # D blocks the same things F did.
        result = classify_rubric(RubricInputs(
            rr=2.10, target_atr_multiple=1.71, regime="neutral_choppy",
            rs_delta_pct=-19.83, dist_sma20_atr=-1.96, earnings_days_out=None,
        ))
        assert result.score == 2
        assert result.grade == "D"
        assert result.criteria == {
            "rr": False, "target_atr": True, "regime": False,
            "rs": False, "sma20_extension": True, "event": False,
        }

    def test_wgmi_2026_07_21_alternate_setup_real_magnitudes(self):
        # Real numbers from _decision_WGMI.json's Alternate setup (Failed
        # Breakdown, trigger 51.02/stop 43.92, target1 67.885):
        # rr=2.38, target_atr=3.51x, regime=neutral_choppy,
        # rs_vs_spy_5d=9.41 (reversal setup -> 5d window), dist_sma20_atr=-0.10.
        # NOTE: the real report graded this D overall and cited
        # "rr_below_2_5_on_t1" -- an older 2.5 R:R bar (CONSISTENCY_RULES.md
        # rule 14's own text confirms the rubric threshold moved from 2.5 to
        # 2.3). This module uses the current, documented 2.3 threshold
        # (SCREENER_v3.md:131), so it legitimately disagrees with that one
        # stale figure -- this is exactly the drift rule 27 exists to stop,
        # not a bug in this test.
        result = classify_rubric(RubricInputs(
            rr=2.38, target_atr_multiple=3.51, regime="neutral_choppy",
            rs_delta_pct=9.41, dist_sma20_atr=-0.10, earnings_days_out=None,
        ))
        assert result.criteria["rr"] is True
        assert result.criteria["target_atr"] is True
        assert result.criteria["regime"] is False
        assert result.criteria["rs"] is True
        assert result.criteria["sma20_extension"] is True
        assert result.criteria["event"] is False
        # Re-baselined 2026-08-02: 4/5 -> B (was 4/6 -> C). This setup lost its
        # only point to an unverified earnings date, which the 2026-08-02
        # earnings fix also addressed -- see indicators_core.earnings_is_verified.
        assert result.score == 4
        assert result.grade == "B"
