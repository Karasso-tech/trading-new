"""Tests for setup_types.py -- the closed list the shadow book groups by.

The values below are the real ones read out of the live `shadow_outcomes` and
`ideas` tables on 2026-08-09, including the two paragraph-length ones that made
this module necessary.
"""

import pytest

import setup_types as st

# The real row that broke grouping: a whole paragraph in a label field.
REAL_PROSE = (
    "V-reversal / capitulation reclaim (swing, backfilled 2026-07-15 from historical data "
    "as of 2026-06-29). Sharp decline from 278.56 (2026-05-05) to a 225.55 capitulation low "
    "(2026-06-25) on rising volume, huge-volume reversal bar 2026-06-26 (248M shares)."
)
REAL_PROSE_2 = (
    "Breakout/Continuation (backfilled 2026-07-15 from historical data as of 2026-06-17) -- "
    "retesting the base of a major multi-month resistance wall (54.065-56.515, 9 touches "
    "since 2025-08) from underneath, after a 5-day run to a fresh local high."
)


class TestCanonical:
    @pytest.mark.parametrize("value", list(st.SETUP_TYPES))
    def test_every_official_name_maps_to_itself(self, value):
        assert st.canonical(value) == value

    @pytest.mark.parametrize("value,expected", [
        ("breakout", st.BREAKOUT),
        ("  BREAKOUT  ", st.BREAKOUT),
        ("Failed  Breakdown", st.FAILED_BREAKDOWN),
        ("gap and hold", st.GAP_AND_HOLD),
        ("Breakout/Continuation", st.BREAKOUT),
        ("Retest/Continuation", st.RETEST),
        ("Failed Breakdown/Reclaim", st.FAILED_BREAKDOWN),
    ])
    def test_near_misses_are_normalised(self, value, expected):
        assert st.canonical(value) == expected

    def test_real_prose_rows_are_recoverable_for_backfill(self):
        # canonical() is the lenient READ path -- these historical rows are
        # worth rescuing rather than discarding.
        assert st.canonical(REAL_PROSE) == st.RECLAIM
        assert st.canonical(REAL_PROSE_2) == st.BREAKOUT

    def test_a_non_setup_stays_none_rather_than_being_invented(self):
        # The backfilled legacy holdings are not setups at all. Guessing a
        # label here would put made-up rows into the research table.
        assert st.canonical("Legacy holding (backfilled 2026-07-09, no thesis on file)") is None
        assert st.canonical("") is None
        assert st.canonical(None) is None


class TestRequire:
    def test_normalises_a_near_miss(self):
        assert st.require("breakout") == st.BREAKOUT

    def test_none_passes_through(self):
        # A sleeve-only stub genuinely has no type; inventing one is worse.
        assert st.require(None) is None

    def test_prose_is_refused_even_though_canonical_could_guess(self):
        # The strict WRITE path. Mining a keyword out of a paragraph is exactly
        # how the field filled up with paragraphs.
        assert st.canonical(REAL_PROSE) == st.RECLAIM
        with pytest.raises(ValueError, match="prose"):
            st.require(REAL_PROSE)

    def test_an_unknown_short_label_is_refused_with_the_list(self):
        with pytest.raises(ValueError) as exc:
            st.require("Momentum Squeeze")
        assert "Breakout" in str(exc.value)

    def test_the_error_names_the_field(self):
        with pytest.raises(ValueError, match="alternate_setup"):
            st.require("nonsense", label="alternate_setup")


class TestProseIsReadFromTheLabelNotTheEssay:
    """Caught on the cleanup dry-run, 2026-08-09. The first version scanned the
    WHOLE prose value for a setup word, and a real row --

        "Core/Layer1 holding (ETF, backfilled 2026-07-15 from historical data
         as of 2026-06-30) ... pullback ..."

    -- came back `Pullback`. A Core ETF holding turned into a swing setup that
    never existed, inside the very table meant to hold only real ones. The
    search is now confined to the leading label, before the bracket or the full
    stop, which is where every real row actually puts it."""

    CORE_HOLDING = (
        "Core/Layer1 holding (ETF, backfilled 2026-07-15 from historical data as of "
        "2026-06-30). Held through a pullback to the rising SMA50; no swing thesis."
    )

    def test_a_core_holding_is_not_mined_for_a_setup_word(self):
        assert st.canonical(self.CORE_HOLDING) is None

    def test_a_real_label_before_the_bracket_still_resolves(self):
        assert st.canonical(REAL_PROSE) == st.RECLAIM
        assert st.canonical(REAL_PROSE_2) == st.BREAKOUT

    def test_a_setup_word_only_in_the_body_is_ignored(self):
        assert st.canonical(
            "Legacy holding (backfilled). The chart shows a clean breakout above 54."
        ) is None
