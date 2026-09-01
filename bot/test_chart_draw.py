"""Unit tests for chart_draw.py's trigger-level extraction (2026-07-14).

_extract_trigger_level() is a best-effort visualization convenience -- pulling a
price or price-range out of a trigger field that may be a plain number, a range
("retest 126-128"), or a full condition sentence with a number embedded in it
("daily close above 136.10", per SCREENER_v3.md rule 14). Never used for
arithmetic/consistency checks (report_lint.py's own _clean_number stays strict
and untouched for that).

Two real regressions were caught by hand while building this and are locked in
here: "reclaim SMA20" was wrongly extracting 20 (the moving-average length, not
a price), and "סגירה 2H מתחת ל-183" (2H close below 183) was wrongly grabbing 2
(from "2H") instead of the real price 183."""

import json

from chart_draw import (ALTERNATE, POSITION, PRIMARY, _extract_trigger_level,
                        _lines_for_position, _lines_for_setup)


def test_plain_number_string():
    assert _extract_trigger_level("136.10") == ("price", 136.10)


def test_plain_float():
    assert _extract_trigger_level(136.10) == ("price", 136.10)


def test_prose_with_embedded_price_pltr_real_case():
    assert _extract_trigger_level("סגירה יומית מעל 136.10") == ("price", 136.10)


def test_plain_range():
    assert _extract_trigger_level("retest 126-128") == ("range", 126.0, 128.0)


def test_hebrew_prose_with_embedded_range():
    result = _extract_trigger_level("משיכה מחדש ל-127-128 עם נר אישור")
    assert result == ("range", 127.0, 128.0)


def test_prose_with_no_real_price_is_skipped():
    # "SMA20" must not be misread as a price of 20 -- regression check.
    assert _extract_trigger_level("reclaim SMA20") is None


def test_timeframe_notation_does_not_shadow_the_real_price():
    # "2H" must not be misread as a price of 2 -- regression check.
    assert _extract_trigger_level("סגירה 2H מתחת ל-183") == ("price", 183.0)


def test_multiplier_notation_does_not_interfere_with_a_real_range():
    result = _extract_trigger_level("close above 0.7x ATR from 189.8-191.14 base")
    assert result == ("range", 189.8, 191.14)


def test_number_immediately_followed_by_letters_is_skipped():
    assert _extract_trigger_level("EMA9 reclaim") is None


def test_missing_trigger_is_skipped():
    assert _extract_trigger_level(None) is None


def test_lines_for_setup_range_trigger_produces_a_rectangle():
    setup = {
        "type": "Retest 126-128 (Pending)",
        "trigger": "משיכה מחדש ל-127-128 עם נר אישור",
        "stop": 124.0,
        "targets": [{"price": "165.08", "pct": "40%", "status": "pass"}],
    }
    lines = _lines_for_setup(setup, PRIMARY, dashed=False, prefix="")
    rect = next(l for l in lines if l["shape"] == "rectangle")
    assert rect["low"] == 127.0 and rect["high"] == 128.0
    assert rect["text"] == "Retest 126-128 (Pending)"
    stop_line = next(l for l in lines if l["text"] == "Stop")
    assert stop_line["shape"] == "horizontal_line" and stop_line["price"] == 124.0


def test_rectangle_overrides_use_rectangle_specific_property_names():
    # Found real, 2026-07-14: passing horizontal_line's override keys
    # (linecolor/linewidth) to a rectangle was silently ignored by TradingView
    # (draw_get_properties showed its own unrelated defaults instead) --
    # rectangles need color/backgroundColor/textColor, not linecolor/textcolor.
    setup = {"type": "Retest 126-128 (Pending)", "trigger": "126-128", "stop": None}
    lines = _lines_for_setup(setup, PRIMARY, dashed=False, prefix="")
    rect = next(l for l in lines if l["shape"] == "rectangle")
    overrides = json.loads(rect["overrides"])
    assert "color" in overrides and "backgroundColor" in overrides and "textColor" in overrides
    assert "linecolor" not in overrides


def test_lines_for_setup_numeric_trigger_produces_a_horizontal_line_labeled_with_type():
    setup = {"type": "Reclaim", "trigger": "169.52", "stop": 160.0}
    lines = _lines_for_setup(setup, PRIMARY, dashed=False, prefix="")
    trigger_line = next(l for l in lines if l["price"] == 169.52)
    assert trigger_line["shape"] == "horizontal_line"
    assert trigger_line["text"] == "Reclaim"


def test_lines_for_setup_alternate_prefix_applies_to_trigger_label():
    setup = {"type": "Pullback", "trigger": "25.10", "stop": 24.0}
    lines = _lines_for_setup(setup, ALTERNATE, dashed=True, prefix="Alt ")
    trigger_line = next(l for l in lines if l["price"] == 25.10)
    assert trigger_line["text"] == "Alt Pullback"


def test_lines_for_setup_missing_type_falls_back_to_generic_trigger_label():
    setup = {"trigger": "50.0", "stop": 48.0}
    lines = _lines_for_setup(setup, PRIMARY, dashed=False, prefix="")
    trigger_line = next(l for l in lines if l["price"] == 50.0)
    assert trigger_line["text"] == "Trigger"


def test_checkpoints_only_draw_wall_boundaries_not_every_member():
    # Found real, 2026-07-15/16: a 12-touch wall was drawing all 12 as separate
    # dashed lines -- unreadable clutter on the live chart. Only the wall's two
    # edges are decision-relevant (SCREENER_v3.md's own wording for the
    # interior members is "not tested individually"), so only min/max survive.
    setup = {
        "trigger": "50.0", "stop": 48.0,
        "checkpoints": [{"price": p} for p in
                         ["67253.17", "68668", "69268", "70038", "70937", "79490"]],
    }
    lines = _lines_for_setup(setup, PRIMARY, dashed=False, prefix="")
    cp_prices = sorted(l["price"] for l in lines if l["text"] == "Checkpoint")
    assert cp_prices == [67253.17, 79490.0]


def test_checkpoints_duplicate_price_within_one_setup_collapses_to_one_line():
    setup = {"trigger": "50.0", "stop": 48.0, "checkpoints": [{"price": "100"}, {"price": "100"}]}
    lines = _lines_for_setup(setup, PRIMARY, dashed=False, prefix="")
    cp_lines = [l for l in lines if l["text"] == "Checkpoint"]
    assert len(cp_lines) == 1 and cp_lines[0]["price"] == 100.0


def test_checkpoints_single_unique_price_draws_once():
    setup = {"trigger": "50.0", "stop": 48.0, "checkpoints": [{"price": "100"}]}
    lines = _lines_for_setup(setup, PRIMARY, dashed=False, prefix="")
    cp_lines = [l for l in lines if l["text"] == "Checkpoint"]
    assert len(cp_lines) == 1 and cp_lines[0]["price"] == 100.0


def test_checkpoint_vert_alignment_follows_primary_vs_alternate():
    # The earlier collision bug: checkpoint styling hardcoded dashed=True for
    # BOTH primary and alternate, so identical-price checkpoints (common when
    # both setups reference the same wall) stacked their labels on the same
    # side and rendered as overlapping/garbled text.
    setup = {"trigger": "50.0", "stop": 48.0, "checkpoints": [{"price": "100"}]}
    primary_line = _lines_for_setup(setup, PRIMARY, dashed=False, prefix="")[-1]
    alt_line = _lines_for_setup(setup, ALTERNATE, dashed=True, prefix="Alt ")[-1]
    assert json.loads(primary_line["overrides"])["vertLabelsAlign"] == "bottom"
    assert json.loads(alt_line["overrides"])["vertLabelsAlign"] == "top"


# ---------------------------------------------------------------------------
# Open-position lines (2026-08-07). The NOW incident: a position held since
# 2026-08-04 (entry 115.68, stop 105.02) had its chart drawn from the stored
# THESIS -- Primary plus Alternate -- so nine lines were up, including the
# Alternate's own stop at 100.49 for a trade that was never taken. Two stop
# lines, one real. These lock in that a held ticker draws the position instead.
# ---------------------------------------------------------------------------

def _now_position(**overrides):
    """The real NOW position row, as persistence hands it out."""
    pos = {
        "ticker": "NOW", "entry_price": 115.68, "qty": 138, "remaining_qty": 138,
        "initial_stop": 105.02, "current_stop": 105.02,
        "entry_setup": {
            "type": "Breakout", "trigger": 113.79, "stop": 105.02,
            "targets": [{"price": 136.63, "pct": "40", "status": "pass"}],
            "checkpoints": [{"price": "118.99"}, {"price": "126.67"}],
        },
        "tranche_plan": {
            "tranches": [
                {"label": "target_1", "price": 136.63, "planned_pct": 40.0,
                 "planned_qty": 55, "filled_qty": 0, "status": "waiting"},
                {"label": "runner", "price": None, "planned_pct": 60.0,
                 "planned_qty": 83, "filled_qty": 0, "status": "waiting"},
            ],
        },
    }
    pos.update(overrides)
    return pos


def test_position_draws_entry_stop_and_target_only():
    lines = _lines_for_position(_now_position())
    assert [l["price"] for l in lines] == [115.68, 105.02, 136.63]
    assert [l["text"] for l in lines] == ["Entry 138 sh", "Stop", "Target 1 (40%)"]


def test_position_never_draws_the_alternate_stop_the_now_incident():
    # The whole point: only ONE stop line, and it is the live one.
    lines = _lines_for_position(_now_position())
    stops = [l for l in lines if l["text"].startswith("Stop")]
    assert len(stops) == 1 and stops[0]["price"] == 105.02


def test_position_never_draws_the_already_happened_trigger_or_checkpoints():
    texts = [l["text"] for l in _lines_for_position(_now_position())]
    assert not any("Breakout" in t or "Checkpoint" in t for t in texts)


def test_position_uses_the_trailed_stop_and_says_so():
    lines = _lines_for_position(_now_position(current_stop=112.40))
    stop = next(l for l in lines if l["text"].startswith("Stop"))
    assert stop["price"] == 112.40 and stop["text"] == "Stop (trailed)"


def test_position_falls_back_to_initial_stop_when_current_is_missing():
    lines = _lines_for_position(_now_position(current_stop=None))
    stop = next(l for l in lines if l["text"].startswith("Stop"))
    assert stop["price"] == 105.02 and stop["text"] == "Stop"


def test_sold_target_loses_its_line_rule_30():
    pos = _now_position()
    pos["tranche_plan"]["tranches"][0].update({"filled_qty": 55, "status": "filled"})
    pos["remaining_qty"] = 83
    lines = _lines_for_position(pos)
    assert [l["text"] for l in lines] == ["Entry 83 sh", "Stop"]


def test_partially_sold_target_keeps_its_line_and_is_labeled():
    pos = _now_position()
    pos["tranche_plan"]["tranches"][0].update({"filled_qty": 20, "status": "partial"})
    target = _lines_for_position(pos)[-1]
    assert target["price"] == 136.63 and target["text"] == "Target 1 (40%) partial"


def test_runner_tranche_has_no_price_and_draws_nothing():
    pos = _now_position()
    pos["tranche_plan"]["tranches"][0].update({"filled_qty": 55, "status": "filled"})
    assert not [l for l in _lines_for_position(pos) if "Runner" in l["text"]]


def test_entry_line_uses_remaining_shares_not_the_original_fill():
    lines = _lines_for_position(_now_position(remaining_qty=83))
    assert lines[0]["text"] == "Entry 83 sh"


def test_entry_line_omits_share_count_when_unknown():
    pos = _now_position(remaining_qty=None)
    pos.pop("qty")
    assert _lines_for_position(pos)[0]["text"] == "Entry"


def test_position_lines_are_all_solid_and_bottom_labeled():
    # No primary-vs-alternate distinction survives entry, so nothing is dashed
    # and no label needs the opposite-side collision dodge.
    for line in _lines_for_position(_now_position()):
        o = json.loads(line["overrides"])
        assert "linestyle" not in o
        assert o["vertLabelsAlign"] == "bottom"


def test_position_entry_color_is_distinct_from_stop_and_target():
    lines = _lines_for_position(_now_position())
    assert json.loads(lines[0]["overrides"])["linecolor"] == POSITION["entry"]
    assert json.loads(lines[1]["overrides"])["linecolor"] == POSITION["stop"]
    assert json.loads(lines[2]["overrides"])["linecolor"] == POSITION["target"]


def test_position_without_tranche_plan_falls_back_to_entry_setup_targets():
    pos = _now_position()
    pos.pop("tranche_plan")
    assert [l["text"] for l in _lines_for_position(pos)][-1] == "Target 1 (40%)"


def test_position_with_entry_setup_stored_as_json_string_still_draws_targets():
    pos = _now_position()
    pos.pop("tranche_plan")
    pos["entry_setup"] = json.dumps(pos["entry_setup"])
    assert [l["text"] for l in _lines_for_position(pos)][-1] == "Target 1 (40%)"


def test_no_position_draws_nothing():
    assert _lines_for_position(None) == []
