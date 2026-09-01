"""Sector, industry, and "these move together" (rewritten 2026-08-04).

TWO LABELS, TWO JOBS -- agreed with the user, who asked for one standard and
then agreed to keep the correlation warning as a separate thing:

  1. **Sector** -- one standard, no exceptions, no overrides. The 11 SPDR/GICS
     sectors (XLK, XLV, XLF, XLY, XLP, XLE, XLI, XLB, XLRE, XLU, XLC), derived
     from the SIC code each company files with the SEC. Plus **industry**, the
     SEC's own description, as the sub-level underneath. Answers: *am I too
     heavy in one industry?*

  2. **Correlation group** -- a short, hand-kept list, ONLY for the handful of
     baskets where the official sector label is misleading. Answers: *am I
     secretly making one big bet?*

WHY BOTH, and why the second one is not redundant:

    NVDA, MSFT and CRM are Information Technology. GOOGL and META are
    Communication Services. AMZN is Consumer Discretionary. Hold all six and
    the sector view reports a comfortable spread across three sectors -- while
    in reality it is one position in one trade that rises and falls together.

    That is not hypothetical. The 2026-07 strategy review found ~84% of this
    account was a single correlated bet (SPY+QQQ+NVDA+AMZN, since NVDA/AMZN are
    themselves top QQQ holdings) with nothing anywhere flagging it. The old
    hand-made sector map existed to catch exactly that -- and folding it into a
    single "one standard" field would have quietly switched it back off.

    So the standard stays pure, and the correlation warning lives beside it.

WHAT CHANGED FROM THE OLD FILE (2026-07-18 to 2026-07-30):

    The old design was one hand-written map used for everything. It held ~102
    tickers, which covered only **7.4%** of names in the 15-year backtest --
    so every other stock fell into one shared "unknown" bucket, and a 40% cap
    over a single bucket containing everything is a cap that can never fire. It
    silently rejected every candidate after the first open position in the
    first protocol run, and weakened the same rule live.

    Coverage is now **97.6%**, because the sector comes from a real source
    rather than from whoever last edited this file.

HONEST CAVEAT, stated once: **SIC is not GICS.** GICS belongs to S&P/MSCI and
costs money; SIC is free from the SEC. They agree on the obvious cases and
differ at the edges -- GOOGL and META file under computer services, so SIC puts
them in Technology where real GICS says Communication Services. This is a
close, free approximation, labelled as one. The correlation group is what
actually protects against the concentration those edge cases can hide.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import sic_to_sector

_SEC_PATH = Path(__file__).resolve().parent / "sector_codes_sec.json"
try:
    _SEC: dict = json.loads(_SEC_PATH.read_text(encoding="utf-8"))
except (OSError, ValueError):
    # Generated file, not source. A machine without it degrades to "unknown"
    # rather than failing to import.
    _SEC = {}


# --- label 2: the short hand list -------------------------------------------
#
# ONLY groups whose members sit in DIFFERENT official sectors but trade as one
# position. A basket that is already one sector needs no entry here -- the
# sector cap alone catches it, and a redundant entry is one more thing to keep
# in sync for no benefit.
CORRELATION_GROUP = {
    # The original 84%-one-bet finding. Officially three sectors, in practice
    # one trade.
    "NVDA": "mega_cap_growth_tech",
    "MSFT": "mega_cap_growth_tech",
    "CRM": "mega_cap_growth_tech",
    "GOOGL": "mega_cap_growth_tech",
    "META": "mega_cap_growth_tech",
    "AMZN": "mega_cap_growth_tech",

    # Crypto equities file under data processing, mining, even finance -- what
    # they actually track is bitcoin.
    "BTC": "crypto_exposure",
    "BTCUSD": "crypto_exposure",
    "ETHUSD": "crypto_exposure",
    "HUT": "crypto_exposure",
    "RIOT": "crypto_exposure",
    "WGMI": "crypto_exposure",
    "CIFR": "crypto_exposure",
    "IREN": "crypto_exposure",

    # Broad index ETFs are not a sector at all, and holding both alongside
    # mega-cap tech is the concentration this whole file exists to surface.
    "SPY": "us_large_cap_index",
    "QQQ": "us_large_cap_index",

    # Officially Communication Services (ASTS) and Industrials (RKLB); one
    # speculative space trade in practice.
    "ASTS": "space_satellite_speculative",
    "RKLB": "space_satellite_speculative",
}


def get_sector(ticker: str) -> Optional[str]:
    """The standard sector name, e.g. "Information Technology". None only when
    the ticker genuinely has none (an ETF, a foreign issuer) -- never a guess."""
    entry = _SEC.get(ticker.upper())
    return entry.get("sector") if entry else None


def get_sector_etf(ticker: str) -> Optional[str]:
    """The ETF ticker for that sector, e.g. "XLK" -- the shorthand the user
    reads sectors in."""
    entry = _SEC.get(ticker.upper())
    return entry.get("sector_etf") if entry else None


def get_industry(ticker: str) -> Optional[str]:
    """The sub-level under the sector, in the SEC's own words, e.g.
    "Semiconductors & Related Devices". Display/context only -- never the
    cap-check denominator, which would make almost every position 100% of its
    own tiny industry."""
    entry = _SEC.get(ticker.upper())
    return entry.get("industry") if entry else None


def get_correlation_group(ticker: str) -> Optional[str]:
    """The "these move together" basket, or None for the vast majority of
    tickers that need no special handling. Deliberately sparse: an entry here
    is a claim that the official sector is misleading for this name, and that
    claim should be rare and defensible."""
    return CORRELATION_GROUP.get(ticker.upper())


def get_sector_group(ticker: str) -> Optional[str]:
    """What the 40% cap gates against (get_sector_exposure(), /maxadd's
    sector line, every report's sector disclosure).

    Kept under the old name so every existing caller keeps working, but it now
    returns the ONE standard sector rather than a hand-made grouping. Callers
    that also want the concentration warning ask get_correlation_group()
    separately -- see this module's docstring for why they are two questions."""
    return get_sector(ticker)


def get_sector_subgroup(ticker: str) -> Optional[str]:
    """Retained name for the fine-grained label; now the SEC industry."""
    return get_industry(ticker)


def describe(ticker: str) -> dict:
    """Everything known about one ticker, for a report line. `moves_with` is
    None for most names and that is the normal case, not missing data."""
    return {
        "ticker": ticker.upper(),
        "sector": get_sector(ticker),
        "sector_etf": get_sector_etf(ticker),
        "industry": get_industry(ticker),
        "moves_with": get_correlation_group(ticker),
    }
