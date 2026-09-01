"""If the morning decides, six positions are not six bets. How many are they?

READ-ONLY. Prints numbers, changes nothing. Proposes no rule change.

This is the one place the day finding pays, and it pays whether or not a good
morning can ever be predicted -- which it cannot.

Position sizing quietly assumes independence. Six slots at 1% each is treated
as six separate 1% coin flips, and the worst realistic day is reasoned about as
if the six could not all fail together. But whether an entry works turns out to
be 48% about the morning it fired on and 0% about the company. Entries opened
near each other are therefore partly the SAME bet wearing six names, and the
real spread of outcomes is wider than the arithmetic says.

Three ways of putting a number on it, because a single formula could be wrong:

  1. The plain statistic -- how much two entries from the same morning agree,
     beyond what chance gives. This turns directly into "six positions are
     really N".
  2. A direct measurement, no formula: take real baskets of six that were
     actually open together, and compare their spread against baskets of six
     drawn from different days. If the day matters, the real baskets are wider.
  3. What it costs in the tail: how often a real basket of six loses all six,
     against how often independence says it should.

    python research/correlation_cost.py
"""

from __future__ import annotations

import json
import pathlib
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

HERE = pathlib.Path(__file__).resolve().parent
TARGET = "win_2r"
SLOTS = 6
WINDOW_DAYS = 10       # "open together" means opened inside this many days
DRAWS = 20000


def main():
    d = json.loads((HERE / "dataset.json").read_text(encoding="utf-8"))["rows"]
    df = pd.DataFrame(d)
    df["fired_date"] = pd.to_datetime(df["fired_date"])
    df = df.dropna(subset=[TARGET]).sort_values("fired_date").reset_index(drop=True)
    y = df[TARGET].to_numpy(float)
    p = y.mean()
    print(f"{len(df)} entries, hit rate {100 * p:.1f}%")

    # ------------------------------------------------------------------ 1
    print("\n" + "=" * 78)
    print("1. HOW MUCH DO TWO ENTRIES FROM ONE MORNING AGREE?")
    print("=" * 78)
    g = df.groupby("fired_date")[TARGET].agg(["mean", "size"])
    g = g[g["size"] >= 3]
    within = p * (1 - p)
    between = max(g["mean"].var() - (within / g["size"]).mean(), 0.0)
    icc = between / (between + within)
    print(f"\n  spread between mornings, real part   {between:.5f}")
    print(f"  spread inside a morning              {within:.5f}")
    print(f"  agreement between two same-day entries: {icc:.3f}")
    print(f"\n  {'positions held together':>26} {'really worth':>14} {'risk is really':>16}")
    for n in (2, 3, 4, 6, 8):
        eff = n / (1 + (n - 1) * icc)
        print(f"  {n:>26} {eff:>13.1f} {100 * np.sqrt(n / eff) - 100:>15.0f}% more")
    print("\n  Reading the last column: six slots sized as six independent bets")
    print(f"  carry about {100 * np.sqrt(SLOTS / (SLOTS / (1 + (SLOTS - 1) * icc))) - 100:.0f}% more swing than the arithmetic assumes.")

    # ------------------------------------------------------------------ 2
    print("\n" + "=" * 78)
    print(f"2. THE SAME THING MEASURED, NOT DERIVED")
    print("=" * 78)
    print(f"  Real baskets: {SLOTS} entries that actually opened within")
    print(f"  {WINDOW_DAYS} days of each other. Fake baskets: {SLOTS} entries pulled")
    print("  from anywhere in the five years. Same size, same hit rate.")

    rng = np.random.default_rng(11)
    dates = df["fired_date"].to_numpy()
    real, fake = [], []
    for _ in range(DRAWS):
        s = rng.integers(0, len(df) - 1)
        near = np.where((dates >= dates[s])
                        & (dates <= dates[s] + np.timedelta64(WINDOW_DAYS, "D")))[0]
        if len(near) < SLOTS:
            continue
        real.append(y[rng.choice(near, SLOTS, replace=False)].sum())
        fake.append(y[rng.choice(len(y), SLOTS, replace=False)].sum())
    real, fake = np.array(real), np.array(fake)
    print(f"\n  {len(real)} baskets of each")
    print(f"  {'':>22} {'real baskets':>14} {'spread out':>12}")
    print(f"  {'average wins of ' + str(SLOTS):>22} {real.mean():>14.2f} {fake.mean():>12.2f}")
    print(f"  {'spread':>22} {real.std():>14.2f} {fake.std():>12.2f}")
    print(f"  {'widest 5%':>22} {np.percentile(real, 95):>14.0f} "
          f"{np.percentile(fake, 95):>12.0f}")
    print(f"\n  the real baskets swing "
          f"{100 * (real.std() / fake.std() - 1):+.0f}% wider")

    # ------------------------------------------------------------------ 3
    print("\n" + "=" * 78)
    print("3. WHAT IT COSTS IN THE BAD TAIL")
    print("=" * 78)
    print(f"  How often do all {SLOTS} fail together?")
    all_lose_real = 100 * (real == 0).mean()
    all_lose_fake = 100 * (fake == 0).mean()
    print(f"\n    real baskets, opened together : {all_lose_real:>5.1f}% of the time")
    print(f"    spread across the years       : {all_lose_fake:>5.1f}%")
    print(f"    plain arithmetic says         : {100 * (1 - p) ** SLOTS:>5.1f}%")
    print(f"\n  and all {SLOTS} winning together:")
    print(f"    real baskets                  : {100 * (real == SLOTS).mean():>5.1f}%")
    print(f"    spread across the years       : {100 * (fake == SLOTS).mean():>5.1f}%")
    print(f"    plain arithmetic says         : {100 * p ** SLOTS:>5.1f}%")
    print("\n  Both tails are fatter than the arithmetic. That is what shared")
    print("  weather does: the good days are better and the bad days are worse")
    print("  than six independent bets would ever produce.")


if __name__ == "__main__":
    main()
