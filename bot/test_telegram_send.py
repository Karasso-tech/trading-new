"""Unit tests for telegram_send.py's message-length chunking (2026-07-14) --
found real: /pending with 12 tickers produced a 5757-char message and failed
outright against Telegram's real 4096-char cap with zero visibility. Only
_split_for_telegram is tested directly (pure function, no network); send_text
itself is exercised indirectly by every bot script already, and hitting the
real Telegram API isn't appropriate for a unit test."""

from unittest.mock import MagicMock

import pytest
import requests

import telegram_send
from telegram_send import _request_with_retry, _scrub_token, _split_for_telegram, _TELEGRAM_TEXT_LIMIT


def test_short_text_is_a_single_chunk():
    text = "short message"
    assert _split_for_telegram(text) == [text]


def test_long_text_splits_on_line_boundaries():
    # 200 lines of 30 chars each = ~6000 chars, well over the limit
    lines = [f"line {i:03d} " + "x" * 20 for i in range(200)]
    text = "\n".join(lines)
    chunks = _split_for_telegram(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= _TELEGRAM_TEXT_LIMIT
    # every original line must survive, in order, none dropped or merged wrong
    rejoined = "\n".join(chunks).split("\n")
    assert rejoined == lines


def test_chunk_never_splits_a_single_line_unless_the_line_itself_is_too_long():
    # A "card" (multi-line block for one ticker) must never be split mid-card
    # by ending up broken across chunks at the wrong newline.
    card = "header\n" + "detail line\n" * 5
    cards = [card] * 300  # force multiple chunks
    text = "\n\n".join(cards)
    chunks = _split_for_telegram(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= _TELEGRAM_TEXT_LIMIT


def test_pathological_single_line_over_limit_is_hard_sliced_not_left_whole():
    line = "x" * (_TELEGRAM_TEXT_LIMIT + 500)
    chunks = _split_for_telegram(line)
    assert len(chunks) == 2
    assert all(len(c) <= _TELEGRAM_TEXT_LIMIT for c in chunks)
    assert "".join(chunks) == line


def test_real_pending_sized_message_splits_under_limit():
    # Mirrors the actual real failure: 12 ticker cards, ~5700 chars total.
    card = (
        "🎯 <b>TICKER</b> · Grade B · ימי Pending: 3\n"
        "<b>Primary:</b> Retest\n"
        "   טריגר: 625.37\n"
        "   סטופ: 610.2\n"
        "   יעד: 655.0 (40% · R:R 4.15)\n"
        "   יעד: 670.0 (35% · R:R 2.5)"
    )
    text = "\n\n".join([card] * 30)
    assert len(text) > 4096  # confirms this reproduces the real over-limit case
    chunks = _split_for_telegram(text)
    assert all(len(c) <= _TELEGRAM_TEXT_LIMIT for c in chunks)


class TestRequestWithRetry:
    """_request_with_retry (2026-07-16): retries a transient network failure up
    to _MAX_ATTEMPTS times, never retries a clean 4xx. Patches time.sleep so
    these tests don't actually wait out the real backoff."""

    def _ok_response(self):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        return resp

    def test_succeeds_on_first_try_with_no_retry(self, monkeypatch):
        monkeypatch.setattr(telegram_send.time, "sleep", lambda s: None)
        method = MagicMock(return_value=self._ok_response())
        resp = _request_with_retry(method, "https://example.invalid")
        assert resp is method.return_value
        assert method.call_count == 1

    def test_retries_transient_failure_then_succeeds(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(telegram_send.time, "sleep", lambda s: sleeps.append(s))
        ok = self._ok_response()
        method = MagicMock(side_effect=[
            requests.exceptions.ConnectionError("blip"),
            requests.exceptions.ConnectionError("blip again"),
            ok,
        ])
        resp = _request_with_retry(method, "https://example.invalid")
        assert resp is ok
        assert method.call_count == 3
        assert sleeps == [2, 5]  # backoff before attempt 2 and attempt 3

    def test_raises_after_exhausting_all_retries(self, monkeypatch):
        monkeypatch.setattr(telegram_send.time, "sleep", lambda s: None)
        method = MagicMock(side_effect=requests.exceptions.ConnectionError("still down"))
        with pytest.raises(requests.exceptions.ConnectionError):
            _request_with_retry(method, "https://example.invalid")
        assert method.call_count == 3

    def test_client_error_4xx_is_never_retried(self, monkeypatch):
        monkeypatch.setattr(telegram_send.time, "sleep", lambda s: None)
        bad_resp = MagicMock()
        bad_resp.status_code = 400
        http_error = requests.exceptions.HTTPError(response=bad_resp)
        bad_resp.raise_for_status.side_effect = http_error
        method = MagicMock(return_value=bad_resp)
        with pytest.raises(requests.exceptions.HTTPError):
            _request_with_retry(method, "https://example.invalid")
        assert method.call_count == 1  # no retry -- a real client error, not transient

    def test_server_error_5xx_is_retried(self, monkeypatch):
        monkeypatch.setattr(telegram_send.time, "sleep", lambda s: None)
        bad_resp = MagicMock()
        bad_resp.status_code = 500
        http_error = requests.exceptions.HTTPError(response=bad_resp)
        bad_resp.raise_for_status.side_effect = http_error
        method = MagicMock(return_value=bad_resp)
        with pytest.raises(requests.exceptions.HTTPError):
            _request_with_retry(method, "https://example.invalid")
        assert method.call_count == 3  # transient-looking -- retried to the cap

    def test_rate_limit_429_is_retried_not_treated_as_fatal(self, monkeypatch):
        """2026-07-30 full-system checkup: 429 used to fall into the generic
        4xx "never retry" bucket even though Telegram's own 429 means "you're
        sending too fast, try again shortly" -- the definition of transient."""
        monkeypatch.setattr(telegram_send.time, "sleep", lambda s: None)
        bad_resp = MagicMock()
        bad_resp.status_code = 429
        http_error = requests.exceptions.HTTPError(response=bad_resp)
        bad_resp.raise_for_status.side_effect = http_error
        method = MagicMock(return_value=bad_resp)
        with pytest.raises(requests.exceptions.HTTPError):
            _request_with_retry(method, "https://example.invalid")
        assert method.call_count == 3  # treated as transient, retried to the cap


class TestScrubToken:
    """2026-07-30 full-system checkup: every URL this module builds embeds the
    live bot token as /bot{token}/... -- an uncaught exception's message must
    never leak it into a log file in plain text."""

    def test_token_removed_from_exception_message(self):
        exc = requests.exceptions.HTTPError(
            "400 Client Error: Bad Request for url: "
            "https://api.telegram.org/bot123456:ABC-DEF_ghijk/sendMessage"
        )
        _scrub_token(exc)
        assert "123456:ABC-DEF_ghijk" not in str(exc)
        assert "/bot***/sendMessage" in str(exc)

    def test_exception_with_no_message_does_not_crash(self):
        exc = requests.exceptions.HTTPError(response=MagicMock(status_code=400))
        _scrub_token(exc)  # must not raise even though .args is empty


class TestAttachmentSizeGuard:
    """2026-07-30 full-system checkup: send_photo/send_document used to attempt
    the real network call regardless of size -- a too-large file just failed
    against Telegram's real API with no advance warning, sometimes after an
    earlier attachment in the same report had already sent (confusing partial
    delivery). Checked BEFORE any network call, real credentials never needed."""

    def test_oversized_photo_rejected_before_network_call(self, monkeypatch):
        monkeypatch.setattr(telegram_send, "_load_credentials", lambda: ("t", "c"))
        called = []
        monkeypatch.setattr(telegram_send, "_request_with_retry", lambda *a, **kw: called.append(1))
        resp = telegram_send.send_photo(b"x" * (telegram_send._MAX_PHOTO_BYTES + 1))
        assert resp["ok"] is False
        assert not called  # never attempted the real send

    def test_oversized_document_rejected_before_network_call(self, monkeypatch):
        monkeypatch.setattr(telegram_send, "_load_credentials", lambda: ("t", "c"))
        called = []
        monkeypatch.setattr(telegram_send, "_request_with_retry", lambda *a, **kw: called.append(1))
        resp = telegram_send.send_document(b"x" * (telegram_send._MAX_DOCUMENT_BYTES + 1), "report.pdf")
        assert resp["ok"] is False
        assert not called

    def test_normal_sized_photo_not_blocked(self, monkeypatch):
        monkeypatch.setattr(telegram_send, "_load_credentials", lambda: ("t", "c"))
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
        monkeypatch.setattr(telegram_send, "_request_with_retry", lambda *a, **kw: fake_resp)
        resp = telegram_send.send_photo(b"small png bytes")
        assert resp["ok"] is True
