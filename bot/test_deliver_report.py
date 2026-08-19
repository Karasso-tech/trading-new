"""Unit tests for deliver_report.py's success/failure delivery contract
(2026-07-16).

Found in review: this script (and the other deliver_*.py scripts) is exactly
where a bug means either bad data silently saved or a real thesis never
reaching the user, yet had zero test coverage. Heavily mocks the
rendering/TradingView layers (Playwright PNG/PDF rendering and a live CDP
connection aren't appropriate for a unit test, and are irrelevant to the
contract being verified here) to isolate what matters: does a successful run
persist the thesis and mark the message sent, and does a failed Telegram send
mark it failed WITHOUT ever calling save_thesis or mark_sent.
"""

import json
import sys

import pytest

import deliver_report
import persistence


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(persistence, "DB_PATH", db_path)
    persistence.init_db()
    return db_path


@pytest.fixture(autouse=True)
def _isolate_report_writes(tmp_path, monkeypatch):
    """main() writes the delivered report to PROJECT_ROOT/reports/ unconditionally
    -- found while writing these tests: without this, a real test run wrote real
    files into this project's actual reports/ folder. Redirects PROJECT_ROOT to
    the test's own tmp_path instead."""
    (tmp_path / "reports").mkdir(exist_ok=True)
    monkeypatch.setattr(deliver_report, "PROJECT_ROOT", tmp_path)


@pytest.fixture(autouse=True)
def _mock_rendering_and_tv_side_effects(monkeypatch):
    """Every test in this file mocks the same heavy/external pieces: PNG/PDF
    rendering (Playwright) and the TradingView watchlist/chart side-effects (a
    live CDP connection) -- both irrelevant to the success/failure contract
    under test, and _tv_side_effects is already best-effort/non-fatal at its
    real call site (wrapped in its own try/except), so replacing it with a
    no-op changes nothing about what's being verified."""
    monkeypatch.setattr(deliver_report, "render_widget_png", lambda *a, **k: b"fake-png-bytes")
    monkeypatch.setattr(deliver_report, "render_report_pdf", lambda *a, **k: b"fake-pdf-bytes")

    async def _fake_tv_side_effects(*a, **k):
        return None

    monkeypatch.setattr(deliver_report, "_tv_side_effects", _fake_tv_side_effects)


def _decision_file(tmp_path, update_id, ticker="CRM"):
    decision = {
        "ticker": ticker, "update_id": update_id, "date": "2026-07-16", "exchange": "NYSE",
        "decision": "Buy Now", "grade": "B",
        "primary_setup": {
            "type": "Reclaim", "trigger": "260.00", "stop": 250.00, "atr_at_build": 5.0,
            "targets": [], "checkpoints": [],
        },
        "alternate_setup": None,
        "metrics": {}, "verdict": "test verdict",
        "potential": None, "potential_note": "",
        "market_regime": "healthy_uptrend",
        "report_markdown": f"# {ticker} test report",
        "summary_text": "test summary",
    }
    path = tmp_path / f"_decision_{ticker}.json"
    path.write_text(json.dumps(decision), encoding="utf-8")
    return path


def _run_main(path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["deliver_report.py", str(path)])
    try:
        deliver_report.main()
    except SystemExit as e:
        return e.code
    return 0


def _message_status(update_id):
    with persistence._db() as conn:
        row = conn.execute("SELECT status FROM messages WHERE update_id=?", (update_id,)).fetchone()
    return row["status"] if row else None


def _ok(message_id):
    return {"ok": True, "result": {"message_id": message_id}}


class TestSuccessfulDelivery:
    def test_successful_run_saves_thesis_and_marks_sent(self, temp_db, tmp_path, monkeypatch):
        update_id = 222001
        persistence.enqueue_message(update_id=update_id, from_id="test", chat_id="test",
                                     message_type="text", message_text="/screener CRM", raw_update={})
        monkeypatch.setattr(deliver_report, "send_photo", lambda *a, **k: _ok(1))
        monkeypatch.setattr(deliver_report, "send_document", lambda *a, **k: _ok(2))
        monkeypatch.setattr(deliver_report, "send_text", lambda *a, **k: _ok(3))

        exit_code = _run_main(_decision_file(tmp_path, update_id), monkeypatch)

        assert exit_code == 0
        assert _message_status(update_id) == "sent"
        thesis = persistence.get_thesis("CRM")
        assert thesis is not None
        assert thesis["status"] == "pending"
        assert thesis["primary_setup"]["type"] == "Reclaim"


class TestFailedTelegramSend:
    @pytest.mark.parametrize("failing_call", ["send_photo", "send_document", "send_text"])
    def test_any_failed_send_marks_failed_and_never_saves_thesis_or_marks_sent(
        self, temp_db, tmp_path, monkeypatch, failing_call
    ):
        update_id = 222002
        persistence.enqueue_message(update_id=update_id, from_id="test", chat_id="test",
                                     message_type="text", message_text="/screener CRM", raw_update={})
        calls = {"send_photo": _ok(1), "send_document": _ok(2), "send_text": _ok(3)}
        calls[failing_call] = {"ok": False}
        monkeypatch.setattr(deliver_report, "send_photo", lambda *a, **k: calls["send_photo"])
        monkeypatch.setattr(deliver_report, "send_document", lambda *a, **k: calls["send_document"])
        monkeypatch.setattr(deliver_report, "send_text", lambda *a, **k: calls["send_text"])

        exit_code = _run_main(_decision_file(tmp_path, update_id), monkeypatch)

        assert exit_code == 1
        assert _message_status(update_id) == "failed"
        assert persistence.get_thesis("CRM") is None  # never reached save_thesis


class TestLintFailedTargetStripping:
    """Found in review, real incident (AMZN, 2026-07-16): a target whose
    stated ATR-distance/R:R fails rule 3's qualify gate used to still render
    as a normal target with only a contradicting warning appended alongside
    it -- see report_lint.failing_target_keys's own docstring. Confirms the
    bad target is now actually removed from both the rendered widget and the
    persisted thesis, while a genuinely passing target on the same setup is
    left untouched."""

    def test_bad_target_is_stripped_from_widget_and_saved_thesis(self, temp_db, tmp_path, monkeypatch):
        update_id = 222003
        persistence.enqueue_message(update_id=update_id, from_id="test", chat_id="test",
                                     message_type="text", message_text="/screener CRM", raw_update={})
        monkeypatch.setattr(deliver_report, "send_photo", lambda *a, **k: _ok(1))
        monkeypatch.setattr(deliver_report, "send_document", lambda *a, **k: _ok(2))
        monkeypatch.setattr(deliver_report, "send_text", lambda *a, **k: _ok(3))

        captured_widget_data = {}

        def _capture_widget(widget_data):
            captured_widget_data.update(widget_data)
            return b"fake-png-bytes"

        monkeypatch.setattr(deliver_report, "render_widget_png", _capture_widget)

        decision = {
            "ticker": "CRM", "update_id": update_id, "date": "2026-07-16", "exchange": "NYSE",
            "decision": "Buy Now", "grade": "B",
            "primary_setup": {
                "type": "Reclaim", "trigger": 100.0, "stop": 97.0, "atr_at_build": 2.0,
                "targets": [
                    # dist=1.0x ATR (1.0-1.5x band, needs RR>=2.5) -- stated RR 0.67 fails the gate.
                    {"price": 102.0, "pct": "40%", "atr_mult": "1.00x", "rr": "0.67", "status": "pass"},
                    # dist=3.0x ATR (>=1.5x band, needs RR>=2) -- stated RR 2.00 passes cleanly.
                    {"price": 106.0, "pct": "60%", "atr_mult": "3.00x", "rr": "2.00", "status": "pass"},
                ],
                "checkpoints": [],
            },
            "alternate_setup": None,
            "metrics": {}, "verdict": "test verdict",
            "potential": None, "potential_note": "",
            "market_regime": "healthy_uptrend",
            "report_markdown": "# CRM test report",
            "summary_text": "test summary",
        }
        path = tmp_path / "_decision_CRM.json"
        path.write_text(json.dumps(decision), encoding="utf-8")

        exit_code = _run_main(path, monkeypatch)

        assert exit_code == 0
        primary_setup_widget = next(s for s in captured_widget_data["setups"] if s["role"] == "primary")
        assert len(primary_setup_widget["targets"]) == 1
        assert primary_setup_widget["targets"][0]["price"] == "106.0"

        thesis = persistence.get_thesis("CRM")
        assert len(thesis["primary_setup"]["targets"]) == 1
        assert thesis["primary_setup"]["targets"][0]["price"] == 106.0


# Captured at import, before any fixture runs: the autouse
# _mock_rendering_and_tv_side_effects above replaces _tv_side_effects with a
# no-op for every test in this file, and the class below is the one place that
# needs the REAL one -- it is the function under test, not a side effect of it.
_REAL_TV_SIDE_EFFECTS = deliver_report._tv_side_effects


class TestScreenerDrawsPositionLinesWhenHeld:
    """A re-screen of a ticker the user already holds still saves and reports
    the fresh thesis in full -- but the CHART has to keep showing the trade
    that is actually on, with its real trailed stop, not a plan for an entry
    that already happened (2026-08-07, the NOW incident)."""

    class _StubClient:
        def __init__(self):
            self.watchlisted = []
            self.position_draws = []
            self.setup_draws = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def add_to_watchlist(self, ticker, list_name=None):
            self.watchlisted.append(ticker)

    def _run(self, monkeypatch, position):
        import chart_draw
        stub = self._StubClient()
        monkeypatch.setattr(deliver_report, "TVClient", lambda: stub)

        async def _fake_position(client, symbol, pos, timeframe="D"):
            stub.position_draws.append((symbol, pos))
            return 3

        async def _fake_setup(client, symbol, primary, alternate, timeframe="D"):
            stub.setup_draws.append((symbol, primary, alternate))
            return 9

        monkeypatch.setattr(chart_draw, "annotate_position_chart", _fake_position)
        monkeypatch.setattr(chart_draw, "annotate_chart", _fake_setup)
        deliver_report.asyncio.run(_REAL_TV_SIDE_EFFECTS(
            "NOW", {"type": "Breakout", "stop": 105.02},
            {"type": "Pullback", "stop": 100.49}, position=position))
        return stub

    def test_held_ticker_draws_the_position_not_the_new_plan(self, monkeypatch):
        stub = self._run(monkeypatch, position={"ticker": "NOW", "entry_price": 115.68,
                                                 "current_stop": 105.02})
        assert stub.setup_draws == []
        assert stub.position_draws and stub.position_draws[0][0] == "NOW"

    def test_unheld_ticker_still_draws_the_setup_unchanged(self, monkeypatch):
        stub = self._run(monkeypatch, position=None)
        assert stub.position_draws == []
        assert stub.setup_draws and stub.setup_draws[0][0] == "NOW"

    def test_watchlist_sync_happens_either_way(self, monkeypatch):
        assert self._run(monkeypatch, position=None).watchlisted == ["NOW"]
        assert self._run(monkeypatch, position={"entry_price": 1.0}).watchlisted == ["NOW"]


class TestPercentagesAreNotOffByAHundred:
    """Found on the first live /screener run, 2026-08-09. The delivered ORCL
    report told the owner his portfolio heat after the trade would be 0.05%
    against a cap of 0.06%. The real figures were 4.8% and 6%.

    Every ratio in this system is stored as a FRACTION -- risk_pct is 0.01,
    portfolio_heat_cap_pct is 0.06, sector_cap_pct is 0.40 -- while the field
    names all end in "pct". The template appends its own % sign, so passing the
    stored value straight through is wrong by a factor of a hundred."""

    def test_a_stored_fraction_becomes_a_real_percentage(self):
        assert deliver_report._as_percent(0.048) == pytest.approx(4.8)
        assert deliver_report._as_percent(0.06) == pytest.approx(6.0)
        assert deliver_report._as_percent(0.40) == pytest.approx(40.0)

    def test_a_value_already_in_percent_is_left_alone(self):
        # Nothing here holds a fraction above 1.0 -- that would mean risking
        # more than the whole account -- so a bigger number is already converted.
        assert deliver_report._as_percent(4.8) == pytest.approx(4.8)
        assert deliver_report._as_percent(72.29) == pytest.approx(72.29)

    def test_missing_is_none_not_zero(self):
        assert deliver_report._as_percent(None) is None
        assert deliver_report._as_percent("not a number") is None

    def test_a_string_fraction_still_converts(self):
        assert deliver_report._as_percent("0.048") == pytest.approx(4.8)

    def test_the_real_orcl_numbers_render_correctly(self):
        decision = {
            "ticker": "ORCL", "decision": "Buy Only If Confirmed",
            "rubric_grade": "B", "thesis_sentence": "x",
            "primary_setup": {"type": "Reclaim", "trigger": 153.06, "stop": 145.30,
                               "targets": [{"price": 171.76, "pct": 40.0, "rr": 2.41}]},
            "portfolio_heat_after": 0.0481, "portfolio_heat_cap_pct": 0.06,
            "cash_required_usd": 11173.38, "cash_available_usd": 11257.90,
            "sizing": {"qty": 73},
        }
        text = deliver_report.build_summary_he(decision)
        assert "<b>4.81</b>%" in text
        assert "<b>6.00</b>%" in text
        assert "<b>0.05</b>%" not in text


class TestReplacingAFiredThesisIsAnnounced:
    """Found by the owner on the first live run, 2026-08-09. A /screener ORCL
    replaced a plan whose trigger had confirmed green on 08-07 and again on
    08-08, and produced a different entry and stop -- on a day the market never
    opened. From the phone it looked like the numbers had changed by themselves.

    The rebuild is allowed (it was asked for, and the new plan may be better).
    Being silent about it is not."""

    def _thesis(self, trigger, stop, built):
        persistence.save_thesis(
            ticker="ORCL", status="pending", source="SCREENER_v3",
            primary_setup={"type": "Reclaim", "trigger": trigger, "stop": stop,
                            "atr_at_build": 7.6, "targets": []},
            decision="Buy Only If Confirmed", rubric_grade="B",
        )
        with persistence._db() as c:
            c.execute("UPDATE thesis SET date_built=? WHERE ticker='ORCL'", (built,))

    def _new_decision(self):
        return {"primary_setup": {"type": "Reclaim", "trigger": 153.06, "stop": 145.30}}

    def _green_on(self, when):
        """A green dated in the past, like the real ORCL ones (08-07, 08-08)
        against a rebuild on 08-09. log_monitor_check always stamps 'now', and
        the timing is the whole point of this class."""
        persistence.log_monitor_check("ORCL", status="green", price=147.02)
        with persistence._db() as c:
            c.execute("UPDATE monitor_log SET checked_at=? WHERE ticker='ORCL'", (when,))

    def test_a_fired_thesis_being_replaced_is_announced_with_both_plans(self, temp_db):
        self._thesis(149.65, 139.83, "2026-08-06T05:51:48+00:00")
        self._green_on("2026-08-07T20:35:29+00:00")
        block = deliver_report.replaced_fired_thesis_block_he("ORCL", self._new_decision())
        assert block
        assert "149.65" in block and "139.83" in block      # what it replaced
        assert "153.06" in block and "145.30" in block      # what replaced it
        assert "לא כי המחיר זז" in block                     # the actual confusion

    def test_a_thesis_that_never_fired_says_nothing(self, temp_db):
        self._thesis(149.65, 139.83, "2026-08-06T05:51:48+00:00")
        assert deliver_report.replaced_fired_thesis_block_he("ORCL", self._new_decision()) == ""

    def test_a_brand_new_ticker_says_nothing(self, temp_db):
        assert deliver_report.replaced_fired_thesis_block_he("ZZZZ", self._new_decision()) == ""

    def test_it_is_read_before_the_overwrite_not_after(self, temp_db):
        # save_thesis resets date_built to today, and get_trigger_fired_age only
        # counts greens on or after that date -- so once the rebuild lands the
        # fired signal is invisible. Reading afterwards would always return "".
        self._thesis(149.65, 139.83, "2026-08-06T05:51:48+00:00")
        self._green_on("2026-08-07T20:35:29+00:00")
        assert deliver_report.replaced_fired_thesis_block_he("ORCL", self._new_decision())
        self._thesis(153.06, 145.30, persistence._now())      # the rebuild lands
        assert persistence.get_trigger_fired_age("ORCL") is None
