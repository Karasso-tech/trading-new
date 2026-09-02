"""Point-in-time membership for the S&P MidCap 400 and SmallCap 600.

Same method as universe.py: take today's published list and walk Wikipedia's
change log backwards. Written as a separate file rather than folded into
universe.py so the S&P 500 universe, which every existing result depends on,
cannot be disturbed by work on this one.

How complete the two logs are, measured the same way -- if the log had holes the
member count would drift as we walk back, and it barely does:

    S&P 400   400 today  ->  409 in 2019   (drift +9 over seven years, ~2%)
    S&P 600   603 today  ->  617 in 2020   (drift +14 over five years, ~2%)

Both are usable. The S&P 500's own drift is +4, so these are slightly rougher
but the same order.

**One hard limit, and it is not a rounding error.** The S&P 600 change log on
Wikipedia starts on 2019-12-17. Before that date there is nothing to walk back
through, so the December 2019 membership is the earliest honest answer for that
index. Rather than pretend, every 600 spell is clipped at that date and
`reliable_from` records it. Any study that includes small caps before 2020 is
using today's survivors and will read better than reality.

Yahoo coverage for the names that left, sampled: 40% have usable bars, against
44% for the S&P 500's departures. The survivorship hole is the same size here,
not worse.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "universe"
HEADERS = {"User-Agent": "stage0-research/1.0 (personal backtest)"}

INDEXES = {
    "sp400": {
        "url": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
        "size": 400,
        # The change log reaches back to 2012, comfortably before any study window.
        "reliable_from": "1900-01-01",
    },
    "sp600": {
        "url": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
        "size": 600,
        # Wikipedia's log begins here. Nothing earlier can be reconstructed.
        "reliable_from": "2019-12-17",
    },
}


def norm(symbol) -> str:
    return str(symbol).strip().upper().replace(".", "-")


def fetch(name: str, url: str) -> Path:
    dest = RAW / f"{name}.html"
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    dest.write_text(resp.text, encoding="utf-8")
    return dest


def build(name: str, spec: dict, as_of: str, since: str) -> list[dict]:
    tables = pd.read_html(RAW / f"{name}.html")
    current, changes = tables[0], tables[1]
    changes.columns = ["date", "add_t", "add_s", "rem_t", "rem_s", "reason"]
    changes = changes[changes["date"] != "Date"].copy()
    changes["d"] = pd.to_datetime(changes["date"], errors="coerce")
    unparsed = int(changes["d"].isna().sum())
    changes = changes.dropna(subset=["d"]).sort_values("d", ascending=False)

    floor = max(pd.Timestamp(since), pd.Timestamp(spec["reliable_from"]))
    as_of_ts = pd.Timestamp(as_of)

    members = {norm(s) for s in current["Symbol"]}
    open_until = {m: as_of_ts for m in members}
    spells: list[dict] = []
    by_year: dict[str, int] = {}

    for _, row in changes.iterrows():
        day = row["d"]
        if day < floor:
            break
        added, removed = row["add_t"], row["rem_t"]
        if isinstance(added, str) and added.strip():
            t = norm(added)
            if t in members:
                members.discard(t)
                spells.append({"ticker": t, "index": name,
                               "start": day.date().isoformat(),
                               "end": open_until.pop(t).date().isoformat()})
        if isinstance(removed, str) and removed.strip():
            t = norm(removed)
            if t not in members:
                members.add(t)
                open_until[t] = day
        by_year[str(day.year)] = len(members)

    for t in members:
        spells.append({"ticker": t, "index": name,
                       "start": floor.date().isoformat(),
                       "end": open_until[t].date().isoformat()})

    print(f"\n{name}: {len(current)} today, {len(changes)} usable changes "
          f"({unparsed} dates unreadable), reliable from {floor.date()}")
    print(f"  member count walking back (target {spec['size']}):")
    for year in sorted(by_year, reverse=True)[:8]:
        print(f"    {year}: {by_year[year]:>4}   drift {by_year[year] - spec['size']:+d}")
    return spells


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--as-of", default=pd.Timestamp.today().date().isoformat())
    ap.add_argument("--since", default="2019-01-01")
    ap.add_argument("--offline", action="store_true", help="reuse the saved HTML")
    args = ap.parse_args()

    spells: list[dict] = []
    for name, spec in INDEXES.items():
        if not args.offline:
            fetch(name, spec["url"])
        spells += build(name, spec, args.as_of, args.since)

    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "built_at": pd.Timestamp.utcnow().isoformat(),
        "as_of": args.as_of,
        "since": args.since,
        "indexes": {k: {"url": v["url"], "reliable_from": v["reliable_from"]}
                    for k, v in INDEXES.items()},
        "warning": ("The S&P 600 change log starts 2019-12-17. Small-cap membership "
                    "before that date cannot be reconstructed and is not claimed here."),
        "spells": sorted(spells, key=lambda s: (s["ticker"], s["start"])),
    }
    (OUT / "intervals_mid_small.json").write_text(json.dumps(payload, indent=1),
                                                  encoding="utf-8")
    tickers = sorted({s["ticker"] for s in spells})
    (OUT / "tickers_mid_small.json").write_text(json.dumps(tickers, indent=0),
                                                encoding="utf-8")
    print(f"\nspells: {len(spells)}   distinct tickers: {len(tickers)}")
    print(f"wrote {OUT / 'intervals_mid_small.json'}")


if __name__ == "__main__":
    main()
