"""Category A data-gathering for /monitor automation (2026-07-09).

Fetches the stored thesis (or None -- ad-hoc check, MONITOR_v2.md section ד) plus
live intraday (2H and 30m since open) data for the ticker, and SPY's live price for
the "is the stock leading or just riding the tape" comparison MONITOR_v2.md
requires. No judgment here: does not decide status tier, does not compare against
the trigger -- that's Category B, done by whichever Claude session (interactive or
this automation) reads the output.

Also fetches a small daily-bar window (2026-07-16) so a genuine 🟢 (real daily
close, MONITOR_v2.md) can actually be confirmed -- until this, the tool only ever
returned 2H/30m bars, so no automated check (even one run after the close) could
tell a completed daily candle apart from an inferred one, and every run capped at
🟡➕ regardless of the time of day. `daily_freshness` (same fresh/stale check as
the existing intraday `freshness` field) is what tells the reader whether
`bars_daily_recent`'s last bar is an actually-settled session close or still a
live/incomplete forming candle -- only the former may ever be treated as a real
🟢. This does NOT change who flips thesis status: deliver_monitor_report.py /
deliver_auto_monitor_report.py no longer auto-set `open_position` on green either
way (see their own docstrings) -- only `/filled` does that, after a real fill.

2026-07-22: also returns account.risk_usd (effective_equity_usd * risk_pct, same
formula fetch_analysis_data.py's own account block uses) -- a green/yellow_plus
ticker's quantity must be recomputed from this at check time, never copied from
the thesis's stale planned_qty.

2026-07-29 (rule 27, CONSISTENCY_RULES.md): also returns rubric_formula_now --
primary/alternate setups re-scored against LIVE regime/RS/ATR numbers using the
exact same rubric_formula.classify_rubric() SCREENER_v3 uses at build time, so a
setup that scored well when the thesis was built but has since decayed to D/F
gets caught here instead of silently still offering a Starter/order days later
(found real: WGMI, see MONITOR_v2.md).

2026-07-30 (full-system checkup): also returns portfolio_heat/sector_exposure/
cash_available_usd (rules 19/20/21) -- a real 🟢 buy order previously carried
none of the portfolio-wide risk disclosure a fresh screener buy already gets.

2026-07-31: `--strict-open` also fetches 5-min bars since open (`bars_5m_since_open`)
and their volume average (`avg_volume_5m_closed`, prior CLOSED bars only) -- backs
the new strict-open /monitorall scan (market open + 30min), which needs a
finer-resolution confirmation source since no 2H bar can possibly have closed yet
that early. Both fields are None on every normal (non-strict) run. See
MONITOR_v2.md's strict-open bullet for how they're used -- still no judgment here.

Usage: python bot/fetch_monitor_data.py TICKER [--strict-open]
Output: JSON to stdout.
"""

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import indicators_core as ic
import persistence
import regime_formula
import rubric_formula
import setup_types
import sector_map
from market_hours import is_premarket_now
from tv_data import (TVClient, assert_data_fresh, get_economic_calendar_cached,
                     get_index_bars, get_index_quote)


REGIME_LOOKBACK_BARS = 126  # ~6 trading months -- structure_break confirmation window (rule 23), same as fetch_analysis_data.py

# Rule 15's reversal set now comes from setup_types, once (2026-08-09) -- this
# lower-cased literal was one of three separate copies, and setup_types.canonical
# also catches the near-miss spellings this comparison used to miss.


async def _fetch(ticker: str, strict_open: bool = False):
    async with TVClient() as client:
        bars_2h = await client.get_intraday_since_open(ticker, "120")
        bars_30 = await client.get_intraday_since_open(ticker, "30")
        # strict-open scan only (2026-07-31, market-open+30min /monitorall_strict):
        # 2H bars physically cannot have closed yet this early in the session, so
        # that run needs a finer-resolution stand-in confirmation source instead --
        # see MONITOR_v2.md's strict-open bullet for exactly how these are used.
        bars_5 = await client.get_intraday_since_open(ticker, "5") if strict_open else None
        # years=0.1 (~25 trading days) -- only the last few daily bars are ever
        # needed to check "did today's real daily candle close above the trigger",
        # not a full history (that's fetch_analysis_data.py's job at build time).
        daily_bars = await client.get_daily_history(ticker, years=0.1)
        quote = await client.get_quote(ticker)
        # Disk-cached (2026-08-04): one SPY quote per /monitorall run instead of one
        # per ticker -- see get_index_quote. spy_quote_age_seconds is carried all the
        # way into the output so a cached price is never read as a live one.
        spy_quote, spy_quote_age = await get_index_quote(client, "SPY")
        # Regime re-check (rule 23/27) needs the same SPY/QQQ year-long windows
        # fetch_analysis_data.py uses at build time, so classify_regime() here can
        # never drift from that same arithmetic.
        spy_bars = await get_index_bars(client, "SPY", years=1)
        qqq_bars = await get_index_bars(client, "QQQ", years=1)
        # Cached (2026-08-04): identical for every ticker in a batch, see
        # get_economic_calendar_cached -- was one round-trip per ticker.
        economic_calendar = await get_economic_calendar_cached(client, days_ahead=10)
        # same real source (never model memory) as fetch_analysis_data.py's own
        # earnings_verified gate (output_gate.py).
        earnings = await client.get_earnings_date(ticker)
    return (bars_2h, bars_30, bars_5, daily_bars, quote, spy_quote, spy_quote_age,
            spy_bars, qqq_bars, economic_calendar, earnings)


def _regime_snapshot(bars: list[dict]) -> regime_formula.IndexSnapshot:
    c = [b["close"] for b in bars]
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    return regime_formula.IndexSnapshot(
        price=c[-1], sma20=ic.sma(c, 20), sma50=ic.sma(c, 50), sma150=ic.sma(c, 150),
        swing_highs=regime_formula.find_swing_highs(h),
        swing_lows=regime_formula.find_swing_lows(l),
        lookback_low=min(l[-REGIME_LOOKBACK_BARS:]),
    )


def _earnings_days_out(earnings: dict) -> Optional[int]:
    """Moved to indicators_core.earnings_days_out 2026-08-02 so /screener's own
    fetch uses the identical function instead of leaving the model to work the
    number out by hand. Thin wrapper kept so this module's call sites and its
    tests stay unchanged."""
    return ic.earnings_days_out(earnings)


def _setup_rs_window(setup_type: Optional[str]) -> str:
    """'rs_5d' for reversal setup types, 'rs_20d' for everything else
    (trend-following) -- SCREENER_v3.md's own RS-window rule (lines 99-102)."""
    return "rs_5d" if setup_types.is_reversal(setup_type) else "rs_20d"


def _score_setup(setup: Optional[dict], *, atr14: float, dist_sma20_atr: float,
                  regime: str, rs_5d: float, rs_20d: float, earnings_days_out: Optional[int],
                  live_close: Optional[float] = None) -> Optional[dict]:
    """Re-score one stored setup (primary_setup or alternate_setup from the
    thesis) against fresh live numbers. Returns None with a stated reason if
    the setup has no target yet to score against (not yet anchored -- e.g. an
    Alternate still pending) -- never invents a target, same "explicit null,
    stated reason" convention as the rest of this project (rule 16).

    2026-08-08 -- reward:risk is scored from the entry the user would ACTUALLY
    get, not from the stored trigger, whenever price has already closed past
    that trigger. Rule 27 exists so a decayed setup can't keep looking
    tradeable; this closes the same hole one level down, because the R:R
    criterion was still being measured on a trade that is no longer available.

    The real shape: plan is buy 100, stop 97, target 108 -- 3 of risk against 8
    of reward, a clean pass. It fires and closes at 102. The order the user is
    actually being handed is buy 102, same 97 stop: 5 of risk against 6 of
    reward, which should not pass. The stored numbers still scored 8-against-3
    and still graded B, so the order still printed. Position size was already
    recomputed from the real entry (MONITOR_v2 section ו.1), so the dollar risk
    was right -- only the QUALITY of the trade went unchecked.

    Entry becomes max(trigger, live_close), never lower: the trigger is a
    confirmation level in every setup type here (Breakout/Pullback/Reclaim/
    Retest all fire on a daily close ABOVE it, see MONITOR_v2 section ב), so
    price sitting under it means the entry hasn't happened and the stored plan
    is still exactly what would be ordered.

    live_close is the settled daily close, not the live quote, matching the
    daily_freshness.fresh gate this whole re-score already runs behind -- a
    premarket tick must not move a grade.
    """
    if not setup:
        return None
    targets = setup.get("targets") or []
    if not targets or not isinstance(targets[0], dict) or targets[0].get("price") is None:
        return {"grade": None, "score": None, "criteria": None, "reason": "no_target_to_score"}

    entry = setup.get("trigger")
    stop = setup.get("stop")
    target_price = targets[0]["price"]
    if not isinstance(entry, (int, float)) or not isinstance(stop, (int, float)):
        return {"grade": None, "score": None, "criteria": None, "reason": "no_numeric_entry_or_stop"}
    if not isinstance(target_price, (int, float)):
        return {"grade": None, "score": None, "criteria": None, "reason": "no_numeric_target"}

    # Only substitute on a normal long plan (stop below the target, which every
    # real row here is). Anything else is malformed rather than short, and gets
    # the original direction-agnostic arithmetic instead of a guess.
    planned_entry = entry
    entry_is_live = False
    if (isinstance(live_close, (int, float)) and target_price > stop
            and live_close > entry):
        entry = float(live_close)
        entry_is_live = True

    risk = abs(entry - stop)
    if entry_is_live:
        # Directional, and floored at zero: price closing at or beyond the
        # target leaves nothing to win, and abs() would have reported that as a
        # large reward. Zero reward fails the R:R and target-distance criteria,
        # which is the correct read -- there is no trade left here.
        reward = max(0.0, target_price - entry)
    else:
        reward = abs(target_price - entry)
    rr = (reward / risk) if risk else None
    target_atr_multiple = (reward / atr14) if atr14 else None
    if rr is None or target_atr_multiple is None:
        return {"grade": None, "score": None, "criteria": None, "reason": "zero_risk_or_atr"}

    rs_delta_pct = rs_5d if _setup_rs_window(setup.get("type")) == "rs_5d" else rs_20d
    result = rubric_formula.classify_rubric(rubric_formula.RubricInputs(
        rr=rr, target_atr_multiple=target_atr_multiple, regime=regime,
        rs_delta_pct=rs_delta_pct, dist_sma20_atr=dist_sma20_atr,
        earnings_days_out=earnings_days_out,
    ))
    return {"grade": result.grade, "score": result.score, "criteria": result.criteria,
            "rr": rr, "target_atr_multiple": target_atr_multiple,
            # Both entries are reported, always, so the report can say WHICH
            # price the grade was measured at. A grade that quietly changed
            # meaning is the thing rule 27 was written against.
            "planned_entry": planned_entry, "entry_used": entry,
            "entry_is_live_close": entry_is_live,
            "rs_window_used": _setup_rs_window(setup.get("type"))}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("--strict-open", action="store_true", dest="strict_open")
    args = parser.parse_args()
    ticker = args.ticker.upper()

    thesis = persistence.require_thesis_or_flag(ticker)
    (bars_2h, bars_30, bars_5, daily_bars, quote, spy_quote, spy_quote_age,
     spy_bars, qqq_bars, economic_calendar, earnings) = asyncio.run(
        _fetch(ticker, strict_open=args.strict_open)
    )
    # strict-open scan only: latest CLOSED 5m bar's volume vs the average of the
    # PRIOR closed 5m bars (excludes both the still-forming last bar and the bar
    # being tested from its own baseline -- comparing a bar against a mean that
    # already includes itself would trivially reduce to "latest >= previous bar").
    avg_volume_5m_closed = None
    if bars_5:
        closed_5m = bars_5[:-1]  # last entry may still be forming
        if len(closed_5m) >= 2:
            prior_closed = closed_5m[:-1]
            avg_volume_5m_closed = sum(b["volume"] for b in prior_closed) / len(prior_closed)
    # account.risk_usd (2026-07-22): MONITOR_v2.md ו.1/ו.3 requires a green/yellow_plus
    # ticker's quantity be recomputed from risk_usd / (entry - stop) at check time, never
    # the stale planned_qty from the original SCREENER_v3 thesis -- but this script had no
    # account data at all, so every automated /monitor and /monitorall run hit a green
    # ticker and either fabricated a number or (found real, 2026-07-22) stalled asking the
    # user a question a non-interactive claude -p run can never get answered, leaving the
    # message permanently stuck at "did not reach a terminal state". Same effective_equity_usd
    # * risk_pct formula fetch_analysis_data.py's account.risk_usd already uses -- only that
    # one figure is needed here since MONITOR_v2.md's templates don't carry SCREENER_v3's
    # broader portfolio-heat/sector/cash disclosures.
    # 2026-07-30 full-system checkup: that scope decision meant a green ticker's real buy
    # order came with none of rules 19/20/21's disclosure -- the SAME portfolio-wide risk
    # a fresh screener buy already gets checked against. Added here too, reusing the exact
    # same persistence calls fetch_analysis_data.py's account block already uses (all pure
    # DB reads, no extra network fetch) -- report_lint._lint_portfolio_heat_disclosure/
    # _lint_sector_cap_disclosure/_lint_cash_usage_disclosure only fire when the model
    # actually fills in the corresponding *_after/*_required fields on a real order, same
    # "skip silently if absent" convention as everywhere else in this project.
    account_settings = persistence.get_account_settings()
    effective_equity_usd = persistence.get_effective_equity()
    account = {
        "effective_equity_usd": effective_equity_usd,
        "risk_pct": account_settings["risk_pct"],
        "risk_usd": (effective_equity_usd * account_settings["risk_pct"]) if effective_equity_usd else None,
        "portfolio_heat": persistence.get_portfolio_heat(),
        "portfolio_heat_cap_pct": account_settings["portfolio_heat_cap_pct"],
        "sector_group": sector_map.get_sector_group(ticker),
        "sector_exposure": persistence.get_sector_exposure(),
        "sector_cap_pct": account_settings["sector_cap_pct"],
        "cash_available_usd": persistence.get_cash_available(),
        "cash_usage_warn_pct": account_settings["cash_usage_warn_pct"],
    }
    regime_result = regime_formula.classify_regime(_regime_snapshot(spy_bars), _regime_snapshot(qqq_bars))

    # item 6, Hardening Pass -- freshness of the finer-resolution intraday series
    # (30m), copy verbatim into the decision JSON. NYSE-calendar based (never
    # weekday arithmetic), same approach as fetch_analysis_data.py's daily check.
    freshness = assert_data_fresh(bars_30)
    # Same fresh/stale check applied to the daily series (2026-07-16): a daily bar
    # dated today can still be a live, still-forming candle if the session hasn't
    # actually closed yet -- daily_freshness.fresh distinguishes a real settled
    # close from that, which is exactly what gates a genuine 🟢 in MONITOR_v2.md.
    # Computed HERE, before the rubric block below (2026-07-30 full-system
    # checkup fix -- see that block's own comment for why the ordering matters).
    daily_freshness = assert_data_fresh(daily_bars)

    # Live rubric re-score inputs (2026-07-29, rule 27) -- same figures
    # fetch_analysis_data.py computes at build time, reused here so /monitor's
    # re-check can never drift from the same arithmetic. daily_bars is the
    # ticker's own recent window (years=0.15 above); spy_bars/qqq_bars are
    # already fetched at years=1 for the regime check above.
    t_c = [b["close"] for b in daily_bars]
    t_h = [b["high"] for b in daily_bars]
    t_l = [b["low"] for b in daily_bars]
    t_v = [b["volume"] for b in daily_bars]
    spy_c = [b["close"] for b in spy_bars]
    qqq_c = [b["close"] for b in qqq_bars]
    rubric_atr14 = ic.atr_wilder(t_h, t_l, t_c, period=14)
    rubric_sma20 = ic.sma(t_c, 20)
    rubric_dist_sma20_atr = (t_c[-1] - rubric_sma20) / rubric_atr14 if rubric_atr14 else None
    n_spy = min(len(t_c), len(spy_c))
    # 2026-07-30 full-system checkup: guarded exactly like the QQQ case below --
    # a thin/partial SPY fetch used to crash this with an uncaught ValueError.
    rs20_spy = ic.relative_strength(t_c[-n_spy:], spy_c[-n_spy:], 20) if n_spy >= 21 else None
    rs5_spy = ic.relative_strength(t_c[-n_spy:], spy_c[-n_spy:], 5) if n_spy >= 6 else None
    n_qqq = min(len(t_c), len(qqq_c))
    rs20_qqq = ic.relative_strength(t_c[-n_qqq:], qqq_c[-n_qqq:], 20) if n_qqq >= 21 else None
    vol_avg20 = ic.volume_average(t_v, 20)
    earnings_days_out = _earnings_days_out(earnings)

    # 2026-07-30 full-system checkup: this used to compute a real rubric score
    # off `daily_bars` regardless of daily_freshness.fresh -- rule 27's whole
    # point is a trustworthy live re-grade, but a still-forming candle's OHLC
    # can move materially before the real close, so a "live" grade built on it
    # can be quietly wrong mid-session. Gated on daily_freshness.fresh now,
    # same "explicit null, stated reason" convention _score_setup already uses
    # for "no target yet" -- never silently substitutes a real-looking grade
    # for data that isn't actually settled yet.
    rubric_formula_now = None
    if thesis and rubric_atr14 and rubric_dist_sma20_atr is not None and rs5_spy is not None and rs20_spy is not None:
        if not daily_freshness.fresh:
            _stale_reason = {"grade": None, "score": None, "criteria": None, "reason": "daily_bar_not_settled"}
            rubric_formula_now = {"primary": _stale_reason, "alternate": _stale_reason}
        else:
            rubric_formula_now = {
                "primary": _score_setup(
                    thesis.get("primary_setup"), atr14=rubric_atr14, dist_sma20_atr=rubric_dist_sma20_atr,
                    regime=regime_result.regime, rs_5d=rs5_spy.rs_delta_pct, rs_20d=rs20_spy.rs_delta_pct,
                    earnings_days_out=earnings_days_out, live_close=t_c[-1],
                ),
                "alternate": _score_setup(
                    thesis.get("alternate_setup"), atr14=rubric_atr14, dist_sma20_atr=rubric_dist_sma20_atr,
                    regime=regime_result.regime, rs_5d=rs5_spy.rs_delta_pct, rs_20d=rs20_spy.rs_delta_pct,
                    earnings_days_out=earnings_days_out, live_close=t_c[-1],
                ),
            }

    output = {
        "ticker": ticker,
        "thesis": thesis,  # None -> ad-hoc check only, MONITOR_v2.md section ד
        # 2026-08-02: how long ago this thesis's trigger first confirmed, in real
        # trading days, plus a plain stale flag at 2 days. A fired trigger used
        # to keep producing fresh-looking "🟢 trigger fired!" headlines every
        # single run until the thesis was filled or dropped -- see
        # persistence.get_trigger_fired_age for the real MSFT case. None means
        # no green has been logged for this thesis yet. Copy verbatim; never
        # recompute the age by hand.
        "trigger_fired_age": persistence.get_trigger_fired_age(ticker) if thesis else None,
        "account": account,
        "current_price": quote.get("close") or quote.get("last"),
        "spy_price": spy_quote.get("close") or spy_quote.get("last"),
        # 0 means this run fetched it; anything higher means it came from the
        # shared index cache that many tickers in one batch reuse. Never state
        # spy_price as "live" without checking this (rule 1).
        "spy_price_age_seconds": spy_quote_age,
        # 2026-07-31: current_price above can be a real premarket quote (thin
        # volume, no settled 30m/2h bars yet -- bars_2h_since_open/bars_30m_since_open
        # below are empty pre-open since get_intraday_since_open filters to
        # >= today's actual session open). Never blocks the check (a real 🟢 still
        # requires daily_freshness.fresh regardless), just tells the reader the
        # live price/status below is premarket noise, not a regular-session read.
        "is_premarket": is_premarket_now(),
        # market_regime_formula.regime is the AUTHORITATIVE current regime call
        # (rule 23) -- copy verbatim into regime_now, never re-derive or override
        # silently. See regime_formula.py's own docstring and CONSISTENCY_RULES.md
        # rule 23 for the disclosed-override exception.
        "market_regime_formula": {
            "regime": regime_result.regime,
            "score": regime_result.score,
            "structure_break_confirmed": regime_result.structure_break_confirmed,
            "components": regime_result.components,
        },
        # Live rubric re-score (rule 27) -- "primary"/"alternate" mirror the
        # thesis's own two tracked setups (MONITOR_v2.md line 11: both stay
        # tracked in parallel until one triggers). Each is either a full
        # {grade, score, criteria, ...} dict, a {"reason": ...} null-with-reason
        # (no target to score against yet), or None (no thesis / no such setup).
        # MONITOR_v2.md's gate: grade "D"/"F" here blocks the Starter/order
        # language, never the underlying trigger fact -- same posture as the
        # existing regime gate above.
        "rubric_formula_now": rubric_formula_now,
        "bars_2h_since_open": bars_2h,
        "bars_30m_since_open": bars_30,
        # strict-open scan only (2026-07-31) -- None on every normal /monitor and
        # /monitorall run. See MONITOR_v2.md's strict-open bullet for how these two
        # fields substitute for the closed-2H-bar requirement this early in the
        # session.
        "bars_5m_since_open": bars_5,
        "avg_volume_5m_closed": avg_volume_5m_closed,
        "bars_daily_recent": daily_bars[-5:],
        "freshness": {
            "fresh": freshness.fresh,
            "last_bar_date": freshness.last_bar_date,
            "most_recent_complete_session": freshness.most_recent_complete_session,
        },
        "daily_freshness": {
            "fresh": daily_freshness.fresh,
            "last_bar_date": daily_freshness.last_bar_date,
            "most_recent_complete_session": daily_freshness.most_recent_complete_session,
        },
    }
    # ensure_ascii=True (not the DB-storage convention elsewhere in this project):
    # this JSON is printed to stdout and gets echoed straight into a PowerShell
    # tool-result -- any raw non-ASCII byte (e.g. a Hebrew checkpoints[].reason
    # carried over from the stored thesis) mojibakes under the console's cp1255
    # codepage. \uXXXX-escaping keeps the printed text pure ASCII and decodes back
    # to the exact same string on json.loads -- no data loss, just no display
    # artifact for whichever session reads it (found real, 2026-07-10: an
    # automated /monitor run burned its entire turn budget on ~10 failed
    # workaround attempts chasing what it assumed was real data corruption).
    print(json.dumps(output, ensure_ascii=True, default=str))


if __name__ == "__main__":
    main()
