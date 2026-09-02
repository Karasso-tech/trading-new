"""What was knowable at the moment of entry, for every trade in the book.

Entry happens AT the close of the firing day, so a feature may use bars up to
and INCLUDING that close -- you know today's close when you buy at it. Nothing
may use the next bar. Every accessor here is called with `end = i + 1`, where
`i` is the entry bar, and that is the single line to check if look-ahead is ever
suspected.

Grouped the way the owner listed them:

    setup type · trend state · strength against the index · volume ·
    distance from the moving averages · volatility · distance to the stop and
    the target · market state · gaps and chart structure

Two of his eleven are NOT here and their absence is deliberate rather than
forgotten:

    earnings       -- no earnings calendar exists in stage 0. The exact dates are
                      obtainable free from SEC 8-K item 2.02 filings, but the
                      owner's call on 2026-09-01 was to leave it out until we
                      know how it should be treated at all.

Sector WAS missing and is now in, via sectors.py: the label comes from Wikipedia
for today's members and from the SEC's SIC code for the names that left, and the
strength of a sector on a day is read off that sector's SPDR ETF. Both limits of
the label are recorded in sectors.py and are worth restating: it is today's
sector applied to an older trade, and SIC is a translation of GICS rather than
the same thing.

They are listed in MISSING below so that any model report can say out loud which
of the requested inputs it did not have.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Optional

import indicators as ind
from context import Context
from barfile import bar_path
from params import DEFAULT, Params

ROOT = Path(__file__).resolve().parent
BARS = ROOT / "data" / "bars"
DB = ROOT / "data" / "stage0.db"

MISSING = ("earnings proximity -- no earnings calendar in stage 0, "
           "left out on purpose until we know how to treat it",)

# Every numeric feature, in one list, so the table, the insert and the model all
# read the same names from the same place.
NUMERIC = [
    # the plan itself
    "stop_distance_atr", "planned_rr", "target_1_atr",
    # what the entry did to the plan
    "entry_premium_atr", "actual_rr", "risk_atr",
    # trend state, and distance from the averages
    "close_over_sma20_atr", "close_over_sma50_atr", "close_over_sma200_atr",
    "sma20_over_sma50_atr", "sma50_slope_20d_atr", "range_position_252",
    # strength against the index
    "rs_5d", "rs_20d", "rs_60d",
    # volume
    "volume_vs_20d", "volume_20d_vs_60d",
    # volatility
    "atr_pct", "atr_vs_60d_ago", "spy_atr_pct",
    # gaps and structure
    "walls_above_within_5atr", "wall_above_target_atr", "days_since_gap",
    "days_waiting", "up_closes_before_entry",
    # market state
    "spy_above_sma200", "spy_return_20d_pct", "spy_return_60d_pct",
    "spy_over_sma200_pct",
    # sector state, read off the sector's own ETF
    "sector_return_20d", "sector_return_60d", "sector_vs_spy_20d",
    "sector_over_sma200_pct", "stock_vs_sector_5d", "stock_vs_sector_20d",
]
CATEGORICAL = ["setup_type", "confidence", "stop_basis_kind", "target_1_source", "sector"]

SCHEMA = f"""
-- One row per trade, across every run, so two universes can be compared side by
-- side. Rebuilding used to DROP the whole table, which quietly destroyed the
-- other run's rows; now only the run being rebuilt is cleared. Pass --fresh to
-- drop everything, which is what a change to the feature list needs.
CREATE TABLE IF NOT EXISTS features (
    trade_id   INTEGER PRIMARY KEY,
    ticker     TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    label      INTEGER,            -- 1 = reached target 1 first, 0 = did not, NULL = still open
    r_multiple REAL,
    {', '.join(f'{c} TEXT' for c in CATEGORICAL)},
    {', '.join(f'{c} REAL' for c in NUMERIC)}
);
CREATE INDEX IF NOT EXISTS features_date ON features(entry_date);
"""


def _ret(closes, i: int, back: int) -> Optional[float]:
    if i - back < 0 or not closes[i - back]:
        return None
    return (closes[i] - closes[i - back]) / closes[i - back] * 100


def _mean(values) -> Optional[float]:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def build_row(ctx: Context, i: int, trade: sqlite3.Row, spy: dict, sectors: dict,
              p: Params) -> dict:
    """One trade's feature vector, as of the close it was entered on."""
    bars = ctx.bars
    end = i + 1                      # inclusive of the entry bar -- and never past it
    close = bars[i]["close"]
    atr = ctx.atr(end)
    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]

    sma20, sma50 = ctx.sma(end, 20), ctx.sma(end, 50)
    sma200 = ctx.sma(end, 200)
    sma50_then = ctx.sma(end - 20, 50)

    window252 = bars[max(0, i - 251):end]
    hi252 = max(b["high"] for b in window252)
    lo252 = min(b["low"] for b in window252)

    walls = ctx.walls(end, atr) if atr else []
    above = [w.top for w in walls if w.top > close]
    over_target = [t for t in above if t > trade["target_1"]]

    gaps = ctx.gap_edges(end, atr) if atr else []
    days_since_gap = None
    if gaps:
        last_gap_date = max(g.date for g in gaps)
        days_since_gap = sum(1 for b in bars[:end] if b["date"] > last_gap_date)

    up = 0
    for k in range(i, 0, -1):
        if closes[k] > closes[k - 1]:
            up += 1
        else:
            break

    spy_i = spy["index"].get(bars[i]["date"])
    spy_closes = spy["closes"]
    spy_atr_pct = spy_ret60 = spy_over_200 = None
    if spy_i is not None:
        if spy["atr"][spy_i] and spy_closes[spy_i]:
            spy_atr_pct = spy["atr"][spy_i] / spy_closes[spy_i] * 100
        spy_ret60 = _ret(spy_closes, spy_i, 60)
        if spy_i >= 200:
            spy_200 = sum(spy_closes[spy_i - 199:spy_i + 1]) / 200
            spy_over_200 = (spy_closes[spy_i] - spy_200) / spy_200 * 100

    # --- the sector this stock belongs to, and how that sector is doing ------
    sector = sectors["of"].get(trade["ticker"])
    sec_series = sectors["series"].get(sector)
    sec_ret20 = sec_ret60 = sec_vs_spy = sec_over_200 = None
    stock_vs_sec_5 = stock_vs_sec_20 = None
    if sec_series is not None:
        k = sec_series["index"].get(bars[i]["date"])
        if k is not None:
            sc = sec_series["closes"]
            sec_ret20, sec_ret60 = _ret(sc, k, 20), _ret(sc, k, 60)
            sec_vs_spy = _diff(sec_ret20, spy_ret(spy, spy_i, 20))
            stock_vs_sec_5 = _diff(_ret(closes, i, 5), _ret(sc, k, 5))
            stock_vs_sec_20 = _diff(_ret(closes, i, 20), sec_ret20)
            if k >= 200:
                sec_200 = sum(sc[k - 199:k + 1]) / 200
                sec_over_200 = (sc[k] - sec_200) / sec_200 * 100

    risk = trade["risk_per_share"]
    atr_60_ago = ctx.atr(end - 60)

    def gap_atr(value, level):
        return (value - level) / atr if (atr and value is not None and level is not None) else None

    return {
        "trade_id": trade["id"], "ticker": trade["ticker"],
        "entry_date": trade["entry_date"],
        "label": None if trade["outcome"] == "open" else int(trade["outcome"] == "success"),
        "r_multiple": trade["r_multiple"],

        "setup_type": trade["setup_type"], "sector": sector,
        "confidence": None, "stop_basis_kind": None, "target_1_source": None,  # filled by caller

        "stop_distance_atr": (trade["planned_risk"] / atr) if atr else None,
        "planned_rr": ((trade["target_1"] - trade["trigger"]) / trade["planned_risk"]
                       if trade["planned_risk"] else None),
        "target_1_atr": gap_atr(trade["target_1"], trade["trigger"]),

        "entry_premium_atr": trade["entry_premium_atr"],
        "actual_rr": ((trade["target_1"] - trade["entry_price"]) / risk) if risk else None,
        "risk_atr": (risk / atr) if atr else None,

        "close_over_sma20_atr": gap_atr(close, sma20),
        "close_over_sma50_atr": gap_atr(close, sma50),
        "close_over_sma200_atr": gap_atr(close, sma200),
        "sma20_over_sma50_atr": gap_atr(sma20, sma50),
        "sma50_slope_20d_atr": gap_atr(sma50, sma50_then),
        "range_position_252": ((close - lo252) / (hi252 - lo252)) if hi252 > lo252 else None,

        "rs_5d": _diff(_ret(closes, i, 5), spy_ret(spy, spy_i, 5)),
        "rs_20d": _diff(_ret(closes, i, 20), spy_ret(spy, spy_i, 20)),
        "rs_60d": _diff(_ret(closes, i, 60), spy_ret60),

        "volume_vs_20d": _ratio(volumes[i], _mean(volumes[max(0, i - 20):i])),
        "volume_20d_vs_60d": _ratio(_mean(volumes[max(0, i - 20):i]),
                                    _mean(volumes[max(0, i - 60):i])),

        "atr_pct": (atr / close * 100) if atr and close else None,
        "atr_vs_60d_ago": _ratio(atr, atr_60_ago),
        "spy_atr_pct": spy_atr_pct,

        "walls_above_within_5atr": (sum(1 for t in above if (t - close) / atr <= 5)
                                    if atr else None),
        "wall_above_target_atr": (gap_atr(min(over_target), trade["target_1"])
                                  if over_target else None),
        "days_since_gap": days_since_gap,
        "days_waiting": None,        # filled by caller from the idea row
        "up_closes_before_entry": up,

        "spy_above_sma200": trade["spy_above_sma200"],
        "spy_return_20d_pct": trade["spy_return_20d_pct"],
        "spy_return_60d_pct": spy_ret60,
        "spy_over_sma200_pct": spy_over_200,

        "sector_return_20d": sec_ret20,
        "sector_return_60d": sec_ret60,
        "sector_vs_spy_20d": sec_vs_spy,
        "sector_over_sma200_pct": sec_over_200,
        "stock_vs_sector_5d": stock_vs_sec_5,
        "stock_vs_sector_20d": stock_vs_sec_20,
    }


def _diff(a, b):
    return None if a is None or b is None else a - b


def _ratio(a, b):
    return None if a is None or not b else a / b


def spy_ret(spy: dict, i: Optional[int], back: int):
    return None if i is None else _ret(spy["closes"], i, back)


def load_index(ticker: str, p: Params) -> dict:
    """A benchmark series, keyed by date so a stock's bar can find the same day."""
    bars = json.loads(bar_path(BARS, ticker).read_text(encoding="utf-8"))["bars"]
    ctx = Context(bars, p)
    return {"closes": [b["close"] for b in bars],
            "index": {b["date"]: k for k, b in enumerate(bars)},
            "atr": [ctx.atr(k + 1) for k in range(len(bars))],
            "ctx": ctx}


def load_spy(p: Params) -> dict:
    return load_index("SPY", p)


def load_sectors(p: Params) -> dict:
    """The sector of every ticker, and one loaded ETF series per sector."""
    blob = json.loads((ROOT / "data" / "universe" / "sectors.json").read_text(encoding="utf-8"))
    series = {}
    for sector, etf in blob["sector_etf"].items():
        if bar_path(BARS, etf).exists():
            series[sector] = load_index(etf, p)
    return {"of": {t: v["sector"] for t, v in blob["tickers"].items()}, "series": series}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", type=int)
    ap.add_argument("--fresh", action="store_true",
                    help="drop the whole table first -- needed when the feature list changes")
    args = ap.parse_args()

    p = DEFAULT
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    run_id = args.run or conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
    if args.fresh:
        conn.execute("DROP TABLE IF EXISTS features")
    conn.executescript(SCHEMA)
    conn.execute("DELETE FROM features WHERE trade_id IN "
                 "(SELECT id FROM trades WHERE run_id = ?)", (run_id,))
    conn.commit()

    spy = load_spy(p)
    sectors = load_sectors(p)
    trades = list(conn.execute(
        "SELECT t.*, i.confidence, i.stop_basis_kind, i.target_1_source, i.days_to_fire"
        " FROM trades t JOIN ideas i ON i.id = t.idea_id WHERE t.run_id = ?"
        " ORDER BY t.ticker, t.entry_date", (run_id,)))
    print(f"run {run_id}: {len(trades):,} trades")

    columns = ["trade_id", "ticker", "entry_date", "label", "r_multiple"] + CATEGORICAL + NUMERIC
    written = skipped = 0
    ctx = None
    current = None
    for trade in trades:
        if trade["ticker"] != current:
            current = trade["ticker"]
            bars = json.loads(bar_path(BARS, current).read_text(encoding="utf-8"))["bars"]
            ctx = Context(bars, p)
            index = {b["date"]: k for k, b in enumerate(bars)}
        i = index.get(trade["entry_date"])
        if i is None:
            skipped += 1
            continue
        row = build_row(ctx, i, trade, spy, sectors, p)
        row["confidence"] = trade["confidence"]
        row["stop_basis_kind"] = trade["stop_basis_kind"]
        row["target_1_source"] = trade["target_1_source"]
        row["days_waiting"] = trade["days_to_fire"]
        conn.execute(f"INSERT OR REPLACE INTO features ({','.join(columns)})"
                     f" VALUES ({','.join('?' * len(columns))})",
                     [row.get(c) for c in columns])
        written += 1
        if written % 2000 == 0:
            conn.commit()
            print(f"  {written:,}/{len(trades):,}", flush=True)
    conn.commit()

    print(f"\nwrote {written:,} feature rows, skipped {skipped}")
    print(f"{len(NUMERIC)} numeric + {len(CATEGORICAL)} categorical features")
    print("\nrequested inputs NOT available in stage 0:")
    for m in MISSING:
        print(f"  - {m}")

    print("\ncoverage (how often each feature is actually present):")
    total = conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    for col in NUMERIC:
        n = conn.execute(f"SELECT COUNT({col}) FROM features").fetchone()[0]
        if n < total:
            print(f"  {col:26s} {n:>7,} of {total:,}  ({100*n//total}%)")
    conn.close()


if __name__ == "__main__":
    main()
