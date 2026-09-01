"""Delivery half of /monitorall automation (2026-07-11). Takes a batch of
already-decided monitor checks (one per pending ticker, MONITOR_v2.md status tier
already applied by whichever claude -p session produced it) and does the mechanical
rest: log every check (mandatory per MONITOR_v2.md, same as single-ticker /monitor),
auto-close on red, and send ONE consolidated Telegram summary -- deliberately not a
full report per ticker (that's what /monitor TICKER is still for) so a scan with
nothing active doesn't spam N messages. Makes no judgment calls, same principle as
deliver_monitor_report.py.

2026-07-16: green used to also auto-flip the thesis to open_position here. It no
longer does, for the same reason as deliver_monitor_report.py's identical change --
see that script's docstring. This one runs fully unattended overnight
(trigger_auto_monitor.py), so it mattered even more here: only /filled, a real fill
the user reports, should ever write open_position.

2026-08-10: the per-ticker line is no longer copied from the model. bot/monitor_text.py
holds MONITOR_v2.md section ו.3's fixed templates and this script builds each line from
the numbers in the payload plus the stored thesis. Anything written into `headline` is
ignored -- two sources for one message is exactly how the format drifted. The model
supplies one plain `sentence` per ticker and nothing else in prose.

2026-08-11: a ticker whose line would carry no order and no Starter -- no live
grade, a blocked/D grade, or a trigger that fired days ago -- no longer gets a
block at all. All three say the same thing to the reader (price crossed the
level, nothing to place), and twice a day they crowded out the lines that were
actually actionable. They are named in one line at the bottom instead, and
everything else about them -- the check, the log row, the lint, their place on
the waiting list -- is unchanged. /monitor TICKER still shows the full picture.

Usage: python bot/deliver_auto_monitor_report.py path/to/decision.json
Expected JSON shape:
{
  "update_id": 123456789, "date": "2026-07-11",
  "results": [
    {"ticker": "PLTR", "status": "white"|"yellow"|"yellow_plus"|"green"|"red",
     "price": 130.5, "distance_atr": 0.3, "is_active_rejection": false,
     "note": "free text for monitor_log",
     "sentence": "ONE plain sentence saying what actually happened -- the only prose this script asks
                   for. Everything else in the ticker's line is built by bot/monitor_text.py from the
                   numbers below. Required when status is green or yellow_plus.",
     "setup_used": "primary"|"alternate",   // which setup this check is about; picks which half of
                                             // rubric_formula_now is read. Defaults to primary.
     "rubric_formula_now": {...},           // fetch_monitor_data.py's own block, copied VERBATIM (both
                                             // setups). The live grade, the failing criteria and the
                                             // "cannot be scored" reason are all read out of it -- same
                                             // copy-don't-retype convention as market_regime_formula.
     "order": {"type":"...", "price":..., "stop":..., "qty":...} | null,  // only meaningful for green
     "regime_blocked": false, "regime_now": "risk_off", "market_regime_formula": "risk_off",
     "regime_override_reason": null,   // rule 18/23 -- same fields/meaning as deliver_monitor_report.py's
                                        // single-ticker schema, see that docstring for the full contract
     "rubric_blocked": false, "rubric_grade_now": null,
     "rubric_grade_formula_now": "B"   // rule 27 (added 2026-07-30 full-system checkup) -- this batch
                                        // path previously ran NO safety checks at all; every field on
                                        // this line is now cross-checked exactly like single-ticker
                                        // /monitor, see report_lint.lint_monitor_decision
    },
    ...
  ]
}
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision_policy
import monitor_text
import persistence
import regime_formula
import report_lint
from telegram_send import send_text, send_failure_alert

_VALID_STATUSES = ("white", "yellow", "yellow_plus", "green", "red")
_ACTIVE_STATUSES = ("green", "yellow_plus")


def _rubric_block(result: dict) -> dict:
    """The live rubric figures for whichever setup this check is about.

    fetch_monitor_data.py returns `rubric_formula_now` as {"primary": ...,
    "alternate": ...}, each either a real {grade, score, criteria} or a
    {"grade": None, ..., "reason": "..."} -- explicit null with a stated reason,
    never a silent gap. Copied verbatim into the decision JSON, it is the one
    place the live grade, the failing criteria and the "cannot be scored"
    reason all come from, so none of the three can be retyped wrong.

    An older payload that carries no rubric_formula_now at all still works: the
    grade falls back to the flat `rubric_grade_formula_now` field, and the
    criteria list comes back missing rather than invented."""
    block = result.get("rubric_formula_now") or {}
    side = result.get("setup_used") or "primary"
    scored = block.get(side) if isinstance(block, dict) else None
    if not isinstance(scored, dict):
        scored = {}
    return {
        "grade": scored.get("grade") or result.get("rubric_grade_formula_now"),
        "criteria": scored.get("criteria"),
        "reason": scored.get("reason"),
    }


# The one line that stands in for every hidden ticker (2026-08-11). Named
# rather than counted only: a ticker that silently stops appearing reads as a
# ticker that stopped existing, and the whole point of hiding these is to be
# less noisy, not less honest.
ORDERLESS_LINE = ("🔕 <b>עברו את רמת הקנייה אבל אין מהם פקודה ({count})</b>: {tickers}\n"
                   "לא הצגתי אותם כאן. לראות למה: <code>/monitor {example}</code>")


def _add_lint_warning(bucket: list, ticker: str, status: str, lint_result) -> None:
    """A ticker with no block of its own still has to report a failed
    verification -- otherwise the only place a wrong self-reported field would
    have surfaced is a block that is no longer printed. Kept in its own bucket,
    never in `headlines`, so a warning can never flip the "nothing active"
    summary into a false active one."""
    if lint_result.ok:
        return
    warning_lines = lint_result.warning_lines_he()
    if warning_lines:
        bucket.append(f"⚠️ <b>{ticker}</b> ({status}) — בדיקת אימות נכשלה: "
                       + "; ".join(warning_lines))


def _stored_trigger(thesis: dict | None, side: str):
    """The stored trigger of the setup this check is actually about.

    persistence.get_stored_trigger only ever reads the Primary, and a real scan
    routinely fires on the Alternate -- HOOD's 2026-08-07 line confirmed the
    Failed Breakdown at 89.00 while the Primary's own trigger sat at 95.60, so
    a Primary-only read prints the wrong level next to the right price.

    A trigger saved as free text (26 of 55 real theses once were) comes back
    None and is said out loud by the template, never parsed out of a sentence."""
    thesis = thesis or {}
    setup = thesis.get(f"{side}_setup") or thesis.get("primary_setup") or {}
    value = setup.get("trigger") if isinstance(setup, dict) else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _build_headline(result: dict, *, stored: dict | None, age: dict) -> str:
    """One ticker's block, built here rather than taken from the model.

    Two sources for one message is how the format drifted in the first place,
    so whatever the model wrote into `headline` is ignored -- the only prose
    read out of the payload is its one `sentence`.

    A trigger that first confirmed days ago is reported as a fact but carries
    no order (MONITOR_v2.md's stale-trigger rule): by then price has moved away
    from the level the stop was sized against, so the order that looks like the
    plan is a worse trade than the one that was planned."""
    ticker = result["ticker"]
    order = result.get("order") or {}
    rubric = _rubric_block(result)
    thesis = stored or {}
    return monitor_text.build_scan_headline(
        ticker=ticker,
        status=result["status"],
        sentence=result.get("sentence"),
        price=result.get("price"),
        trigger=_stored_trigger(stored, result.get("setup_used") or "primary"),
        grade_now=rubric["grade"],
        grade_at_build=thesis.get("rubric_grade"),
        ungradeable_reason=rubric["reason"],
        criteria=rubric["criteria"],
        rubric_blocked=bool(result.get("rubric_blocked")),
        entry=order.get("price"),
        stop=order.get("stop"),
        qty=order.get("qty"),
        starter_qty=monitor_text.starter_qty_from_planned(thesis.get("planned_qty")),
        stale_trading_days=age.get("trading_days") if age.get("stale") else None,
    )


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python bot/deliver_auto_monitor_report.py path/to/decision.json", file=sys.stderr)
        sys.exit(1)

    decision = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    update_id = decision["update_id"]
    results = decision.get("results", [])

    try:
        headlines = []
        lint_only_warnings = []
        orderless = []
        for r in results:
            ticker, status = r["ticker"], r["status"]
            if status not in _VALID_STATUSES:
                raise ValueError(f"{ticker}: invalid status {status!r}")

            # Mandatory per MONITOR_v2.md: log every check -- identical bookkeeping
            # to deliver_monitor_report.py's single-ticker path.
            persistence.log_monitor_check(
                ticker, status, price=r.get("price"),
                distance_atr=r.get("distance_atr"),
                is_active_rejection=r.get("is_active_rejection", False),
                note=r.get("note"),
            )
            # 2026-07-16: green no longer auto-flips to open_position here either --
            # see deliver_monitor_report.py's identical change for why. This matters
            # even more for this script specifically: it's the one that runs fully
            # unattended overnight (trigger_auto_monitor.py), so a real green here
            # would otherwise write open_position while nobody's watching, before
            # the user has decided anything or placed a real order. Only /filled
            # does that now, after an actual fill. red still auto-closes (no
            # capital at risk on an idea nobody has entered).
            if status == "red":
                persistence.set_status(ticker, "closed")

            # 2026-08-08, the user's request: write down what this idea was
            # CALLED at the moment it confirmed. thesis.decision is one mutable
            # column that the next rebuild overwrites in place, so "it was a
            # Watchlist when it fired" only exists while it is happening.
            # Idempotent -- this same green is seen again every night until the
            # idea is filled or dropped.
            if status == "green":
                persistence.record_decision_transition(
                    ticker, status=status, price=r.get("price"),
                    trigger_price=persistence.get_stored_trigger(ticker),
                    rubric_grade_now=r.get("rubric_grade_formula_now"),
                )

            # Found in the 2026-07-30 full-system checkup: this batch path ran NO
            # lint checks at all, unlike single-ticker /monitor -- an overnight,
            # fully unattended run had zero backstop against exactly the kind of
            # self-reported-but-wrong field (regime gate, rubric gate) rule 18/27
            # exist to catch. Same lint_monitor_decision call as
            # deliver_monitor_report.py, once per ticker in the batch.
            atr_at_build = r.get("atr_at_build") or persistence.get_atr_at_build(ticker)
            # The lint checks are about what actually goes out. Since 2026-08-10
            # this script builds the message itself, so the payload it lints is
            # given the REAL trigger age (from monitor_log, not self-reported)
            # and, when that age makes an order unshowable, loses the order
            # fields the message will not print -- otherwise the stale-trigger
            # check warns about a line nobody sees.
            age = persistence.get_trigger_fired_age(ticker) or {}
            lint_view = dict(r, trigger_fired_age=age) if age else r
            if age.get("stale"):
                lint_view.pop("order", None)
                lint_view.pop("starter_qty", None)
            lint_result = report_lint.lint_monitor_decision(lint_view, atr_at_build=atr_at_build)
            persistence.record_lint_result(ticker, "MONITOR_v2", lint_result.to_dict())

            # A trigger that fired but yields nothing to place is named at the
            # bottom, not shown as a block (2026-08-11, the user's call). The
            # three cases -- no grade at all, a blocked/D grade, a trigger that
            # fired days ago -- all end the same way for the reader: nothing to
            # do. They stay fully logged, linted and on the waiting list; only
            # the five-line block goes away. Asked of monitor_text so the test
            # for "would this line carry an order" lives next to the templates
            # that decide it, and cannot drift from them.
            if status in _ACTIVE_STATUSES and monitor_text.scan_line_is_orderless(
                    status=status,
                    grade_now=_rubric_block(r)["grade"],
                    rubric_blocked=bool(r.get("rubric_blocked")),
                    stale_trading_days=age.get("trading_days") if age.get("stale") else None):
                orderless.append(ticker)
                _add_lint_warning(lint_only_warnings, ticker, status, lint_result)
                continue

            if status in _ACTIVE_STATUSES:
                # The decision sign (2026-08-03, user's request). Read from the
                # STORED thesis, not from this batch's JSON: this scan runs
                # unattended overnight, and asking the model to restate a
                # decision that is already written in the DB is exactly how the
                # two drift apart. The headline is the ONLY thing the user sees
                # for this ticker (MONITOR_v2.md section ו.3), so the sign has
                # to ride along with it, on its own first line -- it names its
                # own source, so a 🟢 headline under a stored 👁 reads as two
                # separate facts. A ticker with no stored thesis (an ad-hoc
                # entry in the batch) gets no sign rather than a guessed one.
                stored = persistence.get_thesis(ticker)
                headline = _build_headline(r, stored=stored, age=age)
                if stored:
                    headline = (f"{decision_policy.decision_line(stored.get('decision'), status)}"
                                f"\n{headline}")
                warning_block = report_lint.format_warning_block_he(lint_result)
                headlines.append(f"{warning_block}{headline}" if warning_block else headline)
            else:
                # No headline shown for white/yellow/red -- a lint failure here
                # would otherwise be completely invisible.
                _add_lint_warning(lint_only_warnings, ticker, status, lint_result)

        date = decision.get("date", "")
        # Market-condition awareness line, on TOP (2026-08-03, user's request).
        # This scan is the one real decision moment of the day, so the line
        # belongs above the tickers, not buried under them. Every ticker in a
        # scan shares the same market, so the first result carrying a regime
        # is authoritative for the whole message -- a per-ticker copy would be
        # the same string repeated N times.
        regime_line = ""
        for r in results:
            candidate = r.get("regime_now") or r.get("market_regime_formula") or ""
            if isinstance(candidate, dict):
                candidate = candidate.get("regime") or ""
            regime_line = regime_formula.regime_headline_he(candidate)
            if regime_line:
                break
        header = f"{regime_line}\n\n" if regime_line else ""
        if headlines:
            body = "\n\n".join(headlines)
            summary = f"{header}🔔 <b>סריקה אוטומטית — {date}</b> ({len(results)} טיקרים נבדקו)\n\n{body}"
        else:
            # Worded off what was actually found: with hidden tickers in hand,
            # "no trigger is active" is not true -- triggers fired, they just
            # produced nothing to place, and the line below says exactly that.
            nothing = ("אין פקודה פעילה כרגע." if orderless
                       else "שום טריגר לא פעיל כרגע.")
            summary = (
                f"{header}🔍 <b>סריקה אוטומטית הושלמה — {date}</b>\n"
                f"{len(results)} טיקרים נבדקו — {nothing}"
            )
        if orderless:
            summary += "\n\n" + ORDERLESS_LINE.format(
                count=len(orderless), tickers=", ".join(orderless), example=orderless[0])
        if lint_only_warnings:
            summary += "\n\n" + "\n".join(lint_only_warnings)

        resp = send_text(summary)
        if not resp.get("ok"):
            persistence.mark_failed(update_id, "send_text did not confirm ok=True")
            print("FAILED: send_text did not confirm ok=True")
            send_failure_alert("auto-monitor scan", "send_text did not confirm ok=True")
            sys.exit(1)

        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        print(f"OK: auto-scan delivered ({len(results)} tickers, {len(headlines)} active, update_id={update_id})")
    except Exception as e:
        persistence.mark_failed(update_id, str(e))
        print(f"FAILED: {e}", file=sys.stderr)
        send_failure_alert("auto-monitor scan", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
