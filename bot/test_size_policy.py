"""Tests for size_policy.py (CONSISTENCY_RULES.md rule 28).

The two "incident shape" tests below reproduce the pair of sizing mistakes that
made this rule necessary -- one entry sized far under a full position, one
sized far over the ceiling. The entry/stop prices are kept; the account is a
round $100,000 example so nothing here carries a real balance.
"""

import math

import pytest

import size_policy as sp

FULL_RISK = 1000.0  # 100,000 example equity x 1% -- one full risk unit


# --- clamp_multiplier -------------------------------------------------------
#
# 2026-08-03: volatility and volume joined regime in ADVISORY_MULTIPLIER_KEYS --
# every trade is now a flat 1% risk, and rule 28's floor/ceiling does the whole
# job. These tests use a made-up key ("custom") wherever the clamp machinery
# itself is what's under test, so they keep testing the arithmetic without
# re-asserting a sizing policy that no longer exists.

def test_a_single_derate_is_untouched():
    m = sp.clamp_multiplier({"custom": 0.5})
    assert m.raw == 0.5
    assert m.applied == 0.5
    assert m.floored is False


def test_no_derate_is_full_size():
    m = sp.clamp_multiplier({"custom": 1.0})
    assert m.applied == 1.0
    assert m.floored is False


def test_two_stacked_derates_hit_the_floor():
    m = sp.clamp_multiplier({"custom": 0.5, "custom2": 0.5})
    assert m.raw == pytest.approx(0.25)
    assert m.applied == 0.5
    assert m.floored is True


def test_volatility_and_volume_no_longer_shrink_anything():
    """The whole 2026-08-03 sizing decision, pinned. Flat 1% made $6.82M in the
    backtest vs $4.15M with these multipliers, and risk-based sizing already
    accounts for volatility once (a jumpy stock needs a wider stop, so 1% risk
    buys fewer shares) -- the multiplier charged it twice."""
    m = sp.clamp_multiplier({"volatility": 0.5, "volume": 0.5})
    assert m.raw == 1.0            # neither is multiplied in
    assert m.applied == 1.0
    assert m.floored is False
    assert m.parts["volatility"] == 0.5   # still reported, for the sizing table


def test_market_condition_is_shown_but_never_multiplied_in():
    m = sp.clamp_multiplier({"custom": 0.5, "choppy": 0.5})
    assert m.raw == pytest.approx(0.5)      # NOT 0.25
    assert m.applied == 0.5
    assert m.parts["choppy"] == 0.5


def test_every_advisory_key_together_leaves_a_full_position():
    m = sp.clamp_multiplier({"choppy": 0.5, "regime": 0.5, "market": 0.5,
                              "volatility": 0.5, "volume": 0.5})
    assert m.raw == 1.0
    assert m.applied == 1.0


def test_multiplier_above_one_is_clamped_down():
    """No upsize path exists by design -- see the module docstring."""
    m = sp.clamp_multiplier({"conviction": 2.0})
    assert m.applied == 1.0
    assert m.floored is True


def test_sequence_form_matches_dict_form():
    assert sp.clamp_multiplier([0.5, 0.5]).applied == sp.clamp_multiplier({"a": 0.5, "b": 0.5}).applied


def test_none_values_are_ignored_not_treated_as_zero():
    m = sp.clamp_multiplier({"custom": 0.75, "custom2": None})
    assert m.raw == pytest.approx(0.75)


# --- size_position ----------------------------------------------------------

def test_full_size_when_nothing_derates():
    r = sp.size_position(FULL_RISK, entry=100.0, stop=90.0, multipliers={})
    assert r.qty == 100                      # 1000.0 / 10.00
    assert r.risk_usd_actual == pytest.approx(1000.0)
    assert r.risk_fraction_of_full == pytest.approx(1.0, abs=0.005)
    assert sp.describe_fraction_he(r.risk_fraction_of_full) == "פוזיציה מלאה"


def test_stacked_derates_never_go_below_half():
    r = sp.size_position(FULL_RISK, entry=100.0, stop=90.0,
                          multipliers={"custom": 0.5, "custom2": 0.5})
    assert r.multiplier.raw == pytest.approx(0.25)
    assert r.qty == 50                       # half, not an eighth (which would be 12)
    assert r.risk_fraction_of_full == pytest.approx(0.5, abs=0.005)
    assert sp.fraction_within_bounds(r.risk_fraction_of_full)


def test_result_never_exceeds_a_full_position():
    r = sp.size_position(FULL_RISK, entry=100.0, stop=90.0, multipliers={"x": 1.0})
    assert r.risk_usd_actual <= FULL_RISK + 1e-9


def test_undersized_entry_is_lifted_to_the_floor():
    """The incident shape: a 'full' entry that only risked a sixth of a
    position. Rule 28 sizes the same entry/stop at no less than half."""
    r = sp.size_position(FULL_RISK, entry=188.11, stop=179.80,
                          multipliers={"custom": 0.5, "custom2": 0.5})
    assert r.qty >= 60                       # vs the 30 that were bought
    assert r.risk_usd_actual >= FULL_RISK * sp.RISK_FRACTION_FLOOR - 1e-6
    assert r.risk_usd_actual <= FULL_RISK


def test_oversized_entry_is_capped_at_the_ceiling():
    """The incident shape: an entry sized at 2.3x the stated risk rule, on an
    F-graded idea. Rule 28 caps the same entry/stop at one full risk unit."""
    r = sp.size_position(FULL_RISK, entry=227.62, stop=182.07, multipliers={})
    assert r.qty == 21
    assert r.risk_usd_actual == pytest.approx(956.6, abs=1.0)
    assert r.risk_usd_actual <= FULL_RISK


def test_expensive_stock_with_a_wide_stop_is_refused_not_oversized():
    """One share already risks more than a full position -- an honest 'cannot
    be sized', never a silently oversized fill."""
    r = sp.size_position(FULL_RISK, entry=5000.0, stop=3000.0, multipliers={})
    assert r.qty == 0
    assert r.reason_skipped is not None
    assert sp.describe_fraction_he(r.risk_fraction_of_full) == "לא ניתן לפתוח פוזיציה בגודל חוקי"


def test_one_share_that_fits_is_allowed():
    r = sp.size_position(FULL_RISK, entry=1200.0, stop=200.0, multipliers={})
    assert r.qty == 1
    assert sp.fraction_within_bounds(r.risk_fraction_of_full)


def test_full_qty_is_reported_for_comparison():
    r = sp.size_position(FULL_RISK, entry=100.0, stop=90.0, multipliers={"custom": 0.5})
    assert r.full_qty == 100
    # Half of the full position risks exactly the $500 floor -- whole-share
    # rounding always resolves toward the floor being respected.
    assert r.qty == 50


def test_cost_is_shares_times_entry():
    r = sp.size_position(FULL_RISK, entry=100.0, stop=90.0, multipliers={})
    assert r.cost_usd == pytest.approx(r.qty * 100.0)


def test_stop_above_entry_is_rejected():
    with pytest.raises(ValueError):
        sp.size_position(FULL_RISK, entry=90.0, stop=100.0)


def test_zero_risk_target_is_rejected():
    with pytest.raises(ValueError):
        sp.size_position(0, entry=100.0, stop=90.0)


# --- evaluate_order ---------------------------------------------------------

def test_evaluate_order_measures_an_already_chosen_qty():
    r = sp.evaluate_order(30, entry=188.11, stop=179.80, risk_usd_target=FULL_RISK)
    assert r.risk_usd_actual == pytest.approx(249.3, abs=0.1)
    assert r.risk_fraction_of_full == pytest.approx(0.249, abs=0.002)
    assert not sp.fraction_within_bounds(r.risk_fraction_of_full)


def test_evaluate_order_flags_an_oversized_order():
    r = sp.evaluate_order(74, entry=227.62, stop=182.07, risk_usd_target=FULL_RISK)
    assert r.risk_fraction_of_full > sp.RISK_FRACTION_CEILING
    assert not sp.fraction_within_bounds(r.risk_fraction_of_full)


def test_evaluate_order_agrees_with_size_position():
    sized = sp.size_position(FULL_RISK, entry=100.0, stop=90.0, multipliers={"custom": 0.5})
    measured = sp.evaluate_order(sized.qty, 100.0, 90.0, FULL_RISK, {"custom": 0.5})
    assert measured.risk_usd_actual == pytest.approx(sized.risk_usd_actual)
    assert measured.full_qty == sized.full_qty


# --- bounds + wording -------------------------------------------------------

@pytest.mark.parametrize("fraction,expected", [
    (1.0, "פוזיציה מלאה"),
    (0.95, "פוזיציה מלאה"),
    (0.75, "כשלושה רבעים מפוזיציה מלאה"),
    (0.5, "חצי פוזיציה"),
    (0.17, "פחות מחצי פוזיציה — קטן מדי לפי הכללים"),
    (0.0, "לא ניתן לפתוח פוזיציה בגודל חוקי"),
])
def test_plain_hebrew_labels(fraction, expected):
    assert sp.describe_fraction_he(fraction) == expected


def test_bounds_allow_rounding_slack_only():
    assert sp.fraction_within_bounds(0.49)      # whole-share rounding, fine
    assert sp.fraction_within_bounds(1.01)
    assert not sp.fraction_within_bounds(0.40)
    assert not sp.fraction_within_bounds(1.10)


def test_telegram_block_names_the_size_in_plain_words():
    r = sp.size_position(FULL_RISK, entry=100.0, stop=90.0,
                          multipliers={"custom": 0.5, "custom2": 0.5})
    text = sp.format_size_lines_he(r, FULL_RISK, equity_usd=100000.0)
    assert "חצי פוזיציה" in text
    assert "100" in text            # the full-position share count, for comparison
    assert "0.50%" in text          # this trade's real risk as a % of the account
    assert "הוגדל בחזרה" in text     # says out loud that the floor was applied


def test_telegram_block_for_an_unsizeable_trade():
    r = sp.size_position(FULL_RISK, entry=5000.0, stop=3000.0)
    text = sp.format_size_lines_he(r, FULL_RISK)
    assert "🚫" in text
    assert "פוזיציה מלאה" in text


def test_telegram_block_warns_when_an_existing_order_is_out_of_bounds():
    r = sp.evaluate_order(74, entry=227.62, stop=182.07, risk_usd_target=FULL_RISK)
    text = sp.format_size_lines_he(r, FULL_RISK, equity_usd=100000.0)
    assert "⚠️" in text
    assert "חורג" in text


def test_no_multiplier_note_when_nothing_was_clamped():
    r = sp.size_position(FULL_RISK, entry=100.0, stop=90.0, multipliers={"custom": 0.75})
    text = sp.format_size_lines_he(r, FULL_RISK, equity_usd=100000.0)
    assert "🛟" not in text


# --- full size is the default, not the ceiling (2026-08-09) -----------------
#
# Once every derate was removed on 2026-08-03, the 0.5-1.0 band stopped
# describing a range to choose inside: with nothing left that may legitimately
# shrink a position, the only correct answer is one full risk unit. These cover
# the separate line that says so, and the real exception (cash) that keeps a
# genuinely small fill legal.

def test_full_size_order_reads_as_full_size():
    r = sp.evaluate_order(170, entry=132.40, stop=124.09, risk_usd_target=FULL_RISK)
    assert sp.is_full_size(r.risk_fraction_of_full)


def test_half_position_is_legal_but_not_full_size():
    r = sp.evaluate_order(90, entry=132.40, stop=124.09, risk_usd_target=FULL_RISK)
    assert sp.fraction_within_bounds(r.risk_fraction_of_full)
    assert not sp.is_full_size(r.risk_fraction_of_full)


def test_whole_share_rounding_just_under_the_line_still_reads_as_full():
    # Rounding a cent under 0.9 must not read as a deliberate derate -- the same
    # tolerance the bounds check already allows.
    assert sp.is_full_size(sp.RISK_FRACTION_FULL_MIN - sp.RISK_FRACTION_TOLERANCE / 2)


def test_telegram_block_warns_on_a_small_order_with_no_reason():
    r = sp.evaluate_order(90, entry=132.40, stop=124.09, risk_usd_target=FULL_RISK)
    text = sp.format_size_lines_he(r, FULL_RISK, equity_usd=100000.0)
    assert "אין סיבה רשומה" in text


def test_telegram_block_states_the_reason_instead_of_warning():
    r = sp.evaluate_order(90, entry=132.40, stop=124.09, risk_usd_target=FULL_RISK)
    text = sp.format_size_lines_he(r, FULL_RISK, equity_usd=100000.0,
                                   reduction_reason="cash_limited")
    assert "cash_limited" in text
    assert "אין סיבה רשומה" not in text


def test_full_size_order_gets_no_size_note_either_way():
    r = sp.evaluate_order(170, entry=132.40, stop=124.09, risk_usd_target=FULL_RISK)
    text = sp.format_size_lines_he(r, FULL_RISK, equity_usd=100000.0)
    assert "אין סיבה רשומה" not in text


# --- the CLI door that let a retired derate back in (2026-08-09) ------------
#
# ADVISORY_MULTIPLIER_KEYS can only ignore a derate it can NAME. The CLI used to
# take `--multiplier 0.5` as a bare float, which arrives as an anonymous
# sequence -- so every removed multiplier sailed past the guard written to stop
# it and halved the position for real. The screener prompt was, at the same
# time, still telling the model to pass them.

def test_cli_rejects_an_unnamed_multiplier(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [
        "size_policy.py", "--risk-usd-target", str(FULL_RISK),
        "--entry", "132.40", "--stop", "124.09", "--multiplier", "0.5",
    ])
    with pytest.raises(SystemExit) as exc:
        sp._main()
    assert exc.value.code != 0
    assert "NAME=VALUE" in capsys.readouterr().err


def test_cli_accepts_a_named_multiplier_and_ignores_a_retired_one(monkeypatch, capsys):
    import json
    monkeypatch.setattr("sys.argv", [
        "size_policy.py", "--risk-usd-target", str(FULL_RISK),
        "--entry", "132.40", "--stop", "124.09", "--multiplier", "volume=0.5",
    ])
    sp._main()
    payload = json.loads(capsys.readouterr().out.splitlines()[0])
    # "volume" is retired: shown, never multiplied in -- so this is full size.
    assert payload["multiplier_applied"] == 1.0
    assert payload["is_full_size"] is True
