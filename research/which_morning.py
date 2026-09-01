"""If the morning decides, can the morning be known in advance?

READ-ONLY. Prints numbers, changes nothing. Proposes no rule change.

Where the investigation landed. Whether an entry works is 48% about the DAY it
fired on and 0% about the company. Erase the day -- score every outcome against
its own morning's average -- and every trace of structure goes with it: the
clustering of winners drops inside what shuffled labels produce, and the best
rule out of 131,352 is matched by pure luck 24% of the time.

So "what do the winners have in common" has an answer, and it is not a property
of the stocks. **The winners fired on good mornings.**

That relocates the whole problem and raises the only question left worth
asking. A good morning is worth nothing if it can only be recognised afterwards.
So: using only what was knowable at the previous close, can the coming
fortnight's hit rate be called at all?

Two things are measured, and the second is the one that counts:

  1. How big is the prize. The gap between the best quarter of mornings and the
     worst, measured after the fact. This is the ceiling -- what perfect
     foresight would have been worth, and nothing can beat it.
  2. How much of it is reachable. A model trained only on the past, predicting
     forward, walked through the five years. Against a shuffled control.

If (2) is flat, then the day decides and the day cannot be called, and the
honest conclusion is that neither choosing stocks nor choosing mornings is
where this system can be improved.

    python research/which_morning.py
"""

from __future__ import annotations

import json
import pathlib
import warnings

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import RidgeCV
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")

HERE = pathlib.Path(__file__).resolve().parent
TARGET = "win_2r"
MIN_ENTRIES = 3
PERMS = 200


def main():
    d = json.loads((HERE / "dataset.json").read_text(encoding="utf-8"))["rows"]
    df = pd.DataFrame(d)
    df["fired_date"] = pd.to_datetime(df["fired_date"])
    df = df.dropna(subset=[TARGET])

    # One row per morning: how the market looked, and how that morning's
    # entries went. Market columns are identical across the day's entries, so
    # `first` is exact, not an approximation.
    market = ["spy_dist_sma200_atr", "spy_ret_21", "spy_realized_vol_20",
              "qqq_minus_spy_21"]
    day = df.groupby("fired_date").agg(
        rate=(TARGET, "mean"), n=(TARGET, "size"),
        **{m: (m, "first") for m in market}).reset_index()
    day = day[day["n"] >= MIN_ENTRIES].dropna(subset=market).reset_index(drop=True)
    print(f"{len(day)} mornings with {MIN_ENTRIES}+ entries, "
          f"{int(day['n'].sum())} entries in total")
    print(f"overall hit rate {100 * df[TARGET].mean():.1f}%")

    print("\n" + "=" * 80)
    print("1. THE CEILING -- WHAT PERFECT FORESIGHT WOULD BE WORTH")
    print("=" * 80)
    print("  Mornings sorted by how they actually turned out. Nobody can reach")
    print("  this; it is the size of the prize, not an achievable number.")
    day["q"] = pd.qcut(day["rate"], 4, labels=False, duplicates="drop")
    print(f"\n  {'quarter of mornings':>22} {'days':>6} {'entries':>9} {'hit rate':>10}")
    for q in sorted(day["q"].unique()):
        g = day[day["q"] == q]
        w = (g["rate"] * g["n"]).sum() / g["n"].sum()
        print(f"  {int(q) + 1:>22} {len(g):>6} {int(g['n'].sum()):>9} "
              f"{100 * w:>9.1f}%")
    top = day[day["q"] == day["q"].max()]
    bot = day[day["q"] == 0]
    print(f"\n  best quarter minus worst quarter: "
          f"{100 * ((top['rate'] * top['n']).sum() / top['n'].sum() - (bot['rate'] * bot['n']).sum() / bot['n'].sum()):+.1f} points")

    print("\n" + "=" * 80)
    print("2. HOW MUCH OF IT CAN BE CALLED IN ADVANCE?")
    print("=" * 80)
    print("  Trained on the past only, predicting forward, eight times through.")
    print("  The score is rank correlation between what was predicted for a")
    print("  morning and how that morning went.")

    X = day[market]
    y = day["rate"].to_numpy(float)
    w = day["n"].to_numpy(float)
    dates = day["fired_date"]
    edges = pd.date_range(dates.min(), dates.max(), periods=10)[1:-1]
    # a fortnight of purge: an entry made on the last training morning is still
    # unresolved for weeks, so training right up to the test would use it
    purge = pd.Timedelta(days=45)

    def run(labels):
        out = []
        for k, cut in enumerate(edges):
            nxt = edges[k + 1] if k + 1 < len(edges) else dates.max() + pd.Timedelta(days=1)
            tr = (dates < (cut - purge)).values
            te = ((dates >= cut) & (dates < nxt)).values
            if tr.sum() < 60 or te.sum() < 25:
                continue
            r = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                              RidgeCV(alphas=np.logspace(-1, 4, 20)))
            r.fit(X[tr], labels[tr], ridgecv__sample_weight=w[tr])
            g = HistGradientBoostingRegressor(max_depth=3, max_iter=200,
                                              learning_rate=0.05,
                                              min_samples_leaf=25, random_state=0)
            g.fit(X[tr], labels[tr], sample_weight=w[tr])
            out.append({
                "fold": k + 1, "n": int(te.sum()),
                "from": str(dates[te].min().date()), "to": str(dates[te].max().date()),
                "ridge": stats.spearmanr(r.predict(X[te]), labels[te]).statistic,
                "gbm": stats.spearmanr(g.predict(X[te]), labels[te]).statistic,
            })
        return out

    real = run(y)
    print(f"\n  {'fold':>5} {'test window':>24} {'days':>6} {'plain':>9} {'tree':>9}")
    for r in real:
        print(f"  {r['fold']:>5} {r['from']} .. {r['to']:>10} {r['n']:>6} "
              f"{r['ridge']:>+9.3f} {r['gbm']:>+9.3f}")
    for k, lab in (("ridge", "plain"), ("gbm", "tree")):
        v = np.array([r[k] for r in real])
        t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v))) if v.std(ddof=1) else 0
        print(f"    {lab:>5} average {v.mean():+.3f}   positive in "
              f"{100 * (v > 0).mean():.0f}% of folds   t {t:+.2f}")

    rng = np.random.default_rng(5)
    nulls = {"ridge": [], "gbm": []}
    for _ in range(5):
        for r in run(rng.permutation(y)):
            nulls["ridge"].append(r["ridge"])
            nulls["gbm"].append(r["gbm"])
    print("\n  shuffled mornings, five times through -- what nothing looks like:")
    for k, lab in (("ridge", "plain"), ("gbm", "tree")):
        v = np.array(nulls[k])
        print(f"    {lab:>5} average {v.mean():+.3f}   positive in "
              f"{100 * (v > 0).mean():.0f}% of folds")

    print("\n" + "=" * 80)
    print("3. THE SIMPLEST VERSION OF THE SAME QUESTION")
    print("=" * 80)
    print("  Forget models. Split the mornings by one market number at a time,")
    print("  known the evening before, and see the hit rate on each side.")
    print(f"\n  {'market number':>24} {'low half':>12} {'high half':>12} "
          f"{'gap':>8} {'±':>7}")
    for m in market:
        med = day[m].median()
        # df already carries the market columns; merging them back in would
        # collide and rename them
        a = df[df[m] <= med][TARGET]
        b = df[df[m] > med][TARGET]
        gap = b.mean() - a.mean()
        se = np.sqrt(a.mean() * (1 - a.mean()) / len(a)
                     + b.mean() * (1 - b.mean()) / len(b))
        tag = "real" if abs(gap) > 1.96 * se else ""
        print(f"  {m:>24} {100 * a.mean():>11.1f}% {100 * b.mean():>11.1f}% "
              f"{100 * gap:>+8.1f} {100 * 1.96 * se:>6.1f} {tag}")
    print("\n  These margins treat entries as independent and they are not --")
    print("  entries on one morning share a fate, which is the whole finding.")
    print("  Halve their apparent width in your head before believing them.")


if __name__ == "__main__":
    main()
