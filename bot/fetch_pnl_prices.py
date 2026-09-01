"""Category A data-gathering for /pnl (2026-08-19): the last price of every
ticker still held, and nothing else.

/pnl needs exactly one live number per open position -- what a share is worth
right now -- to turn "shares held" into "dollars". No bars, no ATR, no
indicators: none of that changes the answer to "how much am I up". So this
fetches quotes only, over ONE TradingView session for the whole list, rather
than reusing fetch_maxadd_data.py per ticker (which would also pull a month of
daily bars for each one and re-open the connector every time).

A ticker whose quote fails does NOT fail the run. Its price comes back null,
pnl_split.py names it as unvalued, and the message says the total is missing
that piece -- one dead symbol must not blank out an otherwise true answer.

Usage: python bot/fetch_pnl_prices.py SPY QQQ AAPL
Output: JSON to stdout -- {"prices": {...}, "errors": {...}}
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tv_data import TVClient


async def _fetch(tickers: list[str]) -> tuple[dict, dict]:
    prices: dict = {}
    errors: dict = {}
    async with TVClient() as client:
        for ticker in tickers:
            try:
                quote = await client.get_quote(ticker)
            except Exception as exc:  # noqa: BLE001 -- one bad symbol must not sink the rest
                prices[ticker] = None
                errors[ticker] = str(exc)
                continue
            price = quote.get("close") or quote.get("last")
            prices[ticker] = price
            if price is None:
                errors[ticker] = "quote carried no close/last price"
    return prices, errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tickers", nargs="+")
    args = parser.parse_args()
    tickers = []
    for t in args.tickers:
        upper = t.upper()
        if upper not in tickers:
            tickers.append(upper)

    if not tickers:
        print(json.dumps({"prices": {}, "errors": {}}))
        return
    prices, errors = asyncio.run(_fetch(tickers))
    print(json.dumps({"prices": prices, "errors": errors}, ensure_ascii=True))


if __name__ == "__main__":
    main()
