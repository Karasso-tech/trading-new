"""READ-ONLY. Does any single rubric criterion actually separate winners from
losers?

Different question from "what does a trade that passed this criterion average".
A criterion earns its place only if WINNERS pass it more often than LOSERS do.
If they pass it equally it carries no information, whatever its averages look
like. If losers pass it MORE often, it is pointing the wrong way.
"""
import json
import math
import pathlib
import statistics
from collections import defaultdict

HERE = pathlib.Path(__file__).resolve().parent
CRITERIA = ("rr", "target_atr", "rs", "sma20_extension", "event")


def load(mode_allow_d: bool):
    trades, seen = [], set()
    for f in sorted(HERE.glob("results_portfolio_*s3*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if bool(d["summary"].get("allow_grade_d_research")) != mode_allow_d:
            continue
        for t in d.get("trades") or []:
            key = (t.get("ticker"), t.get("entry_date"), round(t.get("entry") or 0, 4))
            if key in seen or t.get("r") is None:
                continue
            seen.add(key)
            trades.append(t)
    return trades


def rate_margin(k, n):
    if not n:
        return 0.0
    p = k / n
    return 1.96 * math.sqrt(max(p * (1 - p), 1e-9) / n)


def main():
    trades = [t for t in load(False) if isinstance(t.get("rubric_criteria"), dict)]
    winners = [t for t in trades if t["r"] > 0]
    losers = [t for t in trades if t["r"] <= 0]
    print(f"{len(trades)} trades: {len(winners)} winners, {len(losers)} losers\n")

    print("Does a criterion separate winners from losers?")
    print("(a useful one is passed MORE by winners than by losers)\n")
    print(f"{'criterion':18s} {'winners pass':>13} {'losers pass':>12} {'gap':>18}")
    print("-" * 66)
    for c in CRITERIA:
        wk = sum(1 for t in winners if t["rubric_criteria"].get(c))
        lk = sum(1 for t in losers if t["rubric_criteria"].get(c))
        wp, lp = wk / len(winners), lk / len(losers)
        gap = wp - lp
        m = math.sqrt(rate_margin(wk, len(winners)) ** 2 + rate_margin(lk, len(losers)) ** 2)
        tell = "  <-- REAL" if abs(gap) > m else ""
        if wp == lp == 1.0:
            tell = "  <-- never fails: no information"
        print(f"{c:18s} {wp*100:>12.1f}% {lp*100:>11.1f}% "
              f"{gap*100:>+10.1f}pt +/-{m*100:.1f}{tell}")

    print("\n\nWhere a criterion acts: on how OFTEN you win, or on how MUCH\n")
    print(f"{'criterion':18s} {'':>7} {'win rate':>9} {'avg win':>9} {'avg loss':>9} {'mean R':>9}")
    print("-" * 68)
    for c in CRITERIA:
        for label in ("passed", "failed"):
            group = [t for t in trades
                     if bool(t["rubric_criteria"].get(c)) == (label == "passed")]
            head = c if label == "passed" else ""
            if not group:
                print(f"{head:18s} {label:>7} {'--':>9} {'--':>9} {'--':>9} {'--':>9}")
                continue
            w = [t["r"] for t in group if t["r"] > 0]
            l = [t["r"] for t in group if t["r"] <= 0]
            print(f"{head:18s} {label:>7} {100*len(w)/len(group):>8.1f}% "
                  f"{(statistics.mean(w) if w else 0):>+9.2f} "
                  f"{(statistics.mean(l) if l else 0):>+9.2f} "
                  f"{statistics.mean([t['r'] for t in group]):>+9.3f}")
        print()

    print("\nHow many criteria a trade passed, against what it did\n")
    print(f"{'passed':>7} {'trades':>7} {'win rate':>9} {'mean R':>9} {'median':>8}")
    print("-" * 45)
    by_score = defaultdict(list)
    for t in trades:
        by_score[sum(1 for c in CRITERIA if t["rubric_criteria"].get(c))].append(t["r"])
    for k in sorted(by_score):
        v = by_score[k]
        print(f"{k:>7} {len(v):>7} {100*sum(1 for x in v if x>0)/len(v):>8.1f}% "
              f"{statistics.mean(v):>+9.3f} {statistics.median(v):>+8.2f}")


if __name__ == "__main__":
    main()
