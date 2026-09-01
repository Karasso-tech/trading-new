"""Unit tests for fetch_x_feed.py's pure normalization helpers (2026-07-22):
cashtag extraction and Twitter's classic date format -> this project's ISO-8601
UTC convention. No network/Apify calls -- _fetch_tweets() itself is a thin,
untested HTTP wrapper, same posture as fetch_analysis_data.py's TVClient calls
not being unit-tested either; only the deterministic logic around it is.

TestClassifySentiment mocks subprocess.Popen -- no real `claude -p` process is
spawned by this suite.
"""

import json
import subprocess

import fetch_x_feed as fxf


class TestExtractTickers:
    def test_extracts_single_cashtag(self):
        assert fxf._extract_tickers("Watching $NVDA break out today") == ["NVDA"]

    def test_extracts_multiple_cashtags_sorted_and_deduped(self):
        assert fxf._extract_tickers("$tsla and $NVDA and $tsla again") == ["NVDA", "TSLA"]

    def test_no_cashtags_returns_empty_list(self):
        assert fxf._extract_tickers("Just talking about the Fed today") == []

    def test_dollar_amount_without_letters_is_not_a_cashtag(self):
        assert fxf._extract_tickers("Bought at $150 today") == []

    def test_none_text_returns_empty_list(self):
        assert fxf._extract_tickers(None) == []


class TestParsePostedAt:
    def test_parses_twitter_format_to_iso_utc(self):
        result = fxf._parse_posted_at("Wed Jul 22 03:43:01 +0000 2026")
        assert result == "2026-07-22T03:43:01+00:00"

    def test_non_utc_offset_is_converted_to_utc(self):
        result = fxf._parse_posted_at("Wed Jul 22 03:43:01 -0400 2026")
        assert result == "2026-07-22T07:43:01+00:00"


class TestFormatCandidatesMessage:
    """The idea-sourcing alert's message body (2026-07-22) -- pure formatting,
    no network. Real sending/marking-alerted is exercised in
    test_persistence.py's TestXCandidateAlerts, not here."""

    def _candidate(self, ticker="GEV", account="StockMKTNewz", text="text",
                    url="https://x.com/x/status/1"):
        return {"ticker": ticker, "account": account, "posted_at": "2026-07-22T10:00:00+00:00",
                "text": text, "url": url}

    def test_includes_ticker_account_and_url(self):
        msg = fxf._format_candidates_message([self._candidate()])
        assert "$GEV" in msg
        assert "@StockMKTNewz" in msg
        assert "https://x.com/x/status/1" in msg

    def test_includes_the_posted_date(self):
        msg = fxf._format_candidates_message([self._candidate()])
        assert "2026-07-22 10:00 UTC" in msg

    def test_multiple_candidates_all_appear(self):
        msg = fxf._format_candidates_message([self._candidate(ticker="GEV"), self._candidate(ticker="NVDA")])
        assert "$GEV" in msg
        assert "$NVDA" in msg

    def test_html_special_characters_are_escaped(self):
        msg = fxf._format_candidates_message([self._candidate(text="AT&T beat < estimates >")])
        assert "AT&amp;T" in msg
        assert "&lt;" in msg and "&gt;" in msg

    def test_long_tweet_text_is_truncated(self):
        msg = fxf._format_candidates_message([self._candidate(text="x" * 500)])
        assert "x" * (fxf._SNIPPET_MAX_CHARS + 1) not in msg
        assert "..." in msg

    def test_newlines_in_tweet_text_are_collapsed(self):
        msg = fxf._format_candidates_message([self._candidate(text="line one\nline two")])
        assert "line one line two" in msg


class _FakeProc:
    """Stands in for subprocess.Popen's return value -- .communicate() returns
    canned stdout, matching claude -p's --output-format json shape."""

    def __init__(self, combined):
        self._combined = combined
        self.pid = 4242

    def communicate(self, input=None, timeout=None):
        return self._combined, None


class _TimeoutProc(_FakeProc):
    def communicate(self, input=None, timeout=None):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)


class TestClassifySentiment:
    """fetch_x_feed.py's sentiment filter (2026-07-22) -- invokes claude -p,
    never the Anthropic API SDK (see module docstring). All tests here mock
    subprocess.Popen; no real claude -p process runs."""

    def _claude_stdout(self, classifications):
        result_json = json.dumps({"classifications": classifications})
        return json.dumps({"result": result_json})

    def _candidate(self, ticker="NVDA", account="charliebilello", text="text"):
        return {"ticker": ticker, "account": account, "text": text}

    def test_empty_candidates_returns_empty_without_invoking_claude(self, monkeypatch):
        calls = []
        monkeypatch.setattr(fxf.subprocess, "Popen", lambda *a, **k: calls.append(1))
        assert fxf._classify_sentiment([]) == {}
        assert calls == []

    def test_parses_positive_and_negative_classifications(self, monkeypatch):
        stdout = self._claude_stdout([
            {"ticker": "NVDA", "sentiment": "positive"},
            {"ticker": "GEV", "sentiment": "negative"},
        ])
        monkeypatch.setattr(fxf.subprocess, "Popen", lambda *a, **k: _FakeProc(stdout))
        result = fxf._classify_sentiment([self._candidate(ticker="NVDA"), self._candidate(ticker="GEV")])
        assert result == {"NVDA": "positive", "GEV": "negative"}

    def test_ticker_lookup_is_uppercased(self, monkeypatch):
        stdout = self._claude_stdout([{"ticker": "nvda", "sentiment": "positive"}])
        monkeypatch.setattr(fxf.subprocess, "Popen", lambda *a, **k: _FakeProc(stdout))
        result = fxf._classify_sentiment([self._candidate(ticker="NVDA")])
        assert result == {"NVDA": "positive"}

    def test_malformed_json_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(fxf.subprocess, "Popen", lambda *a, **k: _FakeProc("not json"))
        assert fxf._classify_sentiment([self._candidate()]) == {}

    def test_missing_result_key_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(fxf.subprocess, "Popen", lambda *a, **k: _FakeProc(json.dumps({"no_result": True})))
        assert fxf._classify_sentiment([self._candidate()]) == {}

    def test_timeout_kills_process_tree_and_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(fxf.subprocess, "Popen", lambda *a, **k: _TimeoutProc(""))
        killed = []
        monkeypatch.setattr(fxf.subprocess, "run", lambda *a, **k: killed.append(a))
        assert fxf._classify_sentiment([self._candidate()]) == {}
        assert len(killed) == 1  # taskkill was invoked on the orphaned process tree
