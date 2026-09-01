"""Unit tests for decision_policy.py (CONSISTENCY_RULES.md rule 29, 2026-08-03).

Pure functions, synthetic inputs, plus the real 2026-08-02 rescan rows that
made the drift visible in the first place -- same "arithmetic, not a live-data
check" scope as test_rubric_formula.py.
"""

import decision_policy as dp


class TestMaxAllowedDecision:
    def test_no_target_is_no_trade_whatever_else_is_true(self):
        # "Nowhere to sell" outranks every other fact: a hostile market or a weak
        # grade describes a trade not to take yet; no target means there is no
        # trade at all.
        assert dp.max_allowed_decision(has_target=False, grade="A",
                                        regime="healthy_uptrend") == dp.NO_TRADE

    def test_hostile_regime_caps_at_watchlist(self):
        assert dp.max_allowed_decision(has_target=True, grade="A",
                                        regime="risk_off") == dp.WATCHLIST

    def test_bottom_grade_caps_at_watchlist(self):
        assert dp.max_allowed_decision(has_target=True, grade="D",
                                        regime="healthy_uptrend") == dp.WATCHLIST

    def test_old_scale_f_still_caps(self):
        # Theses stored under the six-criterion scale still say F. They must keep
        # blocking exactly as they did rather than falling through as unknown.
        assert dp.max_allowed_decision(has_target=True, grade="F",
                                        regime="healthy_uptrend") == dp.WATCHLIST

    def test_a_clean_setup_permits_a_buy(self):
        assert dp.max_allowed_decision(has_target=True, grade="B",
                                        regime="neutral_choppy") == dp.BUY_NOW

    def test_the_two_buy_words_are_not_ranked_against_each_other(self):
        # Which buy word is right turns on whether the trigger already confirmed
        # on a settled daily close -- a fact this module is never given. So a
        # clean setup permits either, and neither may be flagged as too strong.
        for grade in ("A", "B", "C"):
            assert dp.is_decision_allowed(dp.BUY_NOW, has_target=True, grade=grade,
                                           regime="risk_on") is True
            assert dp.is_decision_allowed(dp.BUY_IF_CONFIRMED, has_target=True,
                                           grade=grade, regime="risk_on") is True


class TestIsDecisionAllowed:
    def test_the_real_onds_row_is_rejected(self):
        # ONDS, 2026-08-02: grade D, zero qualifying targets, came back
        # "Watchlist" while MSFT/RKLB/SPOT in the identical position came back
        # "No Trade". This is the exact case rule 29 was written for.
        assert dp.is_decision_allowed(dp.WATCHLIST, has_target=False, grade="D",
                                       regime="neutral_choppy") is False

    def test_the_real_pltr_row_is_allowed(self):
        # PLTR, same day, same grade -- but it HAS a target paying 2.11x, so
        # Watchlist is the correct word and must not be flagged.
        assert dp.is_decision_allowed(dp.WATCHLIST, has_target=True, grade="D",
                                       regime="neutral_choppy") is True

    def test_a_weaker_decision_is_always_allowed(self):
        # The facts set a ceiling, never a floor: a real reason to wait is
        # judgment and stays with the model.
        assert dp.is_decision_allowed(dp.NO_TRADE, has_target=True, grade="A",
                                       regime="risk_on") is True
        assert dp.is_decision_allowed(dp.WATCHLIST, has_target=True, grade="A",
                                       regime="risk_on") is True

    def test_buy_on_a_bottom_grade_is_rejected(self):
        assert dp.is_decision_allowed(dp.BUY_IF_CONFIRMED, has_target=True,
                                       grade="D", regime="risk_on") is False

    def test_unknown_decision_string_is_not_judged(self):
        # One bad label must not masquerade as a gate violation -- report_lint
        # reports an unrecognized decision separately.
        assert dp.is_decision_allowed("Maybe", has_target=False, grade="D",
                                       regime="risk_off") is True


class TestHasQualifyingTarget:
    def test_empty_targets_is_false(self):
        assert dp.has_qualifying_target({"targets": []}) is False

    def test_missing_setup_is_false(self):
        assert dp.has_qualifying_target(None) is False

    def test_one_target_is_true(self):
        assert dp.has_qualifying_target({"targets": [{"price": 100.0}]}) is True


class TestExplainReasons:
    def test_a_json_string_is_parsed_not_iterated_as_characters(self):
        # The real shape from get_pending_report_rows, which does NOT parse this
        # column. Iterating the raw string walks single characters, so every
        # pattern misses and every idea silently shows the fallback -- which is
        # exactly what happened on the first real run.
        raw = '["no_qualifying_target", "rr_below_2"]'
        out = dp.explain_reasons(raw)
        assert out != [dp.FALLBACK_REASON]
        assert any("למכור" in s for s in out)

    def test_a_real_list_works_the_same(self):
        assert dp.explain_reasons(["no_qualifying_target"]) == \
               dp.explain_reasons('["no_qualifying_target"]')

    def test_empty_gives_the_fallback_never_a_blank(self):
        assert dp.explain_reasons(None) == [dp.FALLBACK_REASON]
        assert dp.explain_reasons([]) == [dp.FALLBACK_REASON]

    def test_unrecognized_tokens_give_the_fallback(self):
        assert dp.explain_reasons(["something_nobody_mapped"]) == [dp.FALLBACK_REASON]

    def test_the_cap_holds(self):
        many = ["no_qualifying_target", "rr_below_2", "earnings_soon",
                "downtrend", "extended_vs_sma20", "rs_weak"]
        assert len(dp.explain_reasons(many)) == 2
        assert len(dp.explain_reasons(many, limit=4)) == 4

    def test_the_decisive_reason_outranks_the_universal_one(self):
        # "the price hasn't got there yet" is true of every waiting idea and
        # says nothing; "nowhere to sell" is what actually decided the outcome.
        out = dp.explain_reasons(["trigger_not_fired", "no_qualifying_target"], limit=1)
        assert "למכור" in out[0]

    def test_duplicate_sentences_are_not_repeated(self):
        # Several distinct tokens map to the same sentence (regime/choppy,
        # extended/parabolic). The reader must never see it twice.
        out = dp.explain_reasons(["regime_not_supportive", "neutral_choppy"], limit=4)
        assert len(out) == len(set(out))


class TestDecisionSign:
    """The sign shown in /list, /pending, /monitor and /monitorall
    (2026-08-03, user's request)."""

    def test_every_one_of_the_four_has_its_own_sign(self):
        signs = {dp.decision_sign(d) for d in dp.ALL_DECISIONS}
        assert len(signs) == len(dp.ALL_DECISIONS)

    def test_no_sign_collides_with_a_monitor_status_tier(self):
        # MONITOR_v2.md's ⚪/🟡/🟢/🔴 already mean "how far from the trigger".
        # A decision sign that looks like one of those is worse than none.
        status_tiers = {"⚪", "🟡", "🟢", "🔴"}
        for d in list(dp.ALL_DECISIONS) + [None]:
            assert dp.decision_sign(d) not in status_tiers

    def test_buy_now_is_the_green_square_not_the_green_circle(self):
        # The one place the two vocabularies want the same color -- green means
        # "go" in both -- so they are separated by shape. Every status tier is a
        # circle; no decision sign is.
        assert dp.decision_sign(dp.BUY_NOW) == "🟩"

    def test_an_unstored_decision_is_visible_not_blank(self):
        assert dp.decision_sign(None) == dp.UNKNOWN_SIGN[0]
        assert dp.decision_line(None) == f"{dp.UNKNOWN_SIGN[0]} {dp.SIGN_LEAD}: {dp.UNKNOWN_SIGN[1]}"

    def test_an_unrecognized_label_falls_to_the_unknown_sign(self):
        assert dp.decision_sign("Maybe Later") == dp.UNKNOWN_SIGN[0]

    def test_case_and_spacing_do_not_lose_the_sign(self):
        # The column is written by a model on every screener run; a capital
        # letter must not drop a real decision to "no decision stored".
        for written in ("buy now", "BUY NOW", " Buy  Now ", "Buy Now"):
            assert dp.decision_sign(written) == dp.decision_sign(dp.BUY_NOW)
            assert dp.decision_sign(written) != dp.UNKNOWN_SIGN[0]

    def test_the_line_names_where_the_words_came_from(self):
        # A live 🟢 beside a stored Watchlist is two facts, not a contradiction --
        # only true if the line says the words come from the stored thesis.
        line = dp.decision_line(dp.WATCHLIST)
        assert dp.SIGN_LEAD in line
        assert "רשימת מעקב" in line

    def test_a_confirmed_trigger_adds_the_live_half(self):
        # The real PLTR message (2026-08-08): stored "Watchlist" -- don't buy
        # yet -- printed above a real 115-share order, because the trigger fired
        # on 166.08 and closed at 172.01 long after that word was chosen. The
        # stored word is written once by /screener and never rewritten, so the
        # live fact has to be shown beside it.
        line = dp.decision_line(dp.WATCHLIST, "green")
        assert dp.SIGN_LEAD in line and "רשימת מעקב" in line
        assert dp.LIVE_LEAD in line and dp.TRIGGER_CONFIRMED_WORDS in line

    def test_only_a_confirmed_trigger_earns_it(self):
        # 🟢 is the one tier that means a settled daily close beyond the trigger.
        # Every other tier is still waiting, so the stored word stands alone.
        for status in (None, "white", "yellow", "yellow_plus", "red"):
            assert dp.LIVE_LEAD not in dp.decision_line(dp.WATCHLIST, status)

    def test_a_stored_buy_now_needs_no_second_half(self):
        # Nothing to reconcile: the stored word already says the trigger fired.
        assert dp.LIVE_LEAD not in dp.decision_line(dp.BUY_NOW, "green")

    def test_the_live_half_never_replaces_the_stored_one(self):
        # Both halves, always -- this must not become a quiet rewrite of what
        # /screener decided. Every decision keeps its own sign and words.
        for d in (dp.WATCHLIST, dp.BUY_IF_CONFIRMED, dp.NO_TRADE):
            line = dp.decision_line(d, "green")
            assert line.startswith(dp.decision_sign(d))
            assert dp.SIGN_LEAD in line

    def test_the_line_carries_no_html_tags(self):
        # Callers send it through Telegram's HTML parse mode next to text they
        # escape themselves; a tag here would make "is this pre-escaped?" a
        # question every caller has to answer.
        for d in list(dp.ALL_DECISIONS) + [None, "nonsense"]:
            assert "<" not in dp.decision_line(d)
class TestAnInventedStopCannotCarryAnOrder:
    """CONSISTENCY_RULES.md rule 1, enforced 2026-08-30.

    `level_picker.pick_stop` falls back to a plain 2x ATR distance when no daily
    low can hold the stop, tags it, and says in its own reason that this is "not
    a level the chart gave us". Nothing had ever read that tag, so a stop with
    no source in it produced a full order like any other.

    Watchlist and not No Trade: there IS somewhere to sell, so the idea is real.
    What is missing is a defensible place to be wrong, and a real low can form
    on any day.
    """

    def _setup(self, basis_kind):
        return {"trigger": 100.0, "stop": 96.0, "stop_basis_kind": basis_kind,
                "targets": [{"price": 106.0}]}

    def test_a_distance_only_stop_caps_the_decision_at_watchlist(self):
        assert dp.max_allowed_decision(
            has_target=True, grade="A", regime="risk_on",
            has_structural_stop=False) == dp.WATCHLIST

    def test_the_same_setup_with_a_real_low_may_carry_an_order(self):
        assert dp.max_allowed_decision(
            has_target=True, grade="A", regime="risk_on",
            has_structural_stop=True) == dp.BUY_NOW

    def test_no_target_still_outranks_it(self):
        # "Nowhere to sell" is the stronger statement and must stay on top.
        assert dp.max_allowed_decision(
            has_target=False, grade="A", regime="risk_on",
            has_structural_stop=False) == dp.NO_TRADE

    def test_the_tag_is_what_is_read_not_the_stop_price(self):
        assert dp.setup_stop_stands_on_structure(
            self._setup("recent_higher_low")) is True
        assert dp.setup_stop_stands_on_structure(
            self._setup(dp.NO_STRUCTURE_STOP_BASIS)) is False

    def test_an_older_thesis_with_no_tag_is_not_punished_for_it(self):
        # stop_basis_kind is newer than the oldest stored theses. Refusing to
        # order every one of them on a missing field would be its own bug.
        assert dp.setup_stop_stands_on_structure(
            {"trigger": 100.0, "stop": 96.0, "targets": [{"price": 106.0}]}) is True

    def test_both_halves_must_belong_to_the_same_setup(self):
        # A qualifying target on Primary does not license an order on an
        # Alternate whose stop was invented, and the reverse.
        primary = {"stop_basis_kind": dp.NO_STRUCTURE_STOP_BASIS,
                    "targets": [{"price": 106.0}]}
        alternate = {"stop_basis_kind": "recent_higher_low", "targets": []}
        assert dp.has_orderable_setup(primary, alternate) is False

    def test_one_good_setup_is_enough(self):
        primary = {"stop_basis_kind": dp.NO_STRUCTURE_STOP_BASIS,
                    "targets": [{"price": 106.0}]}
        alternate = {"stop_basis_kind": "flush_low", "targets": [{"price": 104.0}]}
        assert dp.has_orderable_setup(primary, alternate) is True

