"""The building blocks every setup reads: ATR, moving averages, pivots, walls,
gap edges and sideways bases.

Pure arithmetic. Lists in, numbers out. No fetching, no database, no clock, and
nothing that knows what a trade is. Every function takes only the bars it is
allowed to see, so a look-ahead bug has to be an explicit slicing mistake at the
call site rather than something this module could do quietly on its own.

A bar is a plain dict: {"date", "open", "high", "low", "close", "volume"},
oldest first. That is exactly the shape fetch_bars.py writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from params import DEFAULT, Params

Bar = dict


# --- ATR ---------------------------------------------------------------------

def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr_wilder(bars: list[Bar], period: int = 14) -> Optional[float]:
    """Wilder/RMA average true range -- the same recurrence TradingView's ATR uses.

        seed   = mean of the first `period` true ranges
        ATR[i] = (ATR[i-1] * (period - 1) + TR[i]) / period

    Not a simple moving average of true ranges; that is a different number and
    it drifts away from every chart the owner looks at.

    None when there are not enough bars, rather than a partial answer computed
    off a shorter window. A setup with no ATR is a setup with no usable
    distances, and it gets rejected for that reason with the reason recorded.
    """
    if len(bars) < period + 1:
        return None
    trs = [true_range(bars[i]["high"], bars[i]["low"], bars[i - 1]["close"])
           for i in range(1, len(bars))]
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def sma(bars: list[Bar], period: int) -> Optional[float]:
    if len(bars) < period:
        return None
    return sum(b["close"] for b in bars[-period:]) / period


# --- pivots ------------------------------------------------------------------
#
# A pivot needs `side` bars on BOTH sides, so the most recent `side` bars can
# never be pivots. That is not a limitation to work around -- it is the honest
# statement that you cannot know today's bar was a peak until a few days have
# passed. Any code that "fixes" this by looking at fewer bars on the right is
# reading the future.

@dataclass
class Pivot:
    date: str
    price: float


def swing_highs(bars: list[Bar], side: int) -> list[Pivot]:
    out = []
    for i in range(side, len(bars) - side):
        high = bars[i]["high"]
        if all(high > bars[i - k]["high"] for k in range(1, side + 1)) and \
                all(high > bars[i + k]["high"] for k in range(1, side + 1)):
            out.append(Pivot(bars[i]["date"], high))
    return out


def swing_lows(bars: list[Bar], side: int) -> list[Pivot]:
    out = []
    for i in range(side, len(bars) - side):
        low = bars[i]["low"]
        if all(low < bars[i - k]["low"] for k in range(1, side + 1)) and \
                all(low < bars[i + k]["low"] for k in range(1, side + 1)):
            out.append(Pivot(bars[i]["date"], low))
    return out


# --- walls -------------------------------------------------------------------

@dataclass
class Wall:
    top: float                      # the HIGHEST price in the chain -- the only one anyone uses
    bottom: float                   # the lowest. Kept for the record, never a support level.
    touches: list = field(default_factory=list)
    is_wall: bool = False           # False means a lone swing high, not a real wall


def chain_walls(highs: list[Pivot], atr: float, p: Params = DEFAULT) -> list[Wall]:
    """Group swing highs that sit at roughly the same price.

    Sort by price, walk upward, and keep adding to the current chain while the
    next high is within tolerance of the PREVIOUS one. Tolerance is the larger
    of `wall_tolerance_pct` of the price and `wall_tolerance_atr` of the ATR.
    A chain of `wall_min_touches` or more is a wall; anything shorter is a lone
    swing high and is marked as such so the two can be counted separately.

    The chaining is neighbour-to-neighbour and therefore transitive: a chain can
    drift a long way from bottom to top, one small step at a time, with no real
    structure in the middle. So `bottom` is never a level to lean on. This bit
    the old code once -- it was used as a stop basis and produced a backtest
    that took zero trades. Use a swing LOW for support, never a wall's bottom.
    """
    if not highs:
        return []
    ordered = sorted(highs, key=lambda pv: pv.price)
    chains: list[list[Pivot]] = [[ordered[0]]]
    for pivot in ordered[1:]:
        previous = chains[-1][-1].price
        tolerance = max(previous * p.wall_tolerance_pct, p.wall_tolerance_atr * atr)
        if pivot.price - previous <= tolerance:
            chains[-1].append(pivot)
        else:
            chains.append([pivot])
    return [Wall(top=c[-1].price, bottom=c[0].price,
                 touches=[{"date": pv.date, "price": pv.price} for pv in c],
                 is_wall=len(c) >= p.wall_min_touches)
            for c in chains]


def nearest_wall_above(walls: list[Wall], price: float) -> Optional[Wall]:
    above = [w for w in walls if w.top > price]
    return min(above, key=lambda w: w.top) if above else None


def nearest_wall_below(walls: list[Wall], price: float) -> Optional[Wall]:
    below = [w for w in walls if w.top <= price]
    return max(below, key=lambda w: w.top) if below else None


# --- gaps and bases ----------------------------------------------------------

def gap_edges(bars: list[Bar], atr: float, p: Params = DEFAULT) -> list[Pivot]:
    """The low of every gap-up bar whose gap has never been filled.

    A gap leaves a band of prices where nothing traded, so the bar that made it
    is real support until price closes back into that band. "Gap" here means
    today's low above yesterday's high -- a genuine untraded band, not merely an
    open above the previous close.
    """
    out = []
    for i in range(1, len(bars)):
        if bars[i]["low"] - bars[i - 1]["high"] >= p.gap_min_atr * atr:
            filled = any(b["low"] < bars[i - 1]["high"] for b in bars[i + 1:])
            if not filled:
                out.append(Pivot(bars[i]["date"], bars[i]["low"]))
    return out


@dataclass
class Base:
    high: Optional[float] = None
    low: Optional[float] = None
    start: Optional[str] = None
    end: Optional[str] = None
    note: Optional[str] = None


def last_base(bars: list[Bar], atr: float, p: Params = DEFAULT) -> Base:
    """The most recent stretch where the stock stopped going anywhere.

    Walk the END of the window back from the newest bar; for each end, grow the
    window backwards while the whole high-to-low range still fits inside
    `base_max_height_atr`. The first window that reaches `base_min_bars` is the
    answer -- the last time this stock went sideways before whatever it is doing
    now.

    Used for two things: the low is a stop candidate (the shelf it paused on),
    and the height is projected upward for a measured-move target.
    """
    if not bars or not atr or atr <= 0:
        return Base(note="no bars or no usable ATR")
    max_height = p.base_max_height_atr * atr
    for end in range(len(bars) - 1, p.base_min_bars - 2, -1):
        high, low, start = bars[end]["high"], bars[end]["low"], end
        while start > 0 and (end - start + 1) <= p.base_max_bars:
            nxt = bars[start - 1]
            new_high, new_low = max(high, nxt["high"]), min(low, nxt["low"])
            if new_high - new_low > max_height:
                break
            high, low, start = new_high, new_low, start - 1
        if (end - start + 1) >= p.base_min_bars:
            return Base(high=high, low=low,
                        start=bars[start]["date"], end=bars[end]["date"])
    return Base(note="no sideways stretch found in the bars given")


def measured_move(base: Base, from_level: float) -> Optional[float]:
    """The height of the base, projected up from a level."""
    if base.high is None or base.low is None:
        return None
    return from_level + (base.high - base.low)


FIB_RATIOS = (1.272, 1.618, 2.618)


def fib_extension(a: float, b: float, c: float) -> dict:
    """Project A->B from C. A is a low, B the high above it, C the pullback low."""
    move = b - a
    return {ratio: c + move * ratio for ratio in FIB_RATIOS}
