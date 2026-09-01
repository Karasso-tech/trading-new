"""Unit tests for process_queue.py's batch /screener support (2026-07-13).

Uses an isolated temp SQLite DB (never the real trading_new.db) via monkeypatching
persistence.DB_PATH -- same pattern as test_circuit_breaker.py. send_text is
monkeypatched too -- these tests must never hit the real Telegram API.
"""

import json

import pytest

import persistence
import process_queue


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(persistence, "DB_PATH", db_path)
    persistence.init_db()
    return db_path


@pytest.fixture
def fake_send_text(monkeypatch):
    sent = []

    def _fake(text):
        sent.append(text)
        return {"ok": True, "result": {"message_id": len(sent)}}

    monkeypatch.setattr(process_queue, "send_text", _fake)
    return sent


@pytest.fixture(autouse=True)
def fake_chart_draw(monkeypatch):
    """Records what /filled, /add and /exit would draw, instead of driving the
    real chart (2026-08-07).

    Autouse: those three handlers redraw the position's lines after every
    write, so EVERY test touching them would otherwise fall through to
    _redraw_position_chart's failure path -- which sends the user an extra
    "lines not updated" message and breaks unrelated message-count assertions.
    conftest's _block_tradingview already guarantees nothing reaches the real
    chart; this fixture is about the tests staying readable, not safety.

    Patched at _draw_position_chart (the thin TVClient wrapper) rather than at
    _redraw_position_chart, so the real read-back-and-decide logic above it --
    including "a fully closed position means clear the chart" -- is still the
    code under test. Each entry is (ticker, position-or-None)."""
    drawn = []

    async def _fake(ticker, position):
        drawn.append((ticker, position))

    monkeypatch.setattr(process_queue, "_draw_position_chart", _fake)
    return drawn


def _received_texts() -> list[str]:
    with persistence._db() as conn:
        rows = conn.execute(
            "SELECT message_text FROM messages WHERE status='received' ORDER BY update_id"
        ).fetchall()
    return [r["message_text"] for r in rows]


class TestParseScreenerTickers:
    def test_single_ticker(self):
        assert process_queue._parse_screener_tickers("/screener AAPL") == ["AAPL"]

    def test_comma_separated(self):
        assert process_queue._parse_screener_tickers("/screener AAPL,MSFT,NVDA") == ["AAPL", "MSFT", "NVDA"]

    def test_space_separated(self):
        assert process_queue._parse_screener_tickers("/screener AAPL MSFT NVDA") == ["AAPL", "MSFT", "NVDA"]

    def test_mixed_commas_and_spaces_and_extra_whitespace(self):
        assert process_queue._parse_screener_tickers("/screener  AAPL,  MSFT   NVDA ,GOOGL") == \
            ["AAPL", "MSFT", "NVDA", "GOOGL"]

    def test_lowercase_normalized_to_upper(self):
        assert process_queue._parse_screener_tickers("/screener aapl, msft") == ["AAPL", "MSFT"]

    def test_duplicate_tickers_deduped_preserving_order(self):
        assert process_queue._parse_screener_tickers("/screener AAPL, MSFT, AAPL") == ["AAPL", "MSFT"]

    def test_no_ticker_returns_empty(self):
        assert process_queue._parse_screener_tickers("/screener") == []
        assert process_queue._parse_screener_tickers("/screener   ") == []

    def test_non_screener_text_returns_empty(self):
        assert process_queue._parse_screener_tickers("/monitor AAPL") == []


class TestHandleScreenerBatch:
    def test_single_ticker_delegates_to_handle_screener_unchanged(self, monkeypatch, temp_db, fake_send_text):
        calls = []
        monkeypatch.setattr(process_queue, "_handle_screener", lambda uid, text: calls.append((uid, text)) or True)

        result = process_queue._handle_screener_batch(1001, "/screener AAPL")

        assert result is True
        assert calls == [(1001, "/screener AAPL")]
        # single-ticker path must not enqueue any synthetic messages
        assert _received_texts() == []
        assert fake_send_text == []

    def test_batch_enqueues_one_synthetic_message_per_ticker(self, temp_db, fake_send_text):
        persistence.enqueue_message(
            update_id=2001, from_id="u", chat_id="c", message_type="text",
            message_text="/screener AAPL, MSFT, NVDA", raw_update={},
        )

        result = process_queue._handle_screener_batch(2001, "/screener AAPL, MSFT, NVDA")

        assert result is True
        assert sorted(_received_texts()) == ["/screener AAPL", "/screener MSFT", "/screener NVDA"]
        # the original batch message itself gets an ack, marked sent
        with persistence._db() as conn:
            row = conn.execute("SELECT status FROM messages WHERE update_id=2001").fetchone()
        assert row["status"] == "sent"
        assert len(fake_send_text) == 1
        assert "3" in fake_send_text[0]  # ack mentions the ticker count

    def test_synthetic_update_ids_are_negative_and_unique(self, temp_db, fake_send_text):
        process_queue._handle_screener_batch(3001, "/screener AAPL, MSFT")
        with persistence._db() as conn:
            ids = [r["update_id"] for r in conn.execute(
                "SELECT update_id FROM messages WHERE status='received'"
            ).fetchall()]
        assert len(ids) == 2
        assert len(set(ids)) == 2  # unique
        assert all(uid < 0 for uid in ids)  # can never collide with a real Telegram update_id

    def test_over_max_batch_size_enqueues_nothing_and_warns(self, temp_db, fake_send_text):
        tickers = ", ".join(f"T{i}" for i in range(process_queue._MAX_BATCH_SCREENER + 1))
        result = process_queue._handle_screener_batch(4001, f"/screener {tickers}")

        assert result is True
        assert _received_texts() == []
        assert len(fake_send_text) == 1
        assert str(process_queue._MAX_BATCH_SCREENER) in fake_send_text[0]

    def test_no_ticker_returns_false_for_fallthrough_to_unrecognized(self, temp_db, fake_send_text):
        result = process_queue._handle_screener_batch(5001, "/screener")
        assert result is False
        assert _received_texts() == []
        assert fake_send_text == []


class TestSelfDrainingIntegration:
    """Exercises the same two-round pattern main()'s while-loop relies on
    (claim -> dispatch -> re-claim), without calling main() itself -- main()
    acquires the real, shared bot/.process_queue.lock file, which must not be
    touched by a test run (the live ack_listener.py may hold it concurrently)."""

    def test_batch_dispatch_then_reclaim_runs_each_ticker_once(self, temp_db, fake_send_text, monkeypatch):
        screener_calls = []

        def _fake_handle_screener(uid, text):
            screener_calls.append((uid, text))
            # Real _handle_screener always reaches a terminal state (sent/failed,
            # via deliver_report.py or its own safety-net check) -- simulate that
            # so count_pending_messages() behaves like it would in production.
            persistence.mark_sent(uid)
            return True

        monkeypatch.setattr(process_queue, "_handle_screener", _fake_handle_screener)

        persistence.enqueue_message(
            update_id=6001, from_id="u", chat_id="c", message_type="text",
            message_text="/screener AAPL, MSFT", raw_update={},
        )

        # Round 1: main() would claim_next_messages() and dispatch each -- here
        # there's just the one batch message.
        round1 = persistence.claim_next_messages()
        assert [r["update_id"] for r in round1] == [6001]
        for row in round1:
            process_queue._dispatch(row)

        # The batch handler enqueued 2 synthetic messages -- main()'s loop checks
        # count_pending_messages() and, seeing >0, re-claims for a second round.
        assert persistence.count_pending_messages() == 2
        round2 = persistence.claim_next_messages()
        assert len(round2) == 2
        for row in round2:
            process_queue._dispatch(row)

        assert persistence.count_pending_messages() == 0
        assert len(screener_calls) == 2
        assert {c[1] for c in screener_calls} == {"/screener AAPL", "/screener MSFT"}
        assert all(uid < 0 for uid, _ in screener_calls)  # dispatched via the synthetic update_ids


class TestOnlyDeniedViaWrongTool:
    """Regression coverage for the wrong-tool-bailout detector (2026-07-13,
    widened 2026-07-14). Fixtures are near-verbatim `combined` JSON shapes
    pulled from real process_queue.log entries, not synthetic -- the whole
    point of this function is to match/reject actual model wording, so the
    tests should exercise actual wording it's seen get this wrong."""

    _DOWNLOAD_PHOTO_DENIAL = {
        "tool_name": "Bash",
        "tool_input": {"command": "python bot\\download_photo.py AgAC... _playbook_1.jpg"},
    }
    _PERMITTED = ["download_photo.py", "fetch_analysis_data.py", "deliver_playbook_report.py"]

    def test_original_2026_07_13_wording_matches(self):
        # The exact shape the detector was originally built for.
        combined = (
            '{"result": "I attempted to run `python bot\\\\download_photo.py` twice '
            'and both times it was blocked with \\"This command requires approval\\" '
            '-- this session appears to be non-interactive, so I can\'t get that '
            'approval granted here.", "permission_denials": [%s, %s]}'
            % (str(self._DOWNLOAD_PHOTO_DENIAL).replace("'", '"'),
               str(self._DOWNLOAD_PHOTO_DENIAL).replace("'", '"'))
        )
        assert process_queue._only_denied_via_wrong_tool(combined, self._PERMITTED) is True

    def test_2026_07_14_reworded_bailout_matches(self):
        # Found real 2026-07-14: same denial shape, but the model's own closing
        # line was "I need your approval... could you approve it" -- neither
        # "requires approval" nor "approve this tool" appear verbatim, which is
        # exactly why the retry didn't fire and the user saw a raw failure.
        combined = (
            '{"result": "I need your approval to run the photo download command. '
            'Could you approve it so I can proceed?", "permission_denials": [%s]}'
            % str(self._DOWNLOAD_PHOTO_DENIAL).replace("'", '"')
        )
        assert process_queue._only_denied_via_wrong_tool(combined, self._PERMITTED) is True

    def test_successful_run_with_incidental_bash_denial_does_not_match(self):
        # Bash-only denial for a permitted script also shows up in genuinely
        # successful runs (denied via Bash once, retried via PowerShell,
        # finished within the same session) -- the result text talks about the
        # actual delivered report, not approval/permission, so must NOT match.
        combined = (
            '{"result": "Playbook delivered successfully (7 positions).", '
            '"permission_denials": [%s]}' % str(self._DOWNLOAD_PHOTO_DENIAL).replace("'", '"')
        )
        assert process_queue._only_denied_via_wrong_tool(combined, self._PERMITTED) is False

    def test_unrelated_denial_for_a_non_permitted_command_does_not_match(self):
        # A denied Remove-Item cleanup call (correctly blocked, unrelated to the
        # three permitted scripts) must not itself trigger a retry.
        combined = (
            '{"result": "That delete command isn\'t one of the three permitted '
            'commands for this task, so it was correctly blocked.", '
            '"permission_denials": [{"tool_name": "PowerShell", '
            '"tool_input": {"command": "Remove-Item _playbook_1.jpg"}}]}'
        )
        assert process_queue._only_denied_via_wrong_tool(combined, self._PERMITTED) is False

    def test_non_bash_denial_for_a_permitted_command_does_not_match(self):
        # If PowerShell itself (the actually-allowlisted tool) was denied on a
        # permitted script, that's a real permission problem, not a wrong-tool
        # mistake a retry could fix -- must not match.
        combined = (
            '{"result": "I need approval to proceed.", "permission_denials": '
            '[{"tool_name": "PowerShell", "tool_input": '
            '{"command": "python bot\\\\download_photo.py x"}}]}'
        )
        assert process_queue._only_denied_via_wrong_tool(combined, self._PERMITTED) is False

    def test_malformed_json_does_not_match(self):
        assert process_queue._only_denied_via_wrong_tool("not json", self._PERMITTED) is False


class TestHandlePlaybookDeterministicDownload:
    """2026-07-14: the photo download used to be a tool call the inner claude -p
    session made itself, which broke repeatedly because the model's exact shell
    command text for it wasn't stable (see _handle_playbook's own comment).
    process_queue now downloads the photo directly in Python, before ever
    spawning claude -p. These tests cover the failure branch (which must fail
    fast with a specific reason and never spend a claude -p call) and confirm
    a successful download reaches the claude -p invocation."""

    def _raw_update(self, update_id: int) -> dict:
        return {"message": {"photo": [{"file_id": f"file_{update_id}"}]}}

    def test_download_failure_marks_failed_without_calling_claude(self, temp_db, fake_send_text, monkeypatch):
        persistence.enqueue_message(9001, "u1", "c1", "photo", "", None)

        def _fake_run(cmd, **kwargs):
            class _Result:
                returncode = 1
                stdout = ""
                stderr = "FAILED: getFile did not return ok=True"
            return _Result()

        monkeypatch.setattr(process_queue.subprocess, "run", _fake_run)
        called = []
        monkeypatch.setattr(process_queue, "_run_claude_with_retry",
                             lambda *a, **k: called.append(1) or (0, "{}"))

        result = process_queue._handle_playbook(9001, self._raw_update(9001))

        assert result is True
        assert called == []  # claude -p must never be invoked after a download failure
        status = process_queue._message_status(9001)
        assert status == "failed"
        assert len(fake_send_text) == 1  # user gets a specific Telegram failure notice

    def test_successful_download_proceeds_to_claude_invocation(self, temp_db, fake_send_text, monkeypatch):
        persistence.enqueue_message(9002, "u1", "c1", "photo", "", None)

        def _fake_run(cmd, **kwargs):
            class _Result:
                returncode = 0
                stdout = "OK: downloaded 123 bytes"
                stderr = ""
            return _Result()

        monkeypatch.setattr(process_queue.subprocess, "run", _fake_run)
        seen_cmds = []

        def _fake_retry(cmd, **kwargs):
            seen_cmds.append(cmd)
            persistence.mark_sent(9002)
            return 0, "{}"

        monkeypatch.setattr(process_queue, "_run_claude_with_retry", _fake_retry)

        result = process_queue._handle_playbook(9002, self._raw_update(9002))

        assert result is True
        assert len(seen_cmds) == 1
        # download_photo.py is no longer a tool the inner session needs/has permission for.
        assert not any("download_photo.py" in str(part) for part in seen_cmds[0])
        assert process_queue._message_status(9002) == "sent"


class TestAddRegex:
    def test_matches_the_documented_shape(self):
        m = process_queue._ADD_RE.match("/add NVDA 210.50 25")
        assert m is not None
        assert m.groups() == ("NVDA", "210.50", "25")

    def test_case_insensitive(self):
        assert process_queue._ADD_RE.match("/ADD nvda 210.50 25") is not None

    def test_rejects_missing_qty(self):
        assert process_queue._ADD_RE.match("/add NVDA 210.50") is None

    def test_rejects_trailing_junk(self):
        assert process_queue._ADD_RE.match("/add NVDA 210.50 25 extra") is None


class TestHandleAdd:
    """2026-07-15: /add tops up an already-open position (starter -> full)
    via persistence.add_to_position() -- see that function's own docstring
    and test_persistence.py's TestAddToPosition for the persistence-layer
    coverage. These tests cover the command handler's own plumbing: does it
    call add_to_position with the right args, and does a ValueError (no open
    position) mark the message failed instead of crashing."""

    def _open_starter(self, ticker="NVDA", qty=50, entry_price=208.62):
        with persistence._db() as conn:
            conn.execute(
                "INSERT INTO thesis (ticker, status, sleeve, updated_at) VALUES (?, 'open_position', 'swing', ?)",
                (ticker, persistence._now()),
            )
        persistence.create_position(
            ticker=ticker, entry_date="2026-07-11", entry_price=entry_price, qty=qty,
            entry_type="starter", entry_setup={"type": "Reclaim", "stop": 189.80, "atr_at_build": 7.13},
            initial_stop=189.80,
        )

    def test_happy_path_adds_and_marks_sent(self, temp_db, fake_send_text):
        self._open_starter(qty=50, entry_price=208.62)
        persistence.enqueue_message(9101, "u1", "c1", "text", "/add NVDA 210.62 50", None)

        result = process_queue._handle_add(9101, "/add NVDA 210.62 50")

        assert result is True
        assert process_queue._message_status(9101) == "sent"
        pos = persistence.get_open_position("NVDA")
        assert pos["qty"] == 100
        assert pos["entry_type"] == "full"
        assert len(fake_send_text) == 1
        assert "הוספה נרשמה" in fake_send_text[0]

    def test_discloses_risk_against_cap_same_as_filled(self, temp_db, fake_send_text):
        """2026-07-29: /add used to skip the same cap disclosure /filled has
        had since the CRDO incident (see TestBuildFillRiskLine) -- exactly the
        starter->full top-up case that disclosure exists for. Now reuses it
        against the post-add totals."""
        persistence.set_equity(100000)
        persistence.set_risk_pct(0.01)
        self._open_starter(qty=50, entry_price=208.62)
        persistence.get_open_position("NVDA")
        process_queue._handle_add(9105, "/add NVDA 210.62 50")
        assert "סיכון" in fake_send_text[0]

    def test_no_open_position_marks_failed_not_crashed(self, temp_db, fake_send_text):
        persistence.enqueue_message(9102, "u1", "c1", "text", "/add MSFT 300.0 10", None)

        result = process_queue._handle_add(9102, "/add MSFT 300.0 10")

        assert result is True
        assert process_queue._message_status(9102) == "failed"
        assert len(fake_send_text) == 1  # user gets a specific failure notice, not silence

    def test_non_matching_text_returns_false_for_fallthrough(self, temp_db, fake_send_text):
        assert process_queue._handle_add(9103, "/notadd NVDA 1 1") is False


class TestHandleOpenRemainingQty:
    """Found in review (2026-07-16, real XLF incident): /open used to display
    positions.qty (the original, fixed fill size) as if it were the live share
    count -- a position with any prior partial exit showed its stale original
    size forever, not what's really left. Now shows remaining_qty, with an
    explicit "out of" qualifier only once something has actually been
    partially exited."""

    def _open_xlf(self):
        with persistence._db() as conn:
            conn.execute(
                "INSERT INTO thesis (ticker, status, sleeve, updated_at) VALUES ('XLF', 'open_position', 'swing', ?)",
                (persistence._now(),),
            )
        persistence.create_position(
            ticker="XLF", entry_date="2026-06-17", entry_price=54.765, qty=350,
            entry_type="full", entry_setup={"type": "Breakout", "stop": 53.9, "atr_at_build": 0.73},
            initial_stop=53.9, current_stop=56.01,
        )

    def test_no_partial_exit_shows_plain_qty(self, temp_db, fake_send_text):
        self._open_xlf()
        process_queue._handle_open(9201)
        assert "כמות: 350" in fake_send_text[0]
        assert "מתוך" not in fake_send_text[0]

    def test_partial_exit_shows_remaining_out_of_original(self, temp_db, fake_send_text):
        self._open_xlf()
        persistence.record_exit("XLF", exit_price=56.52, exit_qty=140, exit_date="2026-07-14",
                                 source="exit_command")
        process_queue._handle_open(9202)
        assert "כמות: 210 (מתוך 350)" in fake_send_text[0]

    def test_no_equity_set_shows_a_plain_warning_not_a_crash(self, temp_db, fake_send_text):
        self._open_xlf()
        process_queue._handle_open(9203)
        assert "לא הוגדר שווי חשבון" in fake_send_text[0]

    def test_equity_set_shows_allocation_and_heat_figures(self, temp_db, fake_send_text):
        self._open_xlf()
        persistence.set_equity(100000)
        process_queue._handle_open(9204)
        text = fake_send_text[0]
        assert "איזור תיק" in text or "איזון תיק" in text
        assert "Core" in text and "Swing" in text
        assert "חשיפת תיק" in text


class TestHandleEquity:
    def test_sets_equity_and_confirms(self, temp_db, fake_send_text):
        persistence.enqueue_message(9301, "u1", "c1", "text", "/equity 150000", None)
        result = process_queue._handle_equity(9301, "/equity 150000")
        assert result is True
        assert process_queue._message_status(9301) == "sent"
        assert persistence.get_account_settings()["equity_usd"] == 150000

    def test_accepts_dollar_sign_and_thousands_commas(self, temp_db, fake_send_text):
        result = process_queue._handle_equity(9302, "/equity $150,000")
        assert result is True
        assert persistence.get_account_settings()["equity_usd"] == 150000

    def test_accepts_decimal_value(self, temp_db, fake_send_text):
        result = process_queue._handle_equity(9303, "/equity 150000.50")
        assert result is True
        assert persistence.get_account_settings()["equity_usd"] == pytest.approx(150000.50)

    def test_non_matching_text_returns_false_for_fallthrough(self, temp_db, fake_send_text):
        assert process_queue._handle_equity(9304, "/notequity 100") is False
        assert fake_send_text == []


class TestHandleSetRisk:
    def test_sets_risk_pct_with_percent_sign(self, temp_db, fake_send_text):
        result = process_queue._handle_setrisk(9401, "/setrisk 1%")
        assert result is True
        assert persistence.get_account_settings()["risk_pct"] == pytest.approx(0.01)

    def test_sets_risk_pct_without_percent_sign(self, temp_db, fake_send_text):
        result = process_queue._handle_setrisk(9402, "/setrisk 0.5")
        assert result is True
        assert persistence.get_account_settings()["risk_pct"] == pytest.approx(0.005)

    def test_non_matching_text_returns_false_for_fallthrough(self, temp_db, fake_send_text):
        assert process_queue._handle_setrisk(9403, "/notsetrisk 1") is False
        assert fake_send_text == []

    def test_a_number_above_the_ceiling_is_refused_outright(self, temp_db, fake_send_text):
        # "/setrisk 20" -- one stray digit -- used to store 20% risk per trade,
        # and every guard in rule 28 is a fraction OF that number, so they would
        # all have scaled up with it without a word.
        persistence.set_risk_pct(0.01)
        persistence.enqueue_message(9404, "u1", "c1", "text", "/setrisk 20", None)

        assert process_queue._handle_setrisk(9404, "/setrisk 20") is True

        assert persistence.get_account_settings()["risk_pct"] == 0.01
        assert "20" in fake_send_text[-1]

    def test_two_percent_is_refused_too_not_only_a_wild_number(self, temp_db, fake_send_text):
        persistence.set_risk_pct(0.01)
        process_queue._handle_setrisk(9405, "/setrisk 2")
        assert persistence.get_account_settings()["risk_pct"] == 0.01

    def test_lowering_is_always_allowed(self, temp_db, fake_send_text):
        persistence.set_risk_pct(0.01)
        persistence.enqueue_message(9406, "u1", "c1", "text", "/setrisk 0.25", None)

        process_queue._handle_setrisk(9406, "/setrisk 0.25")

        assert persistence.get_account_settings()["risk_pct"] == 0.0025


class TestHandleWithdraw:
    def test_sets_pending_withdrawal_and_confirms(self, temp_db, fake_send_text):
        result = process_queue._handle_withdraw(9501, "/withdraw 17500")
        assert result is True
        assert persistence.get_account_settings()["pending_withdrawal_usd"] == 17500
        assert "17,500" in fake_send_text[0]

    def test_accepts_dollar_sign_and_commas(self, temp_db, fake_send_text):
        result = process_queue._handle_withdraw(9502, "/withdraw $17,500")
        assert result is True
        assert persistence.get_account_settings()["pending_withdrawal_usd"] == 17500

    def test_zero_clears_it_with_a_distinct_confirmation(self, temp_db, fake_send_text):
        persistence.set_pending_withdrawal(17500)
        result = process_queue._handle_withdraw(9503, "/withdraw 0")
        assert result is True
        assert persistence.get_account_settings()["pending_withdrawal_usd"] == 0
        assert "אופסה" in fake_send_text[0]

    def test_non_matching_text_returns_false_for_fallthrough(self, temp_db, fake_send_text):
        assert process_queue._handle_withdraw(9504, "/notwithdraw 100") is False
        assert fake_send_text == []


class TestBuildFillRiskLine:
    """2026-07-27: /filled's confirmation now discloses the fill's own risk %
    against the account's 1% cap -- added after a real CRDO fill sized at 2.2x
    the cap sat unnoticed for a week. Disclosure-only, never blocks (rule 19
    posture): these tests check the line's content, not any rejection."""

    def test_under_cap_shows_plain_risk_line_no_warning(self, temp_db):
        persistence.set_equity(100000)
        persistence.set_risk_pct(0.01)
        line = process_queue._build_fill_risk_line(price=100.0, qty=100, stop=95.0)
        assert "$500" in line
        assert "0.50%" in line
        assert "⚠️" not in line

    def test_over_cap_flags_warning_with_max_qty(self, temp_db):
        persistence.set_equity(152330)
        persistence.set_risk_pct(0.01)
        line = process_queue._build_fill_risk_line(price=227.62270270270272, qty=74, stop=182.07)
        assert "2.21%" in line
        assert "⚠️" in line
        assert "33 מניות" in line

    def test_no_stop_states_plainly_instead_of_guessing(self, temp_db):
        persistence.set_equity(100000)
        line = process_queue._build_fill_risk_line(price=100.0, qty=5, stop=None)
        assert "לא ניתן לחשב" in line
        assert "$" not in line

    def test_equity_unset_states_plainly_instead_of_guessing(self, temp_db):
        line = process_queue._build_fill_risk_line(price=100.0, qty=5, stop=95.0)
        assert "הון לא מוגדר" in line

    def test_stop_above_price_returns_empty_not_negative_risk(self, temp_db):
        persistence.set_equity(100000)
        persistence.set_risk_pct(0.01)
        line = process_queue._build_fill_risk_line(price=100.0, qty=5, stop=105.0)
        assert line == ""


class TestMaxAddRegex:
    def test_matches_the_documented_shape(self):
        m = process_queue._MAXADD_RE.match("/maxadd SCHW")
        assert m is not None
        assert m.groups() == ("SCHW",)

    def test_case_insensitive(self):
        assert process_queue._MAXADD_RE.match("/MAXADD schw") is not None

    def test_rejects_trailing_junk(self):
        assert process_queue._MAXADD_RE.match("/maxadd SCHW 105.97") is None


class TestRrGate:
    """CONSISTENCY_RULES.md rule 3, verbatim: >=1.5x ATR needs R:R>=2:1; the
    1.0x-1.5x band needs the stricter R:R>=2.5:1 instead of being disqualified
    by distance alone; below 1.0x ATR never qualifies regardless of R:R."""

    def test_passes_at_1_5x_atr_with_rr_2(self):
        assert process_queue._rr_gate(atr_mult=1.5, rr=2.0) is True

    def test_fails_at_1_5x_atr_with_rr_below_2(self):
        assert process_queue._rr_gate(atr_mult=1.5, rr=1.9) is False

    def test_mid_band_needs_stricter_2_5_rr(self):
        assert process_queue._rr_gate(atr_mult=1.2, rr=2.4) is False
        assert process_queue._rr_gate(atr_mult=1.2, rr=2.5) is True

    def test_below_1x_atr_never_qualifies(self):
        assert process_queue._rr_gate(atr_mult=0.9, rr=10.0) is False


class TestMaxAddBySectorCap:
    """Solves N directly (see the function's own docstring for the algebra)
    rather than searching -- these tests verify the closed form against a
    hand-computed boundary, not just a plausible-looking number."""

    def test_empty_exposure_returns_none_not_zero(self):
        # no swing book yet -- an unknown denominator, not "already at the cap"
        assert process_queue._max_add_by_sector_cap("X", {}, 0.40, 5.0) is None

    def test_group_already_over_cap_returns_zero(self):
        exposure = {"Tech": {"risk_usd": 5000}, "Fin": {"risk_usd": 1000}}  # swing_total=6000
        assert process_queue._max_add_by_sector_cap("Tech", exposure, 0.40, 5.0) == 0

    def test_normal_case_matches_hand_computed_boundary(self):
        exposure = {"Fin": {"risk_usd": 1000}, "Other": {"risk_usd": 2000}}  # swing_total=3000
        # room = 0.40*3000-1000=200; denom=5*(1-0.4)=3.0 -> floor(200/3.0)=66
        n = process_queue._max_add_by_sector_cap("Fin", exposure, 0.40, 5.0)
        assert n == 66
        # verify the boundary itself: N shares must keep the post-add pct at/under cap,
        # N+1 must not
        added = n * 5.0
        assert (1000 + added) / (3000 + added) <= 0.40
        added_next = (n + 1) * 5.0
        assert (1000 + added_next) / (3000 + added_next) > 0.40


class TestBuildMaxAddBody:
    """Pure arithmetic against already-fetched live/DB data -- see
    _build_maxadd_body's own docstring for why this is split from
    _handle_maxadd (so these tests never need to mock a subprocess/TVClient
    fetch, same posture as TestBuildFillRiskLine). Default heat/sector args
    below are deliberately loose (heat_usd=0, sector_exposure={}) so they
    never bind unless a test is specifically exercising that cap -- the
    per-trade 1% cap is what's under test otherwise."""

    def _pos(self, qty=200, entry_price=103.58, stop=99.06, targets=None):
        return {
            "qty": qty, "entry_price": entry_price, "current_stop": stop,
            "entry_setup": {"targets": targets or []},
        }

    def _call(self, pos, current_price=105.97, atr14=2.5, fresh=True, equity=150000,
               trade_cap_pct=0.01, heat=None, sector_exposure=None,
               sector_group="unclassified", sector_cap_pct=0.40):
        return process_queue._build_maxadd_body(
            "SCHW", pos, equity, trade_cap_pct,
            current_price=current_price, atr14=atr14, fresh=fresh,
            heat=heat or {"heat_usd": 0.0, "cap_pct": 0.06},
            sector_exposure=sector_exposure if sector_exposure is not None else {},
            sector_group=sector_group, sector_cap_pct=sector_cap_pct,
        )

    def test_happy_path_reports_max_addable_qty(self):
        body = self._call(self._pos())
        # trade cap $1500, existing risk (103.58-99.06)*200=$904 -> room $596
        # new-share risk at live price 105.97-99.06=$6.91 -> floor(596/6.91)=86
        # heat/sector both loose by default -> trade cap is the binding one
        assert "86 מניות" in body
        assert "סיכון בודד" in body  # binding-cap label shown

    def test_already_over_cap_shows_zero_not_negative(self):
        body = self._call(self._pos(qty=200), equity=20000)  # existing risk $904 alone exceeds a $200 cap
        assert "0 מניות" in body
        assert "אין מקום להוספה" in body

    def test_live_price_at_or_below_stop_reports_no_room(self):
        body = self._call(self._pos(stop=99.06), current_price=98.0)
        assert "אין תוספת אפשרית" in body

    def test_stop_trailed_above_entry_shows_locked_profit_not_negative_risk(self):
        # real case (INCY, found via user report): a trailed stop above entry
        # makes (entry_price - stop) negative -- that's locked-in profit on the
        # existing shares, not a loss, and must never be shown as "$-88 risk".
        body = self._call(self._pos(entry_price=115.92, stop=118.12), current_price=129.93)
        assert "$-" not in body
        assert "רווח נעול" in body

    def test_no_stop_states_plainly(self):
        body = self._call(self._pos(stop=None))
        assert "לא ניתן לחשב" in body

    def test_stale_data_adds_a_warning(self):
        body = self._call(self._pos(), fresh=False)
        assert "לא טריים" in body

    def test_heat_cap_binds_tighter_than_trade_cap(self):
        # trade cap loose (5%): room ~$6596 -> ~954 shares
        # heat cap 6% of 150000=$9000, heat already at $8500 -> room $500
        # -> floor(500/6.91)=72, well under the trade cap's ~954
        body = self._call(self._pos(), trade_cap_pct=0.05, heat={"heat_usd": 8500.0, "cap_pct": 0.06})
        assert "72 מניות" in body
        assert "חשיפת תיק" in body  # binding-cap label shown

    def test_sector_cap_binds_tighter_than_others(self):
        # both trade (5%) and heat (6%, empty book) are loose here; sector book
        # is small and the group's already close to 40% of it
        exposure = {"Financials": {"risk_usd": 1000.0}, "Tech": {"risk_usd": 2000.0}}  # swing_total=3000
        # room = 0.40*3000-1000=200; denom=6.91*0.6=4.146 -> floor(200/4.146)=48
        body = self._call(
            self._pos(), trade_cap_pct=0.05, heat={"heat_usd": 0.0, "cap_pct": 0.06},
            sector_exposure=exposure, sector_group="Financials", sector_cap_pct=0.40,
        )
        assert "48 מניות" in body
        assert "סקטור" in body

    def test_no_swing_book_states_plainly_for_sector(self):
        body = self._call(self._pos(), sector_exposure={})
        assert "אין עדיין חשיפת swing" in body

    def test_open_target_within_gate_shows_pass_verdict(self):
        # target 109.20, price 105.97 -> distance 3.23, atr 2.5 -> 1.29x ATR
        # risk/share = 105.97-99.06 = 6.91 -> R:R = 3.23/6.91 = 0.47 -> fails
        body = self._call(self._pos(targets=[{"price": 109.20, "status": "pass"}]))
        assert "יעד קרוב" in body
        assert "כבר לא תקף" in body

    def test_no_open_targets_states_plainly(self):
        body = self._call(self._pos(targets=[{"price": 109.20, "status": "hit"}]))
        assert "אין יעד פתוח" in body


class TestHandleMaxAdd:
    """2026-07-29: pre-trade check for 'how many shares can I add right now,
    and does the stored target still make sense' -- asked before placing the
    order, unlike /add's and /filled's post-fill disclosure. Standalone:
    fetches its own live price/ATR14 via a subprocess call to
    fetch_maxadd_data.py, mocked here the same way
    TestHandlePlaybookDeterministicDownload mocks its own subprocess.run
    call -- these tests cover the handler's wiring/error-handling, not the
    live TradingView fetch itself."""

    def _open_schw(self, qty=200, entry_price=103.58, stop=99.06):
        with persistence._db() as conn:
            conn.execute(
                "INSERT INTO thesis (ticker, status, sleeve, updated_at) VALUES ('SCHW', 'open_position', 'swing', ?)",
                (persistence._now(),),
            )
        persistence.create_position(
            ticker="SCHW", entry_date="2026-07-27", entry_price=entry_price, qty=qty,
            entry_type="starter", entry_setup={"type": "Pullback", "stop": stop, "atr_at_build": 2.41},
            initial_stop=stop, current_stop=stop,
        )

    def _fake_run_ok(self, current_price=105.97, atr14=2.5, fresh=True):
        payload = json.dumps({
            "ticker": "SCHW", "current_price": current_price, "atr14": atr14,
            "freshness": {"fresh": fresh, "last_bar_date": "2026-07-28",
                          "most_recent_complete_session": "2026-07-28"},
        })

        def _run(cmd, **kwargs):
            class _Result:
                returncode = 0
                stdout = payload
                stderr = ""
            return _Result()
        return _run

    def test_happy_path_fetches_and_reports(self, temp_db, fake_send_text, monkeypatch):
        persistence.set_equity(150000)
        persistence.set_risk_pct(0.01)
        self._open_schw()
        # Filler position in a DIFFERENT sector_map group (mega_cap_growth_tech,
        # not SCHW's unclassified bucket) -- without this, SCHW would be the
        # only swing position in the book and thus already 100% of it, which
        # would make the sector cap (not the 1% trade cap) the binding
        # constraint below and defeat the point of this test (confirming the
        # trade-cap arithmetic wires through end to end). The sector cap's own
        # binding behavior is covered directly in TestBuildMaxAddBody.
        with persistence._db() as conn:
            conn.execute(
                "INSERT INTO thesis (ticker, status, sleeve, updated_at) VALUES ('NVDA', 'open_position', 'swing', ?)",
                (persistence._now(),),
            )
        persistence.create_position(
            ticker="NVDA", entry_date="2026-07-20", entry_price=500.0, qty=1000,
            entry_type="full", entry_setup={"type": "Breakout", "stop": 497.0, "atr_at_build": 5.0},
            initial_stop=497.0, current_stop=497.0,
        )
        monkeypatch.setattr(process_queue.subprocess, "run", self._fake_run_ok())
        persistence.enqueue_message(9601, "u1", "c1", "text", "/maxadd SCHW", None)

        result = process_queue._handle_maxadd(9601, "/maxadd SCHW")

        assert result is True
        assert process_queue._message_status(9601) == "sent"
        assert "86 מניות" in fake_send_text[0]

    def test_no_open_position_marks_failed_without_fetching(self, temp_db, fake_send_text, monkeypatch):
        called = []
        monkeypatch.setattr(process_queue.subprocess, "run", lambda *a, **k: called.append(1))
        persistence.enqueue_message(9604, "u1", "c1", "text", "/maxadd MSFT", None)

        result = process_queue._handle_maxadd(9604, "/maxadd MSFT")

        assert result is True
        assert process_queue._message_status(9604) == "failed"
        assert called == []  # no open position -- must never spend a live fetch

    def test_equity_unset_states_plainly_without_fetching(self, temp_db, fake_send_text, monkeypatch):
        self._open_schw()
        called = []
        monkeypatch.setattr(process_queue.subprocess, "run", lambda *a, **k: called.append(1))
        result = process_queue._handle_maxadd(9605, "/maxadd SCHW")
        assert result is True
        assert "הון לא מוגדר" in fake_send_text[0]
        assert called == []

    def test_fetch_failure_marks_failed_not_crashed(self, temp_db, fake_send_text, monkeypatch):
        persistence.set_equity(100000)
        self._open_schw()

        def _fake_run(cmd, **kwargs):
            class _Result:
                returncode = 1
                stdout = ""
                stderr = "FAILED: TradingView connection lost"
            return _Result()

        monkeypatch.setattr(process_queue.subprocess, "run", _fake_run)
        persistence.enqueue_message(9606, "u1", "c1", "text", "/maxadd SCHW", None)

        result = process_queue._handle_maxadd(9606, "/maxadd SCHW")

        assert result is True
        assert process_queue._message_status(9606) == "failed"

    def test_fetch_timeout_marks_failed_not_crashed(self, temp_db, fake_send_text, monkeypatch):
        persistence.set_equity(100000)
        self._open_schw()

        def _fake_run(cmd, **kwargs):
            raise process_queue.subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 90))

        monkeypatch.setattr(process_queue.subprocess, "run", _fake_run)
        persistence.enqueue_message(9607, "u1", "c1", "text", "/maxadd SCHW", None)

        result = process_queue._handle_maxadd(9607, "/maxadd SCHW")

        assert result is True
        assert process_queue._message_status(9607) == "failed"

    def test_non_matching_text_returns_false_for_fallthrough(self, temp_db, fake_send_text, monkeypatch):
        called = []
        monkeypatch.setattr(process_queue.subprocess, "run", lambda *a, **k: called.append(1))
        assert process_queue._handle_maxadd(9608, "/notmaxadd SCHW") is False
        assert fake_send_text == []
        assert called == []


class TestLoggedOutDetection:
    """2026-08-02, found in the real log: three /monitor BTCUSD runs failed at
    03:38, 04:41 and 10:03 with the generic "did not reach a terminal state
    (exit=1)". The actual payload said `"result": "Not logged in - Please run
    /login"` and each died in under 1.5 seconds having analysed nothing. The
    fourth attempt at 10:26 ran normally, because by then someone had signed in.

    This condition fails EVERY judgment command identically -- including the
    unattended overnight scans -- and is fixed by one command on the PC, so it
    gets a specific alert naming that fix instead of a generic failure."""

    _REAL_PAYLOAD = (
        '{"type":"result","subtype":"success","is_error":true,"api_error_status":null,'
        '"duration_ms":250,"num_turns":1,"result":"Not logged in \u00b7 Please run /login",'
        '"stop_reason":"stop_sequence"}'
    )

    def test_the_real_payload_is_recognized(self):
        assert process_queue._is_logged_out(self._REAL_PAYLOAD) is True

    def test_a_normal_successful_run_is_not(self):
        ok = '{"is_error":false,"result":"Delivered. BTCUSD white, no order."}'
        assert process_queue._is_logged_out(ok) is False

    def test_quota_exhaustion_is_not_confused_with_being_logged_out(self):
        # Two different problems with two different fixes -- signing in does
        # nothing for an exhausted quota, and waiting does nothing for a
        # signed-out CLI.
        quota = '{"api_error_status":429,"result":"rate limit exceeded"}'
        assert process_queue._is_logged_out(quota) is False
        assert process_queue._is_quota_exhausted(quota) is True

    def test_non_json_output_still_matches_on_the_text(self):
        # The CLI does not always manage to emit JSON when it fails this early.
        assert process_queue._is_logged_out("error: Invalid API key") is True

    def test_empty_output_is_not_a_false_positive(self):
        assert process_queue._is_logged_out("") is False

    def test_the_alert_names_the_fix_and_marks_the_message_failed(self, monkeypatch, temp_db):
        sent = []
        monkeypatch.setattr(process_queue, "send_text",
                            lambda text, **kw: sent.append(text) or {"ok": True, "result": {"message_id": 1}})
        persistence.enqueue_message(update_id=-9001, from_id="t", chat_id="t",
                                     message_type="text", message_text="/monitor BTCUSD",
                                     raw_update={})
        process_queue._alert_logged_out("/monitor BTCUSD", -9001)
        assert len(sent) == 1
        assert "claude /login" in sent[0]
        assert process_queue._message_status(-9001) == "failed"

    def test_only_one_alert_per_run_however_many_commands_fail(self, monkeypatch, temp_db):
        # The nightly rebuild queues 20+ screener runs that one process drains
        # in a single pass. A signed-out CLI fails all of them within a minute,
        # and 20 copies of the same message is how a real alert gets muted.
        monkeypatch.setattr(process_queue, "_logged_out_alert_sent", False)
        sent = []
        monkeypatch.setattr(process_queue, "send_text",
                            lambda text, **kw: sent.append(text) or {"ok": True, "result": {"message_id": 1}})
        for i in range(5):
            update_id = -9100 - i
            persistence.enqueue_message(update_id=update_id, from_id="t", chat_id="t",
                                         message_type="text", message_text=f"/screener T{i}",
                                         raw_update={})
            process_queue._alert_logged_out(f"/screener T{i}", update_id)
        assert len(sent) == 1
        # Every message is still individually marked failed -- only the
        # notification is deduplicated, never the bookkeeping.
        for i in range(5):
            assert process_queue._message_status(-9100 - i) == "failed"


class TestPromptTemplatesFormat:
    """Found live 2026-08-03: a JSON example written into
    _SCREENER_PROMPT_TEMPLATE with single braces made str.format() read it as a
    placeholder, so every /screener run died with KeyError('"entry"') before the
    model was ever launched -- three nightly rebuilds failed in a row with no
    hint of the real cause. These templates are big, prose-heavy, and edited
    often; a literal brace slipping in is a recurring hazard, not a one-off.

    Each template is formatted with the SAME keyword set its real call site
    passes, so a placeholder that is renamed or dropped on one side and not the
    other is caught here too."""

    CALL_SITE_KWARGS = {
        "_SCREENER_PROMPT_TEMPLATE": {"ticker": "ABC", "update_id": 1, "date": "2026-08-03"},
        "_MONITOR_PROMPT_TEMPLATE": {"ticker": "ABC", "update_id": 1, "date": "2026-08-03"},
        "_AUTOMONITOR_PROMPT_TEMPLATE": {"tickers": "ABC, XYZ", "update_id": 1, "date": "2026-08-03",
                                          "strict_flag": "", "strict_gate_note": ""},
        "_PLAYBOOK_PROMPT_TEMPLATE": {"update_id": 1, "date": "2026-08-03"},
        "_POSITION_STATUS_PROMPT_TEMPLATE": {"tickers": "ABC", "update_id": 1, "date": "2026-08-03",
                                              "run_label": "midday"},
    }

    def test_every_prompt_template_is_covered_by_this_test(self):
        # A new template added without a kwargs entry would otherwise never be
        # checked -- the gap that let this bug through in the first place.
        found = {n for n in dir(process_queue) if n.endswith("_PROMPT_TEMPLATE")
                 and isinstance(getattr(process_queue, n), str)}
        assert found == set(self.CALL_SITE_KWARGS)

    @pytest.mark.parametrize("name", sorted(CALL_SITE_KWARGS))
    def test_template_formats_without_raising(self, name):
        getattr(process_queue, name).format(**self.CALL_SITE_KWARGS[name])

    def test_the_screener_json_example_survives_formatting(self):
        # The specific regression: the model must still receive real, SINGLE
        # braces -- escaping them in the source must not leak {{ or }} into the
        # actual prompt.
        out = process_queue._SCREENER_PROMPT_TEMPLATE.format(**self.CALL_SITE_KWARGS["_SCREENER_PROMPT_TEMPLATE"])
        assert '{"plan": ..., "data": ...}' in out
        assert "{{" not in out
        assert "}}" not in out

    def test_the_screener_prompt_names_its_commands_with_real_backslashes(self):
        """A backslash in a normal Python string is an escape. `bot\\build_plan`
        written singly becomes a BACKSPACE character and the command silently
        stops matching the --allowed-tools entry -- and unlike `\\s` or `\\d`,
        `\\b` is a VALID escape, so Python does not even warn. Caught while
        rewriting this prompt on 2026-08-09."""
        out = process_queue._SCREENER_PROMPT_TEMPLATE.format(**self.CALL_SITE_KWARGS["_SCREENER_PROMPT_TEMPLATE"])
        for command in ("build_plan.py", "size_policy.py", "deliver_report.py"):
            assert f"python bot\\{command}" in out, command
        for control in ("\b", "\f", "\v", "\a"):
            assert control not in out

    def test_the_screener_prompt_delegates_the_arithmetic(self):
        """2026-08-09. The prompt was ~2,200 words, most of it telling a model
        to copy a number exactly and not change it. build_plan.py computes the
        setup, trigger, stop, targets and potential now; the prompt must point
        at it rather than re-describing how to derive them by hand."""
        out = process_queue._SCREENER_PROMPT_TEMPLATE.format(**self.CALL_SITE_KWARGS["_SCREENER_PROMPT_TEMPLATE"])
        assert "build_plan.py" in out
        assert len(out.split()) < 1200, "the prompt has grown back"
        # The override path must stay visible: a differing call needs a reason.
        assert "--setup-type" in out and "--reason" in out

    def test_the_screener_prompt_never_asks_for_a_size_multiplier(self):
        """2026-08-09. The multipliers were removed on 2026-08-03 and this
        prompt still carried "Multiply them as before and show every column,
        then CLAMP the product" three sentences after saying there were none --
        so a run got told both things at once, and `--multiplier 0.5` on the
        command line was applied for real (see size_policy._main). The prompt
        must never again instruct a derate."""
        out = process_queue._SCREENER_PROMPT_TEMPLATE.format(**self.CALL_SITE_KWARGS["_SCREENER_PROMPT_TEMPLATE"])
        assert "Multiply them as before" not in out
        assert "--multiplier <each one>" not in out
        assert "NO size multipliers" in out


class TestWhichSetupFilled:
    """2026-08-30. /filled took the Primary setup's stop every time, with a
    "verify this is right" note next to it -- so an entry that really came from
    the Alternate stored the wrong stop, and every later report, trail check and
    tranche plan was measured against a level the broker never had.

    The real NOW thesis is the shape that makes it concrete: Primary stop 105.02,
    Alternate stop 100.49. Nothing in the fill price says which one it was.
    """

    def _two_setup_thesis(self, ticker="NOW"):
        persistence.save_thesis(
            ticker=ticker, status="pending", source="SCREENER_v3",
            primary_setup={"type": "Breakout", "trigger": 113.79, "stop": 105.02,
                            "atr_at_build": 6.783,
                            "targets": [{"price": 136.63, "pct": "40", "status": "pass"}]},
            alternate_setup={"type": "Pullback", "trigger": 107.0, "stop": 100.49,
                              "atr_at_build": 6.783},
        )

    def _one_setup_thesis(self, ticker="MU"):
        persistence.save_thesis(
            ticker=ticker, status="pending", source="SCREENER_v3",
            primary_setup={"type": "Breakout", "trigger": 113.79, "stop": 105.02,
                            "atr_at_build": 6.783,
                            "targets": [{"price": 136.63, "pct": "40", "status": "pass"}]},
            alternate_setup=None,
        )

    def test_two_setups_and_no_word_records_nothing(self, temp_db, fake_send_text):
        self._two_setup_thesis()
        persistence.enqueue_message(9501, "u1", "c1", "text", "/filled NOW 115.68 138 full", None)

        process_queue._handle_filled(9501, "/filled NOW 115.68 138 full")

        assert persistence.get_open_position("NOW") is None
        assert process_queue._message_status(9501) == "failed"
        body = fake_send_text[-1]
        # both stops are named, so the answer is readable off the message itself
        assert "105.02" in body and "100.49" in body

    def test_alternate_stores_the_alternates_stop(self, temp_db, fake_send_text):
        self._two_setup_thesis()
        persistence.enqueue_message(9502, "u1", "c1", "text",
                                     "/filled NOW 115.68 138 full alternate", None)

        process_queue._handle_filled(9502, "/filled NOW 115.68 138 full alternate")

        position = persistence.get_open_position("NOW")
        assert position["initial_stop"] == 100.49
        assert position["entry_setup"]["type"] == "Pullback"
        assert "Alternate" in fake_send_text[-1]

    def test_primary_still_stores_the_primarys_stop(self, temp_db, fake_send_text):
        self._two_setup_thesis()
        persistence.enqueue_message(9503, "u1", "c1", "text",
                                     "/filled NOW 115.68 138 full primary", None)

        process_queue._handle_filled(9503, "/filled NOW 115.68 138 full primary")

        position = persistence.get_open_position("NOW")
        assert position["initial_stop"] == 105.02
        assert position["entry_setup"]["type"] == "Breakout"

    def test_one_setup_needs_no_word_at_all(self, temp_db, fake_send_text):
        # Nothing to disambiguate -- this must not become a new prompt on the
        # common case, which is most of the book.
        self._one_setup_thesis()
        persistence.enqueue_message(9504, "u1", "c1", "text", "/filled MU 115.68 138 full", None)

        process_queue._handle_filled(9504, "/filled MU 115.68 138 full")

        assert persistence.get_open_position("MU")["initial_stop"] == 105.02
        assert process_queue._message_status(9504) == "sent"

    def test_asking_for_an_alternate_that_does_not_exist_records_nothing(
        self, temp_db, fake_send_text
    ):
        self._one_setup_thesis()
        persistence.enqueue_message(9505, "u1", "c1", "text",
                                     "/filled MU 115.68 138 full alternate", None)

        process_queue._handle_filled(9505, "/filled MU 115.68 138 full alternate")

        assert persistence.get_open_position("MU") is None
        assert process_queue._message_status(9505) == "failed"


class TestPositionChartRedraw:
    """The NOW incident (2026-08-07): a held ticker's chart kept showing the
    stored THESIS -- Primary plus Alternate -- so NOW carried nine lines
    including a second, lower stop at 100.49 from a trade never taken. Only one
    of those two stop lines was real. /filled, /add and /exit now redraw the
    chart as the position it is; a full exit clears it.

    These cover the handler wiring. chart_draw's own tests cover which lines a
    position produces; the fake here records what each command sent to be drawn."""

    def _thesis_and_fill(self, ticker="NOW"):
        persistence.save_thesis(
            ticker=ticker, status="pending", source="SCREENER_v3",
            primary_setup={
                "type": "Breakout", "trigger": 113.79, "stop": 105.02, "atr_at_build": 6.783,
                "targets": [{"price": 136.63, "pct": "40", "status": "pass"}],
            },
            alternate_setup={"type": "Pullback", "trigger": 107.0, "stop": 100.49,
                              "atr_at_build": 6.783},
        )

    def test_filled_redraws_the_chart_as_a_position(self, temp_db, fake_send_text, fake_chart_draw):
        self._thesis_and_fill()
        persistence.enqueue_message(9401, "u1", "c1", "text", "/filled NOW 115.68 138 full primary", None)

        process_queue._handle_filled(9401, "/filled NOW 115.68 138 full primary")

        assert process_queue._message_status(9401) == "sent"
        assert len(fake_chart_draw) == 1
        ticker, position = fake_chart_draw[0]
        assert ticker == "NOW"
        assert position["entry_price"] == 115.68 and position["current_stop"] == 105.02

    def test_filled_draws_one_stop_not_the_dead_alternates_too(self, temp_db, fake_send_text,
                                                                fake_chart_draw):
        # The actual NOW bug, end to end: whatever gets drawn must not carry
        # the Alternate's 100.49 stop for a trade that was never entered.
        import chart_draw
        self._thesis_and_fill()
        process_queue._handle_filled(9402, "/filled NOW 115.68 138 full primary")

        lines = chart_draw._lines_for_position(fake_chart_draw[0][1])
        stops = [l for l in lines if l["text"].startswith("Stop")]
        assert len(stops) == 1 and stops[0]["price"] == 105.02
        assert 100.49 not in [l["price"] for l in lines]

    def test_add_redraws_with_the_new_average_entry(self, temp_db, fake_send_text, fake_chart_draw):
        self._thesis_and_fill()
        process_queue._handle_filled(9403, "/filled NOW 115.68 100 starter primary")
        fake_chart_draw.clear()

        process_queue._handle_add(9404, "/add NOW 117.68 100")

        assert len(fake_chart_draw) == 1
        assert fake_chart_draw[0][1]["entry_price"] == 116.68  # the blended average

    def test_partial_exit_redraws_with_the_position_that_is_left(self, temp_db, fake_send_text,
                                                                  fake_chart_draw):
        import chart_draw
        self._thesis_and_fill()
        process_queue._handle_filled(9405, "/filled NOW 115.68 138 full primary")
        fake_chart_draw.clear()

        # 55 shares at the target price -- record_exit attributes this to
        # target_1, which is what retires that tranche.
        process_queue._handle_exit(9406, "/exit NOW 136.63 55")

        assert len(fake_chart_draw) == 1
        position = fake_chart_draw[0][1]
        assert position is not None and position["remaining_qty"] == 83
        # Rule 30: the spent tranche must not come back as a live line.
        texts = [l["text"] for l in chart_draw._lines_for_position(position)]
        assert not any(t.startswith("Target 1") for t in texts)

    def test_full_exit_clears_the_chart_instead_of_leaving_ghost_lines(self, temp_db, fake_send_text,
                                                                       fake_chart_draw):
        self._thesis_and_fill()
        process_queue._handle_filled(9407, "/filled NOW 115.68 138 full primary")
        fake_chart_draw.clear()

        process_queue._handle_exit(9408, "/exit NOW 136.63 138")

        # None is the signal _draw_position_chart turns into clear_chart():
        # a closed trade leaves no lines pretending to be a live plan.
        assert fake_chart_draw == [("NOW", None)]

    def test_a_failed_draw_never_costs_the_user_the_fill(self, temp_db, fake_send_text, monkeypatch):
        """TradingView being closed is not a reason to lose a recorded fill --
        the write and the confirmation both already happened by then."""
        async def _boom(ticker, position):
            raise RuntimeError("TradingView connection lost")

        monkeypatch.setattr(process_queue, "_draw_position_chart", _boom)
        self._thesis_and_fill()
        persistence.enqueue_message(9409, "u1", "c1", "text", "/filled NOW 115.68 138 full primary", None)

        process_queue._handle_filled(9409, "/filled NOW 115.68 138 full primary")

        assert process_queue._message_status(9409) == "sent"
        assert persistence.get_open_position("NOW")["qty"] == 138
        # ...and the user is told the lines on screen are the old ones.
        assert len(fake_send_text) == 2
        assert "לא עודכנו" in fake_send_text[1]


class TestFieldShapeRetry:
    """2026-08-09. A /screener PANW run on 2026-08-06 died with

        alternate_setup.trigger must be numeric once stop is set, got '...'

    after roughly twenty minutes of real work. The refusal was CORRECT -- bad
    data must not reach the DB -- and binning the run over it was not, because
    the error message already says exactly what is wrong and what the field
    should hold. One retry, with that message handed back verbatim."""

    def _fail(self, update_id, error):
        persistence.enqueue_message(update_id, from_id="1", chat_id="1",
                                     message_type="text", message_text="/screener PANW",
                                     raw_update={})
        persistence.mark_failed(update_id, error)

    def test_a_field_shape_error_is_recognised(self, temp_db):
        self._fail(1, "alternate_setup.trigger must be numeric once stop is set, got 'wait for close'")
        assert process_queue._fixable_field_error(1) is not None

    def test_the_new_setup_type_refusal_is_recognised(self, temp_db):
        self._fail(2, "primary_setup.type must be one of ['Breakout', ...], got 120 characters of prose")
        assert process_queue._fixable_field_error(2) is not None

    def test_a_target_price_refusal_is_recognised(self, temp_db):
        self._fail(3, "primary_setup.targets[1].price must be numeric, got 'the wall top'")
        assert process_queue._fixable_field_error(3) is not None

    def test_an_unrelated_failure_is_not_retried(self, temp_db):
        # Retrying a TradingView outage or a quota exhaustion just burns the
        # cost twice for the same answer.
        self._fail(4, "screener automation did not reach a terminal state (exit=1)")
        assert process_queue._fixable_field_error(4) is None

    def test_a_sent_message_is_never_retried(self, temp_db):
        persistence.enqueue_message(5, from_id="1", chat_id="1", message_type="text",
                                     message_text="/screener PANW", raw_update={})
        persistence.mark_sent(5, [1])
        assert process_queue._fixable_field_error(5) is None

    def test_reset_puts_the_message_back_in_processing(self, temp_db):
        self._fail(6, "primary_setup.stop must be numeric, got 'below the base'")
        persistence.reset_failed_for_retry(6)
        assert process_queue._message_status(6) == "processing"
        # ...and the old error is cleared, so a retry that fails differently
        # reports its own reason rather than the previous one.
        assert process_queue._fixable_field_error(6) is None

    def test_reset_leaves_a_sent_message_alone(self, temp_db):
        persistence.enqueue_message(7, from_id="1", chat_id="1", message_type="text",
                                     message_text="/screener PANW", raw_update={})
        persistence.mark_sent(7, [1])
        persistence.reset_failed_for_retry(7)
        assert process_queue._message_status(7) == "sent"

    def test_the_correction_note_names_both_fixable_fields(self):
        note = process_queue._CORRECTION_NOTE.format(error="x")
        assert "Gap-and-Hold" in note          # the closed setup-type list
        assert "real JSON numbers" in note     # the numeric-field rule
        assert "RETRY" in note


class TestHandlePnl:
    """/pnl (2026-08-19): the broker's one blended up/down figure, taken apart
    into the Core pile and the trading pile.

    The handler's own job is small -- read, fetch prices, render, send -- so
    these tests pin the two things that are genuinely its own: the price fetch
    is asked only for tickers still held, and a dead price fetch still produces
    a real answer instead of an error message.
    """

    def _book(self):
        with persistence._db() as conn:
            for ticker in ("SPY", "AAPL", "MSFT"):
                conn.execute(
                    "INSERT INTO thesis (ticker, status, sleeve, updated_at) VALUES (?, 'open_position', 'swing', ?)",
                    (ticker, persistence._now()),
                )
        for ticker, price, qty in (("SPY", 700.0, 10), ("AAPL", 300.0, 10), ("MSFT", 400.0, 10)):
            persistence.create_position(
                ticker=ticker, entry_date="2026-06-01", entry_price=price, qty=qty,
                entry_type="full",
                entry_setup={"type": "Breakout", "stop": price * 0.9, "atr_at_build": 1.0},
                initial_stop=price * 0.9, current_stop=price * 0.9,
            )
        persistence.record_exit("MSFT", exit_price=390.0, exit_qty=10, exit_date="2026-06-10",
                                 source="exit_command")

    def test_prices_are_requested_only_for_shares_still_held(self, temp_db, fake_send_text, monkeypatch):
        asked = []

        def _fake(tickers):
            asked.append(list(tickers))
            return {t: 1.0 for t in tickers}, {}

        monkeypatch.setattr(process_queue, "_fetch_pnl_prices", _fake)
        self._book()
        process_queue._handle_pnl(9301)
        # MSFT is fully closed -- fetching a quote for it would buy nothing.
        assert asked == [["AAPL", "SPY"]]

    def test_the_two_books_are_reported_separately(self, temp_db, fake_send_text, monkeypatch):
        monkeypatch.setattr(process_queue, "_fetch_pnl_prices",
                            lambda tickers: ({"SPY": 750.0, "AAPL": 310.0}, {}))
        self._book()
        process_queue._handle_pnl(9302)
        text = fake_send_text[0]
        assert "Core" in text
        assert 'סה"כ מהמסחר: <b>$0</b>' in text      # -100 banked on MSFT, +100 open on AAPL
        assert "שניהם ביחד: <b>+$500</b>" in text     # ...so the whole sum is Core's

    def test_a_dead_price_fetch_still_answers_with_the_closed_trades(self, temp_db, fake_send_text, monkeypatch):
        """TradingView being shut must not cost the user the half of this
        report that needs no live data at all."""
        monkeypatch.setattr(process_queue, "_fetch_pnl_prices", lambda tickers: ({}, {"_": "timeout"}))
        self._book()
        process_queue._handle_pnl(9303)
        text = fake_send_text[0]
        assert "עסקאות שנסגרו (1): <b>-$100</b>" in text
        assert "⚠️" in text                            # ...and says what is missing
        assert process_queue._message_status(9303) != "failed"

    def test_an_empty_book_says_so_instead_of_showing_zeros(self, temp_db, fake_send_text, monkeypatch):
        monkeypatch.setattr(process_queue, "_fetch_pnl_prices", lambda tickers: ({}, {}))
        process_queue._handle_pnl(9304)
        assert "אין עדיין שום פוזיציה רשומה" in fake_send_text[0]

    def test_the_command_is_wired_into_the_dispatcher(self, temp_db, fake_send_text, monkeypatch):
        called = []
        monkeypatch.setattr(process_queue, "_handle_pnl", lambda update_id: called.append(update_id) or True)
        process_queue._dispatch({"update_id": 9305, "message_type": "text", "message_text": "/pnl"})
        assert called == [9305]
