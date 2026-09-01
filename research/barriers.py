"""Which comes first from this entry -- the target, or the stop?

READ-ONLY over the bar files. Adds columns to the dataset. Touches no live code.

WHY THIS IS NOT THE SAME QUESTION AS BEFORE, and the earlier work did not cover it.

The previous pass asked "how far did the price travel". That is a question
about SIZE. The question actually being asked is about CHANCE: of all the
triggers the system could name today, which one is most likely to work.

Those are related but they are not the same, and one can exist without the
other. A signal could leave the average move untouched while still shifting
the odds -- more small wins and fewer small losses, with the big tails
unchanged. Rank correlation on a continuous move is not blind to that, but it
is not built to see it either.

So here the answer is the thing the system actually cares about, and it is
binary. Starting from the real entry price, with the plan's real stop distance:

    does price reach +1R, +2R or +3R before it reaches -1R?

One R is this plan's own stop distance, so every idea is judged on its own
scale and a wide-stop idea is not flattered against a tight-stop one. Nothing
about trailing, partials, runners or time exits enters -- only which line the
price touches first. That is as close to "was this trigger any good" as the
data can get.

Three honest details, each of which would otherwise bend the answer:

  * The bar's high and low do not say which came first INSIDE the day. When a
    bar touches both lines, this counts it as the STOP. That is the pessimistic
    reading and it is the right one: assuming the good line first would invent
    wins that never happened.
  * A trade that touches neither line inside the window is recorded as
    `unresolved`, not as a loss. Dropping them silently, or scoring them zero,
    would both quietly rewrite the question.
  * The window is fixed at 40 trading days for every row, so a fast idea and a
    slow idea are given the same amount of rope.

    python research/barriers.py
"""

from __future__ import annotations

import json
import pathlib

import build_dataset as bd

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "dataset.json"
SRC = HERE.parent / "backtest" / "signals_all_5y.json"
WINDOW = 40
RUNGS = (1.0, 2.0, 3.0)


def first_touch(bars, entry_i, entry_px, risk, window=WINDOW):
    """Walk the bars forward and record which line each rung met first.

    Returns, for every rung, one of: 1 (target first), 0 (stop first), or None
    (neither inside the window)."""
    stop_px = entry_px - risk
    out = {r: None for r in RUNGS}
    left = {r: entry_px + r * risk for r in RUNGS}
    for k in range(entry_i, min(entry_i + window + 1, len(bars))):
        lo, hi = bars[k]["low"], bars[k]["high"]
        stopped = lo <= stop_px
        for r in list(left):
            if out[r] is not None:
                continue
            # both lines inside one bar: the stop is assumed, always
            if stopped:
                out[r] = 0
            elif hi >= left[r]:
                out[r] = 1
        if stopped:
            break
    return out


def main():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    rows = data["rows"]
    master = {(r["ticker"], r["fired_date"]): r
              for r in json.loads(SRC.read_text(encoding="utf-8"))["rows"]
              if r.get("fired_date")}
    print(f"{len(rows)} entries to score")

    by_ticker = {}
    for i, r in enumerate(rows):
        by_ticker.setdefault(r["ticker"], []).append(i)

    counts = {r: {"win": 0, "loss": 0, "open": 0} for r in RUNGS}
    missing = 0
    for n, (ticker, idxs) in enumerate(sorted(by_ticker.items()), 1):
        bars = bd.load(ticker)
        if not bars:
            continue
        ix = {b["date"]: i for i, b in enumerate(bars)}
        if n % 150 == 0:
            print(f"  {n}/{len(by_ticker)} companies")
        for i in idxs:
            r = rows[i]
            m = master.get((r["ticker"], r["fired_date"]))
            risk = (m or {}).get("risk_per_share")
            j = ix.get(r["fired_date"])
            if not risk or risk <= 0 or j is None or j + 1 >= len(bars):
                missing += 1
                continue
            res = first_touch(bars, j + 1, r["entry_px"], risk)
            r["risk_per_share"] = risk
            for rung, v in res.items():
                r[f"win_{rung:g}r"] = v
                key = "open" if v is None else ("win" if v else "loss")
                counts[rung][key] += 1

    print(f"\n  {missing} entries had no usable stop distance and were left blank")
    print("\n" + "=" * 74)
    print(f"WHAT ACTUALLY HAPPENS FROM OUR ENTRIES, INSIDE {WINDOW} TRADING DAYS")
    print("=" * 74)
    print("  One R is that idea's own stop distance. A bar touching both lines")
    print("  is counted as the stop.\n")
    print(f"  {'reached':>10} {'first':>8} {'stop first':>12} {'neither':>9} "
          f"{'chance it works':>17}")
    for rung in RUNGS:
        c = counts[rung]
        decided = c["win"] + c["loss"]
        pct = 100 * c["win"] / decided if decided else 0
        print(f"  {'+' + format(rung, 'g') + 'R':>10} {c['win']:>8} {c['loss']:>12} "
              f"{c['open']:>9} {pct:>16.1f}%")
    print("\n  'chance it works' counts only the ones that resolved, so the")
    print("  unresolved ones cannot quietly be scored as either.")

    data["barrier_window"] = WINDOW
    data["barrier_rungs"] = list(RUNGS)
    DATA.write_text(json.dumps(data), encoding="utf-8")
    print(f"\nwritten back into {DATA.name}")


if __name__ == "__main__":
    main()
