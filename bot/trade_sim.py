"""The single exit engine (2026-08-03).

Why this module exists: there were two different simulations of "what would
this trade have done" -- score_shadow.py's (live theses, nightly) and
backtest_strategy.py's (5 years of history) -- and they disagreed on almost
everything that matters. The shadow book had no runner and no second target;
the backtest had no gap-through fills and entered at the signal bar's close.
Two engines answering the same question two different ways cannot be compared,
and the user's requirement was explicit: the backtest must be 1-1 with the
live system. So there is now exactly one engine, here, and both callers use it.

WHAT IT MODELS (all straight from the real rules, not invented here):
  - CONSISTENCY_RULES.md rule 7's exit allocation: one qualifying target ->
    40% there / 60% runner; two -> 40% / 35% / 25% runner.
  - STRATEGY_v3.md's post-tranche trailing stop for the runner: the highest
    daily low since entry that still clears 0.7x ATR14 from the CURRENT price,
    minus rule 24's 0.15x ATR14 buffer, ratcheting up only -- recomputed every
    bar against that bar's own ATR, never a frozen entry-day ATR.
  - Rule 6's runner-only case: no qualifying target at all -> the whole
    position is a runner from bar one, trailing from the start.

ASSUMPTIONS, stated openly, because a simulation whose assumptions are hidden
is a number generator rather than evidence:
  1. Entry is the NEXT bar's open after the signal bar closes. That is when a
     human acting on a daily-close confirmation can actually buy. The distance
     from the trigger to that open is a real cost of the daily-close rule and
     is reported (`entry_gap_pct`), never hidden.
  2. Risk per share = entry - stop, using the ORIGINAL stop. A gap-up entry
     genuinely widens real risk; re-deriving the stop from the new entry would
     flatter every result. R is always measured against this original risk,
     never against a trailed stop.
  3. Stop and target touched in the same bar -> the STOP is assumed hit first.
     A daily bar cannot say which came first, and assuming the good one is how
     backtests lie.
  4. A gap through a level fills at the OPEN, not the level. A stop below an
     open that gapped under it fills at that open -- worse than planned, which
     is what really happens.
  5. Targets fill at the target price (or the open, if the bar gapped past it).
  6. No commissions and no slippage beyond the gap effects above.
  7. A position still open when the data ends is marked to the last close and
     flagged `open` -- never silently counted as a win, never dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import indicators_core as ic

TRAIL_NOISE_FLOOR_ATR = 0.7    # rule 4, and STRATEGY_v3.md's post-tranche method
TRAIL_BUFFER_ATR = 0.15        # rule 24's stop buffer

# WHICH ATR the trailing stop measures against (2026-08-30).
#
# "frozen" is what the live system does, and is the default here because a
# simulation that manages a position differently from the live system describes
# a different system. bot/trail_stop.py uses the position's own atr_at_build and
# says why in its own docstring: judging an existing stop against a freshly
# recomputed ATR silently re-rates every open position whenever volatility
# moves, which is the exact recurring error report_lint was written to catch
# (the AMZN/LLY/CRM/UPS incidents, twice in July 2026).
#
# "per_bar" is what this engine did until now, and is kept as a research option
# rather than deleted -- the difference between the two is itself a measurable
# thing, and deleting the old behaviour would make it unmeasurable.
ATR_MODE_FROZEN = "frozen"
ATR_MODE_PER_BAR = "per_bar"

# WHERE the trailing stop is allowed to stand (2026-08-30).
#
# "live_levels" asks level_picker.stop_candidates -- the same four sources the
# live system uses, in the same order: the last real higher low, the shelf the
# stock paused at, an unfilled gap edge, then the major multi-month pivots.
#
# "daily_lows" is what this engine did until now: every daily low since entry,
# any of them eligible. That is a strictly larger and looser set, so the trail
# could sit on a low the live system would never have offered -- and a
# simulation that places stops somewhere the live system cannot is measuring a
# different strategy, however good its arithmetic.
#
# Kept as an option rather than deleted, so the size of the difference stays
# measurable instead of becoming an assertion.
_N_PIVOT = 3   # regime_formula.N_PIVOT, copied rather than imported to keep the
               # engine free of the live data-fetch stack

STRUCTURE_LIVE = "live_levels"
STRUCTURE_DAILY_LOWS = "daily_lows"
SIM_VERSION = "2.1"            # bump whenever an assumption above changes
                               # 2.1: mfe_r/mae_r measured to the exit bar only


@dataclass
class TrailRule:
    """Where the stop sits BEFORE target 1 — a research knob, not a live change.

    The default instance reproduces the live rule exactly (stop frozen until
    target 1, then the structure trail), so every existing caller that passes
    nothing keeps byte-identical behaviour. Only `backtest_trail.py` ever passes
    a non-default rule, to price the alternatives against 15 years of bars
    (see `_backtest_results/PREREGISTRATION_TRAIL.md`).

      start           "after_t1" (live) | "entry" (trail from bar one)
      method          "structure" (highest qualifying daily low, live) |
                      "chandelier" (highest high since entry - k x ATR)
      noise_floor_atr how far a daily low must sit below the close to count
      chandelier_atr  k, for the chandelier method. No 0.15x buffer is added on
                      top: the multiple IS the buffer, and stacking both would
                      make a 3x ATR chandelier quietly a 3.15x one.
      breakeven_at_r  once the trade has been up this many R at any point, the
                      stop moves to the entry price. Independent of the trail.
      giveback_after_r / giveback_frac
                      once the peak gain reaches this many R, leave if that
                      fraction of the peak gain is handed back. Decided on a
                      close, filled at the next open (assumption 1).
    """
    start: str = "after_t1"
    method: str = "structure"
    noise_floor_atr: float = TRAIL_NOISE_FLOOR_ATR
    chandelier_atr: float = 3.0
    breakeven_at_r: Optional[float] = None
    giveback_after_r: Optional[float] = None
    giveback_frac: float = 0.5


LIVE_TRAIL = TrailRule()      # the live rule, named so the default is explicit


@dataclass
class Tranche:
    """One planned exit. `pct` is a percentage of the ORIGINAL position."""
    price: Optional[float]     # None = the runner (no fixed price, trails out)
    pct: float


@dataclass
class Plan:
    entry: float
    stop: float
    tranches: list           # list[Tranche], the runner last with price=None


@dataclass
class SimResult:
    resolution: str = "open"          # stop | target_1 | target_2 | runner_trailed |
                                      # giveback (research rules only) | open
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    r_multiple: Optional[float] = None        # the real, tranche-weighted result
    r_multiple_full_exit_at_t1: Optional[float] = None  # counterfactual: no runner at all
    mfe_r: Optional[float] = None
    mae_r: Optional[float] = None
    bars_held: int = 0
    reached_t1: bool = False
    reached_t2: bool = False
    # Is there anything left in this position (2026-08-30)? `resolution` cannot
    # say: it is set to "target_1" the moment the first tranche sells and stays
    # there while the runner keeps running, so a live trade and a finished one
    # are spelled identically. Anything averaging over "resolution != open" is
    # mixing marks-to-last-close into closed results, and something did.
    is_fully_closed: bool = False
    tranche_exits: list = field(default_factory=list)   # [(date, price, pct, reason)]


def atr_series(highs: list[float], lows: list[float], closes: list[float],
                period: int = 14) -> list[Optional[float]]:
    """Wilder ATR for EVERY bar in one pass, same recurrence as
    indicators_core.atr_wilder (which returns only the final value).

    Written because the trailing stop needs that bar's own ATR on every bar of
    every trade: calling atr_wilder(bars[:i]) per bar is O(n^2) and made the
    capture-everything backtest unrunnable. Index i holds the ATR as of bar i;
    the first `period` entries are None (not yet defined), never 0.0."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    trs = [ic.true_range(highs[i], lows[i], closes[i - 1]) for i in range(1, n)]
    atr = sum(trs[:period]) / period
    out[period] = atr
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
        out[i + 1] = atr
    return out


def build_plan(entry: float, stop: float, targets: list) -> Plan:
    """Rule 7's allocation, applied mechanically to however many qualifying
    targets there are. `targets` is a list of prices already known to pass rule
    3's gate -- this function never re-tests them, same Category A/B split the
    rest of the system draws."""
    usable = [t for t in (targets or []) if t is not None][:2]
    if not usable:
        return Plan(entry, stop, [Tranche(None, 100.0)])          # rule 6: runner-only
    if len(usable) == 1:
        return Plan(entry, stop, [Tranche(usable[0], 40.0), Tranche(None, 60.0)])
    return Plan(entry, stop, [Tranche(usable[0], 40.0), Tranche(usable[1], 35.0),
                               Tranche(None, 25.0)])


def _pivot_lows(known: list) -> list:
    """The multi-month pivot lows, the same shape the live picker is handed.

    A bar is a pivot low when N bars either side are higher -- which means the
    last N bars can never be one yet, and that is correct rather than a
    limitation: on the day itself nobody knows whether a low will hold."""
    n = _N_PIVOT
    return [{"price": known[j]["low"], "date": None}
            for j in range(n, len(known) - n)
            if all(known[j]["low"] < known[j - k]["low"] for k in range(1, n + 1))
            and all(known[j]["low"] < known[j + k]["low"] for k in range(1, n + 1))]


def _structure_lows(bars: list, i: int, entry_idx: int, atr_now: float,
                     lows_since_entry: list, structure: str) -> list:
    """The prices this bar's trailing stop may stand under.

    Only bars up to and including `i` are ever passed in. That is not a detail:
    handing the level picker the whole series would let a stop on day 5 stand on
    a low that had not happened yet, and every result after it would be a number
    no one could have traded. There is a test that changes future bars and
    asserts the answer for an earlier day does not move.

    Falls back to the daily lows when the level picker offers nothing usable, so
    a trade never silently loses its trail on an empty candidate list."""
    if structure == STRUCTURE_DAILY_LOWS:
        return lows_since_entry
    try:
        import level_picker
    except ImportError:
        return lows_since_entry
    known = bars[:i + 1]
    # All FOUR of the live sources, not three. The major multi-month pivots
    # reach the live picker as fetch_analysis_data's `swing_lows_recent`, so
    # leaving them out here would compare the sim against a live system missing
    # a quarter of its own candidate list. Same pivot definition
    # (regime_formula.N_PIVOT), computed only from bars already known.
    levels = level_picker.stop_candidates(known, atr_now, _pivot_lows(known))
    prices = [c["price"] for c in levels if c.get("price") is not None]
    return prices or lows_since_entry


def simulate(bars: list[dict], entry_idx: int, plan: Plan,
              atrs: list, date_of, trail: Optional[TrailRule] = None,
              atr_mode: str = ATR_MODE_FROZEN,
              structure: str = STRUCTURE_LIVE) -> SimResult:
    """Walk the trade forward one bar at a time from `entry_idx` (the bar whose
    OPEN is the entry). `atrs` is atr_series() output aligned to `bars`;
    `date_of` turns a bar into an ISO date string (the two callers store dates
    differently, so it stays a parameter rather than a second convention).

    `trail` defaults to the live rule; only the trail research script passes
    anything else."""
    rule = trail or LIVE_TRAIL
    result = SimResult()
    risk = plan.entry - plan.stop
    if risk <= 0:
        return result

    forward = bars[entry_idx:]
    if not forward:
        return result

    # MFE/MAE cover only the bars the trade was actually alive for. They are
    # accumulated inside the walk below, never precomputed over `forward` in
    # full. (Fixed 2026-08-07: they used to be max()/min() over every remaining
    # bar in the dataset, so a trade stopped out in its first week still
    # reported whatever high the stock printed months after it was closed. The
    # field exists to answer "how much profit did this trade hand back before it
    # died", and the old form answered a different question with a much bigger
    # number -- on the 15-year lab file it claimed 85% of stopped-out trades had
    # been up 3R first.)
    best_high = forward[0]["high"]
    worst_low = forward[0]["low"]

    fixed = [t for t in plan.tranches if t.price is not None]
    runner_pct = sum(t.pct for t in plan.tranches if t.price is None)
    t1 = fixed[0].price if len(fixed) >= 1 else None

    live_stop = plan.stop
    remaining_pct = 100.0
    realized_r = 0.0
    next_target = 0
    lows_since_entry: list[float] = []
    # rule 6: runner-only trails from bar one. A research rule may also switch
    # the trail on from bar one for a trade that does have a target.
    trailing = (not fixed) or rule.start == "entry"
    pending_giveback = False

    for offset, bar in enumerate(forward):
        i = entry_idx + offset
        result.bars_held = offset + 1

        # A give-back exit is decided on the previous close and filled at this
        # open -- the same "act on a daily close at the next open" assumption
        # the entry itself uses. The trade is flat from that open, so this bar's
        # own high/low never reach MFE/MAE and the stop below is not consulted.
        if pending_giveback and remaining_pct > 0:
            fill = bar["open"]
            realized_r += (remaining_pct / 100.0) * (fill - plan.entry) / risk
            result.tranche_exits.append((date_of(bar), fill, remaining_pct, "giveback"))
            remaining_pct = 0.0
            result.exit_date, result.exit_price = date_of(bar), fill
            result.resolution = "giveback"
            break

        lows_since_entry.append(bar["low"])
        best_high = max(best_high, bar["high"])
        worst_low = min(worst_low, bar["low"])

        # Assumption 3+4: the stop is checked first, and a gap fills at the open.
        if bar["low"] <= live_stop:
            fill = min(bar["open"], live_stop)
            realized_r += (remaining_pct / 100.0) * (fill - plan.entry) / risk
            result.tranche_exits.append((date_of(bar), fill, remaining_pct,
                                          "trail" if trailing and result.reached_t1 else "stop"))
            remaining_pct = 0.0
            result.exit_date, result.exit_price = date_of(bar), fill
            # "stop" means the ORIGINAL stop took it out. Once the trail has
            # moved (either after target 1, or from bar one on a runner-only
            # trade under rule 6), the exit is a trail exit even though the
            # mechanism is the same -- calling a +10R trail exit a "stop" reads
            # as a loss to anyone scanning the column.
            result.resolution = "runner_trailed" if live_stop > plan.stop else "stop"
            break

        # Targets, in order, possibly more than one in the same bar.
        while next_target < len(fixed) and bar["high"] >= fixed[next_target].price:
            tranche = fixed[next_target]
            fill = max(bar["open"], tranche.price)        # assumption 5
            realized_r += (tranche.pct / 100.0) * (fill - plan.entry) / risk
            remaining_pct -= tranche.pct
            result.tranche_exits.append((date_of(bar), fill, tranche.pct,
                                          f"target_{next_target + 1}"))
            if next_target == 0:
                result.reached_t1 = True
                result.resolution = "target_1"
                trailing = True                            # the runner starts trailing
            else:
                result.reached_t2 = True
                result.resolution = "target_2"
            next_target += 1

        if remaining_pct > 0:
            # Frozen by default: the ATR as it stood on the entry bar, for
            # every bar of the trade, exactly as the live trail does it.
            atr_now = (atrs[entry_idx] if atr_mode == ATR_MODE_FROZEN
                       else (atrs[i] if i < len(atrs) else None))
            if atr_now is None and i < len(atrs):
                # An entry bar too early in the series to have an ATR yet. Fall
                # back to that bar's own rather than losing the trail entirely,
                # and only for the trail -- R is never computed from this.
                atr_now = atrs[i]
            peak_r = (best_high - plan.entry) / risk

            # A research rule governs the stop ONLY while target 1 is still
            # unreached. Once the first tranche sells, the live post-tranche
            # trail resumes for the runner, whatever the rule said.
            #
            # This is not a detail. Letting a variant keep its own trail after
            # target 1 measures two changes at once -- earlier protection AND a
            # different runner trail -- and the second one dominates: a wide
            # chandelier is far looser than the structure trail, so it rides
            # runners much further and books the gain as if early protection had
            # earned it. The first run of backtest_trail.py did exactly that and
            # its numbers were void (see the pre-registration's 2nd amendment).
            active = rule if not result.reached_t1 else LIVE_TRAIL

            # Breakeven is independent of the trail: it can fire while the stop
            # is otherwise still frozen.
            if active.breakeven_at_r is not None and peak_r >= active.breakeven_at_r:
                live_stop = max(live_stop, plan.entry)

            if trailing and atr_now:
                candidate = None
                if active.method == "chandelier":
                    candidate = best_high - active.chandelier_atr * atr_now
                else:
                    lows = _structure_lows(bars, i, entry_idx, atr_now,
                                            lows_since_entry, structure)
                    qualifying = [low for low in lows
                                   if bar["close"] - low >= active.noise_floor_atr * atr_now]
                    if qualifying:
                        candidate = max(qualifying) - TRAIL_BUFFER_ATR * atr_now
                if candidate is not None:
                    live_stop = max(live_stop, candidate)

            if active.giveback_after_r is not None and peak_r >= active.giveback_after_r:
                keep = plan.entry + (1.0 - active.giveback_frac) * (best_high - plan.entry)
                if bar["close"] <= keep:
                    pending_giveback = True

    result.mfe_r = (best_high - plan.entry) / risk
    result.mae_r = (worst_low - plan.entry) / risk

    if remaining_pct > 0:
        last = forward[-1]
        realized_r += (remaining_pct / 100.0) * (last["close"] - plan.entry) / risk
        result.exit_date, result.exit_price = date_of(last), last["close"]
        if not result.reached_t1:
            result.resolution = "open"
    else:
        # Every share is out. This is the only place that can be known, which is
        # exactly why the flag is set here rather than inferred from resolution
        # by a reader afterwards.
        result.is_fully_closed = True

    result.r_multiple = realized_r

    # The counterfactual the 2026-08-03 backtest showed matters more than any
    # entry filter: what the identical trade would have returned with the whole
    # position sold at target 1 and no runner at all.
    if t1 is not None and result.reached_t1:
        first = result.tranche_exits[0]
        result.r_multiple_full_exit_at_t1 = (first[1] - plan.entry) / risk
    elif not result.reached_t1:
        result.r_multiple_full_exit_at_t1 = result.r_multiple
    return result
