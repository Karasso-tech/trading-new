"""Download 10 years of daily bars from Yahoo Finance for every ticker in
backtest/data/snpdata.json (plus the SPY/QQQ benchmarks) and store them as JSON.

Storage layout: one file per ticker under backtest/data/bars/, not one giant
blob -- ~503 tickers x ~2,500 sessions is well over a million rows, and a single
JSON file that size has to be parsed whole before a backtest can read one name.
Per-ticker files load lazily and can be re-fetched individually when one symbol
fails. backtest/data/bars/_manifest.json indexes them.

Bars are stored UNADJUSTED (raw open/high/low/close/volume) with Yahoo's
adjusted close alongside, so the split/dividend factor stays recoverable:
    adj_factor = adj_close / close
Nothing here adjusts prices for you -- that stays a decision for whatever reads
the data.

Usage:
  python backtest/fetch_bars.py                 # everything in snpdata.json
  python backtest/fetch_bars.py AAPL NVDA       # just these
  python backtest/fetch_bars.py --years 15
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
BARS = DATA / "bars"
UNIVERSE = DATA / "snpdata.json"
MANIFEST = BARS / "_manifest.json"

BENCHMARKS = ["SPY", "QQQ"]
BATCH = 40          # symbols per yfinance call
PAUSE = 1.0         # seconds between batches -- Yahoo is a free service
RETRIES = 2


def yahoo_symbol(ticker: str) -> str:
    """BRK.B -> BRK-B. Yahoo uses a dash where the index list uses a dot."""
    return ticker.replace(".", "-")


def load_universe() -> list[str]:
    data = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    return list(data["tickers"])


def _rows_from_frame(frame: pd.DataFrame) -> list[dict]:
    rows = []
    for stamp, r in frame.iterrows():
        close = r.get("Close")
        adj = r.get("Adj Close", close)
        if close is None or (isinstance(close, float) and math.isnan(close)):
            continue
        rows.append({
            "date": pd.Timestamp(stamp).date().isoformat(),
            "open": round(float(r["Open"]), 6),
            "high": round(float(r["High"]), 6),
            "low": round(float(r["Low"]), 6),
            "close": round(float(close), 6),
            "adj_close": round(float(adj), 6) if adj == adj else None,
            "volume": int(r["Volume"]) if r["Volume"] == r["Volume"] else 0,
        })
    return rows


def _split_frame(raw: pd.DataFrame, symbols: list[str]) -> dict:
    """yfinance returns a single-level frame for one symbol and a MultiIndex
    (symbol, field) frame for many. Normalize both to {symbol: DataFrame}."""
    if isinstance(raw.columns, pd.MultiIndex):
        out = {}
        for sym in symbols:
            if sym in raw.columns.get_level_values(0):
                out[sym] = raw[sym].dropna(how="all")
        return out
    return {symbols[0]: raw.dropna(how="all")}


def fetch_batch(symbols: list[str], start: str, end: str) -> dict:
    raw = yf.download(symbols, start=start, end=end, auto_adjust=False,
                      actions=False, group_by="ticker", progress=False,
                      threads=True)
    if raw is None or raw.empty:
        return {}
    return _split_frame(raw, symbols)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*")
    parser.add_argument("--years", type=float, default=10)
    args = parser.parse_args()

    end = date.today() + timedelta(days=1)          # yfinance end is exclusive
    start = date.today() - timedelta(days=int(args.years * 365.25) + 1)

    tickers = [t.upper() for t in args.tickers] or (load_universe() + BENCHMARKS)
    BARS.mkdir(parents=True, exist_ok=True)
    print(f"{len(tickers)} symbols, {start} -> {date.today()}")

    sym_of = {yahoo_symbol(t): t for t in tickers}
    written, empty, failed = {}, [], []

    batches = [tickers[i:i + BATCH] for i in range(0, len(tickers), BATCH)]
    for n, group in enumerate(batches, 1):
        ysyms = [yahoo_symbol(t) for t in group]
        frames = {}
        for attempt in range(1, RETRIES + 1):
            try:
                frames = fetch_batch(ysyms, start.isoformat(), end.isoformat())
                break
            except Exception as exc:                # network/rate-limit
                print(f"  batch {n} attempt {attempt} failed: {str(exc)[:80]}")
                time.sleep(3.0 * attempt)

        for ysym in ysyms:
            ticker = sym_of[ysym]
            frame = frames.get(ysym)
            if frame is None or frame.empty:
                failed.append(ticker)
                continue
            rows = _rows_from_frame(frame)
            if not rows:
                empty.append(ticker)
                continue
            payload = {
                "ticker": ticker,
                "yahoo_symbol": ysym,
                "source": "Yahoo Finance (yfinance)",
                "adjusted": False,
                "note": "raw OHLCV; adj_factor = adj_close / close",
                "fetched_on": date.today().isoformat(),
                "first_date": rows[0]["date"],
                "last_date": rows[-1]["date"],
                "count": len(rows),
                "bars": rows,
            }
            (BARS / f"{ticker}.json").write_text(
                json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            written[ticker] = {"count": len(rows),
                               "first_date": rows[0]["date"],
                               "last_date": rows[-1]["date"]}
        print(f"  [{n}/{len(batches)}] {len(written)} written, "
              f"{len(failed)} failed, {len(empty)} empty")
        time.sleep(PAUSE)

    manifest = {
        "source": "Yahoo Finance (yfinance)",
        "fetched_on": date.today().isoformat(),
        "requested_start": start.isoformat(),
        "requested_end": date.today().isoformat(),
        "adjusted": False,
        "universe_file": "snpdata.json",
        "benchmarks": BENCHMARKS,
        "written": len(written),
        "failed": sorted(failed),
        "empty": sorted(empty),
        "tickers": dict(sorted(written.items())),
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    total = sum(v["count"] for v in written.values())
    print(f"\n{len(written)} files, {total:,} bars -> {BARS}")
    if failed:
        print(f"no data returned for {len(failed)}: {', '.join(sorted(failed)[:30])}")
    print("Reminder: this is TODAY's index membership. Names that left the "
          "index are missing, so any test over the full window is flattered.")


if __name__ == "__main__":
    main()
