"""The Telegram summary, built in Python instead of retyped by a model
(2026-08-09).

SCREENER_v3.md section ח does not describe a style. It dictates three literal
templates -- fixed emoji, fixed line order, fixed separators (exactly fifteen
bars, the same character), fixed wording, every number in bold and rounded to
two decimals, and a fixed translation table for the six setup names. It then
spends most of a page insisting on all of that, twice, because it kept drifting.

A fixed form with numbers dropped into it is an f-string. Asking a model to
reproduce it word for word on every run is asking it to do a job code does
perfectly, and the failure mode is silent: nobody notices a missing separator
or a slightly reworded line, they just notice that two reports look different
and stop trusting the format.

This module is the template. Which of the three applies is picked mechanically
from the decision field, exactly as section ח already says it must be ("the
choice between them is mechanical, by the `decision` field, not judgment").

What is still the model's to write, and the only thing it is asked for here:
the one-sentence thesis (`thesis_sentence`), and for a No Trade, the one or two
plain sentences explaining why. Those are genuine judgment and this module never
invents them -- a missing sentence comes back as a clearly-marked gap, not as
filler.
"""

from __future__ import annotations

from typing import Optional

SEP = "━" * 15

# Section ח's fixed translation table. Never reworded per ticker.
SETUP_HE = {
    "Breakout": "פריצה מעל התנגדות",
    "Retest": "בדיקה חוזרת של רמת הפריצה",
    "Pullback": "ירידה זמנית בתוך מגמה עולה",
    "Reclaim": "חזרה מעל רמה חשובה",
    "Failed Breakdown": "שבירה כלפי מטה שנכשלה",
    "Gap-and-Hold": "פער מחיר שהחזיק מעמד",
}

BUY_NOW = "Buy Now"
BUY_IF_CONFIRMED = "Buy Only If Confirmed"
WATCHLIST = "Watchlist"
NO_TRADE = "No Trade"

MISSING_SENTENCE = "‏(חסר משפט הסבר)"

# Section ח's disclosure lines, in the fixed order it lists them. Each is
# phrased as what it MEANS for the reader, not as the name of the phenomenon --
# that requirement is in the template's own rules and is easy to lose when the
# line is rewritten by hand each time.
DISCLOSURE_LINES = {
    "regime": ("מצב השוק הכללי כרגע לא תומך בבירור בטרייד הזה — שווה להיות בררן יותר. "
                "זו המלצה, לא כלל, והיא לא משנה את גודל הקנייה."),
    # 2026-08-09: this line used to end "...אז גם בגלל זה קונים כמות קטנה יותר",
    # which described the x0.5 volume derate. That derate was removed on
    # 2026-08-03 (rule 22) -- the backtest measured below-average-volume breaks
    # at +0.13R against +0.08R for above-average, so it pointed the wrong way.
    # Weak volume is still worth saying; it just no longer changes the size, and
    # a line claiming it does is a line that quietly re-introduces a rule the
    # owner deleted.
    "volume": ("יום הפריצה נסחר בשקט יחסית — סימן שהתנועה עוד לא אושרה במלואה. "
                "זו הערה בלבד; היא לא משנה את גודל הקנייה."),
    "event": "אין לנו תאריך ידוע לדוח כספים קרוב של החברה הזו.",
    "heat": "אחרי הקנייה הזו, סך הכסף שבסיכון בכל התיק יעבור את הרף שקבעתי לעצמי.",
    "sector": "המניה הזו שייכת לקבוצת מניות שכבר תופסת נתח גדול מדי מהתיק.",
    "cash": "הקנייה הזו תשתמש בחלק גדול מהמזומן הפנוי שיש כרגע.",
    # Watchlist-only additions (section ח.2)
    "trigger": "המחיר עדיין לא הגיע לרמה שצריך כדי להיכנס.",
    "rs": "המניה הזו זזה פחות טוב מהשוק הכללי לאחרונה.",
    "extended": "המחיר התרחק יותר מדי מהר מהמגמה הרגילה שלו.",
}

# The order they appear in, per section ח. Fixed, so two reports with the same
# facts always list them the same way.
DISCLOSURE_ORDER = ("regime", "volume", "event", "heat", "sector", "cash",
                    "trigger", "rs", "extended")


def _num(value) -> Optional[float]:
    """A real number out of whatever arrived.

    Prices in this system genuinely do arrive as strings -- deliver_report.py's
    own documented shape has `"trigger":"..."`, and plenty of stored setups
    carry "132.40" rather than 132.40. Formatting one with :,.2f raises
    "Unknown format code 'f' for object of type 'str'" and kills the whole
    delivery, which is a very expensive way to lose a completed analysis.

    A value that is genuinely not a number (rule 14's "no order ready yet"
    free text) comes back None and prints as a dash -- never parsed out of a
    sentence, same posture as persistence._setup_number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _n(value) -> str:
    """A number, bold, at most two decimals. Section ח allows no exceptions --
    7.1351 shows as 7.14. This affects the displayed text only; the stored JSON
    keeps full precision."""
    number = _num(value)
    if number is None:
        return "<b>—</b>"
    return f"<b>{number:,.2f}</b>"


def _i(value) -> str:
    """Same, for a whole number (share counts, percentages)."""
    number = _num(value)
    if number is None:
        return "<b>—</b>"
    return f"<b>{number:,.0f}</b>"


def _setup_block(label_he: str, emoji: str, setup: Optional[dict],
                  *, watchlist: bool, suffix: str = "") -> list[str]:
    """One setup's fixed five lines, identical in both templates.

    Section ח is explicit that the Primary and Alternate blocks use the SAME
    field emoji in the SAME order (never A/B markers), and that a setup with no
    price yet says so on its own line rather than being dropped -- rule 14's
    "a required section is never silently omitted" applied to the message.
    """
    head = f"{emoji} <b>{label_he}</b>"
    if not setup or _num(setup.get("trigger")) is None:
        return [head + f" — {SETUP_HE.get((setup or {}).get('type'), '')}".rstrip(" —"),
                "🔸 עדיין אין מחיר מוגדר לתרחיש הזה — ממתין לתנועת שוק שטרם קרתה"]

    lines = [f"{head} — {SETUP_HE.get(setup.get('type'), setup.get('type') or '')}{suffix}"]
    lines.append(f"🟢 {'טריגר לצפייה' if watchlist else 'כניסה'}: {_n(setup.get('trigger'))}")
    stop_label = "סטופ (אם וכאשר ניכנס)" if watchlist else "סטופ (המחיר שבו יוצאים אם הטרייד לא עובד)"
    lines.append(f"🛑 {stop_label}: {_n(setup.get('stop'))}")

    targets = setup.get("targets") or []
    if not targets:
        lines.append("🏆 יעד: עדיין אין יעד כשיר בטווח הנתונים")
        return lines

    first = targets[0]
    target_line = f"🏆 יעד: {_n(first.get('price'))}"
    if not watchlist:
        target_line += f" — מוכרים {_i(first.get('pct'))}% מהפוזיציה שם"
    if len(targets) > 1:
        second = targets[1]
        target_line += f"  ·  יעד נוסף: {_n(second.get('price'))}"
        if not watchlist:
            target_line += f", מוכרים {_i(second.get('pct'))}%"
    lines.append(target_line)

    rr = _num(first.get("rr"))
    if rr is not None:
        rr_line = f"⚖️ יחס סיכון/סיכוי: {_n(rr)}"
        if not watchlist:
            rr_line += f" (על כל $1 בסיכון, פוטנציאל רווח של ${rr:,.2f}"
            rr2 = _num(targets[1].get("rr")) if len(targets) > 1 else None
            if rr2 is not None:
                rr_line += f", ויעד נוסף: {rr2:,.2f}"
            rr_line += ")"
        lines.append(rr_line)

    if not watchlist:
        runner = 100.0 - sum((_num(t.get("pct")) or 0.0) for t in targets)
        lines.append(
            f"🔄 יתרה ({_i(runner)}%): לא נמכרת במחיר קבוע — נשארת בפוזיציה עם סטופ שעולה בהדרגה אחריה"
        )
    return lines


def _disclosures(flags: Optional[list]) -> list[str]:
    """The 🔸 lines, in section ח's fixed relative order, skipping what does not
    apply and never inventing new content."""
    active = set(flags or [])
    return [f"🔸 {DISCLOSURE_LINES[key]}" for key in DISCLOSURE_ORDER
            if key in active and key in DISCLOSURE_LINES]


def build_buy(*, ticker: str, decision: str, grade: Optional[str],
               thesis_sentence: Optional[str], primary: dict,
               alternate: Optional[dict], potential: Optional[float],
               disclosure_flags: Optional[list] = None,
               qty: Optional[int] = None, cost_usd: Optional[float] = None,
               heat_after_pct: Optional[float] = None,
               heat_cap_pct: Optional[float] = None,
               cash_available_usd: Optional[float] = None,
               trigger_fired: bool = False) -> str:
    """Section ח.1 -- Buy Now / Buy Only If Confirmed."""
    title = "קנייה עכשיו" if decision == BUY_NOW else "קנייה רק לאחר אישור"
    suffix = "" if trigger_fired else ", עדיין לא קרה בפועל"

    blocks: list[list[str]] = []
    blocks.append([f"🔍 <b>{ticker} — {title}</b>", f"⭐ ציון: <b>{grade or '—'}</b>"])
    blocks.append([f"💡 {thesis_sentence or MISSING_SENTENCE}"])
    blocks.append(
        _setup_block("סטאפ ראשי", "🎯", primary, watchlist=False, suffix=suffix)
        + [""]
        + _setup_block("סטאפ חלופי", "🔁", alternate, watchlist=False,
                        suffix=" (תרחיש שני, לא פעיל כרגע)")
    )
    blocks.append([
        f"📐 עד כמה התנועה יכולה להגיע בגדול (הערכה בלבד, לא מחיר יעד למכירה): {_n(potential)}"
    ])
    # Always present, even when nothing applies (rule 14: a required section is
    # never silently omitted -- a reader must be able to tell "nothing to flag"
    # apart from "the check was skipped").
    blocks.append(["⚠️ <b>לפני שקונים, כדאי לדעת:</b>"]
                   + (_disclosures(disclosure_flags) or ["🔸 אין הערות מיוחדות לטרייד הזה."]))
    cost = _num(cost_usd)
    cash = _num(cash_available_usd)
    blocks.append([
        "💰 <b>גודל הקנייה</b>",
        f"🛒 כמות: {_i(qty)} מניות",
        f"💵 עלות כוללת: <b>${cost:,.2f}</b>" if cost is not None
        else "💵 עלות כוללת: <b>—</b>",
        f"📊 אחוז מהתיק שיהיה בסיכון אחרי הקנייה: {_n(heat_after_pct)}% "
        f"(מהמקסימום שקבעתי לעצמי, {_n(heat_cap_pct)}%)",
        f"🏦 מזומן בשימוש: ${cost:,.2f} מתוך ${cash:,.2f} זמינים"
        if (cost is not None and cash is not None)
        else "🏦 מזומן בשימוש: —",
    ])
    blocks.append(["📄 הדוח המלא מצורף כקובץ PDF. ✅"])
    return f"\n\n{SEP}\n\n".join("\n".join(b) for b in blocks)


def build_watchlist(*, ticker: str, grade: Optional[str],
                     thesis_sentence: Optional[str], primary: dict,
                     alternate: Optional[dict],
                     disclosure_flags: Optional[list] = None,
                     wait_for: Optional[str] = None,
                     invalidation: Optional[float] = None) -> str:
    """Section ח.2 -- Watchlist. Same field emoji as ח.1, no size block (there
    is no order to price), and the closing line has no check mark because
    nothing was completed."""
    blocks: list[list[str]] = []
    blocks.append([f"👀 <b>{ticker} — עדיין לא, שווה לעקוב</b>",
                    f"⭐ ציון: <b>{grade or '—'}</b>"])
    blocks.append([f"💡 {thesis_sentence or MISSING_SENTENCE}"])
    blocks.append(
        _setup_block("סטאפ ראשי", "🎯", primary, watchlist=True)
        + [""]
        + _setup_block("סטאפ חלופי", "🔁", alternate, watchlist=True, suffix=" (תרחיש שני)")
    )
    disclosures = _disclosures(disclosure_flags)
    blocks.append(["⏳ <b>למה זה לא הזמנה עדיין:</b>"]
                   + (disclosures or ["🔸 ממתין לתנאים שעדיין לא התקיימו"]))
    blocks.append([
        f"👀 מה לחכות לו: {wait_for or 'תנאי לא הוגדר'}",
        f"🛑 מה מבטל את הרעיון לגמרי: {_n(invalidation)}",
    ])
    blocks.append(["📄 הניתוח המלא מצורף כקובץ PDF."])
    return f"\n\n{SEP}\n\n".join("\n".join(b) for b in blocks)


def build_no_trade(*, ticker: str, grade: Optional[str],
                    why_sentence: Optional[str]) -> str:
    """Section ח.3 -- No Trade. Deliberately short: there is no reason to show a
    full setup table in Telegram for something that fails the basic gates. The
    full analysis, both scenarios included, is still in the PDF."""
    blocks = [
        [f"✋ <b>{ticker} — לא מספיק טוב כרגע</b>", f"⭐ ציון: <b>{grade or '—'}</b>"],
        [f"💬 {why_sentence or MISSING_SENTENCE}"],
        ["📄 הניתוח המלא (כולל שני התרחישים) מצורף כקובץ PDF, למקרה שתרצה לעיין."],
    ]
    return f"\n\n{SEP}\n\n".join("\n".join(b) for b in blocks)


# Which fields each template actually takes. The caller hands over everything it
# has -- a Watchlist decision still carries a quantity, a Buy still carries an
# invalidation level -- and each template takes only what belongs in it. Filtering
# here rather than at every call site means one place to look when a field is
# missing from a message, and no caller has to know which template it will hit.
_BUY_FIELDS = {"ticker", "grade", "thesis_sentence", "primary", "alternate",
                "potential", "disclosure_flags", "qty", "cost_usd",
                "heat_after_pct", "heat_cap_pct", "cash_available_usd",
                "trigger_fired"}
_WATCHLIST_FIELDS = {"ticker", "grade", "thesis_sentence", "primary", "alternate",
                      "disclosure_flags", "wait_for", "invalidation"}
_NO_TRADE_FIELDS = {"ticker", "grade"}


def build(decision: Optional[str], **kwargs) -> str:
    """The right template for this decision. Mechanical, per section ח.

    An unrecognised decision falls through to the No Trade shape rather than
    raising: the message still has to go out, and the shortest honest template
    is the safest place to land."""
    if decision in (BUY_NOW, BUY_IF_CONFIRMED):
        return build_buy(decision=decision,
                          **{k: v for k, v in kwargs.items() if k in _BUY_FIELDS})
    if decision == WATCHLIST:
        return build_watchlist(**{k: v for k, v in kwargs.items() if k in _WATCHLIST_FIELDS})
    why = kwargs.get("why_sentence") or kwargs.get("thesis_sentence")
    return build_no_trade(why_sentence=why,
                          **{k: v for k, v in kwargs.items() if k in _NO_TRADE_FIELDS})
