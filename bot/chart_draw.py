"""Draws a setup's trigger/stop/target/checkpoint levels directly onto the live
TradingView chart as labeled horizontal lines (2026-07-13) -- via tv_data.py's
TVClient, which itself routes through tradingview-mcp's native chart-drawing API
(not DOM/mouse simulation). Presentation only, same principle as widget_render.py:
makes no judgment calls, just turns an already-decided primary/alternate setup
dict into drawn lines. Shared by deliver_report.py (/screener) and
deliver_monitor_report.py (/monitor) so the color scheme and filtering logic
isn't duplicated between the two.

No screenshot/export step -- the whole point is the user's own live TradingView
window, not a Telegram attachment.

Expected setup shape (same as SCREENER_v3.md/persistence.save_thesis's own):
{"type": "...", "trigger": "...", "stop": ..., "atr_at_build": ...,
 "targets": [{"price": "...", "pct": "...", "atr_mult": "...", "rr": "...", "status": "pass"}],
 "checkpoints": [{"price": "...", "reason": "..."}]}
stop/target-price/checkpoint-price are drawn ONLY when they're a real number
(report_lint._clean_number() -- the existing, already-tested helper for telling
a real number apart from prose, reused here rather than reimplemented). `trigger`
gets more lenient handling (2026-07-14, by request): SCREENER_v3.md rule 14
allows a trigger to be a plain number, a price RANGE ("retest 126-128"), or a
full condition sentence with a number embedded in it ("daily close above
136.10") -- see _extract_trigger_level(). A plain number/range still goes
through the same strict _clean_number path; only the prose-with-an-embedded-
number case is a best-effort visualization convenience, never used for
arithmetic (report_lint.py's own lint checks stay untouched and strict).

2026-08-07 -- OPEN POSITIONS DRAW A DIFFERENT SET OF LINES. Until now every
caller drew the stored thesis's pre-entry plan, both Primary AND Alternate,
even for a ticker the user already holds. Found real on NOW (held since
2026-08-04 at 115.68, stop 105.02): its chart carried nine lines including a
second, LOWER stop at 100.49 belonging to the Alternate -- a trade that was
never taken. Two stop lines on one chart, only one of them real, is a
materially dangerous thing to glance at in a fast tape. Once a position is
open, the Alternate is dead and the trigger is history; what matters is the
real fill price, the live (possibly trailed) stop, and the targets not yet
sold. See _lines_for_position()/annotate_position_chart(), and clear_chart()
for the full-exit case. Which of the two line sets to draw is the CALLER's
decision (it owns the DB read) -- this module stays pure presentation and
never queries persistence itself.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report_lint import _clean_number

LINEWIDTH = 2

# How far a trigger RANGE's rectangle extends in time, so it reads as a visible
# horizontal zone rather than a sliver on a single bar. Purely a visualization
# choice, not derived from anything -- adjust freely.
_RECT_DAYS_BACK = 60
_RECT_DAYS_FORWARD = 15

# (?<![A-Za-z0-9]) / (?![A-Za-z]) exclude a number glued to a Latin letter on
# either side -- found real testing this: "reclaim SMA20" wrongly extracted 20
# (the moving-average length, not a price) and "סגירה 2H מתחת ל-183" (2H close
# below 183) wrongly grabbed 2 (from "2H") instead of the actual price 183.
# Indicator names (SMA20, EMA9) and timeframe/multiplier notation (2H, 30m,
# 0.7x) are exactly the false-positive shapes this guards against. The
# lookbehind excludes digits too, not just letters -- otherwise the regex could
# start matching mid-number (e.g. at the "0" of "SMA20", since "2" right before
# it isn't a letter), silently extracting "0" instead of correctly rejecting
# the whole glued-to-a-letter number.
_RANGE_RE = re.compile(r"(?<![A-Za-z0-9])(\d[\d,]*(?:\.\d+)?)(?![A-Za-z])\s*[-–]\s*(?<![A-Za-z0-9])(\d[\d,]*(?:\.\d+)?)(?![A-Za-z])")
_EMBEDDED_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])(\d[\d,]*(?:\.\d+)?)(?![A-Za-z])")


def _extract_trigger_level(raw) -> Optional[tuple]:
    """Returns ("price", value), ("range", low, high), or None (nothing
    extractable -- e.g. "reclaim SMA20" with no explicit number at all, still
    correctly skipped). Checked in order: exact number first (unchanged, strict
    path), then a price RANGE embedded in prose, then a single number embedded
    in prose. Range is checked before single-number so "retest 126-128" doesn't
    get mis-read as just "126"."""
    clean = _clean_number(raw)
    if clean is not None:
        return ("price", clean)
    if not isinstance(raw, str):
        return None
    text = raw.replace(",", "")
    m = _RANGE_RE.search(text)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return ("range", min(lo, hi), max(lo, hi))
    m = _EMBEDDED_NUMBER_RE.search(text)
    if m:
        return ("price", float(m.group(1)))
    return None

# Primary: solid. Alternate: dashed (visually distinct from Primary at a glance).
# Checkpoint: muted gray dashed -- informational near-miss levels, never actionable
# (SCREENER_v3.md's own vocabulary), so deliberately not styled like a real level.
PRIMARY = {"trigger": "#2962ff", "stop": "#e53935", "target": "#26a69a"}
ALTERNATE = {"trigger": "#ff9800", "stop": "#8e0000", "target": "#00897b"}
CHECKPOINT_COLOR = "#9e9e9e"

# An OPEN position (see module docstring). Stop/target deliberately keep
# PRIMARY's exact colors -- red is the stop and teal is a target whether or not
# the trade has been entered, and re-teaching that at fill time would be a
# needless trap. Only the entry line gets its own color (amber), because it is
# the one level with no equivalent on a pre-entry chart: it is not a trigger to
# wait for, it is the price already paid.
POSITION = {"entry": "#ffb300", "stop": "#e53935", "target": "#26a69a"}


# Label readability (2026-07-14, by request -- user reported the default labels
# were unreadably small/thin and scattered across the candles instead of sitting
# on the right edge like TradingView's own native price-axis tags). fontsize:14 +
# bold:true + horzLabelsAlign:"right" all CONFIRMED live via draw_get_properties
# on a real throwaway drawn shape -- both horizontal_line AND rectangle accept
# the exact same key names (horzLabelsAlign/vertLabelsAlign/fontsize/bold), which
# is NOT obvious/documented -- a first attempt using "horzAlign"/"vertAlign" for
# the rectangle was silently ignored (kept TradingView's own centered default)
# rather than erroring, so this was only caught by reading back getProperties.
#
# vertLabelsAlign is deliberately tied to `dashed` (bottom for primary/solid,
# top for alternate+checkpoint/dashed) rather than hardcoded, found real on a
# live PLTR chart 2026-07-14: a checkpoint sitting at the exact same price as
# its own setup's trigger (both legitimate, independent levels that just
# happened to coincide) rendered as unreadable smashed-together text
# ("Reclaim C.Checkpoint") because both used the same vertical offset, and
# similarly a rectangle's own bottom-anchored label landed almost exactly on
# top of the Stop line's label because the two prices were only ~0.35 apart.
# `dashed` already distinguishes primary (trigger/stop/target: solid) from
# alternate+checkpoint (dashed) in every caller below, so reusing it here to
# put them on opposite sides of their line costs nothing extra and resolves
# same-price / near-price collisions without needing real collision detection.
_LABEL_FONTSIZE = 14

def _overrides(color: str, dashed: bool = False, vert_top: Optional[bool] = None) -> str:
    """Returns the JSON STRING draw_shape's own `overrides` arg expects, for a
    horizontal_line. linecolor/linewidth/linestyle=2 (dashed) all CONFIRMED live
    (2026-07-14) via draw_get_properties on real drawn shapes -- linestyle:0 came
    back for a solid line, linestyle:2 for a dashed one, exactly as assumed.
    Only applies to horizontal_line -- a rectangle uses entirely different
    property names for color/fill (see _rect_overrides), but the SAME label
    property names (see module docstring note above).

    `vert_top` (defaults to `dashed` when omitted) is deliberately a SEPARATE
    knob from `dashed`'s linestyle -- found real, 2026-07-15, checking the live
    BTCUSD/ETHUSD charts: checkpoint lines always pass dashed=True (intentional,
    checkpoints should always render visually dashed regardless of primary vs
    alternate -- see CHECKPOINT_COLOR caller below), which before this fix also
    forced vertLabelsAlign to "top" for BOTH primary's and alternate's
    checkpoints. Since a checkpoint is frequently the SAME wall level for both
    setups (an alternate's checkpoints are often literally identical to the
    primary's), both labels landed on the same side of the same price and
    rendered as unreadable overlapping/doubled text. Trigger/stop/target never
    hit this because they already pass `dashed` (not hardcoded True) straight
    through, so primary lands bottom and alternate lands top. Callers that want
    linestyle and label-side to move together (the common case) can just omit
    vert_top; checkpoint's caller passes it explicitly so the label side still
    tracks primary-vs-alternate even though the line itself is always dashed."""
    if vert_top is None:
        vert_top = dashed
    o = {
        "linecolor": color, "linewidth": LINEWIDTH,
        "textcolor": color, "fontsize": _LABEL_FONTSIZE, "bold": True,
        "horzLabelsAlign": "right", "vertLabelsAlign": "top" if vert_top else "bottom",
    }
    if dashed:
        o["linestyle"] = 2
    return json.dumps(o)


def _rect_overrides(color: str, dashed: bool = False) -> str:
    """Returns the JSON STRING draw_shape's `overrides` arg expects, for a
    rectangle -- a DIFFERENT property set than horizontal_line for color/fill.
    Found real, 2026-07-14: passing linecolor/linewidth (the horizontal_line
    keys) to a rectangle was silently ignored -- draw_get_properties showed
    TradingView's own unrelated defaults (a cyan border, purple fill) instead of
    the requested color. Rectangles use `color` (border), `backgroundColor`
    (fill), and `textColor` (label) instead of `linecolor`/`textcolor`."""
    o = {
        "color": color, "backgroundColor": color, "textColor": color,
        "linewidth": LINEWIDTH, "transparency": 85,
        "fontsize": _LABEL_FONTSIZE, "bold": True,
        "horzLabelsAlign": "right", "vertLabelsAlign": "top" if dashed else "bottom",
    }
    if dashed:
        o["linestyle"] = 2
    return json.dumps(o)


def _lines_for_setup(setup: Optional[dict], scheme: dict, dashed: bool, prefix: str) -> list[dict]:
    """Returns a list of shape dicts for one setup dict -- trigger, stop, each
    status=="pass" target, each checkpoint. Each item is either
    {"shape": "horizontal_line", "price": float, "text": str, "overrides": ...}
    or {"shape": "rectangle", "low": float, "high": float, "text": str,
    "overrides": ...} (trigger only, when it's a price RANGE). Silently skips
    any level with nothing extractable (prose with no number at all, missing
    field, etc.) -- not an error, just nothing to draw for that one line."""
    if not setup:
        return []
    lines: list[dict] = []

    # Labeled with the setup's own type (Breakout/Retest/Pullback/Reclaim/
    # Gap-and-Hold/Failed Breakdown, per SCREENER_v3.md's vocabulary) instead of
    # a generic "Trigger" -- for a Reclaim setup this level IS "the level that
    # was reclaimed" (SCREENER_v3.md line 68), so the label should say so rather
    # than hide which kind of level this actually is. Falls back to "Trigger"
    # when type is missing (e.g. an older stored thesis).
    trigger_label = f"{prefix}{setup['type']}" if setup.get("type") else f"{prefix}Trigger"
    extracted = _extract_trigger_level(setup.get("trigger"))
    if extracted and extracted[0] == "price":
        lines.append({
            "shape": "horizontal_line", "price": extracted[1],
            "text": trigger_label, "overrides": _overrides(scheme["trigger"], dashed),
        })
    elif extracted and extracted[0] == "range":
        lines.append({
            "shape": "rectangle", "low": extracted[1], "high": extracted[2],
            "text": trigger_label, "overrides": _rect_overrides(scheme["trigger"], dashed),
        })

    stop = _clean_number(setup.get("stop"))
    if stop is not None:
        lines.append({
            "shape": "horizontal_line", "price": stop,
            "text": f"{prefix}Stop", "overrides": _overrides(scheme["stop"], dashed),
        })

    for i, t in enumerate(setup.get("targets") or [], start=1):
        if t.get("status") != "pass":
            continue
        price = _clean_number(t.get("price"))
        if price is None:
            continue
        pct = _clean_number(t.get("pct"))
        label = f"{prefix}Target {i} ({pct:.0f}%)" if pct is not None else f"{prefix}Target {i}"
        lines.append({
            "shape": "horizontal_line", "price": price,
            "text": label, "overrides": _overrides(scheme["target"], dashed),
        })

    # Only the wall's two boundary prices get drawn, not every intermediate
    # member -- SCREENER_v3.md's own wording for these is "all wall members,
    # not tested individually" (see chart_draw's own module docstring), so a
    # 12-touch wall was rendering 12 near-identical dashed lines stacked a few
    # hundred points apart for zero extra decision-relevant information (found
    # real, 2026-07-15/16: user reported the live BTCUSD/ETHUSD chart as
    # unreadable clutter). The two edges (nearest-to-price and farthest) are
    # what the report and rubric actually reason about; everything between is
    # summarized by the wall itself. `set()` also collapses accidental
    # duplicate prices within one setup's own checkpoints list.
    cp_prices = sorted({
        p for p in (_clean_number(cp.get("price")) for cp in setup.get("checkpoints") or [])
        if p is not None
    })
    for price in {cp_prices[0], cp_prices[-1]} if cp_prices else set():
        lines.append({
            "shape": "horizontal_line", "price": price,
            "text": f"{prefix}Checkpoint",
            "overrides": _overrides(CHECKPOINT_COLOR, dashed=True, vert_top=dashed),
        })

    return lines


def _position_target_lines(position: dict) -> list[dict]:
    """The target lines for an open position: every numbered tranche that still
    has shares against it, sold ones omitted.

    Source of truth is the position's own `tranche_plan` (persistence attaches
    it to every position it hands out -- indicators_core.build_tranche_plan
    over the position's stored targets and its REAL exits rows). That is the
    only structure in the system that knows a target has already been
    realized, which is exactly what a chart must not keep showing: rule 30 and
    the ASTS incident behind it (a spent tranche presented as still pending,
    then sold a second time) are the same failure in table form.

    Falls back to the entry_setup's own status=="pass" targets when there is no
    tranche plan at all (an ad-hoc position dict, e.g. from a test or a legacy
    row). That fallback cannot know what was already sold, so it is strictly a
    last resort -- never preferred over a real plan, even an empty one."""
    plan = position.get("tranche_plan") or {}
    tranches = plan.get("tranches")
    if tranches is not None:
        lines = []
        for t in tranches:
            label = (t.get("label") or "")
            if not label.startswith("target_") or t.get("status") == "filled":
                continue
            price = _clean_number(t.get("price"))
            if price is None:
                continue
            number = label.split("_", 1)[1]
            pct = _clean_number(t.get("planned_pct"))
            text = f"Target {number} ({pct:.0f}%)" if pct is not None else f"Target {number}"
            # A partially-sold tranche stays on the chart (shares are still
            # there) but says so, so its size is never read as the full one.
            if t.get("status") == "partial":
                text += " partial"
            lines.append({
                "shape": "horizontal_line", "price": price, "text": text,
                "overrides": _overrides(POSITION["target"]),
            })
        return lines

    setup = position.get("entry_setup")
    if isinstance(setup, str):
        try:
            setup = json.loads(setup)
        except (json.JSONDecodeError, TypeError):
            setup = None
    lines = []
    for i, t in enumerate((setup or {}).get("targets") or [], start=1):
        if t.get("status") != "pass":
            continue
        price = _clean_number(t.get("price"))
        if price is None:
            continue
        pct = _clean_number(t.get("pct"))
        text = f"Target {i} ({pct:.0f}%)" if pct is not None else f"Target {i}"
        lines.append({
            "shape": "horizontal_line", "price": price, "text": text,
            "overrides": _overrides(POSITION["target"]),
        })
    return lines


def _lines_for_position(position: Optional[dict]) -> list[dict]:
    """Returns the shape dicts for a ticker the user actually HOLDS -- entry,
    live stop, unsold targets. Same output contract as _lines_for_setup(), so
    annotate_* can render either list without caring which it got.

    Deliberately absent, and each for a reason:
      * the trigger -- it already happened; a level to wait for is not a fact
        about a position that is on.
      * the Alternate setup -- a backup plan only exists before entry. Drawing
        it after the fill is what put a second, wrong stop on NOW's chart (see
        module docstring).
      * checkpoints -- near-miss levels that failed the R:R bar at build time.
        They are reading material for the decision to enter, not for managing
        an entered trade, and they are the bulk of the old clutter.

    All solid: on a position chart there is no primary-vs-alternate distinction
    left to encode, so `dashed` (which _overrides also reuses to pick a label
    side) stays off for everything and every label sits below its line."""
    if not position:
        return []
    lines: list[dict] = []

    entry = _clean_number(position.get("entry_price"))
    if entry is not None:
        qty = position.get("remaining_qty")
        if qty is None:
            qty = position.get("qty")
        qty = _clean_number(qty)
        # The share count rides on the entry label rather than getting a line
        # of its own -- it is the one number that makes a chart glance answer
        # "how much is at risk here", and it costs no extra clutter.
        text = f"Entry {qty:.0f} sh" if qty else "Entry"
        lines.append({
            "shape": "horizontal_line", "price": entry, "text": text,
            "overrides": _overrides(POSITION["entry"]),
        })

    # current_stop is the live, possibly-trailed stop and is what the user is
    # actually working to; initial_stop is only a fallback for a row that
    # somehow never got one. The original-risk stop is NOT drawn alongside it:
    # two stop lines on one chart is the exact bug this whole path exists to
    # remove.
    stop = _clean_number(position.get("current_stop"))
    if stop is None:
        stop = _clean_number(position.get("initial_stop"))
    if stop is not None:
        initial = _clean_number(position.get("initial_stop"))
        text = "Stop (trailed)" if initial is not None and stop != initial else "Stop"
        lines.append({
            "shape": "horizontal_line", "price": stop, "text": text,
            "overrides": _overrides(POSITION["stop"]),
        })

    lines.extend(_position_target_lines(position))
    return lines


async def _draw_lines(client, symbol: str, lines: list[dict], timeframe: str) -> int:
    """Switch to symbol/timeframe, clear, draw. Shared by every annotate_*
    entry point below so the prepare/clear/draw order -- and the fact that
    clearing happens AFTER the symbol switch, so it only ever wipes the lines
    belonging to the symbol being redrawn -- lives in exactly one place."""
    await client.prepare_chart(symbol, timeframe)
    await client.draw_clear()
    if not lines:
        return 0

    anchor_time = int(datetime.now(timezone.utc).timestamp())
    for line in lines:
        if line["shape"] == "rectangle":
            point = {"time": anchor_time - _RECT_DAYS_BACK * 86400, "price": line["high"]}
            point2 = {"time": anchor_time + _RECT_DAYS_FORWARD * 86400, "price": line["low"]}
            await client.draw_shape(
                "rectangle", point=point, point2=point2,
                overrides=line["overrides"], text=line["text"],
            )
        else:
            await client.draw_shape(
                "horizontal_line",
                point={"time": anchor_time, "price": line["price"]},
                overrides=line["overrides"], text=line["text"],
            )
    return len(lines)


async def annotate_position_chart(client, symbol: str, position: dict,
                                   timeframe: str = "D") -> int:
    """Draws the OPEN-POSITION line set (entry / live stop / unsold targets),
    replacing whatever was on that symbol's chart before -- which, for a
    freshly-filled ticker, is the pre-entry plan this must now supersede.
    Returns the count of lines drawn, same convention as annotate_chart()."""
    return await _draw_lines(client, symbol, _lines_for_position(position), timeframe)


async def clear_chart(client, symbol: str, timeframe: str = "D") -> int:
    """Wipes a symbol's lines and draws nothing back. For a full exit: a closed
    trade must not leave entry/stop/target lines behind pretending to be a live
    plan. Always returns 0 (nothing drawn) -- the return exists only so every
    entry point in this module has the same shape."""
    return await _draw_lines(client, symbol, [], timeframe)


async def annotate_chart(client, symbol: str, primary_setup: Optional[dict],
                          alternate_setup: Optional[dict], timeframe: str = "D") -> int:
    """Switches the live chart to `symbol`/`timeframe`, clears whatever was drawn
    there before (the chart is a single shared resource reused sequentially
    across tickers -- see tv_data.py's own concurrency docstring), then draws
    every real (non-prose) level from both setups as labeled horizontal lines.
    Sequential draw_shape calls only, never concurrent, same fix #12 constraint
    tv_data.py's module docstring already documents. Returns the count of lines
    actually drawn (0 if there was nothing numeric to draw) -- purely for the
    caller's own logging, not a fatal/success signal on its own.

    PRE-ENTRY ONLY. A caller holding the ticker must use
    annotate_position_chart() instead -- see the module docstring."""
    lines = _lines_for_setup(primary_setup, PRIMARY, dashed=False, prefix="") + \
        _lines_for_setup(alternate_setup, ALTERNATE, dashed=True, prefix="Alt ")
    return await _draw_lines(client, symbol, lines, timeframe)
