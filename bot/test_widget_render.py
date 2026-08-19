"""Regression tests for widget_render.py's structured-dict -> WIDGET_DATA adapters.

A2: RS was silently truncated out of the compact widget for essentially every real
screener render (ATR + SMA20/50/150 filled all 4 metric slots before RS was ever
appended, then metrics[:4] cut it). This test exists so that never happens again
silently -- any future field added to screener_node_to_widget_data() that pushes RS
back out of the first 4 slots fails this test, not just a live report months later.
"""

from widget_render import screener_node_to_widget_data


def _fully_populated_trend_node() -> dict:
    return {
        "decision": "Watchlist",
        "grade": "D",
        "atr14_at_build": 2.731,
        "atr14_pct": 2.48,
        "sma20": 107.77,
        "sma50": 104.84,
        "sma150": 104.58,
        "dist_sma20_atr": 0.82,
        "rs_vs_spy_20d": 0.59,
        "rs_vs_qqq_20d": 2.22,
        "primary_setup": {"type": "Breakout", "trigger": "close above 112.67"},
    }


def _fully_populated_reversal_node() -> dict:
    node = _fully_populated_trend_node()
    node["primary_setup"] = {"type": "Reclaim", "trigger": "hammer at 106-108"}
    node["rs_vs_spy_5d"] = -1.32
    return node


def test_rs_present_for_trend_following_setup():
    data = screener_node_to_widget_data("UPS", _fully_populated_trend_node(), "2026-07-07")
    labels = [m["label"] for m in data["metrics"]]
    assert any("RS" in label for label in labels), (
        f"RS missing from widget metrics entirely -- got {labels}. This is the exact "
        f"A2 regression: RS silently truncated by metrics[:4]."
    )
    assert len(data["metrics"]) <= 4


def test_both_rs_windows_present_for_reversal_setup():
    data = screener_node_to_widget_data("XYZ", _fully_populated_reversal_node(), "2026-07-07")
    labels = [m["label"] for m in data["metrics"]]
    rs_labels = [l for l in labels if "RS" in l]
    assert len(rs_labels) == 2, (
        f"Reversal setup must show BOTH RS 20d and RS 5d (a fixed 20-day window "
        f"structurally penalizes a reversal thesis still carrying its own crash) -- "
        f"got only {rs_labels}."
    )
    assert any("20d" in l or "20" in l for l in rs_labels)
    assert any("5d" in l or "5" in l for l in rs_labels)
    assert len(data["metrics"]) <= 4


def test_targets_and_checkpoints_pass_through():
    node = _fully_populated_trend_node()
    node["primary_setup"]["targets"] = [
        {"price": 122.41, "pct": "40%", "atr_mult": "3.44x", "rr": "2.30", "status": "pass"},
    ]
    node["primary_setup"]["checkpoints"] = [
        {"price": 116.92, "reason": "1.44x ATR -- too close"},
    ]
    data = screener_node_to_widget_data("UPS", node, "2026-07-07")
    setup = data["setups"][0]
    assert setup["targets"], "B4 regression: targets array not passed through to widget"
    assert setup["targets"][0]["pct"] == "40%"
    assert setup["checkpoints"], "B4 regression: checkpoints array not passed through to widget"


if __name__ == "__main__":
    test_rs_present_for_trend_following_setup()
    test_both_rs_windows_present_for_reversal_setup()
    test_targets_and_checkpoints_pass_through()
    print("All widget_render regression tests passed.")
