"""Unit tests for the append-only `ideas` build record (2026-08-07).

Why this exists. `thesis` is keyed on ticker, so it can hold exactly one plan
per symbol -- re-screening XLF overwrote the previous XLF plan in place. Found
on the live DB: 28 overwrites across 18 tickers, and XLF alone had three
genuinely different builds (Breakout at 54.765, Pullback at 56.44, Breakout at
58.41, with different stops, setups and grades) that the shadow book had been
recording as ONE ticker changing its mind night to night. Anything grouped by
ticker was therefore measuring a blend of unrelated plans.

These tests lock in the shape of the fix: every build gets its own row, exactly
one build per ticker is live at a time, superseded builds keep the status and
levels they had when they were replaced, and a simulated result binds to the
build it scored rather than to the ticker.

Isolated temp SQLite DB throughout via monkeypatched persistence.DB_PATH --
same pattern as test_persistence.py. Never touches the real trading_new.db.
"""

import sqlite3

import pytest

import persistence


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(persistence, "DB_PATH", db_path)
    persistence.init_db()
    return db_path


def _setup(trigger=100.0, stop=95.0, targets=(110.0, 120.0), setup_type="Breakout"):
    return {
        "type": setup_type,
        "trigger": trigger,
        "stop": stop,
        "atr_at_build": 2.5,
        "targets": [{"price": p} for p in targets],
    }


def _save(ticker="XLF", **kwargs):
    params = {
        "ticker": ticker,
        "status": "pending",
        "source": "SCREENER_v3",
        "primary_setup": _setup(),
        "rubric_grade": "B",
        "decision": "Watchlist",
        "market_regime_at_build": "neutral_choppy",
    }
    params.update(kwargs)
    return persistence.save_thesis(**params)


class TestOneRowPerBuild:
    def test_first_build_creates_an_idea(self, temp_db):
        idea_id = _save()
        idea = persistence.get_idea(idea_id)
        assert idea["ticker"] == "XLF"
        assert idea["seq"] == 1
        assert idea["superseded_at"] is None
        assert idea["trigger"] == 100.0
        assert idea["stop"] == 95.0
        assert idea["target_1"] == 110.0
        assert idea["target_2"] == 120.0
        assert idea["setup_type"] == "Breakout"

    def test_rescreening_appends_instead_of_overwriting(self, temp_db):
        """The whole point. Three builds of one symbol stay three rows."""
        first = _save(primary_setup=_setup(100.0, 95.0, setup_type="Breakout"))
        second = _save(primary_setup=_setup(56.44, 55.34, setup_type="Pullback"), rubric_grade="D")
        third = _save(primary_setup=_setup(58.41, 56.89, setup_type="Breakout"), rubric_grade="A")

        assert len({first, second, third}) == 3
        history = persistence.get_ideas(ticker="XLF")
        assert [i["seq"] for i in history] == [3, 2, 1]
        assert [i["trigger"] for i in history] == [58.41, 56.44, 100.0]
        assert [i["setup_type"] for i in history] == ["Breakout", "Pullback", "Breakout"]

    def test_thesis_still_holds_only_the_newest(self, temp_db):
        """The live workflow is untouched: thesis keeps its one-per-ticker shape."""
        _save(primary_setup=_setup(100.0, 95.0))
        _save(primary_setup=_setup(58.41, 56.89))
        stored = persistence.get_thesis("XLF")
        assert stored["primary_setup"]["trigger"] == 58.41
        with persistence._db() as conn:
            count = conn.execute("SELECT COUNT(*) c FROM thesis WHERE ticker='XLF'").fetchone()["c"]
        assert count == 1

    def test_exactly_one_live_build_per_ticker(self, temp_db):
        _save()
        _save()
        _save()
        live = persistence.get_ideas(ticker="XLF", live_only=True)
        assert len(live) == 1
        assert live[0]["seq"] == 3
        assert persistence.get_live_idea("XLF")["seq"] == 3

    def test_superseded_build_records_who_replaced_it(self, temp_db):
        first = _save()
        second = _save()
        old = persistence.get_idea(first)
        assert old["superseded_at"] is not None
        assert old["superseded_by_id"] == second

    def test_two_tickers_do_not_supersede_each_other(self, temp_db):
        xlf = _save("XLF")
        nvda = _save("NVDA")
        assert persistence.get_idea(xlf)["superseded_at"] is None
        assert persistence.get_idea(nvda)["superseded_at"] is None
        assert persistence.get_idea(nvda)["seq"] == 1

    def test_ticker_is_normalized_to_upper(self, temp_db):
        idea_id = _save("xlf")
        assert persistence.get_idea(idea_id)["ticker"] == "XLF"
        assert persistence.get_live_idea("xlf")["id"] == idea_id


class TestFlattenedColumns:
    """The flat columns are what make the data pullable without touching JSON."""

    def test_prose_trigger_is_kept_as_text_never_parsed_into_a_number(self, temp_db):
        """Rule 14's "no order ready yet" case. Guessing 231.91 out of the
        sentence would invent a level the screener never committed to."""
        prose = "Daily close above 231.91 (reclaim of the Dec 2025 breakout level)"
        idea_id = _save(primary_setup={"type": "Reclaim", "trigger": prose, "targets": []})
        idea = persistence.get_idea(idea_id)
        assert idea["trigger"] is None
        assert idea["trigger_text"] == prose

    def test_full_json_is_preserved_beside_the_flat_columns(self, temp_db):
        setup = _setup()
        setup["checkpoints"] = ["something the flat columns do not model"]
        idea_id = _save(primary_setup=setup)
        idea = persistence.get_idea(idea_id)
        assert idea["primary_setup"]["checkpoints"] == ["something the flat columns do not model"]

    def test_missing_targets_are_null_not_zero(self, temp_db):
        idea_id = _save(primary_setup=_setup(targets=()))
        idea = persistence.get_idea(idea_id)
        assert idea["target_1"] is None
        assert idea["target_2"] is None

    def test_only_the_first_two_targets_are_flattened(self, temp_db):
        """Rule 7 allows at most two sellable targets; the rest is the runner."""
        idea_id = _save(primary_setup=_setup(targets=(110.0, 120.0, 130.0)))
        idea = persistence.get_idea(idea_id)
        assert (idea["target_1"], idea["target_2"]) == (110.0, 120.0)

    def test_boolean_is_not_mistaken_for_a_price(self, temp_db):
        idea_id = _save(primary_setup={"type": "Breakout", "trigger": True, "targets": []})
        idea = persistence.get_idea(idea_id)
        assert idea["trigger"] is None
        assert idea["trigger_text"] == "True"


class TestStatusTracking:
    def test_status_change_moves_the_live_build_only(self, temp_db):
        first = _save()
        second = _save()
        persistence.set_status("XLF", "open_position")
        assert persistence.get_idea(first)["status"] == "pending"
        assert persistence.get_idea(second)["status"] == "open_position"

    def test_drop_records_the_reason_on_the_live_build(self, temp_db):
        idea_id = _save()
        persistence.drop_thesis("XLF", "thesis broke")
        idea = persistence.get_idea(idea_id)
        assert idea["status"] == "dropped"
        assert idea["drop_reason"] == "thesis broke"

    def test_status_at_build_is_never_rewritten(self, temp_db):
        idea_id = _save(status="pending")
        persistence.set_status("XLF", "closed")
        idea = persistence.get_idea(idea_id)
        assert idea["status"] == "closed"
        assert idea["status_at_build"] == "pending"

    def test_no_trade_still_goes_cold(self, temp_db):
        """Rule 29 routing must survive the change."""
        idea_id = _save(decision="No Trade")
        assert persistence.get_thesis("XLF")["status"] == "cold"
        assert persistence.get_idea(idea_id)["status"] == "cold"


class TestShadowBinding:
    def test_shadow_candidates_are_live_builds_carrying_their_id(self, temp_db):
        _save(primary_setup=_setup(100.0, 95.0))
        newest = _save(primary_setup=_setup(58.41, 56.89))
        candidates = persistence.get_shadow_candidates()
        assert len(candidates) == 1
        assert candidates[0]["idea_id"] == newest
        assert candidates[0]["date_built"] == candidates[0]["built_at"]
        assert candidates[0]["primary_setup"]["trigger"] == 58.41

    def test_results_for_different_builds_stay_separate(self, temp_db):
        """The XLF failure, reproduced: two builds, two results, no blending."""
        first = _save(primary_setup=_setup(54.765, 53.9))
        persistence.record_shadow_outcome(
            "XLF", checked_date="2026-08-04", price=55.0, hypothetical_trigger_fired=True,
            max_favorable_excursion=1.0, max_adverse_excursion=-0.2,
            idea_id=first, trigger=54.765, r_multiple_planned=0.02, resolution="runner_trailed")
        second = _save(primary_setup=_setup(58.41, 56.89))
        persistence.record_shadow_outcome(
            "XLF", checked_date="2026-08-06", price=57.0, hypothetical_trigger_fired=False,
            max_favorable_excursion=None, max_adverse_excursion=None,
            idea_id=second, trigger=58.41, resolution="never_fired")

        with persistence._db() as conn:
            rows = conn.execute(
                "SELECT idea_id, resolution FROM shadow_outcomes ORDER BY id").fetchall()
        assert [(r["idea_id"], r["resolution"]) for r in rows] == [
            (first, "runner_trailed"), (second, "never_fired")]

    def test_same_build_cannot_be_scored_twice_in_one_day(self, temp_db):
        """A scheduled run that fires twice used to write two rows that did not
        always agree -- both were kept, and neither was marked as the repeat."""
        idea_id = _save()
        kwargs = dict(checked_date="2026-08-06", price=57.0, hypothetical_trigger_fired=False,
                      max_favorable_excursion=None, max_adverse_excursion=None, idea_id=idea_id)
        persistence.record_shadow_outcome("XLF", **kwargs)
        with pytest.raises(sqlite3.IntegrityError):
            persistence.record_shadow_outcome("XLF", **kwargs)

    def test_the_same_build_can_be_scored_on_different_days(self, temp_db):
        idea_id = _save()
        for date in ("2026-08-05", "2026-08-06"):
            persistence.record_shadow_outcome(
                "XLF", checked_date=date, price=57.0, hypothetical_trigger_fired=False,
                max_favorable_excursion=None, max_adverse_excursion=None, idea_id=idea_id)
        with persistence._db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) c FROM shadow_outcomes WHERE idea_id=?", (idea_id,)).fetchone()["c"]
        assert count == 2


class TestReadHelpers:
    def test_numeric_trigger_filter_excludes_prose_builds(self, temp_db):
        _save("AAA", primary_setup=_setup(100.0, 95.0))
        _save("BBB", primary_setup={"type": "Reclaim", "trigger": "close above 50", "targets": []})
        tickers = [i["ticker"] for i in persistence.get_ideas(with_numeric_trigger=True)]
        assert tickers == ["AAA"]

    def test_get_live_idea_returns_none_for_an_unknown_ticker(self, temp_db):
        assert persistence.get_live_idea("NOPE") is None

    def test_get_idea_returns_none_for_an_unknown_id(self, temp_db):
        assert persistence.get_idea(9999) is None

    def test_sleeve_only_stub_creates_no_build(self, temp_db):
        """set_sleeve inserts a thesis row with no screener run behind it."""
        persistence.set_sleeve("QQQ", "core")
        assert persistence.get_ideas(ticker="QQQ") == []
        assert persistence.get_shadow_candidates() == []
