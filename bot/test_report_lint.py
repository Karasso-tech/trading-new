"""Unit tests for report_lint.py -- Hardening Pass item 3.

Synthetic structured dicts only (the same shape deliver_report.py/
deliver_monitor_report.py/deliver_playbook_report.py load from disk) -- no live
data, no DB. Per report_lint.py's own docstring: this proves the arithmetic
re-check itself is correct, it is not a second opinion on which level should
have been chosen.
"""

import pytest

import report_lint
import setup_types
from report_lint import (
    lint_monitor_decision,
    lint_playbook_decision,
    lint_position_status_decision,
    lint_screener_decision,
)


def _clean_primary_setup() -> dict:
    # trigger=100, stop=97 -> noise = 3.0, atr=2.0, floor = 0.7*2 = 1.4 -- passes.
    # target price=106 -> dist = |106-100|/2 = 3.0x ATR (>=1.5x band, needs RR>=2:1).
    # risk = 100-97 = 3, reward = 106-100 = 6, rr = 2.0 -- passes the gate exactly.
    return {
        "type": "Breakout", "trigger": 100.0, "stop": 97.0, "atr_at_build": 2.0,
        "targets": [{"price": 106.0, "pct": "40%", "atr_mult": "3.00x", "rr": "2.00", "status": "pass"}],
        "checkpoints": [],
    }


def _clean_alternate_setup() -> dict:
    return {
        "type": "Pullback", "trigger": 95.0, "stop": 92.0, "atr_at_build": 2.0,
        "targets": [{"price": 101.0, "pct": "40%", "atr_mult": "3.00x", "rr": "2.00", "status": "pass"}],
        "checkpoints": [],
    }


def _decision_graded_d() -> dict:
    """A decision whose own disclosed numbers genuinely compute to D -- 0 of 5.

    The gate reads the recomputed letter, not the claimed one, so a test that
    only relabels the field is testing the label rather than the gate."""
    decision = _clean_decision()
    decision["rubric_inputs"] = {"rr": 1.0, "target_atr_multiple": 0.9,
                                  "regime": "risk_off", "rs_delta_pct": -3.0,
                                  "dist_sma20_atr": 3.5, "earnings_days_out": 4}
    decision["grade"] = "D"
    decision.pop("rubric_grade", None)
    return decision


def _clean_decision() -> dict:
    return {
        "ticker": "TEST", "decision": "Buy Now", "grade": "B",
        # The five numbers behind the letter, matching _clean_primary_setup:
        # rr 2.0 fails the 2.3 bar, the other four pass -> 4/5 -> B. Without
        # this block nothing can check the grade, and rule 27's recompute is a
        # gate, so an unverifiable "B" would (correctly) refuse to carry a Buy.
        "rubric_inputs": {"rr": 2.0, "target_atr_multiple": 3.0,
                           "regime": "healthy_uptrend", "rs_delta_pct": 5.0,
                           "dist_sma20_atr": 0.5, "earnings_days_out": 40},
        "primary_setup": _clean_primary_setup(),
        "alternate_setup": _clean_alternate_setup(),
        "metrics": {"rs_vs_spy_20d": 5.0},
        "potential": "120.00", "potential_note": "Measured Move",
        "market_regime": "healthy_uptrend",
    }


def test_fully_clean_decision_has_no_findings():
    result = lint_screener_decision(_clean_decision())
    assert result.ok
    assert result.findings == []


def test_wrong_atr_multiple_is_flagged():
    decision = _clean_decision()
    # Stated 1.10x but the real distance is still 3.00x (target/trigger/atr unchanged)
    # -- a report claiming "1.1x ATR" when the numbers say 3.0x, exactly the class of
    # bug this item exists to catch.
    decision["primary_setup"]["targets"][0]["atr_mult"] = "1.10x"
    result = lint_screener_decision(decision)
    assert not result.ok
    checks = {f.check for f in result.findings}
    assert "atr_multiple_mismatch" in checks
    finding = next(f for f in result.findings if f.check == "atr_multiple_mismatch")
    assert finding.detail["stated_atr_mult"] == 1.10
    assert finding.detail["computed_atr_mult"] == pytest.approx(3.0)


def test_target_corrections_gives_the_verified_number_for_a_still_qualifying_target():
    decision = _clean_decision()
    # Stated 1.10x but the real distance is still 3.00x -- still clears the
    # >=1.5x-ATR/RR>=2 gate either way (stated rr also still 2.00, unchanged),
    # so this target survives (not in failing_target_keys) but its displayed
    # number is still wrong and needs correcting.
    decision["primary_setup"]["targets"][0]["atr_mult"] = "1.10x"
    result = lint_screener_decision(decision)
    assert report_lint.failing_target_keys(result) == set()  # still a valid target
    corrections = report_lint.target_corrections(result)
    assert corrections == {("Primary", 1): {"atr_mult": pytest.approx(3.0)}}


def test_checkpoint_mislabeled_as_target_is_flagged():
    decision = _clean_decision()
    # price=102 -> dist = |102-100|/2 = 1.0x ATR (the 1.0x-1.5x band, needs RR>=2.5:1).
    # risk=3, reward=2, rr=0.667 -- correctly STATED (so ATR-multiple/RR-mismatch
    # checks both pass), but the target itself fails the qualifying gate and should
    # never have been left in targets[] as a sellable level.
    decision["primary_setup"]["targets"][0] = {
        "price": 102.0, "pct": "40%", "atr_mult": "1.00x", "rr": "0.67", "status": "pass",
    }
    result = lint_screener_decision(decision)
    assert not result.ok
    checks = {f.check for f in result.findings}
    assert "checkpoint_mislabeled_as_target" in checks
    assert "atr_multiple_mismatch" not in checks
    assert "rr_mismatch" not in checks


def test_failing_target_keys_identifies_the_exact_bad_target():
    decision = _clean_decision()
    decision["primary_setup"]["targets"][0] = {
        "price": 102.0, "pct": "40%", "atr_mult": "1.00x", "rr": "0.67", "status": "pass",
    }
    result = lint_screener_decision(decision)
    assert report_lint.failing_target_keys(result) == {("Primary", 1)}


def test_failing_target_keys_empty_for_a_clean_decision():
    result = lint_screener_decision(_clean_decision())
    assert report_lint.failing_target_keys(result) == set()


class TestComputeAllTargetMetrics:
    """2026-07-22 fix -- the recurring real failure (2026-07-20 AMZN/LLY/CRM/UPS,
    same tickers again 2026-07-22 with CRDO/INCY) this exists to eliminate at
    the root: /playbook re-evaluates an ALREADY-open position, so
    atr_at_build (frozen at entry) can genuinely differ from today's current
    ATR -- unlike a fresh SCREENER_v3 build, where they're the same number.
    Every target gets a real computed value here regardless of what the model
    stated, so deliver_playbook_report.py can always overwrite the displayed
    figure instead of trusting the model's arithmetic."""

    def _decision(self, **target_overrides):
        target = {"price": 258.60, "pct": "40%"}
        target.update(target_overrides)
        return {
            "positions": [
                {"ticker": "AMZN", "qty": 50, "price": 254.96, "stop": 238.25,
                 "atr_at_build": 8.59, "targets": [target]},
            ]
        }

    def test_computes_correct_metrics_regardless_of_wildly_wrong_stated_values(self):
        # Real AMZN incident numbers: stated 2.10x/2.66 (from avg cost, not
        # current price) vs the real dist=|258.60-254.96|/8.59=0.42x,
        # rr=(258.60-254.96)/(254.96-238.25)=0.218.
        decision = self._decision(atr_mult="2.10x", rr="2.66")
        metrics = report_lint.compute_all_target_metrics(decision)
        assert metrics[("AMZN", 1)]["atr_mult"] == pytest.approx(0.4238, abs=1e-3)
        assert metrics[("AMZN", 1)]["rr"] == pytest.approx(0.2180, abs=1e-3)

    def test_computes_correct_metrics_when_model_provides_no_atr_mult_or_rr_at_all(self):
        # 2026-07-22: the prompt no longer asks the model to get these right --
        # must not depend on atr_mult/rr being present in the target dict at all.
        decision = self._decision()  # no atr_mult/rr key
        metrics = report_lint.compute_all_target_metrics(decision)
        assert ("AMZN", 1) in metrics
        assert metrics[("AMZN", 1)]["atr_mult"] == pytest.approx(0.4238, abs=1e-3)

    def test_falls_back_to_atr_lookup_when_atr_at_build_absent_from_position(self):
        decision = self._decision()
        del decision["positions"][0]["atr_at_build"]
        metrics = report_lint.compute_all_target_metrics(decision, atr_lookup=lambda t: 8.59)
        assert ("AMZN", 1) in metrics
        assert metrics[("AMZN", 1)]["atr_mult"] == pytest.approx(0.4238, abs=1e-3)

    def test_skips_a_position_with_no_usable_atr_and_does_not_guess(self):
        decision = self._decision()
        del decision["positions"][0]["atr_at_build"]
        metrics = report_lint.compute_all_target_metrics(decision, atr_lookup=lambda t: None)
        assert metrics == {}

    def test_no_gate_verdict_in_metrics(self):
        # Rule 3, changed 2026-07-31: an open position's target is graded once
        # at entry, never re-tested against a moving current price, so this
        # function no longer judges validity at all -- only atr_mult/rr.
        decision = self._decision(price=300.00)
        metrics = report_lint.compute_all_target_metrics(decision)
        assert "passes_gate" not in metrics[("AMZN", 1)]


class TestWarningLinesExcludeChecks:
    def _decision_with_bad_target_and_mismatch(self):
        decision = _clean_decision()
        # Fails the qualify gate (checkpoint_mislabeled_as_target) AND has a
        # wrong stated atr_mult (atr_multiple_mismatch) at the same time.
        decision["primary_setup"]["targets"][0] = {
            "price": 102.0, "pct": "40%", "atr_mult": "9.99x", "rr": "0.67", "status": "pass",
        }
        return decision

    def test_exclude_checks_drops_only_the_named_checks(self):
        result = lint_screener_decision(self._decision_with_bad_target_and_mismatch())
        checks_present = {f.check for f in result.findings}
        assert "atr_multiple_mismatch" in checks_present
        assert "checkpoint_mislabeled_as_target" in checks_present

        filtered = result.warning_lines_he(exclude_checks={"atr_multiple_mismatch", "rr_mismatch"})
        assert not any("מרחק ATR מוצג" in line for line in filtered)
        assert any("לא עומד בשער" in line for line in filtered)

    def test_no_exclude_checks_is_unchanged(self):
        result = lint_screener_decision(self._decision_with_bad_target_and_mismatch())
        assert result.warning_lines_he() == result.warning_lines_he(exclude_checks=None)

    def test_format_warning_block_returns_empty_when_every_finding_is_excluded(self):
        decision = _clean_decision()
        decision["primary_setup"]["targets"][0]["atr_mult"] = "1.10x"  # only a mismatch, gate still passes
        result = lint_screener_decision(decision)
        assert not result.ok  # a real finding exists...
        block = report_lint.format_warning_block_he(
            result, exclude_checks={"atr_multiple_mismatch", "rr_mismatch"}
        )
        assert block == ""  # ...but it's fully excluded, so no header-over-nothing


def test_patch_report_markdown_corrects_combined_cell_playbook_format():
    # STRATEGY_v3/playbook table: one cell holds both figures, e.g.
    # '106.00 | 1.10x ATR / R:R 2.00'. Stated atr_mult is wrong (1.10x vs the
    # real 3.00x); the target still clears the gate, so it should be corrected
    # in place, not struck out.
    markdown = "| AAPL | Breakout | 100.00 | ... | 106.00 | 1.10x ATR / R:R 2.00 | 40% |\n"
    stated = {("AAPL", 1): {"price": "106.00", "atr_mult": "1.10x", "rr": "2.00"}}
    patched = report_lint.patch_report_markdown(
        markdown, stated, bad_targets=set(), corrections={("AAPL", 1): {"atr_mult": 3.0}}
    )
    assert "106.00 | 3.00x ATR / R:R 2.00" in patched
    assert "1.10x" not in patched


def test_patch_report_markdown_demotes_failing_target_to_checkpoint_combined_cell():
    markdown = "| AAPL | Breakout | 100.00 | ... | 102.00 | 1.00x ATR / R:R 0.67 | 40% |\n"
    stated = {("AAPL", 1): {"price": "102.00", "atr_mult": "1.00x", "rr": "0.67"}}
    patched = report_lint.patch_report_markdown(
        markdown, stated, bad_targets={("AAPL", 1)}, corrections={}
    )
    assert "~~102.00~~" in patched
    assert "Checkpoint" in patched
    assert "1.00x ATR / R:R 0.67" not in patched


def test_patch_report_markdown_corrects_split_cells_screener_format():
    # SCREENER_v3 table: separate cells per figure, e.g.
    # '| 130.92 | **40%** | wall | 6.14x | **5.50:1** OK | כשיר |'.
    markdown = "| 130.92 | **40%** | wall | 6.14x | **5.50:1** OK | כשיר |\n"
    stated = {("Primary", 1): {"price": "130.92", "atr_mult": "6.14x", "rr": "5.50"}}
    patched = report_lint.patch_report_markdown(
        markdown, stated, bad_targets=set(), corrections={("Primary", 1): {"atr_mult": 4.78, "rr": 1.94}}
    )
    assert "4.78x" in patched
    assert "**1.94:1** OK" in patched
    assert "6.14x" not in patched


def test_patch_report_markdown_demotes_failing_target_to_checkpoint_split_cells():
    markdown = "| 130.92 | **40%** | wall | 4.78x | **1.94:1** OK | כשיר |\n"
    stated = {("Primary", 1): {"price": "130.92", "atr_mult": "4.78x", "rr": "1.94"}}
    patched = report_lint.patch_report_markdown(
        markdown, stated, bad_targets={("Primary", 1)}, corrections={}
    )
    assert "~~130.92~~" in patched
    assert "Checkpoint" in patched
    assert "כשיר" not in patched


def test_patch_report_markdown_skips_target_missing_from_stated():
    # A key present in bad_targets/corrections but absent from `stated` (the
    # caller failed to capture it) must be silently skipped, never guessed at.
    markdown = "| 106.00 | 1.10x ATR / R:R 2.00 |\n"
    patched = report_lint.patch_report_markdown(
        markdown, stated={}, bad_targets=set(), corrections={("AAPL", 1): {"atr_mult": 3.0}}
    )
    assert patched == markdown


def test_portfolio_heat_breach_without_disclosure_is_flagged():
    decision = _clean_decision()
    decision["portfolio_heat_after"] = 0.08
    decision["portfolio_heat_cap_pct"] = 0.06
    # portfolio_heat_disclosed left unset/false -- the report never showed the warning.
    result = lint_screener_decision(decision)
    assert not result.ok
    checks = {f.check for f in result.findings}
    assert "portfolio_heat_not_disclosed" in checks
    finding = next(f for f in result.findings if f.check == "portfolio_heat_not_disclosed")
    assert finding.detail == {"heat_after": 0.08, "cap_pct": 0.06}


def test_portfolio_heat_breach_with_disclosure_is_not_flagged():
    decision = _clean_decision()
    decision["portfolio_heat_after"] = 0.08
    decision["portfolio_heat_cap_pct"] = 0.06
    decision["portfolio_heat_disclosed"] = True
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "portfolio_heat_not_disclosed" not in checks


def test_portfolio_heat_within_cap_is_never_flagged_regardless_of_disclosure():
    decision = _clean_decision()
    decision["portfolio_heat_after"] = 0.03
    decision["portfolio_heat_cap_pct"] = 0.06
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "portfolio_heat_not_disclosed" not in checks


def test_portfolio_heat_check_never_touches_the_decision_line():
    # Rule 19 is disclosure-only -- confirms a breach never produces anything
    # resembling the regime gate's decision-blocking finding.
    decision = _clean_decision()
    decision["decision"] = "Buy Now"
    decision["market_regime"] = "healthy_uptrend"  # not risk_off/structure_break
    decision["portfolio_heat_after"] = 0.50  # way over cap
    decision["portfolio_heat_cap_pct"] = 0.06
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "regime_gate_violation" not in checks
    assert decision["decision"] == "Buy Now"  # untouched


def test_sector_cap_breach_without_disclosure_is_flagged():
    decision = _clean_decision()
    decision["sector_pct_after"] = 0.55
    decision["sector_cap_pct"] = 0.40
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "sector_cap_not_disclosed" in checks
    finding = next(f for f in result.findings if f.check == "sector_cap_not_disclosed")
    assert finding.detail == {"sector_pct_after": 0.55, "cap_pct": 0.40}


def test_sector_cap_breach_with_disclosure_is_not_flagged():
    decision = _clean_decision()
    decision["sector_pct_after"] = 0.55
    decision["sector_cap_pct"] = 0.40
    decision["sector_disclosed"] = True
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "sector_cap_not_disclosed" not in checks


def test_sector_cap_within_limit_is_never_flagged():
    decision = _clean_decision()
    decision["sector_pct_after"] = 0.20
    decision["sector_cap_pct"] = 0.40
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "sector_cap_not_disclosed" not in checks


def test_sector_cap_check_never_touches_the_decision_line():
    decision = _clean_decision()
    decision["decision"] = "Buy Now"
    decision["sector_pct_after"] = 0.90
    decision["sector_cap_pct"] = 0.40
    result = lint_screener_decision(decision)
    assert decision["decision"] == "Buy Now"


def test_cash_usage_breach_without_disclosure_is_flagged():
    decision = _clean_decision()
    decision["cash_required_usd"] = 28500.0
    decision["cash_available_usd"] = 31395.69
    decision["cash_usage_warn_pct"] = 0.30
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "cash_usage_not_disclosed" in checks


def test_cash_usage_breach_with_disclosure_is_not_flagged():
    decision = _clean_decision()
    decision["cash_required_usd"] = 28500.0
    decision["cash_available_usd"] = 31395.69
    decision["cash_usage_warn_pct"] = 0.30
    decision["cash_usage_disclosed"] = True
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "cash_usage_not_disclosed" not in checks


def test_cash_usage_within_threshold_is_never_flagged():
    decision = _clean_decision()
    decision["cash_required_usd"] = 5000.0
    decision["cash_available_usd"] = 31395.69
    decision["cash_usage_warn_pct"] = 0.30
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "cash_usage_not_disclosed" not in checks


def test_cash_usage_check_never_touches_the_decision_line():
    decision = _clean_decision()
    decision["decision"] = "Buy Now"
    decision["cash_required_usd"] = 100000.0  # way over available cash
    decision["cash_available_usd"] = 31395.69
    decision["cash_usage_warn_pct"] = 0.30
    result = lint_screener_decision(decision)
    assert decision["decision"] == "Buy Now"


def test_cash_usage_zero_available_skips_rather_than_divide_by_zero():
    decision = _clean_decision()
    decision["cash_required_usd"] = 1000.0
    decision["cash_available_usd"] = 0.0
    decision["cash_usage_warn_pct"] = 0.30
    result = lint_screener_decision(decision)  # must not raise
    checks = {f.check for f in result.findings}
    assert "cash_usage_not_disclosed" not in checks


def test_breakout_low_volume_is_no_longer_flagged():
    """2026-08-09: the check retired with the multiplier it policed. It used to
    demand disclosure of a x0.5 derate that was removed on 2026-08-03, so its
    own presence read as evidence the derate was still live."""
    decision = _clean_decision()
    decision["primary_setup"]["breakout_volume_pct_of_avg"] = 82.0
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "breakout_volume_derate_not_disclosed" not in checks


def test_breakout_volume_missing_is_recorded_as_skipped_not_a_finding():
    """The figure is still research data for the shadow book, so its absence is
    still worth a note -- it is just not a finding any more."""
    decision = _clean_decision()
    decision["primary_setup"].pop("breakout_volume_pct_of_avg", None)
    result = lint_screener_decision(decision)
    assert not any(f.check == "breakout_volume_derate_not_disclosed" for f in result.findings)
    assert any("breakout_volume_pct_of_avg missing" in s for s in result.skipped)


def test_breakout_volume_at_or_above_average_is_never_flagged():
    decision = _clean_decision()
    decision["primary_setup"]["breakout_volume_pct_of_avg"] = 100.0
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "breakout_volume_derate_not_disclosed" not in checks


def test_low_volume_on_non_breakout_setup_type_is_not_flagged():
    decision = _clean_decision()
    # alternate_setup's type is "Pullback" -- rule 22 only applies to Breakout/Retest.
    decision["alternate_setup"]["breakout_volume_pct_of_avg"] = 50.0
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "breakout_volume_derate_not_disclosed" not in checks


def test_breakout_volume_check_never_touches_the_decision_line():
    decision = _clean_decision()
    decision["decision"] = "Buy Now"
    decision["primary_setup"]["breakout_volume_pct_of_avg"] = 40.0
    result = lint_screener_decision(decision)
    assert decision["decision"] == "Buy Now"


def test_silent_regime_override_is_flagged():
    decision = _clean_decision()
    decision["market_regime_formula"] = "healthy_uptrend"
    decision["market_regime"] = "risk_off"  # differs, no reason given
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "regime_override_not_disclosed" in checks
    finding = next(f for f in result.findings if f.check == "regime_override_not_disclosed")
    assert finding.detail == {"market_regime_formula": "healthy_uptrend", "market_regime": "risk_off"}


def test_disclosed_regime_override_is_not_flagged():
    decision = _clean_decision()
    decision["market_regime_formula"] = "healthy_uptrend"
    decision["market_regime"] = "risk_off"
    decision["regime_override_reason"] = "FOMC decision today at 2pm, deliberately more cautious"
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "regime_override_not_disclosed" not in checks


def test_matching_regime_values_are_never_flagged_regardless_of_reason():
    decision = _clean_decision()
    decision["market_regime_formula"] = "healthy_uptrend"
    decision["market_regime"] = "healthy_uptrend"  # same -- no override happened
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "regime_override_not_disclosed" not in checks


def test_whitespace_only_override_reason_does_not_count_as_disclosed():
    decision = _clean_decision()
    decision["market_regime_formula"] = "healthy_uptrend"
    decision["market_regime"] = "risk_off"
    decision["regime_override_reason"] = "   "  # not a real reason
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "regime_override_not_disclosed" in checks


def test_missing_market_regime_formula_field_skips_rather_than_crashes():
    decision = _clean_decision()
    # market_regime_formula never set (e.g. an older decision JSON) -- must not
    # be treated as a mismatch against market_regime.
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "regime_override_not_disclosed" not in checks


class TestMonitorDecisionRegimeOverride:
    """lint_monitor_decision uses the same shared check as the screener, keyed
    on `regime_now` instead of `market_regime` (MONITOR_v2's own field name
    for the live-rechecked regime, rule 18's second enforcement point)."""

    def test_silent_override_on_regime_now_is_flagged(self):
        decision = {
            "market_regime_formula": "healthy_uptrend",
            "regime_now": "risk_off",
        }
        result = lint_monitor_decision(decision)
        checks = {f.check for f in result.findings}
        assert "regime_override_not_disclosed" in checks

    def test_disclosed_override_on_regime_now_is_not_flagged(self):
        decision = {
            "market_regime_formula": "healthy_uptrend",
            "regime_now": "risk_off",
            "regime_override_reason": "CPI print this morning",
        }
        result = lint_monitor_decision(decision)
        checks = {f.check for f in result.findings}
        assert "regime_override_not_disclosed" not in checks

    def test_no_regime_now_present_skips_rather_than_crashes(self):
        # The normal, non-blocking case -- regime_now is omitted entirely.
        decision = {"order": None}
        result = lint_monitor_decision(decision)  # must not raise
        checks = {f.check for f in result.findings}
        assert "regime_override_not_disclosed" not in checks


def test_missing_alternate_setup_is_flagged():
    decision = _clean_decision()
    decision["alternate_setup"] = None
    result = lint_screener_decision(decision)
    assert not result.ok
    checks = {f.check for f in result.findings}
    assert "missing_alternate_setup" in checks


def test_missing_primary_setup_is_flagged():
    decision = _clean_decision()
    decision["primary_setup"] = None
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "missing_primary_setup" in checks


def test_missing_potential_field_is_flagged():
    decision = _clean_decision()
    decision["potential"] = None
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "missing_potential_field" in checks


def test_reversal_setup_missing_5d_rs_is_flagged():
    decision = _clean_decision()
    decision["primary_setup"]["type"] = "Reclaim"
    # metrics has no rs_vs_spy_5d
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "missing_reversal_rs_5d" in checks


def test_reversal_setup_with_5d_rs_present_is_not_flagged():
    decision = _clean_decision()
    decision["primary_setup"]["type"] = "Reclaim"
    decision["metrics"]["rs_vs_spy_5d"] = -1.0
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "missing_reversal_rs_5d" not in checks


def test_reversal_alternate_setup_missing_5d_rs_is_flagged():
    """2026-07-30 full-system checkup: this used to only check primary_setup's
    type -- a reversal setup shown as the ALTERNATE skipped rule 15 entirely."""
    decision = _clean_decision()
    decision["alternate_setup"]["type"] = "Gap-and-Hold"
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "missing_reversal_rs_5d" in checks


def test_screener_setup_allocation_over_100_pct_is_flagged():
    """2026-07-30 full-system checkup: rule 7's allocation-sum check only ran
    for STRATEGY_v3/playbook positions before this -- a fresh screener setup's
    own target table could exceed 100% allocation with nothing catching it."""
    decision = _clean_decision()
    decision["primary_setup"]["targets"].append(
        {"price": 110.0, "pct": "70%", "atr_mult": "5.00x", "rr": "3.00", "status": "pass"}
    )
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "allocation_pct_exceeds_100" in checks


def test_screener_setup_allocation_at_100_pct_is_not_flagged():
    decision = _clean_decision()
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "allocation_pct_exceeds_100" not in checks


def test_regime_gate_violation_is_flagged():
    decision = _clean_decision()
    decision["market_regime"] = "risk_off"
    decision["decision"] = "Buy Now"
    result = lint_screener_decision(decision)
    assert not result.ok
    checks = {f.check for f in result.findings}
    assert "regime_gate_violation" in checks


def test_regime_gate_allows_watchlist_in_risk_off():
    decision = _clean_decision()
    decision["market_regime"] = "risk_off"
    decision["decision"] = "Watchlist"
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "regime_gate_violation" not in checks


def test_stop_noise_floor_violation_is_flagged():
    decision = _clean_decision()
    # trigger=100, stop=99.5 -> noise=0.5, atr=2.0 -> floor=1.4 -- fails.
    decision["primary_setup"]["stop"] = 99.5
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "stop_noise_floor" in checks


def test_non_numeric_trigger_is_skipped_not_guessed():
    """The ATR/R:R arithmetic must not guess at a trigger it cannot read.

    Updated 2026-08-31: this fixture is itself the case the new wording check
    was written for. "close above 112.67" names a level -- 112.67 IS the
    trigger -- and storing it as a sentence hides the setup from every
    mechanical check there is. The arithmetic still skips (the original
    intent); the report is no longer silent about why it had to.
    """
    decision = _clean_decision()
    decision["primary_setup"]["trigger"] = "close above 112.67"
    result = lint_screener_decision(decision)
    assert any("Primary" in s for s in result.skipped)
    assert {f.check for f in result.findings} == {"level_named_in_prose_trigger"}


def test_a_trigger_with_no_level_at_all_is_clean_when_it_uses_the_agreed_wording():
    # Rule 5's real case: the level has not formed. Allowed, and countable.
    decision = _clean_decision()
    decision["primary_setup"]["trigger"] = setup_types.PENDING_TRIGGER_PHRASES[0]
    result = lint_screener_decision(decision)
    assert result.ok
    assert any("Primary" in s for s in result.skipped)


def test_free_wording_for_a_level_less_trigger_is_pointed_at_the_list():
    # 28 distinct wordings out of 28 real Alternates, some Hebrew and some
    # English -- which made "how often is there no level" unanswerable without
    # reading all of them.
    decision = _clean_decision()
    decision["primary_setup"]["trigger"] = "not ready yet, waiting for something"
    result = lint_screener_decision(decision)
    assert {f.check for f in result.findings} == {"pending_trigger_wording"}


def test_playbook_allocation_qty_exceeds_position_is_flagged():
    decision = {
        "positions": [
            {"ticker": "ABC", "qty": 100, "price": 50.0, "stop": 47.0, "atr_at_build": 2.0,
             "targets": [
                 {"price": 56.0, "qty": 60, "atr_mult": "3.00x", "rr": "2.00"},
                 {"price": 60.0, "qty": 60, "atr_mult": "5.00x", "rr": "3.33"},
             ]},
        ]
    }
    result = lint_playbook_decision(decision)
    checks = {f.check for f in result.findings}
    assert "allocation_qty_exceeds_position" in checks


def test_playbook_allocation_pct_exceeds_100_is_flagged():
    """2026-07-30 full-system checkup: the percent-based branch (used when no
    target carries a numeric qty) had zero test coverage before this -- only
    the qty-based branch above was ever tested."""
    decision = {
        "positions": [
            {"ticker": "ABC", "price": 50.0, "stop": 47.0, "atr_at_build": 2.0,
             "targets": [
                 {"price": 56.0, "pct": "60%", "atr_mult": "3.00x", "rr": "2.00"},
                 {"price": 60.0, "pct": "60%", "atr_mult": "5.00x", "rr": "3.33"},
             ]},
        ]
    }
    result = lint_playbook_decision(decision)
    checks = {f.check for f in result.findings}
    assert "allocation_pct_exceeds_100" in checks


def test_playbook_allocation_pct_at_100_is_not_flagged():
    decision = {
        "positions": [
            {"ticker": "ABC", "price": 50.0, "stop": 47.0, "atr_at_build": 2.0,
             "targets": [
                 {"price": 56.0, "pct": "40%", "atr_mult": "3.00x", "rr": "2.00"},
                 {"price": 60.0, "pct": "60%", "atr_mult": "5.00x", "rr": "3.33"},
             ]},
        ]
    }
    result = lint_playbook_decision(decision)
    checks = {f.check for f in result.findings}
    assert "allocation_pct_exceeds_100" not in checks


def test_playbook_clean_position_has_no_findings():
    decision = {
        "positions": [
            {"ticker": "ABC", "qty": 100, "price": 50.0, "stop": 47.0, "atr_at_build": 2.0,
             "targets": [{"price": 56.0, "qty": 40, "atr_mult": "3.00x", "rr": "2.00"}]},
        ]
    }
    result = lint_playbook_decision(decision)
    assert result.ok


class TestPositionStatusDecision:
    """2026-07-30 full-system checkup: /positions ran zero lint checks of any
    kind before this -- the only deliver_*.py script with none."""

    def test_missing_price_or_stop_is_flagged(self):
        decision = {"results": [{"ticker": "ABC", "headline": "x", "entry_date": "2026-07-10"}]}
        result = lint_position_status_decision(decision)
        checks = {f.check for f in result.findings}
        assert "position_status_missing_stop_distance_fields" in checks

    def test_missing_entry_date_is_flagged(self):
        decision = {"results": [{"ticker": "ABC", "headline": "x", "price": 100.0, "stop": 95.0}]}
        result = lint_position_status_decision(decision)
        checks = {f.check for f in result.findings}
        assert "position_status_missing_entry_date" in checks

    def test_all_fields_present_has_no_findings(self):
        decision = {"results": [
            {"ticker": "ABC", "headline": "x", "price": 100.0, "stop": 95.0, "entry_date": "2026-07-10"},
        ]}
        result = lint_position_status_decision(decision)
        assert result.ok


class TestPlaybookAddGate:
    """2026-07-30 full-system checkup: rules 18/27 already blocked a fresh
    SCREENER_v3 Buy in a bad regime or on an F-graded rubric -- "Add Only If
    Confirmed" (new risk on an already-open position) had neither check."""

    def test_add_in_blocking_regime_is_flagged(self):
        decision = {
            "market_regime": "risk_off",
            "positions": [{"ticker": "ABC", "action": "Add Only If Confirmed"}],
        }
        result = lint_playbook_decision(decision)
        checks = {f.check for f in result.findings}
        assert "playbook_add_regime_gate_violation" in checks

    def test_add_with_f_grade_at_build_is_flagged(self):
        decision = {
            "market_regime": "healthy_uptrend",
            "positions": [{"ticker": "ABC", "action": "Add Only If Confirmed", "rubric_grade_at_build": "F"}],
        }
        result = lint_playbook_decision(decision)
        checks = {f.check for f in result.findings}
        assert "playbook_add_rubric_gate_violation" in checks

    def test_add_in_healthy_regime_with_good_grade_is_not_flagged(self):
        decision = {
            "market_regime": "healthy_uptrend",
            "positions": [{"ticker": "ABC", "action": "Add Only If Confirmed", "rubric_grade_at_build": "B"}],
        }
        result = lint_playbook_decision(decision)
        checks = {f.check for f in result.findings}
        assert "playbook_add_regime_gate_violation" not in checks
        assert "playbook_add_rubric_gate_violation" not in checks

    def test_hold_action_in_blocking_regime_is_not_flagged(self):
        # The gate only applies to Add Only If Confirmed -- holding through a
        # bad regime is not adding new risk, nothing to block here.
        decision = {
            "market_regime": "risk_off",
            "positions": [{"ticker": "ABC", "action": "Hold"}],
        }
        result = lint_playbook_decision(decision)
        checks = {f.check for f in result.findings}
        assert "playbook_add_regime_gate_violation" not in checks


def test_playbook_regime_override_without_reason_is_flagged():
    # Rule 23, extended to STRATEGY_v3.md 2026-07-20: market_regime must match
    # market_regime_formula.regime verbatim unless a written override reason
    # is given -- same check SCREENER_v3/MONITOR_v2 already had.
    decision = {
        "market_regime_formula": {"regime": "healthy_uptrend"},
        "market_regime": "neutral_choppy",
        "positions": [],
    }
    result = lint_playbook_decision(decision)
    checks = {f.check for f in result.findings}
    assert "regime_override_not_disclosed" in checks


def test_playbook_regime_override_with_reason_is_not_flagged():
    decision = {
        "market_regime_formula": {"regime": "healthy_uptrend"},
        "market_regime": "neutral_choppy",
        "regime_override_reason": "FOMC today, formula lags real-time risk",
        "positions": [],
    }
    result = lint_playbook_decision(decision)
    checks = {f.check for f in result.findings}
    assert "regime_override_not_disclosed" not in checks


def test_playbook_regime_fields_absent_is_skipped_not_guessed():
    result = lint_playbook_decision({"positions": []})
    checks = {f.check for f in result.findings}
    assert "regime_override_not_disclosed" not in checks
    assert any("regime formula" in s for s in result.skipped)


def test_playbook_matching_regime_dict_shape_is_never_flagged():
    # Real 2026-07-21 incident: market_regime_formula is documented as a dict
    # ({"regime": ..., "score": ..., ...}) for playbook, unlike screener/monitor's
    # plain string. A same-value dict vs string comparison must not be treated as
    # a mismatch just because a dict is never == a string.
    decision = {
        "market_regime_formula": {"regime": "neutral_choppy", "score": -2,
                                   "structure_break_confirmed": False, "components": {}},
        "market_regime": "neutral_choppy",  # same regime -- no override happened
        "positions": [],
    }
    result = lint_playbook_decision(decision)
    checks = {f.check for f in result.findings}
    assert "regime_override_not_disclosed" not in checks


def test_rubric_grade_f_with_buy_now_is_flagged():
    # CONSISTENCY_RULES.md rule 27 / SCREENER_v3.md:138 -- F is No Trade
    # automatic, same shape as the regime gate.
    # "F" belongs to the retired six-criterion scale, so no set of numbers can
    # produce it any more. A stored thesis still claiming F therefore fails as a
    # disagreement with its own arithmetic -- which blocks just the same, and is
    # the more honest description of what is wrong with it.
    decision = _decision_graded_d()
    decision["grade"] = "F"
    decision["decision"] = "Buy Now"
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "rubric_grade_mismatch" in checks
    assert "decision_word_too_strong" in checks


def test_rubric_grade_f_with_watchlist_is_not_flagged():
    decision = _clean_decision()
    decision["rubric_grade"] = "F"
    decision["decision"] = "Watchlist"
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "rubric_grade_gate_violation" not in checks


def test_rubric_grade_c_with_buy_now_is_not_flagged():
    decision = _clean_decision()
    decision["rubric_grade"] = "C"
    decision["decision"] = "Buy Now"
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "rubric_grade_gate_violation" not in checks


def test_rubric_grade_d_with_buy_now_is_flagged():
    # 2026-08-02: the rubric went from six criteria to five and F was retired,
    # so D is now the bottom of the scale. This gate used to test for "F"
    # literally, which after that change could never fire again -- the check
    # would have gone quietly dead while still looking present in the code.
    decision = _decision_graded_d()
    decision["decision"] = "Buy Now"
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "rubric_grade_gate_violation" in checks


def test_rubric_grade_d_with_watchlist_is_not_flagged():
    # The gate blocks the ORDER, never the thesis -- same split as rule 18's
    # regime gate. A D-graded idea is still allowed to be tracked.
    decision = _clean_decision()
    decision["rubric_grade"] = "D"
    decision["decision"] = "Watchlist"
    result = lint_screener_decision(decision)
    checks = {f.check for f in result.findings}
    assert "rubric_grade_gate_violation" not in checks


class TestMonitorDecisionRubricLiveGate:
    def test_rubric_blocked_with_order_is_flagged(self):
        decision = {"rubric_blocked": True, "order": {"price": 51.02, "stop": 43.92}}
        result = lint_monitor_decision(decision)
        checks = {f.check for f in result.findings}
        assert "rubric_gate_violation_order" in checks

    def test_rubric_blocked_with_starter_qty_is_flagged(self):
        decision = {"rubric_blocked": True, "starter_qty": 16}
        result = lint_monitor_decision(decision)
        checks = {f.check for f in result.findings}
        assert "rubric_gate_violation_starter" in checks

    def test_rubric_blocked_with_no_order_or_starter_is_not_flagged(self):
        decision = {"rubric_blocked": True}
        result = lint_monitor_decision(decision)
        checks = {f.check for f in result.findings}
        assert "rubric_gate_violation_order" not in checks
        assert "rubric_gate_violation_starter" not in checks

    def test_rubric_not_blocked_with_order_is_not_flagged(self):
        decision = {"rubric_blocked": False, "order": {"price": 51.02, "stop": 43.92}}
        result = lint_monitor_decision(decision)
        checks = {f.check for f in result.findings}
        assert "rubric_gate_violation_order" not in checks

    def test_rubric_field_absent_skips_rather_than_crashes(self):
        result = lint_monitor_decision({"order": None})  # must not raise
        checks = {f.check for f in result.findings}
        assert "rubric_gate_violation_order" not in checks
        assert "rubric_gate_violation_starter" not in checks


class TestMonitorDecisionDisclosures:
    """2026-07-30 full-system checkup: rules 19/20/21's disclosure checks used
    to only run for lint_screener_decision -- a real green-ticker buy order
    carries the same portfolio-wide risk but had none of this checked."""

    def test_portfolio_heat_over_cap_not_disclosed_is_flagged(self):
        decision = {"portfolio_heat_after": 0.10, "portfolio_heat_cap_pct": 0.06}
        result = lint_monitor_decision(decision)
        checks = {f.check for f in result.findings}
        assert "portfolio_heat_not_disclosed" in checks

    def test_portfolio_heat_over_cap_disclosed_is_not_flagged(self):
        decision = {"portfolio_heat_after": 0.10, "portfolio_heat_cap_pct": 0.06,
                     "portfolio_heat_disclosed": True}
        result = lint_monitor_decision(decision)
        checks = {f.check for f in result.findings}
        assert "portfolio_heat_not_disclosed" not in checks

    def test_sector_cap_over_and_not_disclosed_is_flagged(self):
        decision = {"sector_pct_after": 0.50, "sector_cap_pct": 0.40}
        result = lint_monitor_decision(decision)
        checks = {f.check for f in result.findings}
        assert "sector_cap_not_disclosed" in checks

    def test_cash_usage_over_and_not_disclosed_is_flagged(self):
        decision = {"cash_required_usd": 20000.0, "cash_available_usd": 30000.0, "cash_usage_warn_pct": 0.30}
        result = lint_monitor_decision(decision)
        checks = {f.check for f in result.findings}
        assert "cash_usage_not_disclosed" in checks

    def test_disclosure_fields_absent_skip_rather_than_crash(self):
        result = lint_monitor_decision({"order": None})  # must not raise
        checks = {f.check for f in result.findings}
        assert "portfolio_heat_not_disclosed" not in checks
        assert "sector_cap_not_disclosed" not in checks
        assert "cash_usage_not_disclosed" not in checks


class TestStaleTriggerGate:
    """MONITOR_v2.md's stale-trigger rule (2026-08-02). A trigger that fired
    days ago is still a fact, but the buy order it implies was sized against a
    price that has since moved -- real case: MSFT's 389.03 trigger was still
    producing live "trigger fired" headlines with price at 451."""

    def test_stale_trigger_with_order_is_flagged(self):
        decision = {"trigger_fired_age": {"stale": True, "trading_days": 4,
                                           "first_green_date": "2026-07-28"},
                    "order": {"price": 522.61, "stop": 495.96}}
        checks = {f.check for f in lint_monitor_decision(decision).findings}
        assert "stale_trigger_violation_order" in checks

    def test_stale_trigger_with_starter_qty_is_flagged(self):
        decision = {"trigger_fired_age": {"stale": True, "trading_days": 4,
                                           "first_green_date": "2026-07-28"},
                    "starter_qty": 16}
        checks = {f.check for f in lint_monitor_decision(decision).findings}
        assert "stale_trigger_violation_starter" in checks

    def test_fresh_trigger_with_order_is_not_flagged(self):
        decision = {"trigger_fired_age": {"stale": False, "trading_days": 1,
                                           "first_green_date": "2026-07-31"},
                    "order": {"price": 58.86, "stop": 51.84}}
        checks = {f.check for f in lint_monitor_decision(decision).findings}
        assert "stale_trigger_violation_order" not in checks

    def test_stale_trigger_without_an_order_is_not_flagged(self):
        # Reporting the fact is exactly what the rule ASKS for -- only the
        # actionable half is blocked, same split as the regime/rubric gates.
        decision = {"trigger_fired_age": {"stale": True, "trading_days": 5,
                                           "first_green_date": "2026-07-27"}}
        checks = {f.check for f in lint_monitor_decision(decision).findings}
        assert not any(c.startswith("stale_trigger") for c in checks)

    def test_missing_age_field_skips_silently(self):
        # An older decision JSON from before this field existed must not start
        # failing lint -- same posture as every other optional-field check here.
        decision = {"order": {"price": 100.0, "stop": 95.0}}
        checks = {f.check for f in lint_monitor_decision(decision).findings}
        assert not any(c.startswith("stale_trigger") for c in checks)


class TestSizeFloor:
    """CONSISTENCY_RULES.md rule 28 (2026-08-02). The multipliers may halve a
    position, never quarter it, and nothing may exceed a full 1%. The two
    orders below are the shapes of the incidents that produced this rule: one
    entry sized at a sixth of a position, one at over twice the ceiling. The
    account is a round $100,000 example, so a full risk unit is $1,000."""

    FULL_RISK = 1000.0

    def _decision(self, qty, entry=188.11, stop=179.80, multipliers=None):
        return {"sizing": {"entry": entry, "stop": stop, "qty": qty,
                            "risk_usd_target": self.FULL_RISK,
                            "multipliers": multipliers if multipliers is not None
                            else {"volatility": 1.0, "choppy": 1.0, "volume": 1.0}}}

    def test_undersized_order_is_flagged(self):
        # The real ANET fill: 30 shares = $249 = 0.17% of the account.
        checks = {f.check for f in lint_screener_decision(self._decision(30)).findings}
        assert "size_outside_bounds" in checks

    def test_oversized_order_is_flagged(self):
        # The real CRDO fill: 74 shares at 227.62/182.07 = $3,371 = 2.26%.
        decision = self._decision(74, entry=227.62, stop=182.07)
        checks = {f.check for f in lint_screener_decision(decision).findings}
        assert "size_outside_bounds" in checks

    def test_correctly_sized_order_is_clean(self):
        decision = self._decision(114)   # 114 x 8.31 = $947 = 0.95x full
        checks = {f.check for f in lint_screener_decision(decision).findings}
        assert "size_outside_bounds" not in checks

    def test_half_position_stays_inside_the_legal_band(self):
        # Still legal (rule 28's floor is unchanged) -- but see the full-size
        # tests below: legal is no longer the same as unremarkable.
        decision = self._decision(60, multipliers={"choppy": 0.5})   # 60 x 8.31 = $499
        checks = {f.check for f in lint_screener_decision(decision).findings}
        assert "size_outside_bounds" not in checks

    def test_multiplier_floor_check_is_retired(self):
        """2026-08-09. Every named derate is in ADVISORY_MULTIPLIER_KEYS and
        multiplied by nothing, so the raw product is always 1.0 -- the check
        could only ever fire on a number the sizing code had already refused to
        use. It is replaced by the full-size check below."""
        decision = self._decision(60, multipliers={"custom": 0.5, "custom2": 0.5})
        checks = {f.check for f in lint_screener_decision(decision).findings}
        assert "size_multiplier_below_floor" not in checks

    def test_volatility_and_volume_are_display_only(self):
        # The 2026-08-03 sizing change: flat 1%, these two never resize.
        decision = self._decision(114, multipliers={"volatility": 0.5, "volume": 0.5})
        checks = {f.check for f in lint_screener_decision(decision).findings}
        assert "size_outside_bounds" not in checks
        assert "size_below_full_no_reason" not in checks

    def test_market_condition_is_display_only(self):
        decision = self._decision(114, multipliers={"volatility": 1.0, "choppy": 0.5})
        checks = {f.check for f in lint_screener_decision(decision).findings}
        assert "size_outside_bounds" not in checks
        assert "size_below_full_no_reason" not in checks

    # --- full size is the default, not the ceiling (2026-08-09) -------------
    #
    # The live gap this closes: with the multipliers gone, a half position sat
    # quietly inside the 0.5-1.0 band and nothing said a word. Two real fills
    # (2026-08-03 GLD at 0.13% risk, 2026-08-06 SCHW at 0.18%) were genuinely
    # cash-limited, which is a real reason -- but nothing recorded that, so
    # from the data they are indistinguishable from the sizing bug that used to
    # produce exactly this shape.

    def test_below_full_size_without_a_reason_is_flagged(self):
        decision = self._decision(60)   # 60 x 8.31 = $499 = 0.50x full
        checks = {f.check for f in lint_screener_decision(decision).findings}
        assert "size_below_full_no_reason" in checks

    def test_below_full_size_with_a_reason_is_not_flagged(self):
        decision = self._decision(60)
        decision["sizing"]["size_reduction_reason"] = "cash_limited"
        checks = {f.check for f in lint_screener_decision(decision).findings}
        assert "size_below_full_no_reason" not in checks

    def test_a_blank_reason_does_not_count_as_a_reason(self):
        decision = self._decision(60)
        decision["sizing"]["size_reduction_reason"] = "   "
        checks = {f.check for f in lint_screener_decision(decision).findings}
        assert "size_below_full_no_reason" in checks

    def test_full_size_order_needs_no_reason(self):
        decision = self._decision(170)   # 0.95x full
        checks = {f.check for f in lint_screener_decision(decision).findings}
        assert "size_below_full_no_reason" not in checks

    def test_out_of_band_size_reports_bounds_not_the_full_size_note(self):
        # A single finding, not two saying nearly the same thing.
        decision = self._decision(40)    # 40 x 8.31 = $332 = 0.22x full
        checks = {f.check for f in lint_screener_decision(decision).findings}
        assert "size_outside_bounds" in checks
        assert "size_below_full_no_reason" not in checks

    def test_missing_sizing_block_skips_silently(self):
        # Watchlist/No Trade reports have no order to size, and every decision
        # JSON written before this field existed must keep linting clean.
        result = lint_screener_decision({"primary_setup": None})
        assert not any(c.check.startswith("size_") for c in result.findings)
        assert any("sizing" in s for s in result.skipped)

    def test_partial_sizing_block_skips_silently(self):
        result = lint_screener_decision({"sizing": {"entry": 100.0, "qty": 10}})
        assert not any(c.check.startswith("size_") for c in result.findings)
        assert any("sizing" in s for s in result.skipped)

    def test_stop_above_entry_skips_rather_than_crashing(self):
        result = lint_screener_decision(self._decision(10, entry=90.0, stop=100.0))
        assert not any(c.check.startswith("size_") for c in result.findings)
        assert any("sizeable" in s for s in result.skipped)


class TestSmallSizeWithARealReason:
    """Found on the first live /screener run, 2026-08-09. ORCL came back at 36%
    of a full position because the account held $11,258 free and a full position
    needed more than that. The report said "buy 73 shares", then said "do not
    send this order as it is", and put the actual cause (low cash) two blocks
    away in a different list. Four signals and no instruction.

    An order below the floor WITH a stated reason is a different fact from one
    without, and telling those apart is the entire point of the field."""

    def _decision(self, qty, reason=None):
        decision = _clean_decision()
        sizing = {"entry": 132.40, "stop": 124.09, "qty": qty,
                  "risk_usd_target": 1000.0}
        if reason:
            sizing["size_reduction_reason"] = reason
        decision["sizing"] = sizing
        return decision

    def test_below_the_floor_with_a_reason_explains_instead_of_scolding(self):
        checks = {f.check for f in lint_screener_decision(
            self._decision(40, "cash_limited")).findings}
        assert "size_below_floor_with_reason" in checks
        assert "size_outside_bounds" not in checks

    def test_the_message_names_the_reason_and_gives_a_choice(self):
        result = lint_screener_decision(self._decision(40, "cash_limited"))
        finding = next(f for f in result.findings if f.check == "size_below_floor_with_reason")
        assert "cash_limited" in finding.message_he
        assert "לא טעות" in finding.message_he

    def test_below_the_floor_with_no_reason_is_still_a_plain_violation(self):
        checks = {f.check for f in lint_screener_decision(self._decision(40)).findings}
        assert "size_outside_bounds" in checks
        assert "size_below_floor_with_reason" not in checks

    def test_an_oversized_order_is_never_excused_by_a_reason(self):
        # The ceiling is not negotiable -- the single largest loss in the book
        # was a sizing error (CRDO at 2.26% risk, 48% of all realized losses).
        checks = {f.check for f in lint_screener_decision(
            self._decision(300, "cash_limited")).findings}
        assert "size_outside_bounds" in checks


class TestWarningHeaderMatchesTheFindings:
    """A header reading "the numeric check FAILED" printed above a line that
    starts with an information mark is the message contradicting itself. Seen
    live on ORCL, 2026-08-09, where the only finding was "sized down because
    there is not enough cash" -- a fact, not an error."""

    def _sized(self, qty, reason=None):
        decision = _clean_decision()
        sizing = {"entry": 132.40, "stop": 124.09, "qty": qty,
                  "risk_usd_target": 1000.0}
        if reason:
            sizing["size_reduction_reason"] = reason
        decision["sizing"] = sizing
        return decision

    def test_an_all_informational_result_gets_a_soft_header(self):
        block = report_lint.format_warning_block_he(
            lint_screener_decision(self._sized(40, "cash_limited")))
        assert block.startswith("ℹ️")
        assert "נכשלה" not in block

    def test_a_real_failure_still_gets_the_loud_header(self):
        block = report_lint.format_warning_block_he(
            lint_screener_decision(self._sized(40)))
        assert block.startswith("⚠️")
        assert "נכשלה" in block

    def test_one_real_failure_beside_a_note_is_never_softened(self):
        # A genuine error must not be dressed down because an informational
        # line happens to sit next to it.
        decision = self._sized(40, "cash_limited")
        decision["primary_setup"]["targets"][0]["atr_mult"] = "1.10x"
        block = report_lint.format_warning_block_he(lint_screener_decision(decision))
        assert block.startswith("⚠️")

    def test_a_clean_decision_still_produces_nothing_at_all(self):
        assert report_lint.format_warning_block_he(
            lint_screener_decision(_clean_decision())) == ""
class TestTheTargetScanHasToLeaveAnAnswer:
    """Rule 7 says every setup gets its own target scan and records the miss
    that produced the rule: MU, ONDS and ANET each had the Primary analysed and
    the Alternate left as trigger/stop only.

    It happened 23 more times, found on 2026-08-31 by reading the shadow book
    once the Alternate started being simulated. Twenty of those were honest --
    the scan ran, found levels, none passed rule 3's gate, and `checkpoints`
    records exactly that. Three had neither, which is not an answer.
    """

    def _alt(self, **kw):
        d = _clean_decision()
        d["alternate_setup"] = {"type": "Pullback", "trigger": 95.0, "stop": 92.0,
                                 "atr_at_build": 2.0, "targets": [], "checkpoints": [], **kw}
        return d

    def _checks(self, decision):
        return {f.check for f in lint_screener_decision(decision).findings}

    def test_no_targets_and_no_checkpoints_is_flagged(self):
        assert "target_scan_missing" in self._checks(self._alt())

    def test_a_scan_that_found_nothing_qualifying_is_fine(self):
        # This is the honest majority: levels were found, none passed the gate.
        d = self._alt(checkpoints=[{"price": 101.0, "atr_mult": "3.00x", "rr": "1.20"}])
        assert "target_scan_missing" not in self._checks(d)

    def test_a_scan_that_found_a_target_is_fine(self):
        d = self._alt(targets=[{"price": 104.0, "pct": "40%", "atr_mult": "4.50x",
                                 "rr": "3.00", "status": "pass"}])
        assert "target_scan_missing" not in self._checks(d)

    def test_a_pending_setup_with_no_levels_yet_is_not_flagged(self):
        # Rule 14's pending path: nothing to scan FROM, so nothing is owed.
        d = self._alt(trigger="a deeper flush, level not yet formed", stop=None)
        assert "target_scan_missing" not in self._checks(d)

    def test_the_primary_is_held_to_the_same_standard(self):
        d = _clean_decision()
        d["primary_setup"] = dict(d["primary_setup"], targets=[], checkpoints=[])
        checks = self._checks(d)
        assert "target_scan_missing" in checks

