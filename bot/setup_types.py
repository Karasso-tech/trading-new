"""The closed list of setup names, and the one place that decides what counts
as which (2026-08-09).

Why this module exists. `setup_type` is the field the shadow book groups by --
it is how "which kind of idea actually made money" gets answered at all. It had
never been constrained, and the live `shadow_outcomes` table showed exactly
what that costs:

    setup_type: {'Breakout': 79, 'Reclaim': 78, None: 64, 'Pullback': 33,
                 'Failed Breakdown': 21,
                 'V-reversal / capitulation reclaim (swing, backfilled
                  2026-07-15 from historical data as of 2026-06-29). Sharp
                  decline from 278.56 (2026-05-05) to a 225.55 capitulation
                  low (2026-06-25) on rising volume, huge-volume reversal bar
                  2026-06-26 (248M shares), ...': 6,
                 'Breakout/Continuation (backfilled 2026-07-15 ...)': 2,
                 'Retest': 1}

Two rows out of that table hold a whole paragraph where a label belongs. They
can never be counted with anything, and a group of one is a group that proves
nothing. SCREENER_v3.md section ה already has a place for the full description
of a setup -- the report body. This field is a tally mark.

Scope, deliberately narrow, same posture as rubric_formula.py/regime_formula.py:
this module never decides which setup a chart IS. That is Category B judgment
and stays with the model. It only decides whether the word that came back is
one of the six this system recognises, and normalises the near-misses that a
writer plainly meant.
"""

from __future__ import annotations

import re

from typing import Optional

BREAKOUT = "Breakout"
RETEST = "Retest"
PULLBACK = "Pullback"
RECLAIM = "Reclaim"
FAILED_BREAKDOWN = "Failed Breakdown"
GAP_AND_HOLD = "Gap-and-Hold"

# Order is the display order used anywhere that lists them; nothing depends on
# it otherwise.
SETUP_TYPES = (BREAKOUT, RETEST, PULLBACK, RECLAIM, FAILED_BREAKDOWN, GAP_AND_HOLD)

# Which of the six are reversals (rule 15). This lives HERE, once, because it
# had grown three separate copies -- report_lint.REVERSAL_SETUP_TYPES,
# fetch_monitor_data._REVERSAL_SETUP_TYPES and setup_classifier's own set --
# and three copies of one list is how a list stops agreeing with itself. Rule 15
# is the reason it matters: a reversal is scored on a 5-day relative-strength
# window and everything else on 20 days, because a 20-day window structurally
# fails almost every reversal thesis -- the stock is still dragging the fall it
# is recovering from. A setup missing from this set is silently mis-scored.
REVERSAL_SETUPS = frozenset({RECLAIM, FAILED_BREAKDOWN, GAP_AND_HOLD})

TREND_RS_WINDOW_DAYS = 20
REVERSAL_RS_WINDOW_DAYS = 5


# --- a setup that has no level yet -------------------------------------------
#
# Rule 5 permits a second setup whose level has not formed: "an honest 'not yet
# defined' still satisfies this rule". That permission is right and it was being
# used for two different things.
#
# Read on 2026-08-31 across 28 real pending Alternates: 28 distinct wordings out
# of 28, some Hebrew and some English -- and 25 of them NAMED A PRICE inside the
# sentence. ANET's read "ממתין לירידה לאזור SMA50 (159.91)". 159.91 is the
# trigger. It was sitting in prose because the sentence explained that no order
# was ready yet, and the number came along for the ride.
#
# So two separate things, and only the second one needs words:
#   * a level exists -> it belongs in `trigger`, as a number. The "no order
#     ready yet" wording is a note beside it, not a replacement for it.
#   * no level exists -> one of the phrases below.
#
# A fixed list rather than free text for the same reason rule 29 fixed the
# rejection reasons: the identical situation came back in five spellings and
# "how often does this happen" stopped being answerable. Three of the 28 were
# genuinely level-less, and nobody could have counted them without reading all
# twenty-eight.
PENDING_TRIGGER_PHRASES = (
    "אזור ירידה עמוק יותר — טרם נוצרה רמה",
    "ממתין לנר אישור — הרמה תיקבע אחריו",
    "שפל קפיטולציה חדש — טרם נוצר",
    "ריטסט של הרמה — השפל טרם נוצר",
)

# A price, as opposed to the number inside an indicator's name. Decimals and
# grouped thousands only: "159.91" and "61,750.06" are levels, the 50 in SMA50
# and the 14 in ATR14 are not, and a rule that could not tell them apart would
# fire on every report ever written.
_LEVEL_IN_TEXT = re.compile(
    r"(?<![\d.,])(?:\d{1,3}(?:[,\u00a0 ]\d{3})+(?:\.\d+)?|\d+\.\d+)(?![\d])")


def levels_named_in_text(text) -> list:
    """Price levels written inside a prose trigger, if any.

    A non-empty result means the setup does have a level and the number belongs
    in the numeric field -- not that the wording is wrong."""
    if not isinstance(text, str):
        return []
    return _LEVEL_IN_TEXT.findall(text)


def is_pending_trigger_phrase(text) -> bool:
    """True when a prose trigger uses one of the agreed phrases.

    Matched on the opening, so a phrase may carry a short note after it -- what
    has to be fixed is the part that gets counted, not the whole sentence."""
    if not isinstance(text, str):
        return False
    cleaned = " ".join(text.split())
    return any(cleaned.startswith(phrase) for phrase in PENDING_TRIGGER_PHRASES)


def is_reversal(setup_type: Optional[str]) -> bool:
    """True for the three reversal shapes, matched through canonical() so a
    near-miss spelling ("failed breakdown", "Breakout/Continuation") lands on
    the right side of the line rather than defaulting to trend-following."""
    return canonical(setup_type) in REVERSAL_SETUPS


def rs_window_days(setup_type: Optional[str]) -> int:
    """Rule 15's relative-strength window for this setup type."""
    return REVERSAL_RS_WINDOW_DAYS if is_reversal(setup_type) else TREND_RS_WINDOW_DAYS

# Spellings that mean one of the six and are not worth rejecting a whole
# screener run over. Matched after lowercasing and squashing whitespace.
# Deliberately short: this is a list of real spellings seen in the live DB, not
# an open-ended synonym engine. A new entry belongs here only after something
# real produced it.
_ALIASES = {
    "gap and hold": GAP_AND_HOLD,
    "gap-and-hold": GAP_AND_HOLD,
    "gap_and_hold": GAP_AND_HOLD,
    "failed breakdown": FAILED_BREAKDOWN,
    "failed-breakdown": FAILED_BREAKDOWN,
    "breakout/continuation": BREAKOUT,
    "breakout / continuation": BREAKOUT,
    "retest/continuation": RETEST,
    "retest / continuation": RETEST,
    "failed breakdown/reclaim": FAILED_BREAKDOWN,
    "failed breakdown / reclaim": FAILED_BREAKDOWN,
    "v-reversal": RECLAIM,
    "capitulation reclaim": RECLAIM,
}

# Last-resort word match for the historical rows that hold a paragraph. Order
# matters: "failed breakdown" must be tested before the plain words, since a
# capitulation paragraph mentions a great many things.
_PROSE_MARKERS = (
    ("failed breakdown", FAILED_BREAKDOWN),
    ("gap-and-hold", GAP_AND_HOLD),
    ("gap and hold", GAP_AND_HOLD),
    ("reclaim", RECLAIM),
    ("pullback", PULLBACK),
    ("retest", RETEST),
    ("breakout", BREAKOUT),
)


def _leading_label(key: str) -> str:
    """The part of a prose value that was meant to be the label.

    Every real paragraph row in this DB has the same shape -- the label, then
    the essay in brackets or after a full stop:

        "V-reversal / capitulation reclaim (swing, backfilled 2026-07-15 ...)"
        "Breakout/Continuation (backfilled 2026-07-15 from historical data ...)"
        "Core/Layer1 holding (ETF, backfilled 2026-07-15 from historical ...)"

    So the search is confined to the head. Scanning the WHOLE string was the
    first version and it was wrong for real: the third row above mentions a
    pullback somewhere in its body, and got labelled `Pullback` -- a Core ETF
    holding turned into a swing setup that never existed, in the very table
    meant to hold only real ones. Caught on the cleanup dry-run, which is what
    a dry-run is for."""
    for sep in ("(", ".", ",", ";", " -- ", " — "):
        head, found, _ = key.partition(sep)
        if found:
            key = head
    return key.strip()

# A value at or above this many characters is prose, not a label, and only the
# best-effort prose path may touch it -- never the strict writer path. The
# longest real label is "Failed Breakdown" at 16.
PROSE_LENGTH = 40


def _key(value: str) -> str:
    return " ".join(str(value).split()).lower()


def canonical(value: Optional[str]) -> Optional[str]:
    """The one of the six this value means, or None when it means none of them.

    Best-effort by design, in three widening steps: exact (case- and
    whitespace-insensitive), then a known alias, then -- only for a value long
    enough to be prose -- the first recognisable setup word inside it. The last
    step exists for the historical rows that already hold a paragraph; a fresh
    write never reaches it, because require() rejects prose outright.

    Returns None for things that are genuinely not setups at all, e.g. the
    backfilled "Legacy holding (...)" rows. None is the honest answer there --
    inventing a label for a position that was never screened would put made-up
    rows into the very table meant to hold only real ones.
    """
    if value is None:
        return None
    key = _key(value)
    if not key:
        return None
    for known in SETUP_TYPES:
        if _key(known) == key:
            return known
    if key in _ALIASES:
        return _ALIASES[key]
    # "Breakout/Continuation" and friends: take the part before the slash and
    # try again, before falling back to scanning prose.
    head = key.split("/")[0].strip()
    if head != key:
        for known in SETUP_TYPES:
            if _key(known) == head:
                return known
        if head in _ALIASES:
            return _ALIASES[head]
    if len(key) >= PROSE_LENGTH:
        head = _leading_label(key)
        for marker, setup in _PROSE_MARKERS:
            if marker in head:
                return setup
    return None


def require(value: Optional[str], label: str = "setup") -> Optional[str]:
    """The strict writer path: normalise, or raise with a message that says what
    to send instead.

    None passes through untouched -- a setup genuinely may not have a type yet
    (a sleeve-only stub, a position backfilled without a thesis), and inventing
    one would be worse than leaving the field empty.

    Prose is rejected rather than mined for a keyword, even though canonical()
    could often guess: the whole point is that the writer sends a label. Letting
    a paragraph through because it happens to contain the word "breakout" is how
    the field filled up with paragraphs in the first place.
    """
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) >= PROSE_LENGTH:
        raise ValueError(
            f"{label}.type must be one of {list(SETUP_TYPES)}, got {len(text)} characters of prose "
            f"({text[:60]!r}...). The full description belongs in the report body, not this field."
        )
    resolved = canonical(text)
    if resolved is None:
        raise ValueError(
            f"{label}.type must be one of {list(SETUP_TYPES)}, got {text!r}"
        )
    return resolved
