"""Historical earnings-announcement dates from SEC EDGAR (2026-08-03).

Why: the rubric's 5th criterion ("no earnings inside the holding window") could
never be scored in the backtest -- there were no historical earnings dates
anywhere in this project, only the NEXT one from TradingView, which is all the
live path needs. So every one of 43,957 backtested breaks failed that criterion
automatically, capping every historical grade at B and making it impossible to
ask whether the earnings rule is worth anything at all.

The source is the exact announcement, not an estimate: US companies file an
**8-K with item 2.02 ("Results of Operations and Financial Condition")** on the
day they report. That filing date IS the earnings date. Free, no API key,
history back to the 1990s, published by the regulator.

Rejected alternatives, for the record:
  - Yahoo: next date plus roughly four past quarters. Not enough history.
  - TradingView (what the live system uses): next date only, by design.
  - Paid vendors: money for something the government gives away.
  - Inferring from price (big gap + volume spike every ~63 bars): works, but
    approximating something that is available exactly is a poor trade.

Politeness: SEC asks for a declared User-Agent and a reasonable rate. Both are
honoured below -- this is someone else's free service.

Output: `_backtest_bars_cache/_earnings.csv`, one row per (ticker, date).

Usage:
  python bot/fetch_earnings_history.py                # every ticker in the bar cache
  python bot/fetch_earnings_history.py AAPL NVDA      # just these
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

import requests

CACHE = Path(__file__).resolve().parent.parent / "_backtest_bars_cache"
OUT = CACHE / "_earnings.csv"
# SEC requires a real contact address in the User-Agent. Anonymous scraping of
# this service gets blocked, and deservedly.
# Set SEC_CONTACT_EMAIL in your environment; SEC blocks anonymous callers.
SEC_CONTACT = os.environ.get("SEC_CONTACT_EMAIL", "your_email@example.com")
HEADERS = {"User-Agent": f"TradingResearch {SEC_CONTACT}"}
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
RATE_DELAY = 0.12          # ~8 requests/sec, inside SEC's stated 10/sec ceiling
EARNINGS_ITEM = "2.02"     # "Results of Operations and Financial Condition"


def load_ticker_map() -> dict:
    resp = requests.get(TICKER_MAP_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in resp.json().values()}


def _extract(block: dict) -> list:
    """8-K rows whose `items` list includes 2.02. `items` is a comma-joined
    string like "2.02,9.01" -- a substring check is enough and avoids depending
    on the ordering."""
    forms = block.get("form") or []
    dates = block.get("filingDate") or []
    items = block.get("items") or [""] * len(forms)
    out = []
    for form, date, item in zip(forms, dates, items):
        if form == "8-K" and EARNINGS_ITEM in (item or ""):
            out.append(date)
    return out


def earnings_dates(cik: str) -> list:
    """Every earnings announcement this company ever filed, oldest first.

    EDGAR splits long filing histories: the `recent` block holds roughly the
    last thousand filings and older ones live in separate archive files listed
    under `files`. Reading only `recent` silently truncates big filers to about
    ten years, which for a 15-year backtest would quietly drop a third of the
    data -- so the archives are followed too."""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    dates = _extract(data.get("filings", {}).get("recent", {}))
    for extra in data.get("filings", {}).get("files") or []:
        time.sleep(RATE_DELAY)
        sub = requests.get(f"https://data.sec.gov/submissions/{extra['name']}",
                            headers=HEADERS, timeout=30)
        if sub.ok:
            dates.extend(_extract(sub.json()))
    return sorted(set(dates))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*")
    parser.add_argument("--since", default="2010-01-01",
                        help="drop announcements before this date")
    args = parser.parse_args()

    tickers = [t.upper() for t in args.tickers] or sorted(
        p.stem for p in CACHE.glob("*.csv") if not p.stem.startswith("_"))
    print(f"{len(tickers)} tickers")

    print("loading the SEC ticker->CIK map...")
    ticker_map = load_ticker_map()
    print(f"  {len(ticker_map):,} companies in the map")

    rows, unmapped, failed, no_earnings = [], [], [], []
    for n, ticker in enumerate(tickers, 1):
        cik = ticker_map.get(ticker)
        if not cik:
            unmapped.append(ticker)
            continue
        try:
            dates = [d for d in earnings_dates(cik) if d >= args.since]
        except Exception as e:
            failed.append((ticker, str(e)[:60]))
            time.sleep(1.0)
            continue
        if not dates:
            no_earnings.append(ticker)
        rows.extend((ticker, d) for d in dates)
        time.sleep(RATE_DELAY)
        if n % 50 == 0:
            print(f"  [{n}/{len(tickers)}] {len(rows):,} announcements so far")

    with OUT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ticker", "earnings_date"])
        writer.writerows(sorted(rows))

    covered = len({t for t, _ in rows})
    print(f"\nwrote {len(rows):,} announcements for {covered} tickers -> {OUT}")
    print(f"  unmapped (not found at the SEC -- ETFs, foreign issuers, renamed): "
          f"{len(unmapped)}")
    if unmapped:
        print(f"    {', '.join(unmapped[:25])}{' ...' if len(unmapped) > 25 else ''}")
    print(f"  mapped but no 8-K item 2.02 filings: {len(no_earnings)}")
    if failed:
        print(f"  fetch failures: {len(failed)} -- {failed[:5]}")
    print("\nA ticker with no earnings rows is NOT the same as a ticker whose "
          "earnings are unknown.\nETFs genuinely have none; that distinction is "
          "kept in the consumer, never guessed here.")


if __name__ == "__main__":
    main()
