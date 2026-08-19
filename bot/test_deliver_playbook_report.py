"""Unit tests for deliver_playbook_report.py's target-metrics correction and
take-profit heads-up (rule 3, changed 2026-07-31).

Originally (2026-07-16) a target whose current-price-based ATR-distance/R:R
failed rule 3's gate was stripped from the widget and relabeled Checkpoint --
see report_lint.compute_all_target_metrics's docstring for why that's gone:
an open position's target is graded once, at entry, never re-tested against
a moving current price (R:R mechanically shrinks as price runs toward any
target -- that's normal, not evidence the target went bad). What survives
from the original fix is the DISPLAYED-number correction (the model's stated
atr_mult/rr get overwritten with the real computed value regardless of gate
result) and a new plain proximity flag: when current price is close enough
to a stored target, that's a take-profit heads-up, not a validity verdict.

Heavily mocks the rendering/Telegram layers (Playwright PNG/PDF and the
actual HTTP calls aren't appropriate for a unit test and are irrelevant to
the fix being verified here), same pattern as test_deliver_report.py.
"""

import json
import sys

import pytest

import deliver_playbook_report as dpr
import position_text
import persistence


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(persistence, "DB_PATH", db_path)
    persistence.init_db()
    return db_path


@pytest.fixture(autouse=True)
def _isolate_report_writes(tmp_path, monkeypatch):
    (tmp_path / "reports").mkdir(exist_ok=True)
    monkeypatch.setattr(dpr, "PROJECT_ROOT", tmp_path)


def _ok(message_id):
    return {"ok": True, "result": {"message_id": message_id}}


def _run_main(path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["deliver_playbook_report.py", str(path)])
    try:
        dpr.main()
    except SystemExit as e:
        return e.code
    return 0


class TestTargetsNeverStrippedFromOpenPositions:
    def test_amzn_style_gate_failing_target_is_kept_and_corrected_not_stripped(self, temp_db, tmp_path, monkeypatch):
        update_id = 333001
        persistence.enqueue_message(update_id=update_id, from_id="test", chat_id="test",
                                     message_type="text", message_text="portfolio screenshot", raw_update={})
        monkeypatch.setattr(dpr, "send_photo", lambda *a, **k: _ok(1))
        monkeypatch.setattr(dpr, "send_document", lambda *a, **k: _ok(2))
        monkeypatch.setattr(dpr, "send_text", lambda *a, **k: _ok(3))
        monkeypatch.setattr(dpr, "render_report_pdf", lambda *a, **k: b"fake-pdf-bytes")

        captured_widget_data = {}

        def _capture_widget(widget_data):
            captured_widget_data.update(widget_data)
            return b"fake-png-bytes"

        monkeypatch.setattr(dpr, "render_widget_png", _capture_widget)

        decision = {
            "update_id": update_id, "date": "2026-07-16",
            "positions": [
                {
                    "ticker": "AMZN", "sleeve": "swing", "qty": 50, "avg": 240.58, "price": 254.96,
                    "action": "Hold With Alert", "stop": 238.25, "atr_at_build": 8.59,
                    "targets": [
                        # Real AMZN numbers: current price 254.96, atr 8.59 -> dist =
                        # |258.60-254.96|/8.59 = 0.42x (fails the gate outright, <1.0x).
                        # Stated 2.10x/2.66 (computed from avg cost, not current price).
                        {"price": "258.60", "pct": "40%", "atr_mult": "2.10x", "rr": "2.66", "status": "pass"},
                        # A second position's clean target, same ticker's setup, to
                        # confirm only the bad one is removed if both existed together
                        # -- but AMZN only had 2 targets both failing in the real
                        # incident, so this uses a distinct, genuinely passing level:
                        # dist = |300-254.96|/8.59 = 5.24x, rr = (300-254.96)/(254.96-238.25)=2.70 -- passes.
                        {"price": "300.00", "pct": "35%", "atr_mult": "5.24x", "rr": "2.70", "status": "pass"},
                    ],
                },
            ],
            "report_markdown": "# portfolio test report",
            "summary_text": "test summary",
        }
        path = tmp_path / "_decision_playbook_test.json"
        path.write_text(json.dumps(decision), encoding="utf-8")

        exit_code = _run_main(path, monkeypatch)

        assert exit_code == 0
        amzn_setup = next(s for s in captured_widget_data["setups"] if s["title"].startswith("AMZN"))
        # Both targets kept -- rule 3 no longer strips an open position's target
        # for failing the gate against current price; only the displayed
        # atr_mult/rr get corrected to the real computed value.
        assert len(amzn_setup["targets"]) == 2
        prices = {t["price"] for t in amzn_setup["targets"]}
        assert prices == {"258.60", "300.00"}
        first = next(t for t in amzn_setup["targets"] if t["price"] == "258.60")
        assert first["atr_mult"] == "0.42x"  # corrected, was stated "2.10x"

    def test_nvda_style_still_qualifying_target_gets_its_displayed_number_corrected(
        self, temp_db, tmp_path, monkeypatch
    ):
        """Real 2026-07-16 incident: NVDA's two targets both still cleared the
        qualify gate for real (unlike AMZN above, neither failed outright), but
        the DISPLAYED ATR-distance was measurably wrong for both (2.68x/3.26x
        shown, 2.78x/3.37x actually computed from the real stored atr_at_build
        and reported current price) -- traced to an internally-consistent
        ~0.7-1.0-point-higher implied reference price behind the model's own
        numbers than what it finally reported, not a gate-relevant judgment
        error. Confirms both targets survive (still valid) but their displayed
        atr_mult is corrected to the verified figure."""
        update_id = 333002
        persistence.enqueue_message(update_id=update_id, from_id="test", chat_id="test",
                                     message_type="text", message_text="portfolio screenshot", raw_update={})
        monkeypatch.setattr(dpr, "send_photo", lambda *a, **k: _ok(1))
        monkeypatch.setattr(dpr, "send_document", lambda *a, **k: _ok(2))
        monkeypatch.setattr(dpr, "send_text", lambda *a, **k: _ok(3))
        monkeypatch.setattr(dpr, "render_report_pdf", lambda *a, **k: b"fake-pdf-bytes")

        captured_widget_data = {}

        def _capture_widget(widget_data):
            captured_widget_data.update(widget_data)
            return b"fake-png-bytes"

        monkeypatch.setattr(dpr, "render_widget_png", _capture_widget)

        decision = {
            "update_id": update_id, "date": "2026-07-16",
            "positions": [
                {
                    "ticker": "NVDA", "sleeve": "swing", "qty": 75, "avg": 208.62, "price": 212.5,
                    "action": "Hold", "stop": 206.04, "atr_at_build": 7.127252572592015,
                    "targets": [
                        {"price": "232.28", "pct": "40%", "atr_mult": "2.68x", "rr": "3.06", "status": "pass"},
                        {"price": "236.54", "pct": "35%", "atr_mult": "3.26x", "rr": "3.72", "status": "pass"},
                    ],
                },
            ],
            "report_markdown": "# portfolio test report",
            "summary_text": "test summary",
        }
        path = tmp_path / "_decision_playbook_test2.json"
        path.write_text(json.dumps(decision), encoding="utf-8")

        exit_code = _run_main(path, monkeypatch)

        assert exit_code == 0
        nvda_setup = next(s for s in captured_widget_data["setups"] if s["title"].startswith("NVDA"))
        assert len(nvda_setup["targets"]) == 2  # both still valid -- neither removed
        assert nvda_setup["targets"][0]["atr_mult"] == "2.78x"  # corrected, was "2.68x"
        assert nvda_setup["targets"][1]["atr_mult"] == "3.37x"  # corrected, was "3.26x"


class TestAtrRrMismatchNoLongerAlarmsTheUser:
    """2026-07-22 fix: the recurring real failure (07-20 AMZN/LLY/CRM/UPS,
    same tickers again 07-22 with CRDO/INCY, every single /playbook run) --
    the model kept stating a target's ATR-distance/R:R against a fresher
    current ATR instead of the frozen atr_at_build. Since
    compute_all_target_metrics now always overwrites the displayed number
    regardless of what finding fired, telling the user "displayed X but
    actually Y" is stale-by-construction noise once they're only ever shown
    Y -- these two checks must no longer appear in the Telegram text, even
    though the underlying numbers are still (silently) corrected."""

    def _run(self, decision, monkeypatch, tmp_path, update_id):
        persistence.enqueue_message(update_id=update_id, from_id="test", chat_id="test",
                                     message_type="text", message_text="portfolio screenshot", raw_update={})
        monkeypatch.setattr(dpr, "send_photo", lambda *a, **k: _ok(1))
        monkeypatch.setattr(dpr, "send_document", lambda *a, **k: _ok(2))
        sent_texts = []
        monkeypatch.setattr(dpr, "send_text", lambda text, *a, **k: (sent_texts.append(text), _ok(3))[1])
        monkeypatch.setattr(dpr, "render_report_pdf", lambda *a, **k: b"fake-pdf-bytes")
        monkeypatch.setattr(dpr, "render_widget_png", lambda *a, **k: b"fake-png-bytes")
        path = tmp_path / f"_decision_playbook_{update_id}.json"
        path.write_text(json.dumps(decision), encoding="utf-8")
        exit_code = _run_main(path, monkeypatch)
        return exit_code, sent_texts

    def test_nvda_style_wrong_but_still_qualifying_target_no_longer_shows_a_warning(
        self, temp_db, tmp_path, monkeypatch
    ):
        # Same fixture as TestLintFailedTargetStripping's NVDA test above --
        # both targets still clear the gate for real, only their DISPLAYED
        # number was wrong. Before this fix, that alone would have printed
        # the "⚠️ בדיקת אימות מספרית נכשלה" block; now it must not.
        decision = {
            "update_id": 333011, "date": "2026-07-22",
            "positions": [
                {"ticker": "NVDA", "sleeve": "swing", "qty": 75, "avg": 208.62, "price": 212.5,
                 "action": "Hold", "stop": 206.04, "atr_at_build": 7.127252572592015,
                 "targets": [
                     {"price": "232.28", "pct": "40%", "atr_mult": "2.68x", "rr": "3.06", "status": "pass"},
                     {"price": "236.54", "pct": "35%", "atr_mult": "3.26x", "rr": "3.72", "status": "pass"},
                 ]},
            ],
            "report_markdown": "# test", "summary_text": "test summary",
        }
        exit_code, sent_texts = self._run(decision, monkeypatch, tmp_path, 333011)
        assert exit_code == 0
        assert "בדיקת אימות מספרית נכשלה" not in sent_texts[0]

    def test_amzn_style_close_target_shows_take_profit_heads_up_not_a_gate_failure(
        self, temp_db, tmp_path, monkeypatch
    ):
        # Rule 3, changed 2026-07-31: current price close to a stored target
        # (here dist=0.42x ATR) is a take-profit heads-up now, never a
        # "checkpoint_mislabeled_as_target"/gate-failure warning -- that check
        # no longer runs at all for an already-open position's target.
        decision = {
            "update_id": 333012, "date": "2026-07-22",
            "positions": [
                {"ticker": "AMZN", "sleeve": "swing", "qty": 50, "avg": 240.58, "price": 254.96,
                 "action": "Hold With Alert", "stop": 238.25, "atr_at_build": 8.59,
                 "targets": [
                     {"price": "258.60", "pct": "40%", "atr_mult": "2.10x", "rr": "2.66", "status": "pass"},
                 ]},
            ],
            "report_markdown": "# test", "summary_text": "test summary",
        }
        exit_code, sent_texts = self._run(decision, monkeypatch, tmp_path, 333012)
        assert exit_code == 0
        assert "שקול מימוש רווח" in sent_texts[0]
        assert "לא עומד בשער" not in sent_texts[0]
        assert "מרחק ATR מוצג" not in sent_texts[0]

    def test_target_survives_and_is_corrected_even_with_no_stated_atr_mult_or_rr_at_all(
        self, temp_db, tmp_path, monkeypatch
    ):
        # 2026-07-22: the prompt no longer asks the model for these fields at
        # all -- the widget must still get the correct computed number. Does
        # NOT use self._run (it hardcodes render_widget_png to a plain stub,
        # which would clobber the capturing stub this test needs).
        update_id = 333013
        persistence.enqueue_message(update_id=update_id, from_id="test", chat_id="test",
                                     message_type="text", message_text="portfolio screenshot", raw_update={})
        monkeypatch.setattr(dpr, "send_photo", lambda *a, **k: _ok(1))
        monkeypatch.setattr(dpr, "send_document", lambda *a, **k: _ok(2))
        monkeypatch.setattr(dpr, "send_text", lambda *a, **k: _ok(3))
        monkeypatch.setattr(dpr, "render_report_pdf", lambda *a, **k: b"fake-pdf-bytes")

        captured_widget_data = {}

        def _capture_widget(widget_data):
            captured_widget_data.update(widget_data)
            return b"fake-png-bytes"

        monkeypatch.setattr(dpr, "render_widget_png", _capture_widget)
        decision = {
            "update_id": update_id, "date": "2026-07-22",
            "positions": [
                {"ticker": "AMZN", "sleeve": "swing", "qty": 50, "avg": 240.58, "price": 254.96,
                 "action": "Hold", "stop": 238.25, "atr_at_build": 8.59,
                 "targets": [{"price": "300.00", "pct": "35%"}]},  # no atr_mult/rr keys at all
            ],
            "report_markdown": "# test", "summary_text": "test summary",
        }
        path = tmp_path / f"_decision_playbook_{update_id}.json"
        path.write_text(json.dumps(decision), encoding="utf-8")
        exit_code = _run_main(path, monkeypatch)

        assert exit_code == 0
        amzn_setup = next(s for s in captured_widget_data["setups"] if s["title"].startswith("AMZN"))
        assert len(amzn_setup["targets"]) == 1
        assert amzn_setup["targets"][0]["atr_mult"] == "5.24x"
        assert amzn_setup["targets"][0]["rr"] == "2.70"


class TestAutoEquityUpdate:
    """Found 2026-07-18: a real $17,500 pending withdrawal exposed that
    equity had to be updated manually every time. account_equity_usd, read
    by the model off the same portfolio screenshot already used for
    /playbook, now auto-refreshes equity_usd every run instead."""

    def _run(self, decision, monkeypatch, tmp_path, update_id):
        persistence.enqueue_message(update_id=update_id, from_id="test", chat_id="test",
                                     message_type="text", message_text="portfolio screenshot", raw_update={})
        monkeypatch.setattr(dpr, "send_photo", lambda *a, **k: _ok(1))
        monkeypatch.setattr(dpr, "send_document", lambda *a, **k: _ok(2))
        sent_texts = []
        monkeypatch.setattr(dpr, "send_text", lambda text, *a, **k: (sent_texts.append(text), _ok(3))[1])
        monkeypatch.setattr(dpr, "render_report_pdf", lambda *a, **k: b"fake-pdf-bytes")
        monkeypatch.setattr(dpr, "render_widget_png", lambda *a, **k: b"fake-png-bytes")
        path = tmp_path / f"_decision_playbook_{update_id}.json"
        path.write_text(json.dumps(decision), encoding="utf-8")
        exit_code = _run_main(path, monkeypatch)
        return exit_code, sent_texts

    def test_account_equity_usd_present_updates_equity(self, temp_db, tmp_path, monkeypatch):
        decision = {
            "update_id": 333003, "date": "2026-07-18", "account_equity_usd": 100000.0,
            "positions": [{"ticker": "SPY", "sleeve": "core", "qty": 78, "avg": 726.10,
                           "price": 745.40, "action": "Hold", "stop": 700.91, "targets": []}],
            "report_markdown": "# test", "summary_text": "test summary",
        }
        exit_code, _ = self._run(decision, monkeypatch, tmp_path, 333003)
        assert exit_code == 0
        assert persistence.get_account_settings()["equity_usd"] == 100000.0

    def test_missing_account_equity_usd_does_not_crash_or_touch_equity(self, temp_db, tmp_path, monkeypatch):
        persistence.set_equity(100000)
        decision = {
            "update_id": 333004, "date": "2026-07-18",
            "positions": [{"ticker": "SPY", "sleeve": "core", "qty": 78, "avg": 726.10,
                           "price": 745.40, "action": "Hold", "stop": 700.91, "targets": []}],
            "report_markdown": "# test", "summary_text": "test summary",
        }
        exit_code, _ = self._run(decision, monkeypatch, tmp_path, 333004)
        assert exit_code == 0
        assert persistence.get_account_settings()["equity_usd"] == 100000  # untouched

    def test_invalid_account_equity_usd_does_not_crash_delivery(self, temp_db, tmp_path, monkeypatch):
        persistence.set_equity(100000)
        decision = {
            "update_id": 333005, "date": "2026-07-18", "account_equity_usd": "not-a-number",
            "positions": [{"ticker": "SPY", "sleeve": "core", "qty": 78, "avg": 726.10,
                           "price": 745.40, "action": "Hold", "stop": 700.91, "targets": []}],
            "report_markdown": "# test", "summary_text": "test summary",
        }
        exit_code, _ = self._run(decision, monkeypatch, tmp_path, 333005)
        assert exit_code == 0  # delivery still succeeds
        assert persistence.get_account_settings()["equity_usd"] == 100000  # left untouched, not crashed

    def test_pending_withdrawal_reminder_shown_when_auto_updating(self, temp_db, tmp_path, monkeypatch):
        persistence.set_pending_withdrawal(17500)
        decision = {
            "update_id": 333006, "date": "2026-07-18", "account_equity_usd": 100000.0,
            "positions": [{"ticker": "SPY", "sleeve": "core", "qty": 78, "avg": 726.10,
                           "price": 745.40, "action": "Hold", "stop": 700.91, "targets": []}],
            "report_markdown": "# test", "summary_text": "test summary",
        }
        exit_code, sent_texts = self._run(decision, monkeypatch, tmp_path, 333006)
        assert exit_code == 0
        assert "17,500" in sent_texts[0]
        assert "/withdraw 0" in sent_texts[0]

    def test_no_reminder_when_nothing_pending(self, temp_db, tmp_path, monkeypatch):
        decision = {
            "update_id": 333007, "date": "2026-07-18", "account_equity_usd": 100000.0,
            "positions": [{"ticker": "SPY", "sleeve": "core", "qty": 78, "avg": 726.10,
                           "price": 745.40, "action": "Hold", "stop": 700.91, "targets": []}],
            "report_markdown": "# test", "summary_text": "test summary",
        }
        exit_code, sent_texts = self._run(decision, monkeypatch, tmp_path, 333007)
        assert exit_code == 0
        assert "/withdraw" not in sent_texts[0]


class TestMarketRegimeSummaryLine:
    """2026-07-20: STRATEGY_v3.md's §ב regime call is now sourced verbatim from
    market_regime_formula.regime (rule 23, CONSISTENCY_RULES.md) the same way
    SCREENER_v3/MONITOR_v2 already do, and surfaced as its own line at the top
    of the /playbook Telegram summary so it's visible without opening the PDF."""

    def _run(self, decision, monkeypatch, tmp_path, update_id):
        persistence.enqueue_message(update_id=update_id, from_id="test", chat_id="test",
                                     message_type="text", message_text="portfolio screenshot", raw_update={})
        monkeypatch.setattr(dpr, "send_photo", lambda *a, **k: _ok(1))
        monkeypatch.setattr(dpr, "send_document", lambda *a, **k: _ok(2))
        sent_texts = []
        monkeypatch.setattr(dpr, "send_text", lambda text, *a, **k: (sent_texts.append(text), _ok(3))[1])
        monkeypatch.setattr(dpr, "render_report_pdf", lambda *a, **k: b"fake-pdf-bytes")
        monkeypatch.setattr(dpr, "render_widget_png", lambda *a, **k: b"fake-png-bytes")
        path = tmp_path / f"_decision_playbook_{update_id}.json"
        path.write_text(json.dumps(decision), encoding="utf-8")
        exit_code = _run_main(path, monkeypatch)
        return exit_code, sent_texts

    def _base_decision(self, update_id, **extra):
        return {
            "update_id": update_id, "date": "2026-07-20",
            "positions": [{"ticker": "SPY", "sleeve": "core", "qty": 78, "avg": 726.10,
                           "price": 745.40, "action": "Hold", "stop": 700.91, "targets": []}],
            "report_markdown": "# test", "summary_text": "test summary",
            **extra,
        }

    def test_market_regime_line_shown_when_present(self, temp_db, tmp_path, monkeypatch):
        decision = self._base_decision(333008, market_regime="neutral_choppy")
        exit_code, sent_texts = self._run(decision, monkeypatch, tmp_path, 333008)
        assert exit_code == 0
        # 2026-08-10: shown in plain words now, through the one table the whole
        # system shares -- the raw English token is never put in front of the
        # reader (STRATEGY_v3.md section ח).
        assert "⭐ מצב שוק כללי: שוק מבולבל, בלי כיוון ברור" in sent_texts[0]
        assert "neutral_choppy" not in sent_texts[0]

    def test_market_regime_line_absent_when_field_missing(self, temp_db, tmp_path, monkeypatch):
        decision = self._base_decision(333009)
        exit_code, sent_texts = self._run(decision, monkeypatch, tmp_path, 333009)
        assert exit_code == 0
        assert "מצב שוק" not in sent_texts[0]

    def test_market_regime_override_reason_shown(self, temp_db, tmp_path, monkeypatch):
        decision = self._base_decision(
            333010,
            market_regime="risk_off",
            market_regime_formula={"regime": "healthy_uptrend"},
            regime_override_reason="FOMC today",
        )
        exit_code, sent_texts = self._run(decision, monkeypatch, tmp_path, 333010)
        assert exit_code == 0
        # Both halves in plain words: what this run used, and what the formula
        # said, with the stated reason for the difference.
        assert "שוק חלש" in sent_texts[0]
        assert "מגמה עולה בריאה" in sent_texts[0] and "FOMC today" in sent_texts[0]
        assert "healthy_uptrend" not in sent_texts[0]


class TestTheReviewIsBuiltNotCopied:
    """Since 2026-08-10 the review text is built from the stored positions plus
    this run's figures, not copied out of the model's `summary_text`."""

    SETUP = {"type": "Breakout", "trigger": 95.0, "stop": 88.0, "atr_at_build": 4.0,
             "targets": [{"price": 130.0, "pct": 40.0, "qty": 20},
                         {"price": 150.0, "pct": 35.0, "qty": 17}]}

    def _open_pltr(self, *, qty=50, entry_price=90.0, entry_type="full", planned_qty=90):
        persistence.save_thesis(ticker="PLTR", status="pending", source="SCREENER_v3",
                                 primary_setup=self.SETUP, planned_qty=planned_qty)
        persistence.create_position(ticker="PLTR", entry_date="2026-07-10",
                                     entry_price=entry_price, qty=qty, entry_type=entry_type,
                                     entry_setup=self.SETUP, initial_stop=88.0)

    def _decision(self, **position_extra):
        position = {"ticker": "PLTR", "sleeve": "swing", "qty": 50, "avg": 90.0,
                    "price": 100.0, "action": "Hold", "stop": 88.0, "targets": []}
        position.update(position_extra)
        return {"update_id": 334001, "date": "2026-08-10", "positions": [position],
                "report_markdown": "# test", "summary_text": "🟠 משהו אחר לגמרי"}

    def test_the_models_own_summary_is_ignored(self, temp_db):
        self._open_pltr()
        text = dpr._build_summary(self._decision())
        assert "משהו אחר לגמרי" not in text
        assert text.startswith("📊 <b>תיק ההשקעות שלי — 2026-08-10</b>")

    def test_the_profit_and_stop_come_from_the_recorded_position(self, temp_db):
        self._open_pltr(entry_price=90.0, qty=50)
        persistence.update_current_stop("PLTR", 93.0)
        text = dpr._build_summary(self._decision(stop=55.55))
        assert "<b>11.11</b>% ($500.00)" in text
        assert "🛑 סטופ: <b>93.00</b>" in text and "55.55" not in text

    def test_a_sold_tranche_is_never_shown_as_a_target_again(self, temp_db):
        # The ASTS incident: one stored target, sold into twice.
        self._open_pltr()
        persistence.record_exit(ticker="PLTR", exit_date="2026-08-01", exit_price=130.0,
                                 exit_qty=20, source="exit_command")
        text = dpr._build_summary(self._decision())
        assert "<b>150.00</b>" in text
        assert "130.00" not in text

    def test_fewer_shares_on_the_screen_asks_for_exit_with_the_real_numbers(self, temp_db):
        self._open_pltr(qty=50)
        text = dpr._build_summary(self._decision(qty=30))
        assert position_text.MISMATCH_SOLD in text
        assert "<code>/exit PLTR 100.00 20</code>" in text

    def test_more_shares_on_the_screen_asks_for_add(self, temp_db):
        self._open_pltr(qty=50)
        text = dpr._build_summary(self._decision(qty=65))
        assert "<code>/add PLTR 100.00 15</code>" in text

    def test_a_ticker_this_system_never_filled_asks_for_filled(self, temp_db):
        text = dpr._build_summary(self._decision())
        assert "<code>/filled PLTR 100.00 50 full</code>" in text

    def test_a_partial_exit_alone_is_not_reported_as_a_discrepancy(self, temp_db):
        # qty stays at the original fill size by design; remaining_qty is what
        # is actually held. Comparing against the wrong one flags every
        # position that has ever taken profit.
        self._open_pltr(qty=50)
        persistence.record_exit(ticker="PLTR", exit_date="2026-08-01", exit_price=130.0,
                                 exit_qty=20, source="exit_command")
        text = dpr._build_summary(self._decision(qty=30))
        assert "🔴" not in text

    def test_a_starter_gets_its_confirmation_line(self, temp_db):
        self._open_pltr(qty=30, entry_type="starter", planned_qty=90)
        text = dpr._build_summary(self._decision(qty=30, last_bar_close=96.0, bar_fresh=True))
        assert "🌱 ✅" in text and "<b>60</b>" in text


class TestPositionChartRedraw:
    """The daily chart refresh for held tickers (2026-08-07). /playbook is its
    home because it already runs once a day AND already persists each
    position's freshly-trailed stop -- a stop that moved in the DB but not on
    the chart is exactly the drift this closes."""

    class _StubClient:
        """Stands in for TVClient. `fail_on` names a ticker whose draw blows up,
        so one bad symbol can be shown not to cost the others their redraw."""

        def __init__(self, fail_on=None):
            self.drawn = []
            self.fail_on = fail_on

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def prepare_chart(self, symbol, timeframe):
            if symbol == self.fail_on:
                raise RuntimeError("chart failed to load")
            self.drawn.append(symbol)

        async def draw_clear(self):
            pass

        async def draw_shape(self, *a, **kw):
            pass

    def _open(self, ticker, entry_price, stop):
        persistence.save_thesis(ticker=ticker, status="open_position", source="SCREENER_v3",
                                primary_setup={"type": "Breakout", "trigger": entry_price,
                                               "stop": stop, "atr_at_build": 5.0})
        persistence.create_position(
            ticker=ticker, entry_date="2026-08-04", entry_price=entry_price, qty=100,
            entry_type="full", entry_setup={"type": "Breakout", "stop": stop, "atr_at_build": 5.0},
            initial_stop=stop,
        )

    def test_draws_every_open_position(self, temp_db, monkeypatch):
        self._open("NOW", 115.68, 105.02)
        self._open("SCHW", 108.60, 105.36)
        stub = self._StubClient()
        monkeypatch.setattr(dpr, "TVClient", lambda: stub)

        drawn, failed = dpr.asyncio.run(dpr._redraw_all_position_charts())

        assert sorted(drawn) == ["NOW", "SCHW"] and failed == []
        assert sorted(stub.drawn) == ["NOW", "SCHW"]

    def test_one_bad_symbol_does_not_cost_the_others_their_redraw(self, temp_db, monkeypatch):
        self._open("NOW", 115.68, 105.02)
        self._open("SCHW", 108.60, 105.36)
        stub = self._StubClient(fail_on="NOW")
        monkeypatch.setattr(dpr, "TVClient", lambda: stub)

        drawn, failed = dpr.asyncio.run(dpr._redraw_all_position_charts())

        assert drawn == ["SCHW"] and failed == ["NOW"]

    def test_draws_the_stored_trailed_stop_not_the_entry_stop(self, temp_db, monkeypatch):
        # The whole reason this lives in /playbook: the run just moved the stop.
        import chart_draw
        self._open("NOW", 115.68, 105.02)
        persistence.update_current_stop("NOW", 112.40)
        stub = self._StubClient()
        monkeypatch.setattr(dpr, "TVClient", lambda: stub)

        dpr.asyncio.run(dpr._redraw_all_position_charts())

        position = next(p for p in persistence.get_open_positions() if p["ticker"] == "NOW")
        stop = next(l for l in chart_draw._lines_for_position(position)
                    if l["text"].startswith("Stop"))
        assert stop["price"] == 112.40 and stop["text"] == "Stop (trailed)"

    def test_no_open_positions_is_a_clean_no_op(self, temp_db, monkeypatch):
        monkeypatch.setattr(dpr, "TVClient", lambda: (_ for _ in ()).throw(
            AssertionError("must not open a TradingView session with nothing to draw")))
        assert dpr.asyncio.run(dpr._redraw_all_position_charts()) == ([], [])
