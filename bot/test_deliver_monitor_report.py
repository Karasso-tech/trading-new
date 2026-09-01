"""Unit tests for deliver_monitor_report.py's chart-draw dispatch (2026-08-07).

The NOW incident: /monitor drew the stored thesis -- Primary AND Alternate --
for every ticker, including ones the user already held. NOW (held since
2026-08-04, entry 115.68, stop 105.02) carried nine lines, among them the
Alternate's own stop at 100.49 for a trade that was never entered. Two stop
lines on one chart, only one of them real.

_redraw_chart() is the one place that decision now lives, so these tests are
about which line set a ticker gets and about the drawing never being able to
fail the monitor check that already went out.
"""

import pytest

import deliver_monitor_report as dmr
import monitor_text
import persistence


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(persistence, "DB_PATH", db_path)
    persistence.init_db()
    return db_path


@pytest.fixture
def drawn(monkeypatch):
    """Records (kind, ticker, payload) instead of touching the real chart."""
    calls = []

    async def _fake_position(ticker, position):
        calls.append(("position", ticker, position))

    async def _fake_setup(ticker, primary, alternate):
        calls.append(("setup", ticker, (primary, alternate)))

    monkeypatch.setattr(dmr, "_draw_position_chart", _fake_position)
    monkeypatch.setattr(dmr, "_draw_monitor_chart", _fake_setup)
    return calls


THESIS = {
    "primary_setup": {"type": "Breakout", "trigger": 113.79, "stop": 105.02},
    "alternate_setup": {"type": "Pullback", "trigger": 107.0, "stop": 100.49},
}


def _hold_now():
    persistence.save_thesis(ticker="NOW", status="open_position", source="SCREENER_v3",
                            primary_setup=THESIS["primary_setup"],
                            alternate_setup=THESIS["alternate_setup"])
    persistence.create_position(
        ticker="NOW", entry_date="2026-08-04", entry_price=115.68, qty=138,
        entry_type="full", entry_setup={"type": "Breakout", "stop": 105.02,
                                        "atr_at_build": 6.783},
        initial_stop=105.02,
    )


def test_held_ticker_draws_the_position(temp_db, drawn):
    _hold_now()
    assert dmr._redraw_chart("NOW", THESIS) == "position"
    kind, ticker, position = drawn[0]
    assert (kind, ticker) == ("position", "NOW")
    assert position["entry_price"] == 115.68


def test_held_ticker_never_draws_the_dead_alternate_stop(temp_db, drawn):
    import chart_draw
    _hold_now()
    dmr._redraw_chart("NOW", THESIS)
    prices = [l["price"] for l in chart_draw._lines_for_position(drawn[0][2])]
    assert 100.49 not in prices  # the Alternate's stop, for a trade never taken
    assert 105.02 in prices


def test_waiting_ticker_still_draws_the_stored_setup(temp_db, drawn):
    persistence.save_thesis(ticker="NOW", status="pending", source="SCREENER_v3",
                            primary_setup=THESIS["primary_setup"],
                            alternate_setup=THESIS["alternate_setup"])
    assert dmr._redraw_chart("NOW", THESIS) == "setup"
    assert drawn[0][0] == "setup"


def test_closed_position_goes_back_to_the_setup_lines(temp_db, drawn):
    # A fully exited ticker is no longer held, so the pre-entry plan is once
    # again the right thing to look at.
    _hold_now()
    persistence.record_exit("NOW", exit_price=136.63, exit_qty=138,
                            exit_date="2026-08-07", source="exit_command")
    assert dmr._redraw_chart("NOW", THESIS) == "setup"


def test_no_thesis_and_no_position_draws_nothing(temp_db, drawn):
    assert dmr._redraw_chart("NOW", None) is None
    assert drawn == []


class TestTheSummaryIsBuiltNotCopied:
    """Since 2026-08-10 the Telegram text is built from the stored thesis and
    this run's figures, not copied out of the model's `summary_text`."""

    SETUP = {"type": "Breakout", "trigger": 113.79, "stop": 105.02, "atr_at_build": 6.78,
             "targets": [{"price": 140.0, "pct": 40.0, "rr": 3.0}]}
    ALTERNATE = {"type": "Pullback", "trigger": 107.0, "stop": 100.49,
                 "targets": [{"price": 130.0, "pct": 40.0, "rr": 2.6}]}

    def _thesis(self, **kwargs):
        persistence.save_thesis(ticker="NOW", status="pending", source="SCREENER_v3",
                                 primary_setup=self.SETUP, alternate_setup=self.ALTERNATE,
                                 rubric_grade=kwargs.pop("rubric_grade", "B"),
                                 planned_qty=kwargs.pop("planned_qty", 100))
        return persistence.get_thesis("NOW")

    def _decision(self, **kwargs):
        base = {"ticker": "NOW", "status": "green", "price": 115.0,
                "sentence": "המניה סגרה מעל הרמה.", "setup_used": "primary",
                "order": {"type": "Breakout", "price": 113.79, "stop": 105.02, "qty": 60},
                "rubric_formula_now": {"primary": {"grade": "B", "criteria": {}}},
                "summary_text": "🟠 NOW — משהו אחר לגמרי"}
        base.update(kwargs)
        return base

    def test_the_models_own_summary_is_ignored(self, temp_db):
        thesis = self._thesis()
        text = dmr._build_summary(self._decision(), thesis, 6.78)
        assert "משהו אחר לגמרי" not in text
        assert text.startswith("🟢 <b>NOW</b> — הטריגר הופעל!")

    def test_the_targets_come_from_the_stored_thesis(self, temp_db):
        thesis = self._thesis()
        text = dmr._build_summary(self._decision(), thesis, 6.78)
        assert "<b>140.00</b>" in text

    def test_the_setup_name_comes_from_the_thesis_not_the_order_kind(self, temp_db):
        # Real payloads carry order.type = "limit" -- the kind of ORDER, not one
        # of the six setup names. Reading it first printed "פרטי ההזמנה — limit".
        thesis = self._thesis()
        text = dmr._build_summary(
            self._decision(order={"type": "limit", "price": 113.79, "stop": 105.02, "qty": 60}),
            thesis, 6.78)
        assert monitor_text.SETUP_HE["Breakout"] in text
        assert "limit" not in text

    def test_a_check_on_the_alternate_reads_the_alternate_plan(self, temp_db):
        thesis = self._thesis()
        text = dmr._build_summary(
            self._decision(setup_used="alternate",
                           order={"type": "Pullback", "price": 107.0, "stop": 100.49, "qty": 60},
                           rubric_formula_now={"alternate": {"grade": "B", "criteria": {}}}),
            thesis, 6.78)
        assert "<b>130.00</b>" in text and "140.00" not in text

    def test_a_drifted_entry_is_measured_here_not_claimed(self, temp_db):
        # 115.00 against a planned 113.79: inside max(1%, 0.3 x ATR) -- quiet.
        thesis = self._thesis()
        quiet = dmr._build_summary(self._decision(), thesis, 6.78)
        assert "מתוכנן" not in quiet
        # 125.00 is well outside it, and nobody had to notice for it to show.
        drifted = dmr._build_summary(
            self._decision(order={"type": "Breakout", "price": 125.0, "stop": 105.02, "qty": 60}),
            thesis, 6.78)
        assert "מתוכנן <b>113.79</b>, בפועל <b>125.00</b>" in drifted

    def test_a_stale_trigger_drops_the_order_card(self, temp_db):
        thesis = self._thesis()
        persistence.log_monitor_check("NOW", "green", price=115.0)
        with persistence._db() as conn:
            # The trigger first confirmed weeks ago, on a thesis built before that.
            conn.execute("UPDATE thesis SET date_built='2026-06-01' WHERE ticker='NOW'")
            conn.execute("UPDATE monitor_log SET checked_at='2026-07-01T20:00:00Z' "
                         "WHERE id=(SELECT MAX(id) FROM monitor_log)")
        text = dmr._build_summary(self._decision(), thesis, 6.78)
        assert "📋" not in text
        assert "ימי מסחר" in text

    def test_the_portfolio_disclosures_are_computed_from_the_copied_figures(self, temp_db):
        thesis = self._thesis()
        text = dmr._build_summary(
            self._decision(portfolio_heat_after=0.071, portfolio_heat_cap_pct=0.06,
                           cash_required_usd=9000.0, cash_available_usd=20000.0,
                           cash_usage_warn_pct=0.30),
            thesis, 6.78)
        assert monitor_text.DISCLOSURE_LINES["heat"] in text
        assert monitor_text.DISCLOSURE_LINES["cash"] in text
        assert monitor_text.DISCLOSURE_LINES["sector"] not in text

    def test_the_starter_offer_comes_from_the_stored_plan(self, temp_db):
        thesis = self._thesis(planned_qty=100)
        text = dmr._build_summary(
            self._decision(status="yellow_plus", order=None), thesis, 6.78)
        assert "<b>30 מניות</b>" in text

    def test_a_dropped_idea_that_was_actually_held_says_so(self, temp_db):
        thesis = self._thesis()
        persistence.create_position(ticker="NOW", entry_date="2026-08-04", entry_price=115.68,
                                     qty=138, entry_type="full", entry_setup=self.SETUP,
                                     initial_stop=105.02)
        text = dmr._build_summary(self._decision(status="red", order=None), thesis, 6.78)
        assert monitor_text.HELD_POSITION_NOTE in text


def test_a_failed_draw_is_swallowed_not_raised(temp_db, monkeypatch, capsys):
    """The monitor check is already delivered by the time this runs -- a closed
    TradingView window must not turn a sent report into a failed one."""
    async def _boom(ticker, position):
        raise RuntimeError("TradingView connection lost")

    monkeypatch.setattr(dmr, "_draw_position_chart", _boom)
    _hold_now()

    assert dmr._redraw_chart("NOW", THESIS) is None
    assert "chart draw failed for NOW" in capsys.readouterr().err
