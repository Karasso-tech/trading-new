"""Which of the six setups the price data is showing, and where its trigger
sits (2026-08-09).

This is the one module in today's batch that replaces something genuinely
chart-shaped, so it follows the pattern this project already established for
exactly that situation -- `regime_formula.py` (rule 23) and `rubric_formula.py`
(rule 27): **the code decides, and a model may only differ from it by writing
down a reason.** Never a silent substitution.

The reason that pattern exists, from rule 23's own history: the market-state
call was "a fresh, unaccountable read of the chart every single run, with no
way to check consistency or backtest whether the calls were ever right". Setup
type is in exactly that position today -- and worse, because it is the field
the shadow book groups by, so an inconsistent call does not merely produce one
odd report, it makes a whole category unmeasurable.

**The honest caveat, stated plainly rather than buried.** The thresholds below
are a reasonable first draft. They are not backtested. Same posture as rule 4's
0.7x ATR noise floor and regime_formula's score cutoffs: written down, visible,
worth re-checking against real history once the shadow book is full, and not to
be trusted blindly in the meantime. What this buys today is not correctness --
it is *consistency*, which is the thing that was actually missing. The same
chart now gets the same label every time, so the shadow book can finally ask
whether that label predicts anything.

What this module does NOT do: decide whether the setup is worth trading, pick
the stop or the targets (level_picker.py), or write the story. It answers one
question -- what shape is this -- and reports how sure it is.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import setup_types

# The six names, from their single home. Never re-spelled here: a label this
# module invents is a label save_thesis refuses, and the whole run is lost.
BREAKOUT = setup_types.BREAKOUT
RETEST = setup_types.RETEST
PULLBACK = setup_types.PULLBACK
RECLAIM = setup_types.RECLAIM
FAILED_BREAKDOWN = setup_types.FAILED_BREAKDOWN
GAP_AND_HOLD = setup_types.GAP_AND_HOLD

# How far back "recently" means when asking whether a level was broken, lost or
# gapped through. Ten bars is two trading weeks -- long enough that a real
# reclaim is still the story, short enough that a three-month-old break is not.
RECENT_BARS = 10

# A gap has to be worth noticing, and it has to be a REAL gap: today's low above
# yesterday's high, so there is a band of prices where nothing traded at all.
#
# The first version measured open-minus-previous-close instead, and it was
# wrong in a way that only showed up under test: a stock trending half an ATR a
# day trips that test EVERY day, so any steady riser came back as Gap-and-Hold.
# Comparing low to high is the textbook definition and it cannot be fooled that
# way -- overlapping bars are not a gap however far the open moved.
GAP_MIN_ATR = 0.5

# ...and it has to be the CURRENT story, not something that happened a while
# back. Found on the first real run of this module: a stock in a steady uptrend
# that had gapped eight sessions earlier came back as Gap-and-Hold, when the
# tradeable setup was plainly the resistance wall sitting just overhead. A
# gap-and-hold thesis is about a fresh gap; an old one is just a feature of the
# chart. One trading week.
GAP_RECENT_BARS = 5

# A trigger that sits just underneath a wall is not a trigger, it is an invitation
# to buy into resistance. Rule 11 already says the trigger for an identified wall
# is a close above the wall's TOP -- this applies that to every setup type, not
# just the breakout, because the wall does not care what the thesis is called.
# Beyond this distance there is real room to run before the wall matters, and the
# level is disclosed as a checkpoint instead (rule 3).
NEARBY_WALL_ATR = 1.0

# "Near" a level, for a retest holding it or a pullback finding support.
NEAR_LEVEL_ATR = 0.5

# A pullback is only a pullback inside an uptrend, and only while it has not
# gone so far that it is a breakdown. Measured from the recent high, in ATR.
PULLBACK_MIN_ATR = 0.75
PULLBACK_MAX_ATR = 3.0


@dataclass
class SetupCall:
    setup_type: Optional[str]
    trigger: Optional[float]
    trigger_basis: Optional[str]         # what the trigger is a close above, in words
    confidence: str = "clear"            # clear | weak | none
    evidence: list = field(default_factory=list)
    note: Optional[str] = None


def _recent(bars: list[dict], n: int = RECENT_BARS) -> list[dict]:
    return bars[-n:] if len(bars) > n else list(bars)


def _wall_top_above(wall_chains: list[dict], price: float) -> Optional[dict]:
    """The nearest chained wall whose top sits above the current price -- the
    thing a breakout would have to close above (rule 11: the trigger for an
    identified wall is a close above the wall's HIGHEST point)."""
    candidates = [c for c in (wall_chains or [])
                  if c.get("top") is not None and c["top"] > price]
    if not candidates:
        return None
    return min(candidates, key=lambda c: c["top"])


def _wall_top_below(wall_chains: list[dict], price: float) -> Optional[dict]:
    """The nearest wall top BELOW the current price -- i.e. a level this stock
    has already cleared, which is what a retest comes back to."""
    candidates = [c for c in (wall_chains or [])
                  if c.get("top") is not None and c["top"] <= price]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c["top"])


def _trigger_clear_of_walls(trigger: float, atr14: float, wall_chains: list[dict],
                             evidence: list[str]) -> float:
    """Raise a trigger that sits just underneath a wall, up to that wall's top.

    Rule 11 says the breakout trigger for an identified wall is a close above
    the wall's highest point. That reasoning is not specific to a Breakout
    thesis: a Reclaim or a Gap-and-Hold whose entry sits half an ATR below a
    three-touch wall is an entry into resistance whatever it is called, and the
    stop is underneath it either way, so the risk is real and the reward is
    blocked. Beyond NEARBY_WALL_ATR there is genuine room to run and the wall is
    disclosed as a checkpoint instead, which is what rule 3 already does."""
    wall = _wall_top_above(wall_chains, trigger)
    if wall is None or not wall.get("is_wall"):
        return trigger
    gap_atr = (wall["top"] - trigger) / atr14
    if gap_atr <= NEARBY_WALL_ATR:
        evidence.append(
            f"entry raised from {trigger:.2f} to the {wall['top']:.2f} wall top -- "
            f"the wall sat only {gap_atr:.2f}x ATR overhead, too close to buy underneath"
        )
        return wall["top"]
    return trigger


def classify(*, bars: list[dict], atr14: float, sma20: Optional[float],
              sma50: Optional[float], wall_chains: list[dict],
              swing_lows: list[dict]) -> SetupCall:
    """The setup this data is showing, with its trigger.

    Order matters and is not arbitrary. The reversal shapes are tested first,
    because a stock that just gapped up or just reclaimed a lost level is
    ALSO, incidentally, near a high -- and calling that a plain Breakout throws
    away the thing that actually makes it what it is. Rule 15 depends on this
    distinction directly: reversal setups are measured on a 5-day relative
    strength window and trend-following ones on 20 days, so a mislabel here
    silently mis-scores the grade downstream.
    """
    if not bars or not atr14 or atr14 <= 0:
        return SetupCall(None, None, None, confidence="none",
                         note="no bars or no usable ATR -- nothing to classify")

    last = bars[-1]
    close = last["close"]
    recent = _recent(bars)
    recent_high = max(b["high"] for b in recent)
    recent_low = min(b["low"] for b in recent)
    evidence: list[str] = []

    # --- Gap-and-Hold ------------------------------------------------------
    # A real gap up in the recent window, and price has stayed above that gap
    # candle's own open ever since. If it filled the gap, the story is over.
    for i in range(len(bars) - 1, max(len(bars) - GAP_RECENT_BARS, 0), -1):
        gap = bars[i]["low"] - bars[i - 1]["high"]
        if gap >= GAP_MIN_ATR * atr14:
            held = all(b["low"] >= bars[i]["open"] - NEAR_LEVEL_ATR * atr14
                       for b in bars[i:])
            if held:
                evidence.append(
                    f"gapped up {gap / atr14:.2f}x ATR on {bars[i].get('date')} "
                    f"and has held above the gap open ({bars[i]['open']:.2f}) since"
                )
                trigger = _trigger_clear_of_walls(recent_high, atr14, wall_chains, evidence)
                return SetupCall(GAP_AND_HOLD, trigger=trigger,
                                 trigger_basis="close above the post-gap high",
                                 evidence=evidence)
            break

    # --- Failed Breakdown --------------------------------------------------
    # Price was ABOVE a support level, closed BELOW it, then closed back above
    # it -- in that order. Tested before Reclaim because it is the more specific
    # of the two: every failed breakdown is technically a reclaim of something,
    # but not the reverse.
    #
    # The order is the whole thing, and the first version did not check it. It
    # asked only "did some bar dip under this level" and "is the close above it
    # now", which is true of ANY stock that has rallied through an old level.
    # On the first live run that made four tickers out of five come back Failed
    # Breakdown, including MSFT -- which had run from 381 to 500 in ten days and
    # was not failing anything. Its "broken" level was an April swing low the
    # rally had simply passed on the way up.
    #
    # A wick under the level is also not a breakdown. It takes a CLOSE below to
    # break a level and a CLOSE back above to fail the break -- the same
    # daily-close standard the rest of this system uses for a confirmed trigger.
    closes = [b["close"] for b in recent]
    for level in sorted((lo["price"] for lo in swing_lows
                          if lo.get("price") is not None
                          and recent_low <= lo["price"] < close), reverse=True):
        broke_at = next((i for i, c in enumerate(closes) if c < level), None)
        if broke_at is None:
            continue
        if not any(c > level for c in closes[:broke_at]):
            # Never above it inside this window -- price came up THROUGH the
            # level rather than falling out of it. That is a rally, not a
            # failed breakdown.
            continue
        evidence.append(
            f"held above {level:.2f}, closed below it on {recent[broke_at].get('date')}, "
            f"then closed back above it"
        )
        trigger = _trigger_clear_of_walls(recent_high, atr14, wall_chains, evidence)
        return SetupCall(FAILED_BREAKDOWN, trigger=trigger,
                         trigger_basis="close above the recovery high",
                         evidence=evidence)

    # --- Reclaim -----------------------------------------------------------
    # Price was under a moving average it now sits above, within the window.
    for name, level in (("SMA50", sma50), ("SMA20", sma20)):
        if level is None:
            continue
        was_below = any(b["close"] < level for b in recent[:-1])
        if was_below and close > level:
            evidence.append(f"closed back above its {name} ({level:.2f}) after trading under it")
            trigger = _trigger_clear_of_walls(recent_high, atr14, wall_chains, evidence)
            return SetupCall(RECLAIM, trigger=trigger,
                             trigger_basis=f"close above the high made since reclaiming {name}",
                             evidence=evidence)

    # --- Breakout ----------------------------------------------------------
    # A wall overhead that price has not cleared yet. Rule 11: the trigger is a
    # close above the wall's TOP, never a level inside it.
    wall = _wall_top_above(wall_chains, close)
    if wall is not None:
        distance_atr = (wall["top"] - close) / atr14
        if distance_atr <= PULLBACK_MAX_ATR:
            touches = len(wall.get("touches") or [])
            evidence.append(
                f"{'wall' if wall.get('is_wall') else 'swing high'} at {wall['top']:.2f} "
                f"({touches} touches) sits {distance_atr:.2f}x ATR overhead"
            )
            return SetupCall(BREAKOUT, trigger=wall["top"],
                             trigger_basis=("close above the resistance wall top"
                                            if wall.get("is_wall") else
                                            "close above the swing high"),
                             evidence=evidence,
                             confidence="clear" if wall.get("is_wall") else "weak")

    # --- Retest ------------------------------------------------------------
    # Price cleared a wall and has come back to sit on it.
    cleared = _wall_top_below(wall_chains, close)
    if cleared is not None and abs(close - cleared["top"]) <= NEAR_LEVEL_ATR * atr14:
        evidence.append(
            f"sitting on the {cleared['top']:.2f} level it already cleared, "
            f"within {NEAR_LEVEL_ATR}x ATR"
        )
        trigger = _trigger_clear_of_walls(recent_high, atr14, wall_chains, evidence)
        return SetupCall(RETEST, trigger=trigger,
                         trigger_basis="close above the retest bar's high",
                         evidence=evidence)

    # --- Pullback ----------------------------------------------------------
    # An uptrend that has paused. Last, because it is the weakest claim of the
    # six: "in an uptrend and not at a high" describes a great many charts.
    in_uptrend = (sma20 is not None and sma50 is not None
                  and close > sma50 and sma20 > sma50)
    if in_uptrend:
        off_high = (recent_high - close) / atr14
        if PULLBACK_MIN_ATR <= off_high <= PULLBACK_MAX_ATR:
            evidence.append(
                f"above its SMA50 with the shorter average on top, and {off_high:.2f}x ATR "
                f"below the recent {recent_high:.2f} high"
            )
            trigger = _trigger_clear_of_walls(recent_high, atr14, wall_chains, evidence)
            return SetupCall(PULLBACK, trigger=trigger,
                             trigger_basis="close above the pullback high",
                             evidence=evidence)

    return SetupCall(
        None, None, None, confidence="none",
        note=("no setup shape matched -- price is not near a wall, has reclaimed nothing, "
              "gapped nowhere, and is not pulling back inside an uptrend"),
        evidence=[f"close {close:.2f}, recent range {recent_low:.2f}-{recent_high:.2f}"],
    )


# Rule 15's reversal set and window now live in setup_types, once, because this
# list had grown three separate copies across the codebase. Re-exported here so
# existing callers keep working.
REVERSAL_SETUPS = setup_types.REVERSAL_SETUPS
rs_window_days = setup_types.rs_window_days
