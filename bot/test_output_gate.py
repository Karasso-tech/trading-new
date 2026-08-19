"""Unit tests for output_gate.py's classify_output() -- A8, plus Hardening Pass
item 5's earnings_verified condition and item 6's data_fresh condition."""

from output_gate import classify_output


def _all_true_kwargs() -> dict:
    return dict(
        sender_authorized=True,
        data_timestamp_recorded=True,
        sleeve_known=True,
        source_provenance_ok=True,
        earnings_verified=True,
        data_fresh=True,
    )


def test_all_conditions_true_is_actionable():
    result = classify_output(**_all_true_kwargs())
    assert result.is_actionable
    assert result.classification == "actionable"
    assert result.user_facing_reasons_he == []


def test_earnings_unverified_forces_analysis_only():
    kwargs = _all_true_kwargs()
    kwargs["earnings_verified"] = False
    result = classify_output(**kwargs)
    assert not result.is_actionable
    assert result.classification == "analysis_only"
    assert "תאריך דוחות לא אומת ממקור נתונים אמיתי" in result.user_facing_reasons_he


def test_earnings_verified_defaults_to_false():
    kwargs = _all_true_kwargs()
    del kwargs["earnings_verified"]
    result = classify_output(**kwargs)
    assert not result.is_actionable
    assert "תאריך דוחות לא אומת ממקור נתונים אמיתי" in result.user_facing_reasons_he


def test_data_not_fresh_forces_analysis_only():
    kwargs = _all_true_kwargs()
    kwargs["data_fresh"] = False
    result = classify_output(**kwargs)
    assert not result.is_actionable
    assert any("עדכני" in r for r in result.user_facing_reasons_he)


def test_data_fresh_defaults_to_true_backward_compatible():
    # data_fresh is a later addition (item 6) -- existing callers that don't pass
    # it yet must not be silently downgraded, unlike earnings_verified (item 5,
    # which is deliberately opt-in/never-assumed since there is no legacy caller
    # relying on the old default).
    kwargs = _all_true_kwargs()
    del kwargs["data_fresh"]
    result = classify_output(**kwargs)
    assert result.is_actionable


def test_unauthorized_sender_forces_analysis_only():
    kwargs = _all_true_kwargs()
    kwargs["sender_authorized"] = False
    result = classify_output(**kwargs)
    assert not result.is_actionable
    assert "השולח לא מזוהה" in result.user_facing_reasons_he


def test_multiple_failing_conditions_all_listed():
    result = classify_output(
        sender_authorized=True,
        data_timestamp_recorded=True,
        sleeve_known=False,
        source_provenance_ok=True,
        earnings_verified=False,
        data_fresh=False,
    )
    assert not result.is_actionable
    assert len(result.user_facing_reasons_he) == 3
