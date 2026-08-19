"""Delivery half of /playbook automation (2026-07-09). Takes a fully-decided,
multi-position portfolio analysis (already read from the screenshot and reasoned
about by whichever Claude session produced it -- Category B) and does the
mechanical rest: persist each position's freshly-recomputed current_stop
(2026-07-11, so it stops going stale after entry -- see persistence.update_current_stop),
render the portfolio widget, deliver, save each position's thesis where
applicable. Makes no judgment calls -- same principle as
deliver_report.py/deliver_monitor_report.py.

Usage: python bot/deliver_playbook_report.py path/to/decision.json
Expected JSON shape:
{
  "update_id": 123456789, "date": "2026-07-09",
  "account_equity_usd": 100000.0,  // optional, 2026-07-18 -- the broker's own total account value read
                                    // off the same screenshot; auto-applied via persistence.set_equity()
                                    // so equity stays fresh without a separate /equity command. Omitted
                                    // entirely if the screenshot didn't show a clear total -- never
                                    // guessed/summed from positions. Any pending withdrawal tracked via
                                    // /withdraw is untouched by this -- persistence.get_effective_equity()
                                    // still subtracts it from whatever this field sets.
  "market_regime": "neutral_choppy",  // optional, 2026-07-20 (rule 23, CONSISTENCY_RULES.md) -- copied
                                       // verbatim from that run's market_regime_formula.regime (STRATEGY_v3.md
                                       // §ב), never a fresh eyeballed call. Surfaced as its own line in the
                                       // Telegram summary. Absent entirely on an older decision JSON --
                                       // report_lint.py's rule-23 check skips silently when missing.
  "market_regime_formula": {"regime": "neutral_choppy", "score": -1, ...},  // optional -- the formula's raw,
                                       // untouched call; must stay present (and differ from market_regime)
                                       // whenever regime_override_reason is used, so the deviation stays visible.
  "regime_override_reason": null,  // optional -- non-empty only for a real, disclosed override (rule 23).
  "positions": [
    {"ticker": "SPY", "sleeve": "core"|"swing", "qty": 78, "avg": 726.10,
     "price": 745.40, "action": "Hold", "stop": 700.91,
     "stop_basis_level": 706.91,  // rule 24, CONSISTENCY_RULES.md (added 2026-07-30 checkup): the raw
                                  // structural low the trailed stop sits below (whatever daily low the
                                  // post-tranche trailing-stop method chose), BEFORE the 0.15x ATR14
                                  // buffer -- `stop` must equal this minus 0.15x atr_at_build or lower.
                                  // report_lint._lint_stop_buffer checks it.
     "targets": [{"price":"...","pct":"...","atr_mult":"...","rr":"...","status":"pass"}],
     // targets[].atr_mult/rr (2026-07-22): whatever the model writes here is
     // NEVER trusted for display -- report_lint.compute_all_target_metrics()
     // always overwrites both fields with the deterministically-computed
     // value (using price/stop/atr_at_build below), for every target, not
     // just ones that turn out wrong. This replaced a recurring real failure
     // (2026-07-20 AMZN/LLY/CRM/UPS, same tickers again 2026-07-22) where the
     // model kept computing these against a fresher current ATR instead of
     // the frozen atr_at_build. Only targets[].price is an actual judgment
     // call the model makes; these two fields exist in the schema only
     // because report_markdown's prose table still needs some text there.
     "atr_at_build": 12.4,  // optional -- item 3, Hardening Pass; falls back to
                            // persistence.get_atr_at_build(ticker) when absent
     "rubric_grade_at_build": "B",  // rules 18/27, added 2026-07-30 full-system checkup -- only
                                     // meaningful when action="Add Only If Confirmed": the thesis's
                                     // original build-time grade, copied verbatim from that ticker's own
                                     // fetch_analysis_data.py open_position.rubric_grade, never invented.
                                     // report_lint._lint_playbook_add_gate flags an Add recommendation
                                     // alongside an F grade here, or alongside a risk_off/structure_break
                                     // market_regime (the shared decision-level field above) -- same
                                     // consistency-check posture as SCREENER_v3's rule 18/27 gates.
     "freshness": {...}},  // optional -- item 6, Hardening Pass; copied verbatim from
                           // that ticker's own fetch_analysis_data.py output
    ...
  ],
  "report_markdown": "# full .md report text",
  "summary_text": "IGNORED since 2026-08-10 -- the message is built here, see below"
}

Per position, the message now reads only: `action` (one of the eight fixed words -- the real
judgment call), `price`, `qty` as read off the screenshot, an optional one-line `sentence`, and
for a Starter `last_bar_close`/`bar_fresh`. Everything else comes from the database: the stop
this run just persisted, the shares still held, the blended entry price, which tranches are
still unsold, the sleeve, and the Starter's original trigger.

2026-08-10: bot/position_text.py holds STRATEGY_v3.md section ח.1's fixed template and this
script fills it. The reconciliation block -- screenshot share count against what is recorded,
and the exact /exit, /add or /filled command that fixes the difference -- is computed here too,
from numbers both sides already have.
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chart_draw
import persistence
import position_text
import regime_formula
import report_lint
from tv_data import TVClient
from widget_render import portfolio_to_widget_data, render_widget_png
from report_pdf import render_report_pdf
from telegram_send import send_text, send_photo, send_document, send_failure_alert

PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def _redraw_all_position_charts() -> tuple[list[str], list[str]]:
    """Redraws every open position's entry / live stop / unsold-target lines,
    one symbol at a time on the single shared chart. Returns (drawn, failed)
    ticker lists for the caller's own logging.

    Added 2026-08-07 as the daily refresh for held tickers. /playbook is the
    right home for it rather than a new nightly job: it already runs once a
    day, it is the run that persists each position's freshly-trailed stop just
    above, and a stop that moved in the DB but not on the chart is precisely
    the drift this is here to close. The pending list needs no equivalent --
    a waiting idea's lines were drawn when its thesis was built and only
    change when it is rebuilt, which redraws them anyway.

    One CDP session for all of them (same reasoning as deliver_report.py's
    _tv_side_effects), and one try/except PER TICKER: a single symbol that
    fails to load must not cost the remaining positions their redraw.
    Positions are read fresh here, not taken from the decision JSON -- the
    stop-persistence loop above may have just moved a stop, and the drawn line
    has to be the stored one, never the model's copy of it."""
    positions = persistence.get_open_positions()
    drawn, failed = [], []
    if not positions:
        return drawn, failed
    async with TVClient() as client:
        for pos in positions:
            ticker = pos.get("ticker")
            if not ticker:
                continue
            try:
                await chart_draw.annotate_position_chart(client, ticker, pos)
                drawn.append(ticker)
            except Exception as e:
                failed.append(ticker)
                print(f"WARNING: chart redraw failed for {ticker}: {e}", file=sys.stderr)
    return drawn, failed


def _format_age_he(updated_at_iso) -> str:
    """2026-07-30 full-system checkup: no report ever showed how old the cash
    figure actually is -- the user has to be able to see "this number is from
    3 days ago" at a glance, not just trust it silently. `updated_at_iso` is
    account_settings.updated_at, set by persistence.set_equity() every time
    it runs (including via bot/update_equity.py, called at the START of a
    /playbook run now -- see process_queue.py's prompt)."""
    if not updated_at_iso:
        return "לא ידוע -- שווי חשבון מעולם לא הוגדר"
    try:
        updated = datetime.fromisoformat(updated_at_iso)
    except ValueError:
        return "לא ידוע"
    age_seconds = (datetime.now(timezone.utc) - updated).total_seconds()
    if age_seconds < 3600:
        return f"לפני {int(age_seconds // 60)} דקות"
    if age_seconds < 86400:
        return f"לפני {int(age_seconds // 3600)} שעות"
    return f"לפני {int(age_seconds // 86400)} ימים"


def _unsold_targets(tranche_plan: dict) -> list:
    """The levels still to be sold into, nearest first.

    Read from the tranche plan rather than from the payload's own target list,
    which does not know what has already been realised. The ASTS incident is
    exactly this: one stored target, sold into twice, because every report kept
    presenting it as still waiting."""
    out = []
    for tranche in (tranche_plan or {}).get("tranches") or []:
        if not isinstance(tranche, dict):
            continue
        if tranche.get("label") == "runner" or tranche.get("status") == "filled":
            continue
        if tranche.get("price") is None:
            continue
        out.append({"price": tranche["price"], "pct": tranche.get("planned_pct")})
    return out


def _mismatch(ticker: str, screenshot_qty, price, position: Optional[dict]) -> Optional[dict]:
    """What the broker screen says against what this system has recorded.

    Computed here rather than described by the model: both numbers are already
    on hand, and the command that fixes it -- with the real share count and
    price already filled in -- is arithmetic, not judgment. The comparison is
    against `remaining_qty`, never `qty`: the original fill size is left
    untouched by design after a partial exit, so comparing against it would
    report a discrepancy on every position that has ever taken profit."""
    screen = position_text._num(screenshot_qty)
    if screen is None:
        return None
    price_num = position_text._num(price)
    price_text = f"{price_num:,.2f}" if price_num is not None else "מחיר"

    if not position:
        return {"ticker": ticker, "kind": "new",
                "command": f"/filled {ticker} {price_text} {screen:,.0f} full"}

    held = position_text._num(position.get("remaining_qty"))
    if held is None or abs(screen - held) < 1:
        return None
    difference = abs(screen - held)
    if screen < held:
        return {"ticker": ticker, "kind": "sold",
                "command": f"/exit {ticker} {price_text} {difference:,.0f}"}
    return {"ticker": ticker, "kind": "added",
            "command": f"/add {ticker} {price_text} {difference:,.0f}"}


def _position_facts(pos: dict, today: str) -> tuple[dict, Optional[dict]]:
    """One position's block inputs, and its mismatch note if there is one.

    Everything that describes the holding is read from the database, which by
    this point already carries this run's freshly trailed stop (the loop above
    persisted it). The payload supplies the action word, the live price, the
    share count read off the screenshot, and at most one sentence."""
    ticker = pos.get("ticker")
    position = persistence.get_open_position(ticker) or {}
    tranche_plan = position.get("tranche_plan") or {}
    setup = position.get("entry_setup")
    setup = setup if isinstance(setup, dict) else {}

    days_since_starter = None
    starter_stale = False
    if position.get("entry_type") == "starter" and position.get("entry_date"):
        try:
            days_since_starter = persistence.count_trading_days(
                str(position["entry_date"])[:10], today)
            starter_stale = days_since_starter >= persistence.STARTER_STALE_TRADING_DAYS
        except Exception:
            days_since_starter = None

    facts = dict(
        ticker=ticker,
        action=pos.get("action"),
        price=pos.get("price"),
        entry_price=position.get("entry_price"),
        qty=position.get("remaining_qty"),
        stop=position.get("current_stop", pos.get("stop")),
        targets=_unsold_targets(tranche_plan),
        sleeve=position.get("sleeve") or pos.get("sleeve"),
        runner_only=(bool(tranche_plan.get("runner_only"))
                     or tranche_plan.get("next_label") == "runner"),
        entry_type=position.get("entry_type"),
        trigger=setup.get("trigger"),
        add_to_full_qty=position.get("add_to_full_qty"),
        last_bar_close=pos.get("last_bar_close"),
        bar_fresh=bool(pos.get("bar_fresh")),
        days_since_starter=days_since_starter,
        starter_stale=starter_stale,
        sentence=pos.get("sentence"),
        warnings=tranche_plan.get("warnings"),
    )
    return facts, _mismatch(ticker, pos.get("qty"), pos.get("price"), position or None)


def _build_summary(decision: dict) -> str:
    """The review's Telegram text, built here rather than copied from the model.

    Anything written into `summary_text` is ignored -- two sources for one
    message is how the wording drifted in the first place. The market state is
    translated through the one table the whole system shares, so the word here
    and the word in every other report can never disagree."""
    today = decision.get("date", "")
    regime = decision.get("market_regime")
    regime_he = regime_formula.regime_name_he(regime or "") or (regime or "")
    override = decision.get("regime_override_reason")
    if override:
        formula_regime = (decision.get("market_regime_formula") or {}).get("regime")
        formula_he = regime_formula.regime_name_he(formula_regime or "") or (formula_regime or "—")
        regime_he = f"{regime_he} (הנוסחה אמרה {formula_he} — {override})"

    positions, mismatches = [], []
    for pos in decision.get("positions") or []:
        if not pos.get("ticker"):
            continue
        facts, mismatch = _position_facts(pos, today)
        positions.append(facts)
        if mismatch:
            mismatches.append(mismatch)

    return position_text.build_playbook_summary(
        date=today, regime_he=regime_he, positions=positions, mismatches=mismatches)


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python bot/deliver_playbook_report.py path/to/decision.json", file=sys.stderr)
        sys.exit(1)

    decision = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    update_id = decision["update_id"]

    try:
        # Persist each position's freshly-recomputed stop (Category B judgment already
        # done by the calling claude -p session) -- makes /open reflect the real trailed
        # stop instead of going stale after entry. Not fatal per-ticker: a portfolio
        # screenshot can include tickers this system never filled through /filled, and
        # one bad ticker shouldn't block delivery of the rest of the report.
        for pos in decision["positions"]:
            ticker, new_stop = pos.get("ticker"), pos.get("stop")
            if not ticker or new_stop is None:
                continue
            try:
                persistence.update_current_stop(ticker, new_stop)
            except ValueError as e:
                # The monotonic guard rejected a lower stop -- the position's real
                # current_stop is UNCHANGED (not silently loosened), but this is
                # worth a loud, greppable log line rather than the generic WARNING
                # below, since it means this run's own computed number disagreed
                # with the real trailed stop already on file.
                print(f"STOP GUARD REJECTED for {ticker}: {e}", file=sys.stderr)
            except Exception as e:
                print(f"WARNING: failed to persist current_stop for {ticker}: {e}", file=sys.stderr)

        # Auto-refresh equity from the same screenshot (2026-07-18) -- keeps
        # account_settings.equity_usd fresh every /playbook run instead of
        # requiring a separate /equity command each time. account_equity_usd
        # is the RAW broker total the model read off the screenshot; any
        # pending withdrawal already tracked via /withdraw is still correctly
        # subtracted downstream by persistence.get_effective_equity() -- this
        # only ever updates the raw figure, never touches pending_withdrawal_usd
        # itself. Not fatal: a missing/invalid figure just skips the update,
        # same "one bad field doesn't block the rest of delivery" principle as
        # the stop-persistence loop above.
        # 2026-07-30 full-system checkup: process_queue.py's /playbook prompt now
        # calls bot/update_equity.py FIRST, before this run's own per-ticker
        # fetch_analysis_data.py calls, so those calls' own cash/heat math already
        # sees the fresh number -- this call here is now just a harmless fallback
        # (idempotent) in case that early call was ever skipped.
        equity_update_note = None
        account_equity_usd = decision.get("account_equity_usd")
        if account_equity_usd is not None:
            try:
                persistence.set_equity(float(account_equity_usd))
                pending = persistence.get_account_settings()["pending_withdrawal_usd"]
                if pending:
                    equity_update_note = (
                        f"💡 שווי חשבון עודכן אוטומטית מהצילום (${account_equity_usd:,.2f}). "
                        f"עדיין רשומה משיכה ממתינה של ${pending:,.2f} -- אם היא כבר בוצעה בפועל "
                        f"בחשבון, שלח /withdraw 0."
                    )
            except (ValueError, TypeError) as e:
                print(f"WARNING: could not auto-update equity from account_equity_usd={account_equity_usd!r}: {e}",
                      file=sys.stderr)

        # 2026-07-30 full-system checkup: always show how old the cash figure
        # actually is, not just when there's a pending-withdrawal note. Read
        # AFTER the refresh attempt above, so this reflects reality: "just now"
        # if a fresh number came in this run, or the real stale age if none did.
        cash_updated_at = persistence.get_account_settings().get("updated_at")
        cash_freshness_line = f"💰 שווי חשבון עודכן לאחרונה: {_format_age_he(cash_updated_at)}"

        # Item 3 (Hardening Pass): deterministic arithmetic re-check on every
        # position's stop/target numbers -- never blocks the send, always logged,
        # always surfaced prominently on failure. One combined lint pass across
        # all positions in this run (one analysis_runs row, ticker=None, same
        # convention as /pending's cross-ticker calls).
        lint_result = report_lint.lint_playbook_decision(decision, atr_lookup=persistence.get_atr_at_build)
        persistence.record_lint_result(None, "STRATEGY_v3", lint_result.to_dict())

        # Always-computed target metrics (2026-07-22 fix -- see
        # report_lint.compute_all_target_metrics's own docstring for the
        # recurring real failure this replaces, same tickers every day since
        # 2026-07-20: the model kept computing a target's stated ATR-distance/
        # R:R against a fresher current ATR instead of the frozen
        # atr_at_build). Unlike the old failing_target_keys/target_corrections
        # pair (still used as-is by deliver_report.py's SCREENER_v3 path, where
        # this staleness can't happen since atr_at_build IS the current ATR at
        # a fresh build), EVERY target here gets its atr_mult/rr overwritten
        # with the deterministically-computed value below -- whether or not
        # the model's own stated number happened to match -- so there is
        # nothing left for the model to get wrong for these two fields.
        all_target_metrics = report_lint.compute_all_target_metrics(
            decision, atr_lookup=persistence.get_atr_at_build
        )
        corrections = {
            key: {"atr_mult": m["atr_mult"], "rr": m["rr"]}
            for key, m in all_target_metrics.items()
        }
        # Fix (2026-07-20, real AMZN/LLY/CRM/UPS incident): the above fix only
        # ever patched this structured dict (which feeds the widget PNG) --
        # report_markdown (which feeds the PDF and the saved .md, arguably the
        # more authoritative "full report") kept showing the exact wrong
        # numbers report_lint already caught. `stated` captures each target's
        # ORIGINAL text before it's overwritten below, since that's the exact
        # substring still sitting in report_markdown's table row -- see
        # report_lint.patch_report_markdown's own docstring. No targets are
        # ever dropped here (rule 3, changed 2026-07-31: an open position's
        # target is never invalidated by a moving current price) -- only the
        # displayed atr_mult/rr get corrected.
        stated_targets = {}
        for pos in decision["positions"]:
            ticker_key, targets = pos.get("ticker"), pos.get("targets")
            if ticker_key and targets:
                for i, t in enumerate(targets, start=1):
                    stated_targets[(ticker_key, i)] = {
                        "price": t.get("price"), "atr_mult": t.get("atr_mult"), "rr": t.get("rr"),
                    }
                    fix = corrections.get((ticker_key, i))
                    if fix:
                        if "atr_mult" in fix:
                            t["atr_mult"] = f"{fix['atr_mult']:.2f}x"
                        if "rr" in fix:
                            t["rr"] = f"{fix['rr']:.2f}"
        if decision.get("report_markdown"):
            decision["report_markdown"] = report_lint.patch_report_markdown(
                decision["report_markdown"], stated_targets, set(), corrections
            )

        # Item 6 (Hardening Pass): each position's own freshness dict is a verbatim
        # pass-through from that ticker's fetch_analysis_data.py call -- one combined
        # warning naming every stale ticker, never blocking the send.
        stale_tickers = [
            pos.get("ticker", "?") for pos in decision["positions"]
            if pos.get("freshness") and not pos["freshness"].get("fresh", True)
        ]
        freshness_warning = (
            f"⚠️ נתונים לא עדכניים עבור: {', '.join(stale_tickers)}" if stale_tickers else None
        )

        widget_data = portfolio_to_widget_data(decision["positions"], decision.get("date", ""))
        # atr_multiple_mismatch/rr_mismatch are excluded from the user-facing
        # text here (2026-07-22): compute_all_target_metrics above already
        # overwrote every target's displayed number with the correct value
        # regardless of what finding fired, so "displayed X but actual Y" is
        # never true anymore by the time the user sees it -- surfacing it
        # would be confusing, stale-by-construction noise. Both checks are
        # still fully present in lint_result.to_dict() for
        # persistence.record_lint_result's audit trail above; only this
        # display step drops them. checkpoint_mislabeled_as_target (a real,
        # still-relevant judgment fact -- this level doesn't qualify as a
        # sellable target) is untouched and still shown.
        combined_warnings = lint_result.warning_lines_he(
            exclude_checks={"atr_multiple_mismatch", "rr_mismatch"}
        )
        if freshness_warning:
            combined_warnings.append(freshness_warning)
        if combined_warnings:
            widget_data["warnings"] = combined_warnings
        png = render_widget_png(widget_data)
        pdf_bytes = render_report_pdf(decision["report_markdown"])

        # update_id in the filename avoids silently overwriting an earlier same-day
        # report if /playbook runs more than once in one day (found real, 2026-07-09:
        # a test run collided with and overwrote that day's actual delivered report).
        report_filename = f"playbook_portfolio_{decision.get('date', 'unknown')}_{update_id}.md"
        (PROJECT_ROOT / "reports" / report_filename).write_text(
            decision["report_markdown"], encoding="utf-8"
        )

        resp_photo = send_photo(png, caption=f"תיק השקעות — {decision.get('date', '')}")
        resp_doc = send_document(pdf_bytes, filename=report_filename.replace(".md", ".pdf"),
                                  caption="דוח מלא — תיק השקעות")
        # 2026-07-20: surface the run's final market-regime call (rule 23,
        # CONSISTENCY_RULES.md -- sourced verbatim from market_regime_formula,
        # never a fresh eyeballed read) at the top of the summary so it's
        # visible without opening the PDF. None on an older decision JSON
        # predating this field -- omitted entirely, never fabricated.
        # The review's own text is built here (2026-08-10), from the stored
        # positions plus this run's figures -- anything the model wrote into
        # `summary_text` is ignored, the same rule the screener, the scan and
        # the two monitor reports already follow. The market state used to be
        # printed here as its raw English token; it is now translated inside
        # the template, through the one table the whole system shares.
        summary_text = (
            report_lint.format_warning_block_he(
                lint_result, exclude_checks={"atr_multiple_mismatch", "rr_mismatch"}
            )
            + (f"{freshness_warning}\n\n" if freshness_warning else "")
            + (f"{equity_update_note}\n\n" if equity_update_note else "")
            + f"{cash_freshness_line}\n\n"
            + _build_summary(decision)
        )
        resp_text = send_text(summary_text)

        if not (resp_photo.get("ok") and resp_doc.get("ok") and resp_text.get("ok")):
            persistence.mark_failed(update_id, "a Telegram send did not confirm ok=True")
            print("FAILED: a Telegram send did not confirm ok=True")
            send_failure_alert("playbook report", "a Telegram send did not confirm ok=True")
            sys.exit(1)

        persistence.mark_sent(update_id, [
            resp_photo.get("result", {}).get("message_id"),
            resp_doc.get("result", {}).get("message_id"),
            resp_text.get("result", {}).get("message_id"),
        ])

        # Daily chart refresh for every held ticker, AFTER delivery is fully
        # confirmed and marked sent -- same non-fatal posture as every other
        # TradingView side effect in this system. A closed TradingView window
        # costs the user some lines, never their report.
        try:
            drawn, failed = asyncio.run(_redraw_all_position_charts())
            print(f"charts redrawn: {len(drawn)} ok, {len(failed)} failed"
                  + (f" ({', '.join(failed)})" if failed else ""))
        except Exception as e:
            print(f"WARNING: position chart redraw pass failed entirely: {e}", file=sys.stderr)

        print(f"OK: playbook delivered ({len(decision['positions'])} positions, update_id={update_id})")
    except Exception as e:
        persistence.mark_failed(update_id, str(e))
        print(f"FAILED: {e}", file=sys.stderr)
        send_failure_alert("playbook report", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
