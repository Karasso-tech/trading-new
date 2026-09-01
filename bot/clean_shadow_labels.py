"""One-time cleanup of the labels already stored in the shadow book and the
ideas/thesis tables (2026-08-09).

The write path is locked from today (persistence._normalize_setup_type /
_normalize_decision), so nothing new can arrive dirty. This is the other half:
the rows written before the lock existed, which are the ones the first real
analysis will actually read.

What it fixes, all found in the live DB on 2026-08-09:

  * `setup_type` holding a paragraph instead of a label -- 4 distinct builds,
    8 shadow rows. Those rows can never be grouped with anything, and a group
    of one proves nothing. setup_types.canonical() recovers the real label from
    the prose ("V-reversal / capitulation reclaim ..." -> Reclaim).
  * `decision` arriving as both "Buy" (8 rows) and "Buy Now" (6) for the same
    call, so the strongest decision this system makes was split in two and each
    half looked too small to read.
  * `owner_bought` never populated, because the column did not exist until
    today. It is computable for every historical row that carries an idea_id.

What it deliberately does NOT touch:

  * Rows whose label is genuinely not a setup ("Legacy holding (backfilled
    ...)"). Guessing one would put invented rows into the research table --
    worse than an honest NULL.
  * The 93 shadow rows with no idea_id. They predate 2026-08-07 and there is no
    honest way to bind them to a build after the fact; a ticker match would
    guess, and a wrong join is worse than a missing one. They stay as they are
    and simply cannot be used for per-build questions.
  * Any price, R-multiple or simulated result. This script renames things. It
    never changes what happened.

Usage:
  python bot/clean_shadow_labels.py --dry-run   # print every change, write none
  python bot/clean_shadow_labels.py             # apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision_policy
import persistence
import setup_types


def _has_column(conn, table: str, column: str) -> bool:
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _plan_setup_type_fixes(conn, table: str) -> list[tuple]:
    """(rowid, old, new) for every row whose setup_type is recoverable but not
    already canonical. A row whose label cannot be resolved is reported by the
    caller and left alone."""
    fixes, unresolved = [], []
    key = "id" if table != "thesis" else "ticker"
    if not _has_column(conn, table, "setup_type"):
        # thesis keeps its setup type inside the primary_setup JSON blob, not in
        # a column of its own -- _fix_thesis_setup_json handles that one.
        return fixes, unresolved
    for row in conn.execute(f"SELECT {key} AS k, setup_type FROM {table} WHERE setup_type IS NOT NULL"):
        old = row["setup_type"]
        new = setup_types.canonical(old)
        if new is None:
            unresolved.append((row["k"], old))
        elif new != old:
            fixes.append((row["k"], old, new))
    return fixes, unresolved


def _plan_decision_fixes(conn, table: str) -> list[tuple]:
    fixes = []
    key = "id" if table != "thesis" else "ticker"
    if not _has_column(conn, table, "decision"):
        return fixes
    for row in conn.execute(f"SELECT {key} AS k, decision FROM {table} WHERE decision IS NOT NULL"):
        old = row["decision"]
        new = decision_policy.canonical_decision(old)
        if new is not None and new != old:
            fixes.append((row["k"], old, new))
    return fixes


def _fix_thesis_setup_json(conn, dry_run: bool) -> int:
    """thesis.primary_setup/alternate_setup are JSON blobs with their own `type`
    inside. The flattened ideas column and this blob must agree -- one fixed
    without the other is a new way for the same field to contradict itself."""
    changed = 0
    for row in conn.execute("SELECT ticker, primary_setup, alternate_setup FROM thesis"):
        updates = {}
        for field in ("primary_setup", "alternate_setup"):
            raw = row[field]
            if not raw:
                continue
            try:
                setup = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(setup, dict) or setup.get("type") is None:
                continue
            new = setup_types.canonical(setup["type"])
            if new is not None and new != setup["type"]:
                print(f"  thesis {row['ticker']}.{field}.type: {str(setup['type'])[:50]!r} -> {new!r}")
                setup["type"] = new
                updates[field] = json.dumps(setup, ensure_ascii=False)
        if updates and not dry_run:
            sets = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE thesis SET {sets} WHERE ticker=?",
                          (*updates.values(), row["ticker"]))
        changed += len(updates)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="print every change and write nothing")
    args = parser.parse_args()
    dry = args.dry_run
    if dry:
        print("DRY RUN -- nothing will be written\n")

    total = 0
    with persistence._db() as conn:
        for table in ("shadow_outcomes", "ideas", "thesis"):
            print(f"== {table} ==")
            key = "id" if table != "thesis" else "ticker"

            fixes, unresolved = _plan_setup_type_fixes(conn, table)
            for rowid, old, new in fixes:
                print(f"  setup_type [{rowid}]: {str(old)[:60]!r} -> {new!r}")
                if not dry:
                    conn.execute(f"UPDATE {table} SET setup_type=? WHERE {key}=?", (new, rowid))
            total += len(fixes)
            for rowid, old in unresolved:
                # Reported, never guessed -- see the module docstring.
                print(f"  LEFT ALONE [{rowid}]: not a setup at all -- {str(old)[:60]!r}")

            dfixes = _plan_decision_fixes(conn, table)
            for rowid, old, new in dfixes:
                print(f"  decision [{rowid}]: {old!r} -> {new!r}")
                if not dry:
                    conn.execute(f"UPDATE {table} SET decision=? WHERE {key}=?", (new, rowid))
            total += len(dfixes)
            print()

        print("== thesis setup JSON ==")
        total += _fix_thesis_setup_json(conn, dry)
        print()

        # owner_bought: computable for every historical row that has an idea_id.
        print("== shadow_outcomes.owner_bought backfill ==")
        rows = conn.execute(
            "SELECT id, idea_id FROM shadow_outcomes "
            "WHERE idea_id IS NOT NULL AND owner_bought IS NULL"
        ).fetchall()
        taken = {r["id"] for r in conn.execute("SELECT DISTINCT idea_id AS id FROM positions "
                                                "WHERE idea_id IS NOT NULL")}
        bought = sum(1 for r in rows if r["idea_id"] in taken)
        print(f"  {len(rows)} rows to set; {bought} of them were actually bought")
        if not dry:
            for r in rows:
                conn.execute("UPDATE shadow_outcomes SET owner_bought=? WHERE id=?",
                              (int(r["idea_id"] in taken), r["id"]))
        total += len(rows)

        no_idea = conn.execute(
            "SELECT COUNT(*) AS n FROM shadow_outcomes WHERE idea_id IS NULL"
        ).fetchone()["n"]
        if no_idea:
            print(f"  {no_idea} rows have no idea_id (pre-2026-08-07) -- left as they are, "
                  f"see the module docstring")

    print(f"\n{'would change' if dry else 'changed'}: {total} values")


if __name__ == "__main__":
    main()
