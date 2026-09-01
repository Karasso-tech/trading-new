"""Where an open position's stop belongs today, computed rather than judged
(2026-08-09).

`/playbook` re-decides every open position's stop on every run. STRATEGY_v3
already spells the method out completely -- start from the stored current_stop,
move it up only to the highest daily low that still clears rule 4's 0.7x ATR
noise floor, put rule 24's 0.15x ATR buffer underneath that low, and never move
it down. There is nothing left in that sentence to decide, so it is arithmetic,
and arithmetic that decides where real money exits should not be retyped by
hand on every run.

The ATR used is the position's own `atr_at_build`, frozen at entry -- never a
freshly recomputed one. Judging an existing stop against today's ATR is the
exact recurring error report_lint exists to catch (AMZN/LLY/CRM/UPS, twice in
July 2026), and it quietly re-rates every open position whenever volatility
moves.

    python bot/trail_stop.py TICKER            # fetch and compute
    python bot/trail_stop.py --from-json PATH  # from a saved fetch payload

Prints one JSON object. It never writes anything: `/playbook` still persists the
stop through deliver_playbook_report.py, and persistence.update_current_stop's
monotonic guard is still the last word.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import level_picker

BOT_DIR = Path(__file__).resolve().parent


def compute(data: dict) -> dict:
    """The trailed stop for one fetch_analysis_data.py payload."""
    position = data.get("open_position") or {}
    current_stop = position.get("current_stop")
    if current_stop is None:
        return {
            "ticker": data.get("ticker"),
            "error": "no open position on file for this ticker -- nothing to trail. "
                     "A fresh entry's stop comes from build_plan.py instead.",
        }

    # CONSISTENCY_RULES.md rule 8: a Core/Layer1 holding gets a wide structural
    # stop only -- no numeric target, no tranche table, none of rules 1-7. It is
    # not a swing trade being managed, it is an allocation being held.
    #
    # This guard is here because the Runner-only unlock below would otherwise
    # have caught them. Core positions have no targets, so `build_tranche_plan`
    # marks them `runner_only` for the same mechanical reason a rule-6 position
    # is marked so -- and the live book has two of them, SPY and QQQ. Reading
    # "no targets" as "trail it" would have started ratcheting the structural
    # stop under the two positions this system is least entitled to touch.
    # `sleeve` is folded into every fetch_analysis_data.py payload already; a
    # position whose sleeve is unknown is NOT trailed either, same "if unclear,
    # ask instead of assume" posture sleeve.py itself was written to enforce.
    sleeve = data.get("sleeve")
    if sleeve != "swing":
        return {
            "ticker": data.get("ticker"),
            "sleeve": sleeve,
            "stop_now": float(current_stop),
            "stop_should_be": float(current_stop),
            "moved": False,
            "reason": (f"sleeve is {sleeve or 'unknown'}, not swing -- rule 8 exempts a Core "
                        f"holding from tranche and trailing management entirely, and an "
                        f"unclassified position is never trailed on an assumption. Set the "
                        f"sleeve first if this really is a swing trade."),
        }

    # The frozen ATR, with the live one as a stated fallback only when the
    # position predates the field. Never silently swapped -- which one was used
    # is reported, because it changes the answer.
    entry_setup = position.get("entry_setup") or {}
    atr = entry_setup.get("atr_at_build")
    atr_source = "atr_at_build (frozen at entry)"
    if not atr:
        atr = data.get("atr14")
        atr_source = "current ATR14 -- this position has no atr_at_build on file"

    # Has the first target actually been sold at? Read from the position's own
    # tranche plan, which persistence builds from the real `exits` rows -- never
    # inferred from price having merely touched the level. See level_picker's
    # own comment for why this gates the whole thing.
    tranche_plan = position.get("tranche_plan") or {}
    tranches = tranche_plan.get("tranches") or []
    past_target_1 = any(
        t.get("label", "").startswith("target") and (t.get("filled_qty") or 0) > 0
        for t in tranches
    )

    # Rule 6's Runner-only position has NO numbered target, by definition
    # (`build_tranche_plan` sets runner_only when the position has no priced
    # target at all). So the gate above could never once become true for one,
    # and its stop could never move -- on a position where the trailing stop is
    # not one exit rule among several, it is the ENTIRE exit method. A Runner-only
    # trade could run for months with its stop still sitting where it was opened.
    #
    # Found 2026-08-30, by reading the two conditions against each other. It is
    # not a judgement call about how tight to trail: the rule this file already
    # implements says the Runner trails, and this position is all Runner.
    # `trade_sim.py` has always simulated it that way too ("rule 6's runner-only
    # case: the whole position is a runner from bar one, trailing from the
    # start") -- so until now the backtest and the live system managed the same
    # trade by two different rules.
    #
    # Nothing else loosens: rule 4's noise floor, rule 24's buffer and the
    # never-move-down guard all still apply, exactly as after a real target 1.
    runner_only = bool(tranche_plan.get("runner_only"))
    if runner_only:
        past_target_1 = True

    result = level_picker.trail_stop(
        current_price=data.get("current_price"),
        current_stop=float(current_stop),
        atr_at_build=atr,
        swing_lows=data.get("swing_lows_recent") or [],
        past_target_1=past_target_1,
        bars=data.get("recent_bars_40") or [],
    )
    # Is the level behind this stop actually NEW structure, or an older low the
    # price has since climbed back over? STRATEGY_v3 asks for "real NEW
    # structure since entry", and the difference is not academic: on the first
    # live run, ASTS came back wanting its stop lifted from 54.78 to 66.53 on
    # the strength of a low dated 2026-04-29 -- three months before the position
    # was opened. That may still be the right level; it is emphatically not the
    # same claim as "the trade has made a new higher low". Reported, never
    # silently applied.
    entry_date = (position.get("entry_date") or "")[:10]
    basis_after_entry = None
    if result.basis_date and entry_date:
        basis_after_entry = result.basis_date >= entry_date

    # A low dated before the position was opened is NOT a higher low this trade
    # has made -- it is an older level price has since climbed back over. Until
    # 2026-08-30 this was reported as a caution and the stop was moved anyway,
    # which made the two facts fight each other in the same message: "raise your
    # stop to 66.53" next to "this level is from three months before you bought".
    #
    # The real case, first live run: ASTS came back wanting its stop lifted from
    # 54.78 to 66.53 on the strength of a 2026-04-29 low, on a position opened in
    # July. That may well still be a real support level. It is emphatically not
    # evidence the trade has made new structure, which is the thing
    # STRATEGY_v3's post-tranche method is actually asking about.
    #
    # So it is shown, named and dated -- and it does not move the stop by itself.
    # Only structure built since entry does that automatically. Nothing is
    # hidden; what changes is that an old level no longer acts on its own.
    old_structure_hold = bool(result.moved and basis_after_entry is False)
    if old_structure_hold:
        result = dataclasses.replace(
            result, stop=float(current_stop), moved=False,
            reason=(f"the highest qualifying low is {result.basis_level:.2f}, dated "
                    f"{result.basis_date} -- BEFORE this position was opened on "
                    f"{entry_date}. That is an older support level price has climbed back "
                    f"over, not a higher low this trade has made, so it does not raise the "
                    f"stop on its own. Shown so it can be used deliberately, not silently."),
        )

    out = {
        "ticker": data.get("ticker"),
        "current_price": data.get("current_price"),
        "stop_now": float(current_stop),
        "stop_should_be": result.stop,
        "moved": result.moved,
        # True when a stop raise was available but rests only on pre-entry
        # structure. The level itself is still reported below.
        "held_on_pre_entry_structure": old_structure_hold,
        "stop_basis_level": result.basis_level,
        "stop_basis_date": result.basis_date,
        "basis_after_entry": basis_after_entry,
        "reason": result.reason,
        "atr_used": atr,
        "atr_source": atr_source,
        "entry_price": position.get("entry_price"),
        "entry_date": entry_date or None,
        "initial_stop": position.get("initial_stop"),
        "past_target_1": past_target_1,
        # Reported so a reader can tell WHY the stop was allowed to move: a real
        # first tranche sold, or rule 6's no-target-at-all case.
        "runner_only": runner_only,
    }
    if old_structure_hold:
        out["caution"] = (
            f"the {result.basis_level:.2f} low is dated {result.basis_date}, BEFORE this position "
            f"was opened on {entry_date} -- it is an older level price has climbed back over, not a "
            f"new higher low the trade has made. The stop was NOT raised to it. Treat it as an "
            f"old support level, and move the stop there only as a deliberate decision."
        )
    if result.moved and data.get("current_price"):
        out["distance_atr"] = (data["current_price"] - result.stop) / atr
    return out


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
    parser.add_argument("--from-json", metavar="PATH")
    args = parser.parse_args()

    if args.from_json:
        data = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    elif args.ticker:
        data = _fetch(args.ticker)
    else:
        parser.error("give a TICKER or --from-json PATH")
    print(json.dumps(compute(data), ensure_ascii=True))


if __name__ == "__main__":
    main()
