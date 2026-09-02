"""Every tunable number in one frozen place.

There are twenty of them and not one has ever been tested. They are copied
verbatim from the numbers the live system uses today, because stage 0 is asking
whether the pipeline agrees with itself -- not whether the numbers are any good.
Inventing better-looking values here would guarantee the backtest and the live
system disagree, which is the exact thing stage 0 exists to fix.

They live in a frozen dataclass rather than as module constants for one reason,
stated by the owner on 2026-09-01: some day we will want to sweep them and see
whether execution improves. A sweep needs the values to be data you can vary and
stamp onto a result, not literals baked into the code that produced it. So:

    from params import DEFAULT, Params
    tighter = DEFAULT.replace(noise_floor_atr=0.9)

Every result written anywhere must carry `params.fingerprint()`. A number in a
results file whose parameters cannot be recovered is a number nobody can ever
reproduce, and the whole point of stage 0 is that one trade can be replayed
exactly.

SETUPS.md is the plain-language version of this file. If one changes and the
other does not, the one that was NOT changed is the bug.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Params:
    # --- measuring movement ------------------------------------------------
    atr_period: int = 14              # Wilder ATR window; the unit every distance below is in
    sma_fast: int = 20
    sma_slow: int = 50

    # --- finding shapes on the chart ---------------------------------------
    pivot_side: int = 3               # bars either side for a major swing high/low
    recent_pivot_side: int = 2        # ...and for the small dips a steady climber makes
    wall_tolerance_pct: float = 0.03  # two highs are "the same level" within the LARGER
    wall_tolerance_atr: float = 0.5   # ...of these two
    wall_min_touches: int = 3         # a chain this long stops being a high and becomes a wall
    recent_bars: int = 10             # what "recently" means everywhere
    gap_recent_bars: int = 5          # ...except for a gap, which has to be fresher

    # --- what makes each setup itself --------------------------------------
    gap_min_atr: float = 0.5          # smaller than this is not a gap worth a thesis
    near_level_atr: float = 0.5       # "sitting on" a level, for a retest or a held gap
    pullback_min_atr: float = 0.75    # closer to the high than this is not a pullback at all
    pullback_max_atr: float = 3.0     # further than this is a breakdown, not a pause
                                      # (also: how far overhead a wall may sit and still
                                      #  count as the breakout in front of us)

    # --- the trigger --------------------------------------------------------
    nearby_wall_atr: float = 1.0      # a trigger under a wall this close is raised to the wall

    # --- the stop -----------------------------------------------------------
    stop_buffer_atr: float = 0.15     # the cushion; a stop never sits exactly on the low
    noise_floor_atr: float = 0.7      # closer than this to the trigger is inside daily noise
    no_structure_atr: float = 2.0     # the fallback distance when the chart offers nothing

    # --- target 1 -----------------------------------------------------------
    target_min_atr: float = 1.5       # a level this far out...
    target_min_rr: float = 2.0        # ...needs this reward-to-risk
    target_near_atr: float = 1.0      # a nearer level, down to here...
    target_near_rr: float = 2.5       # ...needs a stricter one. Closer than target_near_atr
                                      #    is never a target, whatever it pays.
    base_max_height_atr: float = 2.0  # a stretch of bars is "sideways" if it fits in this
    base_min_bars: int = 5            # ...and lasts at least this long
    base_max_bars: int = 60           # ...and no longer than this

    def replace(self, **changes) -> "Params":
        """A copy with some numbers changed -- the entry point for a future sweep."""
        return dataclasses.replace(self, **changes)

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)

    def fingerprint(self) -> str:
        """A short stable hash of every value, to stamp on results.

        Two results carrying the same fingerprint were produced by the same
        numbers. Two carrying different ones cannot be compared without saying
        so out loud."""
        blob = json.dumps(self.as_dict(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


# The frozen stage-0 set. Do not edit during a study: change it, and every
# result produced before the change becomes a different experiment.
DEFAULT = Params()


if __name__ == "__main__":
    print(f"fingerprint: {DEFAULT.fingerprint()}")
    for key, value in DEFAULT.as_dict().items():
        print(f"  {key:24s} {value}")
