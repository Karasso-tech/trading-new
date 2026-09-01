"""Unit tests for ack_listener.py's pure auth/routing logic (2026-07-16).

Found in review: ack_listener.py is the single entrypoint for every Telegram
command in the system, auth check included, yet had zero test coverage -- a
bug here means either a stranger's message gets treated as the owner's, or the
owner's own commands silently get dropped. Only the pure functions are tested
here; run_forever() itself does real network I/O (get_updates) and isn't
appropriate to exercise in a unit test.
"""

import ack_listener
from ack_listener import _ack_text_for, _instant_reply_for, _is_authorized


class TestIsAuthorized:
    def setup_method(self):
        self._orig = ack_listener.ALLOWED_USER_ID
        ack_listener.ALLOWED_USER_ID = "12345"

    def teardown_method(self):
        ack_listener.ALLOWED_USER_ID = self._orig

    @staticmethod
    def _update(from_id, chat_id):
        return {"message": {"from": {"id": from_id}, "chat": {"id": chat_id}}}

    def test_matching_user_and_chat_id_is_authorized(self):
        assert _is_authorized(self._update(12345, 12345)) is True

    def test_mismatched_from_id_is_rejected(self):
        assert _is_authorized(self._update(99999, 12345)) is False

    def test_mismatched_chat_id_is_rejected(self):
        assert _is_authorized(self._update(12345, 99999)) is False

    def test_missing_message_fields_are_rejected_not_a_crash(self):
        assert _is_authorized({"message": {}}) is False
        assert _is_authorized({}) is False


class TestInstantReplyFor:
    def test_help_and_start_return_help_text(self):
        assert _instant_reply_for("/help") == ack_listener.HELP_TEXT
        assert _instant_reply_for("/start") == ack_listener.HELP_TEXT
        assert _instant_reply_for("/HELP") == ack_listener.HELP_TEXT  # case-insensitive

    def test_not_yet_wired_command_returns_a_reply(self):
        for cmd in ack_listener.NOT_YET_WIRED:
            result = _instant_reply_for(cmd)
            assert result is not None
            assert result != ack_listener.HELP_TEXT

    def test_normal_command_returns_none_so_it_gets_queued(self):
        assert _instant_reply_for("/screener AAPL") is None
        assert _instant_reply_for("/pending") is None

    def test_empty_or_blank_text_returns_none(self):
        assert _instant_reply_for("") is None
        assert _instant_reply_for("   ") is None


class TestAckTextFor:
    def test_photo_message_gets_the_photo_specific_ack(self):
        update = {"message": {"photo": [{"file_id": "abc"}]}}
        result = _ack_text_for(update)
        assert "תמונה" in result

    def test_pending_gets_a_distinct_ack(self):
        exact = _ack_text_for({"message": {"text": "/pending"}})
        with_args = _ack_text_for({"message": {"text": "/pending extra"}})
        generic = _ack_text_for({"message": {"text": "/screener AAPL"}})
        assert exact != generic
        assert with_args != generic
        assert exact == with_args

    def test_normal_command_gets_the_generic_ack_echoing_the_text(self):
        result = _ack_text_for({"message": {"text": "/screener AAPL"}})
        assert "AAPL" in result
