"""Point-in-time S&P 500 membership, rebuilt from Wikipedia.

Why this exists: a backtest run over TODAY's index members only ever sees the
companies that survived. Every name that was bought, went bankrupt or fell out
of the index is invisible, and those are exactly the losers. This module walks
the published change log backwards from today's list so we know, for any date,
who was actually in the index THAT day.

Two sources, both fetched fresh and saved to data/raw/ so any result can be
re-checked against the exact HTML it came from:
  * List_of_S&P_500_companies         -- today's members
  * Historical_components_of_the_S&P_500 -- every add/remove with its date

Output: data/universe/intervals.json -- one row per (ticker, membership spell)
with the first and last day that spell was in the index. A ticker that left and
came back gets two rows.

Sanity check the walk prints: the member count must stay near 500 all the way
back. If the change log had holes the count would drift, and it does not --
503 today, 507 in 2016.
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

CURRENT_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CHANGES_URL = "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500"
HEADERS = {"User-Agent": "stage0-research/1.0 (personal backtest)"}

# Yahoo writes class shares with a dash where Wikipedia writes a dot.
def norm(sym: str) -> str:
    return sym.strip().upper().replace(".", "-")


def fetch(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    dest.write_text(resp.text, encoding="utf-8")
    return dest


def build(as_of: str, since: str) -> list[dict]:
    """Walk the change log backwards from today's list.

    as_of -- the day today's member list is true for.
    since -- stop walking here; spells still open get start=since.
    """
    as_of_ts, since_ts = pd.Timestamp(as_of), pd.Timestamp(since)

    current = pd.read_html(RAW / "current.html")[0]
    members = {norm(s) for s in current["Symbol"]}
    print(f"members today: {len(members)}")

    changes = pd.read_html(RAW / "changes.html")[0]
    changes.columns = ["date", "add_t", "add_s", "rem_t", "rem_s", "reason", "refs"]
    changes = changes[changes["date"] != "Effective Date"].copy()
    changes["d"] = pd.to_datetime(changes["date"])
    changes = changes.sort_values("d", ascending=False)

    # open_until[t] = last day of the spell we are currently tracing for t
    open_until = {m: as_of_ts for m in members}
    spells: list[dict] = []
    by_year: dict[str, int] = {}

    for _, row in changes.iterrows():
        day = row["d"]
        if day < since_ts:
            break
        added, removed = row["add_t"], row["rem_t"]
        # Undo the change: whoever was added that day was NOT a member before it.
        if isinstance(added, str) and added.strip():
            t = norm(added)
            if t in members:
                members.discard(t)
                spells.append({"ticker": t, "start": day.date().isoformat(),
                               "end": open_until.pop(t).date().isoformat(),
                               "left_because": None})
        if isinstance(removed, str) and removed.strip():
            t = norm(removed)
            if t not in members:
                members.add(t)
                open_until[t] = day
        by_year[str(day.year)] = len(members)

    for t in members:
        spells.append({"ticker": t, "start": since, "end": open_until[t].date().isoformat(),
                       "left_because": None})

    print("member count walking back (must stay near 500):")
    for year in sorted(by_year, reverse=True):
        print(f"  {year}: {by_year[year]}")
    return spells


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--as-of", default=pd.Timestamp.today().date().isoformat(),
                    help="the day the current member list is true for")
    ap.add_argument("--since", default="2016-01-01", help="how far back to rebuild")
    ap.add_argument("--offline", action="store_true",
                    help="reuse the saved HTML instead of re-downloading")
    args = ap.parse_args()

    if not args.offline:
        fetch(CURRENT_URL, RAW / "current.html")
        fetch(CHANGES_URL, RAW / "changes.html")
        print(f"saved raw HTML to {RAW}")

    spells = build(args.as_of, args.since)
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "built_at": pd.Timestamp.utcnow().isoformat(),
        "as_of": args.as_of,
        "since": args.since,
        "source_current": CURRENT_URL,
        "source_changes": CHANGES_URL,
        "spells": sorted(spells, key=lambda s: (s["ticker"], s["start"])),
    }
    (OUT / "intervals.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tickers = sorted({s["ticker"] for s in spells})
    (OUT / "tickers.json").write_text(json.dumps(tickers, indent=0), encoding="utf-8")
    print(f"\nspells: {len(spells)}  distinct tickers: {len(tickers)}")
    print(f"wrote {OUT/'intervals.json'} and {OUT/'tickers.json'}")


if __name__ == "__main__":
    main()
