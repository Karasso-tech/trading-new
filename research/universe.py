"""Is there a direction signal ANYWHERE in this data, or only missing from our
entries?

READ-ONLY over the bar files. Prints numbers, changes nothing.

This is the control the whole investigation has been missing. Every result so
far has been computed inside the 9,195 days our own rule selected. Two very
different worlds produce identical-looking flatness there:

  A. There is no forecastable direction in daily stock data at this horizon,
     for anybody. Then no rule change can help, and the honest response is to
     stop looking for one and spend the effort on the exit and on size.

  B. There IS forecastable direction, and our entry rule is standing on top of
     it -- selecting a slice where it happens to be gone. Then the rule itself
     is the problem, and it is worth replacing.

Telling them apart needs the same model run on the WHOLE universe, not on our
slice: every fifth trading day, every company, no setup logic, no trigger, no
grade. If the model finds nothing there either, the answer is A. If it finds
something there and nothing in our slice, the answer is B.

The model, the walk-forward, the purge and the shuffled control are identical
to research/model.py, so the two numbers are directly comparable.

    python research/universe.py
    python research/universe.py --every 3 --target exret_atr_20
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
from sklearn.impute import SimpleImputer
from sklearn.linear_model import RidgeCV
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

import build_dataset as bd          # the same feature code, not a copy of it

warnings.filterwarnings("ignore")

HERE = pathlib.Path(__file__).resolve().parent
CACHE = HERE / "universe.parquet"


def build(every: int, start: str) -> pd.DataFrame:
    spy, qqq = bd.Series(bd.load("SPY")), bd.Series(bd.load("QQQ"))
    tickers = sorted(p.stem for p in bd.BARS.glob("*.json")
                     if not p.stem.startswith("_"))
    out = []
    for n, t in enumerate(tickers, 1):
        bars = bd.load(t)
        if not bars or len(bars) < 400:
            continue
        s = bd.Series(bars)
        if n % 100 == 0:
            print(f"  {n}/{len(tickers)} companies, {len(out)} rows")
        for i in range(300, len(bars) - max(bd.HORIZONS) - 2, every):
            d = bars[i]["date"]
            if d < start:
                continue
            si, qi = spy.date_ix.get(d), qqq.date_ix.get(d)
            if si is None or qi is None or not s.atr14[i]:
                continue
            f = bd.features(s, i, spy, qqq, si, qi)
            if not f:
                continue
            entry = s.open[i + 1]
            tg = bd.targets(s, i + 1, entry, s.atr14[i], spy, si + 1, f.get("beta_60"))
            if f"ret_atr_{bd.HORIZONS[-1]}" not in tg:
                continue
            rec = {"ticker": t, "fired_date": d}
            rec.update(f)
            rec.update(tg)
            out.append(rec)
    df = pd.DataFrame(out)
    df["fired_date"] = pd.to_datetime(df["fired_date"])
    return df.sort_values("fired_date").reset_index(drop=True)


def walk(df, feats, target, horizon, folds=8, shuffle=False):
    d = df.dropna(subset=[target]).reset_index(drop=True)
    X = d[feats]
    y = d[target].to_numpy(float)
    if shuffle:
        y = np.random.default_rng(0).permutation(y)
    dates = d["fired_date"]
    edges = pd.date_range(dates.min(), dates.max(), periods=folds + 2)[1:-1]
    purge = pd.Timedelta(days=int(horizon * 1.5))
    rows = []
    for k, cut in enumerate(edges):
        nxt = edges[k + 1] if k + 1 < len(edges) else dates.max() + pd.Timedelta(days=1)
        tr = (dates < (cut - purge)).values
        te = ((dates >= cut) & (dates < nxt)).values
        if tr.sum() < 2000 or te.sum() < 500:
            continue
        ridge = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                              RidgeCV(alphas=np.logspace(-1, 4, 20)))
        ridge.fit(X[tr], y[tr])
        gbm = HistGradientBoostingRegressor(max_depth=4, max_iter=400,
                                            learning_rate=0.05, min_samples_leaf=200,
                                            l2_regularization=1.0, random_state=0)
        gbm.fit(X[tr], y[tr])
        pr, pg = ridge.predict(X[te]), gbm.predict(X[te])
        yt = y[te]
        n = max(len(yt) // 5, 20)
        og = np.argsort(pg)
        rows.append({
            "fold": k + 1, "n": int(te.sum()),
            "from": str(dates[te].min().date()), "to": str(dates[te].max().date()),
            "ridge": stats.spearmanr(pr, yt).statistic,
            "gbm": stats.spearmanr(pg, yt).statistic,
            "spread": yt[og[-n:]].mean() - yt[og[:n]].mean(),
        })
    return rows


def report(name, rows):
    if not rows:
        print(f"  {name}: no usable folds")
        return
    print(f"\n  {name}")
    print(f"    {'fold':>5} {'test window':>24} {'n':>7} {'ridge IC':>9} "
          f"{'gbm IC':>9} {'gbm top-bot':>12}")
    for r in rows:
        print(f"    {r['fold']:>5} {r['from']} .. {r['to']:>10} {r['n']:>7} "
              f"{r['ridge']:>+9.4f} {r['gbm']:>+9.4f} {r['spread']:>+12.3f}")
    for k in ("ridge", "gbm"):
        v = np.array([r[k] for r in rows])
        t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v))) if v.std(ddof=1) else 0
        print(f"    {k:>5} average IC {v.mean():+.4f}   positive in "
              f"{100 * (v > 0).mean():.0f}% of folds   t {t:+.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=int, default=5,
                    help="sample every Nth trading day per company")
    ap.add_argument("--start", default="2021-08-01")
    ap.add_argument("--target", default="exret_atr_20")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    horizon = int(args.target.rsplit("_", 1)[1])

    if CACHE.exists() and not args.rebuild:
        df = pd.read_parquet(CACHE)
        print(f"loaded {len(df)} rows from cache")
    else:
        print(f"building: every {args.every}th day from {args.start}")
        df = build(args.every, args.start)
        df.to_parquet(CACHE)
        print(f"cached {len(df)} rows")

    feats = [c for c in df.columns
             if c not in ("ticker", "fired_date")
             and not any(c.startswith(a) for a in
                         ("mfe_atr_", "mae_atr_", "ret_atr_", "exret_atr_", "edge_ratio_"))
             and pd.api.types.is_numeric_dtype(df[c])]
    print(f"{len(df)} rows, {df['ticker'].nunique()} companies, "
          f"{df['fired_date'].min().date()} .. {df['fired_date'].max().date()}")
    print(f"{len(feats)} features, target {args.target}")

    print("\n" + "=" * 80)
    print("THE SAME MODEL, ON THE WHOLE UNIVERSE INSTEAD OF OUR OWN ENTRIES")
    print("=" * 80)
    report("REAL ANSWERS", walk(df, feats, args.target, horizon))
    report("SHUFFLED ANSWERS -- what nothing looks like",
           walk(df, feats, args.target, horizon, shuffle=True))

    print("\n" + "=" * 80)
    print("AND THE PLAIN ONES, ONE NUMBER AT A TIME, ACROSS QUARTERS")
    print("=" * 80)
    d = df.dropna(subset=[args.target]).copy()
    d["q"] = d["fired_date"].dt.to_period("Q")
    res = []
    for f in feats:
        per = []
        for q, g in d.groupby("q"):
            a, b = g[f], g[args.target]
            ok = a.notna() & b.notna()
            if ok.sum() < 200 or a[ok].nunique() < 5:
                continue
            per.append(stats.spearmanr(a[ok], b[ok]).statistic)
        if len(per) < 12:
            continue
        v = np.array(per)
        t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v))) if v.std(ddof=1) else 0
        hit = 100 * (v > 0).mean() if v.mean() > 0 else 100 * (v < 0).mean()
        res.append((abs(t), f, v.mean(), hit, len(v), t))
    res.sort(reverse=True)
    print(f"\n  {'number':>26} {'avg rank corr':>14} {'same sign':>11} "
          f"{'quarters':>9} {'t':>7}")
    for _, f, m, hit, n, t in res[:15]:
        star = "  <--" if abs(t) >= 2.5 and hit >= 65 else ""
        print(f"  {f:>26} {m:>+14.4f} {hit:>10.0f}% {n:>9} {t:>+7.2f}{star}")


if __name__ == "__main__":
    main()
