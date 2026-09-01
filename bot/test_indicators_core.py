"""Unit tests for indicators_core.py -- known-correct values, checkable by hand.

Per CLAUDE_CODE_COST_REDUCTION_INSTRUCTIONS.md Part 2 verification step 1: this proves
the arithmetic is internally correct, nothing more. It does NOT prove atr_wilder()
matches TradingView's own indicator -- that requires the live human side-by-side check
(ACCEPTANCE_TEST_NON_TECHNICAL.md Check 1), which only a human can do, and is not
skipped or replaced by this file.
"""

import math

import pytest

from indicators_core import (
    adx_series,
    ema_series,
    macd_series,
    rsi_wilder,
    anchored_vwap,
    atr_wilder,
    build_tranche_plan,
    compute_r_multiple,
    derive_exit_reason,
    fibonacci_extension,
    measured_move_target,
    parabolic_sar,
    relative_strength,
    sma,
    true_range,
    volume_average,
)


def test_true_range_basic_cases():
    # Simple case: today's range is the widest measure.
    assert true_range(high=10, low=8, prev_close=9) == 2
    # Gap up: prev_close far below today's low -> high-prev_close dominates.
    assert true_range(high=15, low=13, prev_close=10) == 5
    # Gap down: prev_close far above today's high -> prev_close-low dominates.
    assert true_range(high=10, low=8, prev_close=13) == 5


def test_atr_wilder_hand_computed():
    # Hand-computable example: constant true range of exactly 2.0 for every bar after
    # the seed. With TR constant, Wilder's smoothing recurrence converges to that same
    # constant value immediately (seed = mean of first `period` TRs = 2.0, and every
    # subsequent update (2.0*(period-1) + 2.0)/period == 2.0), so ATR must equal 2.0
    # exactly regardless of period -- a strong, easy-to-verify-by-hand invariant.
    period = 5
    n = period + 6
    closes = [100.0 + i for i in range(n)]  # arbitrary increasing closes
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    # Rig true range to be exactly 2.0 every bar: high-low=2.0 already; also need
    # abs(high-prev_close) and abs(low-prev_close) <= 2.0 so high-low dominates.
    # closes increasing by 1.0/bar with a 1.0 wick on each side keeps TR pinned at 2.0.
    atr = atr_wilder(highs, lows, closes, period=period)
    assert atr == pytest.approx(2.0, abs=1e-9)


def test_atr_wilder_seed_then_recurrence_by_hand():
    # Explicit tiny hand-worked example, period=2 (n=4 closes -> 3 true ranges).
    # closes: prev_close=10 (implicit via first bar's own low/high only), then bars:
    highs =  [11, 12, 10]
    lows =   [9,  10, 8]
    closes = [10, 11, 9]
    # trs[i] = true_range(highs[i], lows[i], closes[i-1]) for i in 1..n-1
    # tr[1] = true_range(12, 10, 10) = max(2, 2, 0) = 2
    # tr[2] = true_range(10, 8, 11)  = max(2, 1, 3) = 3
    period = 2
    atr = atr_wilder(highs, lows, closes, period=period)
    # seed = mean(trs[:2]) = mean([2, 3]) = 2.5; no further bars after the seed (only
    # 2 true-range values total for n=3 closes), so ATR == the seed exactly.
    assert atr == pytest.approx(2.5, abs=1e-9)


def test_atr_wilder_raises_on_insufficient_bars():
    with pytest.raises(ValueError):
        atr_wilder([1, 2], [1, 2], [1, 2], period=14)


def test_atr_wilder_raises_on_mismatched_lengths():
    with pytest.raises(ValueError):
        atr_wilder([1, 2, 3], [1, 2], [1, 2, 3], period=1)


def test_parabolic_sar_uptrend_hand_computed():
    # Hand-computable uptrend: every bar makes a new high/low, so SAR keeps
    # trending, EP tracks the running high, and AF steps up by af_step each
    # bar (0.02 -> 0.04 -> 0.06 -> 0.08). This only proves the recurrence is
    # internally consistent -- it does NOT confirm this matches TradingView's
    # own Parabolic SAR for a real symbol; that's a separate human
    # side-by-side check (same posture as this file's ATR tests above).
    highs = [10, 11, 12, 13, 14]
    lows = [9, 10, 11, 12, 13]
    points = parabolic_sar(highs, lows, af_start=0.02, af_step=0.02, af_max=0.2)

    assert len(points) == len(highs) - 1
    # seed (bar index 1): trend up (midpoint 21 >= 19), sar = lows[0], ep = highs[1]
    assert points[0].trend == 1
    assert points[0].sar == pytest.approx(9.0)
    assert points[0].ep == pytest.approx(11.0)
    assert points[0].af == pytest.approx(0.02)
    # bar index 2: raw = 9 + 0.02*(11-9) = 9.04, clipped to min(9.04, lows[1]=10, lows[0]=9) = 9
    assert points[1].trend == 1
    assert points[1].sar == pytest.approx(9.0)
    assert points[1].ep == pytest.approx(12.0)
    assert points[1].af == pytest.approx(0.04)
    # bar index 3: raw = 9 + 0.04*(12-9) = 9.12, clipped to min(9.12, 11, 10) = 9.12
    assert points[2].sar == pytest.approx(9.12)
    assert points[2].ep == pytest.approx(13.0)
    assert points[2].af == pytest.approx(0.06)
    # bar index 4: raw = 9.12 + 0.06*(13-9.12) = 9.3528, clipped to min(9.3528, 12, 11) = 9.3528
    assert points[3].sar == pytest.approx(9.3528)
    assert points[3].ep == pytest.approx(14.0)
    assert points[3].af == pytest.approx(0.08)
    # invariant: throughout a clean uptrend, SAR must stay strictly below that bar's low
    for i, p in enumerate(points):
        assert p.sar < lows[i + 1]


def test_parabolic_sar_trend_flip_resets_ep_and_af():
    # Same uptrend as above for 4 bars, then a sharp drop whose low breaks
    # below the clipped SAR -- must flip: new sar = the just-ended uptrend's
    # own EP, new ep = the drop bar's low, af resets to af_start.
    highs = [10, 11, 12, 13, 14, 9]
    lows = [9, 10, 11, 12, 13, 8]
    points = parabolic_sar(highs, lows, af_start=0.02, af_step=0.02, af_max=0.2)

    flipped = points[-1]
    assert flipped.trend == -1
    assert flipped.sar == pytest.approx(14.0)   # prior uptrend's EP
    assert flipped.ep == pytest.approx(8.0)      # the flip bar's own low
    assert flipped.af == pytest.approx(0.02)     # reset to af_start


def test_parabolic_sar_raises_on_insufficient_bars():
    with pytest.raises(ValueError):
        parabolic_sar([10, 11], [9, 10])


def test_parabolic_sar_raises_on_mismatched_lengths():
    with pytest.raises(ValueError):
        parabolic_sar([10, 11, 12], [9, 10])


def test_sma_basic():
    closes = [1, 2, 3, 4, 5]
    assert sma(closes, 5) == 3.0
    assert sma(closes, 3) == pytest.approx((3 + 4 + 5) / 3)
    assert sma(closes, 1) == 5.0


def test_sma_raises_on_insufficient_closes():
    with pytest.raises(ValueError):
        sma([1, 2], 5)


def test_relative_strength_hand_computed():
    # ticker up 10% over the window, benchmark up 5% -> RS delta = +5 percentage points.
    ticker_closes = [100.0] * 10 + [110.0]  # 10-day-old close=100, now=110 (+10%)
    benchmark_closes = [200.0] * 10 + [210.0]  # 200 -> 210 (+5%)
    rs = relative_strength(ticker_closes, benchmark_closes, window_days=10)
    assert rs.window_days == 10
    assert rs.ticker_change_pct == pytest.approx(10.0)
    assert rs.benchmark_change_pct == pytest.approx(5.0)
    assert rs.rs_delta_pct == pytest.approx(5.0)


def test_relative_strength_raises_on_misaligned_lengths():
    with pytest.raises(ValueError):
        relative_strength([1.0, 2.0], [1.0], window_days=1)


def test_relative_strength_raises_on_insufficient_window():
    with pytest.raises(ValueError):
        relative_strength([1.0, 2.0], [1.0, 2.0], window_days=5)


def test_volume_average_excludes_current_by_default():
    # 20 prior bars of volume=100, plus a current bar of volume=100000 that must be
    # excluded from the "typical volume" average by default.
    volumes = [100] * 20 + [100000]
    avg = volume_average(volumes, period=20)
    assert avg == pytest.approx(100.0)


def test_volume_average_includes_current_when_requested():
    volumes = [0] * 19 + [20 * 100]  # last bar carries all the volume
    avg = volume_average(volumes, period=20, include_current=True)
    assert avg == pytest.approx(100.0)


def test_volume_average_raises_on_insufficient_bars():
    with pytest.raises(ValueError):
        volume_average([100, 100], period=20)


def test_fibonacci_extension_hand_computed():
    # Upward move A=100 -> B=150 (move size = 50), pullback to C=130.
    # 127.2% level = 130 + 50*1.272 = 193.6; 161.8% = 130 + 50*1.618 = 210.9;
    # 261.8% = 130 + 50*2.618 = 260.9 -- exact hand arithmetic.
    fib = fibonacci_extension(anchor_a=100.0, anchor_b=150.0, anchor_c=130.0)
    assert fib.levels[1.272] == pytest.approx(193.6)
    assert fib.levels[1.618] == pytest.approx(210.9)
    assert fib.levels[2.618] == pytest.approx(260.9)


def test_fibonacci_extension_downward_move():
    # Downward move A=150 -> B=100 (move size = -50), bounce to C=120.
    # 127.2% level = 120 + (-50)*1.272 = 56.4 -- extension projects below C.
    fib = fibonacci_extension(anchor_a=150.0, anchor_b=100.0, anchor_c=120.0)
    assert fib.levels[1.272] == pytest.approx(56.4)


def test_anchored_vwap_hand_computed():
    # Equal volume every bar -> VWAP is a plain average of typical price ((H+L+C)/3).
    highs =  [10, 10, 10]
    lows =   [8, 8, 8]
    closes = [9, 9, 9]
    volumes = [100, 100, 100]
    vwap = anchored_vwap(highs, lows, closes, volumes, anchor_index=0)
    assert vwap == pytest.approx(9.0)  # typical price is exactly 9 every bar


def test_anchored_vwap_weights_by_volume():
    # Two bars, second bar has 3x the volume of the first -> VWAP should be much
    # closer to the second bar's typical price than a plain average would be.
    highs =  [10, 20]
    lows =   [10, 20]
    closes = [10, 20]  # typical price = 10 and 20 respectively
    volumes = [100, 300]
    vwap = anchored_vwap(highs, lows, closes, volumes, anchor_index=0)
    # weighted: (10*100 + 20*300) / 400 = 17.5
    assert vwap == pytest.approx(17.5)


def test_anchored_vwap_respects_anchor_index():
    highs =  [100, 10, 10]
    lows =   [100, 8, 8]
    closes = [100, 9, 9]
    volumes = [999, 100, 100]
    # anchor_index=1 must exclude the first (wildly different) bar entirely.
    vwap = anchored_vwap(highs, lows, closes, volumes, anchor_index=1)
    assert vwap == pytest.approx(9.0)


def test_anchored_vwap_raises_on_out_of_range_anchor():
    with pytest.raises(ValueError):
        anchored_vwap([1, 2], [1, 2], [1, 2], [1, 2], anchor_index=5)


def test_anchored_vwap_raises_on_mismatched_lengths():
    with pytest.raises(ValueError):
        anchored_vwap([1, 2, 3], [1, 2], [1, 2, 3], [1, 2, 3], anchor_index=0)


def test_measured_move_target_hand_computed():
    # Base (pre-move consolidation) 100-120, height 20. Breakout at 120 ->
    # projected potential = 120 + 20 = 140.
    assert measured_move_target(base_high=120.0, base_low=100.0, breakout_level=120.0) == pytest.approx(140.0)


def test_measured_move_target_breakout_above_base_high():
    # Breakout level need not equal base_high exactly (price may have already
    # run some distance past the base before this is computed) -- height still
    # projects from wherever breakout_level actually is.
    assert measured_move_target(base_high=120.0, base_low=100.0, breakout_level=125.0) == pytest.approx(145.0)


# ---------------------------------------------------------------------------
# derive_exit_reason / compute_r_multiple (2026-07-30 full-system checkup --
# these write permanently to the journal's exit/R-multiple history via
# persistence.record_exit, and had zero test coverage before this, unlike
# every other function in this file.
# ---------------------------------------------------------------------------

def test_derive_exit_reason_matches_stop_exactly():
    match = derive_exit_reason(exit_price=97.0, targets=[{"price": 110.0}], stop=97.0, atr_at_build=2.0)
    assert match.reason == "stop"
    assert match.matched_price == 97.0


def test_derive_exit_reason_matches_stop_within_tolerance():
    # tolerance = max(97.5*0.01, 0.3*2.0) = max(0.975, 0.6) = 0.975
    match = derive_exit_reason(exit_price=97.5, targets=[{"price": 110.0}], stop=97.0, atr_at_build=2.0)
    assert match.reason == "stop"


def test_derive_exit_reason_matches_correct_target_index():
    match = derive_exit_reason(
        exit_price=110.0,
        targets=[{"price": 105.0}, {"price": 110.0}, {"price": 115.0}],
        stop=97.0, atr_at_build=2.0,
    )
    assert match.reason == "target_2"
    assert match.matched_price == 110.0

    match_first = derive_exit_reason(
        exit_price=105.0,
        targets=[{"price": 105.0}, {"price": 110.0}],
        stop=97.0, atr_at_build=2.0,
    )
    assert match_first.reason == "target_1"


def test_derive_exit_reason_stop_takes_priority_over_a_tied_target():
    # A target sitting exactly at the stop price (shouldn't happen in practice,
    # but stop must win if it ever does -- see the function's own docstring).
    match = derive_exit_reason(exit_price=97.0, targets=[{"price": 97.0}], stop=97.0, atr_at_build=2.0)
    assert match.reason == "stop"


def test_derive_exit_reason_unmatched_when_outside_every_tolerance():
    match = derive_exit_reason(exit_price=103.0, targets=[{"price": 110.0}], stop=97.0, atr_at_build=2.0)
    assert match.reason == "unmatched"
    assert match.matched_price is None


def test_derive_exit_reason_target_with_no_price_is_skipped_not_crashed():
    match = derive_exit_reason(
        exit_price=110.0,
        targets=[{"price": None}, {"price": 110.0}],
        stop=97.0, atr_at_build=2.0,
    )
    assert match.reason == "target_2"


def test_derive_exit_reason_just_outside_tolerance_boundary_is_unmatched():
    # tolerance for stop=97.0 exit near it: max(exit*0.01, 0.3*2.0=0.6) -- pick
    # an exit_price where the boundary is clearly on the "too far" side.
    match = derive_exit_reason(exit_price=95.0, targets=[{"price": 110.0}], stop=97.0, atr_at_build=2.0)
    assert match.reason == "unmatched"


def test_derive_exit_reason_never_matches_an_already_realized_target():
    """The ASTS regression, stated directly (position 25, 2026-08-05/06): one
    stored target at 72.36, sold at once, then sold at AGAIN four days later.
    The second sell must not come back as target_1 -- there is nothing left to
    realize at that level, so it is a Runner trim."""
    first = derive_exit_reason(exit_price=72.36, targets=[{"price": 72.36, "pct": 40}],
                                stop=54.78, atr_at_build=6.39)
    assert first.reason == "target_1"

    second = derive_exit_reason(exit_price=73.64, targets=[{"price": 72.36, "pct": 40}],
                                 stop=54.78, atr_at_build=6.39,
                                 filled_target_indexes={1})
    assert second.reason == "runner_trim"
    assert second.matched_price is None


def test_derive_exit_reason_skips_a_filled_target_but_still_matches_the_next_one():
    match = derive_exit_reason(
        exit_price=110.0,
        targets=[{"price": 110.0}, {"price": 110.4}],
        stop=97.0, atr_at_build=2.0,
        filled_target_indexes={1},
    )
    assert match.reason == "target_2"


def test_derive_exit_reason_runner_only_position_trims_are_named_as_such():
    # CONSISTENCY_RULES.md rule 6: no qualifying target was ever stored, so any
    # sell above the stop is by definition a Runner trim, not off-plan.
    match = derive_exit_reason(exit_price=130.0, targets=[], stop=97.0, atr_at_build=2.0)
    assert match.reason == "runner_trim"


def test_derive_exit_reason_below_stop_is_never_a_runner_trim():
    # A sell BELOW the stop with every target already realized is a bad fill,
    # not a planned Runner trim -- it must stay honestly unmatched.
    match = derive_exit_reason(exit_price=90.0, targets=[{"price": 110.0}],
                                stop=97.0, atr_at_build=2.0, filled_target_indexes={1})
    assert match.reason == "unmatched"


# ---------------------------------------------------------------------------
# build_tranche_plan -- CONSISTENCY_RULES.md rule 7's allocation applied to a
# position's real exits. Added 2026-08-07 with the ASTS fix above.
# ---------------------------------------------------------------------------

def test_tranche_plan_single_target_is_40_then_runner():
    plan = build_tranche_plan(original_qty=211, targets=[{"price": 72.36, "pct": 40}], exits=[])
    assert [t.label for t in plan.tranches] == ["target_1", "runner"]
    assert plan.tranches[0].planned_qty == 84       # round(211 * 0.40)
    assert plan.tranches[1].planned_qty == 127      # remainder, so the two sum to exactly 211
    assert plan.tranches[0].planned_qty + plan.tranches[1].planned_qty == 211
    assert plan.next_label == "target_1"
    assert plan.next_price == 72.36
    assert plan.next_qty == 84
    assert plan.remaining_qty == 211


def test_tranche_plan_after_target_1_fills_the_runner_is_next_and_has_no_price():
    plan = build_tranche_plan(
        original_qty=211, targets=[{"price": 72.36, "pct": 40}],
        exits=[{"exit_qty": 84, "exit_reason": "target_1"}],
    )
    assert plan.tranches[0].status == "filled"
    assert plan.next_label == "runner"
    assert plan.next_price is None          # nothing left to name a price for
    assert plan.runner_qty_left == 127
    assert plan.remaining_qty == 127


def test_tranche_plan_counts_a_runner_trim_against_the_runner_not_the_target():
    """The real ASTS shape after the fix: 84 at target 1, then 45 more as a
    Runner trim. Target 1 stays filled exactly once and the Runner shrinks --
    the whole point, since the Runner tranche is where the edge lives."""
    plan = build_tranche_plan(
        original_qty=211, targets=[{"price": 72.36, "pct": 40}],
        exits=[{"exit_qty": 84, "exit_reason": "target_1"},
               {"exit_qty": 45, "exit_reason": "runner_trim"}],
    )
    target_1, runner = plan.tranches
    assert target_1.filled_qty == 84 and target_1.status == "filled"
    assert runner.filled_qty == 45 and runner.status == "partial"
    assert plan.runner_qty_left == 82
    assert plan.remaining_qty == 82
    assert plan.warnings == []


def test_tranche_plan_two_targets_use_rule_7s_40_35_25_split():
    plan = build_tranche_plan(
        original_qty=100, targets=[{"price": 110.0}, {"price": 120.0}], exits=[],
    )
    assert [(t.label, t.planned_qty) for t in plan.tranches] == [
        ("target_1", 40), ("target_2", 35), ("runner", 25),
    ]


def test_tranche_plan_stored_pct_beats_the_rule_7_default():
    # The report the user actually read is the authority on the split.
    plan = build_tranche_plan(
        original_qty=100, targets=[{"price": 110.0, "pct": 50}], exits=[],
    )
    assert plan.tranches[0].planned_qty == 50
    assert plan.tranches[1].planned_qty == 50


def test_tranche_plan_flags_a_target_sold_beyond_its_planned_size():
    plan = build_tranche_plan(
        original_qty=100, targets=[{"price": 110.0, "pct": 40}],
        exits=[{"exit_qty": 70, "exit_reason": "target_1"}],
    )
    assert plan.tranches[0].over_filled_by == 30
    assert any("more than its planned" in w for w in plan.warnings)
    # The Runner planned 60 but the position only holds 30 -- never report more
    # Runner left than actually exists.
    assert plan.remaining_qty == 30
    assert plan.runner_qty_left == 30


def test_tranche_plan_runner_only_position_has_no_numeric_target():
    plan = build_tranche_plan(original_qty=100, targets=[], exits=[])
    assert plan.runner_only is True
    assert [t.label for t in plan.tranches] == ["runner"]
    assert plan.tranches[0].planned_qty == 100
    assert plan.next_label == "runner"


def test_tranche_plan_stop_out_comes_out_of_the_runner():
    plan = build_tranche_plan(
        original_qty=100, targets=[{"price": 110.0, "pct": 40}],
        exits=[{"exit_qty": 40, "exit_reason": "target_1"},
               {"exit_qty": 60, "exit_reason": "stop"}],
    )
    assert plan.remaining_qty == 0
    assert plan.next_label is None
    assert plan.runner_qty_left == 0


def test_tranche_plan_with_no_quantity_on_file_says_so_instead_of_guessing():
    plan = build_tranche_plan(original_qty=0, targets=[{"price": 110.0}], exits=[])
    assert plan.tranches == []
    assert any("no original quantity" in w for w in plan.warnings)


def test_compute_r_multiple_profit_is_positive_r():
    # entry 100, initial_stop 95 (risk 5/share), exit 110 -> +2R
    assert compute_r_multiple(entry_price=100.0, initial_stop=95.0, exit_price=110.0) == pytest.approx(2.0)


def test_compute_r_multiple_loss_at_stop_is_minus_1r():
    assert compute_r_multiple(entry_price=100.0, initial_stop=95.0, exit_price=95.0) == pytest.approx(-1.0)


def test_compute_r_multiple_breakeven_exit_is_zero_r():
    assert compute_r_multiple(entry_price=100.0, initial_stop=95.0, exit_price=100.0) == pytest.approx(0.0)


def test_compute_r_multiple_uses_initial_stop_not_trailed_stop():
    # Same exit, but a LOWER initial_stop (wider original risk) must give a
    # smaller R-multiple for the identical dollar profit -- this is exactly
    # why the function takes initial_stop explicitly, never the live/trailed one.
    wide_risk = compute_r_multiple(entry_price=100.0, initial_stop=90.0, exit_price=110.0)
    narrow_risk = compute_r_multiple(entry_price=100.0, initial_stop=95.0, exit_price=110.0)
    assert wide_risk == pytest.approx(1.0)
    assert narrow_risk == pytest.approx(2.0)
    assert wide_risk < narrow_risk


def test_compute_r_multiple_raises_when_stop_not_below_entry():
    with pytest.raises(ValueError):
        compute_r_multiple(entry_price=100.0, initial_stop=100.0, exit_price=110.0)
    with pytest.raises(ValueError):
        compute_r_multiple(entry_price=100.0, initial_stop=105.0, exit_price=110.0)


class TestRsiWilder:
    """RSI added 2026-08-03 -- this project had no overbought/oversold measure
    at all, so the backtest could not even ask whether RSI at entry predicts
    anything. Wilder's smoothing, matching TradingView's default RSI, not the
    simple-average variant that draws a visibly different line."""

    def test_only_gains_is_one_hundred(self):
        assert rsi_wilder(list(range(1, 40))) == pytest.approx(100.0)

    def test_only_losses_is_zero(self):
        assert rsi_wilder([40 - i for i in range(1, 40)]) == pytest.approx(0.0)

    def test_flat_series_is_neutral(self):
        # No gains and no losses at all -- neither overbought nor oversold.
        assert rsi_wilder([100.0] * 30) == pytest.approx(50.0)

    def test_too_short_returns_none_rather_than_raising(self):
        # Callers scan bar by bar; a missing early value is expected, not an error.
        assert rsi_wilder([1, 2, 3]) is None
        assert rsi_wilder([1] * 14, period=14) is None

    def test_known_wilder_worked_example(self):
        # Wilder's own textbook series (New Concepts in Technical Trading
        # Systems, the classic 14-period example) rounds to 70.53.
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
                  45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
        assert rsi_wilder(closes, period=14) == pytest.approx(70.53, abs=0.2)

    def test_stays_inside_zero_and_one_hundred(self):
        import random
        random.seed(7)
        closes = [100.0]
        for _ in range(300):
            closes.append(closes[-1] * (1 + random.uniform(-0.05, 0.05)))
        value = rsi_wilder(closes)
        assert 0.0 <= value <= 100.0


class TestEmaMacdAdx:
    """MACD and ADX added 2026-08-03. Neither existed in this project, so the
    backtest could not ask whether they predict anything. Whether they earn a
    place in the scoring system is decided by the training years, not by their
    reputation -- these tests only pin that the arithmetic is right."""

    def test_ema_of_a_flat_series_is_that_value(self):
        assert ema_series([5.0] * 30, 10)[-1] == pytest.approx(5.0)

    def test_ema_is_undefined_before_its_seed(self):
        out = ema_series([1.0] * 20, 10)
        assert out[:9] == [None] * 9
        assert out[9] is not None

    def test_ema_too_short_is_all_none(self):
        assert ema_series([1.0, 2.0], 10) == [None, None]

    def test_macd_is_positive_on_a_rising_series(self):
        macd, signal, hist = macd_series([100 + i for i in range(80)])
        assert macd[-1] > 0
        assert signal[-1] > 0

    def test_macd_is_negative_on_a_falling_series(self):
        macd, _, _ = macd_series([200 - i for i in range(80)])
        assert macd[-1] < 0

    def test_macd_equals_fast_ema_minus_slow_ema(self):
        closes = [100 + (i % 7) * 3 for i in range(120)]
        macd, _, _ = macd_series(closes)
        fast = ema_series(closes, 12)
        slow = ema_series(closes, 26)
        assert macd[-1] == pytest.approx(fast[-1] - slow[-1])

    def test_macd_histogram_is_macd_minus_signal(self):
        closes = [100 + (i % 11) * 2 for i in range(150)]
        macd, signal, hist = macd_series(closes)
        assert hist[-1] == pytest.approx(macd[-1] - signal[-1])

    def test_adx_is_high_in_a_clean_trend(self):
        highs = [100 + i for i in range(80)]
        lows = [99 + i for i in range(80)]
        closes = [99.5 + i for i in range(80)]
        assert adx_series(highs, lows, closes)[-1] > 50

    def test_adx_is_lower_in_chop_than_in_a_trend(self):
        n = 120
        trend_h = [100 + i for i in range(n)]
        trend_l = [99 + i for i in range(n)]
        trend_c = [99.5 + i for i in range(n)]
        chop_h = [101 + (i % 2) for i in range(n)]
        chop_l = [99 - (i % 2) for i in range(n)]
        chop_c = [100 + (0.5 if i % 2 else -0.5) for i in range(n)]
        assert adx_series(chop_h, chop_l, chop_c)[-1] < adx_series(trend_h, trend_l, trend_c)[-1]

    def test_adx_too_short_is_all_none(self):
        assert adx_series([1, 2], [1, 2], [1, 2]) == [None, None]
