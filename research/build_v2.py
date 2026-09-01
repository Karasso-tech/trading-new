"""Build the full table: our entries and the whole market, all features, all answers.

READ-ONLY over bar files. Writes two tables. Touches no live code.

THE HOLD-OUT, set here and honoured everywhere downstream:

    everything from 2025-06-09 onward is sealed.

Nothing is searched on it, no threshold is chosen on it, no model sees it. It
exists so that at the end there is one honest test left to run. Every earlier
pass in this project failed because the thing that looked good had already
touched every row it was later judged on. A sealed year is the only cure, and
it only works if it is set before the searching starts, which is why it is set
here rather than in the file that does the searching.

Two tables come out:

  entries_v2   -- the 9,195 entries the system actually produced
  universe_v2  -- every 5th trading day on every company, no entry rule at all

The second exists because the first cannot answer its own question. If the
system's rules already select one narrow kind of moment, then finding no spread
among the survivors says nothing about whether a spread exists. Only the
unfiltered market can separate "there is no signal" from "our filter already
spent it".

Both carry the same answers: from the next open, with the plan's own stop
distance where there is one and a 2-ATR stop where there is not, does price
reach +1R, +2R or +3R before -1R.

    python research/build_v2.py
"""

from __future__ import annotations

import json
import pathlib

import numpy as np

import build_dataset as bd
import features_v2 as fv

HERE = pathlib.Path(__file__).resolve().parent
SEALED_FROM = "2025-06-09"
WINDOW = 40
RUNGS = (1.0, 2.0, 3.0)
DEFAULT_STOP_ATR = 2.0     # for universe rows, which have no plan


class Arrays:
    """One ticker's bars as numpy, computed once."""

    def __init__(self, bars):
        self.bars = bars
        self.dates = [b["date"] for b in bars]
        self.date_ix = {d: i for i, d in enumerate(self.dates)}
        self.close = np.array([b["close"] for b in bars], float)
        self.high = np.array([b["high"] for b in bars], float)
        self.low = np.array([b["low"] for b in bars], float)
        self.open = np.array([b["open"] for b in bars], float)
        self.vol = np.array([b.get("volume") or 0 for b in bars], float)
        self.atr14 = np.array([x if x else np.nan for x in
                               bd.wilder_atr(bd.true_ranges(
                                   list(self.high), list(self.low), list(self.close)),
                                   14)], float)


def how_far(a: Arrays, entry_i, entry_px, risk):
    """The furthest the trade ever got, in R, before the stop took it out.

    Needed for the question that has a control group inside the winners: given
    an entry DID reach its first target, what separates the ones that kept
    going from the ones that stalled."""
    stop, best, bars_to_1r = entry_px - risk, -9.9, None
    for k in range(entry_i, min(entry_i + WINDOW + 1, len(a.close))):
        best = max(best, (a.high[k] - entry_px) / risk)
        if bars_to_1r is None and (a.high[k] - entry_px) / risk >= 1.0:
            bars_to_1r = k - entry_i
        if a.low[k] <= stop:
            break
    return best, bars_to_1r


def barriers(a: Arrays, entry_i, entry_px, risk):
    """Which line is touched first, at each rung. A bar touching both counts
    as the stop -- assuming otherwise would invent wins."""
    stop = entry_px - risk
    out = {r: None for r in RUNGS}
    for k in range(entry_i, min(entry_i + WINDOW + 1, len(a.close))):
        hit_stop = a.low[k] <= stop
        for r in RUNGS:
            if out[r] is None:
                if hit_stop:
                    out[r] = 0
                elif a.high[k] >= entry_px + r * risk:
                    out[r] = 1
        if hit_stop:
            break
    return out


def one_row(s, a, i, spy, qqq, spy_arr, si, qi, risk, entry_px, trigger):
    """The original 45 numbers plus the new ones. `s` is the Series the first
    pass used, `a` the numpy arrays the new features need -- same bars, two
    shapes, kept apart so neither file has to know about the other."""
    f = bd.features(s, i, spy, qqq, si, qi)
    if not f:
        return None
    f.update(fv.all_of_it(a.bars, i, a.atr14[i], a.close, a.high, a.low, a.vol,
                          a.dates, spy_arr.close, spy_arr.date_ix,
                          risk=risk, entry=entry_px, trigger=trigger))
    return f


def main():
    spy, qqq = bd.Series(bd.load("SPY")), bd.Series(bd.load("QQQ"))
    spy_arr = Arrays(bd.load("SPY"))

    # ---------------------------------------------------------- our entries
    master = json.loads((HERE.parent / "backtest" / "signals_all_5y.json")
                        .read_text(encoding="utf-8"))["rows"]
    fired = [r for r in master if r.get("fired") and r.get("fired_date")]
    by_t = {}
    for r in fired:
        by_t.setdefault(r["ticker"], []).append(r)

    print(f"our entries: {len(fired)} across {len(by_t)} companies")
    rows, dropped = [], 0
    for n, (t, group) in enumerate(sorted(by_t.items()), 1):
        raw = bd.load(t)
        if not raw:
            continue
        a = Arrays(raw)
        s = bd.Series(raw)
        if n % 100 == 0:
            print(f"  {n}/{len(by_t)}, {len(rows)} rows")
        for r in group:
            i = a.date_ix.get(r["fired_date"])
            si, qi = spy.date_ix.get(r["fired_date"]), qqq.date_ix.get(r["fired_date"])
            risk = r.get("risk_per_share")
            if (i is None or si is None or qi is None or i + 1 >= len(a.close)
                    or not risk or risk <= 0 or np.isnan(a.atr14[i]) or i < 260):
                dropped += 1
                continue
            entry_px = a.open[i + 1]
            f = one_row(s, a, i, spy, qqq, spy_arr, si, qi, risk, entry_px,
                        r.get("trigger"))
            if f is None:
                dropped += 1
                continue
            b = barriers(a, i + 1, entry_px, risk)
            best_r, bars_1r = how_far(a, i + 1, entry_px, risk)
            rec = {"best_r": best_r, "bars_to_1r": bars_1r,
                   "ticker": t, "fired_date": r["fired_date"], "setup": r.get("setup"),
                   "regime_at_fire": r.get("regime_at_fire"),
                   "grade_at_fire": r.get("grade_at_fire"),
                   "stop_basis": r.get("stop_basis"),
                   "rubric_score": r.get("rubric_score"), "rr_at_fire": r.get("rr_at_fire"),
                   "days_to_fire": r.get("days_to_fire"), "n_targets": r.get("targets"),
                   "entry_gap_pct": r.get("entry_gap_pct"), "r_actual": r.get("r"),
                   "stop_atr": risk / r["atr_at_build"] if r.get("atr_at_build") else None,
                   "sealed": r["fired_date"] >= SEALED_FROM}
            rec.update(f)
            for rung, v in b.items():
                rec[f"win_{rung:g}r"] = v
            rows.append(rec)
    print(f"  {len(rows)} rows, {dropped} dropped")
    (HERE / "entries_v2.json").write_text(
        json.dumps({"rows": rows, "sealed_from": SEALED_FROM}), encoding="utf-8")
    print(f"  written to entries_v2.json")

    # ------------------------------------------------------------- universe
    tickers = sorted(p.stem for p in bd.BARS.glob("*.json")
                     if not p.stem.startswith("_"))
    print(f"\nuniverse: every 5th day on {len(tickers)} companies")
    urows = []
    for n, t in enumerate(tickers, 1):
        raw = bd.load(t)
        if not raw or len(raw) < 500:
            continue
        a = Arrays(raw)
        s = bd.Series(raw)
        if n % 50 == 0:
            print(f"  {n}/{len(tickers)}, {len(urows)} rows")
        for i in range(300, len(raw) - WINDOW - 2, 5):
            d = a.dates[i]
            if d < "2021-08-01":
                continue
            si, qi = spy.date_ix.get(d), qqq.date_ix.get(d)
            if si is None or qi is None or np.isnan(a.atr14[i]):
                continue
            entry_px = a.open[i + 1]
            # no plan here, so a plain 2-ATR stop stands in. Stated rather than
            # hidden: it makes universe rungs comparable to each other, not to
            # our entries' rungs, whose stops came from real structure.
            risk = DEFAULT_STOP_ATR * a.atr14[i]
            f = one_row(s, a, i, spy, qqq, spy_arr, si, qi, risk, entry_px, None)
            if f is None:
                continue
            b = barriers(a, i + 1, entry_px, risk)
            best_r, bars_1r = how_far(a, i + 1, entry_px, risk)
            rec = {"ticker": t, "fired_date": d, "sealed": d >= SEALED_FROM,
                   "best_r": best_r, "bars_to_1r": bars_1r}
            rec.update(f)
            for rung, v in b.items():
                rec[f"win_{rung:g}r"] = v
            urows.append(rec)
    print(f"  {len(urows)} rows")
    import pandas as pd
    pd.DataFrame(urows).to_parquet(HERE / "universe_v2.parquet")
    print("  written to universe_v2.parquet")

    open_rows = sum(1 for r in rows if not r["sealed"])
    print(f"\nSEALED FROM {SEALED_FROM}")
    print(f"  entries  : {open_rows} open for searching, "
          f"{len(rows) - open_rows} sealed")
    su = sum(1 for r in urows if not r["sealed"])
    print(f"  universe : {su} open for searching, {len(urows) - su} sealed")


if __name__ == "__main__":
    main()
