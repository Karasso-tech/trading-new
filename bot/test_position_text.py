"""Tests for position_text.py -- STRATEGY_v3.md section ח.2's pulse-check line.

Section ח.2 dictates a form: a fixed action word from a closed list of eight,
a fixed price/profit line, then EXACTLY one of three status lines, chosen by
comparing real distances to the 0.7x/1x ATR thresholds. These tests pin that
form, and pin harder on the two places where getting it wrong costs money
rather than looks: which status line is picked, and whether a Starter is told
it may be topped up.
"""

import pytest

import position_text as pt

BASE = dict(ticker="PLTR", action="Hold", price=100.0, entry_price=90.0, qty=50,
            stop=88.0, target=130.0, atr14=4.0)


class TestFixedForm:
    def test_the_line_is_three_lines_when_nothing_extra_applies(self):
        lines = pt.build_position_status(**BASE).split("\n")
        assert len(lines) == 3
        assert lines[0].startswith("✅ <b>PLTR</b> — ")
        assert lines[1].startswith("מחיר עכשיו:")
        assert lines[2] == pt.SAFE_DISTANCE

    def test_the_action_uses_the_fixed_table_never_the_english_word(self):
        text = pt.build_position_status(**{**BASE, "action": "Trim"})
        assert "✂️ <b>PLTR</b> — מוכרים חלק, מקטינים סיכון" in text
        assert "Trim" not in text

    def test_an_unknown_action_is_marked_not_guessed(self):
        text = pt.build_position_status(**{**BASE, "action": "Sell Everything"})
        assert pt.UNKNOWN_ACTION[1] in text
        assert "מחזיקים כרגיל" not in text     # never quietly rendered as Hold

    def test_every_action_strategy_v3_lists_has_one_emoji_and_one_translation(self):
        assert len(pt.ACTION_HE) == 8
        assert len({emoji for emoji, _ in pt.ACTION_HE.values()}) == 8

    def test_numbers_are_bold_and_two_decimals(self):
        text = pt.build_position_status(**BASE)
        assert "<b>100.00</b>" in text
        assert "<b>11.11</b>%" in text          # 100/90 - 1, rounded once, here

    def test_the_sentence_is_only_shown_when_there_is_one(self):
        assert "💬" not in pt.build_position_status(**BASE)
        assert "💬 שינינו סטופ." in pt.build_position_status(**BASE, sentence="שינינו סטופ.")

    def test_tranche_warnings_are_passed_through_word_for_word(self):
        text = pt.build_position_status(**BASE, warnings=["יעד 2 נמכר פעמיים"])
        assert "⚠️ יעד 2 נמכר פעמיים" in text


class TestProfit:
    def test_profit_is_measured_on_the_shares_still_held(self):
        # 20 shares left of an original 50: the sold half is realised money and
        # belongs in the trade's record, not in "how is this doing right now".
        text = pt.profit_line(price=100.0, entry_price=90.0, qty=20)
        assert "($200.00)" in text

    def test_a_loss_is_shown_as_a_loss(self):
        text = pt.profit_line(price=81.0, entry_price=90.0, qty=10)
        assert "<b>-10.00</b>%" in text and "($-90.00)" in text

    def test_an_unknown_entry_price_shows_a_dash_not_a_zero(self):
        # A zero here would read as "flat", which is a different fact.
        assert "רווח/הפסד: <b>—</b>" in pt.profit_line(price=100.0, entry_price=None, qty=10)


class TestExactlyOneStatusLine:
    def test_close_to_stop_wins_when_both_are_close(self):
        # 0.5 ATR from the stop AND 0.5 ATR from the target -- risk is the
        # thing worth saying, so the document orders the checks this way.
        line = pt.status_line(price=100.0, stop=98.0, target=102.0, atr14=4.0)
        assert line == pt.CLOSE_TO_STOP.format(stop="<b>98.00</b>")

    def test_the_stop_line_fires_just_inside_the_threshold(self):
        assert pt.status_line(price=100.0, stop=97.2, atr14=4.0,
                               target=None) == pt.CLOSE_TO_STOP.format(stop="<b>97.20</b>")
        # 0.7 x 4 = 2.8 exactly -- not inside it.
        assert pt.status_line(price=100.0, stop=97.2 - 0.001, atr14=4.0,
                               target=None) == pt.SAFE_DISTANCE

    def test_approaching_target_fires_inside_one_atr(self):
        line = pt.status_line(price=100.0, stop=80.0, target=103.0, atr14=4.0)
        assert line == pt.APPROACHING_TARGET.format(target="<b>103.00</b>")

    def test_a_core_position_never_gets_the_target_line(self):
        # And it says so in its own words: an index fund nobody set a target
        # for is not "a position whose targets are all sold".
        line = pt.status_line(price=100.0, stop=80.0, target=103.0, atr14=4.0,
                               sleeve="core", runner_only=True)
        assert line == pt.SAFE_DISTANCE_CORE

    def test_a_core_position_still_gets_the_stop_line_when_it_is_close(self):
        line = pt.status_line(price=100.0, stop=98.0, target=None, atr14=4.0,
                               sleeve="core", runner_only=True)
        assert line == pt.CLOSE_TO_STOP.format(stop="<b>98.00</b>")

    def test_a_runner_says_there_is_no_target_left(self):
        # The ASTS lesson: a level that was already sold must never be shown as
        # an approaching target.
        line = pt.status_line(price=100.0, stop=80.0, target=103.0, atr14=4.0,
                               runner_only=True)
        assert line == pt.SAFE_DISTANCE_RUNNER

    def test_with_no_atr_figure_no_distance_is_claimed(self):
        line = pt.status_line(price=100.0, stop=99.9, target=100.1, atr14=None)
        assert line == pt.SAFE_DISTANCE

    def test_exactly_one_status_line_appears_in_a_built_block(self):
        text = pt.build_position_status(**{**BASE, "stop": 98.0})
        shown = [candidate for candidate in
                 (pt.CLOSE_TO_STOP.format(stop="<b>98.00</b>"),
                  pt.APPROACHING_TARGET.format(target="<b>130.00</b>"),
                  pt.SAFE_DISTANCE, pt.SAFE_DISTANCE_RUNNER, pt.SAFE_DISTANCE_CORE)
                 if candidate in text]
        assert len(shown) == 1


class TestTargetProgressLine:
    """The 🎯 line (2026-08-12, the owner's GLD question): which numbered
    target is next, and which are already sold. Built from the tranche plan
    the database attaches to the position, never from the payload."""

    def _tranche(self, n, price, planned, filled=0, status="waiting"):
        return {"label": f"target_{n}", "price": price, "planned_qty": planned,
                "filled_qty": filled, "status": status}

    def test_nothing_sold_shows_the_first_target_and_its_share_count(self):
        line = pt.target_progress_line([self._tranche(1, 130.0, 20),
                                         self._tranche(2, 150.0, 17)])
        assert line == "🎯 היעד הבא: <b>130.00</b> — מוכרים שם 20 יחידות"

    def test_a_sold_target_is_named_as_sold_and_the_next_one_shown(self):
        # The GLD question in one test: target 1 realised, target 2 is next.
        line = pt.target_progress_line([self._tranche(1, 130.0, 20, 20, "filled"),
                                         self._tranche(2, 150.0, 17)])
        assert line == "יעד 1 כבר מומש · 🎯 היעד הבא: <b>150.00</b> — מוכרים שם 17 יחידות"

    def test_two_sold_targets_are_both_named(self):
        line = pt.target_progress_line([self._tranche(1, 120.0, 15, 15, "filled"),
                                         self._tranche(2, 130.0, 15, 15, "filled"),
                                         self._tranche(3, 150.0, 10)])
        assert line.startswith("יעדים 1+2 כבר מומשו · ")

    def test_a_partially_sold_target_stays_the_next_one_with_what_is_left(self):
        line = pt.target_progress_line([self._tranche(1, 130.0, 20, 8, "partial")])
        assert line == "🎯 היעד הבא: <b>130.00</b> — מוכרים שם 12 יחידות"

    def test_no_line_once_every_numbered_target_is_sold(self):
        # The runner wording of the status line already covers this case.
        assert pt.target_progress_line([self._tranche(1, 130.0, 20, 20, "filled"),
                                         {"label": "runner", "price": None,
                                          "planned_qty": 30, "filled_qty": 0,
                                          "status": "waiting"}]) is None

    def test_no_line_for_a_core_holding_or_an_empty_plan(self):
        assert pt.target_progress_line([self._tranche(1, 130.0, 20)], sleeve="core") is None
        assert pt.target_progress_line([]) is None
        assert pt.target_progress_line(None) is None

    def test_the_line_lands_in_the_built_block_after_the_status_line(self):
        text = pt.build_position_status(**BASE, tranches=[self._tranche(1, 130.0, 20)])
        lines = text.split("\n")
        assert lines[2] == pt.SAFE_DISTANCE
        assert lines[3].startswith("🎯 היעד הבא:")


class TestThePlaybookReview:
    """Section ח.1 -- the daily review. Same opening two lines as the pulse
    check, then the levels the longer report has room for."""

    # The review has no ATR thresholds to apply -- it shows the levels rather
    # than judging distances -- so it takes the position's facts and its still
    # unsold targets, nothing else.
    POSITION = dict(ticker="PLTR", action="Hold", price=100.0, entry_price=90.0,
                    qty=50, stop=88.0,
                    targets=[{"price": 130.0, "pct": 40.0}, {"price": 150.0, "pct": 35.0}])

    def test_the_message_has_a_header_the_positions_and_a_closing_line(self):
        text = pt.build_playbook_summary(date="2026-08-10", regime_he="מגמה עולה בריאה",
                                          positions=[self.POSITION])
        assert text.count(pt.SEP) == 2
        assert text.startswith(pt.PLAYBOOK_HEAD.format(date="2026-08-10"))
        assert "⭐ מצב שוק כללי: מגמה עולה בריאה" in text
        assert text.rstrip().endswith(pt.PLAYBOOK_CLOSER)

    def test_the_target_line_says_what_the_move_is_worth_in_plain_percent(self):
        # 130 from 100 is +30.00%, computed here -- no ratios, no ATR multiples.
        line = pt.playbook_target_line(100.0, [{"price": 130.0, "pct": 40.0}])
        assert "🏆 יעד: <b>130.00</b> — מוכרים <b>40</b>% (עלייה של <b>+30.00%</b> מכאן)" == line

    def test_a_second_target_rides_on_the_same_line(self):
        line = pt.playbook_target_line(100.0, self.POSITION["targets"])
        assert "יעד נוסף: <b>150.00</b>, מוכרים <b>35</b>%" in line

    def test_a_core_holding_has_no_target_line_at_all(self):
        assert pt.playbook_target_line(100.0, [{"price": 130.0, "pct": 40.0}],
                                        sleeve="core") is None

    def test_a_position_with_every_target_sold_says_it_rides_the_stop(self):
        assert pt.playbook_target_line(100.0, [], runner_only=True) == pt.TARGET_RUNNER

    def test_the_stop_is_always_shown_with_its_real_price(self):
        # Section ח.1: never a qualitative-only line. The number is the point.
        text = pt.playbook_position_block(**self.POSITION)
        assert "🛑 סטופ: <b>88.00</b>" in text

    def test_the_first_two_lines_match_the_pulse_check_exactly(self):
        review = pt.playbook_position_block(**self.POSITION).split("\n")[:2]
        pulse = pt.build_position_status(**BASE).split("\n")[:2]
        assert review == pulse

    def test_no_positions_says_so_rather_than_showing_an_empty_section(self):
        text = pt.build_playbook_summary(date="2026-08-10", positions=[])
        assert "אין פוזיציות פתוחות כרגע." in text


class TestTheMismatchBlock:
    """The one place the system says its own records are behind. The exact
    command, with the real numbers in it, is the whole point."""

    def test_shares_sold_but_not_reported_asks_for_exit(self):
        block = pt.mismatch_block(ticker="ASTS", kind="sold",
                                   command="/exit ASTS 71.94 20")
        assert pt.MISMATCH_SOLD in block
        assert "<code>/exit ASTS 71.94 20</code>" in block

    def test_shares_bought_but_not_reported_asks_for_add(self):
        block = pt.mismatch_block(ticker="NBIS", kind="added",
                                   command="/add NBIS 187.97 10")
        assert pt.MISMATCH_ADDED in block

    def test_a_ticker_this_system_never_saw_asks_for_filled(self):
        block = pt.mismatch_block(ticker="GOOGL", kind="new",
                                   command="/filled GOOGL 210.00 30 full")
        assert pt.MISMATCH_NEW in block

    def test_the_alerts_come_before_the_positions(self):
        # A position line built on a share count the system knows is wrong must
        # not be read before the note that says so.
        text = pt.build_playbook_summary(
            date="2026-08-10", positions=[dict(TestThePlaybookReview.POSITION)],
            mismatches=[{"ticker": "PLTR", "kind": "sold", "command": "/exit PLTR 100.00 20"}])
        assert text.index("🔴") < text.index("✅ <b>PLTR</b>")
        assert text.count(pt.SEP) == 3

    def test_no_alert_block_when_everything_agrees(self):
        text = pt.build_playbook_summary(date="2026-08-10",
                                          positions=[dict(TestThePlaybookReview.POSITION)])
        assert "🔴" not in text


class TestStarter:
    STARTER = dict(BASE, entry_type="starter", trigger=95.0, add_to_full_qty=40)

    def test_a_full_position_never_shows_the_starter_line(self):
        assert "🌱" not in pt.build_position_status(**{**self.STARTER,
                                                       "entry_type": "full"})

    def test_a_settled_close_above_the_trigger_confirms(self):
        text = pt.build_position_status(**self.STARTER, last_bar_close=96.0, bar_fresh=True)
        assert "🌱 ✅" in text and "<b>40</b>" in text

    def test_an_unsettled_bar_never_confirms_however_high_it_is(self):
        text = pt.build_position_status(**self.STARTER, last_bar_close=120.0, bar_fresh=False)
        assert "🌱 ⏳" in text

    def test_a_close_below_the_trigger_never_confirms(self):
        text = pt.build_position_status(**self.STARTER, last_bar_close=94.99, bar_fresh=True)
        assert "🌱 ⏳" in text

    def test_a_thesis_with_no_full_size_says_the_quantity_is_unknown(self):
        text = pt.build_position_status(**{**self.STARTER, "add_to_full_qty": None},
                                         last_bar_close=96.0, bar_fresh=True)
        assert "הכמות המדויקת לא ידועה" in text

    def test_already_at_full_size_asks_for_nothing(self):
        text = pt.build_position_status(**{**self.STARTER, "add_to_full_qty": 0},
                                         last_bar_close=96.0, bar_fresh=True)
        assert "אין תוספת נדרשת" in text

    def test_no_trigger_on_file_drops_the_line_rather_than_inviting_a_blind_add(self):
        assert pt.starter_line(trigger=None, add_to_full_qty=40, last_bar_close=96.0,
                               bar_fresh=True) is None

    def test_an_old_starter_says_how_long_it_has_been_one(self):
        text = pt.build_position_status(**self.STARTER, last_bar_close=90.0, bar_fresh=True,
                                         days_since_starter=7, starter_stale=True)
        assert "כבר 7 ימי מסחר כ-Starter" in text
