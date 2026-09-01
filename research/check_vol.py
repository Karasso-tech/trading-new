"""READ-ONLY. One range survived both splits: the most volatile tenth of trades.
Before it is believed, the four ways it could be fake are checked.

1. COSTS. The exit charge is a fixed fraction of price divided by the stop
   distance, so it is mechanically larger for a tight stop -- and a tight stop
   is what a low-volatility stock has. Costs alone could manufacture this whole
   finding. Checked against r_gross, which has no charge in it.
2. ONE TRADE. The best trade's share of the group's total.
3. ONE YEAR. 2022 was violent; if the whole effect is 2022 it is a market, not
   a rule.
4. ONE COMPANY. If a handful of tickers carry it, it is those companies.

Also: is it the top tenth only, or does it climb the whole way? A cliff at the
tenth decile is a fitted edge. A steady climb is a real relationship.
"""
import json
import pathlib
import statistics
from collections import defaultdict

SRC = pathlib.Path(__file__).resolve().parent.parent / "backtest" / "signals_all_5y.json"
CUT = 4.62

rows = [r for r in json.loads(SRC.read_text(encoding="utf-8"))["rows"]
        if r.get("r") is not None and r.get("is_fully_closed")
        and isinstance(r.get("atr_pct"), (int, float))]

hi = [r for r in rows if r["atr_pct"] >= CUT]
lo = [r for r in rows if r["atr_pct"] < CUT]
print(f"{len(rows)} finished trades. volatility >= {CUT}%: {len(hi)}, below: {len(lo)}")

print("\n1) IS IT THE COSTS?")
for name, key in (("net of costs", "r"), ("before costs", "r_gross")):
    h = [r[key] for r in hi if r.get(key) is not None]
    l = [r[key] for r in lo if r.get(key) is not None]
    print(f"   {name:14s} high vol {statistics.mean(h):+.3f}   "
          f"low vol {statistics.mean(l):+.3f}   gap {statistics.mean(h)-statistics.mean(l):+.3f}")
print("   if the gap barely moves between the two lines, costs are not the cause.")

print("\n2) IS IT ONE TRADE?")
for name, g in (("high vol", hi), ("low vol", lo)):
    rs = sorted((r["r"] for r in g), reverse=True)
    tot = sum(rs)
    print(f"   {name:9s} total {tot:+8.1f}R   best trade {rs[0]:+.1f}R = "
          f"{100*rs[0]/tot:.0f}% of it   without top 5: "
          f"{statistics.mean(rs[5:]):+.3f} per trade")

print("\n3) IS IT ONE YEAR?")
print(f"   {'year':>6} {'high vol':>22} {'low vol':>22}")
by = defaultdict(lambda: ([], []))
for r in rows:
    y = (r.get("fired_date") or "????")[:4]
    by[y][0 if r["atr_pct"] >= CUT else 1].append(r["r"])
for y in sorted(by):
    h, l = by[y]
    hs = f"{statistics.mean(h):+.3f} (n{len(h)})" if len(h) >= 20 else f"thin (n{len(h)})"
    ls = f"{statistics.mean(l):+.3f} (n{len(l)})" if len(l) >= 20 else f"thin (n{len(l)})"
    print(f"   {y:>6} {hs:>22} {ls:>22}")

print("\n4) IS IT A FEW COMPANIES?")
per = defaultdict(list)
for r in hi:
    per[r["ticker"]].append(r["r"])
big = {t: v for t, v in per.items() if len(v) >= 5}
tot = sum(sum(v) for v in per.values())
top = sorted(per.items(), key=lambda kv: -sum(kv[1]))[:5]
print(f"   {len(per)} companies in the high-volatility group, "
      f"{len(big)} with 5+ trades")
print(f"   the five biggest contributors are {100*sum(sum(v) for _, v in top)/tot:.0f}% "
      f"of the group's total R")
for t, v in top:
    print(f"     {t:6s} n={len(v):<4} total {sum(v):+7.1f}R")
share = [statistics.mean(v) for v in big.values()]
print(f"   of the {len(big)} companies with 5+ trades, "
      f"{sum(1 for m in share if m > 0)} are profitable "
      f"({100*sum(1 for m in share if m > 0)/len(big):.0f}%)")

print("\n5) A CLIFF, OR A CLIMB?")
have = sorted(rows, key=lambda r: r["atr_pct"])
size = len(have) // 10
print(f"   {'volatility band':>22} {'n':>6} {'mean R':>9} {'win%':>6}")
for b in range(10):
    c = have[b*size: (b+1)*size if b < 9 else len(have)]
    rs = [x["r"] for x in c]
    print(f"   {c[0]['atr_pct']:>9.2f} .. {c[-1]['atr_pct']:<9.2f} {len(c):>6} "
          f"{statistics.mean(rs):>+9.3f} {100*sum(1 for x in rs if x>0)/len(rs):>5.0f}%")

print("\n6) IS IT REALLY VOLATILITY, OR THE SETUP SHAPE RIDING ALONG?")
print(f"   {'setup':>18} {'high vol':>22} {'low vol':>22}")
bys = defaultdict(lambda: ([], []))
for r in rows:
    bys[r.get("setup") or "?"][0 if r["atr_pct"] >= CUT else 1].append(r["r"])
for s in sorted(bys):
    h, l = bys[s]
    hs = f"{statistics.mean(h):+.3f} (n{len(h)})" if len(h) >= 30 else f"thin (n{len(h)})"
    ls = f"{statistics.mean(l):+.3f} (n{len(l)})" if len(l) >= 30 else f"thin (n{len(l)})"
    print(f"   {s:>18} {hs:>22} {ls:>22}")
