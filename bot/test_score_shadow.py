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
class TestWhatANonFiringIdeaIsWorthKnowing:
    """Until 2026-08-30 an idea that never fired recorded "did not fire" and
    nothing else -- every field describing what price did was NULL.

    So the book could say how many ideas started, and nothing at all about why
    the rest did not. Missed by a cent and missed by a mile were stored
    identically, and the first of those says the trigger is set slightly too
    high while the second says the idea was simply wrong. Those are opposite
    findings with opposite fixes.

    This is also the part that waiting longer cannot supply: a year of waiting
    still only answers "fired / did not fire". The gap answers "by how much",
    now, on every non-firing idea already on file.
    """

    TRIGGER = 100.0
    ATR = 2.0

    def _bars(self, closes, start=1767225600):
        return [{"time": start + i * 86400, "open": c, "high": c + 0.5,
                  "low": c - 0.5, "close": c} for i, c in enumerate(closes)]

    def _metrics(self, closes):
        return compute_shadow_metrics(
            self._bars(closes), trigger=self.TRIGGER, since_date="2026-01-01",
            atr_at_build=self.ATR)

    def test_a_miss_by_a_cent_is_recorded_as_a_miss_by_a_cent(self):
        m = self._metrics([95, 97, 99.99, 98, 96])
        assert m.hypothetical_trigger_fired is False
        assert m.closest_approach_pct == pytest.approx(-0.01, abs=0.005)
        assert m.closest_approach_atr == pytest.approx(-0.005, abs=0.005)
        assert m.closest_approach_date == "2026-01-03"

    def test_a_miss_by_a_mile_looks_nothing_like_it(self):
        m = self._metrics([95, 92, 90, 88, 86])
        assert m.closest_approach_pct == pytest.approx(-5.0)
        assert m.closest_approach_atr == pytest.approx(-2.5)

    def test_the_gap_is_measured_on_the_close_not_the_intraday_high(self):
        # The trigger is a daily-close rule. A spike through the level that
        # closed back under it did not trigger, and calling it a near miss
        # would rewrite the entry rule while measuring it.
        bars = self._bars([95, 96, 97])
        bars[1]["high"] = 105.0                     # straight through, closed at 96
        m = compute_shadow_metrics(
            bars, trigger=self.TRIGGER, since_date="2026-01-01", atr_at_build=self.ATR)
        assert m.hypothetical_trigger_fired is False
        assert m.closest_approach_pct == pytest.approx(-3.0)   # from the 97 close

    def test_it_says_whether_standing_aside_cost_or_saved(self):
        ran_away = self._metrics([95, 97, 99.5, 99.8, 99.2])
        went_wrong = self._metrics([95, 92, 90, 88, 86])
        assert ran_away.move_without_entry_pct > 0     # right idea, entry too high
        assert went_wrong.move_without_entry_pct < 0   # staying out was correct

    def test_a_trade_that_fired_carries_no_near_miss(self):
        # There is no gap to report once the thing happened, and a 0 here would
        # read as "touched the trigger exactly".
        m = self._metrics([95, 99, 101, 104, 103])
        assert m.hypothetical_trigger_fired is True
        assert m.closest_approach_pct is None
        assert m.closest_approach_date is None

    def test_a_missing_atr_costs_only_the_atr_figure(self):
        m = compute_shadow_metrics(
            self._bars([95, 97, 99]), trigger=self.TRIGGER,
            since_date="2026-01-01", atr_at_build=None)
        assert m.closest_approach_atr is None
        assert m.closest_approach_pct == pytest.approx(-1.0)

