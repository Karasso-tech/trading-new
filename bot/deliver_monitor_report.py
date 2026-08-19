"""Delivery half of /monitor automation (2026-07-09). Takes a fully-decided monitor
check (status tier, order if green, mismatch warnings) and does the mechanical
rest: render widget, deliver to Telegram, log the check, auto-close on red (see
2026-07-16 note below on green -- that no longer auto-flips status). Makes no
judgment calls -- same principle as deliver_report.py.

2026-07-16: green used to also auto-flip the thesis to open_position here. It no
longer does -- confirming a real trigger and actually owning the position are two
different events, and only /filled (a real fill the user reports) should ever
write open_position. This script still reports the order/targets as information
for the user to act on manually.

Usage: python bot/deliver_monitor_report.py path/to/decision.json
Expected JSON shape:
{
  "ticker": "CRM", "update_id": 123456789, "date": "2026-07-09",
  "status": "white"|"yellow"|"yellow_plus"|"green"|"red",
  "price": 169.52, "distance_atr": 0.1, "is_active_rejection": false,
  "note": "free text for monitor_log",
  "order": {"type":"...", "price":..., "stop":..., "qty":...} | null,
  "warnings": ["prominent mismatch warning text, if the live trigger/stop deviates
                from the stored thesis beyond tolerance -- MONITOR_v2.md's own rule"],
  "regime_blocked": false,  // Item 4 (Hardening Pass), CONSISTENCY_RULES.md rule 18: true only
                            // when status="green" AND the market regime re-checked live right now
                            // is risk_off/structure_break -- the trigger still fired (a real fact),
                            // but no buy order is issued and the thesis stays 'pending'. Always a
                            // plain boolean Claude already decided, never computed here.
  "regime_at_build": "healthy_uptrend",  // only meaningful (and shown) when regime_blocked=true
  "regime_now": "risk_off",              // ditto -- both regimes named, never just "market is against it"
  "market_regime_formula": "risk_off",   // rule 23 -- regime_formula.py's raw, untouched call, copied
                                          // verbatim from fetch_monitor_data.py; must equal regime_now
                                          // unless regime_override_reason is present and non-empty
  "regime_override_reason": null,        // rule 23 -- required non-empty string whenever regime_now
                                          // differs from market_regime_formula, otherwise omitted/null
  "rubric_blocked": false,   // rule 27 -- true only when the live rubric re-score (rubric_formula_now,
                             // whichever setup triggered) grades D/F -- order/starter fields must be
                             // absent when true (see report_lint._lint_rubric_live_gate)
  "rubric_grade_now": null,        // only meaningful (and shown) when rubric_blocked=true
  "rubric_grade_formula_now": "B", // rule 27, added 2026-07-30 full-system checkup -- the raw grade
                                    // ("A"-"F") from fetch_monitor_data.py's rubric_formula_now for
                                    // whichever setup triggered, copied verbatim, never computed here.
                                    // report_lint._lint_rubric_formula_match cross-checks this against
                                    // rubric_blocked above -- unlike rule 23, no override is allowed:
                                    // a D/F live grade must always block, no exceptions.
  "freshness": {...},  // item 6, Hardening Pass -- copied verbatim from fetch_monitor_data.py's own
                        // "freshness" field, never recomputed here
  "portfolio_heat_after": 0.045,       // rules 19/20/21, added 2026-07-30 full-system checkup -- only
                                        // meaningful (and required) when status="green" and a real order
                                        // is being issued: account.portfolio_heat.heat_pct recomputed to
                                        // include THIS order's own risk_usd. Omit entirely for white/
                                        // yellow/yellow_plus/red (no order, disclosure check skips
                                        // silently, same convention as every other optional field here).
  "portfolio_heat_cap_pct": 0.06,      // copied verbatim from account.portfolio_heat_cap_pct
  "portfolio_heat_disclosed": true,    // true iff the report actually shows the heat warning when
                                        // portfolio_heat_after exceeds the cap -- disclosure-only, never
                                        // blocks the order (same posture as SCREENER_v3's identical field)
  "sector_pct_after": 0.32,            // rule 20 -- this ticker's correlation group's % of the swing book
                                        // including this order's own risk
  "sector_cap_pct": 0.40,              // copied verbatim from account.sector_cap_pct
  "sector_disclosed": true,            // same disclosure-only convention as portfolio_heat_disclosed
  "cash_required_usd": 28500.00,       // rule 21 -- full dollar cost of this order at its computed qty
  "cash_available_usd": 31395.69,      // copied verbatim from account.cash_available_usd
  "cash_usage_warn_pct": 0.30,         // copied verbatim from account.cash_usage_warn_pct
  "cash_usage_disclosed": true,        // same disclosure-only convention -- never blocks or reduces size
  "report_markdown": "# full .md report text",
  "sentence": "ONE plain sentence -- what happened on the chart (green), or what still has to happen
               (every other tier). The only prose this script reads.",
  "setup_used": "primary"|"alternate",   // which setup this check is about: picks the stored levels
                                          // and targets shown, and which half of rubric_formula_now
                                          // is read. Defaults to primary.
  "rubric_formula_now": {...},           // fetch_monitor_data.py's own block, copied VERBATIM
  "summary_text": "IGNORED since 2026-08-10 -- the message is built here, see below"
}

2026-08-10: the Telegram text is no longer copied from the model. bot/monitor_text.py holds
MONITOR_v2.md section ו.1/ו.2's fixed templates and this script fills them from the payload's
copied figures plus what is already written down: the planned levels and targets from the stored
thesis, the live grade and its failing criteria from the copied rubric block, the trigger's age
from the logged checks, and whether this ticker is genuinely held from the positions table. The
order card is shown only when nothing stands against it -- rule 27's grade gate, rule 18's regime
gate, a trigger too old for its stop to still mean anything, or missing numbers each drop it whole
and say which one it was.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chart_draw
import decision_policy
import monitor_text
import persistence
import regime_formula
import report_lint
from tv_data import TVClient
from widget_render import monitor_node_to_widget_data, render_widget_png
from telegram_send import send_text, send_photo, send_failure_alert

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_VALID_STATUSES = ("white", "yellow", "yellow_plus", "green", "red")


async def _draw_monitor_chart(ticker: str, primary_setup: dict, alternate_setup: dict) -> None:
    async with TVClient() as client:
        await chart_draw.annotate_chart(client, ticker, primary_setup, alternate_setup)


async def _draw_position_chart(ticker: str, position: dict) -> None:
    async with TVClient() as client:
        await chart_draw.annotate_position_chart(client, ticker, position)


def _redraw_chart(ticker: str, thesis: Optional[dict]) -> Optional[str]:
    """Picks which line set this ticker gets and draws it. Returns "position",
    "setup" or None (nothing drawn) -- for the caller's log line and for tests.

    A ticker the user HOLDS gets the position lines (entry / live stop / unsold
    targets), never the stored thesis's pre-entry plan. /monitor runs on open
    positions too, and drawing a held ticker's dead Alternate stop beside its
    real one is the NOW incident chart_draw's docstring describes. Checked
    against the DB rather than the thesis's own status field, because the
    positions table is what /filled and /exit actually write.

    Never raises: the monitor check itself is already delivered and logged by
    the time this runs, and a closed TradingView window must not turn a
    delivered report into a failed one."""
    try:
        position = persistence.get_open_position(ticker)
        if position:
            asyncio.run(_draw_position_chart(ticker, position))
            return "position"
        if thesis and thesis.get("primary_setup"):
            asyncio.run(_draw_monitor_chart(ticker, thesis.get("primary_setup"),
                                             thesis.get("alternate_setup")))
            return "setup"
        # An ad-hoc check on a ticker with no stored thesis and no position has
        # nothing to draw -- silently skipped, not a warning.
        return None
    except Exception as e:
        print(f"WARNING: chart draw failed for {ticker}: {e}", file=sys.stderr)
        return None


MISMATCH_TOLERANCE_PCT = 0.01
MISMATCH_TOLERANCE_ATR = 0.3


def _setup_for(thesis: Optional[dict], side: str) -> dict:
    """The stored setup this check is about -- Primary unless the check says
    Alternate, and Primary again when the Alternate is missing. A real check
    routinely fires on the Alternate, and reading the Primary's levels for it
    prints the wrong plan beside the right price."""
    thesis = thesis or {}
    setup = thesis.get(f"{side}_setup") or thesis.get("primary_setup") or {}
    return setup if isinstance(setup, dict) else {}


def _deviation(setup: dict, order: dict, atr_at_build) -> Optional[dict]:
    """MONITOR_v2.md's own tolerance check: has the live entry drifted away
    from the level the thesis planned, by more than max(1%, 0.3x the ATR frozen
    at build time)?

    Measured here rather than claimed in the payload, because both numbers are
    already on hand and a warning that depends on the model noticing a
    difference is a warning that goes missing exactly when the difference is
    real. It never blocks anything -- a confirmed trigger is a confirmed
    trigger; it only asks the reader to look twice."""
    planned = monitor_text._num(setup.get("trigger"))
    actual = monitor_text._num((order or {}).get("price"))
    if planned is None or actual is None:
        return None
    atr = monitor_text._num(atr_at_build) or 0.0
    tolerance = max(abs(planned) * MISMATCH_TOLERANCE_PCT, MISMATCH_TOLERANCE_ATR * atr)
    if abs(actual - planned) <= tolerance:
        return None
    return {"planned": planned, "actual": actual}


def _disclosure_flags(decision: dict) -> list:
    """Which of rules 19/20/21 this order actually crosses, from the figures
    the payload copied out of the account data. Information only -- none of
    them blocks or resizes anything, and a figure that is missing is simply not
    claimed either way."""
    flags = []
    pairs = (("heat", "portfolio_heat_after", "portfolio_heat_cap_pct"),
             ("sector", "sector_pct_after", "sector_cap_pct"))
    for flag, value_key, cap_key in pairs:
        value = monitor_text._num(decision.get(value_key))
        cap = monitor_text._num(decision.get(cap_key))
        if value is not None and cap is not None and value > cap:
            flags.append(flag)
    required = monitor_text._num(decision.get("cash_required_usd"))
    available = monitor_text._num(decision.get("cash_available_usd"))
    warn_pct = monitor_text._num(decision.get("cash_usage_warn_pct"))
    if (required is not None and available and warn_pct is not None
            and required / available > warn_pct):
        flags.append("cash")
    return flags


def _build_summary(decision: dict, thesis: Optional[dict], atr_at_build) -> str:
    """The check's Telegram text, built here rather than copied from the model.

    Everything that is already written down is read from where it is written:
    the planned levels and their targets from the stored thesis, the live grade
    and its failing criteria from the copied rubric block, the trigger's age
    from the logged checks, whether this ticker is actually held from the
    positions table. The model supplies one sentence and the figures it fetched
    this run."""
    ticker = decision["ticker"]
    order = decision.get("order") or {}
    setup = _setup_for(thesis, decision.get("setup_used") or "primary")
    rubric = decision.get("rubric_formula_now") or {}
    scored = rubric.get(decision.get("setup_used") or "primary") if isinstance(rubric, dict) else None
    scored = scored if isinstance(scored, dict) else {}
    age = persistence.get_trigger_fired_age(ticker) or {}

    return monitor_text.build_check_summary(
        ticker=ticker,
        status=decision["status"],
        sentence=decision.get("sentence"),
        price=decision.get("price"),
        trigger=setup.get("trigger"),
        # The stored setup's name first. `order.type` is the ORDER kind ("limit"
        # in every real payload), not one of the six setup names -- reading it
        # first printed "פרטי ההזמנה — limit" on a live WGMI check.
        setup_type=setup.get("type") or order.get("type"),
        entry=order.get("price"),
        stop=order.get("stop"),
        qty=order.get("qty"),
        targets=setup.get("targets"),
        grade_now=scored.get("grade") or decision.get("rubric_grade_formula_now"),
        grade_at_build=(thesis or {}).get("rubric_grade"),
        rubric_blocked=bool(decision.get("rubric_blocked")),
        ungradeable_reason=scored.get("reason"),
        criteria=scored.get("criteria"),
        regime_blocked=bool(decision.get("regime_blocked")),
        regime_at_build_he=regime_formula.regime_name_he(decision.get("regime_at_build") or ""),
        regime_now_he=regime_formula.regime_name_he(decision.get("regime_now") or ""),
        deviation=_deviation(setup, order, atr_at_build),
        disclosure_flags=_disclosure_flags(decision),
        starter_qty=monitor_text.starter_qty_from_planned((thesis or {}).get("planned_qty")),
        has_open_position=persistence.get_open_position(ticker) is not None,
        stale_trading_days=age.get("trading_days") if age.get("stale") else None,
    )


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python bot/deliver_monitor_report.py path/to/decision.json", file=sys.stderr)
        sys.exit(1)

    decision = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    ticker = decision["ticker"]
    update_id = decision["update_id"]
    status = decision["status"]

    if status not in _VALID_STATUSES:
        persistence.mark_failed(update_id, f"invalid status {status!r}")
        print(f"FAILED: invalid status {status!r}", file=sys.stderr)
        sys.exit(1)

    try:
        node = {"status": status, "order": decision.get("order"), "note": decision.get("note", "")}

        # Item 3 (Hardening Pass): deterministic arithmetic re-check -- never blocks
        # the send, always logged, always surfaced prominently on failure.
        atr_at_build = persistence.get_atr_at_build(ticker)
        # The lint checks are about what actually goes out. Since 2026-08-10
        # the message is built here, and an order whose trigger fired days ago
        # is dropped from it -- so the payload lints without those fields, or
        # the stale-trigger check warns about a line nobody will see.
        trigger_age = persistence.get_trigger_fired_age(ticker) or {}
        lint_view = dict(decision, trigger_fired_age=trigger_age) if trigger_age else decision
        if trigger_age.get("stale"):
            lint_view.pop("order", None)
            lint_view.pop("starter_qty", None)
        lint_result = report_lint.lint_monitor_decision(lint_view, atr_at_build=atr_at_build)
        persistence.record_lint_result(ticker, "MONITOR_v2", lint_result.to_dict())

        # Item 4 (Hardening Pass, CONSISTENCY_RULES.md rule 18): a trigger firing
        # during a blocking regime (risk_off/structure_break) is still reported as
        # fact, but no buy order is issued and the thesis stays 'pending' -- the
        # regime check itself is Category B, decided by the claude -p session per
        # SCREENER_v3.md/MONITOR_v2.md and passed through here verbatim as a plain
        # boolean; this script only honors it, never computes it.
        regime_blocked = bool(decision.get("regime_blocked"))
        if regime_blocked and status == "green":
            regime_warning = (
                f"⚠️ הטריגר הופעל אך המצב שוק חוסם פקודה חדשה "
                f"(נבנה ב-{decision.get('regime_at_build', '?')}, כרגע {decision.get('regime_now', '?')}) — "
                f"כלל 18. אין פקודת קנייה, התזה נשארת Pending."
            )
        else:
            regime_warning = None

        # Item 6 (Hardening Pass): verbatim pass-through from fetch_monitor_data.py.
        freshness_warning = report_lint.format_freshness_warning_he(decision.get("freshness"))

        # Item 8 (Hardening Pass): circuit breaker -- standing warning only, never a block.
        breaker = persistence.circuit_breaker_status()
        breaker_warning = (
            f"🛑 {breaker['streak']} סטופים רצופים — שקול הפסקה" if breaker["tripped"] else None
        )

        widget_data = monitor_node_to_widget_data(ticker, node, decision.get("date", ""))
        combined_warnings = list(decision.get("warnings") or []) + lint_result.warning_lines_he()
        if freshness_warning:
            combined_warnings.append(freshness_warning)
        if regime_warning:
            combined_warnings.append(regime_warning)
        if breaker_warning:
            combined_warnings.append(breaker_warning)
        if combined_warnings:
            widget_data["warnings"] = combined_warnings
        png = render_widget_png(widget_data)

        if decision.get("report_markdown"):
            # update_id in the filename -- /monitor is checked repeatedly through the
            # day BY DESIGN, so same-day collisions here are the normal case, not an
            # edge case (same bug class found real in deliver_playbook_report.py).
            report_filename = f"{ticker.lower()}_monitor_{decision.get('date', 'unknown')}_{update_id}.md"
            (PROJECT_ROOT / "reports" / report_filename).write_text(
                decision["report_markdown"], encoding="utf-8"
            )

        resp_photo = send_photo(png, caption=f"{ticker} — {status}")
        # Market-condition awareness line, on TOP (2026-08-03, user's request).
        # One line here rather than /screener's full block: this report gets
        # read fast and often, and a four-line block on every one trains the
        # eye to skip it. `regime_now` is the value this check actually used;
        # the formula's raw call is the fallback when it's absent.
        regime_line = regime_formula.regime_headline_he(
            decision.get("regime_now") or decision.get("market_regime_formula") or ""
        )
        # The decision sign (2026-08-03, user's request), read from the STORED
        # thesis, never from this check's own JSON: which of the four decisions
        # /screener landed on is a fact already written down, and re-asking the
        # model for it here would be a second opinion that can disagree with the
        # DB. An ad-hoc check with no stored thesis (MONITOR_v2.md section ד)
        # gets no sign line at all rather than a made-up one. The line names its
        # own source ("what the thesis decided"), so a live 🟢 beside a stored
        # 👁 reads as two facts, not a contradiction.
        stored_thesis = persistence.get_thesis(ticker)
        decision_line = (
            decision_policy.decision_line(stored_thesis.get("decision"), status)
            if stored_thesis else ""
        )
        # The check's own text is built from the stored thesis and this run's
        # figures (2026-08-10) -- anything the model wrote into `summary_text`
        # is ignored, the same rule the screener and the scan already follow.
        # The regime block, when it applies, is part of that template now, so
        # it is not repeated here; it still rides along on the widget image.
        summary_text = (
            (f"{regime_line}\n\n" if regime_line else "")
            + (f"{decision_line}\n\n" if decision_line else "")
            + report_lint.format_warning_block_he(lint_result)
            + (f"{freshness_warning}\n\n" if freshness_warning else "")
            + (f"{breaker_warning}\n\n" if breaker_warning else "")
            + _build_summary(decision, stored_thesis, atr_at_build)
        )
        resp_text = send_text(summary_text)

        if not (resp_photo.get("ok") and resp_text.get("ok")):
            persistence.mark_failed(update_id, "a Telegram send did not confirm ok=True")
            print("FAILED: a Telegram send did not confirm ok=True")
            send_failure_alert(f"{ticker} monitor check", "a Telegram send did not confirm ok=True")
            sys.exit(1)

        # Mandatory per MONITOR_v2.md: log every check.
        persistence.log_monitor_check(
            ticker, status, price=decision.get("price"),
            distance_atr=decision.get("distance_atr"),
            is_active_rejection=decision.get("is_active_rejection", False),
            note=decision.get("note"),
        )
        # 2026-08-08, the user's request: record what this idea was CALLED at the
        # moment it confirmed. Same call as the batch path -- a manual /monitor
        # is just as valid a first sighting as the overnight scan, and whichever
        # runs first wins (the write is idempotent per build of the thesis).
        if status == "green":
            persistence.record_decision_transition(
                ticker, status=status, price=decision.get("price"),
                trigger_price=persistence.get_stored_trigger(ticker),
                rubric_grade_now=decision.get("rubric_grade_formula_now"),
            )
        # 2026-07-16: green no longer auto-flips the thesis to open_position, even
        # unblocked -- confirming a real trigger and executing a real trade are two
        # different events now. The user decides whether to actually buy, and only
        # /filled (persistence.create_position) marks a thesis open_position, after
        # a real fill. red still auto-closes: that's invalidating an idea nobody
        # has entered yet (no capital at risk), not claiming a trade happened.
        if status == "red":
            persistence.set_status(ticker, "closed")

        # 2026-07-13: re-draw this ticker's levels on the live chart, same
        # non-fatal principle as deliver_report.py's TradingView side-effects --
        # the check itself is already fully delivered and logged above, and a
        # chart-drawing failure must never flip this to failed (_redraw_chart
        # swallows its own errors for exactly that reason).
        #
        # /monitor's own decision JSON has no setup data (only order/price/
        # status), so the pre-entry line set comes from the stored thesis --
        # purely mechanical, no LLM involvement. `stored_thesis` is the row
        # already read above for the decision sign; nothing between here and
        # there rewrites its setups (log_monitor_check writes monitor_log,
        # set_status touches status only). Which of the two line sets actually
        # gets drawn is _redraw_chart's call -- see its docstring.
        _redraw_chart(ticker, stored_thesis)

        persistence.mark_sent(update_id, [
            resp_photo.get("result", {}).get("message_id"),
            resp_text.get("result", {}).get("message_id"),
        ])
        print(f"OK: {ticker} monitor check delivered (update_id={update_id})")
    except Exception as e:
        persistence.mark_failed(update_id, str(e))
        print(f"FAILED: {e}", file=sys.stderr)
        send_failure_alert(f"{ticker} monitor check", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
