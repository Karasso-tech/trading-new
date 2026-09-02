"""What each of the six setups actually did, before anyone builds a model on top.

Eight questions per setup, asked the same way for all six:

  1. how often it appeared
  2. how often it reached target 1 before the stop
  3. how long that took
  4. what it made or lost on average
  5. the worst losing run it went through
  6. which market states it worked in
  7. which market states it failed in
  8. whether the answer holds across years or comes from one good stretch

Two things this is NOT, said plainly so no one reads more into it than is there:

  * It is not a portfolio. Trades overlap, nothing is sized, no risk budget is
    applied. Every number is per trade, in R -- one R being the distance from
    the entry to the stop actually taken.
  * A win is target 1 reached before the stop. Nothing here models what a runner
    would have done after target 1, because stage 0 does not model exits.

Market state is the two raw SPY numbers stored on every trade, cut into bands
here and nowhere else. No band is a claim that it matters -- that is exactly
what questions 6 and 7 are asking.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parent / "data" / "stage0.db"

SETUPS = ("Breakout", "Failed Breakdown", "Reclaim", "Gap-and-Hold", "Retest", "Pullback")

# A trade is only counted once it has an answer. `open` ones ran out of days and
# would quietly count as losses if left in.
CLOSED = "outcome <> 'open'"


def trend_band(above):
    if above is None:
        return "unknown"
    return "SPY above its 200-day" if above else "SPY below its 200-day"


def momentum_band(ret):
    if ret is None:
        return "unknown"
    if ret < -2:
        return "SPY falling (20d under -2%)"
    if ret > 2:
        return "SPY rising (20d over +2%)"
    return "SPY flat (20d within 2%)"


def worst_losing_run(rows) -> tuple[int, str, str]:
    """The longest run of consecutive losses, in entry-date order.

    Anything that is not a win is a loss here, `doubt` included -- it exits at
    the stop, so calling it anything else would flatter the record.
    """
    best = run = 0
    start = first = last = None
    for r in rows:
        if r["outcome"] == "success":
            run, start = 0, None
        else:
            run += 1
            start = start or r["entry_date"]
            if run > best:
                best, first, last = run, start, r["entry_date"]
    return best, first, last


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", type=int, help="which run id (default: the newest)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    run_id = args.run or conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    print(f"run {run_id}   {run['start_date']} .. {run['end_date']}   "
          f"params {run['fingerprint']}")
    print("per-trade R, no sizing, no portfolio. a win is target 1 before the stop.\n")

    appeared = {r["setup_type"]: r["n"] for r in conn.execute(
        "SELECT setup_type, COUNT(*) n FROM ideas WHERE run_id=? AND setup_type IS NOT NULL"
        " GROUP BY 1", (run_id,))}
    tradeable = {r["setup_type"]: r["n"] for r in conn.execute(
        "SELECT setup_type, COUNT(*) n FROM ideas WHERE run_id=? AND tradeable=1"
        " GROUP BY 1", (run_id,))}

    trades = defaultdict(list)
    for r in conn.execute(f"SELECT * FROM trades WHERE run_id=? AND {CLOSED}"
                          " ORDER BY entry_date", (run_id,)):
        trades[r["setup_type"]].append(r)

    # --- 1-5: the funnel, the hit rate, the timing, the money, the worst run --
    print("=" * 92)
    print(f"{'setup':18s} {'appeared':>9s} {'has target':>11s} {'traded':>8s} "
          f"{'win%':>6s} {'avg R':>7s} {'med days':>9s} {'worst run':>10s}")
    print("-" * 92)
    for setup in SETUPS:
        rows = trades.get(setup, [])
        if not rows:
            print(f"{setup:18s} {appeared.get(setup,0):>9,} {tradeable.get(setup,0):>11,} "
                  f"{0:>8}  -- never traded --")
            continue
        wins = [r for r in rows if r["outcome"] == "success"]
        days = sorted(r["days_held"] for r in wins if r["days_held"] is not None)
        avg_r = sum(r["r_multiple"] for r in rows) / len(rows)
        run_len, run_from, run_to = worst_losing_run(rows)
        print(f"{setup:18s} {appeared.get(setup,0):>9,} {tradeable.get(setup,0):>11,} "
              f"{len(rows):>8,} {100*len(wins)/len(rows):>5.1f}% {avg_r:>7.3f} "
              f"{(days[len(days)//2] if days else 0):>9} {run_len:>10}")
    print()

    # --- 3 in full: how long a winner takes, and how long a loser takes -------
    print("=" * 92)
    print("how long it took")
    print("-" * 92)
    for setup in SETUPS:
        rows = trades.get(setup, [])
        if not rows:
            continue
        w = sorted(r["days_held"] for r in rows if r["outcome"] == "success")
        l = sorted(r["days_held"] for r in rows if r["outcome"] != "success")
        pct = lambda xs, q: xs[min(len(xs) - 1, int(len(xs) * q))] if xs else 0
        print(f"  {setup:18s} winners: median {pct(w,.5):>3} days, "
              f"slowest quarter over {pct(w,.75):>3}   |   "
              f"losers: median {pct(l,.5):>3} days")
    print()

    # --- 6 and 7: market state -----------------------------------------------
    for label, fn, field in (("SPY vs its 200-day average", trend_band, "spy_above_sma200"),
                             ("SPY over the last 20 days", momentum_band, "spy_return_20d_pct")):
        print("=" * 92)
        print(f"market state: {label}   (the state on the ENTRY day)")
        print("-" * 92)
        bands = sorted({fn(r[field]) for rows in trades.values() for r in rows})
        header = "".join(f"{b.split('(')[0].strip():>30s}" for b in bands)
        print(f"{'setup':18s}{header}")
        for setup in SETUPS:
            rows = trades.get(setup, [])
            if not rows:
                continue
            cells = ""
            for band in bands:
                sub = [r for r in rows if fn(r[field]) == band]
                if not sub:
                    cells += f"{'--':>30s}"
                    continue
                win = 100 * sum(r["outcome"] == "success" for r in sub) / len(sub)
                avg = sum(r["r_multiple"] for r in sub) / len(sub)
                cells += f"{f'n={len(sub):,} {win:.0f}% {avg:+.2f}R':>30s}"
            print(f"{setup:18s}{cells}")
        print()

    # --- 8: is it one good stretch? ------------------------------------------
    print("=" * 92)
    print("year by year -- average R per trade (n)")
    print("-" * 92)
    years = sorted({r["entry_date"][:4] for rows in trades.values() for r in rows})
    print(f"{'setup':18s}" + "".join(f"{y:>16s}" for y in years) + f"{'+years':>9s}")
    for setup in SETUPS:
        rows = trades.get(setup, [])
        if not rows:
            continue
        cells, positive = "", 0
        for year in years:
            sub = [r for r in rows if r["entry_date"][:4] == year]
            if not sub:
                cells += f"{'--':>16s}"
                continue
            avg = sum(r["r_multiple"] for r in sub) / len(sub)
            positive += avg > 0
            cells += f"{f'{avg:+.2f} ({len(sub):,})':>16s}"
        counted = sum(1 for y in years if any(r["entry_date"][:4] == y for r in rows))
        print(f"{setup:18s}{cells}{f'{positive}/{counted}':>9s}")
    print()

    print("=" * 92)
    print("how much of each setup's whole result came from its single best year")
    print("-" * 92)
    for setup in SETUPS:
        rows = trades.get(setup, [])
        if not rows:
            continue
        total = sum(r["r_multiple"] for r in rows)
        by_year = defaultdict(float)
        for r in rows:
            by_year[r["entry_date"][:4]] += r["r_multiple"]
        best_year = max(by_year, key=by_year.get)
        share = 100 * by_year[best_year] / total if total else float("nan")
        print(f"  {setup:18s} total {total:>9,.1f}R   best year {best_year} "
              f"({by_year[best_year]:+,.1f}R = {share:.0f}% of everything)")


if __name__ == "__main__":
    main()
