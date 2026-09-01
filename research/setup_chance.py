"""Reclaim against Breakout: which trigger actually has the better chance?

READ-ONLY. Prints numbers, changes nothing. Proposes no rule change.

What turned up without a model anywhere near it. Scored on "did price reach the
target before the stop", with every idea measured against its own stop
distance, Reclaim beats the average at all three target distances and Breakout
trails it at all three -- and Reclaim's number barely moves between the first
half of the data and the second, while Breakout's climbs.

That is the first thing in this whole investigation that is about CHANCE, is
independent of the exit engine, and did not come out of a model. So it gets the
hardest test available rather than a victory lap:

  1. Head to head, with margins, at all three distances.
  2. Year by year. A gap that lives in one year is that year.
  3. The ticker split -- learn nothing, just check the gap holds on a different
     half of the companies.
  4. Is it really the trigger, or something riding along with it? Reclaim and
     Breakout do not fire in the same weather and may not carry the same stop
     width, and either could produce this on its own.
  5. What it is worth in R, and whether the real trades agree.

Point 4 is the one that matters. The two shapes fire at different moments by
construction -- that is what makes them different shapes -- so "Reclaim is
better" and "the conditions Reclaim fires in are better" are very hard to pull
apart, and this file does not claim to have pulled them fully apart.

    python research/setup_chance.py
"""

from __future__ import annotations

import json
import math
import pathlib

import numpy as np
import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
RUNGS = ("win_1r", "win_2r", "win_3r")
PAIR = ("Reclaim", "Breakout")


def load():
    d = json.loads((HERE / "dataset.json").read_text(encoding="utf-8"))["rows"]
    df = pd.DataFrame(d)
    df["fired_date"] = pd.to_datetime(df["fired_date"])
    return df


def gap(a: pd.Series, b: pd.Series):
    """Difference in hit rate, and the width either side of it."""
    a, b = a.dropna(), b.dropna()
    if len(a) < 30 or len(b) < 30:
        return None
    d = a.mean() - b.mean()
    se = math.sqrt(a.mean() * (1 - a.mean()) / len(a)
                   + b.mean() * (1 - b.mean()) / len(b))
    return d, 1.96 * se, len(a), len(b)


def line(label, g, width=0):
    if not g:
        print(f"    {label:>22} too thin")
        return
    d, m, na, nb = g
    tag = "real" if abs(d) > m else "inside the noise"
    print(f"    {label:>22} {100 * d:>+7.1f} points  ±{100 * m:>4.1f}   "
          f"n {na:>4} vs {nb:>4}   {tag}")


def main():
    df = load()
    hi, lo = df[df["setup"] == PAIR[0]], df[df["setup"] == PAIR[1]]
    print(f"{len(hi)} {PAIR[0]} entries, {len(lo)} {PAIR[1]} entries, "
          f"{df['fired_date'].min().date()} .. {df['fired_date'].max().date()}")

    print("\n" + "=" * 80)
    print(f"1. {PAIR[0].upper()} MINUS {PAIR[1].upper()}, AT EACH TARGET DISTANCE")
    print("=" * 80)
    for r in RUNGS:
        line(r.replace("win_", "+").replace("r", "R"), gap(hi[r], lo[r]))

    print("\n" + "=" * 80)
    print("2. YEAR BY YEAR, AT +2R")
    print("=" * 80)
    print("  A gap that lives in one year is that year, not a rule.")
    for y in sorted(df["fired_date"].dt.year.unique()):
        a = hi[hi["fired_date"].dt.year == y]["win_2r"]
        b = lo[lo["fired_date"].dt.year == y]["win_2r"]
        line(str(y), gap(a, b))

    print("\n" + "=" * 80)
    print("3. HELD-OUT COMPANIES")
    print("=" * 80)
    print("  Nothing is learned here -- the gap is simply re-measured on the")
    print("  other half of the companies, to see whether it is about these firms.")
    names = sorted(df["ticker"].unique())
    even = {t for i, t in enumerate(names) if i % 2 == 0}
    for half, keep in (("even companies", even), ("odd companies", set(names) - even)):
        a = hi[hi["ticker"].isin(keep)]["win_2r"]
        b = lo[lo["ticker"].isin(keep)]["win_2r"]
        line(half, gap(a, b))

    print("\n" + "=" * 80)
    print("4. IS IT THE TRIGGER, OR SOMETHING TRAVELLING WITH IT?")
    print("=" * 80)
    print("  First, how the two differ on everything else:\n")
    print(f"    {'':>26} {PAIR[0]:>12} {PAIR[1]:>12}")
    for c in ("stop_atr", "atr_pct", "dist_sma20_atr", "rs21", "days_to_fire",
              "entry_gap_pct", "rr_at_fire", "log_dollar_vol_50"):
        if c in df.columns:
            print(f"    {c:>26} {hi[c].median():>12.3f} {lo[c].median():>12.3f}")

    print("\n  Now the same gap INSIDE each market weather, so the weather cannot")
    print("  be the explanation:")
    for reg, g in df.groupby("regime_at_fire"):
        a = g[g["setup"] == PAIR[0]]["win_2r"]
        b = g[g["setup"] == PAIR[1]]["win_2r"]
        line(reg, gap(a, b))

    print("\n  And inside each band of stop width, so a wider or tighter stop")
    print("  cannot be the explanation either:")
    df = df.copy()
    df["sb"] = pd.qcut(df["stop_atr"], 4, labels=False, duplicates="drop")
    for b in sorted(df["sb"].dropna().unique()):
        g = df[df["sb"] == b]
        a = g[g["setup"] == PAIR[0]]["win_2r"]
        c = g[g["setup"] == PAIR[1]]["win_2r"]
        line(f"stop width group {int(b) + 1}", gap(a, c))

    print("\n" + "=" * 80)
    print("5. WHAT IT IS WORTH, AND WHETHER THE REAL TRADES AGREE")
    print("=" * 80)
    print("  A plain 2R target with a 1R stop pays 2 when it works and costs 1")
    print("  when it does not, so the hit rate turns straight into money.\n")
    print(f"    {'setup':>18} {'n':>6} {'hit rate':>10} {'that pays':>11} "
          f"{'real R':>9} {'real win rate':>14}")
    for s, g in df.groupby("setup"):
        if len(g) < 150:
            continue
        w = g["win_2r"].mean()
        rr = g["r_actual"].dropna()
        print(f"    {s:>18} {len(g):>6} {100 * w:>9.1f}% {2 * w - (1 - w):>+11.3f}R "
              f"{rr.mean():>+9.3f} {100 * (rr > 0).mean():>13.0f}%")
    print("\n  'that pays' is what a flat 2R-target, 1R-stop rule would have")
    print("  earned. 'real R' is what our actual exit engine earned, which is a")
    print("  different rule and does not have to agree.")


if __name__ == "__main__":
    main()
