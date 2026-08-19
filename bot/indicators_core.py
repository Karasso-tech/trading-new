"""
indicators_core.py — ONLY objectively-correct, formulaic calculations.

Scope rule (do not violate): a function belongs here if and only if it has
ONE correct numeric answer for a given input, checkable against what
TradingView itself displays. Anything requiring judgment about WHICH level
matters for a SPECIFIC thesis (base-of-breakout, resistance clusters,
checkpoint-vs-target classification, setup type selection) does NOT belong
here — that stays with Claude's reasoning over raw price data, per
SCREENER_v3.md / MONITOR_v2.md / STRATEGY_v3.md / CONSISTENCY_RULES.md.

This module deliberately does NOT contain:
  - swing high/low ("pivot") detection of any kind
  - "base of breakout" or any thesis-dependent level selection
  - setup grading, target validity, or trading decisions of any kind

If you're about to add a function that decides which price level is
structurally significant, STOP — that's exactly the mistake this file's
predecessor (the original indicators.py) made with its N=3-bar fractal
rule, and exactly why it was reverted. Put that logic in the prompt, not
here.
"""

from dataclasses import dataclass, field
from datetime import date as _date
from typing import Collection, Optional


# ---------------------------------------------------------------------------
# ATR — Wilder's smoothing (RMA), matching TradingView's own ATR indicator.
# A naive "simple average of the last N true ranges" is WRONG and will
# silently disagree with the chart. This is not a style choice.
# ---------------------------------------------------------------------------

def true_range(high: float, low: float, prev_close: float) -> float:
    return max(
        high - low,
        abs(high - prev_close),
        abs(low - prev_close),
    )


def atr_wilder(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """
    Wilder/RMA ATR, matching TradingView's default ATR indicator.

    Wilder's smoothing recurrence (NOT a simple moving average):
        ATR[period-1] = mean(TR[0:period])          # seed value only
        ATR[i] = (ATR[i-1] * (period - 1) + TR[i]) / period   for i >= period

    Requires len(highs) == len(lows) == len(closes) >= period + 1
    (one extra bar at the start to compute the first true range's
    prev_close).
    """
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("highs/lows/closes must be the same length")
    if n < period + 1:
        raise ValueError(f"need at least {period + 1} bars, got {n}")

    trs = [
        true_range(highs[i], lows[i], closes[i - 1])
        for i in range(1, n)
    ]
    # trs has n-1 elements, aligned to closes[1:]

    if len(trs) < period:
        raise ValueError(f"need at least {period} true-range values, got {len(trs)}")

    atr = sum(trs[:period]) / period  # seed = simple average of first `period` TRs
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period

    return atr


# ---------------------------------------------------------------------------
# Parabolic SAR — Wilder's stop-and-reverse recurrence, matching TradingView's
# own Parabolic SAR indicator at its default settings (AF start=0.02,
# step=0.02, max=0.2). Like ATR above, this is pure recurrence arithmetic:
# one correct SAR value per bar given the prior bar's SAR/EP/AF and the
# current high/low. This function does NOT decide how a stop should be used
# (e.g. "trail an open position's stop once profitable") -- that is a
# trading-rule decision and belongs in CONSISTENCY_RULES.md / the prompt,
# never here, same boundary every other function in this file draws.
# ---------------------------------------------------------------------------

@dataclass
class SarPoint:
    sar: float
    trend: int    # 1 = uptrend (SAR below price), -1 = downtrend (SAR above price)
    ep: float     # extreme point (highest high / lowest low) of the current trend leg
    af: float     # current acceleration factor


def parabolic_sar(highs: list[float], lows: list[float],
                   af_start: float = 0.02, af_step: float = 0.02, af_max: float = 0.2) -> list["SarPoint"]:
    """
    Standard Wilder recurrence:
        sar[i] = sar[i-1] + af[i-1] * (ep[i-1] - sar[i-1])
    clipped so an uptrend SAR never rises above the prior two bars' lows (and
    a downtrend SAR never falls below the prior two bars' highs). If that
    clipped value would be crossed by the current bar's low (uptrend) or
    high (downtrend), the trend reverses: SAR resets to the just-ended
    trend's own extreme point, EP resets to the current bar's opposite
    extreme, and AF resets to af_start. Otherwise EP extends (and AF steps
    up by af_step, capped at af_max) whenever a new extreme is made.

    Only highs/lows are used (matching what the indicator itself plots) --
    initial trend direction (bar 1) is seeded from the midpoint
    ((high+low)/2) of bars 0 vs 1, since there is no close input here and
    Wilder's own convention for the very first bar is arbitrary regardless.

    Returns one SarPoint per bar from index 1 onward (len(result) ==
    len(highs) - 1) -- bar 0 has no prior bar to seed a recurrence from.
    """
    n = len(highs)
    if len(lows) != n:
        raise ValueError("highs and lows must be the same length")
    if n < 3:
        raise ValueError(f"need at least 3 bars, got {n}")

    trend = 1 if (highs[1] + lows[1]) >= (highs[0] + lows[0]) else -1
    sar = lows[0] if trend == 1 else highs[0]
    ep = highs[1] if trend == 1 else lows[1]
    points = [SarPoint(sar=sar, trend=trend, ep=ep, af=af_start)]

    for i in range(2, n):
        prev = points[-1]
        raw_sar = prev.sar + prev.af * (prev.ep - prev.sar)

        if prev.trend == 1:
            clipped = min(raw_sar, lows[i - 1], lows[i - 2])
            if clipped > lows[i]:
                points.append(SarPoint(sar=prev.ep, trend=-1, ep=lows[i], af=af_start))
            else:
                new_ep = max(prev.ep, highs[i])
                new_af = min(prev.af + af_step, af_max) if new_ep > prev.ep else prev.af
                points.append(SarPoint(sar=clipped, trend=1, ep=new_ep, af=new_af))
        else:
            clipped = max(raw_sar, highs[i - 1], highs[i - 2])
            if clipped < highs[i]:
                points.append(SarPoint(sar=prev.ep, trend=1, ep=highs[i], af=af_start))
            else:
                new_ep = min(prev.ep, lows[i])
                new_af = min(prev.af + af_step, af_max) if new_ep < prev.ep else prev.af
                points.append(SarPoint(sar=clipped, trend=-1, ep=new_ep, af=new_af))

    return points


# ---------------------------------------------------------------------------
# Simple moving average. No ambiguity here, but pinning the exact
# "closes[-period:]" slicing convention explicitly to match TradingView's
# SMA (which uses the most recent `period` closes, including the current bar).
# ---------------------------------------------------------------------------

def rsi_wilder(closes: list[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI, the same smoothing family as atr_wilder above (and the same
    thing TradingView's default RSI draws) -- NOT the simple-average variant,
    which gives visibly different numbers on the same chart.

        avg_gain[period] = mean(gains[0:period])          # seed
        avg_gain[i] = (avg_gain[i-1] * (period-1) + gain[i]) / period
        RSI = 100 - 100 / (1 + avg_gain / avg_loss)

    Added 2026-08-03: this project had no RSI at all -- ATR, moving averages,
    relative strength, VWAP and Parabolic SAR, but nothing measuring
    overbought/oversold -- so the question "does RSI at entry predict anything"
    could not even be asked of the backtest. Returns None rather than raising
    when there is not enough history, since its callers are scanning bar by bar
    and a missing early value is expected, not an error.
    """
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def ema_series(values: list[float], period: int) -> list:
    """Exponential moving average for every bar, one pass. Seeded with a simple
    average of the first `period` values, which is the standard convention and
    the one MACD below depends on. Entries before the seed are None, never 0.0
    -- a zero there would read as a real value and quietly poison anything
    built on top."""
    out: list = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    out[period - 1] = ema
    for i in range(period, len(values)):
        ema = values[i] * k + ema * (1 - k)
        out[i] = ema
    return out


def macd_series(closes: list[float], fast: int = 12, slow: int = 26,
                 signal: int = 9) -> tuple:
    """Standard MACD (12/26/9): the fast EMA minus the slow one, plus its own
    signal line and the histogram between them. Returns three lists aligned to
    `closes`.

    Added 2026-08-03 -- the most widely used momentum indicator was missing
    from this project entirely, so the backtest could not test whether it
    predicts anything. Whether it earns a place is decided by the data on the
    training years, not by its reputation."""
    fast_e = ema_series(closes, fast)
    slow_e = ema_series(closes, slow)
    macd = [(f - s) if (f is not None and s is not None) else None
            for f, s in zip(fast_e, slow_e)]
    defined = [(i, v) for i, v in enumerate(macd) if v is not None]
    sig: list = [None] * len(closes)
    hist: list = [None] * len(closes)
    if len(defined) >= signal:
        vals = [v for _, v in defined]
        sig_vals = ema_series(vals, signal)
        for (i, _), sv in zip(defined, sig_vals):
            sig[i] = sv
            if sv is not None:
                hist[i] = macd[i] - sv
    return macd, sig, hist


def adx_series(highs: list[float], lows: list[float], closes: list[float],
                period: int = 14) -> list:
    """Wilder's ADX -- how strong a trend is, regardless of direction. High
    means a real trend (up or down); low means chop. Same smoothing family as
    atr_wilder/rsi_wilder above.

    Returns one value per bar, None until enough history exists. Added
    2026-08-03 alongside MACD for the same reason: it was never available to
    measure."""
    n = len(closes)
    out: list = [None] * n
    if n < 2 * period + 1:
        return out
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))

    atr = sum(trs[:period])
    pdm = sum(plus_dm[:period])
    mdm = sum(minus_dm[:period])
    dxs = []
    for i in range(period, len(trs)):
        atr = atr - atr / period + trs[i]
        pdm = pdm - pdm / period + plus_dm[i]
        mdm = mdm - mdm / period + minus_dm[i]
        if atr == 0:
            dxs.append(0.0)
        else:
            pdi, mdi = 100 * pdm / atr, 100 * mdm / atr
            denom = pdi + mdi
            dxs.append(100 * abs(pdi - mdi) / denom if denom else 0.0)
        if len(dxs) == period:
            adx = sum(dxs) / period
            out[i + 1] = adx
        elif len(dxs) > period:
            adx = (adx * (period - 1) + dxs[-1]) / period
            out[i + 1] = adx
    return out


def sma(closes: list[float], period: int) -> float:
    if len(closes) < period:
        raise ValueError(f"need at least {period} closes, got {len(closes)}")
    return sum(closes[-period:]) / period


# ---------------------------------------------------------------------------
# Relative Strength vs a benchmark, fixed lookback window.
# Per SCREENER_v3.md's RS-window rule: trend-following setups use a fixed
# 20-trading-day window; reversal setups (Reclaim / Failed Breakdown /
# Gap-and-Hold) additionally need a 5-day window, since a 20-day window on
# a stock that just crashed will structurally fail RS regardless of how
# strong the actual reclaim is. This function takes the window as a
# parameter rather than hardcoding 20 — the CALLER (the prompt / Claude's
# setup-type classification) decides which window applies, because that
# classification is thesis-dependent judgment, not something this module
# should decide.
# ---------------------------------------------------------------------------

@dataclass
class RelativeStrength:
    window_days: int
    ticker_change_pct: float
    benchmark_change_pct: float
    rs_delta_pct: float  # ticker_change_pct - benchmark_change_pct


def relative_strength(
    ticker_closes: list[float],
    benchmark_closes: list[float],
    window_days: int,
) -> RelativeStrength:
    """
    Both close-price lists must be aligned to the same trading calendar
    (same length, same dates in the same order) and cover at least
    window_days + 1 bars.
    """
    if len(ticker_closes) != len(benchmark_closes):
        raise ValueError("ticker_closes and benchmark_closes must be aligned (same length)")
    if len(ticker_closes) < window_days + 1:
        raise ValueError(f"need at least {window_days + 1} bars, got {len(ticker_closes)}")

    t_now, t_then = ticker_closes[-1], ticker_closes[-(window_days + 1)]
    b_now, b_then = benchmark_closes[-1], benchmark_closes[-(window_days + 1)]

    t_chg = (t_now / t_then - 1) * 100
    b_chg = (b_now / b_then - 1) * 100

    return RelativeStrength(
        window_days=window_days,
        ticker_change_pct=t_chg,
        benchmark_change_pct=b_chg,
        rs_delta_pct=t_chg - b_chg,
    )


# ---------------------------------------------------------------------------
# Volume average, for relative-volume checks (e.g. "today's volume vs the
# 20-day average"). Excludes the current/last bar from its own average by
# default, since "is today high-volume relative to typical" is the usual
# question — pass include_current=True if you specifically need the
# inclusive average instead.
# ---------------------------------------------------------------------------

def volume_average(volumes: list[float], period: int = 20, include_current: bool = False) -> float:
    window = volumes[-period:] if include_current else volumes[-(period + 1):-1]
    if len(window) < period:
        raise ValueError(f"need at least {period} prior volume bars, got {len(window)}")
    return sum(window) / period


# ---------------------------------------------------------------------------
# Fibonacci extension — pure arithmetic ONLY. This function does not choose
# the anchor points (A, B, C) — that is thesis-dependent judgment and must
# come from the prompt / Claude's reasoning, per SCREENER_v3.md's explicit
# anchor-selection rule (A = the structure the current thesis rests on, not
# any arbitrary prior swing). Passing the wrong anchors in produces a
# perfectly-computed extension of the wrong move — this function cannot
# detect that, and is not supposed to.
# ---------------------------------------------------------------------------

FIB_EXTENSION_RATIOS = (1.272, 1.618, 2.618)


@dataclass
class FibExtension:
    anchor_a: float
    anchor_b: float
    anchor_c: float
    levels: dict  # {1.272: price, 1.618: price, 2.618: price}


def fibonacci_extension(anchor_a: float, anchor_b: float, anchor_c: float) -> FibExtension:
    """
    Standard extension: measures the A→B move, projects ratios of that
    move's size forward from C. Works for either direction (B > A for an
    upward move being extended after a pullback to C, or B < A for a
    downward move) — the caller is responsible for anchor_a/b/c meaning
    what the thesis needs them to mean.
    """
    move = anchor_b - anchor_a
    levels = {r: anchor_c + move * r for r in FIB_EXTENSION_RATIOS}
    return FibExtension(anchor_a=anchor_a, anchor_b=anchor_b, anchor_c=anchor_c, levels=levels)


# ---------------------------------------------------------------------------
# Anchored VWAP — pure arithmetic ONLY, same caveat as above: the caller
# supplies anchor_index (which bar to start accumulating from), chosen per
# SCREENER_v3.md's anchor-selection rule (the gap day / flush day / breakout
# day the current thesis is actually about) — never chosen by this function.
# ---------------------------------------------------------------------------

def anchored_vwap(highs: list[float], lows: list[float], closes: list[float],
                   volumes: list[float], anchor_index: int) -> float:
    """
    Volume-weighted average price computed from anchor_index through the
    end of the provided series (inclusive). Uses the typical price
    ((H+L+C)/3) per bar, the standard VWAP convention.
    """
    n = len(closes)
    if not (len(highs) == len(lows) == len(volumes) == n):
        raise ValueError("highs/lows/closes/volumes must be the same length")
    if not (0 <= anchor_index < n):
        raise ValueError(f"anchor_index {anchor_index} out of range for {n} bars")

    typical = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(anchor_index, n)]
    vol_slice = volumes[anchor_index:n]
    total_vol = sum(vol_slice)
    if total_vol == 0:
        raise ValueError("zero total volume in anchored window — cannot compute VWAP")
    return sum(tp * v for tp, v in zip(typical, vol_slice)) / total_vol


# ---------------------------------------------------------------------------
# Measured Move — pure arithmetic ONLY. Same caveat as Fibonacci/VWAP above:
# the caller supplies base_high/base_low/breakout_level, chosen per
# CONSISTENCY_RULES.md rule 17 ("movement potential") -- WHICH base counts as
# "the" base the current move broke out of is contextual judgment, never
# decided here. This is deliberately the SAME formula rule 12's near-term
# Measured Move target already uses; the only difference between "near-term
# target" and "movement potential" (rule 17) is which base gets anchored --
# a nearby swing for the former, the larger pre-move consolidation for the
# latter. Rule 17 also means the result is never gated by rule 3's ATR/R:R
# test and is never presented as a sellable target.
# ---------------------------------------------------------------------------

def measured_move_target(base_high: float, base_low: float, breakout_level: float) -> float:
    """
    Standard flagpole/Measured-Move projection: the height of the base
    (base_high - base_low) projected upward from the breakout level. This
    system is long-only (see STRATEGY_v3.md: "Long בלבד. אין שורטים") --
    unlike fibonacci_extension, there is no downward-move case to support
    here; base_high/base_low/breakout_level are always the base range and
    the level price broke out above.
    """
    return breakout_level + (base_high - base_low)


# ---------------------------------------------------------------------------
# Exit-reason matching and R-multiple -- pure arithmetic ONLY, Journal/Pending-
# flow Stage 3. The caller supplies the position's OWN stored entry_setup
# (targets/stop/atr_at_build, snapshotted at fill time in persistence.py's
# positions.entry_setup -- never re-derived or re-looked-up here, and never
# the ticker's live thesis, which may have since changed).
# ---------------------------------------------------------------------------

@dataclass
class ExitMatch:
    reason: str                     # 'stop' | 'target_1' | 'target_2' | ... | 'unmatched'
    matched_price: float | None     # the stored level it matched, if any


def derive_exit_reason(exit_price: float, targets: list[dict], stop: float,
                        atr_at_build: float,
                        filled_target_indexes: Collection[int] = ()) -> ExitMatch:
    """
    Matches a real exit price against a position's own stored stop/targets.
    Tolerance is max(1%, 0.3 x atr_at_build) -- the same band MONITOR_v2.md's
    🟢 mismatch check uses (Journal/Pending-flow Stage 1), so a fill and an
    exit are held to the same "how close counts as the same level" standard.

    Checks stop first (an exit at/below stop is a stop-out even if it
    happens to also fall within tolerance of some target -- shouldn't occur
    in practice since a valid target sits well above a valid stop, but stop
    takes priority if it ever does), then targets in the order given.

    `filled_target_indexes` (2026-08-07) is the 1-based index of every target
    a PRIOR exit on this same position already sold at -- a level that has
    been realized is skipped here and can never be matched a second time.
    Without it this function was purely price-shaped and had no memory: a
    real ASTS incident (position 25) recorded two separate exits four days
    apart, both labeled 'target_1' against the same 72.36 level, because the
    only stored target kept matching every sell near it. That mislabel is not
    cosmetic -- CONSISTENCY_RULES.md rule 7 allocates a fixed percentage per
    target and everything left over is the Runner, so a target that appears
    to fill twice silently doubles that target's allocation and eats the
    Runner tranche the backtest showed carries the whole edge.

    'runner_trim' is returned for a sell above the stop when there is no
    unrealized target left to attribute it to -- either every stored target
    is already in `filled_target_indexes`, or the position never had one
    (CONSISTENCY_RULES.md rule 6's Runner-only case). That is a real,
    correctly-planned kind of exit and deserves its own name; calling it
    'unmatched' would file a by-the-book Runner sell next to genuine
    off-plan discretion.

    'unmatched' still means what it always did: a sell that lines up with
    nothing stored while a planned level is still open -- a discretionary
    trim, a worse-than-planned stop-out in a fast market. Never forced into
    a false match; 'unmatched' is an honest outcome, not an error.
    """
    tolerance = max(exit_price * 0.01, 0.3 * atr_at_build)

    if abs(exit_price - stop) <= tolerance:
        return ExitMatch(reason="stop", matched_price=stop)

    already_filled = set(filled_target_indexes)
    priced_indexes = {i for i, t in enumerate(targets, start=1) if t.get("price") is not None}
    for i, t in enumerate(targets, start=1):
        price = t.get("price")
        if price is None or i in already_filled:
            continue
        if abs(exit_price - price) <= tolerance:
            return ExitMatch(reason=f"target_{i}", matched_price=price)

    if exit_price > stop and not (priced_indexes - already_filled):
        return ExitMatch(reason="runner_trim", matched_price=None)

    return ExitMatch(reason="unmatched", matched_price=None)


# ---------------------------------------------------------------------------
# Tranche accounting -- CONSISTENCY_RULES.md rule 7's exit allocation, applied
# to a live position's real recorded exits. Pure arithmetic over stored
# numbers: no live price, no judgment about whether a level is worth selling
# at, nothing that decides anything. It only answers "of the plan that was
# already written down at entry, which pieces are done and which is next".
# ---------------------------------------------------------------------------

# Rule 7: one qualifying target -> 40% there / 60% Runner. Two qualifying
# targets -> 40% / 35% / 25% Runner. Used only when a stored target carries no
# `pct` of its own (older thesis rows predate the field); a stored pct always
# wins, since that is what the report the user actually read told them to do.
_RULE7_DEFAULT_TARGET_PCTS = {1: (40.0,), 2: (40.0, 35.0)}

_RUNNER_LABEL = "runner"


@dataclass
class Tranche:
    label: str                      # 'target_1' | 'target_2' | ... | 'runner'
    price: Optional[float]          # the stored level; None for the Runner, which has no target price
    planned_pct: float
    planned_qty: int
    filled_qty: int
    status: str                     # 'filled' | 'partial' | 'waiting'
    over_filled_by: int = 0         # shares sold against this tranche beyond its planned size


@dataclass
class TranchePlan:
    tranches: list[Tranche] = field(default_factory=list)
    next_label: Optional[str] = None
    next_price: Optional[float] = None
    next_qty: int = 0
    runner_qty_left: int = 0
    remaining_qty: int = 0
    runner_only: bool = False       # rule 6: no qualifying target was ever stored
    warnings: list[str] = field(default_factory=list)


def build_tranche_plan(original_qty: int, targets: list[dict],
                        exits: list[dict]) -> TranchePlan:
    """
    Turns a position's own stored targets plus its real exits rows into "which
    tranche is done, which is next, how many shares each". Added 2026-08-07
    after the ASTS incident described in derive_exit_reason(): nothing in the
    system had ever written down that a tranche was realized, so every report
    kept presenting an already-sold target as still pending, and the second
    sell against it looked like a normal target fill instead of the Runner
    being cut in half.

    `targets` is the position's OWN entry_setup targets (snapshotted at fill
    time) -- never the ticker's live thesis, same rule as derive_exit_reason.
    `exits` is that position's exits rows; only `exit_reason` and `exit_qty`
    are read, so a plain list of dicts works.

    Attribution is by recorded reason, not by re-matching prices: 'target_N'
    fills tranche N, and everything else that removed shares ('stop',
    'runner_trim', 'unmatched') comes out of the Runner, which is by
    definition whatever the plan did not assign to a numbered target. This
    keeps the accounting honest about off-plan sells rather than quietly
    crediting them to the next target in line.

    Share counts are rounded per tranche and the Runner absorbs the rounding
    remainder, so the tranche quantities always sum to exactly original_qty --
    a 211-share position never silently plans for 210.
    """
    plan = TranchePlan()
    if not original_qty or original_qty <= 0:
        plan.warnings.append("position has no original quantity on file -- no tranche plan can be built")
        return plan

    priced = [t for t in (targets or []) if isinstance(t, dict) and t.get("price") is not None]
    defaults = _RULE7_DEFAULT_TARGET_PCTS.get(len(priced), ())
    target_pcts: list[float] = []
    for i, t in enumerate(priced):
        pct = t.get("pct")
        if isinstance(pct, (int, float)) and pct > 0:
            target_pcts.append(float(pct))
        elif i < len(defaults):
            target_pcts.append(defaults[i])
        else:
            # More than two stored targets with no pct of their own is outside
            # anything rule 7 describes -- say so rather than inventing a split.
            plan.warnings.append(
                f"target {i + 1} has no stored allocation and rule 7 defines no default for "
                f"{len(priced)} targets -- its share count is a guess, check the original report"
            )
            target_pcts.append(0.0)

    runner_pct = 100.0 - sum(target_pcts)
    plan.runner_only = not priced
    if runner_pct < 0:
        plan.warnings.append(
            f"stored target allocations add up to {sum(target_pcts):.0f}% -- over 100%, leaving no Runner"
        )
        runner_pct = 0.0

    filled_by_label: dict[str, int] = {}
    runner_filled = 0
    for e in (exits or []):
        reason = (e.get("exit_reason") or "").strip()
        qty = int(e.get("exit_qty") or 0)
        if reason.startswith("target_"):
            filled_by_label[reason] = filled_by_label.get(reason, 0) + qty
        else:
            runner_filled += qty

    assigned = 0
    for i, (t, pct) in enumerate(zip(priced, target_pcts), start=1):
        qty = int(round(original_qty * pct / 100.0))
        assigned += qty
        label = f"target_{i}"
        plan.tranches.append(_make_tranche(label, t.get("price"), pct, qty,
                                            filled_by_label.get(label, 0)))
    # The Runner row is always present, even at 0% -- it is where every
    # off-target sell (stop, runner_trim, unmatched) is accounted for, so
    # dropping it would make those shares disappear from the arithmetic.
    runner = _make_tranche(_RUNNER_LABEL, None, runner_pct,
                            max(original_qty - assigned, 0), runner_filled)
    plan.tranches.append(runner)
    if runner.planned_qty == 0 and runner_filled:
        plan.warnings.append(
            f"{runner_filled} share(s) exited outside every planned target, but the plan leaves no Runner"
        )

    total_exited = sum(t.filled_qty for t in plan.tranches)
    plan.remaining_qty = max(original_qty - total_exited, 0)
    # Clamped to what the position actually still holds: when an earlier tranche
    # was over-sold, its own planned size no longer bounds the Runner, and a
    # Runner "left" figure larger than the whole remaining position would read
    # as if untouched shares were still there. The warning below says why.
    plan.runner_qty_left = min(max(runner.planned_qty - runner.filled_qty, 0), plan.remaining_qty)

    nxt = next((t for t in plan.tranches if t.status != "filled" and t.planned_qty > 0), None)
    if nxt is not None and plan.remaining_qty > 0:
        plan.next_label, plan.next_price = nxt.label, nxt.price
        plan.next_qty = min(max(nxt.planned_qty - nxt.filled_qty, 0), plan.remaining_qty)

    for t in plan.tranches:
        if t.over_filled_by:
            plan.warnings.append(
                f"{t.label} sold {t.over_filled_by} share(s) more than its planned {t.planned_qty} -- "
                "those shares came out of a later tranche"
            )
    return plan


def _make_tranche(label: str, price: Optional[float], pct: float,
                   planned_qty: int, filled_qty: int) -> Tranche:
    if filled_qty <= 0:
        status = "waiting"
    elif filled_qty >= planned_qty:
        status = "filled"
    else:
        status = "partial"
    return Tranche(label=label, price=price, planned_pct=pct, planned_qty=planned_qty,
                    filled_qty=filled_qty, status=status,
                    over_filled_by=max(filled_qty - planned_qty, 0))


def compute_r_multiple(entry_price: float, initial_stop: float, exit_price: float) -> float:
    """
    Standard R-multiple: profit/loss expressed in units of the originally
    planned risk (entry_price - initial_stop) -- NOT the current/trailed
    stop. Using the trailed stop instead would make R-multiples computed at
    different points in a position's life (e.g. two different tranche
    exits) incomparable, since the denominator would keep shrinking as the
    stop is raised. initial_stop must be below entry_price (long-only
    system, per STRATEGY_v3.md).
    """
    risk_per_share = entry_price - initial_stop
    if risk_per_share <= 0:
        raise ValueError(f"initial_stop ({initial_stop}) must be below entry_price ({entry_price})")
    return (exit_price - entry_price) / risk_per_share


def earnings_days_out(earnings: Optional[dict], today: Optional[_date] = None) -> Optional[int]:
    """Calendar days until `earnings.next_earnings_date`, or None if genuinely
    not found/unparseable -- never invented (same "unverified stays None"
    posture as output_gate.py's earnings_verified gate). Calendar days, not
    trading days: rubric_formula.EARNINGS_WINDOW_DAYS is a first-draft
    threshold either way.

    Moved here 2026-08-02 from fetch_monitor_data.py, which already computed it
    correctly, so fetch_analysis_data.py (the /screener side) can use the SAME
    function instead of leaving the model to work it out by hand -- see
    earnings_is_verified() for why that mattered.
    """
    raw_date = (earnings or {}).get("next_earnings_date")
    if not raw_date:
        return None
    try:
        return (_date.fromisoformat(str(raw_date)[:10]) - (today or _date.today())).days
    except ValueError:
        return None


def earnings_is_verified(earnings: Optional[dict]) -> bool:
    """True when the feed actually returned a usable earnings date for this
    ticker. Added 2026-08-02.

    Why: `output_gate.classify_output(earnings_verified=...)` defaults to False
    and was only ever set True if the report-writing session remembered to pass
    it. Checked against the live feed on 2026-08-02 -- every ticker tried
    (NVDA/GOOGL/PM/LLY/SCHW/ASTS) returned a real, dated answer -- yet roughly
    half of all stored theses still carry an `earnings_unverified` rejection
    reason. So the fact was sitting in the fetched data the whole time and got
    reported as unknown anyway, which then costs the setup a real rubric point
    (criterion 6) for a data-plumbing reason rather than anything about the
    trade. Deriving it mechanically here is the same fix rule 23 applied to
    regime and rule 27 applied to the grade: stop asking a judgment call to
    report a number that can simply be computed.

    `next_earnings_confirmed=false` still counts as verified -- an estimated
    date from the real calendar is a real data source, which is the only thing
    the gate was ever guarding against (a date recalled from model memory).
    Whether it's confirmed or estimated stays visible in the raw block for the
    report to say out loud.
    """
    if not earnings or earnings.get("error"):
        return False
    return earnings_days_out(earnings) is not None


# ---------------------------------------------------------------------------
# Self-test — run this file directly. This is NOT a substitute for the
# real acceptance test (ACCEPTANCE_TEST_NON_TECHNICAL.md Check 1: compare
# atr_wilder()'s output against TradingView's own displayed ATR for a real
# symbol, by eye, before this ships). This block only proves the arithmetic
# is internally consistent — it cannot prove it matches TradingView, since
# it has no real market data. Do not skip the human check.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Minimal internal consistency check with synthetic data.
    highs =  [10, 11, 10.5, 11.5, 12, 11.8, 12.5, 13, 12.7, 13.2, 13.5, 13.1, 13.8, 14, 14.2]
    lows =   [9.5, 10, 9.8, 10.5, 11, 11, 11.5, 12, 12, 12.5, 12.8, 12.5, 13, 13.3, 13.5]
    closes = [9.8, 10.5, 10.0, 11.2, 11.5, 11.3, 12.2, 12.5, 12.3, 13.0, 13.2, 12.9, 13.5, 13.8, 13.9]

    a = atr_wilder(highs, lows, closes, period=14)
    print(f"ATR14 (synthetic): {a:.4f}")
    assert a > 0, "ATR must be positive"

    s20_input = closes * 2  # pad to satisfy period requirement for the demo
    print(f"SMA5 (synthetic): {sma(closes, 5):.4f}")

    bench = [c * 1.01 for c in closes]  # benchmark tracking slightly ahead
    rs = relative_strength(closes, bench, window_days=5)
    print(f"RS(5d) delta (synthetic): {rs.rs_delta_pct:.2f}%")

    vols = [1_000_000 + i * 10_000 for i in range(21)]
    print(f"Volume avg, 20d, current excluded (synthetic): {volume_average(vols, 20):,.0f}")

    fib = fibonacci_extension(anchor_a=100.0, anchor_b=150.0, anchor_c=130.0)
    print(f"Fib extension (synthetic, A=100 B=150 C=130): {fib.levels}")

    vwap = anchored_vwap(highs, lows, closes, [1000]*len(closes), anchor_index=5)
    print(f"Anchored VWAP from bar 5 (synthetic): {vwap:.4f}")

    print("\nInternal consistency checks passed.")
    print("REMINDER: this does NOT confirm ATR matches TradingView.")
    print("Run the live check from ACCEPTANCE_TEST_NON_TECHNICAL.md before trusting this in production.")
