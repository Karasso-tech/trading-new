"""Unit tests for deliver_auto_monitor_report.py's message building
(2026-08-10).

The scan line used to be copied straight out of the model's `headline` field.
It is now built here from the payload's numbers plus the stored thesis, so
these tests are about the wiring: which facts reach the template, that the
model's own copy of the message is ignored, and that a Starter quantity comes
from the stored plan rather than from the scan.

Mocks send_text only, and uses an isolated temp DB -- same pattern as
test_deliver_position_status_report.py.
"""

import json
import sys

import pytest

import deliver_auto_monitor_report as damr
import persistence

PRIMARY = {"type": "Breakout", "trigger": 165.08, "stop": 159.44,
           "targets": [{"price": 190.0, "pct": 40.0, "rr": 4.4}]}


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(persistence, "DB_PATH", db_path)
    persistence.init_db()
    return db_path


@pytest.fixture
def sent(monkeypatch):
    """Captures the one consolidated message instead of sending it."""
    messages = []

    def _fake_send(text, *a, **k):
        messages.append(text)
        return {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr(damr, "send_text", _fake_send)
    return messages


def _save_pltr(planned_qty=275, grade="B"):
    persistence.save_thesis(ticker="PLTR", status="pending", source="SCREENER_v3",
                             primary_setup=PRIMARY, rubric_grade=grade,
                             planned_qty=planned_qty, decision="Watchlist")


def _run(tmp_path, monkeypatch, results, update_id=990001):
    persistence.enqueue_message(update_id=update_id, from_id="scheduled", chat_id="scheduled",
                                message_type="text", message_text="/monitorall", raw_update={})
    path = tmp_path / "_automonitor.json"
    path.write_text(json.dumps({"update_id": update_id, "date": "2026-08-10",
                                 "results": results}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["deliver_auto_monitor_report.py", str(path)])
    try:
        damr.main()
    except SystemExit as e:
        return e.code
    return 0


GREEN_RESULT = {
    "ticker": "PLTR", "status": "green", "price": 172.01, "distance_atr": 1.1,
    "note": "trigger confirmed", "sentence": "המניה פרצה וסגרה מעל הרמה.",
    "setup_used": "primary",
    "order": {"type": "Breakout", "price": 165.08, "stop": 159.44, "qty": 275},
    "rubric_blocked": False, "rubric_grade_formula_now": "B",
    "rubric_formula_now": {"primary": {"grade": "B", "score": 4, "criteria": {}},
                            "alternate": None},
}


class TestTheMessageIsBuiltNotCopied:
    def test_the_models_own_headline_is_ignored(self, temp_db, tmp_path, sent, monkeypatch):
        _save_pltr()
        payload = dict(GREEN_RESULT, headline="🟠 PLTR — משהו אחר לגמרי")
        assert _run(tmp_path, monkeypatch, [payload]) == 0
        assert "משהו אחר לגמרי" not in sent[0]
        assert "🟢 <b>PLTR</b> — הטריגר הופעל!" in sent[0]

    def test_the_order_numbers_come_through_with_the_cost_computed(self, temp_db, tmp_path,
                                                                    sent, monkeypatch):
        _save_pltr()
        _run(tmp_path, monkeypatch, [GREEN_RESULT])
        assert "<b>275 מניות</b>" in sent[0]
        assert "($45,397.00)" in sent[0]

    def test_the_stored_decision_sign_still_leads_the_line(self, temp_db, tmp_path,
                                                            sent, monkeypatch):
        _save_pltr()
        _run(tmp_path, monkeypatch, [GREEN_RESULT])
        # Both halves: what the plan decided, and what price has since done.
        assert "רשימת מעקב" in sent[0] and "הטריגר כבר הופעל" in sent[0]

    def test_the_sentence_is_the_only_prose_taken_from_the_model(self, temp_db, tmp_path,
                                                                  sent, monkeypatch):
        _save_pltr()
        _run(tmp_path, monkeypatch, [GREEN_RESULT])
        assert "💬 המניה פרצה וסגרה מעל הרמה." in sent[0]


class TestFactsComeFromTheStoredThesis:
    def test_the_starter_quantity_is_thirty_percent_of_the_stored_plan(self, temp_db, tmp_path,
                                                                        sent, monkeypatch):
        _save_pltr(planned_qty=200)
        _run(tmp_path, monkeypatch, [dict(GREEN_RESULT, status="yellow_plus", order=None,
                                           rubric_grade_formula_now="C",
                                           rubric_formula_now={"primary": {"grade": "C",
                                                                            "criteria": {}}})])
        assert "<b>60 מניות</b>" in sent[0]

    def test_an_older_thesis_without_a_plan_drops_the_starter_line(self, temp_db, tmp_path,
                                                                    sent, monkeypatch):
        _save_pltr(planned_qty=None)
        _run(tmp_path, monkeypatch, [dict(GREEN_RESULT, status="yellow_plus", order=None,
                                           rubric_grade_formula_now="C",
                                           rubric_formula_now={"primary": {"grade": "C",
                                                                            "criteria": {}}})])
        assert "Starter" not in sent[0]

    def test_the_trigger_shown_is_the_stored_one_not_a_reported_one(self, temp_db, tmp_path,
                                                                     sent, monkeypatch):
        _save_pltr()
        _run(tmp_path, monkeypatch, [dict(GREEN_RESULT, status="yellow_plus", order=None,
                                           rubric_grade_formula_now="C",
                                           rubric_formula_now={"primary": {"grade": "C",
                                                                            "criteria": {}}})])
        assert "<b>165.08</b>" in sent[0]

    def test_a_check_on_the_alternate_shows_the_alternate_trigger(self, temp_db, tmp_path,
                                                                   sent, monkeypatch):
        # HOOD, 2026-08-07: the scan confirmed the Alternate at 89.00 while the
        # Primary's own trigger sat at 95.60. A Primary-only read prints the
        # wrong level right next to the right price.
        persistence.save_thesis(ticker="PLTR", status="pending", source="SCREENER_v3",
                                 primary_setup=PRIMARY,
                                 alternate_setup={"type": "Failed Breakdown", "trigger": 150.0,
                                                  "stop": 144.0,
                                                  "targets": [{"price": 175.0, "pct": 40.0}]},
                                 rubric_grade="B", planned_qty=275)
        _run(tmp_path, monkeypatch, [dict(GREEN_RESULT, status="yellow_plus", order=None,
                                           setup_used="alternate",
                                           rubric_formula_now={"alternate": {"grade": "C",
                                                                              "criteria": {}}})])
        assert "<b>150.00</b>" in sent[0]
        assert "165.08" not in sent[0]


WEAKENED = dict(GREEN_RESULT, rubric_blocked=True, rubric_grade_formula_now="D",
                rubric_formula_now={"primary": {"grade": "D", "score": 2,
                                                 "criteria": {"rr": False, "rs": True}}})
UNGRADEABLE = dict(GREEN_RESULT, rubric_grade_formula_now=None,
                   rubric_formula_now={"primary": {"grade": None,
                                                    "reason": "no_target_to_score"}})


class TestATriggerWithNoOrderIsNotShown:
    """2026-08-11, the user's call: a fired trigger that produces nothing to
    place is not worth a five-line block twice a day. It is named once at the
    bottom instead -- named, not merely counted, so a ticker going quiet never
    reads as a ticker that disappeared."""

    def test_a_weakened_setup_gets_no_block(self, temp_db, tmp_path, sent, monkeypatch):
        _save_pltr(grade="A")
        _run(tmp_path, monkeypatch, [WEAKENED])
        assert "הסטאפ נחלש" not in sent[0]
        assert "יחס סיכון/סיכוי נמוך" not in sent[0]

    def test_one_that_cannot_be_scored_gets_no_block(self, temp_db, tmp_path, sent, monkeypatch):
        _save_pltr()
        _run(tmp_path, monkeypatch, [UNGRADEABLE])
        assert "אי אפשר לחשב ציון" not in sent[0]

    def test_a_trigger_that_fired_days_ago_gets_no_block(self, temp_db, tmp_path,
                                                          sent, monkeypatch):
        _save_pltr()
        monkeypatch.setattr(damr.persistence, "get_trigger_fired_age",
                            lambda ticker: {"stale": True, "trading_days": 9})
        _run(tmp_path, monkeypatch, [GREEN_RESULT])
        assert "ימי מסחר" not in sent[0]
        assert "PLTR" in sent[0]

    def test_the_hidden_ones_are_named_in_one_line_at_the_bottom(self, temp_db, tmp_path,
                                                                  sent, monkeypatch):
        _save_pltr(grade="A")
        _run(tmp_path, monkeypatch, [WEAKENED])
        assert "אין מהם פקודה (1)" in sent[0]
        assert "PLTR" in sent[0]
        assert "/monitor PLTR" in sent[0]

    def test_a_scan_where_everything_fired_but_nothing_is_placeable_says_so(
            self, temp_db, tmp_path, sent, monkeypatch):
        # "no trigger is active" would be a lie here -- one fired, it just gave
        # nothing to place.
        _save_pltr(grade="A")
        _run(tmp_path, monkeypatch, [WEAKENED])
        assert "אין פקודה פעילה כרגע" in sent[0]
        assert "שום טריגר לא פעיל" not in sent[0]

    def test_a_real_order_is_still_shown_and_adds_no_bottom_line(self, temp_db, tmp_path,
                                                                  sent, monkeypatch):
        _save_pltr()
        _run(tmp_path, monkeypatch, [GREEN_RESULT])
        assert "🟢 <b>PLTR</b> — הטריגר הופעל!" in sent[0]
        assert "אין מהם פקודה" not in sent[0]

    def test_a_hidden_ticker_is_still_logged_and_still_linted(self, temp_db, tmp_path,
                                                               sent, monkeypatch):
        # Hiding is a display choice only. The check itself, its monitor_log
        # row and its lint verdict all still happen.
        _save_pltr(grade="A")
        _run(tmp_path, monkeypatch, [WEAKENED])
        with persistence._db() as conn:
            rows = conn.execute("SELECT status FROM monitor_log WHERE ticker='PLTR'").fetchall()
        assert [r["status"] for r in rows] == ["green"]
        assert persistence.get_thesis("PLTR")["status"] == "pending"

    def test_a_failed_verification_on_a_hidden_ticker_still_shouts(self, temp_db, tmp_path,
                                                                    sent, monkeypatch):
        # The one thing that must never be hidden with the block: a field the
        # scan reported that the lint could not confirm.
        _save_pltr(grade="A")

        class _Bad:
            ok = False

            def to_dict(self):
                return {"ok": False}

            def warning_lines_he(self):
                return ["בדיקה נכשלה בכוונה"]

        monkeypatch.setattr(damr.report_lint, "lint_monitor_decision",
                            lambda *a, **k: _Bad())
        _run(tmp_path, monkeypatch, [WEAKENED])
        assert "בדיקה נכשלה בכוונה" in sent[0]


class TestScanBookkeepingStillHappens:
    def test_every_check_is_logged_and_red_closes_the_thesis(self, temp_db, tmp_path,
                                                              sent, monkeypatch):
        _save_pltr()
        _run(tmp_path, monkeypatch, [dict(GREEN_RESULT, status="red", order=None)])
        assert persistence.get_thesis("PLTR")["status"] == "closed"
        with persistence._db() as conn:
            rows = conn.execute("SELECT status FROM monitor_log WHERE ticker='PLTR'").fetchall()
        assert [r["status"] for r in rows] == ["red"]

    def test_a_scan_with_nothing_active_says_so(self, temp_db, tmp_path, sent, monkeypatch):
        _save_pltr()
        _run(tmp_path, monkeypatch, [dict(GREEN_RESULT, status="white", order=None)])
        assert "שום טריגר לא פעיל כרגע" in sent[0]

    def test_an_unknown_status_fails_the_run(self, temp_db, tmp_path, sent, monkeypatch):
        _save_pltr()
        monkeypatch.setattr(damr, "send_failure_alert", lambda *a, **k: None)
        assert _run(tmp_path, monkeypatch, [dict(GREEN_RESULT, status="purple")]) == 1
