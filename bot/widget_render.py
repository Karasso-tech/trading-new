"""Renders SCREENER_v3/MONITOR_v2/STRATEGY_v3 structured output into the
widget_template.html dashboard and screenshots it to a PNG for Telegram delivery
(MASTER_VERIFICATION_CHECKLIST.md Section 4). Adapter only -- no protocol/trading
logic here, just structured-dict -> WIDGET_DATA mapping and the Playwright screenshot
mechanics. The structured dict itself is still produced by whoever did the actual
analysis (the bot's own pipeline, or a direct live analysis) -- this module never
invents a number, it only reformats ones already decided.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = PROJECT_ROOT / "widget_template.html"

# SCREENER_v3.md's four decision values map directly onto the widget's own labels.
_SCREENER_DECISION_MAP = {
    "Buy Now": "buy_now",
    "Buy Only If Confirmed": "buy_confirmed",
    "Watchlist": "watchlist",
    "No Trade": "no_trade",
}

# MONITOR_v2.md's four statuses mapped onto the same widget decision-badge palette --
# green=triggered/ready (buy_now), yellow_plus=2H-confirmed-not-daily (buy_confirmed),
# yellow=still waiting (watchlist), red=invalidated (no_trade).
_MONITOR_STATUS_MAP = {
    "green": "buy_now",
    "yellow_plus": "buy_confirmed",
    "yellow": "watchlist",
    "red": "no_trade",
}

# 2026-07-30 full-system checkup: the widget used to print the raw internal
# status code ("yellow_plus") straight into the metrics row -- readable only
# to someone who knows the code names. Same five statuses/wording as
# MONITOR_v2.md's own status legend (⚪/🟡/🟡➕/🟢/🔴).
_MONITOR_STATUS_LABEL_HE = {
    "white": "⚪ עדיין רחוק",
    "yellow": "🟡 נבדק, לא סגור",
    "yellow_plus": "🟡➕ אישור 2H",
    "green": "🟢 הופעל",
    "red": "🔴 נפסל",
}

# STRATEGY_v3.md's fixed action set mapped onto the same palette for a quick-glance badge.
_STRATEGY_ACTION_MAP = {
    "Add Only If Confirmed": "buy_confirmed",
    "Hold": "watchlist",
    "Hold With Alert": "watchlist",
    "Trim": "watchlist",
    "Sell Partial": "watchlist",
    "Protect With Stop": "watchlist",
    "Exit": "no_trade",
    "No Action": "watchlist",
}


def render_widget_png(widget_data: dict, viewport_width: int = 960) -> bytes:
    """Injects widget_data into widget_template.html, renders it in headless Chromium,
    and screenshots the .wrap element specifically (not the full page) for a tightly
    cropped image -- exactly the pipeline MASTER_VERIFICATION_CHECKLIST.md Section 4
    specifies."""
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    injected = html.replace(
        '<div class="wrap" id="app"></div>',
        '<div class="wrap" id="app"></div>\n<script>window.WIDGET_DATA = '
        + json.dumps(widget_data, ensure_ascii=False) + ";</script>",
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": viewport_width, "height": 800})
        page.set_content(injected)
        page.wait_for_selector("#app > *", timeout=10_000)
        wrap = page.query_selector(".wrap")
        png_bytes = wrap.screenshot()
        browser.close()
    return png_bytes


def _setup_to_rows(setup: dict) -> list[dict]:
    """2026-07-30 full-system checkup: this used to also look for "target"/"r_r"
    (singular) keys -- no producer anywhere ever writes those; every real setup
    dict only ever has a "targets" list, rendered separately by
    _setup_to_targets() below. Removed as dead code that could never populate."""
    rows = []
    for label, key in (("טריגר", "trigger"), ("סטופ", "stop")):
        val = setup.get(key)
        if val is None:
            continue
        rows.append({"label": label, "value": str(val)})
    if setup.get("verdict"):
        rows.append({"label": "הערה", "value": str(setup["verdict"])})
    return rows


def _setup_to_targets(setup: dict) -> list[dict]:
    """B4 fix: the mandatory 40%/60% (or 40/35/25) allocation table and the
    Checkpoint list (CONSISTENCY_RULES.md rules 7/14) already exist in full in every
    structured report -- this adapter just never read them. Without this, relying on
    the rendered widget instead of the full report made that allocation split and the
    Checkpoint list invisible. Passes through as-is; this module still never computes
    or invents a target/percentage, only reformats what the analysis already decided."""
    targets = setup.get("targets")
    if not targets:
        return []
    return [
        {
            "price": str(t.get("price", "")),
            "pct": t.get("pct"),
            "atr_mult": t.get("atr_mult"),
            "rr": t.get("rr"),
            "status": t.get("status"),
        }
        for t in targets
    ]


def _setup_to_checkpoints(setup: dict) -> list[dict]:
    checkpoints = setup.get("checkpoints")
    if not checkpoints:
        return []
    return [
        {"price": str(c.get("price", "")), "reason": c.get("reason", "")}
        for c in checkpoints
    ]


_REVERSAL_SETUP_TYPES = {"Reclaim", "Failed Breakdown", "Gap-and-Hold"}


def screener_node_to_widget_data(ticker: str, node: dict, date: str, exchange: str = "") -> dict:
    """One ticker's entry from a run_screener() structured dict -> WIDGET_DATA.

    A2 fix: the old version filled all 4 metric slots with ATR + SMA20/50/150 before
    RS was ever appended, so `metrics[:4]` silently truncated RS -- the single most
    corrected metric in this project's history -- out of the compact view essentially
    every time. RS now gets a guaranteed slot: SMA20/50/150 are consolidated into one
    "distance from SMA20" row (the full breakdown still lives in the PDF/report),
    which structurally leaves room for RS. Reversal setups (Reclaim/Failed
    Breakdown/Gap-and-Hold) show BOTH the 20d and 5d RS windows, since a fixed 20-day
    window structurally penalizes a reversal thesis still carrying its own crash --
    a reversal widget with only one RS number is itself an incomplete render."""
    decision = _SCREENER_DECISION_MAP.get(node.get("decision"), "watchlist")
    primary_type = (node.get("primary_setup") or {}).get("type", "")
    is_reversal = primary_type in _REVERSAL_SETUP_TYPES

    metrics = []
    if node.get("atr14_at_build") is not None:
        pct = node.get("atr14_pct")
        val = str(node["atr14_at_build"]) + (f" ({pct}%)" if pct is not None else "")
        metrics.append({"label": "ATR14", "value": val})

    if node.get("dist_sma20_atr") is not None:
        metrics.append({"label": "מול SMA20", "value": f"{node['dist_sma20_atr']}x ATR"})
    elif node.get("sma20") is not None:
        metrics.append({"label": "SMA20", "value": str(node["sma20"])})

    if node.get("rs_vs_spy_20d") is not None:
        metrics.append({"label": "RS 20d vs SPY", "value": f"{node['rs_vs_spy_20d']}%"})
    if is_reversal and node.get("rs_vs_spy_5d") is not None:
        metrics.append({"label": "RS 5d vs SPY", "value": f"{node['rs_vs_spy_5d']}%"})
    elif node.get("rs_vs_qqq_20d") is not None:
        metrics.append({"label": "RS 20d vs QQQ", "value": f"{node['rs_vs_qqq_20d']}%"})

    setups = []
    for role, key in (("primary", "primary_setup"), ("alternate", "alternate_setup")):
        setup = node.get(key)
        if not setup:
            continue
        setups.append({
            "role": role,
            "title": f"{'Primary' if role == 'primary' else 'Alternate'} -- {setup.get('type', '')}",
            "rows": _setup_to_rows(setup),
            "targets": _setup_to_targets(setup),
            "checkpoints": _setup_to_checkpoints(setup),
        })

    result = {
        "ticker": ticker, "exchange": exchange, "timeframe": "Daily", "date": date,
        "decision": decision, "grade": node.get("grade"),
        "metrics": metrics[:4],
        "setups": setups,
        "verdict": node.get("verdict") or node.get("note") or "",
    }
    # Rule 17 fix: the widget template previously had no field for this at all -- only
    # the text-report side had been fixed after the earlier MU/ONDS/ANET miss. Passed
    # through whenever the caller computed one (via indicators_core.measured_move_target),
    # never computed here -- this module only reformats numbers already decided.
    if node.get("potential") is not None:
        result["potential"] = {"value": str(node["potential"]), "note": node.get("potential_note", "")}
    return result


def monitor_node_to_widget_data(ticker: str, node: dict, date: str) -> dict:
    """MONITOR_v2's per-ticker check -> WIDGET_DATA."""
    status = node.get("status", "yellow")
    decision = _MONITOR_STATUS_MAP.get(status, "watchlist")
    order = node.get("order") or {}
    rows = []
    for label, key in (("סוג פקודה", "type"), ("מחיר", "price"), ("סטופ", "stop"), ("כמות", "qty")):
        if order.get(key) is not None:
            rows.append({"label": label, "value": str(order[key])})

    return {
        "ticker": ticker, "exchange": "", "timeframe": "2H/30m", "date": date,
        "decision": decision, "grade": None,
        "metrics": [{"label": "סטטוס", "value": _MONITOR_STATUS_LABEL_HE.get(status, status)}],
        "setups": [{"role": "primary", "title": "פקודה" if order else "אין פקודה מוכנה", "rows": rows}] if rows else [],
        "verdict": node.get("note") or "",
    }


def strategy_node_to_widget_data(ticker: str, node: dict, date: str) -> dict:
    """STRATEGY_v3's per-position management entry -> WIDGET_DATA."""
    action = node.get("action", "No Action")
    decision = _STRATEGY_ACTION_MAP.get(action, "watchlist")
    metrics = []
    for k, label in (("qty", "כמות"), ("avg_cost", "ממוצע"), ("current_stop", "סטופ נוכחי")):
        if node.get(k) is not None:
            metrics.append({"label": label, "value": str(node[k])})

    return {
        "ticker": ticker, "exchange": "", "timeframe": "Daily", "date": date,
        "decision": decision, "grade": None,
        "metrics": metrics[:4],
        "setups": [{"role": "primary", "title": action, "rows": []}],
        "verdict": node.get("note") or node.get("reason") or "",
    }


_PENDING_STATUS_EMOJI = {
    "yellow_plus": "🟡➕", "yellow": "🟡", "white": "⚪", "green": "🟢", "red": "🔴",
}


def pending_aggregate_to_widget_data(rows: list[dict], date: str) -> dict:
    """/pending's multi-ticker aggregate -> WIDGET_DATA. No existing adapter handles
    more than one ticker (screener/monitor/strategy are all single-ticker) -- this
    reuses the same setups[]-card layout, one card per pending ticker, already
    sorted by persistence.get_pending_report_rows()'s pending_sort_key(). Never
    re-sorts here -- the sort order is the whole point of /pending and must come
    from the one place it's defined."""
    flagged_count = sum(1 for r in rows if r.get("flag"))
    metrics = [
        {"label": "סה\"כ ממתינים", "value": str(len(rows))},
        {"label": "מסומנים", "value": str(flagged_count), "status": "fail" if flagged_count else "pass"},
    ]

    setups = []
    for r in rows:
        status = r.get("latest_status") or "white"
        emoji = _PENDING_STATUS_EMOJI.get(status, "⚪")
        # 2026-07-30 full-system checkup: "red" (invalidated) used to fall into
        # the same "alternate" (blue) bucket as a plain "white" ticker -- it
        # rendered as an ordinary-looking blue card instead of standing out as
        # dead, even though widget_template.html already has a dedicated red
        # "no_trade" role for exactly this. green/yellow_plus/yellow (still
        # live) stay in the green "primary" bucket; only red gets its own.
        if status == "red":
            role = "no_trade"
        elif status in ("yellow_plus", "yellow", "green"):
            role = "primary"
        else:
            role = "alternate"
        setup_rows = []
        if r.get("days_pending") is not None:
            setup_rows.append({"label": "ימי Pending", "value": str(r["days_pending"])})
        if r.get("latest_price") is not None:
            setup_rows.append({"label": "מחיר", "value": str(r["latest_price"])})
        if r.get("latest_distance_atr") is not None:
            setup_rows.append({"label": "מרחק מהטריגר (ATR)", "value": f"{r['latest_distance_atr']}x"})
        if r.get("flag_reasons"):
            setup_rows.append({
                "label": "דגל", "value": ", ".join(r["flag_reasons"]),
                "status": "fail",
            })
        setups.append({
            "role": role,
            "title": f"{r['ticker']} — {emoji}",
            "rows": setup_rows,
        })

    return {
        "ticker": "Pending", "exchange": "", "timeframe": "Daily", "date": date,
        "decision": "watchlist", "grade": None,
        "metrics": metrics,
        "setups": setups,
        "verdict": f"{len(rows)} טיקרים ב-Pending, {flagged_count} מסומנים לבדיקה (גיל או שינוי מצב שוק).",
    }


def portfolio_to_widget_data(positions: list[dict], date: str) -> dict:
    """/playbook's multi-position aggregate -> WIDGET_DATA. Same gap as /pending had --
    screener/monitor/strategy adapters are all single-ticker. One card per position:
    ticker, sleeve, qty/avg/current price, action, and stop/target when the position
    has real target/stop data (Core/Layer1 holdings show the structural stop only,
    per CONSISTENCY_RULES.md rule 8 -- no numeric target, never invented for them)."""
    metrics = [
        {"label": "פוזיציות", "value": str(len(positions))},
        {"label": "Core", "value": str(sum(1 for p in positions if p.get("sleeve") == "core"))},
        {"label": "Swing", "value": str(sum(1 for p in positions if p.get("sleeve") == "swing"))},
    ]

    setups = []
    for p in positions:
        role = "alternate" if p.get("sleeve") == "core" else "primary"
        rows = []
        for label, key in (("כמות", "qty"), ("ממוצע", "avg"), ("מחיר", "price"), ("פעולה", "action")):
            if p.get(key) is not None:
                rows.append({"label": label, "value": str(p[key])})
        if p.get("stop") is not None:
            rows.append({"label": "סטופ", "value": str(p["stop"])})
        setups.append({
            "role": role,
            "title": f"{p['ticker']} ({p.get('sleeve', '?')})",
            "rows": rows,
            "targets": p.get("targets") or [],
        })

    return {
        "ticker": "תיק השקעות", "exchange": "", "timeframe": "Daily", "date": date,
        "decision": "watchlist", "grade": None,
        "metrics": metrics,
        "setups": setups,
        "verdict": f"{len(positions)} פוזיציות פתוחות -- ר' דוח מלא לסטופים/יעדים ולהסבר לכל טיקר.",
    }
