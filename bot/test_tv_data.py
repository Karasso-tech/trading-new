"""Unit tests for tv_data.py's assert_data_fresh() -- Hardening Pass item 6.

Uses the real NYSE calendar (via a throwaway probe call) to find "the most recent
complete session" as of whenever the test actually runs, rather than hardcoding a
date -- weekend/holiday handling must come from pandas_market_calendars, not from
naive weekday arithmetic (the exact bug _session_open_utc_ts was already fixed for
once; assert_data_fresh reuses that same calendar-based approach)."""

import asyncio
from datetime import datetime, timezone

import pandas_market_calendars as mcal
import pytest

import tv_data
from tv_data import BadSymbol, TVClient, assert_data_fresh

_NYSE = mcal.get_calendar("NYSE")


def _bar_on(date_iso: str) -> dict:
    ts = datetime.fromisoformat(date_iso).replace(tzinfo=timezone.utc).timestamp()
    return {"time": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1}


def test_bars_ending_on_most_recent_complete_session_are_fresh():
    target = assert_data_fresh([]).most_recent_complete_session  # empty-bars probe still computes this
    result = assert_data_fresh([_bar_on(target)])
    assert result.fresh
    assert result.last_bar_date == target


def test_bars_ending_three_sessions_ago_are_stale():
    target = assert_data_fresh([]).most_recent_complete_session
    schedule = _NYSE.schedule(start_date="2020-01-01", end_date=target)
    stale_date = schedule.index[-4].date().isoformat()  # 3 real NYSE sessions before target
    result = assert_data_fresh([_bar_on(stale_date)])
    assert not result.fresh
    assert result.last_bar_date == stale_date


def test_empty_bars_are_stale_not_fresh():
    result = assert_data_fresh([])
    assert not result.fresh
    assert result.last_bar_date is None
    assert result.most_recent_complete_session is not None


def test_bars_one_session_ahead_are_still_fresh():
    # A bar dated one real trading session AFTER "most recent complete" shouldn't
    # happen in practice, but the >= comparison must not treat it as stale.
    target = assert_data_fresh([]).most_recent_complete_session
    schedule = _NYSE.schedule(start_date=target, end_date="2035-12-31")
    if len(schedule) >= 2:
        next_session = schedule.index[1].date().isoformat()
        result = assert_data_fresh([_bar_on(next_session)])
        assert result.fresh


# ---------------------------------------------------------------------------
# _set_symbol -- found real, 2026-07-13: a /monitorall run got byte-for-byte
# identical current_price/bars data for 4 different tickers because chart_ready
# reported true (a false positive) and the old code trusted it with zero
# verification. Fix: always cross-check chart_get_state()'s own symbol field,
# on both the chart_ready=True and chart_ready=False paths.
# ---------------------------------------------------------------------------

def _client_with_fake_call(responses: dict, *, starting_symbol: str = "AMEX:XLE") -> TVClient:
    """`responses` maps tool name -> fixed response. chart_get_state is special-cased:
    the FIRST call reports `starting_symbol` (whatever the chart was already on before
    _set_symbol ran) and every later call reports responses["chart_get_state"] (what
    the chart claims after the switch). Without that distinction every test would hit
    _set_symbol's already-on-this-symbol shortcut and verify nothing (2026-08-04)."""
    client = TVClient()
    calls: list[str] = []

    async def fake_call(tool, **kwargs):
        calls.append(tool)
        if tool == "chart_get_state" and calls.count("chart_get_state") == 1:
            return {"symbol": starting_symbol}
        return responses[tool]

    client._call = fake_call
    client.calls = calls  # so tests can assert a switch did/didn't happen
    return client


def test_set_symbol_chart_ready_true_and_state_matches_succeeds():
    client = _client_with_fake_call({
        "chart_set_symbol": {"chart_ready": True},
        "chart_get_state": {"symbol": "NASDAQ:AAPL"},
    })
    asyncio.run(client._set_symbol("AAPL"))  # must not raise
    assert "chart_set_symbol" in client.calls


def test_set_symbol_chart_ready_true_but_state_mismatch_raises():
    # The exact real failure shape: chart_ready claimed success but the chart
    # was actually still showing a different (prior) symbol.
    client = _client_with_fake_call({
        "chart_set_symbol": {"chart_ready": True},
        "chart_get_state": {"symbol": "AMEX:SPY"},
    })
    with pytest.raises(BadSymbol):
        asyncio.run(client._set_symbol("CRM"))


def test_set_symbol_chart_ready_false_but_state_matches_succeeds():
    client = _client_with_fake_call({
        "chart_set_symbol": {"chart_ready": False},
        "chart_get_state": {"symbol": "NASDAQ:AAPL"},
    })
    asyncio.run(client._set_symbol("AAPL"))  # must not raise -- slow/cold chart, not a bad symbol


def test_set_symbol_chart_ready_false_and_state_mismatch_raises():
    client = _client_with_fake_call({
        "chart_set_symbol": {"chart_ready": False},
        "chart_get_state": {"symbol": "AMEX:SPY"},
    })
    with pytest.raises(BadSymbol):
        asyncio.run(client._set_symbol("NOTAREALTICKER"))


# ---------------------------------------------------------------------------
# Already-on-this-symbol shortcut (2026-08-04) -- fetch_monitor_data.py reads
# four things off one ticker (2h, 30m, daily, quote) and every public method
# calls _set_symbol, so three of those four switches changed nothing while
# costing a real chart round-trip each.
# ---------------------------------------------------------------------------

def test_set_symbol_skips_the_switch_when_chart_is_already_on_that_symbol():
    client = _client_with_fake_call({"chart_get_state": {"symbol": "BATS:NVDA"}},
                                     starting_symbol="BATS:NVDA")
    asyncio.run(client._set_symbol("NVDA"))
    assert "chart_set_symbol" not in client.calls  # no switch, no wait


def test_set_symbol_shortcut_accepts_an_exchange_prefixed_request():
    client = _client_with_fake_call({"chart_get_state": {"symbol": "BATS:NVDA"}},
                                     starting_symbol="BATS:NVDA")
    asyncio.run(client._set_symbol("BATS:NVDA"))
    assert "chart_set_symbol" not in client.calls


def test_set_symbol_shortcut_is_strict_and_does_not_treat_gme_as_gm():
    """The verification path below deliberately uses a LOOSE substring test, which
    would accept a chart sitting on GME as "already showing GM" -- and the shortcut
    would then hand back GME's bars under GM's name, the exact 2026-07-13 bug. The
    shortcut must compare strictly, so this still performs a real switch."""
    client = _client_with_fake_call({
        "chart_set_symbol": {"chart_ready": True},
        "chart_get_state": {"symbol": "NYSE:GM"},
    }, starting_symbol="NYSE:GME")
    asyncio.run(client._set_symbol("GM"))
    assert "chart_set_symbol" in client.calls


# ---------------------------------------------------------------------------
# get_index_bars -- 2026-07-21: fetch_analysis_data.py/fetch_monitor_data.py each
# refetch the identical SPY/QQQ history once per ticker in a batch run (e.g. 6-7x
# per /playbook run), each paying a real ~10-15s CDP round-trip for no new data.
# Disk-cached (not in-process) since each ticker is its own subprocess.
# ---------------------------------------------------------------------------

class _CountingClient:
    """Fake TVClient that only implements get_daily_history, counting calls."""

    def __init__(self, bars: list[dict]):
        self.bars = bars
        self.calls = 0

    async def get_daily_history(self, symbol: str, years: float = 1) -> list[dict]:
        self.calls += 1
        return self.bars


@pytest.fixture(autouse=True)
def _isolated_index_cache(tmp_path, monkeypatch):
    # Every get_index_bars test gets its own throwaway cache file so tests never
    # read/write the real project-root cache and never leak state between tests.
    monkeypatch.setattr(tv_data, "INDEX_CACHE_PATH", tmp_path / "_index_bars_cache.json")


def test_get_index_bars_cold_cache_fetches_and_caches():
    client = _CountingClient([{"time": 1, "close": 1.0}])
    bars = asyncio.run(tv_data.get_index_bars(client, "SPY", years=1))
    assert bars == client.bars
    assert client.calls == 1


def test_get_index_bars_second_call_within_ttl_reuses_cache():
    client = _CountingClient([{"time": 1, "close": 1.0}])
    asyncio.run(tv_data.get_index_bars(client, "SPY", years=1))
    bars = asyncio.run(tv_data.get_index_bars(client, "SPY", years=1))
    assert bars == client.bars
    assert client.calls == 1  # second call served from disk cache, no new fetch


def test_get_index_bars_different_symbol_is_a_separate_cache_key():
    client = _CountingClient([{"time": 1, "close": 1.0}])
    asyncio.run(tv_data.get_index_bars(client, "SPY", years=1))
    asyncio.run(tv_data.get_index_bars(client, "QQQ", years=1))
    assert client.calls == 2  # SPY's cached entry must never be served for a QQQ request


def test_get_index_bars_expired_cache_refetches():
    client = _CountingClient([{"time": 1, "close": 1.0}])
    asyncio.run(tv_data.get_index_bars(client, "SPY", years=1))
    cache = tv_data._load_index_cache()
    cache["SPY:1"]["fetched_at"] -= tv_data.INDEX_CACHE_TTL_SECONDS + 1  # simulate 30+ min elapsed
    tv_data._save_index_cache(cache)
    asyncio.run(tv_data.get_index_bars(client, "SPY", years=1))
    assert client.calls == 2


def test_get_index_bars_corrupt_cache_file_is_a_miss_not_a_crash():
    tv_data.INDEX_CACHE_PATH.write_text("{not valid json", encoding="utf-8")
    client = _CountingClient([{"time": 1, "close": 1.0}])
    bars = asyncio.run(tv_data.get_index_bars(client, "SPY", years=1))  # must not raise
    assert bars == client.bars


# ---------------------------------------------------------------------------
# get_index_quote -- 2026-08-04: fetch_monitor_data.py fetched a fresh SPY quote
# for EVERY ticker in a /monitorall run (20+ fetches of one number, each costing
# a chart switch away from the ticker being analysed and back). Same disk cache
# as get_index_bars, shorter TTL because this one is a live price.
# ---------------------------------------------------------------------------

class _CountingQuoteClient:
    def __init__(self, quote: dict):
        self.quote = quote
        self.calls = 0

    async def get_quote(self, symbol: str) -> dict:
        self.calls += 1
        return self.quote


def test_get_index_quote_cold_cache_fetches_and_reports_zero_age():
    client = _CountingQuoteClient({"close": 700.0})
    quote, age = asyncio.run(tv_data.get_index_quote(client, "SPY"))
    assert quote == client.quote
    assert client.calls == 1
    assert age == 0  # 0 means "this call fetched it", never "cached but unknown age"


def test_get_index_quote_second_call_within_ttl_reuses_cache():
    client = _CountingQuoteClient({"close": 700.0})
    asyncio.run(tv_data.get_index_quote(client, "SPY"))
    quote, _age = asyncio.run(tv_data.get_index_quote(client, "SPY"))
    assert quote == client.quote
    assert client.calls == 1


def test_get_index_quote_reports_real_age_when_served_from_cache():
    client = _CountingQuoteClient({"close": 700.0})
    asyncio.run(tv_data.get_index_quote(client, "SPY"))
    cache = tv_data._load_index_cache()
    cache["quote:SPY"]["fetched_at"] -= 120  # two minutes ago
    tv_data._save_index_cache(cache)
    _quote, age = asyncio.run(tv_data.get_index_quote(client, "SPY"))
    assert client.calls == 1
    assert 119 <= age <= 122  # the caller must be able to see it is NOT live


def test_get_index_quote_expired_cache_refetches():
    client = _CountingQuoteClient({"close": 700.0})
    asyncio.run(tv_data.get_index_quote(client, "SPY"))
    cache = tv_data._load_index_cache()
    cache["quote:SPY"]["fetched_at"] -= tv_data.INDEX_QUOTE_CACHE_TTL_SECONDS + 1
    tv_data._save_index_cache(cache)
    _quote, age = asyncio.run(tv_data.get_index_quote(client, "SPY"))
    assert client.calls == 2
    assert age == 0


# ---------------------------------------------------------------------------
# get_economic_calendar_cached -- 2026-08-04: the CPI/PPI/NFP/FOMC list does not
# depend on the ticker at all, yet every ticker in a batch refetched it.
# ---------------------------------------------------------------------------

class _CountingCalendarClient:
    def __init__(self, events: list[dict]):
        self.events = events
        self.calls = 0

    async def get_economic_calendar(self, days_ahead: int = 7) -> list[dict]:
        self.calls += 1
        return self.events


def test_economic_calendar_second_call_within_ttl_reuses_cache():
    client = _CountingCalendarClient([{"event": "CPI", "date": "2026-08-12"}])
    first = asyncio.run(tv_data.get_economic_calendar_cached(client, days_ahead=10))
    second = asyncio.run(tv_data.get_economic_calendar_cached(client, days_ahead=10))
    assert first == second == client.events
    assert client.calls == 1


def test_economic_calendar_different_window_is_a_separate_cache_key():
    client = _CountingCalendarClient([{"event": "CPI"}])
    asyncio.run(tv_data.get_economic_calendar_cached(client, days_ahead=10))
    asyncio.run(tv_data.get_economic_calendar_cached(client, days_ahead=3))
    assert client.calls == 2  # a 3-day window must never be served a 10-day answer


def test_economic_calendar_expired_cache_refetches():
    client = _CountingCalendarClient([{"event": "CPI"}])
    asyncio.run(tv_data.get_economic_calendar_cached(client, days_ahead=10))
    cache = tv_data._load_index_cache()
    cache["econ:10"]["fetched_at"] -= tv_data.INDEX_CACHE_TTL_SECONDS + 1
    tv_data._save_index_cache(cache)
    asyncio.run(tv_data.get_economic_calendar_cached(client, days_ahead=10))
    assert client.calls == 2


def test_economic_calendar_empty_list_is_cached_not_treated_as_a_miss():
    """An empty list is a real answer -- "no CPI/NFP/FOMC in the next 10 days" is
    itself the disclosed check the rules ask for, not missing data. Caching it
    must not fall through and refetch every time."""
    client = _CountingCalendarClient([])
    assert asyncio.run(tv_data.get_economic_calendar_cached(client, days_ahead=10)) == []
    assert asyncio.run(tv_data.get_economic_calendar_cached(client, days_ahead=10)) == []
    assert client.calls == 1


def test_all_three_index_cache_kinds_coexist_in_one_file():
    """bars, quote and econ share one cache file -- their keys must not collide."""
    bars_c = _CountingClient([{"time": 1, "close": 1.0}])
    quote_c = _CountingQuoteClient({"close": 700.0})
    cal_c = _CountingCalendarClient([{"event": "CPI"}])
    asyncio.run(tv_data.get_index_bars(bars_c, "SPY", years=1))
    asyncio.run(tv_data.get_index_quote(quote_c, "SPY"))
    asyncio.run(tv_data.get_economic_calendar_cached(cal_c, days_ahead=10))
    # every one still served from its own entry, none clobbered by the others
    assert asyncio.run(tv_data.get_index_bars(bars_c, "SPY", years=1)) == bars_c.bars
    assert asyncio.run(tv_data.get_index_quote(quote_c, "SPY"))[0] == quote_c.quote
    assert asyncio.run(tv_data.get_economic_calendar_cached(cal_c, days_ahead=10)) == cal_c.events
    assert (bars_c.calls, quote_c.calls, cal_c.calls) == (1, 1, 1)


def test_get_index_quote_does_not_collide_with_the_bars_cache_key():
    """"SPY:1" (bars) and "quote:SPY" must be separate entries -- a quote served
    where bars were asked for would be a crash at best, wrong numbers at worst."""
    bars_client = _CountingClient([{"time": 1, "close": 1.0}])
    quote_client = _CountingQuoteClient({"close": 700.0})
    asyncio.run(tv_data.get_index_bars(bars_client, "SPY", years=1))
    asyncio.run(tv_data.get_index_quote(quote_client, "SPY"))
    assert asyncio.run(tv_data.get_index_bars(bars_client, "SPY", years=1)) == bars_client.bars
    assert bars_client.calls == 1 and quote_client.calls == 1
