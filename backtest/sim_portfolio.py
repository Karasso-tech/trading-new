"""Portfolio backtest: N random S&P 500 stocks through the SAME per-ticker
engine as sim_breakout.py (live system code: setup_classifier, level_picker,
rubric_formula, regime_formula, decision_policy), with a book-level cap.

Pre-registered mechanics on top of sim_breakout.py's (owner, 2026-08-11):
  * 50 tickers drawn at random with a FIXED seed (recorded in the output).
  * Max 6 positions open at once. One position per ticker, ever, at a time.
  * Each position risks 1% of CURRENT total equity (cash + open positions
    marked at that day's close).
  * When more triggers fire on one day than there are free slots: better
    build-grade enters first (A, then B, then C), ties alphabetical. The
    skipped fires are recorded; their plans stay live for the next day.
  * Cash is a hard cap -- a position is never bought with money the book
    does not have. Partial-exit proceeds return to cash the day they happen.
  * Everything else identical to the one-stock run: plan built on day t can
    fire day t+1 onward on a settled close above trigger, entry at the fire
    day's close, stop before targets, gap fills at the open, trail only
    after target 1, force-close at the last bar.

Usage: python backtest/sim_portfolio.py [--n 50] [--seed 42] [--years 5]
Output: printed summary + backtest/results_portfolio_<seed>.json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "bot"))
sys.path.insert(0, str(ROOT))

import decision_policy
import regime_formula

import sim_breakout as engine

DATA = ROOT / "data"
START_EQUITY = 100_000.0
RISK_PCT = 0.01
GRADE_RANK = {"A": 0, "B": 1, "C": 2, None: 3}


def pick_universe(n: int, seed: int) -> list[str]:
    data = json.loads((DATA / "snpdata.json").read_text(encoding="utf-8"))
    have_bars = {p.stem for p in (DATA / "bars").glob("*.json")
                 if not p.stem.startswith("_")}
    pool = sorted(t for t in data["tickers"] if t in have_bars)
    return sorted(random.Random(seed).sample(pool, n))


def day_regime(spy, qqq, si, qi) -> str:
    spy_win = spy[max(0, si - engine.INDEX_WINDOW_BARS + 1):si + 1]
    qqq_win = qqq[max(0, qi - engine.INDEX_WINDOW_BARS + 1):qi + 1]
    return regime_formula.classify_regime(
        engine.index_snapshot(spy_win), engine.index_snapshot(qqq_win)).regime


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--years", type=float, default=5)
    parser.add_argument("--max-positions", type=int, default=6)
    parser.add_argument("--rank", choices=["grade", "rs20"], default="grade",
                        help="who wins the slot when fires exceed free slots: "
                             "grade = build grade then alphabetical (the original rule); "
                             "rs20 = strongest 20-day relative strength vs SPY first")
    # Pre-registered 2026-08-11 from the 335-signal book (seed 42), BEFORE this
    # rerun: (1) no entry while price is below its own SMA20, (2) no chasing an
    # entry more than 2% above the trigger, (3) no grade C at fire (A/B only).
    parser.add_argument("--entry-filters", action="store_true",
                        help="apply the three pre-registered entry filters above")
    parser.add_argument("--skip-sma-filter", action="store_true",
                        help="with --entry-filters: drop rule (1), keep only "
                             "no-chase and no-grade-C (owner request 2026-08-11)")
    parser.add_argument("--rr-min", type=float, default=None,
                        help="RESEARCH ONLY: score the rubric's R:R criterion against this "
                             "minimum instead of 2.3. See PREREGISTRATION_RR_THRESHOLD.md.")
    parser.add_argument("--drop-rr-criterion", action="store_true",
                        help="RESEARCH ONLY: do not score R:R at all -- four criteria, "
                             "cutoffs shifted down one. The removal arm.")
    parser.add_argument("--allow-grade-d-research", action="store_true",
                        help="RESEARCH ONLY, never live: let a D-graded fire enter. "
                             "The live system blocks D (rule 27) and this backtest has "
                             "always blocked it too -- 289 D plans were refused at build "
                             "on seed 42 alone -- so the question 'is a D trade actually "
                             "bad' has never once been measured. --skip-grade-filter does "
                             "NOT do this: it only lifts the C filter.")
    parser.add_argument("--skip-grade-filter", action="store_true",
                        help="with --entry-filters: drop rule (3), so C grades "
                             "may enter (owner request 2026-08-11)")
    # Pre-registered 2026-08-13 (PREREGISTRATION_EARLY_TRAIL.md), owner's own
    # variant: once a trade is up 1.5x ATR, start trailing 1.2x ATR under the
    # running peak, before target 1. Default off = today's live rule.
    parser.add_argument("--early-arm-atr", type=float, default=None,
                        help="arm the early trail at this profit, in ATRs (1.5)")
    parser.add_argument("--early-trail-atr", type=float, default=1.2,
                        help="trail distance under the running peak, in ATRs")
    parser.add_argument("--early-anchor", choices=["close", "high"],
                        default="close", help="peak measured on closes or highs")
    # Close-only stop (pre-registered 2026-08-19). Default off = live rule.
    parser.add_argument("--stop-basis", choices=["intraday", "close"],
                        default="intraday",
                        help="'close' = a stop only counts on a settled close")
    parser.add_argument("--stop-fill", choices=["close", "next_open"],
                        default="close",
                        help="with --stop-basis close: sell at that close or "
                             "at the next day's open")
    parser.add_argument("--emergency-atr", type=float, default=None,
                        help="second, hard stop this many ATRs BELOW the "
                             "normal stop, fired intraday (needs --stop-basis "
                             "close to mean anything)")
    # Entry timing (added 2026-08-19). Default off = today's live rule: buy at
    # the close of the day whose settled close cleared the trigger.
    parser.add_argument("--entry-fill", choices=["signal_close", "next_open"],
                        default="signal_close",
                        help="'next_open' buys at the following bar's open "
                             "instead of the signal day's close")
    # Trading costs (added 2026-08-20). Default off = the frictionless run
    # every earlier study used.
    parser.add_argument("--costs", action="store_true",
                        help="charge IB tiered commission and slippage")
    parser.add_argument("--slippage-bps", type=float, default=5.0,
                        help="basis points paid per side when --costs is on")
    parser.add_argument("--cost-multiplier", type=float, default=1.0,
                        help="scale commission and slippage (2 = twice the "
                             "best estimate)")
    # DIAGNOSTIC ONLY, off by default. Scores the rubric without its earnings
    # criterion, to size what the missing earnings history costs. Not a rule.
    parser.add_argument("--drop-event-criterion", action="store_true",
                        help="grade on 4 criteria instead of 5, cutoffs moved "
                             "down one (diagnostic, never a live rule)")
    parser.add_argument("--commission-per-share", type=float, default=None,
                        help="override the per-share commission (0 = a flat "
                             "per-order fee only)")
    parser.add_argument("--commission-min", type=float, default=None,
                        help="override the per-order commission floor")
    parser.add_argument("--commission-cap-pct", type=float, default=None,
                        help="override the cap on a commission as a share of "
                             "the order's value (1.0 = effectively no cap)")
    # The real capital structure (added 2026-08-26). Default off.
    parser.add_argument("--portfolio-model", action="store_true",
                        help="hold a passive core and trade only the sleeve")
    parser.add_argument("--core-pct", type=float, default=0.60,
                        help="share of the account held passively")
    parser.add_argument("--core-spy-pct", type=float, default=0.60,
                        help="SPY share within the core (rest is QQQ)")
    # The daily monitor (added 2026-08-27, see MONITOR_GAP.md). Default off.
    parser.add_argument("--monitor", action="store_true",
                        help="run the daily playbook on every open position: "
                             "per-day ledger, rebuilt picture, the re-entry "
                             "wait after a stop-out, and a fresh target scan "
                             "for a runner that has outrun its plan")
    parser.add_argument("--monitor-retarget", action="store_true",
                        help="with --monitor: actually sell at a rebuilt "
                             "runner target (40%% of what is still held). The "
                             "share count is NOT specified by any rule -- this "
                             "exists to measure the effect, not as a proposal")
    # DIAGNOSTIC ONLY, off by default. Skips a fire that would buy more than
    # L dollars of stock per dollar risked. Not a proposed rule.
    parser.add_argument("--max-entry-leverage", type=float, default=None,
                        help="skip fires where entry/(entry-stop) exceeds this")
    parser.add_argument("--tag", default="", help="suffix for the output file")
    args = parser.parse_args()
    engine.EARLY_TRAIL.update(arm_atr=args.early_arm_atr,
                              trail_atr=args.early_trail_atr,
                              anchor=args.early_anchor)
    engine.STOP_MODE.update(basis=args.stop_basis, fill=args.stop_fill,
                            emergency_atr=args.emergency_atr)
    engine.ENTRY_MODE.update(fill=args.entry_fill)
    engine.RUBRIC_MODE.update(drop_event=args.drop_event_criterion)
    engine.MONITOR_MODEL.update(on=args.monitor,
                                retarget=args.monitor_retarget)
    engine.RR_RULE.update(min=args.rr_min, drop=bool(args.drop_rr_criterion))
    engine.PORTFOLIO_MODEL.update(on=args.portfolio_model, core_pct=args.core_pct,
                                  core_spy_pct=args.core_spy_pct,
                                  core_qqq_pct=1.0 - args.core_spy_pct)
    if args.costs:
        m = args.cost_multiplier
        engine.COSTS.update(on=True, per_share=0.0035 * m,
                            min_per_order=0.35 * m,
                            slippage_bps=args.slippage_bps * m)
        for key, val in (("per_share", args.commission_per_share),
                         ("min_per_order", args.commission_min),
                         ("max_pct_of_trade", args.commission_cap_pct)):
            if val is not None:
                engine.COSTS[key] = val

    tickers = pick_universe(args.n, args.seed)
    print(f"universe ({args.n}, seed {args.seed}): {', '.join(tickers)}")

    spy = engine.load_bars("SPY")
    qqq = engine.load_bars("QQQ")
    spy_ix = {b["date"]: i for i, b in enumerate(spy)}
    qqq_ix = {b["date"]: i for i, b in enumerate(qqq)}

    bars_of, ix_of, earnings_of = {}, {}, {}
    for t in tickers:
        bars_of[t] = engine.load_bars(t)
        ix_of[t] = {b["date"]: i for i, b in enumerate(bars_of[t])}
        earnings_of[t] = engine.load_earnings(t)
    no_earnings = sorted(t for t in tickers if not earnings_of[t])

    last_day = date.fromisoformat(spy[-1]["date"])
    sim_start = (last_day - timedelta(days=int(args.years * 365.25))).isoformat()
    calendar = [b["date"] for b in spy if b["date"] >= sim_start]

    PM = engine.PORTFOLIO_MODEL
    spy_close = {b["date"]: b["close"] for b in spy}
    qqq_close = {b["date"]: b["close"] for b in qqq}
    core_spy_sh = core_qqq_sh = 0.0
    if PM["on"]:
        core_usd = START_EQUITY * PM["core_pct"]
        core_spy_sh = core_usd * PM["core_spy_pct"] / spy_close[calendar[0]]
        core_qqq_sh = core_usd * PM["core_qqq_pct"] / qqq_close[calendar[0]]
        cash = START_EQUITY - core_usd          # the sleeve is all a trade gets
    else:
        cash = START_EQUITY
    positions: dict[str, engine.Position] = {}
    credited: dict[str, int] = {}          # exits already returned to cash
    pending: dict[str, dict] = {}
    deferred: dict[str, dict] = {}         # fires waiting for the next bar's open
    entry_slip = []                        # signal close -> next open, per fire
    trades = []
    blocked_at_build: dict[str, int] = {}
    blocked_at_fire = []
    skipped_capacity = []
    skipped_sizing = []
    cash_capped = []                       # entries shrunk by the cash cap
    skipped_no_cash = []                   # fires the sleeve could not afford
    skipped_leverage = []                  # fires above the leverage cap
    skipped_cooldown = []                  # fires inside the re-entry wait
    cooldown_from: dict[str, int] = {}     # ticker -> day index of its stop-out
    pos_share_of_sleeve = []               # each entry's cost / sleeve value
    entry_leverage = []                    # entry / (entry - stop), per fire
    grades_at_build: dict[str, int] = {}    # every plan built, by grade
    grades_at_fire: dict[str, int] = {}     # every fire re-scored, by grade
    equity_curve = []

    for day_idx, d in enumerate(calendar):
        si, qi = spy_ix.get(d), qqq_ix.get(d)
        if si is None or qi is None:
            continue
        regime = day_regime(spy, qqq, si, qi)

        # the core was bought once on the first bar and is never touched again
        core_val = (core_spy_sh * spy_close[d] + core_qqq_sh * qqq_close[d]
                    if PM["on"] else 0.0)

        days: dict[str, engine.Day] = {}
        for t in tickers:
            i = ix_of[t].get(d)
            if i is None or i < 60:        # not listed yet / warmup too short
                continue
            days[t] = engine.Day(bars_of[t], i, spy, qqq, spy_ix, qqq_ix,
                                 earnings_of[t], regime=regime)

        # 0) OPTIONAL next-open entries: fires committed on an EARLIER day fill
        # at THIS bar's open, before today's bar is applied to anything -- so
        # today's stop and targets can act on the brand new position. A ticker
        # with no bar today simply waits for its own next bar.
        for t in list(deferred):
            day = days.get(t)
            if day is None:
                continue
            dfr = deferred.pop(t)
            entry_px = engine.slip_buy(day.bar["open"])
            per_share = entry_px - dfr["plan"]["stop"]
            plan_risk = dfr["signal_close"] - dfr["plan"]["stop"]
            entry_slip.append({
                "date": d, "ticker": t,
                "signal_close": round(dfr["signal_close"], 4),
                "next_open": round(entry_px, 4),
                "slip_pct": round(100 * (entry_px / dfr["signal_close"] - 1), 4),
                "slip_r": round((entry_px - dfr["signal_close"]) / plan_risk, 4)
                          if plan_risk > 0 else None,
            })
            # `dfr["equity"]` is the SLEEVE, not the whole account -- `equity` is
            # assigned once per day as `cash + sleeve_open`, and under
            # --portfolio-model `cash` starts at START_EQUITY minus the core, with
            # the core held separately as share counts and only rejoined in
            # final_equity. Checked 2026-08-30 against a report that this path
            # risked a percent of the whole account; it does not.
            #
            # KNOWN INCONSISTENCY, deliberately NOT changed in this pass, because
            # changing it re-prices every historical result and belongs with the
            # risk-level comparison that will actually read it: under
            # --portfolio-model the SAME-DAY path REFUSES a trade the sleeve
            # cannot pay for in full (skipped_no_cash, "never quietly shrink"),
            # while this next-open path silently shrinks it to whatever cash
            # allows and records that as cash_capped. next_open is the default
            # fill mode, so in practice almost every entry takes the shrinking
            # branch. It matters for a risk-level comparison specifically: a
            # higher risk % gets cash-capped more often, and a shrink lowers the
            # realized risk instead of recording a refusal -- which flatters the
            # higher setting exactly where it should be showing strain.
            want = int((dfr["equity"] * RISK_PCT) // per_share) if per_share > 0 else 0
            qty = min(want, int(cash // entry_px))
            if qty < want:
                cash_capped.append({"date": d, "ticker": t, "wanted": want,
                                    "got": qty})
            if qty <= 0:
                skipped_sizing.append({"date": d, "ticker": t,
                                       "reason": "qty_zero_next_open"})
                continue
            pos = engine.Position(dfr["plan"], entry_px, qty, day.date,
                                  raw_entry=day.bar["open"])
            positions[t] = pos
            credited[t] = 0
            cash -= qty * entry_px + pos.entry_fee
            pending.pop(t, None)

        # 1) manage every open position; credit exits to cash as they happen
        for t in list(positions):
            day = days.get(t)
            if day is None:
                continue
            pos = positions[t]
            closed = pos.process_day(day)
            new_exits = pos.exits[credited[t]:]
            cash += sum(e["qty"] * e["price"] - e.get("fee", 0.0)
                        for e in new_exits)
            credited[t] = len(pos.exits)
            if closed:
                rec = pos.to_record()
                rec["ticker"] = t
                trades.append(rec)
                # STRATEGY_v3's re-entry wait: after a real stop-out (real
                # money was in the trade) this name cannot be bought back the
                # same day or the next trading day. Only a stop counts -- a
                # target sale is not a defensive exit, and the force-close at
                # the end of the data is not an exit the rules know about.
                if (engine.MONITOR_MODEL["on"] and pos.exits
                        and engine.is_defensive_exit(pos.exits[-1]["reason"])):
                    cooldown_from[t] = day_idx
                del positions[t], credited[t]
                pending.pop(t, None)

        # equity for sizing: cash + open positions at today's close
        sleeve_open = sum(pos.remaining * days[t].close
                          for t, pos in positions.items() if t in days)
        # Sizing money is the SLEEVE: free cash plus what the open trades are
        # worth. The core is never counted. With the model off there is no core
        # and this is the same figure the engine always used.
        equity = cash + sleeve_open

        # 2) collect fires from plans built on EARLIER days, gate them, rank them
        fires = []
        for t, plan in list(pending.items()):
            if t in positions or t in deferred or "blocked" in plan:
                continue
            day = days.get(t)
            if day is None:
                continue
            if day.close > plan["trigger"] and day.close > plan["stop"]:
                # the re-entry wait, before anything else looks at this fire:
                # inside it there is no buy to consider at all. The rule's
                # escape hatch -- "unless there is clear technical
                # confirmation" -- has no definition anywhere, so the wait is
                # unconditional here and is reported as such.
                wait = engine.MONITOR_MODEL["reentry_wait_trading_days"]
                since = cooldown_from.get(t)
                if since is not None and day_idx <= since + wait:
                    skipped_cooldown.append(
                        {"date": d, "ticker": t,
                         "stopped_out_on": calendar[since],
                         "trading_days_since": day_idx - since})
                    continue
                grade_now, _ = engine.regrade_at_fire(plan, day)
                grades_at_fire[grade_now] = grades_at_fire.get(grade_now, 0) + 1
                regime_bad = day.regime in decision_policy.BLOCKING_REGIMES
                filter_reason = None
                if args.entry_filters:
                    if (not args.skip_sma_filter
                            and day.dist_sma20_atr is not None
                            and day.dist_sma20_atr < 0):
                        filter_reason = "below_sma20"
                    elif (day.close - plan["trigger"]) / plan["trigger"] > 0.02:
                        filter_reason = "chased_over_2pct"
                    elif grade_now == "C" and not args.skip_grade_filter:
                        filter_reason = "grade_C_at_fire"
                grade_blocks = (grade_now in decision_policy.BLOCKING_GRADES
                                and not args.allow_grade_d_research)
                if grade_blocks or regime_bad:
                    blocked_at_fire.append(
                        {"date": d, "ticker": t,
                         "reason": ("regime_" + day.regime) if regime_bad
                                   else ("grade_" + grade_now)})
                elif filter_reason is not None:
                    blocked_at_fire.append({"date": d, "ticker": t,
                                            "reason": filter_reason})
                elif args.max_entry_leverage is not None and (
                        (engine.slip_buy(day.close) - plan["stop"]) <= 0
                        or engine.slip_buy(day.close)
                        / (engine.slip_buy(day.close) - plan["stop"])
                        > args.max_entry_leverage):
                    # buys too much stock per dollar risked -- turned away here,
                    # before it can take a slot, and written down as such
                    px = engine.slip_buy(day.close)
                    ps = px - plan["stop"]
                    skipped_leverage.append(
                        {"date": d, "ticker": t,
                         "leverage": round(px / ps, 3) if ps > 0 else None,
                         "cap": args.max_entry_leverage})
                else:
                    if args.rank == "rs20":
                        # strongest stock vs the market wins the slot; a missing
                        # RS reading ranks last, ties alphabetical
                        key = (-(day.rs20 if day.rs20 is not None else -1e9),)
                    else:
                        key = (GRADE_RANK.get(plan["grade"], 3),)
                    fires.append((key, t, plan, day))

        for _, t, plan, day in sorted(fires, key=lambda x: (x[0], x[1])):
            if len(positions) + len(deferred) >= args.max_positions:
                skipped_capacity.append({"date": d, "ticker": t,
                                         "grade": plan["grade"]})
                continue                    # plan stays live for tomorrow
            if engine.ENTRY_MODE["fill"] == "next_open":
                # the slot is committed tonight; the fill happens tomorrow, and
                # the size is worked out from tonight's equity, as live.
                deferred[t] = {"plan": plan, "signal_close": day.close,
                               "equity": equity}
                pending.pop(t, None)
                continue
            entry_px = engine.slip_buy(day.close)
            per_share = entry_px - plan["stop"]
            if PM["on"]:
                # 1% of the sleeve as it stands at THIS entry: earlier buys
                # today have already moved cash into open positions, and both
                # sides are counted, so the size does not fall just because
                # money is deployed.
                live_open = sum(pos.remaining * days[tt].close
                                for tt, pos in positions.items() if tt in days)
                size_equity = cash + live_open
            else:
                size_equity = equity
            want = int((size_equity * RISK_PCT) // per_share) if per_share > 0 else 0
            if per_share > 0:
                entry_leverage.append(entry_px / per_share)
            affordable = int(cash // entry_px)
            if PM["on"]:
                # never quietly shrink: if the sleeve cannot pay for the whole
                # position, the trade is turned away and recorded as such.
                if want <= 0:
                    skipped_sizing.append({"date": d, "ticker": t,
                                           "reason": "qty_zero"})
                    continue
                if affordable < want:
                    skipped_no_cash.append(
                        {"date": d, "ticker": t, "intended_qty": want,
                         "affordable_qty": affordable,
                         "intended_cost": round(want * entry_px, 2),
                         "sleeve_cash": round(cash, 2),
                         "price": round(entry_px, 4)})
                    skipped_sizing.append({"date": d, "ticker": t,
                                           "reason": "sleeve_cash"})
                    continue
                qty = want
            else:
                qty = min(want, affordable)
                if qty < want:
                    cash_capped.append({"date": d, "ticker": t, "wanted": want,
                                        "got": qty, "sleeve_cash": round(cash, 2)})
                if qty <= 0:
                    skipped_sizing.append({"date": d, "ticker": t,
                                           "reason": "qty_zero"})
                    continue
            sleeve_val = cash + sleeve_open
            if sleeve_val > 0:
                pos_share_of_sleeve.append(qty * entry_px / sleeve_val)
            pos = engine.Position(plan, entry_px, qty, day.date,
                                  raw_entry=day.close)
            positions[t] = pos
            credited[t] = 0
            cash -= qty * entry_px + pos.entry_fee
            pending.pop(t, None)

        # 3) rebuild pending plans for every flat ticker from data through today
        for t, day in days.items():
            if t in positions or t in deferred:
                continue
            plan = engine.build_plan(day)
            if plan is not None and plan.get("grade"):
                g = plan["grade"]
                grades_at_build[g] = grades_at_build.get(g, 0) + 1
            if plan is not None and "blocked" in plan:
                blocked_at_build[plan["blocked"]] = \
                    blocked_at_build.get(plan["blocked"], 0) + 1
                pending[t] = plan if "targets" in plan else None
            else:
                pending[t] = plan
            if pending[t] is None:
                pending.pop(t, None)

        deployed = sum(pos.remaining * days[t].close
                       for t, pos in positions.items() if t in days)
        mark = cash + deployed + core_val
        equity_curve.append({"date": d, "equity": round(mark, 2),
                             "core": round(core_val, 2),
                             "sleeve": round(cash + deployed, 2),
                             "sleeve_cash": round(cash, 2),
                             "deployed": round(deployed, 2),
                             "open_positions": len(positions)})

    # force-close whatever is still open at each ticker's last bar
    forced = []
    for t, pos in list(positions.items()):
        last_bar = bars_of[t][-1]
        pos.force_close(date.fromisoformat(last_bar["date"]), last_bar["close"])
        forced.append({"ticker": t, "date": last_bar["date"],
                       "pnl": round(pos.pnl(), 2), "r": round(pos.r_multiple(), 3)})
        new_exits = pos.exits[credited[t]:]
        cash += sum(e["qty"] * e["price"] - e.get("fee", 0.0)
                    for e in new_exits)
        rec = pos.to_record()
        rec["ticker"] = t
        trades.append(rec)
    positions.clear()

    core_final = (core_spy_sh * spy_close[calendar[-1]]
                  + core_qqq_sh * qqq_close[calendar[-1]]) if PM["on"] else 0.0

    # ---- summary ----------------------------------------------------------
    final_equity = cash + core_final
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]

    spy_start = next(b["close"] for b in spy if b["date"] >= sim_start)
    spy_bh = START_EQUITY * spy[-1]["close"] / spy_start

    peak, max_dd = -1e18, 0.0
    for p in equity_curve:
        peak = max(peak, p["equity"])
        max_dd = max(max_dd, (peak - p["equity"]) / peak)

    per_ticker: dict[str, dict] = {}
    for t in trades:
        s = per_ticker.setdefault(t["ticker"], {"trades": 0, "pnl": 0.0, "r": 0.0})
        s["trades"] += 1
        s["pnl"] = round(s["pnl"] + t["pnl"], 2)
        s["r"] = round(s["r"] + t["r"], 3)

    def _dd(series):
        peak, worst = -1e18, 0.0
        for v in series:
            peak = max(peak, v)
            if peak > 0:
                worst = max(worst, (peak - v) / peak)
        return round(100 * worst, 2)

    def _mean(xs): return sum(xs) / len(xs) if xs else None
    def _median(xs):
        if not xs: return None
        v = sorted(xs); n = len(v)
        return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2

    # "everything in the core, no trading at all" -- the same 60/40 blend,
    # bought once and never touched, holding the WHOLE account instead of 60%.
    a_spy = START_EQUITY * PM["core_spy_pct"] / spy_close[calendar[0]]
    a_qqq = START_EQUITY * PM["core_qqq_pct"] / qqq_close[calendar[0]]
    allcore = [a_spy * spy_close[x] + a_qqq * qqq_close[x] for x in calendar
               if x in spy_close and x in qqq_close]

    core_series = [p["core"] for p in equity_curve if "core" in p]
    sleeve_series = [p["sleeve"] for p in equity_curve if "sleeve" in p]
    sleeve_start = START_EQUITY * (1.0 - PM["core_pct"]) if PM["on"] else START_EQUITY
    core_start = START_EQUITY * PM["core_pct"] if PM["on"] else 0.0

    # longest run of losing trades, in the order they were closed
    by_exit = sorted(trades, key=lambda t: (t["exit_date"] or "", t["ticker"]))
    streak = worst_streak = 0
    for t in by_exit:
        if t["r"] <= 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0

    portfolio_view = {
        "on": PM["on"],
        "core_pct": PM["core_pct"], "core_spy_pct": PM["core_spy_pct"],
        "core_qqq_pct": PM["core_qqq_pct"], "rebalance": PM["rebalance"],
        "risk_basis": PM["risk_basis"],
        "dividends_included": False,
        "core_start": round(core_start, 2),
        "core_final": round(core_final, 2),
        "core_return_pct": (round((core_final / core_start - 1) * 100, 2)
                            if core_start else None),
        "sleeve_start": round(sleeve_start, 2),
        "sleeve_final": round(cash, 2),
        "sleeve_return_pct": round((cash / sleeve_start - 1) * 100, 2),
        "sleeve_high": round(max(sleeve_series), 2) if sleeve_series else None,
        "sleeve_low": round(min(sleeve_series), 2) if sleeve_series else None,
        "max_dd_total_pct": _dd([p["equity"] for p in equity_curve]),
        "max_dd_core_pct": _dd(core_series) if core_series else None,
        "max_dd_sleeve_pct": _dd(sleeve_series) if sleeve_series else None,
        "all_core_no_trading_final": round(allcore[-1], 2) if allcore else None,
        "all_core_no_trading_return_pct": (round((allcore[-1] / START_EQUITY - 1) * 100, 2)
                                           if allcore else None),
        "max_dd_all_core_pct": _dd(allcore) if allcore else None,
        "pos_share_of_sleeve_avg_pct": (round(_mean(pos_share_of_sleeve) * 100, 2)
                                        if pos_share_of_sleeve else None),
        "pos_share_of_sleeve_median_pct": (round(_median(pos_share_of_sleeve) * 100, 2)
                                           if pos_share_of_sleeve else None),
        "entry_leverage_avg": round(_mean(entry_leverage), 3) if entry_leverage else None,
        "entry_leverage_median": round(_median(entry_leverage), 3) if entry_leverage else None,
        "turned_away_no_slot": len(skipped_capacity),
        "turned_away_sleeve_cash": len(skipped_no_cash),
        "turned_away_leverage_cap": len(skipped_leverage),
        "max_entry_leverage": args.max_entry_leverage,
        "entries_shrunk_by_cash": len(cash_capped),
        "days_book_full": sum(1 for p in equity_curve
                              if p["open_positions"] >= args.max_positions),
        "days_book_empty": sum(1 for p in equity_curve if p["open_positions"] == 0),
        "forced_closes_on_last_bar": len(forced),
        "forced_closes_pnl": round(sum(f["pnl"] for f in forced), 2),
        "longest_losing_streak": worst_streak,
        "open_positions_at_end": len(positions),
    }
    # what risk-per-trade would let six positions fit inside the sleeve:
    # each costs risk_pct x sleeve x (entry / (entry - stop)).
    for lbl, lev in (("median", portfolio_view["entry_leverage_median"]),
                     ("avg", portfolio_view["entry_leverage_avg"])):
        if lev:
            portfolio_view["risk_pct_to_fill_6_slots_" + lbl] = round(
                100.0 / (args.max_positions * lev), 4)

    max_open = max((p["open_positions"] for p in equity_curve), default=0)
    ndays = len(equity_curve) or 1
    slot_use = {
        "days": len(equity_curve),
        "max_positions_allowed": args.max_positions,
        "avg_slots_filled": round(
            sum(p["open_positions"] for p in equity_curve) / ndays, 3),
        "pct_days_book_full": round(
            100 * sum(1 for p in equity_curve
                      if p["open_positions"] >= args.max_positions) / ndays, 2),
        "pct_days_any_position": round(
            100 * sum(1 for p in equity_curve if p["open_positions"] > 0) / ndays, 2),
        "pct_equity_deployed_avg": round(
            100 * sum(p["deployed"] / p["equity"] for p in equity_curve
                      if p["equity"] > 0) / ndays, 2),
    }
    # ---- how long trades were held ----------------------------------------
    held = []
    for t in trades:
        if not t.get("exit_date"):
            continue
        a = date.fromisoformat(t["entry_date"])
        b = date.fromisoformat(t["exit_date"])
        held.append(((b - a).days, t))
    held.sort(key=lambda x: -x[0])
    hd = [x[0] for x in held]
    buckets = {"up to 7": 0, "8 to 30": 0, "31 to 90": 0,
               "91 to 180": 0, "over 180": 0}
    for n in hd:
        key = ("up to 7" if n <= 7 else "8 to 30" if n <= 30
               else "31 to 90" if n <= 90 else "91 to 180" if n <= 180
               else "over 180")
        buckets[key] += 1
    over180 = [t for n, t in held if n > 180]
    hold_view = {
        "calendar_days_median": _median(hd),
        "calendar_days_avg": round(_mean(hd), 1) if hd else None,
        "calendar_days_max": hd[0] if hd else None,
        "buckets": buckets,
        "longest_five": [{"ticker": t["ticker"], "days": n, "r": t["r"]}
                         for n, t in held[:5]],
        "over_180_days_count": len(over180),
        "over_180_days_total_r": round(sum(t["r"] for t in over180), 2),
        "all_trades_total_r": round(sum(t["r"] for t in trades), 2),
    }

    # ---- what the daily monitor actually did ------------------------------
    monitor_view = None
    if engine.MONITOR_MODEL["on"]:
        # both counts, because they answer different questions: how many trades
        # ever saw this, and how many times it happened in total.
        flags: dict[str, dict] = {}
        for t in trades:
            for k, v in (t.get("monitor_flags") or {}).items():
                slot = flags.setdefault(k, {"trades": 0, "times": 0})
                slot["trades"] += 1
                slot["times"] += v
        moved = [t for t in trades if t.get("stop_moves", 0) > 0]
        lift_r = []
        for t in moved:
            risk = t["entry"] - t["initial_stop"]
            if risk > 0:
                lift_r.append((t["final_stop"] - t["initial_stop"]) / risk)
        retarget_sales = [e for t in trades for e in t["exits"]
                          if e["reason"] == "runner_target"]
        monitor_view = {
            "on": True,
            "retarget": engine.MONITOR_MODEL["retarget"],
            "reentry_wait_trading_days":
                engine.MONITOR_MODEL["reentry_wait_trading_days"],
            "ledger_days_written": sum(t.get("monitor_days", 0) for t in trades),
            "flags": flags,
            "trades_with_a_stop_move": len(moved),
            "stop_moves_total": sum(t.get("stop_moves", 0) for t in trades),
            "stop_lift_median_r": round(_median(lift_r), 3) if lift_r else None,
            "stop_lift_avg_r": round(_mean(lift_r), 3) if lift_r else None,
            "fires_inside_the_reentry_wait": len(skipped_cooldown),
            "tickers_held_back_by_the_wait":
                len({s["ticker"] for s in skipped_cooldown}),
            "runner_retargets_adopted":
                sum(len(t.get("added_targets") or []) for t in trades),
            "runner_retarget_sales": len(retarget_sales),
            "runner_retarget_proceeds": round(
                sum(e["qty"] * e["price"] for e in retarget_sales), 2),
        }

    slippage_paid = sum(t.get("slippage", 0.0) for t in trades)
    early_armed = sum(1 for t in trades if t.get("early_armed"))
    early_moved = sum(1 for t in trades if t.get("early_moved"))
    summary = {
        "tickers": tickers, "seed": args.seed, "slot_rank": args.rank,
        # RESEARCH ONLY. Two result files that differ on this are answering
        # two different questions and must never be pooled.
        "allow_grade_d_research": bool(args.allow_grade_d_research),
        "rr_min": args.rr_min,
        "drop_rr_criterion": bool(args.drop_rr_criterion),
        "entry_filters": bool(args.entry_filters),
        "early_trail": ({"arm_atr": args.early_arm_atr,
                         "trail_atr": args.early_trail_atr,
                         "anchor": args.early_anchor}
                        if args.early_arm_atr else None),
        "trades_early_armed": early_armed, "trades_early_moved": early_moved,
        "stop_mode": {"basis": args.stop_basis, "fill": args.stop_fill,
                      "emergency_atr": args.emergency_atr},
        "entry_mode": {"fill": args.entry_fill},
        "costs": dict(engine.COSTS),
        "cost_tally": {**engine.COST_TALLY,
                       "commission": round(engine.COST_TALLY["commission"], 2),
                       "slippage": round(engine.COST_TALLY["slippage"], 2)},
        "commission_paid_total": round(sum(t.get("fees", 0.0) for t in trades), 2),
        "slippage_paid_total": round(slippage_paid, 2),
        "costs_paid_total": round(sum(t.get("fees", 0.0) for t in trades)
                                  + slippage_paid, 2),
        "fills_total": sum(t.get("fills", 0) for t in trades),
        "rubric_mode": dict(engine.RUBRIC_MODE),
        "monitor": monitor_view,
        "holding_period": hold_view,
        "grades_at_build": grades_at_build,
        "grades_at_fire": grades_at_fire,
        "portfolio_model": portfolio_view,
        "slot_use": slot_use,
        "cash_capped_count": len(cash_capped),
        "skipped_no_sleeve_cash_count": len(skipped_no_cash),
        "skipped_leverage_cap_count": len(skipped_leverage),
        "entry_slip_stats": engine.slip_stats(entry_slip),
        "window": [calendar[0], calendar[-1]],
        "start_equity": START_EQUITY, "final_equity": round(final_equity, 2),
        "return_pct": round((final_equity / START_EQUITY - 1) * 100, 2),
        "spy_buy_hold_return_pct": round((spy_bh / START_EQUITY - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "trades": len(trades), "wins": len(wins),
        "win_rate_pct": round(100 * len(wins) / len(rs), 1) if rs else None,
        "total_r": round(sum(rs), 2),
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "runner_pnl_total": round(sum(t["runner_pnl"] for t in trades), 2),
        "target_pnl_total": round(sum(t.get("gross_pnl", t["pnl"])
                                      - t["runner_pnl"] for t in trades), 2),
        "max_positions_seen": max_open,
        "blocked_at_build": blocked_at_build,
        "blocked_at_fire_count": len(blocked_at_fire),
        "skipped_capacity_count": len(skipped_capacity),
        "skipped_sizing_count": len(skipped_sizing),
        "skipped_reentry_wait_count": len(skipped_cooldown),
        "tickers_missing_earnings_history": no_earnings,
    }

    out = {"summary": summary, "per_ticker": per_ticker, "trades": trades,
           "entry_slip": entry_slip, "cash_capped": cash_capped,
           "skipped_no_sleeve_cash": skipped_no_cash,
           "skipped_leverage_cap": skipped_leverage,
           "skipped_reentry_wait": skipped_cooldown,
           "blocked_at_fire": blocked_at_fire,
           "skipped_capacity": skipped_capacity,
           "equity_curve": equity_curve}
    suffix = "_filtered" if args.entry_filters else ""
    if args.entry_filters and args.skip_sma_filter:
        suffix = "_filtered_nosma"
    if args.entry_filters and args.skip_grade_filter:
        suffix += "_allowc"
    if args.entry_fill == "next_open":
        suffix += "_nextopen"
    if args.costs:
        suffix += "_costs" + ("%g" % args.cost_multiplier) + "x"
    if args.drop_event_criterion:
        suffix += "_noevent"
    if args.portfolio_model:
        suffix += "_realacct"
    if args.monitor:
        suffix += "_monitor" + ("retarget" if args.monitor_retarget else "")
    if args.max_entry_leverage is not None:
        suffix += "_lev" + ("%g" % args.max_entry_leverage).replace(".", "p")
    if args.tag:
        suffix += "_" + args.tag
    out_path = ROOT / f"results_portfolio_{args.seed}_{args.rank}{suffix}.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(f"\nfull detail -> {out_path}")


if __name__ == "__main__":
    main()
