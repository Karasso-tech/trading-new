"""Stop, targets and movement potential, computed instead of chosen
(2026-08-09).

Every rule these three follow was already written down and complete. Nothing
below is a new decision -- it is the existing rules, in code, so the same inputs
always give the same numbers:

  * the stop:      rules 2, 4 and 24 together
  * the targets:   rules 3, 7, 11, 12 and 13
  * the potential: rule 17

Why move them at all. The screener's instruction sheet had grown to roughly
4,000 words, and most of it was telling a model to copy a number exactly and not
change it. Copying numbers exactly is what code is for. A number that is never
retyped can never be retyped wrong -- and these three had all been retyped
wrong at least once in the live record (a stop set exactly at the candle low
with no cushion, an ATR-distance computed against a fresher ATR than the one
frozen at build time, a target that was really the middle of a wall rather than
its top).

Scope, and it is the same line every mechanical module in this project draws:
this decides levels from facts already computed. It does NOT decide whether the
trade is a good idea, what the story is, or which of two plausible setups is the
real one. That stays with the model.

Everything here is pure -- lists and floats in, dataclasses out. No fetch, no
database, no clock.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision_policy
import indicators_core as ic

# --- rule 24: the cushion between the stop and the level backing it ---------
STOP_BUFFER_ATR = 0.15

# --- rule 4: below this, a stop is inside ordinary daily noise --------------
NOISE_FLOOR_ATR = 0.7

# --- rule 3: what makes a level a sellable target rather than a checkpoint --
TARGET_MIN_ATR = 1.5          # normal bar
TARGET_MIN_RR = 2.0           # ...at or above TARGET_MIN_ATR
TARGET_NEAR_BAND_MIN_ATR = 1.0   # a nearer level may still qualify...
TARGET_NEAR_BAND_MIN_RR = 2.5    # ...but only at a stricter reward:risk

# --- rule 7: how the position is sold, once the targets are known ----------
ALLOCATION_ONE_TARGET = (40.0,)          # ...and 60% runner
ALLOCATION_TWO_TARGETS = (40.0, 35.0)    # ...and 25% runner
MAX_TARGETS = 2


# What kind of structure a stop is standing on. Recorded on every trade from
# 2026-08-10 so the shadow book can eventually answer the question nobody can
# answer today: do stops placed under recent structure do better or worse than
# stops placed under old structure? Until it can, these are labels, not claims.
BASIS_RECENT_LOW = "recent_higher_low"
BASIS_SHELF = "consolidation_shelf"
BASIS_GAP = "gap_edge"
BASIS_MAJOR_PIVOT = "major_swing_low"
# One spelling, defined next to the gate that reads it -- see
# decision_policy.setup_stop_stands_on_structure.
BASIS_NO_STRUCTURE = decision_policy.NO_STRUCTURE_STOP_BASIS

# How far below the entry a stop goes when there is genuinely no structure to
# stand on. A first draft, stated as one -- 2x ATR is the ordinary desk
# convention and it is not backtested here. Same honest-caveat posture as rule
# 4's own 0.7x floor. It is labelled BASIS_NO_STRUCTURE in the output precisely
# so these trades can be counted separately later.
NO_STRUCTURE_ATR = 2.0

# Bars either side for the recent-pullback scan. Deliberately smaller than the
# 3 used for the major pivots in fetch_analysis_data: a stock climbing steadily
# barely makes a 3-bar pivot at all, which is exactly how MMM ended up with its
# nearest "structure" five months old and 12% away.
RECENT_PIVOT_SIDE = 2


@dataclass
class StopChoice:
    stop: Optional[float]
    basis_level: Optional[float]      # the raw structural low, BEFORE the buffer
    basis_date: Optional[str]
    buffer: Optional[float]           # what was subtracted (0.15 x ATR14)
    distance_atr: Optional[float]     # trigger-to-stop, in ATR -- rule 4's check
    reason: Optional[str] = None      # set only when no stop could be placed
    basis_kind: Optional[str] = None  # which of the five above it stands on


@dataclass
class TargetChoice:
    price: float
    source: str                       # which structure this level came from
    source_date: Optional[str]
    atr_mult: float                   # distance from the trigger, in ATR
    rr: float                         # reward:risk at the trigger
    pct: float = 0.0                  # rule 7 allocation, filled in by pick_targets
    status: str = "pass"


@dataclass
class TargetScan:
    targets: list[TargetChoice] = field(default_factory=list)
    checkpoints: list[TargetChoice] = field(default_factory=list)
    runner_pct: float = 100.0
    note: Optional[str] = None
    sources_checked: list = field(default_factory=list)
    # False whenever nothing passed AND an authorized source is still unchecked.
    # Rule 12 is explicit that concluding "Runner-only / No Trade" without
    # checking all five sources is itself a miss, so this flag exists to stop
    # this module's own partial scan from being read as a finished one.
    complete: bool = True


def _gate(atr_mult: float, rr: float) -> bool:
    """Rule 3's two checks, exactly as written: at or beyond 1.5x ATR needs
    2:1, and a nearer level in the 1.0-1.5x band needs a stricter 2.5:1.
    Anything closer than 1.0x ATR is a checkpoint whatever its reward looks
    like."""
    if atr_mult >= TARGET_MIN_ATR:
        return rr >= TARGET_MIN_RR
    if atr_mult >= TARGET_NEAR_BAND_MIN_ATR:
        return rr >= TARGET_NEAR_BAND_MIN_RR
    return False


def _recent_pullback_lows(bars: list[dict], side: int = RECENT_PIVOT_SIDE) -> list[dict]:
    """The small dips a climbing stock actually makes.

    `fetch_analysis_data` finds swing lows with 3 bars either side, which is the
    right filter for the major structure of a chart and the wrong one for the
    last three weeks of a steady uptrend -- a stock going up in a straight line
    barely makes a 3-bar pivot at all. MMM on 2026-08-10 is the case in point:
    it had climbed 153 to 183 and every stored low was from February to early
    July, so the nearest "structure" the stop could stand on was five months old
    and 12% below the price. That stop failed rule 2's own test -- price does
    not have to pass through 162 to reach 184.90, it is already far above it.

    Two bars either side instead of three. Still a filter, never a decision:
    which level a thesis actually depends on stays judgment, exactly as
    CLAUDE_CODE_INSTRUCTIONS warns."""
    out = []
    for i in range(side, len(bars) - side):
        low = bars[i]["low"]
        if all(low < bars[i - k]["low"] for k in range(1, side + 1)) and \
                all(low < bars[i + k]["low"] for k in range(1, side + 1)):
            out.append({"price": low, "date": bars[i].get("date"), "kind": BASIS_RECENT_LOW})
    return out


def _gap_edges(bars: list[dict], atr14: float) -> list[dict]:
    """The low of a recent unfilled gap-up bar. A gap leaves a band of prices
    where nothing traded; the bar that made it is real support until price
    closes back into it."""
    out = []
    for i in range(1, len(bars)):
        if bars[i]["low"] - bars[i - 1]["high"] >= 0.5 * atr14:
            filled = any(b["low"] < bars[i - 1]["high"] for b in bars[i + 1:])
            if not filled:
                out.append({"price": bars[i]["low"], "date": bars[i].get("date"),
                             "kind": BASIS_GAP})
    return out


def stop_candidates(bars: list[dict], atr14: float,
                     swing_lows: list[dict]) -> list[dict]:
    """Every level a stop could honestly stand on, nearest-to-price first.

    Four sources, in the order a person would actually look:
      1. the last real higher low the climb is standing on
      2. the shelf the stock paused at before it broke out
      3. the edge of a gap, if there is an unfilled one
      4. the major multi-month pivots (what the old code used, and only that)

    Sorting is by PRICE, highest first, not by source -- rule 2 asks which
    structure the trade depends on, and the nearest one below is the one price
    has to break to prove the idea wrong."""
    candidates: list[dict] = []
    if bars and atr14 and atr14 > 0:
        candidates += _recent_pullback_lows(bars)
        base = movement_potential(bars, 0.0, atr14)
        if base.base_low is not None:
            candidates.append({"price": base.base_low, "date": base.base_start,
                                "kind": BASIS_SHELF})
        candidates += _gap_edges(bars, atr14)
    for lo in (swing_lows or []):
        if lo.get("price") is not None:
            candidates.append({"price": lo["price"], "date": lo.get("date"),
                                "kind": BASIS_MAJOR_PIVOT})
    # De-duplicate on price, keeping the first (most specific) label for each.
    seen, unique = set(), []
    for c in sorted(candidates, key=lambda c: c["price"], reverse=True):
        key = round(c["price"], 4)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def pick_stop(trigger: float, atr14: float, swing_lows: list[dict],
               current_price: Optional[float] = None,
               bars: Optional[list[dict]] = None) -> StopChoice:
    """The stop, from rules 2, 4 and 24 at once.

    Rule 2 says the base is the structure the thesis depends on -- the test
    being "does price have to pass through this level to reach the trigger?",
    which for a long setup means the highest structural low that still sits
    below the entry. Rule 24 says the stop goes 0.15x ATR14 BELOW that level,
    never exactly at it, because a stop sitting on the low gets filled by any
    wick that merely touches it. Rule 4 says the whole gap from entry to stop
    must be at least 0.7x ATR14, or the placement is inside ordinary daily
    noise and is not signal at all.

    Read together those three pick one level and only one: walk the structural
    lows downward from the entry and take the FIRST that still clears the noise
    floor once the buffer is applied. That is exactly what STRATEGY_v3's
    post-tranche method already says in words -- "the highest daily low that
    still clears 0.7x ATR14, and if the most recent low is too close, go to the
    next real low further back."

    `current_price` matters and is not optional in practice. A structural low
    ABOVE where the stock is trading right now is a level that has already
    failed -- price is underneath it today -- so a stop placed there describes a
    trade that is stopped out before it is ever entered. Found on the first live
    run of this module, 2026-08-09: AMD at 483.36 came back with a trigger of
    530.13 and a stop of 492.41, taken from a 498.15 shelf dated a month earlier
    that price had since broken straight through. Rule 2's own test is "does
    price have to pass through this level to reach the trigger?" -- and the
    honest reading of a level price is currently below is that the trade does
    not depend on it any more, it already lost it.

    Returns a StopChoice with `stop` None and a stated reason when no low
    qualifies. That is a real answer -- "this setup has no honest stop in the
    data available" -- and inventing a round number below the lowest low would
    be the dishonest alternative.
    """
    if not atr14 or atr14 <= 0:
        return StopChoice(None, None, None, None, None,
                          reason="no usable ATR -- cannot place or check a stop")
    buffer = STOP_BUFFER_ATR * atr14
    ceiling = trigger if current_price is None else min(trigger, current_price)
    # Highest first: rule 2 wants the nearest structure the trade leans on, and
    # rule 4 is what pushes it further back when the nearest one is too tight.
    candidates = [c for c in stop_candidates(bars or [], atr14, swing_lows)
                  if c["price"] < ceiling]
    for low in candidates:
        stop = low["price"] - buffer
        if stop <= 0:
            continue
        distance_atr = (trigger - stop) / atr14
        if distance_atr >= NOISE_FLOOR_ATR:
            return StopChoice(stop=stop, basis_level=low["price"],
                              basis_date=low.get("date"), buffer=buffer,
                              distance_atr=distance_atr, basis_kind=low["kind"])
    if current_price is not None and any(
            lo.get("price") is not None and current_price <= lo["price"] < trigger
            for lo in swing_lows):
        return StopChoice(
            None, None, None, buffer, None,
            reason=("every structural low under the entry sits ABOVE the current price -- "
                    "those levels have already broken, so a stop there would be violated "
                    "before the trade is even entered"),
        )
    # Nothing structural works. A stop still has to exist for the trade to be
    # sizeable at all, so fall back to a plain distance -- and LABEL it, so a
    # trade standing on no structure is countable later rather than looking
    # like every other trade in the book.
    fallback = ceiling - NO_STRUCTURE_ATR * atr14
    if fallback > 0:
        return StopChoice(
            stop=fallback, basis_level=None, basis_date=None, buffer=buffer,
            distance_atr=(trigger - fallback) / atr14,
            basis_kind=BASIS_NO_STRUCTURE,
            reason=(f"no structure to stand on -- every candidate low was either above the price "
                    f"or inside the {NOISE_FLOOR_ATR}x ATR noise band, so this is a plain "
                    f"{NO_STRUCTURE_ATR}x ATR distance, not a level the chart gave us"),
        )
    return StopChoice(
        None, None, None, buffer, None,
        reason=("no structural low below the entry leaves at least "
                f"{NOISE_FLOOR_ATR}x ATR of room, and a distance-based stop would land at or "
                f"below zero -- this setup cannot be given an honest stop"),
    )


def _wall_levels(wall_chains: list[dict]) -> list[dict]:
    """One candidate price per chain, with its source named.

    The top of a wall, never a level inside it -- rule 11 says the breakout
    trigger for an identified wall is a close above the wall's HIGHEST point,
    and the same logic makes the top the only sellable level in the chain: a
    target set at the middle of a wall is a target set underneath resistance.

    This also satisfies rule 11's recursive step for free. The chaining that
    produced these walls ran over every swing high at once, so a level that is
    part of a further wall higher up is already inside that chain and is never
    offered as a candidate on its own. The real PLTR miss the recursive step
    was written for -- 157.78 looked like a valid target but was part of a
    157.78-163.70 wall whose true top was 163.70 -- cannot recur here.
    """
    out = []
    for chain in wall_chains or []:
        top = chain.get("top")
        if top is None:
            continue
        touches = chain.get("touches") or []
        date = max((t.get("date") for t in touches if t.get("date")), default=None)
        out.append({
            "price": float(top),
            "source": ("resistance wall top "
                       f"({len(touches)} touches)") if chain.get("is_wall") else "swing high",
            "date": date,
        })
    out.sort(key=lambda x: x["price"])
    return out


# Rule 12's five authorized target sources. Two of them are pure arithmetic
# once the anchors are mechanical, and this module computes those. The other
# three need an anchor CHOSEN -- rule 12 says so outright ("anchor selection is
# contextual judgment and stays with the model; the arithmetic once an anchor is
# chosen is pure formula") -- so they stay Category B and their absence is
# reported rather than silently treated as "checked and failed".
SOURCE_SWING = "swing high / resistance wall"
SOURCE_MEASURED_MOVE = "measured move from the base"
SOURCE_FIB = "Fibonacci extension"
NEEDS_ANCHOR = ("Anchored VWAP / Volume Profile", "defined pattern target")


def _mechanical_extra_levels(trigger: float, atr14: float, bars: list[dict],
                              swing_lows: list[dict],
                              wall_chains: list[dict]) -> list[dict]:
    """Target candidates from the authorized sources that need no chosen anchor.

    Added 2026-08-09 after the first full run over the real pending list: 8 of
    16 tickers came back No Trade and EVERY ONE of them for the same reason,
    `no_qualifying_target`. The cause was not the market -- it was that this
    function only ever looked at swing highs, which is one of rule 12's five
    sources, and then let the answer be read as a finished scan. The owner
    spotted it from the outside, asking whether "the two nearest levels that pay
    2-to-1" was really the rule. It is not.

    Both anchors below are mechanical and stated, never eyeballed:
      * measured move -- the height of the last sideways base, projected from
        the trigger. Same construction rule 17 uses for movement potential.
      * Fibonacci extension -- A is the lowest recent swing low, B the highest
        high since it, C the latest swing low after B. That is the ordinary
        impulse-then-pullback shape; when the bars do not form it, no level is
        offered rather than one being forced.
    """
    out = []
    if not bars:
        return out

    # atr14 is passed in, never recomputed here. A second ATR derived from a
    # different window is a second answer to a question that already has one,
    # and every level below is measured against it.
    base = movement_potential(bars, trigger, atr14)
    if base.price is not None and base.price > trigger:
        out.append({"price": base.price, "source": SOURCE_MEASURED_MOVE,
                    "date": base.base_end})

    lows = [lo for lo in (swing_lows or []) if lo.get("price") is not None]
    if lows and wall_chains:
        anchor_a = min(lows, key=lambda lo: lo["price"])
        highs_above_a = [c["top"] for c in wall_chains
                         if c.get("top") is not None and c["top"] > anchor_a["price"]]
        later_lows = [lo["price"] for lo in lows if lo["price"] > anchor_a["price"]]
        if highs_above_a and later_lows:
            anchor_b = max(highs_above_a)
            anchor_c = max(later_lows)
            fib = ic.fibonacci_extension(anchor_a["price"], anchor_b, anchor_c)
            for ratio, price in sorted(fib.levels.items()):
                if price > trigger:
                    out.append({"price": price,
                                "source": f"{SOURCE_FIB} {ratio:g} "
                                          f"({anchor_a['price']:.2f}-{anchor_b:.2f}-{anchor_c:.2f})",
                                "date": None})
                    break        # the nearest projection, never the whole ladder
    return out


def pick_targets(trigger: float, stop: float, atr14: float,
                  wall_chains: list[dict], bars: Optional[list[dict]] = None,
                  swing_lows: Optional[list[dict]] = None) -> TargetScan:
    """Every level above the entry, gated by rule 3, allocated by rule 7.

    The candidates come from the wall scan that rule 11 already requires and
    `fetch_analysis_data.py` already computes -- this never re-finds levels, it
    only judges the ones found. Rule 13's closed source list is respected by
    construction: a moving average is not a swing high, so it can never appear
    here.

    Levels that fail the gate are NOT dropped. They come back as `checkpoints`,
    because rule 3 says a failing level is a checkpoint rather than nothing at
    all, and rule 14 says a section is never silently empty. A reader needs to
    see that a level was considered and why it did not qualify.

    At most two sellable targets (rule 7). Everything past the second is the
    runner, which has no fixed price by definition.
    """
    scan = TargetScan()
    if not atr14 or atr14 <= 0 or trigger <= stop:
        scan.note = "no usable ATR or no positive risk -- targets cannot be gated"
        return scan
    risk = trigger - stop
    scan.sources_checked = [SOURCE_SWING]
    candidates = _wall_levels(wall_chains)

    # Rule 12: the swing-high scan is the FIRST source, not the only one. Only
    # when it produces nothing that passes do the other mechanical sources get
    # added -- checking them always would clutter a clean setup with projected
    # levels nobody asked for.
    def _passing(levels):
        return [lv for lv in levels
                if lv["price"] > trigger
                and _gate((lv["price"] - trigger) / atr14, (lv["price"] - trigger) / risk)]

    if not _passing(candidates) and bars:
        extra = _mechanical_extra_levels(trigger, atr14, bars, swing_lows or [], wall_chains)
        if extra:
            candidates = candidates + extra
            scan.sources_checked += [SOURCE_MEASURED_MOVE, SOURCE_FIB]

    for level in sorted(candidates, key=lambda lv: lv["price"]):
        price = level["price"]
        if price <= trigger:
            continue
        atr_mult = (price - trigger) / atr14
        rr = (price - trigger) / risk
        choice = TargetChoice(price=price, source=level["source"],
                              source_date=level.get("date"),
                              atr_mult=atr_mult, rr=rr)
        if _gate(atr_mult, rr) and len(scan.targets) < MAX_TARGETS:
            scan.targets.append(choice)
        else:
            choice.status = "checkpoint"
            scan.checkpoints.append(choice)

    allocation = ALLOCATION_TWO_TARGETS if len(scan.targets) == 2 else ALLOCATION_ONE_TARGET
    for target, pct in zip(scan.targets, allocation):
        target.pct = pct
    scan.runner_pct = 100.0 - sum(t.pct for t in scan.targets)
    if not scan.targets:
        # Rule 12, and this is the important part: an empty result here is NOT
        # "there is no target". It is "the sources that need no chosen anchor
        # found none". Two authorized sources are still unchecked, and rule 12
        # says concluding Runner-only or No Trade without them is a miss.
        scan.complete = False
        scan.note = (
            "no level passed from the sources that can be checked mechanically "
            f"({', '.join(scan.sources_checked)}). STILL UNCHECKED, and they need an anchor "
            f"chosen before any No Trade is final: {', '.join(NEEDS_ANCHOR)} (rule 12)"
        )
    return scan


@dataclass
class Potential:
    price: Optional[float]
    base_high: Optional[float]
    base_low: Optional[float]
    base_start: Optional[str]
    base_end: Optional[str]
    note: Optional[str] = None


# A base is a stretch of bars that went sideways. "Sideways" here means the
# whole stretch fits inside this many ATRs top to bottom. 2.0 is a first draft,
# stated as one -- same honest-caveat posture as rule 4's 0.7x noise floor and
# regime_formula's score cutoffs. It is not backtested and should be re-checked
# against real history rather than trusted forever.
BASE_MAX_HEIGHT_ATR = 2.0
BASE_MIN_BARS = 5
BASE_MAX_BARS = 60


def movement_potential(bars: list[dict], breakout_level: float,
                        atr14: float) -> Potential:
    """Rule 17's big-picture figure: the height of the consolidation the current
    move came out of, projected up from the breakout level.

    This is deliberately NOT a target and must never be shown as one -- rule 17
    is explicit that it answers a different question ("how far could this
    plausibly go") from the target table ("what price do I plan to sell at"),
    and it is not gated by rule 3.

    Which base counts is called contextual judgment in rule 17, so this is the
    one number here that a model may still override with a stated reason. What
    it computes is a defensible default rather than a guess: walking back from
    the most recent bar, the last stretch of at least five bars whose entire
    high-to-low range fits inside BASE_MAX_HEIGHT_ATR -- in other words the
    last time this stock stopped going anywhere before the move that is
    happening now.

    Returns price None with a stated note when no such stretch exists in the
    bars given. Rule 17 says that case is written down explicitly ("no current
    move defined to measure from"), never skipped in silence.
    """
    if not bars or not atr14 or atr14 <= 0:
        return Potential(None, None, None, None, None,
                         note="no bars or no usable ATR -- potential cannot be measured")
    max_height = BASE_MAX_HEIGHT_ATR * atr14
    n = len(bars)
    # Walk the END of the window backwards from the most recent bar, and for
    # each end grow the window backwards as far as it stays tight. The first
    # window that reaches BASE_MIN_BARS is the most recent real base.
    for end in range(n - 1, BASE_MIN_BARS - 2, -1):
        high = bars[end]["high"]
        low = bars[end]["low"]
        start = end
        while start > 0 and (end - start + 1) <= BASE_MAX_BARS:
            nxt = bars[start - 1]
            new_high = max(high, nxt["high"])
            new_low = min(low, nxt["low"])
            if new_high - new_low > max_height:
                break
            high, low, start = new_high, new_low, start - 1
        if (end - start + 1) >= BASE_MIN_BARS:
            return Potential(
                price=ic.measured_move_target(high, low, breakout_level),
                base_high=high, base_low=low,
                base_start=bars[start].get("date"), base_end=bars[end].get("date"),
            )
    return Potential(None, None, None, None, None,
                     note="no sideways base found in the bars given -- nothing to measure from")


@dataclass
class TrailedStop:
    stop: float                       # what the live stop should be now
    moved: bool                       # did it actually change
    basis_level: Optional[float]      # the structural low behind it, if it moved
    basis_date: Optional[str]
    reason: str


def trail_stop(*, current_price: float, current_stop: float, atr_at_build: float,
                swing_lows: list[dict], past_target_1: bool = True,
                bars: Optional[list[dict]] = None) -> TrailedStop:
    """Where an OPEN position's stop belongs today (2026-08-09).

    STRATEGY_v3's post-tranche method already states this exactly -- "the
    highest daily low that still clears 0.7x ATR14 from the current price, and
    if the most recent low is too close, go to the next real low further back"
    -- plus rule 24's 0.15x ATR buffer underneath the level. That is the same
    arithmetic pick_stop already does for a fresh entry, measured from the live
    price instead of from a trigger.

    Two hard properties, both of which exist because a stop is real money:

    1. **It never moves down.** Not "should not" -- cannot. A trail that
       loosens is not a trail, and persistence.update_current_stop rejects it
       at the database anyway. Returning the existing stop unchanged, with a
       stated reason, is the honest answer when nothing has improved.
    2. **ATR comes from build time, not today.** `atr_at_build` is frozen at
       entry on purpose. Judging an existing stop's distance against a freshly
       recomputed ATR is the exact recurring error report_lint was written to
       catch (the 2026-07-20 and 2026-07-22 AMZN/LLY/CRM/UPS incidents), and it
       silently re-rates every open position every time volatility changes.
    """
    if not atr_at_build or atr_at_build <= 0:
        return TrailedStop(current_stop, False, None, None,
                           "no usable build-time ATR -- stop left exactly where it is")

    # BEFORE target 1, this system does not trail. That is not a preference, it
    # is this project's own measured result: twelve pre-registered ways to lock
    # in profit before the first target were tested and ALL TWELVE lost money,
    # with tighter consistently worse (private backtest notes, point 7). The same
    # five years also found the runner tranche to be the entire edge -- +40.6R
    # with it against -33.3R without -- and a stop tightened early is precisely
    # how a runner gets killed before it can run.
    #
    # This guard was missing on the first version of this function, written the
    # same day. Run against the real book it recommended lifting the stop on
    # three full-size positions that had not reached target 1: NBIS, BE and NOW.
    # The owner asked whether he should act on it, which is the only reason it
    # was caught before a real stop moved.
    if not past_target_1:
        return TrailedStop(
            current_stop, False, None, None,
            "this position has not reached target 1 yet, so the stop does not move. "
            "Every tested way of tightening a stop before the first target lost money "
            "in this system's own backtest, and the runner is where its edge lives",
        )

    buffer = STOP_BUFFER_ATR * atr_at_build
    floor = NOISE_FLOOR_ATR * atr_at_build
    # Same candidate sources as a fresh entry (2026-08-10): a position that has
    # run needs the recent higher lows too, not only the multi-month pivots.
    for low in stop_candidates(bars or [], atr_at_build, swing_lows):
        candidate = low["price"] - buffer
        if candidate <= current_stop:
            # Sorted high to low, so nothing further down can beat this either.
            break
        if current_price - candidate < floor:
            continue          # too close to today's price to be signal (rule 4)
        return TrailedStop(candidate, True, low["price"], low.get("date"),
                           f"trailed up to sit below the {low['price']:.2f} "
                           f"{low.get('kind', 'level')}")
    return TrailedStop(current_stop, False, None, None,
                       "no new structure above the current stop that clears the noise floor "
                       "-- stop stays exactly where it is")


# --- why not yet ------------------------------------------------------------
#
# `rejection_reasons` was free text written by the model, and the same situation
# came back as "trigger_not_fired", "trigger_not_confirmed", "no_live_trigger",
# "trigger_pending" and "awaiting_trigger_confirmation" across five runs of one
# week. Useless for counting anything later, which is the only reason the field
# exists. The gates already know why they failed; they should say so themselves.

def rejection_reasons(*, has_target: bool, rr: Optional[float],
                       grade: Optional[str], regime: Optional[str],
                       rs_delta_pct: Optional[float],
                       dist_sma20_atr: Optional[float],
                       earnings_days_out: Optional[int],
                       trigger_fired: bool,
                       stop: Optional[float]) -> list[str]:
    """The stable tokens for why this idea is not a clean buy right now.

    One token per real, checkable fact, in a fixed vocabulary, emitted by the
    code that knows the answer rather than retyped by whoever writes the report.
    decision_policy.explain_reasons already turns these into plain sentences for
    the user; it matches by substring, so these names must stay stable."""
    reasons = []
    if not has_target:
        reasons.append("no_qualifying_target")
    if stop is None:
        reasons.append("no_usable_stop")
    if rr is not None and rr < TARGET_MIN_RR:
        reasons.append("rr_below_2")
    if regime in ("risk_off", "structure_break"):
        reasons.append("regime_against")
    if grade in ("D", "F"):
        reasons.append("grade_below_c")
    if rs_delta_pct is not None and rs_delta_pct <= 0:
        reasons.append("rs_weaker_than_market")
    if dist_sma20_atr is not None and abs(dist_sma20_atr) > 2.0:
        reasons.append("extended_vs_sma20")
    if earnings_days_out is not None and 0 <= earnings_days_out <= 10:
        reasons.append("earnings_inside_window")
    if earnings_days_out is None:
        reasons.append("earnings_unverified")
    if not trigger_fired:
        reasons.append("trigger_not_fired")
    return reasons
