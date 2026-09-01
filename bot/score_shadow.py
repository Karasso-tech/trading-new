"""Hardening Pass item 7: shadow-book scoring -- CAPTURE ONLY, no analysis, no
calibration, no conclusions built on top of this yet (deliberately deferred until
a real trade sample exists, per the user's own decision -- see the journal/
threshold-calibration deferral already documented in CLAUDE_CODE_INSTRUCTIONS.md).

In 3-6 months the journal (bot/persistence.py's closing_summaries) will show how
TAKEN trades performed. It will never show whether the filters rejected winners --
that's only knowable if every screened setup's hypothetical outcome (fired or not,
and if fired, how far it ran/fell) is captured from the start, not reconstructed
later from memory. This script is that capture: pure arithmetic against a stored
thesis's own primary_setup and real daily bars, nothing judgment-requiring.

compute_shadow_metrics() is the pure, unit-testable core -- no fetch, no DB,
callable directly with synthetic bars. run_one()/main() are the DB+fetch wiring
around it.

Usage:
  python bot/score_shadow.py                 # score every candidate thesis (get_shadow_candidates())
  python bot/score_shadow.py TICKER           # score one ticker only

Sequential fetches only (one TVClient session, one ticker at a time -- see
tv_data.py's own module docstring on why concurrent CDP calls are unsafe). One
ticker's fetch failure is logged and skipped, never kills the batch.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import persistence
import rubric_formula
import sector_map
import trade_sim
import tv_lock
from tv_data import TVClient


@dataclass
class ShadowMetrics:
    hypothetical_trigger_fired: bool
    trigger_fired_date: Optional[str]
    max_favorable_excursion: Optional[float]  # % above trigger, best case after the hypothetical fire
    max_adverse_excursion: Optional[float]    # % below trigger, worst case after the hypothetical fire (<=0)
    # --- what an idea that never fired is actually worth knowing (2026-08-30) ---
    #
    # Until now a non-firing idea recorded "did not fire" and nothing else: every
    # field describing what price did was NULL. So the book could say how many
    # ideas started and could not say ANYTHING about why the rest did not --
    # whether the trigger was missed by a cent or by a mile.
    #
    # That distinction is the single most useful thing here for making entries
    # more precise, and it is the one thing waiting longer cannot supply. A year
    # of waiting still only answers "fired / did not fire". Measuring the gap
    # answers "by how much", today, on all 394 of them.
    closest_approach_pct: Optional[float] = None   # how far short of the trigger, % (0 = touched it)
    closest_approach_atr: Optional[float] = None   # the same gap in the setup's own ATR
    closest_approach_date: Optional[str] = None    # the day it came nearest
    move_without_entry_pct: Optional[float] = None  # what the stock did while we stood aside


def _bar_date(bar: dict) -> str:
    return datetime.fromtimestamp(bar["time"], tz=timezone.utc).date().isoformat()


def _near_miss(relevant: list[dict], trigger: float,
                atr_at_build: Optional[float]) -> dict:
    """How near an idea that never fired actually came, and what the stock did.

    Measured against the CLOSE, not the intraday high, because the trigger is a
    daily-close rule (SCREENER_v3's own standard). A stock that spiked through
    the level intraday and closed back under it did not trigger, and calling
    that a near miss would quietly rewrite the entry rule while measuring it.

    closest_approach_pct is 0 at the trigger and negative below it, so it reads
    the same way as max_adverse_excursion. In ATR because a cent means something
    different on a $9 stock than on a $900 one, and the whole point is to be able
    to compare across the book.

    move_without_entry_pct is the last close against the close on the day the
    idea was built: did the stock go the way we thought while we stood aside, or
    did staying out save us. Those are opposite findings and the raw
    "did not fire" cannot tell them apart."""
    if not relevant:
        return {}
    best = max(relevant, key=lambda b: b["close"])
    gap = best["close"] - trigger
    first_close = relevant[0]["close"]
    return {
        "closest_approach_pct": gap / trigger * 100 if trigger else None,
        "closest_approach_atr": (gap / atr_at_build) if atr_at_build else None,
        "closest_approach_date": _bar_date(best),
        "move_without_entry_pct": ((relevant[-1]["close"] - first_close) / first_close * 100
                                    if first_close else None),
    }


def compute_shadow_metrics(bars: list[dict], trigger: float, since_date: str,
                            atr_at_build: Optional[float] = None) -> ShadowMetrics:
    """Pure arithmetic, no fetch, no DB -- unit-testable with synthetic bars.

    bars: raw daily OHLCV, oldest-first (tv_data.py's shape: time/open/high/low/close).
    trigger: the stored setup's own trigger level (long-only per STRATEGY_v3.md --
      a hypothetical fire is the first bar whose CLOSE reaches or exceeds trigger,
      matching SCREENER_v3.md's own daily-close-confirmation standard, not an
      intraday touch).
    since_date: the thesis's date_built (ISO YYYY-MM-DD) -- bars before this date
      predate the setup and are never considered for firing or excursion.

    Never fired -> max_favorable_excursion/max_adverse_excursion are None, not
    0.0 -- there is no entry point to measure excursion from, and 0.0 would
    misleadingly read as "fired and went nowhere."
    """
    since_dt = datetime.fromisoformat(since_date).date()
    relevant = [b for b in bars if datetime.fromtimestamp(b["time"], tz=timezone.utc).date() >= since_dt]

    fired_index = None
    for i, b in enumerate(relevant):
        if b["close"] >= trigger:
            fired_index = i
            break

    if fired_index is None:
        return ShadowMetrics(
            hypothetical_trigger_fired=False, trigger_fired_date=None,
            max_favorable_excursion=None, max_adverse_excursion=None,
            **_near_miss(relevant, trigger, atr_at_build),
        )

    after = relevant[fired_index:]
    best_high = max(b["high"] for b in after)
    worst_low = min(b["low"] for b in after)
    return ShadowMetrics(
        hypothetical_trigger_fired=True,
        trigger_fired_date=_bar_date(relevant[fired_index]),
        max_favorable_excursion=(best_high - trigger) / trigger * 100,
        max_adverse_excursion=(worst_low - trigger) / trigger * 100,
    )


# --- Full-plan simulation (2026-08-03) --------------------------------------
#
# Everything above captures "did it fire, and how far did it swing". That was
# deliberately shallow (capture-only, no conclusions). It is not enough to
# answer the question the user actually wants answered -- "do my filters reject
# winners?" -- because a raw percentage move favours volatile tickers by
# construction: on the first real run, the F-graded rejects showed the biggest
# moves purely because F-graded tickers are the jumpy ones. Measuring in R
# (units of the trade's own risk) removes that bias entirely.
#
# ASSUMPTIONS, stated openly, because a backtest whose assumptions are hidden
# is a number generator, not evidence:
#   1. A trigger fires on the first daily CLOSE at or above it, on/after the
#      thesis's own date_built. Same standard SCREENER_v3.md/MONITOR_v2.md use
#      for a real 🟢 -- never an intraday touch.
#   2. Entry is the NEXT bar's open, not the trigger price. That is when a
#      human acting on a daily-close confirmation can actually buy. The gap
#      between trigger and that open is recorded as `entry_gap_pct` -- it is a
#      real cost of the daily-close rule and worth measuring, not hiding.
#   3. Risk per share = entry - stop, using the thesis's own stored stop. The
#      stop is NOT re-derived from the new entry: a gap-up entry genuinely does
#      widen real risk, and pretending otherwise would flatter every result.
#   4. Stop and target in the same bar -> the STOP is assumed hit first. Daily
#      bars cannot say which came first; assuming the good one is how
#      backtests lie.
#   5. A gap through a level fills at the open, not the level -- a stop below
#      an open that gapped under it fills at the open, worse than planned.
#   6. No commissions, no slippage beyond the gap effects above, no partial
#      fills, no position sizing (everything is in R, which is size-free).
#   7. `r_multiple_planned` follows rule 7's real allocation (40/60 for one
#      target, 40/35/25 for two) and STRATEGY_v3.md's real ratcheting runner
#      trail. As of 2026-08-03 the walk itself lives in trade_sim.py, shared
#      verbatim with the 5-year backtest -- see that module for the full
#      assumption list, which is now the single copy. (Until that date this
#      module used a breakeven-stop stand-in instead of the real trail, and the
#      backtest used something else again; two engines answering one question
#      two ways cannot be compared, which is why there is now only one.)
#   8. Still-open trades are marked to the last close and flagged `open` --
#      never silently counted as a win or dropped from the sample.
SIM_VERSION = "2.1"   # 2.0: the walk moved into trade_sim.py (shared engine).
                      # 2.1 (2026-08-09): the benchmark, the fire clock and the
                      # never-fired expiry below. No change to the walk itself,
                      # so 2.0 and 2.1 R-multiples remain directly comparable --
                      # which is the whole reason this field exists.

# How long a plan stays measurable before "it never started" becomes the answer
# rather than an open question (2026-08-09).
#
# 35 of 99 live builds sat at resolution 'never_fired' with no ending, and would
# have sat there forever: every night re-asked "has it fired yet?" and wrote the
# same non-answer again. That makes the most basic question about this whole
# strategy -- out of 100 ideas, how many even start? -- permanently unanswerable,
# because the denominator never settles.
#
# Three trading months, by the owner's decision on 2026-08-30. The previous
# value was 20 days -- one trading month -- and it was written down at the time
# as a first draft, not tuned and not claimed to be.
#
# What changed is the purpose. The 20 days existed to settle a denominator: out
# of 100 ideas, how many even start. Three months exists to collect entry
# evidence, which is a different job and needs a longer look. The owner's aim
# is to make entry points precise, so an idea that came within a whisker of its
# trigger in week six is exactly the case worth having, and the old window threw
# it away as "expired" before it could be seen.
#
# A plan that HAS fired is not governed by this at all: it runs to a real ending,
# with no time limit, however long that takes. Only the waiting has a deadline.
NEVER_FIRED_EXPIRY_TRADING_DAYS = 63

# The Alternate had nothing numeric to simulate. Its own resolution, never
# "never_fired": rule 5 explicitly permits a second setup with no cited level
# yet, so folding those into the not-triggered bucket would drag down the one
# statistic the book exists to produce.
UNTESTABLE = "not_testable"


@dataclass
class TradeSim:
    """One hypothetical trade, played out bar by bar under the assumptions
    above. Every field is either a fact from the bars or None -- nothing here
    is estimated when the inputs don't support it."""
    fired: bool
    fired_date: Optional[str] = None
    entry: Optional[float] = None
    entry_date: Optional[str] = None
    entry_gap_pct: Optional[float] = None    # (entry - trigger) / trigger * 100
    risk_per_share: Optional[float] = None
    resolution: str = "never_fired"           # never_fired|stop|target_1|target_2|open
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    r_multiple_simple: Optional[float] = None   # whole position out at first resolution
    r_multiple_planned: Optional[float] = None  # tranche-weighted, breakeven stop after target 1
    mfe_r: Optional[float] = None               # best unrealized move, in units of risk
    mae_r: Optional[float] = None               # worst unrealized move, in units of risk
    bars_held: Optional[int] = None
    note: Optional[str] = None
    # Is anything left in this position (2026-08-30)? `resolution` cannot say:
    # it reads "target_1" from the moment the first tranche sells and stays
    # there while the runner runs, so a live trade and a finished one are
    # spelled identically.
    #
    # Copied from the engine's own answer rather than re-derived here. There are
    # two result types in this project -- trade_sim.SimResult, which the engine
    # returns, and this one, which score_shadow hands to run_one -- and adding
    # the field to only the first is exactly the mistake that shipped for an
    # hour: every test passed, because nothing tests run_one, and the nightly
    # scan would have died on the first row with an AttributeError.
    is_fully_closed: bool = False


def _numeric(value) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def extract_plan(primary_setup: Optional[dict]) -> dict:
    """Trigger/stop/targets/allocations out of a stored primary_setup, in the
    exact shape SCREENER_v3.md section ז saves them. Only clean numbers are
    accepted -- a pending trigger still written as free text (rule 14) has
    nothing to simulate against and is never guessed at, same posture as
    _extract_trigger above."""
    setup = primary_setup or {}
    targets = []
    for t in (setup.get("targets") or []):
        price = _numeric(t.get("price"))
        if price is None:
            continue
        targets.append({"price": price, "pct": _numeric(t.get("pct")) or 0.0})
    targets.sort(key=lambda t: t["price"])
    return {
        "trigger": _numeric(setup.get("trigger")),
        "stop": _numeric(setup.get("stop")),
        "atr_at_build": _numeric(setup.get("atr_at_build")),
        "setup_type": setup.get("type"),
        "stop_basis_kind": setup.get("stop_basis_kind"),
        "targets": targets[:2],   # rule 7 allows at most two sellable targets
    }


def simulate_trade(bars: list[dict], plan: dict, since_date: str) -> TradeSim:
    """Pure arithmetic, no fetch, no DB -- unit-testable with synthetic bars,
    same split compute_shadow_metrics() already uses. See the assumptions block
    above; every one of them is visible in the code below rather than buried."""
    trigger, stop = plan.get("trigger"), plan.get("stop")
    if trigger is None:
        # Never fired, so there is no position; is_fully_closed stays False and
        # the never-fired expiry in run_one is what ends this row's life.
        return TradeSim(fired=False, note="no numeric trigger on the stored setup")

    since_dt = datetime.fromisoformat(since_date).date()
    relevant = [b for b in bars if datetime.fromtimestamp(b["time"], tz=timezone.utc).date() >= since_dt]

    fired_index = next((i for i, b in enumerate(relevant) if b["close"] >= trigger), None)
    if fired_index is None:
        return TradeSim(fired=False)

    fired_date = _bar_date(relevant[fired_index])
    if fired_index + 1 >= len(relevant):
        # Fired on the most recent bar -- there is no next open to buy at yet.
        return TradeSim(fired=True, fired_date=fired_date, resolution="open",
                        note="fired on the last available bar; no entry bar yet")

    entry_bar = relevant[fired_index + 1]
    entry = entry_bar["open"]
    sim = TradeSim(
        fired=True, fired_date=fired_date, entry=entry, entry_date=_bar_date(entry_bar),
        entry_gap_pct=(entry - trigger) / trigger * 100,
    )

    if stop is None or stop >= entry:
        # A gap-up straight through the stop is a real outcome, not a bug -- but
        # there is no positive risk denominator, so no R can honestly be stated.
        sim.resolution = "open"
        sim.note = "no usable stop below the entry -- R cannot be computed"
        return sim

    sim.risk_per_share = entry - stop

    # 2026-08-03: the walk itself moved to trade_sim.py -- the ONE engine the
    # 5-year backtest also uses. Before that, the nightly shadow book and the
    # backtest simulated the same question two different ways (this one had no
    # runner trail and no second target), so their numbers could never be
    # compared. The user's requirement was explicit: the research must be 1-1
    # with what the live system does.
    entry_index = fired_index + 1
    all_bars = relevant
    highs = [b["high"] for b in all_bars]
    lows = [b["low"] for b in all_bars]
    closes = [b["close"] for b in all_bars]
    atrs = trade_sim.atr_series(highs, lows, closes)
    if atrs[entry_index] is None and plan.get("atr_at_build"):
        # A thesis built very recently may not have 15 bars since date_built for
        # a fresh ATR; the stored build-time ATR is the honest fallback, and it
        # only feeds the trailing stop, never the R denominator.
        atrs = [plan["atr_at_build"]] * len(all_bars)

    targets = [t["price"] for t in (plan.get("targets") or [])]
    tsim = trade_sim.simulate(
        all_bars, entry_index,
        trade_sim.build_plan(entry, stop, targets), atrs, _bar_date,
    )
    sim.resolution = tsim.resolution
    sim.exit_date = tsim.exit_date
    sim.exit_price = tsim.exit_price
    sim.r_multiple_planned = tsim.r_multiple
    sim.r_multiple_simple = tsim.r_multiple_full_exit_at_t1
    sim.mfe_r = tsim.mfe_r
    sim.mae_r = tsim.mae_r
    sim.bars_held = tsim.bars_held
    sim.is_fully_closed = tsim.is_fully_closed
    return sim


def benchmark_return(index_bars: list[dict], start_date: str,
                      end_date: Optional[str]) -> Optional[float]:
    """SPY's own % move over the same window this trade was open, or None when
    the window cannot be measured from the bars given.

    Why this is here at all (2026-08-09): a shadow row said what an idea did and
    never what the market did while it did it. "+0.05R on average" is a good
    number in a week SPY fell and a bad one in a week SPY ran, and the table had
    no way to tell those two apart -- so the average could not be read in either
    direction. Every result now carries its own yardstick.

    Measured close-to-close on the SAME calendar window as the trade (entry date
    to exit date), NOT the whole history: comparing a 4-day trade against a
    5-year index return would flatter or damn it purely by holding period.
    `end_date` None means still open, and the last available bar is used -- the
    same mark-to-last-close treatment trade_sim already gives an open trade.

    Pure arithmetic on bars handed in, no fetch -- same testable split as
    compute_shadow_metrics/simulate_trade above."""
    if not index_bars or not start_date:
        return None
    start = datetime.fromisoformat(start_date).date()
    at_or_after = [b for b in index_bars
                   if datetime.fromtimestamp(b["time"], tz=timezone.utc).date() >= start]
    if not at_or_after:
        return None
    first = at_or_after[0]
    if end_date:
        end = datetime.fromisoformat(end_date).date()
        upto = [b for b in at_or_after
                if datetime.fromtimestamp(b["time"], tz=timezone.utc).date() <= end]
        if not upto:
            return None
        last = upto[-1]
    else:
        last = at_or_after[-1]
    if not first["close"]:
        return None
    return (last["close"] - first["close"]) / first["close"] * 100


def trading_days_between(bars: list[dict], start_date: str, end_date: str) -> Optional[int]:
    """How many real bars sit between two dates, counted from the bars
    themselves rather than from a calendar.

    Using the ticker's own bars is deliberate: a holiday, a halt, or a symbol
    that simply did not trade is already absent from them, so this can never
    count a day the market was shut. persistence.count_trading_days answers the
    same question from the NYSE calendar and is the right tool when there are no
    bars to hand; here there always are."""
    if not bars or not start_date or not end_date:
        return None
    start = datetime.fromisoformat(start_date).date()
    end = datetime.fromisoformat(end_date).date()
    if end < start:
        return None
    return sum(1 for b in bars
               if start <= datetime.fromtimestamp(b["time"], tz=timezone.utc).date() <= end) - 1


def rr_at_build(plan: dict) -> Optional[float]:
    """The plan's own reward-to-risk when it was written: distance to the FIRST
    sellable target over distance to the stop.

    First target, not the furthest: rule 3 gates on the nearest sellable level,
    rule 7 sells the first tranche there, and quoting the far one would state a
    reward the plan never claimed it would take in full.

    None whenever the plan lacks a numeric trigger, stop or target -- never a
    guess, same posture as extract_plan above."""
    trigger, stop = plan.get("trigger"), plan.get("stop")
    targets = plan.get("targets") or []
    if trigger is None or stop is None or not targets:
        return None
    risk = trigger - stop
    if risk <= 0:
        return None
    return (targets[0]["price"] - trigger) / risk


def _extract_trigger(primary_setup: Optional[dict]) -> Optional[float]:
    """Only a clean numeric trigger is usable -- a pending setup whose trigger is
    still free text ("no order ready; trigger determined after the confirmation
    candle forms", rule 14) has nothing to hypothetically fire against yet.
    Never guesses a number out of that text."""
    if not primary_setup:
        return None
    trigger = primary_setup.get("trigger")
    if isinstance(trigger, (int, float)):
        return float(trigger)
    return None


async def _fetch_bars(ticker: str) -> list[dict]:
    async with TVClient() as client:
        return await client.get_daily_history(ticker, years=2)


async def _fetch_spy_bars() -> list[dict]:
    async with TVClient() as client:
        return await client.get_daily_history("SPY", years=2)


# One SPY fetch per batch, not one per ticker. The benchmark is the same series
# for every row scored in a run, and score_shadow is deliberately sequential
# (see the module docstring on why concurrent CDP calls are unsafe), so fetching
# it 40 times would multiply the run's length for identical data.
_spy_bars_cache: Optional[list[dict]] = None

# One fetch per TICKER per run, not one per idea (2026-08-30). Superseded builds
# rejoined the nightly scan in the same change, which took the candidate list
# from 61 to 246 -- across 62 distinct tickers. The bars for a ticker are
# identical for every idea on it, so without this the run would do four times
# the network work for exactly the same data.
_bars_cache: dict = {}


def clear_bars_cache() -> None:
    """Between runs, and in tests. The cache is per-run by intent: bars move."""
    _bars_cache.clear()


def _spy_bars() -> list[dict]:
    """SPY's daily bars for this run, fetched at most once.

    A failure here returns an empty list rather than raising: the benchmark is
    context, and losing it must never cost the night its actual capture. The
    affected rows get a NULL spy_return_pct, which reads honestly as "not
    measured" -- unlike a 0.0, which would read as "the market went nowhere"."""
    global _spy_bars_cache
    if _spy_bars_cache is None:
        try:
            _spy_bars_cache = asyncio.run(_fetch_spy_bars())
        except Exception as e:
            print(f"WARNING: SPY benchmark fetch failed, rows will have no benchmark ({e})",
                  file=sys.stderr)
            _spy_bars_cache = []
    return _spy_bars_cache


def regrade_at_fire(rubric_inputs, entry: Optional[float], stop: Optional[float],
                     target: Optional[float], atr_at_build: Optional[float]):
    """The grade for the trade that was actually on offer, not the one planned.

    CONSISTENCY_RULES.md rule 27 already draws this line for the live system:
    the stored trigger, stop and target are never re-picked, but R:R is measured
    from the entry the user would ACTUALLY get. Buy 100 / stop 97 / target 108
    that fires and fills at 102 is five of risk against six of reward, not three
    against eight -- and it was being graded as the second.

    The shadow book had no field for that second number at all, so a build-time
    grade and a fire-time grade sat in one column and no analysis could say
    which of the two had failed.

    Only the two price criteria are recomputed. The other three -- market
    regime, relative strength, an event in the window -- are the build-time
    values, because this book stores no history of them per date and inventing
    one would be worse than reusing a stated one. That is a real limit and it is
    the reason this returns a grade "at fire" rather than a grade "today".

    Returns (grade, rr) or (None, None) when the inputs are not all there."""
    if not isinstance(rubric_inputs, dict) or entry is None or stop is None:
        return None, None
    risk = entry - stop
    if risk <= 0 or target is None:
        return None, None
    reward = max(0.0, target - entry)
    rr = reward / risk
    try:
        merged = dict(rubric_inputs)
        merged["rr"] = rr
        merged["target_atr_multiple"] = (reward / atr_at_build) if atr_at_build else 0.0
        result = rubric_formula.classify_rubric(rubric_formula.RubricInputs(
            rr=float(merged["rr"]),
            target_atr_multiple=float(merged["target_atr_multiple"]),
            regime=str(merged.get("regime") or ""),
            rs_delta_pct=float(merged["rs_delta_pct"]),
            dist_sma20_atr=float(merged["dist_sma20_atr"]),
            earnings_days_out=(None if merged.get("earnings_days_out") is None
                                else int(merged["earnings_days_out"])),
        ))
    except (KeyError, TypeError, ValueError):
        return None, None
    return result.grade, rr


def _record_untestable(ticker: str, idea_id, why: str) -> Optional[int]:
    """One row saying the Alternate could not be simulated, and why.

    A row rather than silence, so "how often is the second setup only prose"
    becomes a countable question instead of an impression. Its own resolution,
    so it can never be read as a plan that waited and failed.
    """
    try:
        row_id = persistence.record_shadow_outcome(
            ticker, checked_date=datetime.now().date().isoformat(), price=None,
            hypothetical_trigger_fired=False,
            max_favorable_excursion=None, max_adverse_excursion=None,
            idea_id=idea_id, setup_side="alternate",
            resolution=UNTESTABLE, sim_note=why, sim_version=SIM_VERSION,
            is_fully_closed=0,
        )
    except sqlite3.IntegrityError:
        return None
    print(f"OK {ticker} [alternate]: {UNTESTABLE} ({why})")
    return row_id


def alternate_is_testable(setup) -> tuple:
    """Can the Alternate be simulated at all, and if not, why not.

    Rule 5 requires every report to carry two setups, and explicitly allows the
    second to have no cited level yet -- "an honest 'not yet defined' still
    satisfies this rule". So a large share of Alternates are prose, not numbers,
    and that is correct behaviour rather than a defect.

    It matters here because of how such a row would otherwise be counted. An
    Alternate with no trigger cannot fire, and a simulator that records it as
    "never fired" would fold every undefined second setup into the same bucket
    as real plans the price never reached -- dragging down the very statistic
    the book exists to produce. So it gets its own resolution and is never
    counted as a plan that failed to trigger.

    Needs all three: a trigger to fire on, a stop to measure risk from, and at
    least one priced target. Two out of three is not a plan."""
    if not isinstance(setup, dict):
        return False, "no alternate setup on this build"
    if _extract_trigger(setup) is None:
        return False, "alternate has no numeric trigger (rule 5 allows this)"
    if _numeric(setup.get("stop")) is None:
        return False, "alternate has no numeric stop"
    targets = [t for t in (setup.get("targets") or [])
               if isinstance(t, dict) and _numeric(t.get("price")) is not None]
    if not targets:
        return False, "alternate has no priced target"
    return True, ""


def run_one(thesis: dict) -> Optional[int]:
    """Score every unsettled setup on one build -- Primary and Alternate both.

    Returns the first row id written, or None when nothing was written; the
    return exists for callers that log it, and the work is the point.

    Both sides, as of 2026-08-30. Rule 5 has required two setups in every report
    from the beginning and only the Primary was ever simulated, so the book was
    learning from half of what the system produces -- and rule 7 records two real
    cases where the Alternate's deeper entry turned the SAME level into a better
    trade than the Primary's (ANET 179.80 failing from 179.80 and paying 2.54:1
    from ~162; MU's identical target going from 1.78:1 to 8.19:1). Exactly the
    half that was invisible.

    `thesis` is one dict from persistence.get_shadow_candidates(), which since
    2026-08-07 returns live `ideas` rows rather than ticker-keyed thesis rows.
    The idea_id it carries is written onto every result, so a symbol screened
    three times produces three separate scorecards instead of one that appears
    to change its mind nightly."""
    ticker = thesis["ticker"]
    since_date = (thesis.get("date_built") or thesis.get("built_at") or "")[:10]
    if not since_date:
        print(f"SKIP {ticker}: no date_built on this build")
        return None

    done = set(thesis.get("finished_setup_sides") or [])
    first_id = None
    for side in persistence.SHADOW_SIDES:
        if side in done:
            continue
        row_id = _score_side(thesis, side)
        first_id = first_id or row_id
    return first_id


def _score_side(thesis: dict, side: str) -> Optional[int]:
    """Fetch, score and persist ONE setup of one build."""
    ticker = thesis["ticker"]
    idea_id = thesis.get("idea_id") or thesis.get("id")
    since_date = (thesis.get("date_built") or thesis.get("built_at") or "")[:10]
    setup = thesis.get("primary_setup") if side == "primary" else thesis.get("alternate_setup")

    if side == "alternate":
        testable, why = alternate_is_testable(setup)
        if not testable:
            return _record_untestable(ticker, idea_id, why)

    trigger = _extract_trigger(setup)
    if trigger is None or not since_date:
        print(f"SKIP {ticker} [{side}]: no numeric trigger or date_built on the stored setup")
        return None
    if ticker in _bars_cache:
        bars = _bars_cache[ticker]
    else:
        try:
            bars = asyncio.run(_fetch_bars(ticker))
        except Exception as e:
            print(f"FAILED {ticker}: fetch error, skipping ({e})", file=sys.stderr)
            return None
        _bars_cache[ticker] = bars

    plan = extract_plan(setup)
    metrics = compute_shadow_metrics(bars, trigger, since_date,
                                      atr_at_build=plan.get("atr_at_build"))
    sim = simulate_trade(bars, plan, since_date)
    price = bars[-1]["close"] if bars else None
    today = datetime.now().date().isoformat()
    targets = plan.get("targets") or []

    fire_grade, fire_rr = regrade_at_fire(
        thesis.get("rubric_inputs"), sim.entry, plan.get("stop"),
        targets[0]["price"] if targets else None, plan.get("atr_at_build"))

    # --- the 2026-08-09 columns -------------------------------------------
    resolution = sim.resolution
    days_to_fire = trading_days_between(bars, since_date, sim.fired_date) if sim.fired_date else None

    # A plan that never reached its trigger inside one trading month is not
    # "still waiting", it is finished -- see NEVER_FIRED_EXPIRY_TRADING_DAYS.
    # Without this the never_fired rows have no ending and the most basic
    # question about the strategy (how many ideas even start?) has no
    # denominator that ever settles.
    if resolution == "never_fired":
        days_waited = trading_days_between(bars, since_date, today)
        if days_waited is not None and days_waited >= NEVER_FIRED_EXPIRY_TRADING_DAYS:
            resolution = "expired_never_fired"

    # The benchmark covers the window the trade was actually exposed for: entry
    # to exit, or entry to now while it is still open. An idea that never fired
    # was never exposed, so it gets no benchmark rather than a misleading one.
    spy_pct = None
    if sim.entry_date:
        spy_pct = benchmark_return(_spy_bars(), sim.entry_date, sim.exit_date)
    # The same move in THIS trade's own units, so it can sit beside
    # r_multiple_planned and be read without a second calculation. Uses the real
    # entry and risk_per_share: "what would one R of this trade have been worth
    # if the money had simply been in SPY."
    spy_r = None
    if spy_pct is not None and sim.entry and sim.risk_per_share:
        spy_r = (sim.entry * spy_pct / 100) / sim.risk_per_share

    try:
        row_id = persistence.record_shadow_outcome(
            ticker, checked_date=today, price=price,
            hypothetical_trigger_fired=metrics.hypothetical_trigger_fired,
            max_favorable_excursion=metrics.max_favorable_excursion,
            max_adverse_excursion=metrics.max_adverse_excursion,
            idea_id=idea_id,
            # Which of the thesis's two setups this row simulated, and whether
            # the simulated position still holds anything. Both are new on
            # 2026-08-30 -- see persistence.py's column comments.
            setup_side=side,
            is_fully_closed=int(sim.is_fully_closed),
            # What an idea that never fired is worth knowing -- see
            # ShadowMetrics for why these matter more than waiting longer does.
            # The grade of the trade actually on offer, beside the grade the
            # plan was written with. Two different claims; one column could
            # never say which of them failed.
            grade_at_fire=fire_grade,
            rr_at_fire=fire_rr,
            closest_approach_pct=metrics.closest_approach_pct,
            closest_approach_atr=metrics.closest_approach_atr,
            closest_approach_date=metrics.closest_approach_date,
            move_without_entry_pct=metrics.move_without_entry_pct,
            # Denormalized build-time context -- see persistence.py's own comment on
            # why these are copied rather than joined at read time.
            setup_type=plan.get("setup_type"),
            decision=thesis.get("decision"),
            rubric_grade=thesis.get("rubric_grade"),
            market_regime_at_build=thesis.get("market_regime_at_build"),
            trigger=plan.get("trigger"), stop=plan.get("stop"),
            target_1=targets[0]["price"] if len(targets) >= 1 else None,
            target_2=targets[1]["price"] if len(targets) >= 2 else None,
            atr_at_build=plan.get("atr_at_build"),
            fired_date=sim.fired_date, entry=sim.entry, entry_date=sim.entry_date,
            entry_gap_pct=sim.entry_gap_pct, risk_per_share=sim.risk_per_share,
            resolution=resolution, exit_date=sim.exit_date, exit_price=sim.exit_price,
            r_multiple_simple=sim.r_multiple_simple, r_multiple_planned=sim.r_multiple_planned,
            mfe_r=sim.mfe_r, mae_r=sim.mae_r, bars_held=sim.bars_held,
            sim_version=SIM_VERSION, sim_note=sim.note,
            spy_return_pct=spy_pct, spy_return_r=spy_r, days_to_fire=days_to_fire,
            sector=sector_map.get_sector(ticker),
            rr_at_build=rr_at_build(plan),
            stop_basis_kind=plan.get("stop_basis_kind"),
            # Did a real position get opened against THIS build -- not this
            # ticker. Reads positions.idea_id, which /filled has written since
            # 2026-08-07, so it can never confuse two builds of one symbol.
            owner_bought=int(persistence.position_exists_for_idea(idea_id)) if idea_id else None,
        )
    except sqlite3.IntegrityError:
        # One row per build per day (unique index, 2026-08-07). A scheduled run
        # that fires twice used to write the night's result twice -- and the two
        # copies did not always agree, because they fetched at different moments.
        # The first write is the night's answer; this one is a repeat, not news.
        print(f"SKIP {ticker} [{side}]: build {idea_id} already scored on {today}")
        return None
    r_text = "n/a" if sim.r_multiple_planned is None else f"{sim.r_multiple_planned:+.2f}R"
    print(f"OK {ticker} [{side}]: {resolution}, planned={r_text}, "
          f"MFE={sim.mfe_r and round(sim.mfe_r, 2)}R MAE={sim.mae_r and round(sim.mae_r, 2)}R "
          f"(shadow_outcomes id={row_id})")
    return row_id


def export_csv(path: Path, min_checked_date: Optional[str] = None) -> int:
    """Dump the shadow book to CSV so it can be taken into any real backtest
    tool -- pandas, Excel, whatever -- without anyone needing to read this
    project's SQLite schema first. One row per (ticker, run date), the same
    append-only shape the table already has, so a row's numbers can be seen
    changing as a trade plays out over successive nights."""
    import csv

    rows = persistence.get_shadow_rows(min_checked_date=min_checked_date)
    if not rows:
        print("nothing to export")
        return 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"exported {len(rows)} rows -> {path}")
    return len(rows)


# Nightly runs start after the post-close /monitorall, which can still be
# working when this fires. Waiting up to 3h is safe: this job's whole window is
# overnight, and a scan that hasn't finished in 3h has its own problem worth
# seeing in the log rather than silently overwriting.
_LOCK_WAIT_SECONDS = 3 * 60 * 60


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker", nargs="?", default=None)
    parser.add_argument("--no-lock", action="store_true",
                        help="skip the TradingView lock (manual/interactive use only -- "
                             "the scheduled run must always take it)")
    parser.add_argument("--export", metavar="PATH",
                        help="write the whole shadow book to CSV and exit -- no fetch, "
                             "no TradingView, safe to run any time")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="with --export: only rows checked on/after this date")
    args = parser.parse_args()

    if args.export:
        export_csv(Path(args.export), min_checked_date=args.since)
        return

    candidates = persistence.get_shadow_candidates(ticker=args.ticker)
    if not candidates:
        print("no candidates found" + (f" for {args.ticker}" if args.ticker else ""))
        return

    if args.no_lock:
        for thesis in candidates:
            run_one(thesis)
        return

    # Only one process can hold the TradingView bridge -- a second one dies on
    # EADDRINUSE mid-run, which would leave the night's capture half-written
    # with nothing but a stack trace to show for it. See tv_lock.py.
    with tv_lock.held(_LOCK_WAIT_SECONDS) as got_lock:
        if not got_lock:
            print(f"SKIPPED: TradingView still busy after {_LOCK_WAIT_SECONDS // 3600}h "
                  f"-- nothing scored this run (no partial writes).")
            return
        for thesis in candidates:
            run_one(thesis)


if __name__ == "__main__":
    main()
