"""Tests for the /pnl message shape (pnl_text.py).

The command exists because one blended number was frightening and unreadable,
so the message's job is to stay readable under exactly the conditions that
made the blended number bad. These pin the four places where a wrong render
would put the old confusion back:

* the two books each appear as their own block, with their own total;
* profit and loss are told apart by more than a minus sign;
* a percent never appears next to banked dollars;
* a position with no price is named on the page, not silently missing.
"""

import pnl_split
import pnl_text


def _row(ticker, sleeve, *, entry_price=100.0, qty=10, remaining_qty=10,
         realized_usd=0.0, entry_date="2026-06-01"):
    return {
        "id": 1, "ticker": ticker, "sleeve": sleeve,
        "status": "open" if remaining_qty else "closed", "entry_date": entry_date,
        "entry_price": entry_price, "qty": qty, "sold_qty": qty - remaining_qty,
        "remaining_qty": remaining_qty, "realized_usd": realized_usd, "fees_usd": 0.0,
    }


def _message(rows, prices):
    return pnl_text.build_pnl_message(pnl_split.split_pnl(rows, prices))


FULL_ROWS = [
    _row("SPY", "core", entry_price=700.0, qty=10, remaining_qty=10, entry_date="2026-04-30"),
    _row("AAPL", "swing", entry_price=300.0, qty=10, remaining_qty=10),
    _row("MSFT", "swing", qty=10, remaining_qty=0, realized_usd=-250.0),
]
FULL_PRICES = {"SPY": 750.0, "AAPL": 310.0}


class TestTheTwoBooksEachGetTheirOwnBlock:
    def test_both_headings_are_present(self):
        text = _message(FULL_ROWS, FULL_PRICES)
        assert "Core" in text
        assert "מסחר" in text

    def test_the_trading_book_carries_its_own_total(self):
        text = _message(FULL_ROWS, FULL_PRICES)
        assert 'סה"כ מהמסחר: <b>-$150</b>' in text      # -250 banked + 100 on paper

    def test_the_combined_number_is_shown_and_named_as_the_brokers_one(self):
        text = _message(FULL_ROWS, FULL_PRICES)
        assert "שניהם ביחד: <b>+$350</b>" in text        # 500 core + (-150) trading
        assert "הברוקר" in text

    def test_a_green_core_does_not_erase_the_red_trading_line(self):
        """The failure this command was built to prevent, at the render layer."""
        text = _message(FULL_ROWS, FULL_PRICES)
        assert "-$150" in text
        assert "+$500" in text


class TestProfitAndLossAreToldApart:
    def test_a_profit_always_carries_a_plus_sign(self):
        text = _message([_row("AAPL", "swing", entry_price=300.0)], {"AAPL": 310.0})
        assert "+$100" in text

    def test_a_loss_carries_a_minus_sign_and_never_a_plus(self):
        text = _message([_row("AAPL", "swing", entry_price=300.0)], {"AAPL": 290.0})
        assert "-$100" in text
        assert "+$100" not in text

    def test_money_put_in_is_shown_without_a_sign(self):
        """Cost is a size, not an up or down -- "+$3,000 put in" would read as
        a gain."""
        text = _message([_row("SPY", "core", entry_price=300.0)], {"SPY": 310.0})
        assert "שמתי פנימה: <b>$3,000</b>" in text


class TestNoPercentNextToBankedDollars:
    def test_the_closed_trades_line_has_no_percent(self):
        rows = [_row("MSFT", "swing", qty=10, remaining_qty=0, realized_usd=-250.0)]
        closed_line = next(l for l in _message(rows, {}).split("\n") if "עסקאות שנסגרו" in l)
        assert "%" not in closed_line

    def test_the_open_line_does_carry_its_percent(self):
        rows = [_row("AAPL", "swing", entry_price=300.0)]
        open_line = next(l for l in _message(rows, {"AAPL": 310.0}).split("\n") if "עסקאות פתוחות" in l)
        assert "+3.3%" in open_line


class TestAMissingPriceIsVisible:
    def test_the_unpriced_ticker_is_named_in_a_warning(self):
        rows = [_row("HOOD", "swing", entry_price=90.0)]
        text = _message(rows, {})
        assert "HOOD" in text
        assert "⚠️" in text

    def test_the_holding_line_says_no_price_rather_than_showing_a_number(self):
        rows = [_row("HOOD", "swing", entry_price=90.0)]
        line = next(l for l in _message(rows, {}).split("\n") if l.startswith("📈 <b>HOOD"))
        assert "אין מחיר עכשיו" in line

    def test_no_warning_appears_when_every_price_arrived(self):
        assert "⚠️" not in _message(FULL_ROWS, FULL_PRICES)


class TestTheHonestyLines:
    def test_the_start_date_is_stated_so_it_is_not_read_as_the_whole_account(self):
        text = _message(FULL_ROWS, FULL_PRICES)
        assert "30.04.2026" in text

    def test_an_empty_book_still_renders_instead_of_crashing(self):
        text = pnl_text.build_pnl_message(pnl_split.split_pnl([], {}))
        assert "אין כרגע Core פתוח" in text
        assert "שניהם ביחד: <b>$0</b>" in text


class TestRoundingNeverInventsASign:
    def test_a_few_cents_of_loss_reads_as_zero_not_as_minus_zero(self):
        """"-$0" and "-0.0%" look like something is wrong when nothing is."""
        rows = [_row("AAPL", "swing", entry_price=300.0, qty=10, remaining_qty=10)]
        text = _message(rows, {"AAPL": 299.98})
        assert "-$0" not in text
        assert "-0.0%" not in text
        assert "$0" in text

    def test_a_real_loss_still_shows_its_minus(self):
        rows = [_row("AAPL", "swing", entry_price=300.0, qty=10, remaining_qty=10)]
        assert "-$100" in _message(rows, {"AAPL": 290.0})
