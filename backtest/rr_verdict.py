"""READ-ONLY. Scores the four R:R arms against the four conditions written down
in backtest/PREREGISTRATION_RR_THRESHOLD.md, before any result was seen.

Prints the verdict on each condition separately, so a pass on three of four
cannot be read as a pass.
"""
import json
import pathlib
import statistics
from collections import defaultdict

HERE = pathlib.Path(__file__).resolve().parent
ARMS = {"rrLIVE": "control (2.3)", "rr2.0": "2.0", "rr1.8": "1.8", "rrDROP": "removed"}
CRITERIA = ("rr", "target_atr", "rs", "sma20_extension", "event")


def load():
    """{arm: {seed: run}} from the filenames, checked against each file's own
    summary so a mislabelled file cannot join the wrong arm."""
    out = defaultdict(dict)
    for f in sorted(HERE.glob("results_portfolio_*_rr*.json")):
        arm = next((a for a in ARMS if f.name.endswith(f"_{a}.json")), None)
        if arm is None:
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        smry = d.get("summary") or {}
        # the file's own record of what produced it
        expect = {"rrLIVE": (None, False), "rr2.0": (2.0, False),
                  "rr1.8": (1.8, False), "rrDROP": (None, True)}[arm]
        if (smry.get("rr_min"), bool(smry.get("drop_rr_criterion"))) != expect:
            print(f"  ! {f.name} does not match arm {arm} -- skipped")
            continue
        out[arm][smry.get("seed")] = d
    return out


def drop_best(vals):
    if len(vals) < 2:
        return vals
    v = list(vals)
    v.remove(max(v))
    return v


def main():
    runs = load()
    missing = [a for a in ARMS if len(runs.get(a, {})) < 4]
    if missing:
        print(f"still incomplete: {missing}  (have "
              f"{ {a: len(runs.get(a, {})) for a in ARMS} })\n")

    seeds = sorted({s for a in runs for s in runs[a]})
    print(f"{'arm':16s} " + " ".join(f"{('seed '+str(s)):>11}" for s in seeds)
          + f" {'pooled R':>10} {'maxDD':>8} {'trades':>7}")
    print("-" * 90)
    totals, dds, pooled_trades = {}, {}, {}
    for arm, label in ARMS.items():
        if arm not in runs:
            continue
        cells, per_seed, dd = [], {}, []
        seen, trades = set(), []
        for s in seeds:
            d = runs[arm].get(s)
            if not d:
                cells.append(f"{'--':>11}")
                continue
            tr = d["summary"]["total_r"]
            per_seed[s] = tr
            dd.append(d["summary"]["max_drawdown_pct"])
            cells.append(f"{tr:>+11.1f}")
            for t in d["trades"]:
                k = (t.get("ticker"), t.get("entry_date"), round(t.get("entry") or 0, 4))
                if k not in seen and t.get("r") is not None:
                    seen.add(k)
                    trades.append(t)
        rs = [t["r"] for t in trades]
        totals[arm], dds[arm], pooled_trades[arm] = per_seed, dd, trades
        print(f"{label:16s} " + " ".join(cells)
              + f" {sum(rs):>+10.1f} {statistics.mean(dd) if dd else 0:>7.1f}% {len(rs):>7}")

    ctrl = "rrLIVE"
    if ctrl not in totals:
        print("\nno control arm -- cannot judge")
        return
    print("\n\nthe four conditions, each scored on its own\n")
    for arm, label in ARMS.items():
        if arm == ctrl or arm not in totals:
            continue
        beat = sum(1 for s in seeds
                   if s in totals[arm] and s in totals[ctrl]
                   and totals[arm][s] > totals[ctrl][s])
        c1 = beat >= 3
        a_rs = [t["r"] for t in pooled_trades[arm]]
        c_rs = [t["r"] for t in pooled_trades[ctrl]]
        c2 = (sum(drop_best(a_rs)) > sum(drop_best(c_rs))) if a_rs and c_rs else False
        dd_delta = (statistics.mean(dds[arm]) - statistics.mean(dds[ctrl])) if dds[arm] else 99
        c3 = dd_delta <= 3.0
        by_g = defaultdict(list)
        for t in pooled_trades[arm]:
            if t.get("grade_at_build") in ("A", "B", "C"):
                by_g[t["grade_at_build"]].append(t["r"])
        means = {g: statistics.mean(v) for g, v in by_g.items() if v}
        backwards = (means.get("A", 0) < means.get("C", 0))
        c4 = not backwards
        print(f"{label}")
        print(f"   1. beats control in >=3 of 4 draws      {beat}/4   "
              f"{'PASS' if c1 else 'FAIL'}")
        print(f"   2. survives dropping each best trade    "
              f"{sum(drop_best(a_rs)):+.1f} vs {sum(drop_best(c_rs)):+.1f}   "
              f"{'PASS' if c2 else 'FAIL'}")
        print(f"   3. drawdown not worse by >3pt           {dd_delta:+.1f}pt   "
              f"{'PASS' if c3 else 'FAIL'}")
        order = " ".join(f"{g}{means[g]:+.2f}" for g in ("A", "B", "C") if g in means)
        print(f"   4. grade order not backwards            {order}   "
              f"{'PASS' if c4 else 'FAIL'}")
        print(f"   ==> {'ADOPT' if all((c1, c2, c3, c4)) else 'REJECTED'}\n")


if __name__ == "__main__":
    main()
