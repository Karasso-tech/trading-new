"""Tests for sector_map.py, rewritten 2026-08-04 alongside the module.

The old tests locked in the previous design: one hand-made map used for
everything, where get_sector_group("NVDA") returned "mega_cap_growth_tech".
That design covered only 7.4% of real tickers, which made the 40% cap
unfireable. It has been replaced by two labels with two jobs, so the tests
that asserted the old shape are replaced rather than patched -- a test that
pins a design you deliberately changed is not protecting anything.

What these tests DO protect:
  - the sector is one standard, from a real source, with wide coverage;
  - the correlation warning still catches the 84%-one-bet case that the sector
    view structurally cannot see;
  - "unknown" still means genuinely unknown, never a guess.
"""

import sector_map
import sic_to_sector


class TestStandardSector:
    def test_the_eleven_standard_sectors_are_the_vocabulary(self):
        assert set(sic_to_sector.SECTORS) == {
            "XLK", "XLV", "XLF", "XLY", "XLP", "XLE", "XLI", "XLB", "XLRE", "XLU", "XLC"}

    def test_a_known_ticker_gets_a_sector_and_an_etf(self):
        assert sector_map.get_sector("AAPL") == "Information Technology"
        assert sector_map.get_sector_etf("AAPL") == "XLK"

    def test_industry_is_the_sub_level(self):
        assert sector_map.get_industry("NVDA") == "Semiconductors & Related Devices"
        assert sector_map.get_sector("NVDA") == "Information Technology"

    def test_lowercase_is_normalised(self):
        assert sector_map.get_sector("nvda") == sector_map.get_sector("NVDA")
        assert sector_map.get_correlation_group("nvda") == "mega_cap_growth_tech"

    def test_amazon_is_consumer_not_technology(self):
        # The case that makes the point: officially AMZN is a retailer, which
        # is why the sector view alone cannot see the mega-cap-tech bet.
        assert sector_map.get_sector("AMZN") == "Consumer Discretionary"

    def test_banks_utilities_and_reits_land_where_expected(self):
        assert sector_map.get_sector_etf("JPM") == "XLF"
        assert sector_map.get_sector_etf("NEE") == "XLU"
        assert sector_map.get_sector_etf("PLD") == "XLRE"
        assert sector_map.get_sector_etf("LLY") == "XLV"

    def test_unknown_ticker_returns_none_not_a_guess(self):
        assert sector_map.get_sector("ZZZZ") is None
        assert sector_map.get_sector_etf("ZZZZ") is None
        assert sector_map.get_industry("ZZZZ") is None

    def test_etfs_have_no_sector_and_that_is_correct(self):
        # An ETF genuinely has no SIC code. None here is the right answer, not
        # a coverage gap to be papered over.
        assert sector_map.get_sector("XLF") is None

    def test_the_cap_gates_on_the_standard_sector(self):
        # get_sector_group is what every cap check calls; it must now be the
        # one standard, not a hand-made grouping.
        assert sector_map.get_sector_group("NVDA") == sector_map.get_sector("NVDA")
        assert sector_map.get_sector_group("NVDA") == "Information Technology"


class TestCorrelationWarning:
    """The second label. It exists because the first one structurally cannot
    see a concentrated bet spread across sectors."""

    def test_the_mega_cap_bet_is_flagged_across_sectors(self):
        names = ["NVDA", "MSFT", "CRM", "GOOGL", "META", "AMZN"]
        # All six are one trade...
        assert all(sector_map.get_correlation_group(t) == "mega_cap_growth_tech"
                   for t in names)
        # ...while the official view scatters them, which is the whole problem.
        assert len({sector_map.get_sector(t) for t in names}) >= 2

    def test_crypto_names_are_grouped_by_what_they_track(self):
        for t in ("HUT", "RIOT", "WGMI", "BTCUSD"):
            assert sector_map.get_correlation_group(t) == "crypto_exposure"

    def test_index_etfs_are_their_own_bet(self):
        assert sector_map.get_correlation_group("SPY") == "us_large_cap_index"
        assert sector_map.get_correlation_group("QQQ") == "us_large_cap_index"

    def test_most_tickers_have_no_correlation_group(self):
        # Sparse on purpose: an entry claims the official sector is misleading
        # for that name, and that claim should be rare.
        assert sector_map.get_correlation_group("JPM") is None
        assert sector_map.get_correlation_group("LLY") is None

    def test_correlation_group_never_overrides_the_sector(self):
        # Two labels, two jobs. The bet basket must not leak into the standard.
        assert sector_map.get_sector("AMZN") == "Consumer Discretionary"
        assert sector_map.get_correlation_group("AMZN") == "mega_cap_growth_tech"


class TestCoverage:
    def test_coverage_is_wide_enough_for_the_cap_to_function(self):
        # The old map covered 7.4% of traded names, which made the 40% cap
        # unfireable -- everything shared one "unknown" bucket. This test is
        # the regression guard for that specific failure.
        known = sum(1 for t in sector_map._SEC if sector_map.get_sector(t))
        assert known > 400, f"only {known} tickers have a sector -- cap will not work"

    def test_describe_returns_every_label_at_once(self):
        info = sector_map.describe("NVDA")
        assert info["sector"] == "Information Technology"
        assert info["sector_etf"] == "XLK"
        assert info["moves_with"] == "mega_cap_growth_tech"
        assert info["industry"]


class TestSicMapping:
    def test_missing_or_junk_sic_returns_none(self):
        assert sic_to_sector.sector_etf_for_sic(None) is None
        assert sic_to_sector.sector_etf_for_sic("") is None
        assert sic_to_sector.sector_etf_for_sic("9999") is None

    def test_pharma_beats_the_coarse_chemicals_group(self):
        # SIC 28 is "chemicals" as a whole; 2834 is pharmaceutical preparations
        # and belongs in Health Care, not Materials.
        assert sic_to_sector.sector_etf_for_sic("2834") == "XLV"
        assert sic_to_sector.sector_etf_for_sic("2820") == "XLB"

    def test_software_and_machinery_split_correctly(self):
        assert sic_to_sector.sector_etf_for_sic("7372") == "XLK"   # software
        assert sic_to_sector.sector_etf_for_sic("3531") == "XLI"   # construction machinery

    def test_medical_instruments_are_health_care_not_industrials(self):
        assert sic_to_sector.sector_etf_for_sic("3841") == "XLV"
        assert sic_to_sector.sector_etf_for_sic("3823") == "XLI"

    def test_sic_is_padded_not_truncated(self):
        # A 3-digit code must not be read as a different 2-digit major group.
        assert sic_to_sector.sector_etf_for_sic("100") == sic_to_sector.sector_etf_for_sic("0100")
