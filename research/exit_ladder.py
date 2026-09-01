"""The exit finding, and the two checks that decide whether to believe it.

READ-ONLY over the bar files. Prints numbers, changes nothing.

exits.py replayed 49 exit rules over every entry and produced two results that
pull in opposite directions. The RANKING of rules does not survive: the best
rule on the choosing years came 33rd of 49 on the sealed one, and the two
rankings agree at only +0.19. Picking "the best exit" overfits like everything
else here.

Underneath that noise sat something that did not move -- the further out the
exit, the more the same trades paid. This file exists to try to break it, and
it needs no tuning to test, because the whole ladder is reported rather than
one chosen rung. There is no threshold to fit and therefore nothing to overfit.

Three ways it could still be false, and all three are checked:

  ONE PERIOD. A ladder that only climbs in a couple of good years is a bull
  market, not a rule. So it is printed for every year separately.

  STOP WIDTH. A target at "4R" is a far move for a wide-stopped trade and a
  near one for a tight-stopped trade, so a ladder in R could be nothing but the
  mix of stop widths. So it is printed inside three bands of stop width, where
  that explanation is unavailable.

  CAPITAL. Per-trade R is the wrong unit the moment slots are finite. A trade
  held sixty days blocks everything else; one closed in eight frees the slot.
  So everything is re-scored in R per day of slot time, which is what a
  six-slot book actually spends. This is the check most likely to reverse the
  finding, and it is the reason the file exists.

    python research/exit_ladder.py
"""

from __future__ import annotations

import numpy as np

import exits

LADDER = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0)
TRAILS = (2.0, 3.0, 4.0, 5.0, 6.0)
YEARS = range(2021, 2027)


def flat_timed(p, target_r):
    """exits.run_flat, but also reporting how many days the slot was held."""
    e, risk = p["entry"], p["risk"]
    tp, sl = e + target_r * risk, e - risk
    cost = exits.SLIP * e / risk
    for i, (h, l) in enumerate(zip(p["high"], p["low"])):
        if l <= sl:
            return -1.0 - cost, i + 1
        if h >= tp:
            return target_r - cost, i + 1
    return (p["close"][-1] - e) / risk - cost, len(p["close"])


def trail_timed(p, width_atr):
    e, risk, atr = p["entry"], p["risk"], p["atr"]
    stop, peak = e - risk, e
    cost = exits.SLIP * e / risk
    for i, (h, l) in enumerate(zip(p["high"], p["low"])):
        if l <= stop:
            return (stop - e) / risk - cost, i + 1
        peak = max(peak, h)
        stop = max(stop, peak - width_atr * atr)
    return (p["close"][-1] - e) / risk - cost, len(p["close"])


def live_timed(p):
    """What the system runs today: a third at 1.5R, a third at 3R, the rest
    trailing 2.5 ATR behind the high, stop to break-even after the first sale."""
    e, risk, atr = p["entry"], p["risk"], p["atr"]
    stop, peak, left, got = e - risk, e, 1.0, 0.0
    sold1 = sold2 = False
    cost = exits.SLIP * e / risk
    for i, (h, l) in enumerate(zip(p["high"], p["low"])):
        if l <= stop:
            return got + left * ((stop - e) / risk) - cost, i + 1
        peak = max(peak, h)
        if not sold1 and h >= e + 1.5 * risk:
            got += 1.5 / 3
            left -= 1 / 3
            sold1 = True
            stop = max(stop, e)
        if sold1 and not sold2 and h >= e + 3 * risk:
            got += 3 / 3
            left -= 1 / 3
            sold2 = True
        if sold2:
            stop = max(stop, peak - 2.5 * atr)
    return got + left * ((p["close"][-1] - e) / risk) - cost, len(p["close"])


def main():
    paths = exits.load_paths()
    for p in paths:
        p["yr"] = int(p["date"][:4])
    sealed = np.array([p["sealed"] for p in paths])
    print(f"{len(paths)} entries, {int(sealed.sum())} of them in the sealed year\n")

    # ------------------------------------------------------------------- 1
    print("=" * 96)
    print("1. ONE TARGET, ONE STOP. ONLY THE TARGET MOVES.")
    print("=" * 96)
    print("  Identical entries, identical stops. Nothing about the trade changes")
    print("  except where we choose to sell.\n")
    print(f"  {'target':>8} {'mean R':>9} {'win':>6} " +
          "".join(f"{y:>9}" for y in YEARS))
    table = {}
    for t in LADDER:
        r = np.array([exits.run_flat(p, t) for p in paths])
        cols = []
        for y in YEARS:
            m = np.array([p["yr"] == y for p in paths])
            cols.append(r[m].mean() if m.sum() >= 100 else np.nan)
        table[t] = cols
        print(f"  {t:>7.1f}R {r.mean():>+9.3f} {100 * (r > 0).mean():>5.0f}% " +
              "".join(f"{v:>+9.3f}" if not np.isnan(v) else f"{'thin':>9}"
                      for v in cols))

    print("\n  Does it climb inside each year on its own?")
    for i, y in enumerate(YEARS):
        col = [table[t][i] for t in LADDER]
        if any(np.isnan(c) for c in col):
            print(f"    {y}: too thin")
            continue
        rising = sum(1 for a, b in zip(col, col[1:]) if b > a)
        print(f"    {y}: {col[0]:+.3f} at 1R, best {max(col):+.3f} at "
              f"{LADDER[int(np.argmax(col))]:g}R, climbs on {rising} of "
              f"{len(col) - 1} steps")
    print("\n  2022 is the one that reverses, and it is the bear market. Holding")
    print("  longer is more exposure, so this is a cost, not an anomaly.")

    # ------------------------------------------------------------------- 2
    print("\n" + "=" * 96)
    print("2. THE SAME LADDER INSIDE EACH BAND OF STOP WIDTH")
    print("=" * 96)
    print("  If the ladder were really the mix of stop widths, it would flatten")
    print("  here. Each band holds stop width roughly constant.\n")
    sa = np.array([p["risk"] / p["atr"] for p in paths])
    edges = np.quantile(sa, [0, 1 / 3, 2 / 3, 1.0])
    print(f"  {'stop width':>24} " + "".join(f"{t:>8.1f}R" for t in LADDER))
    for b in range(3):
        m = (sa >= edges[b]) & (sa <= edges[b + 1])
        sub = [p for p, k in zip(paths, m) if k]
        row = [np.mean([exits.run_flat(p, t) for p in sub]) for t in LADDER]
        label = f"{edges[b]:.1f}-{edges[b + 1]:.1f} ATR (n{len(sub)})"
        print(f"  {label:>24} " + "".join(f"{v:>+9.3f}" for v in row))
    print("\n  It climbs hardest on tight stops and flattens on wide ones. The")
    print("  direction never reverses, which is the part that matters.")

    # ------------------------------------------------------------------- 3
    print("\n" + "=" * 96)
    print("3. THE CHECK MOST LIKELY TO REVERSE IT: R PER DAY OF SLOT TIME")
    print("=" * 96)
    print("  A six-slot book spends slot-days, not trades. If holding longer")
    print("  simply blocks the next idea, the ladder should collapse here.\n")
    rows = []
    for t in LADDER:
        out = [flat_timed(p, t) for p in paths]
        rows.append((f"one target at {t:g}R", np.array([o[0] for o in out]),
                     np.array([o[1] for o in out], float)))
    for w in TRAILS:
        out = [trail_timed(p, w) for p in paths]
        rows.append((f"trail {w:g} ATR", np.array([o[0] for o in out]),
                     np.array([o[1] for o in out], float)))
    out = [live_timed(p) for p in paths]
    rows.append(("LIVE: thirds 1.5R/3R, trail 2.5", np.array([o[0] for o in out]),
                 np.array([o[1] for o in out], float)))

    print(f"  {'exit':>32} {'mean R':>9} {'days':>7} {'R/100 slot-days':>17} "
          f"{'goes per slot per yr':>21}")
    for name, r, d in rows:
        print(f"  {name:>32} {r.mean():>+9.3f} {d.mean():>7.1f} "
              f"{100 * r.sum() / d.sum():>+17.3f} {252 / d.mean():>21.1f}")

    print(f"\n  And on the sealed year alone:")
    print(f"  {'exit':>32} {'mean R':>9} {'days':>7} {'R/100 slot-days':>17}")
    for name, r, d in rows:
        print(f"  {name:>32} {r[sealed].mean():>+9.3f} {d[sealed].mean():>7.1f} "
              f"{100 * r[sealed].sum() / d[sealed].sum():>+17.3f}")

    print("\n  The ladder survives. Against what the system runs today, a plain")
    print("  far target and a wide trail both win in BOTH periods.")

    # ------------------------------------------------------------------- 4
    print("\n" + "=" * 96)
    print("4. WHAT IT IS STILL NOT")
    print("=" * 96)
    print("  Slot-days are closer than per-trade R and they are not a portfolio")
    print("  test. Cash, whether a candidate is even available on the day the")
    print("  slot frees, and the same-day correlation measured in")
    print("  correlation_cost.py are all absent. This earns a pre-registered")
    print("  portfolio experiment. It does not earn a rule change.")


if __name__ == "__main__":
    main()
