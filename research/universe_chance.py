"""The last test: is there a chance signal in the market that our rules threw away?

READ-ONLY. Reads universe_v2.parquet, prints numbers, changes nothing.

Every negative result so far was measured inside the 9,473 moments our own
rules selected. That leaves one escape hatch open, and it has to be closed
before "there is no signal" can be said: if the entry rules already pick one
narrow kind of moment, then finding no spread among the survivors proves
nothing about whether a spread exists in the market.

So the identical question is put to 121,054 days across 501 companies with no
entry rule of any kind -- every fifth trading day, whatever the chart looked
like. Same 87 numbers. Same answer: from the next open, with a plain 2-ATR
stop, does price reach +1R, +2R or +3R before -1R.

Four tests, and the last two exist because the first two are not enough:

  1. A classifier, walked forward, purged, against a shuffled control.
  2. Ranking stocks against each other on the same morning, scored only on
     days whose windows do not overlap.
  3. Every one- and two-condition rule searched, against the best rule the
     same search finds on shuffled outcomes.
  4. AND THEN THE YEAR-BY-YEAR CHECK ON WHATEVER WON. This is the one that has
     killed everything: a permutation test controls for how much was searched,
     and is completely blind to a rule whose every trade sits inside one market
     episode. Both checks are needed and neither substitutes for the other.

Nothing from 2025-06-09 onward is touched.

    python research/universe_chance.py
    python research/universe_chance.py --rung 1
"""

from __future__ import annotations

import argparse
import pathlib
import warnings

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

warnings.filterwarnings("ignore")

HERE = pathlib.Path(__file__).resolve().parent
ANSWERS = ("mfe_atr_", "mae_atr_", "ret_atr_", "exret_atr_", "edge_ratio_",
           "win_", "best_r", "bars_to_1r")
NOT_FEATURES = {"ticker", "fired_date", "sealed"}
MIN_SUPPORT = 2000        # a rule must cover this many of the universe rows
PERMS = 200
SEARCH_ROWS = 32000       # subsample for the pair search, so 200 shuffles fit
MIN_NAMES = 50


def load():
    df = pd.read_parquet(HERE / "universe_v2.parquet")
    df["fired_date"] = pd.to_datetime(df["fired_date"])
    return df.sort_values("fired_date").reset_index(drop=True)


def feats_of(df):
    return [c for c in df.columns
            if c not in NOT_FEATURES and not any(c.startswith(a) for a in ANSWERS)
            and pd.api.types.is_numeric_dtype(df[c])]


# ------------------------------------------------------------------------- 1
def walk_forward(df, feats, target, folds=7, shuffle=False):
    d = df.dropna(subset=[target]).reset_index(drop=True)
    X, y, dates = d[feats], d[target].to_numpy(int), d["fired_date"]
    if shuffle:
        y = np.random.default_rng(4).permutation(y)
    edges = pd.date_range(dates.min(), dates.max(), periods=folds + 2)[1:-1]
    purge = pd.Timedelta(days=60)
    pred = np.full(len(d), np.nan)
    rows = []
    for k, cut in enumerate(edges):
        nxt = edges[k + 1] if k + 1 < len(edges) else dates.max() + pd.Timedelta(days=1)
        tr = (dates < (cut - purge)).values
        te = ((dates >= cut) & (dates < nxt)).values
        if tr.sum() < 5000 or te.sum() < 2000:
            continue
        m = HistGradientBoostingClassifier(max_depth=4, max_iter=400,
                                           learning_rate=0.05, min_samples_leaf=200,
                                           l2_regularization=1.0, random_state=0)
        m.fit(X[tr], y[tr])
        p = m.predict_proba(X[te])[:, 1]
        pred[te] = p
        rows.append({"fold": k + 1, "n": int(te.sum()),
                     "from": str(dates[te].min().date()),
                     "to": str(dates[te].max().date()),
                     "base": 100 * y[te].mean(),
                     "auc": roc_auc_score(y[te], p)})
    d["pred"] = pred
    d["y"] = y
    return rows, d.dropna(subset=["pred"])


# ------------------------------------------------------------------------- 2
def same_morning(scored, spacing=8):
    """Rank stocks against each other on one morning, on non-overlapping days.

    Cache rows are every 5th trading day and the answer is 40 days long, so
    every 8th row is the first spacing at which two scored mornings share no
    future. Without that the t is inflated about three-fold."""
    per = []
    for day, g in scored.groupby("fired_date"):
        if len(g) < MIN_NAMES or g["y"].nunique() < 2:
            continue
        per.append((day, roc_auc_score(g["y"], g["pred"]), len(g)))
    per.sort()
    out = {}
    for label, sel in (("every scored morning", per),
                       (f"every {spacing * 5}th, no shared future", per[::spacing])):
        v = np.array([p[1] for p in sel])
        if len(v) < 15:
            continue
        out[label] = {"n": len(v), "mean": v.mean(),
                      "pos": 100 * (v > 0.5).mean(),
                      "t": (v.mean() - 0.5) / (v.std(ddof=1) / np.sqrt(len(v)))}
    return out, per


# ------------------------------------------------------------------------- 3
def rule_search(df, feats, target, seed=0):
    d = df.dropna(subset=[target])
    if len(d) > SEARCH_ROWS:
        d = d.sample(SEARCH_ROWS, random_state=seed).sort_values("fired_date")
    X = d[feats]
    y = d[target].to_numpy(np.float32)
    names, masks = [], []
    for c in X.columns:
        v = X[c].to_numpy(float)
        ok = ~np.isnan(v)
        if ok.sum() < MIN_SUPPORT * 2:
            continue
        if len(np.unique(v[ok])) <= 2:
            names.append(f"{c} is on")
            masks.append((np.nan_to_num(v, nan=0) > 0.5).astype(np.float32))
            continue
        for q in (0.2, 0.4, 0.6, 0.8):
            thr = np.nanquantile(v, q)
            for gt, lab in ((True, ">"), (False, "<")):
                m = ((v > thr) if gt else (v < thr)) & ok
                if MIN_SUPPORT <= m.sum() <= len(v) - MIN_SUPPORT:
                    names.append(f"{c} {lab} {thr:.4g}")
                    masks.append(m.astype(np.float32))
    M = np.array(masks, dtype=np.float32)
    s1, s2 = M.sum(axis=1), M @ M.T

    def best(lab):
        r1 = np.where(s1 >= MIN_SUPPORT, (M @ lab) / np.maximum(s1, 1), -9)
        with np.errstate(invalid="ignore", divide="ignore"):
            r2 = np.where(s2 >= MIN_SUPPORT, ((M * lab) @ M.T) / np.maximum(s2, 1), -9)
        np.fill_diagonal(r2, -9)
        return max(r1.max(), r2.max()), r1, r2

    real, r1, r2 = best(y)
    i, j = np.unravel_index(np.argmax(r2), r2.shape)
    rng = np.random.default_rng(9)
    null = np.array([best(rng.permutation(y))[0] for _ in range(PERMS)])
    return {"names": names, "base": y.mean(), "rows": len(d),
            "pairs": int((s2 >= MIN_SUPPORT).sum() / 2),
            "real": real, "null": null, "beat": (null >= real).mean(),
            "one": (names[int(np.argmax(r1))], r1.max(), int(s1[int(np.argmax(r1))])),
            "pair": (names[i], names[j], r2[i, j], int(s2[i, j])),
            "mask_i": M[i], "mask_j": M[j], "index": d.index}


# ------------------------------------------------------------------------- 4
def by_year(df, target, mask, label):
    """The check the permutation test cannot make."""
    d = df.dropna(subset=[target]).copy()
    d["yr"] = d["fired_date"].dt.year
    m = pd.Series(mask, index=d.index) if not isinstance(mask, pd.Series) else mask
    print(f"\n  {label}")
    print(f"    {'year':>6} {'inside':>20} {'outside':>20} {'gap':>8}")
    gaps = []
    for yv, g in d.groupby("yr"):
        gm = m.reindex(g.index).fillna(False).astype(bool)
        a, b = g[gm][target], g[~gm][target]
        if len(a) < 200:
            print(f"    {yv:>6} {'too thin':>20}  (n={len(a)})")
            continue
        gaps.append(a.mean() - b.mean())
        print(f"    {yv:>6} {100 * a.mean():>13.1f}% (n{len(a):>4}) "
              f"{100 * b.mean():>13.1f}% (n{len(b):>5}) "
              f"{100 * (a.mean() - b.mean()):>+8.1f}")
    if len(gaps) >= 3:
        g = np.array(gaps)
        print(f"    positive in {100 * (g > 0).mean():.0f}% of the "
              f"{len(g)} years that had enough trades, average {100 * g.mean():+.1f}")
        print(f"    ==> {'HOLDS ACROSS YEARS' if (g > 0).mean() >= 0.8 else 'ONE OR TWO EPISODES, NOT A RULE'}")
    else:
        print("    ==> fewer than three usable years. That alone is the answer:")
        print("        the rule only exists in a couple of market episodes.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rung", type=float, default=2.0)
    args = ap.parse_args()
    target = f"win_{args.rung:g}r"

    df = load()
    open_ = df[~df["sealed"]].reset_index(drop=True)
    feats = feats_of(open_)
    d = open_.dropna(subset=[target])
    print(f"{len(df)} market days, {df['ticker'].nunique()} companies")
    print(f"  {len(open_)} open for searching, {int(df['sealed'].sum())} sealed")
    print(f"  {len(d)} resolved at +{args.rung:g}R, "
          f"{100 * d[target].mean():.1f}% reached it before the stop")
    print(f"  {len(feats)} numbers per day")

    print("\n" + "=" * 86)
    print("1. A MODEL, WALKED FORWARD")
    print("=" * 86)
    print("  0.500 is a coin. 0.550 would be a business.")
    rows, scored = walk_forward(open_, feats, target)
    print(f"\n  {'fold':>5} {'test window':>24} {'n':>7} {'base':>8} {'score':>8}")
    for r in rows:
        print(f"  {r['fold']:>5} {r['from']} .. {r['to']:>10} {r['n']:>7} "
              f"{r['base']:>7.1f}% {r['auc']:>8.3f}")
    v = np.array([r["auc"] for r in rows])
    print(f"    average {v.mean():.3f}, above a coin in "
          f"{100 * (v > 0.5).mean():.0f}% of folds")
    nrows, _ = walk_forward(open_, feats, target, shuffle=True)
    nv = np.array([r["auc"] for r in nrows])
    print(f"    shuffled: average {nv.mean():.3f}, above a coin in "
          f"{100 * (nv > 0.5).mean():.0f}% of folds")

    print("\n" + "=" * 86)
    print("2. RANKING THE STOCKS AGAINST EACH OTHER ON ONE MORNING")
    print("=" * 86)
    print("  The market's own move drops out: everyone in the comparison had it.")
    res, per = same_morning(scored)
    for label, s in res.items():
        print(f"    {label:>38}  score {s['mean']:.3f}  "
              f"right way {s['pos']:.0f}% of days  t {s['t']:+.2f}  "
              f"({s['n']} days)")
    print("\n  The second line is the honest one. The first counts the same")
    print("  fortnight up to eight times over.")

    print("\n" + "=" * 86)
    print("3. THE BEST RULE IN THE WHOLE MARKET")
    print("=" * 86)
    r = rule_search(open_, feats, target)
    print(f"  {len(r['names'])} conditions, {r['pairs']:,} pairs, "
          f"{r['rows']:,} days searched, base {100 * r['base']:.1f}%")
    print(f"\n  best single: {r['one'][0]}")
    print(f"    {100 * r['one'][1]:.1f}% over {r['one'][2]:,} days "
          f"({100 * (r['one'][1] - r['base']):+.1f} points)")
    print(f"  best pair:   {r['pair'][0]}")
    print(f"          AND  {r['pair'][1]}")
    print(f"    {100 * r['pair'][2]:.1f}% over {r['pair'][3]:,} days "
          f"({100 * (r['pair'][2] - r['base']):+.1f} points)")
    print(f"\n  luck's best over {PERMS} shuffles: {100 * r['null'].mean():.1f}% "
          f"average, {100 * r['null'].max():.1f}% highest")
    print(f"  luck matched it {100 * r['beat']:.1f}% of the time  ==> "
          f"{'passes the shuffle test' if r['beat'] < 0.05 else 'FAILS the shuffle test'}")

    print("\n" + "=" * 86)
    print("4. AND NOW THE CHECK THE SHUFFLE TEST CANNOT MAKE")
    print("=" * 86)
    print("  A rule whose every day sits inside one market episode passes the")
    print("  shuffle test easily and is worth nothing. Only the calendar shows it.")
    both = pd.Series((r["mask_i"] > 0.5) & (r["mask_j"] > 0.5), index=r["index"])
    full = open_.dropna(subset=[target])
    by_year(open_, target, both.reindex(full.index).fillna(False),
            f"{r['pair'][0]}  AND  {r['pair'][1]}")


if __name__ == "__main__":
    main()
