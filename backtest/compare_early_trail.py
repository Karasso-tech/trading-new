"""Side-by-side table for the pre-registered early-trail test (2026-08-13).

Reads the baseline and early-trail portfolio results for each seed and prints
money, trade counts and the diagnosis of what the early stop actually did.

Usage: python backtest/compare_early_trail.py [--tag et15] [--seeds 42 7 13 99]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load(seed: int, tag: str | None):
    suffix = f"_{tag}" if tag else ""
    p = ROOT / f"results_portfolio_{seed}_grade{suffix}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="et15")
    ap.add_argument("--seeds", type=int, nargs="+", default=[42, 7, 13, 99])
    args = ap.parse_args()

    rows = []
    for s in args.seeds:
        base, var = load(s, None), load(s, args.tag)
        if not base or not var:
            print(f"seed {s}: missing ({'base' if not base else 'variant'})")
            continue
        b, v = base["summary"], var["summary"]
        rows.append((s, b, v))
        print(f"\n=== seed {s} ({b['window'][0]} .. {b['window'][1]}) ===")
        print(f"{'':22} {'leave stop alone':>18} {'early trail':>18}")
        for label, key, fmt in [
            ("final equity $", "final_equity", "{:,.0f}"),
            ("return %", "return_pct", "{:+.2f}"),
            ("worst drop %", "max_drawdown_pct", "{:.2f}"),
            ("trades", "trades", "{:.0f}"),
            ("win rate %", "win_rate_pct", "{:.1f}"),
            ("total R", "total_r", "{:+.2f}"),
            ("money from runners $", "runner_pnl_total", "{:,.0f}"),
            ("money from targets $", "target_pnl_total", "{:,.0f}"),
        ]:
            bv = b.get(key)
            vv = v.get(key)
            bs = fmt.format(bv) if bv is not None else "-"
            vs = fmt.format(vv) if vv is not None else "-"
            print(f"{label:22} {bs:>18} {vs:>18}")
        print(f"{'SPY buy and hold %':22} {b['spy_buy_hold_return_pct']:>18.2f}")
        print(f"trades where the early stop armed: {v.get('trades_early_armed')}"
              f" / moved the stop: {v.get('trades_early_moved')}")

    if not rows:
        return
    print("\n=== all draws together ===")
    bt = sum(b["return_pct"] for _, b, _ in rows) / len(rows)
    vt = sum(v["return_pct"] for _, _, v in rows) / len(rows)
    bm = sum(b["final_equity"] - b["start_equity"] for _, b, _ in rows)
    vm = sum(v["final_equity"] - v["start_equity"] for _, _, v in rows)
    wins = sum(1 for _, b, v in rows if v["return_pct"] > b["return_pct"])
    print(f"average return: leave alone {bt:+.2f}%   early trail {vt:+.2f}%"
          f"   difference {vt - bt:+.2f}%")
    print(f"total profit on ${rows[0][1]['start_equity']:,.0f} per draw: "
          f"leave alone ${bm:,.0f}   early trail ${vm:,.0f}   "
          f"difference ${vm - bm:,.0f}")
    print(f"draws the early trail won: {wins} of {len(rows)}")


if __name__ == "__main__":
    main()
