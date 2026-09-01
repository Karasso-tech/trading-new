"""Once a trade reaches its first target, what decides whether the runner pays?

READ-ONLY over the bar files. Prints numbers, changes nothing.

This is the question with the most money attached in the whole system: the
runner tranche is where the entire measured edge lives, and the runner only
earns on trades that keep going after the first target.

A first look said speed decides it, and decisively -- reach +1R inside three
days and 70% go on to +2R; take more than fifteen days and only 33% do. Before
believing any of that, two confounds have to be removed, and both of them would
manufacture exactly that pattern out of nothing:

  THE WINDOW. Follow-through was measured inside a fixed 40 days FROM ENTRY. A
  trade that reached +1R on day 2 then had 38 days to reach +2R; one that
  reached it on day 20 had 20. The slow group was given half the rope. Fixed
  here by starting the clock at the moment target 1 is touched and giving every
  trade the same 20 bars from there.

  THE UNIT. R is the trade's own stop distance, so "+2R" is a small move for a
  tight-stopped idea and a large one for a wide-stopped idea. Rank trades by
  anything correlated with stop width and follow-through in R will sort itself,
  meaning nothing. Fixed by reporting the same thing in ATR beside it, where
  the stop plays no part.

What replaces the naive measure:

    from the bar that first touches +1R, over the next 20 bars,
    does price reach +2R before falling back to the entry price

Falling back to entry is the real alternative, not the original stop: once the
first target is sold, the system does not let the rest go back to a full loss.

    python research/follow_through.py
"""

from __future__ import annotations

import json
import pathlib
import warnings

import numpy as np
import pandas as pd
from scipy import stats

import build_dataset as bd
from build_v2 import Arrays

warnings.filterwarnings("ignore")

HERE = pathlib.Path(__file__).resolve().parent
SEALED_FROM = "2025-06-09"
AFTER = 20          # bars given to every trade, counted from the 1R touch
FIRST = 40          # bars allowed to reach 1R in the first place


def measure(a: Arrays, entry_i, entry_px, risk):
    """Find the 1R touch, then judge the next 20 bars from there."""
    stop = entry_px - risk
    touch = None
    for k in range(entry_i, min(entry_i + FIRST + 1, len(a.close))):
        if a.low[k] <= stop:
            return None                      # stopped before target 1
        if a.high[k] >= entry_px + risk:
            touch = k
            break
    if touch is None or touch + AFTER >= len(a.close):
        return None

    w = slice(touch, touch + AFTER + 1)
    hi, lo = a.high[w], a.low[w]
    atr = a.atr14[entry_i - 1]
    reached2 = back_to_entry = None
    for n, (h, l) in enumerate(zip(hi, lo)):
        if reached2 is None and h >= entry_px + 2 * risk:
            reached2 = n
        if back_to_entry is None and l <= entry_px:
            back_to_entry = n
        if reached2 is not None or back_to_entry is not None:
            break
    return {
        "days_to_1r": touch - entry_i,
        # the honest binary: 2R first, or back to entry first, same rope for all
        "runner_paid": 1 if (reached2 is not None and
                             (back_to_entry is None or reached2 <= back_to_entry))
                       else 0,
        "best_after_r": (hi.max() - entry_px) / risk,
        "best_after_atr": (hi.max() - entry_px) / atr if atr else None,
        "worst_after_r": (lo.min() - entry_px) / risk,
        # how far it travelled BEFORE the first target, in ATR -- speed with the
        # stop taken out of it
        "atr_per_day_to_1r": (risk / atr / max(touch - entry_i, 1)) if atr else None,
    }


def main():
    d = json.loads((HERE / "entries_v2.json").read_text(encoding="utf-8"))
    rows = pd.DataFrame(d["rows"])
    master = {(r["ticker"], r["fired_date"]): r
              for r in json.loads((HERE.parent / "backtest" / "signals_all_5y.json")
                                  .read_text(encoding="utf-8"))["rows"]
              if r.get("fired_date")}

    out = []
    by_t = {}
    for i, r in rows.iterrows():
        by_t.setdefault(r["ticker"], []).append(i)
    for t, idxs in sorted(by_t.items()):
        raw = bd.load(t)
        if not raw:
            continue
        a = Arrays(raw)
        for i in idxs:
            r = rows.loc[i]
            m = master.get((r["ticker"], r["fired_date"]))
            risk = (m or {}).get("risk_per_share")
            j = a.date_ix.get(r["fired_date"])
            if not risk or j is None or j + 1 >= len(a.close):
                continue
            res = measure(a, j + 1, a.open[j + 1], risk)
            if res:
                res.update({k: r[k] for k in
                            ("ticker", "fired_date", "setup", "regime_at_fire",
                             "sealed", "r_actual", "stop_atr", "atr_pct",
                             "year_high_r", "clear_air_r", "clear_air_atr",
                             "dist_high_252_atr", "corr_spy_60", "rr_at_fire")})
                out.append(res)

    df = pd.DataFrame(out)
    df["fired_date"] = pd.to_datetime(df["fired_date"])
    open_ = df[~df["sealed"]].copy()
    print(f"{len(df)} entries reached target 1 and had {AFTER} bars afterwards")
    print(f"  {len(open_)} open for searching, {int(df['sealed'].sum())} sealed")
    print(f"\n  runner paid (2R before back to entry): "
          f"{100 * open_['runner_paid'].mean():.1f}%")

    print("\n" + "=" * 88)
    print("1. DOES SPEED STILL MATTER ONCE EVERY TRADE GETS THE SAME ROPE?")
    print("=" * 88)
    print(f"  Every trade below is given the same {AFTER} bars, counted from the")
    print("  moment it touched target 1. The window confound is gone.")
    open_["speed"] = pd.cut(open_["days_to_1r"], [-1, 1, 3, 7, 15, 99],
                            labels=["1 day", "2-3 days", "4-7 days",
                                    "8-15 days", "over 15 days"])
    print(f"\n  {'days to target 1':>18} {'trades':>8} {'runner paid':>13} "
          f"{'best after, R':>15} {'best after, ATR':>17} {'real R':>9}")
    for s, g in open_.groupby("speed", observed=True):
        print(f"  {str(s):>18} {len(g):>8} "
              f"{100 * g['runner_paid'].mean():>12.1f}% "
              f"{g['best_after_r'].median():>14.2f} "
              f"{g['best_after_atr'].median():>16.2f} "
              f"{g['r_actual'].mean():>+9.3f}")
    fast = open_[open_["days_to_1r"] <= 3]["runner_paid"]
    slow = open_[open_["days_to_1r"] > 15]["runner_paid"]
    se = np.sqrt(fast.var() / len(fast) + slow.var() / len(slow))
    gap = fast.mean() - slow.mean()
    print(f"\n  fast minus slow: {100 * gap:+.1f} points ±{100 * 1.96 * se:.1f}  "
          f"{'real' if abs(gap) > 1.96 * se else 'inside the noise'}")
    print("\n  The ATR column is the check that matters: if it moves with the R")
    print("  column, speed is real. If only the R column moves, it was stop width.")

    print("\n" + "=" * 88)
    print("2. YEAR BY YEAR, SO ONE PERIOD CANNOT CARRY IT")
    print("=" * 88)
    open_["yr"] = open_["fired_date"].dt.year
    print(f"\n  {'year':>6} {'fast (<=3 days)':>20} {'slow (>15 days)':>20} {'gap':>8}")
    for y, g in open_.groupby("yr"):
        f_ = g[g["days_to_1r"] <= 3]["runner_paid"]
        s_ = g[g["days_to_1r"] > 15]["runner_paid"]
        if len(f_) < 25 or len(s_) < 25:
            print(f"  {y:>6} too thin (fast n={len(f_)}, slow n={len(s_)})")
            continue
        print(f"  {y:>6} {100 * f_.mean():>13.1f}% (n{len(f_):>3}) "
              f"{100 * s_.mean():>13.1f}% (n{len(s_):>3}) "
              f"{100 * (f_.mean() - s_.mean()):>+8.1f}")

    print("\n" + "=" * 88)
    print("3. IS IT SPEED, OR IS IT STOP WIDTH WEARING SPEED'S CLOTHES?")
    print("=" * 88)
    print("  A tight stop makes +1R a short move, so a tight-stopped trade gets")
    print("  there faster AND needs less to reach +2R. Both columns would move")
    print("  together for that reason alone. So: the same comparison inside each")
    print("  band of stop width, where that explanation is unavailable.")
    open_["sb"] = pd.qcut(open_["stop_atr"], 4, labels=False, duplicates="drop")
    print(f"\n  {'stop width':>14} {'median stop':>13} {'fast':>16} {'slow':>16} {'gap':>8}")
    for b in sorted(open_["sb"].dropna().unique()):
        g = open_[open_["sb"] == b]
        f_ = g[g["days_to_1r"] <= 3]["runner_paid"]
        s_ = g[g["days_to_1r"] > 15]["runner_paid"]
        if len(f_) < 25 or len(s_) < 25:
            print(f"  {'group ' + str(int(b) + 1):>14} too thin")
            continue
        print(f"  {'group ' + str(int(b) + 1):>14} {g['stop_atr'].median():>12.2f} "
              f"{100 * f_.mean():>10.1f}% (n{len(f_):>3}) "
              f"{100 * s_.mean():>10.1f}% (n{len(s_):>3}) "
              f"{100 * (f_.mean() - s_.mean()):>+8.1f}")

    print("\n" + "=" * 88)
    print("4. WHAT ELSE SEPARATES A RUNNER THAT PAYS")
    print("=" * 88)
    cols = ["days_to_1r", "clear_air_atr", "clear_air_r", "year_high_r",
            "dist_high_252_atr", "stop_atr", "atr_pct", "corr_spy_60",
            "rr_at_fire", "atr_per_day_to_1r"]
    print(f"\n  {'number':>22} {'paid':>10} {'did not':>10} {'apart':>8}")
    res = []
    for c in cols:
        a_, b_ = open_[open_["runner_paid"] == 1][c].dropna(), \
                 open_[open_["runner_paid"] == 0][c].dropna()
        if len(a_) < 50 or len(b_) < 50:
            continue
        pooled = np.sqrt(((len(a_) - 1) * a_.var() + (len(b_) - 1) * b_.var())
                         / (len(a_) + len(b_) - 2))
        dd = (a_.mean() - b_.mean()) / pooled if pooled else 0
        res.append((abs(dd), c, a_.median(), b_.median(), dd))
    for _, c, ma, mb, dd in sorted(res, reverse=True):
        tag = "  <-- real" if abs(dd) >= 0.20 else ("  <-- slight" if abs(dd) >= 0.10 else "")
        print(f"  {c:>22} {ma:>10.2f} {mb:>10.2f} {dd:>+8.2f}{tag}")

    print("\n" + "=" * 88)
    print("5. WHAT IT IS WORTH")
    print("=" * 88)
    print("  If the runner is only carried when target 1 arrives quickly, and")
    print("  closed out when it crawls. Measured on our own exit engine's R.")
    for name, m in (("all of them", open_["days_to_1r"] >= 0),
                    ("target 1 within 3 days", open_["days_to_1r"] <= 3),
                    ("target 1 within 7 days", open_["days_to_1r"] <= 7),
                    ("target 1 took over 15 days", open_["days_to_1r"] > 15)):
        g = open_[m]["r_actual"].dropna()
        print(f"    {name:>28} n={len(g):<5} mean {g.mean():+.3f}R  "
              f"total {g.sum():+.1f}R")


if __name__ == "__main__":
    main()
