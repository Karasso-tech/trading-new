"""Does the A/B/C grade rank a trade, and if not, which criterion is at fault?

READ-ONLY. Reads results files already on disk and prints tables. It runs no
backtest, fetches nothing, and writes nothing except the report you ask for.

Why this exists at all, and why it is careful
---------------------------------------------
An earlier answer to this question was produced by hand, in one afternoon, and
was wrong twice over. It counted rows in the shadow book as if they were trades
(they are nightly snapshots: 104 "closed trades" were 13), and it read a
difference of +0.375R between two grade buckets as a finding when the margin
around it comfortably covered zero. Both mistakes pointed the same way -- toward
a conclusion -- which is the direction mistakes usually point.

So the rules here are stricter than the arithmetic needs:

  * every table shows the MEDIAN beside the mean, because one +9.8R trade
    carried an entire grade bucket's positive total in the first attempt
  * every table shows how much of the total came from its single best trade
  * every difference gets a margin, and a margin covering zero is reported as
    "cannot be told apart", never as a small effect
  * seeds are reported separately as well as pooled -- four draws agreeing is
    evidence, one pooled number is an average of four things
  * duplicate trades across seeds are removed; the draws overlap by a few
    tickers and the same trade would otherwise be counted twice

The decision this is meant to settle, stated in advance
-------------------------------------------------------
The grade keeps its job -- ranking ideas -- only if A > B > C holds in EVERY
seed, with costs on, and still holds after each bucket's best trade is dropped.
Anything less and the letter is a research label: recorded, not acted on.

Usage:
    python backtest/analyze_grade.py                       # every s3* results file
    python backtest/analyze_grade.py --pattern 's3base'    # one configuration
    python backtest/analyze_grade.py --out report.md
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
from collections import defaultdict

HERE = pathlib.Path(__file__).resolve().parent
GRADES = ("A", "B", "C", "D", "F")
CRITERIA = ("rr", "target_atr", "rs", "sma20_extension", "event")


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def load(pattern: str) -> dict:
    """Every results file matching the pattern, keyed by seed and mode.

    A file's own summary says which configuration produced it; the filename is
    a convenience, not the source of truth. Two files that disagree on
    allow_grade_d_research are answering different questions and are never
    pooled."""
    runs = {}
    for path in sorted(HERE.glob(f"results_portfolio_*{pattern}*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:                      # a half-written file
            print(f"  skipped {path.name}: {type(exc).__name__}")
            continue
        summary = data.get("summary") or {}
        runs[path.name] = {
            "seed": summary.get("seed"),
            "allow_d": bool(summary.get("allow_grade_d_research")),
            "trades": data.get("trades") or [],
            "summary": summary,
        }
    return runs


def dedupe(trades: list) -> list:
    """One entry per real trade.

    The four seeds draw overlapping ticker lists, so the same trade appears in
    more than one of them -- 29 of 632 in the first pass. Pooling without this
    counts those twice and quietly narrows every margin."""
    seen, out = set(), []
    for t in trades:
        key = (t.get("ticker"), t.get("entry_date"), round(t.get("entry") or 0, 4))
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


# --------------------------------------------------------------------------
# statistics, kept deliberately blunt
# --------------------------------------------------------------------------

def describe(values: list) -> dict:
    """Mean, median, spread, and how much of the total rests on one trade."""
    if not values:
        return {}
    total = sum(values)
    best = max(values)
    n = len(values)
    sd = statistics.stdev(values) if n > 1 else 0.0
    return {
        "n": n,
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "win_pct": 100 * sum(1 for v in values if v > 0) / n,
        "total": total,
        "best": best,
        # A bucket whose whole positive total is one trade is not a bucket with
        # an edge, it is a bucket with a lottery ticket in it.
        "best_share": (best / total * 100) if total > 0 else None,
        "margin": 1.96 * sd / math.sqrt(n) if n > 1 else None,
    }


def difference(a: list, b: list) -> dict:
    """b minus a, with a margin. Overlapping margins mean "cannot be told
    apart" -- reported that way rather than as a small effect."""
    if len(a) < 2 or len(b) < 2:
        return {}
    diff = statistics.mean(b) - statistics.mean(a)
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    return {"diff": diff, "margin": 1.96 * se,
            "separable": abs(diff) > 1.96 * se}


def drop_best(values: list) -> list:
    """The same numbers without their single largest winner."""
    if len(values) < 2:
        return values
    out = list(values)
    out.remove(max(out))
    return out


# --------------------------------------------------------------------------
# the questions
# --------------------------------------------------------------------------

def by_grade(trades: list, field: str) -> dict:
    buckets = defaultdict(list)
    for t in trades:
        grade = t.get(field)
        if grade in GRADES and t.get("r") is not None:
            buckets[grade].append(t["r"])
    return buckets


def ranks_correctly(buckets: dict) -> bool:
    """A > B > C on the mean. D and F are not part of the ordering claim: the
    system refuses to trade them, so they appear only in a run that was
    deliberately told to allow them."""
    present = [g for g in ("A", "B", "C") if buckets.get(g)]
    if len(present) < 2:
        return False
    means = [statistics.mean(buckets[g]) for g in present]
    return all(x > y for x, y in zip(means, means[1:]))


def by_criterion(trades: list) -> dict:
    """R split by each rubric criterion, passed against failed.

    This is the question the grade itself cannot answer. A letter is five
    yes/no answers added up, so a letter that does not rank might still be
    hiding one criterion that works and one that hurts -- and the immediate
    suspect is R:R >= 2.3, which buys quality with distance and distance costs
    hit rate."""
    out = {}
    for crit in CRITERIA:
        passed, failed = [], []
        for t in trades:
            crits = t.get("rubric_criteria")
            if not isinstance(crits, dict) or crit not in crits or t.get("r") is None:
                continue
            (passed if crits[crit] else failed).append(t["r"])
        if passed or failed:
            out[crit] = {"passed": passed, "failed": failed}
    return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def fmt(d: dict) -> str:
    if not d:
        return "| — | — | — | — | — | — |"
    margin = f"±{d['margin']:.2f}" if d["margin"] is not None else "—"
    share = f"{d['best_share']:.0f}%" if d["best_share"] is not None else "—"
    return (f"| {d['n']} | {d['mean']:+.3f} {margin} | {d['median']:+.2f} | "
            f"{d['win_pct']:.0f}% | {d['total']:+.1f} | {share} |")


def report(runs: dict) -> list:
    L = ["# Does the grade rank a trade?", "",
         "Read-only: no backtest was run and nothing was written to produce this.",
         ""]
    if not runs:
        L += ["No results files matched. Run the stage-3 backtests first.", ""]
        return L

    modes = defaultdict(list)
    for name, run in runs.items():
        modes["D allowed" if run["allow_d"] else "as the system runs"].append(run)

    for mode, group in sorted(modes.items()):
        seeds = sorted(str(r["seed"]) for r in group)
        pooled = dedupe([t for r in group for t in r["trades"]])
        raw = sum(len(r["trades"]) for r in group)
        L += [f"## {mode}", "",
              f"Seeds {', '.join(seeds)} · {raw} trades, {len(pooled)} after removing "
              f"the {raw - len(pooled)} that appear in more than one draw.", ""]

        for field, label in (("grade_at_build", "the grade the plan was written with"),
                              ("grade_at_fire", "the grade it actually filled at")):
            buckets = by_grade(pooled, field)
            if not buckets:
                L += [f"### By {label}", "", "No trade carries this field yet.", ""]
                continue
            L += [f"### By {label}", "",
                  "| grade | trades | mean R | median | win% | total R | best trade's share |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
            for g in GRADES:
                if buckets.get(g):
                    L.append(f"| **{g}** " + fmt(describe(buckets[g])))
            L.append("")

            per_seed = []
            for r in sorted(group, key=lambda r: r["seed"] or 0):
                b = by_grade(dedupe(r["trades"]), field)
                ok = ranks_correctly(b)
                per_seed.append(ok)
                order = " · ".join(f"{g} {statistics.mean(b[g]):+.2f}"
                                   for g in ("A", "B", "C") if b.get(g))
                L.append(f"- seed {r['seed']}: {order}   →  "
                         f"{'A > B > C' if ok else 'not in order'}")
            L.append("")

            stripped = {g: drop_best(v) for g, v in buckets.items()}
            survives = ranks_correctly(stripped)
            a, c = buckets.get("A", []), buckets.get("C", [])
            d = difference(a, c)
            if d:
                verdict = ("a real difference" if d["separable"]
                           else "**cannot be told apart from no difference**")
                L += [f"C minus A: {d['diff']:+.3f}R, margin ±{d['margin']:.3f} — {verdict}.", ""]
            L += ["**Verdict on this field.** The grade keeps its ranking job only if "
                  "A > B > C holds in every seed and survives dropping each bucket's best "
                  "trade.", "",
                  f"- holds in every seed: **{'yes' if all(per_seed) and per_seed else 'no'}**",
                  f"- survives dropping the best trade: **{'yes' if survives else 'no'}**", ""]

        crits = by_criterion(pooled)
        if crits:
            L += ["### Which criterion is doing the work", "",
                  "A letter is five yes/no answers added together, so a letter that does "
                  "not rank can still hide one criterion that helps and one that hurts.", "",
                  "| criterion | passed it | failed it | difference |",
                  "|---|---|---|---|"]
            for crit, sides in crits.items():
                p, f = describe(sides["passed"]), describe(sides["failed"])
                d = difference(sides["failed"], sides["passed"])
                if d:
                    tail = (f"{d['diff']:+.3f} ±{d['margin']:.2f}"
                            + ("" if d["separable"] else " (not separable)"))
                else:
                    tail = "—"
                ps = f"{p['n']} trades, {p['mean']:+.2f}R" if p else "—"
                fs = f"{f['n']} trades, {f['mean']:+.2f}R" if f else "—"
                L.append(f"| `{crit}` | {ps} | {fs} | {tail} |")
            L.append("")
        else:
            L += ["### Which criterion is doing the work", "",
                  "No trade carries `rubric_criteria` yet — re-run the backtest with the "
                  "current engine.", ""]

    L += ["## What this cannot tell you", "",
          "- These are Breakout/Retest trades only. The other four setup types were "
          "never backtested, and rule 26 already limits its own finding the same way.",
          "- One five-year window. A pattern that holds here held here.",
          "- The engine changed on 2026-08-30, so numbers from older result files are "
          "not comparable with these and must not be pooled with them.", ""]
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pattern", default="s3", help="substring of the results filenames")
    ap.add_argument("--out", help="write the report here as well as printing it")
    args = ap.parse_args()

    runs = load(args.pattern)
    text = "\n".join(report(runs)) + "\n"
    print(text)
    if args.out:
        pathlib.Path(args.out).write_text(text, encoding="utf-8")
        print(f"written: {args.out}")


if __name__ == "__main__":
    main()
