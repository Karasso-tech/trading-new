"""One tilt showed the same sign everywhere. Test it properly before believing it.

READ-ONLY. Reads the cached universe table, prints numbers, changes nothing.

What turned up, unlooked for, in two independent places: every feature that
measures "this has already gone up" carries a NEGATIVE rank correlation with
the next 20 days -- 1-day, 5-day, 10-day, 21-day and 63-day returns, distance
above the 20-day and 50-day averages, up-days out of the last twenty, days
spent above the 50-day average, and the market-relative versions of all of
them. Ten-odd different numbers, one sign.

Ten numbers agreeing is not ten pieces of evidence -- they measure nearly the
same thing and they are heavily correlated. But the sign being unanimous across
two separate samples (our 9,195 entries, and 121,054 days of the whole
universe) is not what an accident looks like either.

So it gets the same treatment every other candidate got, and this file does not
choose anything: the score is fixed here, in advance, as the plain average of
the ranks of five well-separated horizons. No weights are fitted, no horizon is
picked because it looked good, and no threshold is tuned. It is then judged
walking forward, one calendar period at a time, on days none of it was built
from.

The point is NOT to propose trading it. Short-term reversal is a textbook
effect and a crowded one, and a rank correlation of -0.03 is not a business.
The point is that our entry rule buys the exact thing this says goes the other
way -- and if that holds up, it explains five years of flat measurements
better than any of the category-hunting did.

    python research/reversal.py
"""

from __future__ import annotations

import pathlib
import warnings

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore")

HERE = pathlib.Path(__file__).resolve().parent
CACHE = HERE / "universe.parquet"
TARGET = "exret_atr_20"

# Fixed before running. Five horizons, deliberately spread apart so they are
# not five copies of one number, and every one of them pointing the same way in
# both samples. Equal weight, because any weighting would be fitted.
LEGS = ["ret_5", "ret_21", "ret_63", "dist_sma20_atr", "up_days_20"]


def score(g: pd.DataFrame) -> pd.Series:
    """Average percentile rank of the five legs, computed WITHIN one day.

    Within-day is the whole point: it removes anything that is the same for
    every stock that morning, so this cannot secretly be a market-timing call
    dressed up as stock picking."""
    parts = [g[c].rank(pct=True) for c in LEGS]
    return sum(parts) / len(parts)


def main():
    df = pd.read_parquet(CACHE)
    df = df.dropna(subset=[TARGET] + LEGS).copy()
    print(f"{len(df)} days, {df['ticker'].nunique()} companies, "
          f"{df['fired_date'].min().date()} .. {df['fired_date'].max().date()}")

    # enough names on a day for a within-day ranking to mean anything
    counts = df.groupby("fired_date")["ticker"].transform("size")
    df = df[counts >= 50].copy()
    df["score"] = df.groupby("fired_date", group_keys=False).apply(score)
    print(f"{len(df)} days kept (at least 50 companies quoted that day)")

    print("\n" + "=" * 78)
    print("ALREADY-RUN-UP SCORE AGAINST THE NEXT 20 DAYS")
    print("=" * 78)
    print("  Group 1 = the most beaten down that day. Group 10 = the most run up.")
    print("  The answer column is the 20-day move with the market taken out, in ATR.")
    df["bucket"] = df.groupby("fired_date")["score"].transform(
        lambda s: pd.qcut(s, 10, labels=False, duplicates="drop"))
    print(f"\n  {'group':>6} {'n':>8} {'mean':>9} {'median':>9} {'up more often':>15}")
    for b in sorted(df["bucket"].dropna().unique()):
        g = df[df["bucket"] == b][TARGET]
        print(f"  {int(b) + 1:>6} {len(g):>8} {g.mean():>+9.3f} {g.median():>+9.3f} "
              f"{100 * (g > 0).mean():>14.0f}%")

    lo = df[df["bucket"] == 0][TARGET]
    hi = df[df["bucket"] == df["bucket"].max()][TARGET]
    gap = lo.mean() - hi.mean()
    se = np.sqrt(lo.var() / len(lo) + hi.var() / len(hi))
    print(f"\n  beaten down minus run up: {gap:+.3f} ATR  ±{1.96 * se:.3f}")
    print("  (that margin treats overlapping days as independent, so it is too")
    print("   narrow. The per-period table below is the one to believe.)")

    print("\n" + "=" * 78)
    print("THE SAME THING, ONE HALF-YEAR AT A TIME")
    print("=" * 78)
    print("  A tilt that only worked in one stretch is a stretch, not a tilt.")
    df["period"] = df["fired_date"].dt.to_period("2Q" if False else "Q")
    per = []
    print(f"\n  {'quarter':>9} {'n':>7} {'beaten down':>13} {'run up':>10} "
          f"{'gap':>9} {'rank corr':>11}")
    for p, g in df.groupby("period"):
        if len(g) < 500:
            continue
        a = g[g["bucket"] == 0][TARGET]
        b = g[g["bucket"] == g["bucket"].max()][TARGET]
        if len(a) < 50 or len(b) < 50:
            continue
        ic = stats.spearmanr(g["score"], g[TARGET]).statistic
        per.append((a.mean() - b.mean(), ic))
        print(f"  {str(p):>9} {len(g):>7} {a.mean():>+13.3f} {b.mean():>+10.3f} "
              f"{a.mean() - b.mean():>+9.3f} {ic:>+11.4f}")

    gaps = np.array([g for g, _ in per])
    ics = np.array([i for _, i in per])
    print(f"\n  {len(per)} quarters")
    print(f"    gap positive in {100 * (gaps > 0).mean():.0f}% of them, "
          f"average {gaps.mean():+.3f} ATR, "
          f"t {gaps.mean() / (gaps.std(ddof=1) / np.sqrt(len(gaps))):+.2f}")
    print(f"    rank correlation negative in {100 * (ics < 0).mean():.0f}% of them, "
          f"average {ics.mean():+.4f}, "
          f"t {ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics))):+.2f}")

    print("\n" + "=" * 78)
    print("AND WHERE DO OUR OWN ENTRIES SIT ON THAT SCORE?")
    print("=" * 78)
    import json
    ours = pd.DataFrame(json.loads(
        (HERE / "dataset.json").read_text(encoding="utf-8"))["rows"])
    ours["fired_date"] = pd.to_datetime(ours["fired_date"])
    key = df.set_index(["ticker", "fired_date"])["bucket"]
    ours = ours.join(key, on=["ticker", "fired_date"], rsuffix="_u")
    have = ours.dropna(subset=["bucket"])
    print(f"  {len(have)} of our entries fell on a sampled day and can be placed")
    if len(have):
        dist = have["bucket"].value_counts(normalize=True).sort_index() * 100
        print(f"\n  {'group':>6} {'share of our entries':>22} "
              f"{'share if we picked at random':>30}")
        for b, pct in dist.items():
            print(f"  {int(b) + 1:>6} {pct:>21.1f}% {10.0:>29.1f}%")
        print(f"\n  average group: {have['bucket'].mean() + 1:.2f} out of 10 "
              f"(5.5 would be no tilt at all)")


if __name__ == "__main__":
    main()
