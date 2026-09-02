"""One ticker's bars, with everything position-based computed once.

Why this exists. The study builds an idea on many thousands of ticker-days, and
a fresh scan of thirteen years of bars on every one of them costs billions of
operations. But almost nothing it scans for actually changes: whether bar 400
was a swing high is a fact about bar 400, not about the day we are asking on.
So the pivots, the gaps and the ATR are computed once per ticker, and a build
"as of day i" reads slices of them.

The rule that keeps this honest: **a Context never answers with anything at or
after `end`.** Every accessor takes `end` and clips to it. A pivot needs `side`
bars on both sides, so the newest `side` bars can never be pivots -- you cannot
know today was a peak until a few days have passed, and any code that shortens
the right-hand side is reading the future.

`setups.build` uses this and nothing else, so there is one implementation of the
rules rather than a slow correct one and a fast different one.
"""

from __future__ import annotations

from typing import Optional

import indicators as ind
from params import DEFAULT, Params


class Context:
    def __init__(self, bars: list[dict], p: Params = DEFAULT):
        self.bars = bars
        self.p = p
        n = len(bars)

        self._closes = [b["close"] for b in bars]

        # ATR, Wilder, as one running series over the whole history. atr[i] is
        # the value using bars 0..i inclusive; None until there are enough.
        self._atr: list[Optional[float]] = [None] * n
        if n > p.atr_period:
            trs = [ind.true_range(bars[k]["high"], bars[k]["low"], bars[k - 1]["close"])
                   for k in range(1, n)]
            atr = sum(trs[:p.atr_period]) / p.atr_period
            self._atr[p.atr_period] = atr
            for k in range(p.atr_period, len(trs)):
                atr = (atr * (p.atr_period - 1) + trs[k]) / p.atr_period
                self._atr[k + 1] = atr

        # Pivots, by the sides the rules actually ask for. Stored with their bar
        # index so a build can drop the ones not yet confirmed.
        self._highs = {p.pivot_side: self._find_pivots(bars, p.pivot_side, high=True)}
        self._lows = {side: self._find_pivots(bars, side, high=False)
                      for side in (p.pivot_side, p.recent_pivot_side)}

        # Gap-up bars: the raw size, and the first later bar that closed the band
        # back up. Whether a gap is big enough depends on the ATR of the day we
        # are asking on, so the size is stored and the test is applied later.
        self._gaps: list[tuple[int, float, Optional[int]]] = []
        for k in range(1, n):
            size = bars[k]["low"] - bars[k - 1]["high"]
            if size > 0:
                ceiling = bars[k - 1]["high"]
                fill = next((j for j in range(k + 1, n) if bars[j]["low"] < ceiling), None)
                self._gaps.append((k, size, fill))

    @staticmethod
    def _find_pivots(bars, side: int, high: bool) -> list[tuple[int, ind.Pivot]]:
        out = []
        key = "high" if high else "low"
        for i in range(side, len(bars) - side):
            value = bars[i][key]
            if high:
                ok = (all(value > bars[i - k][key] for k in range(1, side + 1))
                      and all(value > bars[i + k][key] for k in range(1, side + 1)))
            else:
                ok = (all(value < bars[i - k][key] for k in range(1, side + 1))
                      and all(value < bars[i + k][key] for k in range(1, side + 1)))
            if ok:
                out.append((i, ind.Pivot(bars[i]["date"], value)))
        return out

    # --- everything below answers "as of `end`", exclusive -------------------

    def atr(self, end: int) -> Optional[float]:
        return self._atr[end - 1] if 0 < end <= len(self.bars) else None

    def sma(self, end: int, period: int) -> Optional[float]:
        if end < period:
            return None
        return sum(self._closes[end - period:end]) / period

    def swing_highs(self, end: int, side: int) -> list[ind.Pivot]:
        limit = end - 1 - side
        return [pv for i, pv in self._highs[side] if i <= limit]

    def swing_lows(self, end: int, side: int) -> list[ind.Pivot]:
        limit = end - 1 - side
        return [pv for i, pv in self._lows[side] if i <= limit]

    def gap_edges(self, end: int, atr: float) -> list[ind.Pivot]:
        """Unfilled gap-up bars, as they looked on day `end - 1`."""
        floor = self.p.gap_min_atr * atr
        return [ind.Pivot(self.bars[k]["date"], self.bars[k]["low"])
                for k, size, fill in self._gaps
                if k < end and size >= floor and (fill is None or fill >= end)]

    def walls(self, end: int, atr: float) -> list[ind.Wall]:
        return ind.chain_walls(self.swing_highs(end, self.p.pivot_side), atr, self.p)

    def last_base(self, end: int, atr: float) -> ind.Base:
        return ind.last_base(self.bars[:end], atr, self.p)
