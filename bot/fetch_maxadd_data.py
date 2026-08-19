"""Category A data-gathering for /maxadd (2026-07-29).

Fetches the live quote and a small daily-bar window for one ticker and computes
ATR14 (Wilder/RMA, same as every other script in this codebase) -- the two real
numbers /maxadd needs that a stored position/thesis row can never hold, since
they change every session. No judgment here: does not decide whether a target
still qualifies, does not re-pick a stop -- process_queue.py's _handle_maxadd
does that arithmetic (rule 3/4 gates) against this output plus the already-
stored position/entry_setup, same Category A/B split as fetch_analysis_data.py
and fetch_monitor_data.py.

Deliberately NOT reused fetch_analysis_data.py directly: that script also pulls
SPY/QQQ, 5 years of history, sector data, and resistance-wall chaining -- all
needed for a fresh SCREENER_v3 build, none of it for /maxadd's narrower "is the
live price/ATR still within the already-stored plan" check. Same tiny-fetch
precedent as fetch_monitor_data.py's years=0.15 daily window.

Usage: python bot/fetch_maxadd_data.py TICKER
Output: JSON to stdout.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import indicators_core as ic
from tv_data import TVClient, assert_data_fresh


async def _fetch(ticker: str):
    async with TVClient() as client:
        # years=0.15 (~38 trading days) -- enough bars for a real ATR14 window
        # with margin, same figure/reasoning as fetch_monitor_data.py's own
        # daily fetch (2026-07-29 bump there for the identical reason).
        daily_bars = await client.get_daily_history(ticker, years=0.15)
        quote = await client.get_quote(ticker)
        return daily_bars, quote


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    args = parser.parse_args()
    ticker = args.ticker.upper()

    daily_bars, quote = asyncio.run(_fetch(ticker))
    freshness = assert_data_fresh(daily_bars)

    highs = [b["high"] for b in daily_bars]
    lows = [b["low"] for b in daily_bars]
    closes = [b["close"] for b in daily_bars]
    atr14 = ic.atr_wilder(highs, lows, closes, period=14)

    output = {
        "ticker": ticker,
        "current_price": quote.get("close") or quote.get("last"),
        "atr14": atr14,
        "freshness": {
            "fresh": freshness.fresh,
            "last_bar_date": freshness.last_bar_date,
            "most_recent_complete_session": freshness.most_recent_complete_session,
        },
    }
    print(json.dumps(output, ensure_ascii=True))


if __name__ == "__main__":
    main()
