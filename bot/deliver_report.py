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
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chart_draw
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
    return size_policy.format_size_lines_he(
        result, target, equity_usd=equity,
        reduction_reason=reason.strip() if isinstance(reason, str) and reason.strip() else None,
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
        grade=decision.get("rubric_grade") or decision.get("grade"),
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
            "grade": decision.get("grade"),
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
        if freshness_warning:
            combined_warnings.append(freshness_warning)
        if breaker_warning:
            combined_warnings.append(breaker_warning)
        if combined_warnings:
            widget_data["warnings"] = combined_warnings

        png = render_widget_png(widget_data)
        pdf_bytes = render_report_pdf(decision["report_markdown"])

        # Save the report to reports/ for the historical record, same convention as
        # every interactively-produced report this session.
        # update_id in the filename avoids overwriting an earlier same-day report if
        # the same ticker is screened more than once in one day (same class of bug
        # found real in deliver_playbook_report.py, 2026-07-09).
        report_filename = f"{ticker.lower()}_screener_{decision.get('date', 'unknown')}_{update_id}.md"
        (PROJECT_ROOT / "reports" / report_filename).write_text(
            decision["report_markdown"], encoding="utf-8"
        )

        resp_photo = send_photo(png, caption=f"{ticker} — {decision['decision']} (Grade {decision.get('grade', '?')})")
        resp_doc = send_document(pdf_bytes, filename=report_filename.replace(".md", ".pdf"),
                                  caption=f"דוח מלא {ticker}")
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
            report_lint.format_warning_block_he(lint_result)
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
            rubric_grade=decision.get("grade"),
            market_regime_at_build=decision.get("market_regime"),
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
