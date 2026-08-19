"""Unit tests for score_shadow.py's compute_shadow_metrics() -- Hardening Pass
item 7. Pure function, synthetic bars only -- no fetch, no DB."""

from datetime import datetime, timezone

import pytest

from score_shadow import compute_shadow_metrics


def _bar(date_iso: str, high: float, low: float, close: float) -> dict:
    ts = datetime.fromisoformat(date_iso).replace(tzinfo=timezone.utc).timestamp()
    return {"time": ts, "open": close, "high": high, "low": low, "close": close}


def test_trigger_fires_and_mfe_mae_computed_correctly():
    bars = [
        _bar("2026-01-02", high=99, low=97, close=98),     # before trigger, not fired
        _bar("2026-01-03", high=102, low=99, close=101),   # fires here (close >= 100)
        _bar("2026-01-04", high=105, low=100, close=103),  # best high so far
        _bar("2026-01-05", high=103, low=97, close=99),    # worst low so far
    ]
    result = compute_shadow_metrics(bars, trigger=100.0, since_date="2026-01-01")
    assert result.hypothetical_trigger_fired
    assert result.trigger_fired_date == "2026-01-03"
    assert result.max_favorable_excursion == pytest.approx(5.0)
    assert result.max_adverse_excursion == pytest.approx(-3.0)


def test_never_fired_returns_none_not_zero():
    bars = [
        _bar("2026-01-02", high=99, low=97, close=98),
        _bar("2026-01-03", high=99.5, low=98, close=99),
    ]
    result = compute_shadow_metrics(bars, trigger=100.0, since_date="2026-01-01")
    assert not result.hypothetical_trigger_fired
    assert result.trigger_fired_date is None
    assert result.max_favorable_excursion is None
    assert result.max_adverse_excursion is None


def test_bars_before_since_date_are_excluded():
    # A close above trigger BEFORE since_date must not count as a fire -- the
    # setup didn't exist yet at that point.
    bars = [
        _bar("2025-12-01", high=110, low=105, close=108),  # would fire, but predates the thesis
        _bar("2026-01-02", high=99, low=97, close=98),
    ]
    result = compute_shadow_metrics(bars, trigger=100.0, since_date="2026-01-01")
    assert not result.hypothetical_trigger_fired


def test_fires_on_first_bar_at_since_date():
    bars = [_bar("2026-01-01", high=101, low=99, close=100.5)]
    result = compute_shadow_metrics(bars, trigger=100.0, since_date="2026-01-01")
    assert result.hypothetical_trigger_fired
    assert result.trigger_fired_date == "2026-01-01"
    assert result.max_favorable_excursion == pytest.approx(1.0)
    assert result.max_adverse_excursion == pytest.approx(-1.0)
