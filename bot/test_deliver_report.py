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
        "decision": "Buy Now", "grade": "A",
        # The numbers behind the letter, consistent with the setup below:
        # rr 2.5 >= 2.3, target 5.0x ATR, RS positive, not extended, earnings
        # far out -> 5/5 -> A. Rule 27's recompute is a gate, so a report that
        # does not disclose these cannot carry an order at all.
        "rubric_inputs": {"rr": 2.5, "target_atr_multiple": 5.0,
                           "regime": "healthy_uptrend", "rs_delta_pct": 4.0,
                           "dist_sma20_atr": 0.4, "earnings_days_out": 45},
        # A real qualifying target, not an empty list: trigger 260 / stop 250 /
        # ATR 5.0, target 285 -> 5.00x ATR and 2.50 R:R, so it passes rule 3's
        # gate. This used to be `"targets": []`, which meant the fixture was a
        # "Buy Now" with nowhere to sell -- a combination rule 29 forbids, and
        # which enforce_decision_ceiling now (correctly) rewrites to No Trade.
        "primary_setup": {
            "type": "Reclaim", "trigger": "260.00", "stop": 250.00, "atr_at_build": 5.0,
            "targets": [{"price": 285.0, "pct": "40%", "atr_mult": "5.00x", "rr": "2.50",
                          "status": "pass"}],
            "checkpoints": [],
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


class TestTheOrderIsBlocked:
    """CONSISTENCY_RULES.md rules 27/29, enforced 2026-08-30.

    report_lint has always WARNED that a Buy alongside a blocking grade is not
    allowed, and the message went out anyway -- decision word, share count and
    red warning all in the same Telegram bubble. These prove the order is
    actually removed now, not merely objected to.
    """

    def _send(self, monkeypatch, captured):
        monkeypatch.setattr(deliver_report, "send_photo", lambda *a, **k: _ok(1))
        monkeypatch.setattr(deliver_report, "send_document", lambda *a, **k: _ok(2))

        def _text(body, *a, **k):
            captured.append(body)
            return _ok(3)

        monkeypatch.setattr(deliver_report, "send_text", _text)

    def _deliver(self, tmp_path, monkeypatch, update_id, mutate, ticker="CRM"):
        persistence.enqueue_message(update_id=update_id, from_id="test", chat_id="test",
                                     message_type="text", message_text="/screener " + ticker,
                                     raw_update={})
        path = _decision_file(tmp_path, update_id, ticker=ticker)
        decision = json.loads(path.read_text(encoding="utf-8"))
        mutate(decision)
        path.write_text(json.dumps(decision), encoding="utf-8")
        captured = []
        self._send(monkeypatch, captured)
        assert _run_main(path, monkeypatch) == 0
        return persistence.get_thesis(ticker), captured

    def test_grade_d_cannot_ship_a_buy_or_a_quantity(self, temp_db, tmp_path, monkeypatch):
        def mutate(d):
            # The numbers really do compute to D (0 of 5), not just the label --
            # the gate reads the recomputed letter, so relabelling alone would
            # be testing the label rather than the gate.
            d["grade"] = "D"
            d["rubric_inputs"] = {"rr": 1.0, "target_atr_multiple": 0.8,
                                   "regime": "risk_off", "rs_delta_pct": -2.0,
                                   "dist_sma20_atr": 3.0, "earnings_days_out": 3}
            d["sizing"] = {"entry": 260.0, "stop": 250.0, "qty": 40,
                            "risk_usd_target": 400.0, "multipliers": {}}

        thesis, captured = self._deliver(tmp_path, monkeypatch, 222101, mutate)
        assert thesis["decision"] == "Watchlist"
        assert thesis["rubric_grade"] == "D"
        assert thesis["planned_qty"] is None
        body = captured[0]
        # The 🚫 line leads the message and names what was lowered. It quotes the
        # original word on purpose -- "the decision 'Buy Now' was lowered" is the
        # explanation, so a bare "Buy Now" not in body would be the wrong test.
        assert body.startswith("🚫")
        # No order survives: rule 28's size block is the only place a share count
        # is ever printed, and it renders nothing without a sizing block.
        assert "בדיקת גודל" not in body
        assert "40" not in body.split("—")[0]
        # and it says out loud that this particular block has never been measured
        assert "לא נמדד" in body

    def test_a_report_with_no_grade_at_all_is_treated_the_same_way(
        self, temp_db, tmp_path, monkeypatch
    ):
        # The real _runs/_decision_NVDA.json shape: neither field name present.
        # It was delivered, and nothing anywhere stopped it.
        def mutate(d):
            d.pop("grade", None)
            d.pop("rubric_grade", None)
            d["sizing"] = {"entry": 260.0, "stop": 250.0, "qty": 40,
                            "risk_usd_target": 400.0, "multipliers": {}}

        thesis, captured = self._deliver(tmp_path, monkeypatch, 222102, mutate)
        assert thesis["decision"] == "Watchlist"
        assert thesis["planned_qty"] is None
        body = captured[0]
        assert body.startswith("🚫")
        assert "בדיקת גודל" not in body

    def test_the_grade_is_stored_under_either_field_name(self, temp_db, tmp_path, monkeypatch):
        # build_plan.py emits rubric_grade; this line used to read only `grade`,
        # so a perfectly good letter was stored as NULL.
        def mutate(d):
            d.pop("grade", None)
            d["rubric_grade"] = "A"

        thesis, _ = self._deliver(tmp_path, monkeypatch, 222103, mutate)
        assert thesis["rubric_grade"] == "A"
        assert thesis["decision"] == "Buy Now"


    def test_a_clean_buy_keeps_its_planned_quantity(self, temp_db, tmp_path, monkeypatch):
        def mutate(d):
            d["sizing"] = {"entry": 260.0, "stop": 250.0, "qty": 40,
                            "risk_usd_target": 400.0, "multipliers": {}}

        thesis, _ = self._deliver(tmp_path, monkeypatch, 222104, mutate)
        assert thesis["decision"] == "Buy Now"
        assert thesis["planned_qty"] == 40


class TestTheRecomputedGradeIsTheGate:
    """CONSISTENCY_RULES.md rule 27, as a gate rather than a remark.

    The recompute shipped for one day as a lint finding, which meant a report
    claiming A while its own five numbers said D went out as an order with a
    warning beside it -- the exact shape that had just been removed from the
    grade gate itself.
    """

    REPORT = (
        "# TEST\n\n"
        "## ב. טבלת החלטה ראשית\n\n"
        "| סעיף | תשובה |\n"
        "|---|---|\n"
        "| החלטה | **Buy Now** |\n"
        "| דירוג סטאפ | **A** |\n\n"
        "## ג. טבלת שני סטאפים\n\n"
        "כאן הסטאפים.\n\n"
        "## ד. גודל פוזיציה\n\n"
        "| סיכון $ | כמות מלאה | שווי פוזיציה |\n"
        "|---|---|---|\n"
        "| $400 | 40 מניות | $10,400 |\n\n"
        "**הזמנה:** קנה 40 מניות בגבול 260.00, סטופ 250.00.\n\n"
        "## ה. ניתוח טכני\n\n"
        "כאן הניתוח, והוא נשאר.\n"
    )

    A_GRADE_INPUTS = {"rr": 2.5, "target_atr_multiple": 5.0,
                       "regime": "healthy_uptrend", "rs_delta_pct": 4.0,
                       "dist_sma20_atr": 0.4, "earnings_days_out": 45}
    D_GRADE_INPUTS = {"rr": 1.0, "target_atr_multiple": 0.8,
                       "regime": "risk_off", "rs_delta_pct": -2.0,
                       "dist_sma20_atr": 3.0, "earnings_days_out": 3}

    def _deliver(self, tmp_path, monkeypatch, update_id, mutate, ticker="CRM",
                  report=None):
        persistence.enqueue_message(update_id=update_id, from_id="test", chat_id="test",
                                     message_type="text", message_text="/screener " + ticker,
                                     raw_update={})
        path = _decision_file(tmp_path, update_id, ticker=ticker)
        decision = json.loads(path.read_text(encoding="utf-8"))
        decision["report_markdown"] = report if report is not None else self.REPORT
        decision["sizing"] = {"entry": 260.0, "stop": 250.0, "qty": 40,
                               "risk_usd_target": 400.0, "multipliers": {}}
        mutate(decision)
        path.write_text(json.dumps(decision), encoding="utf-8")

        sent = {"markdown": None, "caption": None, "summary": None, "documents": []}
        monkeypatch.setattr(deliver_report, "send_photo",
                             lambda png, caption="", *a, **k: (sent.__setitem__("caption", caption),
                                                                _ok(1))[1])
        monkeypatch.setattr(deliver_report, "send_document",
                             lambda *a, **k: (sent["documents"].append(k.get("filename")),
                                              _ok(2))[1])
        monkeypatch.setattr(deliver_report, "send_text",
                             lambda body, *a, **k: (sent.__setitem__("summary", body), _ok(3))[1])

        def _pdf(markdown):
            sent["markdown"] = markdown
            return b"%PDF-fake"

        monkeypatch.setattr(deliver_report, "render_report_pdf", _pdf)
        assert _run_main(path, monkeypatch) == 0
        self.sent = sent
        saved = sorted((deliver_report.PROJECT_ROOT / "reports").glob(f"{ticker.lower()}_screener_*_{update_id}.md"))
        sent["saved_markdown"] = saved[0].read_text(encoding="utf-8") if saved else None
        return persistence.get_thesis(ticker), sent["markdown"]

    def test_says_A_but_the_numbers_say_D_is_blocked(self, temp_db, tmp_path, monkeypatch):
        # The letter claims A. The five disclosed numbers compute to D. Nothing
        # about the report otherwise looks wrong, which is the whole danger.
        def mutate(d):
            d["grade"] = "A"
            d["rubric_inputs"] = self.D_GRADE_INPUTS

        thesis, markdown = self._deliver(tmp_path, monkeypatch, 222201, mutate)
        assert thesis["decision"] == "Watchlist"
        assert thesis["planned_qty"] is None
        # stored as what the arithmetic says, not as what the report claimed
        assert thesis["rubric_grade"] == "D"
        assert "Buy Now" not in markdown

    def test_missing_rubric_inputs_is_blocked(self, temp_db, tmp_path, monkeypatch):
        # An unverifiable quality claim is not a verified one. This is the real
        # _decision_NVDA.json class of report, and it used to ship an order.
        def mutate(d):
            d["grade"] = "A"
            d.pop("rubric_inputs", None)

        thesis, markdown = self._deliver(tmp_path, monkeypatch, 222202, mutate)
        assert thesis["decision"] == "Watchlist"
        assert thesis["planned_qty"] is None
        assert "Buy Now" not in markdown

    def test_malformed_rubric_inputs_is_blocked(self, temp_db, tmp_path, monkeypatch):
        def mutate(d):
            d["grade"] = "A"
            d["rubric_inputs"] = {"rr": "not a number", "target_atr_multiple": 5.0}

        thesis, _ = self._deliver(tmp_path, monkeypatch, 222203, mutate)
        assert thesis["decision"] == "Watchlist"
        assert thesis["planned_qty"] is None

    def test_the_two_grade_fields_disagreeing_is_blocked(self, temp_db, tmp_path, monkeypatch):
        def mutate(d):
            d["grade"] = "A"
            d["rubric_grade"] = "C"
            d["rubric_inputs"] = self.A_GRADE_INPUTS

        thesis, _ = self._deliver(tmp_path, monkeypatch, 222204, mutate)
        assert thesis["decision"] == "Watchlist"
        assert thesis["planned_qty"] is None

    def test_a_blocked_pdf_carries_no_quantity_and_no_order(
        self, temp_db, tmp_path, monkeypatch
    ):
        # The PDF is rendered from report_markdown, and clearing `sizing` never
        # touched it -- so a blocked idea used to ship a PDF containing the full
        # section D table while the message beside it said there was no order.
        def mutate(d):
            d["grade"] = "A"
            d["rubric_inputs"] = self.D_GRADE_INPUTS

        _, markdown = self._deliver(tmp_path, monkeypatch, 222205, mutate)

        assert "40 מניות" not in markdown
        assert "$10,400" not in markdown
        assert "קנה 40 מניות" not in markdown
        assert "Buy Now" not in markdown
        assert "Watchlist" in markdown
        # and the rest of the report survives -- this removes the order, not
        # the analysis
        assert "כאן הניתוח, והוא נשאר." in markdown
        assert "## ג. טבלת שני סטאפים" in markdown

    def test_the_recomputed_grade_replaces_the_claimed_one_in_every_display(
        self, temp_db, tmp_path, monkeypatch
    ):
        # Says A, computes D. The gate refused the order in the change before
        # this one -- and the caption, the summary line, the widget and the PDF's
        # own grade row all still printed A, which reads as the system being
        # wrong rather than the report.
        def mutate(d):
            d["grade"] = "A"
            d["rubric_inputs"] = self.D_GRADE_INPUTS

        thesis, _ = self._deliver(tmp_path, monkeypatch, 222301, mutate)
        sent = self.sent

        assert thesis["rubric_grade"] == "D"                     # stored
        assert "Grade D" in sent["caption"]                       # photo caption
        assert "Grade A" not in sent["caption"]
        assert "\u2b50 \u05e6\u05d9\u05d5\u05df: <b>D</b>" in sent["summary"]  # telegram summary line
        assert "| **D** (מחושב מהמספרים) |" in sent["saved_markdown"]  # PDF/markdown row
        assert "| דירוג סטאפ | **A** |" not in sent["saved_markdown"]
        # and the note at the head names both numbers rather than asserting one
        assert "הציון הכתוב בדוח הוא A" in sent["saved_markdown"]
        assert "הוא D, וזה הציון הקובע" in sent["saved_markdown"]

    def test_a_watchlist_report_still_gets_its_grade_corrected(
        self, temp_db, tmp_path, monkeypatch
    ):
        """Found by a live run against the real AMZN report, not by a test.

        Correcting the grade used to happen inside the order block, and the
        block only fires when the stated decision claims MORE than the facts
        permit. A report already sitting at Watchlist claims nothing extra, so
        nothing ran -- and a Watchlist whose body said "A" while its own numbers
        computed to D went out with the caption and the summary saying D and the
        PDF still saying A. Three surfaces, two answers, and the document is the
        one that gets kept.
        """
        def mutate(d):
            d["decision"] = "Watchlist"          # already at the ceiling
            d["grade"] = "A"
            d["rubric_inputs"] = self.D_GRADE_INPUTS
            d["sizing"] = None                    # a Watchlist carries no order

        thesis, _ = self._deliver(tmp_path, monkeypatch, 222701, mutate)
        sent = self.sent

        assert thesis["rubric_grade"] == "D"
        assert "Grade D" in sent["caption"]
        assert "| **D** (מחושב מהמספרים) |" in sent["saved_markdown"]
        assert "| דירוג סטאפ | **A** |" not in sent["saved_markdown"]
        assert "הציון הכתוב בדוח הוא A" in sent["saved_markdown"]
        # nothing was blocked here -- there was no order to block
        assert "ההחלטה הורדה" not in sent["summary"]
        assert sent["documents"] != []

    def test_a_report_whose_letter_matches_its_numbers_is_not_touched(
        self, temp_db, tmp_path, monkeypatch
    ):
        def mutate(d):
            d["decision"] = "Watchlist"
            d["grade"] = "A"
            d["rubric_inputs"] = self.A_GRADE_INPUTS
            d["sizing"] = None

        _, _ = self._deliver(tmp_path, monkeypatch, 222702, mutate)
        assert self.sent["saved_markdown"] == self.REPORT
        assert "הציון תוקן" not in self.sent["summary"]

    def test_a_blocked_report_with_an_unrecognised_sizing_heading_withholds_the_pdf(
        self, temp_db, tmp_path, monkeypatch
    ):
        # The redaction is anchored on SCREENER_v3's own section heading, which
        # is the right anchor and not a guarantee -- report_markdown is written
        # by a model. "The regex did not match" and "there was no order" produce
        # the same empty result and mean opposite things, so a body that cannot
        # be PROVEN clean is not sent.
        odd = self.REPORT.replace("## ד. גודל פוזיציה", "## ד. כמה לקנות")

        def mutate(d):
            d["grade"] = "A"
            d["rubric_inputs"] = self.D_GRADE_INPUTS

        thesis, _ = self._deliver(tmp_path, monkeypatch, 222302, mutate, report=odd)
        sent = self.sent

        assert thesis["decision"] == "Watchlist"
        assert sent["documents"] == []                    # no PDF went out
        assert "40 מניות" in sent["saved_markdown"]        # the local record keeps everything
        assert "הדוח המלא לא צורף" in sent["summary"]      # and the message says why
        assert "סעיף הגודל לא זוהה" in sent["summary"]

    def test_a_leftover_share_count_also_withholds_the_pdf(
        self, temp_db, tmp_path, monkeypatch
    ):
        # The heading matched and the section went, but the same share count is
        # quoted again further down. Finding the heading is not the same as
        # proving the number is gone.
        leaky = self.REPORT.replace(
            "כאן הניתוח, והוא נשאר.",
            "כאן הניתוח, והוא נשאר. התוכנית הייתה 40 מניות.")

        def mutate(d):
            d["grade"] = "A"
            d["rubric_inputs"] = self.D_GRADE_INPUTS

        _, _ = self._deliver(tmp_path, monkeypatch, 222303, mutate, report=leaky)
        assert self.sent["documents"] == []
        assert "עדיין מופיע בדוח ככמות מניות" in self.sent["summary"]

    @pytest.mark.parametrize("written", [
        "40",        # as the sizing block wrote it
        "40.0",      # the same count with a decimal tail
    ])
    def test_every_way_of_writing_forty_still_withholds_the_pdf(
        self, temp_db, tmp_path, monkeypatch, written, request
    ):
        # The first version of this check searched for the quantity as a STRING,
        # so a report that spelled the same number any other way passed a test
        # looking for text it never contained.
        leaky = self.REPORT.replace(
            "כאן הניתוח, והוא נשאר.",
            f"כאן הניתוח, והוא נשאר. התוכנית הייתה {written} מניות.")

        def mutate(d):
            d["grade"] = "A"
            d["rubric_inputs"] = self.D_GRADE_INPUTS

        uid = 222310 + abs(hash(written)) % 50
        self._deliver(tmp_path, monkeypatch, uid, mutate, report=leaky)
        assert self.sent["documents"] == []
        assert "עדיין מופיע בדוח ככמות מניות" in self.sent["summary"]

    @pytest.mark.parametrize("written", [
        "1000",       # plain
        "1,000",      # comma grouping
        "1 000",      # space grouping
        "1\u00a0000",  # non-breaking space grouping
        "1,000.0",    # grouped with a decimal tail
    ])
    def test_every_way_of_writing_a_thousand_still_withholds_the_pdf(
        self, temp_db, tmp_path, monkeypatch, written
    ):
        leaky = self.REPORT.replace(
            "כאן הניתוח, והוא נשאר.",
            f"כאן הניתוח, והוא נשאר. התוכנית הייתה {written} מניות.")

        def mutate(d):
            d["grade"] = "A"
            d["rubric_inputs"] = self.D_GRADE_INPUTS
            d["sizing"] = {"entry": 260.0, "stop": 250.0, "qty": 1000,
                            "risk_usd_target": 10000.0, "multipliers": {}}

        uid = 222360 + abs(hash(written)) % 50
        self._deliver(tmp_path, monkeypatch, uid, mutate, report=leaky)
        assert self.sent["documents"] == []

    @pytest.mark.parametrize("written,qty", [
        ("40%", 40),        # rule 7's own allocation
        ("40.0%", 40),      # the same, written with a decimal
        ("1,400", 40),      # 40 sitting inside a longer number
        ("$10,400", 40),    # and inside a dollar figure
        ("260.00", 40),     # a price, not a count
        ("1,000", 40),      # a different number entirely
    ])
    def test_numbers_that_are_not_the_share_count_still_let_the_pdf_through(
        self, temp_db, tmp_path, monkeypatch, written, qty
    ):
        # The check has to be tight as well as thorough: a version that
        # withheld on any digit would withhold every blocked report's PDF and
        # stop being read.
        body = self.REPORT.replace(
            "כאן הניתוח, והוא נשאר.",
            f"כאן הניתוח, והוא נשאר. המספר הזה הוא {written} ואינו כמות מניות.")

        def mutate(d):
            d["grade"] = "A"
            d["rubric_inputs"] = self.D_GRADE_INPUTS
            d["sizing"] = {"entry": 260.0, "stop": 250.0, "qty": qty,
                            "risk_usd_target": 400.0, "multipliers": {}}

        uid = 222410 + abs(hash(written)) % 50
        self._deliver(tmp_path, monkeypatch, uid, mutate, report=body)
        assert self.sent["documents"] != []

    @pytest.mark.parametrize("qty,line", [
        # Every one of these is a real line shape from the report template, and
        # every one collided with a plausible share count before the check was
        # narrowed. Measured across 40 real reports: a bare value match fired on
        # 39 of them for a 50-share position, 37 for 17 and 30 for 22.
        (50, "| SPY | 771.10 | מעל SMA20 (768.10), SMA50 (753.39), SMA150 (718.62) |"),
        (17, "## פוטנציאל תנועה (כלל 17, מידע בלבד — לא יעד מכירה)"),
        (22, "**שער נפח (כלל 22):** נפח יום הפריצה מדווח, לא משנה את הגודל."),
        (78, "| אירוע קרוב | הבא 27-10 (78 ימים) |"),
        (14, "**ATR14:** 6.09 — תנודתיות בינונית."),
        (20, "המחיר סגר מעל SMA20 שלושה ימים ברצף."),
    ])
    def test_a_number_that_is_not_about_shares_does_not_withhold_the_pdf(
        self, temp_db, tmp_path, monkeypatch, qty, line
    ):
        body = self.REPORT.replace("כאן הניתוח, והוא נשאר.",
                                    "כאן הניתוח, והוא נשאר.\n" + line)

        def mutate(d):
            d["grade"] = "A"
            d["rubric_inputs"] = self.D_GRADE_INPUTS
            d["sizing"] = {"entry": 260.0, "stop": 250.0, "qty": qty,
                            "risk_usd_target": 400.0, "multipliers": {}}

        uid = 222510 + qty
        self._deliver(tmp_path, monkeypatch, uid, mutate, report=body)
        assert self.sent["documents"] != [], f"withheld on: {line}"

    @pytest.mark.parametrize("line", [
        "התוכנית הייתה 50 מניות.",
        "| כמות מלאה | 50 |",
        "**הזמנה:** קנה 50 בגבול 260.00.",
        "קנייה של 50 יחידות בפתיחה.",
        "Buy 50 at the open.",
    ])
    def test_the_same_number_beside_a_quantity_or_an_order_still_withholds(
        self, temp_db, tmp_path, monkeypatch, line
    ):
        # The narrowing must not become a hole: 50 next to the moving average is
        # noise, 50 next to "מניות" or "קנה" is the thing being guarded against.
        body = self.REPORT.replace("כאן הניתוח, והוא נשאר.",
                                    "כאן הניתוח, והוא נשאר.\n" + line)

        def mutate(d):
            d["grade"] = "A"
            d["rubric_inputs"] = self.D_GRADE_INPUTS
            d["sizing"] = {"entry": 260.0, "stop": 250.0, "qty": 50,
                            "risk_usd_target": 400.0, "multipliers": {}}

        uid = 222610 + abs(hash(line)) % 60
        self._deliver(tmp_path, monkeypatch, uid, mutate, report=body)
        assert self.sent["documents"] == [], f"let through: {line}"

    def test_a_rule_7_allocation_percentage_is_not_mistaken_for_a_share_count(
        self, temp_db, tmp_path, monkeypatch
    ):
        # "40% / 60%" is in every report, and this position is 40 shares. An
        # allocation percentage must not withhold the PDF forever.
        with_pcts = self.REPORT.replace(
            "כאן הניתוח, והוא נשאר.",
            "כאן הניתוח, והוא נשאר. החלוקה היא 40% ליעד ו-60% ל-Runner.")

        def mutate(d):
            d["grade"] = "A"
            d["rubric_inputs"] = self.D_GRADE_INPUTS

        _, _ = self._deliver(tmp_path, monkeypatch, 222304, mutate, report=with_pcts)
        assert self.sent["documents"] != []               # the PDF still goes out
        assert "40 מניות" not in self.sent["markdown"]

    def test_a_clean_report_is_left_completely_alone(self, temp_db, tmp_path, monkeypatch):
        def mutate(d):
            d["grade"] = "A"
            d["rubric_inputs"] = self.A_GRADE_INPUTS

        thesis, markdown = self._deliver(tmp_path, monkeypatch, 222206, mutate)
        assert thesis["decision"] == "Buy Now"
        assert thesis["planned_qty"] == 40
        assert markdown == self.REPORT
        assert "40 מניות" in markdown


class TestPortfolioHeatBlocks:
    """2026-08-30. Rules 19-21 were disclosure-only from the start, by explicit
    direction. Heat is the first of the three to stop an order.

    It could become a block precisely because it is the only one the book is not
    already standing on: sector concentration sits at 81% against a 40% cap and
    free cash covers a fraction of a full position, so turning either of those on
    would have blocked every new idea overnight and taught its reader to route
    around it. Both were re-confirmed as disclosure the same day.
    """

    def _deliver(self, tmp_path, monkeypatch, update_id, mutate, ticker="CRM"):
        persistence.enqueue_message(update_id=update_id, from_id="test", chat_id="test",
                                     message_type="text", message_text="/screener " + ticker,
                                     raw_update={})
        path = _decision_file(tmp_path, update_id, ticker=ticker)
        decision = json.loads(path.read_text(encoding="utf-8"))
        decision["sizing"] = {"entry": 260.0, "stop": 250.0, "qty": 22,
                               "risk_usd_target": 400.0, "multipliers": {}}
        mutate(decision)
        path.write_text(json.dumps(decision), encoding="utf-8")
        sent = {}
        monkeypatch.setattr(deliver_report, "send_photo", lambda *a, **k: _ok(1))
        monkeypatch.setattr(deliver_report, "send_document", lambda *a, **k: _ok(2))
        monkeypatch.setattr(deliver_report, "send_text",
                             lambda body, *a, **k: (sent.__setitem__("summary", body), _ok(3))[1])
        monkeypatch.setattr(deliver_report, "render_report_pdf", lambda md: b"%PDF")
        assert _run_main(path, monkeypatch) == 0
        self.summary = sent.get("summary", "")
        return persistence.get_thesis(ticker)

    def _over_cap(self, d):
        d["portfolio_heat_after"] = 0.072      # 7.2%
        d["portfolio_heat_cap_pct"] = 0.06

    def _under_cap(self, d):
        d["portfolio_heat_after"] = 0.041
        d["portfolio_heat_cap_pct"] = 0.06

    def test_over_the_cap_the_order_does_not_go_out(self, temp_db, tmp_path, monkeypatch):
        thesis = self._deliver(tmp_path, monkeypatch, 222801, self._over_cap)
        assert thesis["decision"] == "Watchlist"
        assert thesis["planned_qty"] is None
        assert "7.20%" in self.summary and "6.00%" in self.summary
        assert "/override" in self.summary

    def test_under_the_cap_nothing_happens(self, temp_db, tmp_path, monkeypatch):
        thesis = self._deliver(tmp_path, monkeypatch, 222802, self._under_cap)
        assert thesis["decision"] == "Buy Now"
        assert thesis["planned_qty"] == 22

    def test_an_override_lets_exactly_one_order_through(self, temp_db, tmp_path, monkeypatch):
        persistence.add_risk_override("CRM", "heat", "מכוון, מקטין ROP מחר")
        first = self._deliver(tmp_path, monkeypatch, 222803, self._over_cap)
        assert first["decision"] == "Buy Now"

        # spent. the next report over the cap is stopped again, with no memory
        # of having been waved through once.
        second = self._deliver(tmp_path, monkeypatch, 222804, self._over_cap)
        assert second["decision"] == "Watchlist"

    def test_an_override_is_for_one_ticker_only(self, temp_db, tmp_path, monkeypatch):
        persistence.add_risk_override("MU", "heat", "לא רלוונטי ל-CRM")
        thesis = self._deliver(tmp_path, monkeypatch, 222805, self._over_cap)
        assert thesis["decision"] == "Watchlist"

    def test_an_override_needs_a_written_reason(self, temp_db):
        with pytest.raises(ValueError):
            persistence.add_risk_override("CRM", "heat", "   ")

    def test_an_expired_override_does_not_work(self, temp_db, tmp_path, monkeypatch):
        persistence.add_risk_override("CRM", "heat", "אתמול")
        with persistence._db() as conn:
            conn.execute("UPDATE risk_overrides SET expires_at='2020-01-01T00:00:00+00:00'")
        thesis = self._deliver(tmp_path, monkeypatch, 222806, self._over_cap)
        assert thesis["decision"] == "Watchlist"

    def test_a_heat_number_that_cannot_be_computed_is_not_overridable(
        self, temp_db, tmp_path, monkeypatch
    ):
        # An override is agreement to a KNOWN risk. There is nothing to agree to
        # when a position has no stop and the total is a floor rather than a
        # total, so this path deliberately has no way past it.
        persistence.set_equity(100_000.0)
        with persistence._db() as conn:
            conn.execute("INSERT INTO thesis (ticker, status, sleeve, updated_at) "
                          "VALUES ('ZZZZ', 'open_position', 'swing', ?)", (persistence._now(),))
        persistence.create_position(
            ticker="ZZZZ", entry_date="2026-07-01", entry_price=100.0, qty=10,
            entry_type="full", entry_setup={"type": "Breakout", "atr_at_build": 1.0},
            initial_stop=None, current_stop=None)
        persistence.add_risk_override("CRM", "heat", "לא אמור לעזור כאן")

        thesis = self._deliver(tmp_path, monkeypatch, 222807, self._under_cap)
        assert thesis["decision"] == "Watchlist"
        assert "ZZZZ" in self.summary
        assert "אין לזה עקיפה" in self.summary


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
