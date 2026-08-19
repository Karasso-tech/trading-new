"""Portfolio backtest: N random S&P 500 stocks through the SAME per-ticker
engine as sim_breakout.py (live system code: setup_classifier, level_picker,
rubric_formula, regime_formula, decision_policy), with a book-level cap.

Pre-registered mechanics on top of sim_breakout.py's (owner, 2026-08-11):
  * 50 tickers drawn at random with a FIXED seed (recorded in the output).
  * Max 6 positions open at once. One position per ticker, ever, at a time.
  * Each position risks 1% of CURRENT total equity (cash + open positions
    marked at that day's close).
  * When more triggers fire on one day than there are free slots: better
    build-grade enters first (A, then B, then C), ties alphabetical. The
    skipped fires are recorded; their plans stay live for the next day.
  * Cash is a hard cap -- a position is never bought with money the book
    does not have. Partial-exit proceeds return to cash the day they happen.
  * Everything else identical to the one-stock run: plan built on day t can
    fire day t+1 onward on a settled close above trigger, entry at the fire
    day's close, stop before targets, gap fills at the open, trail only
    after target 1, force-close at the last bar.

Usage: python backtest/sim_portfolio.py [--n 50] [--seed 42] [--years 5]
Output: printed summary + backtest/results_portfolio_<seed>.json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "bot"))
sys.path.insert(0, str(ROOT))

import decision_policy
import regime_formula

import sim_breakout as engine

DATA = ROOT / "data"
START_EQUITY = 100_000.0
RISK_PCT = 0.01
GRADE_RANK = {"A": 0, "B": 1, "C": 2, None: 3}


def pick_universe(n: int, seed: int) -> list[str]:
    data = json.loads((DATA / "snpdata.json").read_text(encoding="utf-8"))
    have_bars = {p.stem for p in (DATA / "bars").glob("*.json")
                 if not p.stem.startswith("_")}
    pool = sorted(t for t in data["tickers"] if t in have_bars)
    return sorted(random.Random(seed).sample(pool, n))


def day_regime(spy, qqq, si, qi) -> str:
    spy_win = spy[max(0, si - engine.INDEX_WINDOW_BARS + 1):si + 1]
    qqq_win = qqq[max(0, qi - engine.INDEX_WINDOW_BARS + 1):qi + 1]
    return regime_formula.classify_regime(
        engine.index_snapshot(spy_win), engine.index_snapshot(qqq_win)).regime


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--years", type=float, default=5)
    parser.add_argument("--max-positions", type=int, default=6)
    parser.add_argument("--rank", choices=["grade", "rs20"], default="grade",
                        help="who wins the slot when fires exceed free slots: "
                             "grade = build grade then alphabetical (the original rule); "
                             "rs20 = strongest 20-day relative strength vs SPY first")
    # Pre-registered 2026-08-11 from the 335-signal book (seed 42), BEFORE this
    # rerun: (1) no entry while price is below its own SMA20, (2) no chasing an
    # entry more than 2% above the trigger, (3) no grade C at fire (A/B only).
    parser.add_argument("--entry-filters", action="store_true",
                        help="apply the three pre-registered entry filters above")
    parser.add_argument("--skip-sma-filter", action="store_true",
                        help="with --entry-filters: drop rule (1), keep only "
                             "no-chase and no-grade-C (owner request 2026-08-11)")
    parser.add_argument("--skip-grade-filter", action="store_true",
                        help="with --entry-filters: drop rule (3), so C grades "
                             "may enter (owner request 2026-08-11)")
    # Pre-registered 2026-08-13 (PREREGISTRATION_EARLY_TRAIL.md), owner's own
    # variant: once a trade is up 1.5x ATR, start trailing 1.2x ATR under the
    # running peak, before target 1. Default off = today's live rule.
    parser.add_argument("--early-arm-atr", type=float, default=None,
                        help="arm the early trail at this profit, in ATRs (1.5)")
    parser.add_argument("--early-trail-atr", type=float, default=1.2,
                        help="trail distance under the running peak, in ATRs")
    parser.add_argument("--early-anchor", choices=["close", "high"],
                        default="close", help="peak measured on closes or highs")
    # Close-only stop (pre-registered 2026-08-19). Default off = live rule.
    parser.add_argument("--stop-basis", choices=["intraday", "close"],
                        default="intraday",
                        help="'close' = a stop only counts on a settled close")
    parser.add_argument("--stop-fill", choices=["close", "next_open"],
                        default="close",
                        help="with --stop-basis close: sell at that close or "
                             "at the next day's open")
    parser.add_argument("--emergency-atr", type=float, default=None,
                        help="second, hard stop this many ATRs BELOW the "
                             "normal stop, fired intraday (needs --stop-basis "
                             "close to mean anything)")
    parser.add_argument("--tag", default="", help="suffix for the output file")
    args = parser.parse_args()
    engine.EARLY_TRAIL.update(arm_atr=args.early_arm_atr,
                              trail_atr=args.early_trail_atr,
                              anchor=args.early_anchor)
    engine.STOP_MODE.update(basis=args.stop_basis, fill=args.stop_fill,
                            emergency_atr=args.emergency_atr)

    tickers = pick_universe(args.n, args.seed)
    print(f"universe ({args.n}, seed {args.seed}): {', '.join(tickers)}")

    spy = engine.load_bars("SPY")
    qqq = engine.load_bars("QQQ")
    spy_ix = {b["date"]: i for i, b in enumerate(spy)}
    qqq_ix = {b["date"]: i for i, b in enumerate(qqq)}

    bars_of, ix_of, earnings_of = {}, {}, {}
    for t in tickers:
        bars_of[t] = engine.load_bars(t)
        ix_of[t] = {b["date"]: i for i, b in enumerate(bars_of[t])}
        earnings_of[t] = engine.load_earnings(t)
    no_earnings = sorted(t for t in tickers if not earnings_of[t])

    last_day = date.fromisoformat(spy[-1]["date"])
    sim_start = (last_day - timedelta(days=int(args.years * 365.25))).isoformat()
    calendar = [b["date"] for b in spy if b["date"] >= sim_start]

    cash = START_EQUITY
    positions: dict[str, engine.Position] = {}
    credited: dict[str, int] = {}          # exits already returned to cash
    pending: dict[str, dict] = {}
    trades = []
    blocked_at_build: dict[str, int] = {}
    blocked_at_fire = []
    skipped_capacity = []
    skipped_sizing = []
    equity_curve = []

    for d in calendar:
        si, qi = spy_ix.get(d), qqq_ix.get(d)
        if si is None or qi is None:
            continue
        regime = day_regime(spy, qqq, si, qi)

        days: dict[str, engine.Day] = {}
        for t in tickers:
            i = ix_of[t].get(d)
            if i is None or i < 60:        # not listed yet / warmup too short
                continue
            days[t] = engine.Day(bars_of[t], i, spy, qqq, spy_ix, qqq_ix,
                                 earnings_of[t], regime=regime)

        # 1) manage every open position; credit exits to cash as they happen
        for t in list(positions):
            day = days.get(t)
            if day is None:
                continue
            pos = positions[t]
            closed = pos.process_day(day)
            new_exits = pos.exits[credited[t]:]
            cash += sum(e["qty"] * e["price"] for e in new_exits)
            credited[t] = len(pos.exits)
            if closed:
                rec = pos.to_record()
                rec["ticker"] = t
                trades.append(rec)
                del positions[t], credited[t]
                pending.pop(t, None)

        # equity for sizing: cash + open positions at today's close
        equity = cash + sum(
            pos.remaining * days[t].close
            for t, pos in positions.items() if t in days)

        # 2) collect fires from plans built on EARLIER days, gate them, rank them
        fires = []
        for t, plan in list(pending.items()):
            if t in positions or "blocked" in plan:
                continue
            day = days.get(t)
            if day is None:
                continue
            if day.close > plan["trigger"] and day.close > plan["stop"]:
                grade_now, _ = engine.regrade_at_fire(plan, day)
                regime_bad = day.regime in decision_policy.BLOCKING_REGIMES
                filter_reason = None
                if args.entry_filters:
                    if (not args.skip_sma_filter
                            and day.dist_sma20_atr is not None
                            and day.dist_sma20_atr < 0):
                        filter_reason = "below_sma20"
                    elif (day.close - plan["trigger"]) / plan["trigger"] > 0.02:
                        filter_reason = "chased_over_2pct"
                    elif grade_now == "C" and not args.skip_grade_filter:
                        filter_reason = "grade_C_at_fire"
                if grade_now in decision_policy.BLOCKING_GRADES or regime_bad:
                    blocked_at_fire.append(
                        {"date": d, "ticker": t,
                         "reason": ("regime_" + day.regime) if regime_bad
                                   else ("grade_" + grade_now)})
                elif filter_reason is not None:
                    blocked_at_fire.append({"date": d, "ticker": t,
                                            "reason": filter_reason})
                else:
                    if args.rank == "rs20":
                        # strongest stock vs the market wins the slot; a missing
                        # RS reading ranks last, ties alphabetical
                        key = (-(day.rs20 if day.rs20 is not None else -1e9),)
                    else:
                        key = (GRADE_RANK.get(plan["grade"], 3),)
                    fires.append((key, t, plan, day))

        for _, t, plan, day in sorted(fires, key=lambda x: (x[0], x[1])):
            if len(positions) >= args.max_positions:
                skipped_capacity.append({"date": d, "ticker": t,
                                         "grade": plan["grade"]})
                continue                    # plan stays live for tomorrow
            per_share = day.close - plan["stop"]
            qty = int((equity * RISK_PCT) // per_share)
            qty = min(qty, int(cash // day.close))
            if qty <= 0:
                skipped_sizing.append({"date": d, "ticker": t, "reason": "qty_zero"})
                continue
            positions[t] = engine.Position(plan, day.close, qty, day.date)
            credited[t] = 0
            cash -= qty * day.close
            pending.pop(t, None)

        # 3) rebuild pending plans for every flat ticker from data through today
        for t, day in days.items():
            if t in positions:
                continue
            plan = engine.build_plan(day)
            if plan is not None and "blocked" in plan:
                blocked_at_build[plan["blocked"]] = \
                    blocked_at_build.get(plan["blocked"], 0) + 1
                pending[t] = plan if "targets" in plan else None
            else:
                pending[t] = plan
            if pending[t] is None:
                pending.pop(t, None)

        mark = cash + sum(pos.remaining * days[t].close
                          for t, pos in positions.items() if t in days)
        equity_curve.append({"date": d, "equity": round(mark, 2),
                             "open_positions": len(positions)})

    # force-close whatever is still open at each ticker's last bar
    for t, pos in list(positions.items()):
        last_bar = bars_of[t][-1]
        pos.force_close(date.fromisoformat(last_bar["date"]), last_bar["close"])
        new_exits = pos.exits[credited[t]:]
        cash += sum(e["qty"] * e["price"] for e in new_exits)
        rec = pos.to_record()
        rec["ticker"] = t
        trades.append(rec)
    positions.clear()

    # ---- summary ----------------------------------------------------------
    final_equity = cash
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]

    spy_start = next(b["close"] for b in spy if b["date"] >= sim_start)
    spy_bh = START_EQUITY * spy[-1]["close"] / spy_start

    peak, max_dd = -1e18, 0.0
    for p in equity_curve:
        peak = max(peak, p["equity"])
        max_dd = max(max_dd, (peak - p["equity"]) / peak)

    per_ticker: dict[str, dict] = {}
    for t in trades:
        s = per_ticker.setdefault(t["ticker"], {"trades": 0, "pnl": 0.0, "r": 0.0})
        s["trades"] += 1
        s["pnl"] = round(s["pnl"] + t["pnl"], 2)
        s["r"] = round(s["r"] + t["r"], 3)

    max_open = max((p["open_positions"] for p in equity_curve), default=0)
    early_armed = sum(1 for t in trades if t.get("early_armed"))
    early_moved = sum(1 for t in trades if t.get("early_moved"))
    summary = {
        "tickers": tickers, "seed": args.seed, "slot_rank": args.rank,
        "entry_filters": bool(args.entry_filters),
        "early_trail": ({"arm_atr": args.early_arm_atr,
                         "trail_atr": args.early_trail_atr,
                         "anchor": args.early_anchor}
                        if args.early_arm_atr else None),
        "trades_early_armed": early_armed, "trades_early_moved": early_moved,
        "stop_mode": {"basis": args.stop_basis, "fill": args.stop_fill,
                      "emergency_atr": args.emergency_atr},
        "window": [calendar[0], calendar[-1]],
        "start_equity": START_EQUITY, "final_equity": round(final_equity, 2),
        "return_pct": round((final_equity / START_EQUITY - 1) * 100, 2),
        "spy_buy_hold_return_pct": round((spy_bh / START_EQUITY - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "trades": len(trades), "wins": len(wins),
        "win_rate_pct": round(100 * len(wins) / len(rs), 1) if rs else None,
        "total_r": round(sum(rs), 2),
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "runner_pnl_total": round(sum(t["runner_pnl"] for t in trades), 2),
        "target_pnl_total": round(sum(t["pnl"] - t["runner_pnl"] for t in trades), 2),
        "max_positions_seen": max_open,
        "blocked_at_build": blocked_at_build,
        "blocked_at_fire_count": len(blocked_at_fire),
        "skipped_capacity_count": len(skipped_capacity),
        "skipped_sizing_count": len(skipped_sizing),
        "tickers_missing_earnings_history": no_earnings,
    }

    out = {"summary": summary, "per_ticker": per_ticker, "trades": trades,
           "blocked_at_fire": blocked_at_fire,
           "skipped_capacity": skipped_capacity,
           "equity_curve": equity_curve}
    suffix = "_filtered" if args.entry_filters else ""
    if args.entry_filters and args.skip_sma_filter:
        suffix = "_filtered_nosma"
    if args.entry_filters and args.skip_grade_filter:
        suffix += "_allowc"
    if args.tag:
        suffix += "_" + args.tag
    out_path = ROOT / f"results_portfolio_{args.seed}_{args.rank}{suffix}.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(f"\nfull detail -> {out_path}")


if __name__ == "__main__":
    main()
