"""A rule can beat 200 shuffles and still be one week of 2022.

READ-ONLY. Prints numbers, changes nothing.

This file exists because of a mistake that nearly went out as a finding.

The searches in winners.py, after_target1.py and universe_chance.py each look
through hundreds of thousands of candidate rules and keep the best. The honest
guard against that is a permutation max-statistic test: repeat the ENTIRE
search on shuffled outcomes, many times, and see how good the best rule looks
when nothing is there. Three rules cleared it outright -- no shuffle out of 200
came near any of them.

All three were market episodes.

The permutation test answers "how much did you search". It is completely blind
to "every trade in this rule happened inside one two-month stretch", because
shuffling outcomes leaves the calendar structure of the rule untouched. A rule
that fires 360 times, all of them in the fourth quarter of 2022, has exactly
the same permutation null as one spread evenly across five years.

So the two checks are not alternatives. Both are required, always, and this is
the second one.

    python research/episode_check.py
"""

from __future__ import annotations

import json
import pathlib

import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
MIN_PER_YEAR = 25
MIN_YEARS = 3
MIN_SAME_SIGN = 0.8


def entries():
    d = json.loads((HERE / "entries_v2.json").read_text(encoding="utf-8"))
    df = pd.DataFrame(d["rows"])
    df["fired_date"] = pd.to_datetime(df["fired_date"])
    return df[~df["sealed"]].reset_index(drop=True)


def check(df, target, mask, name, words, source):
    d = df.dropna(subset=[target]).copy()
    d["yr"] = d["fired_date"].dt.year
    m = mask.reindex(d.index).fillna(False).astype(bool)
    base = d[target].mean()

    print(f"\n  {name}")
    print(f"    the rule    : {words}")
    print(f"    checked on  : {source}")
    print(f"    pooled      : inside {100 * d[m][target].mean():.1f}% "
          f"(n{int(m.sum())}), outside {100 * d[~m][target].mean():.1f}%, "
          f"base {100 * base:.1f}%")

    print(f"\n    {'year':>6} {'inside':>19} {'outside':>21} {'gap':>8}")
    gaps = []
    for y, g in d.groupby("yr"):
        gm = m.reindex(g.index).fillna(False).astype(bool)
        a, b = g[gm][target], g[~gm][target]
        if len(a) < MIN_PER_YEAR:
            print(f"    {y:>6} {'too thin to judge':>19}   (n={len(a)})")
            continue
        gaps.append(a.mean() - b.mean())
        print(f"    {y:>6} {100 * a.mean():>12.1f}% (n{len(a):>4}) "
              f"{100 * b.mean():>14.1f}% (n{len(b):>5}) "
              f"{100 * (a.mean() - b.mean()):>+8.1f}")

    # TWO things have to hold. An earlier draft checked only the first, and
    # passed a rule that reversed by 19 points in one of its four years --
    # which is precisely the failure this whole file exists to catch. Counting
    # judgeable years is not the same as the gap keeping its sign in them.
    n, same = len(gaps), sum(1 for g in gaps if g > 0)
    if n < MIN_YEARS:
        v = (f"only {n} year(s) had enough trades. That by itself is the "
             f"answer: an episode, not a rule.")
    elif same / n < MIN_SAME_SIGN:
        v = (f"positive in only {same} of {n} judgeable years. It reverses, "
             f"so it is a market, not a rule.")
    else:
        v = (f"positive in {same} of {n} judgeable years -- this one is worth "
             f"taking further.")
    print(f"    ==> {v}")


def main():
    df = entries()
    print(f"{len(df)} of our entries in the searchable years")
    print("\n" + "=" * 86)
    print("THE THREE RULES THAT BEAT EVERY ONE OF 200 SHUFFLES")
    print("=" * 86)

    check(df, "win_1r",
          (df["spy_dist_sma200_atr"] < -2.137) & (df["month"] > 9),
          "reaches +1R before the stop, 77% against a 52% base",
          "index far below its 200-day average AND October to December",
          "our entries, where it was found")

    check(df, "win_2r",
          (df["stop_atr"] < 1.565) & (df["corr_spy_60"] > 0.7655),
          "the runner reaches +2R, 48% against a 34% base",
          "tight stop AND the stock moves closely with the index",
          "our entries, where it was found")

    # This one came out of the WHOLE-MARKET search, so it is re-checked on the
    # market table rather than quietly swapped onto our entries, which is a
    # different sample and would be a different test.
    uni = HERE / "universe_v2.parquet"
    if uni.exists():
        u = pd.read_parquet(uni)
        u["fired_date"] = pd.to_datetime(u["fired_date"])
        u = u[~u["sealed"]].reset_index(drop=True)
        check(u, "win_2r",
              (u["pct_of_52w_range"] < 49.77) & (u["month"] > 10),
              "reaches +2R, 52.5% against a 33.6% base",
              "below the middle of its yearly range AND November or December",
              "121,054 market days, where it was found")
    else:
        print("\n  The whole-market rule cannot be re-checked here:")
        print("  universe_v2.parquet is not on disk (it is rebuilt output, not")
        print("  source). Rebuild it with `python research/build_v2.py`.")

    print("\n" + "=" * 86)
    print("WHAT TO TAKE FROM THIS")
    print("=" * 86)
    print("  Each of these cleared the shuffle test without difficulty, and each")
    print("  concentrates in one or two market stretches.")
    print("  A rule needs BOTH: luck must not be able to find it, and it has to")
    print("  exist in more than one market. Neither check substitutes for the")
    print("  other, and running only the first is how a search convinces itself.")


if __name__ == "__main__":
    main()
