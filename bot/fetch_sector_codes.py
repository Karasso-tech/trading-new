"""Official industry codes from SEC EDGAR, as a fallback under the hand-made
sector map (2026-08-03).

Why: `sector_map.py` is a deliberately hand-curated list -- it encodes real
judgment about which tickers actually MOVE TOGETHER, which no automatic
classifier gets right (NVDA and AMZN trade as one bet; a GICS lookup files them
in different sectors). That design is correct and is not being replaced.

The problem is coverage, not design. It holds ~102 tickers. Across the 15-year
backtest only **7.4%** of breaks were on a mapped ticker, so every other name
fell into one shared "unknown" bucket -- and a cap applied to one giant bucket
containing everything is not a cap at all. It quietly rejected every candidate
after the first open position in the first protocol run, and it almost
certainly weakens the same 40% rule in the live system.

So: the hand map stays FIRST and always wins. This adds a second layer
underneath it, so an unmapped ticker gets a real industry instead of "unknown".
The source is the SIC code the company itself files with the SEC -- already
available from the same endpoint the earnings history came from, free.

`sector_map.get_sector_source()` reports which layer answered, so the two are
never confused and any ticker worth a real judgment call can be promoted into
the hand map by hand.

Output: `bot/sector_codes_sec.json`.

Usage:
  python bot/fetch_sector_codes.py            # every ticker in the bar cache
  python bot/fetch_sector_codes.py AAPL NVDA
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sic_to_sector

CACHE = Path(__file__).resolve().parent.parent / "_backtest_bars_cache"
OUT = Path(__file__).resolve().parent / "sector_codes_sec.json"
# Set SEC_CONTACT_EMAIL in your environment; SEC blocks anonymous callers.
SEC_CONTACT = os.environ.get("SEC_CONTACT_EMAIL", "your_email@example.com")
HEADERS = {"User-Agent": f"TradingResearch {SEC_CONTACT}"}
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
RATE_DELAY = 0.12

# SIC major groups collapsed into the same coarse buckets the hand map already
# uses, so the two layers speak one language. Deliberately coarse: this is a
# fallback for names nobody has judged yet, and a wrong-but-broad bucket is far
# less harmful than a confident wrong-but-narrow one.
_SIC_PREFIX_TO_GROUP = {
    "01": "consumer_staples", "02": "consumer_staples", "07": "consumer_staples",
    "10": "materials", "12": "energy", "13": "energy", "14": "materials",
    "15": "industrials", "16": "industrials", "17": "industrials",
    "20": "consumer_staples", "21": "consumer_staples", "22": "consumer_discretionary",
    "23": "consumer_discretionary", "24": "industrials", "25": "consumer_discretionary",
    "26": "materials", "27": "communication_services", "28": "health_care",
    "29": "energy", "30": "materials", "31": "consumer_discretionary",
    "32": "materials", "33": "materials", "34": "industrials",
    "35": "industrials", "36": "technology", "37": "industrials",
    "38": "health_care", "39": "consumer_discretionary",
    "40": "industrials", "41": "industrials", "42": "industrials",
    "44": "industrials", "45": "industrials", "46": "energy", "47": "industrials",
    "48": "communication_services", "49": "utilities",
    "50": "industrials", "51": "industrials", "52": "consumer_discretionary",
    "53": "consumer_discretionary", "54": "consumer_staples",
    "55": "consumer_discretionary", "56": "consumer_discretionary",
    "57": "consumer_discretionary", "58": "consumer_discretionary",
    "59": "consumer_discretionary",
    "60": "financials", "61": "financials", "62": "financials", "63": "financials",
    "64": "financials", "65": "real_estate", "67": "financials",
    "70": "consumer_discretionary", "72": "consumer_discretionary",
    "73": "technology", "75": "consumer_discretionary", "78": "communication_services",
    "79": "consumer_discretionary", "80": "health_care", "82": "consumer_discretionary",
    "83": "consumer_discretionary", "87": "industrials", "89": "industrials",
}


def group_for_sic(sic: str) -> str:
    """Coarse bucket for a 4-digit SIC code. Unknown/blank -> 'other', never a
    guess dressed up as a classification."""
    if not sic or len(sic) < 2:
        return "other"
    return _SIC_PREFIX_TO_GROUP.get(sic[:2], "other")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*")
    args = parser.parse_args()

    tickers = [t.upper() for t in args.tickers] or sorted(
        p.stem for p in CACHE.glob("*.csv") if not p.stem.startswith("_"))
    print(f"{len(tickers)} tickers")

    ticker_map = {v["ticker"].upper(): str(v["cik_str"]).zfill(10)
                   for v in requests.get(TICKER_MAP_URL, headers=HEADERS, timeout=30).json().values()}
    print(f"SEC map: {len(ticker_map):,} companies")

    # MERGE, never replace. Found the hard way, 2026-08-04: running this for 16
    # extra tickers wiped the 493 already in the file, because a plain
    # write_text of a fresh dict is a silent full overwrite. A fetcher that
    # destroys prior work when you ask it for one more ticker is a trap.
    try:
        out = json.loads(OUT.read_text(encoding="utf-8"))
        print(f"merging into {len(out)} existing entries")
    except (OSError, ValueError):
        out = {}
    unmapped, failed = [], []
    for n, ticker in enumerate(tickers, 1):
        cik = ticker_map.get(ticker)
        if not cik:
            unmapped.append(ticker)
            continue
        try:
            data = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                                 headers=HEADERS, timeout=30).json()
            sic = str(data.get("sic") or "")
            etf = sic_to_sector.sector_etf_for_sic(sic)
            out[ticker] = {
                "sic": sic,
                "industry": data.get("sicDescription") or "",
                "sector_etf": etf,
                "sector": sic_to_sector.SECTORS.get(etf) if etf else None,
            }
        except Exception as e:
            failed.append((ticker, str(e)[:60]))
        time.sleep(RATE_DELAY)
        if n % 100 == 0:
            print(f"  [{n}/{len(tickers)}] {len(out)} classified")

    OUT.write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
    print(f"\nwrote {len(out)} classifications -> {OUT}")
    print(f"  not found at the SEC (ETFs, foreign issuers): {len(unmapped)}")
    if unmapped:
        print(f"    {', '.join(unmapped[:20])}")
    if failed:
        print(f"  failures: {len(failed)}")
    from collections import Counter
    print("\n  bucket spread:")
    for group, count in Counter(v.get("sector_etf") or "(none)" for v in out.values()).most_common():
        print(f"    {group:<26}{count:>5}")


if __name__ == "__main__":
    main()
