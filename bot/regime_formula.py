"""Numeric market-regime classifier (2026-07-20, CONSISTENCY_RULES.md rule 23).

Replaces the previous fully-qualitative "read the SPY/QQQ chart" regime call
with a mechanical, reproducible score. Found in the strategy review: the
regime gate (rule 18) is the single highest-leverage decision in the whole
system -- it alone decides whether ANY buy order is allowed -- yet had no
objective backstop at all, just a fresh AI judgment call every run with no
way to check consistency or backtest whether the calls were ever right.

Scope, precisely: this module ONLY scores SPY+QQQ trend/structure into one of
the six existing regime labels. It does NOT decide whether to override that
score for a real-world reason (a scheduled Fed meeting, breaking news) -- an
AI session may still flag such a concern as a plainly-visible note, but never
silently substitute its own regime call for this module's output. See rule 23
for the exact disclosure requirement report_lint.py checks.

All four inputs below are already computed elsewhere in this project
(fetch_analysis_data.py) for the ticker being screened -- this module just
needs the same figures for SPY and QQQ specifically, which is new (until now
only SMA20/50 were fetched for the two indexes, not SMA150/ATR14/swing
structure).
"""

from dataclasses import dataclass, field
from typing import Optional

N_PIVOT = 3  # bars on each side, same convention as fetch_analysis_data.py's own pivot detection

# Score-range cutoffs -- a reasonable first draft, NOT derived from backtested
# data (same honest caveat as CONSISTENCY_RULES.md rule 4's 0.7x ATR noise
# floor: a sensible starting point, worth checking against real history once
# there's a track record, not something to trust blindly forever).
_CUTOFF_RISK_ON = 8
_CUTOFF_HEALTHY_UPTREND = 4
_CUTOFF_PULLBACK = 1
_CUTOFF_NEUTRAL_CHOPPY = -3
_CUTOFF_RISK_OFF = -7  # below this -> structure_break candidate, pending confirmation


@dataclass
class IndexSnapshot:
    """One index's (SPY or QQQ) inputs to the formula, all already-computed
    Category A figures -- nothing here is judgment."""
    price: float
    sma20: float
    sma50: float
    sma150: float
    swing_highs: list  # chronological order (oldest first), raw prices, no judgment filtering
    swing_lows: list   # chronological order (oldest first), raw prices
    lookback_low: float  # lowest low over the structure-break confirmation window (e.g. 6 months)


@dataclass
class RegimeResult:
    regime: str
    score: int
    structure_break_confirmed: bool
    components: dict = field(default_factory=dict)


def find_swing_highs(highs: list[float], n_pivot: int = N_PIVOT) -> list[float]:
    """Chronological (oldest-first) list of local highs -- a bar higher than
    every bar within n_pivot on both sides. Deliberately narrow, mechanical
    pivot detection only -- never picks which pivot "matters" for a thesis,
    same scope discipline as indicators_core.py's own docstring."""
    return [
        highs[i] for i in range(n_pivot, len(highs) - n_pivot)
        if all(highs[i] > highs[i - k] for k in range(1, n_pivot + 1))
        and all(highs[i] > highs[i + k] for k in range(1, n_pivot + 1))
    ]


def find_swing_lows(lows: list[float], n_pivot: int = N_PIVOT) -> list[float]:
    return [
        lows[i] for i in range(n_pivot, len(lows) - n_pivot)
        if all(lows[i] < lows[i - k] for k in range(1, n_pivot + 1))
        and all(lows[i] < lows[i + k] for k in range(1, n_pivot + 1))
    ]


def _ma_stack_score(price: float, sma20: float, sma50: float, sma150: float) -> int:
    """-3 to +3: three yes/no checks on moving-average order, +1 per "yes",
    -1 per "no". All three "yes" (price > sma20 > sma50 > sma150) = +3, clean
    uptrend. All three "no" = -3, clean downtrend."""
    return (
        (1 if price > sma20 else -1)
        + (1 if sma20 > sma50 else -1)
        + (1 if sma50 > sma150 else -1)
    )


def _sequence_score(values: list[float]) -> int:
    """+1 if strictly ascending, -1 if strictly descending, 0 otherwise
    (mixed, flat, or fewer than 2 points to compare). Uses only the last 3
    values (the most recent swing points) -- older structure matters less
    for "what's happening right now"."""
    recent = values[-3:]
    if len(recent) < 2:
        return 0
    diffs = [recent[i + 1] - recent[i] for i in range(len(recent) - 1)]
    if all(d > 0 for d in diffs):
        return 1
    if all(d < 0 for d in diffs):
        return -1
    return 0


def _structure_score(swing_highs: list[float], swing_lows: list[float]) -> int:
    """-2 to +2: ascending recent highs (+1) or descending (-1), same for
    recent lows, summed. Turns "does this look like an uptrend" into a
    computable fact instead of an eyeballed read of the chart."""
    return _sequence_score(swing_highs) + _sequence_score(swing_lows)


# Plain-Hebrew name + what this reading usually means + what the user might do
# about it. ADVISORY ONLY, by explicit user direction (2026-08-03): market
# condition is a caution sign with a recommendation, never an automatic size
# change -- "we don't know yet how it actually impacts the trades", and this
# system's own backtest (rule 26) agrees the effect is not what anyone assumed
# (Breakout/Retest was NEGATIVE in healthy_uptrend and BEST in neutral_choppy).
# The only mechanical thing regime still does is rule 18's hard block on
# risk_off/structure_break -- see that rule for why the extreme case stays a
# gate while the middle ground became advice.
_ADVICE_HE = {
    "risk_on": ("שוק חזק",
                 "המדדים עולים יחד וכל הממוצעים מסודרים למעלה.",
                 "אפשר לפעול רגיל. זה לא אישור לקנות יותר מהמותר."),
    "healthy_uptrend": ("מגמה עולה בריאה",
                         "המדדים מעל הממוצעים שלהם, בלי סימני שבירה.",
                         "אפשר לפעול רגיל."),
    "pullback_in_uptrend": ("ירידה זמנית בתוך מגמה עולה",
                             "המגמה הגדולה עדיין למעלה, אבל יש נסיגה כרגע.",
                             "אפשר לפעול רגיל. שווה להעדיף כניסות קרובות לתמיכה על פני רדיפה אחרי פריצות."),
    "neutral_choppy": ("שוק מבולבל, בלי כיוון ברור",
                        "המדדים מתנדנדים למעלה ולמטה בלי מגמה — פריצות נוטות להיכשל יותר מהרגיל.",
                        "כדאי להיות בררן: פחות טריידים, רק הסטאפים הכי טובים. הגודל לא יורד אוטומטית — זו החלטה שלך."),
    "risk_off": ("שוק חלש",
                  "המדדים מתחת לממוצעים שלהם והמבנה יורד.",
                  "אין קניות חדשות — זה כן חוק, לא המלצה. מחזיקים מה שיש ומגנים עם סטופ."),
    "structure_break": ("שבירת מבנה",
                         "המדדים סגרו מתחת לשפל של החודשים האחרונים.",
                         "אין קניות חדשות — זה כן חוק, לא המלצה. מגנים על מה שכבר פתוח."),
}

BLOCKING_REGIMES = {"risk_off", "structure_break"}


def describe_regime_he(regime: str, score: Optional[int] = None) -> str:
    """The market-condition block appended to every /screener Telegram summary
    (deliver_report.py), in the beginner-facing wording SCREENER_v3.md section
    ח mandates -- no score jargon, no rule numbers, no English tokens.

    Says three things and no more: what the market looks like right now, what
    that usually means, and what the user might do about it -- explicitly
    labelled as advice, not a rule, except for the two blocking readings where
    it says outright that this one IS a rule. An unknown label returns "" so a
    stale or misspelled regime string never invents advice it can't back."""
    entry = _ADVICE_HE.get(regime)
    if not entry:
        return ""
    name, meaning, advice = entry
    lines = [f"🌍 <b>מצב השוק כרגע: {name}</b>", f"ℹ️ {meaning}"]
    if regime in BLOCKING_REGIMES:
        lines.append(f"🛑 {advice}")
    else:
        lines.append(f"💭 המלצה (לא חוק — ההחלטה שלך): {advice}")
        lines.append("📎 מצב השוק לא מקטין את גודל הקנייה בעצמו. הוא נרשם בכל טרייד, "
                      "כדי שבעוד כמה חודשים נוכל לבדוק מהנתונים האמיתיים אם הוא בכלל משנה משהו.")
    return "\n".join(lines)


def regime_name_he(regime: str) -> str:
    """Just the plain-words name of a market state, for messages that quote it
    inside a sentence of their own ("built in X, now Y") rather than showing
    the full advice block. Empty string for a label nobody recognises -- an
    unknown market state is never given an invented Hebrew name.

    Same table as the two functions above on purpose: MONITOR_v2.md carries its
    own copy of this translation, and one of the two was already a word apart
    from the other."""
    entry = _ADVICE_HE.get(regime)
    return entry[0] if entry else ""


def regime_headline_he(regime: str) -> str:
    """One-line version of describe_regime_he(), for the TOP of the /monitor
    and /monitorall messages (user's own request, 2026-08-03: "an awareness
    line on top", not the four-line block -- those two reports are read fast,
    often several a night, and a full block on each would train the eye to skip
    it). Same source table, so the two wordings can never drift apart.

    The line always ends by naming which kind of statement it is -- advice or
    rule -- because that distinction is the entire point of the change."""
    entry = _ADVICE_HE.get(regime)
    if not entry:
        return ""
    name, _meaning, advice = entry
    short = advice.split(".")[0].split(" — ")[0].strip()
    kind = "חוק" if regime in BLOCKING_REGIMES else "המלצה, לא חוק"
    return f"🌍 <b>מצב השוק: {name}</b> — {short} ({kind})"


def classify_regime(spy: IndexSnapshot, qqq: IndexSnapshot) -> RegimeResult:
    """The full six-way call, purely from the four numeric inputs above --
    see this module's own docstring for what it deliberately does NOT do
    (override for a real-world reason not visible in the price data)."""
    spy_ma = _ma_stack_score(spy.price, spy.sma20, spy.sma50, spy.sma150)
    qqq_ma = _ma_stack_score(qqq.price, qqq.sma20, qqq.sma50, qqq.sma150)
    spy_structure = _structure_score(spy.swing_highs, spy.swing_lows)
    qqq_structure = _structure_score(qqq.swing_highs, qqq.swing_lows)

    if spy_ma == 0 or qqq_ma == 0:
        agreement = 0
    elif (spy_ma > 0) == (qqq_ma > 0):
        agreement = 1  # both point the same way -- reinforcing
    else:
        agreement = -1  # a real disagreement -- pulls toward neutral/choppy, not averaged away

    total = spy_ma + qqq_ma + spy_structure + qqq_structure + agreement

    if total >= _CUTOFF_RISK_ON:
        regime = "risk_on"
    elif total >= _CUTOFF_HEALTHY_UPTREND:
        regime = "healthy_uptrend"
    elif total >= _CUTOFF_PULLBACK:
        regime = "pullback_in_uptrend"
    elif total >= _CUTOFF_NEUTRAL_CHOPPY:
        regime = "neutral_choppy"
    elif total >= _CUTOFF_RISK_OFF:
        regime = "risk_off"
    else:
        regime = "structure_break"  # candidate -- confirmed or downgraded below

    # Structure Break needs a real, confirmed broken level -- not just a very
    # negative score. A deep-but-still-above-support pullback is Risk Off,
    # not an overstated Structure Break it didn't actually earn.
    structure_break_confirmed = False
    if regime == "structure_break":
        structure_break_confirmed = spy.price < spy.lookback_low or qqq.price < qqq.lookback_low
        if not structure_break_confirmed:
            regime = "risk_off"

    return RegimeResult(
        regime=regime, score=total, structure_break_confirmed=structure_break_confirmed,
        components={
            "spy_ma_stack": spy_ma, "qqq_ma_stack": qqq_ma,
            "spy_structure": spy_structure, "qqq_structure": qqq_structure,
            "agreement": agreement,
        },
    )
