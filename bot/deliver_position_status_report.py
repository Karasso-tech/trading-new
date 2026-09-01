"""Delivery half of /positions (2026-07-16). Takes a batch of already-decided
per-position status headlines (one per currently-open position, STRATEGY_v3
judgment already applied by whichever claude -p session produced it) and
sends ONE consolidated Telegram summary -- no photo, no PDF, unlike
/playbook's full report. Unlike deliver_auto_monitor_report.py (which only
surfaces "active" tickers), EVERY open position gets a line here -- that's
the point of a status check, not just an alert feed.

Deliberately informational only: makes no judgment calls and, unlike
deliver_playbook_report.py, never calls persistence.update_current_stop. This
runs unattended twice a day (trigger_position_status.py) with no user in the
loop to catch a bad computed stop before it would be written -- only
/filled, /exit, and the user-triggered /playbook mutate position state today.

2026-08-10: each position's line is no longer copied from the model. bot/position_text.py
holds STRATEGY_v3.md section ח.2's fixed template and this script builds every line from
the payload's copied figures plus the position as the database actually records it -- the
stop the system holds, the shares still held after partial exits, the blended entry price,
the next unsold tranche. Anything written into `headline` is ignored. The model supplies
the action word, the live price and ATR, and (only when there is one) a single sentence.

Usage: python bot/deliver_position_status_report.py path/to/decision.json
Expected JSON shape:
{
  "update_id": 123456789, "date": "2026-07-16", "run_label": "midday"|"eod"|"manual",
  "results": [
    {"ticker": "PLTR",
     "action": "Hold"|"Hold With Alert"|"Trim"|"Sell Partial"|"Exit"|"Protect With Stop"|
               "Add Only If Confirmed"|"No Action",   // the one real judgment call per position
     "atr14": 3.21,          // copied from this ticker's fetch_analysis_data.py output; the three
                             // status lines are chosen against it, here, by comparing distances
     "sentence": "one plain line, ONLY when there is something real to say",   // optional
     "last_bar_close": 25.10, "bar_fresh": true,   // starter positions only -- the two facts the
                             // confirmation rule is made of. The rule itself is applied here.
     "price": 24.50, "stop": 22.00, "entry_date": "2026-07-10"  // added 2026-07-30 full-system checkup --
                  // real numbers the model already has from that ticker's own fetch_analysis_data.py
                  // output (current_stop, open_position.entry_date), copied verbatim. Used to compute a
                  // deterministic "stop distance in dollars / days in trade" line appended to the
                  // headline HERE, in code -- never left to the model's own arithmetic, same "recompute
                  // it deterministically" principle as every other report in this project. This was the
                  // thinnest, least-verified report in the whole system before this: no dollar distance,
                  // no days-in-trade, and (see report_lint.lint_position_status_decision) zero lint
                  // coverage of any kind.
    },
    ...
  ]
}
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import persistence
import position_text
import report_lint
from telegram_send import send_text, send_failure_alert

_HEADER_BY_LABEL = {
    "midday": "📊 <b>סטטוס פוזיציות פתוחות — אמצע היום</b>",
    "eod": "📊 <b>סטטוס פוזיציות פתוחות — סוף היום</b>",
    "manual": "📊 <b>סטטוס פוזיציות פתוחות</b>",
}


def _stop_for(result: dict, position: dict):
    """The stop this position really carries. The database is the source: it is
    what /filled, /exit and /playbook write and what the R-multiple is measured
    against, so a stop retyped into the payload can only ever agree with it or
    be wrong. The payload's own value is the fallback for a ticker this system
    has no open position row for."""
    stored = position.get("current_stop")
    return stored if stored is not None else result.get("stop")


def _build_line(result: dict, position: dict, today: str) -> str:
    """One position's block, built here rather than taken from the model.

    Every figure that describes the position itself is read from the database,
    not from the payload: the shares still held after partial exits, the
    blended entry price, the stop the system holds, and which tranche is
    genuinely next. That last one is the ASTS lesson -- a single stored target
    was sold into twice, because nothing checked whether it had already been
    spent before calling it the approaching target."""
    ticker = result["ticker"]
    tranche = position.get("tranche_plan") or {}
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
            days_since_starter = None   # a malformed date drops the age, never the line

    return position_text.build_position_status(
        ticker=ticker,
        action=result.get("action"),
        price=result.get("price"),
        entry_price=position.get("entry_price"),
        qty=position.get("remaining_qty"),
        stop=_stop_for(result, position),
        target=tranche.get("next_price"),
        atr14=result.get("atr14"),
        sleeve=position.get("sleeve"),
        runner_only=bool(tranche.get("runner_only")) or tranche.get("next_label") == "runner",
        tranches=tranche.get("tranches"),
        entry_type=position.get("entry_type"),
        trigger=setup.get("trigger"),
        add_to_full_qty=position.get("add_to_full_qty"),
        last_bar_close=result.get("last_bar_close"),
        bar_fresh=bool(result.get("bar_fresh")),
        days_since_starter=days_since_starter,
        starter_stale=starter_stale,
        sentence=result.get("sentence"),
        warnings=tranche.get("warnings"),
    )


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python bot/deliver_position_status_report.py path/to/decision.json", file=sys.stderr)
        sys.exit(1)

    decision = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    update_id = decision["update_id"]
    results = decision.get("results", [])
    header = _HEADER_BY_LABEL.get(decision.get("run_label"), _HEADER_BY_LABEL["manual"])

    try:
        if results:
            for r in results:
                # `headline` is no longer read at all (2026-08-10) -- the line
                # is built here. A ticker with no action word still gets a line,
                # visibly marked as having none; a result with no ticker has
                # nothing to build from and is a real failure.
                if not r.get("ticker"):
                    raise ValueError(f"result missing ticker: {r!r}")

            # 2026-07-30 full-system checkup: this report used to run zero lint
            # checks of any kind (every other deliver_*.py script runs at least
            # one). Verifies the new required price/stop/entry_date fields are
            # actually present -- a missing-field finding here, not a mismatch,
            # since the numbers below are computed deterministically, not
            # claimed by the model.
            lint_result = report_lint.lint_position_status_decision(decision)
            persistence.record_lint_result(None, "STRATEGY_v3_positions", lint_result.to_dict())

            today = decision["date"]
            lines = []
            for r in results:
                position = persistence.get_open_position(r["ticker"]) or {}
                headline = _build_line(r, position, today)
                price, stop = r.get("price"), _stop_for(r, position)
                entry_date = position.get("entry_date") or r.get("entry_date")
                extra_bits = []
                if isinstance(price, (int, float)) and isinstance(stop, (int, float)):
                    extra_bits.append(f"מרחק לסטופ: ${abs(price - stop):,.2f}")
                if entry_date:
                    try:
                        days = persistence.count_trading_days(entry_date, today)
                        extra_bits.append(f"{days} ימי מסחר בפוזיציה")
                    except Exception:
                        pass  # a malformed date must never break the whole report over one ticker
                if extra_bits:
                    headline = f"{headline}\n<i>{' · '.join(extra_bits)}</i>"
                lines.append(headline)
            body = "\n\n".join(lines)
            summary = f"{header}\n\n{report_lint.format_warning_block_he(lint_result)}{body}"
        else:
            summary = f"{header}\nאין פוזיציות פתוחות כרגע."

        resp = send_text(summary)
        if not resp.get("ok"):
            persistence.mark_failed(update_id, "send_text did not confirm ok=True")
            print("FAILED: send_text did not confirm ok=True")
            send_failure_alert("position status report", "send_text did not confirm ok=True")
            sys.exit(1)

        persistence.mark_sent(update_id, [resp.get("result", {}).get("message_id")])
        print(f"OK: position status delivered ({len(results)} positions, update_id={update_id})")
    except Exception as e:
        persistence.mark_failed(update_id, str(e))
        print(f"FAILED: {e}", file=sys.stderr)
        send_failure_alert("position status report", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
