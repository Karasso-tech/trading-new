"""Delivery half of the /screener automation pipeline (2026-07-09).

Takes a JSON file containing a FULLY-DECIDED analysis (setup classification,
targets, grade, verdict -- all Category B judgment already applied by whichever
Claude session produced it) and does the mechanical rest: render widget PNG,
render PDF from the markdown report, send all three to Telegram, save the thesis,
and mark the originating queue message sent. This script makes NO judgment calls
either -- it only reformats and delivers values it's given, same principle as
widget_render.py's own module docstring.

Usage: python bot/deliver_report.py path/to/decision.json
Expected JSON shape:
{
  "ticker": "CRM", "update_id": 123456789, "date": "2026-07-09", "exchange": "NYSE",
  "decision": "Buy Now", "grade": "B",
  "primary_setup": {"type":"...", "trigger":"...", "stop":..., "atr_at_build":...,
                     "stop_basis_level":...,  // rule 24, CONSISTENCY_RULES.md (added 2026-07-30 checkup):
                                              // the raw structural price the stop sits below (base low,
                                              // retest low, Higher Low, Reclaim/Flush/gap candle low) --
                                              // BEFORE the 0.15x ATR14 buffer is subtracted. `stop` itself
                                              // must equal this minus 0.15x atr_at_build (or lower/more
                                              // conservative) -- report_lint._lint_stop_buffer checks it.
                     "targets":[{"price":"...","pct":"...","atr_mult":"...","rr":"...","status":"pass"}],
                     "checkpoints":[{"price":"...","reason":"..."}]},
  "alternate_setup": {...same shape...} | null,
  "metrics": {"atr14_at_build":..., "atr14_pct":..., "dist_sma20_atr":...,
              "rs_vs_spy_20d":..., "rs_vs_spy_5d":..., "rs_vs_qqq_20d":...},
  "verdict": "free text closing paragraph",
  "potential": "194.60", "potential_note": "...",
  "market_regime": "pullback_in_uptrend",  // rule 23 -- copied verbatim from
                                             // account.market_regime_formula.regime UNLESS overridden
  "market_regime_formula": "pullback_in_uptrend",  // rule 23 -- the formula's raw, untouched call --
                                                     // must equal market_regime unless regime_override_reason
                                                     // is present and non-empty
  "regime_override_reason": null,          // rule 23 -- required non-empty string whenever market_regime
                                             // differs from market_regime_formula, otherwise omitted/null
  "rejection_reasons": ["rr_below_2", "regime_against"],  // item 7, Hardening Pass -- which gate/rubric
                                // items actually failed; [] or omitted for a clean Buy Now
  "earnings_verified": false,  // item 5, Hardening Pass -- true ONLY if the earnings date came from
                                // real fetched/user-provided data this run, never model memory
  "freshness": {...},  // item 6, Hardening Pass -- copied verbatim from fetch_analysis_data.py's own
                        // "freshness" field, never recomputed here
  "portfolio_heat_after": 0.045,       // CONSISTENCY_RULES.md rule 19 -- account.portfolio_heat.heat_pct
                                        // recomputed to include this trade's own risk_usd
  "portfolio_heat_cap_pct": 0.06,      // copied verbatim from account.portfolio_heat.cap_pct
  "portfolio_heat_disclosed": true,    // true iff the report actually shows the heat warning when
                                        // portfolio_heat_after exceeds the cap -- report_lint.py checks
                                        // this mechanically but NEVER blocks the decision on it (rule 19
                                        // is disclosure-only, unlike rule 18's regime gate)
  "sector_pct_after": 0.32,            // rule 20 -- this ticker's correlation group's % of the swing book
                                        // including this trade's own risk
  "sector_cap_pct": 0.40,              // copied verbatim from account.sector_cap_pct
  "sector_disclosed": true,            // same disclosure-only convention as portfolio_heat_disclosed --
                                        // never blocks the decision, report_lint.py only checks it's shown
  "cash_required_usd": 28500.00,       // rule 21 -- full dollar cost of this trade at its computed qty
  "cash_available_usd": 31395.69,      // copied verbatim from account.cash_available_usd
  "cash_usage_warn_pct": 0.30,         // copied verbatim from account.cash_usage_warn_pct
  "cash_usage_disclosed": true,        // same disclosure-only convention -- never blocks or reduces size
  "sizing": {                          // rule 28 (added 2026-08-02) -- the order's OWN final numbers,
                                        // copied verbatim from section ד's sizing table, never re-derived.
                                        // Omit entirely for Watchlist/No Trade (no order to size).
    "entry": 132.40,                    // the trigger price the qty was computed from
    "stop": 116.55,                     // that same setup's stop
    "qty": 47,                          // the FINAL quantity
    "risk_usd_target": 1000.0,         // account.risk_usd -- the FULL 1% figure, never a pre-derated one
    "size_reduction_reason": null       // 2026-08-09. There are no size multipliers any more (rule 22/28):
                                        // every trade is one full risk unit. Set this ONLY when the full
                                        // quantity genuinely could not be taken -- "cash_limited" being the
                                        // real case -- and leave it out otherwise. An order under 90% of a
                                        // full position with nothing here is flagged by
                                        // report_lint._lint_size_floor as size_below_full_no_reason.
                                        // A legacy "multipliers" dict is still accepted and still displayed,
                                        // but every key in it is ignored for arithmetic, see
                                        // size_policy.ADVISORY_MULTIPLIER_KEYS.
  },
  "report_markdown": "# full .md report text",
  "summary_text": "short HTML Telegram summary -- must follow SCREENER_v3.md section ח's fixed template
                   exactly for whichever decision this is (ח.1 Buy Now/Buy Only If Confirmed, ח.2
                   Watchlist, ח.3 No Trade), not free-form"
}
"""

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chart_draw
import decision_policy
import persistence
import regime_formula
import report_lint
import size_policy
import summary_text
from tv_data import TVClient, BOT_WATCHLIST_NAME
from widget_render import screener_node_to_widget_data, render_widget_png
from report_pdf import render_report_pdf
from telegram_send import send_text, send_photo, send_document, send_failure_alert

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def display_grade(decision: dict) -> Optional[str]:
    """The letter every surface shows: the RECOMPUTED one wherever the report's
    own numbers were there to check it, the claimed one only when they were not.

    Added 2026-08-30 because the gate and the displays had drifted apart in the
    same change that closed the gate. resolve_grade correctly refused to let a
    report claiming A with five numbers computing to D carry an order -- and the
    photo caption, the Telegram summary line and the PDF's own grade row all
    went on printing A. A blocked order with an A beside it invites the reader
    to wonder what went wrong with the system rather than with the report."""
    verdict = decision_policy.resolve_grade(decision)
    return verdict.recomputed or decision_policy.grade_of(decision)


# The share count, if the report ever carried one -- read BEFORE the gate clears
# `sizing`, and used afterwards to prove the number really is gone from the body.
def _sizing_qty(decision: dict) -> Optional[int]:
    try:
        return int((decision.get("sizing") or {}).get("qty"))
    except (TypeError, ValueError):
        return None


_GRADE_ROW = re.compile(r"(\|\s*(?:דירוג סטאפ|דירוג|ציון)\s*\|)[^|\n]*(\|)")

# One written number, in any of the shapes a number gets written in. Either a
# grouped form whose separators fall in real groups of three (1,000 / 1 000 /
# 1 000 with a non-breaking or narrow space), or a plain run of digits, either
# of them optionally carrying a decimal tail. The lookaround stops a match from
# starting or ending inside a longer number, so 1400 never reads as 40.
_NUMBER_TOKEN = re.compile(
    r"(?<![\d.,])"
    r"(?:\d{1,3}(?:[,\u00a0\u202f ]\d{3})+|\d+)"
    r"(?:\.\d+)?"
    r"(?![\d])"
)


# A number only reads as a share count when something right beside it says so.
#
# Two rounds of measurement shaped this. Matching any number anywhere fired on
# 39 of 40 real reports for a 50-share position -- every report discusses the
# 50-day moving average -- and on most reports for 17 or 22, which are rule
# numbers. Narrowing to "lines that mention shares" still fired on 25 of 40 for
# a 20-share position, because the ordinary Hebrew word for the stock itself,
# "המניה", contains the word for shares, so every line of technical analysis
# qualified. Neither version could be believed, and a check nobody believes
# protects nothing.
#
# So the words have to sit AGAINST the number: a count is written "50 מניות",
# or is labelled "כמות" just before it, or follows a verb that places an order.
# A moving average and a rule number never appear that way. Measured again
# afterwards: 0 of 40 for a 50-share position, 0 for 14, 1 for 17.
#
# Plural units only. Nobody writes "50 מניה", and the singular's only effect was
# to match "המניה" a few words after an unrelated number.
_UNITS_AFTER = ("מניות", "יחידות", "shares")
_LABEL_BEFORE = ("כמות", "qty")
_ORDER_BEFORE = ("קנה", "קניה", "קנייה", "לקנות", "הזמנה", "פקודה", "buy", "limit")

_AFTER_WINDOW = 14    # "50 מניות" -- the unit follows the number immediately
_BEFORE_WINDOW = 16   # "| כמות מלאה | 50 |", "קנה 50 בגבול" -- label or verb first.
                      # Kept short on purpose: a longer reach picks up a buy verb
                      # from an unrelated clause earlier in the same sentence.


def _share_count_numbers(markdown: str):
    """Every number in the text that reads as a quantity of shares.

    Values, not digits: a report that spelled the same count another way --
    1,000 or 1 000 for 1000, 40.0 for 40 -- would otherwise slip past a check
    looking for a string it never contained.

    Percentages are excluded outright. Rule 7's "40% / 60%" allocation is in
    every report, and a number followed by % is an allocation, not a count.

    Deliberately narrow, and what it gives up is worth stating: a count written
    with no unit, no label and no ordering verb anywhere near it is not caught
    here. This is the backstop behind the structural removal, never the defence
    itself.
    """
    for line in markdown.splitlines():
        for m in _NUMBER_TOKEN.finditer(line):
            tail = line[m.end():m.end() + 2]
            if tail[:1] == "%" or tail[:2] in (" %", "\u00a0%", "\u202f%"):
                continue
            after = line[m.end():m.end() + _AFTER_WINDOW]
            before = line[max(0, m.start() - _BEFORE_WINDOW):m.start()].lower()
            if not (any(w in after for w in _UNITS_AFTER)
                    or any(w in before for w in _LABEL_BEFORE)
                    or any(w in before for w in _ORDER_BEFORE)):
                continue
            cleaned = (m.group(0)
                       .replace(",", "")
                       .replace("\u00a0", "")
                       .replace("\u202f", "")
                       .replace(" ", ""))
            try:
                yield float(cleaned)
            except ValueError:
                continue


_SIZING_HEADING = re.compile(
    r"^##\s*(?:[\u05d0-\u05ea]\s*[.．]\s*)?.*(?:גודל פוזיציה|גודל הפוזיציה|sizing).*$",
    re.IGNORECASE)
_NEXT_H2 = re.compile(r"^##(?!#)")


def redact_order_from_markdown(markdown: str, ceiling: str, why: str) -> tuple:
    """Take the share count and the order out of the report body, not just out
    of the Telegram message.

    The Telegram summary is built from structured fields, so clearing `sizing`
    was enough to remove the quantity there. `report_markdown` is different: it
    is prose the model wrote, it is what report_pdf renders and what lands in
    reports/, and it carried the full section D sizing table -- share count,
    position value, cash required -- entirely untouched. A blocked idea was
    therefore shipping a PDF with a complete order in it, attached to a message
    saying there is no order.

    Two edits, both anchored on structure rather than on guessing at numbers:

      * the section D heading (SCREENER_v3's own "גודל פוזיציה" section) through
        to the next H2 is replaced with a stated notice. Everything else --
        both setups, every target table, the technical analysis, the portfolio
        disclosures -- is untouched.
      * the decision row of section B's table is rewritten to the ceiling, so
        the PDF cannot say "Buy Now" while the message says the order was
        refused.

    Returns (markdown, removed_section) so the caller can say whether a table
    was actually taken out or there was simply never one to take. It is the
    caller's job to decide what to do when nothing was found -- see
    order_is_provably_gone."""
    lines = markdown.splitlines()
    out = []
    removed = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if _SIZING_HEADING.match(line.strip()):
            removed = True
            out.append(line)
            out.append("")
            out.append(f"> **הוסר בשליחה.** ההחלטה הורדה ל-{ceiling}, ולכן אין כאן פקודה. "
                        f"{why}. הכמות, שווי הפוזיציה ופרטי ההזמנה הוסרו מהדוח הזה כדי "
                        f"שלא יישאר בו מספר שאפשר להריץ בטעות.")
            out.append("")
            i += 1
            while i < len(lines) and not _NEXT_H2.match(lines[i]):
                i += 1
            continue
        out.append(line)
        i += 1

    text = "\n".join(out)
    # Section B's decision row: "| החלטה | **Buy Now** |" -> the real ceiling.
    text = re.sub(r"(\|\s*החלטה\s*\|)[^|\n]*(\|)",
                   lambda m: f"{m.group(1)} **{ceiling}** (הורד בשליחה) {m.group(2)}",
                   text, count=1)
    return text, removed


def align_report_grade(decision: dict) -> Optional[str]:
    """Put the recomputed letter into the report body, whenever it differs from
    the one written there.

    This lives outside enforce_decision_ceiling because of a gap a live run
    found (2026-08-30, real AMZN report): the ceiling only acts when the stated
    decision claims MORE than the facts permit. A report already sitting at
    Watchlist claims nothing extra, so nothing ran -- and a Watchlist whose body
    said "A" while its own five numbers computed to D shipped with the caption
    and the summary line saying D and the PDF still saying A. Three surfaces,
    two different answers, and the document is the one a person keeps.

    Correcting the letter is not the same act as refusing an order, and it does
    not depend on one. Every report whose stated grade disagrees with its own
    arithmetic gets the row rewritten and both numbers named, blocked or not.

    Returns the Hebrew line for the Telegram message, or None when the report's
    letter already agrees with its numbers (or there are no numbers to check)."""
    verdict = decision_policy.resolve_grade(decision)
    if not verdict.recomputed or verdict.recomputed == verdict.stated:
        return None
    body = decision.get("report_markdown")
    stated = verdict.stated or "לא נכתב"
    note = (f"הציון הכתוב בדוח הוא {stated}; הציון שיוצא מהמספרים של הדוח עצמו "
            f"הוא {verdict.recomputed}, וזה הציון הקובע")
    if body:
        body = _GRADE_ROW.sub(
            lambda m: f"{m.group(1)} **{verdict.recomputed}** (מחושב מהמספרים) {m.group(2)}",
            body, count=1)
        decision["report_markdown"] = f"> **הערה שנוספה בשליחה:** {note}.\n\n" + body
    return f"⭐ <b>הציון תוקן</b> — {note}."


def order_is_provably_gone(markdown: str, removed_section: bool,
                            qty: Optional[int]) -> tuple:
    """Can this body be shown to carry no order any more? Two conditions, both
    required (2026-08-30).

    The redaction above is anchored on SCREENER_v3's own section heading. That
    is the right anchor and it is not a guarantee: report_markdown is written by
    a model, and a run that words the heading differently, or puts the share
    count somewhere else, would sail through a redaction that quietly removed
    nothing. "The regex did not match" and "there was no order" produce the same
    empty result and mean opposite things.

    So the caller is given a definite answer rather than a hopeful one:

      * the sizing section has to have actually been found and replaced, and
      * the share count the report was sized for must no longer appear anywhere
        in the body, as a standalone number

    The quantity is compared by value, and only where it reads as a share count.
    See _share_count_numbers for the two rounds of measurement behind that, and
    for why the looser versions of this check were worth nothing.

    Returns (safe, reason) -- reason is the plain-words explanation to show when
    it is not safe."""
    if not removed_section:
        return False, ("סעיף הגודל לא זוהה בדוח, אז אי אפשר להבטיח שהכמות וההזמנה "
                        "הוסרו ממנו")
    if qty is not None and any(n == float(qty) for n in _share_count_numbers(markdown)):
        return False, (f"המספר {qty} עדיין מופיע בדוח ככמות מניות, אז אי אפשר "
                        f"להבטיח שהכמות ירדה משם")
    return True, ""





def _heat_block(decision: dict) -> Optional[str]:
    """Portfolio heat as a block rather than a note (2026-08-30, owner's call).

    Rules 19-21 were disclosure-only from the start, by explicit direction: the
    system shows the number, the person decides. That is still true for sector
    concentration and for cash, both re-confirmed the same day. Heat is the one
    that changed, and the reason it could is that it is the only one of the
    three the book is not already standing on -- turning on a cap that is
    already breached would block every new idea overnight and teach its reader
    to route around it, which is how a cap stops meaning anything.

    Two refusals that are deliberately NOT overridable:
      * at least one open position has no stop, so the heat total is a floor
        rather than a total
    An override is permission to accept a known risk. It cannot be permission to
    accept an unknown one, because there is nothing there to have accepted.

    Reads the report's OWN disclosed heat rather than recomputing it: the
    decision and the number it is judged against have to be the same number,
    the identical consistency-check-only shape rules 18/23/27 already use.

    Returns the plain-words reason to refuse, or None when the order may pass.
    """
    after = report_lint._clean_number(decision.get("portfolio_heat_after"))
    cap = report_lint._clean_number(decision.get("portfolio_heat_cap_pct"))
    if after is None or cap is None:
        return None          # nothing disclosed to judge -- report_lint says so on its own
    live = persistence.get_portfolio_heat()
    if not live.get("complete"):
        names = ", ".join(u["ticker"] for u in live.get("unmeasurable") or []) or "פוזיציה פתוחה"
        return (f"אי אפשר לחשב את חום התיק: ל-{names} אין סטופ שמור, אז הסיכון הכולל "
                f"הוא לפחות מה שמוצג ואולי יותר. אין לזה עקיפה — עקיפה היא הסכמה "
                f"לסיכון ידוע, לא לסיכון שאיש לא יודע")
    if after <= cap:
        return None
    ticker = decision.get("ticker", "")
    override = persistence.find_risk_override(ticker, "heat")
    if override:
        persistence.consume_risk_override(override["id"])
        decision["heat_override_reason"] = override["reason"]
        return None
    return (f"חום התיק אחרי הטרייד הזה יהיה {after * 100:.2f}% מול תקרה של "
            f"{cap * 100:.2f}%. זה הסיכון הכולל בכל הפוזיציות יחד, לא של הטרייד הזה. "
            f"אם זו החלטה מכוונת: <code>/override {ticker} heat הסיבה שלך</code>")


def enforce_decision_ceiling(decision: dict, node: dict) -> Optional[str]:
    """Make the decision line and the order OBEY the gate, instead of shipping
    alongside a warning that says they should not (2026-08-30).

    `report_lint` has always been advisory by design -- it warns loudly and
    never blocks the send. That is the right posture for an arithmetic
    disagreement about a stated ATR multiple. It is the wrong posture for the
    one finding that says an order should not exist at all: a real delivered
    message carried "Buy Now", a share count, a dollar cost, and a red warning
    saying that combination is forbidden. Four signals, no instruction -- the
    same failure shape rule 28's own size block was written to fix.

    So the ceiling is applied here rather than reported: the decision word is
    lowered to whatever `decision_policy.max_allowed_decision` permits, and the
    `sizing` block is removed so no quantity, cost or order can be rendered from
    it. Nothing is deleted from the analysis -- the thesis, both setups, every
    target scan and the grade are saved and shown exactly as computed. This
    blocks the ORDER, not the thesis, the identical split rule 18 already draws.

    Returns the Hebrew line explaining what was lowered and why, or None when
    the decision already sat inside its own ceiling."""
    stated = decision.get("decision")
    if stated not in decision_policy.ALL_DECISIONS:
        return None
    verdict = decision_policy.resolve_grade(decision)
    grade = verdict.grade
    regime = decision.get("market_regime")
    has_target = (decision_policy.has_qualifying_target(decision.get("primary_setup"))
                  or decision_policy.has_qualifying_target(decision.get("alternate_setup")))
    has_structural_stop = decision_policy.has_orderable_setup(
        decision.get("primary_setup"), decision.get("alternate_setup"))
    # Heat is checked alongside the rest rather than after it, so a report that
    # is fine on every other count is still stopped by it.
    heat_block = _heat_block(decision) if stated in decision_policy.BUY_DECISIONS else None
    if heat_block is None and decision_policy.is_decision_allowed(
            stated, has_target=has_target, grade=grade, regime=regime,
            has_structural_stop=has_structural_stop):
        return None

    ceiling = decision_policy.max_allowed_decision(
        has_target=has_target, grade=grade, regime=regime,
        has_structural_stop=has_structural_stop)
    if heat_block and decision_policy._RANK[ceiling] > decision_policy._RANK[
            decision_policy.WATCHLIST]:
        # Everything else permitted an order; heat is what stopped it.
        ceiling = decision_policy.WATCHLIST
    if heat_block:
        why = heat_block
    elif not has_target:
        why = "אין אף יעד כשיר בשני הסטאפים — אין מה למכור בו, אז אין פקודה לכתוב"
    elif not has_structural_stop:
        why = ("הסטופ לא עומד על שום שפל בגרף — הוא מרחק קבוע של 2 ATR. "
               "זה מספר בלי מקור, והכלל הראשון כאן הוא שאין מחירים מומצאים. "
               "הרעיון נשאר במעקב, ואם ייבנה שפל אמיתי הוא יכול לחזור לפקודה")
    elif regime in decision_policy.BLOCKING_REGIMES:
        why = f"מצב השוק הוא '{regime}', ובמצב כזה לא נשלחת פקודת קנייה"
    elif verdict.reason_key == "blocking_grade":
        why = (f"הציון שיוצא מהמספרים של הדוח הוא {verdict.recomputed}, "
               f"ובציון כזה לא נשלחת פקודה. "
               f"{decision_policy.GRADE_BLOCK_UNMEASURED_HE}")
    elif verdict.reason_key == "mismatch":
        why = (f"כתוב בדוח ציון {verdict.stated}, אבל החישוב מהמספרים שהדוח עצמו "
               f"מציג נותן {verdict.recomputed}. {verdict.reason_he}")
    else:
        why = verdict.reason_he or "אין ציון שאפשר לסמוך עליו בדוח הזה"

    decision["decision"] = ceiling
    node["decision"] = ceiling
    had_order = bool(decision.get("sizing"))
    qty = _sizing_qty(decision)          # read BEFORE the block is cleared
    decision["sizing"] = None
    line = f"🚫 <b>ההחלטה הורדה ל-{ceiling}</b> — {why}."
    if had_order:
        line += " הכמות ופרטי הפקודה הוסרו מההודעה."
    if decision.get("report_markdown"):
        body, removed_section = redact_order_from_markdown(
            decision["report_markdown"], ceiling, why)
        safe, unsafe_why = order_is_provably_gone(body, removed_section, qty)
        decision["report_markdown"] = (
            f"> **הערה שנוספה בשליחה:** ההחלטה בדוח הזה הורדה ל-{ceiling}. {why}.\n\n"
            + body
        )
        decision["_withhold_pdf"] = not safe
        decision["_withhold_reason"] = unsafe_why
        if safe:
            line += " גם מהדוח המלא ומה-PDF."
        else:
            line += (f" \u26a0\ufe0f הדוח המלא לא צורף: {unsafe_why}. "
                     f"הוא נשמר מקומית עם ההערה בראשו.")
    return line


def size_check_block_he(decision: dict) -> str:
    """CONSISTENCY_RULES.md rule 28: the "is this a full position or a small
    one?" lines, computed here in Python and appended to the Telegram summary
    rather than left to SCREENER_v3.md section ח's model-written template.

    Same reasoning as deliver_position_status_report.py computing its own
    stop-distance line: the number that decides whether an order is the right
    size must not depend on a model remembering to write it. Asked by the user
    directly, 2026-08-02, looking at a real size block -- qty, total cost,
    portfolio heat and cash used were all shown, and none of them answered
    whether that order was a full position or a quarter of one.

    The full-risk figure comes from the account settings (the real, stored
    equity x risk_pct), NOT from the decision's own copy of it -- a report
    mis-copying that number is exactly the kind of error this block exists to
    surface. Returns "" whenever there's no order to measure (Watchlist/No
    Trade, or an incomplete sizing block); report_lint records the reason."""
    sizing = decision.get("sizing")
    if not isinstance(sizing, dict):
        return ""
    try:
        entry = float(sizing["entry"])
        stop = float(sizing["stop"])
        qty = int(sizing["qty"])
    except (KeyError, TypeError, ValueError):
        return ""
    if entry <= stop or qty <= 0:
        return ""

    settings = persistence.get_account_settings()
    equity = persistence.get_effective_equity()
    risk_pct = settings.get("risk_pct")
    target = (equity * risk_pct) if (equity and risk_pct) else None
    if not target:
        return ""

    multipliers = sizing.get("multipliers") if isinstance(sizing.get("multipliers"), dict) else {}
    result = size_policy.evaluate_order(qty, entry, stop, target, multipliers)
    reason = sizing.get("size_reduction_reason")
    # The swing sleeve's own size, as a TARGET rather than as what happens to be
    # invested today: equity x (1 - core target). Deliberately not
    # get_allocation_drift()'s swing_pct_actual, which counts only money already
    # in open swing positions -- with one position open that denominator is tiny
    # and the percentage would balloon for no real reason. The planned budget is
    # stable from day to day, which is what makes the number worth reading.
    core_target = settings.get("core_pct_target")
    swing_usd = (equity * (1 - core_target)) if (equity and core_target is not None) else None
    return size_policy.format_size_lines_he(
        result, target, equity_usd=equity,
        reduction_reason=reason.strip() if isinstance(reason, str) and reason.strip() else None,
        swing_usd=swing_usd,
    )


def replaced_fired_thesis_block_he(ticker: str, decision: dict) -> str:
    """Say out loud when a fresh build has just replaced a plan whose trigger
    had ALREADY fired (2026-08-09).

    Found by the owner, on the very first live run of the rebuilt pipeline. A
    /screener ORCL replaced a thesis whose alternate trigger had confirmed green
    on 2026-08-07 and again on 2026-08-08, and produced a new plan with a
    different entry and a different stop -- on a day the market never opened.
    From the phone it looked as though the numbers had changed by themselves.
    They had not: a rebuild had happened, and nothing said so.

    Worse than the confusion, the fired signal is genuinely FORGOTTEN.
    get_trigger_fired_age only counts greens dated on or after the thesis's own
    date_built, so a rebuild resets that clock to today and the system stops
    knowing the trigger ever fired. refresh_pending.py already refuses to
    rebuild a fired thesis for exactly this reason; a hand-sent /screener has
    never had that guard.

    This does not block the rebuild -- the owner asked for it, and the new plan
    may well be the better one. It states what was replaced, with both sets of
    numbers, so the swap is visible rather than silent. Same posture as
    refresh_pending's own before/after message and the house rule behind it:
    never quietly overwrite stored work, always show the diff."""
    fired = persistence.get_trigger_fired_age(ticker)
    if not fired:
        return ""
    prior = persistence.get_thesis(ticker) or {}
    new_setup = decision.get("primary_setup") or {}

    def _levels(setup):
        setup = setup or {}
        trigger = report_lint._clean_number(setup.get("trigger"))
        stop = report_lint._clean_number(setup.get("stop"))
        return (f"{trigger:,.2f}" if trigger is not None else "—",
                f"{stop:,.2f}" if stop is not None else "—")

    old_t, old_s = _levels(prior.get("primary_setup"))
    new_t, new_s = _levels(new_setup)
    return (
        "🔄 <b>שים לב — התוכנית הקודמת הוחלפה</b>\n"
        f"לטיקר הזה כבר הייתה תוכנית שהטריגר שלה הופעל (לפני {fired['trading_days']} ימי מסחר, "
        f"ב-{fired['first_green_date']}). הריצה הזו בנתה תוכנית חדשה במקומה.\n"
        f"• קודם: כניסה <b>{old_t}</b> · סטופ <b>{old_s}</b>\n"
        f"• עכשיו: כניסה <b>{new_t}</b> · סטופ <b>{new_s}</b>\n"
        "המספרים השתנו כי נבנתה תוכנית חדשה — לא כי המחיר זז. "
        "אם התכוונת לפעול לפי התוכנית הישנה, היא שמורה ואפשר לחזור אליה."
    )


def _as_percent(value) -> Optional[float]:
    """A stored fraction as a real percentage for display (0.048 -> 4.8).

    Every ratio this system stores is a fraction: `risk_pct` is 0.01,
    `portfolio_heat_cap_pct` is 0.06, `sector_cap_pct` is 0.40. The suffix says
    "pct" but the value never has been one, and that mismatch is exactly what
    printed "0.05%" on a real report where the truth was 4.8%.

    Values above 1 are passed through untouched. Nothing in this system holds a
    heat or sector fraction above 1.0 (that would mean risking more than the
    whole account), so a number bigger than that is already a percentage --
    which is what a caller hands over when it has done the conversion itself."""
    number = report_lint._clean_number(value)
    if number is None:
        return None
    return number * 100 if abs(number) <= 1 else number


def build_summary_he(decision: dict) -> str:
    """SCREENER_v3.md section ח's message, built here rather than taken from the
    decision JSON (2026-08-09).

    Section ח does not describe a style, it dictates a form: fixed emoji, fixed
    line order, exactly fifteen separator bars, every number bold and rounded to
    two decimals, a fixed translation table for the six setup names, and the
    choice between the three templates made mechanically from the decision word.
    A fixed form with numbers dropped into it is an f-string, and asking a model
    to retype it every run is asking it to do a job code does perfectly. The
    failure mode was silent: a reworded line or a missing separator is not
    something anyone notices, it just makes two reports look different.

    What the model still supplies, because it is genuinely judgment: the one
    plain sentence of thesis, and for a Watchlist what to wait for and what
    kills the idea. Those arrive as their own top-level fields. A `summary_text`
    that arrives anyway is ignored rather than merged -- two sources for one
    message is exactly how the wording drifted in the first place.

    Falls back to whatever the model wrote if the structured fields are not
    there at all (an older decision JSON): a delivered message with drifted
    wording still beats no message."""
    setup = decision.get("primary_setup") or {}
    if not setup.get("trigger") and not decision.get("thesis_sentence"):
        return decision.get("summary_text") or f"🔍 <b>{decision.get('ticker')} — {decision.get('decision')}</b>"

    return summary_text.build(
        decision.get("decision"),
        ticker=decision.get("ticker"),
        grade=display_grade(decision),
        thesis_sentence=decision.get("thesis_sentence"),
        primary=setup,
        alternate=decision.get("alternate_setup"),
        potential=report_lint._clean_number(decision.get("potential")),
        disclosure_flags=decision.get("disclosure_flags"),
        qty=(decision.get("sizing") or {}).get("qty"),
        cost_usd=report_lint._clean_number(decision.get("cash_required_usd")),
        # Heat and its cap are stored as FRACTIONS everywhere in this system
        # (account_settings.portfolio_heat_cap_pct is 0.06, get_portfolio_heat
        # returns 0.0445), and the template appends its own "%" sign. Passing
        # them raw printed "0.05%" where the real answer was 4.8% -- a number
        # nearly a hundred times too small, shown to the owner on a real
        # delivered report. Found on the first live run, 2026-08-09.
        heat_after_pct=_as_percent(decision.get("portfolio_heat_after")),
        heat_cap_pct=_as_percent(decision.get("portfolio_heat_cap_pct")),
        cash_available_usd=report_lint._clean_number(decision.get("cash_available_usd")),
        wait_for=decision.get("wait_for"),
        invalidation=report_lint._clean_number(decision.get("invalidation")),
    )


async def _tv_side_effects(ticker: str, primary_setup: dict, alternate_setup: dict,
                            position: Optional[dict] = None) -> None:
    """Watchlist sync + chart annotation share one CDP connection -- opening two
    separate TVClient sessions back-to-back for two unrelated side effects on the
    same run would double the connect overhead and the failure surface for no
    benefit. Both are optional/best-effort (see this function's own call site).

    `position` (2026-08-07): a re-screen of a ticker the user already holds
    draws the position line set instead of this fresh build's plan. The new
    thesis is still saved and reported in full -- it is the right thing to read
    before deciding to add or exit -- but the CHART has to keep showing the
    trade that is actually on, including its real trailed stop, not a plan for
    an entry that already happened. See chart_draw.py's module docstring."""
    async with TVClient() as client:
        await client.add_to_watchlist(ticker, list_name=BOT_WATCHLIST_NAME)
        if position:
            await chart_draw.annotate_position_chart(client, ticker, position)
        else:
            await chart_draw.annotate_chart(client, ticker, primary_setup, alternate_setup)


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python bot/deliver_report.py path/to/decision.json", file=sys.stderr)
        sys.exit(1)

    decision_path = Path(sys.argv[1])
    decision = json.loads(decision_path.read_text(encoding="utf-8"))

    ticker = decision["ticker"]
    update_id = decision["update_id"]

    try:
        node = {
            "decision": decision["decision"],
            "primary_setup": decision.get("primary_setup"),
            "alternate_setup": decision.get("alternate_setup"),
            "grade": display_grade(decision),
            "atr14_at_build": decision.get("metrics", {}).get("atr14_at_build"),
            "atr14_pct": decision.get("metrics", {}).get("atr14_pct"),
            "dist_sma20_atr": decision.get("metrics", {}).get("dist_sma20_atr"),
            "rs_vs_spy_20d": decision.get("metrics", {}).get("rs_vs_spy_20d"),
            "rs_vs_spy_5d": decision.get("metrics", {}).get("rs_vs_spy_5d"),
            "rs_vs_qqq_20d": decision.get("metrics", {}).get("rs_vs_qqq_20d"),
            "verdict": decision.get("verdict", ""),
            "potential": decision.get("potential"),
            "potential_note": decision.get("potential_note", ""),
        }

        # Item 3 (Hardening Pass): deterministic arithmetic re-check on the numbers
        # Claude already chose -- never blocks the send, always logged, always
        # surfaced prominently on any failure (see report_lint.py's own docstring).
        lint_result = report_lint.lint_screener_decision(decision)
        persistence.record_lint_result(ticker, "SCREENER_v3", lint_result.to_dict())

        # Fix (2026-07-16, real AMZN incident): a target whose recomputed ATR-
        # distance/R:R fails rule 3's qualify gate is removed from what actually
        # gets rendered/persisted as a target, not just flagged alongside the
        # still-wrong numbers -- see report_lint.failing_target_keys's own
        # docstring. node["primary_setup"]/["alternate_setup"] are the SAME dict
        # objects as decision[...], so this also fixes what save_thesis() below
        # persists -- a target already known to be invalid should never become
        # part of the stored thesis /monitor later reads as real either.
        bad_targets = report_lint.failing_target_keys(lint_result)
        # Fix (2026-07-16, real NVDA/CRM/AMZN incident): a target that still
        # passes the gate can still have a measurably wrong DISPLAYED distance/
        # R:R (e.g. from a stale intermediate price read mid-session) -- see
        # report_lint.target_corrections's own docstring. Surviving targets get
        # their stated numbers overwritten with the verified-correct ones,
        # rather than showing the user a wrong number next to a correct
        # pass/fail conclusion.
        corrections = report_lint.target_corrections(lint_result)
        # Fix (2026-07-20, real AMZN/LLY/CRM/UPS incident, deliver_playbook_report.py):
        # the above two fixes only ever patched this structured dict (which feeds
        # the widget PNG) -- report_markdown (which feeds the PDF and the saved
        # .md, arguably the more authoritative "full report") kept showing the
        # exact wrong numbers/mislabeled targets report_lint already caught.
        # `stated` captures each target's ORIGINAL text before it's overwritten
        # below, since that's the exact substring still sitting in
        # report_markdown's table row -- see report_lint.patch_report_markdown's
        # own docstring.
        stated_targets = {}
        for label, setup in (("Primary", node.get("primary_setup")), ("Alternate", node.get("alternate_setup"))):
            if setup and setup.get("targets"):
                for i, t in enumerate(setup["targets"], start=1):
                    stated_targets[(label, i)] = {
                        "price": t.get("price"), "atr_mult": t.get("atr_mult"), "rr": t.get("rr"),
                    }
                kept = []
                for i, t in enumerate(setup["targets"], start=1):
                    if (label, i) in bad_targets:
                        continue
                    fix = corrections.get((label, i))
                    if fix:
                        t = dict(t)
                        if "atr_mult" in fix:
                            t["atr_mult"] = f"{fix['atr_mult']:.2f}x"
                        if "rr" in fix:
                            t["rr"] = f"{fix['rr']:.2f}"
                    kept.append(t)
                setup["targets"] = kept
        if decision.get("report_markdown"):
            decision["report_markdown"] = report_lint.patch_report_markdown(
                decision["report_markdown"], stated_targets, bad_targets, corrections
            )

        # The one lint finding that is not advisory: a decision line claiming
        # more than the facts permit is corrected here, not merely reported.
        # Runs AFTER the target corrections above, so "does a qualifying target
        # exist" is asked of the targets that actually survived the gate.
        ceiling_warning = enforce_decision_ceiling(decision, node)
        # Independent of the block above, and deliberately after it so the two
        # notes read in the order they happened: the letter in the body is made
        # to agree with the letter every other surface shows.
        grade_warning = align_report_grade(decision)

        # Item 6 (Hardening Pass): freshness dict is a verbatim pass-through from
        # fetch_analysis_data.py -- never recomputed here, never blocks the send.
        freshness_warning = report_lint.format_freshness_warning_he(decision.get("freshness"))

        # Item 8 (Hardening Pass): circuit breaker -- standing warning only, never
        # a block; the human decides. Inactive (None) unless CIRCUIT_BREAKER_STOPOUTS
        # is actually set in .env.
        breaker = persistence.circuit_breaker_status()
        breaker_warning = (
            f"🛑 {breaker['streak']} סטופים רצופים — שקול הפסקה" if breaker["tripped"] else None
        )

        widget_data = screener_node_to_widget_data(
            ticker, node, decision.get("date", ""), exchange=decision.get("exchange", "")
        )
        combined_warnings = list(decision.get("warnings") or []) + lint_result.warning_lines_he()
        if grade_warning:
            combined_warnings.insert(0, grade_warning)
        if ceiling_warning:
            combined_warnings.insert(0, ceiling_warning)
        if freshness_warning:
            combined_warnings.append(freshness_warning)
        if breaker_warning:
            combined_warnings.append(breaker_warning)
        if combined_warnings:
            widget_data["warnings"] = combined_warnings

        png = render_widget_png(widget_data)
        # Withheld when the gate blocked the order and the redaction could not
        # PROVE the body no longer carries it (see order_is_provably_gone). The
        # .md is still written to reports/ below, with the note at its head --
        # the local record keeps everything, what stops is sending a document
        # that might still contain a runnable order.
        withhold_pdf = bool(decision.pop("_withhold_pdf", False))
        withhold_reason = decision.pop("_withhold_reason", "")
        pdf_bytes = None if withhold_pdf else render_report_pdf(decision["report_markdown"])

        # Save the report to reports/ for the historical record, same convention as
        # every interactively-produced report this session.
        # update_id in the filename avoids overwriting an earlier same-day report if
        # the same ticker is screened more than once in one day (same class of bug
        # found real in deliver_playbook_report.py, 2026-07-09).
        report_filename = f"{ticker.lower()}_screener_{decision.get('date', 'unknown')}_{update_id}.md"
        (PROJECT_ROOT / "reports" / report_filename).write_text(
            decision["report_markdown"], encoding="utf-8"
        )

        resp_photo = send_photo(png, caption=f"{ticker} — {decision['decision']} (Grade {display_grade(decision) or '?'})")
        resp_doc = ({"ok": True, "withheld": True} if withhold_pdf else
                    send_document(pdf_bytes, filename=report_filename.replace(".md", ".pdf"),
                                   caption=f"דוח מלא {ticker}"))
        # Rule 28's size block goes AFTER the model's own summary, as its own
        # separated section -- it answers a question section ח's template
        # cannot (full position or a small one), and appending it rather than
        # asking the template to carry it keeps the number deterministic.
        size_block = size_check_block_he(decision)
        # Market condition, 2026-08-03: advisory, computed here from the
        # decision's own regime field so the wording is identical on every
        # report and can't drift per run. Same append-don't-delegate reasoning
        # as the size block above.
        regime_block = regime_formula.describe_regime_he(decision.get("market_regime") or "")
        # Read BEFORE save_thesis below replaces the row -- once the rebuild
        # lands, date_built is today and the old plan's greens no longer count.
        replaced_block = replaced_fired_thesis_block_he(ticker, decision)
        summary_text = (
            (f"{ceiling_warning}\n\n" if ceiling_warning else "")
            + (f"{grade_warning}\n\n" if grade_warning else "")
            + report_lint.format_warning_block_he(lint_result)
            + (f"{replaced_block}\n\n" if replaced_block else "")
            + (f"{freshness_warning}\n\n" if freshness_warning else "")
            + (f"{breaker_warning}\n\n" if breaker_warning else "")
            + build_summary_he(decision)
            + (f"\n\n━━━━━━━━━━━━━━━\n\n{size_block}" if size_block else "")
            + (f"\n\n━━━━━━━━━━━━━━━\n\n{regime_block}" if regime_block else "")
        )
        resp_text = send_text(summary_text)

        if not (resp_photo.get("ok") and resp_doc.get("ok") and resp_text.get("ok")):
            persistence.mark_failed(update_id, "one or more Telegram sends failed")
            print("FAILED: a Telegram send did not confirm ok=True")
            send_failure_alert(f"{ticker} screener report", "one or more Telegram sends failed")
            sys.exit(1)

        persistence.save_thesis(
            ticker, status="pending", source="SCREENER_v3",
            primary_setup=decision.get("primary_setup"),
            alternate_setup=decision.get("alternate_setup"),
            # Both field names, via one function: `build_plan.py` emits
            # `rubric_grade`, hand-assembled decision files carry `grade`, and
            # this line used to read only the second -- so a report stating a
            # perfectly good letter under the first name was stored as NULL.
            rubric_grade=display_grade(decision),
            # The six numbers behind the letter, so the shadow book can
            # re-score this exact setup against the entry it really gets.
            rubric_inputs=decision.get("rubric_inputs"),
            market_regime_at_build=decision.get("market_regime"),
            # Rule 27 / MONITOR_v2's Starter option is computed as half of this
            # number, and it was never once passed here: 3 theses out of 67 had
            # it on file. Read from the sizing block the report already carries
            # (None for Watchlist/No Trade, which have no order to size, and
            # None once enforce_decision_ceiling has removed a blocked one --
            # both correct: an idea with no order has no planned quantity).
            planned_qty=(decision.get("sizing") or {}).get("qty"),
            # Item 7 (Hardening Pass, shadow-book capture): saved on EVERY run,
            # including Watchlist/No Trade -- same unconditional call as everything
            # else here, no separate code path for a "rejected" outcome.
            decision=decision.get("decision"),
            rejection_reasons=decision.get("rejection_reasons"),
        )

        # 2026-07-11: mirror a real thesis (has a primary_setup, same criterion
        # get_pending_report_rows() uses) onto the TradingView "Bot Watchlist", and
        # (2026-07-13) draw its trigger/stop/target/checkpoint levels directly on
        # the live chart. Neither is fatal -- the report is already delivered and
        # the thesis already saved above; UI automation against TradingView's DOM
        # or its chart-drawing API can legitimately fail (CDP down, markup drift)
        # without that meaning /screener itself failed.
        if decision.get("primary_setup"):
            try:
                asyncio.run(_tv_side_effects(
                    ticker, decision.get("primary_setup"), decision.get("alternate_setup"),
                    position=persistence.get_open_position(ticker),
                ))
            except Exception as e:
                print(f"WARNING: TradingView side-effects (watchlist/chart draw) failed for {ticker}: {e}", file=sys.stderr)

        persistence.mark_sent(update_id, [
            resp_photo.get("result", {}).get("message_id"),
            resp_doc.get("result", {}).get("message_id"),
            resp_text.get("result", {}).get("message_id"),
        ])
        print(f"OK: {ticker} delivered and marked sent (update_id={update_id})")
    except Exception as e:
        persistence.mark_failed(update_id, str(e))
        print(f"FAILED: {e}", file=sys.stderr)
        send_failure_alert(f"{ticker} screener report", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
