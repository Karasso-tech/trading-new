"""What the four decision words mean, and when each one is allowed
(CONSISTENCY_RULES.md rule 29, added 2026-08-03).

Why this module exists. `SCREENER_v3.md` has listed four possible decision
lines -- Buy Now / Buy Only If Confirmed / Watchlist / No Trade -- since the
beginning, and never once defined them. The choice was made fresh, by judgment,
on every single run. The 2026-08-02 rescan made the drift visible in one table:

    MSFT  D  No Trade   -- no target worth selling into
    RKLB  D  No Trade   -- no target worth selling into
    SPOT  D  No Trade   -- no target worth selling into
    ONDS  D  Watchlist  -- no target worth selling into   <- same facts, other word
    PLTR  D  Watchlist  -- HAS a target paying 2.11x

Four of those five are in the identical situation and two different words came
back. The user spotted it unprompted ("i have stocks with the same grade but
different call").

The dividing line the data actually shows is NOT the grade -- it is whether a
place to sell exists at all:

  No Trade            nowhere to sell. No target clears rule 3's gate, so there
                      is no trade to place, not merely one to wait for.
  Watchlist           a real target exists, but something is wrong TODAY: chart
                      still falling, earnings inside the holding window, price
                      too extended, grade D, or a hostile regime (rule 18).
  Buy Only If         a real target exists, grade C or better, nothing wrong.
  Confirmed           The only missing thing is the price. An order can be
                      written now and left to wait.
  Buy Now             all of the above AND the trigger already confirmed on a
                      real settled daily close. In practice this comes from
                      MONITOR_v2's 🟢, almost never from a fresh screener run.

Scope discipline, same as rubric_formula.py/regime_formula.py: this module
decides nothing about WHICH level is a target -- that stays Category B, in the
.md files. It only reads facts already computed (does a qualifying target
exist, what grade came back, what regime) and reports which decision words
those facts permit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import rubric_formula

BUY_NOW = "Buy Now"
BUY_IF_CONFIRMED = "Buy Only If Confirmed"
WATCHLIST = "Watchlist"
NO_TRADE = "No Trade"

ALL_DECISIONS = (BUY_NOW, BUY_IF_CONFIRMED, WATCHLIST, NO_TRADE)
BUY_DECISIONS = {BUY_NOW, BUY_IF_CONFIRMED}

# Kept in sync with report_lint.BLOCKING_GRADES / BLOCKING_REGIMES on purpose --
# imported from here by report_lint so there is one definition, not two that can
# drift (the exact failure this whole module exists to stop).
BLOCKING_GRADES = {"D", "F"}   # "F" retained for theses stored under the old 6-criterion scale
BLOCKING_REGIMES = {"risk_off", "structure_break"}

# Every letter rubric_formula.classify_rubric can return, plus "F" for theses
# stored under the old 6-criterion scale. Anything else -- including None -- is
# NOT a grade, and is treated exactly like a blocking one below.
KNOWN_GRADES = {"A", "B", "C", "D", "F"}

# The `stop_basis_kind` level_picker writes when no daily low could hold the
# stop and it fell back to a plain 2x ATR distance. Defined here, imported by
# level_picker, so the label a stop is TAGGED with and the label a decision is
# GATED on can never drift into two spellings.
NO_STRUCTURE_STOP_BASIS = "no_structure_distance_only"

# Rule 27's block on a D grade is a deliberately conservative policy, not a
# measured one: the 5-year portfolio backtest blocked every D plan at build
# time (289 of them on seed 42 alone), so this system has never observed what a
# D-graded trade actually does. Stated here, once, and quoted wherever the
# block is announced to the user -- a rule the data has not tested yet should
# not read like a rule the data confirmed.
GRADE_BLOCK_UNMEASURED_HE = (
    "החסימה על ציון D היא כלל זהירות שעדיין לא נמדד — הבקטסט חסם כל רעיון D "
    "לפני הכניסה, אז אין נתונים על מה שהם היו עושים"
)


def grade_of(decision: Optional[dict]) -> Optional[str]:
    """The rubric letter out of a decision JSON, under either field name.

    `build_plan.py` emits `rubric_grade`; `summary_inputs` and several
    hand-built decision files carry a plain `grade`; some real runs carried
    only one of the two, and one (`_runs/_decision_NVDA.json`) carried neither.
    Every reader went through its own `.get()` and they did not agree, so the
    gate below silently saw None on reports that plainly stated a letter. One
    function, used everywhere, is the fix -- same single-source-of-truth
    reasoning as this module's own docstring."""
    if not decision:
        return None
    return decision.get("rubric_grade") or decision.get("grade")


def has_qualifying_target(setup: Optional[dict]) -> bool:
    """True when this setup has at least one entry in `targets[]`.

    Deliberately a presence check, not a re-run of rule 3's gate: by the time a
    setup is saved, report_lint._lint_setup has ALREADY moved every level that
    fails the gate into `checkpoints[]`. Re-deriving the gate here would be a
    second opinion on arithmetic that has already been checked once, and two
    copies of a threshold is exactly how thresholds drift apart."""
    if not setup:
        return False
    return bool(setup.get("targets"))


def setup_stop_stands_on_structure(setup: Optional[dict]) -> bool:
    """False only when this setup's stop is the fallback distance, not a level.

    `level_picker.pick_stop` places the stop under a real daily low. When no low
    qualifies it still returns a stop -- a plain 2x ATR distance -- because a
    trade cannot be sized without one, and it tags that stop
    NO_STRUCTURE_STOP_BASIS and states plainly in its own reason that this is
    "not a level the chart gave us".

    Rule 1 is the oldest rule in this system: no invented price levels, every
    stop cites its source. A distance measured off the entry cites nothing. It
    is an honest answer to "where would I have to give up", and it is not an
    answer to "what would have to break". So it may be researched, and it may
    not carry a live order.

    A setup with no stop_basis_kind recorded at all is treated as structural --
    that field is newer than the oldest stored theses, and refusing to order
    every pre-2026-08 thesis on a missing field would be a different bug."""
    if not setup:
        return False
    return setup.get("stop_basis_kind") != NO_STRUCTURE_STOP_BASIS


def has_orderable_setup(primary: Optional[dict], alternate: Optional[dict]) -> bool:
    """True when at least ONE setup could carry a real order: it has somewhere
    to sell AND its stop stands on something. Both halves have to belong to the
    same setup -- a qualifying target on Primary does not license an order on an
    Alternate whose stop was invented."""
    return any(has_qualifying_target(s) and setup_stop_stands_on_structure(s)
               for s in (primary, alternate))


# Why a grade can fail to be usable at all, in the order the checks run. The
# wording lives here rather than in each caller so the Telegram message, the lint
# finding and the PDF notice cannot drift into three different explanations of
# the same refusal.
GRADE_BLOCK_REASONS_HE = {
    "names_conflict": ("הדוח מציין שני ציונים שונים בשני שדות שונים. אי אפשר לדעת "
                        "איזה מהם נכון, ולכן אין ציון"),
    "inputs_missing": ("אין בדוח את המספרים שמהם מחושב הציון, אז אי אפשר לבדוק "
                        "שהציון נכון"),
    "inputs_malformed": ("המספרים שמהם מחושב הציון פגומים, אז אי אפשר לבדוק "
                          "שהציון נכון"),
    "stated_missing": "אין ציון בדוח הזה בכלל",
    "mismatch": ("הציון שכתוב בדוח אינו הציון שיוצא מהמספרים של הדוח עצמו. "
                  "אי אפשר לדעת על איזה מהשניים נכתב שאר הדוח"),
    "blocking_grade": "הציון שיוצא מהמספרים הוא ציון שלעולם אינו פקודה",
}


@dataclass
class GradeVerdict:
    """The one authoritative answer to "what is this report's grade, and may it
    carry an order at all".

    `grade` is what the gate reads: the RECOMPUTED letter when the report's own
    numbers could be checked and agreed with, and None whenever they could not.
    None is not "unknown, carry on" -- max_allowed_decision treats any letter
    outside KNOWN_GRADES as blocking, so an unresolvable grade caps the decision
    at Watchlist exactly like a D does.

    `stated` and `recomputed` are both kept so a message can name both numbers
    instead of just asserting one, and so storage can record the arithmetic
    answer rather than the claim."""
    grade: Optional[str]
    blocked: bool
    reason_key: str
    stated: Optional[str]
    recomputed: Optional[str]
    score: Optional[int] = None

    @property
    def reason_he(self) -> str:
        return GRADE_BLOCK_REASONS_HE.get(self.reason_key, "")


def _recompute(inputs: dict):
    """classify_rubric on a decision's own disclosed rubric_inputs, or None when
    the block is absent or cannot be read as numbers."""
    try:
        return rubric_formula.classify_rubric(rubric_formula.RubricInputs(
            rr=float(inputs["rr"]),
            target_atr_multiple=float(inputs["target_atr_multiple"]),
            regime=str(inputs.get("regime") or ""),
            rs_delta_pct=float(inputs["rs_delta_pct"]),
            dist_sma20_atr=float(inputs["dist_sma20_atr"]),
            earnings_days_out=(None if inputs.get("earnings_days_out") is None
                                else int(inputs["earnings_days_out"])),
        ))
    except (KeyError, TypeError, ValueError):
        return None


def resolve_grade(decision: Optional[dict]) -> GradeVerdict:
    """Rule 27's recompute, as a gate rather than a remark (2026-08-30).

    The recompute existed for one day as a lint finding, which meant a report
    claiming A while its own five numbers said D shipped an order with a warning
    beside it -- the identical shape this project had just finished removing from
    the grade gate itself. Arithmetic on five thresholds has exactly one right
    answer, so there is nothing here for a human to weigh and nothing a warning
    adds.

    Four ways a report fails to produce a usable grade, all of them refusals
    rather than defaults:

      * the two field names carry different letters -- there is no fact to read,
        only a contradiction
      * rubric_inputs is missing, or cannot be read as numbers -- the claim
        cannot be checked, and an unchecked quality claim is not a checked one
      * the stated letter and the recomputed letter disagree -- the letter can be
        resolved (arithmetic wins) but the REPORT cannot, because its targets,
        its sizing and its prose were written against one of the two and nothing
        records which
      * the recomputed letter is itself a blocking grade

    A report with no rubric_inputs is the common legacy case and is treated as
    unresolvable on purpose. That is a real cost -- older stored theses lose the
    ability to carry an order until they are rebuilt -- and it is the intended
    one: the alternative is trusting a number nothing can verify."""
    if not decision:
        return GradeVerdict(None, True, "stated_missing", None, None)

    named = {k: decision.get(k) for k in ("rubric_grade", "grade")}
    present = [v for v in named.values() if v]
    if len(present) == 2 and present[0] != present[1]:
        return GradeVerdict(None, True, "names_conflict",
                            named["rubric_grade"], None)

    stated = grade_of(decision)
    inputs = decision.get("rubric_inputs")
    if not isinstance(inputs, dict):
        return GradeVerdict(None, True, "inputs_missing", stated, None)

    result = _recompute(inputs)
    if result is None:
        return GradeVerdict(None, True, "inputs_malformed", stated, None)

    if stated is None:
        return GradeVerdict(None, True, "stated_missing", None,
                            result.grade, result.score)
    if stated != result.grade:
        return GradeVerdict(None, True, "mismatch", stated,
                            result.grade, result.score)
    if result.grade in BLOCKING_GRADES:
        return GradeVerdict(result.grade, True, "blocking_grade", stated,
                            result.grade, result.score)
    return GradeVerdict(result.grade, False, "", stated, result.grade, result.score)


def max_allowed_decision(*, has_target: bool, grade: Optional[str],
                          regime: Optional[str],
                          has_structural_stop: bool = True) -> str:
    """The strongest decision these facts permit. The actual decision may always
    be weaker (a real reason to wait is judgment and stays with the model) --
    never stronger.

    Order matters and is not arbitrary: "nowhere to sell" outranks everything
    else, because a hostile regime or a D grade describes a trade you should not
    take yet, while no qualifying target describes a trade that does not exist.
    """
    if not has_target:
        return NO_TRADE
    if not has_structural_stop:
        # Watchlist, never No Trade: there IS somewhere to sell, so the idea is
        # real and worth keeping on the list. What is missing is a defensible
        # place to be wrong, and that can appear on any day the chart builds a
        # low. Defaults to True so a caller that does not know stays unchanged.
        return WATCHLIST
    if regime in BLOCKING_REGIMES:
        return WATCHLIST      # rule 18 -- the thesis is still kept and tracked
    if grade not in KNOWN_GRADES:
        # No grade at all, or an unreadable one. Treated exactly like a blocking
        # grade rather than waved through: an order whose quality was never
        # scored is not a safer order than one scored badly, it is an unscored
        # one. Found real -- `_runs/_decision_NVDA.json` was delivered carrying
        # no grade under either field name, and nothing anywhere stopped it.
        return WATCHLIST
    if grade in BLOCKING_GRADES:
        return WATCHLIST      # rule 27 -- never an order, but still worth watching
    # A buy is permitted. WHICH buy -- "Buy Now" versus "Buy Only If Confirmed"
    # -- turns on whether the trigger has already confirmed on a settled daily
    # close, and this module is not given that fact, so it must not rank the two
    # against each other. (First version returned BUY_IF_CONFIRMED here and
    # thereby flagged every legitimate screener "Buy Now" as too strong -- an
    # over-reach caught by the existing clean-decision test, exactly the kind of
    # false alarm that trains a reader to ignore real findings.)
    return BUY_NOW


_RANK = {NO_TRADE: 0, WATCHLIST: 1, BUY_IF_CONFIRMED: 2, BUY_NOW: 3}


def is_decision_allowed(decision: Optional[str], *, has_target: bool,
                         grade: Optional[str], regime: Optional[str],
                         has_structural_stop: bool = True) -> bool:
    """False only when the decision claims MORE than the facts permit. An
    unrecognized decision string is not judged here (report_lint reports it
    separately) -- this returns True so one bad label can't masquerade as a
    gate violation."""
    if decision not in _RANK:
        return True
    ceiling = max_allowed_decision(has_target=has_target, grade=grade, regime=regime,
                                    has_structural_stop=has_structural_stop)
    return _RANK[decision] <= _RANK[ceiling]


# --- Plain-words reasons ----------------------------------------------------
#
# `rejection_reasons` has always been free text: the same situation came back as
# "trigger_not_fired", "trigger_not_confirmed", "no_live_trigger",
# "trigger_pending" and "awaiting_trigger_confirmation" across five runs of the
# same week. Fine for a human reading one report; useless for showing the user a
# consistent one-line "why not yet", and useless for counting anything later.
#
# The map below is matched by SUBSTRING against each stored token, so the
# existing free-text tokens keep working -- this adds meaning to what is already
# written rather than demanding every writer change at once.
_REASON_PATTERNS = (
    # (substring to look for, plain Hebrew sentence)
    ("no_qualifying_target", "אין רמה למכור בה שמצדיקה את הסיכון"),
    ("no_target", "אין רמה למכור בה שמצדיקה את הסיכון"),
    ("rr_below", "הרווח האפשרי קטן מדי ביחס לסיכון"),
    ("target_below", "היעד קרוב מדי — בתוך התנודה היומית הרגילה"),
    ("earnings", "דוח כספי צפוי בזמן שעוד נחזיק את המניה"),
    ("event", "אירוע צפוי בזמן שעוד נחזיק את המניה"),
    ("downtrend", "המניה עדיין יורדת"),
    ("below_sma", "המניה עדיין מתחת לממוצעים שלה"),
    ("extended", "המחיר רץ יותר מדי מהר, רחוק מדי מהממוצע"),
    ("parabolic", "המחיר רץ יותר מדי מהר, רחוק מדי מהממוצע"),
    ("wall", "יש קיר התנגדות ממש מעל המחיר"),
    ("rs_", "המניה חלשה מהשוק"),
    ("regime", "מצב השוק הכללי לא תומך כרגע"),
    ("choppy", "מצב השוק הכללי לא תומך כרגע"),
    ("sector", "כבר יש לנו הרבה מניות מאותה משפחה"),
    ("trigger", "המחיר עדיין לא הגיע לרמת הכניסה"),
    ("rubric", "הציון הכולל של הרעיון נמוך"),
    ("grade_", "הציון הכולל של הרעיון נמוך"),
)

# Shown when a thesis has no stored reasons at all, or none that match above --
# never a blank line and never a guess at what the writer meant.
FALLBACK_REASON = "ממתין לתנאים שעדיין לא התקיימו"


# --- The decision sign ------------------------------------------------------
#
# Added 2026-08-03 on the user's request: /list, /pending, /monitor and
# /monitorall all showed the levels, the status tier and the grade, and never
# once said WHICH of the four decisions the thesis is actually sitting on. A
# ticker on the watchlist and a ticker with a written order waiting for its
# price looked identical in every one of those views.
#
# Deliberately read straight from the stored `thesis.decision` column and
# rendered mechanically -- this is display, not a second opinion. It never
# re-derives what the decision should be (max_allowed_decision above is the
# only thing that judges that, and report_lint is the only caller that does).
#
# The emoji are chosen NOT to collide with MONITOR_v2.md's status tiers
# (⚪/🟡/🟡➕/🟢/🔴): a sign meaning "watchlist" that looks like a sign meaning
# "still far from the trigger" is worse than no sign at all. Buy Now is the one
# place the two vocabularies want the same color -- green means "go" in both --
# so it is separated by SHAPE instead: 🟩 square for the decision, 🟢 circle for
# the status tier. Every status tier is a circle; no decision sign is.
_SIGNS = {
    BUY_NOW:          ("🟩", "קנייה עכשיו"),
    BUY_IF_CONFIRMED: ("🔷", "קנייה רק אם המחיר יאשר"),
    WATCHLIST:        ("👁", "רשימת מעקב — לא לקנות עדיין"),
    NO_TRADE:         ("🚫", "אין עסקה"),
}

# A thesis saved before the `decision` column existed, or one saved with a
# label nobody recognizes, gets its own visible sign -- never a blank and never
# a guess at which of the four was meant. Same posture as FALLBACK_REASON.
UNKNOWN_SIGN = ("❔", "אין החלטה שמורה")

# Lead-in on the full line. It says WHERE the words come from, which matters
# most in /monitor and /monitorall: there, a live 🟢 ("the trigger fired") can
# sit beside a stored 👁 ("watchlist"), and those two are not in conflict --
# one is what the price did today, the other is what the thesis decided at
# build time. Without the lead-in the pair reads as a contradiction.
SIGN_LEAD = "מה נקבע בתזה"

# The other half of the pair (2026-08-08). The lead-in above was not enough on
# its own: it labels the stored word correctly, but leaves the live fact to be
# inferred from a 🟢 somewhere further down the message. Naming both, side by
# side, is what turns the pair from a contradiction into two facts.
LIVE_LEAD = "מה קרה מאז"
TRIGGER_CONFIRMED_WORDS = "הטריגר כבר הופעל בסגירה יומית"


# Spellings that unambiguously mean one of the four. Kept short and explicit --
# every entry is a form that really arrived. `buy` is the one that matters:
# shadow_outcomes held both "Buy" (8 rows) and "Buy Now" (6 rows) for the same
# thing, so the strongest decision this system has was split across two labels
# and each half looked too small to read (found 2026-08-09).
_DECISION_ALIASES = {
    "buy": BUY_NOW,
    "buy only if confirmed": BUY_IF_CONFIRMED,
    "buy if confirmed": BUY_IF_CONFIRMED,
    "buy_only_if_confirmed": BUY_IF_CONFIRMED,
    "watch list": WATCHLIST,
    "watch": WATCHLIST,
    "no trade": NO_TRADE,
    "notrade": NO_TRADE,
    "no-trade": NO_TRADE,
}


def _canonical(decision: Optional[str]) -> Optional[str]:
    """Match the stored label case- and whitespace-insensitively. The column is
    documented as holding the exact SCREENER_v3.md wording, but it is written by
    a model on every screener run, and "buy now" / "Buy Now " must not fall
    through to the unknown sign over a capital letter."""
    if not decision:
        return None
    key = " ".join(str(decision).split()).lower()
    for known in ALL_DECISIONS:
        if known.lower() == key:
            return known
    return _DECISION_ALIASES.get(key)


def canonical_decision(decision: Optional[str]) -> Optional[str]:
    """Public form of _canonical, for the write path (2026-08-09).

    None in, None out -- a thesis genuinely may carry no decision (rows predate
    the column). An unrecognised label also comes back None rather than raising:
    the caller decides whether that is worth refusing a whole run over, and
    persistence.save_thesis deliberately does not, because losing a completed
    20-minute analysis over one word is worse than storing the word as given."""
    return _canonical(decision)


def decision_sign(decision: Optional[str]) -> str:
    """Just the emoji -- for one-line-per-ticker views like /list."""
    return _SIGNS.get(_canonical(decision), UNKNOWN_SIGN)[0]


def decision_words(decision: Optional[str]) -> str:
    """Just the plain Hebrew words, no emoji and no lead-in -- for places that
    quote a decision inside a sentence of their own (the /pending card's record
    of what an idea was called when its trigger confirmed) rather than showing
    it as a standalone sign line."""
    return _SIGNS.get(_canonical(decision), UNKNOWN_SIGN)[1]


def decision_line(decision: Optional[str], status: Optional[str] = None) -> str:
    """The full sign line: emoji + where it came from + plain Hebrew words.

    Plain text on purpose, no HTML tags: every caller sends this through
    Telegram's HTML parse mode alongside text it escapes itself, and a fixed
    string with no numbers in it has nothing to bold. Returning tag-free text
    means no caller has to remember whether this one is pre-escaped.

    2026-08-08, the user's own reading of a live PLTR message. The stored
    decision is written once, by /screener, on the night the idea is built, and
    nothing ever rewrites it -- so a thesis whose trigger has since confirmed
    still shows the word it was born with. PLTR sat at "Watchlist" (don't buy
    yet) above a real 115-share order, because it fired on 166.08 and closed at
    172.01 weeks after that word was chosen. By this module's own definitions
    that is Buy Now: "all of the above AND the trigger already confirmed on a
    real settled daily close", which it notes "in practice comes from
    MONITOR_v2's 🟢". Nothing writes it, so the word almost never appears.

    Rather than rewrite the stored decision (that is /screener's work, and
    overwriting it quietly is exactly what the user has ruled out), the live
    fact is shown NEXT TO it when the trigger has confirmed. Two labelled
    halves: what the plan decided, and what the price has since done. The old
    line already named its own source, but a reader has to notice that label to
    see two facts instead of one contradiction -- and the owner did not, which
    is the only test that counts.

    Only 🟢 earns the second half: it is the one tier that means the trigger
    confirmed on a settled daily close. A stored Buy Now needs no second half,
    there being nothing to reconcile.
    """
    emoji, words = _SIGNS.get(_canonical(decision), UNKNOWN_SIGN)
    line = f"{emoji} {SIGN_LEAD}: {words}"
    if status == "green" and _canonical(decision) != BUY_NOW:
        line += f" · 🟢 {LIVE_LEAD}: {TRIGGER_CONFIRMED_WORDS}"
    return line


def _as_token_list(value) -> list[str]:
    """Accept the field in every shape it really arrives in.

    `thesis.rejection_reasons` is stored as a JSON string, and only SOME readers
    parse it back (get_thesis does, get_pending_report_rows does not). Handed a
    raw string, a plain `for token in value` loop iterates CHARACTERS -- every
    pattern below then matches nothing and every idea silently shows the
    fallback reason. That is exactly what happened the first time this ran
    against real rows, and it fails quietly, which is the worst way to fail."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            import json
            try:
                parsed = json.loads(text)
            except ValueError:
                return [text]
            return [str(item) for item in parsed] if isinstance(parsed, list) else [text]
        return [text] if text else []
    try:
        return [str(item) for item in value]
    except TypeError:
        return [str(value)]


def explain_reasons(rejection_reasons: Optional[Iterable[str]],
                     limit: int = 2) -> list[str]:
    """Plain Hebrew sentences for the stored reason tokens, de-duplicated and
    capped at `limit`.

    The cap is the point, not a limitation: a thesis routinely stores five or
    six tokens, and a wall of them reads as noise. The FIRST matching patterns
    win, and the pattern order above is deliberate -- "nowhere to sell" and
    "reward too small" are what actually decide the outcome, while "the price
    hasn't got there yet" is true of every waiting idea and says nothing."""
    tokens = _as_token_list(rejection_reasons)
    if not tokens:
        return [FALLBACK_REASON]
    out = []
    for pattern, sentence in _REASON_PATTERNS:
        if sentence in out:
            continue
        if any(pattern in token.lower() for token in tokens):
            out.append(sentence)
            if len(out) >= limit:
                break
    return out or [FALLBACK_REASON]
