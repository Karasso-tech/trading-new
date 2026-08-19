"""Tests for monitor_text.py -- MONITOR_v2.md section ו.3's scan templates.

Same posture as test_summary_text.py. Section ו.3 dictates a form, not a
style: fixed headers, fixed emoji, fixed line order, every number bold and
rounded to two decimals, a fixed translation table for the rubric criteria and
for the "cannot be scored" reasons. These tests pin that form, because its
failure mode is silent -- nobody spots a reworded line in a message that goes
out twice a day, they only notice that two scans look different.

The rules that carry money, rather than looks, get their own tests: a D grade
and a stale trigger must never print an order or a Starter quantity, and a
thesis with no numeric trigger must say so instead of crashing.
"""

import pytest

import monitor_text as mt

GREEN = dict(ticker="PLTR", status="green", sentence="המניה פרצה וסגרה מעל הרמה.",
             price=172.01, trigger=165.08, grade_now="B",
             entry=165.08, stop=159.44, qty=275)

YELLOW_PLUS = dict(ticker="XLF", status="yellow_plus", sentence="המחיר התקרב לרמה.",
                   price=57.6, trigger=58.41, grade_now="C", starter_qty=30)

ALL_PASS = {"rr": True, "target_atr": True, "regime": True, "rs": True,
            "sma20_extension": True, "event": True}


class TestFixedForm:
    def test_the_green_line_has_its_six_fixed_lines_in_order(self):
        lines = mt.build_scan_headline(**GREEN).split("\n")
        assert len(lines) == 6
        assert lines[0].startswith("🟢") and "הטריגר הופעל!" in lines[0]
        assert lines[1].startswith("🟢 כניסה:") and "🛑 סטופ:" in lines[1]
        assert lines[2].startswith("🛒 כמות לפוזיציה מלאה:")
        assert lines[3].startswith("✅ ציון רובריקה חי:")
        assert lines[4].startswith("💬")
        assert lines[5] == mt.FILLED_LINE

    def test_the_yellow_plus_line_has_its_five_fixed_lines_in_order(self):
        lines = mt.build_scan_headline(**YELLOW_PLUS).split("\n")
        assert len(lines) == 5
        assert lines[0].startswith("🟡➕")
        assert lines[1].startswith("מחיר עכשיו:") and "טריגר:" in lines[1]
        assert lines[2].startswith("🛒 אפשרות כניסה חלקית (Starter):")
        assert lines[3].startswith("✅ ציון רובריקה חי:")
        assert lines[4].startswith("💬")

    def test_the_two_facts_on_one_line_are_separated_by_the_fixed_gap(self):
        text = mt.build_scan_headline(**YELLOW_PLUS)
        assert f"<b>57.60</b>{mt.GAP}טריגר:" in text
        assert "\t" not in text

    def test_every_price_is_bold_and_two_decimals(self):
        text = mt.build_scan_headline(**GREEN)
        assert "<b>165.08</b>" in text and "<b>159.44</b>" in text
        assert "165.0800" not in text
        # 57.6 shows as 57.60, never 57.6
        assert "<b>57.60</b>" in mt.build_scan_headline(**YELLOW_PLUS)

    def test_the_cost_is_the_quantity_times_the_entry(self):
        text = mt.build_scan_headline(**GREEN)
        assert "<b>275 מניות</b>" in text
        assert "($45,397.00)" in text     # 275 x 165.08, computed here

    def test_only_green_and_yellow_plus_get_a_line(self):
        for status in ("white", "yellow", "red", None):
            assert mt.build_scan_headline(ticker="XLF", status=status) is None

    def test_the_live_grade_is_shown_on_every_ordinary_line(self):
        # Section ו.3, 2026-08-08: not only on the blocked ones -- a C that
        # carried an order and an A that carried it must not look identical.
        for kwargs in (GREEN, YELLOW_PLUS):
            assert "✅ ציון רובריקה חי:" in mt.build_scan_headline(**kwargs)

    def test_a_missing_sentence_is_marked_missing_not_filled_in(self):
        text = mt.build_scan_headline(**{**GREEN, "sentence": None})
        assert mt.MISSING_SENTENCE in text


class TestWeakenedSetup:
    """Rule 27. A D grade never carries an order or a Starter quantity, and
    "the setup weakened" alone is not an explanation."""

    WEAK = dict(ticker="CRDO", status="green", sentence="הרווח שנשאר קטן מדי.",
                price=91.2, trigger=88.0, grade_now="D", grade_at_build="B",
                rubric_blocked=True, entry=88.0, stop=83.0, qty=120,
                starter_qty=30,
                criteria={**ALL_PASS, "rr": False, "target_atr": False})

    def test_a_blocked_ticker_shows_no_order_and_no_starter(self):
        text = mt.build_scan_headline(**self.WEAK)
        assert "🛒" not in text
        assert "/filled" not in text

    def test_a_d_grade_blocks_even_when_the_payload_forgot_to_say_so(self):
        text = mt.build_scan_headline(**{**self.WEAK, "rubric_blocked": False})
        assert "🛒" not in text
        assert mt.HEAD_WEAKENED.format(ticker="CRDO") in text

    def test_it_lists_exactly_the_criteria_that_failed(self):
        text = mt.build_scan_headline(**self.WEAK)
        assert mt.CRITERIA_HE["rr"] in text
        assert mt.CRITERIA_HE["target_atr"] in text
        assert mt.CRITERIA_HE["rs"] not in text
        assert "rr" not in text.replace("<b>", "")   # never the raw key name

    def test_both_grades_are_shown_side_by_side(self):
        text = mt.build_scan_headline(**self.WEAK)
        assert "<b>B</b>" in text and "<b>D</b>" in text

    def test_criteria_that_were_never_measured_are_not_reported_as_failed(self):
        text = mt.build_scan_headline(**{**self.WEAK, "criteria": {"rr": None}})
        assert mt.NO_CRITERIA in text

    def test_every_criterion_the_formula_reports_has_a_translation(self):
        import rubric_formula
        graded = rubric_formula.classify_rubric(rubric_formula.RubricInputs(
            rr=1.0, target_atr_multiple=1.0, regime="risk_off", rs_delta_pct=-1.0,
            dist_sma20_atr=9.0, earnings_days_out=1))
        assert set(graded.criteria) == set(mt.CRITERIA_HE)
        assert set(graded.criteria) == set(mt.CRITERIA_ORDER)


class TestCannotBeScored:
    """Section ו.3 is explicit: a grade that could not be computed is not a
    failing grade. Different situation, different words, and no buy order in
    either."""

    UNSCORED = dict(ticker="ONDS", status="green", sentence="חסר יעד בתזה.",
                    price=9.11, trigger=8.88, grade_now=None,
                    ungradeable_reason="no_numeric_target",
                    entry=8.88, stop=8.2, qty=400)

    def test_it_uses_its_own_header_not_the_weakened_one(self):
        text = mt.build_scan_headline(**self.UNSCORED)
        assert text.startswith(mt.HEAD_UNGRADEABLE_GREEN.format(ticker="ONDS"))
        assert "הסטאפ נחלש" not in text

    def test_a_near_miss_says_near_not_fired(self):
        text = mt.build_scan_headline(**{**self.UNSCORED, "status": "yellow_plus"})
        assert text.startswith(mt.HEAD_UNGRADEABLE_NEAR.format(ticker="ONDS"))

    def test_it_shows_no_order(self):
        text = mt.build_scan_headline(**self.UNSCORED)
        assert "🛒" not in text and "/filled" not in text

    def test_the_reason_is_translated_never_the_raw_token(self):
        text = mt.build_scan_headline(**self.UNSCORED)
        assert mt.UNGRADEABLE_HE["no_numeric_target"] in text
        assert "no_numeric_target" not in text

    def test_no_grade_at_all_still_blocks_the_order(self):
        # Rule 27's gate cannot be checked against a grade nobody has, so a
        # payload that simply forgot to copy the figures gets no buy order.
        text = mt.build_scan_headline(**{**self.UNSCORED, "ungradeable_reason": None})
        assert "🛒" not in text
        assert mt.UNGRADEABLE_UNKNOWN in text

    def test_an_unknown_reason_says_so_rather_than_printing_a_token(self):
        text = mt.build_scan_headline(**{**self.UNSCORED,
                                          "ungradeable_reason": "something_new"})
        assert mt.UNGRADEABLE_UNKNOWN in text
        assert "something_new" not in text

    def test_every_reason_fetch_monitor_data_can_return_has_a_translation(self):
        import re
        from pathlib import Path
        source = Path(__file__).with_name("fetch_monitor_data.py").read_text(encoding="utf-8")
        emitted = set(re.findall(r'"reason":\s*"([a-z_]+)"', source))
        assert emitted, "no stated reasons found -- the check itself is broken"
        assert emitted <= set(mt.UNGRADEABLE_HE)


class TestStaleTrigger:
    """A trigger that fired days ago is a real fact, but the order sized
    against it is not -- price has moved away from the level the stop was
    measured from."""

    STALE = dict(GREEN, stale_trading_days=11)

    def test_a_stale_green_reports_the_fact_without_an_order(self):
        text = mt.build_scan_headline(**self.STALE)
        assert "🛒" not in text
        assert "ימי מסחר" in text and "<b>11</b>" in text

    def test_it_still_asks_for_filled_because_a_real_fill_may_have_happened(self):
        assert mt.FILLED_LINE in mt.build_scan_headline(**self.STALE)

    def test_a_stale_yellow_plus_offers_no_starter(self):
        text = mt.build_scan_headline(**dict(YELLOW_PLUS, stale_trading_days=11))
        assert "Starter" not in text
        assert "/filled" not in text


class TestHonestGaps:
    def test_a_thesis_with_no_numeric_trigger_says_so(self):
        # Real shape: 26 of 55 stored theses once held a sentence where the
        # trigger should be. It must read as "nothing saved", not as a bug.
        text = mt.build_scan_headline(**{**YELLOW_PLUS, "trigger": None})
        assert mt.NO_TRIGGER in text

    def test_a_half_missing_order_is_stated_never_half_printed(self):
        text = mt.build_scan_headline(**{**GREEN, "stop": None})
        assert mt.NO_ORDER in text
        assert "🛒" not in text

    def test_a_thesis_with_no_planned_quantity_drops_the_starter_line(self):
        text = mt.build_scan_headline(**{**YELLOW_PLUS, "starter_qty": None})
        assert "Starter" not in text
        assert text.count("\n") == 3      # header, prices, grade, sentence


TARGETS = [{"price": 190.0, "pct": 40.0, "rr": 3.1},
           {"price": 210.0, "pct": 35.0, "rr": 4.6}]

CHECK_GREEN = dict(ticker="PLTR", status="green", sentence="הפריצה אושרה בסגירה יומית.",
                   price=172.01, trigger=165.08, setup_type="Breakout",
                   entry=165.08, stop=159.44, qty=275, targets=TARGETS,
                   grade_now="B", grade_at_build="B")


class TestTheFiredTriggerCard:
    """Section ו.1 -- the full order card. Five blocks, four separators."""

    def test_the_message_has_its_five_blocks(self):
        text = mt.build_check_summary(**CHECK_GREEN)
        assert text.count(mt.SEP) == 4
        assert text.startswith(mt.CHECK_HEAD_GREEN.format(ticker="PLTR"))
        assert text.rstrip().endswith(mt.CHECK_FILLED_LINE)

    def test_the_order_card_carries_the_stored_levels_and_targets(self):
        text = mt.build_check_summary(**CHECK_GREEN)
        assert "🟢 כניסה: <b>165.08</b>" in text
        assert "🛑 סטופ: <b>159.44</b>" in text
        assert "🏆 יעד: <b>190.00</b> — מוכרים <b>40</b>%" in text
        assert "יעד נוסף: <b>210.00</b>" in text
        assert "⚖️ יחס סיכון/סיכוי: <b>3.10</b>" in text
        assert "🔄 יתרה (<b>25</b>%)" in text        # 100 - 40 - 35, computed here
        assert "<b>275 מניות</b> ($45,397.00)" in text

    def test_the_setup_name_is_translated_never_the_english_word(self):
        text = mt.build_check_summary(**CHECK_GREEN)
        assert mt.SETUP_HE["Breakout"] in text and "Breakout" not in text

    def test_the_information_only_line_is_always_there(self):
        # It is the reminder that nothing was bought automatically. A reminder
        # that only shows up when something is wrong has not been read by the
        # time it matters.
        assert mt.INFO_ONLY in mt.build_check_summary(**CHECK_GREEN)

    def test_a_thesis_with_no_target_says_so_instead_of_printing_an_empty_line(self):
        text = mt.build_check_summary(**{**CHECK_GREEN, "targets": []})
        assert "אין יעד שמור בתזה" in text


class TestWhatTakesTheOrderCardAway:
    """Four different reasons, each of which drops the whole card and names
    itself. The trigger is still reported as the fact it is."""

    def _has_card(self, text):
        return "📋" in text

    def test_a_weakened_grade(self):
        text = mt.build_check_summary(**{**CHECK_GREEN, "grade_now": "D",
                                          "rubric_blocked": True,
                                          "criteria": {"rr": False}})
        assert not self._has_card(text)
        assert "הסטאפ נחלש" in text and mt.CRITERIA_HE["rr"] in text
        assert mt.CHECK_FILLED_LINE in text     # the trigger is still real

    def test_a_blocking_market(self):
        text = mt.build_check_summary(**{**CHECK_GREEN, "regime_blocked": True,
                                          "regime_at_build_he": "מגמה עולה בריאה",
                                          "regime_now_he": "שוק חלש"})
        assert not self._has_card(text)
        assert "מגמה עולה בריאה" in text and "שוק חלש" in text

    def test_a_trigger_that_fired_days_ago(self):
        text = mt.build_check_summary(**{**CHECK_GREEN, "stale_trading_days": 9})
        assert not self._has_card(text)
        assert "<b>9</b> ימי מסחר" in text

    def test_no_live_grade_at_all(self):
        text = mt.build_check_summary(**{**CHECK_GREEN, "grade_now": None,
                                          "ungradeable_reason": "no_numeric_target"})
        assert not self._has_card(text)
        assert mt.UNGRADEABLE_HE["no_numeric_target"] in text

    def test_the_price_and_the_level_are_still_shown_without_a_card(self):
        text = mt.build_check_summary(**{**CHECK_GREEN, "regime_blocked": True})
        assert "<b>172.01</b>" in text and "<b>165.08</b>" in text


class TestExtraNotes:
    def test_a_drifted_entry_asks_the_reader_to_look_twice(self):
        text = mt.build_check_summary(**CHECK_GREEN,
                                       deviation={"planned": 165.08, "actual": 172.01})
        assert "מתוכנן <b>165.08</b>, בפועל <b>172.01</b>" in text

    def test_the_portfolio_disclosures_use_the_screeners_own_words(self):
        text = mt.build_check_summary(**CHECK_GREEN,
                                       disclosure_flags=["heat", "sector", "cash"])
        for key in ("heat", "sector", "cash"):
            assert mt.DISCLOSURE_LINES[key] in text

    def test_a_disclosure_that_does_not_apply_is_not_shown(self):
        text = mt.build_check_summary(**CHECK_GREEN, disclosure_flags=["heat"])
        assert mt.DISCLOSURE_LINES["heat"] in text
        assert mt.DISCLOSURE_LINES["sector"] not in text


class TestTheWatchingTiers:
    """Section ו.2 -- three short lines, plus only what the situation adds."""

    WATCH = dict(ticker="XLF", status="yellow", sentence="עוד לא סגר מעל הרמה.",
                 price=57.6, trigger=58.41)

    def test_each_tier_has_its_own_fixed_words(self):
        for status, (emoji, words) in mt.STATUS_HE.items():
            text = mt.build_check_summary(**{**self.WATCH, "status": status})
            assert text.startswith(f"{emoji} <b>XLF</b> — {words}")

    def test_a_quiet_tier_is_exactly_three_lines(self):
        assert mt.build_check_summary(**self.WATCH).count("\n") == 2

    def test_a_close_one_offers_the_partial_entry(self):
        text = mt.build_check_summary(**{**self.WATCH, "status": "yellow_plus",
                                          "grade_now": "C", "starter_qty": 30})
        assert "<b>30 מניות</b>" in text
        assert "✅ ציון רובריקה חי: <b>C</b>" in text
        assert mt.STARTER_OR_WAIT in text

    def test_a_weakened_one_replaces_the_offer_rather_than_dropping_it(self):
        # The reader has to be told why yesterday's option is gone.
        text = mt.build_check_summary(**{**self.WATCH, "status": "yellow_plus",
                                          "grade_now": "D", "grade_at_build": "B",
                                          "starter_qty": 30})
        assert "Starter" not in text
        assert "לא מומלצת כניסה חלקית כרגע" in text

    def test_a_stale_one_offers_no_partial_entry_either(self):
        text = mt.build_check_summary(**{**self.WATCH, "status": "yellow_plus",
                                          "grade_now": "C", "starter_qty": 30,
                                          "stale_trading_days": 9})
        assert "Starter" not in text

    def test_a_dropped_idea_says_when_it_was_actually_held(self):
        text = mt.build_check_summary(**{**self.WATCH, "status": "red",
                                          "has_open_position": True})
        assert mt.HELD_POSITION_NOTE in text
        assert mt.HELD_POSITION_NOTE not in mt.build_check_summary(
            **{**self.WATCH, "status": "red"})

    def test_a_thesis_with_no_numeric_trigger_says_so_here_too(self):
        assert mt.NO_TRIGGER in mt.build_check_summary(**{**self.WATCH, "trigger": None})


class TestStarterSize:
    @pytest.mark.parametrize("planned,expected", [
        (100, 30), (275, 82), (1, None), (0, None), (None, None), ("120", 36),
    ])
    def test_it_is_thirty_percent_rounded_down(self, planned, expected):
        assert mt.starter_qty_from_planned(planned) == expected


class TestOrderlessScanLines:
    """The scan's hide rule (2026-08-11). It has to answer the same question
    the templates answer -- "does this line carry an order?" -- so a template
    change can never leave the filter behind, hiding a real buy or showing a
    line the user asked never to see again."""

    def test_a_green_with_a_real_grade_is_not_orderless(self):
        assert mt.scan_line_is_orderless(status="green", grade_now="B") is False

    def test_a_yellow_plus_with_a_real_grade_is_not_orderless(self):
        assert mt.scan_line_is_orderless(status="yellow_plus", grade_now="C") is False

    @pytest.mark.parametrize("kwargs", [
        {"status": "green", "grade_now": None},                     # ONDS: no target to score
        {"status": "green", "grade_now": "D"},                      # LLY: the setup weakened
        {"status": "green", "grade_now": "B", "rubric_blocked": True},
        {"status": "green", "grade_now": "B", "stale_trading_days": 9},
        {"status": "yellow_plus", "grade_now": None},
        {"status": "yellow_plus", "grade_now": "D"},
        {"status": "yellow_plus", "grade_now": "B", "stale_trading_days": 4},
    ])
    def test_every_line_that_carries_no_order_is_orderless(self, kwargs):
        assert mt.scan_line_is_orderless(**kwargs) is True
        # And the answer matches what the template would actually have printed.
        text = mt.build_scan_headline(ticker="X", sentence="—", price=10.0,
                                       trigger=9.0, entry=9.0, stop=8.0, qty=100,
                                       starter_qty=30, **kwargs)
        assert "🛒" not in text

    @pytest.mark.parametrize("status", ["white", "yellow", "red", None])
    def test_a_tier_that_never_gets_a_line_is_not_reported_as_hidden(self, status):
        # These print nothing in a scan anyway -- counting them as hidden would
        # put a ticker in the "fired but no order" line that never fired.
        assert mt.scan_line_is_orderless(status=status, grade_now=None) is False
