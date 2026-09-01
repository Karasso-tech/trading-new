"""The sealed year, opened once.

READ-ONLY. Prints numbers, changes nothing.

Everything from 2025-06-09 was set aside before any searching started. No
threshold was chosen on it, no model saw it, no table was read from it. This
file is the one and only time it is used, and it tests exactly one thing --
the only claim that survived every other check:

    setup shape crossed with the market regime at the moment the trigger fired

That claim was established twice already on data that did not choose it: the
pre-registered two-split test on R (both splits passed), and the exit-independent
barrier measurement on the searchable years. It says Reclaim in a choppy market
reaches +2R before its stop far more often than average, and Breakout in a
pullback far less.

Everything else died: no number predicts direction, no model predicts chance,
the best rule out of hundreds of thousands is always one market episode, and
the whole-market control found nothing our own filter could have destroyed.

So this is a single test with a single answer, and it is written down before
being run:

    PASS  -- Reclaim in choppy beats the sealed year's own base rate, AND
             Breakout in a pullback trails it, AND the Reclaim-minus-Breakout
             gap keeps its sign.
    FAIL  -- anything else.

There is no second attempt. Once this is read, the sealed year is spent, and
any future claim needs data that does not exist yet.

    python research/sealed_test.py
"""

from __future__ import annotations

import json
import math
import pathlib

import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
TARGET = "win_2r"


def band(x):
    """Half-width of the 95% interval for a share."""
    n = len(x)
    p = x.mean()
    return 1.96 * math.sqrt(p * (1 - p) / n) if n else float("nan")


def main():
    d = json.loads((HERE / "entries_v2.json").read_text(encoding="utf-8"))
    df = pd.DataFrame(d["rows"])
    df["fired_date"] = pd.to_datetime(df["fired_date"])
    df = df.dropna(subset=[TARGET])
    open_, sealed = df[~df["sealed"]], df[df["sealed"]]
    print(f"searchable years : {len(open_)} entries, "
          f"base {100 * open_[TARGET].mean():.1f}%")
    print(f"SEALED year      : {len(sealed)} entries, "
          f"{sealed['fired_date'].min().date()} .. "
          f"{sealed['fired_date'].max().date()}, "
          f"base {100 * sealed[TARGET].mean():.1f}%")

    print("\n" + "=" * 82)
    print("THE CLAIM, AND WHAT THE SEALED YEAR SAYS")
    print("=" * 82)
    print(f"\n  {'cell':>38} {'searchable':>18} {'SEALED':>18}")
    cells = [("Reclaim", "neutral_choppy"), ("Failed Breakdown", "neutral_choppy"),
             ("Reclaim", "risk_on"), ("Breakout", "healthy_uptrend"),
             ("Breakout", "pullback_in_uptrend")]
    for s, r in cells:
        a = open_[(open_["setup"] == s) & (open_["regime_at_fire"] == r)][TARGET]
        b = sealed[(sealed["setup"] == s) & (sealed["regime_at_fire"] == r)][TARGET]
        av = f"{100 * a.mean():.1f}% (n{len(a)})" if len(a) >= 40 else f"thin (n{len(a)})"
        bv = f"{100 * b.mean():.1f}% (n{len(b)})" if len(b) >= 40 else f"thin (n{len(b)})"
        print(f"  {s + ' · ' + r:>38} {av:>18} {bv:>18}")
    print(f"  {'everything':>38} "
          f"{100 * open_[TARGET].mean():>17.1f}% {100 * sealed[TARGET].mean():>17.1f}%")

    print(f"\n  {'setup on its own':>38} {'searchable':>18} {'SEALED':>18}")
    for s in sorted(df["setup"].dropna().unique()):
        a = open_[open_["setup"] == s][TARGET]
        b = sealed[sealed["setup"] == s][TARGET]
        av = f"{100 * a.mean():.1f}% (n{len(a)})" if len(a) >= 40 else f"thin (n{len(a)})"
        bv = f"{100 * b.mean():.1f}% (n{len(b)})" if len(b) >= 40 else f"thin (n{len(b)})"
        print(f"  {s:>38} {av:>18} {bv:>18}")

    print("\n" + "=" * 82)
    print("THE THREE CONDITIONS, WRITTEN BEFORE THIS RAN")
    print("=" * 82)
    base = sealed[TARGET].mean()
    rc = sealed[(sealed["setup"] == "Reclaim")
                & (sealed["regime_at_fire"] == "neutral_choppy")][TARGET]
    bp = sealed[(sealed["setup"] == "Breakout")
                & (sealed["regime_at_fire"] == "pullback_in_uptrend")][TARGET]
    rec = sealed[sealed["setup"] == "Reclaim"][TARGET]
    bre = sealed[sealed["setup"] == "Breakout"][TARGET]

    checks = []
    if len(rc) >= 40:
        ok = rc.mean() > base
        checks.append(ok)
        print(f"\n  1. Reclaim in a choppy market beats the base"
              f"        {'PASS' if ok else 'FAIL'}")
        print(f"     {100 * rc.mean():.1f}% ±{100 * band(rc):.1f} over {len(rc)}, "
              f"base {100 * base:.1f}%")
    else:
        print(f"\n  1. Reclaim in a choppy market            "
              f"CANNOT BE JUDGED, only {len(rc)} entries")
    if len(bp) >= 40:
        ok = bp.mean() < base
        checks.append(ok)
        print(f"\n  2. Breakout in a pullback trails the base"
              f"        {'PASS' if ok else 'FAIL'}")
        print(f"     {100 * bp.mean():.1f}% ±{100 * band(bp):.1f} over {len(bp)}, "
              f"base {100 * base:.1f}%")
    else:
        print(f"\n  2. Breakout in a pullback                "
              f"CANNOT BE JUDGED, only {len(bp)} entries")
    if len(rec) >= 40 and len(bre) >= 40:
        gap = rec.mean() - bre.mean()
        ok = gap > 0
        checks.append(ok)
        print(f"\n  3. Reclaim still beats Breakout overall  "
              f"        {'PASS' if ok else 'FAIL'}")
        print(f"     Reclaim {100 * rec.mean():.1f}% (n{len(rec)}), "
              f"Breakout {100 * bre.mean():.1f}% (n{len(bre)}), "
              f"gap {100 * gap:+.1f} points")

    print("\n" + "=" * 82)
    if checks and all(checks):
        print("VERDICT: PASSES. The one surviving claim held on a year it never saw.")
        print("It has now earned a proposal to the portfolio backtest -- whether")
        print("refusing a bad cell actually frees the slot for something better is")
        print("a question this cannot answer.")
    elif checks:
        print(f"VERDICT: FAILS. {sum(checks)} of {len(checks)} conditions held.")
        print("The last thing standing did not survive a year it had never seen.")
        print("No rule changes. The sealed year is spent.")
    else:
        print("VERDICT: could not be judged -- the sealed year is too thin in")
        print("the cells the claim is about. Stated as such rather than softened.")
    print("=" * 82)


if __name__ == "__main__":
    main()
