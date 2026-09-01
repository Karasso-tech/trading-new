"""The professional version of the same question: can the model rank the stocks
against EACH OTHER on the same morning?

READ-ONLY. Reads the cached universe table, prints numbers, changes nothing.

Why the earlier number was not enough, and this is not a formality.

Every IC so far pooled a whole test period together. Pooled like that, a model
that knows nothing about which stock to buy can still score well by knowing
which WEEK to buy -- the day effect swamps everything, because on a strong day
almost every name is up. And the reverse trap is worse: a model that really can
rank stocks against each other can score ZERO pooled, if its within-day skill
is buried under day-to-day market swings it cannot see.

So the pooled number cannot distinguish "no skill" from "skill hidden by
market noise", and the whole conclusion rests on knowing which it is.

The fix is what cross-sectional equity work has always done: score the model
separately on each individual day, comparing only the names quoted that same
morning, then look at the sequence of daily scores. The market's own move drops
out completely -- it is the same for everyone in the comparison.

Reported the standard way: the average daily score, how steady it is, and the
share of days it points the right way. Against a shuffled control that goes
through exactly the same machinery.

    python research/daily_ic.py
    python research/daily_ic.py --target ret_atr_20
"""

from __future__ import annotations

import argparse
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
CACHE = HERE / "universe.parquet"
ANSWERS = ("mfe_atr_", "mae_atr_", "ret_atr_", "exret_atr_", "edge_ratio_")
MIN_NAMES = 50          # a day with fewer names cannot be ranked meaningfully


def predictions(df, feats, target, horizon, folds=8, shuffle=False):
    """Walk forward, keep every held-out prediction with its date."""
    d = df.dropna(subset=[target]).reset_index(drop=True)
    X, y, dates = d[feats], d[target].to_numpy(float), d["fired_date"]
    if shuffle:
        # shuffled WITHIN each day, so the day effect is left completely intact
        # and only the stock-level answer is destroyed. Shuffling globally would
        # also destroy the day effect and make the control too easy to beat.
        y = y.copy()
        for _, ix in d.groupby("fired_date").groups.items():
            ix = np.asarray(ix)
            y[ix] = np.random.default_rng(len(ix)).permutation(y[ix])
    edges = pd.date_range(dates.min(), dates.max(), periods=folds + 2)[1:-1]
    purge = pd.Timedelta(days=int(horizon * 1.5))
    out = np.full(len(d), np.nan)
    which = {}
    for k, cut in enumerate(edges):
        nxt = edges[k + 1] if k + 1 < len(edges) else dates.max() + pd.Timedelta(days=1)
        tr = (dates < (cut - purge)).values
        te = ((dates >= cut) & (dates < nxt)).values
        if tr.sum() < 2000 or te.sum() < 500:
            continue
        for name, m in (("ridge", make_pipeline(SimpleImputer(strategy="median"),
                                                StandardScaler(),
                                                RidgeCV(alphas=np.logspace(-1, 4, 20)))),
                        ("gbm", HistGradientBoostingRegressor(
                            max_depth=4, max_iter=400, learning_rate=0.05,
                            min_samples_leaf=200, l2_regularization=1.0,
                            random_state=0))):
            m.fit(X[tr], y[tr])
            which.setdefault(name, np.full(len(d), np.nan))[te] = m.predict(X[te])
    res = d[["ticker", "fired_date"]].copy()
    res["actual"] = y
    for name, p in which.items():
        res[name] = p
    return res.dropna(subset=list(which))


def daily(res, col, horizon=None, spacing=None):
    """One rank correlation per day, then the sequence of them.

    `spacing` is the correction that decides this whole investigation.

    A 20-day answer measured on consecutive mornings reuses 19 of the same 20
    days. Two neighbouring daily scores are therefore almost the same number,
    and counting 1,000 of them as 1,000 independent facts inflates every t by
    roughly the square root of the overlap -- about four and a half times here.
    That is precisely the mistake that made earlier passes look conclusive.

    So when a spacing is given, only every Nth trading day is scored, with N
    set to the horizon. The remaining days share no future with each other and
    the t below means what it says."""
    days = sorted(res["fired_date"].unique())
    if spacing:
        days = days[::spacing]
    keep = set(days)
    per = []
    for day, g in res.groupby("fired_date"):
        if day not in keep or len(g) < MIN_NAMES:
            continue
        ic = stats.spearmanr(g[col], g["actual"]).statistic
        if not np.isnan(ic):
            per.append((day, ic, len(g)))
    return per


def report(name, res, col, spacing=None):
    per = daily(res, col, spacing=spacing)
    if len(per) < 20:
        print(f"  {name}: too few usable days")
        return
    v = np.array([p[1] for p in per])
    t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))
    print(f"  {name:>34}  average daily score {v.mean():+.4f}   "
          f"steadiness {v.mean() / v.std(ddof=1):+.3f}   "
          f"right way on {100 * (v > 0).mean():.0f}% of days   t {t:+.2f}")
    # by year, because a score that only worked in one year is one year
    s = pd.DataFrame(per, columns=["day", "ic", "n"])
    s["yr"] = pd.to_datetime(s["day"]).dt.year
    parts = []
    for y, g in s.groupby("yr"):
        parts.append(f"{y} {g['ic'].mean():+.3f}")
    print(f"  {'':>34}  {'  '.join(parts)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="exret_atr_20")
    args = ap.parse_args()
    horizon = int(args.target.rsplit("_", 1)[1])

    df = pd.read_parquet(CACHE)
    feats = [c for c in df.columns
             if c not in ("ticker", "fired_date")
             and not any(c.startswith(a) for a in ANSWERS)
             and pd.api.types.is_numeric_dtype(df[c])]
    print(f"{len(df)} days, {df['ticker'].nunique()} companies, {len(feats)} features")
    print(f"target {args.target}, at least {MIN_NAMES} names compared per day\n")

    print("=" * 96)
    print("CAN THE MODEL RANK STOCKS AGAINST EACH OTHER ON THE SAME MORNING?")
    print("=" * 96)
    print("  A steadiness of 0.05 is a usable signal in this business.")
    print("  0.10 is a good one. Below 0.02 is not distinguishable from luck.\n")
    # rows in the cache are every 5th trading day, so N non-overlapping
    # observations of a `horizon`-day answer need every (horizon/5)th row
    spacing = max(int(round(horizon / 5)), 1)
    real = predictions(df, feats, args.target, horizon)
    null = predictions(df, feats, args.target, horizon, shuffle=True)

    print("  -- every sampled day, windows overlapping. The t here is WRONG,")
    print("     inflated by the overlap. Shown only so the correction is visible.")
    for col in ("ridge", "gbm"):
        report(f"real answers, {col}", real, col)
    for col in ("ridge", "gbm"):
        report(f"shuffled, {col}", null, col)
    print(f"\n  -- every {spacing * 5}th trading day only, so no two windows share a day.")
    print("     This is the honest version.")
    for col in ("ridge", "gbm"):
        report(f"real answers, {col}", real, col, spacing=spacing)
    for col in ("ridge", "gbm"):
        report(f"shuffled, {col}", null, col, spacing=spacing)

    print("\n" + "=" * 96)
    print("AND THE PLAIN NUMBERS, SAME TREATMENT, NO MODEL AT ALL")
    print("=" * 96)
    d = df.dropna(subset=[args.target]).copy()
    d = d.rename(columns={args.target: "actual"})
    rows = []
    for f in feats:
        per = daily(d.dropna(subset=[f]), f, spacing=spacing)
        if len(per) < 30:
            continue
        v = np.array([p[1] for p in per])
        t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))
        rows.append((abs(t), f, v.mean(), v.mean() / v.std(ddof=1),
                     100 * (v > 0).mean(), t))
    rows.sort(reverse=True)
    print(f"\n  {'number':>26} {'avg daily':>11} {'steadiness':>12} "
          f"{'right way':>11} {'t':>8}")
    for _, f, m, ir, pos, t in rows[:15]:
        star = "  <--" if abs(ir) >= 0.05 else ""
        print(f"  {f:>26} {m:>+11.4f} {ir:>+12.3f} {pos:>10.0f}% {t:>+8.2f}{star}")
    print(f"\n  {len(rows)} numbers. An arrow needs steadiness of 0.05 or better.")
    print("  t alone is not enough here: with a thousand days almost anything")
    print("  clears t=2, which is exactly how the earlier passes fooled themselves.")


if __name__ == "__main__":
    main()
