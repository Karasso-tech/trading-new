"""The /monitorall scan lines, built in Python instead of retyped by a model
(2026-08-10).

Same problem, same fix as summary_text.py. MONITOR_v2.md section ו.3 does not
describe a style -- it dictates literal templates: fixed emoji, fixed line
order, fixed wording, every number bold and rounded to two decimals, a fixed
translation table for the rubric criteria and for the "cannot be scored"
reasons. It then spends a page insisting on all of it, because it kept
drifting.

This is the message the owner reads more than any other: it goes out twice a
day, one line per pending ticker, and every one of those lines used to be
retyped from the document by hand. The 2026-08-07 scan already shows the drift
-- ONDS and BTCUSD came back under a header ("...אבל אי אפשר לחשב ציון") that
appears nowhere in section ו.3, invented on the spot because the document
describes that case in prose without giving a template for it. It is a
perfectly reasonable line. It is also a line nobody chose, that nobody can
find, and that the next run may or may not produce again.

Which template applies is picked mechanically here, in this order:

  1. no live grade, but a stated reason  -> "cannot be scored" (never the D
     template -- section ו.3 is explicit that those are different situations)
  2. blocked / grade D                   -> "the setup has weakened", no order
  3. the trigger fired too long ago      -> reported as fact, no order
  4. green                               -> the order block
  5. yellow_plus                         -> the Starter block

What is still the model's to write, and the only prose this module asks for:
one plain sentence saying what actually happened (`sentence`). A missing one
comes back clearly marked as missing, never as filler.

Every number arrives already computed -- this module does no arithmetic beyond
multiplying a share count by an entry price, and never parses a number out of a
sentence. A thesis whose trigger was saved as free text (26 of 55 real theses
once were) has no trigger level, and this says so in words rather than
crashing or guessing.
"""

from __future__ import annotations

from typing import Optional

# Grades that are never allowed to carry an order or a Starter quantity
# (rule 27), and the rounding rules the screener's own templates already use --
# imported rather than re-listed, so a number can never be rounded one way in
# one message and another way in the next.
from decision_policy import BLOCKING_GRADES
from summary_text import (DISCLOSURE_LINES, MISSING_SENTENCE, SEP, SETUP_HE,
                           _i, _n, _num)

# Two facts on one line, as section ו.3 writes them. Three spaces, not a tab
# and not a bullet -- Telegram collapses neither.
GAP = "   "

# Section ו.3's fixed status headers.
HEAD_GREEN = "🟢 <b>{ticker}</b> — הטריגר הופעל!"
HEAD_YELLOW_PLUS = "🟡➕ <b>{ticker}</b> — התקרב, עדיין לא סגור"
HEAD_WEAKENED = "📉 <b>{ticker}</b> — הטריגר קרוב/הופעל, אבל הסטאפ נחלש"
HEAD_UNGRADEABLE_GREEN = "📉 <b>{ticker}</b> — הטריגר הופעל, אבל אי אפשר לחשב ציון"
HEAD_UNGRADEABLE_NEAR = "📉 <b>{ticker}</b> — קרוב לטריגר, אבל אי אפשר לחשב ציון"

FILLED_LINE = "👉 אם ביצעת בפועל, שלח /filled"

# Only these two tiers get a line at all. White, yellow and red are counted in
# the scan header and otherwise stay silent, exactly as they do today.
GREEN = "green"
YELLOW_PLUS = "yellow_plus"
SCAN_STATUSES = (GREEN, YELLOW_PLUS)

# Section ו.3's translation of the six rubric criteria. Only the failing ones
# are ever listed, and only in this order, so two tickers failing the same
# things read the same way.
CRITERIA_HE = {
    "rr": "יחס סיכון/סיכוי נמוך מ-2.3",
    "target_atr": "היעד קרוב מדי (פחות מ-1.5x ATR)",
    "regime": "מצב השוק לא תומך",
    "rs": "המניה חלשה יחסית ל-SPY",
    "sma20_extension": "המניה רחוקה מדי מהממוצע הנע 20",
    "event": "דוחות קרובים מדי",
}
CRITERIA_ORDER = ("rr", "target_atr", "regime", "rs", "sma20_extension", "event")

# Section ו.3's translation of fetch_monitor_data._score_setup's stated
# reasons. A grade that could not be computed is NOT a failing grade, and the
# difference matters to the reader: one says the idea got worse, the other says
# the saved thesis is missing a number.
UNGRADEABLE_HE = {
    "no_target_to_score": "אין יעד מספרי בתזה",
    "no_numeric_target": "אין יעד מספרי בתזה",
    "no_numeric_entry_or_stop": "אין טריגר/סטופ מספרי בתזה",
    "zero_risk_or_atr": "סטופ זהה לטריגר או ATR אפס",
    "daily_bar_not_settled": "הנר היומי עדיין לא נסגר",
}
UNGRADEABLE_UNKNOWN = "הסיבה לא נמסרה"

# Said out loud rather than left as a dash. A thesis can genuinely hold a
# sentence where its trigger should be, and "—" reads as a bug.
NO_TRIGGER = "<b>—</b> (אין מחיר טריגר שמור בתזה)"
NO_ORDER = "⚠️ אין פקודה מוכנה — חסרים מחיר כניסה/סטופ/כמות"
NO_CRITERIA = "(לא נמסרה רשימת התנאים שנכשלו)"


def _price_line(price, trigger) -> str:
    """The "where price is against where it needs to be" line, used by every
    template that has no order block to show."""
    trigger_text = _n(trigger) if _num(trigger) is not None else NO_TRIGGER
    return f"מחיר עכשיו: {_n(price)}{GAP}טריגר: {trigger_text}"


def _grade_line(grade_now: Optional[str]) -> Optional[str]:
    """Section ו.3, 2026-08-08: the live grade is shown on EVERY green and
    yellow_plus line, not only on the blocked ones. Without it a C that carried
    an order and an A that carried the same order look identical."""
    if not grade_now:
        return None
    return f"✅ ציון רובריקה חי: <b>{grade_now}</b>"


def _sentence_line(sentence: Optional[str]) -> str:
    return f"💬 {sentence or MISSING_SENTENCE}"


def _failed_criteria(criteria: Optional[dict]) -> str:
    """Only the criteria whose value is exactly false. A criterion that is
    missing or None was not measured, and reporting it as failed would put a
    reason on the screen that nothing actually found."""
    if not isinstance(criteria, dict):
        return NO_CRITERIA
    failed = [CRITERIA_HE[key] for key in CRITERIA_ORDER
              if criteria.get(key) is False]
    return ", ".join(failed) if failed else NO_CRITERIA


def _order_lines(entry, stop, qty) -> list[str]:
    """The green order block. All three numbers or none of them -- half an
    order is worse than a stated gap, because the missing half is the one that
    decides how much money is at risk."""
    entry_num, stop_num, qty_num = _num(entry), _num(stop), _num(qty)
    if entry_num is None or stop_num is None or qty_num is None:
        return [NO_ORDER]
    cost = qty_num * entry_num
    return [
        f"🟢 כניסה: {_n(entry_num)}{GAP}🛑 סטופ: {_n(stop_num)}",
        f"🛒 כמות לפוזיציה מלאה: <b>{qty_num:,.0f} מניות</b> (${cost:,.2f})",
    ]


def build_scan_headline(*, ticker: str, status: Optional[str],
                         sentence: Optional[str] = None,
                         price=None, trigger=None,
                         grade_now: Optional[str] = None,
                         grade_at_build: Optional[str] = None,
                         ungradeable_reason: Optional[str] = None,
                         criteria: Optional[dict] = None,
                         rubric_blocked: bool = False,
                         entry=None, stop=None, qty=None,
                         starter_qty=None,
                         stale_trading_days=None) -> Optional[str]:
    """One ticker's block for the automatic scan, or None when this tier does
    not get one.

    The caller passes facts only. Every choice this function makes -- which
    template, whether an order may be shown, which criteria to list -- follows
    from those facts, in the fixed order documented at the top of this module.
    """
    if status not in SCAN_STATUSES:
        return None

    lines: list[str] = []

    # 1. No live grade. Deliberately checked before the D branch: "could not be
    # scored" is not a bad score, and section ו.3 is explicit that the two are
    # different situations with different words.
    #
    # No grade at all -- reason given or not -- also means no order, because
    # rule 27's gate cannot be checked against a grade nobody has. A payload
    # that simply forgot to copy the figures lands here and says the reason was
    # not supplied, which is true, rather than printing a buy order nothing
    # verified.
    if not grade_now:
        lines.append((HEAD_UNGRADEABLE_GREEN if status == GREEN
                      else HEAD_UNGRADEABLE_NEAR).format(ticker=ticker))
        lines.append(_price_line(price, trigger))
        reason = UNGRADEABLE_HE.get(str(ungradeable_reason), UNGRADEABLE_UNKNOWN)
        lines.append(f"⚠️ ציון עכשיו: לא ניתן לחשב — {reason}")
        lines.append(_sentence_line(sentence))
        return "\n".join(lines)

    # 2. The setup weakened since it was built (rule 27). No order, no Starter,
    # and always the list of what actually failed -- "the setup weakened" on
    # its own is not an explanation.
    if rubric_blocked or (grade_now in BLOCKING_GRADES):
        lines.append(HEAD_WEAKENED.format(ticker=ticker))
        lines.append(_price_line(price, trigger))
        lines.append(
            f"⚠️ ציון בבנייה: <b>{grade_at_build or '—'}</b> · "
            f"ציון עכשיו: <b>{grade_now or '—'}</b> — אין המלצת קנייה/כניסה חלקית כרגע."
        )
        lines.append(f"🔎 נכשל ב: {_failed_criteria(criteria)}")
        lines.append(_sentence_line(sentence))
        return "\n".join(lines)

    # 3. A trigger that fired days ago is still a real fact, but the order that
    # was sized against it is not: price has moved away from the level the stop
    # was measured from, so what looks like the planned trade is a worse one.
    if _num(stale_trading_days) is not None:
        lines.append((HEAD_GREEN if status == GREEN
                      else HEAD_YELLOW_PLUS).format(ticker=ticker))
        lines.append(_price_line(price, trigger))
        lines.append(
            f"⏳ הטריגר הופעל לפני <b>{_num(stale_trading_days):,.0f}</b> ימי מסחר — "
            "מדווח כעובדה בלבד, בלי פקודה חדשה."
        )
        grade_line = _grade_line(grade_now)
        if grade_line:
            lines.append(grade_line)
        lines.append(_sentence_line(sentence))
        if status == GREEN:
            lines.append(FILLED_LINE)
        return "\n".join(lines)

    # 4. Green -- the trigger confirmed on a settled daily close.
    if status == GREEN:
        lines.append(HEAD_GREEN.format(ticker=ticker))
        lines.extend(_order_lines(entry, stop, qty))
        grade_line = _grade_line(grade_now)
        if grade_line:
            lines.append(grade_line)
        lines.append(_sentence_line(sentence))
        lines.append(FILLED_LINE)
        return "\n".join(lines)

    # 5. yellow_plus -- close, not confirmed. The Starter line is dropped
    # whole when the stored thesis has no planned quantity (an older thesis,
    # saved before that field existed); a quantity is never invented here.
    lines.append(HEAD_YELLOW_PLUS.format(ticker=ticker))
    lines.append(_price_line(price, trigger))
    starter = _num(starter_qty)
    if starter is not None and starter >= 1:
        lines.append(
            f"🛒 אפשרות כניסה חלקית (Starter): עד <b>{starter:,.0f} מניות</b> "
            "(30% מהכמות המתוכננת)"
        )
    grade_line = _grade_line(grade_now)
    if grade_line:
        lines.append(grade_line)
    lines.append(_sentence_line(sentence))
    return "\n".join(lines)


# --- The single-ticker check (/monitor, sections ו.1 and ו.2) ---------------
#
# Same form, more room. The scan gets one block per ticker because sixteen of
# them share a message; a single check has the whole message to itself, so
# section ו.1 gives the fired trigger a full order card with separators, and
# ו.2 gives the waiting tiers three short lines.

# Section ו.2's fixed status words. One per tier, never reworded per ticker.
STATUS_HE = {
    "white": ("⚪", "עדיין רחוק"),
    "yellow": ("🟡", "נבדק, עדיין לא סגור"),
    "yellow_plus": ("🟡➕", "התקרב, ממתין לאישור סופי"),
    "red": ("🔴", "ירד מהמעקב"),
}

CHECK_HEAD_GREEN = "🟢 <b>{ticker}</b> — הטריגר הופעל!"
ORDER_HEAD = "📋 <b>פרטי ההזמנה</b>"
BEFORE_YOU_ACT = "⚠️ <b>לפני שמבצעים, כדאי לדעת:</b>"
CHECK_FILLED_LINE = "👉 <b>אם ביצעת את הקנייה בפועל, שלח /filled</b>"

# Section ו.1's closing note, shown on EVERY fired trigger and not only on the
# blocked ones: it is the reminder that this system never places an order
# itself, and a reminder that only appears when something is wrong is a
# reminder nobody has read by the time it matters.
INFO_ONLY = "🔸 זו אינפורמציה בלבד — לא בוצעה שום קנייה באופן אוטומטי."

REGIME_BLOCKED_LINE = ("🔸 מצב השוק השתנה מאז שהתזה נבנתה (אז: {at_build}, עכשיו: {now}) — "
                        "לכן אין המלצת קנייה כרגע, למרות שהטריגר הופעל בפועל.")
RUBRIC_BLOCKED_LINE = ("🔸 הסטאפ נחלש מאז שנבנה (אז: ציון <b>{at_build}</b>, עכשיו: ציון "
                        "<b>{now}</b>) — לכן אין המלצת קנייה כרגע, למרות שהטריגר הופעל בפועל.")
FAILED_ON_LINE = "🔸 נכשל ב: {criteria}"
DEVIATION_LINE = ("🔸 המחיר בפועל קצת שונה ממה שתוכנן מראש (מתוכנן {planned}, בפועל {actual}) — "
                   "כדאי לוודא שהמספרים עדיין הגיוניים לפני שממשיכים.")
STALE_LINE = ("🔸 הטריגר הופעל לפני <b>{days}</b> ימי מסחר — הוא עדיין עובדה, אבל אין פקודה "
               "חדשה על מחיר ישן.")
UNGRADEABLE_LINE = "🔸 אי אפשר לחשב ציון כרגע — {reason}. אין פקודת קנייה."

# Section ו.2's Starter block for a 🟡➕ with a live grade of C or better.
STARTER_OFFER = ("🛒 אפשרות כניסה חלקית (Starter): עד <b>{qty} מניות</b> (30% מהכמות המתוכננת), "
                  "באותו סטופ כמו התזה המקורית")
STARTER_OR_WAIT = "🤔 או: לחכות לאישור מלא (סגירה יומית) לפני שנכנסים בכלל."
STARTER_WEAKENED = ("📉 הסטאפ נחלש מאז שנבנה (אז: ציון <b>{at_build}</b>, עכשיו: ציון "
                     "<b>{now}</b>) — לא מומלצת כניסה חלקית כרגע.")
HELD_POSITION_NOTE = "לתשומת לב: הייתה פוזיציה פתוחה בטיקר הזה."

# The three portfolio disclosures (rules 19/20/21). Word for word the same
# sentences the screener's own message uses -- one warning that reads two
# different ways in two messages is two warnings to learn.
DISCLOSURE_ORDER_CHECK = ("heat", "sector", "cash")


def _target_lines(targets: Optional[list]) -> list[str]:
    """Section ו.1's target, ratio and remainder lines.

    The levels come from the stored thesis, not from this check: MONITOR never
    re-derives a target, it only confirms that the trigger the screener wrote
    has fired. With no stored target there is nothing to sell into, and that is
    said plainly instead of printing an empty line."""
    targets = [t for t in (targets or []) if isinstance(t, dict)]
    if not targets:
        return ["🏆 יעד: אין יעד שמור בתזה"]

    first = targets[0]
    line = f"🏆 יעד: {_n(first.get('price'))} — מוכרים {_i(first.get('pct'))}% מהפוזיציה שם"
    if len(targets) > 1:
        line += f"  ·  יעד נוסף: {_n(targets[1].get('price'))}, מוכרים {_i(targets[1].get('pct'))}%"
    lines = [line]

    rr = _num(first.get("rr"))
    if rr is not None:
        lines.append(f"⚖️ יחס סיכון/סיכוי: {_n(rr)}")

    runner = 100.0 - sum((_num(t.get("pct")) or 0.0) for t in targets)
    lines.append(f"🔄 יתרה ({_i(runner)}%): לא נמכרת במחיר קבוע — נשארת עם סטופ שעולה בהדרגה")
    return lines


def _order_card(*, setup_type, entry, stop, qty, targets, grade_now) -> list[str]:
    """The whole 📋 block. Present or absent as one piece -- section ו.1 drops
    all of it when there is no order to offer, rather than showing an entry
    with no size or a size with no stop."""
    head = f"{ORDER_HEAD} — {SETUP_HE.get(setup_type, setup_type or '')}".rstrip(" —")
    lines = [head, f"🟢 כניסה: {_n(entry)}", f"🛑 סטופ: {_n(stop)}"]
    lines.extend(_target_lines(targets))
    qty_num, entry_num = _num(qty), _num(entry)
    if qty_num is not None and entry_num is not None:
        lines.append(f"🛒 כמות מחושבת מחדש: <b>{qty_num:,.0f} מניות</b> "
                      f"(${qty_num * entry_num:,.2f})")
    else:
        lines.append("🛒 כמות מחושבת מחדש: <b>—</b>")
    if grade_now:
        lines.append(f"✅ ציון רובריקה חי: <b>{grade_now}</b>")
    return lines


def _deviation_line(deviation: Optional[dict]) -> Optional[str]:
    """The planned-versus-actual note, when the live levels have drifted past
    tolerance from the stored thesis. `deviation` is measured by the caller
    against the ATR frozen at build time -- this only says it in words."""
    if not deviation:
        return None
    planned, actual = _num(deviation.get("planned")), _num(deviation.get("actual"))
    if planned is None or actual is None:
        return None
    return DEVIATION_LINE.format(planned=_n(planned), actual=_n(actual))


def build_check_summary(*, ticker: str, status: Optional[str],
                         sentence: Optional[str] = None,
                         price=None, trigger=None,
                         setup_type: Optional[str] = None,
                         entry=None, stop=None, qty=None,
                         targets: Optional[list] = None,
                         grade_now: Optional[str] = None,
                         grade_at_build: Optional[str] = None,
                         rubric_blocked: bool = False,
                         ungradeable_reason: Optional[str] = None,
                         criteria: Optional[dict] = None,
                         regime_blocked: bool = False,
                         regime_at_build_he: Optional[str] = None,
                         regime_now_he: Optional[str] = None,
                         deviation: Optional[dict] = None,
                         disclosure_flags: Optional[list] = None,
                         starter_qty=None,
                         has_open_position: bool = False,
                         stale_trading_days=None) -> str:
    """One /monitor check's Telegram text -- section ו.1 for a fired trigger,
    section ו.2 for every other tier.

    An order is offered only when nothing stands against it: a live grade that
    is not D, a regime that is not blocking, a trigger that fired recently
    enough for its stop to still mean something, and real numbers to print. Any
    one of those failing drops the whole order card and says which one it was.
    The trigger itself is still reported as the fact it is."""
    if status != GREEN:
        return _build_watch_summary(
            ticker=ticker, status=status, sentence=sentence, price=price,
            trigger=trigger, grade_now=grade_now, grade_at_build=grade_at_build,
            rubric_blocked=rubric_blocked, ungradeable_reason=ungradeable_reason,
            starter_qty=starter_qty, has_open_position=has_open_position,
            stale_trading_days=stale_trading_days)

    stale = _num(stale_trading_days)
    grade_blocks = rubric_blocked or (grade_now in BLOCKING_GRADES)
    no_grade = not grade_now
    show_order = not (grade_blocks or no_grade or regime_blocked or stale is not None)

    blocks: list[list[str]] = [[CHECK_HEAD_GREEN.format(ticker=ticker)],
                               [f"💡 {sentence or MISSING_SENTENCE}"]]

    if show_order:
        blocks.append(_order_card(setup_type=setup_type, entry=entry, stop=stop,
                                   qty=qty, targets=targets, grade_now=grade_now))
    else:
        # No order card, so the two facts it would have carried -- where price
        # is, and the level it crossed -- are said on their own line. Without
        # this a blocked check shows a headline and nothing measurable.
        blocks.append([_price_line(price, trigger)]
                      + ([f"✅ ציון רובריקה חי: <b>{grade_now}</b>"] if grade_now else []))

    notes: list[str] = []
    if regime_blocked:
        notes.append(REGIME_BLOCKED_LINE.format(at_build=regime_at_build_he or "—",
                                                 now=regime_now_he or "—"))
    if grade_blocks:
        notes.append(RUBRIC_BLOCKED_LINE.format(at_build=grade_at_build or "—",
                                                 now=grade_now or "—"))
        notes.append(FAILED_ON_LINE.format(criteria=_failed_criteria(criteria)))
    elif no_grade:
        notes.append(UNGRADEABLE_LINE.format(
            reason=UNGRADEABLE_HE.get(str(ungradeable_reason), UNGRADEABLE_UNKNOWN)))
    if stale is not None:
        notes.append(STALE_LINE.format(days=f"{stale:,.0f}"))
    deviation_line = _deviation_line(deviation)
    if deviation_line:
        notes.append(deviation_line)
    active = set(disclosure_flags or [])
    notes.extend(f"🔸 {DISCLOSURE_LINES[key]}" for key in DISCLOSURE_ORDER_CHECK
                 if key in active)
    notes.append(INFO_ONLY)
    blocks.append([BEFORE_YOU_ACT] + notes)

    blocks.append([CHECK_FILLED_LINE])
    return f"\n\n{SEP}\n\n".join("\n".join(b) for b in blocks)


def _build_watch_summary(*, ticker, status, sentence, price, trigger, grade_now,
                          grade_at_build, rubric_blocked, ungradeable_reason,
                          starter_qty, has_open_position, stale_trading_days) -> str:
    """Section ו.2 -- the light template for a tier that is not an order.

    Deliberately three lines plus what the situation genuinely adds. This is
    the message that goes out most often, and a watching update that fills half
    a screen stops being read."""
    emoji, words = STATUS_HE.get(str(status), ("📡", str(status or "—")))
    lines = [f"{emoji} <b>{ticker}</b> — {words}", _price_line(price, trigger),
             f"💬 {sentence or MISSING_SENTENCE}"]

    if status == YELLOW_PLUS:
        grade_blocks = rubric_blocked or (grade_now in BLOCKING_GRADES)
        stale = _num(stale_trading_days)
        starter = _num(starter_qty)
        if grade_blocks:
            # Rule 27: no partial entry into a setup that has weakened. The
            # Starter lines are replaced, never simply dropped -- the reader
            # has to be told why the option they saw yesterday is gone.
            lines.append(STARTER_WEAKENED.format(at_build=grade_at_build or "—",
                                                  now=grade_now or "—"))
        elif not grade_now:
            lines.append(UNGRADEABLE_LINE.format(
                reason=UNGRADEABLE_HE.get(str(ungradeable_reason), UNGRADEABLE_UNKNOWN)))
        elif stale is not None:
            lines.append(STALE_LINE.format(days=f"{stale:,.0f}"))
        elif starter is not None and starter >= 1:
            lines.append(STARTER_OFFER.format(qty=f"{starter:,.0f}"))
            lines.append(f"✅ ציון רובריקה חי: <b>{grade_now}</b>")
            lines.append(STARTER_OR_WAIT)
        else:
            lines.append(f"✅ ציון רובריקה חי: <b>{grade_now}</b>")

    if status == "red" and has_open_position:
        lines.append(HELD_POSITION_NOTE)
    return "\n".join(lines)


def scan_line_is_orderless(*, status: Optional[str],
                            grade_now: Optional[str] = None,
                            rubric_blocked: bool = False,
                            stale_trading_days=None) -> bool:
    """True when this ticker's scan line would carry no order and no Starter
    (2026-08-11, the user's call: "if the trigger is on but the fill is not
    relevant any more, don't show me those").

    Exactly branches 1-3 of build_scan_headline above, asked as a question
    instead of rendered: no live grade at all, a blocking grade, or a trigger
    that fired too long ago for its stop to still size anything. All three end
    the same way for the reader -- price crossed the level, and there is still
    nothing to place -- so the twice-daily scan names them in one short line
    at the bottom instead of spending a five-line block on each.

    Deliberately NOT used by the single-ticker /monitor: asking about one
    ticker by name is asking to see the whole answer, including why it is
    blocked. This only filters the unattended batch."""
    if status not in SCAN_STATUSES:
        return False
    return (not grade_now
            or rubric_blocked
            or grade_now in BLOCKING_GRADES
            or _num(stale_trading_days) is not None)


def starter_qty_from_planned(planned_qty) -> Optional[int]:
    """Section ו.2/ו.3's Starter size: 30% of the thesis's own stored planned
    quantity, rounded down. Never a fresh number, and None (line dropped)
    rather than 0 when there is nothing stored to take 30% of."""
    planned = _num(planned_qty)
    if planned is None or planned <= 0:
        return None
    starter = int(planned * 0.3)
    return starter if starter >= 1 else None
