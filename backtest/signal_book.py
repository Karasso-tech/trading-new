"""Signal book: EVERY trigger that fires is followed to its exit -- no book
cap, no cash cap, no competition for slots. One stream per ticker (a new
signal can only start once the previous one on the same ticker has exited,
same as the plan-rebuild logic everywhere else). Gate-blocked fires (grade D /
hostile regime at fire time) are simulated too, flagged, so the gates
themselves become measurable.

Each signal row carries the full entry-day picture, so wins and losses can be
sliced by any of it later:

  ticker, dates, days_held, final R, pnl-shape (target vs runner), max profit
  reached (in R), worst drawdown reached (in R), exit reasons,
  grade at build + at fire, confidence, regime, RS vs SPY 20d and 5d,
  distance from SMA20 in ATRs, ATR as % of price, volume as % of its 20-day
  average, days to next earnings, trigger, entry, premium of entry over
  trigger, stop distance in ATRs, stop basis, first-target reward:risk at
  build and at the real entry.

Sizing is a flat 1,000 shares per signal purely so tranche rounding behaves;
every comparable number reported is in R, which does not depend on size.

Usage: python backtest/signal_book.py [--years 5] [--seed 42] [--n 50]
Output: backtest/signal_book_<seed>.json + .csv, sorted best R first.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "bot"))
sys.path.insert(0, str(ROOT))

import decision_policy
import indicators_core as ic

import sim_breakout as engine
from sim_portfolio import pick_universe, day_regime

QTY = 1000


def rs5_of(day_bars_c, spy_win_c) -> float | None:
    n = min(len(day_bars_c), len(spy_win_c))
    if n < 6:
        return None
    return ic.relative_strength(day_bars_c[-n:], spy_win_c[-n:], 5).rs_delta_pct


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--years", type=float, default=5)
    parser.add_argument("--tag", default="", help="suffix for the output files")
    args = parser.parse_args()

    tickers = pick_universe(args.n, args.seed)
    spy = engine.load_bars("SPY")
    qqq = engine.load_bars("QQQ")
    spy_ix = {b["date"]: i for i, b in enumerate(spy)}
    qqq_ix = {b["date"]: i for i, b in enumerate(qqq)}

    last_day = date.fromisoformat(spy[-1]["date"])
    sim_start = (last_day - timedelta(days=int(args.years * 365.25))).isoformat()

    # one shared regime series -- identical for every ticker on the same date
    regime_by_date = {}
    for i, b in enumerate(spy):
        if b["date"] >= sim_start and b["date"] in qqq_ix:
            regime_by_date[b["date"]] = day_regime(spy, qqq, i, qqq_ix[b["date"]])

    signals = []
    for t in tickers:
        bars = engine.load_bars(t)
        earnings = engine.load_earnings(t)
        start_i = next((i for i, b in enumerate(bars) if b["date"] >= sim_start), None)
        if start_i is None:
            continue

        position = None
        entry_ctx = None
        pending = None
        for i in range(max(start_i, 60), len(bars)):
            d = bars[i]["date"]
            if d not in regime_by_date:
                continue
            day = engine.Day(bars, i, spy, qqq, spy_ix, qqq_ix, earnings,
                             regime=regime_by_date[d])

            if position is not None:
                if position.process_day(day):
                    signals.append(finish(position, entry_ctx, bars))
                    position, entry_ctx, pending = None, None, None

            if position is None and pending is not None and "blocked" not in pending:
                if day.close > pending["trigger"] and day.close > pending["stop"]:
                    grade_now, rr_now = engine.regrade_at_fire(pending, day)
                    regime_bad = day.regime in decision_policy.BLOCKING_REGIMES
                    gate = "pass"
                    if regime_bad:
                        gate = "blocked_regime_" + day.regime
                    elif grade_now in decision_policy.BLOCKING_GRADES:
                        gate = "blocked_grade_" + grade_now
                    position = engine.Position(pending, day.close, QTY, day.date)
                    entry_ctx = entry_context(pending, day, bars, i, spy, spy_ix,
                                              gate, grade_now, rr_now, t)
                    pending = None

            if position is None:
                plan = engine.build_plan(day)
                pending = plan if (plan and "blocked" not in plan) else None

        if position is not None:
            position.force_close(date.fromisoformat(bars[-1]["date"]), bars[-1]["close"])
            signals.append(finish(position, entry_ctx, bars))
        print(f"  {t}: {sum(1 for s in signals if s['ticker'] == t)} signals")

    signals.sort(key=lambda s: s["r"], reverse=True)
    stem = f"signal_book_{args.seed}" + (f"_{args.tag}" if args.tag else "")
    out_json = ROOT / f"{stem}.json"
    out_json.write_text(json.dumps(signals, indent=1), encoding="utf-8")
    out_csv = ROOT / f"{stem}.csv"
    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(signals[0].keys()))
        w.writeheader()
        w.writerows(signals)
    print(f"\n{len(signals)} signals -> {out_json.name}, {out_csv.name}")


def entry_context(plan, day, bars, i, spy, spy_ix, gate, grade_now, rr_now, t) -> dict:
    upto = bars[:i + 1]
    c = [b["close"] for b in upto]
    v = [b["volume"] for b in upto]
    vol_avg = ic.volume_average(v, 20) if len(v) >= 21 else None
    si = spy_ix[day.date.isoformat()]
    spy_win = spy[max(0, si - engine.INDEX_WINDOW_BARS + 1):si + 1]
    return {
        "ticker": t,
        "entry_date": day.date.isoformat(),
        "gate": gate,
        "grade_build": plan["grade"],
        "grade_fire": grade_now,
        "confidence": plan.get("confidence"),
        "regime": day.regime,
        "rs20": round(day.rs20, 2) if day.rs20 is not None else None,
        "rs5": (lambda x: round(x, 2) if x is not None else None)(
            rs5_of(c, [b["close"] for b in spy_win])),
        "dist_sma20_atr": round(day.dist_sma20_atr, 2),
        "atr_pct": round(day.atr14 / day.close * 100, 2),
        "vol_pct_avg20": round(day.bar["volume"] / vol_avg * 100, 1) if vol_avg else None,
        "earnings_days_out": day.earnings_days_out,
        "trigger": round(plan["trigger"], 4),
        "entry": round(day.close, 4),
        "entry_premium_pct": round((day.close - plan["trigger"]) / plan["trigger"] * 100, 2),
        "stop_dist_atr": round((day.close - plan["stop"]) / day.atr14, 2),
        "stop_basis": plan.get("stop_basis"),
        "n_targets": len(plan["targets"]),
        "rr_t1_build": round((plan["targets"][0]["price"] - plan["trigger"])
                             / (plan["trigger"] - plan["stop"]), 2),
        "rr_t1_fire": round(rr_now, 2),
    }


def finish(position, ctx, bars) -> dict:
    rec = position.to_record()
    ix = {b["date"]: k for k, b in enumerate(bars)}
    i, j = ix[rec["entry_date"]], ix[rec["exit_date"]]
    risk = rec["entry"] - rec["initial_stop"]
    # Best/worst price cover only the bars the trade was actually alive for.
    # Entry is bar i's CLOSE, so bar i's own high and low happened before the
    # position existed and must not count -- the window starts at i+1. Same
    # correction trade_sim.py made at SIM_VERSION 2.1 ("mfe_r/mae_r measured to
    # the exit bar only"); there the entry is a bar's OPEN, so its whole bar
    # does count, which is why the windows differ by one bar.
    alive = bars[i + 1:j + 1]
    if alive:
        mfe = max(b["high"] for b in alive)
        mae = min(b["low"] for b in alive)
    else:
        # entered on the very last bar and force-closed the same day: the trade
        # never saw a bar of its own.
        mfe = mae = rec["entry"]
    days_held = (date.fromisoformat(rec["exit_date"])
                 - date.fromisoformat(rec["entry_date"])).days
    last = rec["exits"][-1] if rec["exits"] else None
    return {
        **ctx,
        "exit_date": rec["exit_date"],
        "days_held": days_held,
        # kept flat on purpose: the export needs the entry stop, the final fill
        # and the reason that closed the position, without walking the exit list
        "initial_stop": rec["initial_stop"],
        "exit_price": round(last["price"], 4) if last else None,
        "exit_reason": last["reason"] if last else None,
        "r": rec["r"],
        "max_r_reached": round((mfe - rec["entry"]) / risk, 2),
        "worst_r_touched": round((mae - rec["entry"]) / risk, 2),
        "hit_target1": any(e["reason"] == "target_1" for e in rec["exits"]),
        "exit_reasons": "|".join(e["reason"] for e in rec["exits"]),
        "runner_r": round(rec["runner_pnl"] / (QTY * risk), 3),
    }


if __name__ == "__main__":
    main()
