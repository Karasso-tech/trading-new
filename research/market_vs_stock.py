"""The structure is real. Is it about the STOCK, or about the MORNING?

READ-ONLY. Prints numbers, changes nothing. Proposes no rule change.

Three results arrived together and they only make sense read together:

  * 48% of the variation in whether an entry works is the DAY it fired on.
    0% is the company. Two entries on the same morning share a fate; the same
    company on two different mornings does not.
  * Winners genuinely cluster in feature space -- 22 standard deviations past
    what shuffled outcomes produce. Structure exists.
  * The best rule out of 131,352 searched is `earnings_days_out < 23 AND
    spy_realized_vol_20 > 21.7`: 54.6% against a 34.2% base, and no shuffled
    search came close.

The second of those does not survive contact with the first. Market-wide
numbers -- how volatile the index has been, where it sits against its long
average, whether the growth side is leading -- are IDENTICAL for every stock on
a given morning. Put them in the feature set and two entries from the same
morning become near-neighbours automatically. The clustering could be nothing
but the calendar wearing a disguise. And the best rule found is half market
number already, which is exactly what that would look like.

So the features get split in two and everything is run again on each half:

  STOCK-ONLY  -- what this company was doing, and nothing else
  MARKET-ONLY -- what the index was doing, identical across names that day

Then the sharpest cut of all: the outcome is measured against its OWN DAY's
average, which erases the calendar completely. Whatever is still there after
that is genuinely about choosing between stocks. Whatever disappears was the
morning all along.

And whatever survives is checked out of sample, because it was found in here.

    python research/market_vs_stock.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import warnings

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")

HERE = pathlib.Path(__file__).resolve().parent
ANSWERS = ("mfe_atr_", "mae_atr_", "ret_atr_", "exret_atr_", "edge_ratio_", "win_")
NOT_FEATURES = {"ticker", "fired_date", "built", "setup", "regime_at_fire",
                "grade_at_fire", "stop_basis", "r_actual", "entry_px",
                "risk_per_share"}
# Same for every ticker on a given morning. This is the list that decides the
# whole question, so it is explicit rather than inferred from a name prefix.
MARKET = ("spy_dist_sma200_atr", "spy_ret_21", "spy_realized_vol_20",
          "qqq_minus_spy_21")
MIN_SUPPORT = 300
PERMS = 200
SPLIT = "2024-02-01"


def load(rung):
    d = json.loads((HERE / "dataset.json").read_text(encoding="utf-8"))["rows"]
    df = pd.DataFrame(d)
    df["fired_date"] = pd.to_datetime(df["fired_date"])
    t = f"win_{rung:g}r"
    return df.dropna(subset=[t]).reset_index(drop=True), t


def columns(df, which):
    num = [c for c in df.columns
           if c not in NOT_FEATURES and not any(c.startswith(a) for a in ANSWERS)
           and pd.api.types.is_numeric_dtype(df[c])]
    if which == "market":
        return [c for c in num if c in MARKET]
    if which == "stock":
        # regime is the market's own label, so it leaves with the market half
        return [c for c in num if c not in MARKET]
    return num


def frame(df, cols, with_regime):
    X = df[cols].copy()
    if with_regime:
        X = pd.concat([X, pd.get_dummies(df["regime_at_fire"].fillna("none"),
                                         prefix="regime", dtype=float)], axis=1)
    else:
        for c in ("setup", "stop_basis"):
            X = pd.concat([X, pd.get_dummies(df[c].fillna("none"), prefix=c,
                                             dtype=float)], axis=1)
    return X


def conditions(X, per_feature=6):
    names, masks = [], []
    for c in X.columns:
        v = X[c].to_numpy(float)
        ok = ~np.isnan(v)
        if ok.sum() < MIN_SUPPORT * 2:
            continue
        if len(np.unique(v[ok])) <= 2:
            names.append(f"{c} is on")
            masks.append(np.nan_to_num(v, nan=0) > 0.5)
            continue
        for q in np.linspace(1 / (per_feature + 1), per_feature / (per_feature + 1),
                             per_feature):
            thr = np.nanquantile(v, q)
            for sign, lab in ((1, ">"), (-1, "<")):
                m = ((v > thr) if sign > 0 else (v < thr)) & ok
                if MIN_SUPPORT <= m.sum() <= len(v) - MIN_SUPPORT:
                    names.append(f"{c} {lab} {thr:.3g}")
                    masks.append(m)
    return names, np.array(masks, dtype=np.float32)


def search(names, M, y, base):
    """Best one- and two-condition rule, and the same search on shuffled y."""
    s1, s2 = M.sum(axis=1), M @ M.T

    def best(labels):
        r1 = np.where(s1 >= MIN_SUPPORT, (M @ labels) / np.maximum(s1, 1), -9)
        with np.errstate(invalid="ignore", divide="ignore"):
            r2 = np.where(s2 >= MIN_SUPPORT, ((M * labels) @ M.T) / np.maximum(s2, 1), -9)
        np.fill_diagonal(r2, -9)
        return max(r1.max(), r2.max()), r1, r2

    real, r1, r2 = best(y)
    rng = np.random.default_rng(1)
    null = np.array([best(rng.permutation(y))[0] for _ in range(PERMS)])
    i, j = np.unravel_index(np.argmax(r2), r2.shape)
    return {"real": real, "null": null, "beat": (null >= real).mean(),
            "pair": (names[i], names[j], r2[i, j], int(s2[i, j])),
            "one": (names[int(np.argmax(r1))], r1.max(),
                    int(s1[int(np.argmax(r1))]))}


def clustered(X, y, k=25, seed=0):
    Z = StandardScaler().fit_transform(SimpleImputer(strategy="median").fit_transform(X))
    idx = NearestNeighbors(n_neighbors=k + 1).fit(Z).kneighbors(
        Z, return_distance=False)[:, 1:]

    def sc(lab):
        near = lab[idx].mean(axis=1)
        # continuous labels need a correlation; binary ones the group gap
        if set(np.unique(lab)) <= {0.0, 1.0}:
            return near[lab == 1].mean() - near[lab == 0].mean()
        return np.corrcoef(near, lab)[0, 1]

    real = sc(y)
    rng = np.random.default_rng(seed)
    null = np.array([sc(rng.permutation(y)) for _ in range(PERMS)])
    return real, null


def block(title, X, y, base, tag=""):
    print(f"\n  {title}   ({X.shape[1]} numbers){tag}")
    r, null = clustered(X, y)
    beat_c = (null >= r).mean()
    unit = "points" if set(np.unique(y)) <= {0.0, 1.0} else "correlation"
    print(f"    neighbours: {r:+.4f} {unit}, shuffled {null.mean():+.4f} "
          f"±{null.std():.4f}   beaten by {100 * beat_c:.1f}% of shuffles")
    names, M = conditions(X)
    if len(names) < 4:
        print("    not enough conditions to search rules")
        return
    s = search(names, M, y.astype(np.float32), base)
    print(f"    best pair: {s['pair'][0]}")
    print(f"          AND  {s['pair'][1]}")
    print(f"           ->  {s['pair'][2]:+.4f} over {s['pair'][3]} trades "
          f"(base {base:+.4f})")
    print(f"    best rule real {s['real']:+.4f} vs shuffled best "
          f"{s['null'].mean():+.4f} (max {s['null'].max():+.4f})   "
          f"beaten by {100 * s['beat']:.1f}%")
    print(f"    ==> {'REAL' if s['beat'] < 0.05 else 'NOT REAL, luck finds this'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rung", type=float, default=2.0)
    args = ap.parse_args()
    df, target = load(args.rung)
    y = df[target].to_numpy(float)
    print(f"{len(df)} entries, {int(y.sum())} winners ({100 * y.mean():.1f}%)")

    print("\n" + "=" * 84)
    print("1. THE FEATURES SPLIT IN TWO")
    print("=" * 84)
    print("  Market numbers are identical for every stock on a morning. Stock")
    print("  numbers are not. Each half is asked the question on its own.")
    block("MARKET ONLY", frame(df, columns(df, "market"), True), y, y.mean())
    block("STOCK ONLY", frame(df, columns(df, "stock"), False), y, y.mean())

    print("\n" + "=" * 84)
    print("2. THE CALENDAR ERASED COMPLETELY")
    print("=" * 84)
    print("  Each outcome is measured against its OWN morning's average, so")
    print("  every trace of 'which day' is gone. Only 'which stock, given the")
    print("  day' is left. This is the question the screener actually faces.")
    d = df.copy()
    d["day_mean"] = d.groupby("fired_date")[target].transform("mean")
    d["day_n"] = d.groupby("fired_date")[target].transform("size")
    d = d[d["day_n"] >= 3].reset_index(drop=True)
    resid = (d[target] - d["day_mean"]).to_numpy(float)
    print(f"\n  {len(d)} entries on days with 3 or more candidates")
    block("STOCK ONLY, day removed", frame(d, columns(d, "stock"), False),
          resid, resid.mean(), tag="  [outcome is now a difference from the day]")

    print("\n" + "=" * 84)
    print("3. THE MARKET RULE, OUT OF SAMPLE")
    print("=" * 84)
    print(f"  The rule was found in this data, so it is re-checked on the part")
    print(f"  after {SPLIT}, which had no say in finding it.")
    for name, mask in (
        ("calm market  (index 20-day swing under 21.7)", df["spy_realized_vol_20"] <= 21.7),
        ("jumpy market (index 20-day swing over 21.7)", df["spy_realized_vol_20"] > 21.7),
    ):
        a = df[mask & (df["fired_date"] < SPLIT)][target]
        b = df[mask & (df["fired_date"] >= SPLIT)][target]
        print(f"\n    {name}")
        print(f"      before {SPLIT}: {100 * a.mean():.1f}% of {len(a)}")
        print(f"      after  {SPLIT}: {100 * b.mean():.1f}% of {len(b)}")

    print("\n  and the same thing year by year, so one year cannot carry it:")
    df["yr"] = df["fired_date"].dt.year
    print(f"\n    {'year':>6} {'calm':>18} {'jumpy':>18} {'gap':>8}")
    for yv, g in df.groupby("yr"):
        c = g[g["spy_realized_vol_20"] <= 21.7][target]
        j = g[g["spy_realized_vol_20"] > 21.7][target]
        if len(c) < 40 or len(j) < 40:
            print(f"    {yv:>6} {'too thin':>18}"
                  f"   (calm n={len(c)}, jumpy n={len(j)})")
            continue
        print(f"    {yv:>6} {100 * c.mean():>11.1f}% (n{len(c):>4}) "
              f"{100 * j.mean():>11.1f}% (n{len(j):>4}) "
              f"{100 * (j.mean() - c.mean()):>+8.1f}")


if __name__ == "__main__":
    main()
