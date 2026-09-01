"""Numeric setup-quality rubric (2026-07-29, CONSISTENCY_RULES.md rule 27).

Same reasoning as regime_formula.py (rule 23): the A-F rubric used to be a
fresh, hand-totaled judgment call every time it ran, and it only ever ran
once, at /screener build time -- SCREENER_v3.md used to say outright that
/monitor never re-checks it. Found real, 2026-07-21/27: WGMI graded D at
build time and still produced an actionable "Starter allowed" /monitorall
headline six days later, because nothing ever re-scored it.

Scope, precisely: this module ONLY scores the 6 already-defined criteria
from numbers that are already computed elsewhere (fetch_analysis_data.py /
fetch_monitor_data.py) plus a setup's own already-chosen trigger/stop/
target. It does NOT pick which level is the target -- wall-chaining,
Fibonacci/VWAP anchor selection stay Category B, unchanged, same scope
discipline as regime_formula.py's own docstring draws for regime override.
"""

from dataclasses import dataclass, field
from typing import Optional

# First-draft thresholds -- documented, not backtested, same honest-caveat
# posture as regime_formula.py's score cutoffs and CONSISTENCY_RULES.md
# rule 4's 0.7x ATR noise floor.
RR_MIN = 2.3
TARGET_ATR_MIN = 1.5
SMA20_EXTENSION_MAX_ATR = 2.0
EARNINGS_WINDOW_DAYS = 10  # trading days; SCREENER_v3.md never gave a number for this

SUPPORTIVE_REGIMES = {"healthy_uptrend", "pullback_in_uptrend", "risk_on"}

# 2026-08-02: market regime was REMOVED from the score. Three reasons, all real:
#
# 1. It was already counted twice elsewhere. Rule 18 hard-blocks any buy in
#    risk_off/structure_break, and neutral_choppy already halves position size.
#    Scoring it a third time didn't add caution, it just moved every grade down.
# 2. It contradicted this system's own backtest. Rule 26 (305 trades) found
#    Breakout/Retest was NEGATIVE in healthy_uptrend (-0.12R, the only losing
#    regime) and BEST in neutral_choppy (+0.36R). The list above hands a point
#    to the worst regime for that setup type and withholds one from the best.
# 3. The live effect was grade collapse. The regime formula read neutral_choppy
#    every day from 2026-07-20 onward, so criterion 3 auto-failed on every
#    thesis, capping the ceiling at B and making D/F the normal outcome. A gate
#    that fires on nearly everything stops being read -- two real fills were
#    taken against it in the last week of July.
#
# Regime still appears in every report, still blocks, still resizes. It just no
# longer moves the letter. Kept in RubricInputs (below) so callers and stored
# decision JSON don't change shape, and so the criteria dict still REPORTS it.
SCORED_CRITERIA = ("rr", "target_atr", "rs", "sma20_extension", "event")

_CUTOFF_A = 5
_CUTOFF_B = 4
_CUTOFF_C = 3
# below _CUTOFF_C -> D. There is no F: with five criteria, "2 or fewer" is
# already the bottom of the scale, and D and F blocked exactly the same things
# (the Starter option and the buy order, per rule 27), so a second failing
# letter carried no extra meaning.


@dataclass
class RubricInputs:
    """All six already-computed Category A figures for one setup (Primary or
    Alternate) -- nothing here is judgment. rs_delta_pct must already be the
    correct window for this setup's type (20d trend-following / 5d reversal,
    SCREENER_v3.md's own RS-window rule) -- picking the window is the
    caller's job, not this module's."""
    rr: float
    target_atr_multiple: float
    regime: str
    rs_delta_pct: float
    dist_sma20_atr: float
    earnings_days_out: Optional[int]  # None = unverified/unknown -> conservative no-point


@dataclass
class RubricResult:
    grade: str
    score: int
    criteria: dict = field(default_factory=dict)


def classify_rubric(inputs: RubricInputs) -> RubricResult:
    """The full 6-criterion score, purely from the numeric inputs above --
    see this module's own docstring for what it deliberately does NOT do
    (choose the target itself)."""
    criteria = {
        "rr": inputs.rr >= RR_MIN,
        "target_atr": inputs.target_atr_multiple >= TARGET_ATR_MIN,
        # Reported, never scored -- see SCORED_CRITERIA above for why. Kept in
        # this dict so every existing reader (MONITOR_v2.md's "🔎 נכשל ב" list,
        # report_lint.py, stored decision JSON) still sees the regime verdict.
        "regime": inputs.regime in SUPPORTIVE_REGIMES,
        "rs": inputs.rs_delta_pct > 0,
        "sma20_extension": abs(inputs.dist_sma20_atr) <= SMA20_EXTENSION_MAX_ATR,
        "event": inputs.earnings_days_out is not None and inputs.earnings_days_out > EARNINGS_WINDOW_DAYS,
    }
    score = sum(1 for key in SCORED_CRITERIA if criteria[key])

    if score >= _CUTOFF_A:
        grade = "A"
    elif score >= _CUTOFF_B:
        grade = "B"
    elif score >= _CUTOFF_C:
        grade = "C"
    else:
        grade = "D"

    return RubricResult(grade=grade, score=score, criteria=criteria)


def _main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rr", type=float, required=True)
    parser.add_argument("--target-atr", type=float, required=True)
    parser.add_argument("--regime", required=True)
    parser.add_argument("--rs-delta", type=float, required=True)
    parser.add_argument("--dist-sma20-atr", type=float, required=True)
    parser.add_argument("--earnings-days-out", type=int, default=None)
    args = parser.parse_args()

    result = classify_rubric(RubricInputs(
        rr=args.rr,
        target_atr_multiple=args.target_atr,
        regime=args.regime,
        rs_delta_pct=args.rs_delta,
        dist_sma20_atr=args.dist_sma20_atr,
        earnings_days_out=args.earnings_days_out,
    ))
    print(json.dumps({"grade": result.grade, "score": result.score, "criteria": result.criteria}))


if __name__ == "__main__":
    _main()
