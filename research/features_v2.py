"""The numbers that were missing. Structure, supply, base quality, participation.

READ-ONLY over bar files. A library -- importing it computes nothing.

The first pass measured momentum, volatility and volume, found nothing, and
concluded there was nothing to find. That conclusion was not earned, because
the first pass never measured the things a trader actually looks at:

  * IS THERE ROOM. Whether price reaches +2R before -1R depends enormously on
    whether an old high is sitting half an ATR above the entry. Not one feature
    in the first 45 described what is overhead. This is the single biggest
    omission and it is measured five ways here.
  * WHAT THE BASE LOOKS LIKE. How long it built, how tight it got, whether it
    is contracting, how many times the level was tested, whether volume dried
    up into it. Every one of these is standard reading and none was present.
  * HOW THE STOCK PARTICIPATES. Up-capture and down-capture against the index
    separate a name that outruns rallies from one that merely tracks -- beta
    alone cannot see the difference.
  * HOW STRAIGHT THE TREND IS. A trend that rises in a line and one that
    staggers to the same place have the same return and are not the same trade.

Everything here is computed from bars at or before index `i`. Nothing looks
forward. The one input that is not a bar -- the plan's own stop distance -- is
passed in, because "how far to the next high, measured in this trade's own
risk" is the feature that most directly answers whether the target is even
reachable, and it cannot be built from bars alone.
"""

from __future__ import annotations

import math

import numpy as np


# --------------------------------------------------------------- swing points
def swing_highs(high, i, lookback=252, span=3):
    """Bar indexes that are the highest point within `span` bars either side.

    A plain rolling maximum cannot answer "where is the next ceiling" -- it
    returns one number and hides the shelf structure underneath it."""
    out = []
    lo = max(i - lookback, span)
    for k in range(lo, i - span + 1):
        seg = high[k - span:k + span + 1]
        if high[k] >= seg.max():
            out.append(k)
    return out


def swing_lows(low, i, lookback=252, span=3):
    out = []
    lo = max(i - lookback, span)
    for k in range(lo, i - span + 1):
        seg = low[k - span:k + span + 1]
        if low[k] <= seg.min():
            out.append(k)
    return out


# ------------------------------------------------------------- overhead supply
def room_above(high, close, vol, i, atr, risk=None, entry=None):
    """How much clear air is above, five ways.

    `risk` and `entry` are the plan's own numbers when available, which turns
    the distance into R -- the only unit in which "can this trade reach its
    target" has a yes-or-no answer."""
    f = {}
    px = entry if entry else close[i]
    sh = swing_highs(high, i)
    above = [high[k] for k in sh if high[k] > px]
    nearest = min(above) if above else None

    f["clear_air_atr"] = ((nearest - px) / atr) if nearest else 12.0
    f["has_ceiling"] = 1.0 if nearest else 0.0
    if risk and risk > 0:
        # the number that decides whether +2R is even geometrically available
        f["clear_air_r"] = ((nearest - px) / risk) if nearest else 12.0
        f["prior_high_r"] = ((high[max(i - 62, 0):i + 1].max() - px) / risk)
        f["year_high_r"] = ((high[max(i - 251, 0):i + 1].max() - px) / risk)

    for n in (63, 252):
        w = slice(max(i - n + 1, 0), i + 1)
        f[f"dist_high_{n}_atr"] = (high[w].max() - px) / atr
        # how much of the period's volume changed hands ABOVE here. Every share
        # bought higher is someone waiting to sell at break-even.
        v, c = vol[w], close[w]
        tot = v.sum()
        f[f"overhead_volume_{n}"] = (100 * v[c > px].sum() / tot) if tot > 0 else None

    f["above_everything_252"] = 1.0 if px >= close[max(i - 251, 0):i + 1].max() else 0.0
    f["n_ceilings_3atr"] = float(sum(1 for h in above if h <= px + 3 * atr))
    return f


# ------------------------------------------------------------------- the base
def base_shape(high, low, close, vol, i, atr, trigger=None):
    f = {}
    for n in (10, 20, 50):
        w = slice(max(i - n + 1, 0), i + 1)
        rng = high[w].max() - low[w].min()
        f[f"range_{n}_atr"] = rng / atr
    # contraction: is the recent range tighter than the one before it. The
    # classic tightening pattern, expressed as a plain ratio.
    a = high[max(i - 9, 0):i + 1].max() - low[max(i - 9, 0):i + 1].min()
    b = high[max(i - 19, 0):i - 9].max() - low[max(i - 19, 0):i - 9].min() \
        if i >= 19 else None
    f["contraction_10_vs_10"] = (a / b) if b and b > 0 else None
    c = high[max(i - 19, 0):i + 1].max() - low[max(i - 19, 0):i + 1].min()
    d = high[max(i - 39, 0):i - 19].max() - low[max(i - 39, 0):i - 19].min() \
        if i >= 39 else None
    f["contraction_20_vs_20"] = (c / d) if d and d > 0 else None

    # how long price has stayed inside a 3-ATR box ending here
    hi = lo = close[i]
    n = 0
    for k in range(i, max(i - 120, 0) - 1, -1):
        hi, lo = max(hi, high[k]), min(lo, low[k])
        if (hi - lo) > 3 * atr:
            break
        n += 1
    f["base_days_3atr"] = float(n)

    if trigger:
        # how many times this level was tested before it broke. A level touched
        # once is not the same trade as one touched five times.
        near = sum(1 for k in range(max(i - 59, 0), i + 1)
                   if abs(high[k] - trigger) <= 0.25 * atr)
        f["touches_of_level"] = float(near)
        f["trigger_over_range_20"] = (trigger - low[max(i - 19, 0):i + 1].min()) / atr

    # where the recent low sits relative to the recent high -- pullback depth
    w = slice(max(i - 62, 0), i + 1)
    h63 = high[w].max()
    f["pullback_from_63_high_atr"] = (h63 - close[i]) / atr
    f["days_since_63_high"] = float(i - (max(i - 62, 0) + int(np.argmax(high[w]))))
    return f


# ------------------------------------------------------------------ volume act
def volume_shape(close, vol, i):
    f = {}
    v50 = vol[max(i - 49, 0):i + 1].mean()
    if v50 <= 0:
        return f
    f["vol_dryup_10_50"] = vol[max(i - 9, 0):i + 1].mean() / v50
    f["vol_on_the_day"] = vol[i] / v50
    up = dn = 0.0
    for k in range(max(i - 19, 1), i + 1):
        if close[k] > close[k - 1]:
            up += vol[k]
        else:
            dn += vol[k]
    f["up_vol_over_down_vol_20"] = (up / dn) if dn > 0 else None
    w = slice(max(i - 19, 1), i + 1)
    big = max(range(max(i - 19, 1), i + 1), key=lambda k: vol[k])
    f["biggest_vol_day_was_up"] = 1.0 if close[big] > close[big - 1] else 0.0
    f["days_since_biggest_vol"] = float(i - big)
    return f


# --------------------------------------------------------------- trend quality
def trend_quality(close, high, low, i, atr):
    f = {}
    for n in (21, 63):
        w = close[max(i - n + 1, 0):i + 1]
        if len(w) < n or w.min() <= 0:
            continue
        x = np.arange(len(w), dtype=float)
        yv = np.log(w)
        sl, ic = np.polyfit(x, yv, 1)
        pred = sl * x + ic
        ss = ((yv - yv.mean()) ** 2).sum()
        # how STRAIGHT the move was, separately from how big it was
        f[f"trend_straightness_{n}"] = (1 - ((yv - pred) ** 2).sum() / ss) if ss > 0 else None
        f[f"trend_slope_{n}_atr"] = sl * close[i] / atr

    up = 0
    for k in range(i, max(i - 30, 1) - 1, -1):
        if close[k] > close[k - 1]:
            up += 1
        else:
            break
    f["consecutive_up_days"] = float(up)

    # Wilder's directional strength, the standard way, no shortcuts
    n = 14
    if i >= n + 1:
        pdm = ndm = tr = 0.0
        for k in range(i - n + 1, i + 1):
            up_move, dn_move = high[k] - high[k - 1], low[k - 1] - low[k]
            pdm += up_move if (up_move > dn_move and up_move > 0) else 0.0
            ndm += dn_move if (dn_move > up_move and dn_move > 0) else 0.0
            tr += max(high[k] - low[k], abs(high[k] - close[k - 1]),
                      abs(low[k] - close[k - 1]))
        if tr > 0:
            pdi, ndi = 100 * pdm / tr, 100 * ndm / tr
            f["trend_strength_adx"] = (100 * abs(pdi - ndi) / (pdi + ndi)
                                       if (pdi + ndi) > 0 else None)
            f["plus_di_minus_di"] = pdi - ndi

    # Wilder's RSI, same reason
    if i >= 15:
        gains = losses = 0.0
        for k in range(i - 13, i + 1):
            ch = close[k] - close[k - 1]
            gains += max(ch, 0.0)
            losses += max(-ch, 0.0)
        f["rsi_14"] = 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)

    w = close[max(i - 19, 0):i + 1]
    sd = w.std()
    f["bollinger_position"] = ((close[i] - w.mean()) / (2 * sd)) if sd > 0 else None
    return f


# ------------------------------------------------------- how it rides the index
def participation(s_close, m_close, s_dates, m_ix, i, n=60):
    """Up-capture and down-capture, plus the part of the move that is its own.

    Beta is one number for both directions and hides the asymmetry that matters:
    a name that gains 1.5x on index up days and loses 1.5x on down days is a
    completely different holding from one that gains 1.5x and loses 0.8x."""
    f = {}
    ra, rb = [], []
    for k in range(max(i - n + 1, 1), i + 1):
        j, jp = m_ix.get(s_dates[k]), m_ix.get(s_dates[k - 1])
        if j is None or jp is None:
            continue
        if s_close[k - 1] > 0 and m_close[jp] > 0:
            ra.append(math.log(s_close[k] / s_close[k - 1]))
            rb.append(math.log(m_close[j] / m_close[jp]))
    if len(ra) < n // 2:
        return f
    ra, rb = np.array(ra), np.array(rb)
    up, dn = rb > 0, rb < 0
    if up.sum() >= 8 and rb[up].sum() != 0:
        f["up_capture_60"] = ra[up].sum() / rb[up].sum()
    if dn.sum() >= 8 and rb[dn].sum() != 0:
        f["down_capture_60"] = ra[dn].sum() / rb[dn].sum()
    if "up_capture_60" in f and "down_capture_60" in f:
        f["capture_spread"] = f["up_capture_60"] - f["down_capture_60"]
    vb = rb.var()
    if vb > 0:
        beta = np.cov(ra, rb)[0, 1] / vb
        resid = ra - beta * rb
        f["own_volatility_60"] = resid.std() * math.sqrt(252) * 100
        tot = ra.std()
        f["own_share_of_moves"] = (resid.std() / tot) if tot > 0 else None
    return f


def calendar(date_str):
    """Plain calendar position. Cheap to compute, and if any of it matters the
    result is far more likely to be a warning about the sample than a signal."""
    import datetime
    d = datetime.date.fromisoformat(date_str)
    return {"day_of_week": float(d.weekday()),
            "month": float(d.month),
            "day_of_month": float(d.day)}


def all_of_it(bars, i, atr, s_close, s_high, s_low, s_vol, s_dates,
              spy_close, spy_ix, risk=None, entry=None, trigger=None):
    """Everything above, in one dictionary."""
    f = {}
    f.update(room_above(s_high, s_close, s_vol, i, atr, risk, entry))
    f.update(base_shape(s_high, s_low, s_close, s_vol, i, atr, trigger))
    f.update(volume_shape(s_close, s_vol, i))
    f.update(trend_quality(s_close, s_high, s_low, i, atr))
    f.update(participation(s_close, spy_close, s_dates, spy_ix, i))
    f.update(calendar(bars[i]["date"]))
    return f
