"""Category A data-gathering for /screener automation (2026-07-09).

Fetches real TradingView data for a ticker + SPY/QQQ, computes every deterministic
(Category A) figure a screener/monitor analysis needs -- ATR, SMA, RS, volume,
market regime, swing highs/lows, and mechanical resistance-wall chaining (rule 11's
neighbor-to-neighbor chaining, applied here as pure arithmetic on already-identified
pivots) -- and prints it all as one JSON blob.

This script makes NO judgment calls: it does not decide which wall matters for a
specific thesis, does not classify setup type, does not pick targets. That's
Category B and stays with whichever Claude session (interactive or the automated
`claude -p` invocation this feeds) reads this output and reasons about it -- see
CLAUDE_CODE_INSTRUCTIONS.md's Category A/B split. This script is deliberately the
one, reviewed, reusable tool for the mechanical half of that split, replacing the
one-off temp scripts (_analyze_crm.py, _crm_wall_scan.py, etc.) written by hand
during interactive sessions earlier the same day.

Usage: python bot/fetch_analysis_data.py TICKER [--years N]
Output: JSON to stdout.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import indicators_core as ic
import regime_formula
from tv_data import (TVClient, assert_data_fresh, describe_history_coverage,
                     get_economic_calendar_cached, get_index_bars)
from persistence import (
    get_sleeve, get_open_position,
    get_account_settings, get_portfolio_heat, get_allocation_drift, get_sector_exposure,
    get_effective_equity, get_cash_available,
    count_trading_days, STARTER_STALE_TRADING_DAYS,
    get_recent_posts_for_ticker, get_recent_macro_posts,
)
import sector_map

N_PIVOT = 3  # bars on each side for swing high/low candidate detection (preliminary
             # filter only -- CLAUDE_CODE_INSTRUCTIONS.md's own warning against a rigid
             # N-bar rule REPLACING judgment applies; this narrows raw data to what a
             # human would visually eyeball as a peak, final wall/target judgment stays
             # with whoever reads this output


def _swing_highs(bars: list[dict], above: float) -> list[tuple[str, float]]:
    out = []
    for i in range(N_PIVOT, len(bars) - N_PIVOT):
        h = bars[i]["high"]
        if h > above and all(h > bars[i - k]["high"] for k in range(1, N_PIVOT + 1)) \
                and all(h > bars[i + k]["high"] for k in range(1, N_PIVOT + 1)):
            date = datetime.fromtimestamp(bars[i]["time"], tz=timezone.utc).date().isoformat()
            out.append((date, h))
    out.sort(key=lambda x: x[1])
    return out


def _swing_lows(bars: list[dict]) -> list[tuple[str, float]]:
    out = []
    for i in range(N_PIVOT, len(bars) - N_PIVOT):
        l = bars[i]["low"]
        if all(l < bars[i - k]["low"] for k in range(1, N_PIVOT + 1)) \
                and all(l < bars[i + k]["low"] for k in range(1, N_PIVOT + 1)):
            date = datetime.fromtimestamp(bars[i]["time"], tz=timezone.utc).date().isoformat()
            out.append((date, l))
    return out


def _classify_touch(bar: dict, level: float, tol: float) -> Optional[str]:
    """Pure arithmetic classification of how a single candle interacts with a fixed
    price level -- given a bar and a level, there is one correct answer, no judgment
    about significance involved (same posture as _chain_walls' own tolerance test).
    'body_cut': the candle's open/close body straddles the level (a close-based
    violation, the strongest form). 'wick_cut': the level sits inside the bar's
    high/low range but the body didn't cross it (a wick-only poke). 'touch': high or
    low landed within tolerance of the level without crossing it. None: no interaction."""
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    body_lo, body_hi = min(o, c), max(o, c)
    if body_lo < level < body_hi:
        return "body_cut"
    if l < level < h:
        return "wick_cut"
    if abs(h - level) <= tol or abs(l - level) <= tol:
        return "touch"
    return None


def _touches_since_formation(bars: list[dict], wall: dict, atr: float) -> dict:
    """Counts body_cut/wick_cut/touch occurrences against a wall's own top price,
    for bars after the wall's last chaining pivot -- how price has behaved around
    the level SINCE it formed, which _chain_walls' pivot-clustering alone doesn't
    capture. Same tolerance convention as _chain_walls (max(3%, 0.5x ATR14)). Pure
    counting, no scoring/weighting -- the LLM decides what the counts mean, same
    'preliminary filter only' posture as _chain_walls itself (see module docstring).

    Uses max() over touch dates, NOT touches[-1] -- _swing_highs sorts its output by
    PRICE (line 58: `out.sort(key=lambda x: x[1])`), and _chain_walls consumes that
    price-sorted list directly, so a wall's `touches` list is in price order, not
    chronological order. touches[-1] is "the highest-priced touch in this cluster,"
    which is NOT reliably "the most recent one" -- using it as the formation date
    silently mis-dated walls whenever the highest-priced touch wasn't also the
    latest-in-time one."""
    if not wall["touches"]:
        return {"body_cut": 0, "wick_cut": 0, "touch": 0}
    last_touch_date = max(t["date"] for t in wall["touches"])
    level = wall["top"]
    tol = max(level * 0.03, 0.5 * atr)
    counts = {"body_cut": 0, "wick_cut": 0, "touch": 0}
    for b in bars:
        bar_date = datetime.fromtimestamp(b["time"], tz=timezone.utc).date().isoformat()
        if bar_date <= last_touch_date:
            continue
        kind = _classify_touch(b, level, tol)
        if kind:
            counts[kind] += 1
    return counts


def _chain_walls(highs: list[tuple[str, float]], atr: float) -> list[dict]:
    """Rule 11's neighbor-to-neighbor chaining, pure arithmetic given already-sorted
    swing highs -- tolerance = max(3%, 0.5x ATR14). 3+ chained = wall."""
    if not highs:
        return []
    chains, cur = [], [highs[0]]
    for i in range(1, len(highs)):
        prev_p = cur[-1][1]
        this_p = highs[i][1]
        tol = max(prev_p * 0.03, 0.5 * atr)
        if this_p - prev_p <= tol:
            cur.append(highs[i])
        else:
            chains.append(cur)
            cur = [highs[i]]
    chains.append(cur)
    return [
        # "bottom"/"top" are the LOWEST/HIGHEST price among the RESISTANCE touches
        # chained into this wall -- both are highs, not a support/base level. A
        # chain can drift across years transitively (each link only needs to be
        # within tolerance of its neighbor), so "bottom" can sit far below "top"
        # with no real structure in between. Never use "bottom" as a stop-basis or
        # a measured-move base -- that was a real bug in an early backtest script
        # (see PASS 3, glittery-jumping-pancake.md), caught by an all-zero-trades
        # sanity check. For a real base/stop, use a recent SWING LOW instead
        # (_swing_lows), not this field.
        {"is_wall": len(c) >= 3, "touches": [{"date": d, "price": p} for d, p in c],
         "bottom": c[0][1], "top": c[-1][1]}
        for c in chains
    ]


async def _fetch_all(ticker: str, years: float) -> dict:
    async with TVClient() as client:
        t_bars = await client.get_daily_history(ticker, years=years)
        t_quote = await client.get_quote(ticker)
        spy_bars = await get_index_bars(client, "SPY", years=1)
        qqq_bars = await get_index_bars(client, "QQQ", years=1)
        # Hardening Pass item 5's gap, closed 2026-07-26: real earnings date (never
        # from model memory) + upcoming CPI/PPI/NFP/FOMC macro events (STRATEGY_v3.md
        # section a). Neither needs the chart itself, so cheap to always include.
        earnings = await client.get_earnings_date(ticker)
        economic_calendar = await get_economic_calendar_cached(client, days_ahead=10)
    return {
        "ticker_bars": t_bars, "ticker_quote": t_quote, "spy_bars": spy_bars, "qqq_bars": qqq_bars,
        "earnings": earnings, "economic_calendar": economic_calendar,
    }


REGIME_LOOKBACK_BARS = 126  # ~6 trading months -- structure_break confirmation window (rule 23)


def _with_starter_staleness(open_position: Optional[dict]) -> Optional[dict]:
    """Folds in days_since_starter/starter_stale for an entry_type='starter'
    position -- real NYSE trading-day count (count_trading_days, same helper
    /pending's days_pending already uses), not naive weekday arithmetic.
    entry_date is never touched by add_to_position() (see persistence.py), so
    it stays accurate as "when did the starter fill happen" even across later
    adds. This is the only Category A piece of the starter-confirmation
    feature -- whether the position is actually confirmed for a full add is a
    Category B judgment (comparing entry_setup.trigger against recent_bars_40/
    freshness), left to STRATEGY_v3.md's claude -p session, same split
    fetch_monitor_data.py already applies to MONITOR_v2's own 🟢 check."""
    if not open_position or open_position.get("entry_type") != "starter":
        return open_position
    today = datetime.now(timezone.utc).date().isoformat()
    days_since_starter = count_trading_days(open_position["entry_date"][:10], today)
    open_position["days_since_starter"] = days_since_starter
    open_position["starter_stale"] = days_since_starter >= STARTER_STALE_TRADING_DAYS
    return open_position


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("--years", type=float, default=5)
    args = parser.parse_args()
    ticker = args.ticker.upper()

    raw = asyncio.run(_fetch_all(ticker, args.years))
    t_bars = raw["ticker_bars"]
    h = [b["high"] for b in t_bars]
    l = [b["low"] for b in t_bars]
    c = [b["close"] for b in t_bars]
    v = [b["volume"] for b in t_bars]
    spy_c = [b["close"] for b in raw["spy_bars"]]
    qqq_c = [b["close"] for b in raw["qqq_bars"]]

    atr14 = ic.atr_wilder(h, l, c, period=14)
    sma20, sma50, sma150 = ic.sma(c, 20), ic.sma(c, 50), ic.sma(c, 150)
    current = c[-1]

    # get_daily_history's own docstring: `years` is a soft target, not a guarantee --
    # ticker and index bar counts can differ even when both request the same years,
    # since TradingView returns whatever's already buffered. Trim to the common
    # trailing window before comparing so a length mismatch never crashes the run --
    # both series are daily bars ending "today", so the last n of each are the same
    # calendar window.
    # 2026-07-30 full-system checkup: rs20_spy/rs5_spy used to call
    # relative_strength() unconditionally -- a thin/partial SPY fetch (e.g. a
    # bad cache entry, same class of bug the n_qqq guard two lines below was
    # already written to prevent) raises an uncaught ValueError here and takes
    # down the ENTIRE screener run for whatever ticker happens to run at that
    # moment, since every /screener call needs this same shared SPY data.
    # Guarded exactly like the QQQ case: None (not a crash) when too few bars.
    n_spy = min(len(c), len(spy_c))
    rs20_spy = ic.relative_strength(c[-n_spy:], spy_c[-n_spy:], 20) if n_spy >= 21 else None
    rs5_spy = ic.relative_strength(c[-n_spy:], spy_c[-n_spy:], 5) if n_spy >= 6 else None
    n_qqq = min(len(c), len(qqq_c))
    rs20_qqq = ic.relative_strength(c[-n_qqq:], qqq_c[-n_qqq:], 20) if n_qqq >= 21 else None

    vol_avg20 = ic.volume_average(v, 20)

    spy_sma20, spy_sma50, spy_sma150 = ic.sma(spy_c, 20), ic.sma(spy_c, 50), ic.sma(spy_c, 150)
    qqq_sma20, qqq_sma50, qqq_sma150 = ic.sma(qqq_c, 20), ic.sma(qqq_c, 50), ic.sma(qqq_c, 150)

    # Numeric regime formula (2026-07-20, CONSISTENCY_RULES.md rule 23) -- replaces
    # the old 3-bucket price-vs-SMA20/50 heuristic with the full 6-way mechanical
    # score. See regime_formula.py's own module docstring for why this exists and
    # what it deliberately does NOT do (override for a real-world reason not
    # visible in the price data -- that stays a disclosed, written-down exception,
    # never a silent substitution).
    spy_h, spy_l = [b["high"] for b in raw["spy_bars"]], [b["low"] for b in raw["spy_bars"]]
    qqq_h, qqq_l = [b["high"] for b in raw["qqq_bars"]], [b["low"] for b in raw["qqq_bars"]]
    spy_snapshot = regime_formula.IndexSnapshot(
        price=spy_c[-1], sma20=spy_sma20, sma50=spy_sma50, sma150=spy_sma150,
        swing_highs=regime_formula.find_swing_highs(spy_h),
        swing_lows=regime_formula.find_swing_lows(spy_l),
        lookback_low=min(spy_l[-REGIME_LOOKBACK_BARS:]),
    )
    qqq_snapshot = regime_formula.IndexSnapshot(
        price=qqq_c[-1], sma20=qqq_sma20, sma50=qqq_sma50, sma150=qqq_sma150,
        swing_highs=regime_formula.find_swing_highs(qqq_h),
        swing_lows=regime_formula.find_swing_lows(qqq_l),
        lookback_low=min(qqq_l[-REGIME_LOOKBACK_BARS:]),
    )
    regime_result = regime_formula.classify_regime(spy_snapshot, qqq_snapshot)

    highs_above = _swing_highs(t_bars, current)
    lows_all = _swing_lows(t_bars)
    wall_chains = _chain_walls(highs_above, atr14)
    for wall in wall_chains:
        wall["touches_since_formation"] = _touches_since_formation(t_bars, wall, atr14)

    sar_points = ic.parabolic_sar(h, l)
    parabolic_sar = {"value": sar_points[-1].sar, "trend": sar_points[-1].trend}

    coverage = describe_history_coverage(t_bars, requested_years=args.years)
    freshness = assert_data_fresh(t_bars)  # item 6, Hardening Pass -- copy verbatim into the decision JSON

    # Portfolio-level risk disclosure (2026-07-18): DEFAULT_RISK_USD was never
    # set and /setrisk was never wired, so real position sizing has never
    # actually been risk-based in practice -- this makes the real numbers
    # (risk_usd, current portfolio heat, allocation drift) available directly,
    # same principle as folding in sleeve/open_position above: the automated
    # /screener session has no permitted way to query these tables itself.
    # Every figure here is informational only -- nothing in this system gates
    # a decision on it, per explicit user direction; the model's job is only
    # to disclose it prominently, never to block on it.
    account_settings = get_account_settings()
    equity_usd = account_settings["equity_usd"]
    effective_equity_usd = get_effective_equity()
    account = {
        "equity_usd": equity_usd,  # raw broker total, as last set/detected -- never use directly for sizing
        "pending_withdrawal_usd": account_settings["pending_withdrawal_usd"],  # see /withdraw
        "effective_equity_usd": effective_equity_usd,  # equity_usd minus pending_withdrawal_usd --
                                                          # THIS is the number every risk/sizing calc uses
        "risk_pct": account_settings["risk_pct"],
        "risk_usd": (effective_equity_usd * account_settings["risk_pct"]) if effective_equity_usd else None,
        "portfolio_heat": get_portfolio_heat(),
        "allocation_drift": get_allocation_drift(),
        # Sector/correlation-group cap (rule 20): this ticker's own group (None
        # if unmapped -- sector_map.py's own docstring covers why that's never
        # guessed), and the CURRENT exposure by group before this trade -- the
        # model adds its own prospective risk_usd to compute the after-trade
        # %, same pattern as portfolio_heat_after above (this script can't
        # precompute that itself since the trigger/stop aren't chosen yet).
        "sector_group": sector_map.get_sector_group(ticker),
        "sector_exposure": get_sector_exposure(),
        "sector_cap_pct": account_settings["sector_cap_pct"],
        # Cash-vs-risk disclosure (2026-07-19): a tight-stop trade sized to the
        # full risk_usd target can cost far more in actual dollars than a
        # wide-stop one for the identical risk figure -- this is what bounds
        # DOLLARS, distinct from portfolio_heat which bounds RISK. The model
        # computes this trade's own cash_required_usd (qty * entry) itself,
        # same pattern as portfolio_heat_after/sector_pct_after above.
        "cash_available_usd": get_cash_available(),
        "cash_usage_warn_pct": account_settings["cash_usage_warn_pct"],
    }

    recent_bars = [
        {"date": datetime.fromtimestamp(b["time"], tz=timezone.utc).date().isoformat(),
         "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"], "volume": b["volume"]}
        for b in t_bars[-40:]
    ]

    output = {
        "ticker": ticker,
        # get_sleeve() folded in here rather than requiring a separate call --
        # bot/sleeve.py is a plain Python function (persistence.get_sleeve), not an
        # invokable script, so /playbook's automated claude -p pipeline had no
        # permitted way to call it on its own (found real, 2026-07-10: this
        # contributed to /playbook never reaching delivery -- STRATEGY_v3.md's own
        # "use get_sleeve, never invent" rule pointed at something the scoped
        # --allowed-tools list couldn't actually run).
        "sleeve": get_sleeve(ticker),
        # Real, already-documented position data if this system filled it through
        # /filled -- null otherwise. Folded in here 2026-07-14 for the exact same
        # reason get_sleeve() was folded in above: /playbook's scoped --allowed-tools
        # list has no permitted way to query the positions table on its own, so
        # without this every real open position was being treated as "undocumented"
        # and getting a brand-new stop reinvented from scratch each run instead of
        # trailed from its actual entry_setup/current_stop (see
        # persistence.update_current_stop's docstring for the bug this caused).
        "open_position": _with_starter_staleness(get_open_position(ticker)),
        "account": account,
        "current_price": current,
        "atr14": atr14,
        "atr14_pct": atr14 / current * 100,
        "parabolic_sar": parabolic_sar,
        "sma20": sma20, "sma50": sma50, "sma150": sma150,
        # 2026-07-30 full-system checkup: a completely flat/halted ticker (zero
        # true range over the whole ATR window) has atr14 == 0, which crashed
        # this line with an uncaught ZeroDivisionError -- the identical
        # calculation in fetch_monitor_data.py was already guarded, this one
        # wasn't. None (not a crash) when there's no real ATR to divide by.
        "dist_sma20_atr": (current - sma20) / atr14 if atr14 else None,
        "rs_20d_vs_spy": rs20_spy.rs_delta_pct if rs20_spy else None,
        "rs_5d_vs_spy": rs5_spy.rs_delta_pct if rs5_spy else None,
        "rs_20d_vs_qqq": rs20_qqq.rs_delta_pct if rs20_qqq else None,
        "volume_avg20": vol_avg20, "volume_last": v[-1],
        "volume_pct_of_avg": v[-1] / vol_avg20 * 100,
        # market_regime_formula.regime is the AUTHORITATIVE regime call (rule 23) --
        # copy the "regime" value verbatim into the decision JSON's market_regime
        # field, never re-derive or override it silently. See regime_formula.py's
        # own docstring and CONSISTENCY_RULES.md rule 23 for the disclosed-override
        # exception (a real reason, written down, never a silent substitution).
        "market_regime_formula": {
            "regime": regime_result.regime,
            "score": regime_result.score,
            "structure_break_confirmed": regime_result.structure_break_confirmed,
            "components": regime_result.components,
        },
        "spy": {"price": spy_c[-1], "sma20": spy_sma20, "sma50": spy_sma50, "sma150": spy_sma150},
        "qqq": {"price": qqq_c[-1], "sma20": qqq_sma20, "sma50": qqq_sma50, "sma150": qqq_sma150},
        "swing_highs_above_current": [{"date": d, "price": p} for d, p in highs_above],
        "wall_chains": wall_chains,
        "swing_lows_recent": [{"date": d, "price": p} for d, p in lows_all[-10:]],
        "recent_bars_40": recent_bars,
        # X/Twitter feed context (2026-07-22, bot/fetch_x_feed.py's x_posts table) --
        # real fetched tweet text, not the model's own assumed knowledge. Informational
        # only, same posture as every other field here: it may inform a disclosed
        # regime-override reason (CONSISTENCY_RULES.md), never justify skipping a rule.
        "recent_related_posts": get_recent_posts_for_ticker(ticker, hours=24),
        "recent_macro_posts": get_recent_macro_posts(hours=6),
        # Real, fetched calendar data (2026-07-26) -- the only permitted source for
        # output_gate.classify_output(earnings_verified=...) and STRATEGY_v3.md
        # section a's "CPI, NFP, FOMC" event check. `earnings.next_earnings_date`
        # null or `earnings.error` set means genuinely not found on TradingView --
        # never substitute a guessed/remembered date in that case.
        "earnings": raw["earnings"],
        # Computed here, not left to the report-writing session (2026-08-02).
        # fetch_monitor_data.py already derived days-out this way and fed it
        # straight into the rubric; /screener did not, so criterion 6 kept
        # scoring 0 on "unverified" for tickers whose real earnings date was
        # sitting right there in the block above. Copy these two verbatim into
        # the decision JSON -- never re-derive by hand, same rule as
        # market_regime_formula and rubric_formula_now.
        "earnings_days_out": ic.earnings_days_out(raw["earnings"]),
        "earnings_verified": ic.earnings_is_verified(raw["earnings"]),
        "economic_calendar_upcoming": raw["economic_calendar"],
        "coverage": coverage,
        "freshness": {
            "fresh": freshness.fresh,
            "last_bar_date": freshness.last_bar_date,
            "most_recent_complete_session": freshness.most_recent_complete_session,
        },
    }
    # ensure_ascii=True: this prints straight into a PowerShell tool-result, and a
    # raw non-ASCII byte mojibakes under the console's cp1255 codepage even though
    # the data itself is fine -- see fetch_monitor_data.py's identical note (found
    # real, 2026-07-10, on the /monitor pipeline; applying the same guard here
    # pre-emptively since this script's output is consumed the same way).
    print(json.dumps(output, ensure_ascii=True))


if __name__ == "__main__":
    main()
