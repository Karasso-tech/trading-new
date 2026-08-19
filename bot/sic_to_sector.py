"""SIC code -> the 11 standard sectors (2026-08-04).

One standard, at the user's own direction:

    "we need one standard ... we have the main sector - XLK, XLV, XLI, XLF,
    XLY, XLC, XLP, XLE, XLU, XLRE, XLB and under them we have sub sectors like
    in the sec"

So: sector = one of the 11 SPDR/GICS sectors, industry = the SEC's own
description underneath it. Both come from the same free SEC filing, so there is
exactly one source and nothing to reconcile.

Honest caveat, stated once: **SIC is not GICS.** GICS is owned by S&P/MSCI and
costs money; the SEC publishes SIC for free. They agree on the obvious cases and
differ at the edges (a company filing as "retail-catalog" lands in Consumer
Discretionary here, which is where GICS puts Amazon too, but other edge cases
will not always match). This is a close, free approximation and is labelled as
such rather than presented as the real thing.

The mapping below is by SIC major group (the first two digits), which is the
level the SEC itself uses to organise industries.
"""

from __future__ import annotations

from typing import Optional

# The 11, with the ETF ticker the user recognises them by.
SECTORS = {
    "XLK": "Information Technology",
    "XLV": "Health Care",
    "XLF": "Financials",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "XLU": "Utilities",
    "XLC": "Communication Services",
}

# SIC major group (first two digits) -> sector ETF.
_MAJOR_GROUP = {
    # 01-09 agriculture, forestry, fishing
    "01": "XLP", "02": "XLP", "07": "XLP", "08": "XLB", "09": "XLP",
    # 10-14 mining
    "10": "XLB", "12": "XLE", "13": "XLE", "14": "XLB",
    # 15-17 construction
    "15": "XLI", "16": "XLI", "17": "XLI",
    # 20-39 manufacturing
    "20": "XLP",   # food
    "21": "XLP",   # tobacco
    "22": "XLY",   # textiles
    "23": "XLY",   # apparel
    "24": "XLB",   # lumber
    "25": "XLY",   # furniture
    "26": "XLB",   # paper
    "27": "XLC",   # printing/publishing
    "28": "XLV",   # chemicals + pharma -- split below by 4-digit
    "29": "XLE",   # petroleum refining
    "30": "XLB",   # rubber/plastics
    "31": "XLY",   # leather
    "32": "XLB",   # stone/clay/glass
    "33": "XLB",   # primary metals
    "34": "XLI",   # fabricated metal
    "35": "XLK",   # industrial + computer equipment -- split below
    "36": "XLK",   # electronics
    "37": "XLI",   # transport equipment
    "38": "XLV",   # instruments -- split below (medical vs industrial)
    "39": "XLY",   # misc manufacturing
    # 40-49 transport, communications, utilities
    "40": "XLI", "41": "XLI", "42": "XLI", "44": "XLI", "45": "XLI",
    "46": "XLE",   # pipelines
    "47": "XLI",
    "48": "XLC",   # communications
    "49": "XLU",   # electric/gas/sanitary
    # 50-51 wholesale
    "50": "XLI", "51": "XLP",
    # 52-59 retail
    "52": "XLY", "53": "XLY", "54": "XLP", "55": "XLY", "56": "XLY",
    "57": "XLY", "58": "XLY", "59": "XLY",
    # 60-67 finance, insurance, real estate
    "60": "XLF", "61": "XLF", "62": "XLF", "63": "XLF", "64": "XLF",
    "65": "XLRE", "67": "XLF",
    # 70-89 services
    "70": "XLY", "72": "XLY",
    "73": "XLK",   # business services incl. software -- split below
    "75": "XLY", "76": "XLY",
    "78": "XLC",   # motion pictures
    "79": "XLC",   # entertainment
    "80": "XLV",   # health services
    "82": "XLY", "83": "XLY", "86": "XLY",
    "87": "XLI",   # engineering/research
    "89": "XLI",
    # 91-99 public administration / non-classifiable
    "99": None,
}

# Exceptions where the 2-digit group is too coarse and lands a company in the
# wrong sector. Only added where the coarse answer is plainly wrong, never to
# hand-tune a result.
_EXACT = {
    "2833": "XLV", "2834": "XLV", "2835": "XLV", "2836": "XLV",   # pharma/biotech
    "2810": "XLB", "2820": "XLB", "2821": "XLB", "2860": "XLB",   # industrial chemicals
    "2870": "XLB", "2890": "XLB", "2891": "XLB",
    "2911": "XLE",                                                  # petroleum refining
    "3570": "XLK", "3571": "XLK", "3572": "XLK", "3576": "XLK",   # computers/networking
    "3578": "XLK", "3579": "XLK",
    "3559": "XLK", "3674": "XLK", "3672": "XLK",                   # semis + equipment
    # SIC 36 lumps semiconductors together with electrical equipment. GICS
    # splits them: chips are Technology, motors/generators/turbines are
    # Industrials. Caught on Bloom Energy, which was landing in Technology.
    "3612": "XLI", "3613": "XLI", "3620": "XLI", "3621": "XLI",
    "3630": "XLY", "3634": "XLY",                                   # home appliances
    "3510": "XLI", "3523": "XLI", "3531": "XLI", "3537": "XLI",   # real machinery
    "3550": "XLI", "3555": "XLI", "3561": "XLI", "3562": "XLI",
    "3826": "XLV", "3841": "XLV", "3842": "XLV", "3843": "XLV",   # medical instruments
    "3844": "XLV", "3845": "XLV", "3851": "XLV",
    "3812": "XLI", "3821": "XLI", "3823": "XLI", "3824": "XLI",   # industrial instruments
    "3825": "XLI", "3827": "XLI", "3829": "XLI",
    "7370": "XLK", "7371": "XLK", "7372": "XLK", "7373": "XLK",   # software/IT services
    "7374": "XLK", "7375": "XLK", "7377": "XLK", "7379": "XLK",
    "7311": "XLC", "7812": "XLC", "7819": "XLC", "7822": "XLC",   # advertising/media
    "7841": "XLC", "7900": "XLC", "7990": "XLC", "7997": "XLY",
    "5961": "XLY",                                                  # catalog/mail-order (AMZN)
    # 87 is "engineering, accounting, research" as a whole, which lands in
    # Industrials -- but 8731 is where biotechs file, and putting Incyte in
    # Industrials is plainly wrong. Caught by eyeballing the user's own real
    # holdings against the output, which is why that check is worth doing.
    "8731": "XLV", "8734": "XLV",
    "6798": "XLRE",                                                 # REITs
    "4813": "XLC", "4822": "XLC", "4832": "XLC", "4833": "XLC",   # telecom/broadcast
    "4841": "XLC", "4899": "XLC",
    "4911": "XLU", "4922": "XLU", "4923": "XLU", "4924": "XLU",   # utilities
    "4931": "XLU", "4932": "XLU", "4941": "XLU", "4991": "XLU",
}


def sector_etf_for_sic(sic: Optional[str]) -> Optional[str]:
    """ETF ticker for a SIC code, or None when the code is missing/unmappable.
    None is a real answer -- an ETF or a foreign issuer genuinely has no SIC --
    and is never replaced with a guess."""
    if not sic:
        return None
    sic = str(sic).strip().zfill(4)
    if sic in _EXACT:
        return _EXACT[sic]
    return _MAJOR_GROUP.get(sic[:2])


def sector_name_for_sic(sic: Optional[str]) -> Optional[str]:
    etf = sector_etf_for_sic(sic)
    return SECTORS.get(etf) if etf else None
