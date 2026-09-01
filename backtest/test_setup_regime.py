"""Runs PREREGISTRATION_SETUP_REGIME.md, exactly as written.

READ-ONLY: reads the signal-study table, prints a verdict, changes nothing.

The rule is derived on a training half by a fixed procedure and judged on a
held-out half it had no say in choosing. Everything the procedure could be
tempted by -- which cells to block, the cutoff, the minimum cell size, what
counts as a pass -- was written down before this file existed. It is repeated
in the code so the two cannot drift.
"""

from __future__ import annotations

import json
import math
import pathlib
import statistics
from collections import defaultdict

HERE = pathlib.Path(__file__).resolve().parent

MIN_CELL = 100        # cells thinner than this are never scored, never blocked
BLOCK_BELOW = 0.0     # "loses money" needs no calibration
MIN_REFUSED_PCT = 10  # a rule that refuses almost nothing earns no credit
TIME_SPLIT = "2024-08-01"


def load():
    d = json.loads((HERE / "signals_all_5y.json").read_text(encoding="utf-8"))
    return [r for r in d["rows"]
            if r.get("r") is not None and r.get("is_fully_closed")
            and r.get("setup") and r.get("regime_at_fire")]


def cell(r):
    return (r["setup"], r["regime_at_fire"])


def derive(train: list) -> tuple:
    """The blocked cells, and the table that produced them."""
    by = defaultdict(list)
    for r in train:
        by[cell(r)].append(r["r"])
    blocked, table = set(), []
    for k, v in sorted(by.items(), key=lambda kv: statistics.mean(kv[1])):
        big = len(v) >= MIN_CELL
        mean = statistics.mean(v)
        if big and mean < BLOCK_BELOW:
            blocked.add(k)
        table.append((k, len(v), mean, big and mean < BLOCK_BELOW, big))
    return blocked, table


def judge(test: list, blocked: set) -> dict:
    kept = [r["r"] for r in test if cell(r) not in blocked]
    refused = [r["r"] for r in test if cell(r) in blocked]
    everything = [r["r"] for r in test]
    if not kept or not everything:
        return {}
    return {
        "all_n": len(everything), "all_mean": statistics.mean(everything),
        "all_total": sum(everything),
        "kept_n": len(kept), "kept_mean": statistics.mean(kept),
        "kept_total": sum(kept),
        "refused_n": len(refused),
        "refused_pct": 100 * len(refused) / len(everything),
        "refused_mean": statistics.mean(refused) if refused else None,
    }


def run(name: str, train: list, test: list) -> bool:
    print(f"\n{'=' * 74}\n{name}\n{'=' * 74}")
    print(f"  training: {len(train)} finished trades · held out: {len(test)}")

    blocked, table = derive(train)
    print(f"\n  what the training half says (cells under {MIN_CELL} are never scored):")
    for (setup, reg), n, mean, is_blocked, big in table:
        mark = "  BLOCK" if is_blocked else ("" if big else "  (too thin)")
        print(f"    {setup:18s} {reg:22s} n={n:<5} {mean:+.3f}{mark}")

    if not blocked:
        print("\n  nothing qualified for blocking -- no rule to test.")
        return False

    v = judge(test, blocked)
    if not v:
        print("\n  the held-out half has nothing to judge on.")
        return False

    print(f"\n  on the held-out half:")
    print(f"    take everything      n={v['all_n']:<5} mean {v['all_mean']:+.3f}  "
          f"total {v['all_total']:+.1f}")
    print(f"    apply the rule       n={v['kept_n']:<5} mean {v['kept_mean']:+.3f}  "
          f"total {v['kept_total']:+.1f}")
    print(f"    refused              n={v['refused_n']:<5} "
          f"({v['refused_pct']:.0f}% of the half) "
          f"mean {v['refused_mean']:+.3f}" if v["refused_mean"] is not None else "")

    c1 = v["kept_mean"] > v["all_mean"]
    c2 = v["kept_total"] > v["all_total"]
    c3 = v["refused_pct"] >= MIN_REFUSED_PCT
    print(f"\n    1. mean R improves           {'PASS' if c1 else 'FAIL'}")
    print(f"    2. total R improves          {'PASS' if c2 else 'FAIL'}")
    print(f"    3. refuses at least {MIN_REFUSED_PCT}%      {'PASS' if c3 else 'FAIL'}")
    ok = c1 and c2 and c3
    print(f"    ==> {'PASSES' if ok else 'FAILS'}")
    return ok


def main() -> None:
    rows = load()
    print(f"{len(rows)} finished trades with a setup and a fire-time regime")

    by_time = run(
        "SPLIT 1 -- BY TIME  (learn on 2021-08..2024-07, judge on 2024-08 onward)",
        [r for r in rows if (r.get("fired_date") or "") < TIME_SPLIT],
        [r for r in rows if (r.get("fired_date") or "") >= TIME_SPLIT])

    names = sorted({r["ticker"] for r in rows})
    even = {t for i, t in enumerate(names) if i % 2 == 0}
    by_ticker = run(
        "SPLIT 2 -- BY TICKER  (learn on every other company, judge on the rest)",
        [r for r in rows if r["ticker"] in even],
        [r for r in rows if r["ticker"] not in even])

    print(f"\n{'=' * 74}")
    print("VERDICT, against the conditions written down before this ran")
    print(f"{'=' * 74}")
    print(f"  time split   : {'PASS' if by_time else 'FAIL'}")
    print(f"  ticker split : {'PASS' if by_ticker else 'FAIL'}")
    if by_time and by_ticker:
        print("\n  ESTABLISHED. Both splits pass, so the pattern survives on data that")
        print("  did not choose it. This earns the right to PROPOSE a rule change and")
        print("  test it inside the portfolio backtest -- it does not make the change.")
    elif by_time or by_ticker:
        which = "period" if by_time else "set of companies"
        print(f"\n  NOT ESTABLISHED. One split of two is a coin. Passing only one means")
        print(f"  the pattern is about this {which}, not about the setups. No rule changes.")
    else:
        print("\n  NOT ESTABLISHED. The pattern did not survive either split -- it was")
        print("  a feature of the sample it was found in. No rule changes.")


if __name__ == "__main__":
    main()
