"""Splits the account's money into the two books it is really made of --
buy-and-hold Core (SPY/QQQ) and the swing trades -- so each one can be looked
at on its own (2026-08-19, user's request).

Why this exists: the broker shows ONE blended up/down number. A good Core
month can hide a bad trading month inside it, and a red Core day can make a
perfectly fine trading record look like a disaster. Neither reading is true,
and both of them drive real decisions -- the whole point of the Core/Swing
split in STRATEGY_v3.md is that the two books are judged by different
standards, on different clocks. A number that merges them cannot be judged by
either standard.

Everything here is arithmetic over rows the caller supplies -- no database, no
network, no judgment. persistence.get_pnl_positions() reads the positions and
their exits; the caller fetches live prices (bot/fetch_pnl_prices.py) and
passes them in. Same Category A/B split as the rest of this codebase, and the
reason this file is trivially testable.

Two rules the numbers follow, both deliberate:

* Money already banked and money still on paper are never added into one
  figure without saying so. A closed trade's dollars are final; an open
  position's are a quote away from changing.
* A position whose price could not be fetched is NAMED, never quietly valued
  at zero or silently dropped. A missing price makes the total incomplete,
  and an incomplete total that looks complete is worse than no total.
"""

from __future__ import annotations

from typing import Optional

CORE = "core"
SWING = "swing"


def _bucket(rows: list[dict], prices: dict) -> dict:
    """One book's money, from its own position rows plus whatever prices came back."""
    realized_usd = 0.0
    held_cost_usd = 0.0
    held_value_usd = 0.0
    holdings: list[dict] = []
    unpriced: list[str] = []

    for r in rows:
        realized_usd += r["realized_usd"] or 0.0
        remaining = r.get("remaining_qty") or 0
        if remaining <= 0:
            continue
        price = prices.get(r["ticker"])
        cost = r["entry_price"] * remaining
        if price is None:
            unpriced.append(r["ticker"])
            holdings.append({
                "ticker": r["ticker"], "qty": remaining, "entry_price": r["entry_price"],
                "price": None, "cost_usd": cost, "value_usd": None,
                "open_usd": None, "open_pct": None,
            })
            continue
        value = price * remaining
        held_cost_usd += cost
        held_value_usd += value
        holdings.append({
            "ticker": r["ticker"], "qty": remaining, "entry_price": r["entry_price"],
            "price": price, "cost_usd": cost, "value_usd": value,
            "open_usd": value - cost,
            "open_pct": (value - cost) / cost if cost else None,
        })

    open_usd = held_value_usd - held_cost_usd
    return {
        "realized_usd": realized_usd,
        "open_usd": open_usd,
        "total_usd": realized_usd + open_usd,
        "held_cost_usd": held_cost_usd,
        "held_value_usd": held_value_usd,
        # A percent on money still in the market. Never computed against
        # realized dollars -- those have no "amount still invested" to divide
        # by, and dividing by the original cost of a position that is already
        # sold answers a question nobody asked.
        "open_pct": (open_usd / held_cost_usd) if held_cost_usd else None,
        "holdings": sorted(holdings, key=lambda h: -(h["cost_usd"] or 0)),
        "unpriced_tickers": unpriced,
    }


def split_pnl(rows: list[dict], prices: Optional[dict] = None) -> dict:
    """Core money and trading money, side by side, plus their sum.

    `rows` is persistence.get_pnl_positions()'s output. `prices` maps ticker ->
    last price; leave a ticker out (or pass None) and its still-held shares are
    reported as unvalued rather than guessed. With no prices at all this still
    returns a truthful answer -- the closed-trade dollars, and an honest "the
    open positions could not be valued".
    """
    prices = {k.upper(): v for k, v in (prices or {}).items() if v is not None}
    core_rows = [r for r in rows if r["sleeve"] == CORE]
    swing_rows = [r for r in rows if r["sleeve"] != CORE]

    core = _bucket(core_rows, prices)
    trades = _bucket(swing_rows, prices)

    unpriced = core["unpriced_tickers"] + trades["unpriced_tickers"]
    return {
        "core": core,
        "trades": trades,
        "total": {
            "realized_usd": core["realized_usd"] + trades["realized_usd"],
            "open_usd": core["open_usd"] + trades["open_usd"],
            "total_usd": core["total_usd"] + trades["total_usd"],
            "held_cost_usd": core["held_cost_usd"] + trades["held_cost_usd"],
            "held_value_usd": core["held_value_usd"] + trades["held_value_usd"],
        },
        # True whenever ANY held position went unvalued -- the one flag a caller
        # needs to decide whether the total may be presented as the whole story.
        "complete": not unpriced,
        "unpriced_tickers": unpriced,
        # The oldest entry still on file. Every dollar below was recorded by
        # this system from that date on; a broker statement that starts earlier
        # will not match, and the caller is expected to say so.
        "first_entry_date": min((r["entry_date"] for r in rows if r.get("entry_date")), default=None),
        "closed_trades": sum(1 for r in swing_rows if (r.get("remaining_qty") or 0) <= 0),
        "open_trades": sum(1 for r in swing_rows if (r.get("remaining_qty") or 0) > 0),
    }
