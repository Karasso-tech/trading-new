"""Tests for the /pnl money split -- pnl_split.py's arithmetic, and
persistence.get_pnl_positions()'s reading of it off real rows.

What these pin, in order of what costs money if it drifts:

1. Core dollars and trading dollars never leak into each other. That leak IS
   the problem the command was built for; a split that silently miscounts one
   book is worse than the blended broker number it replaces.
2. Banked dollars and paper dollars stay separate figures.
3. A position with no live price is named, not valued at zero and not dropped.
4. Commissions come out where they were recorded, once each.
"""

import pytest

import persistence
import pnl_split


def _row(ticker, sleeve, *, entry_price=100.0, qty=10, remaining_qty=10,
         realized_usd=0.0, status="open", entry_date="2026-06-01"):
    return {
        "id": abs(hash((ticker, entry_date, qty))) % 10000, "ticker": ticker, "sleeve": sleeve,
        "status": status, "entry_date": entry_date, "entry_price": entry_price, "qty": qty,
        "sold_qty": qty - remaining_qty, "remaining_qty": remaining_qty,
        "realized_usd": realized_usd, "fees_usd": 0.0,
    }


class TestTheTwoBooksStaySeparate:
    def test_core_dollars_never_land_in_the_trading_bucket(self):
        rows = [
            _row("SPY", "core", entry_price=700.0, qty=10, remaining_qty=10),
            _row("AAPL", "swing", entry_price=300.0, qty=10, remaining_qty=10),
        ]
        split = pnl_split.split_pnl(rows, {"SPY": 750.0, "AAPL": 290.0})
        assert split["core"]["open_usd"] == pytest.approx(500.0)
        assert split["trades"]["open_usd"] == pytest.approx(-100.0)
        assert split["total"]["open_usd"] == pytest.approx(400.0)

    def test_a_green_core_can_never_hide_a_red_trading_book(self):
        """The whole point of the command: the blended number is positive here,
        and the trading book is still losing money."""
        rows = [
            _row("QQQ", "core", entry_price=600.0, qty=100, remaining_qty=100),
            _row("NVDA", "swing", remaining_qty=0, realized_usd=-2000.0, status="closed"),
        ]
        split = pnl_split.split_pnl(rows, {"QQQ": 660.0})
        assert split["total"]["total_usd"] > 0                          # broker's one number: green
        assert split["trades"]["total_usd"] == pytest.approx(-2000.0)    # the truth under it

    def test_a_closed_core_sale_is_counted_as_core_not_as_a_trade(self):
        rows = [_row("SPY", "core", remaining_qty=0, realized_usd=1500.0, status="closed")]
        split = pnl_split.split_pnl(rows, {})
        assert split["core"]["realized_usd"] == pytest.approx(1500.0)
        assert split["trades"]["realized_usd"] == 0.0


class TestBankedVersusPaper:
    def test_the_two_are_reported_as_separate_figures_and_a_sum(self):
        rows = [
            _row("AAPL", "swing", entry_price=300.0, qty=10, remaining_qty=10),
            _row("MSFT", "swing", remaining_qty=0, realized_usd=250.0, status="closed"),
        ]
        split = pnl_split.split_pnl(rows, {"AAPL": 310.0})
        assert split["trades"]["realized_usd"] == pytest.approx(250.0)
        assert split["trades"]["open_usd"] == pytest.approx(100.0)
        assert split["trades"]["total_usd"] == pytest.approx(350.0)

    def test_the_percent_is_measured_only_against_money_still_in(self):
        """A percent over realized dollars has no honest denominator -- 100 of
        paper profit on 3,000 still invested is 3.3%, whatever was banked
        earlier."""
        rows = [
            _row("AAPL", "swing", entry_price=300.0, qty=10, remaining_qty=10),
            _row("MSFT", "swing", remaining_qty=0, realized_usd=99999.0, status="closed"),
        ]
        split = pnl_split.split_pnl(rows, {"AAPL": 310.0})
        assert split["trades"]["open_pct"] == pytest.approx(100.0 / 3000.0)

    def test_no_open_shares_leaves_no_percent_rather_than_a_zero(self):
        split = pnl_split.split_pnl([_row("MSFT", "swing", remaining_qty=0, realized_usd=10.0)], {})
        assert split["trades"]["open_pct"] is None


class TestAMissingPriceIsNamed:
    def test_an_unpriced_holding_is_listed_and_the_split_is_marked_incomplete(self):
        rows = [_row("HOOD", "swing", entry_price=90.0, qty=10, remaining_qty=10)]
        split = pnl_split.split_pnl(rows, {})
        assert split["complete"] is False
        assert split["unpriced_tickers"] == ["HOOD"]

    def test_an_unpriced_holding_is_not_valued_at_zero(self):
        """Counting it as a total loss would be a fabricated -900 here."""
        rows = [_row("HOOD", "swing", entry_price=90.0, qty=10, remaining_qty=10)]
        split = pnl_split.split_pnl(rows, {})
        assert split["trades"]["open_usd"] == 0.0
        assert split["trades"]["held_cost_usd"] == 0.0

    def test_the_other_positions_still_get_a_real_number(self):
        rows = [
            _row("HOOD", "swing", entry_price=90.0, qty=10, remaining_qty=10),
            _row("AAPL", "swing", entry_price=300.0, qty=10, remaining_qty=10),
        ]
        split = pnl_split.split_pnl(rows, {"AAPL": 310.0})
        assert split["trades"]["open_usd"] == pytest.approx(100.0)
        assert split["unpriced_tickers"] == ["HOOD"]

    def test_a_price_of_none_counts_as_no_price_not_as_zero(self):
        rows = [_row("HOOD", "swing", entry_price=90.0, qty=10, remaining_qty=10)]
        split = pnl_split.split_pnl(rows, {"HOOD": None})
        assert split["unpriced_tickers"] == ["HOOD"]

    def test_prices_are_matched_case_insensitively(self):
        rows = [_row("AAPL", "swing", entry_price=300.0, qty=10, remaining_qty=10)]
        split = pnl_split.split_pnl(rows, {"aapl": 310.0})
        assert split["complete"] is True


class TestCounts:
    def test_open_and_closed_trades_are_counted_separately_and_exclude_core(self):
        rows = [
            _row("SPY", "core"),
            _row("AAPL", "swing"),
            _row("MSFT", "swing", remaining_qty=0, status="closed"),
            _row("NVDA", "swing", remaining_qty=0, status="closed"),
        ]
        split = pnl_split.split_pnl(rows, {})
        assert split["open_trades"] == 1
        assert split["closed_trades"] == 2

    def test_the_first_entry_date_is_the_oldest_one_on_file(self):
        rows = [
            _row("AAPL", "swing", entry_date="2026-07-01"),
            _row("SPY", "core", entry_date="2026-04-30"),
        ]
        assert pnl_split.split_pnl(rows, {})["first_entry_date"] == "2026-04-30"

    def test_nothing_recorded_at_all_is_zeros_and_no_date_not_a_crash(self):
        split = pnl_split.split_pnl([], {})
        assert split["total"]["total_usd"] == 0.0
        assert split["first_entry_date"] is None
        assert split["complete"] is True


# ---------------------------------------------------------------------------
# The DB read the numbers above are fed from.
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(persistence, "DB_PATH", db_path)
    persistence.init_db()
    return db_path


def _fill(ticker, entry_price, qty, *, entry_commission=None, entry_date="2026-06-01"):
    # positions.ticker is a foreign key into thesis -- a fill can only exist for
    # a ticker this system already knows about, same as in real use.
    with persistence._db() as conn:
        conn.execute(
            "INSERT INTO thesis (ticker, status, sleeve, updated_at) "
            "VALUES (?, 'open_position', 'swing', ?)",
            (ticker, persistence._now()),
        )
    persistence.create_position(
        ticker=ticker, entry_date=entry_date, entry_price=entry_price, qty=qty,
        entry_type="full",
        entry_setup={"type": "Breakout", "trigger": entry_price, "stop": entry_price * 0.9,
                     "atr_at_build": 1.0},
        initial_stop=entry_price * 0.9, current_stop=entry_price * 0.9,
        entry_commission=entry_commission,
    )


class TestGetPnlPositions:
    def test_a_closed_trade_reports_the_dollars_it_actually_made(self, temp_db):
        _fill("AAPL", 300.0, 10)
        persistence.record_exit("AAPL", exit_price=310.0, exit_qty=10,
                                exit_date="2026-06-10", source="exit_command")
        row = next(r for r in persistence.get_pnl_positions() if r["ticker"] == "AAPL")
        assert row["realized_usd"] == pytest.approx(100.0)
        assert row["remaining_qty"] == 0

    def test_a_partial_exit_banks_its_own_dollars_and_leaves_the_rest_open(self, temp_db):
        _fill("AAPL", 300.0, 10)
        persistence.record_exit("AAPL", exit_price=310.0, exit_qty=4,
                                exit_date="2026-06-10", source="exit_command")
        row = next(r for r in persistence.get_pnl_positions() if r["ticker"] == "AAPL")
        assert row["realized_usd"] == pytest.approx(40.0)
        assert row["remaining_qty"] == 6

    def test_commissions_come_out_once_each(self, temp_db):
        _fill("AAPL", 300.0, 10, entry_commission=1.5)
        persistence.record_exit("AAPL", exit_price=310.0, exit_qty=10,
                                exit_date="2026-06-10", source="exit_command", commission=2.0)
        row = next(r for r in persistence.get_pnl_positions() if r["ticker"] == "AAPL")
        assert row["fees_usd"] == pytest.approx(3.5)
        assert row["realized_usd"] == pytest.approx(96.5)

    def test_the_sleeve_is_the_hard_spy_qqq_rule_not_the_stored_column(self, temp_db):
        _fill("SPY", 700.0, 10)
        _fill("AAPL", 300.0, 10)
        persistence.set_sleeve("SPY", "swing")   # a wrong stored value must not win
        by_ticker = {r["ticker"]: r["sleeve"] for r in persistence.get_pnl_positions()}
        assert by_ticker["SPY"] == "core"
        assert by_ticker["AAPL"] == "swing"

    def test_open_and_closed_positions_both_appear(self, temp_db):
        _fill("AAPL", 300.0, 10)
        _fill("MSFT", 400.0, 10)
        persistence.record_exit("MSFT", exit_price=390.0, exit_qty=10,
                                exit_date="2026-06-10", source="exit_command")
        statuses = {r["ticker"]: r["status"] for r in persistence.get_pnl_positions()}
        assert statuses == {"AAPL": "open", "MSFT": "closed"}
