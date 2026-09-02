"""Collect every historical trigger, day by day, and write them all down.

This is the day-by-day replay the owner asked for on 2026-09-01. It is a
COLLECTOR, not a strategy test. Nothing here is filtered for looking good: every
idea is saved -- the winners, the losers, the ugly ones, the ones in a rising
market and the ones in a falling one, in quiet stretches and violent ones.

The day, in order, and the order is the whole point:

  MORNING, before the open
      If there is no idea waiting on this stock, build one from bars up to and
      including YESTERDAY's close. Save it either way -- a setup that cannot be
      traded is saved with the reason it cannot.

  EVENING, after the close
      1. A trade that is open gets today's bar checked against its stop and its
         target.
      2. An idea that is waiting gets today's CLOSE compared to its trigger.
         Closed above it -> the trigger fired, and the entry is that same close.
         Not above it -> ask whether the idea is still true. If it is not, it is
         retired with its reason and a new one is built tomorrow.
      3. Every retired idea keeps being watched. If its trigger fires later, the
         day it fired is recorded. That is how "how many ideas did we throw away
         that would have worked" becomes a number instead of an opinion.

Rules the owner fixed, so nobody has to guess later:
  * Entry is the close of the day that closed above the trigger. Not the next
    open, not the trigger price.
  * A trade ends at target 1 or at the stop, whichever comes first. What happens
    after target 1 is a separate study and is deliberately not modelled here.
  * When the same daily bar touches both the stop and the target, the STOP wins
    -- a daily bar cannot say which came first, and taking the better of the two
    would be lying to ourselves every time it happened. The day is also FLAGGED,
    and those trades get their own name.
  * One trade at a time per stock. Ideas keep being built and saved while a
    trade is open; they simply cannot become a second trade. They are marked so
    they can be counted in or out later.

Four names for how a trade ended:
    success  -- reached target 1
    failed   -- stopped out before target 1
    doubt    -- target and stop on the same bar
    open     -- the study ended first. A fact, not a judgment.

Look-ahead: the only place a future bar could get in is the `end` passed to
`setups.build`, and it is always `i` -- the index of the day being decided, so
the newest bar visible is `i - 1`. Today's close is read once, in the evening
block, and only to compare against a trigger that already existed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import setups
from context import Context
from barfile import bar_path
from params import DEFAULT, Params

ROOT = Path(__file__).resolve().parent
BARS = ROOT / "data" / "bars"
UNIVERSE = ROOT / "data" / "universe" / "intervals.json"
UNIVERSE_EXTRA = ROOT / "data" / "universe" / "intervals_mid_small.json"
DB = ROOT / "data" / "stage0.db"

SUCCESS, FAILED, DOUBT, OPEN = "success", "failed", "doubt", "open"


SCHEMA = """
CREATE TABLE IF NOT EXISTS ideas (
    id                 INTEGER PRIMARY KEY,
    run_id             INTEGER NOT NULL,
    ticker             TEXT NOT NULL,
    seq                INTEGER NOT NULL,   -- version number for this stock
    built_for          TEXT NOT NULL,      -- the session this idea was meant for
    known_as_of        TEXT,               -- newest close it was built from;
                                           -- NULL on a ticker's very first bar
    close_at_build     REAL,
    setup_type         TEXT,
    confidence         TEXT,
    trigger            REAL,
    trigger_basis      TEXT,
    stop               REAL,
    stop_basis_level   REAL,
    stop_basis_kind    TEXT,
    stop_distance_atr  REAL,
    target_1           REAL,
    target_1_source    TEXT,
    target_1_rr        REAL,
    target_1_atr       REAL,
    atr                REAL,
    sma20              REAL,
    sma50              REAL,
    tradeable          INTEGER NOT NULL,
    rejected_because   TEXT,
    spy_above_sma200   INTEGER,            -- market context, saved never used as a gate
    spy_return_20d_pct REAL,
    while_in_trade     INTEGER NOT NULL,   -- built while this stock already had a trade open
    outcome            TEXT NOT NULL,      -- pending|fired|fired_not_taken|retired|unfinished
    retired_on         TEXT,
    retired_because    TEXT,
    superseded_by      INTEGER,
    fired_on           TEXT,               -- filled in even for retired ideas
    days_to_fire       INTEGER,
    follow_days        INTEGER,            -- sessions it was actually watched for
    params             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ideas_ticker ON ideas(ticker, built_for);
CREATE INDEX IF NOT EXISTS ideas_outcome ON ideas(outcome);

CREATE TABLE IF NOT EXISTS trades (
    id                 INTEGER PRIMARY KEY,
    run_id             INTEGER NOT NULL,
    idea_id            INTEGER NOT NULL,
    ticker             TEXT NOT NULL,
    setup_type         TEXT NOT NULL,
    entry_date         TEXT NOT NULL,
    entry_price        REAL NOT NULL,
    trigger            REAL NOT NULL,
    entry_premium_atr  REAL,               -- how far above the trigger the close was
    stop               REAL NOT NULL,
    target_1           REAL NOT NULL,
    risk_per_share     REAL NOT NULL,      -- entry - stop, the real risk taken
    planned_risk       REAL NOT NULL,      -- trigger - stop, the risk as planned
    outcome            TEXT NOT NULL,
    same_bar_hit       INTEGER NOT NULL,
    target_already_hit INTEGER NOT NULL,   -- the entry close was already past target 1
    exit_date          TEXT,
    exit_price         REAL,
    days_held          INTEGER,
    r_multiple         REAL,
    mfe_r              REAL,               -- best it got, in R, before it ended
    mae_r              REAL,
    spy_above_sma200   INTEGER,
    spy_return_20d_pct REAL
);
CREATE INDEX IF NOT EXISTS trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS trades_outcome ON trades(outcome);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    tickers     INTEGER,
    params      TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    note        TEXT
);
"""


def load_membership(start: str, end: str,
                    universes: tuple[str, ...] = ("sp500", "sp400", "sp600")
                    ) -> dict[str, list[tuple[str, str]]]:
    """When each ticker was actually in an index, clipped to the study window.

    Three indexes now, in two files. A stock that moved between them (small cap
    promoted to mid cap, say) simply has two spells and is eligible across both.

    The S&P 600 spells are already clipped at 2019-12-17 in their own file --
    Wikipedia's small-cap change log begins there, so earlier membership cannot
    be reconstructed and is not claimed.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    sources = []
    if "sp500" in universes and UNIVERSE.exists():
        sources.append((UNIVERSE, "sp500"))
    if UNIVERSE_EXTRA.exists() and ({"sp400", "sp600"} & set(universes)):
        sources.append((UNIVERSE_EXTRA, None))
    for path, default_index in sources:
        for spell in json.loads(path.read_text(encoding="utf-8"))["spells"]:
            if (spell.get("index") or default_index) not in universes:
                continue
            lo, hi = max(spell["start"], start), min(spell["end"], end)
            if lo <= hi:
                out.setdefault(spell["ticker"], []).append((lo, hi))
    return out


def market_context(p: Params) -> dict[str, tuple[Optional[int], Optional[float]]]:
    """Two raw numbers about SPY per session, as of the PREVIOUS close.

    Saved, never used to allow or block anything. The plan is explicit that
    market state is information until the data says otherwise, so no label is
    attached here -- no "healthy" or "risk off", just the numbers, so any cut
    can be made later without this file having pre-judged it.
    """
    path = BARS / "SPY.json"
    if not path.exists():
        return {}
    bars = json.loads(path.read_text(encoding="utf-8"))["bars"]
    closes = [b["close"] for b in bars]
    out = {}
    for i, bar in enumerate(bars):
        above = ret20 = None
        if i >= 200:
            above = int(closes[i - 1] > sum(closes[i - 201:i - 1]) / 200)
        if i >= 21:
            out_prev = closes[i - 21]
            ret20 = (closes[i - 1] - out_prev) / out_prev * 100 if out_prev else None
        out[bar["date"]] = (above, ret20)
    return out


def run_ticker(ticker: str, bars: list[dict], spells: list[tuple[str, str]],
               market: dict, p: Params) -> tuple[list[dict], list[dict]]:
    """Replay one stock. Returns (ideas, trades) as plain dicts."""
    ctx = Context(bars, p)
    in_index = lambda d: any(lo <= d <= hi for lo, hi in spells)

    ideas: list[dict] = []
    trades: list[dict] = []
    pending: Optional[dict] = None      # the row dict of the idea waiting on its trigger
    watching: list[dict] = []           # retired ideas still being followed
    trade: Optional[dict] = None
    seq = 0

    # The last session inside the study window -- orphan follow-up stops here, so
    # the sealed years are never read, not even to answer "did it fire later".
    last_i = max((i for i, b in enumerate(bars) if in_index(b["date"])), default=None)
    if last_i is None:
        return ideas, trades

    for i, bar in enumerate(bars):
        date = bar["date"]
        if not in_index(date):
            continue

        spy_above, spy_ret = market.get(date, (None, None))

        # --- MORNING: build an idea if none is waiting -----------------------
        if pending is None:
            idea = setups.build(ctx, i, ticker, p)
            seq += 1
            row = {
                "ticker": ticker, "seq": seq, "built_for": date,
                "known_as_of": idea.known_as_of, "close_at_build": idea.close_at_build,
                "setup_type": idea.setup_type, "confidence": idea.confidence,
                "trigger": idea.trigger, "trigger_basis": idea.trigger_basis,
                "stop": idea.stop, "stop_basis_level": idea.stop_basis_level,
                "stop_basis_kind": idea.stop_basis_kind,
                "stop_distance_atr": idea.stop_distance_atr,
                "target_1": idea.target_1, "target_1_source": idea.target_1_source,
                "target_1_rr": idea.target_1_rr, "target_1_atr": idea.target_1_atr,
                "atr": idea.atr, "sma20": idea.sma_fast, "sma50": idea.sma_slow,
                "tradeable": int(idea.tradeable), "rejected_because": idea.rejected_because,
                "spy_above_sma200": spy_above, "spy_return_20d_pct": spy_ret,
                "while_in_trade": int(trade is not None),
                "outcome": "pending" if idea.tradeable else "retired",
                "retired_on": None if idea.tradeable else date,
                "retired_because": None if idea.tradeable else idea.rejected_because,
                "superseded_by": None, "fired_on": None, "days_to_fire": None,
                "follow_days": 0, "params": p.fingerprint(),
                "_i": i, "_idea": idea, "_row_index": len(ideas),
            }
            ideas.append(row)
            if idea.tradeable:
                pending = row

        # --- EVENING 1: an open trade meets today's bar ----------------------
        if trade is not None and i > trade["_entry_i"]:
            hit_stop = bar["low"] <= trade["stop"]
            hit_target = bar["high"] >= trade["target_1"]
            risk = trade["risk_per_share"]
            trade["mfe_r"] = max(trade["mfe_r"], (bar["high"] - trade["entry_price"]) / risk)
            trade["mae_r"] = min(trade["mae_r"], (bar["low"] - trade["entry_price"]) / risk)
            if hit_stop or hit_target:
                if hit_stop and hit_target:
                    trade["outcome"], trade["same_bar_hit"] = DOUBT, 1
                    exit_price = trade["stop"]
                elif hit_stop:
                    trade["outcome"], exit_price = FAILED, trade["stop"]
                else:
                    trade["outcome"], exit_price = SUCCESS, trade["target_1"]
                trade["exit_date"] = date
                trade["exit_price"] = exit_price
                trade["days_held"] = i - trade["_entry_i"]
                trade["r_multiple"] = (exit_price - trade["entry_price"]) / risk
                # MFE/MAE stop at the exit bar and never read a later one.
                trades.append(trade)
                trade = None

        # --- EVENING 2: does the waiting idea's trigger fire? ----------------
        if pending is not None:
            fired = bar["close"] > pending["trigger"]
            if fired:
                pending["fired_on"] = date
                pending["days_to_fire"] = i - pending["_i"]
                if trade is not None:
                    # Rule: one trade at a time per stock. The trigger really did
                    # fire and that is recorded -- it simply was not taken.
                    pending["outcome"] = "fired_not_taken"
                else:
                    pending["outcome"] = "fired"
                    risk = bar["close"] - pending["stop"]
                    if risk > 0:
                        trade = {
                            "_entry_i": i, "_idea_row": pending["_row_index"],
                            "ticker": ticker, "setup_type": pending["setup_type"],
                            "entry_date": date, "entry_price": bar["close"],
                            "trigger": pending["trigger"],
                            "entry_premium_atr": ((bar["close"] - pending["trigger"])
                                                  / pending["atr"]) if pending["atr"] else None,
                            "stop": pending["stop"], "target_1": pending["target_1"],
                            "risk_per_share": risk,
                            "planned_risk": pending["trigger"] - pending["stop"],
                            "outcome": OPEN, "same_bar_hit": 0,
                            "target_already_hit": int(bar["close"] >= pending["target_1"]),
                            "exit_date": None, "exit_price": None, "days_held": None,
                            "r_multiple": None, "mfe_r": 0.0, "mae_r": 0.0,
                            "spy_above_sma200": spy_above, "spy_return_20d_pct": spy_ret,
                        }
                    else:
                        # The close cleared the trigger but landed at or under the
                        # stop. There is no trade to take; recorded, not skipped.
                        pending["outcome"] = "fired_not_taken"
                        pending["retired_because"] = ("the close that cleared the trigger was "
                                                      "already at or below the stop")
                pending = None
            else:
                ok, why = setups.still_valid(pending["_idea"], ctx, i + 1, p)
                if not ok:
                    pending["outcome"] = "retired"
                    pending["retired_on"] = date
                    pending["retired_because"] = why
                    watching.append(pending)
                    pending = None

        # --- EVENING 3: retired ideas are still watched ----------------------
        if watching:
            still = []
            for row in watching:
                row["follow_days"] += 1
                if bar["close"] > row["trigger"]:
                    row["fired_on"] = date
                    row["days_to_fire"] = i - row["_i"]
                else:
                    still.append(row)
            watching = still

    # A trade still running when the window closed is not a missing trade. It
    # gets written with outcome `open` and no exit -- the honest record of a
    # position the study ran out of days to finish.
    if trade is not None:
        trade["days_held"] = last_i - trade["_entry_i"]
        trades.append(trade)

    # An idea still waiting when the study ended never got its answer. Saying
    # "never fired" about it would be a lie -- it simply ran out of days.
    if pending is not None:
        pending["outcome"] = "unfinished"
    return ideas, trades


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2024-12-31",
                    help="everything after this stays sealed for a later test")
    ap.add_argument("--tickers", nargs="*", help="just these, for a smoke run")
    ap.add_argument("--note", default="")
    ap.add_argument("--universes", nargs="*", default=["sp500", "sp400", "sp600"],
                    help="which indexes to include")
    ap.add_argument("--resume", type=int, metavar="RUN_ID",
                    help="continue a run that stopped part-way, skipping tickers already stored")
    ap.add_argument("--verbose", action="store_true",
                    help="name every ticker as it is written, so a stall can be located")
    ap.add_argument("--limit", type=int,
                    help="stop after this many tickers and exit cleanly; with --resume this "
                         "lets one long run be done as a series of short ones")
    args = ap.parse_args()

    p = DEFAULT
    membership = load_membership(args.start, args.end, tuple(args.universes))
    market = market_context(p)
    wanted = [t.upper() for t in args.tickers] if args.tickers else sorted(membership)
    # Only tickers that actually have bars. A universe entry with no bar file was
    # dropped at download time with its reason recorded, and counting it here made
    # --limit meaningless: a pass could spend its whole budget skipping files that
    # do not exist and process nothing.
    wanted = [t for t in wanted if bar_path(BARS, t).exists()]

    DB.parent.mkdir(parents=True, exist_ok=True)
    # A long run used to be all-or-nothing: a stall meant throwing the work away
    # and starting over. `timeout` makes a lock wait fail loudly instead of
    # hanging forever, and --resume lets a stopped run pick up where it stopped.
    conn = sqlite3.connect(DB, timeout=30)
    conn.executescript(SCHEMA)
    if args.resume:
        run_id = args.resume
        already = {r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM ideas WHERE run_id = ?", (run_id,))}
        wanted = [t for t in wanted if t not in already]
        print(f"resuming run {run_id}: {len(already):,} tickers already stored, "
              f"{len(wanted):,} to go")
    else:
        cur = conn.execute(
            "INSERT INTO runs (started_at, start_date, end_date, tickers, params, fingerprint, note)"
            " VALUES (datetime('now'), ?, ?, ?, ?, ?, ?)",
            (args.start, args.end, len(wanted), json.dumps(p.as_dict()), p.fingerprint(), args.note))
        run_id = cur.lastrowid
    if args.limit:
        wanted = wanted[:args.limit]
        print(f"this pass will do {len(wanted)} of them")
    conn.commit()

    idea_cols = [c for c in SCHEMA.split("CREATE TABLE IF NOT EXISTS ideas (")[1]
                 .split(");")[0].splitlines() if c.strip() and not c.strip().startswith("--")]
    idea_fields = [c.strip().split()[0] for c in idea_cols][1:]   # drop `id`
    trade_cols = [c for c in SCHEMA.split("CREATE TABLE IF NOT EXISTS trades (")[1]
                  .split(");")[0].splitlines() if c.strip() and not c.strip().startswith("--")]
    trade_fields = [c.strip().split()[0] for c in trade_cols][1:]

    done = kept_ideas = kept_trades = 0
    for ticker in wanted:
        path = bar_path(BARS, ticker)
        if not path.exists():
            continue                      # dropped at download time, with its reason
        bars = json.loads(path.read_text(encoding="utf-8"))["bars"]
        ideas, trades = run_ticker(ticker, bars, membership[ticker], market, p)

        # Ideas first, so a trade can point at the row id of the idea that made it.
        ids = []
        for row in ideas:
            row["run_id"] = run_id
            values = [row.get(f) for f in idea_fields]
            c = conn.execute(f"INSERT INTO ideas ({','.join(idea_fields)}) "
                             f"VALUES ({','.join('?' * len(idea_fields))})", values)
            ids.append(c.lastrowid)
        for tr in trades:
            tr["run_id"] = run_id
            tr["idea_id"] = ids[tr["_idea_row"]]
            values = [tr.get(f) for f in trade_fields]
            conn.execute(f"INSERT INTO trades ({','.join(trade_fields)}) "
                         f"VALUES ({','.join('?' * len(trade_fields))})", values)
        kept_ideas += len(ideas)
        kept_trades += len(trades)
        done += 1
        # Commit every ticker rather than every 25. The transaction stays small,
        # a stall can only ever cost one ticker's work, and --resume has a clean
        # boundary to restart from.
        conn.commit()
        if args.verbose:
            print(f"  {done}/{len(wanted)} {ticker}", flush=True)
        elif done % 25 == 0:
            print(f"  {done}/{len(wanted)}  ideas={kept_ideas:,}  trades={kept_trades:,}",
                  flush=True)
    conn.commit()
    conn.close()
    print(f"\nrun {run_id}: {done} tickers, {kept_ideas:,} ideas, {kept_trades:,} trades")
    print(f"window {args.start} .. {args.end}   params {p.fingerprint()}")
    print(f"written to {DB}")


if __name__ == "__main__":
    main()
