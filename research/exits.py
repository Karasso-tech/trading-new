"""If the money is in the exit, then find the exit.

READ-ONLY over the bar files. Prints numbers, changes nothing.

Everything up to here proved a negative: nothing known before an entry
separates a good one from a bad one, not among our entries and not in 121,054
days of the whole market. That answer is solid and it is also only half the
work, because the same tables can answer a completely different question that
was never run.

For all 9,473 entries the full price path afterwards is now on hand. So every
exit rule that could have been used on them can simply be replayed and scored.
No prediction is needed. Nothing has to be known in advance. The stock does
what it did; only what WE do changes.

What is replayed:

  * plain target-and-stop, at every distance from 1R to 5R
  * pure trailing from the entry, at every width from 1 to 4 ATR
  * the shape the system actually uses -- sell part at a first target, part at
    a second, let the rest run behind a trailing stop -- across the grid of
    where those targets sit and how tight the runner trails
  * a time limit, on and off, at several lengths

How it is judged, and this is where earlier passes went wrong:

  * The rule is CHOSEN on 2021 through May 2025 and then scored on the year
    after it, which no exit rule here was tuned on.
  * An honest note about that year: it was already opened once, for a single
    pre-registered test of a setup claim. Five cell numbers were read from it.
    That is a small leak for a question about exits, which is a different
    dimension entirely -- but it is a leak, it is stated here rather than
    quietly ignored, and it means this test is strong, not pristine.
  * Best-trade share is reported beside every average, because one +40R trade
    can carry a whole rule and has before.

    python research/exits.py
"""

from __future__ import annotations

import json
import pathlib
import warnings

import numpy as np
import pandas as pd

import build_dataset as bd
from build_v2 import Arrays

warnings.filterwarnings("ignore")

HERE = pathlib.Path(__file__).resolve().parent
SEALED_FROM = "2025-06-09"
MAX_BARS = 60
SLIP = 5.0 / 10_000     # the same 5 basis points the study charges


def load_paths():
    """Entry, risk and the bars afterwards, for every entry. Built once."""
    master = [r for r in json.loads(
        (HERE.parent / "backtest" / "signals_all_5y.json").read_text(encoding="utf-8")
    )["rows"] if r.get("fired") and r.get("fired_date")]
    by_t = {}
    for r in master:
        by_t.setdefault(r["ticker"], []).append(r)

    out = []
    for t, group in sorted(by_t.items()):
        raw = bd.load(t)
        if not raw:
            continue
        a = Arrays(raw)
        for r in group:
            j = a.date_ix.get(r["fired_date"])
            risk = r.get("risk_per_share")
            if j is None or not risk or risk <= 0 or j + 1 >= len(a.close):
                continue
            e = j + 1
            end = min(e + MAX_BARS, len(a.close))
            if end - e < 25:
                continue
            atr = a.atr14[j]
            if not atr or np.isnan(atr):
                continue
            out.append({
                "ticker": t, "date": r["fired_date"], "setup": r.get("setup"),
                "entry": a.open[e], "risk": risk, "atr": atr,
                "high": a.high[e:end], "low": a.low[e:end], "close": a.close[e:end],
                "sealed": r["fired_date"] >= SEALED_FROM,
            })
    return out


# --------------------------------------------------------------------- rules
def run_flat(p, target_r, time_bars=None):
    """One target, one stop, optionally a time limit. The simplest thing there is."""
    e, risk = p["entry"], p["risk"]
    tp, sl = e + target_r * risk, e - risk
    for i, (h, l) in enumerate(zip(p["high"], p["low"])):
        if time_bars is not None and i >= time_bars:
            break
        if l <= sl:                       # a bar touching both counts as the stop
            return -1.0 - SLIP * e / risk
        if h >= tp:
            return target_r - SLIP * e / risk
    n = min(time_bars, len(p["close"])) if time_bars else len(p["close"])
    return (p["close"][n - 1] - e) / risk - SLIP * e / risk


def run_trail(p, trail_atr, arm_r=0.0):
    """Trail behind the highest point reached, once the trade is arm_r ahead."""
    e, risk, atr = p["entry"], p["risk"], p["atr"]
    stop, peak, armed = e - risk, e, arm_r <= 0
    for h, l in zip(p["high"], p["low"]):
        if l <= stop:
            return (stop - e) / risk - SLIP * e / risk
        peak = max(peak, h)
        if not armed and peak >= e + arm_r * risk:
            armed = True
        if armed:
            stop = max(stop, peak - trail_atr * atr)
    return (p["close"][-1] - e) / risk - SLIP * e / risk


def run_tranches(p, t1_r, t2_r, trail_atr, breakeven=True, time_bars=None):
    """The shape the system uses: a third out at each target, a third running.

    After the first target the remaining stop moves to break-even, which is
    what the live rules do and what makes the runner survivable."""
    e, risk, atr = p["entry"], p["risk"], p["atr"]
    stop, peak = e - risk, e
    left, got = 1.0, 0.0
    sold1 = sold2 = False
    for i, (h, l) in enumerate(zip(p["high"], p["low"])):
        if time_bars is not None and i >= time_bars:
            break
        if l <= stop:
            return got + left * ((stop - e) / risk) - SLIP * e / risk
        peak = max(peak, h)
        if not sold1 and h >= e + t1_r * risk:
            got += (1 / 3) * t1_r
            left -= 1 / 3
            sold1 = True
            if breakeven:
                stop = max(stop, e)
        if sold1 and not sold2 and h >= e + t2_r * risk:
            got += (1 / 3) * t2_r
            left -= 1 / 3
            sold2 = True
        if sold2:                          # only the runner trails
            stop = max(stop, peak - trail_atr * atr)
    return got + left * ((p["close"][-1] - e) / risk) - SLIP * e / risk


def grid():
    rules = []
    for t in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0):
        rules.append((f"one target at {t:g}R", lambda p, t=t: run_flat(p, t)))
    for t in (2.0, 3.0):
        for n in (10, 20, 40):
            rules.append((f"target {t:g}R, out after {n} days",
                          lambda p, t=t, n=n: run_flat(p, t, n)))
    for w in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0):
        rules.append((f"trail {w:g} ATR from the start",
                      lambda p, w=w: run_trail(p, w)))
        rules.append((f"trail {w:g} ATR, armed at 1R",
                      lambda p, w=w: run_trail(p, w, 1.0)))
    for t1 in (1.0, 1.5, 2.0):
        for t2 in (2.0, 3.0, 4.0):
            if t2 <= t1:
                continue
            for w in (1.5, 2.5, 3.5):
                rules.append((f"thirds at {t1:g}R and {t2:g}R, runner trails {w:g} ATR",
                              lambda p, a=t1, b=t2, w=w: run_tranches(p, a, b, w)))
    return rules


def score(paths, fn):
    r = np.array([fn(p) for p in paths])
    tot = r.sum()
    best = r.max()
    return {"n": len(r), "mean": r.mean(), "median": float(np.median(r)),
            "total": tot, "win": 100 * (r > 0).mean(),
            "best_share": (100 * best / tot) if tot > 0 else float("nan"),
            "without_best": r[r < best].mean() if len(r) > 1 else np.nan}


def main():
    paths = load_paths()
    train = [p for p in paths if not p["sealed"]]
    test = [p for p in paths if p["sealed"]]
    print(f"{len(paths)} entries with a full path")
    print(f"  {len(train)} up to {SEALED_FROM} -- the rule is chosen here")
    print(f"  {len(test)} after it -- the rule is scored here\n")

    rules = grid()
    print(f"{len(rules)} exit rules replayed over every entry.")
    print("Costs are charged on every one of them.\n")

    rows = []
    for name, fn in rules:
        s = score(train, fn)
        s["name"] = name
        rows.append(s)
    df = pd.DataFrame(rows).sort_values("mean", ascending=False)

    print("=" * 96)
    print("1. THE CHOOSING YEARS -- BEST FIFTEEN")
    print("=" * 96)
    print(f"  {'exit rule':>46} {'mean R':>8} {'median':>8} {'total':>9} "
          f"{'win':>6} {'best trade':>11}")
    for _, r in df.head(15).iterrows():
        print(f"  {r['name']:>46} {r['mean']:>+8.3f} {r['median']:>+8.2f} "
              f"{r['total']:>+9.0f} {r['win']:>5.0f}% {r['best_share']:>10.1f}%")
    print(f"\n  {'... and the worst five':>46}")
    for _, r in df.tail(5).iterrows():
        print(f"  {r['name']:>46} {r['mean']:>+8.3f} {r['median']:>+8.2f} "
              f"{r['total']:>+9.0f} {r['win']:>5.0f}% {r['best_share']:>10.1f}%")

    print("\n" + "=" * 96)
    print("2. AND ON THE YEAR THEY WERE NOT CHOSEN ON")
    print("=" * 96)
    print("  If the order changes completely here, the ranking above was luck.")
    pick = df.head(8)["name"].tolist() + df.tail(3)["name"].tolist()
    fns = dict(rules)
    print(f"\n  {'exit rule':>46} {'chosen on':>11} {'SEALED':>10} "
          f"{'sealed rank':>12}")
    sealed_scores = {n: score(test, fns[n]) for n, _ in rules}
    order = sorted(sealed_scores, key=lambda n: -sealed_scores[n]["mean"])
    rank = {n: i + 1 for i, n in enumerate(order)}
    for n in pick:
        tr = df[df["name"] == n].iloc[0]
        se = sealed_scores[n]
        print(f"  {n:>46} {tr['mean']:>+11.3f} {se['mean']:>+10.3f} "
              f"{rank[n]:>7} of {len(rules)}")

    tr_rank = {r["name"]: i + 1 for i, (_, r) in enumerate(df.iterrows())}
    from scipy import stats as st
    common = [n for n, _ in rules]
    rho = st.spearmanr([tr_rank[n] for n in common],
                       [rank[n] for n in common]).statistic
    print(f"\n  agreement between the two rankings: {rho:+.2f}")
    print("  (+1 means the sealed year agrees completely, 0 means no relation)")

    print("\n" + "=" * 96)
    print("3. WHAT THE SYSTEM RUNS TODAY, AGAINST THE ALTERNATIVES")
    print("=" * 96)
    print("  The live shape is thirds at two targets with the runner trailing.")
    print("  Everything is on the same 9,473 entries, so only the exit differs.")
    now = "thirds at 1.5R and 3R, runner trails 2.5 ATR"
    for label, rows_ in (("chosen on", train), ("SEALED year", test)):
        print(f"\n  {label}:")
        show = [now, "one target at 2R", "one target at 3R",
                "trail 2.5 ATR, armed at 1R", "trail 3 ATR from the start",
                "one target at 1R"]
        for n in show:
            if n not in fns:
                continue
            s = score(rows_, fns[n])
            print(f"    {n:>46} mean {s['mean']:>+7.3f}  total {s['total']:>+7.0f}  "
                  f"win {s['win']:>3.0f}%  best trade {s['best_share']:>5.1f}%")

    print("\n" + "=" * 96)
    print("4. THE PART THAT DOES NOT NEED ANY PREDICTION")
    print("=" * 96)
    print("  These are the plain facts of the 9,473 entries. No model, no rule,")
    print("  no choosing. They are what any exit has to work with.")
    r1 = np.array([run_flat(p, 1.0) for p in paths])
    r2 = np.array([run_flat(p, 2.0) for p in paths])
    r3 = np.array([run_flat(p, 3.0) for p in paths])
    for lbl, arr, t in (("1R", r1, 1), ("2R", r2, 2), ("3R", r3, 3)):
        hit = (arr > t - 0.1).mean()
        print(f"    reaches {lbl} before the stop: {100 * hit:>5.1f}%   "
              f"a flat {lbl} target pays {arr.mean():+.3f}R per trade")


if __name__ == "__main__":
    main()
