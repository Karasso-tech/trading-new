# Trading New — Startup / Crash-Recovery Protocol

Read this whenever you're worried the system went down (PC restarted, power
blip, Windows update forced a reboot, etc.).

## TL;DR — do this after any restart or suspected crash

1. Log in to Windows normally.
2. Double-click `start.bat` in `d:\Trading New`.
   - It checks TradingView (Chrome) and relaunches it if it's not open.
   - It checks the Telegram listener and restarts it if it's not running.
   - Safe to run any time, even if everything is already fine — it never
     starts a duplicate of anything already running.
3. In Telegram, send `/list` or `/positions`. A reply within ~30 seconds
   means you're fully back up. Nothing else to do.

## What fixes itself vs. what needs you

**Already self-healing, no action needed (once logged in):** the Telegram
listener (`ack_listener.py`) has a watchdog that checks it every 5 minutes
and force-restarts it if it dies or hangs. This isn't theoretical — it
already happened twice on its own on 2026-07-19 (see
`ack_listener_watchdog.log`).

**Needs you after a full reboot:** every scheduled job on this machine —
the watchdog, auto-monitor, position-status pushes, DB backup — is
registered as "run only when logged on." At the Windows lock screen, none
of them run at all. **Logging back into Windows is the one manual step
that unblocks everything else.**

**Needs you if TradingView itself closed or crashed:** nothing auto-restarts
Chrome/TradingView — it's not managed by any watchdog. `start.bat` now
checks for it and relaunches it automatically. To do it by hand instead:
`tradingview-mcp\scripts\launch_tv_debug.bat`.

## Runs automatically, no `start.bat` needed (once you're logged in)

- **DB backup** — nightly 23:50, copies `trading_new.db` to your Dropbox
  `Trading Backup` folder.
- **Auto-monitor** — 18:30–22:30 (every 2h) plus a close pass that is no longer
  on a clock: the nightly idea refresh starts it once its rebuilds have landed
  (see below), so the last list of the night is built from today's prices.
  `TradingNewAutoMonitorClose` still exists but is now only a safety net —
  05:30, and it stands down unless the chained scan never happened. Tue–Sat,
  not Mon–Fri: 05:30 Israel time is still the previous evening in New York, so
  Tuesday's run covers Monday's session. Mon–Fri would have checked Sunday
  night and missed Friday's altogether.
- **Position status** — 19:45 midday and 23:25 close, pushed to Telegram.
- **Watchdog** — every 5 minutes, restarts the Telegram listener if it's
  dead or stuck.
- **X feed scan** (`TradingNewXFeed` task, added 2026-07-22, confirmed
  registered and running 2026-07-30) — every 30 min during market hours,
  polls the curated X/Twitter account list and Telegrams any new ticker idea.
- **Shadow book** (`TradingNewShadowBook`, added 2026-08-02) — 23:40 nightly.
  Records what every screened idea actually did afterwards, including the ones
  the system said no to. No Telegram message; it just collects. Log:
  `score_shadow.log`.
  **Upgraded 2026-08-03 to a full played-out trade**, not just "did it fire and
  how far did it swing": each idea is now entered at the next open after its
  trigger closed, then walked forward bar by bar to its stop or its targets,
  producing a real R-multiple, best/worst move in R, days held, and the gap
  cost of the daily-close rule. Every assumption behind those numbers is
  written at the top of `bot/score_shadow.py` — read them before trusting a
  result. To pull the whole book out for analysis in any other tool:
  `python bot\score_shadow.py --export shadow.csv` (no fetch, no TradingView,
  safe to run at any time, including while a scan is going).
- **Nightly idea refresh** (`TradingNewRefreshPending`, added 2026-08-02) —
  23:20, Mon–Fri (moved from 00:15 *every* day on 2026-08-08 — the weekend runs
  rebuilt ideas on a market that had been shut for two days, and the scan they
  chain correctly skipped, so they were an hour of work for nothing). Checks every waiting idea,
  rebuilds the ones whose numbers went stale (a full screener run each, one at
  a time), and Telegrams a short summary when it's done. Stops starting new
  rebuilds two hours before the open. Never deletes anything — dead ideas come
  with a `/drop` command to send yourself. Log: `refresh_pending.log`.
  **It then starts the close scan itself**, so the buy list you read is built
  from refreshed numbers instead of pre-refresh ones. That scan now lands
  around midnight–01:00 rather than 23:20. What counts as "stale" did not
  change with the reorder: an idea that just crossed its buy level is a
  fraction of an ATR past it and is left alone, so the scan still reports the
  entry; an idea that ran a full ATR past is rebuilt, because that reward is
  gone and it needs a new plan.

## "🔑 המערכת לא מחוברת לחשבון Claude"

If you get that Telegram message, the analysis engine itself is signed out.
Nothing that needs thinking will run — not `/screener`, not `/monitor`, not
the overnight scans, not the nightly rebuild. They all fail in under two
seconds having done nothing at all.

**Fix:** open a command window on this PC and run `claude /login`, then resend
whatever you were trying to do.

This happened for real on 2026-08-02: three `/monitor BTCUSD` runs failed at
03:38, 04:41 and 10:03 and were reported only as a generic failure. The log
had said `"Not logged in · Please run /login"` all along, and nothing surfaced
it. The alert now names the fix directly, and is sent only once per run no
matter how many commands were queued behind it.

## How to tell something's actually wrong (vs. just quiet)

1. Send `/positions` in Telegram — no reply within ~30s means the listener
   is down or stuck.
2. Run `start.bat` again — always safe, it only starts what's missing.
3. Check the tail of `ack_listener_watchdog.log`: `OK: PID ... healthy` =
   fine; a `restarting` line = it just self-healed, no action needed.

## Not urgent, but worth knowing

`backup_db.log` has a handful of `PermissionError` tracebacks from
2026-07-16 — those were the *log write* racing with something else, not a
failed backup. The actual `.db` backups landed in Dropbox every night since
(confirmed through 2026-07-19). Don't be alarmed if you see red text in
that log.

## If you want it to survive without ever logging in to Windows

Right now the listener, the watchdog, and every scheduled job all require
an active Windows session — that's the one real gap. Making them run
"whether logged on or not" is possible but means Task Scheduler has to
store your Windows password — a bigger, more security-sensitive change.
Ask if you want that set up; it hasn't been done here.
