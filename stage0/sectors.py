"""Which sector each ticker belongs to, and which ETF stands for that sector.

Two sources, because neither alone covers the universe:

  * Wikipedia's current S&P 500 table gives a GICS sector for the 503 companies
    in the index today. That is 90% of the trades in the book.
  * The other ~100 are names that LEFT the index, and Wikipedia drops them. For
    those, the SEC knows the company's SIC code -- an older, coarser industry
    scheme that every filer carries -- and that is mapped to the nearest GICS
    sector below.

Two honest limits, both worth repeating wherever a sector result is shown:

  * The Wikipedia sector is TODAY's sector, applied to a trade from 2019. GICS
    does get reshuffled (Communication Services was created in 2018, and some
    Financials/Technology names moved in 2023), so a small number of rows carry
    a label the company did not have at the time.
  * SIC is not GICS. The mapping below is a reasonable translation, not an
    official one. Rows that came from it are labelled `sec_sic` so they can be
    counted separately, or dropped, by anything that cares.

Sector STRENGTH -- which is what the model actually wants -- comes from the
eleven SPDR sector ETFs, one per GICS sector. Those are real daily bars, so the
strength of a sector on a given day is a fact rather than an approximation.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "universe" / "sectors.json"
MANIFEST = ROOT / "data" / "bars" / "_manifest.json"

HEADERS = {"User-Agent": "stage0-research omrikarasso@gmail.com"}
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# One ETF per GICS sector. XLC only exists from mid-2018 and XLRE from late
# 2015; both cover the whole 2019-2024 study window, so nothing is lost here.
SECTOR_ETF = {
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Information Technology": "XLK",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
}

# SIC -> the nearest GICS sector. Four-digit entries win over two-digit ones,
# and they exist for the groups where the coarse answer would be plainly wrong:
# a pharmaceutical company and a paint company are both "chemicals" under SIC.
SIC4 = {
    "2833": "Health Care", "2834": "Health Care", "2835": "Health Care",
    "2836": "Health Care", "3826": "Health Care", "3841": "Health Care",
    "3842": "Health Care", "3843": "Health Care", "3844": "Health Care",
    "3845": "Health Care", "3851": "Health Care", "8731": "Health Care",
    "3570": "Information Technology", "3571": "Information Technology",
    "3572": "Information Technology", "3576": "Information Technology",
    "3577": "Information Technology", "3578": "Information Technology",
    "3579": "Information Technology", "3661": "Information Technology",
    "3663": "Information Technology", "3669": "Information Technology",
    "3674": "Information Technology", "3678": "Information Technology",
    "7310": "Communication Services", "7812": "Communication Services",
    "7819": "Communication Services", "7822": "Communication Services",
    "7829": "Communication Services", "7841": "Communication Services",
    "7900": "Communication Services", "7990": "Communication Services",
    "7996": "Consumer Discretionary", "7011": "Consumer Discretionary",
    "3711": "Consumer Discretionary", "3713": "Consumer Discretionary",
    "3714": "Consumer Discretionary", "3716": "Consumer Discretionary",
    "3721": "Industrials", "3724": "Industrials", "3728": "Industrials",
    "3760": "Industrials", "3812": "Industrials",
    "6798": "Real Estate", "6500": "Real Estate", "6512": "Real Estate",
    "6552": "Real Estate", "6531": "Real Estate",
    "5411": "Consumer Staples", "5412": "Consumer Staples",
    "2911": "Energy", "4610": "Energy", "4612": "Energy", "4613": "Energy",
}

SIC2 = {
    "01": "Consumer Staples", "02": "Consumer Staples", "07": "Consumer Staples",
    "08": "Materials", "09": "Consumer Staples",
    "10": "Materials", "12": "Energy", "13": "Energy", "14": "Materials",
    "15": "Industrials", "16": "Industrials", "17": "Industrials",
    "20": "Consumer Staples", "21": "Consumer Staples",
    "22": "Consumer Discretionary", "23": "Consumer Discretionary",
    "24": "Materials", "25": "Consumer Discretionary", "26": "Materials",
    "27": "Communication Services", "28": "Materials", "29": "Energy",
    "30": "Materials", "31": "Consumer Discretionary", "32": "Materials",
    "33": "Materials", "34": "Industrials", "35": "Industrials",
    "36": "Information Technology", "37": "Industrials", "38": "Health Care",
    "39": "Consumer Discretionary",
    "40": "Industrials", "41": "Industrials", "42": "Industrials",
    "44": "Industrials", "45": "Industrials", "46": "Energy",
    "47": "Industrials", "48": "Communication Services", "49": "Utilities",
    "50": "Industrials", "51": "Consumer Staples",
    "52": "Consumer Discretionary", "53": "Consumer Discretionary",
    "54": "Consumer Staples", "55": "Consumer Discretionary",
    "56": "Consumer Discretionary", "57": "Consumer Discretionary",
    "58": "Consumer Discretionary", "59": "Consumer Discretionary",
    "60": "Financials", "61": "Financials", "62": "Financials",
    "63": "Financials", "64": "Financials", "65": "Real Estate",
    "67": "Financials",
    "70": "Consumer Discretionary", "72": "Consumer Discretionary",
    "73": "Information Technology", "75": "Consumer Discretionary",
    "76": "Consumer Discretionary", "78": "Communication Services",
    "79": "Consumer Discretionary", "80": "Health Care",
    "82": "Consumer Discretionary", "83": "Health Care",
    "87": "Industrials", "89": "Industrials",
}


# Ten companies the SEC no longer lists as filers -- all acquired around
# 2018-2019, so they left the index AND stopped filing. Their sector is not in
# doubt for any of them, but this is me typing rather than a source, so they are
# labelled `manual` and can be excluded by anything that would rather not
# trust a person's memory over a file.
MANUAL = {
    "AET": "Health Care",             # Aetna, bought by CVS
    "ANDV": "Energy",                 # Andeavor, refiner, bought by Marathon
    "BMS": "Materials",               # Bemis, packaging, bought by Amcor
    "COL": "Industrials",             # Rockwell Collins, avionics, bought by UTC
    "ESRX": "Health Care",            # Express Scripts, bought by Cigna
    "EVHC": "Health Care",            # Envision Healthcare, taken private
    "HAR": "Information Technology",  # Harman, audio electronics, bought by Samsung
    "HOT": "Consumer Discretionary",  # Starwood Hotels, bought by Marriott
    "SCG": "Utilities",               # SCANA, bought by Dominion
    "TWX": "Communication Services",  # Time Warner, bought by AT&T
}


def sic_to_sector(sic: str) -> str | None:
    sic = (sic or "").strip().zfill(4)
    return SIC4.get(sic) or SIC2.get(sic[:2])


def main() -> None:
    norm = lambda s: s.strip().upper().replace(".", "-")
    kept = {t["ticker"] for t in json.loads(MANIFEST.read_text(encoding="utf-8"))["tickers"]}

    out: dict[str, dict] = {}
    # All three index pages publish a GICS sector for their current members.
    for page, label in (("current.html", "wikipedia_sp500"),
                        ("sp400.html", "wikipedia_sp400"),
                        ("sp600.html", "wikipedia_sp600")):
        path = RAW / page
        if not path.exists():
            continue
        table = pd.read_html(path)[0]
        if "GICS Sector" not in table.columns:
            continue
        for symbol, sector in zip(table["Symbol"], table["GICS Sector"]):
            out.setdefault(norm(symbol), {"sector": sector, "source": label})
    print(f"from Wikipedia: {len(out)}")

    missing = sorted(kept - set(out) - set(SECTOR_ETF.values()) - {"SPY", "QQQ", "IWM"})
    print(f"still missing:  {len(missing)}")

    book = requests.get("https://www.sec.gov/files/company_tickers.json",
                        headers=HEADERS, timeout=60).json()
    cik = {v["ticker"].upper().replace(".", "-"): v["cik_str"] for v in book.values()}

    filled = 0
    for ticker in missing:
        if ticker not in cik:
            continue
        try:
            data = requests.get(SUBMISSIONS.format(cik=cik[ticker]),
                                headers=HEADERS, timeout=30).json()
        except Exception:
            continue
        sector = sic_to_sector(str(data.get("sic") or ""))
        if sector:
            out[ticker] = {"sector": sector, "source": "sec_sic",
                           "sic": data.get("sic"), "sic_name": data.get("sicDescription")}
            filled += 1
        time.sleep(0.12)          # SEC asks for no more than 10 requests a second
    print(f"filled from SEC: {filled}")

    for ticker, sector in MANUAL.items():
        out.setdefault(ticker, {"sector": sector, "source": "manual"})
    for ticker, sector in ((v, k) for k, v in SECTOR_ETF.items()):
        out[ticker] = {"sector": sector, "source": "is_the_sector_etf"}

    payload = {"built_at": pd.Timestamp.utcnow().isoformat(),
               "sector_etf": SECTOR_ETF, "tickers": out}
    OUT.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    covered = sum(1 for t in kept if t in out)
    print(f"\ncovered {covered} of {len(kept)} tickers with bars")
    print(f"still unknown: {sorted(kept - set(out) - {'SPY','QQQ','IWM'})}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
