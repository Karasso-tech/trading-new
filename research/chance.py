"""Can anything tell in advance which trigger is more likely to work?

READ-ONLY. Reads research/dataset.json, prints numbers, changes nothing.

The question, stated exactly: of all the entries the system named, some reached
+2R before their stop and some did not. 34.2% did. Is there any number, or any
combination of numbers, known BEFORE the entry, that separates the two -- so
that a chosen tenth of them wins meaningfully more often than 34.2%.

This is a chance question, not a size question, and it gets the tools built for
chance: a classifier, and the area under the curve, which is exactly "pick one
winner and one loser at random -- how often does the model rate the winner
higher". 0.50 is a coin. 0.55 would be worth having.

The same guards as everything else here, because they are what killed every
earlier candidate:

  * walk forward, train on the past only, with the overlap purged out
  * a shuffled control through the identical machinery
  * non-overlapping days when a t is quoted, since a 40-day answer measured on
    consecutive days is nearly the same answer twice
  * per fold and per year, never pooled into one flattering number

And the part that answers the question as it was actually asked. The system
does not choose between a trade and no trade -- it chooses between the
candidates in front of it. So the last section ranks our own entries against
EACH OTHER inside the same week, which is what a screener really does.

    python research/chance.py
    python research/chance.py --rung 1
"""

from __future__ import annotations

import argparse
import json
import pathlib
import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")

HERE = pathlib.Path(__file__).resolve().parent
ANSWERS = ("mfe_atr_", "mae_atr_", "ret_atr_", "exret_atr_", "edge_ratio_", "win_")
NOT_FEATURES = {"ticker", "fired_date", "built", "setup", "regime_at_fire",
                "grade_at_fire", "stop_basis", "r_actual", "entry_px",
                "risk_per_share"}
CATEGORICAL = ("setup", "regime_at_fire", "stop_basis")
FOLDS = 8
HORIZON = 40


def load():
    d = json.loads((HERE / "dataset.json").read_text(encoding="utf-8"))
    df = pd.DataFrame(d["rows"])
    df["fired_date"] = pd.to_datetime(df["fired_date"])
    return df.sort_values("fired_date").reset_index(drop=True)


def design(df, feats):
    X = df[feats].copy()
    for c in CATEGORICAL:
        if c in df.columns:
            X = pd.concat([X, pd.get_dummies(df[c].fillna("none"), prefix=c,
                                             dtype=float)], axis=1)
    return X


def walk(df, feats, target, shuffle=False):
    d = df.dropna(subset=[target]).reset_index(drop=True)
    X = design(d, feats)
    y = d[target].to_numpy(int)
    if shuffle:
        y = np.random.default_rng(3).permutation(y)
    dates = d["fired_date"]
    edges = pd.date_range(dates.min(), dates.max(), periods=FOLDS + 2)[1:-1]
    purge = pd.Timedelta(days=int(HORIZON * 1.5))
    pred = np.full(len(d), np.nan)
    fold_id = np.full(len(d), np.nan)
    rows = []
    for k, cut in enumerate(edges):
        nxt = edges[k + 1] if k + 1 < len(edges) else dates.max() + pd.Timedelta(days=1)
        tr = (dates < (cut - purge)).values
        te = ((dates >= cut) & (dates < nxt)).values
        if tr.sum() < 500 or te.sum() < 150 or len(set(y[tr])) < 2:
            continue
        lin = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                            LogisticRegressionCV(Cs=8, max_iter=2000, cv=3))
        lin.fit(X[tr], y[tr])
        pl = lin.predict_proba(X[te])[:, 1]
        gbm = HistGradientBoostingClassifier(max_depth=3, max_iter=300,
                                             learning_rate=0.05, min_samples_leaf=60,
                                             l2_regularization=1.0, random_state=0)
        gbm.fit(X[tr], y[tr])
        pg = gbm.predict_proba(X[te])[:, 1]
        pred[te] = pg
        fold_id[te] = k
        yt = y[te]
        if len(set(yt)) < 2:
            continue
        rows.append({
            "fold": k + 1, "n": int(te.sum()),
            "from": str(dates[te].min().date()), "to": str(dates[te].max().date()),
            "base": 100 * yt.mean(),
            "auc_lin": roc_auc_score(yt, pl), "auc_gbm": roc_auc_score(yt, pg),
        })
    d["pred"] = pred
    d["fold"] = fold_id
    # Each fold trains its own model, and folds sit in periods whose base rates
    # run from 30% to 52%. A fold's raw probabilities therefore sit at a
    # different level from its neighbours', and pooling them puts most of the
    # "top" bucket inside whichever fold happened to be optimistic -- which
    # measures the calendar, not the model. Ranking inside each fold first
    # removes the level and keeps the ordering, which is the only part being
    # tested. Found the hard way: the un-ranked version showed a large, tidy,
    # entirely fake inversion.
    d["pred_rank"] = d.groupby("fold")["pred"].rank(pct=True)
    return rows, d.dropna(subset=["pred"])


def show(name, rows):
    if not rows:
        print(f"  {name}: no usable folds")
        return
    print(f"\n  {name}")
    print(f"    {'fold':>5} {'test window':>24} {'n':>6} {'base rate':>10} "
          f"{'plain model':>12} {'tree model':>11}")
    for r in rows:
        print(f"    {r['fold']:>5} {r['from']} .. {r['to']:>10} {r['n']:>6} "
              f"{r['base']:>9.1f}% {r['auc_lin']:>12.3f} {r['auc_gbm']:>11.3f}")
    for k, label in (("auc_lin", "plain"), ("auc_gbm", "tree")):
        v = np.array([r[k] for r in rows])
        t = (v.mean() - 0.5) / (v.std(ddof=1) / np.sqrt(len(v))) if v.std(ddof=1) else 0
        print(f"    {label:>5} average {v.mean():.3f}   above a coin in "
              f"{100 * (v > 0.5).mean():.0f}% of folds   t {t:+.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rung", type=float, default=2.0,
                    help="how far the target sits, in this idea's own risk units")
    args = ap.parse_args()
    target = f"win_{args.rung:g}r"

    df = load()
    feats = [c for c in df.columns
             if c not in NOT_FEATURES and not any(c.startswith(a) for a in ANSWERS)
             and pd.api.types.is_numeric_dtype(df[c])]
    d = df.dropna(subset=[target])
    print(f"{len(d)} entries that resolved, {d['ticker'].nunique()} companies")
    print(f"target: reached +{args.rung:g}R before the stop. "
          f"It happened {100 * d[target].mean():.1f}% of the time.")
    print(f"{len(feats)} numbers available before the entry\n")

    print("=" * 84)
    print("CAN A MODEL PICK THE ONES MORE LIKELY TO WORK?")
    print("=" * 84)
    print("  The score is: take one that worked and one that did not, at random.")
    print("  How often does the model rate the one that worked higher?")
    print("  0.500 is a coin. 0.550 would be worth having.")
    rows, scored = walk(df, feats, target)
    show("REAL ANSWERS", rows)
    nrows, _ = walk(df, feats, target, shuffle=True)
    show("SHUFFLED ANSWERS -- what nothing looks like", nrows)

    print("\n" + "=" * 84)
    print("AND IN PLAIN TERMS: HOW OFTEN DOES EACH FIFTH ACTUALLY WORK?")
    print("=" * 84)
    s = scored.dropna(subset=[target]).copy()
    s["b"] = pd.qcut(s["pred_rank"], 5, labels=False, duplicates="drop")
    base = 100 * s[target].mean()
    print(f"\n  base rate across everything: {base:.1f}%")
    print(f"\n  {'group':>6} {'n':>6} {'worked':>9} {'vs base':>9} {'real R':>9}")
    for b in sorted(s["b"].dropna().unique()):
        g = s[s["b"] == b]
        w = 100 * g[target].mean()
        print(f"  {int(b) + 1:>6} {len(g):>6} {w:>8.1f}% {w - base:>+8.1f} "
              f"{g['r_actual'].mean():>+9.3f}")
    hi = s[s["b"] == s["b"].max()][target]
    lo = s[s["b"] == 0][target]
    diff = hi.mean() - lo.mean()
    se = np.sqrt(hi.var() / len(hi) + lo.var() / len(lo))
    print(f"\n  best fifth minus worst fifth: {100 * diff:+.1f} points "
          f"±{100 * 1.96 * se:.1f}  "
          f"{'real' if abs(diff) > 1.96 * se else 'inside the noise'}")

    print("\n  by year, because one good year is one year:")
    s["yr"] = s["fired_date"].dt.year
    print(f"    {'year':>6} {'best fifth':>20} {'worst fifth':>20} {'gap':>8}")
    for y, g in s.groupby("yr"):
        a = g[g["b"] == g["b"].max()][target]
        b = g[g["b"] == 0][target]
        if len(a) < 30 or len(b) < 30:
            print(f"    {y:>6} {'too thin':>20}")
            continue
        print(f"    {y:>6} {100 * a.mean():>13.1f}% (n{len(a):>3}) "
              f"{100 * b.mean():>13.1f}% (n{len(b):>3}) "
              f"{100 * (a.mean() - b.mean()):>+8.1f}")

    print("\n" + "=" * 84)
    print("THE QUESTION AS IT IS ACTUALLY ASKED: RANK THIS WEEK'S CANDIDATES")
    print("=" * 84)
    print("  The system never chooses between a trade and no trade. It chooses")
    print("  between the ideas in front of it. So: inside each week, was the")
    print("  model's top-rated candidate more likely to work than the others?")
    s["wk"] = s["fired_date"].dt.to_period("W")
    picked, rest, weeks = [], [], 0
    for w, g in s.groupby("wk"):
        if len(g) < 4:
            continue
        weeks += 1
        top = g.loc[g["pred_rank"].idxmax()]
        picked.append(top[target])
        rest.extend(g.drop(top.name)[target].tolist())
    picked, rest = np.array(picked, float), np.array(rest, float)
    if len(picked) > 30:
        diff = picked.mean() - rest.mean()
        se = np.sqrt(picked.var() / len(picked) + rest.var() / len(rest))
        print(f"\n  {weeks} weeks with at least 4 candidates")
        print(f"    the model's pick worked {100 * picked.mean():.1f}% of the time")
        print(f"    everything else worked  {100 * rest.mean():.1f}% of the time")
        print(f"    gap {100 * diff:+.1f} points ±{100 * 1.96 * se:.1f}  "
              f"{'real' if abs(diff) > 1.96 * se else 'inside the noise'}")
        print("\n    (weeks are independent of each other here, which is why this")
        print("     margin can be read straight, unlike the day-by-day ones.)")


if __name__ == "__main__":
    main()
