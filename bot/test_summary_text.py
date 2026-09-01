"""Tests for summary_text.py -- SCREENER_v3.md section ח's three templates.

Section ח does not describe a style, it dictates a form: fixed emoji, fixed
line order, exactly fifteen separator bars, every number bold and rounded to two
decimals, a fixed translation table for the six setup names, and the choice
between the three templates made mechanically from the decision field. These
tests pin that form, because its failure mode is silent -- nobody notices a
missing separator or a reworded line, they only notice that two reports look
different and stop trusting the format.
"""

import pytest

import summary_text as st

PRIMARY = {
    "type": "Breakout", "trigger": 213.06, "stop": 207.63,
    "targets": [{"price": 232.28, "pct": 40.0, "rr": 3.54},
                {"price": 236.54, "pct": 35.0, "rr": 4.32}],
}
ALTERNATE = {"type": "Pullback", "trigger": 205.0, "stop": 199.0,
             "targets": [{"price": 220.0, "pct": 40.0, "rr": 2.5}]}

BUY_KWARGS = dict(
    ticker="NVDA", grade="B", thesis_sentence="משפט תזה.",
    primary=PRIMARY, alternate=ALTERNATE, potential=254.1,
    disclosure_flags=["regime"], qty=170, cost_usd=36220.2,
    heat_after_pct=4.8, heat_cap_pct=6.0, cash_available_usd=41000.0,
)


class TestFixedForm:
    def test_the_buy_message_has_seven_blocks_and_six_separators(self):
        text = st.build("Buy Now", **BUY_KWARGS)
        assert text.count(st.SEP) == 6

    def test_the_watchlist_message_has_six_blocks_and_five_separators(self):
        text = st.build("Watchlist", ticker="NVDA", grade="B",
                         thesis_sentence="x", primary=PRIMARY, alternate=ALTERNATE,
                         disclosure_flags=["trigger"], wait_for="סגירה מעל 213",
                         invalidation=207.63)
        assert text.count(st.SEP) == 5

    def test_the_separator_is_exactly_fifteen_bars(self):
        assert st.SEP == "━" * 15

    def test_every_price_is_bold_and_two_decimals(self):
        text = st.build("Buy Now", **BUY_KWARGS)
        assert "<b>213.06</b>" in text
        assert "<b>207.63</b>" in text
        assert "<b>254.10</b>" in text        # 254.1 shows as 254.10, never 254.1
        assert "213.0600" not in text

    def test_both_setups_use_the_same_field_emoji_in_the_same_order(self):
        text = st.build("Buy Now", **BUY_KWARGS)
        # Never A/B markers -- section ח is explicit about 🎯 and 🔁.
        assert "🅰️" not in text and "🅱️" not in text
        assert text.count("🟢") == 2 and text.count("🛑") == 2
        assert text.count("🏆") == 2 and text.count("⚖️") == 2

    def test_setup_names_use_the_fixed_translation_table(self):
        text = st.build("Buy Now", **BUY_KWARGS)
        assert st.SETUP_HE["Breakout"] in text
        assert st.SETUP_HE["Pullback"] in text
        assert "Breakout" not in text        # never the raw English label

    def test_every_official_setup_name_has_a_translation(self):
        import setup_types
        assert set(st.SETUP_HE) == set(setup_types.SETUP_TYPES)


class TestTemplateChoiceIsMechanical:
    def test_the_decision_field_picks_the_template(self):
        assert "קנייה עכשיו" in st.build("Buy Now", **BUY_KWARGS)
        assert "קנייה רק לאחר אישור" in st.build("Buy Only If Confirmed", **BUY_KWARGS)
        assert "שווה לעקוב" in st.build(
            "Watchlist", ticker="NVDA", grade="B", thesis_sentence="x",
            primary=PRIMARY, alternate=ALTERNATE)
        assert "לא מספיק טוב כרגע" in st.build(
            "No Trade", ticker="NVDA", grade="D", thesis_sentence="למה לא")

    def test_an_unknown_decision_lands_on_the_shortest_honest_template(self):
        # The message still has to go out; No Trade is the safest shape.
        text = st.build("Something Else", ticker="NVDA", grade="D",
                         thesis_sentence="x")
        assert "לא מספיק טוב כרגע" in text


class TestNothingIsSilentlyOmitted:
    def test_a_setup_with_no_price_says_so_on_its_own_line(self):
        # Rule 14: a required section is never dropped just because nothing
        # currently qualifies.
        text = st.build("Buy Now", **{**BUY_KWARGS,
                                       "alternate": {"type": "Pullback", "trigger": None}})
        assert "עדיין אין מחיר מוגדר לתרחיש הזה" in text

    def test_a_setup_with_no_target_says_so_rather_than_showing_nothing(self):
        primary = {**PRIMARY, "targets": []}
        text = st.build("Buy Now", **{**BUY_KWARGS, "primary": primary})
        assert "עדיין אין יעד כשיר" in text

    def test_the_warning_block_appears_even_when_nothing_applies(self):
        # A reader must be able to tell "nothing to flag" from "the check was
        # skipped". Same reasoning as rule 14.
        text = st.build("Buy Now", **{**BUY_KWARGS, "disclosure_flags": []})
        assert "לפני שקונים, כדאי לדעת" in text
        assert "אין הערות מיוחדות" in text

    def test_a_missing_thesis_sentence_is_marked_not_filled_in(self):
        # The one sentence is genuinely the model's to write; inventing filler
        # would hide that it never arrived.
        text = st.build("Buy Now", **{**BUY_KWARGS, "thesis_sentence": None})
        assert st.MISSING_SENTENCE in text


class TestDisclosures:
    def test_lines_appear_in_the_fixed_order_whatever_order_they_arrive_in(self):
        text = st.build("Buy Now", **{**BUY_KWARGS,
                                       "disclosure_flags": ["cash", "regime", "event"]})
        positions = [text.index(st.DISCLOSURE_LINES[k]) for k in ("regime", "event", "cash")]
        assert positions == sorted(positions)

    def test_an_unknown_flag_is_ignored_rather_than_inventing_a_line(self):
        text = st.build("Buy Now", **{**BUY_KWARGS,
                                       "disclosure_flags": ["regime", "made_up"]})
        assert "made_up" not in text

    def test_the_volume_line_no_longer_claims_it_changes_the_size(self):
        # The x0.5 volume derate was removed on 2026-08-03 (rule 22) -- the
        # backtest measured below-average-volume breaks at +0.13R against
        # +0.08R, so it pointed the wrong way. A line still saying "that is why
        # we buy less" quietly re-introduces a rule the owner deleted.
        line = st.DISCLOSURE_LINES["volume"]
        assert "לא משנה את גודל הקנייה" in line
        assert "קונים כמות קטנה יותר" not in line


class TestAllocation:
    def test_the_runner_percentage_is_computed_not_stated(self):
        text = st.build("Buy Now", **BUY_KWARGS)      # 40 + 35 sold
        assert "<b>25</b>%" in text

    def test_one_target_leaves_a_sixty_percent_runner(self):
        primary = {**PRIMARY, "targets": [{"price": 232.28, "pct": 40.0, "rr": 3.54}]}
        text = st.build("Buy Now", **{**BUY_KWARGS, "primary": primary})
        assert "<b>60</b>%" in text


class TestWatchlistSpecifics:
    def test_no_size_block_because_there_is_no_order_to_price(self):
        text = st.build("Watchlist", ticker="NVDA", grade="B", thesis_sentence="x",
                         primary=PRIMARY, alternate=ALTERNATE)
        assert "גודל הקנייה" not in text

    def test_the_closing_line_has_no_check_mark(self):
        # Nothing was completed, so section ח.2's closing line differs from ח.1's.
        text = st.build("Watchlist", ticker="NVDA", grade="B", thesis_sentence="x",
                         primary=PRIMARY, alternate=ALTERNATE)
        assert text.rstrip().endswith("PDF.")
        assert "✅" not in text

    def test_a_reason_always_appears_even_with_no_flags(self):
        text = st.build("Watchlist", ticker="NVDA", grade="B", thesis_sentence="x",
                         primary=PRIMARY, alternate=ALTERNATE, disclosure_flags=[])
        assert "ממתין לתנאים" in text


class TestNoTradeIsShort:
    def test_it_carries_no_setup_table_and_no_size_block(self):
        text = st.build("No Trade", ticker="NVDA", grade="D",
                         thesis_sentence="הסיבה")
        assert "סטאפ ראשי" not in text
        assert "גודל הקנייה" not in text
        assert "⭐ ציון: <b>D</b>" in text


class TestValuesArriveAsStrings:
    """Prices in this system genuinely do arrive as strings -- deliver_report's
    own documented shape has `"trigger":"..."` and plenty of stored setups carry
    "132.40" rather than 132.40. Formatting one with :,.2f raises "Unknown
    format code 'f' for object of type 'str'" and kills the entire delivery,
    which is a very expensive way to lose a finished analysis. Found by two real
    delivery tests on 2026-08-09."""

    STRINGY = {
        "type": "Breakout", "trigger": "213.06", "stop": "207.63",
        "targets": [{"price": "232.28", "pct": "40", "rr": "3.54"},
                    {"price": "236.54", "pct": "35", "rr": "4.32"}],
    }

    def test_string_prices_render_normally(self):
        text = st.build("Buy Now", **{**BUY_KWARGS, "primary": self.STRINGY})
        assert "<b>213.06</b>" in text
        assert "<b>232.28</b>" in text

    def test_string_allocations_still_compute_the_runner(self):
        text = st.build("Buy Now", **{**BUY_KWARGS, "primary": self.STRINGY})
        assert "<b>25</b>%" in text

    def test_string_money_fields_render_normally(self):
        text = st.build("Buy Now", **{**BUY_KWARGS, "cost_usd": "36220.20",
                                       "cash_available_usd": "41000"})
        assert "$36,220.20" in text

    def test_free_text_where_a_price_belongs_is_a_dash_not_a_crash(self):
        # Rule 14's "no order ready yet; trigger determined after the
        # confirmation candle forms" is real, stored, free text. It must never
        # be parsed into a number out of a sentence.
        setup = {"type": "Breakout", "trigger": "no order ready yet", "targets": []}
        text = st.build("Buy Now", **{**BUY_KWARGS, "primary": setup})
        assert "עדיין אין מחיר מוגדר" in text
