"""The whole mechanical half of a screener run, in one command (2026-08-09).

`fetch_analysis_data.py` gathers the facts. This turns those facts into the
plan -- setup type, trigger, stop, targets, allocation, movement potential, the
reasons it is not a clean buy, and the finished Telegram message. Everything
here is arithmetic over rules that were already written down.

    python bot/build_plan.py TICKER            # fetch and build
    python bot/build_plan.py --from-json PATH  # build from an existing fetch

What is left for the model afterwards, and it is the part worth having a model
for:

  * one sentence saying what the story is
  * whether this mechanical reading is actually the right one -- and if not,
    which of the six setups it really is AND why, written down
  * the full report body, sections א through ו
  * the final decision word, which may always be WEAKER than the facts permit
    (a real reason to wait is judgment) and never stronger

That last point is unchanged: decision_policy.max_allowed_decision still sets
the ceiling, and this module reports it rather than deciding it.

The override path is the same one rule 23 established for market state and rule
27 for the grade: the computed answer is the default, a model may differ, and
differing requires a stated reason that stays visible in the output. Never a
silent substitution -- that is the failure mode all three rules exist to stop.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision_policy
import level_picker
import rubric_formula
import setup_classifier
import setup_types
import summary_text

BOT_DIR = Path(__file__).resolve().parent


def _bars_from(data: dict) -> list[dict]:
    """recent_bars_40 already carries a `date` on every bar, which is what the
    base scan reports back. Nothing else in this module needs more history --
    the wall chains and swing lows were computed upstream over the full fetch."""
    return data.get("recent_bars_40") or []


def scan_setup(data: dict, *, trigger: float, stop: float,
                setup_type: Optional[str] = None) -> dict:
    """A full rule-7 target scan for ANY entry/stop pair, not just the computed
    primary one (2026-08-09).

    Rule 7, in its own words: *"Every setup shown gets this analysis
    independently -- Alternate is not exempt just because it's second or still
    pending. Each setup has its own hypothetical entry/stop (rule 14), so it
    must get its own target scan (rule 11's wall check, rule 3's gates, rule
    12's alternates) computed from THAT setup's own entry/stop -- never left as
    trigger/stop only while only Primary gets a target table."*

    The first version of this module computed the primary and handed the
    Alternate to the model with no target scan at all, which is exactly the
    omission that sentence forbids. Rule 7 also explains why it matters rather
    than being tidiness: two setups often reach the same resistance from very
    different entries, and the deeper one can pay 2.5:1 where the higher one
    fails outright (ANET's 179.80 level failed from a 179.80 entry and passed
    from ~162).

    Which SCENARIO the Alternate is stays judgment -- rule 5 wants two
    directions, not two depths, and no formula picks that. Once the model has
    chosen its entry and stop, everything downstream of them is arithmetic and
    belongs here."""
    atr14 = data.get("atr14")
    scan = level_picker.pick_targets(
        trigger, stop, atr14, data.get("wall_chains") or [],
        bars=_bars_from(data), swing_lows=data.get("swing_lows_recent") or [],
    )
    return {
        "type": setup_type,
        "trigger": trigger,
        "stop": stop,
        "atr_at_build": atr14,
        "targets": [
            {"price": t.price, "pct": t.pct, "atr_mult": t.atr_mult, "rr": t.rr,
             "status": t.status, "source": t.source}
            for t in scan.targets
        ],
        "checkpoints": [
            {"price": c.price, "atr_mult": c.atr_mult, "rr": c.rr, "source": c.source}
            for c in scan.checkpoints
        ],
        "runner_pct": scan.runner_pct,
        "target_note": _scoped_no_target_note(scan, data),
        "target_sources_checked": scan.sources_checked,
        "target_scan_complete": scan.complete,
    }


def _scoped_no_target_note(scan, data: dict) -> Optional[str]:
    """Rule 16's scoped wording. "No data above this level" is a claim that must
    be earned, and `get_daily_history` typically returns ~2 years rather than
    the 5 the protocol asks for -- so an empty scan says which window it looked
    at, never "there is nothing above"."""
    if scan.note is None:
        return None
    coverage = data.get("coverage") or {}
    start, end = coverage.get("date_start"), coverage.get("date_end")
    if start and end:
        return f"{scan.note}. Searched within fetched history only: {start} to {end} (rule 16)"
    return scan.note


def build(data: dict, *, setup_type_override: Optional[str] = None,
           override_reason: Optional[str] = None,
           alt_trigger: Optional[float] = None,
           alt_stop: Optional[float] = None,
           alt_type: Optional[str] = None) -> dict:
    """The plan, from one fetch_analysis_data.py payload."""
    # Rule 8: a Core/Layer1 holding is exempt from rules 1-7 entirely -- a wide
    # structural stop only, no numeric target, no tranche table. Running the
    # gates over SPY or QQQ and reporting "No Trade, no qualifying target" is
    # not a finding, it is a category error: those positions were never meant
    # to have targets. The exemption is read from the stored sleeve, never
    # inferred from size or age, exactly as the rule requires.
    if (data.get("sleeve") or "").strip().lower() == "core":
        return {
            "ticker": data.get("ticker"),
            "core_exempt": True,
            "note": ("this is a Core/Layer1 holding and is exempt from rules 1-7 (rule 8): "
                     "a wide structural stop only, no numeric target, no tranche table, "
                     "no 2H alerts. Nothing below was computed for it."),
            "primary_setup": None,
            "rejection_reasons": [],
            "max_allowed_decision": None,
        }

    atr14 = data.get("atr14")
    bars = _bars_from(data)
    wall_chains = data.get("wall_chains") or []
    swing_lows = data.get("swing_lows_recent") or []

    call = setup_classifier.classify(
        bars=bars, atr14=atr14, sma20=data.get("sma20"), sma50=data.get("sma50"),
        wall_chains=wall_chains, swing_lows=swing_lows,
    )
    if setup_type_override:
        if not override_reason:
            raise ValueError(
                "a setup_type override requires a written reason -- same rule as a market-state "
                "override (rule 23). An override with no stated reason is not allowed."
            )
        call = dataclasses.replace(call, setup_type=setup_type_override,
                                    note=f"OVERRIDDEN: {override_reason}")

    trigger = call.trigger
    stop_choice = level_picker.StopChoice(None, None, None, None, None,
                                          reason="no trigger, so no stop to place")
    scan = level_picker.TargetScan()
    if trigger is not None and atr14:
        stop_choice = level_picker.pick_stop(trigger, atr14, swing_lows,
                                              current_price=data.get("current_price"),
                                              bars=bars)
        if stop_choice.stop is not None:
            scan = level_picker.pick_targets(trigger, stop_choice.stop, atr14, wall_chains,
                                              bars=bars, swing_lows=swing_lows)

    potential = level_picker.movement_potential(bars, trigger or 0.0, atr14 or 0.0)

    # Rule 15: which relative-strength window this setup type is scored on. The
    # single most common way a reversal used to be mis-graded was being measured
    # on 20 days while still dragging the fall it was recovering from.
    window = setup_classifier.rs_window_days(call.setup_type)
    rs_delta = data.get("rs_5d_vs_spy") if window == 5 else data.get("rs_20d_vs_spy")
    # Rule 15: "Show both windows for a reversal setup -- a reversal widget or
    # report with only one RS number is itself an incomplete render." Reporting
    # only the scored one hid the very comparison the rule exists to surface.
    rs_both = {"rs_5d_vs_spy": data.get("rs_5d_vs_spy"),
                "rs_20d_vs_spy": data.get("rs_20d_vs_spy")}         if setup_types.is_reversal(call.setup_type) else None

    # A movement potential BELOW the furthest sellable target reads as a
    # contradiction to anyone skimming -- "how far it could go: 167" printed
    # under "target 2: 189". Rule 17 says the two answer different questions
    # (the base's own measured move vs. the resistance levels overhead) and both
    # numbers are honest, but a reader has to be told that, not left to work it
    # out. Seen live on ORCL, 2026-08-09. Noted, never quietly adjusted: making
    # the potential stretch to cover the targets would be inventing a number.
    if (potential.price is not None and scan.targets
            and potential.price < scan.targets[-1].price):
        potential = dataclasses.replace(
            potential,
            note=("this is the base's own measured move, which lands BELOW the furthest "
                  "target -- the targets come from resistance levels overhead, not from "
                  "the base. Both are real; they answer different questions (rule 17)."),
        )

    first = scan.targets[0] if scan.targets else None
    grade = None
    # The score and the five pass/fail criteria travel with the letter, not just
    # the letter (2026-08-30). Rule 27 promises report_lint recomputes
    # classify_rubric() from "the decision's own disclosed inputs" and checks it
    # matches -- that check could never have existed, because nothing but the
    # letter was ever written down. `rubric_inputs` is what makes it possible;
    # `rubric_criteria` is what makes a per-criterion measurement possible later.
    rubric_score = None
    rubric_criteria = None
    rubric_inputs = None
    if first is not None and stop_choice.stop is not None:
        inputs = rubric_formula.RubricInputs(
            rr=first.rr, target_atr_multiple=first.atr_mult,
            regime=(data.get("market_regime_formula") or {}).get("regime") or "",
            rs_delta_pct=rs_delta if rs_delta is not None else 0.0,
            dist_sma20_atr=data.get("dist_sma20_atr") or 0.0,
            earnings_days_out=data.get("earnings_days_out"),
        )
        grade_result = rubric_formula.classify_rubric(inputs)
        grade = grade_result.grade
        rubric_score = grade_result.score
        rubric_criteria = grade_result.criteria
        rubric_inputs = dataclasses.asdict(inputs)

    regime = (data.get("market_regime_formula") or {}).get("regime")
    reasons = level_picker.rejection_reasons(
        has_target=bool(scan.targets),
        rr=first.rr if first else None,
        grade=grade, regime=regime, rs_delta_pct=rs_delta,
        dist_sma20_atr=data.get("dist_sma20_atr"),
        earnings_days_out=data.get("earnings_days_out"),
        # The trigger has fired only if the last settled close is already at or
        # above it -- the same daily-close standard MONITOR_v2 uses for a green,
        # never an intraday touch.
        trigger_fired=bool(trigger and data.get("current_price")
                            and data["current_price"] >= trigger),
        stop=stop_choice.stop,
    )

    max_decision = decision_policy.max_allowed_decision(
        has_target=bool(scan.targets), grade=grade, regime=regime,
        # Rule 1: a stop that is a plain 2x ATR distance cites no source, so it
        # may be researched and may not carry a live order. pick_stop already
        # labels it; nothing had ever read the label.
        has_structural_stop=(stop_choice.basis_kind != level_picker.BASIS_NO_STRUCTURE),
    )
    # Rule 12 again. "No target" is the strongest possible verdict -- it means
    # the trade does not exist -- and this module is not entitled to reach it on
    # a scan that skipped two authorized sources. When the scan is incomplete,
    # the ceiling stops at Watchlist and the reason says why, so the model has
    # to finish the scan before anything is written off. Found 2026-08-09: on
    # the real pending list this produced 8 No Trades out of 16, every one of
    # them from a partial scan.
    if max_decision == decision_policy.NO_TRADE and not scan.complete:
        max_decision = decision_policy.WATCHLIST
    # decision_policy deliberately does not rank "Buy Now" against "Buy Only If
    # Confirmed" -- it is not given the one fact that separates them, and its
    # own docstring says so. This module IS given it, so it applies the last
    # step here rather than leaving a trap: on the first live run ORCL came back
    # with a ceiling of "Buy Now" beside a rejection reason of
    # "trigger_not_fired", which are the same sentence disagreeing with itself.
    # By decision_policy's own definitions, Buy Now needs the trigger already
    # confirmed on a settled daily close.
    if max_decision == decision_policy.BUY_NOW and "trigger_not_fired" in reasons:
        max_decision = decision_policy.BUY_IF_CONFIRMED

    setup = {
        "type": call.setup_type,
        "trigger": trigger,
        "stop": stop_choice.stop,
        "stop_basis_level": stop_choice.basis_level,
        # Which KIND of structure the stop stands on (2026-08-10). Recorded on
        # every trade so the shadow book can eventually answer whether stops on
        # recent structure do better than stops on five-month-old structure --
        # today that is an open question, not a claim.
        "stop_basis_kind": stop_choice.basis_kind,
        "atr_at_build": atr14,
        "targets": [
            {"price": t.price, "pct": t.pct, "atr_mult": t.atr_mult, "rr": t.rr,
             "status": t.status, "source": t.source}
            for t in scan.targets
        ],
        "checkpoints": [
            {"price": c.price, "atr_mult": c.atr_mult, "rr": c.rr, "source": c.source}
            for c in scan.checkpoints
        ],
    }

    return {
        "ticker": data.get("ticker"),
        "setup_call": {
            "setup_type": call.setup_type,
            "trigger_basis": call.trigger_basis,
            "confidence": call.confidence,
            "evidence": call.evidence,
            "note": call.note,
            "rs_window_days": window,
            "rs_delta_pct": rs_delta,
            # Rule 15 -- present only for reversals, where both must be shown.
            "rs_both_windows": rs_both,
        },
        "primary_setup": setup,
        "stop_detail": dataclasses.asdict(stop_choice),
        "target_note": _scoped_no_target_note(scan, data),
        "target_sources_checked": scan.sources_checked,
        "target_scan_complete": scan.complete,
        # Rule 7: the Alternate gets its own full target scan from its OWN
        # entry/stop, never left as trigger/stop only. Present whenever the
        # caller supplied that setup's levels -- which scenario it is stays
        # judgment (rule 5), everything after that is arithmetic.
        "alternate_setup": (
            scan_setup(data, trigger=alt_trigger, stop=alt_stop, setup_type=alt_type)
            if (alt_trigger is not None and alt_stop is not None) else None
        ),
        "alternate_note": (
            None if (alt_trigger is not None and alt_stop is not None) else
            "no Alternate scanned -- rule 5 requires two setups in every report and rule 7 "
            "requires the Alternate to get its own target scan. Choose its entry and stop "
            "(that is judgment), then re-run with --alt-trigger and --alt-stop."
        ),
        "rule_26_disclosure": _rule_26_disclosure(call.setup_type, regime),
        "runner_pct": scan.runner_pct,
        "potential": dataclasses.asdict(potential),
        "rubric_grade": grade,
        "rubric_score": rubric_score,
        "rubric_criteria": rubric_criteria,
        "rubric_inputs": rubric_inputs,
        "market_regime": regime,
        "rejection_reasons": reasons,
        "max_allowed_decision": max_decision,
        # Ready to hand to summary_text.build once the model supplies the one
        # sentence and the final decision word.
        "summary_inputs": {
            "ticker": data.get("ticker"),
            "grade": grade,
            "primary": setup,
            "potential": potential.price,
            "disclosure_flags": _disclosure_flags(data, reasons),
        },
    }


def _disclosure_flags(data: dict, reasons: list[str]) -> list[str]:
    """Which of section ח's fixed disclosure lines actually apply.

    Derived from the facts rather than chosen, so the same situation always
    produces the same lines in the same order -- which is the entire point of a
    fixed template."""
    flags = []
    account = data.get("account") or {}
    heat = account.get("portfolio_heat") or {}
    if (data.get("market_regime_formula") or {}).get("regime") == "neutral_choppy":
        flags.append("regime")
    volume_pct = data.get("volume_pct_of_avg")
    if volume_pct is not None and volume_pct < 100:
        flags.append("volume")
    if not data.get("earnings_verified"):
        flags.append("event")
    if heat.get("heat_pct") is not None and heat.get("cap_pct") is not None \
            and heat["heat_pct"] > heat["cap_pct"]:
        flags.append("heat")
    if "trigger_not_fired" in reasons:
        flags.append("trigger")
    if "rs_weaker_than_market" in reasons:
        flags.append("rs")
    if "extended_vs_sma20" in reasons:
        flags.append("extended")
    return flags


# Rule 26, informational only and never a gate: a mechanical 5-year backtest of
# the plain Breakout/Retest-above-a-chained-wall setup found healthy_uptrend was
# the ONLY regime with negative average expectancy for that setup type --
# -0.12R over 91 trades, against +0.20R to +0.36R in every other regime rule 18
# allows. The rule requires stating the figure explicitly when it applies. Its
# scope is narrow and stated: Breakout and Retest only. Pullback, Reclaim,
# Failed Breakdown and Gap-and-Hold were never backtested and this does not
# apply to them.
RULE_26_SETUPS = (setup_types.BREAKOUT, setup_types.RETEST)
RULE_26_REGIME = "healthy_uptrend"
RULE_26_NOTE = (
    "regime=healthy_uptrend -- this setup type historically averaged -0.12R here across 91 "
    "backtested trades, against positive expectancy in every other regime rule 18 allows. "
    "Informational only (rule 26): it never blocks, downgrades or resizes the trade."
)


def _rule_26_disclosure(setup_type: Optional[str], regime: Optional[str]) -> Optional[str]:
    if setup_type in RULE_26_SETUPS and regime == RULE_26_REGIME:
        return RULE_26_NOTE
    return None


# Rule 25 (past-lesson injection) is deliberately NOT computed here -- see the
# rule itself in CONSISTENCY_RULES.md for the owner's reasoning, recorded
# 2026-08-10. Short version: a lesson written from one closed trade is n=1, and
# feeding it back into the decision means the shadow book can no longer separate
# "the rules worked" from "the owner remembered something". The reflections are
# still written and still stored; nothing reads them at decision time.


def _fetch(ticker: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(BOT_DIR / "fetch_analysis_data.py"), ticker],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"fetch_analysis_data.py failed: {proc.stderr[-500:]}")
    return json.loads(proc.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", nargs="?")
    parser.add_argument("--from-json", metavar="PATH",
                        help="build from a saved fetch_analysis_data.py payload "
                             "instead of fetching (no TradingView needed)")
    parser.add_argument("--setup-type", help="override the computed setup type")
    parser.add_argument("--reason", help="required with --setup-type: why the computed "
                                          "call is wrong here")
    # Rule 7: the Alternate needs its own target scan from its own entry/stop.
    parser.add_argument("--alt-trigger", type=float,
                        help="the Alternate setup's own entry, so it gets a real target scan")
    parser.add_argument("--alt-stop", type=float, help="the Alternate setup's own stop")
    parser.add_argument("--alt-type", help="the Alternate's setup type, one of the six")
    args = parser.parse_args()
    if (args.alt_trigger is None) != (args.alt_stop is None):
        parser.error("--alt-trigger and --alt-stop go together: a target scan needs both, "
                     "since reward:risk is measured from the pair (rule 3)")

    if args.from_json:
        data = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    elif args.ticker:
        data = _fetch(args.ticker)
    else:
        parser.error("give a TICKER or --from-json PATH")

    plan = build(data, setup_type_override=args.setup_type, override_reason=args.reason,
                  alt_trigger=args.alt_trigger, alt_stop=args.alt_stop,
                  alt_type=args.alt_type)
    # The source facts ride along with the plan, so a screener run needs ONE
    # TradingView fetch rather than two. The fetch is by far the slowest and
    # most fragile step in the pipeline (see tv_data.py on why it cannot run
    # concurrently), and asking for it twice to get two views of the same
    # moment would also risk the two disagreeing.
    print(json.dumps({"plan": plan, "data": data}, ensure_ascii=True))


if __name__ == "__main__":
    main()
