"""READ-ONLY. What do the profitable entries have in common -- and does it tell
them apart from the unprofitable ones?

"What winners share" is close to worthless on its own: if the losers share it
too, it separates nothing. So every figure below sits next to the losers' figure
for the same thing, and the last column is the only one that matters -- how far
apart the two groups actually are.

Only what was knowable BEFORE the trade decided anything is used. No MFE, no
bars held, no resolution: those are the outcome wearing the clothes of a cause.

Three cuts, as asked: every winner, the better half of them, the top quarter.
"""
import json
import math
import pathlib
import statistics

SRC = pathlib.Path(__file__).resolve().parent.parent / "backtest" / "signals_all_5y.json"

# Everything here is fixed at, or before, the moment of entry.
FEATURES = [
    ("dist_sma20_atr",        "distance from SMA20, in ATR (build)"),
    ("dist_sma20_atr_at_fire", "distance from SMA20, in ATR (at fire)"),
    ("rs20",                  "relative strength, 20 days"),
    ("rs5",                   "relative strength, 5 days"),
    ("atr_pct",               "volatility: ATR as % of price"),
    ("earnings_days_out",     "trading days to the next earnings"),
    ("rr_at_fire",            "reward:risk at the real entry"),
    ("entry_gap_pct",         "gap from trigger to fill, %"),
    ("days_to_fire",          "days the plan waited"),
    ("stop_atr",              "stop distance, in ATR"),
    ("rubric_score",          "rubric score, 0-5"),
    ("targets",               "how many qualifying targets"),
]


def prep(rows):
    out = []
    for r in rows:
        if r.get("r") is None or not r.get("is_fully_closed"):
            continue
        atr = r.get("atr_at_build")
        rps = r.get("risk_per_share")
        r = dict(r)
        r["stop_atr"] = (rps / atr) if (atr and rps) else None
        out.append(r)
    return out


def vals(group, key):
    return [g[key] for g in group if isinstance(g.get(key), (int, float))]


def sep(a, b):
    """How far apart two groups are, in pooled standard deviations.

    Plain effect size. Under 0.2 is nothing you could act on however many
    trades produced it; the sample here is large enough that almost any
    difference is 'statistically significant', which is exactly why that phrase
    is not used anywhere in this output."""
    if len(a) < 30 or len(b) < 30:
        return None
    sa, sb = statistics.stdev(a), statistics.stdev(b)
    pooled = math.sqrt(((len(a) - 1) * sa * sa + (len(b) - 1) * sb * sb)
                       / (len(a) + len(b) - 2))
    return (statistics.mean(a) - statistics.mean(b)) / pooled if pooled else None


def table(title, winners, losers):
    print(f"\n{'=' * 92}\n{title}")
    print(f"  {len(winners)} winners vs {len(losers)} losers")
    print(f"{'=' * 92}")
    print(f"  {'':38s} {'winners':>18} {'losers':>18} {'apart':>8}")
    print(f"  {'':38s} {'median (25-75%)':>18} {'median (25-75%)':>18}")
    scored = []
    for key, label in FEATURES:
        w, l = vals(winners, key), vals(losers, key)
        if len(w) < 30 or len(l) < 30:
            continue
        qw, ql = statistics.quantiles(w, n=4), statistics.quantiles(l, n=4)
        d = sep(w, l)
        scored.append((abs(d) if d else 0, label, statistics.median(w), qw,
                       statistics.median(l), ql, d))
    for _, label, mw, qw, ml, ql, d in sorted(scored, reverse=True):
        flag = ""
        if d is not None and abs(d) >= 0.20:
            flag = "  <-- real"
        elif d is not None and abs(d) >= 0.10:
            flag = "  <-- slight"
        print(f"  {label:38s} {mw:>7.2f} ({qw[0]:>5.2f},{qw[2]:>5.2f}) "
              f"{ml:>7.2f} ({ql[0]:>5.2f},{ql[2]:>5.2f}) {d:>+8.2f}{flag}")


def main():
    rows = prep(json.loads(SRC.read_text(encoding="utf-8"))["rows"])
    winners = sorted([r for r in rows if r["r"] > 0], key=lambda r: -r["r"])
    losers = [r for r in rows if r["r"] <= 0]
    print(f"{len(rows)} finished trades: {len(winners)} winners, {len(losers)} losers")
    print(f"winners average {statistics.mean([r['r'] for r in winners]):+.2f}R, "
          f"losers {statistics.mean([r['r'] for r in losers]):+.2f}R")

    table("ALL WINNERS against all losers", winners, losers)
    table("THE BETTER HALF OF THE WINNERS against all losers",
          winners[:len(winners) // 2], losers)
    table("THE TOP QUARTER OF THE WINNERS against all losers",
          winners[:len(winners) // 4], losers)

    print(f"\n{'=' * 92}")
    print("Reading this: 'apart' is the gap between the two groups measured in")
    print("standard deviations. Below 0.20 is not something a person could trade on,")
    print("no matter how many thousands of trades produced it.")


if __name__ == "__main__":
    main()
