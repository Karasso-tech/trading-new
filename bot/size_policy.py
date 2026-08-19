"""Position-size policy: the floor and ceiling on how much risk one trade may
actually carry (CONSISTENCY_RULES.md rule 28, added 2026-08-02).

Why this module exists -- found in the 2026-08-02 trade review, from the real
`positions` table, not from theory:

    risk actually taken per trade ranged from 0.08% to 2.26% of equity,
    against a stated 1% rule. The typical trade risked 0.2%-0.4%.

Both ends were broken, and each end broke for its own reason:

  - TOO SMALL (the common case, and the expensive one). SCREENER_v3.md section
    ד stacked three independent shrink multipliers -- volatility (0.5/0.75/1.0),
    rule 18's neutral_choppy ×0.5, rule 22's weak-breakout-volume ×0.5 -- with
    nothing bounding their product. All three together is ×0.125. Worse, the
    regime formula has read `neutral_choppy` every single day since
    2026-07-20, so the middle multiplier was not an occasional derate, it was
    effectively permanent. Real consequence: XLF, the best trade in the whole
    book at +1.79R, was sized at 0.20% risk and made $543 instead of ~$2,700.
    A correct call sized at a quarter is a quarter of a correct call.
    (2026-08-03: the regime multiplier itself was then removed entirely --
    see ADVISORY_MULTIPLIER_KEYS below. The floor stays, because volatility
    and breakout-volume can still stack to ×0.25 on their own.)
  - TOO BIG (rare, but half of all realized losses). CRDO was entered at 2.26%
    risk -- 2.3x the rule -- on the system's own F-graded idea, and lost
    $2,641, which is 48% of every dollar lost across all closed trades.

So: multipliers may halve a position. They may never quarter or eighth it, and
nothing may ever push past the full 1% either. Both bounds are hard.

Deliberately NOT in scope, so this stays one decision and not three:
  - No upsize multiplier for an A-grade setup. Every multiplier here is <=1.0
    and this module keeps it that way; adding an upsize path is a real
    strategy change that needs its own decision, not a side effect of adding
    a floor.
  - Nothing here picks the trigger or the stop. Same Category A/B split as
    rubric_formula.py and regime_formula.py: this module is pure arithmetic on
    numbers already chosen elsewhere.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

# The product of every shrink multiplier is clamped into this band. 0.5 is the
# floor because a single documented derate (volatility=high, OR choppy, OR weak
# volume) is exactly ×0.5 -- so the floor says "one derate's worth of caution
# is the most caution any stack of them may express", never a compounding one.
MULTIPLIER_FLOOR = 0.5
MULTIPLIER_CEILING = 1.0

# 2026-08-03, second sizing decision, from the 15-year / 43,957-break backtest:
# the VOLATILITY and VOLUME multipliers were removed too. Every trade is now a
# full 1% risk.
#
# The money argument: flat 1% made $6,817,869 across the backtest; the same
# trades with the multipliers made $4,147,346. The multipliers cost $2.67M --
# not by picking worse trades, only by betting small on the good ones.
#
# The better argument, and the one that actually settles it: risk-based sizing
# ALREADY handles volatility. A jumpy stock needs a wider stop, so 1% risk
# automatically buys fewer shares. The volatility multiplier then cut that
# again -- the same fact charged twice. And the volume derate was pointing the
# wrong way outright: below-average-volume breaks measured BETTER (+0.13R vs
# +0.08R, t=3.6), so halving size there was backwards as well as redundant.
#
# What remains: rule 28's floor and ceiling, which now do the whole job. Any
# multiplier that still arrives from an older decision JSON is ignored, for the
# same reason the regime one is -- a silent re-introduction is exactly the
# failure this guards against.
#
# Market condition is ADVISORY as of 2026-08-03, by explicit user direction:
# "the market condition should be a caution sign with a recommendation, not
# something absolute -- since we don't know how it impacts the trades." So a
# `choppy`/`regime` entry may still be PASSED here (older decision JSON, and
# the sizing table still shows the column) but is never multiplied in. Kept as
# a named ignore rather than a caller-side omission because a silent
# re-introduction of this multiplier is exactly the failure this guards: it was
# the permanent ×0.5 -- on every trading day since 2026-07-20 -- that turned a
# 1% rule into a 0.2%-0.4% one in practice.
#
# What regime still does: rule 18's hard block on risk_off/structure_break (no
# buying at all), and a plain-words advisory block in every report
# (regime_formula.describe_regime_he). What it no longer does: change the size.
ADVISORY_MULTIPLIER_KEYS = {"choppy", "regime", "market", "market_regime",
                             "volatility", "volume"}

# Final, after-rounding bounds on the trade's real dollar risk, as a fraction
# of the account's own full-risk figure (equity x risk_pct, i.e. 1% today).
RISK_FRACTION_FLOOR = 0.5
RISK_FRACTION_CEILING = 1.0

# 2026-08-09. With every multiplier gone (see ADVISORY_MULTIPLIER_KEYS above),
# the 0.5-1.0 band no longer describes a range to pick inside -- there is
# nothing left that may legitimately derate a position, so the only correct
# answer is a full risk unit. The band stays as the hard legal boundary; this
# is the separate line that says "and anything under here needs a reason".
#
# It is not the same thing as the floor, and collapsing the two would be wrong
# in both directions: raising the floor to 0.9 would make a genuinely
# cash-limited fill illegal (the account simply did not hold the cash for the
# full quantity on the day the trigger fired -- real, twice, 2026-08-03 and
# 2026-08-06), while leaving only the 0.5 floor lets a silently-halved position
# pass as though somebody chose it.
RISK_FRACTION_FULL_MIN = 0.9

# Whole-share rounding cannot land exactly on a bound. 2% of the target risk is
# the slack a bound check allows before calling it a real violation -- the same
# "a floor check, not exact equality" posture report_lint.py already takes for
# rule 4's noise floor and rule 24's stop buffer.
RISK_FRACTION_TOLERANCE = 0.02


@dataclass
class MultiplierResult:
    raw: float          # the product as the protocol's own columns computed it
    applied: float      # after the floor/ceiling clamp -- what sizing actually uses
    floored: bool       # True iff the clamp actually changed the number
    parts: dict = field(default_factory=dict)


@dataclass
class SizeResult:
    qty: int
    risk_usd_actual: float
    risk_fraction_of_full: float    # 1.0 == a full-size position
    cost_usd: float
    risk_per_share: float
    multiplier: MultiplierResult
    full_qty: int                   # what a full 1% position would have been
    reason_skipped: Optional[str] = None   # set only when qty comes back 0


def clamp_multiplier(parts: Iterable[float] | dict) -> MultiplierResult:
    """Multiply every shrink multiplier together, then clamp into
    [MULTIPLIER_FLOOR, MULTIPLIER_CEILING]. `parts` may be a plain sequence or
    a {name: value} dict -- the dict form is kept in the result so a report can
    still show each column separately, which SCREENER_v3.md section ד requires
    (never merge the columns into one silent number).

    Keys named in ADVISORY_MULTIPLIER_KEYS are kept in `parts` for display but
    never multiplied in -- see that constant for why."""
    if isinstance(parts, dict):
        named = {k: float(v) for k, v in parts.items() if v is not None}
        values = [v for k, v in named.items() if k.lower() not in ADVISORY_MULTIPLIER_KEYS]
    else:
        values = [float(v) for v in parts if v is not None]
        named = {str(i): v for i, v in enumerate(values)}

    raw = 1.0
    for v in values:
        raw *= v
    applied = min(MULTIPLIER_CEILING, max(MULTIPLIER_FLOOR, raw))
    return MultiplierResult(
        raw=raw, applied=applied, floored=abs(applied - raw) > 1e-9, parts=named
    )


def size_position(risk_usd_target: float, entry: float, stop: float,
                  multipliers: Iterable[float] | dict = ()) -> SizeResult:
    """Whole-share quantity for one trade, with the rule-28 bounds enforced.

    risk_usd_target is the account's FULL risk figure (effective equity x
    risk_pct) -- not an already-derated number. Derates come in through
    `multipliers` so the clamp can see the whole stack at once, which is the
    entire point: applied one at a time, each ×0.5 looks reasonable.

    Long-only, matching the rest of this system (STRATEGY_v3.md): entry must be
    above stop. qty comes back 0 with `reason_skipped` set when a single share
    already risks more than a full position allows -- that is a real, honest
    "this trade cannot be sized within the rules", never a silently oversized
    fill."""
    if risk_usd_target is None or risk_usd_target <= 0:
        raise ValueError(f"risk_usd_target must be positive, got {risk_usd_target!r}")
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        raise ValueError(f"entry ({entry}) must be above stop ({stop}) -- long-only system")

    mult = clamp_multiplier(multipliers)
    full_qty = int(math.floor(risk_usd_target / risk_per_share))

    min_risk = risk_usd_target * RISK_FRACTION_FLOOR
    max_risk = risk_usd_target * RISK_FRACTION_CEILING

    qty = int(round(risk_usd_target * mult.applied / risk_per_share))
    # Whole-share rounding can land just outside either bound; walk it back in.
    while qty > 0 and qty * risk_per_share > max_risk + 1e-9:
        qty -= 1
    while (qty + 1) * risk_per_share <= max_risk + 1e-9 and qty * risk_per_share < min_risk - 1e-9:
        qty += 1

    reason = None
    if qty <= 0 or qty * risk_per_share > max_risk + 1e-9:
        qty = 0
        reason = ("מרחק הכניסה-סטופ גדול מדי: אפילו מניה אחת מסכנת יותר מפוזיציה מלאה "
                  "— הטרייד הזה לא ניתן לגודל לפי הכללים")

    risk_actual = qty * risk_per_share
    return SizeResult(
        qty=qty,
        risk_usd_actual=risk_actual,
        risk_fraction_of_full=(risk_actual / risk_usd_target) if risk_usd_target else 0.0,
        cost_usd=qty * entry,
        risk_per_share=risk_per_share,
        multiplier=mult,
        full_qty=full_qty,
        reason_skipped=reason,
    )


def evaluate_order(qty: int, entry: float, stop: float, risk_usd_target: float,
                    multipliers: Iterable[float] | dict = ()) -> SizeResult:
    """Same measurements as size_position(), for a quantity somebody ALREADY
    chose -- the report's own order, or a real filled position. size_position()
    answers "how many shares should this be"; this answers "how big is the one
    in front of me, really". Both produce the identical SizeResult so the same
    formatter and the same bounds check work on either."""
    if risk_usd_target is None or risk_usd_target <= 0:
        raise ValueError(f"risk_usd_target must be positive, got {risk_usd_target!r}")
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        raise ValueError(f"entry ({entry}) must be above stop ({stop}) -- long-only system")

    risk_actual = qty * risk_per_share
    return SizeResult(
        qty=qty,
        risk_usd_actual=risk_actual,
        risk_fraction_of_full=risk_actual / risk_usd_target,
        cost_usd=qty * entry,
        risk_per_share=risk_per_share,
        multiplier=clamp_multiplier(multipliers),
        full_qty=int(math.floor(risk_usd_target / risk_per_share)),
        reason_skipped=None,
    )


def fraction_within_bounds(fraction: float) -> bool:
    """Does a real, already-taken position's risk fraction respect rule 28?
    Used by report_lint.py on a report's own numbers, and usable later on a
    filled position -- same check either way."""
    return (RISK_FRACTION_FLOOR - RISK_FRACTION_TOLERANCE) <= fraction <= (
        RISK_FRACTION_CEILING + RISK_FRACTION_TOLERANCE)


def is_full_size(fraction: float) -> bool:
    """True when this position carries a full risk unit (2026-08-09).

    Separate from fraction_within_bounds() on purpose -- see
    RISK_FRACTION_FULL_MIN. A False here is not automatically wrong; it means
    "this is smaller than the rule's default and something should say why".
    The same tolerance the bounds check uses applies, so whole-share rounding
    a cent under the line never reads as a deliberate derate."""
    return fraction >= RISK_FRACTION_FULL_MIN - RISK_FRACTION_TOLERANCE


def describe_fraction_he(fraction: float) -> str:
    """Plain-Hebrew name for how big this position actually is, in the
    beginner-facing wording SCREENER_v3.md section ח mandates (no ATR, no
    "multiplier", no rule numbers). This is the line that answers the question
    the Telegram size block could not answer before: full position, or small?"""
    if fraction >= 0.90:
        return "פוזיציה מלאה"
    if fraction >= 0.65:
        return "כשלושה רבעים מפוזיציה מלאה"
    if fraction >= 0.40:
        return "חצי פוזיציה"
    if fraction > 0:
        return "פחות מחצי פוזיציה — קטן מדי לפי הכללים"
    return "לא ניתן לפתוח פוזיציה בגודל חוקי"


def format_size_lines_he(result: SizeResult, risk_usd_target: float,
                          equity_usd: Optional[float] = None,
                          reduction_reason: Optional[str] = None) -> str:
    """The deterministic block appended to every /screener Telegram summary.

    Computed in Python and appended by deliver_report.py rather than left to
    the model's own template, for the same reason
    deliver_position_status_report.py computes its stop-distance line itself:
    the number that decides whether an order is the right size must not depend
    on a model remembering to write it."""
    pct_of_equity = (result.risk_usd_actual / equity_usd * 100) if equity_usd else None
    label = describe_fraction_he(result.risk_fraction_of_full)

    lines = ["📏 <b>בדיקת גודל</b>"]
    if result.qty <= 0:
        lines.append(f"🚫 {result.reason_skipped}")
        lines.append(f"ℹ️ פוזיציה מלאה = <b>${risk_usd_target:,.0f}</b> בסיכון")
        return "\n".join(lines)

    risk_line = f"🎲 כסף בסיכון בטרייד הזה: <b>${result.risk_usd_actual:,.0f}</b>"
    if pct_of_equity is not None:
        risk_line += f" (<b>{pct_of_equity:.2f}%</b> מהתיק)"
    lines.append(risk_line)
    lines.append(
        f"📐 זו <b>{label}</b> — פוזיציה מלאה כאן = <b>${risk_usd_target:,.0f}</b> "
        f"בסיכון = <b>{result.full_qty}</b> מניות"
    )
    if result.multiplier.floored and result.multiplier.raw < result.multiplier.applied:
        lines.append(
            "🛟 החישוב המקורי הקטין את הקנייה יותר מדי (מתחת לחצי פוזיציה) — "
            "הוגדל בחזרה לחצי פוזיציה, שזה המינימום המותר"
        )
    elif result.multiplier.floored:
        lines.append("🛟 הגודל הוגבל כלפי מטה לפוזיציה מלאה — לא קונים יותר מזה")
    if not fraction_within_bounds(result.risk_fraction_of_full):
        # A stated reason changes what this line should SAY. Found live on ORCL,
        # 2026-08-09: the message showed "buy 73 shares" and then "do not send
        # this order as it is", with the actual cause (not enough free cash) two
        # blocks further up in a different list. Four signals, no instruction.
        if reduction_reason:
            lines.append(
                f"ℹ️ הקנייה קטנה מהמותר בכללים, והסיבה היא: {reduction_reason}. "
                f"זו לא טעות — פשוט אין כרגע מספיק כדי לקנות פוזיציה בגודל מלא. "
                f"אתה מחליט: לקנות קטן, או לוותר על הרעיון"
            )
        else:
            lines.append("⚠️ הגודל הזה חורג מהטווח המותר (חצי פוזיציה עד פוזיציה מלאה) — לא לשלוח את הפקודה כמו שהיא")
    elif not is_full_size(result.risk_fraction_of_full):
        # 2026-08-09: the band alone said nothing here -- a half position sat
        # inside it and read as normal. Since every derate was removed, small
        # is either a stated choice or a mistake, and the message says which.
        if reduction_reason:
            lines.append(f"ℹ️ הקנייה קטנה מפוזיציה מלאה בכוונה — הסיבה שנרשמה: {reduction_reason}")
        else:
            lines.append(
                "⚠️ הקנייה קטנה מפוזיציה מלאה ואין סיבה רשומה לכך — לפי הכללים כל טרייד נכנס בגודל מלא. "
                "בדוק את המספרים לפני שליחה"
            )
    return "\n".join(lines)


def _main() -> None:
    import argparse
    import json
    import sys

    # The Hebrew size block below is emoji-bearing, and this script's whole
    # reason for existing is that a /screener run calls it instead of doing the
    # arithmetic by hand. Under the Windows console default (cp1255) that print
    # died with UnicodeEncodeError AFTER emitting the JSON -- so the tool
    # appeared to half-work, exited non-zero, and the model's most likely
    # recovery was to compute the quantity itself, which is precisely what rule
    # 28 exists to stop. Found 2026-08-09 while testing the CLI guard above.
    # fetch_analysis_data.py sidesteps the same problem with ensure_ascii; here
    # the text IS the output, so the stream is reconfigured instead.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="rule 28 position sizing")
    parser.add_argument("--risk-usd-target", type=float, required=True)
    parser.add_argument("--entry", type=float, required=True)
    parser.add_argument("--stop", type=float, required=True)
    # NAME=VALUE only, never a bare number (2026-08-09). clamp_multiplier can
    # only honour ADVISORY_MULTIPLIER_KEYS -- the list of derates that were
    # removed and must never come back -- when it can see each entry's NAME. The
    # old `--multiplier 0.5` form arrived as an anonymous sequence, so every
    # retired derate sailed straight past the guard written to stop it and
    # halved the position for real. An unnamed value is now a hard error rather
    # than a silently-applied one.
    parser.add_argument("--multiplier", action="append", default=[], metavar="NAME=VALUE",
                        help="repeatable, NAME=VALUE (e.g. liquidity=0.8). Bare numbers are "
                             "rejected. Retired derates (volatility/volume/choppy/regime) are "
                             "shown but never multiplied in -- see ADVISORY_MULTIPLIER_KEYS.")
    parser.add_argument("--equity", type=float, default=None)
    args = parser.parse_args()

    multipliers: dict[str, float] = {}
    for raw in args.multiplier:
        if "=" not in raw:
            parser.error(
                f"--multiplier must be NAME=VALUE, got {raw!r}. A bare number cannot be checked "
                f"against the retired-derate list and would be applied for real; name it or drop it."
            )
        name, _, value = raw.partition("=")
        try:
            multipliers[name.strip()] = float(value)
        except ValueError:
            parser.error(f"--multiplier {raw!r}: {value!r} is not a number")

    r = size_position(args.risk_usd_target, args.entry, args.stop, multipliers)
    print(json.dumps({
        "qty": r.qty,
        "risk_usd_actual": round(r.risk_usd_actual, 2),
        "risk_fraction_of_full": round(r.risk_fraction_of_full, 4),
        "cost_usd": round(r.cost_usd, 2),
        "full_qty": r.full_qty,
        "multiplier_raw": round(r.multiplier.raw, 4),
        "multiplier_applied": round(r.multiplier.applied, 4),
        "multiplier_floored": r.multiplier.floored,
        "label_he": describe_fraction_he(r.risk_fraction_of_full),
        "is_full_size": is_full_size(r.risk_fraction_of_full),
        "reason_skipped": r.reason_skipped,
    }, ensure_ascii=False))
    print(format_size_lines_he(r, args.risk_usd_target, args.equity))


if __name__ == "__main__":
    _main()
