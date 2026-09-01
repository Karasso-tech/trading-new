"""Turn every entry the system ever named into a row of numbers and an
exit-independent answer.

READ-ONLY over the bar files. Writes one dataset. Changes no live code.

WHY THIS EXISTS, and it is the whole argument:

Every measurement so far asked "which number predicts R". R is not a property
of the entry. R is what the EXIT ENGINE extracted from the entry -- the same
entry with a wider stop is a different R, and 74% of rows sit on exactly -1.00
because that is where the stop was, not where the stock went. Predicting R
means predicting our own stop placement, which is our own choice. That is a
plausible reason nothing separated: the signal was being measured through a
lens that flattens it.

So the answer here is the PRICE PATH after entry, measured in ATR, over a fixed
number of days, with no stop and no target involved at all:

    how far up it went, how far down it went, where it ended

An entry is good if the stock tends to go up more than down after it. That is a
question about the stock. What we do with that move is a separate question and
belongs to a separate study.

Two things guard against fooling ourselves:

* Every feature is computed from bars up to and including the FIRE day. Entry
  is the next open, exactly as the study does it. Nothing after the entry
  touches a feature.
* Every horizon has a market-adjusted twin. A "signal" that only says "the
  market rose" is strong in the raw column and gone in the adjusted one.

    python research/build_dataset.py
"""

from __future__ import annotations

import json
import math
import pathlib
import statistics

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
BARS = ROOT / "backtest" / "data" / "bars"
SRC = ROOT / "backtest" / "signals_all_5y.json"
OUT = HERE / "dataset.json"

HORIZONS = (5, 10, 20, 40)


# --------------------------------------------------------------- small helpers
def sma(xs, i, n):
    if i + 1 < n or i < 0:
        return None
    return sum(xs[i - n + 1:i + 1]) / n


def log_ret(xs, i, n):
    if i - n < 0 or xs[i - n] <= 0 or xs[i] <= 0:
        return None
    return math.log(xs[i] / xs[i - n]) * 100


def realized_vol(closes, i, n):
    """Standard deviation of daily log returns, annualised, in percent."""
    if i - n < 1:
        return None
    rs = [math.log(closes[k] / closes[k - 1])
          for k in range(i - n + 1, i + 1)
          if closes[k - 1] > 0 and closes[k] > 0]
    if len(rs) < 5:
        return None
    return statistics.pstdev(rs) * math.sqrt(252) * 100


def true_ranges(highs, lows, closes):
    tr = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))
    return tr


def wilder_atr(tr, n=14):
    """The same recursion the live indicator uses, so units match everywhere."""
    out = [None] * len(tr)
    if len(tr) < n:
        return out
    cur = sum(tr[:n]) / n
    out[n - 1] = cur
    for i in range(n, len(tr)):
        cur = (cur * (n - 1) + tr[i]) / n
        out[i] = cur
    return out


def percentile_rank(xs, i, n, value):
    """Where today's value sits inside its own past year, 0..100.

    Absolute volatility partly just names the sector. This asks the different
    and more useful question: is this stock unusually volatile FOR ITSELF."""
    if value is None or i + 1 < n:
        return None
    window = [x for x in xs[i - n + 1:i + 1] if x is not None]
    if len(window) < n // 2:
        return None
    return 100.0 * sum(1 for x in window if x <= value) / len(window)


# ------------------------------------------------------------------ bar loading
def load(ticker):
    p = BARS / f"{ticker}.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    return d["bars"] if isinstance(d, dict) else d


class Series:
    """Everything derived from one ticker's bars, computed once."""

    def __init__(self, bars):
        self.bars = bars
        self.date_ix = {b["date"]: i for i, b in enumerate(bars)}
        self.close = [b["close"] for b in bars]
        self.high = [b["high"] for b in bars]
        self.low = [b["low"] for b in bars]
        self.open = [b["open"] for b in bars]
        self.vol = [b.get("volume") or 0 for b in bars]
        self.tr = true_ranges(self.high, self.low, self.close)
        self.atr14 = wilder_atr(self.tr, 14)
        self.atr50 = wilder_atr(self.tr, 50)
        self.atr_pct = [(a / c * 100) if (a and c) else None
                        for a, c in zip(self.atr14, self.close)]


def beta_corr(s: Series, i: int, mkt: Series, n: int = 60):
    """Beta and correlation against the index, aligned BY DATE.

    Aligning by array index instead would silently pair different days
    whenever the two files disagree by even one bar, and every beta in the
    dataset would be quietly wrong."""
    if i - n < 1:
        return None, None
    ra, rb = [], []
    for k in range(i - n + 1, i + 1):
        j = mkt.date_ix.get(s.bars[k]["date"])
        jp = mkt.date_ix.get(s.bars[k - 1]["date"])
        if j is None or jp is None:
            continue
        if s.close[k - 1] > 0 and s.close[k] > 0 and mkt.close[jp] > 0 and mkt.close[j] > 0:
            ra.append(math.log(s.close[k] / s.close[k - 1]))
            rb.append(math.log(mkt.close[j] / mkt.close[jp]))
    if len(ra) < n // 2:
        return None, None
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / len(ra)
    va = sum((x - ma) ** 2 for x in ra) / len(ra)
    vb = sum((y - mb) ** 2 for y in rb) / len(rb)
    if vb <= 0 or va <= 0:
        return None, None
    return cov / vb, cov / math.sqrt(va * vb)


def features(s: Series, i: int, spy: Series, qqq: Series, si: int, qi: int) -> dict:
    """Everything knowable at the close of the fire day. Nothing after it."""
    c, atr = s.close[i], s.atr14[i]
    f = {}
    if not atr or not c:
        return f

    # --- how far it has already run, over several lengths of time
    for n in (1, 5, 10, 21, 63, 126, 252):
        f[f"ret_{n}"] = log_ret(s.close, i, n)

    # --- where price sits against its own averages, in ATR so a $30 stock and
    #     a $600 stock are on one scale
    for n in (20, 50, 200):
        m = sma(s.close, i, n)
        f[f"dist_sma{n}_atr"] = (c - m) / atr if m else None
    m20 = sma(s.close, i, 20)
    m20_prev = sma(s.close, i - 20, 20)
    f["sma20_slope_atr"] = ((m20 - m20_prev) / atr) if (m20 and m20_prev) else None
    m50, m200 = sma(s.close, i, 50), sma(s.close, i, 200)
    f["sma50_over_sma200_atr"] = ((m50 - m200) / atr) if (m50 and m200) else None

    # --- position inside the year's range
    if i >= 252:
        hi52, lo52 = max(s.high[i - 251:i + 1]), min(s.low[i - 251:i + 1])
        f["dist_52w_high_atr"] = (c - hi52) / atr
        f["dist_52w_low_atr"] = (c - lo52) / atr
        f["pct_of_52w_range"] = 100 * (c - lo52) / (hi52 - lo52) if hi52 > lo52 else None
    # how long the trend has held: days since price last closed under its SMA50
    age = 0
    for k in range(i, max(i - 252, 199), -1):
        mk = sma(s.close, k, 50)
        if mk is None or s.close[k] < mk:
            break
        age += 1
    f["days_above_sma50"] = age

    # --- volatility, absolute and relative to the stock's own past
    f["atr_pct"] = s.atr_pct[i]
    f["atr_pct_rank_252"] = percentile_rank(s.atr_pct, i, 252, s.atr_pct[i])
    f["atr14_over_atr50"] = (s.atr14[i] / s.atr50[i]) if s.atr50[i] else None
    rv20, rv60 = realized_vol(s.close, i, 20), realized_vol(s.close, i, 60)
    f["realized_vol_20"] = rv20
    f["vol_expansion"] = (rv20 / rv60) if (rv20 and rv60) else None
    rng = s.high[i] - s.low[i]
    f["close_loc_in_range"] = ((c - s.low[i]) / rng * 100) if rng > 0 else None
    f["gap_open_atr"] = ((s.open[i] - s.close[i - 1]) / atr) if i > 0 else None

    # --- how much money actually trades in it
    v50 = sma(s.vol, i, 50)
    f["log_dollar_vol_50"] = math.log10(v50 * c) if (v50 and v50 > 0 and c > 0) else None
    f["vol_ratio_1_50"] = (s.vol[i] / v50) if v50 else None
    v5 = sma(s.vol, i, 5)
    f["vol_ratio_5_50"] = (v5 / v50) if (v5 and v50) else None
    if i >= 20:
        signed = sum((1 if s.close[k] > s.close[k - 1] else -1) * s.vol[k]
                     for k in range(i - 19, i + 1))
        total = sum(s.vol[k] for k in range(i - 19, i + 1))
        f["volume_pressure_20"] = (signed / total * 100) if total else None
    f["up_days_20"] = sum(1 for k in range(max(i - 19, 1), i + 1)
                          if s.close[k] > s.close[k - 1])

    # --- the same moves with the market's move taken out
    for n, name in ((5, "rs5"), (21, "rs21"), (63, "rs63"), (126, "rs126")):
        a, b = log_ret(s.close, i, n), log_ret(spy.close, si, n)
        f[name] = (a - b) if (a is not None and b is not None) else None
    f["beta_60"], f["corr_spy_60"] = beta_corr(s, i, spy, 60)

    # --- what the market itself was doing that day. Identical for every ticker
    #     on a given date, kept so the model can learn "only in this weather".
    if spy.atr14[si]:
        sm200 = sma(spy.close, si, 200)
        f["spy_dist_sma200_atr"] = ((spy.close[si] - sm200) / spy.atr14[si]) if sm200 else None
        f["spy_ret_21"] = log_ret(spy.close, si, 21)
        f["spy_realized_vol_20"] = realized_vol(spy.close, si, 20)
        qr, sr = log_ret(qqq.close, qi, 21), log_ret(spy.close, si, 21)
        f["qqq_minus_spy_21"] = (qr - sr) if (qr is not None and sr is not None) else None
    return f


def targets(s: Series, entry_i: int, entry_px: float, atr: float,
            spy: Series, spy_entry_i: int, beta) -> dict:
    """What the price did afterwards. No stop, no target, no exit rule.

    In ATR so every stock is on one scale, and every horizon also reported net
    of the market's own move -- because a "signal" that is really just market
    direction has to be visible as such."""
    t = {}
    for h in HORIZONS:
        end = entry_i + h
        if end >= len(s.close) or spy_entry_i + h >= len(spy.close):
            continue
        t[f"mfe_atr_{h}"] = (max(s.high[entry_i:end + 1]) - entry_px) / atr
        t[f"mae_atr_{h}"] = (min(s.low[entry_i:end + 1]) - entry_px) / atr
        t[f"ret_atr_{h}"] = (s.close[end] - entry_px) / atr
        spy_move = ((spy.close[spy_entry_i + h] - spy.close[spy_entry_i])
                    / spy.close[spy_entry_i])
        if beta is not None:
            t[f"exret_atr_{h}"] = t[f"ret_atr_{h}"] - beta * spy_move * entry_px / atr
        # Tharp's entry quality: room given against heat suffered. The floor
        # stops a trade that never dipped from being infinite.
        t[f"edge_ratio_{h}"] = t[f"mfe_atr_{h}"] / max(abs(t[f"mae_atr_{h}"]), 0.25)
    return t


def main():
    rows = json.loads(SRC.read_text(encoding="utf-8"))["rows"]
    fired = [r for r in rows if r.get("fired") and r.get("fired_date")]
    print(f"{len(fired)} fired entries in the master table")

    spy, qqq = Series(load("SPY")), Series(load("QQQ"))

    by_ticker = {}
    for r in fired:
        by_ticker.setdefault(r["ticker"], []).append(r)

    out, skipped = [], {}
    def drop(why):
        skipped[why] = skipped.get(why, 0) + 1

    for n, (ticker, group) in enumerate(sorted(by_ticker.items()), 1):
        bars = load(ticker)
        if not bars:
            for _ in group:
                drop("no bars")
            continue
        s = Series(bars)
        if n % 100 == 0:
            print(f"  {n}/{len(by_ticker)} tickers, {len(out)} rows so far")
        for r in group:
            i = s.date_ix.get(r["fired_date"])
            si, qi = spy.date_ix.get(r["fired_date"]), qqq.date_ix.get(r["fired_date"])
            if i is None or si is None or qi is None or i + 1 >= len(s.close):
                drop("date missing")
                continue
            atr = s.atr14[i]
            if not atr:
                drop("no atr")
                continue
            f = features(s, i, spy, qqq, si, qi)
            if not f:
                drop("no features")
                continue
            entry_px = s.open[i + 1]
            t = targets(s, i + 1, entry_px, atr, spy, si + 1, f.get("beta_60"))
            if f"ret_atr_{HORIZONS[-1]}" not in t:
                # not enough bars after the entry for the longest horizon. Kept
                # out entirely so every row answers exactly the same questions.
                drop("too near the end of the data")
                continue
            rec = {
                "ticker": ticker, "fired_date": r["fired_date"], "built": r.get("built"),
                "setup": r.get("setup"), "regime_at_fire": r.get("regime_at_fire"),
                "grade_at_fire": r.get("grade_at_fire"),
                "rubric_score": r.get("rubric_score"),
                "rr_at_fire": r.get("rr_at_fire"),
                "entry_gap_pct": r.get("entry_gap_pct"),
                "days_to_fire": r.get("days_to_fire"),
                "n_targets": r.get("targets"),
                "stop_basis": r.get("stop_basis"),
                "earnings_days_out": r.get("earnings_days_out"),
                "stop_atr": (r["risk_per_share"] / r["atr_at_build"])
                            if r.get("risk_per_share") and r.get("atr_at_build") else None,
                "r_actual": r.get("r"),
                "entry_px": entry_px,
            }
            rec.update(f)
            rec.update(t)
            out.append(rec)

    print(f"\n{len(out)} rows built")
    for k, v in sorted(skipped.items()):
        print(f"  dropped, {k}: {v}")
    OUT.write_text(json.dumps({"rows": out, "horizons": list(HORIZONS)}),
                   encoding="utf-8")
    print(f"written to {OUT}")


if __name__ == "__main__":
    main()
