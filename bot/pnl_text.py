"""The /pnl message: the account's money split into Core and trading, in the
plainest words this system has (2026-08-19).

The whole reason this command exists is that one blended broker number is
frightening to read and impossible to act on. So the message is built the
opposite way round from a normal report: the two books first, each complete on
its own, and only then their sum -- with a line saying, out loud, that the sum
is the number the broker shows. Somebody reading only the first block should
already have a true answer.

Three things this message will not do, each one a way the old blended number
misled:

* It never merges banked dollars with paper dollars silently. "Already sold"
  and "still open" are separate lines every time, even when one of them is
  zero.
* It never shows a percent next to realized dollars. There is nothing honest
  to divide them by (see pnl_split._bucket).
* It never hides a position whose price did not arrive. It names it and says
  the total is missing that piece.

Rendering only. Every number here was computed in pnl_split.py against rows
from persistence.get_pnl_positions() -- nothing is re-derived, rounded into a
different number, or judged.
"""

from __future__ import annotations

from typing import Optional

SEP = "━" * 15

# Same emoji the rest of the system uses for the two books, so the split reads
# the same in /pnl as in /open.
CORE_EMOJI = "🏦"
TRADES_EMOJI = "📈"


def _money(usd: Optional[float]) -> str:
    """A dollar figure with its sign always visible. A profit and a loss must
    never be told apart only by a minus sign the eye can skip -- "+$1,204" and
    "-$1,204" differ at the first character."""
    if usd is None:
        return "<b>—</b>"
    # Rounded to whole dollars first, so a -$0.40 shows as "$0" and not as the
    # alarming-looking "-$0": the sign must describe the number the reader can
    # see, never the one that was thrown away by rounding.
    rounded = round(usd)
    if rounded == 0:
        return "<b>$0</b>"
    return f"<b>{'-' if rounded < 0 else '+'}${abs(rounded):,.0f}</b>"


def _plain_money(usd: Optional[float]) -> str:
    """An amount that is not a profit or loss -- money put in, money it is worth
    now. No sign: it is not up or down, it is just a size."""
    if usd is None:
        return "<b>—</b>"
    return f"<b>${usd:,.0f}</b>"


def _pct(fraction: Optional[float]) -> str:
    if fraction is None:
        return ""
    pct = round(fraction * 100, 1)     # same rounding-first rule as _money
    if pct == 0:
        return " (0.0%)"
    return f" ({'-' if pct < 0 else '+'}{abs(pct):.1f}%)"


def _date_he(iso: Optional[str]) -> str:
    if not iso or len(iso) < 10:
        return "—"
    y, m, d = iso[:4], iso[5:7], iso[8:10]
    return f"{d}.{m}.{y}"


def _holding_line(h: dict, emoji: str) -> str:
    if h["price"] is None:
        return f"{emoji} <b>{h['ticker']}</b> — {h['qty']} מניות · אין מחיר עכשיו"
    return (
        f"{emoji} <b>{h['ticker']}</b> — {h['qty']} מניות · "
        f"קניתי ב-{h['entry_price']:,.2f} · עכשיו {h['price']:,.2f} · "
        f"{_money(h['open_usd'])}{_pct(h['open_pct'])}"
    )


def build_pnl_message(split: dict) -> str:
    """The whole /pnl body, from pnl_split.split_pnl()'s output."""
    core, trades, total = split["core"], split["trades"], split["total"]
    lines = ["💰 <b>כמה עשיתי — מסחר לחוד, Core לחוד</b>", ""]

    # --- Core: one buy-and-hold pile. Realized dollars are shown only if there
    # ever were any; selling a piece of Core is rare, and a permanent "$0 sold"
    # line would just be noise on every single run.
    lines.append(f"{CORE_EMOJI} <b>Core — SPY ו-QQQ, קונים ומחזיקים</b>")
    if core["held_cost_usd"]:
        lines.append(f"שמתי פנימה: {_plain_money(core['held_cost_usd'])}")
        lines.append(f"שווה היום: {_plain_money(core['held_value_usd'])}")
        lines.append(f"על הנייר: {_money(core['open_usd'])}{_pct(core['open_pct'])}")
    else:
        lines.append("אין כרגע Core פתוח (או שלא הגיעו מחירים).")
    if abs(core["realized_usd"]) >= 0.5:
        lines.append(f"כבר נמכר ונכנס לכיס: {_money(core['realized_usd'])}")
    lines.append("")

    # --- Trades: two separate money piles, always both shown. "What I already
    # banked" is the number that judges the trading; "what is still open" is the
    # number that moves today.
    lines.append(f"{TRADES_EMOJI} <b>מסחר — הטריידים שלי</b>")
    lines.append(f"עסקאות שנסגרו ({split['closed_trades']}): {_money(trades['realized_usd'])}")
    if trades["held_cost_usd"] or trades["holdings"]:
        lines.append(
            f"עסקאות פתוחות ({split['open_trades']}), על הנייר: "
            f"{_money(trades['open_usd'])}{_pct(trades['open_pct'])}"
        )
    lines.append(f"סה\"כ מהמסחר: {_money(trades['total_usd'])}")
    lines.append("")

    lines.append(SEP)
    lines.append(f"<b>שניהם ביחד: {_money(total['total_usd'])}</b>")
    lines.append("זה בערך המספר האחד שהברוקר מראה לך. עכשיו אתה רואה ממה הוא בנוי.")

    holdings = core["holdings"] + trades["holdings"]
    if holdings:
        lines.append("")
        lines.append("<b>מה פתוח עכשיו</b>")
        for h in core["holdings"]:
            lines.append(_holding_line(h, CORE_EMOJI))
        for h in trades["holdings"]:
            lines.append(_holding_line(h, TRADES_EMOJI))

    if not split["complete"]:
        missing = ", ".join(split["unpriced_tickers"])
        lines.append("")
        lines.append(f"⚠️ לא הגיע מחיר ל: <b>{missing}</b>. הסכומים למעלה בלי אלה.")

    lines.append("")
    lines.append(
        f"הכסף נספר רק ממה שרשום כאן, מ-{_date_he(split['first_entry_date'])} והלאה. "
        "מה שהיה בחשבון לפני זה לא בפנים."
    )
    return "\n".join(lines)
