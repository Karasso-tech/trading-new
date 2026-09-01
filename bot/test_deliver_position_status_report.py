"""Unit tests for deliver_position_status_report.py's delivery contract
(2026-07-16).

Found in review: this script (and the other deliver_*.py scripts) is exactly
where a bug means either the message silently never confirmed sent, or a
Telegram failure gets swallowed instead of surfaced -- yet had zero test
coverage. Mocks send_text only (the one external call this script makes) and
uses an isolated temp DB, same pattern as test_persistence.py's temp_db
fixture.
"""

import json
import sys

import pytest

import deliver_position_status_report as dpsr
import persistence


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(persistence, "DB_PATH", db_path)
    persistence.init_db()
    return db_path


def _decision_file(tmp_path, update_id, results=None, run_label="midday"):
    decision = {"update_id": update_id, "date": "2026-07-16", "run_label": run_label,
                "results": results if results is not None else []}
    path = tmp_path / "_decision_position_status.json"
    path.write_text(json.dumps(decision), encoding="utf-8")
    return path


def _run_main(path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["deliver_position_status_report.py", str(path)])
    try:
        dpsr.main()
    except SystemExit as e:
        return e.code
    return 0


def _message_status(update_id):
    with persistence._db() as conn:
        row = conn.execute("SELECT status FROM messages WHERE update_id=?", (update_id,)).fetchone()
    return row["status"] if row else None


class TestSuccessfulDelivery:
    def test_successful_send_marks_sent(self, temp_db, tmp_path, monkeypatch):
        update_id = 111001
        persistence.enqueue_message(update_id=update_id, from_id="scheduled", chat_id="scheduled",
                                     message_type="text", message_text="/positions", raw_update={})
        monkeypatch.setattr(dpsr, "send_text", lambda *a, **k: {"ok": True, "result": {"message_id": 1}})

        path = _decision_file(tmp_path, update_id, results=[
            {"ticker": "NVDA", "headline": "Hold, no change."},
        ])
        exit_code = _run_main(path, monkeypatch)

        assert exit_code == 0
        assert _message_status(update_id) == "sent"

    def test_empty_results_still_sends_a_message_and_marks_sent(self, temp_db, tmp_path, monkeypatch):
        update_id = 111002
        persistence.enqueue_message(update_id=update_id, from_id="scheduled", chat_id="scheduled",
                                     message_type="text", message_text="/positions", raw_update={})
        sent_texts = []
        monkeypatch.setattr(dpsr, "send_text", lambda text, *a, **k: (sent_texts.append(text),
                                                                       {"ok": True, "result": {"message_id": 2}})[1])

        path = _decision_file(tmp_path, update_id, results=[])
        exit_code = _run_main(path, monkeypatch)

        assert exit_code == 0
        assert _message_status(update_id) == "sent"
        assert "אין פוזיציות פתוחות" in sent_texts[0]


class TestTheLineIsBuiltFromTheRecordedPosition:
    """Since 2026-08-10 the per-position line is built here, from what the
    database records, rather than copied out of the model's `headline`."""

    ENTRY_SETUP = {"type": "Breakout", "trigger": 95.0, "stop": 88.0, "atr_at_build": 4.0,
                   "targets": [{"price": 130.0, "pct": 40.0, "qty": 20},
                               {"price": 150.0, "pct": 35.0, "qty": 17}]}

    @pytest.fixture
    def sent(self, monkeypatch):
        texts = []
        monkeypatch.setattr(dpsr, "send_text",
                            lambda text, *a, **k: (texts.append(text),
                                                   {"ok": True, "result": {"message_id": 9}})[1])
        return texts

    def _open_pltr(self, *, entry_type="full", qty=50, entry_price=90.0, stop=88.0):
        persistence.save_thesis(ticker="PLTR", status="pending", source="SCREENER_v3",
                                 primary_setup=self.ENTRY_SETUP, planned_qty=90)
        persistence.create_position(ticker="PLTR", entry_date="2026-07-10",
                                     entry_price=entry_price, qty=qty, entry_type=entry_type,
                                     entry_setup=self.ENTRY_SETUP, initial_stop=stop)

    def _run(self, tmp_path, monkeypatch, result, update_id=112001):
        persistence.enqueue_message(update_id=update_id, from_id="scheduled", chat_id="scheduled",
                                     message_type="text", message_text="/positions", raw_update={})
        path = _decision_file(tmp_path, update_id, results=[result])
        return _run_main(path, monkeypatch)

    def test_the_models_own_headline_is_ignored(self, temp_db, tmp_path, sent, monkeypatch):
        self._open_pltr()
        self._run(tmp_path, monkeypatch,
                  {"ticker": "PLTR", "action": "Hold", "price": 100.0, "atr14": 4.0,
                   "stop": 88.0, "entry_date": "2026-07-10",
                   "headline": "🟠 PLTR — משהו אחר לגמרי"})
        assert "משהו אחר לגמרי" not in sent[0]
        assert "✅ <b>PLTR</b> — מחזיקים כרגיל" in sent[0]

    def test_the_profit_is_computed_from_the_recorded_fill(self, temp_db, tmp_path,
                                                            sent, monkeypatch):
        self._open_pltr(entry_price=90.0, qty=50)
        self._run(tmp_path, monkeypatch,
                  {"ticker": "PLTR", "action": "Hold", "price": 100.0, "atr14": 4.0,
                   "stop": 88.0, "entry_date": "2026-07-10"})
        assert "<b>11.11</b>% ($500.00)" in sent[0]

    def test_the_stop_shown_is_the_one_the_system_holds(self, temp_db, tmp_path,
                                                         sent, monkeypatch):
        self._open_pltr(stop=88.0)
        persistence.update_current_stop("PLTR", 97.0)
        self._run(tmp_path, monkeypatch,
                  {"ticker": "PLTR", "action": "Hold", "price": 100.0, "atr14": 4.0,
                   "stop": 55.55, "entry_date": "2026-07-10"})    # a stale retyped stop
        # Price 100 against the real stop of 97: $3.00 away, not $44.45.
        assert "מרחק לסטופ: $3.00" in sent[0]
        assert "44.45" not in sent[0]

    def test_a_sold_target_is_never_called_the_approaching_one(self, temp_db, tmp_path,
                                                                sent, monkeypatch):
        # The ASTS incident, in one test: target 1 is already realised, so the
        # next target is 150.00 and price sitting at 130 is not "approaching".
        self._open_pltr()
        persistence.record_exit(ticker="PLTR", exit_date="2026-08-01", exit_price=130.0,
                                 exit_qty=20, source="exit_command")
        self._run(tmp_path, monkeypatch,
                  {"ticker": "PLTR", "action": "Hold", "price": 130.0, "atr14": 4.0,
                   "stop": 100.0, "entry_date": "2026-07-10"})
        assert "מתקרב ליעד (<b>130.00</b>)" not in sent[0]

    def test_a_fresh_position_names_its_first_target(self, temp_db, tmp_path,
                                                      sent, monkeypatch):
        self._open_pltr()
        self._run(tmp_path, monkeypatch,
                  {"ticker": "PLTR", "action": "Hold", "price": 100.0, "atr14": 4.0,
                   "stop": 88.0, "entry_date": "2026-07-10"})
        assert "🎯 היעד הבא: <b>130.00</b> — מוכרים שם 20 יחידות" in sent[0]

    def test_a_sold_target_is_reported_as_sold_with_the_next_one(self, temp_db, tmp_path,
                                                                  sent, monkeypatch):
        # The owner's GLD question (2026-08-12): target 1 was realised the day
        # before, and the pulse check never said so.
        self._open_pltr()
        persistence.record_exit(ticker="PLTR", exit_date="2026-08-01", exit_price=130.0,
                                 exit_qty=20, source="exit_command")
        self._run(tmp_path, monkeypatch,
                  {"ticker": "PLTR", "action": "Hold", "price": 135.0, "atr14": 4.0,
                   "stop": 100.0, "entry_date": "2026-07-10"})
        assert "יעד 1 כבר מומש · 🎯 היעד הבא: <b>150.00</b>" in sent[0]

    def test_the_starter_line_appears_only_for_a_starter(self, temp_db, tmp_path,
                                                          sent, monkeypatch):
        self._open_pltr(entry_type="starter", qty=30)
        self._run(tmp_path, monkeypatch,
                  {"ticker": "PLTR", "action": "Hold", "price": 100.0, "atr14": 4.0,
                   "stop": 88.0, "entry_date": "2026-07-10",
                   "last_bar_close": 99.0, "bar_fresh": True})
        # 90 planned minus 30 held = 60 more to reach the thesis's full size.
        assert "🌱 ✅" in sent[0] and "<b>60</b>" in sent[0]

    def test_the_deterministic_stop_distance_line_is_still_appended(self, temp_db, tmp_path,
                                                                     sent, monkeypatch):
        self._open_pltr()
        self._run(tmp_path, monkeypatch,
                  {"ticker": "PLTR", "action": "Hold", "price": 100.0, "atr14": 4.0,
                   "stop": 88.0, "entry_date": "2026-07-10"})
        assert "מרחק לסטופ: $12.00" in sent[0]
        assert "ימי מסחר בפוזיציה" in sent[0]

    def test_a_position_this_system_never_filled_still_gets_a_line(self, temp_db, tmp_path,
                                                                    sent, monkeypatch):
        exit_code = self._run(tmp_path, monkeypatch,
                              {"ticker": "GOOGL", "action": "Hold", "price": 100.0,
                               "atr14": 4.0, "stop": 88.0, "entry_date": "2026-07-10"})
        assert exit_code == 0
        assert "<b>GOOGL</b>" in sent[0]
        assert "רווח/הפסד: <b>—</b>" in sent[0]


class TestFailedDelivery:
    def test_failed_send_marks_failed_never_marks_sent(self, temp_db, tmp_path, monkeypatch):
        update_id = 111003
        persistence.enqueue_message(update_id=update_id, from_id="scheduled", chat_id="scheduled",
                                     message_type="text", message_text="/positions", raw_update={})
        monkeypatch.setattr(dpsr, "send_text", lambda *a, **k: {"ok": False})

        path = _decision_file(tmp_path, update_id, results=[{"ticker": "NVDA", "headline": "Hold."}])
        exit_code = _run_main(path, monkeypatch)

        assert exit_code == 1
        assert _message_status(update_id) == "failed"

    def test_a_result_missing_its_ticker_marks_failed_not_a_crash(self, temp_db, tmp_path, monkeypatch):
        # Since 2026-08-10 the line is built here, so a missing `headline` is no
        # longer a failure -- there is nothing to copy. A missing ticker still
        # is: there is nothing to build the line from.
        update_id = 111004
        persistence.enqueue_message(update_id=update_id, from_id="scheduled", chat_id="scheduled",
                                     message_type="text", message_text="/positions", raw_update={})
        monkeypatch.setattr(dpsr, "send_text", lambda *a, **k: {"ok": True, "result": {"message_id": 3}})

        path = _decision_file(tmp_path, update_id, results=[{"action": "Hold", "price": 24.5}])
        exit_code = _run_main(path, monkeypatch)

        assert exit_code == 1
        assert _message_status(update_id) == "failed"
