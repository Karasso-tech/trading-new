"""READ-ONLY. Is there a NUMBER, or a RANGE of numbers, that predicts a winner?

The previous pass compared averages, which can only see "more is better" or
"less is better". A sweet spot in the middle -- volatility between 2 and 3,
say -- is invisible to it and would have been reported as "no signal".

So every feature is cut into ten equal-sized buckets and each bucket's real
result is printed. A range that works shows up as a run of good buckets.

The trap this creates, and the reason for the last section: twelve features by
ten buckets is 120 cells, and the best of 120 cells looks impressive even when
every feature is pure noise. So anything that stands out here is re-checked on
data that had no part in finding it, using the same two splits as the earlier
pre-registration.
"""
import json
import math
import pathlib
import statistics

SRC = pathlib.Path(__file__).resolve().parent.parent / "backtest" / "signals_all_5y.json"
BINS = 10
TIME_SPLIT = "2024-08-01"

FEATURES = [
    ("dist_sma20_atr",         "distance from SMA20 at build (ATR)"),
    ("dist_sma20_atr_at_fire", "distance from SMA20 at fire (ATR)"),
    ("rs20",                   "relative strength 20d (%)"),
    ("rs5",                    "relative strength 5d (%)"),
    ("atr_pct",                "volatility, ATR as % of price"),
    ("earnings_days_out",      "days to earnings"),
    ("rr_at_fire",             "reward:risk at the real entry"),
    ("entry_gap_pct",          "gap from trigger to fill (%)"),
    ("days_to_fire",           "days the plan waited"),
    ("stop_atr",               "stop distance (ATR)"),
]


def prep():
    rows = json.loads(SRC.read_text(encoding="utf-8"))["rows"]
    out = []
    for r in rows:
        if r.get("r") is None or not r.get("is_fully_closed"):
            continue
        atr, rps = r.get("atr_at_build"), r.get("risk_per_share")
        r = dict(r)
        r["stop_atr"] = (rps / atr) if (atr and rps) else None
        out.append(r)
    return out


def bucket(rows, key, nbins=BINS):
    """Equal-count buckets, so every bucket carries the same weight of evidence."""
    have = [r for r in rows if isinstance(r.get(key), (int, float))]
    if len(have) < nbins * 40:
        return []
    have.sort(key=lambda r: r[key])
    size = len(have) // nbins
    out = []
    for b in range(nbins):
        chunk = have[b * size: (b + 1) * size if b < nbins - 1 else len(have)]
        rs = [c["r"] for c in chunk]
        m = 1.96 * statistics.stdev(rs) / math.sqrt(len(rs)) if len(rs) > 1 else None
        out.append({
            "lo": chunk[0][key], "hi": chunk[-1][key], "n": len(chunk),
            "mean": statistics.mean(rs), "margin": m,
            "win": 100 * sum(1 for x in rs if x > 0) / len(rs),
        })
    return out


def show(rows, key, label):
    bs = bucket(rows, key)
    if not bs:
        return None
    overall = statistics.mean([r["r"] for r in rows if isinstance(r.get(key), (int, float))])
    print(f"\n  {label}   (all buckets average {overall:+.3f}R)")
    print(f"    {'range':>22} {'n':>5} {'mean R':>9} {'margin':>8} {'win%':>6}")
    best = None
    for b in bs:
        # A bucket only counts as standing out if its whole margin clears the
        # overall average -- otherwise it is the best of ten random draws.
        clears = b["margin"] is not None and (b["mean"] - b["margin"]) > overall
        mark = "  <--" if clears else ""
        print(f"    {b['lo']:>10.2f} .. {b['hi']:<9.2f} {b['n']:>5} {b['mean']:>+9.3f} "
              f"{(b['margin'] or 0):>8.3f} {b['win']:>5.0f}%{mark}")
        if clears and (best is None or b["mean"] > best["mean"]):
            best = dict(b, key=key, label=label)
    return best


def validate(rows, cand):
    """Re-check a standout range on data that had no say in finding it."""
    key, lo, hi = cand["key"], cand["lo"], cand["hi"]
    inside = lambda r: isinstance(r.get(key), (int, float)) and lo <= r[key] <= hi

    def one(train_pred, name):
        train = [r for r in rows if train_pred(r)]
        test = [r for r in rows if not train_pred(r)]
        ti = [r["r"] for r in test if inside(r)]
        to = [r["r"] for r in test if not inside(r)]
        if len(ti) < 100 or len(to) < 100:
            return f"    {name:16s} too few to judge"
        d = statistics.mean(ti) - statistics.mean(to)
        m = 1.96 * math.sqrt(statistics.variance(ti) / len(ti)
                             + statistics.variance(to) / len(to))
        ok = d - m > 0
        return (f"    {name:16s} inside {statistics.mean(ti):+.3f} (n{len(ti)})  "
                f"outside {statistics.mean(to):+.3f} (n{len(to)})  "
                f"gap {d:+.3f} ±{m:.3f}  {'HOLDS' if ok else 'does not hold'}")

    names = sorted({r["ticker"] for r in rows})
    even = {t for i, t in enumerate(names) if i % 2 == 0}
    print(f"\n  {cand['label']}  range {lo:.2f} .. {hi:.2f}")
    print(one(lambda r: (r.get("fired_date") or "") < TIME_SPLIT, "held-out time"))
    print(one(lambda r: r["ticker"] in even, "held-out tickers"))


def main():
    rows = prep()
    print(f"{len(rows)} finished trades, average "
          f"{statistics.mean([r['r'] for r in rows]):+.3f}R")
    print("\nEach feature cut into ten equal-sized buckets. An arrow marks a bucket")
    print("whose entire margin sits above the overall average.")
    cands = []
    for key, label in FEATURES:
        c = show(rows, key, label)
        if c:
            cands.append(c)

    print(f"\n{'=' * 84}")
    print("THE SAME RANGES, ON DATA THAT DID NOT FIND THEM")
    print("Twelve features by ten buckets is 120 cells. The best of 120 looks good")
    print("even when nothing is there, so a range only counts if it survives here.")
    print(f"{'=' * 84}")
    if not cands:
        print("\n  No bucket stood out in the first place.")
        return
    for c in cands:
        validate(rows, c)


if __name__ == "__main__":
    main()
