"""Does anything knowable at entry predict whether target 1 comes before the stop?

Three models, judged on years they were never shown:

    simple    logistic regression -- every input gets one weight you can read
    advanced  gradient boosting   -- finds combinations a straight line cannot
    blend     the average of the two probabilities

Split by TIME, never at random. Rows here are not independent: 10,107 trades sit
on 1,352 days, and the collection run already showed 117 losses in a row inside
nine trading days. A random split would put trades from the same week on both
sides of the line, the model would recognise the week rather than the setup, and
the score would be a lie. Train is 2019-2022 (the 2022 break included, on
purpose). Test is 2023-2024. 2025 onward is still sealed and is not touched here.

The advanced model is only worth keeping if it beats the simple one on the test
years. Being better on the training years means nothing at all.

And the thing the owner asked for last and cares about most: **if the model says
70%, roughly 70 out of 100 such trades must actually have worked.** Sorting
trades best-to-worst is not enough -- the number on the label has to be true.
That is what the reliability table below measures, and a model can rank well and
still be badly wrong about the level.
"""

from __future__ import annotations

import argparse
import sqlite3
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

import features as F

warnings.filterwarnings("ignore")
DB = Path(__file__).resolve().parent / "data" / "stage0.db"

TRAIN_END = "2023-01-01"      # train is everything before this
TEST_END = "2025-01-01"       # test is up to here; after it stays sealed


def load(run_id: int = 1) -> pd.DataFrame:
    """One collection run's finished trades, with their entry-time features.

    The run_id is NOT optional in spirit. `features` holds every run, and the
    wider-universe run re-simulates the same S&P 500 names, so loading without a
    filter returns the S&P 500 trades TWICE alongside the mid- and small-caps.
    That silently doubled part of the sample and produced two flatly
    contradictory analyses of the same question on 2026-09-02.

    The ORDER BY is fully determined -- date, then ticker, then trade id -- and
    that matters more than it looks. The boosted model calibrates itself on
    time-ordered folds of these rows, so ties broken differently produce
    slightly different probabilities, a slightly different ranking, different
    trades selected, and compounding turns the difference into real money. On
    2026-09-02 rebuilding the SAME model on the SAME data returned $198,019
    where it had returned $180,359, purely because the features table had been
    rewritten and the within-day row order changed. Without a total order, every
    account figure carries about +/-$20k of meaningless wobble.
    """
    conn = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT f.* FROM features f JOIN trades t ON t.id = f.trade_id"
        " WHERE f.label IS NOT NULL AND t.run_id = ?"
        " ORDER BY f.entry_date, f.ticker, f.trade_id",
        conn, params=(run_id,))
    conn.close()
    return df


def make_simple() -> Pipeline:
    return Pipeline([
        ("prep", ColumnTransformer([
            ("num", Pipeline([("fill", SimpleImputer(strategy="median")),
                              ("scale", StandardScaler())]), F.NUMERIC),
            ("cat", OneHotEncoder(handle_unknown="ignore", min_frequency=30),
             F.CATEGORICAL)])),
        ("clf", LogisticRegression(max_iter=2000, C=0.5)),
    ])


def make_advanced() -> Pipeline:
    inner = Pipeline([
        ("prep", ColumnTransformer([
            ("num", "passthrough", F.NUMERIC),
            ("cat", OneHotEncoder(handle_unknown="ignore", min_frequency=30),
             F.CATEGORICAL)])),
        ("clf", HistGradientBoostingClassifier(
            max_depth=3, max_iter=250, learning_rate=0.05,
            min_samples_leaf=60, l2_regularization=1.0, random_state=0)),
    ])
    # Boosted trees come out over-confident. Isotonic calibration is fitted
    # inside the TRAINING years only, on time-ordered folds, so the test years
    # stay untouched by it.
    return CalibratedClassifierCV(inner, method="isotonic", cv=TimeSeriesSplit(n_splits=4))


def reliability(y, p, bins=10) -> pd.DataFrame:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    rows = []
    for b in range(bins):
        m = idx == b
        if not m.any():
            continue
        rows.append({"band": f"{edges[b]:.0%}-{edges[b+1]:.0%}", "n": int(m.sum()),
                     "model_says": p[m].mean(), "really_won": y[m].mean()})
    return pd.DataFrame(rows)


def ece(y, p, bins=10) -> float:
    """Expected calibration error: the average gap between what the model
    promised and what happened, weighted by how many trades sat in each band."""
    table = reliability(y, p, bins)
    gap = (table["model_says"] - table["really_won"]).abs()
    return float((gap * table["n"]).sum() / table["n"].sum())


def report(name: str, y, p, r, base: float) -> dict:
    auc = roc_auc_score(y, p)
    brier = brier_score_loss(y, p)
    flat = brier_score_loss(y, np.full_like(p, base))
    print(f"\n{'=' * 74}\n{name}\n{'-' * 74}")
    print(f"  ranking (AUC, 0.5 = coin flip)      {auc:.3f}")
    print(f"  honesty of the number (error)       {ece(y, p):.3f}"
          f"   -- average gap between promise and reality")
    print(f"  Brier score (lower is better)       {brier:.4f}"
          f"   vs {flat:.4f} for always saying '{base:.0%}'")
    table = reliability(y, p)
    print(f"\n  {'model says':>12s} {'trades':>8s} {'promised':>10s} {'really won':>11s}"
          f" {'gap':>7s} {'avg R':>8s}")
    for _, row in table.iterrows():
        m = (p >= float(row["band"].split("-")[0].rstrip("%")) / 100) & \
            (p < float(row["band"].split("-")[1].rstrip("%")) / 100 + 1e-9)
        avg_r = r[m].mean() if m.any() else float("nan")
        print(f"  {row['band']:>12s} {row['n']:>8,} {row['model_says']:>9.1%} "
              f"{row['really_won']:>10.1%} {row['model_says']-row['really_won']:>+7.1%}"
              f" {avg_r:>+8.2f}")
    return {"name": name, "auc": auc, "ece": ece(y, p), "brier": brier}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bins", type=int, default=10)
    args = ap.parse_args()

    df = load()
    train = df[df.entry_date < TRAIN_END]
    test = df[(df.entry_date >= TRAIN_END) & (df.entry_date < TEST_END)]
    base = train.label.mean()

    print(f"train {train.entry_date.min()} .. {train.entry_date.max()}   "
          f"{len(train):,} trades, {train.label.mean():.1%} won")
    print(f"test  {test.entry_date.min()} .. {test.entry_date.max()}   "
          f"{len(test):,} trades, {test.label.mean():.1%} won")
    print(f"\ninputs: {len(F.NUMERIC)} numeric + {len(F.CATEGORICAL)} categorical")
    print("requested inputs NOT available:")
    for m in F.MISSING:
        print(f"  - {m}")

    X_tr, y_tr = train[F.NUMERIC + F.CATEGORICAL], train.label.values
    X_te, y_te = test[F.NUMERIC + F.CATEGORICAL], test.label.values
    r_te = test.r_multiple.values

    simple = make_simple().fit(X_tr, y_tr)
    advanced = make_advanced().fit(X_tr, y_tr)
    p_simple = simple.predict_proba(X_te)[:, 1]
    p_advanced = advanced.predict_proba(X_te)[:, 1]
    p_blend = (p_simple + p_advanced) / 2

    # The bar every model has to clear: say the training win rate, every time.
    scores = [report("always says the same number (the bar to beat)", y_te,
                     np.full(len(y_te), base), r_te, base),
              report("simple -- logistic regression", y_te, p_simple, r_te, base),
              report("advanced -- gradient boosting, calibrated", y_te, p_advanced, r_te, base),
              report("blend -- the average of the two", y_te, p_blend, r_te, base)]

    print(f"\n{'=' * 74}\nside by side, on the test years only\n{'-' * 74}")
    print(f"  {'model':46s} {'AUC':>7s} {'honesty':>9s} {'Brier':>8s}")
    for s in scores:
        print(f"  {s['name']:46s} {s['auc']:>7.3f} {s['ece']:>9.3f} {s['brier']:>8.4f}")

    # What the simple model actually learned, in its own words.
    prep = simple.named_steps["prep"]
    names = list(prep.named_transformers_["num"].feature_names_in_) + \
        list(prep.named_transformers_["cat"].get_feature_names_out(F.CATEGORICAL))
    weights = pd.Series(simple.named_steps["clf"].coef_[0], index=names)
    print(f"\n{'=' * 74}\nwhat the simple model leans on (standardised weights)\n{'-' * 74}")
    for name, w in weights.reindex(weights.abs().sort_values(ascending=False).index).head(14).items():
        arrow = "raises the chance" if w > 0 else "lowers the chance"
        print(f"  {name:36s} {w:>+7.3f}   {arrow}")


if __name__ == "__main__":
    main()
