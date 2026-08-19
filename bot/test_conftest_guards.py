"""Tests for the two safety nets in conftest.py itself (2026-08-03).

These guard the guards. Both fixtures there are autouse and invisible -- a
future edit that quietly drops one would break nothing and fail nothing, and
the first sign would be real Telegram messages arriving on the user's phone
from a `pytest` run again. See conftest.py's docstring for the incident.
"""

import requests

import deliver_position_status_report as dpsr
import telegram_send


class TestNetworkIsBlocked:
    def test_module_level_post_raises_instead_of_sending(self):
        try:
            requests.post("https://api.telegram.org/botFAKE/sendMessage", json={})
        except RuntimeError as e:
            assert "blocked a real network call" in str(e)
        else:
            raise AssertionError("requests.post reached the network from a test")

    def test_module_level_get_raises_instead_of_fetching(self):
        try:
            requests.get("https://example.invalid/")
        except RuntimeError as e:
            assert "blocked a real network call" in str(e)
        else:
            raise AssertionError("requests.get reached the network from a test")

    def test_a_session_of_its_own_is_blocked_too(self):
        try:
            requests.sessions.Session().get("https://example.invalid/")
        except RuntimeError as e:
            assert "blocked a real network call" in str(e)
        else:
            raise AssertionError("Session.request reached the network from a test")


class TestFailureAlertsAreSilenced:
    """The specific call that spammed the real chat. Checked on a deliver_*
    module, not just telegram_send: each one holds its own imported reference,
    which is exactly why patching telegram_send alone would not be enough."""

    def test_deliver_module_alert_is_a_no_op(self):
        assert dpsr.send_failure_alert("test context", "test error") is None

    def test_source_module_alert_is_a_no_op(self):
        assert telegram_send.send_failure_alert("test context", "test error") is None
