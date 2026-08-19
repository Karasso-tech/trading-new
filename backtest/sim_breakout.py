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
        self.earnings_days_out = earnings_days_out(earnings, self.date)


# --------------------------------------------------------------------------
# plan building + grading (live chain: classify -> stop -> targets -> grade)
# --------------------------------------------------------------------------

def build_plan(day: Day) -> dict | None:
    """Returns a pending breakout plan, or None. Mirrors build_plan.py's
    mechanical chain, restricted to Breakout per the pre-registration."""
    call = setup_classifier.classify(
        bars=day.recent40, atr14=day.atr14, sma20=day.sma20, sma50=day.sma50,
        wall_chains=day.wall_chains, swing_lows=day.swing_lows)
    if call.setup_type != setup_classifier.BREAKOUT or call.trigger is None:
        return None

    stop = level_picker.pick_stop(call.trigger, day.atr14, day.swing_lows,
                                  current_price=day.close, bars=day.recent40)
    if stop.stop is None:
        return {"blocked": "no_honest_stop", "trigger": call.trigger}

    scan = level_picker.pick_targets(call.trigger, stop.stop, day.atr14,
                                     day.wall_chains, bars=day.recent40,
                                     swing_lows=day.swing_lows)
    if not scan.targets:
        return {"blocked": "no_qualifying_target", "trigger": call.trigger}

    t1 = scan.targets[0]
    grade = rubric_formula.classify_rubric(rubric_formula.RubricInputs(
        rr=t1.rr, target_atr_multiple=t1.atr_mult, regime=day.regime,
        rs_delta_pct=day.rs20 if day.rs20 is not None else 0.0,
        dist_sma20_atr=day.dist_sma20_atr,
        earnings_days_out=day.earnings_days_out)).grade

    ceiling = decision_policy.max_allowed_decision(
        has_target=True, grade=grade, regime=day.regime)
    plan = {
        "built": day.date.isoformat(), "trigger": call.trigger,
        "stop": stop.stop, "stop_basis": stop.basis_kind,
        "targets": [{"price": t.price, "pct": t.pct} for t in scan.targets],
        "runner_pct": scan.runner_pct, "atr_at_build": day.atr14,
        "grade": grade, "confidence": call.confidence,
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
    grade = rubric_formula.classify_rubric(rubric_formula.RubricInputs(
        rr=rr, target_atr_multiple=(reward / day.atr14) if day.atr14 else 0.0,
        regime=day.regime,
        rs_delta_pct=day.rs20 if day.rs20 is not None else 0.0,
        dist_sma20_atr=day.dist_sma20_atr,
        earnings_days_out=day.earnings_days_out)).grade
    return grade, rr


# --------------------------------------------------------------------------
# position lifecycle
# --------------------------------------------------------------------------

class Position:
    def __init__(self, plan, entry, qty, entry_date, shadow=False):
        self.plan = plan
        self.entry = entry
        self.qty = qty
        self.remaining = qty
        self.stop = plan["stop"]
        self.initial_stop = plan["stop"]
        self.entry_date = entry_date
        self.shadow = shadow
        self.exits = []                       # {date, reason, qty, price}
        # rule 7 tranche sizes; runner absorbs rounding
        self.tranche_qty = []
        assigned = 0
        for t in plan["targets"]:
            q = int(round(qty * t["pct"] / 100.0))
            self.tranche_qty.append(q)
            assigned += q
        self.runner_qty = qty - assigned
        self.filled_targets = set()
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
        self.exits.append({"date": day_date.isoformat(), "reason": reason,
                           "qty": qty, "price": price})
        self.remaining -= qty

    def process_day(self, day: Day) -> bool:
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
        # trail at day end, effective tomorrow -- live rule: only after T1
        if self.past_target_1():
            trailed = level_picker.trail_stop(
                current_price=day.close, current_stop=self.stop,
                atr_at_build=self.plan["atr_at_build"],
                swing_lows=day.swing_lows, past_target_1=True,
                bars=day.recent40)
            if trailed.moved:
                self.stop = trailed.stop
        return False

    def force_close(self, day_date, price):
        self._sell(day_date, "end_of_data", self.remaining, price)

    def pnl(self) -> float:
        return sum(e["qty"] * (e["price"] - self.entry) for e in self.exits)

    def r_multiple(self) -> float:
        risk = self.qty * (self.entry - self.initial_stop)
        return self.pnl() / risk if risk else 0.0

    def to_record(self) -> dict:
        runner_pnl = sum(e["qty"] * (e["price"] - self.entry)
                         for e in self.exits
                         if not e["reason"].startswith("target_"))
        return {
            "entry_date": self.entry_date.isoformat(), "entry": round(self.entry, 4),
            "qty": self.qty, "initial_stop": round(self.initial_stop, 4),
            "final_stop": round(self.stop, 4),
            "targets": self.plan["targets"], "grade_at_build": self.plan["grade"],
            "stop_basis": self.plan.get("stop_basis"),
            "confidence": self.plan.get("confidence"),
            "early_armed": self.early_armed, "early_moved": self.early_moved,
            "peak_atr": (round((self.peak - self.entry)
                               / self.plan["atr_at_build"], 3)
                         if self.plan.get("atr_at_build") else None),
            "exits": self.exits, "pnl": round(self.pnl(), 2),
            "r": round(self.r_multiple(), 3),
            "runner_pnl": round(runner_pnl, 2),
            "exit_date": self.exits[-1]["date"] if self.exits else None,
        }


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
    args = parser.parse_args()
    ticker = args.ticker.upper()
    EARLY_TRAIL.update(arm_atr=args.early_arm_atr,
                       trail_atr=args.early_trail_atr,
                       anchor=args.early_anchor)

    bars = load_bars(ticker)
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
    blocked_at_build: dict[str, int] = {}
    blocked_at_fire = []
    seen_shadow_keys = set()
    skipped_sizing = []
    equity_curve = []

    for i in range(start_i, len(bars)):
        d = bars[i]["date"]
        if d not in spy_ix or d not in qqq_ix:
            continue                     # ticker traded, index data missing
        day = Day(bars, i, spy, qqq, spy_ix, qqq_ix, earnings)

        # 1) manage the open position with today's bar
        if position is not None:
            if position.process_day(day):
                cash += position.pnl() + position.qty * position.entry
                trades.append(position.to_record())
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
        if position is None and pending is not None and "blocked" not in pending:
            if day.close > pending["trigger"] and day.close > pending["stop"]:
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
                else:
                    risk_usd = cash * RISK_PCT
                    per_share = day.close - pending["stop"]
                    qty = int(risk_usd // per_share)
                    qty = min(qty, int(cash // day.close))   # never buy on money we don't have
                    if qty <= 0:
                        skipped_sizing.append({"date": d, "reason": "qty_zero"})
                    else:
                        position = Position(pending, day.close, qty, day.date)
                        cash -= qty * day.close
                        pending = None

        # 3) flat -> rebuild the pending plan from data through today
        if position is None:
            plan = build_plan(day)
            if plan is not None and "blocked" in plan:
                blocked_at_build[plan["blocked"]] = blocked_at_build.get(plan["blocked"], 0) + 1
                pending = plan if "targets" in plan else None
            else:
                pending = plan

        mark = cash if position is None else cash + position.remaining * day.close \
            + sum(e["qty"] * e["price"] for e in position.exits)
        equity_curve.append({"date": d, "equity": round(mark, 2)})

    # force-close anything still open at the last bar
    if position is not None:
        day_last = date.fromisoformat(bars[-1]["date"])
        position.force_close(day_last, bars[-1]["close"])
        cash += position.pnl() + position.qty * position.entry
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
    }

    out = {"summary": summary, "trades": trades, "shadow_trades": shadow_trades,
           "equity_curve": equity_curve}
    out_path = ROOT / f"results_{ticker}_breakout.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(f"\nfull detail -> {out_path}")


if __name__ == "__main__":
    main()
