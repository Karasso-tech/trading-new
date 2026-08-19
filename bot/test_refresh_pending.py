"""Unit tests for refresh_pending.py's decision logic (2026-08-02).

classify()/plan() are pure functions over a /pending row -- no DB, no fetch --
except for the one stale-trigger lookup, which is monkeypatched here. Same
"arithmetic, not a live-data check" scope as test_rubric_formula.py.
"""

import sys
from datetime import datetime, timezone

import pytest

import refresh_pending as rp


@pytest.fixture(autouse=True)
def no_trigger_age(monkeypatch):
    """Default: no green ever logged. Tests that care override it."""
    monkeypatch.setattr(rp.persistence, "get_trigger_fired_age", lambda ticker: None)


def _row(ticker="ABC", trigger=100.0, stop=95.0, atr=2.0, price=100.5, days=1):
    # A real target by default (2026-08-08): a setup with nowhere to sell into
    # is now a rebuild reason of its own, so an empty list here would make every
    # other test in this file classify on that instead of what it means to test.
    return {
        "ticker": ticker,
        "days_pending": days,
        "latest_price": price,
        "primary_setup": {"type": "Breakout", "trigger": trigger, "stop": stop,
                           "atr_at_build": atr,
                           "targets": [{"price": 108.0, "pct": 40}]},
    }


class TestClassify:
    def test_current_thesis_is_kept(self):
        v = rp.classify(_row())
        assert v.action == "keep"
        assert v.reason == "still_current"

    def test_text_trigger_forces_a_rebuild(self):
        # The real shape found in the DB: 26 of 55 stored theses had a whole
        # Hebrew sentence where the trigger price should be, so nothing
        # mechanical could measure them at all.
        v = rp.classify(_row(trigger="סגירה יומית מעל האזור"))
        assert v.action == "rebuild"
        assert v.reason == "no_numeric_trigger"
        assert v.distance_atr is None

    def test_a_number_is_never_parsed_out_of_prose(self):
        # "352.00 (הסבר ...)" is a real stored value. Digits being present does
        # not make it a usable level -- guessing one would be inventing data.
        v = rp.classify(_row(trigger="352.00 (רמה משוערת, לא אושרה)"))
        assert v.reason == "no_numeric_trigger"

    def test_price_below_the_stop_looks_dead(self):
        v = rp.classify(_row(price=94.0))
        assert v.action == "looks_dead"
        assert v.reason == "price_below_stop"

    def test_a_dead_thesis_is_not_rebuilt(self):
        # Rebuilding a broken idea just manufactures a fresh reason to stare at
        # a losing chart -- it gets reported for a manual /drop instead.
        v = rp.classify(_row(price=94.0, days=99))
        assert v.action != "rebuild"

    def test_age_alone_never_rebuilds(self):
        # 2026-08-03: age used to force a rebuild at 5 trading days. It no
        # longer does, at the user's own call -- an idea whose trigger is still
        # a real number, still un-fired, and still within 1 ATR of price is
        # describing the same trade it always was, however long it has waited.
        v = rp.classify(_row(days=99))
        assert v.action == "keep"
        assert v.reason == "still_current"

    def test_an_old_thesis_that_is_actually_broken_still_rebuilds(self):
        # The removal above must not have made the script blind: age is not a
        # reason, but a real defect on an old row still is.
        v = rp.classify(_row(days=99, price=104.0))  # 4.0 away, atr 2.0 -> 2.0x
        assert v.action == "rebuild"
        assert v.reason == "price_moved_away_from_trigger"

    def test_price_drifted_a_full_atr_is_rebuilt(self):
        v = rp.classify(_row(price=102.0))  # 2.0 away, atr 2.0 -> 1.0x
        assert v.action == "rebuild"
        assert v.reason == "price_moved_away_from_trigger"

    def test_a_run_far_past_the_trigger_is_rebuilt_not_protected(self):
        # 2026-08-08, asked and answered: with the refresh now running BEFORE the
        # night's scan, does an idea sitting above its trigger have to be spared
        # so the scan can still report the entry? No. This far past, the reward
        # is gone and the stop is a long way below -- the user's own "don't chase
        # it" rule already forbids the trade, so what is needed is a new plan.
        v = rp.classify(_row(price=106.0))  # 3.0x above
        assert v.action == "rebuild"

    def test_an_entry_that_just_fired_is_kept(self):
        # The other side of the same question, and the reason no direction check
        # is needed: a full ATR is a long way past an entry. One that just
        # crossed sits a fraction of that past it, and is kept -- so the scan
        # that runs right after this still judges it on the stored numbers.
        v = rp.classify(_row(price=100.8))  # 0.4x above
        assert v.action == "keep"
        assert v.reason == "still_current"

    def test_drift_below_the_threshold_is_kept(self):
        v = rp.classify(_row(price=101.9))
        assert v.action == "keep"

    def test_stale_fired_trigger_is_rebuilt(self, monkeypatch):
        # The MSFT case: fired days ago, price long gone, still being reported
        # as a live buy every night.
        monkeypatch.setattr(rp.persistence, "get_trigger_fired_age",
                            lambda ticker: {"stale": True, "trading_days": 5,
                                            "first_green_date": "2026-07-16"})
        v = rp.classify(_row())
        assert v.action == "rebuild"
        assert v.reason == "trigger_fired_and_went_stale"

    def test_freshly_fired_trigger_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(rp.persistence, "get_trigger_fired_age",
                            lambda ticker: {"stale": False, "trading_days": 1,
                                            "first_green_date": "2026-08-01"})
        assert rp.classify(_row()).action == "keep"

    def test_missing_price_still_classifies(self):
        # No monitor check logged yet -- distance is unknowable, but age and the
        # trigger-type check still work. Must never crash or invent a distance.
        v = rp.classify(_row(price=None))
        assert v.distance_atr is None
        assert v.action == "keep"


class TestNoTargetRebuild:
    """2026-08-08, found on a live run: BTCUSD and ONDS both confirmed their
    triggers and both came back 'can't be scored, no target saved'. The live
    re-grade needs a target, so they produce no grade, no reward:risk and no
    order -- night after night, while still looking like live ideas on the
    waiting list. Distance from the trigger can never catch that."""

    def test_an_empty_target_list_forces_a_rebuild(self):
        row = _row()
        row["primary_setup"]["targets"] = []
        v = rp.classify(row)
        assert v.action == "rebuild"
        assert v.reason == "no_target_to_sell_into"

    def test_a_target_with_no_price_is_the_same_as_none(self):
        row = _row()
        row["primary_setup"]["targets"] = [{"pct": 40, "rr": "2.1:1"}]
        assert rp.classify(row).reason == "no_target_to_sell_into"

    def test_a_prose_target_is_never_parsed_into_a_number(self):
        # Same standard as the trigger: digits in a sentence are not a level.
        row = _row()
        row["primary_setup"]["targets"] = [{"price": "אזור ההתנגדות סביב 120"}]
        assert rp.classify(row).reason == "no_target_to_sell_into"

    def test_a_real_target_is_not_flagged(self):
        assert rp.classify(_row()).reason == "still_current"

    def test_a_dead_idea_is_still_dead_not_rebuilt(self):
        # Order matters: nothing to sell into does not outrank "price fell
        # through its own stop". Rebuilding a broken chart is what this whole
        # script refuses to do.
        row = _row(price=94.0)
        row["primary_setup"]["targets"] = []
        assert rp.classify(row).action == "looks_dead"


class TestSessionGate:
    """2026-08-08: the refresh may only run at the end of a day the market was
    actually open. Task Scheduler's Mon-Fri fires on every market holiday too."""

    def test_a_closed_session_goes_ahead(self, monkeypatch):
        monkeypatch.setattr(rp, "todays_close_utc",
                            lambda: datetime(2026, 8, 7, 20, 0, tzinfo=timezone.utc))
        monkeypatch.setattr(rp, "market_closed_today", lambda: True)
        assert rp.session_skip_reason() is None

    def test_a_holiday_or_weekend_is_skipped(self, monkeypatch):
        # No session row at all -- no new prices, so a rebuild would rewrite
        # stored theses off the same closes they already hold.
        monkeypatch.setattr(rp, "todays_close_utc", lambda: None)
        monkeypatch.setattr(rp, "market_closed_today", lambda: False)
        reason = rp.session_skip_reason()
        assert reason and "not an NYSE trading day" in reason

    def test_a_session_still_open_is_a_warning_not_a_quiet_skip(self, monkeypatch):
        # This one means the trigger time drifted ahead of the real close, so
        # the night is being LOST. It must not read like a normal holiday skip.
        monkeypatch.setattr(rp, "todays_close_utc",
                            lambda: datetime(2026, 8, 7, 20, 0, tzinfo=timezone.utc))
        monkeypatch.setattr(rp, "market_closed_today", lambda: False)
        reason = rp.session_skip_reason()
        assert reason and reason.startswith("WARNING")


class TestPlan:
    def test_rebuilds_are_ordered_closest_to_trigger_first(self):
        # All three have drifted at least a full ATR (trigger 100, atr 2.0), so
        # all three are rebuild candidates -- only their distance differs.
        rows = [_row("FAR", price=108.0),
                _row("NEAR", price=102.0),
                _row("MID", price=104.0)]
        rebuild, _, _ = rp.plan(rows)
        assert [v.ticker for v in rebuild] == ["NEAR", "MID", "FAR"]

    def test_unknown_distance_sorts_last(self):
        # An unmeasurable thesis can't be shown to be near the trigger, so it
        # must not jump ahead of one that provably is.
        rows = [_row("TEXT", trigger="אין רמה"), _row("NEAR", price=102.0)]
        rebuild, _, _ = rp.plan(rows)
        assert [v.ticker for v in rebuild] == ["NEAR", "TEXT"]

    def test_the_three_buckets_cover_every_row_exactly_once(self):
        rows = [_row("A"), _row("B", price=104.0), _row("C", price=90.0)]
        rebuild, keep, dead = rp.plan(rows)
        assert len(rebuild) + len(keep) + len(dead) == len(rows)
        assert {v.ticker for v in rebuild + keep + dead} == {"A", "B", "C"}


class TestDrainCondition:
    """Found the first time this ran for real: a run enqueued 22 rebuilds and
    died before draining them. The next run correctly refused to enqueue
    duplicates -- and then, because it had enqueued nothing itself, also
    skipped the drain. The work sat in the queue indefinitely."""

    @pytest.fixture(autouse=True)
    def a_normal_trading_night(self, monkeypatch):
        """These exercise main()'s drain decision, so the calendar gate must not
        decide the outcome (they'd pass or fail depending on the day they run),
        and the chained scan must not actually spawn its subprocess."""
        monkeypatch.setattr(rp, "session_skip_reason", lambda: None)
        monkeypatch.setattr(rp, "_run_post_close_scan", lambda: None)

    def test_drains_when_work_is_already_queued_from_an_earlier_run(self, monkeypatch):
        drained = []
        monkeypatch.setattr(rp.persistence, "get_pending_report_rows", lambda: [_row("ABC")])
        monkeypatch.setattr(rp.persistence, "tickers_already_queued_for_screener",
                            lambda: {"ABC"})
        monkeypatch.setattr(rp, "_drain_queue", lambda: drained.append(True))
        monkeypatch.setattr(rp, "_report", lambda *a, **k: None)
        monkeypatch.setattr(sys, "argv", ["refresh_pending.py"])
        rp.main()
        assert drained == [True]

    def test_does_not_drain_when_there_is_nothing_to_do(self, monkeypatch):
        drained = []
        monkeypatch.setattr(rp.persistence, "get_pending_report_rows", lambda: [_row("ABC")])
        monkeypatch.setattr(rp.persistence, "tickers_already_queued_for_screener", lambda: set())
        monkeypatch.setattr(rp, "_drain_queue", lambda: drained.append(True))
        monkeypatch.setattr(rp, "_report", lambda *a, **k: None)
        monkeypatch.setattr(sys, "argv", ["refresh_pending.py"])
        rp.main()
        assert drained == []


class TestChangeLines:
    """2026-08-03, the user's Change 3: a rewrite he cannot see is a rewrite he
    cannot judge. Only fields that actually moved are named, so an unchanged
    grade doesn't add noise to a line that is really about a moved trigger."""

    def _change(self, **after):
        before = {"decision": "Watchlist", "grade": "B", "trigger": 100.0, "stop": 95.0}
        return {"ticker": "ABC", "before": before, "after": {**before, **after}}

    def test_a_changed_grade_is_named(self):
        line = rp._change_lines([self._change(grade="C")])[0]
        assert "B" in line and "C" in line

    def test_an_unchanged_field_is_not_named(self):
        line = rp._change_lines([self._change(trigger=112.0)])[0]
        assert "112.00" in line and "100.00" in line
        assert "סטופ" not in line          # the stop did not move

    def test_prices_never_print_as_none(self):
        # A prose trigger archives as NULL -- it must read as '?', never 'None'
        # and never a fabricated number.
        change = self._change()
        change["before"]["trigger"] = None
        line = rp._change_lines([change])[0]
        assert "None" not in line
        assert "?" in line

    def test_a_rewrite_that_moved_nothing_still_reports(self):
        line = rp._change_lines([self._change()])[0]
        assert "ABC" in line


class TestReportNoTradeCallout:
    def test_a_rebuild_turned_no_trade_is_called_out_with_a_drop_line(self, monkeypatch):
        # It is no longer removed automatically, so it has to be SAID -- else it
        # sits on the list with a verdict nobody noticed.
        sent = {}
        monkeypatch.setattr(rp, "send_text", lambda text: (sent.__setitem__("text", text), {"ok": True})[1])
        monkeypatch.setattr(rp.persistence, "get_exhausted_cold", lambda: [])
        changes = [{"ticker": "ABC",
                    "before": {"decision": "Buy Only If Confirmed", "grade": "B",
                                "trigger": 100.0, "stop": 95.0},
                    "after": {"decision": "No Trade", "grade": "D",
                               "trigger": 112.0, "stop": 104.0, "status": "pending"}}]
        rp._report(["ABC"], [], [], kept=0, changes=changes)
        assert "/drop ABC" in sent["text"]

    def test_an_idea_that_was_already_no_trade_is_not_re_announced(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(rp, "send_text", lambda text: (sent.__setitem__("text", text), {"ok": True})[1])
        monkeypatch.setattr(rp.persistence, "get_exhausted_cold", lambda: [])
        changes = [{"ticker": "ABC",
                    "before": {"decision": "No Trade", "grade": "D",
                                "trigger": 100.0, "stop": 95.0},
                    "after": {"decision": "No Trade", "grade": "D",
                               "trigger": 112.0, "stop": 104.0, "status": "pending"}}]
        rp._report(["ABC"], [], [], kept=0, changes=changes)
        assert "/drop ABC" not in sent["text"]


class TestDeadIdeasGetQuieterThenGetShelved:
    """2026-08-11. The same three /drop lines every night is a section nobody
    reads, and a newly dead idea hides in it. Loud once, counted after, shelved
    on the fifth night with no answer."""

    @pytest.fixture
    def counts(self, monkeypatch):
        """A fake dead-night counter, so this stays a logic test with no DB."""
        state = {"nights": {}, "cleared": [], "shelved": []}

        def _bump(ticker):
            state["nights"][ticker] = state["nights"].get(ticker, 0) + 1
            return state["nights"][ticker]

        monkeypatch.setattr(rp.persistence, "bump_dead_night", _bump)
        monkeypatch.setattr(rp.persistence, "clear_dead_nights",
                            lambda t: state["cleared"].append(t))
        monkeypatch.setattr(rp.persistence, "set_cold", lambda t: state["shelved"].append(t))
        return state

    def _dead(self, ticker="ABC"):
        return rp.Verdict(ticker, "looks_dead", "price_below_stop", None)

    def test_the_first_night_is_the_loud_one(self, counts):
        first, still, shelved = rp._handle_dead([self._dead()], live=[])
        assert [v.ticker for v in first] == ["ABC"]
        assert still == [] and shelved == []

    def test_the_nights_after_are_only_counted(self, counts):
        counts["nights"]["ABC"] = 1
        first, still, shelved = rp._handle_dead([self._dead()], live=[])
        assert first == [] and shelved == []
        assert [v.ticker for v in still] == ["ABC"]

    def test_the_fifth_night_moves_it_to_the_shelf(self, counts):
        counts["nights"]["ABC"] = rp.persistence.DEAD_NIGHTS_BEFORE_COLD - 1
        first, still, shelved = rp._handle_dead([self._dead()], live=[])
        assert [v.ticker for v in shelved] == ["ABC"]
        assert counts["shelved"] == ["ABC"]

    def test_a_price_back_above_the_stop_starts_the_streak_over(self, counts):
        # Four separate dips over four weeks must not add up to a shelving.
        rp._handle_dead([], live=[rp.Verdict("ABC", "keep", "still_current", 0.2)])
        assert counts["cleared"] == ["ABC"]

    def test_the_shelved_row_is_not_rebuilt_tonight(self, counts):
        counts["nights"]["ABC"] = rp.persistence.DEAD_NIGHTS_BEFORE_COLD - 1
        _, _, shelved = rp._handle_dead([self._dead()], live=[])
        # Shelving is a move, not a re-screen: nothing is enqueued here at all.
        assert [v.action for v in shelved] == ["looks_dead"]


class TestDeadReportWording:
    def _send(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(rp, "send_text",
                            lambda text: (sent.__setitem__("text", text), {"ok": True})[1])
        monkeypatch.setattr(rp.persistence, "get_exhausted_cold", lambda: [])
        return sent

    def test_a_first_night_death_still_shows_its_drop_line(self, monkeypatch):
        sent = self._send(monkeypatch)
        rp._report([], [], [rp.Verdict("ABC", "looks_dead", "price_below_stop", None)], kept=0)
        assert "/drop ABC" in sent["text"]

    def test_a_repeat_death_is_one_short_line_with_no_drop_command(self, monkeypatch):
        sent = self._send(monkeypatch)
        rp._report([], [], [], kept=0,
                   dead_still=[rp.Verdict("ABC", "looks_dead", "price_below_stop", None)])
        assert "/drop ABC" not in sent["text"]
        assert "1 עדיין נראים גמורים" in sent["text"] and "ABC" in sent["text"]

    def test_a_shelved_idea_is_named_and_says_nothing_was_deleted(self, monkeypatch):
        sent = self._send(monkeypatch)
        rp._report([], [], [], kept=0,
                   shelved=[rp.Verdict("ABC", "looks_dead", "price_below_stop", None)])
        assert "הועברו למדף" in sent["text"] and "ABC" in sent["text"]
        assert "לא נמחק כלום" in sent["text"]

    def test_a_night_with_only_repeats_is_not_reported_as_nothing_to_do(self, monkeypatch):
        sent = self._send(monkeypatch)
        rp._report([], [], [], kept=0,
                   dead_still=[rp.Verdict("ABC", "looks_dead", "price_below_stop", None)])
        assert "אין מה לעדכן" not in sent["text"]
