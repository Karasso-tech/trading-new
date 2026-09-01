"""What do the winners share? Three tests that can each settle it.

READ-ONLY. Reads research/dataset.json, prints numbers, changes nothing.

"Find what the winners have in common" is a trap if it is answered directly,
because SOMETHING is always in common -- with 45 numbers and 3,000 winners, a
shared trait can be found in any pile of coin flips. So the question has to be
turned into ones that can come back negative.

  A. WHERE DOES THE VARIATION LIVE -- IN THE DAY, OR IN THE STOCK?
     If two entries on the same morning tend to share a fate, then the thing
     that decides is the calendar, and no amount of choosing between stocks can
     help. If the fate is decided stock by stock, choosing is where the work is.
     This is measured, not assumed: the spread of day-level win rates is
     compared against the spread pure chance would produce on those same day
     sizes. It settles what kind of problem this is before any model is built.

  B. ARE THE WINNERS CLUSTERED IN FEATURE SPACE AT ALL?
     Forget models. If winners sit near other winners -- in any shape, however
     twisted, however many features it takes -- then structure exists and some
     model can eventually find it. If a winner's nearest neighbours are no more
     often winners than chance gives, then there is no structure to find and
     every model that ever failed here was failing for the right reason.
     This is the strongest negative available: it rules out the class, not one
     attempt.

  C. THE BEST RULE THAT EXISTS, AGAINST THE BEST RULE LUCK PRODUCES.
     Every one-condition and two-condition rule over the whole feature set is
     searched -- tens of thousands of them -- and the very best one kept. Then
     the ENTIRE search is repeated on shuffled outcomes, many times, to see how
     good the best rule looks when nothing is there. Comparing the real winner
     against that distribution is the only honest way to report the survivor of
     a large search, and it is exactly what was missing from every earlier pass.

    python research/winners.py
    python research/winners.py --rung 1
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
CATEGORICAL = ("setup", "regime_at_fire", "stop_basis")
MIN_SUPPORT = 300      # a rule covering fewer trades than this is not a rule
PERMS = 200


def load(rung):
    d = json.loads((HERE / "dataset.json").read_text(encoding="utf-8"))["rows"]
    df = pd.DataFrame(d)
    df["fired_date"] = pd.to_datetime(df["fired_date"])
    target = f"win_{rung:g}r"
    return df.dropna(subset=[target]).reset_index(drop=True), target


def matrix(df):
    feats = [c for c in df.columns
             if c not in NOT_FEATURES and not any(c.startswith(a) for a in ANSWERS)
             and pd.api.types.is_numeric_dtype(df[c])]
    X = df[feats].copy()
    for c in CATEGORICAL:
        if c in df.columns:
            X = pd.concat([X, pd.get_dummies(df[c].fillna("none"), prefix=c,
                                             dtype=float)], axis=1)
    return X


# ------------------------------------------------------------------------- A
def where_does_variation_live(df, target):
    print("=" * 84)
    print("A. IS THE FATE DECIDED BY THE DAY, OR BY THE STOCK?")
    print("=" * 84)
    print("  Two entries on the same morning: do they tend to share an outcome?")
    print("  If yes, the calendar decides and choosing between stocks cannot help.")

    g = df.groupby("fired_date")[target].agg(["mean", "size"])
    g = g[g["size"] >= 3]
    p = df[target].mean()
    # Under pure chance, a day of n entries has win-rate variance p(1-p)/n.
    # Anything above that sum is real day-to-day difference.
    expected = (p * (1 - p) / g["size"]).mean()
    observed = g["mean"].var()
    extra = max(observed - expected, 0.0)
    print(f"\n  {len(g)} days with 3 or more entries, {int(g['size'].sum())} entries")
    print(f"  overall win rate {100 * p:.1f}%")
    print(f"\n  spread of day win rates, seen         {observed:.5f}")
    print(f"  spread pure chance would give         {expected:.5f}")
    print(f"  the excess, which is real day effect  {extra:.5f}")
    print(f"\n  share of the variation that is the DAY: {100 * extra / observed:.0f}%")
    print(f"  share that is anything else:            {100 * (1 - extra / observed):.0f}%")

    # the same thing per company, for contrast
    t = df.groupby("ticker")[target].agg(["mean", "size"])
    t = t[t["size"] >= 8]
    e2 = (p * (1 - p) / t["size"]).mean()
    o2 = t["mean"].var()
    print(f"\n  and per COMPANY ({len(t)} with 8+ entries):")
    print(f"    seen {o2:.5f}, chance {e2:.5f}, "
          f"company effect {100 * max(o2 - e2, 0) / o2:.0f}% of it")

    print("\n  Reading: a large day share means the answer to 'which entry' is")
    print("  mostly 'which morning', and a stock-picking model is the wrong tool.")


# ------------------------------------------------------------------------- B
def are_winners_clustered(df, target, k=25, seed=0):
    print("\n" + "=" * 84)
    print("B. DO WINNERS SIT NEAR OTHER WINNERS?")
    print("=" * 84)
    print(f"  For every entry, its {k} closest neighbours among all 45 numbers.")
    print("  How often is a winner's neighbourhood also winning? Compared against")
    print("  the same count after the outcomes are shuffled. This does not care")
    print("  what SHAPE the structure has -- only whether any exists.")

    X = matrix(df)
    Z = StandardScaler().fit_transform(
        SimpleImputer(strategy="median").fit_transform(X))
    y = df[target].to_numpy(float)

    nn = NearestNeighbors(n_neighbors=k + 1).fit(Z)
    idx = nn.kneighbors(Z, return_distance=False)[:, 1:]      # drop self

    def score(labels):
        # how much more often a winner's neighbours win than a loser's do
        near = labels[idx].mean(axis=1)
        return near[labels == 1].mean() - near[labels == 0].mean()

    real = score(y)
    rng = np.random.default_rng(seed)
    null = np.array([score(rng.permutation(y)) for _ in range(PERMS)])
    z = (real - null.mean()) / null.std()
    print(f"\n  {len(y)} entries, {int(y.sum())} of them winners")
    print(f"  winners' neighbourhoods are {100 * real:+.2f} points more winning")
    print(f"  shuffled: {100 * null.mean():+.2f} points, spread {100 * null.std():.2f}")
    print(f"  how far out the real number sits: {z:+.2f} standard deviations")
    print(f"  shuffles that beat it: {100 * (null >= real).mean():.1f}%")
    verdict = ("STRUCTURE EXISTS -- some model can find it"
               if (null >= real).mean() < 0.05
               else "NO STRUCTURE -- winners and losers are mixed together")
    print(f"\n  {verdict}")
    return (null >= real).mean() < 0.05


# ------------------------------------------------------------------------- C
def conditions(df, per_feature=6):
    """Every simple 'this number is above/below X' test, as boolean columns."""
    X = matrix(df)
    names, masks = [], []
    for c in X.columns:
        v = X[c].to_numpy(float)
        ok = ~np.isnan(v)
        if ok.sum() < MIN_SUPPORT * 2:
            continue
        uniq = np.unique(v[ok])
        if len(uniq) <= 2:                       # a yes/no column
            names.append(f"{c} is on")
            masks.append(np.nan_to_num(v, nan=0) > 0.5)
            continue
        for q in np.linspace(1 / (per_feature + 1), per_feature / (per_feature + 1),
                             per_feature):
            thr = np.nanquantile(v, q)
            for sign, lab in ((1, ">"), (-1, "<")):
                m = (v > thr) if sign > 0 else (v < thr)
                m = m & ok
                if MIN_SUPPORT <= m.sum() <= len(v) - MIN_SUPPORT:
                    names.append(f"{c} {lab} {thr:.3g}")
                    masks.append(m)
    return names, np.array(masks, dtype=np.float32)


def best_rule_search(df, target):
    print("\n" + "=" * 84)
    print("C. THE BEST RULE THERE IS, AGAINST THE BEST RULE LUCK MAKES")
    print("=" * 84)
    names, M = conditions(df)
    y = df[target].to_numpy(np.float32)
    base = y.mean()
    n = len(y)
    support1 = M.sum(axis=1)
    support2 = M @ M.T                       # how many trades each PAIR covers
    print(f"  {len(names)} single conditions, "
          f"{int((support2 >= MIN_SUPPORT).sum() / 2):,} usable pairs")
    print(f"  every rule must cover at least {MIN_SUPPORT} trades")
    print(f"  base win rate {100 * base:.1f}%")

    def search(labels):
        """Best win rate over all one- and two-condition rules."""
        w1 = M @ labels
        r1 = np.where(support1 >= MIN_SUPPORT, w1 / np.maximum(support1, 1), -1)
        w2 = (M * labels) @ M.T
        with np.errstate(invalid="ignore", divide="ignore"):
            r2 = np.where(support2 >= MIN_SUPPORT, w2 / np.maximum(support2, 1), -1)
        np.fill_diagonal(r2, -1)
        return max(r1.max(), r2.max()), r1, r2

    real, r1, r2 = search(y)
    i, j = np.unravel_index(np.argmax(r2), r2.shape)
    k1 = int(np.argmax(r1))
    print(f"\n  best single condition:  {names[k1]}")
    print(f"    {100 * r1[k1]:.1f}% win rate over {int(support1[k1])} trades "
          f"({100 * (r1[k1] - base):+.1f} points)")
    print(f"  best pair of conditions: {names[i]}")
    print(f"                      AND  {names[j]}")
    print(f"    {100 * r2[i, j]:.1f}% win rate over {int(support2[i, j])} trades "
          f"({100 * (r2[i, j] - base):+.1f} points)")

    rng = np.random.default_rng(1)
    print(f"\n  now the identical search, {PERMS} times, on shuffled outcomes...")
    null = np.empty(PERMS)
    for p in range(PERMS):
        null[p], _, _ = search(rng.permutation(y))
    print(f"\n  best rule found on real outcomes:      {100 * real:.1f}%")
    print(f"  best rule found on shuffled outcomes:  {100 * null.mean():.1f}% "
          f"on average, up to {100 * null.max():.1f}%")
    beat = (null >= real).mean()
    print(f"  shuffles whose best rule matched it:   {100 * beat:.1f}%")
    print("\n  " + ("THE RULE IS REAL -- luck alone does not reach it"
                    if beat < 0.05 else
                    "THE RULE IS NOT REAL -- pure luck finds rules this good, "
                    f"{100 * beat:.0f}% of the time"))
    return beat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rung", type=float, default=2.0)
    args = ap.parse_args()
    df, target = load(args.rung)
    print(f"{len(df)} entries that resolved, "
          f"{int(df[target].sum())} reached +{args.rung:g}R before the stop "
          f"({100 * df[target].mean():.1f}%)\n")
    where_does_variation_live(df, target)
    are_winners_clustered(df, target)
    best_rule_search(df, target)


if __name__ == "__main__":
    main()
