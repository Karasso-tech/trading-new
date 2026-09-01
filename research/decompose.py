"""The model predicts how far a trade runs UP. Is that an edge, or is it just
predicting how much the stock MOVES?

READ-ONLY. Prints numbers, changes nothing.

The finding to be pulled apart: a model trained on "how far up did it go in 20
days" scores an out-of-sample rank correlation of +0.12, positive in 7 of 8
walk-forward folds, against zero on shuffled answers. Meanwhile "where did it
end up, market removed" is dead, and so is up-versus-down.

That combination has one boring explanation and one useful one, and they
demand completely different responses:

  BORING -- it is a volatility forecast. Volatility clusters: a stock that has
  been moving keeps moving. A model that spots that predicts a big move up AND
  a big move down, because both are just "big move". Worth something for
  sizing and stop width, worth nothing for choosing.

  USEFUL -- it really does find trades that run further without falling
  further. Then it picks.

The test that separates them, and it is not subtle: rank every held-out trade
by what the up-model predicted, then look at what the top fifth and the bottom
fifth actually did -- up, down, and net. If the top fifth's downside is just as
deep as its upside is high, it is the boring one.

Then the question that decides whether any of this is worth building: does the
ranking move REAL R? Our exit sells into strength and lets a runner go, so a
trade that travels further should pay more even though R itself could never be
predicted directly.

    python research/decompose.py
"""

from __future__ import annotations

import json
import pathlib
import warnings

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance

warnings.filterwarnings("ignore")

HERE = pathlib.Path(__file__).resolve().parent
TARGET = "mfe_atr_20"
HORIZON = 20
FOLDS = 8

ANSWERS = ("mfe_atr_", "mae_atr_", "ret_atr_", "exret_atr_", "edge_ratio_")
NOT_FEATURES = {"ticker", "fired_date", "built", "setup", "regime_at_fire",
                "grade_at_fire", "stop_basis", "r_actual", "entry_px"}
CATEGORICAL = ("setup", "regime_at_fire", "stop_basis", "grade_at_fire")


def load():
    d = json.loads((HERE / "dataset.json").read_text(encoding="utf-8"))
    df = pd.DataFrame(d["rows"])
    df["fired_date"] = pd.to_datetime(df["fired_date"])
    return df.sort_values("fired_date").reset_index(drop=True)


def design(df):
    feats = [c for c in df.columns
             if c not in NOT_FEATURES and not any(c.startswith(a) for a in ANSWERS)
             and pd.api.types.is_numeric_dtype(df[c])]
    X = df[feats].copy()
    for c in CATEGORICAL:
        if c in df.columns:
            X = pd.concat([X, pd.get_dummies(df[c].fillna("none"), prefix=c,
                                             dtype=float)], axis=1)
    return X


def walk(df):
    """Same walk-forward as model.py, but keeping every held-out prediction so
    the rows behind them can be inspected."""
    d = df.dropna(subset=[TARGET]).reset_index(drop=True)
    X, y, dates = design(d), d[TARGET].to_numpy(float), d["fired_date"]
    edges = pd.date_range(dates.min(), dates.max(), periods=FOLDS + 2)[1:-1]
    purge = pd.Timedelta(days=int(HORIZON * 1.5))
    preds = np.full(len(d), np.nan)
    imps = []
    for k, cut in enumerate(edges):
        nxt = edges[k + 1] if k + 1 < len(edges) else dates.max() + pd.Timedelta(days=1)
        tr = (dates < (cut - purge)).values
        te = ((dates >= cut) & (dates < nxt)).values
        if tr.sum() < 500 or te.sum() < 100:
            continue
        m = HistGradientBoostingRegressor(max_depth=3, max_iter=300, learning_rate=0.05,
                                          min_samples_leaf=60, l2_regularization=1.0,
                                          random_state=0)
        m.fit(X[tr], y[tr])
        preds[te] = m.predict(X[te])
        if k == FOLDS // 2:      # one fold's worth of "what is it using"
            r = permutation_importance(m, X[te], y[te], n_repeats=5,
                                       random_state=0, scoring="r2")
            imps = sorted(zip(X.columns, r.importances_mean),
                          key=lambda kv: -kv[1])[:14]
    d["pred"] = preds
    return d.dropna(subset=["pred"]).reset_index(drop=True), imps


def quintiles(d, col="pred", n=5):
    return pd.qcut(d[col], n, labels=False, duplicates="drop")


def main():
    df = load()
    d, imps = walk(df)
    d["bucket"] = quintiles(d)
    print(f"{len(d)} held-out predictions, "
          f"{d['fired_date'].min().date()} .. {d['fired_date'].max().date()}")
    print(f"out-of-sample rank correlation with {TARGET}: "
          f"{stats.spearmanr(d['pred'], d[TARGET]).statistic:+.4f}")

    print("\n" + "=" * 86)
    print("A. IS IT AN EDGE, OR IS IT A VOLATILITY FORECAST?")
    print("=" * 86)
    print("  Trades sorted into five groups by what the up-model predicted,")
    print("  bottom group first. Every column is what ACTUALLY happened,")
    print("  in ATR, on data the model never saw.")
    print(f"\n  {'group':>6} {'n':>6} {'went up':>9} {'went down':>10} "
          f"{'ended at':>9} {'market out':>11} {'up+down':>9} {'up/down':>8}")
    for b in sorted(d["bucket"].dropna().unique()):
        g = d[d["bucket"] == b]
        up, dn = g["mfe_atr_20"].mean(), g["mae_atr_20"].mean()
        print(f"  {int(b) + 1:>6} {len(g):>6} {up:>+9.3f} {dn:>+10.3f} "
              f"{g['ret_atr_20'].mean():>+9.3f} {g['exret_atr_20'].mean():>+11.3f} "
              f"{up + abs(dn):>9.3f} {up / abs(dn):>8.3f}")
    print("\n  'up+down' is total travel -- pure volatility, no direction in it.")
    print("  'up/down' is the only column that can show an edge: more room up")
    print("  than heat down. If it is flat across the five groups while total")
    print("  travel climbs, the model is a volatility forecast and nothing more.")

    lo, hi = d[d["bucket"] == 0], d[d["bucket"] == d["bucket"].max()]
    print(f"\n  top group minus bottom group:")
    for c, name in (("mfe_atr_20", "went up"), ("mae_atr_20", "went down"),
                    ("ret_atr_20", "ended at"), ("exret_atr_20", "market out")):
        a, b = hi[c].mean(), lo[c].mean()
        se = np.sqrt(hi[c].var() / len(hi) + lo[c].var() / len(lo))
        tag = "real" if abs(a - b) > 1.96 * se else "inside the noise"
        print(f"    {name:>12} {a - b:>+8.3f}  ±{1.96 * se:.3f}  {tag}")

    print("\n" + "=" * 86)
    print("B. WHAT IS THE MODEL ACTUALLY LOOKING AT?")
    print("=" * 86)
    print("  How much accuracy is lost when each number is scrambled, one fold.")
    for f, v in imps:
        print(f"    {f:>26} {v:>+8.4f}")

    print("\n" + "=" * 86)
    print("C. DOES THE RANKING MOVE REAL MONEY?")
    print("=" * 86)
    print("  Same five groups, but scored on the R our own exit engine produced.")
    print("  This is the only question that pays.")
    r = d.dropna(subset=["r_actual"])
    print(f"\n  {'group':>6} {'n':>6} {'mean R':>9} {'median R':>10} "
          f"{'win rate':>9} {'total R':>9}")
    for b in sorted(r["bucket"].dropna().unique()):
        g = r[r["bucket"] == b]
        print(f"  {int(b) + 1:>6} {len(g):>6} {g['r_actual'].mean():>+9.3f} "
              f"{g['r_actual'].median():>+10.3f} "
              f"{100 * (g['r_actual'] > 0).mean():>8.0f}% {g['r_actual'].sum():>+9.1f}")
    a = r[r["bucket"] == r["bucket"].max()]["r_actual"]
    b = r[r["bucket"] == 0]["r_actual"]
    se = np.sqrt(a.var() / len(a) + b.var() / len(b))
    print(f"\n  top minus bottom: {a.mean() - b.mean():+.3f}R  ±{1.96 * se:.3f}  "
          f"{'real' if abs(a.mean() - b.mean()) > 1.96 * se else 'inside the noise'}")

    print("\n  Per fold, so one good year cannot carry it:")
    r = r.copy()
    r["yr"] = r["fired_date"].dt.year
    print(f"    {'year':>6} {'top fifth':>22} {'bottom fifth':>22} {'gap':>8}")
    for y in sorted(r["yr"].unique()):
        g = r[r["yr"] == y]
        t = g[g["bucket"] == g["bucket"].max()]["r_actual"]
        bo = g[g["bucket"] == 0]["r_actual"]
        if len(t) < 20 or len(bo) < 20:
            print(f"    {y:>6} {'too thin':>22}")
            continue
        print(f"    {y:>6} {t.mean():>+13.3f} (n{len(t):>4}) "
              f"{bo.mean():>+13.3f} (n{len(bo):>4}) {t.mean() - bo.mean():>+8.3f}")


if __name__ == "__main__":
    main()
