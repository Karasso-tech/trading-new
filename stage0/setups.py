"""The six setups, in code. SETUPS.md is the same thing in plain words -- if the
two ever disagree, that is a bug in whichever one was not updated.

One entry point, `build(ctx, end)`. `ctx` holds a ticker's whole history and
`end` says how much of it exists yet: bar `end - 1` is the newest close a person
could have seen. It answers the six questions the owner asked for --

    what makes it appear · the trigger · the stop · target 1 ·
    when it is invalidated · what was known that day

-- plus the one his rule adds: a setup with no qualifying target is recorded
with its reason and kept OUT of the entry study.

Look-ahead is prevented by construction rather than by care. Nothing here reads
`ctx.bars` past `end`, and every derived number comes through a Context accessor
that clips to `end` itself. The caller chooses `end`, and that is the only place
a future bar could get in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import indicators as ind
from context import Context
from params import DEFAULT, Params

# The six names, and nothing else is ever a setup type.
BREAKOUT = "Breakout"
RETEST = "Retest"
PULLBACK = "Pullback"
RECLAIM = "Reclaim"
FAILED_BREAKDOWN = "Failed Breakdown"
GAP_AND_HOLD = "Gap-and-Hold"

SETUP_TYPES = (BREAKOUT, RETEST, PULLBACK, RECLAIM, FAILED_BREAKDOWN, GAP_AND_HOLD)

# Which three are recoveries rather than continuations. Kept here because the
# relative-strength window differs by side (5 days vs 20), and a list that lives
# in more than one file is a list that stops agreeing with itself.
REVERSAL_SETUPS = frozenset({RECLAIM, FAILED_BREAKDOWN, GAP_AND_HOLD})

# What a stop is standing on. Recorded on every idea so "do stops on recent
# structure do better than stops on old structure" is answerable later. Until
# then these are labels, not claims.
BASIS_RECENT_LOW = "recent_low"
BASIS_SHELF = "shelf"
BASIS_GAP = "gap_edge"
BASIS_MAJOR_PIVOT = "major_swing_low"
BASIS_NO_STRUCTURE = "no_structure"

SOURCE_WALL = "wall / swing high"
SOURCE_MEASURED_MOVE = "measured move from the base"


@dataclass
class Idea:
    """One setup as of one day. Written whether or not it is tradeable."""
    ticker: Optional[str] = None
    known_as_of: Optional[str] = None      # the newest bar this was built from
    close_at_build: Optional[float] = None

    setup_type: Optional[str] = None
    confidence: str = "clear"              # clear | weak
    evidence: list = field(default_factory=list)

    trigger: Optional[float] = None
    trigger_basis: Optional[str] = None

    stop: Optional[float] = None
    stop_basis_level: Optional[float] = None
    stop_basis_kind: Optional[str] = None
    stop_distance_atr: Optional[float] = None

    target_1: Optional[float] = None
    target_1_source: Optional[str] = None
    target_1_rr: Optional[float] = None
    target_1_atr: Optional[float] = None

    atr: Optional[float] = None
    sma_fast: Optional[float] = None
    sma_slow: Optional[float] = None

    invalidation: Optional[dict] = None    # what has to happen for this to stop being true
    rejected_because: Optional[str] = None # set when there is no tradeable idea at all
    params_fingerprint: Optional[str] = None

    @property
    def tradeable(self) -> bool:
        """In the entry study, or not. The owner's rule: no clear target, no entry."""
        return (self.setup_type is not None and self.trigger is not None
                and self.stop is not None and self.target_1 is not None)


# --- the trigger --------------------------------------------------------------

def _raise_over_nearby_wall(trigger: float, atr: float, walls: list[ind.Wall],
                            evidence: list, p: Params) -> tuple[float, str]:
    """A trigger sitting just under a wall is not a trigger, it is an invitation to
    buy into resistance. The stop is below it either way, so the risk is real
    while the reward is blocked. Beyond `nearby_wall_atr` there is genuine room
    to run and the wall is only a checkpoint."""
    wall = ind.nearest_wall_above(walls, trigger)
    if wall is None or not wall.is_wall:
        return trigger, "close above the recent high"
    gap_atr = (wall.top - trigger) / atr
    if gap_atr <= p.nearby_wall_atr:
        evidence.append(
            f"entry raised from {trigger:.2f} to the {wall.top:.2f} wall top -- "
            f"the wall sat only {gap_atr:.2f}x ATR overhead, too close to buy underneath")
        return wall.top, "close above the wall top the recent high sat under"
    return trigger, "close above the recent high"


# --- the stop -----------------------------------------------------------------

def _stop_candidates(ctx: Context, end: int, atr: float, p: Params) -> list[dict]:
    """Every level a stop could honestly stand on, nearest to price first.

    Four sources, in the order a person would look: the last small dip the climb
    is standing on, the shelf it paused at, the edge of an unfilled gap, and the
    major multi-month pivots.
    """
    out: list[dict] = []
    for pv in ctx.swing_lows(end, p.recent_pivot_side):
        out.append({"price": pv.price, "date": pv.date, "kind": BASIS_RECENT_LOW})
    base = ctx.last_base(end, atr)
    if base.low is not None:
        out.append({"price": base.low, "date": base.start, "kind": BASIS_SHELF})
    for pv in ctx.gap_edges(end, atr):
        out.append({"price": pv.price, "date": pv.date, "kind": BASIS_GAP})
    for pv in ctx.swing_lows(end, p.pivot_side):
        out.append({"price": pv.price, "date": pv.date, "kind": BASIS_MAJOR_PIVOT})

    seen, unique = set(), []
    for c in sorted(out, key=lambda c: c["price"], reverse=True):
        key = round(c["price"], 4)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def pick_stop(trigger: float, close: float, atr: float, ctx: Context, end: int,
              p: Params) -> dict:
    """Walk the structural lows downward from the entry and take the first one
    that still leaves `noise_floor_atr` of room once the buffer is applied.

    Two rules doing the work together. The buffer, because a stop sitting
    exactly on a low is filled by any wick that merely touches it. The noise
    floor, because a stop closer than that to the entry is inside one ordinary
    day's movement and is not a decision about the trade at all.

    Levels ABOVE the current price are skipped, not used. Price is already
    underneath them, so they have failed -- a stop there describes a trade that
    is stopped out before it is ever entered.
    """
    buffer = p.stop_buffer_atr * atr
    ceiling = min(trigger, close)
    for low in _stop_candidates(ctx, end, atr, p):
        if low["price"] >= ceiling:
            continue
        stop = low["price"] - buffer
        if stop <= 0:
            continue
        distance_atr = (trigger - stop) / atr
        if distance_atr >= p.noise_floor_atr:
            return {"stop": stop, "basis_level": low["price"], "kind": low["kind"],
                    "distance_atr": distance_atr}
    fallback = ceiling - p.no_structure_atr * atr
    if fallback > 0:
        return {"stop": fallback, "basis_level": None, "kind": BASIS_NO_STRUCTURE,
                "distance_atr": (trigger - fallback) / atr}
    return {"stop": None, "basis_level": None, "kind": None, "distance_atr": None,
            "reason": "no level below the entry leaves enough room, and a plain "
                      "distance stop would land at or below zero"}


# --- target 1 -----------------------------------------------------------------

def _passes_gate(atr_mult: float, rr: float, p: Params) -> bool:
    """Far enough to be worth selling into, and paying enough for the risk.

    Two bands: at or beyond `target_min_atr` a level needs `target_min_rr`; a
    nearer one down to `target_near_atr` needs the stricter `target_near_rr`.
    Anything closer than `target_near_atr` is a checkpoint whatever it pays.
    """
    if atr_mult >= p.target_min_atr:
        return rr >= p.target_min_rr
    if atr_mult >= p.target_near_atr:
        return rr >= p.target_near_rr
    return False


def pick_target_1(trigger: float, stop: float, atr: float, walls: list[ind.Wall],
                  ctx: Context, end: int, p: Params) -> dict:
    """The nearest level above the entry that clears the gate.

    Walls first, because a level other traders can see is a level price actually
    reacts to. Only when no wall passes is the projected source added -- checking
    it always would clutter a clean setup with levels nobody drew.
    """
    risk = trigger - stop
    if risk <= 0:
        return {"price": None, "reason": "the stop is not below the trigger -- no risk to measure"}

    candidates = [{"price": w.top, "source": SOURCE_WALL} for w in walls if w.top > trigger]

    def passing(levels):
        return [lv for lv in levels
                if _passes_gate((lv["price"] - trigger) / atr, (lv["price"] - trigger) / risk, p)]

    if not passing(candidates):
        projected = ind.measured_move(ctx.last_base(end, atr), trigger)
        if projected is not None and projected > trigger:
            candidates.append({"price": projected, "source": SOURCE_MEASURED_MOVE})
    # Fibonacci extension is a source the live system also has, and it is left
    # OUT of stage 0 deliberately (owner's call, 2026-09-01). Measured over 13
    # years of AAPL it supplied 59% of all first targets and made the median
    # first target pay 10.6 to 1 -- a stock at 240 given a 512 target. The cause
    # is the anchor: A is the lowest swing low in the whole window, so the
    # projection is a multi-year move, and a level that far out clears the gate
    # on distance alone.
    #
    # The damage was not the silly number. It was that a target always existed,
    # so "a setup with no clear target does not enter the entry study" could
    # never fire, and 89% of days came back tradeable. Removing the source adds
    # no new threshold to argue about and lets that rule do its job.

    for level in sorted(candidates, key=lambda lv: lv["price"]):
        atr_mult = (level["price"] - trigger) / atr
        rr = (level["price"] - trigger) / risk
        if _passes_gate(atr_mult, rr, p):
            return {"price": level["price"], "source": level["source"],
                    "atr_mult": atr_mult, "rr": rr}
    return {"price": None,
            "reason": "no level above the entry cleared the distance-and-reward gate"}


# --- which of the six ---------------------------------------------------------

def _classify(ctx: Context, end: int, atr: float, fast: Optional[float],
              slow: Optional[float], walls: list[ind.Wall], majors: list[ind.Pivot],
              p: Params) -> dict:
    """The setup this data is showing, and what would invalidate it.

    Order is not arbitrary. A stock that just gapped up or just reclaimed a lost
    level is ALSO, incidentally, near a high -- calling that a plain Breakout
    throws away the thing that makes it what it is, and lands it on the wrong
    side of the reversal/continuation split that the scoring window depends on.
    Pullback is last because it is the weakest claim of the six: "in an uptrend
    and not at a high" describes a great many charts.
    """
    bars = ctx.bars
    close = bars[end - 1]["close"]
    first = max(0, end - p.recent_bars)
    recent = bars[first:end]
    recent_high = max(b["high"] for b in recent)
    recent_low = min(b["low"] for b in recent)
    evidence: list = []

    # 1. Gap-and-Hold -- a real untraded band opened up recently and has held.
    for i in range(end - 1, max(end - p.gap_recent_bars, 1) - 1, -1):
        gap = bars[i]["low"] - bars[i - 1]["high"]
        if gap >= p.gap_min_atr * atr:
            floor = bars[i]["open"] - p.near_level_atr * atr
            if all(b["low"] >= floor for b in bars[i:end]):
                evidence.append(f"gapped up {gap / atr:.2f}x ATR on {bars[i]['date']} and has "
                                f"held above the gap open ({bars[i]['open']:.2f}) since")
                trigger, basis = _raise_over_nearby_wall(recent_high, atr, walls, evidence, p)
                return {"setup": GAP_AND_HOLD, "trigger": trigger, "basis": basis,
                        "evidence": evidence,
                        "invalidation": {"rule": "close_below", "level": bars[i]["open"],
                                         "slack_atr": p.near_level_atr,
                                         "what": "the open of the gap bar"}}
            break

    # 2. Failed Breakdown -- above a level, closed below it, closed back above.
    # The ORDER is the whole thing. Without it, any stock that rallied through an
    # old level qualifies, which is most of them in an uptrend.
    # A wick under the level is not a breakdown either: it takes a CLOSE to break
    # a level and a CLOSE to fail the break.
    closes = [b["close"] for b in recent]
    for level in sorted((pv.price for pv in majors if recent_low <= pv.price < close),
                        reverse=True):
        broke_at = next((i for i, c in enumerate(closes) if c < level), None)
        if broke_at is None:
            continue
        if not any(c > level for c in closes[:broke_at]):
            continue    # price came up THROUGH the level, it did not fall out of it
        evidence.append(f"held above {level:.2f}, closed below it on "
                        f"{recent[broke_at]['date']}, then closed back above it")
        trigger, basis = _raise_over_nearby_wall(recent_high, atr, walls, evidence, p)
        return {"setup": FAILED_BREAKDOWN, "trigger": trigger, "basis": basis,
                "evidence": evidence,
                "invalidation": {"rule": "close_below", "level": level, "slack_atr": 0.0,
                                 "what": "the level that was broken and reclaimed"}}

    # 3. Reclaim -- was under a moving average within the window, is above it now.
    for name, level in (("SMA50", slow), ("SMA20", fast)):
        if level is None:
            continue
        if any(b["close"] < level for b in recent[:-1]) and close > level:
            evidence.append(f"closed back above its {name} ({level:.2f}) after trading under it")
            trigger, basis = _raise_over_nearby_wall(recent_high, atr, walls, evidence, p)
            return {"setup": RECLAIM, "trigger": trigger, "basis": basis, "evidence": evidence,
                    "invalidation": {"rule": "close_below_ma", "ma": name, "slack_atr": 0.0,
                                     "what": f"the {name} it reclaimed"}}

    # 4. Breakout -- a wall overhead that price has not cleared yet.
    wall = ind.nearest_wall_above(walls, close)
    if wall is not None:
        distance_atr = (wall.top - close) / atr
        if distance_atr <= p.pullback_max_atr:
            evidence.append(f"{'wall' if wall.is_wall else 'swing high'} at {wall.top:.2f} "
                            f"({len(wall.touches)} touches) sits {distance_atr:.2f}x ATR overhead")
            return {"setup": BREAKOUT, "trigger": wall.top,
                    "basis": "close above the wall top" if wall.is_wall
                             else "close above the swing high",
                    "confidence": "clear" if wall.is_wall else "weak",
                    "evidence": evidence,
                    "invalidation": {"rule": "drifted_below", "level": wall.top,
                                     "max_atr": p.pullback_max_atr,
                                     "what": "the wall it was set up to break"}}

    # 5. Retest -- cleared a wall and has come back to sit on it.
    cleared = ind.nearest_wall_below(walls, close)
    if cleared is not None and abs(close - cleared.top) <= p.near_level_atr * atr:
        evidence.append(f"sitting on the {cleared.top:.2f} level it already cleared, "
                        f"within {p.near_level_atr}x ATR")
        trigger, basis = _raise_over_nearby_wall(recent_high, atr, walls, evidence, p)
        return {"setup": RETEST, "trigger": trigger, "basis": basis, "evidence": evidence,
                "invalidation": {"rule": "close_below", "level": cleared.top,
                                 "slack_atr": p.near_level_atr,
                                 "what": "the level it broke and came back to"}}

    # 6. Pullback -- an uptrend that has paused, and not paused too far.
    if fast is not None and slow is not None and close > slow and fast > slow:
        off_high = (recent_high - close) / atr
        if p.pullback_min_atr <= off_high <= p.pullback_max_atr:
            evidence.append(f"above its SMA50 with the shorter average on top, and "
                            f"{off_high:.2f}x ATR below the recent {recent_high:.2f} high")
            trigger, basis = _raise_over_nearby_wall(recent_high, atr, walls, evidence, p)
            return {"setup": PULLBACK, "trigger": trigger, "basis": basis, "evidence": evidence,
                    "invalidation": {"rule": "pullback_conditions",
                                     "max_atr": p.pullback_max_atr,
                                     "what": "the uptrend the pause sits inside"}}

    return {"setup": None,
            "reason": "no setup shape matched -- price is not near a wall, has reclaimed "
                      "nothing, gapped nowhere, and is not pulling back inside an uptrend"}


# --- the one entry point ------------------------------------------------------

def build(ctx: Context, end: int, ticker: Optional[str] = None,
          p: Params = DEFAULT) -> Idea:
    """One idea, as of the close of bar `end - 1`.

    `end` is the only thing standing between this and a look-ahead bug, so the
    caller should be able to point at the line that sets it.
    """
    idea = Idea(ticker=ticker, params_fingerprint=p.fingerprint())
    if end <= 0 or end > len(ctx.bars):
        idea.rejected_because = "no bars"
        return idea

    idea.known_as_of = ctx.bars[end - 1]["date"]
    idea.close_at_build = ctx.bars[end - 1]["close"]

    atr = ctx.atr(end)
    if not atr or atr <= 0:
        idea.rejected_because = f"fewer than {p.atr_period + 1} bars -- no usable ATR"
        return idea
    idea.atr = atr
    idea.sma_fast = ctx.sma(end, p.sma_fast)
    idea.sma_slow = ctx.sma(end, p.sma_slow)

    walls = ctx.walls(end, atr)
    majors = ctx.swing_lows(end, p.pivot_side)

    call = _classify(ctx, end, atr, idea.sma_fast, idea.sma_slow, walls, majors, p)
    if call["setup"] is None:
        idea.rejected_because = call["reason"]
        return idea

    idea.setup_type = call["setup"]
    idea.confidence = call.get("confidence", "clear")
    idea.evidence = call.get("evidence", [])
    idea.trigger = call["trigger"]
    idea.trigger_basis = call["basis"]
    idea.invalidation = call["invalidation"]

    stop = pick_stop(idea.trigger, idea.close_at_build, atr, ctx, end, p)
    if stop["stop"] is None:
        idea.rejected_because = stop["reason"]
        return idea
    idea.stop = stop["stop"]
    idea.stop_basis_level = stop["basis_level"]
    idea.stop_basis_kind = stop["kind"]
    idea.stop_distance_atr = stop["distance_atr"]

    target = pick_target_1(idea.trigger, idea.stop, atr, walls, ctx, end, p)
    if target["price"] is None:
        # Not an error and not a discarded row. The setup is real and gets saved
        # with its reason -- it simply cannot enter the entry study, because
        # there is no price at which the plan says to sell.
        idea.rejected_because = target["reason"]
        return idea
    idea.target_1 = target["price"]
    idea.target_1_source = target["source"]
    idea.target_1_rr = target["rr"]
    idea.target_1_atr = target["atr_mult"]
    return idea


def build_from_bars(bars: list[dict], ticker: Optional[str] = None,
                    p: Params = DEFAULT) -> Idea:
    """Convenience for one-off use and tests: build from a plain bar list."""
    return build(Context(bars, p), len(bars), ticker, p)


# --- is it still true? --------------------------------------------------------

def still_valid(idea: Idea, ctx: Context, end: int,
                p: Params = DEFAULT) -> tuple[bool, Optional[str]]:
    """Has the thing that made this setup appear stopped being true?

    Stated plainly because it is a decision, not a rule anyone wrote down: a
    setup is invalidated exactly when its own appearance condition fails. The
    protocol documents say when each setup APPEARS and where its stop goes; they
    do not say when it dies. This is the mirror of the appearance test, chosen
    on 2026-09-01 for being the one rule that cannot drift from the definitions.
    """
    rule = (idea.invalidation or {}).get("rule")
    if rule is None or end <= 0:
        return True, None
    close = ctx.bars[end - 1]["close"]
    atr = ctx.atr(end) or idea.atr or 0.0
    what = idea.invalidation.get("what", "its own condition")

    if rule == "close_below":
        floor = idea.invalidation["level"] - idea.invalidation.get("slack_atr", 0.0) * atr
        if close < floor:
            return False, f"closed at {close:.2f}, below {what} ({floor:.2f})"

    elif rule == "close_below_ma":
        period = p.sma_slow if idea.invalidation["ma"] == "SMA50" else p.sma_fast
        level = ctx.sma(end, period)
        if level is not None and close < level:
            return False, f"closed at {close:.2f}, back below {what} ({level:.2f})"

    elif rule == "drifted_below":
        if atr and (idea.invalidation["level"] - close) / atr > idea.invalidation["max_atr"]:
            return False, (f"price fell more than {idea.invalidation['max_atr']}x ATR away "
                           f"from {what}")

    elif rule == "pullback_conditions":
        fast, slow = ctx.sma(end, p.sma_fast), ctx.sma(end, p.sma_slow)
        if slow is None or fast is None:
            return True, None
        if close < slow:
            return False, "closed below its SMA50 -- the uptrend the pause sat inside is gone"
        if fast < slow:
            return False, "the SMA20 fell below the SMA50 -- the uptrend is gone"
        recent_high = max(b["high"] for b in ctx.bars[max(0, end - p.recent_bars):end])
        if atr and (recent_high - close) / atr > idea.invalidation["max_atr"]:
            return False, "fell further than a pause -- this is a breakdown, not a pullback"

    return True, None
