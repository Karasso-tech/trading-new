"""Turn a sim_breakout run into a day-by-day diary a person can read.

Reads the results JSON (which already holds the waiting-plan journal and each
position's per-day ledger) and writes a markdown file in plain words: every day
something actually happened -- a plan written, a plan changed, a buy, a stop
lifted, a sale, an exit -- and nothing on the quiet days.

Nothing here computes a trading decision. It only re-tells what the run did.

Usage: python backtest/write_diary.py results_AAPL_breakout_aapl10k.json [out.md]
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SETUP_HE = {
    "Breakout": "break above resistance",
    "Retest": "come back and hold the broken level",
    "Pullback": "dip inside an up move",
    "Reclaim": "take a lost level back",
    "Failed Breakdown": "the break down failed",
    "Gap-and-Hold": "jumped up and held the jump",
}

REGIME_WORDS = {
    "risk_on": "market strong",
    "healthy_uptrend": "market rising steadily",
    "pullback_in_uptrend": "market dipping inside an up move",
    "neutral_choppy": "market choppy",
    "risk_off": "market weak",
    "structure_break": "market structure broken",
}

BLOCK_WORDS = {
    "regime_risk_off": "the market was too weak to buy anything",
    "regime_structure_break": "the market structure was broken",
    "grade_D": "the idea graded too low to buy",
    "no_qualifying_target": "there was nowhere good to sell",
    "no_honest_stop": "no stop could sit far enough away to be safe",
}

EXIT_WORDS = {
    "stop": "the stop was hit",
    "stop_gap": "the day opened under the stop",
    "target_1": "first target reached",
    "target_2": "second target reached",
    "runner_target": "a new target the runner reached",
    "end_of_data": "the test ran out of days",
    "stop_close": "the day closed under the stop",
}


def money(x: float) -> str:
    return f"${x:,.2f}"


def setup_words(name: str | None) -> str:
    if not name:
        return "no clear shape"
    return f"{name} ({SETUP_HE.get(name, name)})"


def trading_days_between(log_dates: list[str], a: str, b: str) -> int:
    return sum(1 for d in log_dates if a < d <= b)


def write(results_path: Path, out_path: Path, compare: dict | None = None) -> None:
    data = json.loads(results_path.read_text(encoding="utf-8"))
    s = data["summary"]
    trades = data["trades"]
    plan_log = data.get("plan_log", [])
    curve_dates = [p["date"] for p in data["equity_curve"]]

    L: list[str] = []
    add = L.append

    add(f"# AAPL — a {money(s['start_equity'])} account, five years, by the rules\n")
    add(f"Days covered: **{s['window'][0]} to {s['window'][1]}**. "
        f"One trade at a time. Money at risk on each trade: 1% of the account "
        f"as it stands that day. No borrowing — if the shares cost more than "
        f"the cash on hand, only what the cash covers is bought.\n")

    add("## How it went\n")
    add(f"| | |\n|---|---|")
    add(f"| Money at the end | **{money(s['final_equity'])}** ({s['return_pct']:+.2f}%) |")
    add(f"| Just holding AAPL instead | {money(s['buy_hold_final'])} ({s['buy_hold_return_pct']:+.2f}%) |")
    add(f"| Trades | {s['trades']} |")
    add(f"| Winners | {s['wins']} ({s['win_rate_pct']}%) |")
    add(f"| Worst drop along the way | {s['max_drawdown_pct']}% |")
    add(f"| Sum of wins and losses, counted in risk units | {s['total_r']:+.2f} |")
    add(f"| Average per trade, in risk units | {s['avg_r']:+.3f} |")
    add("")

    if compare:
        c = compare["summary"]
        add("### The same five years with the stop left alone until the first target\n")
        add("This is the way the written rules work today. Same days, same "
            "ideas, same buys — the only difference is that the stop does not "
            "move until 40% has been sold at the first target.\n")
        add("| | stop lifted every day | stop left alone |")
        add("|---|---|---|")
        add(f"| Money at the end | {money(s['final_equity'])} | {money(c['final_equity'])} |")
        add(f"| Trades | {s['trades']} | {c['trades']} |")
        add(f"| Winners | {s['win_rate_pct']}% | {c['win_rate_pct']}% |")
        add(f"| Average per trade, in risk units | {s['avg_r']:+.3f} | {c['avg_r']:+.3f} |")
        add(f"| Worst drop along the way | {s['max_drawdown_pct']}% | {c['max_drawdown_pct']}% |")
        add("")

    # ---- the diary -------------------------------------------------------
    add("---\n")
    add("## The diary\n")
    add("Only days something happened. Quiet days are counted, not listed.\n")

    entries = {r["date"]: r for r in plan_log if r["event"] == "entered"}
    plan_by_date: dict[str, list[dict]] = {}
    for r in plan_log:
        plan_by_date.setdefault(r["date"], []).append(r)

    prev_end: str | None = None
    for n, t in enumerate(trades, start=1):
        ent = entries.get(t["entry_date"], {})
        add(f"### Trade {n} — bought {t['entry_date']}\n")

        # what the plan looked like while we waited
        waiting = [r for r in plan_log
                   if r["event"] in ("built", "changed")
                   and (prev_end is None or r["date"] > prev_end)
                   and r["date"] <= t["entry_date"]]
        if waiting:
            first, last = waiting[0], waiting[-1]
            days = trading_days_between(curve_dates,
                                        prev_end or curve_dates[0],
                                        t["entry_date"])
            times = len(waiting) - 1
            how_often = ("was never rewritten" if times == 0
                         else "was rewritten once" if times == 1
                         else f"was rewritten {times} times")
            gap = (f"{days} trading days passed between the last exit and this buy."
                   if prev_end else f"That was {days} trading days before the buy.")
            add(f"**The wait.** The plan was first written on {first['date']} and "
                f"{how_often} while the price stayed "
                f"under the buy line. {gap}\n")
            add(f"The plan on the day it fired: **{setup_words(last['setup'])}**, "
                f"buy above **{last['trigger']}**, stop **{last['stop']}**, "
                f"sell at **{', '.join(str(x) for x in last['targets']) or 'no target'}**, "
                f"grade **{last['grade']}**.\n")

        risk = ent.get("risk_usd", t["qty"] * (t["entry"] - t["initial_stop"]))
        add(f"**The buy.** {t['qty']} shares at **{t['entry']:.2f}**, stop "
            f"**{t['initial_stop']:.2f}**, money at risk **{money(risk)}**. "
            f"Cost of the shares: {money(t['qty'] * t['entry'])}. "
            f"Grade when it fired: **{ent.get('grade_at_fire', t['grade_at_build'])}**.\n")
        tgt = ", ".join(f"{x['price']:.2f} (sell {x['pct']:.0f}%)" for x in t["targets"])
        add(f"**The plan for getting out.** {tgt or 'no fixed target'}"
            + (", the rest rides on the stop.\n" if tgt else ".\n"))

        # day-by-day, only days with something on them
        rows = [r for r in t.get("ledger", []) if r["actions"]]
        retarget_seen = sorted({round(a["price"], 2) for r in rows
                                for a in r["actions"]
                                if a.get("rule") == "runner_retarget_found"})
        if rows:
            add("**What happened, day by day:**\n")
            for r in rows:
                for a in r["actions"]:
                    if a["what"] == "trail":
                        note = ""
                        if a["rule"] == "stop_basis_predates_entry":
                            note = " (this low is older than the trade itself)"
                        add(f"- {r['date']} — stop lifted to **{a['price']:.2f}**, "
                            f"sitting under the {a['basis_level']:.2f} low from "
                            f"{a['basis_date']}{note}. Price closed {r['close']:.2f}.")
                    elif a["what"] == "near_target":
                        add(f"- {r['date']} — price came close to the "
                            f"{a['price']:.2f} target. Heads-up only, nothing sold.")
                    elif a["what"].startswith("target"):
                        add(f"- {r['date']} — **sold {a['qty']} shares at "
                            f"{a['price']:.2f}** — {EXIT_WORDS.get(a['what'], a['what'])}.")
                    elif a["what"] in ("stop", "stop_gap", "stop_close"):
                        add(f"- {r['date']} — **sold {a['qty']} shares at "
                            f"{a['price']:.2f}** — {EXIT_WORDS.get(a['what'], a['what'])}.")
                    elif a.get("rule") == "runner_retarget_found":
                        continue          # collected and told once, below
                    else:
                        add(f"- {r['date']} — {a.get('rule', a['what'])} "
                            f"{a.get('price', '')}")
            if retarget_seen:
                add(f"- while the runner was above every planned target, a "
                    f"possible new level was spotted "
                    f"({', '.join(f'{x:.2f}' for x in retarget_seen)}). "
                    f"Nothing was sold there — the rules do not say how much of "
                    f"a runner to sell at a level found after the buy.")
            add("")

        held = trading_days_between(curve_dates, t["entry_date"], t["exit_date"] or t["entry_date"])
        last_reason = t["exits"][-1]["reason"] if t["exits"] else "still open"
        add(f"**The result.** Out on {t['exit_date']} — "
            f"{EXIT_WORDS.get(last_reason, last_reason)}. Held {held} trading days. "
            f"**{money(t['pnl'])}** ({t['r']:+.3f} risk units). "
            f"Stop was raised {t['stop_moves']} time(s), from {t['initial_stop']:.2f} "
            f"to {t['final_stop']:.2f}.\n")
        prev_end = t["exit_date"]

    # ---- what never got bought ------------------------------------------
    add("---\n")
    add("## Why so many days had no trade\n")
    add("Every day with no position, a fresh plan was written. These are the "
        "reasons a plan could not become a buy order, counted over the five years:\n")
    for k, v in sorted(s["blocked_at_build"].items(), key=lambda kv: -kv[1]):
        add(f"- **{v} days** — {BLOCK_WORDS.get(k, k)}")
    add("")
    if s.get("blocked_at_fire"):
        add("And these were days the price did cross the buy line, but the buy "
            "was still refused:\n")
        for b in s["blocked_at_fire"]:
            add(f"- {b['date']} — price crossed {b['trigger']:.2f}, but "
                f"{BLOCK_WORDS.get(b['reason'], b['reason'])}.")
        add(f"\nThose {len(s['blocked_at_fire'])} refused buys were followed "
            f"anyway, with no money on them, to see what they would have done: "
            f"together **{s['shadow_total_r']:+.2f} risk units**.\n")
    if s.get("skipped_reentry_wait"):
        add(f"On {s['skipped_reentry_wait']} day(s) the price crossed the buy "
            f"line while the trade was still inside the wait that follows a "
            f"stop-out, so it was skipped.\n")

    add("---\n")
    add("## What this test does not do\n")
    add("- No trading fees and no slippage. Every fill is at the exact price.\n"
        "- Only one idea at a time, and only AAPL. Nothing competes for the money.\n"
        "- Earnings dates are known up to 2026-07-30. After that the test cannot "
        "see the next one, so late ideas lose that grading point.\n"
        "- The last stored day is left out, because that session had not "
        "finished when the test was run.\n"
        "- The runner is never given a fresh target after the stored ones are "
        "gone. It leaves on the stop only.\n")

    out_path.write_text("\n".join(L), encoding="utf-8")
    print(f"{out_path}  ({len(L)} lines)")


if __name__ == "__main__":
    res = ROOT / sys.argv[1]
    out = ROOT / (sys.argv[2] if len(sys.argv) > 2 else "AAPL_10K_DIARY.md")
    cmp_path = ROOT / sys.argv[3] if len(sys.argv) > 3 else None
    cmp_data = json.loads(cmp_path.read_text(encoding="utf-8")) if cmp_path else None
    write(res, out, cmp_data)
