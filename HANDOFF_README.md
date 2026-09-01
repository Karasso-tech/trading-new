# Trading New — Handoff Package

**Read this file first.** It is written for someone picking this project up
with zero prior context — a fresh Claude Code session, or a human developer.
It says what the system is, what actually works, what does not, and where to
read next.

This zip is a snapshot taken **2026-08-05**. It is **not** a git repo — no
`.git` history is included. If you want history, ask the owner for the repo.

**Two files in this zip are for reading in a browser, not in an editor:**

- `GUIDE_2026-08-05.html` (`.pdf` too) — the whole system explained in
  first-grader words: what it is, every Telegram command, what runs on its own,
  how to set it up, what to do when it breaks. Start here if you are the person
  who will *use* this, not the person who will change the code.
- `WHATS_NEW_2026-08-05.html` (`.pdf` too) — what changed since the 2026-08-03
  package: the ~6x speed fix, decision signs on every list, scheduled-job
  heartbeats, the sector rewrite, and the volume rule that got switched off.

Plain words are used on purpose throughout. Anything that needs a special
term gets it explained on the spot.

**House rule, and it is a hard one:** anything written back to the owner —
chat, reports, plans, docs, review notes — must use simple words a first
grader would understand. Short sentences, one idea each, no rule numbers, no
`file.py:120` pointers, no internal code names. Code and commit messages stay
normal. See `CLAUDE.md` for the full rule.

---

## What this is

A personal, one-user trading *analysis and alerting* system. It does **not**
place trades. Every buy and every sell is a human sending a Telegram command.
What it automates:

1. **Analysis** — screens tickers (chart data pulled from TradingView), builds
   a structured "thesis" (trigger price, stop, targets, grade, setup type)
   following a long hand-written method (`CONSISTENCY_RULES.md`,
   `SCREENER_v3.md`, `STRATEGY_v3.md`, `MONITOR_v2.md`).
2. **Delivery** — every report goes to the owner's Telegram bot as a rendered
   chart PNG + the full report as a PDF + a short text summary. Never just a
   file path: the owner reads these on a phone with no access to the disk.
3. **Tracking** — a SQLite file (`trading_new.db`, not in this zip) holds
   thesis state, open positions, exits, and auto-writes journal entries with
   R-multiple math once a position closes.
4. **Automation** — a Telegram listener (`bot/ack_listener.py`) plus a queue
   processor (`bot/process_queue.py`) handle incoming commands. Some commands
   (`/list`, `/pending`, `/drop`, `/exit`, `/journal`, `/positions`, `/pnl`) are pure
   Python, no model call at all. Others (`/screener`, `/monitor`, `/playbook`,
   `/filled`) shell out to a scoped `claude -p` call that does the judgment.
5. **Unattended jobs** — Windows Task Scheduler runs scans and status pushes
   at set times and Telegrams the result with no human trigger. These are
   informational; they never move a stop or open a position on their own.

## Architecture at a glance

```
Telegram (owner's phone)
    <-> bot/ack_listener.py       long-lived listener; checks the sender is
                                   allowed, acks, writes the command into
                                   SQLite, spawns process_queue.py
        bot/process_queue.py      drains the queue; mechanical commands run
                                   in-process; judgment commands shell out to
                                   `claude -p` with a narrow --allowed-tools
            bot/fetch_analysis_data.py  price/indicator/regime fetch, no
                                         judgment anywhere in it
            bot/tv_data.py              raw TradingView client, talks to
                                         tradingview-mcp over Chrome CDP
            bot/tv_lock.py              machine-wide lock — only ONE process
                                         may hold the TradingView bridge; a
                                         second one dies with EADDRINUSE
            bot/indicators_core.py      deterministic ATR/SMA/RS/volume math,
                                         deliberately narrow — never add
                                         swing/pivot detection here, see
                                         CLAUDE_CODE_INSTRUCTIONS.md
            bot/deliver_*.py            renders the PNG + PDF, sends through
                                         bot/telegram_send.py, saves state
                                         through bot/persistence.py
        bot/persistence.py         SQLite (trading_new.db): thesis, positions,
                                    exits, closing_summaries, monitor_log,
                                    thesis_history, shadow book
tradingview-mcp/                   vendored, locally patched TradingView
                                    connector (drives Chrome to pull chart
                                    data and screenshots)
```

**There is no orchestrator.** This matters. Nothing is always-on deciding when
to analyse. A Telegram command is only *queued*; something — a live Claude Code
session or a scheduled task — has to come along and drain the queue. "Acked"
means "safely written down", not "done".

### The mechanical helper modules (no judgment inside any of them)

These exist because the same decision used to be re-made by hand on every run
and quietly drifted. Each one turns a written rule into code that always gives
the same answer for the same inputs:

- `bot/regime_formula.py` — what state the market is in (rule 23).
- `bot/rubric_formula.py` — the A–D quality grade for a setup (rule 27). Also
  re-checked live by `/monitor`, which it never used to be.
- `bot/size_policy.py` — how big a position may be (rule 28). Puts a floor and
  a ceiling on risk per trade after several multipliers used to stack down to
  a 0.2%-of-equity position against a stated 1% rule.
- `bot/decision_policy.py` — what the four decision words (Buy Now / Buy Only
  If Confirmed / Watchlist / No Trade) actually mean and when each is allowed
  (rule 29). They had never been defined in four months of use.
- `bot/market_hours.py` — trading-calendar checks, including holidays.
- `bot/setup_types.py` — the closed list of six setup names (added 2026-08-09).
  This is the field the shadow book groups by, and it had never been
  constrained: four live rows held a whole paragraph where a label belongs, and
  a group of one proves nothing. `save_thesis` now refuses anything else.
- `bot/level_picker.py` — the stop, the targets and the movement potential
  (2026-08-09). Rules 2/4/24 pick one stop and only one; rules 3/7/11/12/13
  gate and allocate the targets; rule 17 measures the potential. Also emits the
  `rejection_reasons` tokens, which used to be free text the model retyped (the
  same situation came back five different ways in one week).
- `bot/setup_classifier.py` — which of the six setups the price data shows, and
  where its trigger sits (2026-08-09). Follows the same pattern as
  `regime_formula.py` and `rubric_formula.py`: **the code decides, and a model
  may only differ by writing down a reason.** Its thresholds are an admitted
  first draft — what it buys today is *consistency*, so the shadow book can
  finally ask whether the label predicts anything.
- `bot/summary_text.py` — SCREENER_v3.md section ח's three Telegram templates,
  as code. Section ח dictates a form (fixed emoji, fixed line order, exactly
  fifteen separator bars, two-decimal bold numbers), and a fixed form with
  numbers dropped into it is an f-string. The model now supplies only the one
  sentence of thesis; anything it writes into `summary_text` is ignored.
- `bot/build_plan.py` — all of the above in one command, `python
  bot\build_plan.py TICKER`. It runs the fetch once and returns
  `{"plan": ..., "data": ...}`. This is what `/screener` calls now, and it is
  why that prompt went from ~2,200 words to ~800: most of the old text was
  telling a model to copy a number exactly and not change it.
- `bot/run_files.py` — where a run's leftovers live (`_runs/`) and when they are
  deleted (after 7 days). The project root had 259 files, 231 of them per-run
  payloads. Also rotates any log past 5 MB.
- `bot/sector_map.py` — ticker to sector, rewritten 2026-08-04. Two separate
  labels on purpose: **sector** (one standard — the 11 SPDR/GICS sectors,
  derived from the SIC code each company files with the SEC, plus the SEC's own
  industry description underneath) and **correlation group** (a short hand-kept
  list, only for the baskets where the official sector label is misleading —
  six mega-cap tech names sit in three different sectors and still move as one
  bet). Coverage went from **7.4% to 97.6%** of the tickers this system sees;
  the old hand-written map was small enough that the concentration cap could
  never fire. `bot/sic_to_sector.py` holds the mapping,
  `bot/fetch_sector_codes.py` refreshes `bot/sector_codes_sec.json` from EDGAR.
  Honest caveat kept in the file: SIC is a free approximation of GICS, not GICS.

### The research side (added 2026-08-02/03)

- `bot/score_shadow.py` — the **shadow book**. Every night it records what each
  screened idea actually did afterwards, *including the ideas the system said
  no to*. Since 2026-08-03 it plays each idea out as a full trade (entry at the
  next open after the trigger closed, walked forward bar by bar to stop or
  targets) and reports a real R-multiple. The assumptions are written at the
  top of that file — read them before trusting any number.
  Export the whole book with:
  `python bot\score_shadow.py --export shadow.csv` (no fetch, safe any time).

  **Upgraded 2026-08-09 (sim version 2.1).** The walk itself is unchanged, so
  old and new R-multiples are still directly comparable — that is what the
  version field is for. What changed is that a row now carries the things that
  make it *readable*: what SPY did over the same window (in % and in this
  trade's own R), how many trading days it took the trigger to fire, the
  sector, the plan's own reward:risk at build time, and **whether the owner
  actually bought it**. An idea that never fires within 20 trading days is now
  marked `expired_never_fired` instead of sitting open forever — without an
  ending, "out of 100 ideas, how many even start?" has no denominator that ever
  settles.
- `bot/clean_shadow_labels.py` — the one-time cleanup of labels written before
  the write path was locked. Already run on 2026-08-09 (224 values). Keep it;
  it is the record of what was changed and it is safe to re-run.
- `bot/trade_sim.py` — **one** exit engine, shared by the shadow book and the
  backtest. Before this they were two engines that disagreed, so their numbers
  could not be compared.
- `bot/fetch_earnings_history.py` — pulls historical earnings dates from SEC
  EDGAR, because none existed in the project and every backtested trade was
  failing the "no earnings in the window" criterion automatically.

**The one-off backtest scripts are gone**, deliberately, in commit `3d330b3`
("Retire the one-off backtest scripts"). `backtest_lab.py`,
`backtest_strategy.py`, `backtest_grade.py`, `backtest_score.py`,
`backtest_slots.py` and `backtest_protocol.py` each answered one question,
were answered, and were deleted. Get them back from git history if a question
needs re-asking; do not go looking for them on disk. `trade_sim.py` — the part
that matters, the exit engine they all shared — stayed.

**Read the pre-registration notes before drawing any conclusion from the research
code.** Short version, and it is not comfortable: across 326 trades over five
years the **runner tranche is the entire edge** (+40.6R with it, −33.3R
without it), and the 2025–26 stretch turned negative. Two separate
pre-registered attempts to build a picking score both failed their own tests.
Do not add a scoring feature on the assumption it will work — it has been
tried twice.

## What is genuinely done and verified on real trades

- The full `/screener` → `/monitor` → `/filled` → `/exit` → journal lifecycle,
  end to end, on real money.
- Every Telegram command automated, no manual babysitting:
  `/list /pending /drop /exit /journal /filled /screener /monitor /playbook
  /positions /pnl /reflect`.
- `/positions` plus scheduled midday and end-of-day status pushes. Advisory
  only — confirmed never to change `current_stop`.
- `/pnl` splits the account's money into the Core pile (SPY/QQQ) and the
  trading pile, each with its own total, because the broker shows only the two
  added together. Read-only; it changes nothing.
- Security: the sender is checked against `TELEGRAM_ALLOWED_USER_ID` before
  anything is acked or queued.
- PNG + PDF + text delivery on every report (see the delivery rule at the
  bottom — it is a hard requirement).
- Stored work is protected from the automation. The nightly rebuild keeps a
  copy of the old thesis in `thesis_history`, shows a before/after message,
  and never deletes an idea on its own — a dead idea arrives as a `/drop`
  command for the owner to send.

## What is still open, or worth checking before trusting it

- **The strategy itself is not proven.** See the backtest paragraph above.
  The code is solid; the edge is thin and recently negative.
- Sector labels are now real (97.6% coverage, SEC-derived) but SIC is only a
  free approximation of GICS — the obvious cases agree, edge cases will not.
- Nothing auto-restarts Chrome/TradingView. `start.bat` checks for it and
  relaunches, but no watchdog owns it.
- Every scheduled job on the original machine runs "only when logged on". At
  the Windows lock screen none of them run. Logging in is the one manual step.
  See `STARTUP_PROTOCOL.md`.
- `ack_listener.py` does **not** hot-reload. Editing it does nothing until the
  running process is killed and restarted.
- `_archive_old_api_bot/` is dead code from an earlier design and is **not** in
  this repo.

## Key docs, in reading order

1. `STARTUP_PROTOCOL.md` — what to do after a reboot or a suspected crash, and
   which jobs run on their own.
2. `CLAUDE_CODE_INSTRUCTIONS.md` — the standing instructions a Claude Code
   session working in this repo should follow (architecture rules, decisions
   not to re-open).
3. `MASTER_SYSTEM_SPEC.md` — full command reference (§9/§10 cover exactly what
   each Telegram command does).
4. `CONSISTENCY_RULES.md` — **29** hard trading rules (stop placement, target
   qualification, wall-chaining, movement potential, position size, what the
   decision words mean). Each one was written after a real mistake. Do not
   casually change them; each rule records the incident that produced it.
5. `SCREENER_v3.md` / `STRATEGY_v3.md` / `MONITOR_v2.md` — the three report
   specs, one per command family.
6. `backtest/` — the replay engine, the pre-registration notes for every
   experiment run against it, and `backtest/fetch_bars.py` to download the
   price history it needs. The written-up results of those runs are kept in
   the owner's private notes, not in this repo.

**Read `backtest/PREREGISTRATION_*.md` before drawing any conclusion from the
research code.** Each one states, in advance, what would count as a pass and
what would count as a fail.

## Not in the public repo

The owner's own trade record, account figures and the write-ups built from
them (`MY_TRADING_GUIDE.md`, `CONSULTANT_REVIEW_*.md`, `BACKTEST_RESULTS.md`)
are deliberately kept private. Rules that cite a number from them still state
the number in R-multiples or percentages, which carry the lesson without
carrying anyone's account balance.

## What this zip deliberately leaves out

- `.env` — real Telegram bot token and Telegram user ID. Copy `.env.example`
  to `.env` and fill in **your own** values (your own bot, your own account).
- `trading_new.db` — real positions and trade history. It self-creates empty
  on first run (`persistence.init_db()`).
- `tradingview-mcp/node_modules/` — run `npm install` inside `tradingview-mcp/`.
- `tradingview-mcp/screenshots/` — old sample images, ~1.5 MB of nothing useful.
- `_archive_old_api_bot/` — dead code from the old API-driven design.
- `_runs/` — every per-run payload the pipeline writes (`_decision_*.json`,
  `_monitorall_*.json`, `_automonitor_*.json`, `_position_status_*.json`,
  `scratch_*`, …). They used to land loose in the project root and had reached
  231 files there; since 2026-08-09 they go here and are swept after 7 days.
  See `bot/run_files.py`. All regenerated on use, none of it source.
- All `*.log` files (and their rotated `*.log.1` generations),
  `reports/*.md` (generated output), `_*_cache/` — same, regenerated on use.
- `.git/` history.

## Getting it running from scratch

Windows is assumed — the scheduled jobs are `.bat` files driven by Task
Scheduler. The Python side is portable; the scheduling and the Chrome launcher
are not.

1. `pip install -r requirements.txt` (Python 3.11+).
2. `cd tradingview-mcp && npm install`
3. Copy `.env.example` to `.env`. Fill in a Telegram bot token (from
   @BotFather) and your own numeric Telegram user ID (from @userinfobot).
   Nothing works until `TELEGRAM_ALLOWED_USER_ID` is your ID — the listener
   ignores everyone else on purpose.
4. `python -c "from bot.persistence import init_db; init_db()"` — creates the
   database. Safe to run more than once.
5. Log in to TradingView in the Chrome instance that
   `tradingview-mcp\scripts\launch_tv_debug.bat` starts. The connector drives a
   real logged-in browser; it has no API key of its own.
6. `start.bat` — starts the Telegram listener and TradingView if either is not
   already up. Safe to run any time; it never starts a duplicate.
7. Register the scheduled jobs you want, from the `.bat` files in `bot\`
   (`run_ack_listener_watchdog.bat`, `run_auto_monitor*.bat`,
   `run_position_status_*.bat`, `run_refresh_pending.bat`,
   `run_score_shadow.bat`, `run_backup_db.bat`, `run_x_feed.bat`). Task
   Scheduler state is **not** in this zip, only the scripts. On the original
   machine, `schtasks /query /fo LIST | findstr TradingNew` lists them.
   **Quote the full path** when registering — a path with a space in it
   (`d:\Trading New\...`) breaks silently otherwise. That bug was real.
8. `/screener`, `/monitor` and `/playbook` need a working `claude` CLI login on
   the machine. If it is signed out, every thinking command fails in under two
   seconds and Telegrams "🔑 המערכת לא מחוברת לחשבון Claude". Fix with
   `claude /login`.

Run the tests before trusting a change: `pytest bot/` from the repo root.

## Mandatory delivery rule (do not relax without asking the owner)

Every `/screener`, `/monitor` and `/playbook` report sent to Telegram must
include, with no exceptions: the rendered widget PNG, the full report as a
**PDF** attachment (never `.txt`), and a short plain-text summary. The owner
reads these on a phone with no access to the filesystem — "saved to
`reports/xyz.md`" is not a delivery.
