"""Is there anything to predict at all -- and can a model find it?

READ-ONLY. Reads research/dataset.json, prints numbers, changes nothing.

Three questions, in the only order that makes sense:

  1. Is the new answer column less hopeless than R? R was 74% identical values
     sitting on -1.00, which is the stop, not the stock. If the forward path is
     also degenerate there is nothing to model and we stop here.

  2. Does any SINGLE number carry information? Measured by rank correlation --
     "when this number is higher, does the stock tend to move further" --
     computed inside each calendar quarter and then looked at ACROSS quarters.
     A number that works in 4 quarters out of 20 is noise no matter how big it
     looks pooled.

  3. Can a model beat every single number, out of sample, walking forward?

The honesty machinery, all of it necessary:

  * WALK FORWARD. Train on the past, predict the future, move, repeat. Never a
    random split: with overlapping windows and clustered dates, a random split
    trains on tomorrow and tests on yesterday.
  * PURGE. A 40-day answer for a trade entered on the last training day is not
    known until 40 days later. Those days are cut out between train and test,
    or the model is told the future.
  * A NULL RUN. The same pipeline with the answers shuffled. Whatever score
    that produces is what "nothing" looks like here, and any real score must
    clear it.
  * PER FOLD, NOT POOLED. One good fold in eight is a fold, not a finding.

    python research/model.py
    python research/model.py --target exret_atr_20
"""

from __future__ import annotations

import argparse
import json
import pathlib
import warnings

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import RidgeCV
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer

warnings.filterwarnings("ignore")

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "dataset.json"

# Columns that are answers, or that leak one, and can never be inputs.
ANSWERS = ("mfe_atr_", "mae_atr_", "ret_atr_", "exret_atr_", "edge_ratio_")
NOT_FEATURES = {"ticker", "fired_date", "built", "setup", "regime_at_fire",
                "grade_at_fire", "stop_basis", "r_actual", "entry_px"}

CATEGORICAL = ("setup", "regime_at_fire", "stop_basis", "grade_at_fire")


def load() -> pd.DataFrame:
    d = json.loads(DATA.read_text(encoding="utf-8"))
    df = pd.DataFrame(d["rows"])
    df["fired_date"] = pd.to_datetime(df["fired_date"])
    return df.sort_values("fired_date").reset_index(drop=True)


def feature_columns(df) -> list:
    cols = []
    for c in df.columns:
        if c in NOT_FEATURES or any(c.startswith(a) for a in ANSWERS):
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            cols.append(c)
    return cols


def design(df, feats):
    """Numbers plus one-hot columns for the few word-valued fields."""
    X = df[feats].copy()
    for c in CATEGORICAL:
        if c in df.columns:
            d = pd.get_dummies(df[c].fillna("none"), prefix=c, dtype=float)
            X = pd.concat([X, d], axis=1)
    return X


# ------------------------------------------------------- 1. is there a target
def describe_targets(df):
    print("=" * 78)
    print("1. IS THERE ANYTHING TO PREDICT?")
    print("=" * 78)
    print("  A column that is 74% one value cannot be modelled. R was.")
    print(f"\n  {'column':>16} {'n':>6} {'mean':>8} {'sd':>7} {'median':>8} "
          f"{'most common value':>20}")
    cols = ["r_actual"] + [f"{p}20" for p in
                           ("mfe_atr_", "mae_atr_", "ret_atr_", "exret_atr_", "edge_ratio_")]
    for c in cols:
        if c not in df:
            continue
        v = df[c].dropna()
        top = v.round(2).value_counts()
        share = 100 * top.iloc[0] / len(v)
        print(f"  {c:>16} {len(v):>6} {v.mean():>+8.3f} {v.std():>7.3f} "
              f"{v.median():>+8.3f}   {top.index[0]:>+7.2f} is {share:>4.1f}%")
    print("\n  Reading: R piles onto one number because that number is our stop.")
    print("  The path columns spread out, so they can carry information.")


# --------------------------------------------- 2. does any one number carry it
def single_numbers(df, feats, target):
    print("\n" + "=" * 78)
    print(f"2. DOES ANY SINGLE NUMBER CARRY INFORMATION?   target = {target}")
    print("=" * 78)
    print("  Rank correlation inside each calendar quarter, then read across")
    print("  quarters. 'hit rate' is the share of quarters with the same sign.")
    df = df.dropna(subset=[target]).copy()
    df["q"] = df["fired_date"].dt.to_period("Q")
    quarters = [q for q, g in df.groupby("q") if len(g) >= 60]
    rows = []
    for f in feats:
        per = []
        for q in quarters:
            g = df[df["q"] == q]
            a, b = g[f], g[target]
            ok = a.notna() & b.notna()
            if ok.sum() < 40 or a[ok].nunique() < 5:
                continue
            per.append(stats.spearmanr(a[ok], b[ok]).statistic)
        if len(per) < 12:
            continue
        per = np.array(per)
        m = per.mean()
        hit = 100 * (per > 0).mean() if m > 0 else 100 * (per < 0).mean()
        # t over quarters, not over trades: the unit of independent evidence
        # here is a quarter, and treating 9,000 overlapping trades as 9,000
        # independent facts is how every previous pass overstated itself.
        t = m / (per.std(ddof=1) / np.sqrt(len(per))) if per.std(ddof=1) else 0
        rows.append((abs(t), f, m, hit, len(per), t))
    rows.sort(reverse=True)
    print(f"\n  {'number':>26} {'avg rank corr':>14} {'same sign':>11} "
          f"{'quarters':>9} {'t':>7}")
    for _, f, m, hit, n, t in rows[:18]:
        star = "  <--" if abs(t) >= 2.5 and hit >= 65 else ""
        print(f"  {f:>26} {m:>+14.4f} {hit:>10.0f}% {n:>9} {t:>+7.2f}{star}")
    print(f"\n  {len(rows)} numbers tested. An arrow needs t of 2.5 or more AND")
    print("  the same sign in at least 65% of quarters.")
    return [r[1] for r in rows if abs(r[5]) >= 2.5 and r[3] >= 65]


# ---------------------------------------------------- 3. can a model beat them
def walk_forward(df, feats, target, horizon, folds=8, shuffle=False, seed=0):
    """Expanding window. Train on everything before a date, test on the block
    after it, with the overlap purged out."""
    d = df.dropna(subset=[target]).reset_index(drop=True)
    X_all = design(d, feats)
    y_all = d[target].to_numpy(float)
    dates = d["fired_date"]

    if shuffle:
        y_all = np.random.default_rng(seed).permutation(y_all)

    edges = pd.date_range(dates.min(), dates.max(), periods=folds + 2)[1:-1]
    purge = pd.Timedelta(days=int(horizon * 1.5))  # calendar days for trading days
    out = []
    for k, cut in enumerate(edges):
        tr = dates < (cut - purge)
        te = (dates >= cut) & (dates < (edges[k + 1] if k + 1 < len(edges) else dates.max() + pd.Timedelta(days=1)))
        if tr.sum() < 500 or te.sum() < 100:
            continue
        Xtr, ytr = X_all[tr.values], y_all[tr.values]
        Xte, yte = X_all[te.values], y_all[te.values]

        ridge = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                              RidgeCV(alphas=np.logspace(-1, 4, 20)))
        ridge.fit(Xtr, ytr)
        pr = ridge.predict(Xte)

        gbm = HistGradientBoostingRegressor(
            max_depth=3, max_iter=300, learning_rate=0.05,
            min_samples_leaf=60, l2_regularization=1.0, random_state=0)
        gbm.fit(Xtr, ytr)
        pg = gbm.predict(Xte)

        out.append({
            "fold": k + 1, "train": int(tr.sum()), "test": int(te.sum()),
            "from": str(dates[te].min().date()), "to": str(dates[te].max().date()),
            "ridge_ic": stats.spearmanr(pr, yte).statistic,
            "gbm_ic": stats.spearmanr(pg, yte).statistic,
            "ridge_top_minus_bottom": decile_spread(pr, yte),
            "gbm_top_minus_bottom": decile_spread(pg, yte),
        })
    return out


def decile_spread(pred, actual):
    """What the model is worth in the units of the answer: the best fifth it
    picks, minus the worst fifth."""
    if len(pred) < 30:
        return np.nan
    order = np.argsort(pred)
    n = max(len(pred) // 5, 5)
    return actual[order[-n:]].mean() - actual[order[:n]].mean()


def report(name, folds):
    if not folds:
        print(f"  {name}: no usable folds")
        return
    print(f"\n  {name}")
    print(f"    {'fold':>5} {'test window':>24} {'n':>6} "
          f"{'ridge IC':>9} {'gbm IC':>9} {'ridge top-bot':>14} {'gbm top-bot':>12}")
    for f in folds:
        print(f"    {f['fold']:>5} {f['from']} .. {f['to']:>10} {f['test']:>6} "
              f"{f['ridge_ic']:>+9.4f} {f['gbm_ic']:>+9.4f} "
              f"{f['ridge_top_minus_bottom']:>+14.3f} {f['gbm_top_minus_bottom']:>+12.3f}")
    for key, label in (("ridge_ic", "ridge"), ("gbm_ic", "gbm")):
        v = np.array([f[key] for f in folds])
        pos = 100 * (v > 0).mean()
        t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v))) if v.std(ddof=1) else 0
        print(f"    {label:>5} average IC {v.mean():+.4f}   positive in "
              f"{pos:.0f}% of folds   t {t:+.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="exret_atr_20")
    ap.add_argument("--folds", type=int, default=8)
    args = ap.parse_args()
    horizon = int(args.target.rsplit("_", 1)[1])

    df = load()
    feats = feature_columns(df)
    print(f"{len(df)} entries, {df['ticker'].nunique()} companies, "
          f"{df['fired_date'].min().date()} .. {df['fired_date'].max().date()}")
    print(f"{len(feats)} numeric features + {len(CATEGORICAL)} word features")

    describe_targets(df)
    single_numbers(df, feats, args.target)

    print("\n" + "=" * 78)
    print(f"3. CAN A MODEL DO BETTER?   target = {args.target}")
    print("=" * 78)
    print("  IC is rank correlation between what the model predicted and what")
    print("  happened, on data it never saw. 'top-bot' is the model's best fifth")
    print("  minus its worst fifth, in ATR -- the number that would matter.")
    real = walk_forward(df, feats, args.target, horizon, args.folds)
    report("REAL ANSWERS", real)
    null = walk_forward(df, feats, args.target, horizon, args.folds, shuffle=True)
    report("SHUFFLED ANSWERS -- this is what nothing looks like", null)


if __name__ == "__main__":
    main()
