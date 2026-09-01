"""Does the entry rule beat buying on a random day?

READ-ONLY over the bar files. Prints numbers, changes nothing.

Why this is the question that had to be asked and never was. Every measurement
so far compared our entries against OTHER entries of ours -- grade A against
grade C, breakout against reclaim, one volatility band against another. All of
those can come out flat for a reason nobody checked: the entry rule may already
be doing the work, leaving only noise to rank. Or it may be doing nothing, and
the flatness is the whole truth.

There is exactly one way to tell them apart: a control group.

The control here is deliberately harsh. For every real entry -- same ticker,
same day -- a set of fake entries is drawn on OTHER days for the same ticker,
inside the same five years. Same company, same era, same market weather on
average. The only thing removed is our reason for picking that day.

If the real entries move further up than the fake ones, the rule works and the
argument moves to "we cannot rank inside it". If they do not, the rule adds
nothing and everything downstream of it has been rearranging noise.

    python research/vs_random.py
"""

from __future__ import annotations

import json
import pathlib
import random
import statistics

import numpy as np
from scipy import stats

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
BARS = ROOT / "backtest" / "data" / "bars"
import argparse
H = 20
DRAWS = 5          # fake entries per real entry
SEED = 20260831
NEAR_DAYS = 21     # for the month-matched control below


def load(t):
    p = BARS / f"{t}.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    return d["bars"] if isinstance(d, dict) else d


def true_ranges(h, l, c):
    tr = [h[0] - l[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return tr


def wilder(tr, n=14):
    out = [None] * len(tr)
    if len(tr) < n:
        return out
    cur = sum(tr[:n]) / n
    out[n - 1] = cur
    for i in range(n, len(tr)):
        cur = (cur * (n - 1) + tr[i]) / n
        out[i] = cur
    return out


def path(bars, atr14, i):
    """The same three answers as the real dataset, from an entry at bars[i+1]
    open, held H days, with no stop and no target."""
    e = i + 1
    if e + H >= len(bars) or not atr14[i]:
        return None
    px, atr = bars[e]["open"], atr14[i]
    hi = max(b["high"] for b in bars[e:e + H + 1])
    lo = min(b["low"] for b in bars[e:e + H + 1])
    return {"up": (hi - px) / atr, "down": (lo - px) / atr,
            "end": (bars[e + H]["close"] - px) / atr}


def main():
    rows = json.loads((HERE / "dataset.json").read_text(encoding="utf-8"))["rows"]
    by_ticker = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r["fired_date"])
    print(f"{len(rows)} real entries across {len(by_ticker)} companies")

    rng = random.Random(SEED)
    real, fake = [], []
    paired = []          # one real and its own fakes, kept together

    for t, dates in sorted(by_ticker.items()):
        bars = load(t)
        if not bars:
            continue
        ix = {b["date"]: i for i, b in enumerate(bars)}
        atr14 = wilder(true_ranges([b["high"] for b in bars],
                                   [b["low"] for b in bars],
                                   [b["close"] for b in bars]))
        # the pool of fake days: every day this ticker had bars inside the same
        # window the real entries came from, so era is held constant
        idxs = sorted(ix[d] for d in dates if d in ix)
        if not idxs:
            continue
        lo_i, hi_i = min(idxs), max(idxs)
        pool = [i for i in range(max(lo_i, 200), min(hi_i + 1, len(bars) - H - 2))]
        if len(pool) < DRAWS * 2:
            continue
        pool_set = set(pool)
        for d in dates:
            i = ix.get(d)
            if i is None:
                continue
            pr = path(bars, atr14, i)
            if not pr:
                continue
            fs = []
            for j in rng.sample(pool, min(DRAWS, len(pool))):
                pf = path(bars, atr14, j)
                if pf:
                    fs.append(pf)
            # The sharper control: fake days from the SAME MONTH as the real
            # one. The wide control above still lets a 2023 entry be compared
            # against a 2022 day, so part of any gap could be market timing
            # rather than day-picking. Here the weather is held fixed and the
            # only thing left is whether THIS day was worth choosing.
            near = [j for j in range(i - NEAR_DAYS, i + NEAR_DAYS + 1)
                    if j != i and j in pool_set]
            ns = []
            for j in (rng.sample(near, min(DRAWS, len(near))) if near else []):
                pn = path(bars, atr14, j)
                if pn:
                    ns.append(pn)
            if not fs:
                continue
            real.append(pr)
            fake.extend(fs)
            paired.append((pr, fs, ns))

    print(f"{len(real)} real entries matched against {len(fake)} random days "
          f"on the same companies, same years\n")

    print("=" * 78)
    print(f"WHAT HAPPENED IN THE {H} DAYS AFTER, IN ATR")
    print("=" * 78)
    print(f"  {'':>14} {'our entries':>14} {'random days':>14} {'gap':>10} {'±':>8}")
    for key, label in (("up", "went up"), ("down", "went down"), ("end", "ended at")):
        a = np.array([r[key] for r in real])
        b = np.array([f[key] for f in fake])
        gap = a.mean() - b.mean()
        se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
        tag = "real" if abs(gap) > 1.96 * se else "inside the noise"
        print(f"  {label:>14} {a.mean():>+14.3f} {b.mean():>+14.3f} "
              f"{gap:>+10.3f} {1.96 * se:>8.3f}  {tag}")

    a = np.array([r["up"] / max(abs(r["down"]), 0.25) for r in real])
    b = np.array([f["up"] / max(abs(f["down"]), 0.25) for f in fake])
    gap = statistics.median(a) - statistics.median(b)
    print(f"  {'up/down':>14} {statistics.median(a):>+14.3f} "
          f"{statistics.median(b):>+14.3f} {gap:>+10.3f}    (medians)")

    print("\n" + "=" * 78)
    print("THE SAME THING PAIRED -- each real entry against its own fake days")
    print("=" * 78)
    print("  Removes any chance that the answer is really about which companies")
    print("  or which years happened to supply more entries.")
    for key, label in (("up", "went up"), ("down", "went down"), ("end", "ended at")):
        diffs = np.array([p[key] - statistics.mean([f[key] for f in fs])
                          for p, fs, _ in paired])
        t = stats.ttest_1samp(diffs, 0)
        won = 100 * (diffs > 0).mean()
        print(f"  {label:>14} average difference {diffs.mean():>+7.3f}   "
              f"our day was better {won:>4.0f}% of the time   t {t.statistic:>+6.2f}")


    print("\n" + "=" * 78)
    print(f"THE SHARPEST CONTROL -- the same stock within {NEAR_DAYS} trading days")
    print("=" * 78)
    print("  Same company, same month, same market weather. The ONLY thing left")
    print("  is whether the entry rule picked a better day than a coin would.")
    tight = [(p, ns) for p, _, ns in paired if ns]
    print(f"  {len(tight)} real entries had nearby days to compare against")
    for key, label in (("up", "went up"), ("down", "went down"), ("end", "ended at")):
        diffs = np.array([p[key] - statistics.mean([n[key] for n in ns])
                          for p, ns in tight])
        t = stats.ttest_1samp(diffs, 0)
        won = 100 * (diffs > 0).mean()
        print(f"  {label:>14} average difference {diffs.mean():>+7.3f}   "
              f"our day was better {won:>4.0f}% of the time   t {t.statistic:>+6.2f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=20)
    H = ap.parse_args().horizon
    main()
