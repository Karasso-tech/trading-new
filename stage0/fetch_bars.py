"""Download daily bars from Yahoo for every ticker the index ever held, and
throw away the ones whose data cannot be trusted.

Two ways Yahoo lets us down, and both get dropped:

  1. NOTHING. Yahoo deletes a lot of companies once they stop trading. Twitter,
     Monsanto, Celgene, Silicon Valley Bank -- gone. Nothing to do about it.

  2. SOMEBODY ELSE. A ticker freed up by a dead company gets handed to a new
     listing years later, and Yahoo serves the new company's prices under the
     old symbol. That is worse than missing, because it looks fine. The test:
     the bars have to actually exist during the window when THIS company was in
     the index. Pepco left the index in March 2016; the "POM" bars Yahoo returns
     start in October 2025, so they belong to someone else and get dropped.

Bars are stored UNADJUSTED (raw open/high/low/close/volume) with Yahoo's
adjusted close alongside, so splits stay recoverable:  factor = adj_close/close
Nothing here adjusts anything -- that is a decision for whatever reads the data.

Layout: one JSON per ticker under data/bars/, indexed by data/bars/_manifest.json.
Everything dropped is listed with its reason in data/bars/_dropped.json, so the
hole in the universe is written down rather than silently absent.

Usage:
  python stage0/fetch_bars.py               # the whole universe
  python stage0/fetch_bars.py AAPL SIVB     # just these
"""

from __future__ import annotations

import argparse
import json
import time
import warnings
from pathlib import Path

import pandas as pd
import yfinance as yf

from barfile import bar_path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent
UNIVERSE = ROOT / "data" / "universe" / "intervals.json"
UNIVERSE_EXTRA = ROOT / "data" / "universe" / "intervals_mid_small.json"
BARS = ROOT / "data" / "bars"
BENCHMARKS = ["SPY", "QQQ", "IWM"]

# A spell shorter than this many bars is too short to check honestly, so the
# window test is skipped for it and the ticker is kept on the strength of
# having any data at all in roughly the right era.
MIN_BARS_IN_WINDOW = 40
FIELDS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


def spells_by_ticker() -> dict[str, list[tuple[str, str]]]:
    """Membership windows from every universe file that exists.

    The mid- and small-cap file is merged in when present, so the wrong-company
    check works for those tickers too. A ticker in more than one index simply
    gets both spells, and covers_its_spells passes on the best of them.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for path in (UNIVERSE, UNIVERSE_EXTRA):
        if not path.exists():
            continue
        for s in json.loads(path.read_text(encoding="utf-8"))["spells"]:
            out.setdefault(s["ticker"], []).append((s["start"], s["end"]))
    return out


def download(ticker: str, start: str, end: str, tries: int = 3) -> pd.DataFrame | None:
    for attempt in range(tries):
        try:
            df = yf.download(ticker, start=start, end=end, progress=False,
                             auto_adjust=False, threads=False, actions=False)
        except Exception:
            df = None
        if df is not None and len(df):
            if isinstance(df.columns, pd.MultiIndex):      # single-ticker MultiIndex
                df.columns = df.columns.get_level_values(0)
            return df
        time.sleep(1.5 * (attempt + 1))
    return None


def covers_its_spells(df: pd.DataFrame, spells: list[tuple[str, str]],
                      floor: str) -> tuple[bool, int]:
    """Did this data actually trade while the company was in the index?"""
    best = 0
    for start, end in spells:
        lo = max(pd.Timestamp(start), pd.Timestamp(floor))
        hi = pd.Timestamp(end)
        if hi <= lo:
            continue
        best = max(best, len(df.loc[lo:hi]))
    span = max((pd.Timestamp(e) - max(pd.Timestamp(s), pd.Timestamp(floor))).days
               for s, e in spells)
    need = min(MIN_BARS_IN_WINDOW, max(1, int(span * 0.5)))
    return best >= need, best


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tickers", nargs="*", help="just these, instead of the universe")
    ap.add_argument("--start", default="2013-01-01",
                    help="download from here, so indicators have warm-up before 2016")
    ap.add_argument("--end", default=pd.Timestamp.today().date().isoformat())
    ap.add_argument("--window-floor", default="2016-01-01",
                    help="membership before this date is not checked (no data that far back)")
    ap.add_argument("--missing-only", action="store_true",
                    help="skip tickers whose bar file already exists")
    args = ap.parse_args()

    spells = spells_by_ticker()
    wanted = [t.upper() for t in args.tickers] or sorted(spells) + BENCHMARKS
    if args.missing_only:
        before = len(wanted)
        wanted = [t for t in wanted if not bar_path(BARS, t).exists()]
        print(f"{before - len(wanted)} already on disk, {len(wanted)} to fetch")
    BARS.mkdir(parents=True, exist_ok=True)

    kept: list[dict] = []
    dropped: list[dict] = []

    for i, ticker in enumerate(wanted, 1):
        df = download(ticker, args.start, args.end)
        if df is None or df.empty:
            dropped.append({"ticker": ticker, "reason": "no_data",
                            "note": "Yahoo has nothing for this symbol"})
            print(f"[{i}/{len(wanted)}] {ticker:6s} DROP  no data", flush=True)
            continue

        mine = spells.get(ticker)
        if mine is not None:
            ok, in_window = covers_its_spells(df, mine, args.window_floor)
            if not ok:
                dropped.append({
                    "ticker": ticker, "reason": "wrong_company",
                    "note": "bars do not fall inside this company's index membership -- "
                            "the symbol was almost certainly reused by a later listing",
                    "spells": mine, "bars_in_window": in_window,
                    "yahoo_first": df.index[0].date().isoformat(),
                    "yahoo_last": df.index[-1].date().isoformat(),
                })
                print(f"[{i}/{len(wanted)}] {ticker:6s} DROP  wrong company "
                      f"(yahoo {df.index[0].date()}..{df.index[-1].date()})", flush=True)
                continue

        df = df[[c for c in FIELDS if c in df.columns]].dropna(how="all")
        rows = [{"date": d.date().isoformat(),
                 "open": float(r.get("Open", float("nan"))),
                 "high": float(r.get("High", float("nan"))),
                 "low": float(r.get("Low", float("nan"))),
                 "close": float(r.get("Close", float("nan"))),
                 "adj_close": float(r.get("Adj Close", float("nan"))),
                 "volume": float(r.get("Volume", 0) or 0)}
                for d, r in df.iterrows()]
        bar_path(BARS, ticker).write_text(
            json.dumps({"ticker": ticker, "source": "yahoo",
                        "fetched_at": pd.Timestamp.utcnow().isoformat(),
                        "adjusted": False, "bars": rows}), encoding="utf-8")
        kept.append({"ticker": ticker, "bars": len(rows),
                     "first": rows[0]["date"], "last": rows[-1]["date"]})
        if i % 25 == 0:
            print(f"[{i}/{len(wanted)}] kept={len(kept)} dropped={len(dropped)}", flush=True)

    # MERGE, never replace. Fetching a handful of tickers used to overwrite the
    # record of the whole universe -- the bars survived but the account of what
    # was kept and what was dropped, and why, did not. That account is the only
    # place the hole in the universe is written down, so losing it is worse than
    # losing a download that can simply be run again.
    def merge(path: Path, rows: list[dict], key: str) -> list[dict]:
        old = {}
        if path.exists():
            blob = json.loads(path.read_text(encoding="utf-8"))
            for row in (blob.get("tickers", blob) if isinstance(blob, dict) else blob):
                old[row[key]] = row
        for row in rows:
            old[row[key]] = row
        return sorted(old.values(), key=lambda r: r[key])

    all_kept = merge(BARS / "_manifest.json", kept, "ticker")
    fresh = {r["ticker"] for r in kept}
    all_dropped = [r for r in merge(BARS / "_dropped.json", dropped, "ticker")
                   if r["ticker"] not in fresh]        # a ticker that now works is not dropped
    (BARS / "_manifest.json").write_text(json.dumps({
        "built_at": pd.Timestamp.utcnow().isoformat(),
        "start": args.start, "end": args.end,
        "kept": len(all_kept), "dropped": len(all_dropped), "tickers": all_kept,
    }, indent=1), encoding="utf-8")
    (BARS / "_dropped.json").write_text(json.dumps(all_dropped, indent=1), encoding="utf-8")

    print(f"\nkept {len(kept)}, dropped {len(dropped)}")
    from collections import Counter
    for reason, n in Counter(d["reason"] for d in dropped).most_common():
        print(f"  {reason}: {n}")


if __name__ == "__main__":
    main()
