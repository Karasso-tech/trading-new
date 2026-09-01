"""Only the entries that reached target 1. What separates them, and what
separates the ones that kept going.

READ-ONLY. Prints numbers, changes nothing. Proposes no rule change.

Two questions, and the second is the one with teeth.

  1. WHAT ARE THEY LIKE? A straight description of the entries that reached
     +1R before their stop, beside the ones that did not. Description alone
     proves nothing -- with 79 numbers something is always different -- so the
     gap is reported in standard deviations and the small ones are named small.

  2. GIVEN IT REACHED TARGET 1, WHAT MADE IT KEEP GOING? This is the good
     question. Every trade here already worked once, so the easy differences
     are gone and both groups are inside the same population. Some ran to 2R
     and 3R and beyond; some touched 1R and slid back to the stop. That is a
     clean split with a real control group on both sides.

     It is also the question worth the most money in this system. The runner
     tranche is where the entire measured edge lives, and the runner only pays
     on trades that keep going after the first target. If anything at all
     predicts follow-through, it changes how the runner is sized -- and that is
     a lever the exit already has and the selection never will.

Everything from 2025-06-09 is sealed and is not touched here. It is loaded, and
its row count is printed, purely so it is visible that it exists and was left
alone.

    python research/after_target1.py
"""

from __future__ import annotations

import json
import pathlib
import warnings

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore")

HERE = pathlib.Path(__file__).resolve().parent
ANSWERS = ("mfe_atr_", "mae_atr_", "ret_atr_", "exret_atr_", "edge_ratio_",
           "win_", "best_r", "bars_to_1r", "r_actual")
NOT_FEATURES = {"ticker", "fired_date", "built", "setup", "regime_at_fire",
                "grade_at_fire", "stop_basis", "entry_px", "risk_per_share",
                "sealed"}
MIN_SUPPORT = 150
PERMS = 200


def load():
    d = json.loads((HERE / "entries_v2.json").read_text(encoding="utf-8"))
    df = pd.DataFrame(d["rows"])
    df["fired_date"] = pd.to_datetime(df["fired_date"])
    return df.sort_values("fired_date").reset_index(drop=True), d["sealed_from"]


def feature_names(df):
    return [c for c in df.columns
            if c not in NOT_FEATURES and not any(c.startswith(a) for a in ANSWERS)
            and pd.api.types.is_numeric_dtype(df[c])]


def effect(a, b):
    """Gap between two groups in pooled standard deviations. Under 0.2 is not
    something a person can act on, whatever the sample size."""
    a, b = a.dropna(), b.dropna()
    if len(a) < 30 or len(b) < 30:
        return None
    va, vb = a.var(), b.var()
    pooled = np.sqrt(((len(a) - 1) * va + (len(b) - 1) * vb) / (len(a) + len(b) - 2))
    return (a.mean() - b.mean()) / pooled if pooled > 0 else None


def describe(df, feats, mask, label_a, label_b, top=16):
    rows = []
    for f in feats:
        d = effect(df[mask][f], df[~mask][f])
        if d is None:
            continue
        rows.append((abs(d), f, df[mask][f].median(), df[~mask][f].median(), d))
    rows.sort(reverse=True)
    print(f"\n  {'number':>28} {label_a:>12} {label_b:>12} {'apart':>8}")
    for _, f, ma, mb, d in rows[:top]:
        tag = ("  <-- real" if abs(d) >= 0.20 else
               "  <-- slight" if abs(d) >= 0.10 else "")
        print(f"  {f:>28} {ma:>12.3f} {mb:>12.3f} {d:>+8.2f}{tag}")
    biggest = rows[0][0] if rows else 0
    print(f"\n  {len(rows)} numbers compared. Biggest gap of all: {biggest:.2f}.")
    if biggest < 0.20:
        print("  Nothing reaches 0.20. There is no usable difference here, and")
        print("  the size of the sample does not change that.")
    return rows


def rule_search(df, feats, y, label):
    """Best one- and two-condition rule, against the best that luck produces."""
    X = df[feats]
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
        for q in np.linspace(1 / 7, 6 / 7, 6):
            thr = np.nanquantile(v, q)
            for gt, lab in ((True, ">"), (False, "<")):
                m = ((v > thr) if gt else (v < thr)) & ok
                if MIN_SUPPORT <= m.sum() <= len(v) - MIN_SUPPORT:
                    names.append(f"{c} {lab} {thr:.4g}")
                    masks.append(m.astype(np.float32))
    M = np.array(masks, dtype=np.float32)
    s1, s2 = M.sum(axis=1), M @ M.T
    base = y.mean()

    def best(lab):
        r1 = np.where(s1 >= MIN_SUPPORT, (M @ lab) / np.maximum(s1, 1), -9)
        with np.errstate(invalid="ignore", divide="ignore"):
            r2 = np.where(s2 >= MIN_SUPPORT, ((M * lab) @ M.T) / np.maximum(s2, 1), -9)
        np.fill_diagonal(r2, -9)
        return max(r1.max(), r2.max()), r1, r2

    real, r1, r2 = best(y)
    i, j = np.unravel_index(np.argmax(r2), r2.shape)
    k = int(np.argmax(r1))
    rng = np.random.default_rng(2)
    null = np.array([best(rng.permutation(y))[0] for _ in range(PERMS)])
    beat = (null >= real).mean()

    print(f"\n  {label}")
    print(f"    {len(names)} conditions, {int((s2 >= MIN_SUPPORT).sum() / 2):,} pairs, "
          f"base {100 * base:.1f}%")
    print(f"    best single: {names[k]}")
    print(f"      {100 * r1[k]:.1f}% over {int(s1[k])} trades "
          f"({100 * (r1[k] - base):+.1f} points)")
    print(f"    best pair:   {names[i]}")
    print(f"            AND  {names[j]}")
    print(f"      {100 * r2[i, j]:.1f}% over {int(s2[i, j])} trades "
          f"({100 * (r2[i, j] - base):+.1f} points)")
    print(f"    luck's best: {100 * null.mean():.1f}% on average, "
          f"up to {100 * null.max():.1f}%")
    print(f"    luck matched it {100 * beat:.1f}% of the time  "
          f"==> {'REAL' if beat < 0.05 else 'NOT REAL'}")
    return beat, (names[i], names[j], r2[i, j], int(s2[i, j]))


def main():
    df, sealed_from = load()
    sealed = df[df["sealed"]]
    open_ = df[~df["sealed"]].reset_index(drop=True)
    print(f"{len(df)} entries in total")
    print(f"  {len(open_)} open for searching (before {sealed_from})")
    print(f"  {len(sealed)} SEALED and untouched here")

    feats = feature_names(open_)
    print(f"  {len(feats)} numbers per entry")

    got1 = open_["win_1r"] == 1
    print(f"\n{int(got1.sum())} of the open entries reached target 1 before "
          f"their stop ({100 * got1.mean():.1f}%)")

    print("\n" + "=" * 86)
    print("1. WHAT ARE THE TARGET-1 ENTRIES LIKE?")
    print("=" * 86)
    print("  Beside the ones that stopped out first. 'apart' is the gap in")
    print("  standard deviations -- below 0.20 is not tradable at any sample size.")
    describe(open_, feats, got1, "reached", "stopped")

    print("\n" + "=" * 86)
    print("2. AND NOW THE ONE WITH A REAL CONTROL GROUP")
    print("=" * 86)
    w = open_[got1].reset_index(drop=True)
    print(f"  Only the {len(w)} that reached target 1. Where did they end up?\n")
    b = w["best_r"]
    print(f"    {'furthest it got':>22} {'trades':>8} {'share':>8}")
    for lo, hi, lab in ((1.0, 1.5, "1.0 to 1.5R"), (1.5, 2.0, "1.5 to 2.0R"),
                        (2.0, 3.0, "2.0 to 3.0R"), (3.0, 5.0, "3.0 to 5.0R"),
                        (5.0, 99, "over 5R")):
        n = ((b >= lo) & (b < hi)).sum()
        print(f"    {lab:>22} {n:>8} {100 * n / len(w):>7.1f}%")
    print(f"\n    median furthest point: {b.median():.2f}R, "
          f"average {b.mean():.2f}R")
    print(f"    reached 2R : {100 * (w['win_2r'] == 1).mean():.1f}%")
    print(f"    reached 3R : {100 * (w['win_3r'] == 1).mean():.1f}%")

    kept = (w["win_2r"] == 1)
    print(f"\n  Split: {int(kept.sum())} kept going to 2R, "
          f"{int((~kept).sum())} stalled and came back.")
    print("  Both groups already worked once, so the easy differences are gone.")
    describe(w, feats, kept, "kept going", "stalled")

    print("\n" + "=" * 86)
    print("3. THE BEST RULE THERE IS, AGAINST THE BEST RULE LUCK MAKES")
    print("=" * 86)
    print("  Every one- and two-condition rule searched, then the identical")
    print(f"  search repeated {PERMS} times on shuffled outcomes. A rule only")
    print("  counts if luck cannot reach it.")
    rule_search(open_, feats, got1.to_numpy(np.float32),
                "which entries reach target 1")
    rule_search(w, feats, kept.to_numpy(np.float32),
                "which target-1 entries keep going to 2R")

    print("\n" + "=" * 86)
    print("4. HOW FAST IT GOT THERE -- THE ONE THING NOT KNOWN BEFORE ENTRY")
    print("=" * 86)
    print("  Not usable for choosing a trade. Usable for managing one, which is")
    print("  why it is here: it is the runner's own question.")
    w2 = w.dropna(subset=["bars_to_1r"])
    w2 = w2.copy()
    w2["speed"] = pd.cut(w2["bars_to_1r"], [-1, 1, 3, 7, 15, 99],
                         labels=["1 day", "2-3 days", "4-7 days",
                                 "8-15 days", "over 15 days"])
    print(f"\n    {'days to reach 1R':>18} {'trades':>8} {'went on to 2R':>15} "
          f"{'to 3R':>8} {'median best':>13} {'real R':>9}")
    for s, g in w2.groupby("speed", observed=True):
        print(f"    {str(s):>18} {len(g):>8} "
              f"{100 * (g['win_2r'] == 1).mean():>14.1f}% "
              f"{100 * (g['win_3r'] == 1).mean():>7.1f}% "
              f"{g['best_r'].median():>12.2f}R "
              f"{g['r_actual'].mean():>+9.3f}")


if __name__ == "__main__":
    main()
