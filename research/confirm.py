"""The one surviving signal, put through everything that could still kill it.

READ-ONLY. Reads the cached universe table, prints numbers, changes nothing.

What survived: a gradient-boosted model, ranking stocks against each other on
the same morning, scored only on days whose 20-day windows do not overlap.
Average daily rank correlation +0.034, steady at +0.275, right way on 64% of
days, t +2.04, against a shuffled control flat at zero.

Why that is not yet an answer. t +2.04 rests on about 61 independent days, and
those 61 days are one arbitrary slice -- every 20th day counting from the
first. Nineteen other slices exist and were not looked at. Beyond that, this
is the survivor of a long search: several targets, two model families, two ways
of scoring. The survivor of a search is exactly the thing that most needs a
harder test, not a softer one.

Four tests, all of which it has to pass:

  1. EVERY SLICE. All twenty starting offsets, each one a clean non-overlapping
     sample. If the signal is real it is positive in nearly all of them. If it
     came from one lucky slice, the spread across offsets will show it.
  2. BLOCK BOOTSTRAP. Resample whole calendar months, not days, so the
     overlap and the clustering are carried into the confidence band instead of
     being assumed away.
  3. WORTH ANYTHING? Rank correlation is not money. What is the gap in ATR
     between the model's best tenth and its worst tenth, per year.
  4. DOES IT RANK OUR OWN ENTRIES? A universe-wide signal is only useful here
     if it separates the ideas the system actually produces.

    python research/confirm.py
"""

from __future__ import annotations

import json
import pathlib
import warnings

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.ensemble import HistGradientBoostingRegressor

warnings.filterwarnings("ignore")

HERE = pathlib.Path(__file__).resolve().parent
CACHE = HERE / "universe.parquet"
TARGET = "exret_atr_20"
HORIZON = 20
SPACING = 4          # cache rows are every 5th trading day -> every 20th day
MIN_NAMES = 50
ANSWERS = ("mfe_atr_", "mae_atr_", "ret_atr_", "exret_atr_", "edge_ratio_")


def predictions(df, feats, seed=0, depth=4, leaf=200, shuffle=False):
    d = df.dropna(subset=[TARGET]).reset_index(drop=True)
    X, y, dates = d[feats], d[TARGET].to_numpy(float), d["fired_date"]
    if shuffle:
        y = y.copy()
        for _, ix in d.groupby("fired_date").groups.items():
            ix = np.asarray(ix)
            y[ix] = np.random.default_rng(seed + len(ix)).permutation(y[ix])
    edges = pd.date_range(dates.min(), dates.max(), periods=10)[1:-1]
    purge = pd.Timedelta(days=int(HORIZON * 1.5))
    pred = np.full(len(d), np.nan)
    for k, cut in enumerate(edges):
        nxt = edges[k + 1] if k + 1 < len(edges) else dates.max() + pd.Timedelta(days=1)
        tr = (dates < (cut - purge)).values
        te = ((dates >= cut) & (dates < nxt)).values
        if tr.sum() < 2000 or te.sum() < 500:
            continue
        m = HistGradientBoostingRegressor(max_depth=depth, max_iter=400,
                                          learning_rate=0.05, min_samples_leaf=leaf,
                                          l2_regularization=1.0, random_state=seed)
        m.fit(X[tr], y[tr])
        pred[te] = m.predict(X[te])
    out = d[["ticker", "fired_date"]].copy()
    out["actual"], out["pred"] = y, pred
    return out.dropna(subset=["pred"])


def per_day(res):
    rows = []
    for day, g in res.groupby("fired_date"):
        if len(g) < MIN_NAMES:
            continue
        ic = stats.spearmanr(g["pred"], g["actual"]).statistic
        n = max(len(g) // 10, 5)
        o = np.argsort(g["pred"].to_numpy())
        a = g["actual"].to_numpy()
        rows.append({"day": day, "ic": ic, "n": len(g),
                     "spread": a[o[-n:]].mean() - a[o[:n]].mean()})
    return pd.DataFrame(rows).sort_values("day").reset_index(drop=True)


def summarise(v):
    v = np.asarray(v, float)
    v = v[~np.isnan(v)]
    if len(v) < 5:
        return None
    return {"n": len(v), "mean": v.mean(), "ir": v.mean() / v.std(ddof=1),
            "pos": 100 * (v > 0).mean(),
            "t": v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))}


def main():
    df = pd.read_parquet(CACHE)
    feats = [c for c in df.columns
             if c not in ("ticker", "fired_date")
             and not any(c.startswith(a) for a in ANSWERS)
             and pd.api.types.is_numeric_dtype(df[c])]
    res = predictions(df, feats)
    daily = per_day(res)
    print(f"{len(res)} held-out predictions on {len(daily)} scored days")

    # ------------------------------------------------------------------ 1
    print("\n" + "=" * 82)
    print("1. EVERY NON-OVERLAPPING SLICE, NOT JUST THE FIRST")
    print("=" * 82)
    print(f"  Each row is a clean sample: every {SPACING * 5}th trading day,")
    print("  started one day later than the row above. Twenty independent looks")
    print("  at the same claim.\n")
    print(f"  {'offset':>7} {'days':>6} {'avg IC':>9} {'steadiness':>12} "
          f"{'right way':>11} {'t':>7}")
    ts, means = [], []
    for off in range(SPACING * 5):
        s = summarise(daily["ic"].to_numpy()[off::SPACING * 5])
        if not s:
            continue
        ts.append(s["t"])
        means.append(s["mean"])
        print(f"  {off:>7} {s['n']:>6} {s['mean']:>+9.4f} {s['ir']:>+12.3f} "
              f"{s['pos']:>10.0f}% {s['t']:>+7.2f}")
    ts, means = np.array(ts), np.array(means)
    print(f"\n  average IC across all {len(means)} slices: {means.mean():+.4f}")
    print(f"  slices with a positive average: {100 * (means > 0).mean():.0f}%")
    print(f"  slices reaching t of 2 or more: {100 * (ts >= 2).mean():.0f}%")
    print(f"  worst slice t {ts.min():+.2f}, best slice t {ts.max():+.2f}")

    # ------------------------------------------------------------------ 2
    print("\n" + "=" * 82)
    print("2. BOOTSTRAP ON WHOLE MONTHS, NOT DAYS")
    print("=" * 82)
    print("  Days inside a month share their futures, so resampling days would")
    print("  pretend to have more evidence than exists. Whole months are drawn")
    print("  instead, 5,000 times, and the band below is what remains.")
    daily["month"] = pd.to_datetime(daily["day"]).dt.to_period("M")
    months = daily["month"].unique()
    by_month = {m: g["ic"].to_numpy() for m, g in daily.groupby("month")}
    rng = np.random.default_rng(7)
    draws = []
    for _ in range(5000):
        pick = rng.choice(months, size=len(months), replace=True)
        draws.append(np.concatenate([by_month[m] for m in pick]).mean())
    draws = np.array(draws)
    lo, hi = np.percentile(draws, [2.5, 97.5])
    print(f"\n  average daily IC {daily['ic'].mean():+.4f}")
    print(f"  95% band {lo:+.4f} .. {hi:+.4f}   "
          f"{'clears zero' if lo > 0 else 'DOES NOT clear zero'}")
    print(f"  share of draws above zero: {100 * (draws > 0).mean():.1f}%")

    # -------------------------------------------------- 2b. is it just the model
    print("\n  the same bootstrap on shuffled answers, as a floor:")
    sres = predictions(df, feats, shuffle=True)
    sdaily = per_day(sres)
    sdaily["month"] = pd.to_datetime(sdaily["day"]).dt.to_period("M")
    sby = {m: g["ic"].to_numpy() for m, g in sdaily.groupby("month")}
    sm = sdaily["month"].unique()
    sdraws = np.array([np.concatenate([sby[m] for m in rng.choice(sm, len(sm), True)]).mean()
                       for _ in range(2000)])
    slo, shi = np.percentile(sdraws, [2.5, 97.5])
    print(f"  average daily IC {sdaily['ic'].mean():+.4f}   band {slo:+.4f} .. {shi:+.4f}")

    # ------------------------------------------------------------------ 3
    print("\n" + "=" * 82)
    print("3. WHAT IS IT WORTH? BEST TENTH MINUS WORST TENTH, IN ATR")
    print("=" * 82)
    daily["yr"] = pd.to_datetime(daily["day"]).dt.year
    print(f"\n  {'year':>6} {'days':>6} {'avg IC':>9} {'best-worst tenth':>18}")
    for y, g in daily.groupby("yr"):
        print(f"  {y:>6} {len(g):>6} {g['ic'].mean():>+9.4f} "
              f"{g['spread'].mean():>+18.3f}")
    s = summarise(daily["spread"])
    print(f"\n  overall {s['mean']:+.3f} ATR between the two ends, "
          f"positive on {s['pos']:.0f}% of days")
    print("  For a $1,000-risk position with a 2-ATR stop, one ATR is about $500,")
    print("  so this is roughly ${:.0f} per trade of separation, before costs and"
          .format(abs(s["mean"]) * 500))
    print("  before anything is done about actually harvesting it.")

    # ------------------------------------------------------------------ 4
    print("\n" + "=" * 82)
    print("4. DOES IT SEPARATE OUR OWN ENTRIES?")
    print("=" * 82)
    print("  A universe-wide signal is only useful here if it ranks the ideas")
    print("  the system actually produces. Our entries are matched to the")
    print("  universe table by company and day.")
    ours = pd.DataFrame(json.loads(
        (HERE / "dataset.json").read_text(encoding="utf-8"))["rows"])
    ours["fired_date"] = pd.to_datetime(ours["fired_date"])
    j = ours.merge(res[["ticker", "fired_date", "pred"]],
                   on=["ticker", "fired_date"], how="inner")
    print(f"\n  {len(j)} of our {len(ours)} entries landed on a scored day")
    if len(j) >= 200:
        j["b"] = pd.qcut(j["pred"], 4, labels=False, duplicates="drop")
        print(f"\n  {'group':>6} {'n':>6} {'went up':>9} {'ended at':>10} "
              f"{'market out':>12} {'real R':>9}")
        for b in sorted(j["b"].dropna().unique()):
            g = j[j["b"] == b]
            print(f"  {int(b) + 1:>6} {len(g):>6} {g['mfe_atr_20'].mean():>+9.3f} "
                  f"{g['ret_atr_20'].mean():>+10.3f} "
                  f"{g['exret_atr_20'].mean():>+12.3f} "
                  f"{g['r_actual'].mean():>+9.3f}")
        hi_, lo_ = j[j["b"] == j["b"].max()], j[j["b"] == 0]
        for c, name in (("exret_atr_20", "market out"), ("r_actual", "real R")):
            a, b = hi_[c].dropna(), lo_[c].dropna()
            se = np.sqrt(a.var() / len(a) + b.var() / len(b))
            d = a.mean() - b.mean()
            print(f"    top minus bottom, {name:>12}: {d:>+7.3f} ±{1.96 * se:.3f}  "
                  f"{'real' if abs(d) > 1.96 * se else 'inside the noise'}")


if __name__ == "__main__":
    main()
