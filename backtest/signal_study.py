"""Every entry point the system would ever have named, simulated on its own.

This is NOT a backtest of the strategy, and the difference is the whole point.

`sim_portfolio.py` runs a book: six slots, real cash, and when more triggers
fire on one day than there are slots, THE GRADE DECIDES WHO GETS IN. On one
draw that meant 488 fires became 130 trades, on top of 289 refused for grading
D and 1,198 for the market regime. Asking that output whether the grade ranks
trades is circular -- the grade chose the sample.

So this strips the book away. Every plan the mechanical chain would have built,
on every day, on every ticker, for every setup shape, is simulated by itself:
no slot competition, no cash limit, no grade gate, no regime gate. One row per
plan, carrying everything that was knowable at build time and what the trade
then did.

What it is for: finding which conditions actually separate a good entry from a
bad one, before anyone decides which of them should become rules. The current
rules were written first and measured second, which is how a criterion that
LOSERS pass more often than winners survived this long.

What it deliberately does not do: judge. It writes a table. The reading of that
table belongs in an analysis with its thresholds written down first.

    python backtest/signal_study.py --n 50 --seed 42 --years 5
    python backtest/signal_study.py --tickers AAPL,MSFT --out signals.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys
from datetime import date, timedelta

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "bot"))

import sim_breakout as engine     # noqa: E402
import setup_types                # noqa: E402
import trade_sim                  # noqa: E402
# How far a trigger may drift and still be the same idea. Inside rule 4's own
# 0.7x ATR noise floor by a wide margin -- see same_idea() for the measurement
# this came from.
from refresh_pending import MOVED_ATR_MULTIPLE   # noqa: E402  the live bar

# Set from --nominal-risk-usd at startup; see the cost block for why a study
# that sizes nothing still needs a notional size.
NOMINAL_RISK_USD = 1000.0


def universe(n, seed: int) -> list:
    """Every ticker with bars on disk, unless a sample is explicitly asked for.

    The portfolio backtest draws 50 at random with a fixed seed, and it has to:
    it runs a book with six slots, so the universe must be small enough for slot
    competition to mean something, and several independent draws are how it
    checks a result is not one lucky basket.

    None of that applies here. There is no book. Every entry point stands alone,
    so a bigger universe is strictly more evidence and costs nothing but time --
    and the thing every finding has died on so far is the margin. 505 tickets
    instead of 50 is roughly ten times the sample, which is the difference
    between "cannot be told apart from noise" and an answer.

    What more tickers does NOT fix, and neither does anything else here: it is
    still one five-year window, and the S&P list is today's membership, so
    companies that fell out of the index are missing. That biases how much money
    the whole thing appears to make. It biases comparisons BETWEEN setups and
    conditions far less, which is what this tool is for."""
    data = json.loads((HERE / "data" / "snpdata.json").read_text(encoding="utf-8"))
    have_bars = {p.stem for p in (HERE / "data" / "bars").glob("*.json")
                 if not p.stem.startswith("_")}
    pool = sorted(t for t in data["tickers"] if t in have_bars)
    if n is None:
        return pool
    return sorted(random.Random(seed).sample(pool, min(n, len(pool))))


def study_ticker(ticker: str, years: float) -> list:
    """One row per plan this ticker would ever have produced."""
    try:
        bars = engine.load_bars(ticker)
        spy, qqq = engine.load_bars("SPY"), engine.load_bars("QQQ")
    except Exception as exc:
        print(f"  {ticker}: no bars ({type(exc).__name__})")
        return []
    if len(bars) < 200:
        return []

    earnings = engine.load_earnings(ticker)
    # Day wants date -> row-index maps for the two indexes, not snapshots: it
    # slices its own window out of the raw bars and snapshots that itself.
    spy_ix = {b["date"]: i for i, b in enumerate(spy)}
    qqq_ix = {b["date"]: i for i, b in enumerate(qqq)}
    cutoff = (date.fromisoformat(bars[-1]["date"]) - timedelta(days=int(years * 365.25)))
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    atrs = trade_sim.atr_series(highs, lows, closes)

    def date_of(bar):
        return bar["date"]

    # ONE live plan per ticker at a time, replaced only when the live rule
    # would replace it -- which is what the system actually does.
    #
    # The first version of this file rebuilt a plan every single day and
    # simulated each one independently. On AFL that produced 924 plans and 664
    # entries from 157 distinct entry dates, with 35 "trades" opening on one
    # day: the same breakout, counted thirty-five times, because thirty-five
    # days of near-identical waiting plans all fired at once.
    #
    # Those rows were real -- every number in them was true -- and they were
    # not thirty-five observations. Counting them as such would have made every
    # margin in the analysis look tiny and every difference look certain,
    # including the differences that are noise. Fixing that in the analysis
    # would have been patching a number; the model was what was wrong.
    #
    # bot/refresh_pending.py's classify() is the live rebuild rule and its
    # thresholds are mirrored here rather than invented: no target to sell into,
    # price through the stop, or price a full ATR away from the trigger.
    rows = []
    live_plan = None
    live_from = None
    busy_until = -1

    def still_valid(plan, day) -> bool:
        """Is the plan we are holding still a live plan today?

        This is the live invalidation test, and it replaced a stricter rule
        that quietly destroyed the study (2026-08-31).

        The stricter rule re-classified the setup every day and threw the held
        plan away whenever today's answer differed. That sounds like "would a
        screener give me the same idea", and it is not what the live system
        does -- refresh_pending only re-screens a waiting idea when something
        MECHANICAL breaks, so a pending thesis survives while it waits.

        The damage was total and one-directional. A Breakout trigger sits above
        price and needs weeks; a Reclaim trigger sits against price and fires
        at once. The classifier changes its answer on 22% of days, so every
        slow-firing plan was discarded days before it could fire and every
        fast-firing one survived. The result: 2,409 Reclaim entries against 12
        Breakouts, and a median time-to-fire of ONE day. The study was not
        measuring which setup is better, it was measuring which setup is
        quicker.

        So the test is the live one: the plan dies when price breaks the stop it
        is built on, when price has run a full ATR away from the trigger and the
        trade it described no longer exists, or when it has nothing to sell
        into."""
        if not plan or plan.get("blocked") or not plan.get("targets"):
            return False
        stop, trig, atr = plan.get("stop"), plan.get("trigger"), plan.get("atr_at_build")
        if stop is None or trig is None or not atr:
            return False
        if day.close < stop:
            return False
        return abs(day.close - trig) / atr < MOVED_ATR_MULTIPLE

    for i in range(200, len(bars)):
        if date.fromisoformat(bars[i]["date"]) < cutoff:
            continue
        if bars[i]["date"] not in spy_ix or bars[i]["date"] not in qqq_ix:
            continue
        if i <= busy_until:
            # A position is open on this ticker. The live system holds one at a
            # time, so no new plan is written and no second entry can happen --
            # a per-ticker rule, not a slot or cash limit, so it does not let
            # the grade choose the sample the way the portfolio backtest does.
            continue

        day = engine.Day(bars, i, spy, qqq, spy_ix, qqq_ix, earnings)

        # Keep the plan being held while it is still live; build a new one only
        # when it is not. Exactly the order the nightly refresh uses.
        if live_plan is not None and still_valid(live_plan, day):
            plan = live_plan
        else:
            plan = engine.build_plan(day)
            # Two of build_plan's refusals return a stub with no `targets` key
            # at all -- no_honest_stop and no_qualifying_target. Both mean there
            # is nothing to enter, so the day is recorded as unbuildable and
            # nothing is held. Reaching for plan["targets"] on one of those is
            # what killed ANET on the first full run, and a run that loses whole
            # tickers to an exception leaves a dataset with holes nobody counts.
            if not plan or "targets" not in plan:
                if plan:
                    rows.append({"ticker": ticker, "built": bars[i]["date"],
                                  "unbuildable": plan.get("blocked") or "no_plan",
                                  "setup": plan.get("setup")})
                live_plan = None
                continue
            live_plan, live_from = plan, i
            built_day = day

        plan = live_plan
        if not plan.get("targets") or plan.get("trigger") is None:
            live_plan = None
            continue
        if bars[i]["close"] < plan["trigger"]:
            continue          # written today, not triggered today

        # It fired. One entry point, recorded once.
        row = {
            "ticker": ticker,
            "built": bars[live_from]["date"],
            "setup": plan.get("setup"),
            "trigger": plan.get("trigger"),
            "stop": plan.get("stop"),
            "stop_basis": plan.get("stop_basis"),
            "atr_at_build": plan.get("atr_at_build"),
            "targets": len(plan.get("targets") or []),
            "grade_at_build": plan.get("grade"),
            "rubric_score": plan.get("rubric_score"),
            "rubric_criteria": plan.get("rubric_criteria"),
            "confidence": plan.get("confidence"),
            "regime": built_day.regime,
            "dist_sma20_atr": built_day.dist_sma20_atr,
            "rs20": built_day.rs20,
            "rs5": built_day.rs5,
            "earnings_days_out": built_day.earnings_days_out,
            "atr_pct": (built_day.atr14 / built_day.close * 100) if built_day.close else None,
            # Why the live gates WOULD have refused it, kept as a field and not
            # as a filter -- measuring the trades the gates throw away is the
            # whole reason this tool exists.
            "would_be_blocked": plan.get("blocked"),
            "fired": True,
            "fired_date": bars[i]["date"],
            "days_to_fire": i - live_from,
        }

        if i + 1 >= len(bars):
            row["fired"] = False
            row["note"] = "fired on the last bar; no next open to buy at"
            rows.append(row)
            live_plan = None
            continue

        entry_idx = i + 1
        # Costs are ON by default here, unlike the raw engine call this used to
        # make (2026-08-31 audit). They are not a rounding detail and they are
        # not neutral between the things this tool compares: slippage and
        # commission are close to a fixed sum per trade, so as a fraction of R
        # they fall hardest on a trade with a TIGHT stop -- and stop distance
        # varies systematically by setup shape and is exactly what the R:R
        # criterion pushes around. Leaving them out would have quietly favoured
        # tight-stop setups in every comparison.
        raw_entry = bars[entry_idx]["open"]
        entry = engine.slip_buy(raw_entry)
        risk = entry - plan["stop"]
        if risk <= 0:
            row["no_risk"] = True
            rows.append(row)
            live_plan = None
            continue

        sim = trade_sim.simulate(
            bars, entry_idx,
            trade_sim.build_plan(entry, plan["stop"],
                                  [t["price"] for t in plan["targets"]]),
            atrs, date_of)
        grade_fire, rr_fire = engine.regrade_at_fire(plan, day)
        # Rule 18 re-checks the market regime at the moment the trigger
        # confirms, not only at build. Recording only the build-day regime
        # would have made "does the regime gate help" unanswerable, since the
        # gate fires on the value at the fire.
        row["regime_at_fire"] = day.regime
        row["dist_sma20_atr_at_fire"] = day.dist_sma20_atr
        # Exit slippage, charged once against the trade's own risk. The engine
        # applies it per fill inside its Position class, which this tool does
        # not use; one round-trip charge on the exit side is the honest
        # approximation, and it is stated rather than hidden.
        cost_r = 0.0
        if engine.COSTS["on"] and risk > 0:
            # Slippage on the exit as well as the entry. The entry side is
            # already in `entry` above, which raised the risk-per-share; this
            # is the other half of the round trip.
            exit_slip = raw_entry * engine.COSTS["slippage_bps"] / 10_000.0
            # Commission needs a share count, and this tool sizes nothing --
            # so it borrows the live sizing rule (one full risk unit) purely to
            # express the fee in R. This is where a tight stop is charged: the
            # same dollar risk buys more shares, and per-share commission grows
            # while the risk it is measured against does not.
            qty = int(NOMINAL_RISK_USD / risk) if risk > 0 else 0
            fees = engine.commission(qty, entry) + engine.commission(qty, entry)
            cost_r = (exit_slip / risk) + (fees / NOMINAL_RISK_USD)
        row.update({
            "entry": entry,
            "entry_raw": raw_entry,
            "cost_r": cost_r,
            "entry_gap_pct": (entry - plan["trigger"]) / plan["trigger"] * 100,
            "risk_per_share": risk,
            "grade_at_fire": grade_fire,
            "rr_at_fire": rr_fire,
            "resolution": sim.resolution,
            "is_fully_closed": sim.is_fully_closed,
            # Net of the exit-side charge. `r_gross` is kept beside it so the
            # size of the cost is visible rather than baked in invisibly.
            "r": (sim.r_multiple - cost_r) if sim.r_multiple is not None else None,
            "r_gross": sim.r_multiple,
            "r_full_exit_at_t1": sim.r_multiple_full_exit_at_t1,
            "mfe_r": sim.mfe_r,
            "mae_r": sim.mae_r,
            "bars_held": sim.bars_held,
        })
        # The ticker is occupied until this trade ends -- one position at a
        # time, exactly as the live system holds it.
        busy_until = entry_idx + (sim.bars_held or 1)
        live_plan = None
        rows.append(row)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=None,
                    help="sample this many tickers instead of using every one "
                         "with bars on disk (the default). For quick checks only.")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--years", type=float, default=5)
    ap.add_argument("--tickers", help="comma-separated, instead of a random draw")
    ap.add_argument("--setups", choices=["breakout", "all"], default="all",
                    help="'all' is the point of this tool -- five of the six setup "
                         "shapes have never been backtested at all")
    ap.add_argument("--slippage-bps", type=float, default=5.0,
                    help="basis points paid per side, matching sim_portfolio")
    ap.add_argument("--nominal-risk-usd", type=float, default=1000.0,
                    help="the dollar risk one trade carries, used only to turn "
                         "per-share commission into an R figure. 1%% of a "
                         "100k account, the live rule.")
    ap.add_argument("--no-costs", action="store_true",
                    help="run without slippage and commission. Off by default: "
                         "costs fall hardest on tight-stop trades, so omitting "
                         "them biases the very comparisons this tool exists for.")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    engine.COSTS["on"] = not args.no_costs
    # The dict's own defaults are zeros -- the real numbers live in the other
    # tool's argparse, which this one does not go through. Same values
    # sim_portfolio runs with, so the two are comparable.
    engine.COSTS["slippage_bps"] = 0.0 if args.no_costs else args.slippage_bps
    global NOMINAL_RISK_USD
    NOMINAL_RISK_USD = args.nominal_risk_usd
    engine.SETUPS["allow"] = (tuple(setup_types.SETUP_TYPES) if args.setups == "all"
                              else (setup_types.BREAKOUT,))
    names = ([t.strip().upper() for t in args.tickers.split(",")] if args.tickers
             else universe(args.n, args.seed))
    print(f"{len(names)} tickers, {args.years}y, setups={args.setups}")

    rows, failures = [], []
    for i, tk in enumerate(names, 1):
        try:
            got = study_ticker(tk, args.years)
        except Exception as exc:
            # One bad ticker must never cost a two-hour run its other 504.
            # A dataset missing an uncounted number of tickers is not a
            # dataset, so every failure is carried into the output file itself,
            # not left in a log nobody re-reads.
            print(f"  [{i}/{len(names)}] {tk}: FAILED ({type(exc).__name__}: {exc})",
                  flush=True)
            failures.append({"ticker": tk, "error": f"{type(exc).__name__}: {exc}"})
            continue
        rows.extend(got)
        if i % 25 == 0 or i == len(names):
            print(f"  [{i}/{len(names)}] {tk}: {len(got)} plans "
                  f"({len(rows)} rows so far)", flush=True)

    fired = [r for r in rows if r.get("fired") and r.get("r") is not None]
    print(f"\n{len(rows)} plans · {len(fired)} entered and produced an R")
    if failures:
        print(f"FAILED tickers ({len(failures)}): "
              f"{', '.join(f['ticker'] for f in failures)}")
    out = args.out or str(HERE / f"signals_{args.seed}_{args.setups}.json")
    pathlib.Path(out).write_text(json.dumps(
        {"meta": {"tickers": names, "years": args.years, "setups": args.setups,
                   "seed": args.seed, "plans": len(rows), "entered": len(fired),
                   "costs_on": bool(engine.COSTS["on"]),
                   "slippage_bps": engine.COSTS["slippage_bps"],
                   "nominal_risk_usd": NOMINAL_RISK_USD,
                   "moved_atr_multiple": MOVED_ATR_MULTIPLE,
                   "failed_tickers": failures},
         "rows": rows}, default=str), encoding="utf-8")
    print(f"written: {out}")


if __name__ == "__main__":
    main()
