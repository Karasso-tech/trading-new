"""Unit tests for the late post-close scan's stand-down rules (2026-08-08).

The night's real /monitorall is now chained off the end of refresh_pending.py so
the last list the user reads is built from the refreshed theses. The scheduled
job that used to own that scan survives only as a safety net, and getting its
stand-down wrong is expensive in both directions: too eager and the user gets
two identical 20-minute scans a night, too shy and a refresh that died leaves
the night with no scan at all.

Pure logic over monkeypatched lookups -- no DB, no NYSE calendar, no fetch.
"""

from datetime import datetime, timezone

import pytest

import trigger_auto_monitor as tam

CLOSE = datetime(2026, 8, 7, 20, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def quiet_night(monkeypatch):
    """Default: today was a session, no scan has run, nothing queued."""
    monkeypatch.setattr(tam, "_todays_close_utc", lambda: CLOSE)
    monkeypatch.setattr(tam.persistence, "monitorall_ran_since", lambda since: False)
    monkeypatch.setattr(tam.persistence, "tickers_already_queued_for_screener", lambda: set())


def test_runs_when_the_night_produced_no_scan():
    # The whole reason the fallback exists: the refresh died before it could
    # chain its own scan, so nobody has looked at the list since the close.
    assert tam._fallback_should_stand_down() is False


def test_stands_down_when_the_chained_scan_already_ran(monkeypatch):
    monkeypatch.setattr(tam.persistence, "monitorall_ran_since", lambda since: True)
    assert tam._fallback_should_stand_down() is True


def test_the_since_edge_is_todays_close(monkeypatch):
    # A scan from YESTERDAY evening must not count as tonight's -- so the
    # question asked has to be anchored to today's close, not to "ever".
    asked = []
    monkeypatch.setattr(tam.persistence, "monitorall_ran_since",
                        lambda since: asked.append(since) or False)
    tam._fallback_should_stand_down()
    assert asked == [CLOSE.isoformat()]


def test_stands_down_while_the_refresh_is_still_rebuilding(monkeypatch):
    # Scanning mid-refresh would read a half-rewritten list AND make the
    # refresh's own chained scan look like a duplicate, so the user's last list
    # of the night would be the mid-refresh one.
    monkeypatch.setattr(tam.persistence, "tickers_already_queued_for_screener",
                        lambda: {"NVDA", "PLTR"})
    assert tam._fallback_should_stand_down() is True


def test_no_session_today_still_checks_the_queue(monkeypatch):
    # Weekend/holiday: there is no close to measure from. The market gate in
    # main() already blocks the run, but this must not crash or wave it through
    # on the strength of an un-askable question.
    monkeypatch.setattr(tam, "_todays_close_utc", lambda: None)
    monkeypatch.setattr(tam.persistence, "tickers_already_queued_for_screener",
                        lambda: {"NVDA"})
    assert tam._fallback_should_stand_down() is True
