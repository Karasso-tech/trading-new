"""A5/A6: the real persistence layer this system needs.

Two things live in one SQLite database (trading_new.db, project root):

1. A5 -- message queue state machine (rebuilt 2026-07-09, see below). received ->
   processing -> sent/failed, with a message stuck in "processing" past a timeout
   reclaimed and retried, not lost.

2. A6 -- persistent thesis + sleeve + trade-journal storage. Without this, a /monitor
   check run in a *separate* Claude Code session days later has no way to know what a
   /screener run decided earlier -- MONITOR_v2.md explicitly depends on referencing a
   *saved* trigger, not re-deriving one.

sleeve.py's get_sleeve/set_sleeve delegate here.

2026-07-09 rebuild note: the original Telegram delivery layer (ack_listener.py,
check_telegram.py, telegram_send.py, and this file's message-queue code) was deleted
after real, repeated reliability failures -- messages sitting unprocessed for minutes
to hours (one 12.68-hour gap overnight), traced to a fixed-interval polling/cron
mechanism that (a) only fires while idle and (b) is financially non-viable at any
useful interval (~$0.06-0.17 per invocation even fully cached, meaning even 5-minute
polling would burn a $20/month budget in about a day). The rebuilt design below is
event-driven instead: ack_listener.py triggers bot/process_queue.py immediately after
enqueueing a real message, never on a timer. The A6 trading-data tables/functions
below were never affected by any of this -- only the queue layer was rebuilt.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas_market_calendars as mcal

import decision_policy
import env_config
import indicators_core
import report_lint
import sector_map
import setup_types
from report_lint import _clean_number

DB_PATH = Path(__file__).resolve().parent.parent / "trading_new.db"
_NYSE = mcal.get_calendar("NYSE")
_logger = logging.getLogger("persistence")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    update_id       INTEGER PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'received',
    from_id         TEXT,
    chat_id         TEXT,
    message_type    TEXT,
    message_text    TEXT,
    raw_update      TEXT,
    received_at     TEXT NOT NULL,
    claimed_at      TEXT,
    completed_at    TEXT,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS thesis (
    ticker                  TEXT PRIMARY KEY,
    status                  TEXT NOT NULL DEFAULT 'pending',
    sleeve                  TEXT NOT NULL DEFAULT 'unknown',
    source                  TEXT,
    date_built              TEXT,
    primary_setup           TEXT,
    alternate_setup         TEXT,
    position                TEXT,
    rubric_grade            TEXT,
    market_regime_at_build  TEXT,
    planned_qty             INTEGER,
    drop_reason             TEXT,
    updated_at              TEXT NOT NULL
);

-- Journal/Pending/Closing-Summary flow, Stage 1. One row per real fill (a thesis
-- can have more than one position row if it's re-entered after a full close, but
-- never more than one OPEN row at a time -- that would mean two conflicting live
-- positions on the same ticker, which this system doesn't support).
CREATE TABLE IF NOT EXISTS positions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT NOT NULL REFERENCES thesis(ticker),
    entry_date   TEXT NOT NULL,
    entry_price  REAL NOT NULL,
    qty          INTEGER NOT NULL,
    entry_type   TEXT NOT NULL,   -- 'starter' | 'full' -- asked explicitly, never guessed (see /filled)
    initial_stop REAL,            -- fixed at entry -- the R-multiple denominator, never moved even
                                   -- as current_stop trails (Stage 3, compute_r_multiple in indicators_core.py)
    current_stop REAL,
    entry_setup  TEXT,            -- JSON snapshot of whichever thesis setup (primary/alternate) actually
                                   -- triggered this fill -- {"type","trigger","stop","atr_at_build","targets"},
                                   -- copied verbatim at fill time, never re-derived later. A thesis can have
                                   -- two setups in flight at once (CONSISTENCY_RULES.md); once one fills, this
                                   -- is what /exit's derive_exit_reason() matches against -- not a re-lookup
                                   -- against thesis.primary_setup/alternate_setup, which could differ or update
                                   -- after entry.
    status       TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'closed'
    updated_at   TEXT NOT NULL
);

-- Journal/Pending/Closing-Summary flow, Stage 3. One row per real exit tranche
-- (a position can close in more than one tranche -- e.g. two-target realization
-- plus a Runner stop-out is 3 rows against one positions.id).
CREATE TABLE IF NOT EXISTS exits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id  INTEGER NOT NULL REFERENCES positions(id),
    ticker       TEXT NOT NULL,
    exit_date    TEXT NOT NULL,
    exit_price   REAL NOT NULL,
    exit_qty     INTEGER NOT NULL,
    exit_reason  TEXT NOT NULL,   -- indicators_core.derive_exit_reason()'s output: 'stop' | 'target_1' |
                                   -- 'target_2' | 'runner_trim' | 'unmatched' -- never fabricated, 'unmatched' is a
                                   -- valid, honestly-labeled outcome (a real discretionary exit off any pre-planned
                                   -- level). A target already realized by an earlier exits row on the same position
                                   -- can never be matched again (2026-08-07): a later sell with no unrealized target
                                   -- left to attribute it to is 'runner_trim', which is how rule 7's Runner tranche
                                   -- stays countable -- see derive_exit_reason's docstring for the ASTS incident.
    r_multiple   REAL,            -- indicators_core.compute_r_multiple() against the position's initial_stop --
                                   -- NULL when the position has no known original-risk stop (e.g. a legacy
                                   -- holding backfilled without one, found real 2026-07-09 on GOOGL) -- never
                                   -- forced using a stop that isn't the actual original risk basis
    source       TEXT NOT NULL,   -- 'exit_command' | 'playbook_reconciliation' -- which path recorded this
    created_at   TEXT NOT NULL
);

-- Auto-generated the moment a thesis's last open position row fully closes (Stage 3).
-- thesis_validated is deliberately NULL at close time -- never blocks anything -- and
-- only gets filled in later via /journal's own short batch prompt.
CREATE TABLE IF NOT EXISTS closing_summaries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    setup_type        TEXT,
    rubric_grade      TEXT,
    total_r_multiple  REAL,       -- qty-weighted average R-multiple across every exits row for this thesis
    tags              TEXT,       -- JSON list, freeform
    thesis_validated  INTEGER,    -- NULL until answered via /journal; 0/1 afterward
    closed_at         TEXT NOT NULL
);

-- Append-only. No FK to thesis -- deliberately, so an ad-hoc MONITOR_v2 check
-- (section ד, a ticker with no saved thesis at all) can still log without forcing
-- a placeholder thesis row into existence first.
CREATE TABLE IF NOT EXISTS monitor_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL,
    checked_at          TEXT NOT NULL,
    status              TEXT NOT NULL,   -- white|yellow|yellow_plus|green|red, MONITOR_v2's tiers
    price               REAL,
    distance_atr         REAL,
    is_active_rejection INTEGER NOT NULL DEFAULT 0,
    note                TEXT
);

-- One row the first time a waiting idea's trigger actually confirms, per build
-- of that thesis (2026-08-08, the user's request: "document that a stock was on
-- watchlist / buy if confirmed, and now the trigger is on and it is buy now").
--
-- Why it cannot be reconstructed later. monitor_log records that a green
-- happened and when, but not the WORD the thesis was carrying at that moment --
-- and thesis.decision is a single mutable column that a later /screener rebuild
-- overwrites in place. So "PLTR was a Watchlist when it fired" is knowable
-- exactly once, as it happens, and is gone the next time that thesis is rebuilt.
-- That pairing is the whole point: it is the record of how often ideas the
-- system told the user not to buy went on to confirm anyway.
--
-- date_built ties the row to one build of the thesis, so a rebuilt idea that
-- fires again earns a second row rather than being silently deduped against the
-- old one. The UNIQUE index below is what makes the write idempotent: /monitor
-- and the nightly /monitorall both see the same green for days on end.
CREATE TABLE IF NOT EXISTS decision_transitions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL REFERENCES thesis(ticker),
    occurred_at         TEXT NOT NULL,
    date_built          TEXT NOT NULL,   -- which build of the thesis this belongs to
    decision_stored     TEXT,            -- the word /screener wrote, frozen as it was
    status              TEXT NOT NULL,   -- the monitor tier that confirmed it
    price               REAL,
    trigger_price       REAL,
    rubric_grade_stored TEXT,
    rubric_grade_now    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_transitions_once
    ON decision_transitions (ticker, date_built);

-- Hardening Pass item 7 (shadow-book, capture only -- no analysis built on this
-- yet, deliberately, see bot/score_shadow.py's own docstring). ticker (not a
-- numeric thesis id -- thesis.ticker IS its primary key, same FK convention
-- positions/exits already use) references the thesis whose primary_setup this
-- row is scoring. checked_date is when score_shadow.py ran this row, not when
-- the trigger fired. hypothetical_trigger_fired/MFE/MAE are score_shadow.py's
-- compute_shadow_metrics() output -- pure arithmetic against stored levels.
CREATE TABLE IF NOT EXISTS shadow_outcomes (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                      TEXT NOT NULL REFERENCES thesis(ticker),
    checked_date                TEXT NOT NULL,
    price                       REAL,
    hypothetical_trigger_fired  INTEGER NOT NULL DEFAULT 0,
    max_favorable_excursion     REAL,   -- % from trigger, NULL (never zero) if never fired
    max_adverse_excursion       REAL,   -- % from trigger, NULL (never zero) if never fired
    created_at                  TEXT NOT NULL
);

-- Thesis history (2026-08-03). save_thesis is an upsert keyed on ticker, so every
-- rebuild used to overwrite the previous trigger/stop/targets/grade with no copy
-- kept anywhere -- the old plan was simply gone, unrecoverable. That is fine when
-- the rebuild is an improvement and unacceptable when it is not, and nothing in
-- the system could tell the user which had happened. This table is the copy: one
-- append-only row holding the FULL thesis row exactly as it stood immediately
-- before an overwrite replaced it. Nothing reads it to make decisions -- it exists
-- so a rewrite can be seen (refresh_pending.py's nightly before/after message) and
-- undone by hand if it made an idea worse.
--
-- snapshot is the whole prior row as JSON; the named columns beside it are
-- denormalized copies of the few fields the before/after message actually prints,
-- so reading it never requires parsing JSON.
CREATE TABLE IF NOT EXISTS thesis_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker         TEXT NOT NULL,
    replaced_at    TEXT NOT NULL,
    replaced_by    TEXT,     -- the NEW row's source, i.e. what caused this overwrite
    prior_decision TEXT,
    prior_grade    TEXT,
    prior_trigger  REAL,     -- NULL when the prior trigger was prose, not a number
    prior_stop     REAL,
    snapshot       TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- ideas (2026-08-07). The append-only record of every screener build.
--
-- The thesis table is keyed on ticker, so it can only ever hold ONE plan per
-- symbol: re-screening XLF overwrites the previous XLF plan in place. That is
-- correct for the live workflow (there is exactly one current plan per symbol,
-- and every command reads it that way) and wrong for measurement. Three
-- separate XLF builds -- different setup, trigger, stop and grade each time --
-- are three separate ideas that each deserve their own scorecard, but under a
-- ticker key they collapse into one row that appears to change its mind. That
-- collapse was found on 2026-08-07: 28 overwrites across 18 tickers, and the
-- shadow book had been attaching each night's simulated result to whichever
-- plan happened to be current, mixing plans within a single ticker's history.
--
-- This table is the fix, and it is deliberately ADDITIVE: thesis keeps its
-- exact shape and every existing caller keeps working unchanged. save_thesis()
-- writes here in the same transaction as the upsert, so an idea row exists for
-- every build including the very first one (thesis_history only ever captured
-- the losing side of an overwrite, so a ticker screened once appears nowhere in
-- it). Nothing here is ever updated except `status` on the live row and the two
-- supersede columns at the moment a newer build replaces it.
--
-- The flat columns (setup_type through atr_at_build) are DENORMALIZED copies of
-- what is inside primary_setup's JSON, frozen as built. Two reasons, both
-- learned the hard way: pulling data for research must not require digging
-- through JSON in every query, and grades/formulas get re-scored live (rule 27,
-- and the formula itself changed on 2026-08-02) so a join against live thesis
-- rows would silently re-label history. primary_setup/alternate_setup/position
-- keep the complete original JSON beside them, so the flattening never loses
-- anything -- including trigger_text, which holds the raw trigger whenever it
-- was written as prose rather than a number (rule 14's "no order ready yet"
-- case), so a prose trigger is visibly a prose trigger instead of a silent NULL.
CREATE TABLE IF NOT EXISTS ideas (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                 TEXT NOT NULL,
    seq                    INTEGER NOT NULL,   -- 1, 2, 3... which build this is FOR THIS TICKER,
                                                -- so "XLF idea #3" is sayable without exposing row ids
    built_at               TEXT NOT NULL,
    superseded_at          TEXT,               -- NULL = this is the live plan for the ticker
    superseded_by_id       INTEGER REFERENCES ideas(id),
    source                 TEXT,               -- SCREENER_v3, backfilled_historical_asof, ...
    sleeve                 TEXT,
    status                 TEXT,               -- kept current on the live row (pending/open_position/
                                                -- closed/dropped/cold); frozen as-was on superseded rows
    status_at_build        TEXT,               -- what it was the moment this build landed, never touched again
    decision               TEXT,               -- Buy Now | Buy Only If Confirmed | Watchlist | No Trade
    rubric_grade           TEXT,               -- the grade AS BUILT, not as re-scored since
    market_regime_at_build TEXT,
    planned_qty            INTEGER,
    rejection_reasons      TEXT,               -- JSON list
    drop_reason            TEXT,
    setup_type             TEXT,
    trigger                REAL,               -- NULL when the stored trigger was prose (see trigger_text)
    trigger_text           TEXT,               -- the raw non-numeric trigger, verbatim, else NULL
    stop                   REAL,
    stop_text              TEXT,
    target_1               REAL,
    target_2               REAL,
    atr_at_build           REAL,
    primary_setup          TEXT,               -- full original JSON, nothing dropped
    alternate_setup        TEXT,
    position               TEXT,
    UNIQUE(ticker, seq)
);

CREATE INDEX IF NOT EXISTS idx_ideas_ticker ON ideas(ticker);
CREATE INDEX IF NOT EXISTS idx_ideas_live ON ideas(ticker, superseded_at);
CREATE INDEX IF NOT EXISTS idx_ideas_built_at ON ideas(built_at);

-- Portfolio-level risk settings (2026-07-18): the strategy review found
-- position sizing had never actually been risk-based in practice
-- (DEFAULT_RISK_USD was never set, /setrisk was never wired) and there was no
-- portfolio-wide heat/allocation visibility at all. Single-row settings table
-- (id=1 always) rather than a key-value store -- there are few, named,
-- rarely-changing settings here, not an open-ended list.
CREATE TABLE IF NOT EXISTS account_settings (
    id                          INTEGER PRIMARY KEY CHECK (id = 1),
    equity_usd                  REAL,   -- NULL until /equity is run once -- no invented default,
                                         -- same posture as CIRCUIT_BREAKER_STOPOUTS
    risk_pct                    REAL NOT NULL DEFAULT 0.01,
    portfolio_heat_cap_pct      REAL NOT NULL DEFAULT 0.06,
    sector_cap_pct              REAL NOT NULL DEFAULT 0.40,
    core_pct_target             REAL NOT NULL DEFAULT 0.60,
    spy_within_core_pct_target  REAL NOT NULL DEFAULT 0.60,
    qqq_within_core_pct_target  REAL NOT NULL DEFAULT 0.40,
    cash_usage_warn_pct         REAL NOT NULL DEFAULT 0.30,  -- 2026-07-19: if a trade's full-size dollar
                                                               -- cost would exceed this % of available
                                                               -- cash, disclose it -- see get_cash_available().
                                                               -- Real gap found: a tight-stop trade sized
                                                               -- to the full 1% risk target can cost far
                                                               -- more in actual dollars than a wide-stop
                                                               -- one for the identical risk figure (risk $
                                                               -- is only a small fraction of a tight-stop
                                                               -- position's real notional cost) -- nothing
                                                               -- previously surfaced that a single trade
                                                               -- could quietly consume most of the cash on
                                                               -- hand. Disclosure only, same as every other
                                                               -- cap in this system -- never blocks.
    pending_withdrawal_usd      REAL NOT NULL DEFAULT 0,  -- money leaving the account that the broker's
                                                            -- own total hasn't reflected yet (2026-07-18,
                                                            -- real incident: a $17,500 withdrawal not yet
                                                            -- settled) -- subtracted from equity_usd for
                                                            -- every risk/heat/allocation calc via
                                                            -- get_effective_equity(). Cleared back to 0
                                                            -- once the broker's own total actually drops,
                                                            -- not before.
    updated_at                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                TEXT,
    protocol              TEXT,
    run_at                TEXT NOT NULL,
    bars_received         INTEGER,
    date_start            TEXT,
    date_end              TEXT,
    history_complete      INTEGER NOT NULL DEFAULT 0,
    ath_verified          INTEGER NOT NULL DEFAULT 0,
    telegram_message_ids  TEXT,
    sent_confirmed        INTEGER NOT NULL DEFAULT 0
);

-- X/Twitter account feed (2026-07-22, bot/fetch_x_feed.py). Append-only, deduped
-- by post_id (the tweet's own id -- fetch_x_feed.py polls the same accounts
-- repeatedly and must never double-insert a tweet already seen). tickers is a
-- JSON list of cashtags extracted from the tweet text (e.g. ["NVDA","SPY"]),
-- read back via get_recent_posts_for_ticker(); NULL/empty for tweets with none.
-- Purely a Category A data store -- see CONSISTENCY_RULES.md's x_posts
-- guardrail for how this may (and may not) be used downstream.
CREATE TABLE IF NOT EXISTS x_posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     TEXT NOT NULL UNIQUE,
    account     TEXT NOT NULL,
    posted_at   TEXT NOT NULL,
    text        TEXT NOT NULL,
    url         TEXT NOT NULL,
    tickers     TEXT,
    fetched_at  TEXT NOT NULL
);

-- X-feed idea sourcing (2026-07-22, bot/fetch_x_feed.py's end-of-run alert step).
-- One row per ticker, ever -- once a ticker has been Telegram-alerted as a
-- candidate it is never re-alerted (see get_new_candidate_tickers()'s own
-- docstring for why this is deliberately simple, no cooldown/re-alert logic
-- yet). ticker is the PRIMARY KEY (not post_id) precisely because this table
-- tracks "have we ever told the user about this ticker," not "have we ever
-- seen this post."
CREATE TABLE IF NOT EXISTS x_candidate_alerts (
    ticker      TEXT PRIMARY KEY,
    account     TEXT NOT NULL,
    posted_at   TEXT NOT NULL,
    url         TEXT NOT NULL,
    text        TEXT NOT NULL,
    alerted_at  TEXT NOT NULL
);
"""

VALID_SLEEVES = ("core", "swing", "unknown")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json_loads(raw: str, *, ticker: str, field: str):
    """Isolates one corrupted JSON blob to just its own row/field instead of
    raising and taking down an entire multi-row report. Found in review: every
    multi-row read (get_open_positions/get_pending_report_rows/get_journal_rows/
    get_shadow_candidates) called json.loads() directly per row with no
    try/except -- one ticker with a malformed stored blob (a crash mid-write, a
    future caller bug) would throw for that whole command, hiding every OTHER
    ticker's valid data too, not just the bad row. Logs a warning and returns
    None -- every existing caller already treats a falsy value here as "nothing
    stored" (see e.g. _format_pending_setup's `if not setup: return ""`), so
    this degrades to that same, already-handled case rather than a new one."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        _logger.warning("ticker=%s: failed to parse stored JSON for field=%s: %s", ticker, field, e)
        return None


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL mode: this DB is touched concurrently by design (the always-on
    # ack_listener.py, per-message process_queue.py spawns, and the scheduled
    # auto-monitor/position-status triggers all open their own connections) --
    # WAL reduces "database is locked" contention under that pattern vs. the
    # default rollback-journal mode. Persists in the DB file itself after the
    # first call, so this is a one-time effective setting, not a per-connection
    # cost -- but PRAGMA calls are cheap and idempotent, so no need to special-case it.
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """CREATE TABLE IF NOT EXISTS doesn't retroactively add columns to a table that
    already existed under an older shape (this bit us for real once already: an
    early manual test run created `messages` before `raw_update` was added to
    _SCHEMA, silently breaking every real enqueue afterward with a permanent retry
    loop). Add any column in `columns` (name -> SQL type/default clause) not yet
    present on the actual on-disk table -- the single migration mechanism this
    project uses, not a second one per table."""
    existing_cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, decl in columns.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _relax_exits_r_multiple_nullable(conn: sqlite3.Connection) -> None:
    """Found real, 2026-07-09: a legacy holding (GOOGL) exited for real with no
    original-risk stop ever recorded -- unlike a fresh /filled position (PLTR),
    there's no genuine denominator to compute R-multiple from, and forcing one
    would mean inventing a risk basis that never existed. r_multiple was
    originally NOT NULL; _ensure_columns() can only ADD columns, not relax an
    existing constraint, so this recreates the table when the old constraint is
    still in place. No-op once already relaxed."""
    cols = conn.execute("PRAGMA table_info(exits)").fetchall()
    r_mult_col = next((c for c in cols if c["name"] == "r_multiple"), None)
    if r_mult_col is None or r_mult_col["notnull"] == 0:
        return
    conn.execute("ALTER TABLE exits RENAME TO exits_old")
    conn.execute("""
        CREATE TABLE exits (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id  INTEGER NOT NULL REFERENCES positions(id),
            ticker       TEXT NOT NULL,
            exit_date    TEXT NOT NULL,
            exit_price   REAL NOT NULL,
            exit_qty     INTEGER NOT NULL,
            exit_reason  TEXT NOT NULL,
            r_multiple   REAL,
            source       TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            commission   REAL
        )
    """)
    conn.execute(
        "INSERT INTO exits (id, position_id, ticker, exit_date, exit_price, exit_qty, "
        "exit_reason, r_multiple, source, created_at, commission) "
        "SELECT id, position_id, ticker, exit_date, exit_price, exit_qty, "
        "exit_reason, r_multiple, source, created_at, commission FROM exits_old"
    )
    conn.execute("DROP TABLE exits_old")


def _backfill_ideas(conn: sqlite3.Connection) -> None:
    """One-time reconstruction of the build history that pre-dates the ideas
    table (2026-08-07). Runs only while ideas is empty; save_thesis writes every
    build directly from then on, so this never needs to run twice.

    Sources, in order per ticker: every thesis_history row (each is the FULL
    prior thesis row as JSON, captured at the moment it was overwritten), then
    the live thesis row itself as the newest build. thesis_history alone is not
    enough -- it only ever captured the losing side of an overwrite, so a ticker
    screened exactly once appears nowhere in it.

    What cannot be recovered is stated rather than guessed: builds that happened
    before thesis_history existed (2026-08-03) left no copy anywhere, so a
    ticker screened three times in July reconstructs as one build. The seq
    numbers are therefore "builds we can still see", not "builds that happened".
    """
    if conn.execute("SELECT COUNT(*) c FROM ideas").fetchone()["c"]:
        return

    tickers = [r["ticker"] for r in conn.execute(
        "SELECT ticker FROM thesis WHERE primary_setup IS NOT NULL "
        "UNION SELECT ticker FROM thesis_history ORDER BY 1"
    )]
    for ticker in tickers:
        versions: list[tuple[dict, Optional[str]]] = []   # (thesis-shaped row, superseded_at)
        for h in conn.execute(
            "SELECT replaced_at, snapshot FROM thesis_history WHERE ticker=? ORDER BY id", (ticker,)
        ):
            snap = _safe_json_loads(h["snapshot"], ticker=ticker, field="snapshot")
            if isinstance(snap, dict):
                versions.append((snap, h["replaced_at"]))
        current = conn.execute("SELECT * FROM thesis WHERE ticker=?", (ticker,)).fetchone()
        if current and current["primary_setup"]:
            versions.append((dict(current), None))
        for snap, superseded_at in versions:
            primary = _safe_json_loads(snap.get("primary_setup") or "", ticker=ticker,
                                        field="primary_setup") if snap.get("primary_setup") else None
            alternate = _safe_json_loads(snap.get("alternate_setup") or "", ticker=ticker,
                                          field="alternate_setup") if snap.get("alternate_setup") else None
            position = _safe_json_loads(snap.get("position") or "", ticker=ticker,
                                         field="position") if snap.get("position") else None
            reasons = _safe_json_loads(snap.get("rejection_reasons") or "", ticker=ticker,
                                        field="rejection_reasons") if snap.get("rejection_reasons") else None
            idea_id = _record_idea(
                conn, ticker, status=snap.get("status") or "pending",
                source=snap.get("source"), sleeve=snap.get("sleeve"),
                primary_setup=primary, alternate_setup=alternate, position=position,
                rubric_grade=snap.get("rubric_grade"),
                market_regime_at_build=snap.get("market_regime_at_build"),
                planned_qty=snap.get("planned_qty"), decision=snap.get("decision"),
                rejection_reasons=reasons if isinstance(reasons, list) else None,
                # date_built is when the screener actually built this plan; the
                # archive timestamp is only when it was replaced. Using the
                # latter would date every reconstructed build to the night it
                # died rather than the day it was made.
                built_at=snap.get("date_built") or superseded_at or _now(),
            )
            if superseded_at:
                conn.execute("UPDATE ideas SET superseded_at=? WHERE id=?", (superseded_at, idea_id))
            if snap.get("drop_reason"):
                conn.execute("UPDATE ideas SET drop_reason=? WHERE id=?", (snap["drop_reason"], idea_id))
    _backfill_shadow_idea_ids(conn)
    _backfill_position_idea_ids(conn)


def _backfill_shadow_idea_ids(conn: sqlite3.Connection) -> None:
    """Attach each pre-existing shadow row to the build it actually scored.

    Matched on the levels the row itself stored (ticker + trigger + stop), not
    on time: those numbers ARE the plan, so an exact match is proof, while a
    date window only ever produces a plausible guess. Rows that cannot be
    matched that way -- the early captures that stored price only, before the
    simulation columns existed -- keep idea_id NULL and are simply excluded from
    any per-build study. A wrong link would be worse than a missing one.

    Where two rows would land on the same (idea_id, checked_date), only the
    newest keeps the link; the duplicate is labelled and left NULL rather than
    deleted. Those duplicates are real history (a scheduled run that fired
    twice) and deleting data to satisfy a new index is not this migration's
    call to make.
    """
    claimed: set[tuple[int, str]] = set()
    rows = conn.execute(
        "SELECT id, ticker, checked_date, trigger, stop FROM shadow_outcomes "
        "WHERE trigger IS NOT NULL AND stop IS NOT NULL ORDER BY id DESC"
    ).fetchall()
    for row in rows:
        match = conn.execute(
            "SELECT id FROM ideas WHERE ticker=? AND trigger IS NOT NULL "
            "AND ABS(trigger - ?) < 0.0005 AND stop IS NOT NULL AND ABS(stop - ?) < 0.0005 "
            "ORDER BY seq DESC LIMIT 1",
            (row["ticker"], row["trigger"], row["stop"]),
        ).fetchone()
        if not match:
            continue
        key = (match["id"], row["checked_date"])
        if key in claimed:
            conn.execute(
                "UPDATE shadow_outcomes SET sim_note = COALESCE(sim_note || ' | ', '') || "
                "'duplicate run for this build on this date; superseded by the later row' WHERE id=?",
                (row["id"],),
            )
            continue
        claimed.add(key)
        conn.execute("UPDATE shadow_outcomes SET idea_id=? WHERE id=?", (match["id"], row["id"]))


def _backfill_position_idea_ids(conn: sqlite3.Connection) -> None:
    """Attach each existing fill to the build that was live when it was entered.

    Time-based here, unlike the shadow rows: a position stores its entry price,
    not the plan's trigger, so there are no plan levels to match exactly. The
    build that was live on the entry date is the honest answer -- it is what the
    user was reading when they took the trade."""
    for pos in conn.execute("SELECT id, ticker, entry_date FROM positions WHERE idea_id IS NULL"):
        match = conn.execute(
            "SELECT id FROM ideas WHERE ticker=? AND built_at <= ? "
            "AND (superseded_at IS NULL OR superseded_at > ?) ORDER BY seq DESC LIMIT 1",
            (pos["ticker"], pos["entry_date"] + "T23:59:59", pos["entry_date"]),
        ).fetchone()
        if match:
            conn.execute("UPDATE positions SET idea_id=? WHERE id=?", (match["id"], pos["id"]))


def init_db() -> None:
    with _db() as conn:
        conn.executescript(_SCHEMA)
        _ensure_columns(conn, "messages", {"raw_update": "TEXT"})
        _ensure_columns(conn, "thesis", {
            "rubric_grade": "TEXT",
            "market_regime_at_build": "TEXT",
            "planned_qty": "INTEGER",
            "drop_reason": "TEXT",
        })
        _ensure_columns(conn, "positions", {
            "initial_stop": "REAL",
            "entry_setup": "TEXT",
            "entry_commission": "REAL",  # real fill data, e.g. broker commission -- found missing
                                          # from schema entirely on first real manual /filled entry (PLTR, 2026-07-08)
            "override_of_decision": "TEXT",  # 2026-08-02: what the system had ALREADY said about this
                                              # ticker at the moment of the fill, stored verbatim, but only
                                              # when that verdict was a non-buy (see _classify_override).
                                              # NULL = the fill agreed with the system. Never typed by the
                                              # user -- derived mechanically at /filled time.
            "override_of_grade": "TEXT",     # the thesis's own rubric_grade at that same moment, same rule
            # 2026-08-07: which exact ideas row this fill was taken against. The
            # ticker alone cannot answer that once a symbol has been screened
            # more than once -- entry_setup already froze the setup JSON, but
            # nothing tied the fill back to the identifiable build it came from.
            "idea_id": "INTEGER REFERENCES ideas(id)",
        })
        _ensure_columns(conn, "exits", {
            "commission": "REAL",  # same gap, mirrored on the exit side for round-trip cost tracking
        })
        _relax_exits_r_multiple_nullable(conn)
        _ensure_columns(conn, "analysis_runs", {
            "lint_result": "TEXT",  # Hardening Pass item 3: report_lint.py's full finding detail, JSON,
                                     # logged for every delivered report regardless of pass/fail
        })
        _ensure_columns(conn, "thesis", {
            "decision": "TEXT",            # Hardening Pass item 7: Buy Now/Buy Only If Confirmed/Watchlist/No Trade,
                                            # stored on every screener run including Watchlist/No Trade -- shadow-book capture
            "rejection_reasons": "TEXT",   # JSON list -- which gate/rubric items failed, e.g. ["rr_below_2", "regime_against"]
            # Cold list, rule 29 (2026-08-03). A 'No Trade' thesis leaves the
            # active waiting list (status='cold') instead of sitting in it
            # forever or being deleted -- "nowhere to sell" is usually about
            # where price stands today, not about the company, so it can become
            # a real trade within days. This counts how many automatic
            # re-screens it has already had, so the retries stay bounded.
            "cold_rechecks": "INTEGER NOT NULL DEFAULT 0",
            # Broken-idea shelving (2026-08-11, user's call). How many nightly
            # refreshes in a row have found price under this thesis's own stop.
            # 1 = tell the user in full, with the /drop line; after that only a
            # short count; at DEAD_NIGHTS_BEFORE_COLD the row is shelved (cold)
            # so the waiting list the user actually reads stays short. Reset the
            # moment price is back above the stop, or on any fresh build.
            "dead_nights": "INTEGER NOT NULL DEFAULT 0",
            # When a row was shelved. The cold re-check clock used to run off
            # date_built, which is right for a No Trade (built and shelved the
            # same minute) but wrong for a thesis shelved months after it was
            # built -- that one is instantly "due" and gets re-screened the very
            # next night, which is exactly the burnt hour on a broken chart this
            # shelf exists to avoid. NULL on old rows, which fall back to
            # date_built and so behave exactly as before.
            "cold_since": "TEXT",
        })
        _ensure_columns(conn, "account_settings", {
            "pending_withdrawal_usd": "REAL NOT NULL DEFAULT 0",  # added 2026-07-18, real withdrawal-timing gap
            "cash_usage_warn_pct": "REAL NOT NULL DEFAULT 0.30",  # added 2026-07-19, cash-vs-risk gap
        })
        # Shadow book, upgraded 2026-08-03 from "did it fire + how far did it
        # swing" to a full played-out trade, so the capture is usable as real
        # backtest input rather than only as a curiosity. Everything from
        # setup_type down is DENORMALIZED on purpose: rubric grades get
        # re-scored live (rule 27) and the formula itself changes over time
        # (it changed on 2026-08-02), so a backtest joining live thesis rows
        # would silently re-label history. These columns record what was true
        # when the idea was built. See score_shadow.py's assumptions block for
        # what each simulated field does and does not claim.
        _ensure_columns(conn, "shadow_outcomes", {
            "setup_type": "TEXT",
            "decision": "TEXT",                 # what the system said at build time
            "rubric_grade": "TEXT",             # the grade AS BUILT, not as re-scored since
            "market_regime_at_build": "TEXT",
            "trigger": "REAL",
            "stop": "REAL",
            "target_1": "REAL",
            "target_2": "REAL",
            "atr_at_build": "REAL",
            "fired_date": "TEXT",
            "entry": "REAL",                    # next bar's open after the trigger closed
            "entry_date": "TEXT",
            "entry_gap_pct": "REAL",            # cost of the daily-close confirmation rule
            "risk_per_share": "REAL",
            "resolution": "TEXT",               # never_fired|stop|target_1|target_2|open
            "exit_date": "TEXT",
            "exit_price": "REAL",
            "r_multiple_simple": "REAL",        # whole position out at first resolution
            "r_multiple_planned": "REAL",       # tranche-weighted, breakeven stop after target 1
            "mfe_r": "REAL",                    # best move in units of risk (NOT percent --
                                                 # percent structurally favours volatile tickers)
            "mae_r": "REAL",
            "bars_held": "INTEGER",
            "sim_version": "TEXT",              # so a later change to the simulation is identifiable
                                                 # instead of quietly mixed into old rows
            "sim_note": "TEXT",
            # 2026-08-07: the exact build this simulated result belongs to. Before
            # this, a shadow row carried only the ticker, so three different XLF
            # builds produced rows that looked like one XLF track record changing
            # its mind nightly. Nothing downstream may aggregate by ticker alone.
            "idea_id": "INTEGER REFERENCES ideas(id)",
            # 2026-08-09. Everything above records what an idea did; none of it
            # says whether that was any good. These five make the table
            # answerable rather than merely complete -- see _SHADOW_SIM_COLUMNS
            # for what each one is for.
            "spy_return_pct": "REAL",
            "spy_return_r": "REAL",
            "days_to_fire": "INTEGER",
            "sector": "TEXT",
            "rr_at_build": "REAL",
            "owner_bought": "INTEGER",
            # 2026-08-10: WHICH kind of structure the stop stood on. The stop is
            # the denominator of every R in this table, so a systematic problem
            # with stop placement is invisible in R itself -- MMM was given a
            # five-month-old low 12% away and every downstream number inherited
            # it. This makes "do recent-structure stops beat old-structure ones"
            # a countable question instead of an argument.
            "stop_basis_kind": "TEXT",
        })
        # One shadow row per idea per day. Runs were firing more than once on some
        # nights (found 2026-08-07: two identical XLF rows on 08-04, and two NVDA
        # rows on 08-02 that disagreed on whether the trigger had fired), and
        # nothing in the schema forbade the repeat. Partial index -- rows
        # backfilled before idea_id existed stay NULL and are not constrained.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_one_per_idea_per_day "
            "ON shadow_outcomes(idea_id, checked_date) WHERE idea_id IS NOT NULL"
        )
        _ensure_columns(conn, "closing_summaries", {
            "lesson": "TEXT",  # /reflect's free-text reflection (2026-07-25) -- paired with
                                # thesis_validated, which existed since the original schema but was
                                # never actually written by anything until /reflect (MASTER_SYSTEM_SPEC
                                # section 9 flagged this as an open item). NULL means not yet reflected on.
        })
        # Seed the single settings row if it doesn't exist yet -- CREATE TABLE IF
        # NOT EXISTS creates the table but never inserts a row, and every
        # get/set function below assumes exactly one row (id=1) always exists.
        conn.execute(
            "INSERT OR IGNORE INTO account_settings (id, updated_at) VALUES (1, ?)",
            (_now(),),
        )
        _backfill_ideas(conn)


# init_db() is invoked at the BOTTOM of this module, not here. It used to run at
# this point, but the 2026-08-07 ideas migration calls _record_idea/_safe_json_loads,
# which are defined further down -- running the schema step before those exist
# raises NameError on import. Nothing between here and there touches the DB at
# module level, so the only change is when during import the tables get created.


# ---------------------------------------------------------------------------
# Portfolio-level risk settings (2026-07-18). Found in the strategy review:
# position sizing had never actually been risk-based in practice
# (DEFAULT_RISK_USD was never set, /setrisk was never wired -- every real
# screener report so far reported "cannot compute final quantity"), and there
# was no portfolio-wide heat/allocation visibility at all. Everything here is
# informational/disclosure only, never a gate on any decision -- explicit user
# direction: the system's job is to always show the real number, never to
# decide for the user.
# ---------------------------------------------------------------------------

def get_account_settings() -> dict:
    with _db() as conn:
        row = conn.execute("SELECT * FROM account_settings WHERE id=1").fetchone()
    return dict(row)


def set_equity(equity_usd: float) -> None:
    """/equity TICKER-less command -- updates the current account value used
    for every % based calc below (risk_usd, portfolio heat, allocation drift).
    No live broker feed exists in this system (see MASTER_SYSTEM_SPEC.md's
    "no paid data feed assumed" principle) -- this is the same manual-input
    pattern as everything else, just for account value instead of a chart."""
    if equity_usd <= 0:
        raise ValueError(f"equity_usd must be positive, got {equity_usd!r}")
    with _db() as conn:
        conn.execute(
            "UPDATE account_settings SET equity_usd=?, updated_at=? WHERE id=1",
            (equity_usd, _now()),
        )


def set_pending_withdrawal(usd: float) -> None:
    """/withdraw -- marks money as already-gone for risk purposes even though
    the broker's own total hasn't dropped yet. Real incident (2026-07-18): a
    real $17,500 withdrawal was already decided but not yet settled, and the
    broker screenshot /playbook reads still showed the full pre-withdrawal
    total -- without this, auto-detected equity (see deliver_playbook_report.py)
    would silently use money that's already spoken for. Pass 0 once the
    withdrawal actually clears and the broker's own total has genuinely
    dropped -- at that point equity_usd itself already reflects it correctly,
    so continuing to subtract would double-count."""
    if usd < 0:
        raise ValueError(f"pending withdrawal must be >= 0 (0 clears it), got {usd!r}")
    with _db() as conn:
        conn.execute(
            "UPDATE account_settings SET pending_withdrawal_usd=?, updated_at=? WHERE id=1",
            (usd, _now()),
        )


def get_effective_equity() -> Optional[float]:
    """equity_usd minus pending_withdrawal_usd -- the real number every risk/
    heat/allocation calculation should use, not the raw broker total alone.
    None (not 0 or the raw total) if equity_usd was never set."""
    settings = get_account_settings()
    if settings["equity_usd"] is None:
        return None
    return settings["equity_usd"] - settings["pending_withdrawal_usd"]


def set_risk_pct(risk_pct: float) -> None:
    """/setrisk -- finally wires up the command that sat in ack_listener.py's
    NOT_YET_WIRED tuple since day one. risk_pct is a fraction (0.01 = 1%), not
    a percentage number -- callers parsing "1%" from a Telegram message divide
    by 100 before calling this."""
    if not (0 < risk_pct < 1):
        raise ValueError(f"risk_pct must be a fraction between 0 and 1 (e.g. 0.01 for 1%), got {risk_pct!r}")
    with _db() as conn:
        conn.execute(
            "UPDATE account_settings SET risk_pct=?, updated_at=? WHERE id=1",
            (risk_pct, _now()),
        )


def get_portfolio_heat() -> dict:
    """Sums real dollar risk -- (entry_price - current_stop) * remaining_qty,
    never the stale original qty (see remaining_qty's own 2026-07-16 fix) --
    across every open position, Core sleeve included (a wide structural stop
    is still real risk even though Core is exempt from the rest of the swing
    rules, rule 8). Divides by equity_usd for heat_pct. Returns heat_pct=None
    (not 0.0 -- a real "unknown", not "zero risk") when equity_usd hasn't been
    set yet via /equity, so callers can distinguish "genuinely no risk" from
    "can't compute this yet"."""
    settings = get_account_settings()
    cap_pct = settings["portfolio_heat_cap_pct"]
    with _db() as conn:
        positions = conn.execute(
            "SELECT id, entry_price, current_stop, qty FROM positions WHERE status='open'"
        ).fetchall()
        heat_usd = 0.0
        for p in positions:
            if p["current_stop"] is None:
                continue  # no known stop (e.g. a legacy position) -- no risk figure to add, not zero
            remaining = _remaining_qty(conn, p["id"], p["qty"])
            risk_per_share = p["entry_price"] - p["current_stop"]
            if risk_per_share > 0:
                heat_usd += risk_per_share * remaining

    equity = get_effective_equity()
    heat_pct = (heat_usd / equity) if equity else None
    return {
        "heat_usd": heat_usd,
        "heat_pct": heat_pct,
        "cap_pct": cap_pct,
        "breached": (heat_pct is not None and heat_pct > cap_pct),
    }


def get_allocation_drift() -> dict:
    """Actual vs. target % for Core-vs-Swing and SPY-vs-QQQ within Core --
    informational only, never gates anything (Core is rebalanced far less
    often than swing entries are opened, per explicit user direction). Uses
    each position's own entry_price * remaining_qty as its current dollar
    size -- an honest approximation, not a live mark-to-market (this system
    has no live price feed to value open positions against, same constraint
    documented in MASTER_SYSTEM_SPEC.md). Returns None for any ratio that
    can't be computed yet (equity_usd unset, or no positions in a bucket) --
    never a misleading 0%."""
    settings = get_account_settings()
    equity = get_effective_equity()
    with _db() as conn:
        rows = conn.execute("""
            SELECT p.ticker, p.entry_price, p.qty, p.id, t.sleeve
            FROM positions p LEFT JOIN thesis t ON t.ticker = p.ticker
            WHERE p.status='open'
        """).fetchall()
        core_usd = 0.0
        swing_usd = 0.0
        spy_usd = 0.0
        qqq_usd = 0.0
        for r in rows:
            remaining = _remaining_qty(conn, r["id"], r["qty"])
            value = r["entry_price"] * remaining
            if r["sleeve"] == "core":
                core_usd += value
                if r["ticker"] == "SPY":
                    spy_usd += value
                elif r["ticker"] == "QQQ":
                    qqq_usd += value
            else:
                swing_usd += value

    if not equity:
        return {
            "core_pct_actual": None, "core_pct_target": settings["core_pct_target"],
            "swing_pct_actual": None, "swing_pct_target": 1 - settings["core_pct_target"],
            "spy_within_core_pct_actual": None, "spy_within_core_pct_target": settings["spy_within_core_pct_target"],
            "qqq_within_core_pct_actual": None, "qqq_within_core_pct_target": settings["qqq_within_core_pct_target"],
        }
    core_pct_actual = core_usd / equity
    swing_pct_actual = swing_usd / equity
    spy_within_core_actual = (spy_usd / core_usd) if core_usd else None
    qqq_within_core_actual = (qqq_usd / core_usd) if core_usd else None
    return {
        "core_pct_actual": core_pct_actual, "core_pct_target": settings["core_pct_target"],
        "swing_pct_actual": swing_pct_actual, "swing_pct_target": 1 - settings["core_pct_target"],
        "spy_within_core_pct_actual": spy_within_core_actual, "spy_within_core_pct_target": settings["spy_within_core_pct_target"],
        "qqq_within_core_pct_actual": qqq_within_core_actual, "qqq_within_core_pct_target": settings["qqq_within_core_pct_target"],
    }


def get_sector_exposure() -> dict:
    """Sums swing-sleeve-only $ risk (same (entry_price - current_stop) *
    remaining_qty calc as get_portfolio_heat()) by correlation group
    (sector_map.get_sector_group), each as a % of the total swing book --
    Core sleeve (SPY/QQQ, rule 8) is excluded from both the numerator and the
    denominator here, since those aren't swing decisions being gated by this
    check. A ticker with no group mapping is bucketed under "unclassified"
    rather than silently dropped -- extending sector_map.py is a deliberate
    one-line edit, not something this function should paper over. Returns
    {} if there's no swing risk at all yet (nothing to divide by), not an
    error."""
    with _db() as conn:
        rows = conn.execute("""
            SELECT p.id, p.ticker, p.entry_price, p.current_stop, p.qty, t.sleeve
            FROM positions p LEFT JOIN thesis t ON t.ticker = p.ticker
            WHERE p.status='open'
        """).fetchall()
        group_risk: dict = {}
        swing_total = 0.0
        for r in rows:
            if r["sleeve"] == "core":
                continue
            if r["current_stop"] is None:
                continue
            risk_per_share = r["entry_price"] - r["current_stop"]
            if risk_per_share <= 0:
                continue
            remaining = _remaining_qty(conn, r["id"], r["qty"])
            risk_usd = risk_per_share * remaining
            group = sector_map.get_sector_group(r["ticker"]) or "unclassified"
            group_risk[group] = group_risk.get(group, 0.0) + risk_usd
            swing_total += risk_usd

    if not swing_total:
        return {}
    return {
        group: {"risk_usd": risk_usd, "pct_of_swing_book": risk_usd / swing_total}
        for group, risk_usd in group_risk.items()
    }


def get_cash_available() -> Optional[float]:
    """Effective equity minus every open position's current value -- the real
    dollar figure that bounds what a NEW trade can actually cost, as distinct
    from portfolio heat (which bounds RISK, not dollars spent). Found real,
    2026-07-19: a trade sized to the full 1% risk target can cost far more in
    actual dollars than that risk figure suggests whenever the stop sits close
    to the price (tight-stop trades scale up in dollar notional much faster
    than in risk) -- nothing previously surfaced that a single trade could
    quietly consume most of the cash on hand even while portfolio heat stayed
    well under its own cap. Uses each position's entry_price * remaining_qty
    as its value -- the same honest approximation get_allocation_drift()
    already uses (no live mark-to-market feed exists in this system). None if
    equity_usd was never set."""
    equity = get_effective_equity()
    if equity is None:
        return None
    with _db() as conn:
        rows = conn.execute("SELECT id, entry_price, qty FROM positions WHERE status='open'").fetchall()
        invested = sum(r["entry_price"] * _remaining_qty(conn, r["id"], r["qty"]) for r in rows)
    return equity - invested


# A message claimed but not completed within this window is assumed crashed/stuck,
# not genuinely still working -- reclaimed back to "received" so it's retried.
# Found in the 2026-07-30 full-system checkup: this was 600s, but a real
# /screener or /playbook run is allowed up to 1500s, and /monitorall/positions
# scale to 400s per ticker with no cap (a real 20-ticker batch needs ~8000s).
# A still-legitimately-running message older than 600s could get reclaimed and
# handed out a second time -- e.g. by bot/check_telegram.py, which calls this
# with no lock of its own -- causing the same ticker to be analyzed and
# delivered twice. 7200s (2 hours) comfortably covers every real command this
# system runs today while still eventually recovering a genuinely crashed one.
STALE_PROCESSING_TIMEOUT_SECONDS = 7200


# ---------------------------------------------------------------------------
# A5: message queue state machine (rebuilt 2026-07-09 -- event-driven, see module
# docstring). ack_listener.py enqueues, then immediately triggers
# bot/process_queue.py -- nothing here polls on a timer.
# ---------------------------------------------------------------------------

def enqueue_message(update_id: int, from_id: str, chat_id: str, message_type: str,
                     message_text: Optional[str], raw_update: dict) -> bool:
    """Insert as 'received'. Returns False if update_id already exists -- Telegram's
    own update_id is the idempotency key, so a redelivered update is a no-op, not a
    duplicate row and not a second analysis run. raw_update is the full Telegram
    update dict (needed downstream for photo file_id, entities, etc. -- message_text
    alone isn't enough to process a photo message)."""
    with _db() as conn:
        try:
            conn.execute(
                "INSERT INTO messages (update_id, status, from_id, chat_id, "
                "message_type, message_text, raw_update, received_at) VALUES (?, 'received', ?, ?, ?, ?, ?, ?)",
                (update_id, from_id, chat_id, message_type, message_text,
                 json.dumps(raw_update, ensure_ascii=False), _now()),
            )
            return True
        except sqlite3.IntegrityError:
            return False  # duplicate update_id -- already known, idempotent no-op


def reclaim_stale_processing(timeout_seconds: int = STALE_PROCESSING_TIMEOUT_SECONDS) -> int:
    """Messages stuck in 'processing' past timeout_seconds (a crash mid-analysis,
    not genuine ongoing work) are reset to 'received' so the next claim picks them
    up again. Returns how many were reclaimed. Call this before claim_next_messages()."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
    with _db() as conn:
        cur = conn.execute(
            "UPDATE messages SET status='received', claimed_at=NULL "
            "WHERE status='processing' AND claimed_at < ?",
            (cutoff,),
        )
        return cur.rowcount


def claim_next_messages() -> list[dict]:
    """Atomically transitions every 'received' message to 'processing' and returns
    them for the caller to actually work on. A message claimed here is not visible
    to another concurrent caller as 'received' anymore."""
    with _db() as conn:
        rows = conn.execute("SELECT update_id FROM messages WHERE status='received'").fetchall()
        claimed = []
        for row in rows:
            conn.execute(
                "UPDATE messages SET status='processing', claimed_at=? WHERE update_id=? AND status='received'",
                (_now(), row["update_id"]),
            )
            full = conn.execute("SELECT * FROM messages WHERE update_id=?", (row["update_id"],)).fetchone()
            if full is not None:
                d = dict(full)
                if d.get("raw_update"):
                    d["raw_update"] = json.loads(d["raw_update"])
                claimed.append(d)
        return claimed


def count_pending_messages() -> int:
    """New for the 2026-07-09 rebuild: process_queue.py's self-draining loop calls
    this after each claude -p round to decide whether to run another round (a
    message can arrive mid-run) or stop -- never a blind timer-based re-check."""
    with _db() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE status IN ('received', 'processing')"
        ).fetchone()["c"]


COLD_RECHECK_TRADING_DAYS = 3
COLD_MAX_RECHECKS = 3


def set_cold(ticker: str) -> None:
    """Move a thesis to the cold list -- off the active waiting list, but NOT
    forgotten (CONSISTENCY_RULES.md rule 29, 2026-08-03).

    Why a third state exists at all. A "No Trade" verdict almost always means
    "there is nowhere above this price worth selling into" -- and that is a
    statement about where price is standing today, not about the company. Rule
    7 already documents two real cases where the identical chart produced a
    qualifying target purely from a different entry price: ANET's 179.80 level
    failed from a 179.80 entry and passed at 2.54:1 from a ~162 entry, and MU's
    identical target went from 1.78:1 to 8.19:1 the same way. So a No Trade can
    legitimately become a real trade within days, with no news at all.

    Before this, such a thesis sat in 'pending' forever, cluttering the list
    that is supposed to be short enough to read. Deleting it instead would have
    been worse: refresh_pending.py only rebuilds PENDING rows, so a dropped idea
    is never looked at again by anything.

    Cold rows are re-screened automatically every COLD_RECHECK_TRADING_DAYS,
    up to COLD_MAX_RECHECKS times -- see get_cold_recheck_candidates."""
    with _db() as conn:
        now = _now()
        conn.execute("UPDATE thesis SET status='cold', cold_since=?, updated_at=? WHERE ticker=?",
                      (now, now, ticker.upper()))
        _sync_live_idea_status(conn, ticker, "cold")


def get_cold_recheck_candidates() -> list[dict]:
    """Cold theses due for another look: at least COLD_RECHECK_TRADING_DAYS real
    trading days since they were last built, and fewer than COLD_MAX_RECHECKS
    attempts so far.

    The cap is what keeps this bounded. Each re-check is a full ~25 minute
    screener run, and without a limit the cold list only ever grows -- every
    No Trade ever produced would be re-analysed forever. After the cap, the row
    is reported to the user with a /drop line and left alone; it is never
    silently deleted."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM thesis WHERE status='cold' AND primary_setup IS NOT NULL"
        ).fetchall()
    today = _now()[:10]
    due = []
    for row in rows:
        d = dict(row)
        # Counted from the day it was SHELVED, falling back to the build date
        # for rows shelved before cold_since existed. For a No Trade the two are
        # the same minute; for an idea shelved long after it was built (a thesis
        # whose price fell under its own stop, 2026-08-11) they are not, and
        # date_built would make it due the moment it landed here.
        since = (d.get("cold_since") or d.get("date_built") or "")[:10]
        if not since:
            continue
        d["days_cold"] = count_trading_days(since, today)
        d["cold_rechecks"] = d.get("cold_rechecks") or 0
        if d["days_cold"] >= COLD_RECHECK_TRADING_DAYS and d["cold_rechecks"] < COLD_MAX_RECHECKS:
            due.append(d)
    return due


def get_exhausted_cold(ticker: Optional[str] = None) -> list[dict]:
    """Cold theses that used up every re-check and still never found a place to
    sell. Reported once so the user can /drop them -- never auto-deleted, same
    posture as refresh_pending.py's dead-idea handling."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM thesis WHERE status='cold' AND COALESCE(cold_rechecks,0) >= ?",
            (COLD_MAX_RECHECKS,),
        ).fetchall()
    result = [dict(r) for r in rows]
    if ticker:
        result = [r for r in result if r["ticker"] == ticker.upper()]
    return result


def bump_cold_recheck(ticker: str) -> None:
    """Count one re-check attempt. Called when the re-screen is QUEUED, not when
    it finishes: a run that dies partway must still consume its attempt, or a
    ticker that reliably fails to analyse would be retried forever."""
    with _db() as conn:
        conn.execute(
            "UPDATE thesis SET cold_rechecks = COALESCE(cold_rechecks,0) + 1, updated_at=? "
            "WHERE ticker=?", (_now(), ticker.upper()),
        )


# How many nightly refreshes in a row may find price under a thesis's own stop
# before the row is moved to the shelf. Five is the user's own number: long
# enough that one bad close, or a weekend of not reading Telegram, never shelves
# a live idea; short enough that the waiting list does not fill with corpses.
DEAD_NIGHTS_BEFORE_COLD = 5


def bump_dead_night(ticker: str) -> int:
    """Count one nightly refresh that found this thesis under its own stop, and
    return the new total (2026-08-11).

    Drives two things at once in refresh_pending.py: what the message says (a
    full /drop line the FIRST night, a short count after that -- a message that
    repeats verbatim every night is a message that stops being read, and a new
    corpse hides among the old ones), and when the row is shelved.

    Counted per REFRESH RUN, not per calendar day, so a night the job did not
    run cannot age an idea toward the shelf."""
    with _db() as conn:
        conn.execute(
            "UPDATE thesis SET dead_nights = COALESCE(dead_nights,0) + 1, updated_at=? "
            "WHERE ticker=?", (_now(), ticker.upper()),
        )
        row = conn.execute("SELECT dead_nights FROM thesis WHERE ticker=?",
                            (ticker.upper(),)).fetchone()
    return (row["dead_nights"] if row else 0) or 0


def clear_dead_nights(ticker: str) -> None:
    """Price is back above the stop -- the streak starts over. Without this a
    thesis that dipped under its stop on three separate weeks would be shelved
    on the third, having actually been fine in between."""
    with _db() as conn:
        conn.execute(
            "UPDATE thesis SET dead_nights=0 WHERE ticker=? AND COALESCE(dead_nights,0) <> 0",
            (ticker.upper(),),
        )


def monitorall_ran_since(since_utc_iso: str) -> bool:
    """True if a '/monitorall' message was queued at or after since_utc_iso
    (2026-08-08).

    Backs the late post-close fallback job. The post-close scan is now chained
    off the end of refresh_pending.py, so the scheduled job that used to own it
    is only there to cover the night the refresh dies. Without this check that
    fallback would fire a SECOND full scan every normal night -- ~20 minutes of
    duplicate work and a duplicate report on the same list.

    Matches on received_at (queue time), not completed_at: a scan still draining
    when the fallback fires has already happened as far as the user is
    concerned, and re-queueing it would collide with the run in flight.
    '/monitorall_strict' shares the prefix and is deliberately included -- but
    it only ever runs at the open, hours before any post-close cutoff, so it
    cannot match a same-evening window in practice."""
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE message_text LIKE '/monitorall%' "
            "AND received_at >= ? LIMIT 1", (since_utc_iso,)
        ).fetchone()
    return row is not None


def tickers_already_queued_for_screener() -> set[str]:
    """Tickers with an unfinished '/screener TICKER' message already sitting in
    the queue (received or processing). Added 2026-08-02 for refresh_pending.py.

    Why: the nightly refresh enqueues a real screener run per stale thesis, and
    each one takes ~25 minutes. If a long batch is still draining when the next
    night's run fires -- or when a one-off full rescan overlaps the scheduled
    job -- the same ticker gets queued twice and analyzed twice, wasting an hour
    and delivering two contradictory reports for one idea. Cheap to check, and
    it makes the refresh safe to run at any time, including by hand."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT message_text FROM messages WHERE status IN ('received', 'processing') "
            "AND message_text LIKE '/screener %'"
        ).fetchall()
    queued = set()
    for row in rows:
        parts = (row["message_text"] or "").split()
        if len(parts) >= 2:
            queued.add(parts[1].upper())
    return queued


def mark_sent(update_id: int, telegram_message_ids: Optional[list] = None) -> None:
    """Only call this AFTER Telegram's API actually confirms delivery -- never mark
    'sent' just because the send was attempted."""
    with _db() as conn:
        conn.execute(
            "UPDATE messages SET status='sent', completed_at=? WHERE update_id=?",
            (_now(), update_id),
        )


def mark_failed(update_id: int, error: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE messages SET status='failed', completed_at=?, error=? WHERE update_id=?",
            (_now(), error[:2000], update_id),
        )


def reset_failed_for_retry(update_id: int) -> None:
    """Put a failed message back into 'processing' so one more attempt can run
    (2026-08-09, process_queue._fixable_field_error).

    Deliberately narrow, and the caller is the only thing that decides when it
    applies: this exists for a run that finished its real work and was refused
    at the last step because one field held the wrong shape. Clearing the error
    text too, so a retry that fails differently reports its own reason rather
    than the previous one.

    Never call this to paper over a failure nobody is going to re-attempt. A
    message left in 'processing' with nothing running is invisible -- worse than
    a message that is honestly marked failed."""
    with _db() as conn:
        conn.execute(
            "UPDATE messages SET status='processing', error=NULL, completed_at=NULL "
            "WHERE update_id=? AND status='failed'",
            (update_id,),
        )


def mark_rejected(update_id: int) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE messages SET status='rejected', completed_at=? WHERE update_id=?",
            (_now(), update_id),
        )


# ---------------------------------------------------------------------------
# A6: thesis persistence
# ---------------------------------------------------------------------------

def get_thesis(ticker: str) -> Optional[dict]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM thesis WHERE ticker=?", (ticker.upper(),)).fetchone()
        if row is None:
            return None
        d = dict(row)
        for key in ("primary_setup", "alternate_setup", "position"):
            if d.get(key):
                d[key] = json.loads(d[key])
        if d.get("rejection_reasons"):
            d["rejection_reasons"] = json.loads(d["rejection_reasons"])
        return d


def _validate_setup_numeric_fields(label: str, setup: Optional[dict]) -> None:
    """Found real, 2026-07-31: WGMI's alternate_setup had target prices saved
    as strings ("67.885" not 67.885), and ASTS's primary_setup had its
    trigger saved as a descriptive sentence instead of a number, even though
    it also had a numeric stop and a passing target. Both slipped straight
    into the DB (save_thesis did no type checking at all) and only surfaced
    days later in /monitor as an unscoreable rubric ("no_numeric_target" /
    "no_numeric_entry_or_stop") -- a live trigger fired with no way to
    re-grade it, exactly what CONSISTENCY_RULES.md rule 27 was built to
    prevent. A setup with no stop yet (still watching for a level to form,
    e.g. ASTS's alternate) legitimately has a free-text trigger -- only a
    setup that already HAS a numeric stop, or already has targets, must have
    its trigger/target prices be real numbers too."""
    if not setup:
        return
    stop = setup.get("stop")
    if stop is not None:
        if not isinstance(stop, (int, float)):
            raise ValueError(f"{label}.stop must be numeric, got {stop!r}")
        trigger = setup.get("trigger")
        if not isinstance(trigger, (int, float)):
            # 2026-07-31, real CRM incident: SCREENER_v3 sometimes emits a plain
            # numeric price as a quoted string ("260.00") rather than a real free-
            # text trigger ("close above 112.67"). report_lint._clean_number
            # already draws that exact line (match from start of string, so
            # "close above..." never matches) -- reuse it to silently normalize
            # the quoted-number case instead of rejecting a perfectly good
            # trigger just because it slipped through as a string.
            cleaned = report_lint._clean_number(trigger)
            if cleaned is None:
                raise ValueError(f"{label}.trigger must be numeric once stop is set, got {trigger!r}")
            setup["trigger"] = cleaned
    for i, t in enumerate(setup.get("targets") or [], start=1):
        price = t.get("price")
        if not isinstance(price, (int, float)):
            cleaned = report_lint._clean_number(price)
            if cleaned is None:
                raise ValueError(f"{label}.targets[{i}].price must be numeric, got {price!r}")
            t["price"] = cleaned


def _normalize_setup_type(label: str, setup: Optional[dict]) -> None:
    """Rewrite a setup's `type` in place to one of the six recognised words, or
    raise if it is not one of them (2026-08-09, see bot/setup_types.py).

    This is the write-side lock on the field the shadow book groups by. Before
    it, four live rows held an entire paragraph where a label belongs, and those
    rows can never be counted with anything -- which quietly costs the research
    more than a rejected run costs the trader.

    Normalising in place rather than returning a copy is deliberate: the same
    dict is about to be serialised into BOTH `thesis.primary_setup` and the
    `ideas` row's flattened columns, and a fix applied to only one of those two
    is a new way for the same field to disagree with itself."""
    if not isinstance(setup, dict) or "type" not in setup:
        return
    setup["type"] = setup_types.require(setup.get("type"), label=label)


def _normalize_decision(decision: Optional[str]) -> Optional[str]:
    """One of the four exact decision words, or the value untouched.

    Deliberately lenient where _normalize_setup_type is strict, and the
    asymmetry is the point. A setup type that is prose is unusable and there is
    no way to recover it later. A decision word that is unrecognised is still a
    word -- it can be read, mapped and fixed afterwards -- so refusing the whole
    run over it would throw away a completed analysis to protect a field that
    was not actually lost. decision_policy already renders an unknown label with
    its own visible sign rather than guessing.

    What this DOES fix is the real, common case: "Buy" and "Buy Now" are the
    same decision, and the shadow book was counting them as two."""
    canonical = decision_policy.canonical_decision(decision)
    return canonical if canonical is not None else decision


def _setup_number(setup_json: Optional[str], field: str) -> Optional[float]:
    """One numeric field out of a stored setup JSON blob, or None when the blob
    is missing/unparseable or that field was saved as prose rather than a number
    (26 of 55 real rows had exactly that). Never parses a number out of a
    sentence -- an unmeasurable level reads as None, same posture as
    refresh_pending._numeric."""
    if not setup_json:
        return None
    try:
        value = (json.loads(setup_json) or {}).get(field)
    except (ValueError, TypeError):
        return None
    return float(value) if isinstance(value, (int, float)) else None


# ---------------------------------------------------------------------------
# ideas: the append-only build record (2026-08-07). See the ideas table comment
# in _SCHEMA for why this exists alongside thesis rather than replacing it.
# ---------------------------------------------------------------------------

def _flatten_setup(setup: Optional[dict]) -> dict:
    """The flat, queryable columns pulled out of a primary_setup dict.

    Numbers only for the numeric columns -- a trigger or stop written as prose
    (rule 14's "no order ready yet") lands in trigger_text/stop_text verbatim
    and leaves the numeric column NULL. Never parses a price out of a sentence:
    a level that was not stated as a number is not a measurable level, and
    guessing one would silently invent history that the screener never claimed.
    """
    flat = {"setup_type": None, "trigger": None, "trigger_text": None, "stop": None,
            "stop_text": None, "target_1": None, "target_2": None, "atr_at_build": None}
    if not isinstance(setup, dict):
        return flat
    flat["setup_type"] = setup.get("type")
    for field, num_key, text_key in (("trigger", "trigger", "trigger_text"),
                                     ("stop", "stop", "stop_text")):
        value = setup.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            flat[num_key] = float(value)
        elif value is not None:
            flat[text_key] = str(value)
    atr = setup.get("atr_at_build")
    if isinstance(atr, (int, float)) and not isinstance(atr, bool):
        flat["atr_at_build"] = float(atr)
    # rule 7 allows at most two sellable targets; anything past the second is a
    # runner and has no fixed price to record.
    targets = setup.get("targets") or []
    if isinstance(targets, list):
        for i, key in enumerate(("target_1", "target_2")):
            if i < len(targets):
                price = targets[i].get("price") if isinstance(targets[i], dict) else targets[i]
                if isinstance(price, (int, float)) and not isinstance(price, bool):
                    flat[key] = float(price)
    return flat


def _record_idea(conn, ticker: str, status: str, source: Optional[str], sleeve: Optional[str],
                 primary_setup: Optional[dict], alternate_setup: Optional[dict] = None,
                 position: Optional[dict] = None, rubric_grade: Optional[str] = None,
                 market_regime_at_build: Optional[str] = None, planned_qty: Optional[int] = None,
                 decision: Optional[str] = None, rejection_reasons: Optional[list] = None,
                 built_at: Optional[str] = None) -> int:
    """Append one ideas row and retire whichever build was live for this ticker.

    Takes the open connection so the supersede and the insert land in the SAME
    transaction as save_thesis's own upsert -- the identical reason
    _archive_thesis_row does, and the reason a crash mid-write cannot leave two
    live builds on one ticker.

    Returns the new ideas.id. Callers store it on whatever they are recording
    (a shadow score, a real fill) so the row points at an identifiable build
    instead of at a ticker that may since have been screened again.
    """
    ticker = ticker.upper()
    now = built_at or _now()
    row = conn.execute(
        "SELECT id, seq FROM ideas WHERE ticker=? ORDER BY seq DESC LIMIT 1", (ticker,)
    ).fetchone()
    seq = (row["seq"] + 1) if row else 1
    flat = _flatten_setup(primary_setup)
    cursor = conn.execute(
        "INSERT INTO ideas (ticker, seq, built_at, source, sleeve, status, status_at_build, "
        "decision, rubric_grade, market_regime_at_build, planned_qty, rejection_reasons, "
        "setup_type, trigger, trigger_text, stop, stop_text, target_1, target_2, atr_at_build, "
        "primary_setup, alternate_setup, position) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ticker, seq, now, source, sleeve, status, status,
            decision, rubric_grade, market_regime_at_build, planned_qty,
            json.dumps(rejection_reasons, ensure_ascii=False) if rejection_reasons else None,
            flat["setup_type"], flat["trigger"], flat["trigger_text"], flat["stop"],
            flat["stop_text"], flat["target_1"], flat["target_2"], flat["atr_at_build"],
            json.dumps(primary_setup, ensure_ascii=False) if primary_setup else None,
            json.dumps(alternate_setup, ensure_ascii=False) if alternate_setup else None,
            json.dumps(position, ensure_ascii=False) if position else None,
        ),
    )
    new_id = cursor.lastrowid
    # Retire every previously-live build for this ticker. Normally there is
    # exactly one; the loop-free UPDATE also self-heals if an older bug ever
    # left two, rather than assuming the invariant it is trying to maintain.
    conn.execute(
        "UPDATE ideas SET superseded_at=?, superseded_by_id=? "
        "WHERE ticker=? AND id<>? AND superseded_at IS NULL",
        (now, new_id, ticker, new_id),
    )
    return new_id


def _sync_live_idea_status(conn, ticker: str, status: str) -> None:
    """Mirror a thesis status change onto that ticker's live ideas row.

    Only the live row moves. A superseded build keeps the status it had when it
    was replaced -- that is its final outcome and rewriting it would re-label
    history, the exact failure the denormalized columns exist to prevent."""
    conn.execute(
        "UPDATE ideas SET status=? WHERE ticker=? AND superseded_at IS NULL",
        (status, ticker.upper()),
    )


def get_idea(idea_id: int) -> Optional[dict]:
    """One build by its id, with the JSON blobs parsed. None if there is no such row."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone()
    return _parse_idea_row(row) if row else None


def _parse_idea_row(row) -> dict:
    d = dict(row)
    for key in ("primary_setup", "alternate_setup", "position", "rejection_reasons"):
        if d.get(key):
            d[key] = _safe_json_loads(d[key], ticker=d.get("ticker"), field=key)
    return d


def get_live_idea(ticker: str) -> Optional[dict]:
    """The build that is current for this ticker -- the one thesis holds. None if
    the ticker has never been built (a sleeve-only stub row is not a build)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM ideas WHERE ticker=? AND superseded_at IS NULL ORDER BY seq DESC LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
    return _parse_idea_row(row) if row else None


def get_ideas(ticker: Optional[str] = None, live_only: bool = False,
              since: Optional[str] = None, with_numeric_trigger: bool = False) -> list[dict]:
    """Every build, newest first, filtered however the caller needs.

    This is the read side research is meant to use. Aggregating shadow or fill
    results by ticker is what produced a mixed XLF track record on 2026-08-07 --
    group by ideas.id instead.

    ticker: one symbol's full build history (all versions, not just the live one).
    live_only: only builds that have not been replaced.
    since: ISO timestamp, builds from that moment onward.
    with_numeric_trigger: skip builds whose trigger was prose -- those cannot be
        simulated or scored, so a study that needs a firing level should exclude
        them explicitly rather than silently treating a NULL as "never fired".
    """
    sql = "SELECT * FROM ideas WHERE 1=1"
    params: list = []
    if ticker:
        sql += " AND ticker=?"
        params.append(ticker.upper())
    if live_only:
        sql += " AND superseded_at IS NULL"
    if since:
        sql += " AND built_at >= ?"
        params.append(since)
    if with_numeric_trigger:
        sql += " AND trigger IS NOT NULL"
    sql += " ORDER BY built_at DESC, id DESC"
    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_parse_idea_row(r) for r in rows]


def _archive_thesis_row(conn, prior: dict, replaced_by: Optional[str]) -> None:
    """Copy a thesis row into thesis_history immediately before it is
    overwritten. Takes the open connection so the copy and the overwrite land in
    the SAME transaction -- a crash between them would otherwise be the one case
    that still loses the old plan, which is the entire thing this exists to
    prevent."""
    conn.execute(
        "INSERT INTO thesis_history (ticker, replaced_at, replaced_by, prior_decision, "
        "prior_grade, prior_trigger, prior_stop, snapshot) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            prior["ticker"], _now(), replaced_by, prior.get("decision"), prior.get("rubric_grade"),
            _setup_number(prior.get("primary_setup"), "trigger"),
            _setup_number(prior.get("primary_setup"), "stop"),
            json.dumps(prior, ensure_ascii=False, default=str),
        ),
    )


def get_thesis_changes_since(since_iso: str, tickers: Optional[list[str]] = None) -> list[dict]:
    """What actually changed in each thesis that was overwritten since since_iso:
    the archived 'before' values beside the row's current 'after' values.

    Backs refresh_pending.py's nightly before/after message -- the point being
    that a rewrite the user cannot see is a rewrite the user cannot judge. Only
    the LAST overwrite per ticker in the window is reported: a night that rebuilt
    the same ticker twice is still one net change from the reader's side."""
    params: list = [since_iso]
    clause = ""
    if tickers:
        clause = f" AND h.ticker IN ({','.join('?' * len(tickers))})"
        params.extend(t.upper() for t in tickers)
    with _db() as conn:
        rows = conn.execute(
            "SELECT h.ticker, h.prior_decision, h.prior_grade, h.prior_trigger, h.prior_stop, "
            "       t.decision AS now_decision, t.rubric_grade AS now_grade, "
            "       t.primary_setup AS now_setup, t.status AS now_status "
            "FROM thesis_history h "
            "JOIN thesis t ON t.ticker = h.ticker "
            "WHERE h.replaced_at >= ?" + clause + " "
            # one row per ticker: the most recent archive in the window
            "AND h.id = (SELECT MAX(h2.id) FROM thesis_history h2 "
            "            WHERE h2.ticker = h.ticker AND h2.replaced_at >= ?)",
            params + [since_iso],
        ).fetchall()
    changes = []
    for row in rows:
        d = dict(row)
        changes.append({
            "ticker": d["ticker"],
            "before": {"decision": d["prior_decision"], "grade": d["prior_grade"],
                        "trigger": d["prior_trigger"], "stop": d["prior_stop"]},
            "after": {"decision": d["now_decision"], "grade": d["now_grade"],
                       "trigger": _setup_number(d["now_setup"], "trigger"),
                       "stop": _setup_number(d["now_setup"], "stop"),
                       "status": d["now_status"]},
        })
    return changes


def save_thesis(ticker: str, status: str, source: str, primary_setup: dict,
                alternate_setup: Optional[dict] = None, position: Optional[dict] = None,
                rubric_grade: Optional[str] = None, market_regime_at_build: Optional[str] = None,
                planned_qty: Optional[int] = None, decision: Optional[str] = None,
                rejection_reasons: Optional[list] = None) -> int:
    """SCREENER_v3 builds this; MONITOR_v2 reads it, never re-derives a trigger from
    scratch; STRATEGY_v3 (via set_status) takes ownership once a fill is confirmed.

    rubric_grade/market_regime_at_build/planned_qty are the Journal/Pending-flow
    fields (Stage 1): the exact rubric letter and market-state classification
    SCREENER_v3 already produces, and the illustrative position size from its
    sizing table -- planned_qty is informational only (see /filled in
    STRATEGY_v3.md), never a commitment and never used to infer entry_type.

    decision/rejection_reasons (item 7, Hardening Pass, shadow-book capture):
    decision is the exact SCREENER_v3.md decision-line value ("Buy Now" / "Buy
    Only If Confirmed" / "Watchlist" / "No Trade") -- called on EVERY screener
    run regardless of outcome, same as everything else this function already
    persists unconditionally. rejection_reasons is a plain list of which
    gate/rubric items actually failed (e.g. ["rr_below_2", "regime_against"]),
    empty/None for a clean Buy Now. Note: thesis is keyed on ticker (upsert) --
    re-screening the same ticker overwrites its prior decision/rejection_reasons
    here, same as every other field on this row. Since 2026-08-03 the row being
    overwritten is first copied into thesis_history (see that table's comment):
    the overwrite still happens exactly as before, but the previous plan is no
    longer destroyed by it. shadow_outcomes is the separate append-only side of
    the shadow-book capture, not this.

    Since 2026-08-07 every call also appends an `ideas` row and retires the
    ticker's previous one, in the same transaction. That row -- not this
    ticker-keyed one -- is the unit research measures: three XLF builds are
    three ideas with three scorecards, where thesis can only ever hold the
    newest. Returns the new ideas.id so a caller that goes on to record
    something about this specific build (a fill, a simulated result) can point
    at it exactly. Existing callers that ignore the return value are unaffected."""
    _validate_setup_numeric_fields("primary_setup", primary_setup)
    _validate_setup_numeric_fields("alternate_setup", alternate_setup)
    _normalize_setup_type("primary_setup", primary_setup)
    _normalize_setup_type("alternate_setup", alternate_setup)
    decision = _normalize_decision(decision)
    with _db() as conn:
        existing = conn.execute("SELECT * FROM thesis WHERE ticker=?", (ticker.upper(),)).fetchone()
        sleeve = existing["sleeve"] if existing else "unknown"
        # Was this ticker already a real idea before this run? A sleeve-only stub
        # row (set_sleeve inserts one with no primary_setup) does not count --
        # there is no prior plan to preserve.
        prior = dict(existing) if existing and existing["primary_setup"] else None
        # ...and was it on the ACTIVE waiting list specifically? Only those are
        # protected from automatic cold-listing below. A row that was already
        # cold is not on the list the user reads, so leaving it cold removes
        # nothing from him -- while treating it as "protected" would quietly
        # promote every failed cold re-check back onto the active list, the
        # opposite of what rule 29 exists to do.
        prior_was_active = bool(prior) and prior.get("status") == "pending"
        if prior:
            _archive_thesis_row(conn, prior, replaced_by=source)
        conn.execute(
            "INSERT INTO thesis (ticker, status, sleeve, source, date_built, primary_setup, "
            "alternate_setup, position, rubric_grade, market_regime_at_build, planned_qty, "
            "decision, rejection_reasons, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ticker) DO UPDATE SET status=excluded.status, source=excluded.source, "
            "date_built=excluded.date_built, primary_setup=excluded.primary_setup, "
            "alternate_setup=excluded.alternate_setup, position=excluded.position, "
            "rubric_grade=excluded.rubric_grade, market_regime_at_build=excluded.market_regime_at_build, "
            "planned_qty=excluded.planned_qty, decision=excluded.decision, "
            "rejection_reasons=excluded.rejection_reasons, updated_at=excluded.updated_at",
            (
                ticker.upper(), status, sleeve, source, _now(),
                json.dumps(primary_setup, ensure_ascii=False),
                json.dumps(alternate_setup, ensure_ascii=False) if alternate_setup else None,
                json.dumps(position, ensure_ascii=False) if position else None,
                rubric_grade, market_regime_at_build, planned_qty,
                decision, json.dumps(rejection_reasons, ensure_ascii=False) if rejection_reasons else None,
                _now(),
            ),
        )
        # Same transaction as the upsert above, deliberately: the append and the
        # overwrite it corresponds to must not be separable by a crash.
        idea_id = _record_idea(
            conn, ticker, status=status, source=source, sleeve=sleeve,
            primary_setup=primary_setup, alternate_setup=alternate_setup, position=position,
            rubric_grade=rubric_grade, market_regime_at_build=market_regime_at_build,
            planned_qty=planned_qty, decision=decision, rejection_reasons=rejection_reasons,
        )
    # Cold list routing, rule 29 (2026-08-03). Done HERE rather than left to the
    # caller for the same reason the rubric became a formula: a step the writer
    # has to remember is a step that eventually gets forgotten, and this one
    # decides whether an idea stays on the list the user actually reads.
    #
    # Narrowed the same day, on the user's own instruction: this never fires for
    # an idea that was ALREADY on the active waiting list. A nightly rebuild of
    # an idea the user is actively watching must not move it off that list by
    # itself -- that is the script silently deciding, which is exactly what
    # refresh_pending.py's own docstring forbids ("Nothing is ever deleted
    # automatically... he drops them, not the script"). Such a rebuild keeps
    # status='pending' and gets REPORTED with its /drop line instead; the user
    # decides. A genuinely new No Trade, and a cold idea whose re-check still
    # finds nowhere to sell, both go cold exactly as rule 29 intends.
    if status == "pending" and decision == "No Trade" and not prior_was_active:
        set_cold(ticker)
    elif status == "pending" and decision:
        # A cold idea that came back with somewhere to sell is a live idea
        # again -- clear its retry count so it gets a full set of chances if it
        # ever goes cold once more. Guarded on `decision` being present so a
        # sleeve-only insert never resets a real counter. dead_nights goes with
        # it: this build's stop is a NEW stop, so a streak measured against the
        # old one says nothing about it.
        with _db() as conn:
            conn.execute("UPDATE thesis SET cold_rechecks=0, dead_nights=0 WHERE ticker=?",
                          (ticker.upper(),))
    return idea_id


def set_status(ticker: str, status: str) -> None:
    if status not in ("pending", "open_position", "closed", "dropped", "cold"):
        raise ValueError(f"invalid status {status!r}")
    with _db() as conn:
        conn.execute(
            "UPDATE thesis SET status=?, updated_at=? WHERE ticker=?",
            (status, _now(), ticker.upper()),
        )
        _sync_live_idea_status(conn, ticker, status)


def drop_thesis(ticker: str, reason: str) -> None:
    """/drop TICKER reason -- soft-delete only. The row and its full history stay
    in the DB forever (same audit-trail principle as /close in the old design);
    dropping just excludes it from get_pending_theses()'s query filter going
    forward, per Stage 2."""
    with _db() as conn:
        conn.execute(
            "UPDATE thesis SET status='dropped', drop_reason=?, updated_at=? WHERE ticker=?",
            (reason, _now(), ticker.upper()),
        )
        conn.execute(
            "UPDATE ideas SET status='dropped', drop_reason=? WHERE ticker=? AND superseded_at IS NULL",
            (reason, ticker.upper()),
        )


def require_thesis_or_flag(ticker: str) -> Optional[dict]:
    """MONITOR_v2's continuation-check path must branch explicitly on whether a
    stored thesis actually exists -- this makes "there is nothing to read" a
    checkable return value instead of an implicit assumption a live check could
    silently paper over by fabricating a trigger. Returns get_thesis(ticker)'s
    result verbatim (None if nothing stored); callers MUST treat None as "run
    MONITOR_v2.md section ד's ad-hoc path," never as "assume a trigger anyway."""
    return get_thesis(ticker)


# ---------------------------------------------------------------------------
# X/Twitter account feed (2026-07-22, bot/fetch_x_feed.py). Category A storage
# only -- see CONSISTENCY_RULES.md's x_posts guardrail for how the reasoning
# step is (and is not) allowed to use this data.
# ---------------------------------------------------------------------------

# Accounts whose posts matter regardless of ticker (macro/breaking-news, not
# company-specific) -- used by get_recent_macro_posts() for regime-override
# context. Subset of fetch_x_feed.py's X_ACCOUNTS, not a separate list to
# maintain -- kept here (not there) since it's a persistence-layer read
# concern, not part of what gets fetched.
MACRO_X_ACCOUNTS = (
    "elonmusk", "jimcramer", "NickTimiraos", "LizAnnSonders", "KevRGordon",
    "KobeissiLetter", "DeItaone", "StockMKTNewz", "LiveSquawk", "eWhispers",
)


def record_x_post(post_id: str, account: str, posted_at: str, text: str, url: str,
                   tickers: list[str]) -> None:
    """INSERT OR IGNORE keyed on post_id -- fetch_x_feed.py polls the same
    accounts every 15-30 min and will see already-stored tweets again on every
    run; this makes re-running always safe, never a duplicate row."""
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO x_posts (post_id, account, posted_at, text, url, tickers, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (post_id, account, posted_at, text, url, json.dumps(tickers), _now()),
        )


def get_recent_posts_for_ticker(ticker: str, hours: int = 24) -> list[dict]:
    """Posts whose extracted cashtags include ticker, posted within the last
    `hours`. Filtered in Python (not SQL LIKE) against the parsed JSON list --
    a substring LIKE match on the stored JSON text could false-positive (e.g.
    "NVDA" inside a longer stored string) in a way an exact list-membership
    check can't."""
    ticker = ticker.upper()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _db() as conn:
        rows = conn.execute(
            "SELECT account, posted_at, text, url, tickers FROM x_posts "
            "WHERE posted_at >= ? ORDER BY posted_at DESC",
            (cutoff,),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        post_tickers = _safe_json_loads(d.pop("tickers") or "[]", ticker=ticker, field="x_posts.tickers") or []
        if ticker in post_tickers:
            d["tickers"] = post_tickers
            out.append(d)
    return out


def get_recent_macro_posts(hours: int = 6) -> list[dict]:
    """Posts from MACRO_X_ACCOUNTS (not ticker-specific) within the last
    `hours` -- regime-override/monitor-note context regardless of which
    ticker is being analyzed."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    placeholders = ",".join("?" for _ in MACRO_X_ACCOUNTS)
    with _db() as conn:
        rows = conn.execute(
            f"SELECT account, posted_at, text, url FROM x_posts "
            f"WHERE posted_at >= ? AND account IN ({placeholders}) ORDER BY posted_at DESC",
            (cutoff, *MACRO_X_ACCOUNTS),
        ).fetchall()
    return [dict(row) for row in rows]


def get_known_tickers() -> set[str]:
    """Every ticker with any thesis row, any status -- already looked at/
    tracked by the system at some point (screened, pending, open, dropped,
    whatever). Used by get_new_candidate_tickers() to never flag a ticker the
    user has already had a real screener decision on."""
    with _db() as conn:
        rows = conn.execute("SELECT DISTINCT ticker FROM thesis").fetchall()
    return {row["ticker"] for row in rows}


def get_new_candidate_tickers(hours: int = 24) -> list[dict]:
    """Idea-sourcing step for fetch_x_feed.py's end-of-run alert (2026-07-22,
    explicit user request: "send me names of potential tickers and I'll run
    screener on them" -- this is deliberately just a name-surfacing step, it
    never runs or substitutes for the real /screener gate).

    A ticker counts as a new candidate when: its cashtag appears in x_posts
    within the last `hours` (fresh enough to still matter), it has no thesis
    row at all (get_known_tickers() -- already-tracked tickers are never
    re-flagged as "new"), and it has never been alerted before
    (x_candidate_alerts -- each ticker is surfaced to the user exactly once,
    ever; no cooldown/re-alert logic yet, deliberately simple for a first
    version). One row per new ticker, evidenced by its earliest matching post
    in the window. Caller (fetch_x_feed.py) is responsible for calling
    record_candidate_alerted() after a successful Telegram send -- this
    function only reads, it never marks anything as alerted itself."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    known = get_known_tickers()
    with _db() as conn:
        rows = conn.execute(
            "SELECT account, posted_at, text, url, tickers FROM x_posts "
            "WHERE posted_at >= ? ORDER BY posted_at ASC",
            (cutoff,),
        ).fetchall()
        already_alerted = {r["ticker"] for r in conn.execute("SELECT ticker FROM x_candidate_alerts")}
    seen_this_call = set()
    out = []
    for row in rows:
        d = dict(row)
        post_tickers = _safe_json_loads(d.pop("tickers") or "[]", ticker=d["account"], field="x_posts.tickers") or []
        for t in post_tickers:
            if t in known or t in already_alerted or t in seen_this_call:
                continue
            seen_this_call.add(t)
            out.append({"ticker": t, "account": d["account"], "posted_at": d["posted_at"],
                        "url": d["url"], "text": d["text"]})
    return out


def record_candidate_alerted(ticker: str, account: str, posted_at: str, url: str, text: str) -> None:
    """INSERT OR IGNORE keyed on ticker -- see x_candidate_alerts' own schema
    comment for why this is a permanent, once-ever record, not append-only
    history."""
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO x_candidate_alerts (ticker, account, posted_at, url, text, alerted_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ticker, account, posted_at, url, text, _now()),
        )


# ---------------------------------------------------------------------------
# Journal/Pending/Closing-Summary flow, Stage 2: /pending and /drop
# ---------------------------------------------------------------------------

TRIGGER_STALE_TRADING_DAYS = 2


def get_stored_trigger(ticker: str) -> Optional[float]:
    """The stored primary setup's trigger, or None when it isn't a number
    (2026-08-08). 26 of 55 real theses once held a whole sentence in that field,
    so "there is a value" and "there is a level" are different questions -- same
    never-parse-a-number-out-of-prose standard refresh_pending.py applies."""
    thesis = get_thesis(ticker)
    setup = (thesis or {}).get("primary_setup") or {}
    value = setup.get("trigger") if isinstance(setup, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


def record_decision_transition(ticker: str, *, status: str, price: Optional[float] = None,
                                trigger_price: Optional[float] = None,
                                rubric_grade_now: Optional[str] = None) -> bool:
    """Write down what a thesis was called at the moment its trigger confirmed
    (2026-08-08). True if this call created the row, False if it was already
    there or there was nothing to record.

    Idempotent by design, not by luck: /monitor and the nightly /monitorall both
    keep seeing the same green for days, so this is called over and over for one
    real event. The UNIQUE (ticker, date_built) index takes the first one and
    quietly drops the rest -- the FIRST confirmation is the fact worth keeping,
    and a later call must never overwrite it with a staler-looking grade.

    Reads the stored decision and grade at call time on purpose. Both are single
    mutable columns that the next /screener rebuild overwrites in place, so this
    is the only moment the pairing exists. See the table comment for why.
    """
    thesis = get_thesis(ticker)
    if not thesis or not thesis.get("date_built"):
        return False
    built = str(thesis["date_built"])
    with _db() as conn:
        # When it fired, not when we got round to writing it down. These two are
        # the same on the night it happens and days apart afterwards -- the same
        # green is re-reported every run until the idea is filled or dropped, so
        # a plain _now() would stamp a Monday scan onto a Friday confirmation.
        #
        # No green on record means no confirmation to document, and this returns
        # False rather than stamping the current time. Both real callers write
        # their monitor_log row immediately BEFORE calling this, so a genuine
        # live confirmation is always already here. Found by running a backfill
        # over all 58 stored theses: an earlier version fell back to now() and
        # invented a "fired today" for the 33 that had never gone green at all.
        first_green = conn.execute(
            # Full timestamp, not date(): get_trigger_fired_age compares whole
            # days because it measures an age in trading days, but a green
            # logged EARLIER on the same day a thesis was rebuilt belongs to the
            # build that was replaced. Caught on the first real row -- PLTR's
            # 20:35 green was picked up for a thesis built at 22:19 that same
            # night, pairing a confirmation with a decision written after it.
            "SELECT checked_at FROM monitor_log WHERE ticker=? AND status='green' "
            "AND checked_at >= ? ORDER BY checked_at ASC LIMIT 1",
            (ticker.upper(), built),
        ).fetchone()
        if first_green is None:
            return False
        occurred_at = str(first_green["checked_at"])
        cur = conn.execute(
            "INSERT OR IGNORE INTO decision_transitions "
            "(ticker, occurred_at, date_built, decision_stored, status, price, "
            " trigger_price, rubric_grade_stored, rubric_grade_now) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker.upper(), occurred_at, built, thesis.get("decision"),
             status, price, trigger_price, thesis.get("rubric_grade"), rubric_grade_now),
        )
        return cur.rowcount > 0


def get_decision_transitions(ticker: Optional[str] = None) -> list[dict]:
    """Every recorded "it was called X, then it confirmed" moment, newest first.

    No judgment here, same posture as the shadow book: this is the raw record of
    how often ideas the system said not to buy went on to fire anyway. Whether
    that means the decisions are too cautious is a question for a review with
    enough rows to answer it, not for one ticker on one night."""
    sql = "SELECT * FROM decision_transitions"
    params: tuple = ()
    if ticker:
        sql += " WHERE ticker=?"
        params = (ticker.upper(),)
    sql += " ORDER BY occurred_at DESC, id DESC"
    with _db() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def get_trigger_fired_age(ticker: str) -> Optional[dict]:
    """How long ago this ticker's trigger first confirmed (status 'green'),
    counted in real trading days from the first green logged AT OR AFTER the
    thesis was built. None when no green has ever been logged for the current
    thesis -- there is nothing to age.

    Why (2026-08-02): /monitorall re-reports a fired trigger as freshly
    actionable on every single run, forever, because the thesis stays 'pending'
    until a real /filled. Found real in the live log -- 110 green rows, the same
    tickers repeating night after night. MSFT's trigger of 389.03 first
    confirmed 2026-07-16 and was still producing "🟢 trigger fired!" headlines
    on 2026-07-30 with price at 451, roughly 15% above the level the buy was
    supposed to happen at. The buy order that headline implies has the original
    stop, so the further price runs, the worse the real risk gets -- an alert
    that quietly turns into a chase invitation.

    Returned as data only: the STALENESS decision (MONITOR_v2.md) reads
    `trading_days` against TRIGGER_STALE_TRADING_DAYS. Nothing here judges."""
    thesis = get_thesis(ticker)
    if not thesis or not thesis.get("date_built"):
        return None
    built = str(thesis["date_built"])[:10]
    with _db() as conn:
        row = conn.execute(
            "SELECT checked_at FROM monitor_log WHERE ticker=? AND status='green' "
            "AND date(checked_at) >= date(?) ORDER BY checked_at ASC LIMIT 1",
            (ticker.upper(), built),
        ).fetchone()
    if row is None:
        return None
    first_green = str(row["checked_at"])[:10]
    today = datetime.now(timezone.utc).date().isoformat()
    trading_days = count_trading_days(first_green, today)
    return {
        "first_green_date": first_green,
        "trading_days": trading_days,
        "stale": trading_days > TRIGGER_STALE_TRADING_DAYS,
    }


def log_monitor_check(ticker: str, status: str, price: Optional[float] = None,
                       distance_atr: Optional[float] = None,
                       is_active_rejection: bool = False, note: Optional[str] = None) -> None:
    """Every MONITOR_v2 check (continuation or ad-hoc) appends one row here --
    append-only, no FK to thesis (an ad-hoc check on an unscreened ticker still
    logs). This is /pending's actual data source: it reads the latest row per
    ticker, not live-rechecking every ticker itself every time it's called."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO monitor_log (ticker, checked_at, status, price, distance_atr, "
            "is_active_rejection, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticker.upper(), _now(), status, price, distance_atr, int(is_active_rejection), note),
        )


def count_trading_days(start_date: str, end_date: str) -> int:
    """Real NYSE trading-day count between two ISO dates (YYYY-MM-DD), inclusive
    of both ends -- B2's real-calendar principle applied here too, not naive
    weekday counting (which overcounts across every US market holiday)."""
    schedule = _NYSE.schedule(start_date=start_date, end_date=end_date)
    return len(schedule)


# Trend-following/Risk-On ... structure-break -- ascending risk-off-ness. Only
# flags a stored thesis when CURRENT is strictly more risk-off than at build time
# (degradation only -- a thesis built in a choppy market that's since turned
# healthy should never flag).
_REGIME_RISK_ORDER = [
    "risk_on", "healthy_uptrend", "pullback_in_uptrend",
    "neutral_choppy", "risk_off", "structure_break",
]

PENDING_AGE_FLAG_DAYS = 10  # named constant per the spec, not a magic number

# How many trading days a position may sit as entry_type='starter' before
# /playbook and /positions call out the staleness explicitly (shorter than
# PENDING_AGE_FLAG_DAYS -- this is already live capital, not a watchlist idea).
STARTER_STALE_TRADING_DAYS = 5

# Deliberately larger than the 0.3x-ATR noise floor MONITOR_v2.md uses for its
# own 🟢-moment trigger/stop deviation warning -- that threshold means "still
# noise, don't mention it"; this one means "worth a manual look; consider
# re-running /screener TICKER". Compared against the live price last observed
# via /monitor (monitor_log.price), never a fresh fetch -- see move_flag below.
PENDING_MOVE_FLAG_ATR_MULT = 1.0


def is_regime_more_risk_off(built_regime: str, current_regime: str) -> bool:
    if built_regime not in _REGIME_RISK_ORDER or current_regime not in _REGIME_RISK_ORDER:
        return False  # unrecognized label -- can't compare, don't guess a flag either way
    return _REGIME_RISK_ORDER.index(current_regime) > _REGIME_RISK_ORDER.index(built_regime)


def pending_sort_key(row: dict) -> tuple:
    """Tuple sort -- Python compares tuples left-to-right, which does the spec's
    tiered ordering directly: yellow_plus first, then yellow-with-active-
    rejection, then yellow-normal, then white sorted by ATR-distance ascending.
    green/red shouldn't appear in /pending's result set at all once Stage 1's
    status-flip wiring has actually fired on them -- if one does, that's a signal
    the flip was missed, not something to silently sort to the bottom."""
    status = row.get("latest_status") or "white"
    tier = {"yellow_plus": 0, "yellow": 1, "white": 2}.get(status, 3)
    if status == "yellow":
        sub_tier = 0 if row.get("latest_is_active_rejection") else 1
    else:
        sub_tier = 0
    atr_distance = row.get("latest_distance_atr") if status == "white" else 0.0
    return (tier, sub_tier, atr_distance if atr_distance is not None else 0.0)


def get_pending_report_rows(current_regime: Optional[str] = None) -> list[dict]:
    """/pending's actual query: every thesis with status='pending' AND an actual
    primary_setup, joined with its latest monitor_log row (if any), with
    days_pending (real trading-day count) and flag/flag_reasons computed.
    current_regime is the market classification /pending's own SPY/QQQ fetch
    just produced -- pass None to skip the regime-flag check entirely
    (age-flag and move-flag still apply).

    'moved' flag_reason: the price last observed via /monitor has drifted at
    least PENDING_MOVE_FLAG_ATR_MULT x atr_at_build from the stored trigger --
    purely informational (see move_flag below), same as age/regime. Never
    recomputes or gates the stored trigger/stop/target themselves.

    The primary_setup IS NOT NULL filter is deliberate, found live (2026-07-08):
    set_sleeve() inserts a thesis row with status='pending' by default for any
    ticker not yet in the table, purely to record a core/swing classification --
    it never touches primary_setup. Without this filter, every sleeve-tagged
    ticker with no actual screened thesis (SPY, QQQ, and any other portfolio
    holding just classified for sleeve purposes) showed up in /pending
    indistinguishable from a real awaiting-trigger trade idea. Sleeve
    classification and "a thesis is pending a trigger" are orthogonal facts
    about a ticker; only the latter belongs in /pending.

    An unrecognized label must never just silently disable the regime-flag
    check the way is_regime_more_risk_off() itself does internally (that
    function's False-on-unrecognized return is correct for a pure comparison,
    but /pending is the one place a human actually reads the output, so drift
    here has to surface, not vanish). current_regime is fully under the
    caller's control for this one call -- an unrecognized value is treated as
    a caller bug and raised immediately, not swallowed. market_regime_at_build
    is historical data on a stored thesis row that may predate a later wording
    change to SCREENER_v3.md; a single bad row shouldn't take down the whole
    /pending report, so that case surfaces as a visible per-row flag_reason
    instead (see 'regime_label_unrecognized' below)."""
    if current_regime is not None and current_regime not in _REGIME_RISK_ORDER:
        raise ValueError(
            f"current_regime={current_regime!r} is not one of {_REGIME_RISK_ORDER} -- "
            "check for wording drift against STRATEGY_v3.md §ב's six labels "
            "(see MASTER_SYSTEM_SPEC.md §9 for the Hebrew->snake_case mapping)"
        )
    with _db() as conn:
        rows = conn.execute("""
            SELECT t.*, m.status as latest_status, m.price as latest_price,
                   m.distance_atr as latest_distance_atr,
                   m.is_active_rejection as latest_is_active_rejection,
                   m.note as latest_note, m.checked_at as latest_checked_at
            FROM thesis t
            LEFT JOIN (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY checked_at DESC) as rn
                FROM monitor_log
            ) m ON m.ticker = t.ticker AND m.rn = 1
            WHERE t.status = 'pending' AND t.primary_setup IS NOT NULL
        """).fetchall()

    today = _now()[:10]
    result = []
    for row in rows:
        d = dict(row)
        for key in ("primary_setup", "alternate_setup", "position"):
            if d.get(key):
                d[key] = _safe_json_loads(d[key], ticker=d["ticker"], field=key)
        days_pending = count_trading_days(d["date_built"][:10], today) if d.get("date_built") else None
        age_flag = (days_pending or 0) >= PENDING_AGE_FLAG_DAYS
        built_regime = d.get("market_regime_at_build")
        regime_label_bad = bool(built_regime and built_regime not in _REGIME_RISK_ORDER)
        if regime_label_bad:
            _logger.warning(
                "ticker=%s has unrecognized market_regime_at_build=%r (not in %s) -- "
                "regime-flag check skipped for this row, surfaced as regime_label_unrecognized "
                "instead of silently disabled",
                d.get("ticker"), built_regime, _REGIME_RISK_ORDER,
            )
        regime_flag = bool(
            current_regime and built_regime and not regime_label_bad
            and is_regime_more_risk_off(built_regime, current_regime)
        )
        # "Moved since build" flag -- purely informational, never a re-derivation
        # of the stored trigger/stop/target (MONITOR_v2.md's frozen-thesis
        # principle applies here too). Uses the live price last observed via
        # /monitor (monitor_log.price, already joined above as latest_price) --
        # never a fresh fetch, same "no live TradingView fetch" behavior /pending
        # already has. No monitor_log row yet, or no atr_at_build on either
        # setup, means there's nothing to compare against -- skip, don't guess.
        # trigger is documented (deliver_report.py's own schema comment) as a free-form
        # string as often as a bare number -- e.g. "no order ready; trigger determined
        # only after a confirmation candle forms" for a not-yet-triggered setup -- so it
        # must go through the same _clean_number() extraction chart_draw.py/report_lint.py
        # already use, not a raw subtraction (found real, 2026-07-22: PLTR/MSFT/ARM/GEV/etc
        # all crashed /monitorall's get_pending_report_rows() with "unsupported operand
        # type(s) for -: 'float' and 'str'" the moment any of them had a monitor_log price).
        latest_price = d.get("latest_price")
        setup_for_move = d.get("primary_setup") or d.get("alternate_setup")
        move_flag = False
        if latest_price is not None and setup_for_move:
            move_trigger = _clean_number(setup_for_move.get("trigger"))
            move_atr = _clean_number(setup_for_move.get("atr_at_build"))
            if move_trigger is not None and move_atr:
                move_flag = abs(latest_price - move_trigger) >= PENDING_MOVE_FLAG_ATR_MULT * move_atr
        d["days_pending"] = days_pending
        d["flag"] = age_flag or regime_flag or regime_label_bad or move_flag
        d["flag_reasons"] = [
            r for r, cond in (
                ("age", age_flag), ("regime", regime_flag),
                ("regime_label_unrecognized", regime_label_bad),
                ("moved", move_flag),
            ) if cond
        ]
        result.append(d)

    result.sort(key=pending_sort_key)
    return result


# ---------------------------------------------------------------------------
# A7 (migrated from the flat-JSON version once A6 landed, per A7's own plan)
# ---------------------------------------------------------------------------

_CORE_TICKERS = {"SPY", "QQQ"}  # hard rule (2026-07-13, user-specified): these two
# are always Core; every other ticker is always Swing. Supersedes the earlier
# "ask, don't assume" A7 default of "unknown" -- that default existed because the
# system couldn't tell Core from Swing on its own; the user has now settled the
# question for every ticker permanently, so there is no more "unclear" case left
# to gate on. output_gate.py's sleeve_known check is effectively always True now.


def get_sleeve(ticker: str) -> str:
    """SPY/QQQ -> "core", everything else -> "swing", unconditionally -- ignores
    whatever (if anything) is stored in the thesis table's sleeve column, since
    the hard rule above already answers every ticker with no ambiguity left."""
    return "core" if ticker.upper() in _CORE_TICKERS else "swing"


def set_sleeve(ticker: str, sleeve: str) -> None:
    """Only call after the user has explicitly confirmed the classification --
    never inferred from position size, age, or a guess."""
    if sleeve not in VALID_SLEEVES:
        raise ValueError(f"sleeve must be one of {VALID_SLEEVES}, got {sleeve!r}")
    with _db() as conn:
        existing = conn.execute("SELECT ticker FROM thesis WHERE ticker=?", (ticker.upper(),)).fetchone()
        if existing:
            conn.execute("UPDATE thesis SET sleeve=?, updated_at=? WHERE ticker=?",
                         (sleeve, _now(), ticker.upper()))
        else:
            conn.execute(
                "INSERT INTO thesis (ticker, status, sleeve, updated_at) VALUES (?, 'pending', ?, ?)",
                (ticker.upper(), sleeve, _now()),
            )


# ---------------------------------------------------------------------------
# B1 feed: per-run data-coverage metadata
# ---------------------------------------------------------------------------

def record_analysis_run(ticker: Optional[str], protocol: str, bars_received: int,
                         date_start: str, date_end: str, history_complete: bool,
                         ath_verified: bool) -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO analysis_runs (ticker, protocol, run_at, bars_received, date_start, "
            "date_end, history_complete, ath_verified) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker, protocol, _now(), bars_received, date_start, date_end,
             int(history_complete), int(ath_verified)),
        )
        return cur.lastrowid


def record_sent(run_id: int, telegram_message_ids: list) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE analysis_runs SET telegram_message_ids=?, sent_confirmed=1 WHERE id=?",
            (json.dumps(telegram_message_ids), run_id),
        )


def record_lint_result(ticker: Optional[str], protocol: str, lint_result: dict) -> int:
    """Hardening Pass item 3: report_lint.py's full finding/skipped detail (its
    own .to_dict()), logged for EVERY delivered report -- pass or fail, so a
    failed lint always has a permanent record, not just a transient Telegram
    warning. A fresh analysis_runs row (bars_received/date_start/date_end/
    history_complete/ath_verified stay at schema defaults -- this call's only
    job is the lint_result column; those B1 coverage fields are a separate,
    still-unwired call site via record_analysis_run())."""
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO analysis_runs (ticker, protocol, run_at, lint_result) VALUES (?, ?, ?, ?)",
            (ticker, protocol, _now(), json.dumps(lint_result, ensure_ascii=False)),
        )
        return cur.lastrowid


def get_atr_at_build(ticker: str) -> Optional[float]:
    """Hardening Pass item 3: report_lint.py's fallback ATR source for
    MONITOR_v2/STRATEGY_v3 reports, whose own decision JSON carries no ATR
    figure of its own (see report_lint.lint_monitor_decision/
    lint_playbook_decision). Prefers the ticker's OPEN position's own
    entry_setup.atr_at_build snapshot (the exact figure that setup's stop/
    targets were computed from at fill time -- see positions schema comment);
    falls back to the stored thesis's primary_setup.atr_at_build if there's no
    open position yet (a MONITOR_v2 check on a still-pending thesis). Returns
    None (never a guess or a freshly-recomputed substitute) if neither exists."""
    with _db() as conn:
        pos = conn.execute(
            "SELECT entry_setup FROM positions WHERE ticker=? AND status='open' ORDER BY id DESC LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
        if pos and pos["entry_setup"]:
            atr = json.loads(pos["entry_setup"]).get("atr_at_build")
            if atr is not None:
                return atr
        thesis = conn.execute("SELECT primary_setup FROM thesis WHERE ticker=?", (ticker.upper(),)).fetchone()
        if thesis and thesis["primary_setup"]:
            return json.loads(thesis["primary_setup"]).get("atr_at_build")
        return None




# ---------------------------------------------------------------------------
# Journal/Pending-flow Stage 3 -- the two write paths (/filled, /exit) and
# the auto-generated closing summary.
# ---------------------------------------------------------------------------

def find_possible_duplicate_fill(ticker: str, qty: int, entry_date: str) -> Optional[dict]:
    """Idempotency check, concretely defined per the plan: same ticker + qty
    within +/-5% + entry_date within the same or immediately preceding
    trading day as an existing OPEN position = probable duplicate. Never
    silently merges (that risks corrupting R-multiple math on two genuinely
    separate same-day adds) -- returns the candidate row so the caller can
    ask for confirmation via mark_awaiting_reply(..., kind='duplicate_fill_confirm')
    instead of writing blindly. Returns None if nothing looks like a duplicate."""
    with _db() as conn:
        candidates = conn.execute(
            "SELECT * FROM positions WHERE ticker=? AND status='open' ORDER BY id DESC",
            (ticker.upper(),),
        ).fetchall()
    prior_trading_day = _NYSE.schedule(
        start_date=(datetime.fromisoformat(entry_date) - timedelta(days=10)).date().isoformat(),
        end_date=entry_date,
    )
    prior_day = prior_trading_day.index[-2].date().isoformat() if len(prior_trading_day) >= 2 else entry_date
    for row in candidates:
        d = dict(row)
        qty_ratio = abs(d["qty"] - qty) / d["qty"] if d["qty"] else 1.0
        if qty_ratio <= 0.05 and d["entry_date"][:10] in (entry_date, prior_day):
            return d
    return None


_NON_BUY_DECISIONS = {"no trade", "watchlist"}
_NON_BUY_GRADES = {"D", "F"}


def classify_override(decision: Optional[str], rubric_grade: Optional[str],
                       trigger_confirmed: bool = False) -> Optional[dict]:
    """2026-08-02. Pure, no DB -- given what a thesis already SAID, decide whether
    a fill against it is an override of the system's own verdict.

    Why this exists: the live record shows real fills taken against the system's
    own non-buy verdicts (GOOGL 2026-07-28 on a 'No Trade'/F thesis, ASTS
    2026-07-31 on a 'Watchlist'/D one). Those trades then sit in the journal
    indistinguishable from system-approved ones, so the one question worth asking
    -- 'when the user overrules the system, is he right?' -- is unanswerable.
    Tagging is deliberately automatic: nothing extra for the user to type, per his
    own direction, because a tag that depends on remembering to type it is a tag
    that won't be there when it matters.

    Category A bookkeeping only, never a gate: this never blocks or resizes a
    fill, it only records what was already on file, exactly like rules 19-22's
    disclosure-only family. Returns None when the fill agreed with the system.

    `trigger_confirmed` (2026-08-09) is the fix for a false positive that was
    poisoning the one question above. The stored `decision` is written once, by
    /screener, on the night the idea is built, and nothing ever rewrites it --
    so an idea whose trigger has since confirmed on a settled daily close still
    carries the word it was born with. By decision_policy's OWN definitions that
    idea is now "Buy Now": *"all of the above AND the trigger already confirmed
    on a real settled daily close... in practice this comes from MONITOR_v2's
    green."* Buying it is the system working, not the user overruling it.

    Every one of the four open positions taken from a screened idea was tagged
    an override of "Watchlist" on exactly this mistake. The owner's own account
    of what he did: *"i bought the stocks after the trigger was on and not
    before."* Left alone, the override column would have measured obedience and
    reported rebellion.

    The GRADE half is untouched by a fired trigger, and that is not an
    oversight: a D-graded setup whose price reached the entry is still a
    D-graded setup. Price confirming says nothing about whether the idea was
    any good, which is the whole thing the grade is claiming to know."""
    decision_is_non_buy = (bool(decision) and decision.strip().lower() in _NON_BUY_DECISIONS
                            and not trigger_confirmed)
    grade_is_non_buy = bool(rubric_grade) and rubric_grade.strip().upper() in _NON_BUY_GRADES
    if not (decision_is_non_buy or grade_is_non_buy):
        return None
    return {
        "decision": decision if decision_is_non_buy else None,
        "grade": rubric_grade if grade_is_non_buy else None,
    }


def create_position(ticker: str, entry_date: str, entry_price: float, qty: int,
                     entry_type: str, entry_setup: dict, initial_stop: Optional[float],
                     entry_commission: Optional[float] = None,
                     current_stop: Optional[float] = None) -> int:
    """/filled's write, only ever called after the starter/full question has
    actually been answered -- entry_type is never guessed from planned_qty or
    anything else (that's the user's own capital-allocation decision).
    entry_setup is the exact thesis setup (primary or alternate, whichever
    triggered) copied verbatim -- this is what /exit matches exit prices
    against later, not a re-lookup against thesis's possibly-since-changed
    setups. Also flips the thesis to 'open_position' here -- this IS the fill
    confirmation event STRATEGY_v3.md's own wiring describes, not something
    that waits for the next full STRATEGY_v3 report to notice.

    initial_stop is Optional -- None only for a legacy holding backfilled
    without a genuine original-risk stop on file (found real 2026-07-09,
    GOOGL). A fresh /filled entry always has a real one; never pass None just
    to avoid asking.

    current_stop defaults to initial_stop (the normal case -- a fresh fill's
    live stop starts equal to its original one, then trails over time). Pass
    it explicitly when initial_stop is None but a real protective stop still
    exists for reason-matching purposes (the GOOGL case: no known original
    risk, but yesterday's computed structural stop is still the real level
    price actually respects) -- these are two different facts and must not
    collapse into the same None just because one of them is unknown."""
    if entry_type not in ("starter", "full"):
        raise ValueError(f"entry_type must be 'starter' or 'full', got {entry_type!r}")
    if current_stop is None:
        current_stop = initial_stop
    stored = get_thesis(ticker)
    # Has this ticker's trigger already confirmed on a settled daily close since
    # the thesis was built? get_trigger_fired_age reads the real monitor_log
    # greens, so this is the same fact MONITOR_v2 reports, not a re-derivation.
    # See classify_override for why it changes the answer.
    override = classify_override(
        (stored or {}).get("decision"), (stored or {}).get("rubric_grade"),
        trigger_confirmed=get_trigger_fired_age(ticker) is not None,
    )
    # Which build this fill was actually taken against (2026-08-07). entry_setup
    # already freezes the triggering setup's JSON; this makes the build itself
    # identifiable, so a real trade can be compared against the simulated result
    # for the SAME plan rather than against whatever the ticker looks like now.
    live_idea = get_live_idea(ticker)
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO positions (ticker, entry_date, entry_price, qty, entry_type, "
            "initial_stop, current_stop, entry_setup, entry_commission, "
            "override_of_decision, override_of_grade, idea_id, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (ticker.upper(), entry_date, entry_price, qty, entry_type,
             initial_stop, current_stop, json.dumps(entry_setup, ensure_ascii=False),
             entry_commission,
             (override or {}).get("decision"), (override or {}).get("grade"),
             (live_idea or {}).get("id"),
             _now()),
        )
        position_id = cur.lastrowid
    set_status(ticker, "open_position")
    return position_id


def add_to_position(ticker: str, additional_qty: int, additional_price: float) -> int:
    """/add's write -- records a real top-up fill against an ALREADY-OPEN
    position (e.g. starter -> full), blending qty/entry_price in place rather
    than creating a second positions row (the schema's own comment: never
    more than one open row per ticker -- see create_position/record_exit,
    which both assume exactly that). initial_stop, current_stop, entry_setup,
    and entry_date are never touched -- initial_stop is the permanent
    R-multiple denominator (indicators_core.compute_r_multiple), entry_setup
    is the original thesis snapshot /exit matches against, and an add is a
    sizing event, not a new thesis or a new entry. entry_type auto-flips
    'starter' -> 'full' (the documented "starter now, add later to reach
    full" concept from MONITOR_v2.md/STRATEGY_v3.md's own "Add Only If
    Confirmed" vocabulary) -- an add on an already-full position stays 'full'.
    Raises ValueError if there's no open position for this ticker (use
    /filled for a genuinely new entry, not /add)."""
    with _db() as conn:
        position = conn.execute(
            "SELECT * FROM positions WHERE ticker=? AND status='open' ORDER BY id DESC LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
        if position is None:
            raise ValueError(
                f"no open position found for {ticker.upper()} -- use /filled for a new entry, not /add"
            )
        position = dict(position)
        new_qty = position["qty"] + additional_qty
        new_price = (position["qty"] * position["entry_price"] + additional_qty * additional_price) / new_qty
        new_entry_type = "full" if position["entry_type"] == "starter" else position["entry_type"]
        conn.execute(
            "UPDATE positions SET qty=?, entry_price=?, entry_type=?, updated_at=? WHERE id=?",
            (new_qty, new_price, new_entry_type, _now(), position["id"]),
        )
        return position["id"]


def update_current_stop(ticker: str, new_stop: float, allow_lower: bool = False) -> bool:
    """Trails the live protective stop for an open position. Called by
    /playbook's delivery step (deliver_playbook_report.py) after each run
    recomputes it per STRATEGY_v3.md Category B judgment -- this function
    itself makes no judgment call, it just persists whatever stop the model
    already decided on, same principle as the rest of the deliver_*.py
    scripts. Never touches initial_stop -- that's the R-multiple denominator,
    permanently fixed at fill time (see positions schema comment and
    record_exit()). No-op (returns False) if there's no open position for
    this ticker -- a portfolio screenshot can include tickers this system
    never filled through /filled.

    Monotonic by default (2026-07-14): raises ValueError instead of writing
    if new_stop is below the position's existing current_stop. Found real:
    /playbook's automation used to be blind to a position's real entry_setup
    (blocked from querying the DB at all) and so recomputed a brand-new "stop"
    from generic chart structure on every single run, then this function wrote
    it straight over the real trailed value with zero check on direction --
    confirmed live on the actual DB, NVDA's stop silently moved from 201.92 to
    195.06 between two runs, and ANET's current_stop (165.99) was already
    sitting below its own initial_stop (179.80) from the same pattern. A stop
    is supposed to only ever trail up (lock in more, risk less) -- this makes
    that a hard invariant instead of trusting every caller to get it right.
    allow_lower=True is the deliberate, explicit escape hatch for the rare
    real case (a stop was objectively wrong / structure genuinely invalidated
    it) -- never set it as a default anywhere, only pass it from a human's own
    explicit action."""
    with _db() as conn:
        row = conn.execute(
            "SELECT current_stop FROM positions WHERE ticker=? AND status='open'",
            (ticker.upper(),),
        ).fetchone()
        if row is None:
            return False
        existing = row["current_stop"]
        if existing is not None and new_stop < existing and not allow_lower:
            raise ValueError(
                f"{ticker.upper()}: refusing to lower current_stop from {existing} to {new_stop} "
                f"(stops only trail up -- pass allow_lower=True for a deliberate, explicit override)"
            )
        cur = conn.execute(
            "UPDATE positions SET current_stop=?, updated_at=? WHERE ticker=? AND status='open'",
            (new_stop, _now(), ticker.upper()),
        )
        return cur.rowcount > 0


def _remaining_qty(conn: sqlite3.Connection, position_id: int, original_qty: int) -> int:
    """Found in review (2026-07-16, XLF): positions.qty is the ORIGINAL fill
    size, deliberately fixed forever (it's the R-multiple denominator's
    partner value and the full-close threshold in record_exit() -- never
    touched by a partial exit). But every consumer that needs "how many
    shares are actually open right now" (fetch_analysis_data.py -> /playbook's
    screenshot comparison, /open's own display) was reading that same fixed
    qty as if it were live -- so a position with ANY prior partial exit
    showed its stale original size forever, not what's really left. Real
    incident: XLF (350 original, 140 exited via a genuine /exit on
    2026-07-14, 210 truly remaining) still reported qty=350 two days later,
    making a /playbook run see the real, already-correct 210-share screenshot
    as if it were an undiscovered discrepancy and tell the user to re-record
    an exit that had already happened. This computes the real, live number:
    original qty minus every exits row recorded against this position so far."""
    already_exited = conn.execute(
        "SELECT COALESCE(SUM(exit_qty), 0) as total FROM exits WHERE position_id=?",
        (position_id,),
    ).fetchone()["total"]
    return original_qty - already_exited


def _position_exit_rows(conn: sqlite3.Connection, position_id: int) -> list[dict]:
    """This position's own exits, oldest first."""
    return [dict(r) for r in conn.execute(
        "SELECT exit_date, exit_price, exit_qty, exit_reason, r_multiple FROM exits "
        "WHERE position_id=? ORDER BY id ASC", (position_id,),
    )]


def _filled_target_indexes(exit_rows: list[dict]) -> set[int]:
    """1-based index of every stored target a prior exit already sold at, read
    straight off the recorded exit_reason. Feeds
    indicators_core.derive_exit_reason so a realized level can never be matched
    twice -- see that function's docstring for the ASTS incident behind it."""
    out: set[int] = set()
    for r in exit_rows:
        reason = (r.get("exit_reason") or "").strip()
        if reason.startswith("target_"):
            try:
                out.add(int(reason.split("_", 1)[1]))
            except ValueError:
                continue
    return out


def _tranche_plan_dict(conn: sqlite3.Connection, position: dict,
                        entry_setup: Optional[dict]) -> dict:
    """CONSISTENCY_RULES.md rule 7's tranche plan for one open position, with
    each piece marked done/next off the real exits rows. Attached to every
    position this module hands out (2026-08-07) so no consumer -- /open,
    /positions, /playbook's fetch payload, the model itself -- can present an
    already-realized target as if it were still waiting."""
    plan = indicators_core.build_tranche_plan(
        original_qty=position.get("qty") or 0,
        targets=(entry_setup or {}).get("targets") or [],
        exits=_position_exit_rows(conn, position["id"]),
    )
    return {
        "tranches": [dataclasses.asdict(t) for t in plan.tranches],
        "next_label": plan.next_label,
        "next_price": plan.next_price,
        "next_qty": plan.next_qty,
        "runner_qty_left": plan.runner_qty_left,
        "remaining_qty": plan.remaining_qty,
        "runner_only": plan.runner_only,
        "warnings": plan.warnings,
    }


def get_open_position(ticker: str) -> Optional[dict]:
    """Single-ticker version of get_open_positions() -- returns the real,
    already-documented entry_setup/initial_stop/current_stop for one open
    position, or None if this system never filled it through /filled. Added
    2026-07-14 for fetch_analysis_data.py to feed into /playbook: that
    automation used to have no way to see this (blocked from querying the DB
    directly), so it treated every real open position as "undocumented" and
    reinvented a stop from scratch each run -- see update_current_stop's own
    docstring for the concrete bug this caused.

    `qty` stays the original, fixed fill size (untouched, see _remaining_qty's
    docstring); `remaining_qty` (2026-07-16) is the real live share count
    after any recorded partial exits -- use remaining_qty for anything
    comparing against a live screenshot/broker statement, qty only for
    R-multiple/full-close-threshold math.

    `add_to_full_qty` (2026-07-23): for an entry_type='starter' position whose
    thesis has a stored planned_qty (SCREENER_v3.md section ז's sizing-table
    figure -- risk_usd run through that thesis's own ATR/regime/volume-derate
    multipliers, the actual "full position" size the thesis called for, not a
    naive re-derivation here), this is planned_qty - remaining_qty: how many
    more units to buy to reach that full size. None when there's no
    planned_qty on file (legacy thesis, or a starter opened with no screener
    thesis behind it) -- never invented from a bare risk_usd/(entry-stop)
    guess that would silently ignore whatever multipliers the thesis actually
    used. Clamped at 0 rather than going negative (already at/above the
    thesis's full size). Always None for entry_type='full'."""
    with _db() as conn:
        # t.rubric_grade (2026-07-30 full-system checkup): the thesis's original
        # build-time rubric grade, exposed here so /playbook can disclose/gate an
        # "Add Only If Confirmed" recommendation the same way SCREENER_v3 already
        # gates a fresh Buy on rule 27's F-grade check -- see
        # report_lint._lint_playbook_add_gate.
        row = conn.execute("""
            SELECT p.*, t.sleeve, t.planned_qty, t.rubric_grade
            FROM positions p
            LEFT JOIN thesis t ON t.ticker = p.ticker
            WHERE p.status = 'open' AND p.ticker = ?
        """, (ticker.upper(),)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["remaining_qty"] = _remaining_qty(conn, d["id"], d["qty"])
        setup = json.loads(d["entry_setup"]) if d.get("entry_setup") else None
        d["entry_setup"] = setup if setup is not None else d.get("entry_setup")
        d["tranche_plan"] = _tranche_plan_dict(conn, d, setup)
    d["sleeve"] = get_sleeve(d["ticker"])
    planned_qty = d.pop("planned_qty", None)
    if d.get("entry_type") == "starter" and planned_qty is not None:
        d["add_to_full_qty"] = max(planned_qty - d["remaining_qty"], 0)
    else:
        d["add_to_full_qty"] = None
    return d


def _generate_closing_summary(ticker: str) -> None:
    """Fires once a thesis's last open position row has fully closed --
    computes the qty-weighted average R-multiple across every exits row tied
    to any of this ticker's positions (a thesis can have more than one
    positions row if it was re-entered after an earlier full close, so this
    intentionally isn't scoped to a single position_id). setup_type comes
    from the most recent position's own entry_setup snapshot, not
    thesis.primary_setup blindly -- the setup that actually triggered may
    have been the Alternate, not the Primary."""
    with _db() as conn:
        thesis = conn.execute("SELECT * FROM thesis WHERE ticker=?", (ticker.upper(),)).fetchone()
        last_position = conn.execute(
            "SELECT entry_setup FROM positions WHERE ticker=? ORDER BY id DESC LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
        exit_rows = conn.execute(
            "SELECT e.exit_qty, e.r_multiple FROM exits e "
            "JOIN positions p ON p.id = e.position_id WHERE p.ticker=?",
            (ticker.upper(),),
        ).fetchall()
        # Exits with no R-multiple (legacy positions with no known original-risk stop --
        # see record_exit()) are excluded from the weighted average entirely, not treated
        # as zero -- an unknown R must never silently pull the average toward 0.
        rated = [r for r in exit_rows if r["r_multiple"] is not None]
        rated_qty = sum(r["exit_qty"] for r in rated)
        total_r = (
            sum(r["exit_qty"] * r["r_multiple"] for r in rated) / rated_qty
            if rated_qty else None
        )
        entry_setup = json.loads(last_position["entry_setup"]) if last_position and last_position["entry_setup"] else {}
        conn.execute(
            "INSERT INTO closing_summaries (ticker, setup_type, rubric_grade, total_r_multiple, "
            "tags, thesis_validated, closed_at) VALUES (?, ?, ?, ?, ?, NULL, ?)",
            (ticker.upper(), entry_setup.get("type"),
             thesis["rubric_grade"] if thesis else None, total_r, json.dumps([]), _now()),
        )


def record_exit(ticker: str, exit_price: float, exit_qty: int, exit_date: str, source: str,
                 commission: Optional[float] = None) -> int:
    """/exit's write (source='exit_command') and /playbook's passive
    reconciliation (source='playbook_reconciliation') both funnel through
    here. Finds the ticker's open position, matches exit_price against that
    position's OWN entry_setup (never the ticker's live thesis, which may
    have since changed) via indicators_core.derive_exit_reason(), computes
    R-multiple against initial_stop (never current_stop -- see positions
    schema comment), records the exits row, and closes out the position
    (and the thesis, and generates the closing summary) once cumulative
    exit_qty reaches the position's original qty."""
    if source not in ("exit_command", "playbook_reconciliation"):
        raise ValueError(f"source must be 'exit_command' or 'playbook_reconciliation', got {source!r}")
    with _db() as conn:
        position = conn.execute(
            "SELECT * FROM positions WHERE ticker=? AND status='open' ORDER BY id DESC LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
        if position is None:
            raise ValueError(f"no open position found for {ticker.upper()} -- nothing to exit")
        position = dict(position)
        entry_setup = json.loads(position["entry_setup"]) if position.get("entry_setup") else {}
        targets = entry_setup.get("targets", [])
        atr_at_build = entry_setup.get("atr_at_build")
        # A target this position already sold at is off the table (2026-08-07):
        # without this, one stored target matched every later sell near it and
        # the same level was recorded as filled twice -- real ASTS incident,
        # see indicators_core.derive_exit_reason's docstring. The second sell is
        # a Runner trim, and calling it that is what keeps rule 7's allocation
        # honest about how much Runner is actually left.
        prior_exits = _position_exit_rows(conn, position["id"])
        match = indicators_core.derive_exit_reason(
            exit_price=exit_price, targets=targets,
            stop=position["current_stop"], atr_at_build=atr_at_build,
            filled_target_indexes=_filled_target_indexes(prior_exits),
        )
        # A legacy position with no real original-risk stop (position["initial_stop"] is
        # None) has no valid R-multiple denominator -- left NULL rather than forcing a
        # number off a stop that was never the actual entry-time risk (found real
        # 2026-07-09, GOOGL: its only known stop was computed well after entry, sits
        # above cost basis, and would invert the sign if used as initial_stop).
        r_multiple = (
            indicators_core.compute_r_multiple(
                entry_price=position["entry_price"], initial_stop=position["initial_stop"],
                exit_price=exit_price,
            ) if position["initial_stop"] is not None else None
        )
        cur = conn.execute(
            "INSERT INTO exits (position_id, ticker, exit_date, exit_price, exit_qty, "
            "exit_reason, r_multiple, source, commission, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (position["id"], ticker.upper(), exit_date, exit_price, exit_qty,
             match.reason, r_multiple, source, commission, _now()),
        )
        exit_id = cur.lastrowid

        already_exited = conn.execute(
            "SELECT COALESCE(SUM(exit_qty), 0) as total FROM exits WHERE position_id=?",
            (position["id"],),
        ).fetchone()["total"]

        position_now_closed = already_exited >= position["qty"]
        thesis_now_closed = False
        if position_now_closed:
            conn.execute(
                "UPDATE positions SET status='closed', updated_at=? WHERE id=?",
                (_now(), position["id"]),
            )
            open_positions_remaining = conn.execute(
                "SELECT COUNT(*) as c FROM positions WHERE ticker=? AND status='open'",
                (ticker.upper(),),
            ).fetchone()["c"]
            if open_positions_remaining == 0:
                conn.execute(
                    "UPDATE thesis SET status='closed', updated_at=? WHERE ticker=?",
                    (_now(), ticker.upper()),
                )
                _sync_live_idea_status(conn, ticker, "closed")
                thesis_now_closed = True

    if thesis_now_closed:
        _generate_closing_summary(ticker)
    return exit_id


def get_journal_rows(ticker: Optional[str] = None) -> list[dict]:
    """/journal's read-only aggregate. One row per closed thesis (a ticker
    only appears once here even if it was re-entered and closed more than
    once historically would appear multiple times, correctly, since each
    close generates its own closing_summaries row)."""
    with _db() as conn:
        if ticker:
            rows = conn.execute(
                "SELECT * FROM closing_summaries WHERE ticker=? ORDER BY closed_at DESC",
                (ticker.upper(),),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM closing_summaries ORDER BY closed_at DESC").fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["tags"] = (_safe_json_loads(d["tags"], ticker=d["ticker"], field="tags") or []) if d.get("tags") else []
        result.append(d)
    return result


def summarize_journal(rows: list[dict]) -> dict:
    """Pure aggregation over get_journal_rows()'s output -- count, average
    R-multiple, win rate (R > 0). Kept separate from get_journal_rows() so
    the caller can filter/slice rows first (e.g. by date range) and still
    reuse this."""
    with_r = [r for r in rows if r.get("total_r_multiple") is not None]
    if not with_r:
        return {"count": len(rows), "avg_r_multiple": None, "win_rate": None}
    avg_r = sum(r["total_r_multiple"] for r in with_r) / len(with_r)
    win_rate = sum(1 for r in with_r if r["total_r_multiple"] > 0) / len(with_r)
    return {"count": len(rows), "avg_r_multiple": avg_r, "win_rate": win_rate}


def get_unreflected_closes(ticker: Optional[str] = None) -> list[dict]:
    """/reflect's read: closing_summaries rows where thesis_validated is still
    NULL -- i.e. no one has yet judged whether the original thesis was actually
    right, as opposed to just whether the trade made money. Ordered oldest-first
    so a live Claude session working through a batch clears the longest-pending
    ones first."""
    with _db() as conn:
        if ticker:
            rows = conn.execute(
                "SELECT * FROM closing_summaries WHERE ticker=? AND thesis_validated IS NULL "
                "ORDER BY closed_at ASC",
                (ticker.upper(),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM closing_summaries WHERE thesis_validated IS NULL "
                "ORDER BY closed_at ASC",
            ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["tags"] = (_safe_json_loads(d["tags"], ticker=d["ticker"], field="tags") or []) if d.get("tags") else []
        result.append(d)
    return result


def record_reflection(closing_summary_id: int, thesis_validated: bool, lesson: str) -> None:
    """/reflect's write. thesis_validated judges the SETUP THESIS, not the P/L --
    a trade can close positive on a setup that was actually invalidated early
    (lucky bounce) or close negative on a setup that held right up until a
    normal stop-out (thesis was fine, just didn't work this time). lesson is
    free prose, same shape as TradingAgents' reflection log: 2-4 sentences,
    what held/failed, one concrete takeaway -- stored verbatim and meant to be
    re-read before the next thesis on this ticker or setup type."""
    with _db() as conn:
        cur = conn.execute(
            "UPDATE closing_summaries SET thesis_validated=?, lesson=? WHERE id=?",
            (1 if thesis_validated else 0, lesson, closing_summary_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"no closing_summaries row with id={closing_summary_id}")


def get_ticker_lessons(ticker: str, n: int = 5) -> list[dict]:
    """Same-ticker reflected closes, most recent first, for injection into a new
    /screener or /monitor run on this ticker -- mirrors TradingAgents'
    get_past_context() same-ticker slice. Only rows with a lesson already
    recorded (thesis_validated NOT NULL) are returned; pending ones have
    nothing useful to inject yet."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM closing_summaries WHERE ticker=? AND thesis_validated IS NOT NULL "
            "ORDER BY closed_at DESC LIMIT ?",
            (ticker.upper(), n),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_cross_lessons(exclude_ticker: str, setup_type: Optional[str] = None, n: int = 3) -> list[dict]:
    """Recent reflected closes on OTHER tickers, most recent first -- mirrors
    TradingAgents' cross-ticker lesson slice. Pass setup_type to narrow to the
    same setup family (e.g. only past Breakout lessons when building a new
    Breakout thesis) instead of pulling unrelated setup-type lessons."""
    with _db() as conn:
        if setup_type:
            rows = conn.execute(
                "SELECT * FROM closing_summaries WHERE ticker!=? AND thesis_validated IS NOT NULL "
                "AND setup_type=? ORDER BY closed_at DESC LIMIT ?",
                (exclude_ticker.upper(), setup_type, n),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM closing_summaries WHERE ticker!=? AND thesis_validated IS NOT NULL "
                "ORDER BY closed_at DESC LIMIT ?",
                (exclude_ticker.upper(), n),
            ).fetchall()
    return [dict(r) for r in rows]


def get_open_positions() -> list[dict]:
    """/open's read-only query: every positions row still status='open', joined
    with its thesis for sleeve classification. Static/DB-only, same as
    get_journal_rows() and get_pending_report_rows() -- no live TradingView
    fetch, so this stays instant and free. Unrealized P&L against current price
    is deliberately out of scope here; add a separate live-price path if that's
    ever needed."""
    with _db() as conn:
        rows = conn.execute("""
            SELECT p.*, t.sleeve
            FROM positions p
            LEFT JOIN thesis t ON t.ticker = p.ticker
            WHERE p.status = 'open'
            ORDER BY p.entry_date ASC
        """).fetchall()
        today = _now()[:10]
        result = []
        for row in rows:
            d = dict(row)
            d["remaining_qty"] = _remaining_qty(conn, d["id"], d["qty"])
            if d.get("entry_setup"):
                d["entry_setup"] = _safe_json_loads(d["entry_setup"], ticker=d["ticker"], field="entry_setup")
            d["tranche_plan"] = _tranche_plan_dict(
                conn, d, d["entry_setup"] if isinstance(d.get("entry_setup"), dict) else None)
            d["days_held"] = count_trading_days(d["entry_date"][:10], today) if d.get("entry_date") else None
            d["sleeve"] = get_sleeve(d["ticker"])  # hard rule, not the raw joined DB column -- see get_sleeve()
            result.append(d)
    return result


# ---------------------------------------------------------------------------
# Hardening Pass item 7: shadow-book capture (bot/score_shadow.py's data access).
# ---------------------------------------------------------------------------

def get_shadow_candidates(ticker: Optional[str] = None) -> list[dict]:
    """Every live build with a real primary_setup, as score_shadow.py's input.

    Reads `ideas`, not `thesis`, since 2026-08-07 -- and that is the whole point
    of the change: the returned dict carries `idea_id`, so the result score_shadow
    writes binds to the identifiable build it scored instead of to a ticker that
    may be screened again tomorrow. `date_built` is kept as an alias of the
    idea's built_at so the caller's existing field names still work.

    Only live builds are scored. A superseded build's outcome is already frozen
    in the rows captured while it WAS live; re-simulating it every night would
    keep extending a plan the user has since replaced.

    Not filtered by decision/status -- which subset is worth acting on is
    deferred judgment, not this function's job (see score_shadow.py's docstring).
    A sleeve-only stub never appears here because a stub creates no ideas row.
    """
    sql = "SELECT * FROM ideas WHERE superseded_at IS NULL AND primary_setup IS NOT NULL"
    params: list = []
    if ticker:
        sql += " AND ticker=?"
        params.append(ticker.upper())
    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()
    result = []
    for row in rows:
        d = _parse_idea_row(row)
        d["idea_id"] = d["id"]
        d["date_built"] = d["built_at"]
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Hardening Pass item 8: circuit breaker (optional pass item, implemented).
# Warning only, never a block -- the human decides. Lives here + the deliver
# scripts, per the brief's own locked design decision -- no protocol-file
# change needed, this is pure DB bookkeeping over exits already recorded.
# ---------------------------------------------------------------------------

def get_circuit_breaker_threshold() -> Optional[int]:
    """CIRCUIT_BREAKER_STOPOUTS from .env. Unset or non-numeric -> None (breaker
    inactive) -- there is no built-in default; never invent a limit the user
    didn't set themselves (same posture as DEFAULT_RISK_USD's own optionality)."""
    value = env_config.get_env_value("CIRCUIT_BREAKER_STOPOUTS", env_path=DB_PATH.parent / ".env")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def get_consecutive_stopout_streak() -> int:
    """Streak = consecutive fully-closed positions (most-recent-close first)
    whose FINAL exits row -- the one that actually completed the close, last by
    id -- has exit_reason='stop'. Real exposure only (status='closed' positions;
    a thesis invalidated before any fill was never a position row at all), same
    principle as rule 10's cooldown. Any non-stop final exit breaks the streak
    immediately walking backward from the most recent close."""
    with _db() as conn:
        positions = conn.execute(
            "SELECT id FROM positions WHERE status='closed' ORDER BY updated_at DESC"
        ).fetchall()
        streak = 0
        for pos in positions:
            last_exit = conn.execute(
                "SELECT exit_reason FROM exits WHERE position_id=? ORDER BY id DESC LIMIT 1",
                (pos["id"],),
            ).fetchone()
            if last_exit is None or last_exit["exit_reason"] != "stop":
                break
            streak += 1
        return streak


def circuit_breaker_status() -> dict:
    """Single call site for the deliver scripts: {tripped, streak, threshold}.
    tripped is False whenever the breaker is inactive (no threshold set) --
    never trips on an invented default."""
    threshold = get_circuit_breaker_threshold()
    streak = get_consecutive_stopout_streak()
    return {
        "tripped": threshold is not None and streak >= threshold,
        "streak": streak,
        "threshold": threshold,
    }


_SHADOW_SIM_COLUMNS = (
    "setup_type", "decision", "rubric_grade", "market_regime_at_build",
    "trigger", "stop", "target_1", "target_2", "atr_at_build",
    "fired_date", "entry", "entry_date", "entry_gap_pct", "risk_per_share",
    "resolution", "exit_date", "exit_price",
    "r_multiple_simple", "r_multiple_planned", "mfe_r", "mae_r",
    "bars_held", "sim_version", "sim_note",
    "idea_id",   # 2026-08-07: the build this result belongs to -- see the column comment
    # --- 2026-08-09: the five columns that turn a record into an answer ------
    #
    # Everything above says what an idea DID. None of it says whether that was
    # any good, which is the only question worth asking. "+0.05R on average"
    # cannot be read without knowing what the market did over the same days --
    # in a week SPY ran 6%, it is a bad number; in a week SPY fell 6%, it is a
    # good one. The table had no way to tell those apart.
    "spy_return_pct",      # SPY's own % move over this trade's exact window
    "spy_return_r",        # ...and the same move expressed in THIS trade's R,
                           # so it sits beside r_multiple_planned directly
    "days_to_fire",        # trading days from build to the trigger confirming;
                           # NULL when it never fired. "How long does a plan
                           # stay live" has never been measurable.
    "sector",              # sector_map's label at scoring time -- lets a result
                           # be read as "energy did this", not "these 9 tickers"
    "rr_at_build",         # the plan's own reward:risk when it was written. The
                           # rubric gates on it (rule 3) and nothing has ever
                           # checked whether it predicts anything.
    "owner_bought",        # 1/0/NULL -- did a real position get opened against
                           # this exact build. The one column that makes
                           # "does my system beat me?" a countable question
                           # instead of a feeling.
    # 2026-08-10. The stop is the denominator of every R in this table, so a
    # systematic problem with WHERE stops get placed is invisible in R itself --
    # MMM was handed a five-month-old low 12% away and every number downstream
    # inherited it silently. Recording which kind of structure backed the stop
    # turns "do recent-structure stops beat old ones" into a countable question.
    "stop_basis_kind",
)


def position_exists_for_idea(idea_id: Optional[int]) -> bool:
    """Was a real position ever opened against this exact build (2026-08-09)?

    Open or closed, both count: the question is "did the owner act on this
    idea", and a trade that has since been closed was still acted on.

    Keyed on idea_id, never on ticker. A symbol screened four times in a week
    produces four builds, and only one of them may have been bought -- matching
    on ticker would mark all four as taken and quietly make the comparison this
    exists for come out as a tie every time.

    This is what turns "does my system's opinion beat my own?" from a feeling
    into a countable question. The shadow book already holds the ideas the
    system said no to, which no real trade record can contain; this is the other
    half -- which of them the owner took anyway."""
    if idea_id is None:
        return False
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM positions WHERE idea_id=? LIMIT 1", (idea_id,)
        ).fetchone()
    return row is not None


def get_shadow_rows(min_checked_date: Optional[str] = None) -> list[dict]:
    """Every shadow row, newest first -- the read side of the backtest export
    (score_shadow.py --export). Deliberately unfiltered by decision/grade:
    the whole value of this table is that it contains the ideas the system
    said NO to, which the real trade book can never contain."""
    sql = "SELECT * FROM shadow_outcomes"
    params: tuple = ()
    if min_checked_date:
        sql += " WHERE checked_date >= ?"
        params = (min_checked_date,)
    sql += " ORDER BY checked_date DESC, ticker ASC"
    with _db() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def record_shadow_outcome(ticker: str, checked_date: str, price: Optional[float],
                           hypothetical_trigger_fired: bool,
                           max_favorable_excursion: Optional[float],
                           max_adverse_excursion: Optional[float],
                           **sim_fields) -> int:
    """score_shadow.py's single write -- one row per (ticker, checked_date) run,
    append-only (re-running on a later date adds a new row rather than
    overwriting, so the capture reflects what was known at each check, not just
    the latest). max_favorable_excursion/max_adverse_excursion are NULL, never
    0.0, when hypothetical_trigger_fired is False -- an unfired setup has no
    entry point to measure excursion from, and 0.0 would misleadingly read as
    'fired and went nowhere.'

    **sim_fields (2026-08-03) accepts any column in _SHADOW_SIM_COLUMNS -- the
    played-out-trade fields from score_shadow.simulate_trade(). An unknown key
    raises rather than being silently dropped: a typo'd column name that just
    disappears would leave a permanently half-empty backtest table with nothing
    to show it happened. Passing none of them is valid and writes the same row
    the pre-upgrade caller wrote.

    Pass idea_id (2026-08-07) to bind the row to the exact build it scored. A
    unique index enforces one row per (idea_id, checked_date), so a scheduled
    run that fires twice in a night can no longer write the same result twice --
    that happened for real and produced two NVDA rows for 2026-08-02 that
    disagreed about whether the trigger had fired. The second write raises
    sqlite3.IntegrityError; callers treat that as "already recorded today."""
    unknown = set(sim_fields) - set(_SHADOW_SIM_COLUMNS)
    if unknown:
        raise ValueError(f"unknown shadow_outcomes column(s): {sorted(unknown)}")
    cols = ["ticker", "checked_date", "price", "hypothetical_trigger_fired",
            "max_favorable_excursion", "max_adverse_excursion", "created_at"]
    vals = [ticker.upper(), checked_date, price, int(hypothetical_trigger_fired),
            max_favorable_excursion, max_adverse_excursion, _now()]
    for key, value in sim_fields.items():
        cols.append(key)
        vals.append(value)
    placeholders = ", ".join("?" for _ in cols)
    with _db() as conn:
        cur = conn.execute(
            f"INSERT INTO shadow_outcomes ({', '.join(cols)}) VALUES ({placeholders})", vals,
        )
        return cur.lastrowid


# Schema creation + migrations, run once on import. Deliberately the LAST
# statement in this module: the ideas backfill calls helpers defined above it,
# so this cannot move back up to where it used to sit near init_db's definition.
# Safe to call repeatedly -- every step is CREATE TABLE IF NOT EXISTS, an
# additive column check, or a guarded one-time backfill.
init_db()
