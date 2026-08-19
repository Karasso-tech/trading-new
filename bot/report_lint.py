"""Item 3 (Hardening Pass, pre-data-fixes): deterministic post-hoc arithmetic
verification of levels Claude has ALREADY chosen -- never a second opinion on
WHICH level should have been chosen (that stays Category B, in the .md files).

Scope, precisely:
  - Recomputes a target's stated ATR multiple and R:R from (reference, stop,
    target price, atr_at_build) and flags a mismatch beyond rounding tolerance.
  - Recomputes CONSISTENCY_RULES.md rule 3's qualifying gate (>=1.5x ATR AND
    R:R>=2:1, or >=2.5:1 in the 1.0x-1.5x ATR band) and flags any item sitting
    in a setup's `targets[]` array (implicitly claimed sellable) that doesn't
    actually clear it -- it belongs in `checkpoints[]` instead.
  - Recomputes rule 4's stop-noise floor (>=0.7x ATR between reference and stop).
  - Checks rule 5/14's structural completeness (two setups, a Potential field)
    and rule 15's reversal-setup dual RS window, for SCREENER_v3 reports.
  - Checks rule 18's regime hard gate (CONSISTENCY_RULES.md): a buy decision
    is never allowed while the report's own market_regime is risk_off/
    structure_break.
  - Checks rule 22's breakout-volume sizing derate is disclosed when a
    Breakout/Retest setup's own breakout-day volume is below 100% of its
    20-day average.
  - For STRATEGY_v3/playbook reports: the same per-position gate/noise-floor
    checks, plus rule 7's allocation-table sum-<=-qty check.

Runs on the STRUCTURED decision dict each deliver_*.py script already loads
(the same dict widget_render.py's adapters consume) -- never on report_markdown
prose. A field that isn't a clean number (e.g. a pending trigger written as
free text per rule 14, "אין פקודה מוכנה; הטריגר ייקבע רק לאחר יצירת נר האישור")
is recorded in `.skipped`, never guessed at and never turned into a false
warning.

On any finding: the caller (deliver_*.py) does NOT block the send -- it
prepends a prominent warning block to the Telegram text and adds the same
findings to the widget's existing `warnings[]` array (already rendered as a
red/amber callout box, see widget_template.html), and logs the full result via
persistence.record_lint_result(). A failed lint must be impossible to miss and
impossible to mistake for a clean report -- it must never be silently swallowed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import decision_policy
import setup_types
import size_policy

# Rule 15's reversal set, from its single home (2026-08-09). This used to be
# its own literal set, one of three copies in the codebase.
REVERSAL_SETUP_TYPES = setup_types.REVERSAL_SETUPS
BREAKOUT_VOLUME_SETUP_TYPES = {"Breakout", "Retest"}
BLOCKING_REGIMES = decision_policy.BLOCKING_REGIMES
# 2026-08-02: the rubric dropped from six criteria to five and F was retired --
# D is now the bottom of the scale (see rubric_formula.SCORED_CRITERIA). The
# checks below used to test `grade == "F"`, which after that change could never
# fire again, silently turning three real gates into no-ops. "F" stays in the
# set so theses stored under the old scale keep blocking exactly as they did.
BLOCKING_GRADES = decision_policy.BLOCKING_GRADES
BUY_DECISIONS = decision_policy.BUY_DECISIONS

ATR_MULTIPLE_TOLERANCE = 0.05
RR_TOLERANCE = 0.05
STOP_NOISE_FLOOR_ATR = 0.7
STOP_BUFFER_ATR_MULT = 0.15  # rule 24, CONSISTENCY_RULES.md -- stop = level - 0.15x ATR14
TARGET_QUALIFY_MIN_ATR = 1.5
TARGET_BAND_MIN_ATR = 1.0
TARGET_QUALIFY_MIN_RR = 2.0
TARGET_BAND_MIN_RR = 2.5
TARGET_PROXIMITY_ATR = 0.5  # rule 3, changed 2026-07-31 -- take-profit heads-up distance for open positions

# Accepts a bare number, or a number with one trailing unit marker this
# codebase actually uses ("3.44x", "2.30:1", "40%") -- NOT a free-text
# sentence with a number embedded in it ("close above 112.67"). Extracting a
# number out of prose would be guessing at what the field means; skipping is
# the honest behavior per this module's own docstring.
_NUMERIC_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(x|:1|%)?\s*$", re.IGNORECASE)


def _clean_number(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = _NUMERIC_RE.match(value.replace(",", ""))
        if m:
            return float(m.group(1))
    return None


@dataclass
class LintFinding:
    check: str
    message_he: str
    detail: dict = field(default_factory=dict)


@dataclass
class LintResult:
    findings: list = field(default_factory=list)   # list[LintFinding]
    skipped: list = field(default_factory=list)     # list[str]

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "findings": [
                {"check": f.check, "message": f.message_he, "detail": f.detail}
                for f in self.findings
            ],
            "skipped": self.skipped,
        }

    def warning_lines_he(self, exclude_checks: Optional[set] = None) -> list:
        """Ready-to-send Hebrew lines, one per finding -- for the Telegram
        warning block and the widget's warnings[] array (see module docstring).

        exclude_checks lets a caller drop specific check names from the
        user-facing text while the full, unfiltered findings still go to
        persistence.record_lint_result() for audit -- see
        deliver_playbook_report.py's use of this for atr_multiple_mismatch/
        rr_mismatch, which compute_all_target_metrics() now makes moot for
        display purposes (the shown number is always its computed value
        regardless of what finding fired here)."""
        if exclude_checks:
            return [f.message_he for f in self.findings if f.check not in exclude_checks]
        return [f.message_he for f in self.findings]

    def merge(self, other: "LintResult") -> None:
        self.findings.extend(other.findings)
        self.skipped.extend(other.skipped)


def format_freshness_warning_he(freshness: Optional[dict]) -> Optional[str]:
    """Item 6 (Hardening Pass): `freshness` is tv_data.assert_data_fresh()'s result,
    copied VERBATIM into the decision JSON by fetch_analysis_data.py/
    fetch_monitor_data.py and the claude -p prompt that consumes them -- never
    recomputed here. Returns None when fresh (or the field is simply absent, e.g.
    an older decision JSON) so callers can unconditionally append the result to
    their warnings list. Never blocks the report -- same red-banner-not-a-block
    treatment as every other check in this module."""
    if not freshness or freshness.get("fresh", True):
        return None
    last = freshness.get("last_bar_date") or "?"
    return f"⚠️ הנתונים אינם עדכניים — הבר האחרון: {last}"


def failing_target_keys(result: "LintResult") -> set:
    """(setup_label, target_index) pairs -- 1-indexed, matching the position
    each target holds in its own setup's `targets` list -- for every target
    flagged as failing rule 3's qualify gate (checkpoint_mislabeled_as_target).

    Found in review (2026-07-16, real AMZN incident): this module already
    computes the objectively correct ATR-distance/R:R for every target and
    correctly flags when Claude's own stated numbers don't clear the gate --
    but until now, callers only used that to APPEND a warning next to the
    still-wrong numbers, leaving the widget/summary showing a target as if it
    were a valid sell level with a contradicting disclaimer beside it. Real
    example: AMZN's two targets were both flagged this way (R:R 2.66 shown,
    0.22 actually computed) and still rendered as normal targets. Callers now
    use this to actually remove/demote the bad target from what gets rendered
    (see deliver_report.py / deliver_playbook_report.py) -- the warning text
    still explains why, but the user is never shown two contradicting numbers
    for the same level in the same message."""
    return {
        (f.detail["setup"], f.detail["target_index"])
        for f in result.findings
        if f.check == "checkpoint_mislabeled_as_target"
    }


def compute_all_target_metrics(decision: dict, atr_lookup=None) -> dict:
    """(ticker, target_index) -> {"atr_mult": float, "rr": float} for EVERY
    target across every position in a /playbook decision dict -- computed
    purely from real data (current price, stop, atr_at_build), regardless of
    whatever atr_mult/rr text the model itself wrote for that target. 1-indexed,
    matching failing_target_keys/target_corrections' key shape (those two stay
    unchanged, still used by deliver_report.py's SCREENER_v3 path where
    atr_at_build is always freshly built, never stale).

    No "passes_gate" field (removed 2026-07-31): rule 3's validity gate is now
    checked ONCE, at entry, never re-tested against a moving current price --
    see CONSISTENCY_RULES.md rule 3 and _lint_target_proximity's docstring for
    why. This function only corrects the DISPLAYED numbers now; it never
    determines whether an already-open position's target should be dropped.

    Playbook-specific fix (found real 2026-07-20: AMZN/LLY/CRM/UPS; recurred
    2026-07-22 with CRDO/INCY -- same failure, same root cause, every day):
    unlike a fresh SCREENER_v3 build (where atr_at_build IS the current ATR,
    so this class of error is structurally impossible), /playbook re-evaluates
    an ALREADY-open position, where atr_at_build is whatever ATR was frozen at
    entry, potentially long ago and different from today's ATR. The model
    kept computing each target's stated ATR-distance/R:R against that fresher
    current ATR instead of the frozen atr_at_build STRATEGY_v3.md's prompt
    explicitly tells it to use -- the existing atr_multiple_mismatch/
    rr_mismatch checks could only catch this AFTER the fact, once per run,
    every run, forever. deliver_playbook_report.py now uses this function's
    result to overwrite EVERY target's displayed atr_mult/rr unconditionally
    (not just ones report_lint previously flagged as wrong) -- there is
    nothing left for the model to get wrong, because its own stated numbers
    for these two fields are never trusted for display in the first place."""
    metrics: dict = {}
    for pos in decision.get("positions") or []:
        ticker = pos.get("ticker")
        reference = _clean_number(pos.get("price"))
        stop = _clean_number(pos.get("stop"))
        atr_at_build = pos.get("atr_at_build")
        if atr_at_build is None and atr_lookup is not None and ticker:
            atr_at_build = atr_lookup(ticker)
        atr = _clean_number(atr_at_build)
        if not ticker or reference is None or atr is None or stop is None:
            continue
        risk = reference - stop
        if risk <= 0:
            continue
        for idx, t in enumerate(pos.get("targets") or [], start=1):
            price = _clean_number(t.get("price"))
            if price is None:
                continue
            dist_atr = abs(price - reference) / atr
            rr = (price - reference) / risk
            metrics[(ticker, idx)] = {
                "atr_mult": round(dist_atr, 4),
                "rr": round(rr, 4),
            }
    return metrics


def target_corrections(result: "LintResult") -> dict:
    """(setup_label, target_index) -> {"atr_mult": corrected_float, "rr":
    corrected_float} for every target whose DISPLAYED number didn't match
    this module's own recompute -- including ones that still pass rule 3's
    gate either way (see failing_target_keys for the ones that don't and get
    removed entirely instead of corrected).

    Found in review (2026-07-16, real NVDA/CRM/AMZN incident): a target can
    still be genuinely valid (the recomputed distance/R:R still clears the
    gate) while the model's STATED distance number is measurably wrong --
    traced to NVDA/CRM both showing an internally-consistent ~0.7-1.0 point
    higher implied reference price behind their stated multiples than the
    price actually reported that run, suggesting a slightly stale
    intermediate price read mid-session, not a gate-relevant judgment error.
    Callers use this to overwrite the displayed figure with the one this
    module already computed correctly, rather than showing the user a wrong
    number next to an otherwise-correct pass/fail conclusion."""
    corrections: dict = {}
    for f in result.findings:
        if f.check not in ("atr_multiple_mismatch", "rr_mismatch"):
            continue
        key = (f.detail["setup"], f.detail["target_index"])
        entry = corrections.setdefault(key, {})
        if f.check == "atr_multiple_mismatch":
            entry["atr_mult"] = f.detail["computed_atr_mult"]
        else:
            entry["rr"] = f.detail["computed_rr"]
    return corrections


def format_warning_block_he(result: "LintResult", exclude_checks: Optional[set] = None) -> str:
    """Ready-to-prepend HTML block for a Telegram text message (telegram_send.py's
    HTML parse_mode) -- empty string when the lint is clean, so callers can just
    unconditionally prepend this. Deliberately loud (bold header, bullet per
    finding, both numbers shown) per this module's own docstring: a failed lint
    must be impossible to miss and impossible to mistake for a clean report.

    exclude_checks passes through to warning_lines_he() (see its own docstring
    -- deliver_playbook_report.py uses this to drop atr_multiple_mismatch/
    rr_mismatch, made moot there by compute_all_target_metrics always
    overwriting the displayed number regardless of which finding fired).
    Checks result.ok first (nothing at all found) and then the FILTERED line
    count -- a decision whose only findings are excluded checks must still
    return "", not a bold header over an empty bullet list."""
    if result.ok:
        return ""
    warning_lines = result.warning_lines_he(exclude_checks=exclude_checks)
    if not warning_lines:
        return ""
    lines = "\n".join(f"• {line}" for line in warning_lines)
    # A header saying "the numeric check FAILED" over a line that begins with an
    # information mark is the message contradicting itself, and this system's
    # reader is explicitly a beginner. Some findings are notes rather than
    # errors -- an order sized down for a stated reason is the real case, seen
    # live on ORCL 2026-08-09 -- so the header follows the findings instead of
    # being fixed. Only when EVERY line is informational, so a genuine failure
    # can never be softened by one note sitting beside it.
    all_informational = all(line.lstrip().startswith("ℹ️") for line in warning_lines)
    header = ("ℹ️ <b>שים לב לפני שאתה שולח את הפקודה</b>" if all_informational
              else "⚠️ <b>בדיקת אימות מספרית נכשלה</b>")
    return f"{header}\n{lines}\n\n"


def _passes_target_gate(dist_atr: float, rr: float) -> bool:
    """CONSISTENCY_RULES.md rule 3 / SCREENER_v3.md's ATR-distance section,
    exactly: <1.0x ATR never qualifies regardless of R:R; 1.0x-1.5x ATR
    qualifies only at R:R>=2.5:1; >=1.5x ATR qualifies at R:R>=2:1."""
    if dist_atr >= TARGET_QUALIFY_MIN_ATR - 1e-9:
        return rr >= TARGET_QUALIFY_MIN_RR - 1e-9
    if dist_atr >= TARGET_BAND_MIN_ATR - 1e-9:
        return rr >= TARGET_BAND_MIN_RR - 1e-9
    return False


def _lint_target(t: dict, setup_label: str, idx: int, reference: float,
                  stop: Optional[float], atr: float, result: LintResult, *,
                  check_gate: bool = True) -> None:
    price = _clean_number(t.get("price"))
    if price is None:
        result.skipped.append(f"{setup_label} target {idx}: price not a clean number -- gate/ATR/R:R checks skipped")
        return

    dist_atr = abs(price - reference) / atr
    stated_atr_mult = _clean_number(t.get("atr_mult"))
    if stated_atr_mult is not None:
        if abs(dist_atr - stated_atr_mult) > ATR_MULTIPLE_TOLERANCE:
            result.findings.append(LintFinding(
                check="atr_multiple_mismatch",
                message_he=(f"{setup_label} יעד {idx} ({price}): מרחק ATR מוצג {stated_atr_mult:.2f}x "
                             f"אך המחושב בפועל {dist_atr:.2f}x"),
                detail={"setup": setup_label, "target_index": idx, "price": price,
                        "stated_atr_mult": stated_atr_mult, "computed_atr_mult": round(dist_atr, 4)},
            ))
    else:
        result.skipped.append(f"{setup_label} target {idx}: atr_mult not a clean number -- ATR-multiple check skipped")

    rr = None
    if stop is not None:
        risk = reference - stop
        if risk > 0:
            rr = (price - reference) / risk

    stated_rr = _clean_number(t.get("rr"))
    if rr is not None and stated_rr is not None:
        if abs(rr - stated_rr) > RR_TOLERANCE:
            result.findings.append(LintFinding(
                check="rr_mismatch",
                message_he=(f"{setup_label} יעד {idx} ({price}): R:R מוצג {stated_rr:.2f} "
                             f"אך המחושב בפועל {rr:.2f}"),
                detail={"setup": setup_label, "target_index": idx, "price": price,
                        "stated_rr": stated_rr, "computed_rr": round(rr, 4)},
            ))
    elif stated_rr is not None:
        result.skipped.append(f"{setup_label} target {idx}: R:R stated but stop unusable -- R:R check skipped")

    if not check_gate:
        return

    if rr is not None:
        if not _passes_target_gate(dist_atr, rr):
            result.findings.append(LintFinding(
                check="checkpoint_mislabeled_as_target",
                message_he=(f"{setup_label} יעד {idx} ({price}) מופיע ב-targets אך לא עומד בשער "
                             f"(מרחק {dist_atr:.2f}x ATR, R:R {rr:.2f}) — צריך Checkpoint, לא יעד"),
                detail={"setup": setup_label, "target_index": idx, "price": price,
                        "computed_atr_mult": round(dist_atr, 4), "computed_rr": round(rr, 4)},
            ))
    else:
        result.skipped.append(f"{setup_label} target {idx}: no usable stop -- gate check skipped")


def _lint_target_proximity(t: dict, ticker: str, idx: int, reference: float, atr: float,
                            result: LintResult) -> None:
    """CONSISTENCY_RULES.md rule 3, changed 2026-07-31: an open position's target is
    graded ONCE at entry (see _lint_target's check_gate=False for the playbook path)
    and never re-tested against a moving current price -- R:R shrinking as price runs
    toward a target is normal, not a sign the target went bad. What current price
    still needs to trigger is a plain take-profit heads-up: no validity judgment, just
    "you're close, decide." Fires once the position's current price is within
    TARGET_PROXIMITY_ATR of the stored target, in either direction (approaching or
    having just passed it) -- a floor check like _lint_stop_buffer, not exact-equality."""
    price = _clean_number(t.get("price"))
    if price is None:
        return
    dist_atr = abs(price - reference) / atr
    if dist_atr <= TARGET_PROXIMITY_ATR + 1e-9:
        result.findings.append(LintFinding(
            check="target_near_price",
            message_he=(f"{ticker}: יעד {idx} ({price}) קרוב למחיר הנוכחי "
                         f"(מרחק {dist_atr:.2f}x ATR) — שקול מימוש רווח"),
            detail={"ticker": ticker, "target_index": idx, "price": price,
                    "distance_atr": round(dist_atr, 4)},
        ))


def _lint_stop_buffer(level, stop, atr, label: str, result: LintResult) -> None:
    """CONSISTENCY_RULES.md rule 24, added in the 2026-07-30 full-system checkup
    (found via that review: no lint function existed for this rule at all, even
    though the rule itself was written after a real incident -- stops were
    consistently being placed at the literal candle low with no cushion
    beneath it). `level` (`stop_basis_level`) is the raw structural price the
    stop is meant to sit below -- the breakout base, retest low, Higher Low,
    Reclaim/Flush/gap candle low, or the trailing-stop's chosen daily low --
    expected copied verbatim by the model, same convention as `atr_at_build`.
    Same shape as `stop_noise_floor` above: a floor check (a stop MORE
    conservative than required is never flagged), not exact-equality, since
    rounding to a clean quote price is expected and fine."""
    level = _clean_number(level)
    if level is None or stop is None or atr is None:
        result.skipped.append(f"{label}: stop_basis_level/stop/atr_at_build not all clean numbers -- stop-buffer check skipped")
        return
    required_max_stop = level - STOP_BUFFER_ATR_MULT * atr
    if stop > required_max_stop + 1e-9:
        buffer_actual = level - stop
        buffer_required = STOP_BUFFER_ATR_MULT * atr
        result.findings.append(LintFinding(
            check="stop_buffer_missing",
            message_he=(f"{label}: סטופ {stop:.2f} קרוב מדי לרמה המבנית {level:.2f} "
                         f"(מרווח בפועל {buffer_actual:.2f}, נדרש לפחות 0.15x ATR = {buffer_required:.2f}) — כלל 24, CONSISTENCY_RULES.md"),
            detail={"setup": label, "level": level, "stop": stop, "buffer_actual": round(buffer_actual, 4),
                    "buffer_required": round(buffer_required, 4)},
        ))


def _lint_allocation_pct(targets: list, label: str, result: LintResult) -> None:
    """Rule 7's allocation-split check (CONSISTENCY_RULES.md): the exit
    percentages across a setup's targets must never sum past 100%. Added to
    SCREENER_v3 setups in the 2026-07-30 full-system checkup -- this module's
    own docstring already said rule 7 applied to STRATEGY_v3/playbook
    positions, but a fresh screener setup has no established position qty yet
    to check a qty-sum against, only percentages, so this is the pct-only
    half of _lint_position's combined check, reused here directly."""
    pct_sum = sum(v for v in (_clean_number(t.get("pct")) for t in targets) if v is not None)
    if not any(_clean_number(t.get("pct")) is not None for t in targets):
        result.skipped.append(f"{label}: allocation table has no numeric pct -- allocation-sum check skipped")
        return
    if pct_sum > 100 + 1e-6:
        result.findings.append(LintFinding(
            check="allocation_pct_exceeds_100",
            message_he=f"{label}: סך אחוזי ההקצאה בטבלה ({pct_sum:g}%) עולה על 100% (כלל 7, CONSISTENCY_RULES.md)",
            detail={"setup": label, "allocation_pct_sum": pct_sum},
        ))


def _lint_setup(setup: Optional[dict], setup_label: str, result: LintResult) -> None:
    if not setup:
        return
    reference = _clean_number(setup.get("trigger"))
    stop = _clean_number(setup.get("stop"))
    atr = _clean_number(setup.get("atr_at_build"))

    if reference is None or atr is None:
        result.skipped.append(
            f"{setup_label}: trigger/atr_at_build not both clean numbers -- all arithmetic checks skipped "
            f"(e.g. a pending trigger written as text per rule 14)"
        )
        return

    if stop is not None:
        noise = abs(reference - stop)
        floor = STOP_NOISE_FLOOR_ATR * atr
        if noise < floor - 1e-9:
            result.findings.append(LintFinding(
                check="stop_noise_floor",
                message_he=(f"{setup_label}: מרחק סטופ {noise:.2f} מתחת לרצפת הרעש "
                             f"(0.7x ATR = {floor:.2f})"),
                detail={"setup": setup_label, "distance": round(noise, 4), "floor": round(floor, 4)},
            ))
        _lint_stop_buffer(setup.get("stop_basis_level"), stop, atr, setup_label, result)
    else:
        result.skipped.append(f"{setup_label}: stop not a clean number -- noise-floor check skipped")

    targets = setup.get("targets") or []
    for i, t in enumerate(targets, start=1):
        _lint_target(t, setup_label, i, reference, stop, atr, result)
    _lint_allocation_pct(targets, setup_label, result)


def _lint_breakout_volume_disclosure(setup: Optional[dict], setup_label: str, result: LintResult) -> None:
    """CONSISTENCY_RULES.md rule 22 -- RETIRED as a finding on 2026-08-09.

    This used to flag a Breakout/Retest setup whose breakout-day volume came in
    under its 20-day average and whose sizing table did not disclose the ×0.5
    derate. That derate was removed on 2026-08-03 (the backtest measured
    below-average-volume breaks at +0.13R against +0.08R for above-average --
    the derate pointed the wrong way), so the check was demanding disclosure of
    a multiplier that no longer exists, and its very presence read as evidence
    that the multiplier was still live.

    The volume figure itself is still worth recording, so this stays as a
    `skipped` note rather than being deleted: `breakout_volume_pct_of_avg` is
    research data for the shadow book, and a Breakout/Retest setup that omits
    it silently loses that data point."""
    if not setup:
        return
    if setup.get("type") not in BREAKOUT_VOLUME_SETUP_TYPES:
        return
    if _clean_number(setup.get("breakout_volume_pct_of_avg")) is None:
        result.skipped.append(
            f"{setup_label}: breakout_volume_pct_of_avg missing -- volume is research data only "
            f"(rule 22, no sizing effect since 2026-08-03), nothing to check"
        )


def _lint_structural_completeness(decision: dict, result: LintResult) -> None:
    """Rule 5/14 (two setups) and rule 17 (Potential field), both mandatory in
    every SCREENER_v3 report; rule 15 (dual RS window) for reversal setups."""
    if not decision.get("primary_setup"):
        result.findings.append(LintFinding(
            check="missing_primary_setup",
            message_he="חסר Primary setup בדוח — כלל 5/14 מחייב סטאפ ראשי בכל תשובה",
        ))
    if not decision.get("alternate_setup"):
        result.findings.append(LintFinding(
            check="missing_alternate_setup",
            message_he="חסר Alternate setup בדוח — כלל 5 מחייב שני סטאפים בכל תשובה, כולל Watchlist/No Trade",
        ))
    if decision.get("potential") in (None, ""):
        result.findings.append(LintFinding(
            check="missing_potential_field",
            message_he="שדה 'פוטנציאל תנועה' חסר — כלל 17 מחייב אותו בכל תשובה, גם לסטאפ Pending",
        ))

    # 2026-07-30 full-system checkup: only primary_setup's type was ever
    # checked here -- a reversal-type setup (Reclaim/Failed Breakdown/
    # Gap-and-Hold) shown as the ALTERNATE silently skipped this check
    # entirely, even though rule 15 applies to either setup and metrics.
    # rs_vs_spy_5d is one shared field for the whole ticker (not per-setup),
    # so either setup being reversal-type is enough to require it.
    primary_type = (decision.get("primary_setup") or {}).get("type")
    alternate_type = (decision.get("alternate_setup") or {}).get("type")
    reversal_type = primary_type if primary_type in REVERSAL_SETUP_TYPES else (
        alternate_type if alternate_type in REVERSAL_SETUP_TYPES else None
    )
    if reversal_type:
        metrics = decision.get("metrics") or {}
        if metrics.get("rs_vs_spy_5d") is None:
            result.findings.append(LintFinding(
                check="missing_reversal_rs_5d",
                message_he=(f"סטאפ היפוכי ({reversal_type}) ללא RS 5 יום — כלל 15 מחייב את שני חלונות "
                             f"ה-RS (20 יום ו-5 יום) לסטאפים היפוכיים"),
            ))


def _lint_regime_gate(decision: dict, result: LintResult) -> None:
    """Item 4 / CONSISTENCY_RULES.md rule 18: a buy decision is never allowed
    while the report's own market_regime is risk_off/structure_break --
    maximum allowed decision in that regime is Watchlist. This is a
    consistency check on Claude's own two output fields against each other,
    not Python judging the regime itself -- the rule lives in SCREENER_v3.md."""
    regime = decision.get("market_regime")
    dec = decision.get("decision")
    if regime in BLOCKING_REGIMES and dec in BUY_DECISIONS:
        result.findings.append(LintFinding(
            check="regime_gate_violation",
            message_he=(f"⚠️ החלטה '{dec}' אסורה במצב שוק '{regime}' — מקסימום Watchlist מותר "
                         f"(כלל 18, CONSISTENCY_RULES.md)"),
            detail={"regime": regime, "decision": dec},
        ))


def _lint_rubric_grade_gate(decision: dict, result: LintResult) -> None:
    """CONSISTENCY_RULES.md rule 27 / SCREENER_v3.md:138 (never previously
    backstopped): a bottom-of-scale rubric grade is "No Trade" automatic -- a
    buy decision is never allowed alongside it. Same shape as _lint_regime_gate,
    a consistency check on Claude's own two output fields, not Python
    re-judging the grade itself. See BLOCKING_GRADES for why this reads a set
    rather than testing for "F" directly."""
    grade = decision.get("rubric_grade")
    dec = decision.get("decision")
    if grade in BLOCKING_GRADES and dec in BUY_DECISIONS:
        result.findings.append(LintFinding(
            check="rubric_grade_gate_violation",
            message_he=(f"⚠️ החלטה '{dec}' אסורה עם ציון רובריקה {grade} — No Trade אוטומטי "
                         f"(SCREENER_v3.md, כלל 27 CONSISTENCY_RULES.md)"),
            detail={"rubric_grade": grade, "decision": dec},
        ))


_ADD_ACTION = "Add Only If Confirmed"


def _lint_playbook_add_gate(decision_market_regime: Optional[str], pos: dict, result: LintResult) -> None:
    """CONSISTENCY_RULES.md rules 18/27, extended to STRATEGY_v3/playbook in
    the 2026-07-30 full-system checkup. Found in that review: both gates
    already blocked a fresh SCREENER_v3 Buy in a bad regime or on an F-graded
    rubric, but "Add Only If Confirmed" -- adding NEW risk to an already-open
    position -- had neither check at all. rubric_grade_at_build is the
    thesis's original build-time grade (persistence.get_open_position's
    t.rubric_grade, copied verbatim, never invented); market_regime is the
    same decision-level, rule-23-sourced field _lint_regime_gate already uses
    for screener. Same consistency-check-only posture as those two: this
    never re-judges the regime or grade itself, only that an Add is never
    proposed alongside either red flag."""
    ticker = pos.get("ticker", "?")
    action = pos.get("action")
    if action != _ADD_ACTION:
        return
    if decision_market_regime in BLOCKING_REGIMES:
        result.findings.append(LintFinding(
            check="playbook_add_regime_gate_violation",
            message_he=(f"⚠️ {ticker}: '{_ADD_ACTION}' אסור במצב שוק '{decision_market_regime}' "
                         f"(כלל 18, CONSISTENCY_RULES.md) — אין להוסיף סיכון חדש"),
            detail={"ticker": ticker, "regime": decision_market_regime, "action": action},
        ))
    grade = pos.get("rubric_grade_at_build")
    if grade in BLOCKING_GRADES:
        result.findings.append(LintFinding(
            check="playbook_add_rubric_gate_violation",
            message_he=(f"⚠️ {ticker}: '{_ADD_ACTION}' אסור עם ציון רובריקה {grade} בבנייה "
                         f"(כלל 27, CONSISTENCY_RULES.md) — אין להוסיף סיכון חדש"),
            detail={"ticker": ticker, "rubric_grade_at_build": grade, "action": action},
        ))


def _lint_stale_trigger_gate(decision: dict, result: LintResult) -> None:
    """MONITOR_v2.md's stale-trigger rule (2026-08-02). A trigger that first
    confirmed more than TRIGGER_STALE_TRADING_DAYS ago is reported as a fact but
    must never carry an order or a Starter quantity -- by then price has moved
    away from the level the stop was sized against, so the order implies a much
    worse trade than the one that was planned.

    `trigger_fired_age` comes straight from persistence.get_trigger_fired_age
    (real trading-day arithmetic over monitor_log), so this is the same
    consistency-check-only shape as the regime and rubric gates: it never
    decides whether a trigger is stale, only that a report which says it is
    doesn't also hand over a buy order."""
    age = decision.get("trigger_fired_age") or {}
    if not age.get("stale"):
        return
    days = age.get("trading_days")
    fired = age.get("first_green_date")
    if decision.get("order"):
        result.findings.append(LintFinding(
            check="stale_trigger_violation_order",
            message_he=(f"⚠️ הזמנה מוצגת אף שהטריגר הופעל לפני {days} ימי מסחר ({fired}) — "
                         f"טריגר ישן מדווח כעובדה בלבד, בלי פקודה (MONITOR_v2.md)"),
            detail={"trigger_fired_age": age},
        ))
    if decision.get("starter_qty"):
        result.findings.append(LintFinding(
            check="stale_trigger_violation_starter",
            message_he=(f"⚠️ אפשרות Starter מוצגת אף שהטריגר הופעל לפני {days} ימי מסחר ({fired}) — "
                         f"אסור להציע כניסה על מחיר ישן (MONITOR_v2.md)"),
            detail={"trigger_fired_age": age},
        ))


def _lint_rubric_live_gate(decision: dict, result: LintResult) -> None:
    """CONSISTENCY_RULES.md rule 27: MONITOR_v2's live re-score gate. When the
    delivered decision's own `rubric_blocked` is true (live rubric_formula_now
    grade D/F), the order/Starter fields must be absent -- same shape as
    _lint_regime_gate, a consistency check on Claude's own output fields
    against each other."""
    if not decision.get("rubric_blocked"):
        return
    if decision.get("order"):
        result.findings.append(LintFinding(
            check="rubric_gate_violation_order",
            message_he="⚠️ הזמנה מוצגת למרות rubric_blocked=true — אסור להציג פקודה בציון D/F חי (כלל 27)",
            detail={"rubric_grade_now": decision.get("rubric_grade_now")},
        ))
    if decision.get("starter_qty"):
        result.findings.append(LintFinding(
            check="rubric_gate_violation_starter",
            message_he="⚠️ אפשרות Starter מוצגת למרות rubric_blocked=true — אסור בציון D/F חי (כלל 27)",
            detail={"rubric_grade_now": decision.get("rubric_grade_now")},
        ))


def _lint_rubric_formula_match(decision: dict, result: LintResult) -> None:
    """CONSISTENCY_RULES.md rule 27. Found in the 2026-07-30 full-system checkup:
    _lint_rubric_live_gate above only ever checked Claude's own `rubric_blocked`
    claim against its own `order`/`starter_qty` fields -- pure self-consistency,
    never verified against the real number. `rubric_formula.classify_rubric()`
    (via fetch_monitor_data.py's `rubric_formula_now`) computes the actual live
    grade mechanically; `rubric_grade_formula_now` is that grade for whichever
    setup triggered, expected copied verbatim -- same convention as rule 23's
    `market_regime_formula`. Unlike rule 23, there is no disclosed-override
    allowance for rule 27: a D/F live grade must always block, no written
    reason can excuse it, so any mismatch here is a real finding. This is the
    exact failure shape the CRDO/WGMI incidents rule 27 was written to stop --
    an AI-reported 'not blocked' with nothing checking it against the formula."""
    formula_grade = decision.get("rubric_grade_formula_now")
    if formula_grade is None:
        result.skipped.append(
            "rubric formula: rubric_grade_formula_now not present -- rubric_blocked cross-check skipped"
        )
        return
    should_be_blocked = formula_grade in BLOCKING_GRADES
    is_blocked = bool(decision.get("rubric_blocked"))
    if should_be_blocked and not is_blocked:
        result.findings.append(LintFinding(
            check="rubric_blocked_missing",
            message_he=(f"⚠️ ציון הרובריקה החי הוא {formula_grade} אך rubric_blocked אינו true "
                         f"(כלל 27, CONSISTENCY_RULES.md) — הזמנה/Starter אמורים להיות מוסתרים"),
            detail={"rubric_grade_formula_now": formula_grade, "rubric_blocked": is_blocked},
        ))
    elif not should_be_blocked and is_blocked:
        result.findings.append(LintFinding(
            check="rubric_blocked_false_positive",
            message_he=(f"⚠️ rubric_blocked=true אך ציון הרובריקה החי הוא {formula_grade} (לא D/F) "
                         f"(כלל 27, CONSISTENCY_RULES.md) — נחסם ללא סיבה תואמת"),
            detail={"rubric_grade_formula_now": formula_grade, "rubric_blocked": is_blocked},
        ))


def _lint_portfolio_heat_disclosure(decision: dict, result: LintResult) -> None:
    """CONSISTENCY_RULES.md rule 19: unlike rule 18's regime gate, this never
    blocks the decision line -- it only confirms that when this trade would
    push portfolio heat over its own stored cap, the report actually
    disclosed that plainly rather than silently omitting it. Both fields are
    expected to be copied verbatim from fetch_analysis_data.py's own
    account.portfolio_heat block into the decision JSON -- never recomputed
    here (same verbatim-copy convention as the existing freshness field)."""
    heat_after = _clean_number(decision.get("portfolio_heat_after"))
    cap_pct = _clean_number(decision.get("portfolio_heat_cap_pct"))
    if heat_after is None or cap_pct is None:
        result.skipped.append(
            "portfolio heat: portfolio_heat_after/portfolio_heat_cap_pct not both clean numbers -- "
            "disclosure check skipped"
        )
        return
    if heat_after > cap_pct and not decision.get("portfolio_heat_disclosed"):
        result.findings.append(LintFinding(
            check="portfolio_heat_not_disclosed",
            message_he=(f"⚠️ חשיפת תיק לאחר הטרייד ({heat_after * 100:.1f}%) חורגת מהתקרה "
                         f"({cap_pct * 100:.1f}%) אך לא דווחה במפורש בדוח (כלל 19, CONSISTENCY_RULES.md) "
                         f"-- זו אזהרה בלבד, לא חסימת החלטה"),
            detail={"heat_after": heat_after, "cap_pct": cap_pct},
        ))


def _lint_sector_cap_disclosure(decision: dict, result: LintResult) -> None:
    """CONSISTENCY_RULES.md rule 20: same disclosure-only convention as rule
    19's portfolio-heat check -- never blocks the decision line, only
    confirms the report actually disclosed a sector-cap breach rather than
    silently omitting it. sector_pct_after/sector_cap_pct are expected to be
    copied/computed by the model from fetch_analysis_data.py's own
    account.sector_exposure/sector_cap_pct fields -- never recomputed here."""
    pct_after = _clean_number(decision.get("sector_pct_after"))
    cap_pct = _clean_number(decision.get("sector_cap_pct"))
    if pct_after is None or cap_pct is None:
        result.skipped.append(
            "sector cap: sector_pct_after/sector_cap_pct not both clean numbers -- disclosure check skipped"
        )
        return
    if pct_after > cap_pct and not decision.get("sector_disclosed"):
        result.findings.append(LintFinding(
            check="sector_cap_not_disclosed",
            message_he=(f"⚠️ חשיפת מגזר לאחר הטרייד ({pct_after * 100:.1f}%) חורגת מהתקרה "
                         f"({cap_pct * 100:.1f}%) אך לא דווחה במפורש בדוח (כלל 20, CONSISTENCY_RULES.md) "
                         f"-- זו אזהרה בלבד, לא חסימת החלטה"),
            detail={"sector_pct_after": pct_after, "cap_pct": cap_pct},
        ))


def _lint_cash_usage_disclosure(decision: dict, result: LintResult) -> None:
    """CONSISTENCY_RULES.md rule 21: same disclosure-only convention as rules
    19/20 -- never blocks the decision line or the computed size, only
    confirms the report actually disclosed it when a trade's full dollar cost
    would consume an outsized share of available cash. cash_required_usd/
    cash_available_usd/cash_usage_warn_pct are expected to be copied/computed
    by the model from fetch_analysis_data.py's own account block -- never
    recomputed here."""
    required = _clean_number(decision.get("cash_required_usd"))
    available = _clean_number(decision.get("cash_available_usd"))
    warn_pct = _clean_number(decision.get("cash_usage_warn_pct"))
    if required is None or available is None or warn_pct is None:
        result.skipped.append(
            "cash usage: cash_required_usd/cash_available_usd/cash_usage_warn_pct not all clean "
            "numbers -- disclosure check skipped"
        )
        return
    if available <= 0:
        result.skipped.append("cash usage: cash_available_usd <= 0 -- disclosure check skipped")
        return
    pct_used = required / available
    if pct_used > warn_pct and not decision.get("cash_usage_disclosed"):
        result.findings.append(LintFinding(
            check="cash_usage_not_disclosed",
            message_he=(f"⚠️ עלות הטרייד (${required:,.2f}) היא {pct_used * 100:.0f}% מהמזומן הפנוי "
                         f"(${available:,.2f}) -- חורגת מהסף ({warn_pct * 100:.0f}%) אך לא דווחה במפורש "
                         f"בדוח (כלל 21, CONSISTENCY_RULES.md) -- זו אזהרה בלבד, לא חסימת החלטה"),
            detail={"cash_required_usd": required, "cash_available_usd": available, "warn_pct": warn_pct},
        ))


def _lint_size_floor(decision: dict, result: LintResult) -> None:
    """CONSISTENCY_RULES.md rule 28 (added 2026-08-02): unlike rules 19-22's
    disclosure-only family, this one is a real arithmetic check on the order
    itself -- the size multipliers may halve a position, never quarter it, and
    nothing may push past a full 1%.

    Found in the 2026-08-02 trade review, from the real positions table: risk
    actually taken ranged 0.08%-2.26% against a stated 1% rule, with the
    typical trade at 0.2%-0.4% (three ×0.5 multipliers stacking unbounded) and
    one trade at 2.26% (CRDO, 48% of all realized losses). Nothing anywhere in
    the code looked at the final number.

    Reads the decision's own `sizing` block -- never recomputes which trigger
    or stop was chosen (Category B, unchanged), only whether the quantity that
    came out of them lands inside the allowed band. A missing/partial sizing
    block is `skipped`, same posture as every other check here: a Watchlist or
    No Trade report legitimately has no order to size."""
    sizing = decision.get("sizing")
    if not isinstance(sizing, dict):
        result.skipped.append("sizing: no sizing block on this decision -- size-bounds check skipped")
        return

    target = _clean_number(sizing.get("risk_usd_target"))
    entry = _clean_number(sizing.get("entry"))
    stop = _clean_number(sizing.get("stop"))
    qty = _clean_number(sizing.get("qty"))
    if target is None or entry is None or stop is None or qty is None:
        result.skipped.append(
            "sizing: risk_usd_target/entry/stop/qty not all clean numbers -- size-bounds check skipped"
        )
        return
    if target <= 0 or entry <= stop:
        result.skipped.append(
            f"sizing: risk_usd_target={target}, entry={entry}, stop={stop} not a sizeable long trade "
            "-- size-bounds check skipped"
        )
        return

    # The `size_multiplier_below_floor` check retired on 2026-08-09 along with
    # the multipliers themselves (rule 22/28). Every named derate is now in
    # ADVISORY_MULTIPLIER_KEYS and multiplied by nothing, so the raw product is
    # always 1.0 and the check could only ever fire on a number the sizing code
    # had already refused to use. What replaced it is the full-size check below,
    # which asks the question that actually matters now: this order is smaller
    # than one full risk unit -- did somebody choose that, or did it happen?

    risk_actual = qty * (entry - stop)
    fraction = risk_actual / target
    reduction_reason = sizing.get("size_reduction_reason")
    has_reason = isinstance(reduction_reason, str) and bool(reduction_reason.strip())

    # An order below the floor WITH a stated reason is a different fact from one
    # without, and telling them apart is the whole point of the field. Found on
    # the first live run, 2026-08-09: ORCL came back at 36% of a full position
    # because the account held $11,258 free and the full quantity needed
    # $11,173 more than that. The old wording said "this breaks rule 28, do not
    # send the order as it is" -- which reads as a mistake to correct, when the
    # real answer is "you cannot afford a proper position in this stock today".
    # Those call for opposite responses from the reader.
    if not size_policy.fraction_within_bounds(fraction) and has_reason \
            and fraction < size_policy.RISK_FRACTION_FLOOR:
        result.findings.append(LintFinding(
            check="size_below_floor_with_reason",
            message_he=(
                f"ℹ️ הפקודה מסכנת ${risk_actual:,.0f} — רק {fraction * 100:.0f}% מפוזיציה מלאה "
                f"(${target:,.0f}), והסיבה שנרשמה היא: {reduction_reason.strip()}. "
                f"זו לא טעות בחישוב — פשוט אי אפשר לקנות כאן פוזיציה בגודל מלא כרגע. "
                f"החלט אם לקנות קטן או לוותר על הרעיון"
            ),
            detail={"qty": qty, "risk_usd_actual": round(risk_actual, 2),
                    "risk_usd_target": target,
                    "risk_fraction_of_full": round(fraction, 4),
                    "size_reduction_reason": reduction_reason.strip()},
        ))
        return

    if (size_policy.fraction_within_bounds(fraction)
            and not size_policy.is_full_size(fraction)
            and not has_reason):
        result.findings.append(LintFinding(
            check="size_below_full_no_reason",
            message_he=(
                f"⚠️ הפקודה מסכנת ${risk_actual:,.0f} — רק {fraction * 100:.0f}% מפוזיציה מלאה "
                f"(${target:,.0f}), ולא נרשמה סיבה. מאז שבוטלו כל המכפילים כל טרייד נכנס בגודל מלא; "
                f"קנייה קטנה יותר דורשת סיבה כתובה בשדה size_reduction_reason (למשל אין מספיק מזומן) "
                f"(כלל 28, CONSISTENCY_RULES.md) -- זו אזהרה בלבד, לא חסימת החלטה"
            ),
            detail={"qty": qty, "risk_usd_actual": round(risk_actual, 2),
                    "risk_usd_target": target, "risk_fraction_of_full": round(fraction, 4),
                    "full_size_min": size_policy.RISK_FRACTION_FULL_MIN},
        ))
    if not size_policy.fraction_within_bounds(fraction):
        too_small = fraction < size_policy.RISK_FRACTION_FLOOR
        result.findings.append(LintFinding(
            check="size_outside_bounds",
            message_he=(
                f"⚠️ הפקודה מסכנת ${risk_actual:,.0f} — {'פחות מחצי ' if too_small else 'יותר מ'}"
                f"פוזיציה מלאה (${target:,.0f}). המותר: ${target * size_policy.RISK_FRACTION_FLOOR:,.0f} "
                f"עד ${target:,.0f} (כלל 28, CONSISTENCY_RULES.md)"
            ),
            detail={"qty": qty, "risk_usd_actual": round(risk_actual, 2),
                    "risk_usd_target": target, "risk_fraction_of_full": round(fraction, 4),
                    "floor": size_policy.RISK_FRACTION_FLOOR,
                    "ceiling": size_policy.RISK_FRACTION_CEILING},
        ))


def _lint_regime_formula_override(decision: dict, *, used_field: str, result: LintResult) -> None:
    """CONSISTENCY_RULES.md rule 23: regime_formula.py's raw call
    (`market_regime_formula`) and the regime value actually used (`used_field`
    -- `market_regime` for a screener decision, `regime_now` for a monitor
    decision) must match UNLESS `regime_override_reason` is present and
    non-empty. A silent, undisclosed deviation from the formula is exactly
    the failure mode rule 23 exists to prevent -- the AI may flag a real
    reason to differ, but never substitute its own judgment silently. This
    never judges whether the stated reason is any good, only that a real
    deviation always comes with one, never nothing."""
    formula_value = decision.get("market_regime_formula")
    used_value = decision.get(used_field)
    if not formula_value or not used_value:
        result.skipped.append(
            f"regime formula: market_regime_formula/{used_field} not both present -- "
            "override-disclosure check skipped"
        )
        return
    # market_regime_formula is a plain string for lint_screener_decision/lint_monitor_decision,
    # but lint_playbook_decision's documented shape (deliver_playbook_report.py) is the formula's
    # full dict ({"regime": ..., "score": ..., ...}) -- unwrap it here so both callers compare
    # regime label to regime label, never a dict to a string (which can never be equal, so the
    # playbook case was flagging a mismatch on every run regardless of the actual values).
    formula_regime = formula_value.get("regime") if isinstance(formula_value, dict) else formula_value
    if formula_regime != used_value and not (decision.get("regime_override_reason") or "").strip():
        result.findings.append(LintFinding(
            check="regime_override_not_disclosed",
            message_he=(f"⚠️ מצב השוק בפועל ({used_value}) שונה מקריאת הנוסחה המספרית ({formula_regime}) "
                         f"ללא נימוק מפורש (כלל 23, CONSISTENCY_RULES.md) -- override דורש סיבה כתובה"),
            detail={"market_regime_formula": formula_value, used_field: used_value},
        ))


def _lint_decision_word(decision: dict, result: LintResult) -> None:
    """CONSISTENCY_RULES.md rule 29 (2026-08-03): the decision line may never
    claim more than the facts permit -- see decision_policy.py for the four
    definitions and why they were finally written down.

    The case this exists for, from the real 2026-08-02 rescan: MSFT, RKLB, SPOT
    and ONDS all had zero qualifying targets. Three came back "No Trade" and one
    came back "Watchlist". Identical facts, two different words, because nothing
    had ever defined them. A setup with nowhere to sell is not a trade to wait
    for -- there is no trade.

    Consistency check only, in the same family as the regime and rubric gates:
    it never decides which level is a target, it reads what the report itself
    already saved."""
    stated = decision.get("decision")
    if stated not in decision_policy._RANK:
        return  # unknown/absent label -- not this check's business to invent one
    has_target = (decision_policy.has_qualifying_target(decision.get("primary_setup"))
                  or decision_policy.has_qualifying_target(decision.get("alternate_setup")))
    grade = decision.get("rubric_grade")
    regime = decision.get("market_regime")
    if decision_policy.is_decision_allowed(stated, has_target=has_target,
                                            grade=grade, regime=regime):
        return
    ceiling = decision_policy.max_allowed_decision(has_target=has_target, grade=grade,
                                                    regime=regime)
    if not has_target:
        why = "אין אף יעד כשיר בשני הסטאפים — אין מה לתכנן, לא רק למה לחכות"
    elif regime in decision_policy.BLOCKING_REGIMES:
        why = f"מצב השוק '{regime}' חוסם פקודה (כלל 18)"
    else:
        why = f"ציון רובריקה {grade} לעולם אינו פקודה (כלל 27)"
    result.findings.append(LintFinding(
        check="decision_word_too_strong",
        message_he=(f"⚠️ ההחלטה '{stated}' חזקה מדי — המקסימום המותר כאן הוא "
                     f"'{ceiling}': {why} (כלל 29)"),
        detail={"decision": stated, "max_allowed": ceiling, "has_qualifying_target": has_target,
                "rubric_grade": grade, "market_regime": regime},
    ))


def lint_screener_decision(decision: dict) -> LintResult:
    """decision: deliver_report.py's loaded JSON (see its own module docstring
    for the exact shape) -- primary_setup/alternate_setup/metrics/potential/
    market_regime/decision are all read directly; report_markdown/summary_text
    (prose) are never touched."""
    result = LintResult()
    _lint_setup(decision.get("primary_setup"), "Primary", result)
    _lint_setup(decision.get("alternate_setup"), "Alternate", result)
    _lint_breakout_volume_disclosure(decision.get("primary_setup"), "Primary", result)
    _lint_breakout_volume_disclosure(decision.get("alternate_setup"), "Alternate", result)
    _lint_structural_completeness(decision, result)
    _lint_regime_gate(decision, result)
    _lint_rubric_grade_gate(decision, result)
    _lint_decision_word(decision, result)
    _lint_regime_formula_override(decision, used_field="market_regime", result=result)
    _lint_portfolio_heat_disclosure(decision, result)
    _lint_sector_cap_disclosure(decision, result)
    _lint_cash_usage_disclosure(decision, result)
    _lint_size_floor(decision, result)
    return result


def lint_monitor_decision(decision: dict, atr_at_build: Optional[float] = None) -> LintResult:
    """decision: deliver_monitor_report.py's loaded JSON. MONITOR_v2.md's own
    decision JSON carries only `order` (price/stop/qty), not a targets array --
    it never re-derives targets, it just confirms SCREENER_v3's trigger fired --
    so only the stop-noise-floor check applies here, and only when an ATR figure
    is actually available. Callers should pass the ticker's stored
    atr_at_build (persistence.get_atr_at_build) when there's no fresher figure
    in the dict itself -- never fabricated here.

    Rules 19/20/21 disclosure checks added 2026-07-30 full-system checkup: a
    green ticker's real buy order carries the same portfolio-wide risk as a
    fresh screener buy, but this report used to have none of that disclosure
    checked (fetch_monitor_data.py now computes the same account fields
    fetch_analysis_data.py already did) -- all three skip silently, same as
    everywhere else, when the corresponding fields are absent (e.g. no real
    order this check, or an older decision JSON predating this)."""
    result = LintResult()
    _lint_regime_formula_override(decision, used_field="regime_now", result=result)
    _lint_rubric_live_gate(decision, result)
    _lint_stale_trigger_gate(decision, result)
    _lint_rubric_formula_match(decision, result)
    _lint_portfolio_heat_disclosure(decision, result)
    _lint_sector_cap_disclosure(decision, result)
    _lint_cash_usage_disclosure(decision, result)
    order = decision.get("order") or {}
    if not order:
        return result
    reference = _clean_number(order.get("price"))
    stop = _clean_number(order.get("stop"))
    atr = _clean_number(atr_at_build)

    if reference is None or stop is None or atr is None:
        result.skipped.append("monitor order: price/stop/atr_at_build not all clean numbers -- noise-floor check skipped")
        return result

    noise = abs(reference - stop)
    floor = STOP_NOISE_FLOOR_ATR * atr
    if noise < floor - 1e-9:
        result.findings.append(LintFinding(
            check="stop_noise_floor",
            message_he=f"פקודה: מרחק סטופ {noise:.2f} מתחת לרצפת הרעש (0.7x ATR = {floor:.2f})",
            detail={"distance": round(noise, 4), "floor": round(floor, 4)},
        ))
    return result


def _lint_position(pos: dict, atr_at_build: Optional[float], result: LintResult) -> None:
    ticker = pos.get("ticker", "?")
    reference = _clean_number(pos.get("price"))  # current price -- only for stop-noise/take-profit-heads-up below, never target validity (rule 3, changed 2026-07-31)
    stop = _clean_number(pos.get("stop"))
    atr = _clean_number(atr_at_build)

    if reference is None or atr is None:
        result.skipped.append(f"{ticker}: price/atr_at_build not both clean numbers -- arithmetic checks skipped")
        return

    if stop is not None:
        noise = abs(reference - stop)
        floor = STOP_NOISE_FLOOR_ATR * atr
        if noise < floor - 1e-9:
            result.findings.append(LintFinding(
                check="stop_noise_floor",
                message_he=f"{ticker}: מרחק סטופ {noise:.2f} מתחת לרצפת הרעש (0.7x ATR = {floor:.2f})",
                detail={"ticker": ticker, "distance": round(noise, 4), "floor": round(floor, 4)},
            ))
        _lint_stop_buffer(pos.get("stop_basis_level"), stop, atr, ticker, result)
    else:
        result.skipped.append(f"{ticker}: stop not a clean number -- noise-floor check skipped")

    targets = pos.get("targets") or []
    for i, t in enumerate(targets, start=1):
        _lint_target(t, ticker, i, reference, stop, atr, result, check_gate=False)
        _lint_target_proximity(t, ticker, i, reference, atr, result)

    qty = _clean_number(pos.get("qty"))
    qty_sum = sum(v for v in (_clean_number(t.get("qty")) for t in targets) if v is not None)
    if qty is not None and any(_clean_number(t.get("qty")) is not None for t in targets):
        if qty_sum > qty + 1e-9:
            result.findings.append(LintFinding(
                check="allocation_qty_exceeds_position",
                message_he=(f"{ticker}: סך כמויות המכירה בטבלת ההקצאה ({qty_sum:g}) עולה על כמות "
                             f"הפוזיציה בפועל ({qty:g})"),
                detail={"ticker": ticker, "allocation_qty_sum": qty_sum, "position_qty": qty},
            ))
    elif not any(_clean_number(t.get("qty")) is not None for t in targets):
        pct_sum = sum(v for v in (_clean_number(t.get("pct")) for t in targets) if v is not None)
        if pct_sum > 100 + 1e-6:
            result.findings.append(LintFinding(
                check="allocation_pct_exceeds_100",
                message_he=f"{ticker}: סך אחוזי ההקצאה בטבלה ({pct_sum:g}%) עולה על 100%",
                detail={"ticker": ticker, "allocation_pct_sum": pct_sum},
            ))
        else:
            result.skipped.append(f"{ticker}: allocation table has no numeric qty -- qty-sum-vs-position check skipped")


def _patch_combined_cell(markdown: str, price: str, atr_s: str, rr_s: str,
                          new_atr_s: Optional[str], new_rr_s: Optional[str], *,
                          checkpoint: bool) -> str:
    """STRATEGY_v3/playbook table format: one cell holds both figures together,
    e.g. '258.60 | 1.50x ATR / R:R 2.66'. No-op (returns markdown unchanged) if
    that exact substring isn't present -- e.g. this is a SCREENER_v3 report
    using the split-cell format _patch_split_cells handles instead."""
    old = f"{price} | {atr_s} ATR / R:R {rr_s}"
    if checkpoint:
        new = f"~~{price}~~ | Checkpoint בלבד — נכשל בשער (ראה אזהרה למעלה)"
    else:
        new = f"{price} | {new_atr_s or atr_s} ATR / R:R {new_rr_s or rr_s}"
    return markdown.replace(old, new, 1)


def _patch_split_cells(markdown: str, price: str, atr_s: str, rr_s: str,
                        new_atr_s: Optional[str], new_rr_s: Optional[str], *,
                        checkpoint: bool) -> str:
    """SCREENER_v3 table format: separate cells per target row, e.g.
    '| 130.92 | **40%** | ... | 6.14x | **5.50:1** OK | כשיר |'. Scoped to the
    single line containing this target's own price cell (`| price |`) so a
    numeric coincidence elsewhere in the document is never touched; a no-op if
    that row isn't found (e.g. this is a playbook report using the combined-
    cell format _patch_combined_cell handles instead)."""
    lines = markdown.split("\n")
    row_marker = f"| {price} |"
    for i, line in enumerate(lines):
        if row_marker not in line:
            continue
        if checkpoint:
            line = line.replace(row_marker, f"| ~~{price}~~ |", 1)
            line = line.replace(atr_s, "Checkpoint", 1)
            line = line.replace(f"{rr_s}:1", "נכשל בשער", 1)
            line = line.replace(" כשיר |", " נכשל בשער — Checkpoint |", 1)
        else:
            if new_atr_s:
                line = line.replace(atr_s, new_atr_s, 1)
            if new_rr_s:
                line = line.replace(f"{rr_s}:1", f"{new_rr_s}:1", 1)
        lines[i] = line
        break
    return "\n".join(lines)


def patch_report_markdown(markdown: str, stated: dict, bad_targets: set, corrections: dict) -> str:
    """Surgically fixes the exact table-cell text for each target report_lint
    already flagged, in the free-text `report_markdown` string that
    deliver_report.py/deliver_playbook_report.py render straight into the PDF
    and the saved .md file.

    Found in review (2026-07-20, real AMZN/LLY/CRM/UPS incident): the existing
    fix (failing_target_keys/target_corrections, see their own docstrings)
    only ever patched the STRUCTURED decision dict that feeds the widget PNG --
    report_markdown was never touched, so the PDF (captioned "full report",
    arguably the more authoritative document) kept showing the exact wrong
    numbers report_lint had already flagged, and kept presenting
    checkpoint-only levels as live sell targets. That's the same
    two-contradicting-numbers failure this module's fixes exist to prevent
    (see failing_target_keys's docstring) -- just moved from within one
    document to between the PNG and the PDF.

    stated: (label, target_index) -> {"price", "atr_mult", "rr"} using each
    target's ORIGINAL (pre-correction) stated values -- i.e. captured by the
    caller from its own per-position/per-setup loop BEFORE it overwrites
    atr_mult/rr on the structured dict, since that original text is exactly
    what's still sitting in report_markdown's table row. A key missing from
    `stated` (or holding a non-string/empty field) is silently skipped -- never
    guessed at, same convention as the rest of this module. Tries both known
    table formats (STRATEGY_v3's combined cell, SCREENER_v3's split cells) for
    every key; whichever doesn't match this report's actual format is a no-op.
    """
    for key, vals in stated.items():
        price, atr_s, rr_s = vals.get("price"), vals.get("atr_mult"), vals.get("rr")
        if not price or not atr_s or not rr_s:
            continue
        price, atr_s, rr_s = str(price), str(atr_s), str(rr_s)
        checkpoint = key in bad_targets
        fix = {} if checkpoint else corrections.get(key, {})
        if not checkpoint and not fix:
            continue
        new_atr_s = f"{fix['atr_mult']:.2f}x" if "atr_mult" in fix else None
        new_rr_s = f"{fix['rr']:.2f}" if "rr" in fix else None
        markdown = _patch_combined_cell(markdown, price, atr_s, rr_s, new_atr_s, new_rr_s, checkpoint=checkpoint)
        markdown = _patch_split_cells(markdown, price, atr_s, rr_s, new_atr_s, new_rr_s, checkpoint=checkpoint)
    return markdown


def lint_position_status_decision(decision: dict) -> LintResult:
    """decision: deliver_position_status_report.py's loaded JSON. Added
    2026-07-30 full-system checkup -- this report ran ZERO lint checks of any
    kind before this, the only deliver_*.py script with none. price/stop/
    entry_date are the fields deliver_position_status_report.py itself uses
    to deterministically compute the stop-distance-in-dollars/days-in-trade
    line appended to each headline -- a missing field here is a real finding
    (not a silent skip), since the schema now requires them for every result."""
    result = LintResult()
    for r in decision.get("results") or []:
        ticker = r.get("ticker", "?")
        price = _clean_number(r.get("price"))
        stop = _clean_number(r.get("stop"))
        if price is None or stop is None:
            result.findings.append(LintFinding(
                check="position_status_missing_stop_distance_fields",
                message_he=f"⚠️ {ticker}: חסרים price/stop -- לא ניתן להציג מרחק לסטופ בדולרים",
                detail={"ticker": ticker},
            ))
        if not r.get("entry_date"):
            result.findings.append(LintFinding(
                check="position_status_missing_entry_date",
                message_he=f"⚠️ {ticker}: חסר entry_date -- לא ניתן להציג ימי מסחר בפוזיציה",
                detail={"ticker": ticker},
            ))
    return result


def lint_playbook_decision(decision: dict, atr_lookup=None) -> LintResult:
    """decision: deliver_playbook_report.py's loaded JSON -- one entry per
    position. `atr_lookup` is an optional `ticker -> Optional[float]` callable
    (deliver_playbook_report.py passes persistence.get_atr_at_build) since a
    position dict itself carries no ATR figure -- the thesis's/position's own
    stored atr_at_build is the source of truth here, never invented.

    Also checks rule 23's regime-formula-override disclosure (2026-07-20):
    STRATEGY_v3.md's own market-status section now sources its regime call the
    same way SCREENER_v3/MONITOR_v2 already do -- copied verbatim from
    `market_regime_formula.regime`, never a fresh eyeballed read -- so this
    reuses the exact same check, keyed on `market_regime`. A no-op (goes to
    `.skipped`) whenever either field is absent, e.g. an older decision JSON
    predating this field."""
    result = LintResult()
    _lint_regime_formula_override(decision, used_field="market_regime", result=result)
    market_regime = decision.get("market_regime")
    for pos in decision.get("positions") or []:
        ticker = pos.get("ticker")
        atr_at_build = pos.get("atr_at_build")
        if atr_at_build is None and atr_lookup is not None and ticker:
            atr_at_build = atr_lookup(ticker)
        _lint_position(pos, atr_at_build, result)
        _lint_playbook_add_gate(market_regime, pos, result)
    return result
