"""The /positions pulse-check lines, built in Python instead of retyped by a
model (2026-08-10).

Third of the fixed message forms to move into code, after the screener's
summary (bot/summary_text.py) and the automatic scan (bot/monitor_text.py).
Same reason as both: STRATEGY_v3.md section ח.2 dictates a form, not a style
-- a fixed action word from a closed list of eight, a fixed price/profit line,
then EXACTLY one of three status lines "chosen mechanically" by comparing real
distances against the same 0.7x/1x ATR thresholds the rest of the system uses.
A choice made by comparing two numbers is not judgment; it is an `if`.

This one carries live money, unlike the scan: every ticker here is a real
filled position. So the numbers that describe it are read from the database
rather than accepted from the payload -- the stop the system actually holds,
the shares still held after any partial exits, the blended entry price, the
next unsold tranche. A profit figure retyped from a screenshot and a profit
figure computed from the recorded fills are two different numbers, and only
one of them is checkable.

What the model still supplies, and all it supplies:
  * `action` -- one of the eight fixed words. Which of them is real judgment.
  * `price`, `atr14` -- copied straight from the ticker's fetched data.
  * `sentence` -- one plain line, ONLY when there is something real to say.
  * `last_bar_close` / `bar_fresh` -- for a Starter, the two facts the
    confirmation rule is made of. The rule itself is applied here.

Profit and loss are shown on the shares still held, not on the original fill:
after a partial exit the money already taken off the table is realised and
belongs in the trade's record, not in "how is this position doing right now".
"""

from __future__ import annotations

from typing import Optional

from report_lint import STOP_NOISE_FLOOR_ATR
from summary_text import SEP, _i, _n, _num

# Section ח.2's own threshold for "approaching the target". Named here rather
# than borrowed from one of report_lint's other ATR constants, which measure
# different things (whether a target qualifies at all, how wide its band is) --
# two rules that happen to share a number today must not share a name.
APPROACHING_TARGET_ATR = 1.0

# Section ח's fixed action table, used by BOTH commands. The word is the
# model's to choose; the emoji and the Hebrew are not.
ACTION_HE = {
    "Hold": ("✅", "מחזיקים כרגיל"),
    "Hold With Alert": ("👀", "מחזיקים, אבל שמים לב"),
    "Trim": ("✂️", "מוכרים חלק, מקטינים סיכון"),
    "Sell Partial": ("💰", "מוכרים חלק (מימוש יעד)"),
    "Exit": ("🚪", "יוצאים מהפוזיציה לגמרי"),
    "Protect With Stop": ("🛡️", "מעדכנים סטופ להגנה"),
    "Add Only If Confirmed": ("➕", "אפשר להוסיף, רק אם מאושר"),
    "No Action": ("⚪", "אין צורך לפעול"),
}

# An action nobody recognises gets its own visible mark rather than a guess at
# which of the eight was meant -- same posture as decision_policy's unknown
# sign. Silently rendering it as "Hold" would turn a broken payload into a
# calm-looking message.
UNKNOWN_ACTION = ("❔", "לא נמסרה פעולה")

# The three status lines. Exactly one is shown, never two and never none.
CLOSE_TO_STOP = "🔶 קרוב לסטופ ({stop}) — שווה מעקב היום."
APPROACHING_TARGET = "🏆 מתקרב ליעד ({target})."
SAFE_DISTANCE = "✅ מרחק בטוח מהסטופ ומהיעד, אין צורך בתשומת לב מיוחדת."
# Same "nothing to worry about" case, for a position whose numeric targets are
# all sold: there is no target left to be far from, and saying there is would
# be describing a level that was already realised (the ASTS incident).
SAFE_DISTANCE_RUNNER = ("✅ מרחק בטוח מהסטופ — מה שנשאר זו יתרת הפוזיציה, "
                         "בלי מחיר יעד קבוע.")
# A Core/Layer1 holding never had a swing target to be far from, so it gets its
# own wording rather than the Runner's -- "what is left is the remainder" is a
# true sentence about a swing trade that has sold its targets and a confusing
# one about an index fund nobody set a target for.
SAFE_DISTANCE_CORE = "✅ מרחק בטוח מהסטופ, אין צורך בתשומת לב מיוחדת."

# The 🎯 target-progress line (2026-08-12, the owner's GLD question). The
# pulse check used to say "safe distance from the target" without ever saying
# WHICH target -- after a partial exit that reads as if the system forgot the
# sale. Built from the tranche plan the database holds, never from the payload,
# so a sold target can only ever show as sold.
NEXT_TARGET = "🎯 היעד הבא: {price} — מוכרים שם {qty} יחידות"
NEXT_TARGET_NO_QTY = "🎯 היעד הבא: {price}"
TARGET_DONE_PREFIX = "יעד {n} כבר מומש · "
TARGETS_DONE_PREFIX = "יעדים {ns} כבר מומשו · "

# Section ח's Starter block. Which line shows is decided by the mechanical
# rule, never by reading the chart.
STARTER_CONFIRMED_ADD = ("🌱 ✅ נר יומי נסגר מעל הטריגר המקורי ({trigger}) — אפשר לשקול "
                          "השלמה לפוזיציה מלאה: עוד <b>{add_qty}</b> יחידות כדי להגיע "
                          "לגודל המלא לפי התזה.")
STARTER_CONFIRMED_UNKNOWN = ("🌱 ✅ נר יומי נסגר מעל הטריגר המקורי ({trigger}) — אפשר לשקול "
                              "השלמה לפוזיציה מלאה (הכמות המדויקת לא ידועה — אין גודל מלא "
                              "שמור על התזה המקורית).")
STARTER_CONFIRMED_FULL = ("🌱 ✅ נר יומי נסגר מעל הטריגר המקורי ({trigger}) — הכמות שכבר "
                           "מוחזקת מגיעה לגודל המלא לפי התזה, אין תוספת נדרשת.")
STARTER_NOT_CONFIRMED = ("🌱 ⏳ עדיין אין סגירה יומית מעל הטריגר ({trigger}) — עדיף להמתין "
                          "לפני השלמה לפוזיציה מלאה.")
STARTER_STALE_SUFFIX = " · ⚠️ כבר {days} ימי מסחר כ-Starter"

# Core/Layer1 positions have no swing target by definition, so the
# approaching-target line does not apply to them at all.
CORE_SLEEVE = "core"


def action_line(action: Optional[str], ticker: str) -> str:
    emoji, words = ACTION_HE.get(str(action or "").strip(), UNKNOWN_ACTION)
    return f"{emoji} <b>{ticker}</b> — {words}"


def profit_line(price, entry_price, qty) -> str:
    """Price now, and what the shares still held are up or down.

    Both figures are computed here from the recorded blended entry price and
    the live share count -- never taken from the payload. A position that has
    no usable entry price on file shows a dash rather than a zero, which would
    read as "flat" when it means "unknown"."""
    price_num, entry_num, qty_num = _num(price), _num(entry_price), _num(qty)
    line = f"מחיר עכשיו: {_n(price_num)}"
    if price_num is None or entry_num is None or not entry_num:
        return f"{line}   רווח/הפסד: <b>—</b>"
    pct = (price_num / entry_num - 1.0) * 100.0
    usd = (price_num - entry_num) * (qty_num or 0.0)
    return f"{line}   רווח/הפסד: {_n(pct)}% (${usd:,.2f})"


def status_line(*, price, stop, target, atr14, sleeve: Optional[str] = None,
                 runner_only: bool = False) -> str:
    """Exactly one of section ח.2's three lines, chosen by comparing numbers.

    Order matters and is the document's own: the stop is checked first, because
    a position that is both near its stop and near its target is a position
    whose risk is the thing worth saying out loud.

    With no ATR figure to measure against, neither distance can be judged --
    that comes back as the safe-distance line only when the stop distance
    itself is unknown too; a missing measurement is never reported as a
    reassurance about a level nobody measured."""
    price_num, stop_num, atr_num = _num(price), _num(stop), _num(atr14)
    target_num = _num(target)

    if price_num is not None and stop_num is not None and atr_num:
        if abs(price_num - stop_num) < STOP_NOISE_FLOOR_ATR * atr_num:
            return CLOSE_TO_STOP.format(stop=_n(stop_num))

    target_applies = (sleeve != CORE_SLEEVE) and not runner_only
    if (target_applies and price_num is not None and target_num is not None
            and atr_num and abs(target_num - price_num) < APPROACHING_TARGET_ATR * atr_num):
        return APPROACHING_TARGET.format(target=_n(target_num))

    if sleeve == CORE_SLEEVE:
        return SAFE_DISTANCE_CORE
    return SAFE_DISTANCE_RUNNER if runner_only else SAFE_DISTANCE


def target_progress_line(tranches: Optional[list], *,
                          sleeve: Optional[str] = None) -> Optional[str]:
    """The 🎯 line: which numbered target is next, and which are already sold.

    Reads the position's own tranche plan (persistence attaches it to every
    open position, marked done/next off the real exits rows). No line at all
    for a Core/Layer1 holding, for a position with no numbered targets, or
    once every numbered target is sold -- the runner wording of the status
    line already covers that last case, and saying it twice is noise."""
    if sleeve == CORE_SLEEVE:
        return None
    numbered = [t for t in (tranches or [])
                if isinstance(t, dict) and str(t.get("label", "")).startswith("target_")
                and _num(t.get("planned_qty"))]
    pending = [t for t in numbered if t.get("status") != "filled"]
    if not numbered or not pending:
        return None

    nxt = pending[0]
    qty = _num(nxt.get("planned_qty"))
    filled = _num(nxt.get("filled_qty")) or 0
    left = max(qty - filled, 0) if qty is not None else None
    if left:
        line = NEXT_TARGET.format(price=_n(nxt.get("price")), qty=f"{left:,.0f}")
    else:
        line = NEXT_TARGET_NO_QTY.format(price=_n(nxt.get("price")))

    sold_ns = [str(t.get("label"))[len("target_"):] for t in numbered
               if t.get("status") == "filled"]
    if len(sold_ns) == 1:
        line = TARGET_DONE_PREFIX.format(n=sold_ns[0]) + line
    elif sold_ns:
        line = TARGETS_DONE_PREFIX.format(ns="+".join(sold_ns)) + line
    return line


def starter_line(*, trigger, add_to_full_qty, last_bar_close, bar_fresh: bool,
                  days_since_starter=None, starter_stale: bool = False) -> Optional[str]:
    """Section ח's Starter confirmation, applied rather than judged.

    Confirmed means two facts, both of them already in the payload: the last
    daily bar really has settled, and it closed at or above the trigger the
    position was opened on. Anything else -- including a bar that has not
    settled yet -- is not confirmed. None of that is a reading of the chart,
    which is exactly why it belongs here.

    Returns None when there is no trigger on file to compare against: an
    invitation to "add above the original trigger" with no trigger shown is
    worse than no line at all."""
    trigger_num = _num(trigger)
    if trigger_num is None:
        return None
    close_num = _num(last_bar_close)
    confirmed = bool(bar_fresh) and close_num is not None and close_num >= trigger_num

    if not confirmed:
        line = STARTER_NOT_CONFIRMED.format(trigger=_n(trigger_num))
    else:
        add = _num(add_to_full_qty)
        if add is None:
            line = STARTER_CONFIRMED_UNKNOWN.format(trigger=_n(trigger_num))
        elif add <= 0:
            line = STARTER_CONFIRMED_FULL.format(trigger=_n(trigger_num))
        else:
            line = STARTER_CONFIRMED_ADD.format(trigger=_n(trigger_num), add_qty=f"{add:,.0f}")

    days = _num(days_since_starter)
    if starter_stale and days is not None:
        line += STARTER_STALE_SUFFIX.format(days=f"{days:,.0f}")
    return line


# --- The daily portfolio review (/playbook, section ח.1) --------------------
#
# The same per-position facts as the pulse check, with room for the levels: a
# stop line, a target line that says what the move is worth in plain percent,
# and -- before anything else -- the block that fires when the broker screen
# and this system's own records disagree.

PLAYBOOK_HEAD = "📊 <b>תיק ההשקעות שלי — {date}</b>"
PLAYBOOK_REGIME = "⭐ מצב שוק כללי: {regime}"
PLAYBOOK_CLOSER = "📄 הדוח המלא מצורף כקובץ PDF."

STOP_LINE = "🛑 סטופ: {stop}"
TARGET_LINE = "🏆 יעד: {target} — מוכרים {pct}% (עלייה של <b>+{gain}%</b> מכאן)"
TARGET_SECOND = "  ·  יעד נוסף: {target}, מוכרים {pct}%"
TARGET_RUNNER = "🏆 נשארת כיתרה, בלי מחיר יעד קבוע"

# Section ח.1's mismatch block. It appears only for a real difference and is
# never left out when there is one, because it is the only place the system
# admits its own records are behind -- and the exact command, with the real
# numbers already in it, is the whole point: "send the right command" is not
# an instruction anyone can act on at a glance.
MISMATCH_HEAD = "🔴 <b>לתשומת לב — {ticker}:</b> {sentence}"
MISMATCH_COMMAND = "   שלח: <code>{command}</code>"
MISMATCH_SOLD = "במסך יש פחות מניות ממה שרשום כאן — כנראה נמכרו ולא דווח."
MISMATCH_ADDED = "במסך יש יותר מניות ממה שרשום כאן — כנראה נקנו ולא דווח."
MISMATCH_NEW = "המניה הזו מופיעה במסך אבל לא רשומה כאן בכלל."


def _gain_pct(price, target) -> Optional[float]:
    price_num, target_num = _num(price), _num(target)
    if price_num is None or target_num is None or not price_num:
        return None
    return (target_num / price_num - 1.0) * 100.0


def playbook_target_line(price, targets: Optional[list], *, runner_only: bool = False,
                          sleeve: Optional[str] = None) -> Optional[str]:
    """The 🏆 line: the nearest target still to be sold, what share of the
    position goes there, and what that move is worth from today's price.

    Core/Layer1 holdings have no swing target by definition and get no line at
    all -- section ח.1 is explicit that they omit it rather than write "no
    target". A swing position whose numeric targets are all sold says that its
    remainder rides the trailing stop, which is a fact about the plan, not a
    missing number."""
    if sleeve == CORE_SLEEVE:
        return None
    targets = [t for t in (targets or []) if isinstance(t, dict) and _num(t.get("price")) is not None]
    if runner_only or not targets:
        return TARGET_RUNNER

    first = targets[0]
    gain = _gain_pct(price, first.get("price"))
    line = TARGET_LINE.format(target=_n(first.get("price")), pct=_i(first.get("pct")),
                               gain=f"{gain:,.2f}" if gain is not None else "—")
    if len(targets) > 1:
        line += TARGET_SECOND.format(target=_n(targets[1].get("price")),
                                      pct=_i(targets[1].get("pct")))
    return line


def mismatch_block(*, ticker: str, kind: str, command: str) -> str:
    """One 🔴 block: which ticker, what the difference means in plain words,
    and the exact command that fixes it."""
    sentence = {"sold": MISMATCH_SOLD, "added": MISMATCH_ADDED,
                "new": MISMATCH_NEW}.get(kind, MISMATCH_NEW)
    return (MISMATCH_HEAD.format(ticker=ticker, sentence=sentence) + "\n"
            + MISMATCH_COMMAND.format(command=command))


def playbook_position_block(*, ticker: str, action: Optional[str],
                             price=None, entry_price=None, qty=None,
                             stop=None, targets: Optional[list] = None,
                             sleeve: Optional[str] = None,
                             runner_only: bool = False,
                             entry_type: Optional[str] = None,
                             trigger=None, add_to_full_qty=None,
                             last_bar_close=None, bar_fresh: bool = False,
                             days_since_starter=None, starter_stale: bool = False,
                             sentence: Optional[str] = None,
                             warnings: Optional[list] = None) -> str:
    """One position's block in the daily review.

    Same opening two lines as the pulse check on purpose -- one position should
    read the same way in both reports -- then the levels the longer report has
    room for."""
    lines = [action_line(action, ticker), profit_line(price, entry_price, qty),
             STOP_LINE.format(stop=_n(stop))]

    target_line = playbook_target_line(price, targets, runner_only=runner_only, sleeve=sleeve)
    if target_line:
        lines.append(target_line)

    if entry_type == "starter":
        starter = starter_line(trigger=trigger, add_to_full_qty=add_to_full_qty,
                                last_bar_close=last_bar_close, bar_fresh=bar_fresh,
                                days_since_starter=days_since_starter,
                                starter_stale=starter_stale)
        if starter:
            lines.append(starter)

    if sentence:
        lines.append(f"💬 {sentence}")
    for warning in warnings or []:
        lines.append(f"⚠️ {warning}")
    return "\n".join(lines)


def build_playbook_summary(*, date: str, regime_he: Optional[str] = None,
                            positions: Optional[list] = None,
                            mismatches: Optional[list] = None) -> str:
    """Section ח.1 -- the whole daily review message.

    Four blocks at most, in a fixed order: the header, the mismatch alerts, one
    block per position, and the closing line. The mismatch section comes before
    the positions deliberately -- a position line built on a share count this
    system knows is wrong should not be read before the note saying so."""
    blocks: list[list[str]] = []
    head = [PLAYBOOK_HEAD.format(date=date)]
    if regime_he:
        head.append(PLAYBOOK_REGIME.format(regime=regime_he))
    blocks.append(head)

    mismatch_lines = [mismatch_block(**m) for m in (mismatches or [])]
    if mismatch_lines:
        blocks.append(["\n\n".join(mismatch_lines)])

    position_blocks = [playbook_position_block(**p) for p in (positions or [])]
    blocks.append(["\n\n".join(position_blocks)] if position_blocks
                   else ["אין פוזיציות פתוחות כרגע."])

    blocks.append([PLAYBOOK_CLOSER])
    return f"\n\n{SEP}\n\n".join("\n".join(b) for b in blocks)


def build_position_status(*, ticker: str, action: Optional[str],
                           price=None, entry_price=None, qty=None,
                           stop=None, target=None, atr14=None,
                           sleeve: Optional[str] = None,
                           runner_only: bool = False,
                           tranches: Optional[list] = None,
                           entry_type: Optional[str] = None,
                           trigger=None, add_to_full_qty=None,
                           last_bar_close=None, bar_fresh: bool = False,
                           days_since_starter=None, starter_stale: bool = False,
                           sentence: Optional[str] = None,
                           warnings: Optional[list] = None) -> str:
    """One open position's block for the twice-daily pulse check.

    The Starter line appears for a Starter position and for no other; the
    sentence appears only when the model wrote one, because section ח.2 is
    explicit that it is not filler ("a quiet Hold with nothing changed ends on
    the status line"). Tranche warnings are passed through word for word --
    they are the system telling on itself, and rewording them loses that."""
    lines = [action_line(action, ticker),
             profit_line(price, entry_price, qty),
             status_line(price=price, stop=stop, target=target, atr14=atr14,
                         sleeve=sleeve, runner_only=runner_only)]

    progress = target_progress_line(tranches, sleeve=sleeve)
    if progress:
        lines.append(progress)

    if entry_type == "starter":
        starter = starter_line(trigger=trigger, add_to_full_qty=add_to_full_qty,
                                last_bar_close=last_bar_close, bar_fresh=bar_fresh,
                                days_since_starter=days_since_starter,
                                starter_stale=starter_stale)
        if starter:
            lines.append(starter)

    if sentence:
        lines.append(f"💬 {sentence}")

    for warning in warnings or []:
        lines.append(f"⚠️ {warning}")

    return "\n".join(lines)
