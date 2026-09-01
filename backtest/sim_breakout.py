"""One-ticker breakout backtest that replays the LIVE system's own code
day by day -- setup_classifier, level_picker, rubric_formula, regime_formula,
decision_policy -- over stored Yahoo bars. Nothing re-implements a trading
rule; this file only supplies the daily loop, the fills, and the accounting.

Pre-registered mechanics (agreed with the owner 2026-08-11, before running):
  * $100,000 start, risk = 1% of CURRENT equity per trade (compounding).
  * Breakout setups only. Every other classification is ignored.
  * One position at a time. Re-entry only after a full exit.
  * A plan is built from data through day t. It can only fire on day t+1 or
    later: trigger = settled daily close above the plan's trigger price.
    Entry at the fire day's CLOSE (owner: "enter after the market close").
  * Full live gates, both at build and at fire (rule 27 re-grade at the real
    entry price): qualifying target required, grade D blocks, risk_off /
    structure_break regime blocks. Blocked-at-fire trades are ALSO simulated
    as shadow trades (no money) so the gates' cost/savings is visible.
  * Exits: tranche 1 / tranche 2 at targets (40/35/25 or 40/60 runner),
    runner until stopped. Stop checked before targets each day; gap through
    a level fills at the open. Stop trails only after target 1 (live rule).
  * No lookahead: every computation sees bars[0..t] only. Swing pivots need
    3 later bars to confirm, exactly as the live scanner sees them.
  * Position force-closed at the last bar's close if still open.

Usage: python backtest/sim_breakout.py [TICKER] [--years 5]
Output: printed summary + backtest/results_<TICKER>_breakout.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "bot"))

import decision_policy
import fetch_analysis_data as fad
import indicators_core as ic
import level_picker
import regime_formula
import rubric_formula
import setup_classifier
import setup_types

DATA = ROOT / "data"
START_EQUITY = 100_000.0
RISK_PCT = 0.01

# Optional EARLY trail, off by default (pre-registered 2026-08-13, owner's own
# variant: "reach profit of 1.5 ATR and above, then trigger a 1.2 ATR trail").
# arm_atr  -- profit above entry, in build-time ATRs, that turns the trail on
# trail_atr-- distance below the running peak the stop is then parked at
# anchor   -- "close" (peak of daily closes) or "high" (peak of daily highs)
# Nothing here runs unless a caller sets arm_atr; the live rule is untouched.
EARLY_TRAIL = {"arm_atr": None, "trail_atr": None, "anchor": "close"}

# Optional CLOSE-ONLY stop, off by default (pre-registered 2026-08-19, see
# PREREGISTRATION_CLOSE_STOP.md). The live rule stops out the instant price
# touches the stop at any point in the day, while an ENTRY needs a settled
# daily close -- an asymmetry that showed up live on 2026-08-18 when two of
# five stops fired on bars that closed back above the stop.
# basis -- "intraday" (live rule: low<=stop, and a gap-down fills at the open)
#          or "close" (only a settled close<=stop counts; gap-downs are held)
# fill  -- when basis is "close": "close" sells at that close, "next_open"
#          sells at the following day's open (what the live workflow can
#          actually achieve). Ignored while basis is "intraday".
# emergency_atr -- optional SECOND stop (pre-registered 2026-08-19, owner's
#          idea, see PREREGISTRATION_TWO_STOPS.md), parked this many build-time
#          ATRs BELOW the normal stop and fired the way stops fire today: on
#          an intraday touch, and at the open on a gap below. It rides under
#          the normal stop, so it trails up whenever the normal stop does.
#          Only meaningful while basis is "close" -- with an intraday basis the
#          normal stop is always hit first. None = no emergency stop.
STOP_MODE = {"basis": "intraday", "fill": "close", "emergency_atr": None}

# Optional ENTRY TIMING switch, off by default (added 2026-08-19). The live
# rule buys at the CLOSE of the day whose settled close cleared the trigger.
# fill -- "signal_close" (live rule: buy at that day's close) or "next_open"
#         (buy at the OPEN of the following bar). Nothing else moves: the
#         trigger test, the rule-27 re-grade, the gates, the entry filters and
#         the slot ranking are all still decided on the signal day's close,
#         because that is all a trader knows at that moment. Only the fill
#         price -- and therefore the share count and the R denominator, which
#         are both computed off the real fill -- move to the next open.
ENTRY_MODE = {"fill": "signal_close"}

# Optional TRADING COSTS, off by default (added 2026-08-20). Same convention as
# ENTRY_MODE: nothing here bites while "on" is False, so the live rule and every
# earlier result are unchanged.
#   per_share       -- commission per share (Interactive Brokers US tiered)
#   min_per_order   -- the per-order floor that same schedule charges
#   max_pct_of_trade-- IB's cap: a commission never exceeds this share of the
#                      order's dollar value
#   slippage_bps    -- basis points paid per SIDE, on top of commission, to
#                      model the spread and the fact that a stop or a market
#                      order does not fill at its exact level
# Commission is charged on the entry and on EVERY tranche exit separately --
# each partial sale is its own order and pays its own minimum.
COSTS = {
    "on": False,
    "per_share": 0.0035,
    "min_per_order": 0.35,
    "max_pct_of_trade": 0.01,
    "slippage_bps": 0.0,
}
COST_TALLY = {"fills": 0, "commission": 0.0, "slippage": 0.0, "cap_binds": 0}

# DIAGNOSTIC ONLY, off by default (added 2026-08-20). Not a proposed rule.
# The rubric scores five criteria; the fifth ("event") needs a known next
# earnings date, and 326 of the 503 tickers have no earnings history stored, so
# that criterion is automatically False for them and their grade is capped at B
# for a reason that has nothing to do with the setup. Turning this on scores the
# other four only, with the cutoffs moved down by one so an unchanged setup that
# DID pass the event criterion keeps exactly the grade it had. It exists to
# measure how much the missing data costs. It changes no live rule.
RUBRIC_MODE = {"drop_event": False}

# The real account, off by default (added 2026-08-26). Every run before this one
# assumed the whole account was tradeable, which is not how the owner's money is
# arranged. When this is on:
#   * core_pct of the starting money buys SPY and QQQ ONCE, on the first bar, in
#     a fixed split, and is then left alone to the last bar. Never sold, never
#     trimmed, never rebalanced -- not between SPY and QQQ and not against the
#     sleeve -- and never used to pay for a trade.
#   * the sleeve is the rest, and is the only money a trade can be bought with.
#   * risk per trade is 1% of the CURRENT SLEEVE, worked out fresh at each
#     entry. The core is not counted. Sleeve equity is free sleeve cash plus
#     whatever the open trades are worth at that bar's close, so the size does
#     not fall merely because money is already at work.
#   * a trade that the free sleeve cash cannot pay for in full is turned away
#     whole, not quietly made smaller.
# Dividends are NOT included, which understates the core.
PORTFOLIO_MODEL = {
    "on": False,
    "core_pct": 0.60,          # share of the starting money held passively
    "core_spy_pct": 0.60,      # of the core
    "core_qqq_pct": 0.40,      # of the core
    "rebalance": "never",
    "risk_basis": "sleeve",    # 1% of the sleeve, core excluded
}
# Optional SETUP SCOPE, off by default (added 2026-08-27, owner's request).
# The pre-registration restricted this file to Breakout, and every earlier
# result on disk was produced that way. With "all" the plan builder accepts any
# of the six setups setup_classifier already returns, and rule 15's relative-
# strength window follows the setup type -- 5 trading days for the three
# reversal shapes, 20 for the trend-following ones -- exactly as build_plan.py
# picks it live. Nothing else moves.
SETUPS = {"allow": (setup_classifier.BREAKOUT,)}

# Optional PRE-TARGET-1 STRUCTURAL TRAIL, off by default (added 2026-08-27,
# owner's request). The live rule does not move a stop before the first target
# is sold at, and level_picker.trail_stop enforces that itself through its
# past_target_1 argument -- twelve pre-registered early-trail variants were
# tested here and all twelve lost money. With this on, the SAME function runs
# every day from entry, with the same arithmetic (the highest structural low
# that still clears 0.7x the build-time ATR from today's close, parked 0.15x
# ATR under it, never moving down). It is not the ATR-peak trail EARLY_TRAIL
# implements; it is the structural higher-low trail, simply started earlier.
PRE_T1_TRAIL = {"on": False}

# Optional ALL-IN SIZING, off by default (added 2026-08-27, owner's request).
# The live rule sizes by risk: shares = 1% of equity / (entry - stop), which on
# a 4%-wide stop puts roughly a quarter of the account into one name. With
# "all_in" the position is instead every share the free cash can pay for, so a
# stop-out costs the FULL stop distance of the whole account rather than 1%.
# Nothing else moves -- same triggers, same stops, same targets, same trail.
# This is a measuring tool, not a proposed rule.
SIZING = {"mode": "risk"}

# The DAILY MONITOR, off by default (added 2026-08-27, see MONITOR_GAP.md).
# Until now the simulation entered on the live rules and then held blind: for an
# open position it checked the stop, checked the targets, and moved the stop by
# the post-target-1 trail. Nothing re-ran the daily playbook.
#
# MONITOR_GAP.md phase 1 read every rule that applies to a HELD position -- from
# STRATEGY_v3.md, which is the document that actually governs one (MONITOR_v2.md
# says in its own opening lines that its job ends at the fill) -- and tagged each
# one MECHANICAL or JUDGMENT. Thirteen of the sixteen fully mechanical rules were
# already in the engine. This switch adds the rest of the mechanical ones and
# NOTHING else. No rule is invented here; where STRATEGY_v3.md does not define a
# behaviour, the simulation records the situation and does not act on it.
#
# What turning this on changes:
#   * every open position keeps a per-day ledger -- date, stop, what changed,
#     which rule fired -- so a run can be audited bar by bar
#   * the daily picture is rebuilt for a HELD ticker too (the setup call and the
#     wall scan, on bars through today only) and written into that ledger
#   * a runner that has sold at every stored target gets a fresh target scan,
#     the same one the scanner runs, measured from today's price
#   * after a real stop-out the ticker cannot be re-entered the same day or the
#     next trading day (STRATEGY_v3.md's re-entry wait). The rule's escape hatch
#     -- "unless there is clear technical confirmation" -- is a judgment call
#     with no definition anywhere, so the wait here is unconditional
#   * a stop lifted onto a low that predates the entry is flagged and the move
#     still applies here. KNOWN DIVERGENCE as of 2026-08-30: live
#     (bot/trail_stop.py) now HOLDS the stop in that case and names the level
#     instead of raising to it. The sim is deliberately not changed in the same
#     pass -- aligning the simulator to the live trail is its own step, with its
#     own before/after measurement, and a silent change here would quietly
#     re-price every historical result at the same time
#
# retarget -- OFF even when the monitor is on, and deliberately separate. Finding
#   a fresh runner target is fully mechanical. Deciding how many shares to sell
#   at it is NOT: rule 7's 40%/60% split is written for a position being opened,
#   and nothing says whether the 40% is of the original size or of the runner
#   that is left. Turning this on adopts the one reading that can be defended
#   (40% of what is still held) purely so the effect can be measured. It is not
#   a proposed rule, and it stays off in every reported baseline.
MONITOR_MODEL = {
    "on": False,
    "retarget": False,
    "reentry_wait_trading_days": 1,   # the day of the stop-out, plus this many
}

_NO_EVENT_CRITERIA = ("rr", "target_atr", "rs", "sma20_extension")
_NO_EVENT_CUTOFFS = ((4, "A"), (3, "B"), (2, "C"))


# RESEARCH ONLY (2026-08-31). The R:R minimum the rubric scores against, and
# whether that criterion is scored at all. Live is rubric_formula.RR_MIN (2.3)
# with the criterion on; every other setting exists to price the arms written
# down in PREREGISTRATION_RR_THRESHOLD.md.
#
# Why it needed testing: across 608 trades, LOSERS passed this criterion more
# often than winners did (95.6% against 88.4%), the only one of the five whose
# winner/loser gap exceeds its own margin. A far target pays more when reached
# and is reached much less often, and on this sample the exchange loses.
RR_RULE = {"min": None, "drop": False}


def _rr_scored(res, rr_value: float) -> str:
    """The letter under whichever R:R rule this run is testing.

    Rebuilt from the criteria rather than re-running classify_rubric, so the
    other four criteria are byte-identical to the live scoring and only the one
    being tested can move -- the same discipline the trail experiment had to
    learn after its first run measured two changes at once and was voided."""
    crits = dict(res.criteria)
    scored = ["rr", "target_atr", "rs", "sma20_extension", "event"]
    if RR_RULE["drop"]:
        scored.remove("rr")
    elif RR_RULE["min"] is not None:
        crits["rr"] = rr_value >= RR_RULE["min"]
    score = sum(1 for k in scored if crits[k])
    top = len(scored)
    # Cutoffs keep their meaning: all of them is A, one miss is B, two is C.
    if score >= top:
        return "A"
    if score >= top - 1:
        return "B"
    if score >= top - 2:
        return "C"
    return "D"


def graded(res) -> str:
    """The rubric grade, or the four-criterion diagnostic grade."""
    if not RUBRIC_MODE["drop_event"]:
        return res.grade
    score = sum(1 for k in _NO_EVENT_CRITERIA if res.criteria[k])
    for cut, letter in _NO_EVENT_CUTOFFS:
        if score >= cut:
            return letter
    return "D"


def is_defensive_exit(reason: str) -> bool:
    """Did this exit happen because the trade went wrong, rather than because a
    planned target was reached? STRATEGY_v3's re-entry wait is written for "a
    stop or a defensive exit where there was real exposure". A target sale is
    neither, and the force-close at the end of the data is not an exit the
    rules know about at all."""
    return reason.startswith("stop") or reason.startswith("emergency")


def slip_buy(price: float) -> float:
    """Effective buy price after slippage."""
    if not COSTS["on"]:
        return price
    return price * (1.0 + COSTS["slippage_bps"] / 10_000.0)


def slip_sell(price: float) -> float:
    """Effective sell price after slippage."""
    if not COSTS["on"]:
        return price
    return price * (1.0 - COSTS["slippage_bps"] / 10_000.0)


def commission(qty: int, price: float) -> float:
    """IB tiered: per share, with a per-order floor and a 1%-of-value cap."""
    if not COSTS["on"] or qty <= 0:
        return 0.0
    raw = max(COSTS["min_per_order"], COSTS["per_share"] * qty)
    cap = COSTS["max_pct_of_trade"] * qty * price
    if cap < raw:
        COST_TALLY["cap_binds"] += 1
        raw = cap
    COST_TALLY["fills"] += 1
    COST_TALLY["commission"] += raw
    return raw
# Plain-word names for the ledger, so a day's row says which rule fired without
# a rule number in it. Only read while MONITOR_MODEL is on.
_SELL_RULE = {
    "stop": "stopped_out",
    "stop_gap": "stopped_out_on_a_gap",
    "stop_close": "stopped_out_on_the_close",
    "stop_close_next_open": "stopped_out_at_the_next_open",
    "emergency": "emergency_stop",
    "emergency_gap": "emergency_stop_on_a_gap",
    "target_1": "target_1_sold",
    "target_2": "target_2_sold",
    "runner_target": "added_runner_target_sold",
    "end_of_data": "force_closed_at_the_end_of_the_data",
}

MARKET_BUFFER_ATR = 0.15    # rule 24's buffer: never park a stop at/above price
REGIME_LOOKBACK_BARS = 126          # same as fetch_analysis_data.py
INDEX_WINDOW_BARS = 252             # live fetches ~1 year of SPY/QQQ


# --------------------------------------------------------------------------
# data loading
# --------------------------------------------------------------------------

def load_bars(ticker: str) -> list[dict]:
    raw = json.loads((DATA / "bars" / f"{ticker}.json").read_text())
    # live bar shape: the classifier/level code reads open/high/low/close/
    # volume/date -- our stored rows already match. Add `time` for the wall
    # scanner, which dates pivots via fromtimestamp.
    out = []
    for b in raw["bars"]:
        y, m, d = (int(x) for x in b["date"].split("-"))
        from datetime import datetime, timezone
        ts = datetime(y, m, d, tzinfo=timezone.utc).timestamp()
        out.append({**b, "time": ts})
    return out


def load_earnings(ticker: str) -> list[date]:
    path = DATA / "earnings.csv"
    if not path.exists():
        return []
    with path.open() as fh:
        return sorted(date.fromisoformat(r["earnings_date"])
                      for r in csv.DictReader(fh) if r["ticker"] == ticker)


def earnings_days_out(earnings: list[date], today: date) -> int | None:
    for e in earnings:
        if e >= today:
            return (e - today).days
    return None                       # beyond known filings -> unverified


# --------------------------------------------------------------------------
# per-day system snapshot (bars[0..t] only)
# --------------------------------------------------------------------------

def index_snapshot(bars: list[dict]) -> regime_formula.IndexSnapshot:
    c = [b["close"] for b in bars]
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    return regime_formula.IndexSnapshot(
        price=c[-1], sma20=ic.sma(c, 20), sma50=ic.sma(c, 50), sma150=ic.sma(c, 150),
        swing_highs=regime_formula.find_swing_highs(h),
        swing_lows=regime_formula.find_swing_lows(l),
        lookback_low=min(l[-REGIME_LOOKBACK_BARS:]),
    )


class Day:
    """Everything the live system would have computed at day t's close."""

    def __init__(self, bars, i, spy_bars, qqq_bars, spy_ix, qqq_ix, earnings,
                 regime: str | None = None):
        upto = bars[:i + 1]
        self.date = date.fromisoformat(bars[i]["date"])
        self.bar = bars[i]
        h = [b["high"] for b in upto]
        l = [b["low"] for b in upto]
        c = [b["close"] for b in upto]
        self.close = c[-1]
        self.atr14 = ic.atr_wilder(h, l, c, period=14)
        self.sma20 = ic.sma(c, 20)
        self.sma50 = ic.sma(c, 50)
        self.dist_sma20_atr = (self.close - self.sma20) / self.atr14 if self.atr14 else None

        self.recent40 = upto[-40:]
        highs_above = fad._swing_highs(upto, self.close)
        self.wall_chains = fad._chain_walls(highs_above, self.atr14)
        lows = fad._swing_lows(upto)
        self.swing_lows = [{"price": p, "date": d} for d, p in lows[-10:]]

        # indices aligned by date -- live fetches ~1y of each index. A caller
        # running many tickers over one calendar may pass the day's regime in,
        # since it is identical for every ticker on the same date.
        si, qi = spy_ix[self.date.isoformat()], qqq_ix[self.date.isoformat()]
        spy_win = spy_bars[max(0, si - INDEX_WINDOW_BARS + 1):si + 1]
        if regime is not None:
            self.regime = regime
        else:
            qqq_win = qqq_bars[max(0, qi - INDEX_WINDOW_BARS + 1):qi + 1]
            self.regime = regime_formula.classify_regime(
                index_snapshot(spy_win), index_snapshot(qqq_win)).regime

        spy_c = [b["close"] for b in spy_win]
        n = min(len(c), len(spy_c))
        self.rs20 = (ic.relative_strength(c[-n:], spy_c[-n:], 20).rs_delta_pct
                     if n >= 21 else None)
        # rule 15: the reversal shapes are scored on 5 trading days instead,
        # because a 20-day window still carries the fall they are recovering
        # from. Computed always, used only when the setup is a reversal.
        self.rs5 = (ic.relative_strength(c[-n:], spy_c[-n:], 5).rs_delta_pct
                    if n >= 6 else None)
        self.earnings_days_out = earnings_days_out(earnings, self.date)


# --------------------------------------------------------------------------
# plan building + grading (live chain: classify -> stop -> targets -> grade)
# --------------------------------------------------------------------------

def rs_for(setup_type: str | None, day: Day) -> float | None:
    """Rule 15's window, picked the same way build_plan.py picks it live."""
    if setup_types.rs_window_days(setup_type) == 5:
        return day.rs5
    return day.rs20


def build_plan(day: Day) -> dict | None:
    """Returns a pending breakout plan, or None. Mirrors build_plan.py's
    mechanical chain, restricted to Breakout per the pre-registration."""
    call = setup_classifier.classify(
        bars=day.recent40, atr14=day.atr14, sma20=day.sma20, sma50=day.sma50,
        wall_chains=day.wall_chains, swing_lows=day.swing_lows)
    if call.setup_type not in SETUPS["allow"] or call.trigger is None:
        return None

    stop = level_picker.pick_stop(call.trigger, day.atr14, day.swing_lows,
                                  current_price=day.close, bars=day.recent40)
    if stop.stop is None:
        return {"blocked": "no_honest_stop", "trigger": call.trigger,
                "setup": call.setup_type}

    scan = level_picker.pick_targets(call.trigger, stop.stop, day.atr14,
                                     day.wall_chains, bars=day.recent40,
                                     swing_lows=day.swing_lows)
    if not scan.targets:
        return {"blocked": "no_qualifying_target", "trigger": call.trigger,
                "setup": call.setup_type, "stop": stop.stop}

    t1 = scan.targets[0]
    rs_delta = rs_for(call.setup_type, day)
    _rubric = rubric_formula.classify_rubric(rubric_formula.RubricInputs(
        rr=t1.rr, target_atr_multiple=t1.atr_mult, regime=day.regime,
        rs_delta_pct=rs_delta if rs_delta is not None else 0.0,
        dist_sma20_atr=day.dist_sma20_atr,
        earnings_days_out=day.earnings_days_out))
    grade = (_rr_scored(_rubric, t1.rr)
             if (RR_RULE["min"] is not None or RR_RULE["drop"]) else graded(_rubric))

    ceiling = decision_policy.max_allowed_decision(
        has_target=True, grade=grade, regime=day.regime)
    plan = {
        "built": day.date.isoformat(), "trigger": call.trigger,
        "setup": call.setup_type,
        "stop": stop.stop, "stop_basis": stop.basis_kind,
        "targets": [{"price": t.price, "pct": t.pct} for t in scan.targets],
        "runner_pct": scan.runner_pct, "atr_at_build": day.atr14,
        "grade": grade, "confidence": call.confidence,
        # Kept per plan so every trade can be asked WHICH criterion decided its
        # letter, not just what the letter was. The suspect worth naming: R:R
        # >= 2.3 pushes the target further out, and a further target is reached
        # less often -- so the criterion meant to demand quality may be buying
        # it with hit rate.
        "rubric_criteria": dict(_rubric.criteria),
        "rubric_score": _rubric.score,
    }
    if ceiling not in decision_policy.BUY_DECISIONS:
        plan["blocked"] = ("regime_" + day.regime
                           if day.regime in decision_policy.BLOCKING_REGIMES
                           else "grade_" + str(grade))
    return plan


def regrade_at_fire(plan: dict, day: Day) -> tuple[str, float]:
    """Rule 27: score the trade actually on offer -- entry at today's close."""
    entry = day.close
    risk = entry - plan["stop"]
    t1 = plan["targets"][0]["price"]
    reward = max(0.0, t1 - entry)
    rr = reward / risk if risk > 0 else 0.0
    rs_delta = rs_for(plan.get("setup"), day)
    _res = rubric_formula.classify_rubric(rubric_formula.RubricInputs(
        rr=rr, target_atr_multiple=(reward / day.atr14) if day.atr14 else 0.0,
        regime=day.regime,
        rs_delta_pct=rs_delta if rs_delta is not None else 0.0,
        dist_sma20_atr=day.dist_sma20_atr,
        earnings_days_out=day.earnings_days_out))
    grade = (_rr_scored(_res, rr)
             if (RR_RULE["min"] is not None or RR_RULE["drop"]) else graded(_res))
    plan["grade_at_fire"] = grade
    plan["rr_at_fire"] = rr
    return grade, rr


# --------------------------------------------------------------------------
# position lifecycle
# --------------------------------------------------------------------------

class Position:
    def __init__(self, plan, entry, qty, entry_date, shadow=False,
                 raw_entry=None):
        # `entry` is the price actually paid -- the caller has already applied
        # any buy-side slippage, so sizing and this fill agree. `raw_entry` is
        # the untouched market price, kept only so slippage can be reported.
        self.plan = plan
        self.entry = entry
        self.entry_fee = commission(qty, entry)
        self.slippage_usd = qty * (entry - (entry if raw_entry is None
                                            else raw_entry))
        COST_TALLY["slippage"] += self.slippage_usd
        self.qty = qty
        self.remaining = qty
        self.stop = plan["stop"]
        self.initial_stop = plan["stop"]
        self.entry_date = entry_date
        self.shadow = shadow
        self.exits = []                       # {date, reason, qty, price}
        # rule 7 tranche sizes; runner absorbs rounding. The live system builds
        # this in ONE place -- indicators_core.build_tranche_plan -- and hands
        # the same numbers to every report, precisely so a second copy of the
        # split cannot drift away from the first. This used to be a second copy
        # (a local round() loop). Same arithmetic, one owner (2026-08-27).
        tranche_plan = ic.build_tranche_plan(qty, plan["targets"], [])
        self.tranche_qty = [t.planned_qty for t in tranche_plan.tranches
                            if t.label.startswith("target_")]
        self.runner_qty = next((t.planned_qty for t in tranche_plan.tranches
                                if t.label == "runner"), 0)
        self.filled_targets = set()
        # targets the monitor added after entry (empty unless MONITOR_MODEL is
        # on with retarget). Kept apart from plan["targets"] so a trade record
        # always shows what was planned at entry against what was added later.
        self.added_targets = []
        # the per-day ledger, written only while MONITOR_MODEL is on
        self.ledger = []
        self._actions = []
        self.monitor_flags = {}
        # early-trail bookkeeping (inert while EARLY_TRAIL["arm_atr"] is None)
        self.peak = entry
        self.early_armed = False
        self.early_moved = False
        # close-only stop bookkeeping (inert while STOP_MODE basis is intraday)
        self.stop_close_pending = False

    def past_target_1(self) -> bool:
        return 1 in self.filled_targets

    def _sell(self, day_date, reason, qty, price):
        qty = min(qty, self.remaining)
        if qty <= 0:
            return
        fill = slip_sell(price)
        self.slippage_usd += qty * (price - fill)
        COST_TALLY["slippage"] += qty * (price - fill)
        self.exits.append({"date": day_date.isoformat(), "reason": reason,
                           "qty": qty, "price": fill, "level": price,
                           "fee": commission(qty, fill)})
        self.remaining -= qty
        if MONITOR_MODEL["on"]:
            self._actions.append({"rule": _SELL_RULE.get(reason, "sold"),
                                  "what": reason, "qty": qty,
                                  "price": round(fill, 4)})

    def process_day(self, day: Day) -> bool:
        """Apply one bar. Returns True when fully closed.

        With MONITOR_MODEL off this is exactly what it always was. With it on,
        the day runs in the order the rebuild plan asks for: the stop and target
        checks first, then the picture is rebuilt from bars through today, then
        the remaining mechanical rules are applied, and the whole day is written
        into the position's ledger. The stop can still only ever move up.
        """
        if not MONITOR_MODEL["on"]:
            return self._process_day_core(day)
        stop_before = self.stop
        self._actions = []
        closed = self._process_day_core(day)             # steps 1 and 4
        picture = self._rebuild_picture(day)             # step 2
        if not closed:
            self._apply_monitor_rules(day, picture)      # step 3
        self._log_day(day, stop_before, closed, picture)
        return closed

    # -- the daily monitor (inert unless MONITOR_MODEL["on"]) ---------------

    def _rebuild_picture(self, day: Day) -> dict:
        """The playbook update: the same modules the scanner runs, on bars
        through today only. Nothing here decides anything -- it is the picture
        the mechanical rules below are then applied to, and it is recorded so a
        reader can see what the system saw on the day it acted."""
        call = setup_classifier.classify(
            bars=day.recent40, atr14=day.atr14, sma20=day.sma20,
            sma50=day.sma50, wall_chains=day.wall_chains,
            swing_lows=day.swing_lows)
        return {
            "regime": day.regime,
            "setup_now": call.setup_type,
            "confidence": call.confidence,
            "walls_above": len(day.wall_chains),
            "close": day.close,
        }

    def _apply_monitor_rules(self, day: Day, picture: dict) -> None:
        """Only the MECHANICAL rules MONITOR_GAP.md found missing. Anything the
        open-position guide leaves undefined is recorded, never acted on."""
        atr_build = self.plan.get("atr_at_build") or 0.0

        # A6 -- the low behind a stop lifted today may predate the trade. Live
        # HOLDS the stop in this case as of 2026-08-30; this sim still moves it
        # and records the fact (see the KNOWN DIVERGENCE note in the module
        # header). Counted so the run can be asked how often it mattered, which
        # is also how the size of that divergence gets measured.
        for act in self._actions:
            if act["what"] == "trail" and act.get("basis_date"):
                if act["basis_date"] < self.entry_date.isoformat():
                    act["rule"] = "stop_basis_predates_entry"
                    self.monitor_flags["stop_basis_predates_entry"] = \
                        self.monitor_flags.get("stop_basis_predates_entry", 0) + 1

        # B5 -- price closed within half a build-time ATR of a target that has
        # not been sold at yet. The rules call this a "you are close, decide"
        # heads-up and define no action, so this is a note and nothing else.
        if atr_build > 0:
            for n, t in enumerate(self.plan["targets"], start=1):
                if n in self.filled_targets:
                    continue
                if 0 <= t["price"] - day.close <= 0.5 * atr_build:
                    self._actions.append({"rule": "near_target_heads_up",
                                          "what": "near_target",
                                          "price": round(t["price"], 4)})
                    self.monitor_flags["near_target_heads_up"] = \
                        self.monitor_flags.get("near_target_heads_up", 0) + 1
                    break

        # B6 -- the runner has sold at every stored target and is running with
        # no level left to aim at. STRATEGY_v3.md says to look for a new one,
        # and that it must clear the same gates a fresh entry's target clears,
        # measured from TODAY's price and today's ATR (the frozen ATR is for
        # judging an existing stop, not for gating a brand new level).
        stored_all_filled = (self.plan["targets"]
                             and len(self.filled_targets) >= len(self.plan["targets"]))
        if stored_all_filled and self.remaining > 0 and not self.added_targets:
            scan = level_picker.pick_targets(
                day.close, self.stop, day.atr14, day.wall_chains,
                bars=day.recent40, swing_lows=day.swing_lows)
            if scan.targets:
                found = scan.targets[0]
                self.monitor_flags["runner_retarget_found"] = \
                    self.monitor_flags.get("runner_retarget_found", 0) + 1
                act = {"rule": "runner_retarget_found", "what": "retarget",
                       "price": round(found.price, 4),
                       "atr_mult": round(found.atr_mult, 3),
                       "rr": round(found.rr, 3), "adopted": False}
                if MONITOR_MODEL["retarget"]:
                    # rule 7's single-target split, read as 40% of what is still
                    # held. That reading is NOT written down anywhere -- see
                    # MONITOR_MODEL's docstring. Off by default for that reason.
                    qty = int(round(self.remaining * 0.40))
                    if qty > 0:
                        self.added_targets.append(
                            {"price": found.price, "qty": qty, "filled": False,
                             "added": day.date.isoformat()})
                        act["adopted"] = True
                        act["qty"] = qty
                self._actions.append(act)

    def _log_day(self, day: Day, stop_before: float, closed: bool,
                 picture: dict) -> None:
        self.ledger.append({
            "date": day.date.isoformat(),
            "close": round(day.close, 4),
            "stop": round(self.stop, 4),
            "stop_moved": round(self.stop - stop_before, 4)
                          if self.stop != stop_before else 0.0,
            "shares": self.remaining,
            "regime": picture["regime"],
            "setup_now": picture["setup_now"],
            "actions": list(self._actions),
            "closed": closed,
        })

    def _process_day_core(self, day: Day) -> bool:
        """Apply one bar. Returns True when fully closed."""
        bar = day.bar
        close_basis = STOP_MODE["basis"] == "close"
        # a close-only stop decided on yesterday's close sells into today's open
        if self.stop_close_pending:
            self._sell(day.date, "stop_close_next_open", self.remaining,
                       bar["open"])
            return True
        if not close_basis:
            # live rule -- stop first, gap below fills at the open
            if bar["open"] <= self.stop:
                self._sell(day.date, "stop_gap", self.remaining, bar["open"])
                return True
            if bar["low"] <= self.stop:
                self._sell(day.date, "stop", self.remaining, self.stop)
                return True
        else:
            # OPTIONAL emergency stop: a hard floor under the close-only stop,
            # fired intraday exactly the way the live stop fires. Recomputed
            # each day off self.stop, so it trails up with the normal stop.
            emerg_atr = STOP_MODE.get("emergency_atr")
            atr = self.plan["atr_at_build"] or 0.0
            if emerg_atr and atr > 0:
                floor = self.stop - emerg_atr * atr
                if bar["open"] <= floor:
                    self._sell(day.date, "emergency_gap", self.remaining,
                               bar["open"])
                    return True
                if bar["low"] <= floor:
                    self._sell(day.date, "emergency", self.remaining, floor)
                    return True
        # targets, in order; gap above a target fills at the open
        for n, t in enumerate(self.plan["targets"], start=1):
            if n in self.filled_targets or n - 1 >= len(self.tranche_qty):
                continue
            if bar["high"] >= t["price"]:
                fill = bar["open"] if bar["open"] > t["price"] else t["price"]
                self._sell(day.date, f"target_{n}", self.tranche_qty[n - 1], fill)
                self.filled_targets.add(n)
        # a target the monitor added after entry, if any (empty unless
        # MONITOR_MODEL is on with retarget). Same fill rule as any other.
        for at in self.added_targets:
            if at["filled"] or bar["high"] < at["price"]:
                continue
            fill = bar["open"] if bar["open"] > at["price"] else at["price"]
            self._sell(day.date, "runner_target", at["qty"], fill)
            at["filled"] = True
        if self.remaining <= 0:
            return True
        # OPTIONAL close-only stop, judged on the stop as it stood coming INTO
        # today -- deliberately before the trail block below, so a stop the
        # trail raises tonight cannot retroactively stop today out.
        if close_basis and day.close <= self.stop:
            if STOP_MODE["fill"] == "next_open":
                self.stop_close_pending = True
                return False
            self._sell(day.date, "stop_close", self.remaining, day.close)
            return True
        # OPTIONAL early trail, before target 1. Off unless a caller armed it.
        # Same timing as the live trail: decided on today's close, effective
        # tomorrow. Ratchets up only, and is capped a buffer under today's
        # close so the test never parks a stop at or above the market.
        anchor = bar["high"] if EARLY_TRAIL["anchor"] == "high" else day.close
        self.peak = max(self.peak, anchor)   # tracked always, for the diagnosis
        if not self.past_target_1() and EARLY_TRAIL["arm_atr"]:
            atr = self.plan["atr_at_build"] or 0.0
            if atr > 0:
                if self.peak - self.entry >= EARLY_TRAIL["arm_atr"] * atr:
                    self.early_armed = True
                if self.early_armed:
                    cand = min(self.peak - EARLY_TRAIL["trail_atr"] * atr,
                               day.close - MARKET_BUFFER_ATR * atr)
                    if cand > self.stop:
                        self.stop = cand
                        self.early_moved = True
        # trail at day end, effective tomorrow -- live rule: only after T1.
        # PRE_T1_TRAIL runs the identical function from entry instead; the
        # past_target_1 argument is what that switch overrides, nothing else.
        if self.past_target_1() or PRE_T1_TRAIL["on"]:
            trailed = level_picker.trail_stop(
                current_price=day.close, current_stop=self.stop,
                atr_at_build=self.plan["atr_at_build"],
                swing_lows=day.swing_lows, past_target_1=True,
                bars=day.recent40)
            if trailed.moved:
                self.stop = trailed.stop
                if PRE_T1_TRAIL["on"] and not self.past_target_1():
                    self.early_moved = True
                if MONITOR_MODEL["on"]:
                    self._actions.append({
                        "rule": ("stop_trailed_after_target_1"
                                 if self.past_target_1()
                                 else "stop_trailed_before_target_1"),
                        "what": "trail", "price": round(trailed.stop, 4),
                        "basis_level": trailed.basis_level,
                        "basis_date": trailed.basis_date})
        return False

    def force_close(self, day_date, price):
        self._sell(day_date, "end_of_data", self.remaining, price)

    def fees(self) -> float:
        """Every dollar of commission this position paid, entry and exits."""
        return self.entry_fee + sum(e.get("fee", 0.0) for e in self.exits)

    def proceeds(self) -> float:
        """Cash coming back from the exits so far, after their commissions."""
        return sum(e["qty"] * e["price"] - e.get("fee", 0.0) for e in self.exits)

    def pnl(self) -> float:
        gross = sum(e["qty"] * (e["price"] - self.entry) for e in self.exits)
        return gross - self.fees()

    def r_multiple(self) -> float:
        risk = self.qty * (self.entry - self.initial_stop)
        return self.pnl() / risk if risk else 0.0

    def to_record(self) -> dict:
        # A target the monitor added is still a planned sale at a named price,
        # so it belongs with the targets, not with the free-running remainder.
        runner_pnl = sum(e["qty"] * (e["price"] - self.entry)
                         for e in self.exits
                         if not e["reason"].startswith("target_")
                         and e["reason"] != "runner_target")
        monitor = {}
        if MONITOR_MODEL["on"]:
            moves = [r for r in self.ledger if r["stop_moved"] > 0]
            monitor = {
                "monitor_days": len(self.ledger),
                "monitor_flags": dict(self.monitor_flags),
                "stop_moves": len(moves),
                "stop_lift_total": round(self.stop - self.initial_stop, 4),
                "added_targets": list(self.added_targets),
                "ledger": self.ledger,
            }
        return {**monitor,
            "entry_date": self.entry_date.isoformat(), "entry": round(self.entry, 4),
            "qty": self.qty, "initial_stop": round(self.initial_stop, 4),
            "final_stop": round(self.stop, 4),
            "targets": self.plan["targets"], "grade_at_build": self.plan["grade"],
            # 2026-08-31. The grade the plan was WRITTEN with and the grade the
            # trade actually FILLED at are two different claims about the same
            # setup -- rule 27's own example is buy 100 / stop 97 / target 108
            # filling at 102, which is A on the plan and B on the fill. One
            # column held both, so no analysis could say which of the two
            # failed. Set by the caller at the moment of the fire.
            "grade_at_fire": self.plan.get("grade_at_fire"),
            "rr_at_fire": self.plan.get("rr_at_fire"),
            # The five pass/fail criteria behind the letter. Without them the
            # only question that can be asked is "did the grade rank", and the
            # more useful one -- WHICH criterion is doing the damage -- has no
            # data at all. R:R >= 2.3 is the immediate suspect: it pushes the
            # target further away, and a further target is hit less often.
            "rubric_criteria": self.plan.get("rubric_criteria"),
            "rubric_score": self.plan.get("rubric_score"),
            "stop_basis": self.plan.get("stop_basis"),
            "confidence": self.plan.get("confidence"),
            "early_armed": self.early_armed, "early_moved": self.early_moved,
            "peak_atr": (round((self.peak - self.entry)
                               / self.plan["atr_at_build"], 3)
                         if self.plan.get("atr_at_build") else None),
            "exits": self.exits, "pnl": round(self.pnl(), 2),
            "gross_pnl": round(self.pnl() + self.fees(), 2),
            "fees": round(self.fees(), 2),
            "slippage": round(self.slippage_usd, 2),
            "fills": 1 + len(self.exits),
            "r": round(self.r_multiple(), 3),
            "runner_pnl": round(runner_pnl, 2),
            "exit_date": self.exits[-1]["date"] if self.exits else None,
        }


# --------------------------------------------------------------------------
# entry-slippage stats (signal close -> next open), for ENTRY_MODE "next_open"
# --------------------------------------------------------------------------

def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _median(xs):
    if not xs:
        return None
    v = sorted(xs)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def slip_stats(slips: list[dict]) -> dict | None:
    """Mean/median of the gap between the signal close and the next open."""
    if not slips:
        return None
    pct = [s["slip_pct"] for s in slips]
    r = [s["slip_r"] for s in slips if s["slip_r"] is not None]
    return {"fires": len(slips),
            "mean_pct": round(_mean(pct), 4), "median_pct": round(_median(pct), 4),
            "mean_r": round(_mean(r), 4) if r else None,
            "median_r": round(_median(r), 4) if r else None,
            "r_samples": len(r)}


# --------------------------------------------------------------------------
# the waiting-plan journal (inert unless --journal)
# --------------------------------------------------------------------------

JOURNAL = {"on": False}


def plan_signature(plan: dict | None) -> tuple | None:
    """What has to change before a waiting plan counts as a different plan."""
    if plan is None:
        return None
    return (plan.get("setup"), plan.get("blocked"),
            round(plan.get("trigger") or 0.0, 2),
            round(plan.get("stop") or 0.0, 2),
            tuple(round(t["price"], 2) for t in plan.get("targets", [])),
            plan.get("grade"))


def plan_record(day_str: str, plan: dict | None, event: str) -> dict:
    return {"date": day_str, "event": event,
            "setup": plan.get("setup") if plan else None,
            "trigger": round(plan["trigger"], 2) if plan and plan.get("trigger") else None,
            "stop": round(plan["stop"], 2) if plan and plan.get("stop") else None,
            "targets": [round(t["price"], 2) for t in plan.get("targets", [])] if plan else [],
            "grade": plan.get("grade") if plan else None,
            "blocked": plan.get("blocked") if plan else None}


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", nargs="?", default="AAPL")
    parser.add_argument("--years", type=float, default=5)
    parser.add_argument("--early-arm-atr", type=float, default=None,
                        help="turn the early trail on once profit reaches this "
                             "many build-time ATRs (pre-registered: 1.5)")
    parser.add_argument("--early-trail-atr", type=float, default=1.2,
                        help="once on, park the stop this many ATRs below the "
                             "running peak (pre-registered: 1.2)")
    parser.add_argument("--early-anchor", choices=["close", "high"],
                        default="close", help="peak measured on closes or highs")
    parser.add_argument("--costs", action="store_true",
                        help="charge IB tiered commission and slippage")
    parser.add_argument("--slippage-bps", type=float, default=5.0,
                        help="basis points paid per side when --costs is on")
    parser.add_argument("--cost-multiplier", type=float, default=1.0,
                        help="scale commission and slippage (2 = twice the "
                             "best estimate)")
    parser.add_argument("--commission-per-share", type=float, default=None,
                        help="override the per-share commission (0 = a flat "
                             "per-order fee only)")
    parser.add_argument("--commission-min", type=float, default=None,
                        help="override the per-order commission floor")
    parser.add_argument("--commission-cap-pct", type=float, default=None,
                        help="override the cap on a commission as a share of "
                             "the order's value (1.0 = effectively no cap)")
    parser.add_argument("--entry-fill", choices=["signal_close", "next_open"],
                        default="signal_close",
                        help="buy at the signal day's close (live rule) or at "
                             "the next bar's open")
    parser.add_argument("--monitor", action="store_true",
                        help="run the daily playbook on the open position "
                             "(see MONITOR_MODEL). Default off.")
    parser.add_argument("--monitor-retarget", action="store_true",
                        help="with --monitor: sell at a rebuilt runner target")
    parser.add_argument("--setups", choices=["breakout", "all"],
                        default="breakout",
                        help="which setup shapes may build a plan (see SETUPS)")
    parser.add_argument("--pre-t1-trail", action="store_true",
                        help="run the structural higher-low trail every day "
                             "from entry, not only after target 1 (PRE_T1_TRAIL)")
    parser.add_argument("--equity", type=float, default=None,
                        help="starting account size (default 100,000)")
    parser.add_argument("--tag", default=None,
                        help="suffix for the results file, so a run never "
                             "overwrites an earlier one")
    parser.add_argument("--all-in", action="store_true",
                        help="put every available dollar into each trade "
                             "instead of risking 1% (see SIZING)")
    parser.add_argument("--drop-last-bar", action="store_true",
                        help="ignore the final stored bar -- use it when the "
                             "session it covers has not settled yet")
    parser.add_argument("--journal", action="store_true",
                        help="record every day the waiting plan changed, for "
                             "a day-by-day read-back")
    args = parser.parse_args()
    ticker = args.ticker.upper()
    global START_EQUITY
    if args.equity:
        START_EQUITY = float(args.equity)
    if args.setups == "all":
        SETUPS["allow"] = tuple(setup_types.SETUP_TYPES)
    PRE_T1_TRAIL["on"] = args.pre_t1_trail
    JOURNAL["on"] = args.journal
    SIZING["mode"] = "all_in" if args.all_in else "risk"
    MONITOR_MODEL.update(on=args.monitor, retarget=args.monitor_retarget)
    EARLY_TRAIL.update(arm_atr=args.early_arm_atr,
                       trail_atr=args.early_trail_atr,
                       anchor=args.early_anchor)
    ENTRY_MODE.update(fill=args.entry_fill)
    if args.costs:
        m = args.cost_multiplier
        COSTS.update(on=True, per_share=0.0035 * m, min_per_order=0.35 * m,
                     slippage_bps=args.slippage_bps * m)
        for key, val in (("per_share", args.commission_per_share),
                         ("min_per_order", args.commission_min),
                         ("max_pct_of_trade", args.commission_cap_pct)):
            if val is not None:
                COSTS[key] = val

    bars = load_bars(ticker)
    if args.drop_last_bar:
        bars = bars[:-1]
    spy = load_bars("SPY")
    qqq = load_bars("QQQ")
    earnings = load_earnings(ticker)
    spy_ix = {b["date"]: i for i, b in enumerate(spy)}
    qqq_ix = {b["date"]: i for i, b in enumerate(qqq)}

    last_day = date.fromisoformat(bars[-1]["date"])
    sim_start = last_day - timedelta(days=int(args.years * 365.25))
    start_i = next(i for i, b in enumerate(bars)
                   if date.fromisoformat(b["date"]) >= sim_start)

    cash = START_EQUITY
    position: Position | None = None
    pending: dict | None = None
    trades, shadow_trades, shadow_open = [], [], []
    deferred: dict | None = None       # fire waiting for the next bar's open
    entry_slip = []                    # signal close -> next open, per fire
    plan_log: list[dict] = []
    last_plan_sig: tuple | None = None
    blocked_at_build: dict[str, int] = {}
    blocked_at_fire = []
    seen_shadow_keys = set()
    skipped_sizing = []
    skipped_cooldown = []              # fires inside the re-entry wait
    cooldown_from: int | None = None   # bar index of the last stop-out
    equity_curve = []

    for i in range(start_i, len(bars)):
        d = bars[i]["date"]
        if d not in spy_ix or d not in qqq_ix:
            continue                     # ticker traded, index data missing
        day = Day(bars, i, spy, qqq, spy_ix, qqq_ix, earnings)

        # 0) OPTIONAL next-open entry: a trigger that fired on an EARLIER day
        # is filled at THIS bar's open, before the bar is applied below -- so
        # today's stop and targets can act on the brand new position.
        if deferred is not None:
            entry_px = slip_buy(day.bar["open"])
            per_share = entry_px - deferred["plan"]["stop"]
            plan_risk = deferred["signal_close"] - deferred["plan"]["stop"]
            entry_slip.append({
                "date": d, "signal_close": round(deferred["signal_close"], 4),
                "next_open": round(entry_px, 4),
                "slip_pct": round(100 * (entry_px / deferred["signal_close"] - 1), 4),
                "slip_r": round((entry_px - deferred["signal_close"]) / plan_risk, 4)
                          if plan_risk > 0 else None,
            })
            qty = int((deferred["equity"] * RISK_PCT) // per_share) if per_share > 0 else 0
            qty = min(qty, int(cash // entry_px))
            if qty <= 0:
                skipped_sizing.append({"date": d, "reason": "qty_zero_next_open"})
            else:
                position = Position(deferred["plan"], entry_px, qty, day.date,
                                    raw_entry=day.bar["open"])
                cash -= qty * entry_px + position.entry_fee
            deferred = None

        # 1) manage the open position with today's bar
        if position is not None:
            if position.process_day(day):
                cash += position.proceeds()
                trades.append(position.to_record())
                # the re-entry wait after a real stop-out (monitor only)
                if (MONITOR_MODEL["on"] and position.exits
                        and is_defensive_exit(position.exits[-1]["reason"])):
                    cooldown_from = i
                position = None
                pending = None           # rebuild fresh after an exit
        # shadow positions (gate-blocked fires), same rules, no money
        still = []
        for sp in shadow_open:
            if sp.process_day(day):
                shadow_trades.append(sp.to_record())
            else:
                still.append(sp)
        shadow_open = still

        # 2) flat + pending plan from an EARLIER day -> trigger check
        if (position is None and deferred is None
                and pending is not None and "blocked" not in pending):
            in_wait = (cooldown_from is not None
                       and i <= cooldown_from
                       + MONITOR_MODEL["reentry_wait_trading_days"])
            if in_wait and day.close > pending["trigger"]:
                skipped_cooldown.append({"date": d})
            elif day.close > pending["trigger"] and day.close > pending["stop"]:
                grade_now, _rr = regrade_at_fire(pending, day)
                regime_bad = day.regime in decision_policy.BLOCKING_REGIMES
                if grade_now in decision_policy.BLOCKING_GRADES or regime_bad:
                    reason = ("regime_" + day.regime) if regime_bad else ("grade_" + grade_now)
                    key = (round(pending["trigger"], 2), round(pending["stop"], 2))
                    blocked_at_fire.append({"date": d, "reason": reason,
                                            "trigger": pending["trigger"]})
                    if key not in seen_shadow_keys:
                        seen_shadow_keys.add(key)
                        sq = max(1, int((START_EQUITY * RISK_PCT)
                                        // (day.close - pending["stop"])))
                        shadow_open.append(Position(pending, day.close, sq,
                                                    day.date, shadow=True))
                elif ENTRY_MODE["fill"] == "next_open":
                    # the slot is committed tonight; the fill happens tomorrow
                    deferred = {"plan": pending, "signal_close": day.close,
                                "equity": cash}
                    pending = None
                else:
                    risk_usd = cash * RISK_PCT
                    entry_px = slip_buy(day.close)
                    per_share = entry_px - pending["stop"]
                    if SIZING["mode"] == "all_in":
                        qty = int(cash // entry_px)
                    else:
                        qty = int(risk_usd // per_share) if per_share > 0 else 0
                        qty = min(qty, int(cash // entry_px))   # never buy on money we don't have
                    if qty <= 0:
                        skipped_sizing.append({"date": d, "reason": "qty_zero"})
                    else:
                        position = Position(pending, entry_px, qty, day.date,
                                            raw_entry=day.close)
                        cash -= qty * entry_px + position.entry_fee
                        if JOURNAL["on"]:
                            rec = plan_record(d, pending, "entered")
                            rec.update(price=round(entry_px, 2), qty=qty,
                                       grade_at_fire=grade_now,
                                       risk_usd=round(qty * (entry_px - pending["stop"]), 2),
                                       cash_after=round(cash, 2))
                            plan_log.append(rec)
                        pending = None
                        last_plan_sig = None

        # 3) flat -> rebuild the pending plan from data through today
        if position is None and deferred is None:
            plan = build_plan(day)
            if plan is not None and "blocked" in plan:
                blocked_at_build[plan["blocked"]] = blocked_at_build.get(plan["blocked"], 0) + 1
                pending = plan if "targets" in plan else None
            else:
                pending = plan
            if JOURNAL["on"]:
                sig = plan_signature(plan)
                if sig != last_plan_sig:
                    if plan is None:
                        if last_plan_sig is not None:
                            plan_log.append({"date": d, "event": "no_plan",
                                             "setup": None, "trigger": None,
                                             "stop": None, "targets": [],
                                             "grade": None, "blocked": None})
                    else:
                        plan_log.append(plan_record(
                            d, plan,
                            "built" if last_plan_sig is None else "changed"))
                    last_plan_sig = sig

        mark = cash if position is None else cash + position.remaining * day.close \
            + position.proceeds()
        equity_curve.append({"date": d, "equity": round(mark, 2)})

    # force-close anything still open at the last bar
    if position is not None:
        day_last = date.fromisoformat(bars[-1]["date"])
        position.force_close(day_last, bars[-1]["close"])
        cash += position.proceeds()
        trades.append(position.to_record())
    for sp in shadow_open:
        sp.force_close(date.fromisoformat(bars[-1]["date"]), bars[-1]["close"])
        shadow_trades.append(sp.to_record())

    # ---- summary ----------------------------------------------------------
    final_equity = cash
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    bh_start = bars[start_i]["close"]
    bh = START_EQUITY * bars[-1]["close"] / bh_start

    peak, max_dd = -1e18, 0.0
    for p in equity_curve:
        peak = max(peak, p["equity"])
        max_dd = max(max_dd, (peak - p["equity"]) / peak)

    summary = {
        "ticker": ticker, "window": [bars[start_i]["date"], bars[-1]["date"]],
        "start_equity": START_EQUITY, "final_equity": round(final_equity, 2),
        "return_pct": round((final_equity / START_EQUITY - 1) * 100, 2),
        "buy_hold_final": round(bh, 2),
        "buy_hold_return_pct": round((bh / START_EQUITY - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "trades": len(trades), "wins": len(wins),
        "win_rate_pct": round(100 * len(wins) / len(rs), 1) if rs else None,
        "total_r": round(sum(rs), 2),
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "runner_pnl_total": round(sum(t["runner_pnl"] for t in trades), 2),
        "target_pnl_total": round(sum(t["pnl"] - t["runner_pnl"] for t in trades), 2),
        "blocked_at_build": blocked_at_build,
        "blocked_at_fire": blocked_at_fire,
        "shadow_trades": len(shadow_trades),
        "shadow_total_r": round(sum(t["r"] for t in shadow_trades), 2),
        "skipped_sizing": skipped_sizing,
        "monitor_mode": dict(MONITOR_MODEL),
        "skipped_reentry_wait": len(skipped_cooldown),
        "entry_mode": dict(ENTRY_MODE),
        "costs": dict(COSTS),
        "cost_tally": {**COST_TALLY,
                       "commission": round(COST_TALLY["commission"], 2),
                       "slippage": round(COST_TALLY["slippage"], 2)},
        "fees_paid_total": round(sum(t["fees"] for t in trades), 2),
        "slippage_paid_total": round(sum(t["slippage"] for t in trades), 2),
        "fills_total": sum(t["fills"] for t in trades),
        "entry_slip_stats": slip_stats(entry_slip),
    }

    summary["setups_allowed"] = list(SETUPS["allow"])
    summary["sizing"] = dict(SIZING)
    summary["pre_t1_trail"] = dict(PRE_T1_TRAIL)
    out = {"summary": summary, "trades": trades, "shadow_trades": shadow_trades,
           "entry_slip": entry_slip, "equity_curve": equity_curve,
           "plan_log": plan_log}
    suffix = f"_{args.tag}" if args.tag else ""
    out_path = ROOT / f"results_{ticker}_breakout{suffix}.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(f"\nfull detail -> {out_path}")


if __name__ == "__main__":
    main()
