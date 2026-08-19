# Master System Spec — Swing Trading Operating System (Telegram Bot)

## 1. Purpose

A single operational system that wraps three existing protocol documents — **SCREENER_v3** (pre-market entry screening), **MONITOR_v2** (intraday tracking), **STRATEGY_v3** (open-position management) — into one Telegram-bot-driven workflow. The bot is the interface; the three protocols remain the decision logic. This spec does not replace those documents — it defines how a bot should route to them, what state it needs to hold between them, and what rules must stay consistent across all three regardless of which one is active in a given moment.

## 2. The Three Protocols — Roles and Triggers

| Protocol | Question it answers | When it fires | Typical input |
|---|---|---|---|
| **SCREENER_v3** | "Is this a valid new trade, before I enter?" | Pre-market, or any ad-hoc "should I enter X" check with no existing position | Daily chart + daily CSV (+ SPY/QQQ context) |
| **MONITOR_v2** | "Has a thesis I already built actually triggered yet?" | During market hours, for a ticker with a pending SCREENER_v3 thesis, or a fast ad-hoc intraday check | 30m/2H chart + CSV, referencing the original thesis |
| **STRATEGY_v3** | "What do I do with what I already own?" | Pre-market or anytime, for tickers with an open position | Broker positions table (qty/avg cost/cash) + daily charts for each holding + SPY/QQQ |

**State transition rule (must be enforced by the bot, not just the docs):**
`SCREENER_v3 (idea) → MONITOR_v2 (waiting for trigger) → [trigger fires] → STRATEGY_v3 (owns it until closed)`
A ticker is owned by exactly one protocol at a time. The bot must know which state each ticker is in — this is the single most important piece of state to persist.

## 3. Required Persistent State (per ticker)

The bot needs a lightweight record per ticker, not just a stateless chat:

```
{
  "ticker": "MSFT",
  "status": "pending" | "open_position" | "closed",
  "thesis": {
    "source": "SCREENER_v3",
    "date_built": "...",
    "primary_setup": { "type": "...", "trigger": "...", "stop": "...", "target": "...", "target_source": "...", "atr_at_build": "..." },
    "alternate_setup": { ... }
  },
  "monitor_log": [ { "timestamp": "...", "check_status": "🟡/🟡➕/🟢/🔴", "note": "..." } ],
  "position": { "qty": "...", "avg_cost": "...", "current_stop": "...", "targets": [...] }
}
```
Without this, every check becomes a cold re-analysis instead of a continuation — which is exactly the failure mode that produced several corrections during today's session (stop tied to the wrong base level, targets re-derived inconsistently, etc.). Persisting the *thesis itself*, not just the ticker name, is what lets MONITOR_v2 say "the trigger you defined this morning is 388.80" instead of re-deriving it.

## 4. Telegram Bot Requirements

### 4.1 Input handling
- Accept chart screenshot(s) + CSV export as a single message bundle (as currently done manually).
- Accept a broker positions screenshot (IBKR-style) as a distinct input type for STRATEGY_v3 runs.
- No live market-data feed assumed initially — the bot is a *response* tool to what the person sends, not a poller. (Future version could add polling; out of scope for v1.)

### 4.2 Intent routing
The bot must infer which protocol to invoke from context, in this priority order:
1. **Explicit instruction** ("run STRATEGY", "check MSFT vs its trigger") — always wins.
2. **Ticker state lookup** — if the ticker has a `pending` thesis and intraday-resolution chart is attached → MONITOR_v2. If `open_position` → STRATEGY_v3. If unknown/new → SCREENER_v3.
3. **Time-of-day heuristic** (weak signal only, never overrides 1–2) — pre-market message with no open position on that ticker leans SCREENER_v3; mid-session leans MONITOR_v2.
4. **Ambiguous case** → ask, don't guess. (Matches the existing "don't invent data" principle — routing ambiguity is the same category of error as inventing a price level.)

### 4.3 Output format
- Default: structured text following each protocol's own mandated section order (tables first, then analysis, then orders, then summary) — this format is already defined inside each .md and should not be re-specified here.
- On request: rendered HTML widget (dark theme, card-based) matching the format already used throughout today's session — decision badge, metrics row, setup cards, targets/exit table, verdict.
- Every response that proposes a stop or target must show its ATR multiple and source inline — this is non-negotiable across all three protocols (see §5).

### 4.4 Session vs. one-shot
- Each Telegram message can be self-contained (attach chart+CSV, get an answer) — no requirement to keep a live conversation thread open.
- The state store (§3) is what makes this work across messages sent hours or days apart, not conversation memory.

## 5. Cross-Protocol Rules That Must Never Drift

**See `CONSISTENCY_RULES.md` for the full, current rule set — not duplicated here.** That file is the single canonical copy (currently 15 rules, extended several times since this spec was first written as real gaps were found — resistance-wall chaining and its recursive step, target-source scan requirements, the SMA-not-a-target-source rule, the no-silent-section-omission rule, the RS-window trend-vs-reversal distinction, and more). This section used to hold a second full copy that drifted out of sync with the canonical one (missing several rules entirely, missing clarifying paragraphs added to others after real misses) — that duplication is exactly the failure mode `CONSISTENCY_RULES.md`'s own header warns about, so it has been removed rather than re-synced by hand again. If a rule needs to change, it changes in `CONSISTENCY_RULES.md` only.

## 6. Data Sources the Bot Needs Access To

- TradingView chart exports (screenshot) — daily for SCREENER_v3/STRATEGY_v3, 30m/2H for MONITOR_v2.
- Matching OHLCV CSV export, same timeframe as the chart.
- SPY and QQQ, same cadence, every run (missing today in several checks — should become a hard requirement, not optional).
- Broker positions export (IBKR web UI screenshot works today; API integration would remove this manual step in a later version).
- No paid data feed assumed. Everything today has come from data the person exports and sends manually — the bot should be designed around that constraint first, and only add live polling as a later enhancement.

## 7. Explicitly Out of Scope for v1

- Automated order placement. The system produces order specifications (price, type, stop, GTC/day, size) — a human sends them to the broker.
- Continuous background monitoring / push alerts without the person sending fresh data. v1 is pull-based: person sends data, bot responds.
- Multi-account or multi-strategy support beyond the current single IBKR account and the existing three protocols.

## 8. File Manifest

| File | Role |
|---|---|
| `SCREENER_v3.md` | Entry logic — source of truth for how a thesis is built |
| `MONITOR_v2.md` | Intraday tracking logic |
| `STRATEGY_v3.md` | Open-position management logic |
| `MASTER_SYSTEM_SPEC.md` (this file) | How the three are wired together, what state persists, what the bot must never regress on |

Superseded and safe to delete: `SCREENER_v2.md`, `MONITOR_v1.md`, `STRATEGY_v2.md`.

## 9. Utility Commands — `/pending`, `/list`, `/drop`, `/filled`, `/exit`, `/journal` (pure persistence, minimal judgment)

None of these map to a full daily protocol report above — no chart required, no `SCREENER_v3`/`MONITOR_v2`/`STRATEGY_v3` section format, no PDF.

**As of the 2026-07-09 rebuild (full-automation pass, same day), `/pending`, `/list`, `/drop`, `/filled`, `/exit`, and `/journal` are all handled automatically** by `bot/process_queue.py` — a plain, deterministic Python dispatcher (no LLM call, no live Claude Code session needed) that `ack_listener.py` triggers event-driven, immediately after queueing the message. The steps below describe exactly what that script does; a live Claude session will normally never even see these six commands, since process_queue.py answers them within seconds. **`/filled` was redesigned, not rebuilt as originally planned** — instead of the multi-turn `awaiting_reply` mechanism in §10 (never rebuilt, see that section's status note), `entry_type` (starter/full) is now a required 4th argument in the command itself (`/filled TICKER price qty starter|full`), making the whole command a single deterministic write with no conversational state to keep reliable.

- **`/pending`** — aggregate view of every `thesis` row with `status='pending'`.
  1. Fetch SPY/QQQ once (reuse that one fetch across every row below — never re-fetch per ticker; `/pending`'s per-ticker data all comes from already-stored `monitor_log` rows, not a fresh chart pull) and run STRATEGY_v3.md §ב's six-way market classification for the overall market today. Translate the result to the exact snake_case key `persistence.is_regime_more_risk_off()` compares against, not the Hebrew label: Risk On → `risk_on`, מגמה עולה בריאה → `healthy_uptrend`, תיקון בתוך מגמה עולה → `pullback_in_uptrend`, ניטרלי-Choppy → `neutral_choppy`, Risk Off → `risk_off`, שבירת מבנה → `structure_break`.
  2. Call `persistence.get_pending_report_rows(current_regime=<that key>)`. **This call fails loudly, not silently, on a bad label** — pass anything outside the six keys above and it raises `ValueError` immediately (a caller bug on this one call, not swallowed). A stored thesis row whose *own* `market_regime_at_build` predates a later wording change to `SCREENER_v3.md` doesn't take the whole report down — it surfaces as a visible `"regime_label_unrecognized"` entry in that row's `flag_reasons` (and a `persistence` logger warning) instead of just silently never flagging, which is what happened before this was fixed.
  3. Call `widget_render.pending_aggregate_to_widget_data(rows, date)` → `render_widget_png()` → send as a photo.
  4. Send one HTML text summary (`send_text`) listing flagged tickers and why (age vs. regime, from each row's `flag_reasons`). No PDF and no per-ticker report — the widget already is the full content for this command.
- **`/list`** — the fast, text-only version of `/pending`: just ticker + status, no widget, no market-regime fetch at all (that's the entire point — it's for a quick glance, not a flagged report). Call `persistence.get_pending_report_rows(current_regime=None)` (skips the regime-flag check entirely, per that parameter's own documented behavior — age-flag still computes since it needs no live fetch) and send one HTML text message (`send_text`), one line per row: ticker, the status emoji (`⚪`/`🟡`/`🟡➕` from `latest_status`, defaulting to `⚪` same as `pending_aggregate_to_widget_data` does), and `days_pending`. If the list is empty, say so explicitly ("אין טיקרים ב-Pending כרגע") — never send a blank message. No photo, no PDF, no SPY/QQQ fetch — if the user wants the flagged/widget version, that's `/pending`.
- **`/drop TICKER reason`** — call `persistence.drop_thesis(ticker, reason)`, then reply with a one-line HTML confirmation via `send_text`. No fetch, no widget. The row is never deleted — it's just excluded from `/pending`'s `status='pending'` filter from that point on.
- **`/filled TICKER price qty starter|full`** — **automated** (see intro above). Redesigned 2026-07-09 to avoid needing the never-rebuilt `awaiting_reply` mechanism (§10): `entry_type` is a required 4th argument in the command itself, not a follow-up question. `process_queue.py`'s `_handle_filled`:
  1. If the 4th argument is missing, replies with a one-line correction showing the exact syntax and marks the message failed (`entry_type not specified`) — never guesses.
  2. Requires a stored thesis with a `primary_setup` (`persistence.get_thesis(ticker)`) — refuses with a one-line explanation if none exists (run `/screener` first), never fabricates a setup.
  3. Checks for a probable duplicate fill (`persistence.find_possible_duplicate_fill(ticker, qty, entry_date)` — same ticker + qty within ±5% + same/adjacent trading day as an existing open position). If found, refuses and asks the sender to resend with a materially different qty or handle it manually — never silently merges.
  4. Calls `persistence.create_position(ticker, entry_date, entry_price, qty, entry_type, entry_setup=thesis["primary_setup"], initial_stop=entry_setup.get("stop"))` — this both writes the position and flips `thesis.status` to `'open_position'`.
  5. Replies with a one-line HTML confirmation via `send_text` (including the `position_id`), then `persistence.mark_sent(update_id, ...)`.
- **`/exit TICKER price qty`** — **automated** (see intro above). `process_queue.py` calls `persistence.record_exit(ticker, exit_price, exit_qty, exit_date, source="exit_command")` — this internally matches the price against the open position's own stored `entry_setup` via `indicators_core.derive_exit_reason()`, computes R-multiple via `indicators_core.compute_r_multiple()` (or leaves it `NULL` if the position has no known original-risk stop — never fabricated), and auto-closes the position/thesis/generates the closing summary once cumulative exit qty reaches the position's full size. Replies with a one-line HTML confirmation stating the matched reason (`stop`/`target_1`/`target_2`/`unmatched`) and the R-multiple.
- **`/journal`** — **automated** (see intro above). Calls `persistence.get_journal_rows()` → `persistence.summarize_journal(rows)`, sends one HTML text summary (count, average R-multiple, win rate) — no widget, no PDF. The `thesis_validated`/lesson question is a separate command, `/reflect` (below) — deliberately not in this automated dispatcher.

- **`/reflect [TICKER]`** — **NOT automated, requires a live Claude Code session**, same tier as `/screener`/`/monitor`/`/playbook`, unlike the six commands above. Reasoning why a close was a win/loss (P/L) is arithmetic `process_queue.py` already does in `/journal`; judging whether the *thesis itself* held up is a real judgment call (a trade can close positive on a thesis that was actually invalidated early — a lucky bounce — or close negative on a thesis that stayed correct right up to a normal stop-out) and belongs with the other judgment protocols, not the zero-cost dispatcher.
  1. Call `persistence.get_unreflected_closes(ticker)` — every `closing_summaries` row with `thesis_validated IS NULL`, oldest first (or just the one row if `TICKER` is given).
  2. For each row: read back the original `setup_type`, `rubric_grade`, and `total_r_multiple`, plus that ticker's entry_setup at the position level if still useful context. Write exactly 2-4 plain-prose sentences per TradingAgents' reflection pattern: (1) was the directional call right — cite the R-multiple; (2) which part of the setup thesis (trigger, stop placement, target) held or failed; (3) one concrete, specific lesson for the next similar setup. No bullets, no headers — terse prose that earns re-reading later.
  3. Call `persistence.record_reflection(closing_summary_id, thesis_validated, lesson)` for each row.
  4. Reply with one HTML text summary listing each ticker and its one-line verdict (no widget, no PDF — same weight as `/journal`).
  - **Memory injection into `SCREENER_v3` — REMOVED 2026-08-10 (owner's decision, CONSISTENCY_RULES.md rule 25).** A new thesis no longer reads `get_ticker_lessons` or `get_recent_cross_lessons`, and the screener prompt now tells the model explicitly not to look them up. Reason: a lesson comes from one closed trade, and letting it move a decision means the shadow book can never separate the rules working from the owner remembering. The reflections themselves are still written and still stored — they are a labelled record to be read later, in bulk, by the shadow-book analysis. Rule 26's regime disclosure is unaffected and stays on: it rests on 305 backtested trades rather than one.

## 10. The "awaiting a specific follow-up reply" mechanism — design only, superseded, not built

**Status as of 2026-07-09: deleted along with the rest of the old queue layer, and deliberately NOT rebuilt** — `/filled` was redesigned instead (see §9) to require `entry_type` as an explicit 4th command argument, avoiding the need for any multi-turn conversational state entirely. The design below is preserved only as historical context for why that alternative was chosen (a real, working multi-turn mechanism is meaningfully harder to keep reliable than a stricter command syntax) — it doesn't correspond to any real code and nothing in the live system assumes it exists.

Doesn't exist anywhere before Journal/Pending-flow Stage 3 — `ack_listener.py` has always treated every message as fully independent, but `/filled` genuinely needs an answer that arrives as a **separate** Telegram message, processed in a separate invocation (there's no live session holding state between the question and the answer, per this file's own "no orchestrator" principle in `CLAUDE_CODE_INSTRUCTIONS.md`).

**Concurrency invariant — scoped per ticker, not globally.** Two tranched entries on different tickers the same morning is plausible and legitimate, so multiple open questions across *different* tickers are expected and fine — nothing serializes across them. A second open question on the *same* ticker is rejected outright by `ack_listener.py` itself (`persistence.has_open_awaiting_reply(ticker)`, a pure deterministic DB lookup — not protocol judgment, so it's allowed at this level, same as the existing `NOT_YET_WIRED` instant replies).

**`ack_listener.py` stays dumb — it does no disambiguation.** For every incoming text message, it calls `persistence.get_open_awaiting_replies(chat_id)`; if non-empty, it passes that list as `reply_context` into `enqueue_message()` (stored verbatim, just a snapshot — not interpreted). It has no idea whether this particular text is actually a reply to anything.

**Disambiguation is Category B judgment — happens downstream, in the real analysis step, never in the dumb listener:**
1. Re-query `persistence.get_open_awaiting_replies(chat_id)` fresh (the `reply_context` snapshot on the message row can be stale — always re-check live state, don't trust the snapshot as current truth).
2. **Exactly one open question** → resolve against it directly.
3. **More than one open** (the legitimate two-different-tickers case) → read the reply text and check whether it unambiguously identifies one of the open tickers (e.g. "starter XLV" against `{XLV, GOOGL}` open) → if so, resolve that one.
4. **Genuinely ambiguous** (e.g. "starter" with two tickers open and nothing in the text distinguishing them) → ask directly which ticker this answers. This is itself a new question that re-arms the same mechanism: `persistence.mark_awaiting_reply(update_id, ticker=None initially — resolved once the disambiguation reply names one, question, kind="disambiguate_ticker")`. Never silently guess "the most recent one."
5. Once resolved to a specific open question: call `persistence.resolve_awaiting_reply(update_id)` on the **original** message that asked it (this clears `awaiting_reply_to` and returns that message to `'processing'` so its write can complete — it does not itself call `mark_sent`; the caller still must do that once the actual work, e.g. `create_position`, is done).

**Not a blocker, but watch this happen live before trusting it blindly:** step 3's "does the reply text unambiguously identify a ticker" check is real free-text interpretation, not a mechanical lookup. The first few times two concurrent questions are genuinely open and a reply comes in, manually confirm it resolved against the ticker it was actually meant for, not a phrasing-quirk misfire — same "verify, don't assume" discipline as the rest of this project.
