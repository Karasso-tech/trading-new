"""Event-driven queue processor -- spawned by ack_listener.py immediately after
enqueueing a real message, never on a timer.

2026-07-09 design history (same day, three passes):
1. First version invoked `claude -p` for every message with a broad
   `PowerShell(python *)` allowlist -- blocked by the auto-mode classifier as
   functionally unrestricted execution, correctly held to the user's own original
   ask ("restricted to the project's own known scripts").
2. Second pass: automated ONLY the fully deterministic subset (/list, /pending,
   /drop, /exit, /journal) directly in plain Python, zero LLM cost -- and left
   /screener, /monitor, /playbook, /filled queued for manual handling, since they
   need genuinely fresh judgment per run.
3. Third pass (this version), after the user explicitly asked to automate
   everything and confirmed the cost tradeoff: /screener now runs a scoped
   `claude -p` invocation with a NARROW, specific-script allowlist --
   `bot/fetch_analysis_data.py` (Category A data-gathering, pure arithmetic, no
   judgment) and `bot/deliver_report.py` (rendering/delivery, also no judgment) --
   Claude's own reasoning (setup classification, target selection, grading)
   happens between those two calls with no additional tool permission needed.
   This narrow, two-named-script scope is what actually passed the classifier,
   unlike the earlier wildcard attempt. /monitor, /playbook, /filled are not yet
   wired this way -- still queued for manual handling, tracked as a follow-up.
4. 2026-07-14: each --allowed-tools list below now allowlists the SAME narrow
   per-script commands under BOTH `PowerShell(...)` and `Bash(...)`, not just
   PowerShell. Found real: the inner `claude -p` session doesn't reliably pick
   PowerShell for a `python bot\\....py` invocation -- it sometimes reaches for
   the generic Bash tool instead, which wasn't allowlisted, gets denied
   (non-interactively, so it can't ask for approval), and gives up. A same-day
   retry (_only_denied_via_wrong_tool / _run_claude_with_retry) was added
   first to catch and retry exactly this, but a live /playbook run still hit
   it on BOTH the original attempt and the one retry back to back -- so retrying
   a coin flip isn't a real fix when the coin can land wrong twice. Allowlisting
   both tool names for the same narrow scripts removes the coin flip entirely
   rather than betting on catching it after the fact; the retry logic stays in
   place as a fallback for any other rewording/shape this doesn't cover.

Lock-guarded so at most one drain runs at a time -- tv_data.py's vendored
TradingView connector explicitly cannot handle concurrent CDP connections
("the most likely way to reproduce a hung connection"), and /pending/`/screener`
both need live TradingView fetches. Self-draining: after each pass, re-checks the
queue for anything that arrived mid-run.

2026-07-13: /screener accepts a batch of tickers in one message (comma and/or
whitespace separated, capped at _MAX_BATCH_SCREENER). Implemented as a fan-out,
not a loop: _handle_screener_batch() enqueues one synthetic single-ticker
'/screener TICKER' message per ticker (same pattern trigger_auto_monitor.py uses
for synthetic /monitorall runs) and relies entirely on the self-draining while
loop above to run them one at a time -- no new concurrency, no change to
_handle_screener() itself.

Usage: python bot/process_queue.py
  - If another instance already holds the lock, exits immediately (no-op) -- safe
    and cheap to call every time a message is enqueued.
"""

import asyncio
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chart_draw
import decision_policy
import persistence
import pnl_split
import pnl_text
import setup_types
import sector_map
import tv_lock
from tv_data import TVClient, BOT_WATCHLIST_NAME
from telegram_send import send_text, escape_html


async def _remove_from_watchlist(ticker: str) -> None:
    async with TVClient() as client:
        await client.remove_from_watchlist(ticker)


async def _draw_position_chart(ticker: str, position: Optional[dict]) -> None:
    async with TVClient() as client:
        if position:
            await chart_draw.annotate_position_chart(client, ticker, position)
        else:
            await chart_draw.clear_chart(client, ticker)


def _redraw_position_chart(ticker: str, command: str) -> None:
    """Best-effort chart redraw after a command changed what the user actually
    holds (2026-08-07). Called AFTER the write is committed and the Telegram
    confirmation is already sent, never before: recording the fill/add/exit is
    the job, and a TradingView window that happens to be closed must not cost
    the user their confirmation or leave the DB write in doubt.

    Reads the position back rather than taking it as an argument so the drawn
    lines come from the same source /open and /playbook read -- including the
    tranche plan, which is what knows a target was already sold. A None here
    means the position is fully closed, and clear_chart() wipes the symbol so a
    finished trade leaves no lines behind pretending to be a live plan.

    The failure note is a separate short message, not part of the confirmation:
    by the time this runs the confirmation is sent and its message_id already
    recorded, and re-editing that is not worth the extra failure surface."""
    try:
        position = persistence.get_open_position(ticker)
    except Exception as e:
        _log(f"{command} {ticker}: could not read position back for chart redraw: {e}")
        return
    try:
        asyncio.run(_draw_position_chart(ticker, position))
    except Exception as e:
        _log(f"{command} {ticker}: chart redraw failed: {e}")
        send_text(
            f"📉 <b>{escape_html(ticker)}</b> — הרישום נשמר, אבל הקווים בגרף לא עודכנו "
            f"(TradingView לא זמין). הקווים על המסך הם הישנים — הרץ /monitor {escape_html(ticker)} "
            f"כשהחלון פתוח כדי לצייר מחדש."
        )

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = tv_lock.LOCK_FILE  # re-exported: same path as before, now owned by tv_lock.py
LOG_FILE = PROJECT_ROOT / "process_queue.log"

_PENDING_STATUS_EMOJI = {"yellow_plus": "🟡➕", "yellow": "🟡", "white": "⚪", "green": "🟢", "red": "🔴"}

# Real ticker symbols: letters, optionally one dot + a 1-2 letter share-class suffix
# (BRK.B, BF.A). Found in review: every ticker parsed from Telegram flows straight
# into a claude -p prompt/command that eventually reaches subprocess.Popen(...,
# shell=True) (see _run_claude_screener) -- cmd.exe's handling of &, |, %, ^ inside
# a quoted argument is a well-documented minefield, and the regexes below (\S+)
# accept any non-whitespace token with zero format check. Only the account owner
# can reach this (ack_listener.py's auth check), so this isn't externally
# exploitable today, but a stray character in a real message should get a clean
# rejection here, not an unvalidated trip into a shelled-out command.
_TICKER_RE = re.compile(r"^[A-Z]{1,6}(\.[A-Z]{1,2})?$")


def _reject_invalid_ticker(update_id: int, ticker: str) -> None:
    resp = send_text(
        f"⚠️ '<code>{escape_html(ticker)}</code>' לא נראה כמו טיקר תקין -- "
        f"אותיות באנגלית בלבד, עד 6 תווים, נקודה אחת אופציונלית + עד 2 אותיות סיומת (למשל BRK.B)."
    )
    if not resp.get("ok"):
        _log(f"invalid ticker {ticker!r}: rejection send_text also failed")
    persistence.mark_failed(update_id, f"invalid ticker format: {ticker!r}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{_now()} {msg}\n")


# 2026-08-02: the lock implementation moved to tv_lock.py so the other
# TradingView users in this project (score_shadow.py's nightly run, the nightly
# thesis refresh) share ONE lock instead of racing this one from outside it --
# a second process on the bridge dies with EADDRINUSE, not a graceful wait. The
# lock file path is unchanged, so this interlocks with any instance started
# before the move. These thin wrappers keep the call sites below untouched.
def _is_locked() -> bool:
    return tv_lock.is_locked()


def _acquire_lock() -> bool:
    return tv_lock.acquire()


def _release_lock() -> None:
    tv_lock.release()


# ---------------------------------------------------------------------------
# Deterministic handlers -- one per automatable command. Each returns True if it
# handled (and marked sent/failed) the message, False if it's outside this
# script's scope and should be left queued for manual processing.
# ---------------------------------------------------------------------------

def _handle_journal(update_id: int) -> bool:
    rows = persistence.get_journal_rows()
    stats = persistence.summarize_journal(rows)
    if not rows:
        body = "אין עדיין טריידים סגורים ביומן."
    else:
        lines = [f"{r['ticker']} — {r.get('setup_type') or '?'} — R: {r['total_r_multiple']:.2f}"
                 for r in rows if r.get("total_r_multiple") is not None]
        body = "\n".join(lines)
        if stats["avg_r_multiple"] is not None:
            body += (f"\n\nסה\"כ: {stats['count']} · R ממוצע: {stats['avg_r_multiple']:.2f} · "
                      f"אחוז הצלחה: {stats['win_rate']*100:.0f}%")
    resp = send_text(f"📓 <b>/journal</b>\n\n{body}")
    if resp.get("ok"):
        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        return True
    persistence.mark_failed(update_id, "send_text failed for /journal")
    return True


def _handle_list(update_id: int) -> bool:
    rows = persistence.get_pending_report_rows(current_regime=None)
    if not rows:
        body = "אין טיקרים ב-Pending כרגע"
    else:
        # The decision sign (2026-08-03, user's request) goes FIRST, before the
        # status tier: "which of the four decisions is this" is the thing the
        # user is scanning this list for, and the status emoji next to it is
        # how close the price is -- two different facts, never merged.
        lines = [f"{decision_policy.decision_sign(r.get('decision'))} {r['ticker']} — "
                  f"{_PENDING_STATUS_EMOJI.get(r.get('latest_status') or 'white', '⚪')} "
                  f"(ימי Pending: {r['days_pending']})" for r in rows]
        body = "\n".join(lines)
    resp = send_text(f"📃 <b>/list</b>\n\n{body}")
    if resp.get("ok"):
        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        return True
    persistence.mark_failed(update_id, "send_text failed for /list")
    return True


_ENTRY_TYPE_EMOJI = {"starter": "🌱", "full": "🟢"}


def _handle_open(update_id: int) -> bool:
    rows = persistence.get_open_positions()
    if not rows:
        body = "אין פוזיציות פתוחות כרגע."
    else:
        lines = []
        for r in rows:
            current_stop, initial_stop = r.get("current_stop"), r.get("initial_stop")
            current_text = f"{current_stop:.2f}" if current_stop is not None else "לא מתועד"
            # Shown separately, never merged: current_stop is the live trailing stop
            # (updated by /playbook, see persistence.update_current_stop), initial_stop
            # is the original risk level fixed at entry and is what R-multiple is always
            # computed against (never current_stop -- see record_exit()). Only skip the
            # second line when they're identical (nothing has trailed yet) or the
            # original was never on file (legacy backfill, e.g. GOOGL).
            if initial_stop is None:
                original_text = "\n   (מקורי: לא מתועד -- פוזיציית legacy)"
            elif current_stop is not None and abs(initial_stop - current_stop) < 0.005:
                original_text = ""
            else:
                original_text = f"\n   (מקורי: {initial_stop:.2f})"
            # remaining_qty (2026-07-16) is the real, live share count after any
            # recorded partial exits -- qty is the original fill size, fixed forever
            # (see persistence._remaining_qty's own docstring for the real XLF
            # incident this fixes: showing the stale original qty here made a
            # correctly-already-recorded partial exit look like an unrecorded
            # discrepancy two days later). Only show the "מתוך" (out of) qualifier
            # once anything has actually been partially exited.
            remaining_qty = r.get("remaining_qty", r["qty"])
            qty_text = f"{remaining_qty}" if remaining_qty == r["qty"] else f"{remaining_qty} (מתוך {r['qty']})"
            # Rule-7 tranche state shown on every open position (2026-08-07),
            # not only right after an /exit -- "which piece is next" is exactly
            # the thing that was invisible when ASTS sold its single target
            # twice. See _build_tranche_block.
            tranche_block = _build_tranche_block(r)
            lines.append(
                f"{_ENTRY_TYPE_EMOJI.get(r['entry_type'], '⚪')} <b>{escape_html(r['ticker'])}</b> "
                f"({r.get('sleeve') or 'unknown'})\n"
                f"   כניסה: {r['entry_price']} · כמות: {qty_text} · סטופ נוכחי: {current_text} · "
                f"ימי החזקה: {r['days_held']}{original_text}"
                + (f"\n{tranche_block}" if tranche_block else "")
            )
        body = "\n\n".join(lines)
        # Portfolio-level disclosure (2026-07-18, rule 19) -- informational only,
        # never gates anything, shown every time there's at least one open
        # position so drift is always visible without a separate command.
        drift = persistence.get_allocation_drift()
        heat = persistence.get_portfolio_heat()
        if drift["core_pct_actual"] is None:
            body += "\n\n⚠️ לא הוגדר שווי חשבון (/equity) — אי אפשר לחשב חשיפת תיק או איזון."
        else:
            body += (
                f"\n\n📐 <b>איזון תיק</b>\n"
                f"Core: {drift['core_pct_actual']*100:.0f}% (יעד {drift['core_pct_target']*100:.0f}%) · "
                f"Swing: {drift['swing_pct_actual']*100:.0f}% (יעד {drift['swing_pct_target']*100:.0f}%)"
            )
            if drift["spy_within_core_pct_actual"] is not None:
                body += (
                    f"\nבתוך Core — SPY: {drift['spy_within_core_pct_actual']*100:.0f}% "
                    f"(יעד {drift['spy_within_core_pct_target']*100:.0f}%) · "
                    f"QQQ: {drift['qqq_within_core_pct_actual']*100:.0f}% "
                    f"(יעד {drift['qqq_within_core_pct_target']*100:.0f}%)"
                )
            heat_text = f"{heat['heat_pct']*100:.1f}%" if heat["heat_pct"] is not None else "לא זמין"
            body += f"\n🔥 חשיפת תיק כוללת: {heat_text} (תקרה {heat['cap_pct']*100:.0f}%)"
    resp = send_text(f"📂 <b>/open</b> ({len(rows)} פוזיציות)\n\n{body}")
    if resp.get("ok"):
        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        return True
    persistence.mark_failed(update_id, "send_text failed for /open")
    return True


# One live quote per still-held ticker, so a bounded per-ticker allowance plus a
# fixed floor for the connector's own startup -- same shape and the same reason
# as _MAXADD_FETCH_TIMEOUT_SEC, just over a list instead of one symbol.
_PNL_FETCH_TIMEOUT_SEC_BASE = 60
_PNL_FETCH_TIMEOUT_SEC_PER_TICKER = 40


def _fetch_pnl_prices(tickers: list[str]) -> tuple[dict, dict]:
    """Live prices for /pnl, or ({}, reason) if they could not be fetched.

    Deliberately never raises and never aborts the command: without prices,
    /pnl still has a completely true answer to give (every closed trade's
    banked dollars), and pnl_text names what is missing. Refusing to answer at
    all because TradingView is shut would be a worse failure than answering
    partly -- the closed-trade half of this report needs no live data.
    """
    timeout = _PNL_FETCH_TIMEOUT_SEC_BASE + _PNL_FETCH_TIMEOUT_SEC_PER_TICKER * len(tickers)
    try:
        fetch = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "bot" / "fetch_pnl_prices.py"), *tickers],
            cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        _log(f"/pnl: fetch_pnl_prices.py timed out after {timeout}s for {tickers}")
        return {}, {"_": "timeout"}
    if fetch.returncode != 0:
        _log(f"/pnl: fetch_pnl_prices.py failed rc={fetch.returncode}: {fetch.stdout}{fetch.stderr}")
        return {}, {"_": f"rc={fetch.returncode}"}
    try:
        payload = json.loads(fetch.stdout)
    except json.JSONDecodeError:
        _log(f"/pnl: unparseable price output: {fetch.stdout!r}")
        return {}, {"_": "bad json"}
    return payload.get("prices") or {}, payload.get("errors") or {}


def _handle_pnl(update_id: int) -> bool:
    """"How much did I make from trading, and how much from Core" -- the two
    numbers the broker's single blended figure hides inside each other
    (2026-08-19, user's request: the merged number is what makes a fine
    trading week look frightening, and a bad one look fine).

    Read-only, and advisory in the same sense /positions is: it changes no
    stop, no position, no thesis. The split itself is not judgment either --
    persistence.get_sleeve() answers Core-vs-Swing unconditionally, so this
    command can never disagree with /open about which book a ticker is in.
    """
    rows = persistence.get_pnl_positions()
    if not rows:
        resp = send_text("💰 <b>/pnl</b>\n\nאין עדיין שום פוזיציה רשומה, אז אין מה לחשב.")
        if resp.get("ok"):
            persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
            return True
        persistence.mark_failed(update_id, "send_text failed for /pnl")
        return True

    held = sorted({r["ticker"] for r in rows if (r.get("remaining_qty") or 0) > 0})
    prices, errors = _fetch_pnl_prices(held) if held else ({}, {})
    if errors:
        _log(f"/pnl: {len(errors)} ticker(s) without a usable price: {errors}")

    body = pnl_text.build_pnl_message(pnl_split.split_pnl(rows, prices))
    resp = send_text(body)
    if resp.get("ok"):
        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        return True
    persistence.mark_failed(update_id, "send_text failed for /pnl")
    return True


def _format_pending_setup(label: str, setup: Optional[dict]) -> str:
    """One setup (primary or alternate) rendered as a readable card -- straight
    from the stored thesis JSON, no recomputation. See CLAUDE_CODE_INSTRUCTIONS.md's
    Category A/B split: this is display formatting only, never re-derives a
    number indicators_core.py or the original /screener judgment already produced."""
    if not setup:
        return ""
    lines = [f"<b>{label}:</b> {escape_html(str(setup.get('type') or '?'))}"]
    if setup.get("trigger"):
        lines.append(f"   טריגר: {escape_html(str(setup['trigger']))}")
    if setup.get("stop") is not None:
        lines.append(f"   סטופ: {setup['stop']}")
    for t in setup.get("targets") or []:
        price = t.get("price")
        price_text = escape_html(str(price)) if price is not None else "—"
        # pct is frequently a plain number (40), not a pre-formatted string ("40%") --
        # found real, 2026-07-14: " · ".join() below requires every item to already be
        # a str, so a numeric pct crashed the whole /pending command with
        # "sequence item 0: expected str instance, int found".
        pct_raw = t.get("pct")
        pct_text = f"{pct_raw}%" if isinstance(pct_raw, (int, float)) else (str(pct_raw) if pct_raw else None)
        extra = " · ".join(x for x in (pct_text, f"R:R {t['rr']}" if t.get("rr") else None) if x)
        lines.append(f"   יעד: {price_text}" + (f" ({extra})" if extra else ""))
    return "\n".join(lines)


def _handle_pending(update_id: int) -> bool:
    """Text-only, DB-only 'memory card' per pending ticker (2026-07-11 rebuild) --
    deliberately no live TradingView fetch and no widget PNG anymore (dropped the
    former SPY/QQQ regime check and pending_aggregate_to_widget_data render): the
    user wants to see what they're actually waiting to trigger on, pulled straight
    from the last /screener's stored primary_setup/alternate_setup, not a
    recalculation. /list stays the quick one-line-per-ticker status view; this is
    the detailed one. Age flagging (get_pending_report_rows' own days_pending-based
    check) is untouched -- that's a cheap DB-only comparison, not a live recompute."""
    rows = persistence.get_pending_report_rows(current_regime=None)
    if not rows:
        resp = send_text("📋 <b>/pending</b> (0 טיקרים)\n\nאין טיקרים ב-Pending כרגע.")
        if resp.get("ok"):
            persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
            return True
        persistence.mark_failed(update_id, "send_text failed for /pending")
        return True

    cards = []
    for r in rows:
        header = f"🎯 <b>{escape_html(r['ticker'])}</b>"
        if r.get("rubric_grade"):
            header += f" · Grade {escape_html(str(r['rubric_grade']))}"
        header += f" · ימי Pending: {r['days_pending']}"
        # 2026-08-03 (user's request): which of the four decisions this thesis
        # is actually on. The card showed trigger/stop/targets and the waiting
        # reason, and never said whether an order was written and waiting for a
        # price (Buy Only If Confirmed) or whether the idea is only being
        # watched (Watchlist) -- those read identically otherwise. Straight from
        # the stored column, no re-derivation; not escaped because
        # decision_line() is a fixed string we own, with no ticker data in it.
        card_lines = [header, decision_policy.decision_line(
            r.get("decision"), r.get("latest_status"))]
        # 2026-08-08 (user's request): if this idea's trigger has already
        # confirmed, say what it was CALLED when that happened. Without it the
        # card shows only today's word, so an idea the system said to watch
        # rather than buy -- which then fired anyway -- leaves no trace at all
        # once a rebuild overwrites the decision column.
        for moved in persistence.get_decision_transitions(r["ticker"])[:1]:
            was = decision_policy.decision_words(moved.get("decision_stored"))
            card_lines.append(
                f"📌 ביום {escape_html(str(moved['occurred_at'])[:10])} הטריגר הופעל, "
                f"והרעיון היה מוגדר אז: {escape_html(was)}")
        primary_text = _format_pending_setup("Primary", r.get("primary_setup"))
        alt_text = _format_pending_setup("Alternate", r.get("alternate_setup"))
        if primary_text:
            card_lines.append(primary_text)
        if alt_text:
            card_lines.append(alt_text)
        # Rule 29 (2026-08-03): one plain-words line saying why this is not an
        # order yet. Before this, the card showed the levels and nothing about
        # WHY it was still waiting -- and the raw stored reasons are free text
        # ("trigger_not_fired" / "no_live_trigger" / "trigger_pending" all
        # appeared for the identical situation in one week), so they could not
        # be shown to a human as-is. decision_policy translates them into a
        # fixed short vocabulary, so the same situation always reads the same.
        for sentence in decision_policy.explain_reasons(r.get("rejection_reasons")):
            card_lines.append(f"⏳ {escape_html(sentence)}")
        if r.get("flag_reasons"):
            card_lines.append("⚠️ " + ", ".join(r["flag_reasons"]))
        cards.append("\n".join(card_lines))

    # Telegram's sendMessage hard-caps text at 4096 chars -- found real, 2026-07-14:
    # 12 pending tickers produced a 5757-char message and the whole command failed
    # outright with zero visibility ("Bad Request: message is too long"). Split
    # across multiple messages on CARD boundaries (never mid-card) -- never a fixed
    # ticker-count threshold, since card length varies a lot with how many
    # targets/setups a given thesis has.
    _TELEGRAM_TEXT_LIMIT = 3500  # margin under the real 4096 cap for the header/HTML entities
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for card in cards:
        card_len = len(card) + 2  # +2 for the "\n\n" separator between cards
        if current and current_len + card_len > _TELEGRAM_TEXT_LIMIT:
            chunks.append(current)
            current, current_len = [], 0
        current.append(card)
        current_len += card_len
    if current:
        chunks.append(current)

    message_ids = []
    for i, chunk in enumerate(chunks, start=1):
        header = f"📋 <b>/pending</b> ({len(rows)} טיקרים)"
        if len(chunks) > 1:
            header += f" — חלק {i}/{len(chunks)}"
        body = "\n\n".join(chunk)
        resp = send_text(f"{header}\n\n{body}")
        if not resp.get("ok"):
            persistence.mark_failed(update_id, f"send_text failed for /pending (part {i}/{len(chunks)})")
            return True
        message_ids.append(resp.get("result", {}).get("message_id"))

    persistence.mark_sent(update_id, message_ids)
    return True


_DROP_RE = re.compile(r"^/drop\s+(\S+)\s*(.*)$", re.IGNORECASE)


def _handle_drop(update_id: int, text: str) -> bool:
    m = _DROP_RE.match(text.strip())
    if not m:
        return False
    ticker, reason = m.group(1).upper(), (m.group(2).strip() or "(לא צוין נימוק)")
    if not _TICKER_RE.match(ticker):
        _reject_invalid_ticker(update_id, ticker)
        return True
    persistence.drop_thesis(ticker, reason)

    # 2026-07-11: mirror the drop onto the TradingView "Bot Watchlist" -- the ticker
    # already left /pending in the DB above regardless of whether this succeeds; UI
    # automation against TradingView's DOM failing (CDP down, markup drift) must never
    # block the actual /drop from completing.
    try:
        asyncio.run(_remove_from_watchlist(ticker))
    except Exception as e:
        _log(f"/drop {ticker}: failed to remove from '{BOT_WATCHLIST_NAME}': {e}")

    resp = send_text(f"✅ <b>{escape_html(ticker)}</b> הוסר מ-/pending (נשאר בהיסטוריה, לא נמחק).")
    if resp.get("ok"):
        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        return True
    persistence.mark_failed(update_id, "send_text failed for /drop")
    return True


_EXIT_RE = re.compile(r"^/exit\s+(\S+)\s+([\d.]+)\s+(\d+)\s*$", re.IGNORECASE)

# Plain-words names for what indicators_core.build_tranche_plan calls each
# tranche. The user reads these, so they say what the piece IS, not its code.
_TRANCHE_NAME_HE = {"target_1": "יעד 1", "target_2": "יעד 2", "target_3": "יעד 3",
                     "runner": "Runner (החלק שרץ)"}
_TRANCHE_STATUS_HE = {"filled": "✅ מומש", "partial": "🟡 מומש חלקית", "waiting": "⏳ ממתין"}
_EXIT_REASON_HE = {
    "stop": "סטופ", "target_1": "יעד 1", "target_2": "יעד 2", "target_3": "יעד 3",
    "runner_trim": "מכירה מה-Runner (כל היעדים המתוכננים כבר מומשו)",
    "unmatched": "לא תואם רמה מתוכננת (יציאה שיקולית)",
}


def _tranche_name(label: str) -> str:
    return _TRANCHE_NAME_HE.get(label, label)


def _build_tranche_block(pos: Optional[dict]) -> str:
    """The rule-7 tranche table for one open position, built in code from the
    position's own stored targets and its real exits rows -- never written by
    the model, never re-derived from a live price. Added 2026-08-07 after the
    ASTS incident (position 25 sold twice against a single stored target, both
    recorded as 'target_1', quietly halving the Runner): the system had the
    facts to catch it and simply never showed them anywhere the user looks."""
    plan = (pos or {}).get("tranche_plan") or {}
    tranches = plan.get("tranches") or []
    if not tranches:
        return ""
    lines = ["📦 <b>מצב הטרנצ'ים (כלל 7)</b>"]
    for t in tranches:
        if not t.get("planned_qty") and not t.get("filled_qty"):
            continue
        price_text = f" @ {t['price']:.2f}" if t.get("price") is not None else " (ללא יעד מספרי — סטופ נגרר)"
        done = f"{t.get('filled_qty', 0)}/{t.get('planned_qty', 0)}"
        lines.append(
            f"   {_TRANCHE_STATUS_HE.get(t.get('status'), t.get('status'))} "
            f"{_tranche_name(t.get('label', ''))}{price_text} · {done} מניות "
            f"({t.get('planned_pct', 0):.0f}%)"
        )
    if plan.get("next_label") == "runner":
        lines.append(
            f"   ➡️ הבא: אין עוד יעד מספרי. נשארו {plan.get('runner_qty_left', 0)} מניות Runner — "
            "יוצאות רק על הסטופ הנגרר, לא על יעד שכבר מומש."
        )
    elif plan.get("next_label"):
        nxt_price = plan.get("next_price")
        price_text = f" @ {nxt_price:.2f}" if isinstance(nxt_price, (int, float)) else ""
        lines.append(
            f"   ➡️ הבא: {_tranche_name(plan['next_label'])}{price_text} · "
            f"{plan.get('next_qty', 0)} מניות"
        )
    else:
        lines.append("   ➡️ הבא: כל התוכנית מומשה.")
    for w in plan.get("warnings") or []:
        lines.append(f"   ⚠️ {escape_html(w)}")
    return "\n".join(lines)


def _handle_exit(update_id: int, text: str) -> bool:
    m = _EXIT_RE.match(text.strip())
    if not m:
        return False
    ticker, price, qty = m.group(1).upper(), float(m.group(2)), int(m.group(3))
    if not _TICKER_RE.match(ticker):
        _reject_invalid_ticker(update_id, ticker)
        return True
    today = datetime.now().date().isoformat()
    try:
        exit_id = persistence.record_exit(ticker, exit_price=price, exit_qty=qty,
                                           exit_date=today, source="exit_command")
    except ValueError as e:
        resp = send_text(f"⚠️ <b>/exit {escape_html(ticker)}</b> נכשל: {escape_html(str(e))}")
        persistence.mark_failed(update_id, str(e))
        return True
    with persistence._db() as conn:
        row = dict(conn.execute("SELECT * FROM exits WHERE id=?", (exit_id,)).fetchone())
    r_text = f"{row['r_multiple']:.2f}" if row["r_multiple"] is not None else "לא זמין (ללא סטופ מקורי מתועד)"
    reason_text = _EXIT_REASON_HE.get(row["exit_reason"], row["exit_reason"])
    # The tranche table is read AFTER the write, so it already includes this
    # exit -- the confirmation says what is left, not what was left a moment
    # ago. get_open_position returns None once this exit closed the position.
    tranche_block = _build_tranche_block(persistence.get_open_position(ticker))
    resp = send_text(
        f"🚪 <b>{escape_html(ticker)} — יציאה נרשמה</b>\n\n"
        f"מחיר יציאה: {price} · כמות: {qty}\n"
        f"סיבה: {escape_html(reason_text)}\n"
        f"R-multiple: {r_text}"
        + (f"\n\n{tranche_block}" if tranche_block else "")
    )
    if resp.get("ok"):
        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        # A sold tranche must lose its line (rule 30's whole point), and a full
        # exit must leave the chart clean -- _redraw_position_chart handles both
        # off the same read the tranche block above already used.
        _redraw_position_chart(ticker, "/exit")
        return True
    persistence.mark_failed(update_id, "send_text failed for /exit")
    return True


# The 5th word (2026-08-30) says WHICH setup filled. Optional in the pattern so
# the old 4-word form still parses; _handle_filled refuses to continue without it
# whenever the thesis actually has two stops to choose between.
_FILLED_RE = re.compile(
    r"^/filled\s+(\S+)\s+([\d.]+)\s+(\d+)(?:\s+(starter|full))?(?:\s+(primary|alternate))?\s*$",
    re.IGNORECASE)


def _build_override_line(thesis: dict) -> str:
    """Disclosure-only line for /filled's confirmation, 2026-08-02: says out loud
    when this fill goes against what the system itself already concluded about
    the ticker. The tag is written to the position row by
    persistence.create_position (see classify_override there for why it's
    automatic); this only surfaces it in the reply so the user sees it happen
    rather than discovering it months later. Never blocks the fill -- same
    disclose-never-gate posture as _build_fill_risk_line above."""
    override = persistence.classify_override(thesis.get("decision"), thesis.get("rubric_grade"))
    if not override:
        return ""
    said = []
    if override.get("decision"):
        said.append(f'"{override["decision"]}"')
    if override.get("grade"):
        said.append(f'ציון {override["grade"]}')
    return (
        "📌 לידיעה: המערכת עצמה אמרה על הטיקר הזה " + " · ".join(said) +
        " — הכניסה נרשמה במלואה, רק סומנה בנפרד כדי שנוכל להשוות בהמשך.\n"
    )


def _build_fill_risk_line(price: float, qty: int, stop: Optional[float]) -> str:
    """Disclosure-only line for /filled's confirmation -- same math as
    persistence.get_portfolio_heat(), just against a single fresh fill instead
    of the whole book. Added 2026-07-27 after a real CRDO fill was sized at
    2.2x the account's own 1% risk cap and sat unnoticed for a week: nothing
    between /screener's risk_usd disclosure and the real qty typed into
    /filled ever re-checked the two against each other. Never blocks the
    entry (rule 19's own "disclose, never gate" posture) -- states plainly
    when it can't be computed (no stop, or equity_usd unset) rather than
    guessing, same posture as fetch_analysis_data.py's risk_usd."""
    if stop is None:
        return "⚠️ סיכון: לא ניתן לחשב — אין סטופ ב-Primary setup\n"
    risk_per_share = price - stop
    if risk_per_share <= 0:
        return ""
    equity = persistence.get_effective_equity()
    if not equity:
        return "⚠️ סיכון: לא ניתן לחשב אחוז — הון לא מוגדר (/equity)\n"
    risk_usd = risk_per_share * qty
    risk_pct = risk_usd / equity
    cap_pct = persistence.get_account_settings()["risk_pct"]
    line = f"סיכון: ${risk_usd:,.0f} ({risk_pct * 100:.2f}% מההון)"
    if risk_pct > cap_pct:
        max_qty = int(cap_pct * equity / risk_per_share)
        line += f" — ⚠️ מעל ה-{cap_pct * 100:.0f}% שהוגדר (עד {max_qty} מניות בסטופ הזה)"
    return line + "\n"


def _handle_filled(update_id: int, text: str) -> bool:
    """2026-07-09 redesign: the original spec's multi-turn 'ask starter or full,
    wait for a separate reply' flow needed a real conversational state machine
    (the deleted awaiting_reply mechanism) that's inherently harder to make
    reliable -- disambiguating which reply answers which open question across
    concurrent tickers, handling a reply that never comes, etc. Given the
    priority is 100%-reliable automation, /filled instead takes entry_type as an
    explicit 4th argument -- no waiting, no state machine, fully deterministic."""
    m = _FILLED_RE.match(text.strip())
    if not m:
        return False
    ticker, price, qty, entry_type = m.group(1).upper(), float(m.group(2)), int(m.group(3)), m.group(4)
    which_setup = (m.group(5) or "").lower()
    if not _TICKER_RE.match(ticker):
        _reject_invalid_ticker(update_id, ticker)
        return True

    if not entry_type:
        resp = send_text(
            f"⚠️ נא לציין starter או full:\n<code>/filled {escape_html(ticker)} {price} {qty} starter</code>\n"
            f"או\n<code>/filled {escape_html(ticker)} {price} {qty} full</code>"
        )
        persistence.mark_failed(update_id, "entry_type not specified in command")
        return True
    entry_type = entry_type.lower()

    thesis = persistence.get_thesis(ticker)
    if not thesis or not thesis.get("primary_setup"):
        resp = send_text(f"⚠️ אין תזה שמורה עבור <b>{escape_html(ticker)}</b> — לא ניתן לרשום כניסה בלי תזה (הרץ /screener קודם).")
        persistence.mark_failed(update_id, "no stored thesis with primary_setup")
        return True

    today = datetime.now().date().isoformat()
    dup = persistence.find_possible_duplicate_fill(ticker, qty, today)
    if dup:
        resp = send_text(
            f"⚠️ <b>{escape_html(ticker)}</b> נראה כמו כפילות אפשרית — פוזיציה פתוחה קיימת בכמות דומה "
            f"({dup['qty']} מניות, {dup['entry_date'][:10]}). אם זו כניסה נוספת אמיתית, שלח שוב עם כמות "
            f"שונה משמעותית או פנה ידנית."
        )
        persistence.mark_failed(update_id, f"possible duplicate fill (existing position id={dup['id']})")
        return True

    # Which setup actually triggered. This used to be primary_setup, always, with
    # a "verify this is right" note in the confirmation -- so a fill that really
    # came from the Alternate silently stored the Primary's stop, and from that
    # moment every report, every trail check and the whole tranche plan were
    # measured against a stop the broker had never seen.
    #
    # Trigger levels are sometimes descriptive text rather than a clean number,
    # so matching the fill price against the two setups automatically is not
    # reliable -- which is precisely why this asks instead of guessing, the same
    # posture as the starter/full argument above. With only one setup on file
    # there is nothing to ask about and Primary is used with no prompt, exactly
    # as before.
    alternate = thesis.get("alternate_setup") or {}
    has_real_alternate = bool(alternate.get("stop"))
    if has_real_alternate and not which_setup:
        resp = send_text(
            f"⚠️ ל-<b>{escape_html(ticker)}</b> יש שני סטאפים עם סטופים שונים. "
            f"צריך לומר באיזה נכנסת, כדי שיישמר הסטופ הנכון:\n"
            f"<code>/filled {escape_html(ticker)} {price} {qty} {entry_type or 'full'} primary</code> "
            f"(סטופ {thesis['primary_setup'].get('stop')})\n"
            f"<code>/filled {escape_html(ticker)} {price} {qty} {entry_type or 'full'} alternate</code> "
            f"(סטופ {alternate.get('stop')})"
        )
        persistence.mark_failed(update_id, "two setups on file and no primary/alternate given")
        return True
    if which_setup == "alternate":
        if not has_real_alternate:
            resp = send_text(
                f"⚠️ אין Alternate עם סטופ שמור עבור "
                f"<b>{escape_html(ticker)}</b> — לא נרשם כלום."
            )
            persistence.mark_failed(update_id, "/filled alternate but no alternate_setup with a stop")
            return True
        entry_setup = alternate
        setup_label = "Alternate"
    else:
        entry_setup = thesis["primary_setup"]
        setup_label = "Primary"
    position_id = persistence.create_position(
        ticker, entry_date=today, entry_price=price, qty=qty, entry_type=entry_type,
        entry_setup=entry_setup, initial_stop=entry_setup.get("stop"),
    )
    risk_line = _build_fill_risk_line(price, qty, entry_setup.get("stop"))
    resp = send_text(
        f"✅ <b>{escape_html(ticker)} — כניסה נרשמה</b>\n\n"
        f"סוג: {entry_type} · מחיר: {price} · כמות: {qty}\n"
        f"סטופ: {entry_setup.get('stop')} (מה-{setup_label} setup)\n"
        f"{risk_line}"
        f"{_build_override_line(thesis)}"
        f"position_id: {position_id}"
    )
    if resp.get("ok"):
        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        # The fill is the moment the chart's lines stop being a plan and start
        # being a position -- the pre-entry trigger is now history and the
        # Alternate is dead. Redraw them as such (see chart_draw's docstring).
        _redraw_position_chart(ticker, "/filled")
        return True
    persistence.mark_failed(update_id, "send_text failed for /filled")
    return True


_ADD_RE = re.compile(r"^/add\s+(\S+)\s+([\d.]+)\s+(\d+)\s*$", re.IGNORECASE)


def _handle_add(update_id: int, text: str) -> bool:
    """Records a real top-up fill against an already-open position (starter ->
    full), via persistence.add_to_position() -- see that function's own
    docstring for why this blends into the existing positions row instead of
    creating a second one. Distinct command from /filled (a brand-new
    position) rather than an overload of it, same reasoning /exit already
    gets its own command separate from /filled."""
    m = _ADD_RE.match(text.strip())
    if not m:
        return False
    ticker, price, qty = m.group(1).upper(), float(m.group(2)), int(m.group(3))
    if not _TICKER_RE.match(ticker):
        _reject_invalid_ticker(update_id, ticker)
        return True
    try:
        position_id = persistence.add_to_position(ticker, additional_qty=qty, additional_price=price)
    except ValueError as e:
        resp = send_text(f"⚠️ <b>/add {escape_html(ticker)}</b> נכשל: {escape_html(str(e))}")
        persistence.mark_failed(update_id, str(e))
        return True
    pos = persistence.get_open_position(ticker)
    risk_line = _build_fill_risk_line(pos["entry_price"], pos["qty"], pos["current_stop"])
    resp = send_text(
        f"➕ <b>{escape_html(ticker)} — הוספה נרשמה</b>\n\n"
        f"נוסף: {qty} @ {price}\n"
        f"כמות כוללת: {pos['qty']} · מחיר ממוצע: {pos['entry_price']:.2f}\n"
        f"סוג: {pos['entry_type']}\n"
        f"{risk_line}"
        f"position_id: {position_id}"
    )
    if resp.get("ok"):
        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        # add_to_position() blends the top-up into the same row, so the average
        # entry price moved -- the drawn Entry line is now wrong until redrawn.
        _redraw_position_chart(ticker, "/add")
        return True
    persistence.mark_failed(update_id, "send_text failed for /add")
    return True


_MAXADD_RE = re.compile(r"^/maxadd\s+(\S+)\s*$", re.IGNORECASE)
_MAXADD_FETCH_TIMEOUT_SEC = 90  # TradingView/Chrome/CDP round trip, same order of
                                # magnitude as the other live-fetch scripts this
                                # dispatcher already shells out to (/playbook's
                                # download step, etc.) -- not instant, but bounded.


def _rr_gate(atr_mult: float, rr: float) -> bool:
    """CONSISTENCY_RULES.md rule 3's target-validity gate, verbatim: >=1.5x
    ATR14 AND R:R>=2:1, except the 1.0x-1.5x ATR band which needs R:R>=2.5:1
    instead. A target inside 1.0x-1.5x isn't disqualified by distance alone --
    the stricter R:R bar is what CONSISTENCY_RULES.md substitutes for it."""
    if atr_mult >= 1.5:
        return rr >= 2.0
    if atr_mult >= 1.0:
        return rr >= 2.5
    return False


def _max_add_by_sector_cap(group: str, sector_exposure: dict, sector_cap_pct: float,
                            new_share_risk: float) -> Optional[int]:
    """Largest N such that (group_risk_usd + N*new_share_risk) / (swing_total_usd
    + N*new_share_risk) <= sector_cap_pct -- adding shares grows both the
    group's own risk and the swing book's total by the same amount, since
    they're the same position. Solved directly rather than searched:
        N*r*(1-cap) <= cap*swing_total - group_risk
    Returns None (not 0) when there's no swing book to measure against yet
    (get_sector_exposure() returned {}) -- an unknown denominator is not the
    same as 'already at the cap'."""
    if not sector_exposure:
        return None
    swing_total_usd = sum(v["risk_usd"] for v in sector_exposure.values())
    group_risk_usd = sector_exposure.get(group, {}).get("risk_usd", 0.0)
    room = sector_cap_pct * swing_total_usd - group_risk_usd
    if room <= 0:
        return 0
    denom = new_share_risk * (1 - sector_cap_pct)
    return max(int(room / denom), 0) if denom > 0 else 0


def _build_maxadd_body(ticker: str, pos: dict, equity: float, trade_cap_pct: float,
                        current_price: float, atr14: float, fresh: bool,
                        heat: dict, sector_exposure: dict, sector_group: str,
                        sector_cap_pct: float) -> str:
    """Pure arithmetic against already-fetched live/DB data -- kept separate
    from _handle_maxadd so tests never need to mock a subprocess/TVClient
    fetch, same split fetch_analysis_data.py's own callers use. Answers three
    distinct questions in one message: (1) rule 4/rule 19's cap-sizing 'how
    many more shares fit under the 1% per-trade risk cap', (1b)/(1c) the same
    sizing question against the two portfolio-wide caps (6% total heat, 40%
    per-sector) that a single-position check alone would miss entirely --
    found real: nothing before this checked whether topping up one position
    could itself push total portfolio heat or one sector over its own cap even
    while that position's own 1% was still fine -- and (2) rule 3's 'does the
    thesis's own stored target still clear the ATR-distance/R:R gates at
    today's price and ATR' -- the second is what a stale live_price argument
    could never answer, since ATR itself drifts and rule 3 is checked against
    CURRENT ATR, not atr_at_build. All three caps are disclosure, same posture
    as /screener's own portfolio_heat_after/sector_pct_after fields (rule
    19/20 family) -- the reported max is the binding (smallest) one, never a
    block."""
    stop = pos["current_stop"]
    lines = [f"🧮 <b>{escape_html(ticker)} — כמה אפשר להוסיף</b>\n"]
    if not fresh:
        lines.append("⚠️ נתוני מחיר/ATR לא טריים (סוף שבוע/חג?) — הערכים הבאים עלולים להיות לא מעודכנים.\n")

    if stop is None:
        lines.append("⚠️ סיכון: לא ניתן לחשב — אין סטופ שמור עבור הפוזיציה.")
        return "\n".join(lines)
    new_share_risk = current_price - stop
    if new_share_risk <= 0:
        lines.append(f"⚠️ מחיר חי {current_price:.2f} כבר מתחת לסטופ {stop:.2f} — אין תוספת אפשרית.")
        return "\n".join(lines)

    trade_cap_usd = trade_cap_pct * equity
    existing_risk_usd = (pos["entry_price"] - stop) * pos["qty"]
    trade_room_usd = trade_cap_usd - existing_risk_usd
    max_by_trade = max(int(trade_room_usd / new_share_risk), 0) if trade_room_usd > 0 else 0

    heat_cap_usd = heat["cap_pct"] * equity
    heat_room_usd = heat_cap_usd - heat["heat_usd"]
    max_by_heat = max(int(heat_room_usd / new_share_risk), 0) if heat_room_usd > 0 else 0

    max_by_sector = _max_add_by_sector_cap(sector_group, sector_exposure, sector_cap_pct, new_share_risk)

    caps = [
        (f"סיכון בודד ({trade_cap_pct * 100:.0f}%)", max_by_trade),
        (f"חשיפת תיק ({heat['cap_pct'] * 100:.0f}%)", max_by_heat),
    ]
    if max_by_sector is not None:
        # sector_group is also the dict key sector_exposure/_max_add_by_sector_cap
        # match on ("unclassified", from persistence.get_sector_exposure()'s own
        # fallback) -- translated here only for display, never for the lookup.
        display_group = "לא מסווג" if sector_group == "unclassified" else sector_group
        caps.append((f"סקטור {escape_html(display_group)} ({sector_cap_pct * 100:.0f}%)", max_by_sector))
    binding_label, max_add_qty = min(caps, key=lambda c: c[1])

    lines.append(
        f"מחיר חי: {current_price:.2f} (סטופ: {stop:.2f}, ATR14: {atr14:.2f})\n"
        f"פוזיציה נוכחית: {pos['qty']} מניות @ {pos['entry_price']:.2f}"
    )
    if existing_risk_usd < 0:
        # stop has trailed above entry -- this position no longer risks money,
        # it locks in profit, so "$-N risk" would read as a loss that isn't real.
        lines.append(f"💡 הסטופ כבר מעל מחיר הכניסה — אין סיכון בפוזיציה הקיימת, יש רווח נעול (${-existing_risk_usd:,.0f}).")

    lines.append(f"\n<b>ניתן להוסיף עד {max_add_qty} מניות</b>")
    lines.append(f"מגבלה קובעת: {binding_label}\n")
    lines.append("תקרות:")
    for label, qty in caps:
        arrow = " ⬅" if label == binding_label else ""
        lines.append(f"  • {label}: עד {qty} מניות{arrow}")
    if max_by_sector is None:
        lines.append("  • סקטור: אין עדיין חשיפת swing לחישוב אחוז.")

    if max_add_qty == 0:
        lines.append("\n⚠️ אין מקום להוספה — המגבלה הקובעת כבר מלאה.")
    else:
        lines.append(f"\nלאחר הוספה מלאה: {pos['qty'] + max_add_qty} מניות סה\"כ")

    # Rule 3 relevance check -- is the position's own stored target still a real
    # target at today's price/ATR, or has price run far enough that adding here
    # no longer clears the same gate the original entry had to clear.
    entry_setup = pos.get("entry_setup") or {}
    targets = entry_setup.get("targets", [])
    # "status" here is rule 3's own pass/fail validity tag (set once at
    # SCREENER_v3 build time) -- a "fail" entry is a Checkpoint, not a
    # sellable target (rule 3's own wording), so it's excluded the same way
    # the original build never treated it as one. Nothing in this codebase
    # marks a target "hit" after a fill -- that state lives in the exits
    # table, matched via indicators_core.derive_exit_reason -- so "still
    # ahead of current price" is the only reachability filter available here.
    open_targets = [t for t in targets if t.get("status") == "pass" and t.get("price", 0) > current_price]
    if not open_targets:
        lines.append("\nℹ️ אין יעד פתוח שמור לבדיקת R:R.")
    else:
        nearest = min(open_targets, key=lambda t: t["price"])
        distance = nearest["price"] - current_price
        atr_mult = distance / atr14 if atr14 else 0
        rr = distance / new_share_risk
        qualifies = _rr_gate(atr_mult, rr)
        verdict = "✅ היעד עדיין תקף" if qualifies else "⚠️ היעד כבר לא תקף — קרוב מדי או R:R נמוך מדי"
        lines.append(f"\nיעד קרוב: {nearest['price']:.2f} ({atr_mult:.2f}x ATR, R:R {rr:.2f}:1)\n{verdict}")
    return "\n".join(lines)


def _handle_maxadd(update_id: int, text: str) -> bool:
    """Pre-trade check for 'how many shares can I add right now, and does the
    stored target still make sense, before I breach the 1% risk cap' -- asked
    BEFORE placing the order, unlike /add's and /filled's post-fill
    disclosure. Standalone: fetches its own live price/ATR14 via
    fetch_maxadd_data.py (Category A, same split as fetch_analysis_data.py /
    fetch_monitor_data.py) instead of taking a typed price argument, so the
    R:R check below is against real current ATR, not the thesis's stale
    atr_at_build. Reads only; writes nothing."""
    m = _MAXADD_RE.match(text.strip())
    if not m:
        return False
    ticker = m.group(1).upper()
    if not _TICKER_RE.match(ticker):
        _reject_invalid_ticker(update_id, ticker)
        return True

    pos = persistence.get_open_position(ticker)
    if not pos:
        resp = send_text(f"⚠️ אין פוזיציה פתוחה עבור <b>{escape_html(ticker)}</b>.")
        persistence.mark_failed(update_id, "no open position")
        return True

    equity = persistence.get_effective_equity()
    if not equity:
        resp = send_text("⚠️ סיכון: לא ניתן לחשב אחוז — הון לא מוגדר (/equity)")
        persistence.mark_failed(update_id, "equity_usd unset")
        return True

    try:
        fetch = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "bot" / "fetch_maxadd_data.py"), ticker],
            cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_MAXADD_FETCH_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        resp = send_text(f"⚠️ שליפת נתונים חיים ל-<b>{escape_html(ticker)}</b> נתקעה (timeout).")
        persistence.mark_failed(update_id, "fetch_maxadd_data.py timed out")
        return True
    if fetch.returncode != 0:
        _log(f"/maxadd {ticker}: fetch_maxadd_data.py failed rc={fetch.returncode}: {fetch.stdout}{fetch.stderr}")
        resp = send_text(f"⚠️ שליפת נתונים חיים ל-<b>{escape_html(ticker)}</b> נכשלה.")
        persistence.mark_failed(update_id, f"fetch_maxadd_data.py rc={fetch.returncode}")
        return True
    try:
        live = json.loads(fetch.stdout)
    except json.JSONDecodeError:
        _log(f"/maxadd {ticker}: unparseable output: {fetch.stdout!r}")
        resp = send_text(f"⚠️ נתונים חיים ל-<b>{escape_html(ticker)}</b> לא תקינים.")
        persistence.mark_failed(update_id, "fetch_maxadd_data.py returned invalid JSON")
        return True

    settings = persistence.get_account_settings()
    body = _build_maxadd_body(
        ticker, pos, equity, settings["risk_pct"],
        current_price=live["current_price"], atr14=live["atr14"], fresh=live["freshness"]["fresh"],
        heat=persistence.get_portfolio_heat(), sector_exposure=persistence.get_sector_exposure(),
        sector_group=sector_map.get_sector_group(ticker) or "unclassified",
        sector_cap_pct=settings["sector_cap_pct"],
    )
    resp = send_text(body)
    if resp.get("ok"):
        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        return True
    persistence.mark_failed(update_id, "send_text failed for /maxadd")
    return True


_EQUITY_RE = re.compile(r"^/equity\s+\$?([\d,]+(?:\.\d+)?)\s*$", re.IGNORECASE)


def _handle_equity(update_id: int, text: str) -> bool:
    """Updates the current account value used for every %-based calc (risk
    sizing, portfolio heat, allocation drift) -- no live broker feed exists in
    this system, same manual-input pattern as everything else, just for
    account value instead of a chart. Found in review: DEFAULT_RISK_USD was
    never set and /setrisk was never wired, so real position sizing has never
    actually been risk-based in practice -- this and /setrisk close that gap."""
    m = _EQUITY_RE.match(text.strip())
    if not m:
        return False
    equity_usd = float(m.group(1).replace(",", ""))
    try:
        persistence.set_equity(equity_usd)
    except ValueError as e:
        resp = send_text(f"⚠️ <b>/equity</b> נכשל: {escape_html(str(e))}")
        persistence.mark_failed(update_id, str(e))
        return True
    resp = send_text(f"✅ שווי חשבון עודכן: ${equity_usd:,.2f}")
    if resp.get("ok"):
        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        return True
    persistence.mark_failed(update_id, "send_text failed for /equity")
    return True


_WITHDRAW_RE = re.compile(r"^/withdraw\s+\$?([\d,]+(?:\.\d+)?)\s*$", re.IGNORECASE)


def _handle_withdraw(update_id: int, text: str) -> bool:
    """Marks money as already-gone for risk purposes even though the broker's
    own total hasn't dropped yet -- real incident (2026-07-18): a real
    $17,500 withdrawal was decided but not yet settled. `/withdraw 0` clears
    it once the broker's own total has genuinely dropped (continuing to
    subtract after that would double-count, since equity_usd itself would
    already reflect the withdrawal)."""
    m = _WITHDRAW_RE.match(text.strip())
    if not m:
        return False
    usd = float(m.group(1).replace(",", ""))
    try:
        persistence.set_pending_withdrawal(usd)
    except ValueError as e:
        resp = send_text(f"⚠️ <b>/withdraw</b> נכשל: {escape_html(str(e))}")
        persistence.mark_failed(update_id, str(e))
        return True
    if usd == 0:
        resp = send_text("✅ משיכה ממתינה אופסה -- שווי החשבון משמש כמות שהוא, ללא ניכוי.")
    else:
        resp = send_text(
            f"✅ נרשמה משיכה ממתינה: ${usd:,.2f}\n"
            f"ינוכה משווי החשבון בכל חישוב סיכון עד שהיא באמת תתבצע בפועל בחשבון -- "
            f"ואז שלח <code>/withdraw 0</code> כדי לאפס (המשך ניכוי אחרי שזה כבר קרה יכפיל את הניכוי)."
        )
    if resp.get("ok"):
        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        return True
    persistence.mark_failed(update_id, "send_text failed for /withdraw")
    return True


# /override TICKER heat <reason>  -- see persistence.add_risk_override for why
# this is deliberately as narrow as it is. The reason is the rest of the line,
# free text, and it is required.
_OVERRIDE_RE = re.compile(r"^/override\s+(\S+)\s+(heat)\s+(.+?)\s*$", re.IGNORECASE)


def _handle_override(update_id: int, text: str) -> bool:
    """Permission to place ONE order past ONE risk cap, for a short while.

    Portfolio heat became a real block on 2026-08-30 -- the first of rules
    19-21's figures to stop an order rather than describe one -- and a block
    with no way past it is a block that gets worked around outside the system,
    where nothing records that it happened.

    So the way past is here, and it is narrow on purpose: one ticker, one named
    cap, a written reason, a short life, spent the moment it is used. A broad
    override is the same as no cap at all.

    What it cannot do is wave through a number that could not be computed. That
    refusal lives in deliver_report._heat_block and has no path here: an
    override is agreement to a known risk, and there is nothing to agree to
    when the risk is unknown."""
    m = _OVERRIDE_RE.match(text.strip())
    if not m:
        if text.strip().lower().startswith("/override"):
            resp = send_text(
                "⚠️ השימוש: <code>/override TICKER heat הסיבה שלך</code>\n\n"
                "צריך שם מניה, את המילה heat, וסיבה כתובה. הסיבה נשמרת ולא "
                "נשפטת — מה שחשוב הוא שזו הייתה פעולה מכוונת."
            )
            if resp.get("ok"):
                persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
            else:
                persistence.mark_failed(update_id, "send_text failed for /override usage")
            return True
        return False

    ticker, breach, reason = m.group(1).upper(), m.group(2).lower(), m.group(3)
    if not _TICKER_RE.match(ticker):
        _reject_invalid_ticker(update_id, ticker)
        return True
    try:
        persistence.add_risk_override(ticker, breach, reason)
    except ValueError as e:
        resp = send_text(f"⚠️ <b>/override</b> נכשל: {escape_html(str(e))}")
        persistence.mark_failed(update_id, str(e))
        return True

    resp = send_text(
        f"✅ <b>{escape_html(ticker)} — עקיפת תקרת חום נרשמה</b>\n\n"
        f"הסיבה שרשמת: {escape_html(reason)}\n\n"
        f"תקף למניה הזאת בלבד, לתקרה הזאת בלבד, לפקודה אחת, "
        f"ול-{persistence.OVERRIDE_HOURS} שעות. אחרי זה התקרה חוזרת לעבוד לבד."
    )
    if resp.get("ok"):
        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        return True
    persistence.mark_failed(update_id, "send_text failed for /override")
    return True


_SETRISK_RE = re.compile(r"^/setrisk\s+([\d.]+)\s*%?\s*$", re.IGNORECASE)

# The highest per-trade risk this command will store, as a PERCENTAGE number
# (1.0 means 1%), which is the account's current setting.
#
# `set_risk_pct` only ever rejected a number outside 0-100, so "/setrisk 20" --
# one stray digit -- was a valid instruction to risk a fifth of the account on
# every trade. Nothing downstream would have questioned it: rule 28's floor and
# ceiling are both expressed as a fraction OF this number, so they scale up with
# it silently.
#
# A ceiling rather than a confirmation prompt, deliberately: what the right
# per-trade risk should be is exactly the open question the risk-level
# measurement exists to answer, and until it has an answer there is nothing for
# a confirmation step to be confirming. Lowering is always allowed. This number
# moves when the measurement says to move it, together with rule 28.
RISK_PCT_CEILING = 1.0


def _handle_setrisk(update_id: int, text: str) -> bool:
    """Sets the %-of-equity risked per new trade -- replaces the never-used
    fixed-dollar DEFAULT_RISK_USD. Accepts either "/setrisk 1" or "/setrisk 1%"
    (both mean 1%) -- always a percentage number in the command, never a raw
    fraction, so a user typing "1" doesn't accidentally set 100% risk."""
    m = _SETRISK_RE.match(text.strip())
    if not m:
        return False
    risk_pct_number = float(m.group(1))  # e.g. 1.0 for "1%"
    if risk_pct_number > RISK_PCT_CEILING:
        resp = send_text(
            f"🚫 <b>/setrisk {risk_pct_number:g}%</b> לא בוצע.\n\n"
            f"התקרה כרגע היא <b>{RISK_PCT_CEILING:g}%</b> מכל הכסף בחשבון, וזו גם הרמה "
            f"שהמערכת עובדת בה היום. אפשר להוריד מתחתיה, אי אפשר לעלות מעליה.\n\n"
            f"הסיבה: עוד לא נמדד מה קורה ברמות סיכון אחרות. עד שהמדידה תיתן תשובה, "
            f"אין דרך לאשר מספר גבוה יותר — גם לא בכוונה. כך טעות הקלדה של ספרה אחת "
            f"לא יכולה להפוך כל טרייד לגדול פי עשרים."
        )
        if resp.get("ok"):
            persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        else:
            persistence.mark_failed(update_id, "send_text failed for /setrisk ceiling refusal")
        return True
    try:
        persistence.set_risk_pct(risk_pct_number / 100)
    except ValueError as e:
        resp = send_text(f"⚠️ <b>/setrisk</b> נכשל: {escape_html(str(e))}")
        persistence.mark_failed(update_id, str(e))
        return True
    resp = send_text(f"✅ סיכון לכל טרייד עודכן: {risk_pct_number:g}% מההון")
    if resp.get("ok"):
        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        return True
    persistence.mark_failed(update_id, "send_text failed for /setrisk")
    return True


_SCREENER_RE = re.compile(r"^/screener\s+(\S+)\s*$", re.IGNORECASE)
_SCREENER_ARGS_RE = re.compile(r"^/screener\s+(.+)$", re.IGNORECASE)
_MAX_BATCH_SCREENER = 10  # guard rail: each ticker is a full ~25min claude -p run, sequential

# Best-effort heuristic for the $20/month Agent SDK quota running out.
# Found real, 2026-07-13: a genuine exhaustion's actual message was "You've hit
# your session limit -- resets 8:10pm (Asia/Jerusalem)" -- "session limit", not
# "usage limit", so the keyword list alone missed it and every affected run fell
# through to the generic (and here actively misleading) "did not reach a
# terminal state" / Chrome-hint failure message instead of the correct quota
# alert. QUOTA_KEYWORDS stays as a fallback for outputs that aren't valid JSON,
# but _is_quota_exhausted() below checks the structured api_error_status == 429
# first -- Anthropic's own HTTP status code for this, not free-text phrasing
# that can change wording at any time.
QUOTA_KEYWORDS = (
    "usage limit", "session limit", "quota", "credit balance", "rate limit",
    "insufficient credit", "usage cap", "exceeded your",
)


# The exact `result` string the Claude CLI returns when its own login has
# expired. Matched case-insensitively as a substring so a wording change around
# it doesn't silently break the check.
_LOGGED_OUT_KEYWORDS = ("not logged in", "please run /login", "invalid api key",
                         "authentication_error")


def _is_logged_out(combined: str) -> bool:
    """True when the run failed because the Claude CLI itself is signed out.

    Found real, 2026-08-02: three /monitor BTCUSD runs failed at 03:38, 04:41
    and 10:03 and were recorded as the generic "monitor automation did not reach
    a terminal state (exit=1)". The actual payload said, in full:

        "result": "Not logged in - Please run /login"

    Each one died in 250-1500ms having done nothing. The fourth attempt, at
    10:26, ran normally for 243 seconds -- because by then someone had signed
    in again.

    Why this deserves its own check rather than the generic failure path: it
    fails EVERY judgment command equally -- /screener, /monitor, the unattended
    overnight /monitorall scans, /playbook, the nightly rebuild -- instantly and
    identically, and the generic message gives no hint that one thirty-second
    fix on the PC would restore all of them. The user's own first guess when
    shown these failures was "it happened because I was logged out, no?" -- he
    was right, and the system had the exact answer in its log the whole time and
    never said it.

    Same shape as _is_quota_exhausted above: a specific, recognizable, fully
    recoverable condition gets a specific alert naming the fix."""
    try:
        data = json.loads(combined)
        result_text = str(data.get("result", ""))
        if any(k in result_text.lower() for k in _LOGGED_OUT_KEYWORDS):
            return True
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass
    return any(k in combined.lower() for k in _LOGGED_OUT_KEYWORDS)


_logged_out_alert_sent = False


def _alert_logged_out(label: str, update_id: int) -> None:
    """One clear Telegram message naming the exact fix, then mark the message
    failed WITHOUT the generic notify (the alert already said it better).

    Sent at most ONCE per process. A signed-out CLI fails every queued command
    identically and instantly, so the nightly rebuild -- which can queue twenty
    or more screener runs that this one process drains in a single pass -- would
    otherwise fire twenty identical alerts in about a minute. Twenty copies of
    the same message is how a real alert gets muted. Every message is still
    individually marked failed; only the notification is deduplicated."""
    global _logged_out_alert_sent
    _log(f"{label}: CLAUDE CLI IS LOGGED OUT -- alerting, not retrying")
    if _logged_out_alert_sent:
        _log(f"{label}: logged-out alert already sent this run -- marking failed silently")
        _mark_failed_unless_already_sent(update_id, "claude CLI not logged in", notify=False)
        return
    _logged_out_alert_sent = True
    try:
        send_text(
            "🔑 <b>המערכת לא מחוברת לחשבון Claude</b>\n\n"
            f"הפקודה <b>{escape_html(label)}</b> לא רצה בכלל — היא נכשלה מיד, בלי לנתח כלום.\n\n"
            "<b>מה לעשות:</b> פתח חלון פקודה במחשב והרץ <code>claude /login</code>, "
            "ואז שלח שוב את הפקודה.\n\n"
            "עד שזה יסודר — כל הפקודות שדורשות ניתוח (סריקה, מעקב, תיק) ייכשלו באותו אופן, "
            "כולל הסריקות האוטומטיות של הלילה."
        )
    except Exception:
        _log(f"{label}: failed to send logged-out alert to Telegram")
    _mark_failed_unless_already_sent(update_id, "claude CLI not logged in", notify=False)


def _is_quota_exhausted(combined: str) -> bool:
    try:
        data = json.loads(combined)
        if data.get("api_error_status") == 429:
            return True
    except (json.JSONDecodeError, ValueError):
        pass
    return any(k in combined.lower() for k in QUOTA_KEYWORDS)

_SCREENER_PROMPT_TEMPLATE = (
    # 2026-08-09. This prompt was ~2,200 words, and most of it was telling a
    # model to copy a number exactly and not change it. Copying numbers exactly
    # is what code is for -- and a number that is never retyped can never be
    # retyped wrong, which had happened for real more than once (an ATR-distance
    # computed against a fresher ATR than the one frozen at build time; a stop
    # set at the candle low with no cushion; a target that was the middle of a
    # wall rather than its top).
    #
    # bot/build_plan.py now does the whole mechanical half in one command:
    # setup type, trigger, stop, targets, allocation, movement potential,
    # rubric grade, rejection reasons, and the decision ceiling. What is left
    # here is the part worth having a model for -- reading the chart, saying
    # whether the mechanical call is right, and writing the report.
    "NOTE: if any tool output shows garbled characters where Hebrew or other non-ASCII text would be "
    "expected, that is a known, purely cosmetic Windows console-codepage display artifact -- the "
    "underlying data is correct. Proceed directly using the data as given. "
    "This session's --allowed-tools does NOT include Edit. To correct a file you already wrote, use "
    "Write to overwrite it with the full corrected content. Never stop to ask a human for permission -- "
    "this is an unattended run with no one present to grant it, and stopping ends the run as a failure. "

    "STEP 1. Run `python bot\\build_plan.py {ticker}`. It fetches the real market data once and returns "
    "{{\"plan\": ..., \"data\": ...}}. `data` is fetch_analysis_data.py's full output -- every Category A "
    "metric (ATR14, SMA20/50/150, relative strength, volume, earnings, economic calendar, account/heat/"
    "sector/cash figures, resistance-wall chains, recent bars). `plan` is the mechanical reading of it: "
    "setup type, trigger, stop and its stop_basis_level, targets with their allocation, checkpoints, "
    "movement potential, the four rubric fields (rubric_grade, rubric_score, rubric_criteria, "
    "rubric_inputs), market_regime, rejection_reasons and max_allowed_decision, plus "
    "rule_26_disclosure (state it verbatim when present -- it is information only and never changes the "
    "decision or the size). Do NOT look up past lessons or past trades on this ticker: rule 25's "
    "past-lesson injection was switched off on 2026-08-10 so that what the shadow book records is the "
    "rules being applied, not the rules plus a memory of one earlier trade. "
    "Both are authoritative. There is nothing else to look up, and no other command is permitted except "
    "the two named below -- anything else is silently denied and wastes your turns. "

    "STEP 2. Read SCREENER_v3.md and CONSISTENCY_RULES.md fresh from disk, then look at what the plan "
    "says against the real bars in `data`. Your job is the judgment the code cannot do: "
    "(a) is the computed setup type actually the right reading of this chart; (b) what is the story, in "
    "one plain sentence; (c) the Alternate setup -- the second plausible near-term direction "
    "(rule 5: two directions, never two depths of the same one). Choosing that scenario is yours; its "
    "NUMBERS are not -- once you have its entry and stop, re-run with `--alt-trigger <entry> "
    "--alt-stop <stop> --alt-type <one of the six>` and it comes back with its own full target scan. "
    "Rule 7 is explicit that the Alternate is not exempt from the target table just because it is "
    "second or still pending, and the same level often fails from a high entry and passes from a "
    "deeper one; "
    "(d) the full markdown report, sections a through f, matching the depth of this session's existing "
    "reports/*.md files. "

    "If the computed setup type is wrong, re-run with `--setup-type <one of Breakout|Retest|Pullback|"
    "Reclaim|Failed Breakdown|Gap-and-Hold> --reason \"<why>\"`. A reason is required and stays visible in "
    "the output -- same rule as a market-state override (rule 23). Never override because the call merely "
    "looks surprising; surprising is not the same as wrong. Do NOT hand-edit the plan's numbers: if a "
    "level looks wrong, say so in the report rather than quietly writing a different one. "

    "STEP 3. The decision line. `plan.max_allowed_decision` is the STRONGEST decision the facts permit "
    "(rule 29). You may always choose a weaker one -- a real reason to wait is judgment and stays yours -- "
    "and never a stronger one. Use exactly one of these four words: Buy Now, Buy Only If Confirmed, "
    "Watchlist, No Trade. Not \"Buy\", not your own wording. "

    "STEP 4. Sizing (rule 28). There are NO size multipliers -- volatility, breakout volume and market "
    "regime were all removed on 2026-08-03; show them as information columns, never multiply by them. "
    "Every trade goes in at one full risk unit: `python bot\\size_policy.py --risk-usd-target "
    "<data.account.risk_usd> --entry <plan trigger> --stop <plan stop>`. Do not pass --multiplier. If it "
    "returns qty 0, say so plainly and do not round up to one share. Only if the full quantity genuinely "
    "cannot be taken -- most commonly data.account.cash_available_usd will not cover qty * entry -- size "
    "down to what the cash allows and put a short token in sizing.size_reduction_reason, e.g. "
    "\"cash_limited\". An order under 90% of full size with no reason recorded is a lint failure. "

    "STEP 5. Disclosures, all of which are information only and NEVER change the decision or the size "
    "(rules 19-22): portfolio heat after this trade vs its cap, sector concentration vs its cap, cash "
    "required vs cash available, and the breakout-day volume figure. Copy the account figures verbatim "
    "from `data.account`, compute this trade's own after-values, and set portfolio_heat_disclosed / "
    "sector_disclosed / cash_usage_disclosed to true when you actually wrote each warning -- report_lint "
    "checks these mechanically. The ONLY thing that blocks a buy is the regime gate (rule 18), and the "
    "plan has already applied it to max_allowed_decision. "

    "STEP 6. Write the decision JSON to d:\\Trading New\\_runs\\_decision_{ticker}.json -- every per-run "
    "payload belongs in _runs\\, never the project root -- matching the shape in bot/deliver_report.py's "
    "module docstring, with update_id={update_id} and date={date}. Copy the plan's primary_setup "
    "(including stop_basis_level and atr_at_build), market_regime, market_regime_formula "
    "and rejection_reasons VERBATIM. Copy ALL FOUR rubric fields too -- `rubric_grade`, "
    "`rubric_score`, `rubric_criteria`, `rubric_inputs` -- never retyped, never just the letter. "
    "Delivery recomputes the grade from `rubric_inputs` and REFUSES the order when it cannot, or "
    "when the letter disagrees with the numbers, or when the two grade fields disagree (rule 27): "
    "the decision drops to Watchlist and the sizing goes from the message, the report and the PDF. Copy data.freshness verbatim. Add your own alternate_setup, "
    "potential, report_markdown and summary_text. "

    "`trigger` takes a NUMBER whenever a level has been identified, even when no order is ready. "
    "Measured 2026-08-31: 25 of 28 pending Alternates hid a real price inside a sentence in that "
    "field, and every one was invisible to the shadow book, the rubric and the stale-trigger check. "
    "Put the number in `trigger` and the \"no order ready yet\" wording in the note beside it. "
    "Only when no level exists yet may `trigger` be text, and then it opens with one of these four, "
    "so the case can be counted: \"אזור ירידה עמוק יותר — טרם נוצרה רמה\" · "
    "\"ממתין לנר אישור — הרמה תיקבע אחריו\" · \"שפל קפיטולציה חדש — טרם נוצר\" · "
    "\"ריטסט של הרמה — השפל טרם נוצר\". Never invent a price to satisfy this (rule 1). "

    "Share counts go in report_markdown's sizing section and nowhere else in it: write the "
    "quantity once there, and say \"the full position\" elsewhere. When a decision is refused "
    "delivery deletes that section and then checks no share count survived, so a repeated "
    "quantity either escapes the deletion or costs the report its PDF. "

    "The summary_text is BUILT FOR YOU -- do not write it by hand and do not reproduce section h's "
    "template from memory. bot/summary_text.py holds the three fixed templates; deliver_report.py calls "
    "it with the plan's own numbers. Supply only `thesis_sentence` (one plain sentence, beginner "
    "wording, no ATR/rubric/rule names), and for a Watchlist also `wait_for` and `invalidation`, as "
    "top-level fields in the JSON. Anything you put in summary_text itself is ignored. "

    "STEP 7. Run `python bot\\deliver_report.py <path-to-that-json>` to render, deliver and mark sent. "
    "Do not skip any mandatory section of SCREENER_v3.md. Do not invent a price level that is not "
    "traceable to the fetched data."
)

# The six setup names and four decision words appear literally in the prompt
# above. They are the vocabulary save_thesis enforces, so a prompt that drifts
# from it asks the model for a word that will be refused at write time -- losing
# a completed twenty-minute analysis over a typo. Checked at import, once.
for _name in setup_types.SETUP_TYPES:
    assert _name in _SCREENER_PROMPT_TEMPLATE, (
        f"the screener prompt no longer names the setup type {_name!r} -- it must "
        f"list exactly the vocabulary setup_types enforces"
    )
for _word in decision_policy.ALL_DECISIONS:
    assert _word in _SCREENER_PROMPT_TEMPLATE, (
        f"the screener prompt no longer names the decision {_word!r}"
    )
del _name, _word



def _message_status(update_id: int) -> str | None:
    with persistence._db() as conn:
        row = conn.execute("SELECT status FROM messages WHERE update_id=?", (update_id,)).fetchone()
    return row["status"] if row else None


# The Chrome/TradingView hint used to be appended to EVERY failure notification
# unconditionally -- found real, repeatedly, 2026-07-13/14: a /pending crash (a
# pure Python TypeError, no TradingView involved at all) and a quota-exhaustion
# 429 both showed the exact same "make sure Chrome is open" hint, actively
# misleading the user into checking the wrong thing each time. Only show it when
# the error text itself plausibly mentions TradingView/Chrome/CDP.
_CHROME_RELATED_MARKERS = ("tradingview", "chrome", "cdp", "9222", "chart_", "getchartapi", "chart api")


def _retry_hint_he(error: str) -> str:
    if any(m in error.lower() for m in _CHROME_RELATED_MARKERS):
        return "נסה שוב. אם זה חוזר, ודא שכרום פתוח על TradingView במצב דיבאג (--remote-debugging-port=9222)."
    return "נסה שוב. אם זה חוזר, בדוק את process_queue.log לפרטים."


def _mark_failed_unless_already_sent(update_id: int, error: str, notify: bool = True) -> None:
    """A timeout (or any other error) on our end doesn't mean the underlying work
    didn't actually finish -- found real, 2026-07-09: a timed-out claude -p call's
    orphaned child process (see _run_claude_screener's own docstring for why that
    can happen on Windows) went on to complete delivery and call mark_sent AFTER
    this process gave up. Blindly calling mark_failed afterward silently clobbered
    a real success back to 'failed'. Always re-check current status immediately
    before writing 'failed', never assume our own timeout means nothing happened.

    notify=True (default) also sends a plain Telegram failure notice -- found
    real, 2026-07-12: a /screener run failed (TradingView wasn't open in a
    CDP-enabled Chrome window, and the claude -p session tried a disallowed tool instead of
    reporting that cleanly) and the message was marked 'failed' in the DB with
    ZERO Telegram reply -- from the user's side, indistinguishable from the bot
    being completely unresponsive. Pass notify=False only when the caller
    already sent a more specific message about this exact failure (the
    quota-exhaustion alert), to avoid sending two messages for one failure."""
    if _message_status(update_id) == "sent":
        _log(f"update_id={update_id}: already 'sent' (likely finished after our own timeout) -- NOT overwriting to failed")
        return
    persistence.mark_failed(update_id, error)
    if notify:
        try:
            send_text(f"⚠️ הפעולה נכשלה: <code>{escape_html(error)}</code>\n{_retry_hint_he(error)}")
        except Exception:
            _log(f"update_id={update_id}: failed to send failure notification to Telegram")


def _run_claude_screener(cmd: list[str], timeout: int, prompt: str) -> tuple[int, str]:
    """Runs the scoped claude -p invocation with a REAL process-tree kill on
    timeout. subprocess.run(..., shell=True, timeout=...) was tried first and
    found broken on Windows: shell=True spawns cmd.exe as an intermediary, and
    subprocess's own timeout-kill only terminates that wrapper -- the actual
    claude.exe/node.exe grandchildren keep running orphaned in the background,
    which is exactly what caused the mark_sent-after-mark_failed race this
    function exists to prevent. Popen + CREATE_NEW_PROCESS_GROUP + `taskkill /F
    /T` on timeout kills the whole tree for real.

    `prompt` is fed via stdin (claude -p with no positional prompt argument
    reads it from stdin, --input-format text is the default) rather than
    appended to `cmd` -- found real, 2026-07-20: a batch /screener ARM/CRDO/
    SNDK/BE/NIBS run failed all 5 tickers instantly with cmd.exe's own
    "The command line is too long." (its ~8191-char line limit, not Windows'
    much higher 32767-char CreateProcess limit -- shell=True is required here
    specifically because `claude` is an npm .cmd shim that only cmd.exe knows
    how to launch). _SCREENER_PROMPT_TEMPLATE alone had grown to ~8860 chars
    after several rounds of added rules (portfolio heat/sector/cash
    disclosures, rubric, rule 23's regime formula) stacked on top of the
    --allowed-tools list in the same command line. Stdin has no such limit,
    and every prompt this project sends is comfortably in-memory size, so this
    removes the whole failure class instead of just trimming back under
    today's specific threshold."""
    proc = subprocess.Popen(
        cmd, cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", shell=True,
        stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        output, _ = proc.communicate(input=prompt, timeout=timeout)
        return proc.returncode, output or ""
    except subprocess.TimeoutExpired:
        _log(f"claude -p exceeded {timeout}s -- killing full process tree (PID {proc.pid})")
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        try:
            output, _ = proc.communicate(timeout=10)
        except Exception:
            output = ""
        return -1, output or "(timed out, process tree killed)"


def _only_denied_via_wrong_tool(combined: str, permitted_substrings: list[str]) -> bool:
    """True only for the specific, narrow bailout shape found real 2026-07-13 in
    both a /playbook and a /screener run: every permission denial that targeted
    one of our own permitted script commands used the Bash tool (PowerShell, the
    tool actually allowlisted -- see each --allowed-tools list above -- was never
    attempted even once), AND the model's own final message explicitly frames
    this as a permission/approval problem. That second check is load-bearing,
    not redundant: only-Bash denials for a permitted script ALSO show up in
    plenty of genuinely successful runs (denied via Bash once, then correctly
    retried via PowerShell and finished within the same session) and in
    unrelated real failures (e.g. old logged runs where TradingView Desktop
    itself wasn't running) -- neither of those talks about approval/permission
    anywhere in its final text, only this exact wrong-tool-and-gave-up pattern
    does.

    The phrase list was originally the exact wordings first seen ("requires
    approval", "approve this tool execution"), and missed a real recurrence
    2026-07-14: same denial shape (3x Bash-only denials on download_photo.py,
    message never reached a terminal state), but the model's own wording that
    time was "I need your approval... could you approve it" -- containing
    neither literal substring, so the retry never fired and the user got a
    raw "did not reach a terminal state" failure instead of the automatic
    recovery this function exists to provide. Loosened to word-stem/keyword
    matching ("approv" catches approval/approve/approving) so future rewordings
    of the same bailout aren't missed the same way. Re-verified against this
    project's own historical process_queue.log after the change: still flags
    exactly the real bailouts (now 3, including the 07-14 miss) and none of the
    successes or unrelated failures captured there."""
    try:
        data = json.loads(combined)
    except (json.JSONDecodeError, ValueError):
        return False
    denials = data.get("permission_denials") or []
    relevant = [
        d for d in denials
        if any(s in (d.get("tool_input", {}).get("command") or "") for s in permitted_substrings)
    ]
    if not relevant or not all(d.get("tool_name") == "Bash" for d in relevant):
        return False
    result_text = (data.get("result") or "").lower()
    bailout_phrases = ("approv", "grant", "permission", "blocked")
    return any(phrase in result_text for phrase in bailout_phrases)


def _run_claude_with_retry(cmd: list[str], timeout: int, label: str, permitted_substrings: list[str],
                            update_id: int, prompt: str) -> tuple[int, str]:
    """Wraps _run_claude_screener with exactly one automatic retry for the
    wrong-tool bailout _only_denied_via_wrong_tool() detects -- a fresh invocation
    reliably picks the right tool most of the time (see that function's own
    docstring), so this turns what used to be a silent failure requiring the user
    to notice and manually resend into an automatic, transparent recovery within
    the same run. Capped at one retry so a persistently confused model can't loop
    forever or double the already-real cost/time on every call.

    Only-Bash denials for a permitted script also show up in plenty of genuinely
    SUCCESSFUL runs (the agent gets denied via Bash mid-session, then correctly
    switches to PowerShell and finishes within that same invocation -- found real,
    2026-07-13: several already-delivered runs in this project's own log have this
    exact denial shape). Retrying those would just burn an extra ~$0.30-1.50 for
    nothing, so this only fires when the message ALSO never reached a terminal
    status -- i.e. the first attempt genuinely didn't finish, not merely "tried
    Bash once along the way"."""
    returncode, combined = _run_claude_screener(cmd, timeout=timeout, prompt=prompt)
    if _message_status(update_id) not in ("sent", "failed") and _only_denied_via_wrong_tool(combined, permitted_substrings):
        _log(f"{label}: gave up after Bash-only tool denials, PowerShell never attempted -- retrying once")
        returncode, combined = _run_claude_screener(cmd, timeout=timeout, prompt=prompt)
        return returncode, combined

    # Second retry trigger (2026-08-09): the analysis finished, and one field in
    # the saved JSON was the wrong shape, so persistence refused the write and
    # the whole run was binned. Found real, 2026-08-06: a /screener PANW run
    # died on "alternate_setup.trigger must be numeric once stop is set" after
    # roughly twenty minutes of real work -- the refusal was correct (bad data
    # must not reach the DB) and throwing the run away over it was not.
    #
    # The retry is worth it precisely because the error message already says
    # exactly what is wrong and what the field should hold; it is handed back
    # verbatim so the second attempt is told, not left to guess. Capped at one,
    # same reasoning as the branch above.
    field_error = _fixable_field_error(update_id)
    if field_error:
        _log(f"{label}: field-shape rejection, retrying once with the correction -- {field_error}")
        persistence.reset_failed_for_retry(update_id)
        returncode, combined = _run_claude_screener(
            cmd, timeout=timeout, prompt=prompt + _CORRECTION_NOTE.format(error=field_error),
        )
        if _message_status(update_id) not in ("sent", "failed"):
            # The retry did not reach a terminal state either. Put the original
            # failure back rather than leaving the message stuck in 'processing'
            # forever -- and keep the ORIGINAL error, which is the one that
            # explains what actually went wrong.
            persistence.mark_failed(update_id, field_error)
    return returncode, combined


# Field-shape refusals raised by persistence's own validators. Every one of them
# is phrased "<setup>.<field> must be ..." and carries the offending value, so
# the message is already a complete instruction for a second attempt.
_FIXABLE_FIELD_ERROR_RE = re.compile(
    r"(primary_setup|alternate_setup)[.\[][^\s]* must be", re.IGNORECASE
)

_CORRECTION_NOTE = (
    "\n\nIMPORTANT -- this is a RETRY. Your previous attempt completed the analysis correctly but the "
    "save was REJECTED because one field in the JSON was the wrong shape, and the whole run was lost. "
    "The exact refusal was:\n\n    {error}\n\n"
    "Redo the run and fix ONLY that field. Everything else about your previous answer was fine. "
    "Reminders for the two fields this happens to: a setup's `trigger`/`stop`/`targets[].price` must be "
    "real JSON numbers (12.34, never \"12.34\" and never a sentence) whenever that setup has a numeric "
    "stop; if the level genuinely is not decided yet, leave the whole setup without a stop rather than "
    "writing prose into a numeric field. A setup's `type` must be exactly one of: Breakout, Retest, "
    "Pullback, Reclaim, Failed Breakdown, Gap-and-Hold -- one label, no description, no slash-combination, "
    "no date. The full description of the setup belongs in the report body."
)


def _fixable_field_error(update_id: int) -> Optional[str]:
    """The stored error for this message when it failed on a field-shape
    refusal, or None. Only these are worth a retry: they are deterministic,
    self-describing, and the analysis behind them was already correct."""
    if _message_status(update_id) != "failed":
        return None
    with persistence._db() as conn:
        row = conn.execute("SELECT error FROM messages WHERE update_id=?", (update_id,)).fetchone()
    error = (row["error"] if row else None) or ""
    return error if _FIXABLE_FIELD_ERROR_RE.search(error) else None


def _handle_screener(update_id: int, text: str) -> bool:
    m = _SCREENER_RE.match(text.strip())
    if not m:
        return False
    ticker = m.group(1).upper()
    if not _TICKER_RE.match(ticker):
        _reject_invalid_ticker(update_id, ticker)
        return True
    prompt = _SCREENER_PROMPT_TEMPLATE.format(
        ticker=ticker, update_id=update_id, date=datetime.now().date().isoformat()
    )
    cmd = [
        "claude", "-p", "--allowed-tools", "Read", "Write",
        # 2026-08-09: build_plan.py replaces the direct fetch_analysis_data.py
        # call. It runs that fetch itself and returns BOTH the raw facts and the
        # mechanical plan built from them, so the run still costs exactly one
        # TradingView fetch -- the slowest and most fragile step -- while the
        # arithmetic that used to be retyped by hand is now computed.
        "PowerShell(python bot\\build_plan.py*)",
        "PowerShell(python bot/build_plan.py*)",
        "Bash(python bot\\build_plan.py*)",
        "Bash(python bot/build_plan.py*)",
        # Still allowed: a re-fetch is occasionally the right move when the plan
        # comes back empty and the raw data is worth a second look.
        "PowerShell(python bot\\fetch_analysis_data.py*)",
        "PowerShell(python bot/fetch_analysis_data.py*)",
        "PowerShell(python bot\\deliver_report.py*)",
        "PowerShell(python bot/deliver_report.py*)",
        # rule 28 sizing calculator -- read-only arithmetic, no DB write, no
        # network; allowlisted so the prompt's "don't do this by hand"
        # instruction is actually executable.
        "PowerShell(python bot\\size_policy.py*)",
        "PowerShell(python bot/size_policy.py*)",
        "Bash(python bot\\fetch_analysis_data.py*)",
        "Bash(python bot/fetch_analysis_data.py*)",
        "Bash(python bot\\deliver_report.py*)",
        "Bash(python bot/deliver_report.py*)",
        "Bash(python bot\\size_policy.py*)",
        "Bash(python bot/size_policy.py*)",
        "--output-format", "json",
    ]
    # 1500s (25 min), up from the 900s that got cut right at the edge of real,
    # legitimate completion time on the first live test (2026-07-09, MSFT).
    returncode, combined = _run_claude_with_retry(
        cmd, timeout=1500, label=f"/screener {ticker}",
        permitted_substrings=["build_plan.py", "fetch_analysis_data.py", "deliver_report.py"],
        update_id=update_id,
        prompt=prompt,
    )
    _log(f"/screener {ticker}: claude -p exit={returncode} output_len={len(combined)}")
    if len(combined) < 5000:
        # A real, completed screener run's captured output is always large (the
        # fetch_analysis_data.py JSON blob alone is several KB). Anything this
        # short means something stopped early -- log the actual content, not just
        # its length, so a silent short-output failure like this is diagnosable
        # instead of a mystery (found real, 2026-07-09, NVDA: exit=0, 3849 chars,
        # never reached delivery, and the length-only log gave no way to tell why).
        _log(f"/screener {ticker}: SHORT OUTPUT, full content follows:\n{combined}")
    else:
        # Even a long, apparently-real output can still silently never reach
        # delivery (found real, 2026-07-10: /monitor and /playbook both ran
        # 7000+ chars of real work, exit=0, but never called their deliver
        # script -- and the length-only log gave no way to tell why, same
        # blind spot as the short-output case, just at a different length).
        # --output-format json's terminal result object (stop_reason,
        # is_error, num_turns, etc.) is always at the END of stdout, so the
        # tail alone is enough to diagnose without logging the full multi-KB
        # body every time.
        _log(f"/screener {ticker}: output tail follows:\n{combined[-3000:]}")

    if returncode != 0 and _is_logged_out(combined):
        _alert_logged_out(f"/screener {ticker}", update_id)
        return True

    if returncode != 0 and _is_quota_exhausted(combined):
        _log("QUOTA EXHAUSTION DETECTED for /screener -- alerting, not retrying silently")
        try:
            send_text(
                "⚠️ <b>נגמרה מכסת ה-Agent SDK החודשית</b>\n"
                f"/screener {escape_html(ticker)} לא עובד -- הבוט ימתין לחידוש בתחילת מחזור החיוב הבא."
            )
        except Exception:
            _log("failed to send quota-exhaustion alert to Telegram")
        _mark_failed_unless_already_sent(update_id, "quota exhaustion detected", notify=False)
        return True

    # deliver_report.py itself calls mark_sent/mark_failed on success/internal
    # failure -- but if claude -p exits nonzero before ever reaching that script
    # (e.g. crashed mid-analysis, or the process tree had to be killed on
    # timeout), the message would otherwise be stranded in 'processing' forever.
    # Safety net: if it's still not terminal, mark it failed -- but never
    # overwrite an already-'sent' status (see _mark_failed_unless_already_sent).
    if _message_status(update_id) not in ("sent", "failed"):
        _mark_failed_unless_already_sent(
            update_id, f"screener automation did not reach a terminal state (exit={returncode})"
        )
    return True


def _parse_screener_tickers(text: str) -> list[str]:
    """Accepts one or more tickers after /screener, separated by commas and/or
    whitespace ("/screener AAPL", "/screener AAPL,MSFT", "/screener AAPL, MSFT NVDA").
    Order-preserving de-dupe -- sending the same ticker twice buys nothing and
    would just double the cost/runtime for no reason."""
    m = _SCREENER_ARGS_RE.match(text.strip())
    if not m:
        return []
    seen: set[str] = set()
    tickers = []
    for raw in re.split(r"[,\s]+", m.group(1).strip()):
        t = raw.upper()
        if t and t not in seen:
            seen.add(t)
            tickers.append(t)
    return tickers


def _handle_screener_batch(update_id: int, text: str) -> bool:
    """/screener now accepts a batch of tickers in one message. A single ticker
    keeps the exact prior behavior (this call IS the synchronous analysis, via
    _handle_screener, completely unmodified). Two or more tickers are fanned out
    into individual synthetic '/screener TICKER' messages -- reusing the exact
    same persistence.enqueue_message pattern trigger_auto_monitor.py already
    uses for synthetic /monitorall runs -- so every ticker gets the full,
    unmodified single-ticker analysis and its own report.

    Deliberately NOT processed here in a loop: main()'s self-draining while-loop
    (see its own comment on count_pending_messages()) already re-claims and
    re-drains the queue after this returns, so the synthetic messages enqueued
    below get picked up and run ONE AT A TIME by that same loop -- never
    concurrently, which matters because tv_data.py's vendored TradingView
    connector can't handle concurrent CDP calls (see tv_data.py's own module
    docstring)."""
    tickers = _parse_screener_tickers(text)
    if not tickers:
        return False

    invalid = [t for t in tickers if not _TICKER_RE.match(t)]
    if invalid:
        resp = send_text(
            f"⚠️ טיקר לא תקין: {', '.join(escape_html(t) for t in invalid)} -- "
            f"אותיות באנגלית בלבד, עד 6 תווים, נקודה אחת אופציונלית + עד 2 אותיות סיומת."
        )
        if resp.get("ok"):
            persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        else:
            persistence.mark_failed(update_id, f"invalid ticker(s) {invalid}, ack send_text also failed")
        return True

    if len(tickers) == 1:
        return _handle_screener(update_id, text)

    if len(tickers) > _MAX_BATCH_SCREENER:
        resp = send_text(
            f"⚠️ בקשה ל-{len(tickers)} טיקרים בבת אחת חורגת מהמגבלה ({_MAX_BATCH_SCREENER}) -- "
            f"כל טיקר הוא ריצה נפרדת של כ-25 דקות, ברצף. פצל לכמה הודעות /screener קטנות יותר."
        )
        if resp.get("ok"):
            persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        else:
            persistence.mark_failed(update_id, "batch screener over size limit, ack send_text also failed")
        return True

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    enqueued = []
    for i, ticker in enumerate(tickers):
        sub_update_id = -(now_ms + i)  # can't collide with a real Telegram update_id (always positive)
        ok = persistence.enqueue_message(
            update_id=sub_update_id, from_id="batch_screener", chat_id="batch_screener",
            message_type="text", message_text=f"/screener {ticker}",
            raw_update={"synthetic": True, "source": "batch_screener", "parent_update_id": update_id},
        )
        if ok:
            enqueued.append(ticker)
        else:
            _log(f"batch screener: update_id {sub_update_id} collision -- skipping {ticker} (should not happen)")

    resp = send_text(
        f"📋 <b>סריקה קבוצתית — {len(enqueued)} טיקרים</b>\n"
        f"{', '.join(escape_html(t) for t in enqueued)}\n\n"
        f"כל טיקר יעובד בנפרד וברצף (לא במקביל) ותקבל דוח /screener מלא לכל אחד."
    )
    if resp.get("ok"):
        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
    else:
        persistence.mark_failed(update_id, "batch screener ack send_text failed")
    return True


_MONITOR_RE = re.compile(r"^/monitor\s+(\S+)\s*$", re.IGNORECASE)

_MONITOR_PROMPT_TEMPLATE = (
    "NOTE: if any tool output shows garbled characters where Hebrew or other non-ASCII text would be "
    "expected, that is a known, purely cosmetic Windows console-codepage display artifact -- the "
    "underlying data is correct. Do not attempt to work around it (no encoding tricks, redirects, env "
    "vars, or helper scripts) -- proceed directly using the data as given. "
    "This session's --allowed-tools does NOT include Edit. If you ever need to correct content in a file "
    "you already wrote (a typo, a mis-encoded character, anything), use Write to overwrite that same file "
    "with the fully corrected content -- never call Edit, and never stop to ask a human for permission or "
    "approval to use a disallowed tool. This is an unattended automated run with no one present to grant "
    "it; stopping to ask always ends the run as a failure the same as never fixing it at all. "
    "You have exactly two permitted commands for this task: `python bot\\fetch_monitor_data.py TICKER` "
    "and `python bot\\deliver_monitor_report.py <path>`. Do not query the SQLite database directly, do "
    "not write or run any ad-hoc verification/helper script, and do not run any other PowerShell/python "
    "command -- anything else will be silently denied and wastes your available turns. The permitted commands above must be invoked via the PowerShell tool, not the Bash tool -- use PowerShell for these calls from the start."
    "fetch_monitor_data.py's JSON output (the stored thesis plus live data) is the complete, authoritative "
    "source of truth for this check -- there is nothing else to look up. "
    "Run `python bot\\fetch_monitor_data.py {ticker}` to get the stored thesis (or null) plus live "
    "intraday (2H/30m since open) data and SPY's live price. Read MONITOR_v2.md fresh from disk. Apply "
    "full Category B judgment per its status tiers (white/yellow/yellow_plus/green/red). If the stored "
    "thesis is null, this is an ad-hoc check only (section ד) -- do not fabricate a trigger. If the "
    "status is green, diff the live trigger/stop against the stored thesis (tolerance max(1%, "
    "0.3 x atr_at_build)) -- this never blocks a real, price-confirmed trigger, but surface a prominent "
    "warning if the deviation is outside tolerance. Also, if status is green, apply CONSISTENCY_RULES.md "
    "rule 18's regime hard gate using rule 23's numeric formula (2026-07-20): fetch_monitor_data.py's output "
    "includes market_regime_formula -- copy market_regime_formula.regime VERBATIM into TWO fields: "
    "market_regime_formula (the formula's raw, untouched value) AND regime_now (the value actually used --"
    " normally identical). Do not reuse the thesis's stored market_regime_at_build for regime_now, and do "
    "not re-classify the regime yourself from the chart -- the same disclosed-override-only exception as "
    "/screener applies: only set regime_now to something different from market_regime_formula alongside an "
    "explicit regime_override_reason for a real, specific reason like a same-day Fed/CPI/NFP release -- check "
    "fetch_monitor_data.py's `economic_calendar_upcoming` field (real data, today + next 2 days) rather than "
    "guessing whether one exists -- never override because the formula's call looks surprising. If regime_now is risk_off or structure_break, "
    "set regime_blocked=true and include both regime_at_build (the thesis's stored value) and regime_now "
    "in the JSON; the trigger still gets reported as a real fact, but no order "
    "is written and the thesis stays pending. If regime_now does not block, set regime_blocked=false "
    "and omit regime_at_build/regime_now. Copy fetch_monitor_data.py's own \"freshness\" field into the "
    "JSON's freshness field VERBATIM -- do not recompute or reword it. "
    "The Telegram text is BUILT FOR YOU -- do not write it by hand and do not reproduce MONITOR_v2.md "
    "section ו's templates from memory. bot/monitor_text.py holds them and deliver_monitor_report.py "
    "fills them from your figures plus what is already stored (the planned levels and their targets, "
    "the grade the thesis was built with, the trigger's age, whether the ticker is held). Anything you "
    "put in summary_text is ignored. Supply instead: `sentence` (ONE plain beginner sentence -- what "
    "happened on the chart for a green, what still has to happen for every other tier), `setup_used` "
    "(\"primary\" or \"alternate\", whichever setup this check is about), and `rubric_formula_now` "
    "copied VERBATIM from fetch_monitor_data.py's output -- the live grade, the failing criteria and "
    "any \"cannot be scored\" reason are all read out of it. For a green ticker also supply `order` "
    "with the quantity recomputed from account.risk_usd and this check's real entry-stop distance. The "
    "planned-versus-actual tolerance check, the Starter quantity and the portfolio/sector/cash "
    "disclosures are all computed there from the figures you copy -- do not write those lines. "
    "Write a JSON file (to a new file under d:\\Trading New\\_runs\\, e.g. "
    "_runs\\_decision_monitor_{ticker}.json -- every per-run payload belongs in _runs\\, never "
    "the project root) matching the exact shape documented in "
    "bot/deliver_monitor_report.py's own module docstring, with update_id={update_id} and date={date}. "
    "Finally run `python bot\\deliver_monitor_report.py <path-to-that-json>` to render, deliver, log the "
    "check, and mark the message sent. Do not invent a trigger not traceable to the stored thesis or "
    "live data."
)


def _handle_monitor(update_id: int, text: str) -> bool:
    m = _MONITOR_RE.match(text.strip())
    if not m:
        return False
    ticker = m.group(1).upper()
    if not _TICKER_RE.match(ticker):
        _reject_invalid_ticker(update_id, ticker)
        return True
    prompt = _MONITOR_PROMPT_TEMPLATE.format(
        ticker=ticker, update_id=update_id, date=datetime.now().date().isoformat()
    )
    cmd = [
        "claude", "-p", "--allowed-tools", "Read", "Write",
        "PowerShell(python bot\\fetch_monitor_data.py*)",
        "PowerShell(python bot/fetch_monitor_data.py*)",
        "PowerShell(python bot\\deliver_monitor_report.py*)",
        "PowerShell(python bot/deliver_monitor_report.py*)",
        "Bash(python bot\\fetch_monitor_data.py*)",
        "Bash(python bot/fetch_monitor_data.py*)",
        "Bash(python bot\\deliver_monitor_report.py*)",
        "Bash(python bot/deliver_monitor_report.py*)",
        "--output-format", "json",
    ]
    returncode, combined = _run_claude_with_retry(
        cmd, timeout=900, label=f"/monitor {ticker}",
        permitted_substrings=["fetch_monitor_data.py", "deliver_monitor_report.py"], update_id=update_id,
        prompt=prompt,
    )
    _log(f"/monitor {ticker}: claude -p exit={returncode} output_len={len(combined)}")
    if len(combined) < 3000:
        _log(f"/monitor {ticker}: SHORT OUTPUT, full content follows:\n{combined}")
    else:
        _log(f"/monitor {ticker}: output tail follows:\n{combined[-3000:]}")

    if returncode != 0 and _is_logged_out(combined):
        _alert_logged_out(f"/monitor {ticker}", update_id)
        return True

    if returncode != 0 and _is_quota_exhausted(combined):
        _log("QUOTA EXHAUSTION DETECTED for /monitor -- alerting, not retrying silently")
        try:
            send_text(
                "⚠️ <b>נגמרה מכסת ה-Agent SDK החודשית</b>\n"
                f"/monitor {escape_html(ticker)} לא עובד -- הבוט ימתין לחידוש בתחילת מחזור החיוב הבא."
            )
        except Exception:
            _log("failed to send quota-exhaustion alert to Telegram")
        _mark_failed_unless_already_sent(update_id, "quota exhaustion detected", notify=False)
        return True

    if _message_status(update_id) not in ("sent", "failed"):
        _mark_failed_unless_already_sent(
            update_id, f"monitor automation did not reach a terminal state (exit={returncode})"
        )
    return True


_AUTOMONITOR_PROMPT_TEMPLATE = (
    "NOTE: if any tool output shows garbled characters where Hebrew or other non-ASCII text would be "
    "expected, that is a known, purely cosmetic Windows console-codepage display artifact -- the "
    "underlying data is correct. Do not attempt to work around it (no encoding tricks, redirects, env "
    "vars, or helper scripts) -- proceed directly using the data as given. "
    "This session's --allowed-tools does NOT include Edit. If you ever need to correct content in a file "
    "you already wrote (a typo, a mis-encoded character, anything), use Write to overwrite that same file "
    "with the fully corrected content -- never call Edit, and never stop to ask a human for permission or "
    "approval to use a disallowed tool. This is an unattended automated run with no one present to grant "
    "it; stopping to ask always ends the run as a failure the same as never fixing it at all. "
    "You have exactly two permitted commands for this task: `python bot\\fetch_monitor_data.py TICKER` "
    "and `python bot\\deliver_auto_monitor_report.py <path>`. Do not query the SQLite database directly, "
    "do not write or run any ad-hoc verification/helper script, and do not run any other PowerShell/python "
    "command -- anything else will be silently denied and wastes your available turns. The permitted commands above must be invoked via the PowerShell tool, not the Bash tool -- use PowerShell for these calls from the start."
    "This is an automated batch scan across every ticker currently in /pending: {tickers}. "
    "For EACH ticker in that list, in order: run `python bot\\fetch_monitor_data.py TICKER{strict_flag}` to "
    "get the stored thesis plus live intraday (2H/30m since open) data and SPY's live price. Read "
    "MONITOR_v2.md fresh from disk once (reuse it for every ticker in this run). Apply full Category B "
    "judgment per its status tiers (white/yellow/yellow_plus/green/red) exactly as a single /monitor TICKER "
    "check would -- do not fabricate a trigger for a ticker whose fetched thesis is null. "
    "{strict_gate_note}"
    "For any ticker whose status comes out green or yellow_plus, the line the user sees is BUILT FOR "
    "YOU -- do not write it by hand and do not reproduce MONITOR_v2.md section ו.3's template from "
    "memory. bot/monitor_text.py holds the fixed templates and deliver_auto_monitor_report.py fills "
    "them from your numbers plus the stored thesis. Anything you put in `headline` is ignored. Supply "
    "instead, per ticker: `sentence` (ONE plain beginner sentence saying what actually happened -- the "
    "only prose asked for), `setup_used` (\"primary\" or \"alternate\", whichever setup this check is "
    "about), and `rubric_formula_now` copied VERBATIM from fetch_monitor_data.py's output -- the live "
    "grade, the failing criteria and any \"cannot be scored\" reason are all read out of it, so none of "
    "them is retyped. For a green ticker also supply `order` with the full-position share quantity "
    "recomputed from that ticker's own account.risk_usd and the real entry-stop distance of THIS check "
    "(the same sizing formula a single /monitor green check would use) -- never the stale planned_qty "
    "from the original SCREENER_v3 thesis. The Starter quantity for a yellow_plus ticker, the stored "
    "trigger, the grade the thesis was built with and the trigger's age are all read from the database "
    "-- do not write any of them. Collect every "
    "ticker's result into a single JSON array matching bot/deliver_auto_monitor_report.py's own module "
    "docstring shape, with update_id={update_id} and date={date}. Write it under d:\\Trading New\\_runs\\ "
    "(e.g. _runs\\_automonitor_{date}.json) -- every per-run payload belongs in _runs\\, never the project "
    "root. Finally run `python bot\\"
    "deliver_auto_monitor_report.py <path-to-that-json>` exactly ONCE at the end (never once per ticker) "
    "to log every check, flip thesis status where applicable, and deliver one consolidated summary."
)

# Strict-open scan only (2026-07-31): fills the {strict_flag}/{strict_gate_note}
# placeholders above; both are "" on a normal /monitorall run. This scan fires at
# market open + 30min, when no 2H bar can possibly have closed yet -- the 5-min
# gate SUBSTITUTES for that requirement this early, it is not an extra condition
# stacked on top of an unreachable one. See MONITOR_v2.md's own strict-open bullet,
# which this note just points to rather than duplicating.
_AUTOMONITOR_STRICT_GATE_NOTE = (
    "STRICT-OPEN SCAN (market open + 30min, no 2H bar can have closed yet): before "
    "flipping any ticker to yellow_plus this run, apply MONITOR_v2.md's strict-open "
    "confirmation bullet exactly as written there -- the 5-min-bar gate described "
    "there SUBSTITUTES for the normal closed-2H-bar requirement this early, it does "
    "not add to it. Green stays gated purely on a real daily close as always, "
    "unaffected by this. If the strict-open gate is not met, keep the ticker at its "
    "normal-rubric status this run and note '5-min confirmation not yet met'. "
)


def _handle_automonitor(update_id: int, strict: bool = False) -> bool:
    rows = persistence.get_pending_report_rows(current_regime=None)
    tickers = [r["ticker"] for r in rows]
    if not tickers:
        resp = send_text("🔍 <b>סריקה אוטומטית</b>\nאין טיקרים ב-Pending כרגע -- שום דבר לסרוק.")
        if resp.get("ok"):
            persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
            return True
        persistence.mark_failed(update_id, "send_text failed for /monitorall (empty pending)")
        return True

    prompt = _AUTOMONITOR_PROMPT_TEMPLATE.format(
        tickers=", ".join(tickers), update_id=update_id, date=datetime.now().date().isoformat(),
        strict_flag=" --strict-open" if strict else "",
        strict_gate_note=_AUTOMONITOR_STRICT_GATE_NOTE if strict else "",
    )
    cmd = [
        "claude", "-p", "--allowed-tools", "Read", "Write",
        "PowerShell(python bot\\fetch_monitor_data.py*)",
        "PowerShell(python bot/fetch_monitor_data.py*)",
        "PowerShell(python bot\\deliver_auto_monitor_report.py*)",
        "PowerShell(python bot/deliver_auto_monitor_report.py*)",
        "Bash(python bot\\fetch_monitor_data.py*)",
        "Bash(python bot/fetch_monitor_data.py*)",
        "Bash(python bot\\deliver_auto_monitor_report.py*)",
        "Bash(python bot/deliver_auto_monitor_report.py*)",
        "--output-format", "json",
    ]
    # Longer timeout than single /monitor's 900s -- this is N sequential ticker checks
    # in one claude -p call, not one.
    cmd_label = f"/monitorall_strict ({len(tickers)} tickers)" if strict else f"/monitorall ({len(tickers)} tickers)"
    returncode, combined = _run_claude_with_retry(
        cmd, timeout=max(900, 400 * len(tickers)), label=cmd_label,
        permitted_substrings=["fetch_monitor_data.py", "deliver_auto_monitor_report.py"], update_id=update_id,
        prompt=prompt,
    )
    _log(f"{cmd_label}: claude -p exit={returncode} output_len={len(combined)}")
    if len(combined) < 3000:
        _log(f"{cmd_label}: SHORT OUTPUT, full content follows:\n{combined}")
    else:
        _log(f"{cmd_label}: output tail follows:\n{combined[-3000:]}")

    if returncode != 0 and _is_logged_out(combined):
        _alert_logged_out(cmd_label, update_id)
        return True

    if returncode != 0 and _is_quota_exhausted(combined):
        _log(f"QUOTA EXHAUSTION DETECTED for {cmd_label} -- alerting, not retrying silently")
        try:
            send_text(
                "⚠️ <b>נגמרה מכסת ה-Agent SDK החודשית</b>\n"
                "סריקה אוטומטית (/monitorall) לא עובדת -- הבוט ימתין לחידוש בתחילת מחזור החיוב הבא."
            )
        except Exception:
            _log("failed to send quota-exhaustion alert to Telegram")
        _mark_failed_unless_already_sent(update_id, "quota exhaustion detected", notify=False)
        return True

    if _message_status(update_id) not in ("sent", "failed"):
        _mark_failed_unless_already_sent(
            update_id, f"auto-monitor automation did not reach a terminal state (exit={returncode})"
        )
    return True


_POSITION_STATUS_EMPTY_HEADER = "📊 <b>סטטוס פוזיציות פתוחות</b>"

_POSITION_STATUS_PROMPT_TEMPLATE = (
    "NOTE: if any tool output shows garbled characters where Hebrew or other non-ASCII text would be "
    "expected, that is a known, purely cosmetic Windows console-codepage display artifact -- the "
    "underlying data is correct. Do not attempt to work around it (no encoding tricks, redirects, env "
    "vars, or helper scripts) -- proceed directly using the data as given. "
    "This session's --allowed-tools does NOT include Edit. If you ever need to correct content in a file "
    "you already wrote (a typo, a mis-encoded character, anything), use Write to overwrite that same file "
    "with the fully corrected content -- never call Edit, and never stop to ask a human for permission or "
    "approval to use a disallowed tool. This is an unattended automated run with no one present to grant "
    "it; stopping to ask always ends the run as a failure the same as never fixing it at all. "
    "You have exactly two permitted commands for this task: `python bot\\fetch_analysis_data.py TICKER` "
    "and `python bot\\deliver_position_status_report.py <path>`. Do not query the SQLite database "
    "directly, do not write or run any ad-hoc verification/helper script, and do not run any other "
    "PowerShell/python command -- anything else will be silently denied and wastes your available turns. "
    "The permitted commands above must be invoked via the PowerShell tool, not the Bash tool -- use "
    "PowerShell for these calls from the start. "
    "This is a quick status check across every position currently open in this system: {tickers}. Every "
    "one of these is a REAL filled position -- fetch_analysis_data.py's `open_position` field for each "
    "will be non-null (initial_stop, current_stop, entry_setup). Read STRATEGY_v3.md and "
    "CONSISTENCY_RULES.md fresh from disk once (reuse for every ticker in this run). For EACH ticker, in "
    "order: run `python bot\\fetch_analysis_data.py TICKER`, then apply full STRATEGY_v3 judgment -- "
    "current price vs. current_stop (distance in ATR14), distance to the nearest qualifying target or "
    "checkpoint, and one action from STRATEGY_v3's fixed vocabulary (Hold / Hold With Alert / Trim / Sell "
    "Partial / Exit / Protect With Stop / No Action). This is advisory only: do NOT attempt to persist any "
    "stop change (there is no tool call available or permitted for that here) -- just report what you "
    "computed. \"the nearest qualifying target\" means the nearest target that is still UNREALIZED: "
    "open_position.tranche_plan (2026-08-07) already marks every rule-7 tranche filled/partial/waiting off "
    "the real recorded exits, and next_label/next_price is the only piece still to be sold. A tranche with "
    "status 'filled' is DONE -- never call its price an approaching target, and never emit the "
    "approaching-target status line against a level that was already realized (real ASTS incident: a single "
    "stored target was sold at twice because nothing in the system ever recorded the first one as spent). "
    "When next_label is 'runner' there is no numeric target left at all -- the remaining shares exit only "
    "on the trailing stop, so use the close-to-stop / safe-distance line and say the Runner is what is "
    "left. Never re-derive the tranche split yourself, and surface any tranche_plan.warnings verbatim in "
    "that ticker's optional fourth line. "
    "Each ticker's line is BUILT FOR YOU -- do not write it by hand and do not reproduce "
    "STRATEGY_v3.md section ח.2's template from memory. bot/position_text.py holds the fixed template "
    "and deliver_position_status_report.py fills it from your figures plus the position as the database "
    "actually records it (the live stop, the shares still held after partial exits, the blended entry "
    "price, the next unsold tranche, the Starter's original trigger). Anything you put in `headline` is "
    "ignored. Supply instead, per ticker: `action` (exactly one of the eight words above -- the real "
    "judgment call), `price` and `atr14` copied from that ticker's own fetch_analysis_data.py output, "
    "and `sentence` ONLY when there is something real to say (a quiet Hold with nothing changed gets no "
    "sentence). For a Starter position also copy `last_bar_close` (the close of recent_bars_40's last "
    "bar) and `bar_fresh` (freshness.fresh) -- the confirmation rule is applied in code from those two "
    "facts, so do not decide it yourself. Profit/loss, which of the three status lines applies, the "
    "Starter block, the stop distance in dollars, the days in the trade and any tranche warning are all "
    "computed there; writing any of them yourself only duplicates them. "
    "Also include stop (current_stop) and entry_date (open_position.entry_date) as top-level fields, "
    "copied verbatim -- the delivery script cross-checks them (2026-07-30 fix). "
    "Collect every ticker's result into a single JSON array matching bot/deliver_position_status_report.py's "
    "own module docstring shape, with update_id={update_id}, date={date}, run_label={run_label}. Write it "
    "under d:\\Trading New\\_runs\\ (e.g. _runs\\_position_status_{date}.json) -- every per-run payload "
    "belongs in _runs\\, never the project root. Finally "
    "run `python bot\\deliver_position_status_report.py <path-to-that-json>` exactly ONCE at the end "
    "(never once per ticker) to deliver one consolidated summary."
)


def _handle_position_status(update_id: int, raw_update: dict) -> bool:
    rows = persistence.get_open_positions()
    tickers = [r["ticker"] for r in rows]
    source = (raw_update or {}).get("source", "")
    run_label = "midday" if source.endswith(":midday") else "eod" if source.endswith(":eod") else "manual"

    if not tickers:
        resp = send_text(f"{_POSITION_STATUS_EMPTY_HEADER}\nאין פוזיציות פתוחות כרגע.")
        if resp.get("ok"):
            persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
            return True
        persistence.mark_failed(update_id, "send_text failed for /positions (no open positions)")
        return True

    prompt = _POSITION_STATUS_PROMPT_TEMPLATE.format(
        tickers=", ".join(tickers), update_id=update_id,
        date=datetime.now().date().isoformat(), run_label=run_label,
    )
    cmd = [
        "claude", "-p", "--allowed-tools", "Read", "Write",
        "PowerShell(python bot\\fetch_analysis_data.py*)",
        "PowerShell(python bot/fetch_analysis_data.py*)",
        "PowerShell(python bot\\deliver_position_status_report.py*)",
        "PowerShell(python bot/deliver_position_status_report.py*)",
        "Bash(python bot\\fetch_analysis_data.py*)",
        "Bash(python bot/fetch_analysis_data.py*)",
        "Bash(python bot\\deliver_position_status_report.py*)",
        "Bash(python bot/deliver_position_status_report.py*)",
        "--output-format", "json",
    ]
    returncode, combined = _run_claude_with_retry(
        cmd, timeout=max(900, 400 * len(tickers)), label=f"/positions ({len(tickers)} positions)",
        permitted_substrings=["fetch_analysis_data.py", "deliver_position_status_report.py"], update_id=update_id,
        prompt=prompt,
    )
    _log(f"/positions ({len(tickers)} positions): claude -p exit={returncode} output_len={len(combined)}")
    if len(combined) < 3000:
        _log(f"/positions: SHORT OUTPUT, full content follows:\n{combined}")
    else:
        _log(f"/positions: output tail follows:\n{combined[-3000:]}")

    if returncode != 0 and _is_logged_out(combined):
        _alert_logged_out("/positions", update_id)
        return True

    if returncode != 0 and _is_quota_exhausted(combined):
        _log("QUOTA EXHAUSTION DETECTED for /positions -- alerting, not retrying silently")
        try:
            send_text(
                "⚠️ <b>נגמרה מכסת ה-Agent SDK החודשית</b>\n"
                "סטטוס פוזיציות (/positions) לא עובד -- הבוט ימתין לחידוש בתחילת מחזור החיוב הבא."
            )
        except Exception:
            _log("failed to send quota-exhaustion alert to Telegram")
        _mark_failed_unless_already_sent(update_id, "quota exhaustion detected", notify=False)
        return True

    if _message_status(update_id) not in ("sent", "failed"):
        _mark_failed_unless_already_sent(
            update_id, f"position-status automation did not reach a terminal state (exit={returncode})"
        )
    return True


_PLAYBOOK_PROMPT_TEMPLATE = (
    "NOTE: if any tool output shows garbled characters where Hebrew or other non-ASCII text would be "
    "expected, that is a known, purely cosmetic Windows console-codepage display artifact -- the "
    "underlying data is correct. Do not attempt to work around it (no encoding tricks, redirects, env "
    "vars, or helper scripts) -- proceed directly using the data as given. "
    "This session's --allowed-tools does NOT include Edit. If you ever need to correct content in a file "
    "you already wrote (a typo, a mis-encoded character, anything), use Write to overwrite that same file "
    "with the fully corrected content -- never call Edit, and never stop to ask a human for permission or "
    "approval to use a disallowed tool. This is an unattended automated run with no one present to grant "
    "it; stopping to ask always ends the run as a failure the same as never fixing it at all. "
    "You have exactly three permitted commands for this task: `python bot\\update_equity.py AMOUNT`, "
    "`python bot\\fetch_analysis_data.py TICKER`, "
    "and `python bot\\deliver_playbook_report.py <path>` -- plus the Read tool for the already-downloaded "
    "image below and the protocol .md files. Do not query the "
    "SQLite database directly, do not write or run any ad-hoc verification/helper script, and do not run "
    "any other PowerShell/python command -- anything else will be silently denied and wastes your "
    "available turns. fetch_analysis_data.py's JSON output (which already includes each ticker's sleeve "
    "classification) is the complete, authoritative source of truth per ticker -- there is nothing else "
    "to look up. "
    "The user's portfolio screenshot has ALREADY been downloaded (deterministically, outside this session) "
    "to `_playbook_{update_id}.jpg` in the project root -- no download step needed or possible. Use the "
    "Read tool to VIEW that image file directly (it "
    "supports images) and read every real position's ticker, quantity, and average cost from the table "
    "shown (skip options/derivatives rows -- this system manages equities/ETFs only). "
    "Also read the account summary shown on the same screenshot (2026-07-18) -- the broker's own total "
    "account value (Net Liquidation Value or equivalent \"total\" figure, NOT just cash and NOT just "
    "holdings value). Only proceed with this if the screenshot actually shows a clear total figure -- skip "
    "this whole step (do not guess or sum positions yourself) if the screenshot doesn't show one plainly. "
    "If it does: BEFORE running fetch_analysis_data.py for any ticker below, immediately run "
    "`python bot\\update_equity.py AMOUNT` with that number (2026-07-30 fix -- found real: every "
    "fetch_analysis_data.py call for this run reads cash/heat/allocation math off whatever equity is "
    "ALREADY stored, so doing this first, before the per-ticker loop, is the only way this same run's own "
    "numbers reflect the screenshot instead of a stale prior value). Also include the same number in the "
    "JSON as a top-level numeric field named account_equity_usd (still needed for the pending-withdrawal "
    "note deliver_playbook_report.py builds) -- you make no further judgment call about it beyond reading "
    "the number off the screenshot. For each real "
    "ticker, run `python bot\\fetch_analysis_data.py TICKER` to get real technical data, INCLUDING its "
    "\"sleeve\" field (core/swing/unknown, never invent or re-derive this -- use exactly what's returned). "
    "That output ALSO includes an \"open_position\" field -- null if this system never filled this ticker "
    "through /filled, otherwise the REAL, already-documented position: initial_stop (the original risk "
    "level, fixed at entry, never changes), current_stop (the real live stop as of the last run), and "
    "entry_setup (the original thesis: type/trigger/targets/checkpoints). "
    "open_position.qty is the ORIGINAL fill size and is INTENTIONALLY NEVER updated by a partial exit -- "
    "it is not what is actually held today. open_position.remaining_qty IS the real, live share count "
    "after every partial exit already recorded in this system. When comparing the screenshot's quantity "
    "for a ticker against what this system has on file, ALWAYS compare against remaining_qty, never qty -- "
    "qty being higher than the screenshot does NOT by itself mean anything is unrecorded (it is expected "
    "and correct for any position with prior partial exits). Only treat it as a genuine, unrecorded "
    "discrepancy if the screenshot's quantity differs from remaining_qty. If it does: state the exact "
    "correct command in your summary_text depending on direction -- a LOWER screenshot quantity than "
    "remaining_qty means shares were sold and not yet recorded (`/exit TICKER price qty`, never /filled); "
    "a HIGHER screenshot quantity on a ticker with a non-null open_position means shares were added "
    "(`/add TICKER price qty`, never /filled -- /filled is only for a ticker whose open_position is null, "
    "a wholly new position this system has never seen). "
    "open_position.tranche_plan (2026-08-07) is CONSISTENCY_RULES.md rule 7's exit allocation with each "
    "piece already marked against the real recorded exits: every tranche carries label/price/planned_pct/"
    "planned_qty/filled_qty/status, plus next_label, next_price, next_qty, runner_qty_left and warnings. "
    "This is computed in code from the position's own stored targets and its exits rows -- treat it as "
    "fact and never re-derive the split yourself. A tranche with status 'filled' is DONE: never present "
    "its price as a target still waiting to be sold at, never propose selling there again, and never "
    "count it toward how much is still to be realized. When next_label is 'runner' there is no numeric "
    "target left at all -- the remaining shares are the Runner and they exit only on the trailing stop, "
    "so say exactly that instead of naming a price. If tranche_plan.warnings is non-empty, surface each "
    "warning verbatim in your summary_text; they mean the real exits and the written plan have drifted "
    "apart, which is precisely the condition that goes unnoticed otherwise. "
    "When open_position is NOT null, "
    "this IS the position's documented setup -- do not describe it as undocumented/missing, and do not "
    "invent a new stop from generic chart structure as if starting fresh. **Do not work the trailed stop out by hand: run `python bot\\trail_stop.py TICKER` for each open position.** It applies STRATEGY_v3's own method (highest daily low clearing the 0.7x ATR noise floor, rule 24's 0.15x ATR buffer underneath it, measured against the position's FROZEN atr_at_build, never moving down) and returns stop_should_be plus the level behind it. Use that number and quote its stop_basis_level. Two output fields decide whether a raise is actually on the table, and both must be read before proposing one: `held_on_pre_entry_structure` true means a higher level qualified but is dated BEFORE the position was opened -- report it as an older support level with its date, state plainly that the stop was NOT raised to it, and never present it as a new higher low; `runner_only` true means rule 6 applies and the trailing stop is the entire exit method, so a raise there is the plan working, not an early tightening. A `sleeve` that is not swing comes back with moved=false and no raise at all -- rule 8, and it is not something to work around. Start from current_stop and only "
    "move it if real NEW structure since entry genuinely justifies trailing it UP (never down -- a stop "
    "write that would lower current_stop is mechanically rejected by the persistence layer regardless of "
    "what you write, so there is no point proposing a lower number); if nothing has structurally changed, "
    "keep current_stop exactly as-is and say so. Only fall back to deriving a stop from scratch (highest "
    "daily low clearing the 0.7x ATR noise floor) when open_position is null. "
    "When choosing/keeping a stop for an existing position, judge its distance from noise using "
    "entry_setup.atr_at_build (the ATR frozen at the moment this setup's stop/targets were originally "
    "built), NEVER a freshly recomputed current ATR -- report_lint.py's stop noise-floor check (0.7x "
    "atr_at_build) is real and still surfaced to the user if violated, so the stop VALUE itself still needs "
    "this right. Current ATR is still the right figure for anything about TODAY's price action (e.g. a "
    "fresh stop derived from scratch when open_position is null, or the 0.3x-atr_at_build near-miss check). "
    "For an existing target's atr_mult/rr fields specifically (2026-07-22): unlike the stop, do not spend "
    "effort computing these two precisely and do not worry which ATR to use for them -- "
    "deliver_playbook_report.py now recomputes and overwrites BOTH fields deterministically for every "
    "target after this JSON is written, from the real price/stop/atr_at_build, regardless of what you put "
    "here. Write your best estimate (needed only so report_markdown's prose table has some text in that "
    "cell) and move on; a wrong number in just these two fields has zero effect on what the user ultimately "
    "sees. Your real judgment call is choosing the target PRICE itself (and the position's action) -- spend "
    "your effort there. "
    "Read STRATEGY_v3.md and CONSISTENCY_RULES.md fresh from disk. Apply full Category B judgment per "
    "STRATEGY_v3.md: compute a stop and (for swing positions) target(s) per position, and decide the "
    "action (Hold/Hold With Alert/Trim/Sell Partial/Exit/Protect With Stop/No Action). For each position's "
    "event/earnings check, use that ticker's own fetch_analysis_data.py `earnings` field (TradingView's real "
    "scanner data, 2026-07-26) -- state next_earnings_date verbatim when earnings.error is absent, otherwise "
    "say explicitly it's not verified (never from memory, never guessed). Also check `economic_calendar_"
    "upcoming` for a same-window CPI/PPI/NFP/FOMC/GDP release worth flagging. Copy each ticker's own "
    "\"freshness\" field from its fetch_analysis_data.py output into that position's JSON entry VERBATIM. "
    "The Telegram text is BUILT FOR YOU -- do not write it by hand and do not reproduce STRATEGY_v3.md "
    "section ח's template from memory. bot/position_text.py holds it and deliver_playbook_report.py "
    "fills it from your figures plus what is recorded: the stop this run just persisted, the shares "
    "still held, the blended entry price, which tranches are still unsold, the sleeve, and the "
    "Starter's original trigger. Anything you put in summary_text is ignored. Supply instead, per "
    "position: `action` (one of the eight fixed words), `price`, `qty` exactly as read off the "
    "screenshot, and `sentence` ONLY when there is something real to say. For a Starter also copy "
    "`last_bar_close` and `bar_fresh`. The profit figures, the '% gain from here' on each target, the "
    "reconciliation block and its exact /exit, /add or /filled command, and any tranche warning are all "
    "computed there -- do not write those lines. "
    "Write a JSON file "
    "(to a new file under d:\\Trading New\\_runs\\, e.g. _runs\\_decision_playbook_{update_id}.json -- "
    "every per-run payload belongs in _runs\\, never the project root) matching the exact "
    "shape documented in bot/deliver_playbook_report.py's own module docstring, with "
    "update_id={update_id} and date={date}. Finally run `python bot\\deliver_playbook_report.py "
    "<path-to-that-json>` to render, deliver, and mark the message sent. Do not invent a price level not "
    "traceable to the fetched data. (The downloaded screenshot file is cleaned up automatically after "
    "this run -- do not attempt to delete it yourself, that command is outside the two permitted "
    "above.)"
)


def _handle_playbook(update_id: int, raw_update: dict) -> bool:
    msg = raw_update.get("message", {})
    photos = msg.get("photo", [])
    if not photos:
        return False
    file_id = photos[-1]["file_id"]  # largest resolution is always last
    screenshot_path = PROJECT_ROOT / f"_playbook_{update_id}.jpg"

    # Downloaded HERE, deterministically, rather than asking the claude -p session
    # to do it via a tool call -- same principle as the cleanup a few lines down,
    # and for the same reason: found real 2026-07-14, repeatedly, that the inner
    # session's exact shell-command text for `python bot\download_photo.py` is
    # NOT stable across runs (Bash tool instead of PowerShell; forward vs
    # backslash paths; sometimes prefixed with `cd "..." &&`) -- no fixed
    # --allowed-tools prefix string can cover every phrasing an LLM might
    # generate for the same intent, so matching on exact command text is
    # fundamentally the wrong layer for a purely mechanical, judgment-free
    # operation (download_photo.py's own docstring already says as much: "No
    # judgment here"). Doing it directly in Python removes the whole class of
    # failure instead of chasing another wording variant next time.
    download = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "bot" / "download_photo.py"), file_id, str(screenshot_path)],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if download.returncode != 0:
        _log(f"/playbook: photo download failed rc={download.returncode}: "
             f"{download.stdout}{download.stderr}")
        _mark_failed_unless_already_sent(
            update_id, f"could not download the portfolio screenshot from Telegram (rc={download.returncode})"
        )
        return True

    prompt = _PLAYBOOK_PROMPT_TEMPLATE.format(update_id=update_id, date=datetime.now().date().isoformat())
    cmd = [
        "claude", "-p", "--allowed-tools", "Read", "Write",
        "PowerShell(python bot\\update_equity.py*)",
        "PowerShell(python bot/update_equity.py*)",
        "PowerShell(python bot\\fetch_analysis_data.py*)",
        "PowerShell(python bot/fetch_analysis_data.py*)",
        # rule 24 / STRATEGY_v3 trailing-stop calculator -- read-only arithmetic,
        # no DB write, no network. Allowlisted so the prompt's "do not work it out
        # by hand" instruction is actually executable (2026-08-09).
        "PowerShell(python bot\\trail_stop.py*)",
        "PowerShell(python bot/trail_stop.py*)",
        "Bash(python bot\\trail_stop.py*)",
        "Bash(python bot/trail_stop.py*)",
        "PowerShell(python bot\\deliver_playbook_report.py*)",
        "PowerShell(python bot/deliver_playbook_report.py*)",
        "Bash(python bot\\update_equity.py*)",
        "Bash(python bot/update_equity.py*)",
        "Bash(python bot\\fetch_analysis_data.py*)",
        "Bash(python bot/fetch_analysis_data.py*)",
        "Bash(python bot\\deliver_playbook_report.py*)",
        "Bash(python bot/deliver_playbook_report.py*)",
        "--output-format", "json",
    ]
    returncode, combined = _run_claude_with_retry(
        cmd, timeout=1500, label="/playbook",
        permitted_substrings=["update_equity.py", "fetch_analysis_data.py", "trail_stop.py",
                               "deliver_playbook_report.py"],
        update_id=update_id, prompt=prompt,
    )
    _log(f"/playbook: claude -p exit={returncode} output_len={len(combined)}")
    if len(combined) < 5000:
        _log(f"/playbook: SHORT OUTPUT, full content follows:\n{combined}")
    else:
        _log(f"/playbook: output tail follows:\n{combined[-3000:]}")

    # Deleted here, deterministically, rather than asking the claude -p session to
    # do it -- the "exactly two permitted commands" constraint added to the
    # prompt (2026-07-10) correctly blocks a Remove-Item call from inside that
    # session (found real: Claude tried it, got denied, and honestly reported
    # "that wasn't one of the permitted commands, I shouldn't have tried
    # it" -- a correct outcome, but it left the file on disk every run).
    try:
        screenshot_path.unlink(missing_ok=True)
    except Exception:
        _log(f"/playbook: failed to clean up {screenshot_path}")

    if returncode != 0 and _is_logged_out(combined):
        _alert_logged_out("/playbook", update_id)
        return True

    if returncode != 0 and _is_quota_exhausted(combined):
        _log("QUOTA EXHAUSTION DETECTED for /playbook -- alerting, not retrying silently")
        try:
            send_text(
                "⚠️ <b>נגמרה מכסת ה-Agent SDK החודשית</b>\n"
                "ניתוח התיק לא עובד -- הבוט ימתין לחידוש בתחילת מחזור החיוב הבא."
            )
        except Exception:
            _log("failed to send quota-exhaustion alert to Telegram")
        _mark_failed_unless_already_sent(update_id, "quota exhaustion detected", notify=False)
        return True

    if _message_status(update_id) not in ("sent", "failed"):
        _mark_failed_unless_already_sent(
            update_id, f"playbook automation did not reach a terminal state (exit={returncode})"
        )
    return True


# Reliability fix (2026-07-12, found real: /exit lly 1191.2 -- missing qty --
# and typos /fille, /lisr all sat in the queue forever with zero reply, reading
# to the user as "the bot isn't responding" even while ack_listener.py and
# process_queue.py were both running fine). One correct syntax hint per command
# whose prefix matched but whose own stricter regex then failed to parse the args.
_SYNTAX_HINTS = {
    "/drop": "/drop TICKER סיבה",
    "/exit": "/exit TICKER price qty",
    "/filled": "/filled TICKER price qty starter|full [primary|alternate]",
    "/add": "/add TICKER price qty",
    "/equity": "/equity 150000",
    "/setrisk": "/setrisk 1%",
    "/override": "/override TICKER heat <reason>",
    "/withdraw": "/withdraw 17500  (or /withdraw 0 to clear)",
    "/screener": "/screener TICKER  (or a batch: /screener AAPL, MSFT, NVDA)",
    "/monitor": "/monitor TICKER",
}


def _handle_unrecognized(update_id: int, text: str) -> bool:
    """Fallback for the fully-automated architecture (2026-07-09+): every
    command is supposed to be answered automatically now, so there is no live
    interactive session routinely picking up whatever falls through here
    (check_telegram.py's own 'manual handling' path is a pre-automation
    leftover, not something anyone runs routinely anymore) -- silence here
    reads to the user as the whole bot being unresponsive, which is exactly
    the bug this closes. Covers two distinct cases: a command whose PREFIX
    matched but whose own stricter argument parsing then failed (gets a
    specific syntax hint), and genuinely unrecognized text/typos (gets a
    pointer to /help). Always reaches a terminal state -- never left queued."""
    stripped = text.strip()
    lower = stripped.lower()
    hint = next((v for prefix, v in _SYNTAX_HINTS.items() if lower.startswith(prefix)), None)
    if hint:
        body = f"⚠️ תחביר שגוי. השתמש ב:\n<code>{escape_html(hint)}</code>"
        reason = f"malformed command args: {stripped!r}"
    else:
        body = f"⚠️ הפקודה <code>{escape_html(stripped) or '(ריקה)'}</code> לא זוהתה. שלח /help לרשימת הפקודות הזמינות."
        reason = f"unrecognized command: {stripped!r}"
    resp = send_text(body)
    if resp.get("ok"):
        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
    else:
        persistence.mark_failed(update_id, reason)
    return True


def _dispatch(msg_row: dict) -> bool:
    """Always returns True -- every message reaches a terminal state
    (sent/failed) after one drain pass, via _handle_unrecognized() when no
    known command matches or a matched command's own parsing fails (see that
    function's docstring for why 'leave it queued' is no longer correct
    behavior in this fully-automated architecture)."""
    update_id = msg_row["update_id"]
    text = (msg_row.get("message_text") or "").strip()
    stripped_lower = text.lower()
    try:
        if msg_row.get("message_type") == "photo":
            return _handle_playbook(update_id, msg_row.get("raw_update") or {}) or \
                _handle_unrecognized(update_id, "(תמונה ללא תוכן מזוהה)")
        if stripped_lower == "/journal":
            return _handle_journal(update_id)
        if stripped_lower == "/list":
            return _handle_list(update_id)
        if stripped_lower == "/open":
            return _handle_open(update_id)
        if stripped_lower == "/pending":
            return _handle_pending(update_id)
        if stripped_lower == "/pnl":
            return _handle_pnl(update_id)
        if stripped_lower == "/positions":
            return _handle_position_status(update_id, msg_row.get("raw_update") or {})
        if stripped_lower.startswith("/drop"):
            return _handle_drop(update_id, text) or _handle_unrecognized(update_id, text)
        if stripped_lower.startswith("/exit"):
            return _handle_exit(update_id, text) or _handle_unrecognized(update_id, text)
        if stripped_lower.startswith("/filled"):
            return _handle_filled(update_id, text) or _handle_unrecognized(update_id, text)
        if stripped_lower.startswith("/maxadd"):
            return _handle_maxadd(update_id, text) or _handle_unrecognized(update_id, text)
        if stripped_lower.startswith("/add"):
            return _handle_add(update_id, text) or _handle_unrecognized(update_id, text)
        if stripped_lower.startswith("/equity"):
            return _handle_equity(update_id, text) or _handle_unrecognized(update_id, text)
        if stripped_lower.startswith("/override"):
            return _handle_override(update_id, text) or _handle_unrecognized(update_id, text)
        if stripped_lower.startswith("/setrisk"):
            return _handle_setrisk(update_id, text) or _handle_unrecognized(update_id, text)
        if stripped_lower.startswith("/withdraw"):
            return _handle_withdraw(update_id, text) or _handle_unrecognized(update_id, text)
        if stripped_lower.startswith("/screener"):
            return _handle_screener_batch(update_id, text) or _handle_unrecognized(update_id, text)
        if stripped_lower == "/monitorall":
            return _handle_automonitor(update_id)
        if stripped_lower == "/monitorall_strict":
            return _handle_automonitor(update_id, strict=True)
        if stripped_lower.startswith("/monitor"):
            return _handle_monitor(update_id, text) or _handle_unrecognized(update_id, text)
    except Exception as e:
        _log(f"handler crashed for update_id={update_id} text={text!r}: {e}")
        _mark_failed_unless_already_sent(update_id, str(e))
        return True
    return _handle_unrecognized(update_id, text)


def main() -> None:
    if not _acquire_lock():
        _log("lock held by another instance, exiting (no-op)")
        return
    try:
        persistence.reclaim_stale_processing()
        handled, rounds = 0, 0
        # Self-draining: _dispatch() now always reaches a terminal state
        # (sent/failed) for every message -- including a syntax hint or an
        # "unrecognized command" reply via _handle_unrecognized() -- so there is
        # no more "leave it queued for manual handling" category (2026-07-12
        # reliability fix; see _handle_unrecognized()'s own docstring for the
        # real incident that motivated this). Automatable work can keep arriving
        # while a claude -p call is in flight (the whole point of checking
        # count_pending_messages() instead of a single fixed pass), so the loop
        # keeps claiming until nothing new is left.
        while True:
            claimed = persistence.claim_next_messages()
            if not claimed:
                break
            rounds += 1
            for msg_row in claimed:
                if _dispatch(msg_row):
                    handled += 1
            if persistence.count_pending_messages() == 0:
                break
        _log(f"drain complete after {rounds} round(s): {handled} handled")
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
