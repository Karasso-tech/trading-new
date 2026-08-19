"""Flat CSV export of every screener build, its simulated result, and its real result.

Why this exists
---------------
Research kept having to re-learn this project's SQLite schema before it could
ask a single question, and the answers it got were wrong in a way nothing
surfaced: `thesis` is keyed on ticker, so three separate XLF builds -- different
setup, trigger, stop and grade each time -- collapsed into one row that appeared
to change its mind nightly. Anything grouped by ticker mixed them together.

The `ideas` table (2026-08-07) fixed the storage side: one append-only row per
build, never overwritten. This script is the read side. One CSV row per build,
every column already flat, no JSON to dig through and no joins to get right:

  - what the screener said      (setup, grade, decision, regime, rejection reasons)
  - the plan as built           (trigger, stop, targets, ATR)
  - what the simulation did     (fired, entry, resolution, R, best/worst R, days held)
  - what really happened        (real fills, real exits, real R, commissions)

Two independent result blocks, deliberately side by side. The simulated columns
come from the nightly shadow book; the real columns come from actual fills. They
answer different questions and a study that conflates them is measuring nothing.
Both are NULL-safe: most builds have a simulated result and no real one.

Usage
-----
  python bot/export_ideas.py                          # -> _exports/ideas.csv
  python bot/export_ideas.py --out anywhere.csv
  python bot/export_ideas.py --since 2026-08-01        # builds from this date on
  python bot/export_ideas.py --ticker XLF              # one symbol's full build history
  python bot/export_ideas.py --numeric-trigger-only    # skip prose-trigger builds
  python bot/export_ideas.py --shadow-out shadow.csv   # also dump every nightly row

Nothing here fetches, writes, or mutates anything. It is safe to run at any time,
including while the scheduled jobs are working.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import persistence


# One row per build. Ordered for reading left-to-right: identity, what was said,
# the plan, the simulated outcome, the real outcome.
COLUMNS = [
    # identity
    "idea_id", "ticker", "seq", "built_at", "superseded_at", "is_live",
    "source", "sleeve", "status", "status_at_build", "drop_reason",
    # what the screener said
    "setup_type", "decision", "rubric_grade", "market_regime_at_build",
    "planned_qty", "rejection_reasons",
    # the plan as built
    "trigger", "trigger_text", "stop", "stop_text", "target_1", "target_2",
    "atr_at_build", "risk_per_share_planned", "rr_to_target_1",
    # what the simulation did (nightly shadow book, latest row for this build)
    "sim_checked_date", "sim_runs", "sim_fired", "sim_fired_date",
    "sim_entry", "sim_entry_date", "sim_entry_gap_pct", "sim_resolution",
    "sim_exit_date", "sim_exit_price", "sim_r_planned", "sim_r_simple",
    "sim_mfe_r", "sim_mae_r", "sim_bars_held", "sim_version",
    # what really happened
    "real_position_id", "real_entry_date", "real_entry_price", "real_qty",
    "real_entry_type", "real_initial_stop", "real_status",
    "real_exit_count", "real_exit_qty", "real_avg_exit_price", "real_r_multiple",
    "real_commissions", "real_override_of_decision",
]


def _rr(trigger: Optional[float], stop: Optional[float], target: Optional[float]) -> Optional[float]:
    """Reward-to-risk against the planned entry, or None when any leg is missing
    or the risk is not positive. Never returns a number built on a stop that is
    not below the trigger -- that is a broken plan, not a huge R:R."""
    if trigger is None or stop is None or target is None:
        return None
    risk = trigger - stop
    if risk <= 0:
        return None
    return round((target - trigger) / risk, 3)


def _fetch(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def build_rows(db_path: Optional[Path] = None, ticker: Optional[str] = None,
               since: Optional[str] = None, numeric_trigger_only: bool = False) -> list[dict]:
    """Every build as a flat dict, newest first. Pure read."""
    conn = sqlite3.connect(db_path or persistence.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM ideas WHERE 1=1"
        params: list = []
        if ticker:
            sql += " AND ticker=?"
            params.append(ticker.upper())
        if since:
            sql += " AND built_at >= ?"
            params.append(since)
        if numeric_trigger_only:
            sql += " AND trigger IS NOT NULL"
        sql += " ORDER BY built_at DESC, id DESC"
        ideas = _fetch(conn, sql, tuple(params))

        rows = []
        for idea in ideas:
            row = {c: None for c in COLUMNS}
            row.update({
                "idea_id": idea["id"], "ticker": idea["ticker"], "seq": idea["seq"],
                "built_at": idea["built_at"], "superseded_at": idea["superseded_at"],
                "is_live": 1 if idea["superseded_at"] is None else 0,
                "source": idea["source"], "sleeve": idea["sleeve"], "status": idea["status"],
                "status_at_build": idea["status_at_build"], "drop_reason": idea["drop_reason"],
                "setup_type": idea["setup_type"], "decision": idea["decision"],
                "rubric_grade": idea["rubric_grade"],
                "market_regime_at_build": idea["market_regime_at_build"],
                "planned_qty": idea["planned_qty"],
                "rejection_reasons": _join_reasons(idea["rejection_reasons"]),
                "trigger": idea["trigger"], "trigger_text": idea["trigger_text"],
                "stop": idea["stop"], "stop_text": idea["stop_text"],
                "target_1": idea["target_1"], "target_2": idea["target_2"],
                "atr_at_build": idea["atr_at_build"],
            })
            if idea["trigger"] is not None and idea["stop"] is not None:
                risk = idea["trigger"] - idea["stop"]
                row["risk_per_share_planned"] = round(risk, 6) if risk > 0 else None
            row["rr_to_target_1"] = _rr(idea["trigger"], idea["stop"], idea["target_1"])

            _add_sim(conn, idea["id"], row)
            _add_real(conn, idea["id"], row)
            rows.append(row)
        return rows
    finally:
        conn.close()


def _join_reasons(raw: Optional[str]) -> Optional[str]:
    """Stored as a JSON list; flattened to "a; b" so a spreadsheet filter works
    on it. Left verbatim if it will not parse -- never silently blanked."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    return "; ".join(str(v) for v in value) if isinstance(value, list) else str(value)


def _add_sim(conn: sqlite3.Connection, idea_id: int, row: dict) -> None:
    """The shadow book's LATEST simulated result for this build, plus how many
    nights it was scored. Latest, not first: an open trade's numbers keep
    developing, so the newest row is the most complete answer for that build.
    Every intermediate night stays available via --shadow-out."""
    sim = conn.execute(
        "SELECT * FROM shadow_outcomes WHERE idea_id=? ORDER BY checked_date DESC, id DESC LIMIT 1",
        (idea_id,),
    ).fetchone()
    runs = conn.execute(
        "SELECT COUNT(*) c FROM shadow_outcomes WHERE idea_id=?", (idea_id,)
    ).fetchone()["c"]
    row["sim_runs"] = runs
    if not sim:
        return
    row.update({
        "sim_checked_date": sim["checked_date"],
        "sim_fired": sim["hypothetical_trigger_fired"],
        "sim_fired_date": sim["fired_date"], "sim_entry": sim["entry"],
        "sim_entry_date": sim["entry_date"], "sim_entry_gap_pct": sim["entry_gap_pct"],
        "sim_resolution": sim["resolution"], "sim_exit_date": sim["exit_date"],
        "sim_exit_price": sim["exit_price"], "sim_r_planned": sim["r_multiple_planned"],
        "sim_r_simple": sim["r_multiple_simple"], "sim_mfe_r": sim["mfe_r"],
        "sim_mae_r": sim["mae_r"], "sim_bars_held": sim["bars_held"],
        "sim_version": sim["sim_version"],
    })


def _add_real(conn: sqlite3.Connection, idea_id: int, row: dict) -> None:
    """The real fill taken against this build, if there was one, with its exits
    rolled up. Qty-weighted average R across exit tranches, matching how
    closing_summaries already computes it -- a two-target exit plus a runner
    stop-out is one trade, not three."""
    pos = conn.execute(
        "SELECT * FROM positions WHERE idea_id=? ORDER BY entry_date LIMIT 1", (idea_id,)
    ).fetchone()
    if not pos:
        return
    row.update({
        "real_position_id": pos["id"], "real_entry_date": pos["entry_date"],
        "real_entry_price": pos["entry_price"], "real_qty": pos["qty"],
        "real_entry_type": pos["entry_type"], "real_initial_stop": pos["initial_stop"],
        "real_status": pos["status"],
        "real_override_of_decision": pos["override_of_decision"],
    })
    exits = conn.execute("SELECT * FROM exits WHERE position_id=?", (pos["id"],)).fetchall()
    if not exits:
        row["real_exit_count"] = 0
        return
    total_qty = sum(e["exit_qty"] for e in exits)
    row["real_exit_count"] = len(exits)
    row["real_exit_qty"] = total_qty
    if total_qty:
        row["real_avg_exit_price"] = round(
            sum(e["exit_price"] * e["exit_qty"] for e in exits) / total_qty, 6)
    # Only tranches that actually have an R contribute to the weighted average --
    # a leg with no known original-risk stop has no honest R and must not be
    # treated as 0.0 (see the exits.r_multiple schema comment).
    scored = [e for e in exits if e["r_multiple"] is not None]
    scored_qty = sum(e["exit_qty"] for e in scored)
    if scored_qty:
        row["real_r_multiple"] = round(
            sum(e["r_multiple"] * e["exit_qty"] for e in scored) / scored_qty, 4)
    commissions = [e["commission"] for e in exits if e["commission"] is not None]
    entry_commission = pos["entry_commission"]
    if commissions or entry_commission is not None:
        row["real_commissions"] = round(sum(commissions) + (entry_commission or 0), 4)


def export_shadow_rows(conn_path: Path, out: Path, ticker: Optional[str] = None) -> int:
    """Every nightly shadow row, one per (build, date), so the way a simulated
    trade developed night by night stays inspectable. The main CSV keeps only
    the latest row per build."""
    conn = sqlite3.connect(conn_path)
    conn.row_factory = sqlite3.Row
    try:
        sql = ("SELECT s.*, i.seq AS idea_seq, i.built_at AS idea_built_at "
               "FROM shadow_outcomes s LEFT JOIN ideas i ON i.id = s.idea_id")
        params: tuple = ()
        if ticker:
            sql += " WHERE s.ticker=?"
            params = (ticker.upper(),)
        sql += " ORDER BY s.ticker, s.checked_date, s.id"
        rows = [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()
    if not rows:
        return 0
    _write(out, rows, list(rows[0].keys()))
    return len(rows)


def _write(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: these files get opened in Excel, which reads plain UTF-8 as
    # mojibake. Several stored setups are Hebrew, so this is not hypothetical.
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=None,
                        help="output CSV path (default: _exports/ideas.csv)")
    parser.add_argument("--ticker", default=None, help="one symbol's full build history")
    parser.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                        help="only builds made on/after this date")
    parser.add_argument("--numeric-trigger-only", action="store_true",
                        help="skip builds whose trigger was written as prose -- those "
                             "cannot fire, so a study needing an entry level should "
                             "exclude them on purpose rather than read NULL as 'never fired'")
    parser.add_argument("--shadow-out", default=None, metavar="PATH",
                        help="also write every nightly shadow row (one per build per date)")
    args = parser.parse_args()

    root = Path(persistence.DB_PATH).parent
    out = Path(args.out) if args.out else root / "_exports" / "ideas.csv"

    rows = build_rows(ticker=args.ticker, since=args.since,
                      numeric_trigger_only=args.numeric_trigger_only)
    if not rows:
        print("no builds matched -- nothing exported")
        return 1
    _write(out, rows, COLUMNS)

    live = sum(1 for r in rows if r["is_live"])
    with_sim = sum(1 for r in rows if r["sim_resolution"])
    with_real = sum(1 for r in rows if r["real_position_id"])
    prose = sum(1 for r in rows if r["trigger"] is None)
    print(f"{len(rows)} builds -> {out}")
    print(f"  live now: {live}   superseded: {len(rows) - live}")
    print(f"  with a simulated result: {with_sim}   with a real fill: {with_real}")
    if prose:
        print(f"  {prose} build(s) have a prose trigger and cannot be simulated "
              f"(see trigger_text)")

    if args.shadow_out:
        n = export_shadow_rows(Path(persistence.DB_PATH), Path(args.shadow_out), args.ticker)
        print(f"{n} nightly shadow rows -> {args.shadow_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
