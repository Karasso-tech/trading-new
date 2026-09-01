# Instructions for Claude Code — Trading New (Telegram Bot)

## Rule 0 — write everything in first-grader words

**Every reply, every report, every document the owner reads must use simple
words a first grader would understand.** Short sentences. One idea each.
Everyday words instead of technical ones. No rule numbers, no `file.py:120`
pointers, no internal code names in text the owner reads. If a special word is
needed, explain it in the same sentence in plain words — or leave it out.

This is always on. It is not a special mode for big explanations. Source code,
commit messages, and warnings about things that can't be undone stay normal.

Full version of this rule: `CLAUDE.md` in this folder.

## The one non-negotiable requirement

**Every analysis must use the actual protocol files as context — not a paraphrase, not a summary, not logic re-implemented in Python.** The four files below already exist, were built and refined through extensive manual testing, and are the actual source of truth for every trading decision this bot makes:

- `SCREENER_v3.md` — pre-market entry screening
- `MONITOR_v2.md` — intraday tracking
- `STRATEGY_v3.md` — open-position management
- `CONSISTENCY_RULES.md` — cross-protocol rules that must never drift (canonical copy; `MASTER_SYSTEM_SPEC.md` §5 is a pointer to this file, not a second copy)

Read the relevant file(s) fresh from disk on every single run. Never cache their contents across runs, never hardcode their rules as Python conditionals, never let a summary or a "compiled" version of them drift from the originals. If a rule needs to change, it gets changed in the `.md` file itself — not patched around in code.

**Every judgment-requiring command still gets a real Claude Code reasoning step — as of 2026-07-09/10 that step just runs unattended instead of waiting for an interactive session.** Telegram is the interface; a scoped `claude -p` invocation is the analysis engine for `/screener`, `/monitor`, and portfolio-screenshot `/playbook` — these read the protocol files fresh, fetch real data, and apply Category B judgment, exactly as an interactive session would, but triggered automatically and delivered with no human review. This was a deliberate reversal of the original "no automated per-message LLM call" principle, done only after the user explicitly authorized unattended thesis generation (see `journal-pending-closing-flow.md` in project memory for the exact authorization exchange). `/filled` is fully deterministic instead (no LLM call needed — see below).

**Rebuilt 2026-07-09, then fully automated 2026-07-09/10** (after the original queue/ack layer was deleted following real, repeated reliability failures — messages sitting unprocessed for minutes to hours, one 12.68-hour gap overnight, traced to a fixed-interval cron/polling mechanism that only fires while idle and is financially non-viable at any interval): `bot/ack_listener.py` acks and durably queues every authorized message, then immediately (event-driven, never a timer) spawns `bot/process_queue.py`. That script has two tiers: (1) a plain, deterministic Python dispatcher — **no LLM call, no API cost** — for the fully mechanical subset (`/list`, `/pending`, `/drop`, `/exit`, `/journal`, `/filled`: known functions, no fresh code, no judgment); (2) for `/screener`, `/monitor`, and `/playbook`, a scoped `claude -p --allowed-tools` invocation restricted to two or three named scripts only (never a wildcard — a broad `PowerShell(python *)` allowlist was tried first and correctly blocked by the auto-mode classifier as equivalent to unrestricted execution). Nothing waits for an interactive session anymore.

## Why this matters — read before building anything else

An earlier design had a separate `indicators.py` module compute ATR, SMA, and swing-point detection in hand-written Python, with the model only consuming the final numbers. That design was reverted. Two concrete failures came out of testing that approach manually:

1. A naive "average of the last 14 true ranges" ATR implementation silently disagreed with TradingView's own ATR (which uses Wilder/RMA smoothing) — caught only by manually cross-checking against the chart.
2. A fixed swing-detection rule (e.g., "N=3 bars on each side") is a rigid heuristic that can miss a level that matters for a specific thesis (a level being tested live intraday, not a classic pivot) or flag noise as structure.

**Category A — deterministic, one correct answer, checkable by formula.** ATR (Wilder/RMA), SMA, RS over a stated window, volume averages, Fibonacci-extension arithmetic, Anchored VWAP arithmetic. `bot/indicators_core.py` computes these — verified once against real TradingView output, trustworthy going forward. This is arithmetic, not judgment.

**Category B — contextual judgment, no formula decides it.** Which swing move is "the thesis's base," which anchor a Fibonacci/VWAP calculation should start from, whether a cluster of highs constitutes a resistance wall (and whether that wall is itself part of a second chained wall further up — `CONSISTENCY_RULES.md` rule 11's recursive step), which setup type applies. Written rules reduce how often this goes wrong — they do not make it deterministic. **No version of this system ever reaches a point where Category B stops needing human spot-checking.** Treat any plan that implies otherwise as wrong. When a wall-chain, anchor selection, or setup-type classification materially affects the output, deliberately re-check your own reasoning against the raw data before presenting it as settled — this is exactly the kind of judgment that produced the PLTR recursive-wall miss and the XLV allocation-table miss, both real, both caught only by re-reading the actual numbers.

## Permanent principle — never hand over a number that fails a threshold

`CONSISTENCY_RULES.md`'s thresholds (R:R, ATR-multiple, stop-noise floor, resistance-wall chaining) are a hard block, not a soft warning, on your own output — there is no separate `validator.py` process checking this in code anymore; it's your own discipline before sending anything. If a level fails a threshold, it's a Checkpoint, not a target — say so and don't present a price the reader could act on as if it passed.

## Two output classes — never blur them (see `bot/output_gate.py`)

Every report is either **analysis-only** (sendable even with incomplete information — unknown sleeve, unrecorded data coverage — but visibly marked non-actionable, distinct styling in both the text and the widget) or **actionable** (only when sender is authorized, data timestamp/coverage is recorded, sleeve is known — not "unknown, treated as swing anyway" — and every stop/target/trigger cites a real source). Call `bot/output_gate.py`'s `classify_output()` before deciding how to present a report; never skip straight to presenting something as a ready order.

```
d:\Trading New\
  SCREENER_v3.md, MONITOR_v2.md, STRATEGY_v3.md   — source of truth, read fresh, never auto-edited
  CONSISTENCY_RULES.md      — canonical cross-protocol rules, read fresh alongside whichever protocol is active
  MASTER_SYSTEM_SPEC.md     — how the three protocols wire together; §5 points to CONSISTENCY_RULES.md
  widget_template.html      — WIDGET_DATA-driven dashboard render target
  tradingview-mcp\            — vendored copy of the CDP connector (own node_modules)
    VENDORED_FROM.md            — source path + date frozen from
  reports\                  — every report actually delivered, saved as .md
  bot\
    ack_listener.py         — the ONLY thing that calls Telegram's getUpdates; authorization check (A1) against
                              TELEGRAM_ALLOWED_USER_ID happens here, before ack or queue; instant replies for
                              /help and not-yet-wired commands; queues everything else to the SQLite `messages`
                              table (persistence.py), then immediately (event-driven) spawns process_queue.py
    process_queue.py         — rebuilt 2026-07-09, fully automated same day. Lock-guarded, self-draining
                              dispatcher spawned by ack_listener.py after every real message. Two tiers:
                              (1) plain deterministic Python, no LLM call at all — /list, /pending, /drop,
                              /exit, /journal, /filled (known functions only, no fresh code, no judgment);
                              (2) a scoped `claude -p` pipeline with a narrow --allowed-tools list (two or
                              three named scripts only, never a wildcard) for /screener, /monitor, and
                              portfolio-screenshot /playbook — Claude's own Category B judgment runs between
                              a mechanical fetch script and a mechanical deliver script, per the user's
                              explicit authorization for unattended thesis generation with no human review
    check_telegram.py       — claims/peeks the SQLite queue for a live interactive session to process
                              (never calls Telegram directly)
    tv_data.py               — MCP stdio client to the vendored server. RAW DATA ONLY — no calculation of any kind
                              here. get_daily_history, get_intraday_since_open (DST-aware via
                              zoneinfo("America/New_York")), get_quote, get_chart_screenshot (optional/secondary);
                              distinguishes Chrome-not-open / CDP-hung / bad-symbol failures; sequential fetches only,
                              never concurrent, for any multi-ticker command. assert_data_fresh() (Hardening Pass
                              item 6) -- real-NYSE-calendar freshness check (never weekday arithmetic), same
                              approach as _session_open_utc_ts; called by both fetch scripts, result copied
                              verbatim into the decision JSON
    indicators_core.py        — Category A pure math only: atr_wilder, sma, relative_strength, volume_average,
                              fibonacci_extension, anchored_vwap. No swing/pivot detection, no thesis judgment —
                              that line must never move (see "Category A/B" above)
    report_lint.py             — Hardening Pass item 3: deterministic post-hoc arithmetic re-check of levels
                              Claude already chose (ATR-multiple/R:R recompute, rule 3 gate, rule 4 noise floor,
                              rule 5/14/15/17 structural completeness, rule 18 regime-vs-decision consistency) --
                              never a second opinion on WHICH level should have been chosen. Runs on the
                              structured decision dict inside deliver_report.py/deliver_monitor_report.py/
                              deliver_playbook_report.py, never on report_markdown prose. Never blocks a send;
                              findings go into the widget's warnings[] (red banner), a prepended Telegram warning
                              block, and persistence.record_lint_result() (analysis_runs.lint_result)
    score_shadow.py            — Hardening Pass item 7: shadow-book CAPTURE ONLY (no analysis/calibration built
                              on it yet, deliberately deferred until a real trade sample exists). Pure
                              compute_shadow_metrics(bars, trigger, since_date) -- unit-testable without a fetch
                              -- plus a small CLI wrapper (persistence.get_shadow_candidates /
                              record_shadow_outcome) that scores every stored thesis's hypothetical outcome
                              (fired or not; MFE/MAE if fired, honestly None if not) against real daily bars.
                              Run manually/weekly, sequential fetches only, one ticker's failure never kills
                              the batch
    widget_render.py           — structured-dict -> WIDGET_DATA -> PNG (Playwright). screener/monitor/strategy
                              adapter functions; RS is guaranteed a metric slot (A2); full targets/checkpoints
                              arrays pass through (B4), not just a flat single-target string
    report_pdf.py              — .md report -> RTL Hebrew PDF (Playwright + markdown package)
    telegram_send.py           — send_text/send_photo/send_document/get_updates. HTML parse_mode by default
                              (not legacy Markdown — a bare underscore in a protocol doc name broke it once).
                              escape_html() for any raw/dynamic text. _fix_rtl_bidi() forces correct
                              right-to-left rendering on lines mixing Hebrew with English/numbers
    output_gate.py             — A8: classify_output() -- analysis-only vs actionable, the one checkpoint every
                              report passes through. Six conditions as of the Hardening Pass: sender_authorized,
                              data_timestamp_recorded, sleeve_known, source_provenance_ok, earnings_verified
                              (item 5 -- defaults False, never inferred, never from model memory), data_fresh
                              (item 6 -- defaults True for backward compatibility, sourced from
                              tv_data.assert_data_fresh())
    sleeve.py / sleeve_map.json — A7: per-ticker core|swing classification. Hard rule (2026-07-13, user-specified,
                              in persistence.get_sleeve()): SPY/QQQ are always "core", every other ticker is
                              always "swing" -- the DB's thesis.sleeve column is no longer read for this. "unknown"
                              can no longer occur, so output_gate.py's sleeve_known condition is permanently
                              satisfied in practice (kept as a no-op safety net, not removed)
    persistence.py              — A5/A6 plus, as of the Hardening Pass: analysis_runs.lint_result (item 3),
                              thesis.decision/rejection_reasons + shadow_outcomes table (item 7, shadow-book
                              capture), circuit_breaker_status()/get_consecutive_stopout_streak() (item 8,
                              CIRCUIT_BREAKER_STOPOUTS in .env, warning-only, inactive unless explicitly set)
    test_indicators_core.py, test_widget_render.py, test_report_lint.py, test_output_gate.py, test_tv_data.py,
    test_score_shadow.py, test_circuit_breaker.py — regression tests (RS-presence assertion, targets/checkpoints
                              pass-through, report_lint's arithmetic checks, output_gate's six conditions,
                              assert_data_fresh's NYSE-calendar freshness, score_shadow's MFE/MAE, and the
                              circuit-breaker streak logic against an isolated temp DB — never the real
                              trading_new.db)
  .env.example (TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID, DEFAULT_RISK_USD, CIRCUIT_BREAKER_STOPOUTS;
    ANTHROPIC_API_KEY unused by the live path), requirements.txt, start.bat
  _archive_old_api_bot\      — the retired metered-API architecture (main.py, playbooks.py, validator.py,
                              store.py, quality_checks.py, equivalence_harness.py, etc.) — kept for reference,
                              not imported by anything live
```

**No `indicators.py`, no `playbooks.py`, no `store.py` in the live path.** This is deliberate, not an oversight — see "Category A/B" above and `_archive_old_api_bot/README` context for why the old design was retired.

## Non-negotiable behaviors carried over from the protocol files themselves

These aren't new — they're already written into `SCREENER_v3.md`, `MONITOR_v2.md`, `STRATEGY_v3.md`, and `CONSISTENCY_RULES.md`. Listed here only so nothing gets silently reimplemented differently:

- Never invent a price level. Every stop/target/support/resistance must trace back to something in the actual OHLCV data or an explicitly shown calculation.
- "Base" for a stop = the structure the specific thesis depends on, tested by "does price have to pass through this to reach the trigger" — not the nearest-looking level from an unrelated part of the chart.
- Target validity: ≥1.5x ATR14 from reference price AND R:R ≥ 2:1 (2.5:1 in the 1.0x–1.5x ATR band). Fails either → Checkpoint, not a sellable target.
- Resistance-wall scan (rule 11) before testing any target, including the recursive re-check once a level passes the gate (rule 11's recursive step, A4b).
- Stop-noise floor: ≥0.7x ATR14 in both directions, for new entries and for any stop update on an open position.
- Two setups shown = two near-term plausible directions, tracked in parallel — never two depths of the same direction, never a strict preferred/backup sequence.
- No data above all-time-high → Runner-only, reduced size, trailing stop, no R:R shown — but the allocation table is still shown, never silently skipped (rule 6/14).
- Exit allocation: single target → 40%/60% Runner. Two targets → 40%/35%/25%.
- Core/Layer1 holdings (explicitly flagged via `sleeve.py`, never inferred) are exempt from all target/stop/2H-alert machinery above — wide structural stop only.
- 2H confirmation means different things in Monitor (new entry: present Starter-vs-wait options) vs. Strategy (existing position: re-check trigger, never an auto-add).
- Re-entry cooldown applies only to a position that was actually opened and stopped out — not to a thesis invalidated before any fill.
- RS window depends on setup type: 20-trading-day for trend-following, 20-day + 5-day both shown for reversal setups (rule 15, `CONSISTENCY_RULES.md`).

## Known limitations (deferred on purpose, or genuinely open)

- No live push alerts beyond the queue-and-ack model — the bot only acts on messages sent to it (fully event-driven now, for every command); it never initiates contact.
- `/filled`'s starter-or-full disambiguation (the old multi-turn `awaiting_reply` mechanism) was deleted along with the rest of the pre-2026-07-09 queue layer and deliberately NOT rebuilt — instead, `/filled TICKER price qty starter|full` requires the type as an explicit 4th argument in the command itself, making it fully deterministic (no LLM call, no conversational state machine to keep reliable). A `/filled` without that 4th argument gets a one-line correction telling the sender to resend with it.
- The formal "Category B triple-run consistency check" (re-running a wall/anchor/setup-type classification 3 times and flagging disagreement) described in earlier drafts of this file was never actually implemented as an automated mechanism — treat it as a personal-discipline recommendation (re-check your own reasoning on anything that materially affects the output) rather than a built feature, until/unless it's actually built.
- No real earnings-calendar data source exists yet (item 5, Hardening Pass) — `tv_data.py` fetches OHLCV only. `earnings_verified` defaults to False/absent and forces `analysis_only` until a real fetch (a `tv_data.py` addition or a web source) is built; this is a marked TODO, not a workaround.
- Shadow-book capture (item 7, Hardening Pass: `thesis.decision`/`rejection_reasons`, the `shadow_outcomes` table, `bot/score_shadow.py`) is capture-only by design — no reporting, calibration, or conclusions are built on top of it yet. Judge it in Q4 with ≥30–40 closed trades, same deferral as journal-based threshold/rubric calibration above.
- The circuit breaker (item 8, Hardening Pass) is a standing warning only, never a block, and is inactive by default (no `CIRCUIT_BREAKER_STOPOUTS` in `.env` = no invented limit). Portfolio-heat/correlation-risk checks across the whole book were explicitly excluded from the Hardening Pass by the user's own decision, not deferred by oversight — do not build a `/risk` command or similar without being asked again.
- `report_lint.py` (item 3, Hardening Pass) never judges which level SHOULD have been chosen — only whether the numbers already written down are internally consistent. A clean lint result is not the same claim as "this is a good setup"; Category B judgment (this file's own permanent principle above) still applies in full.

## Verification before trusting a change here

1. `tv_data.py` standalone against a TradingView chart open in Chrome (CDP debug port on): pagination stitches correctly (compare a computed value from the raw data against TradingView's own displayed indicator for the same day), today's 2H/30min bars, all three failure modes produce distinct messages.
2. A screener run on a real ticker matches `SCREENER_v3.md`'s required sections including the mandatory allocation table.
3. A monitor run correctly distinguishes "tested but not closed" from "closed beyond trigger," per `MONITOR_v2.md`'s status tiers.
4. A playbook run with a real portfolio screenshot reads positions correctly and gives SPY/QQQ Core (wide structural stop only) and every other ticker Swing treatment, per the hard rule in `persistence.get_sleeve()` -- "unknown" can no longer occur, so there is no longer a real case for this test to construct.
5. Deliberately construct a case where a threshold fails (e.g. a target under 1.5x ATR): confirm the reply marks it a Checkpoint, not a sellable target, with no usable order presented for it.
6. Access control: message from a second, unauthorized Telegram account — confirm it is not acked, not queued, and appears only in the minimal rejection log (A1).
7. Visual check in real Telegram — widget/PDF render cleanly on a phone, Hebrew RTL correct, not cut off by length limits.
