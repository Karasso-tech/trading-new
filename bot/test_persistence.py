"""Unit tests for persistence.py's stop-monotonicity guard, single-ticker
open-position lookup (2026-07-14), and starter->full add-to-position (2026-07-15).

Uses an isolated temp SQLite DB (never the real trading_new.db) via
monkeypatching persistence.DB_PATH -- same pattern as test_circuit_breaker.py.

Context (stop guard / get_open_position): /playbook's automation used to be
blind to a position's real entry_setup (blocked from querying the DB at all),
so it reinvented a stop from generic chart structure on every run and
update_current_stop() wrote it straight over the real trailed value with zero
check on direction -- found real on the live DB: NVDA's stop silently moved
from 201.92 to 195.06 between two runs, and ANET's current_stop (165.99) ended
up below its own initial_stop (179.80). Those tests lock in the fix:
get_open_position() gives /playbook a way to see the real data, and
update_current_stop() now refuses to lower a stop unless explicitly told to.

Context (add_to_position): /filled always inserted a brand-new positions row,
with no way to record adding shares to an already-open starter position to
bring it up to full size -- see add_to_position()'s own docstring.
"""

from datetime import datetime, timedelta, timezone

import pytest

import persistence


def _utcnow_iso() -> str:
    """"Right now" in the same ISO-8601 UTC format persistence._now() uses --
    see TestXPosts's own docstring for why the X-post tests need this instead
    of a hardcoded date."""
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(persistence, "DB_PATH", db_path)
    persistence.init_db()
    return db_path


def _open_nvda(temp_db, current_stop=195.06, initial_stop=189.80, entry_type="full",
                entry_price=208.62, qty=75):
    with persistence._db() as conn:
        conn.execute(
            "INSERT INTO thesis (ticker, status, sleeve, updated_at) VALUES (?, 'open_position', 'swing', ?)",
            ("NVDA", persistence._now()),
        )
    persistence.create_position(
        ticker="NVDA", entry_date="2026-07-11", entry_price=entry_price, qty=qty,
        entry_type=entry_type,
        entry_setup={"type": "Reclaim", "trigger": "...", "stop": initial_stop, "atr_at_build": 7.13},
        initial_stop=initial_stop, current_stop=current_stop,
    )


class TestUpdateCurrentStopMonotonicGuard:
    def test_raising_the_stop_succeeds(self, temp_db):
        _open_nvda(temp_db, current_stop=195.06)
        assert persistence.update_current_stop("NVDA", 200.0) is True
        assert persistence.get_open_position("NVDA")["current_stop"] == 200.0

    def test_lowering_the_stop_is_rejected_and_leaves_it_unchanged(self, temp_db):
        _open_nvda(temp_db, current_stop=201.92)
        with pytest.raises(ValueError, match="refusing to lower"):
            persistence.update_current_stop("NVDA", 195.06)
        # The real bug this guards against: the rejected write must not have
        # partially applied -- current_stop stays exactly what it was.
        assert persistence.get_open_position("NVDA")["current_stop"] == 201.92

    def test_lowering_with_allow_lower_true_succeeds(self, temp_db):
        _open_nvda(temp_db, current_stop=201.92)
        assert persistence.update_current_stop("NVDA", 195.06, allow_lower=True) is True
        assert persistence.get_open_position("NVDA")["current_stop"] == 195.06

    def test_equal_stop_is_not_treated_as_a_lowering(self, temp_db):
        _open_nvda(temp_db, current_stop=195.06)
        assert persistence.update_current_stop("NVDA", 195.06) is True

    def test_no_open_position_for_ticker_is_a_plain_noop_not_an_error(self, temp_db):
        # A portfolio screenshot can include tickers this system never filled
        # through /filled -- must not raise, just report nothing happened.
        assert persistence.update_current_stop("MSFT", 300.0) is False

    def test_never_touches_initial_stop(self, temp_db):
        _open_nvda(temp_db, current_stop=195.06, initial_stop=189.80)
        persistence.update_current_stop("NVDA", 210.0)
        assert persistence.get_open_position("NVDA")["initial_stop"] == 189.80


class TestGetOpenPosition:
    def test_returns_none_for_a_ticker_never_filled(self, temp_db):
        assert persistence.get_open_position("MSFT") is None

    def test_returns_the_real_documented_position(self, temp_db):
        _open_nvda(temp_db)
        pos = persistence.get_open_position("NVDA")
        assert pos["ticker"] == "NVDA"
        assert pos["initial_stop"] == 189.80
        assert pos["current_stop"] == 195.06
        assert pos["entry_setup"]["type"] == "Reclaim"  # parsed back to a dict, not a JSON string

    def test_returns_none_for_a_closed_position(self, temp_db):
        _open_nvda(temp_db)
        persistence.record_exit("NVDA", exit_price=210.0, exit_qty=75, exit_date="2026-07-15",
                                 source="exit_command")
        assert persistence.get_open_position("NVDA") is None


class TestAddToPosition:
    def test_adding_to_a_starter_blends_qty_and_price_and_flips_to_full(self, temp_db):
        _open_nvda(temp_db, entry_type="starter", entry_price=208.62, qty=50)
        position_id = persistence.add_to_position("NVDA", additional_qty=50, additional_price=210.62)
        pos = persistence.get_open_position("NVDA")
        assert pos["id"] == position_id
        assert pos["qty"] == 100
        assert pos["entry_price"] == pytest.approx(209.62)  # (50*208.62 + 50*210.62) / 100
        assert pos["entry_type"] == "full"

    def test_adding_to_an_already_full_position_stays_full(self, temp_db):
        _open_nvda(temp_db, entry_type="full", entry_price=208.62, qty=75)
        persistence.add_to_position("NVDA", additional_qty=25, additional_price=212.0)
        pos = persistence.get_open_position("NVDA")
        assert pos["qty"] == 100
        assert pos["entry_type"] == "full"

    def test_uneven_qty_weighted_average_is_correct(self, temp_db):
        _open_nvda(temp_db, entry_type="starter", entry_price=100.0, qty=10)
        persistence.add_to_position("NVDA", additional_qty=90, additional_price=200.0)
        pos = persistence.get_open_position("NVDA")
        assert pos["qty"] == 100
        assert pos["entry_price"] == pytest.approx(190.0)  # (10*100 + 90*200) / 100

    def test_initial_stop_current_stop_entry_setup_entry_date_are_all_untouched(self, temp_db):
        _open_nvda(temp_db, entry_type="starter", initial_stop=189.80, current_stop=195.06)
        persistence.add_to_position("NVDA", additional_qty=25, additional_price=210.0)
        pos = persistence.get_open_position("NVDA")
        assert pos["initial_stop"] == 189.80
        assert pos["current_stop"] == 195.06
        assert pos["entry_setup"]["type"] == "Reclaim"
        assert pos["entry_date"] == "2026-07-11"

    def test_no_open_position_raises_value_error(self, temp_db):
        with pytest.raises(ValueError, match="use /filled for a new entry"):
            persistence.add_to_position("MSFT", additional_qty=10, additional_price=300.0)


def _set_planned_qty(ticker, planned_qty):
    with persistence._db() as conn:
        conn.execute("UPDATE thesis SET planned_qty=? WHERE ticker=?", (planned_qty, ticker))


class TestAddToFullQty:
    """add_to_full_qty (2026-07-23): STRATEGY_v3.md's starter-confirmation 🌱
    line now tells the user how many more units to buy to reach the thesis's
    own full-size target (planned_qty, already run through that thesis's
    ATR/regime/volume multipliers at /screener time) -- never a fresh
    risk_usd/(entry-stop) guess that would silently drop those multipliers."""

    def test_starter_with_planned_qty_reports_the_gap(self, temp_db):
        _open_nvda(temp_db, entry_type="starter", qty=50)
        _set_planned_qty("NVDA", 100)
        assert persistence.get_open_position("NVDA")["add_to_full_qty"] == 50

    def test_starter_with_no_planned_qty_on_thesis_is_none(self, temp_db):
        _open_nvda(temp_db, entry_type="starter", qty=50)
        assert persistence.get_open_position("NVDA")["add_to_full_qty"] is None

    def test_full_position_is_always_none_even_with_planned_qty_set(self, temp_db):
        _open_nvda(temp_db, entry_type="full", qty=100)
        _set_planned_qty("NVDA", 100)
        assert persistence.get_open_position("NVDA")["add_to_full_qty"] is None

    def test_already_at_or_above_planned_qty_clamps_to_zero_not_negative(self, temp_db):
        _open_nvda(temp_db, entry_type="starter", qty=100)
        _set_planned_qty("NVDA", 60)
        assert persistence.get_open_position("NVDA")["add_to_full_qty"] == 0

    def test_uses_remaining_qty_not_original_qty_after_a_partial_exit(self, temp_db):
        _open_nvda(temp_db, entry_type="starter", qty=50, entry_price=100.0, initial_stop=90.0)
        _set_planned_qty("NVDA", 100)
        persistence.record_exit("NVDA", exit_price=110.0, exit_qty=20, exit_date="2026-07-16",
                                 source="exit_command")
        assert persistence.get_open_position("NVDA")["add_to_full_qty"] == 70  # 100 - (50-20)

    def test_add_then_partial_exit_uses_the_new_blended_qty_as_the_close_threshold(self, temp_db):
        # End-to-end: confirms record_exit() needs no changes -- it already
        # compares cumulative exit_qty against positions.qty, which
        # add_to_position() updates in place.
        _open_nvda(temp_db, entry_type="starter", entry_price=208.62, qty=50)
        persistence.add_to_position("NVDA", additional_qty=50, additional_price=210.62)  # qty now 100

        persistence.record_exit("NVDA", exit_price=236.54, exit_qty=40, exit_date="2026-07-16",
                                 source="exit_command")
        # 40 of 100 exited -- position must still be open.
        assert persistence.get_open_position("NVDA") is not None

        persistence.record_exit("NVDA", exit_price=240.0, exit_qty=60, exit_date="2026-07-17",
                                 source="exit_command")
        # 100 of 100 exited -- now fully closed.
        assert persistence.get_open_position("NVDA") is None


class TestTranchePlanOnOpenPositions:
    """Rule 7's tranche state, attached to every open position this module hands
    out (2026-08-07). The regression it exists for is real: ASTS position 25 had
    one stored target at 72.36, sold at it on 2026-08-05, and sold again on
    2026-08-06 -- both rows landed in the DB as exit_reason='target_1' because
    derive_exit_reason had no memory of the first one. The Runner tranche
    (which the 5-year backtest showed carries the entire edge) was cut in half
    with nothing anywhere saying so."""

    def _open_asts(self):
        with persistence._db() as conn:
            conn.execute(
                "INSERT INTO thesis (ticker, status, sleeve, updated_at) VALUES (?, 'open_position', 'swing', ?)",
                ("ASTS", persistence._now()),
            )
        persistence.create_position(
            ticker="ASTS", entry_date="2026-07-31", entry_price=58.86, qty=211,
            entry_type="full",
            entry_setup={"type": "Reclaim", "trigger": 53.33, "stop": 51.84, "atr_at_build": 6.39,
                          "targets": [{"price": 72.36, "pct": 40}]},
            initial_stop=51.84, current_stop=54.78,
        )

    def test_a_fresh_position_shows_target_1_as_next(self, temp_db):
        self._open_asts()
        plan = persistence.get_open_position("ASTS")["tranche_plan"]
        assert plan["next_label"] == "target_1"
        assert plan["next_price"] == 72.36
        assert plan["next_qty"] == 84
        assert plan["runner_qty_left"] == 127

    def test_the_same_target_can_never_be_recorded_twice(self, temp_db):
        self._open_asts()
        persistence.record_exit("ASTS", exit_price=72.36, exit_qty=84,
                                 exit_date="2026-08-05", source="exit_command")
        persistence.record_exit("ASTS", exit_price=73.64, exit_qty=45,
                                 exit_date="2026-08-06", source="exit_command")
        with persistence._db() as conn:
            reasons = [r["exit_reason"] for r in conn.execute(
                "SELECT exit_reason FROM exits WHERE ticker='ASTS' ORDER BY id ASC")]
        assert reasons == ["target_1", "runner_trim"]

    def test_after_target_1_the_runner_is_next_and_has_no_price_to_sell_at(self, temp_db):
        self._open_asts()
        persistence.record_exit("ASTS", exit_price=72.36, exit_qty=84,
                                 exit_date="2026-08-05", source="exit_command")
        plan = persistence.get_open_position("ASTS")["tranche_plan"]
        assert plan["next_label"] == "runner"
        assert plan["next_price"] is None
        assert plan["tranches"][0]["status"] == "filled"

    def test_a_runner_trim_shrinks_the_runner_not_the_finished_target(self, temp_db):
        self._open_asts()
        persistence.record_exit("ASTS", exit_price=72.36, exit_qty=84,
                                 exit_date="2026-08-05", source="exit_command")
        persistence.record_exit("ASTS", exit_price=73.64, exit_qty=45,
                                 exit_date="2026-08-06", source="exit_command")
        plan = persistence.get_open_position("ASTS")["tranche_plan"]
        target_1, runner = plan["tranches"]
        assert target_1["filled_qty"] == 84          # still exactly once
        assert runner["filled_qty"] == 45
        assert plan["runner_qty_left"] == 82
        assert plan["remaining_qty"] == 82

    def test_open_positions_list_carries_the_same_plan(self, temp_db):
        self._open_asts()
        row = next(r for r in persistence.get_open_positions() if r["ticker"] == "ASTS")
        assert row["tranche_plan"]["next_label"] == "target_1"


class TestPendingMoveFlag:
    """The 'moved since thesis built' flag on get_pending_report_rows() -- purely
    informational (mirrors the existing age/regime flags), never touches the
    stored trigger/stop/target. Compares the last price seen via /monitor
    (monitor_log.price) against the stored trigger, scaled by atr_at_build."""

    def _save_pending(self, trigger=100.0, atr_at_build=2.0):
        persistence.save_thesis(
            ticker="ABC", status="pending", source="screener",
            primary_setup={"type": "Reclaim", "trigger": trigger, "stop": 95.0,
                           "atr_at_build": atr_at_build, "targets": []},
        )

    def test_flags_when_price_drifted_beyond_the_atr_threshold(self, temp_db):
        self._save_pending(trigger=100.0, atr_at_build=2.0)
        persistence.log_monitor_check("ABC", status="white", price=103.0)  # 3.0 >= 1.0*2.0
        row = persistence.get_pending_report_rows()[0]
        assert row["flag"] is True
        assert "moved" in row["flag_reasons"]

    def test_does_not_flag_when_price_is_within_the_atr_threshold(self, temp_db):
        self._save_pending(trigger=100.0, atr_at_build=2.0)
        persistence.log_monitor_check("ABC", status="white", price=101.0)  # 1.0 < 1.0*2.0
        row = persistence.get_pending_report_rows()[0]
        assert row["flag"] is False
        assert "moved" not in row["flag_reasons"]

    def test_does_not_flag_with_no_monitor_log_yet(self, temp_db):
        # No log_monitor_check call at all -- latest_price is None, nothing to
        # compare against. Must not guess a flag either way.
        self._save_pending(trigger=100.0, atr_at_build=2.0)
        row = persistence.get_pending_report_rows()[0]
        assert "moved" not in row["flag_reasons"]


class TestSetupNumericFieldValidation:
    """Real CRM incident, 2026-07-31: SCREENER_v3 saved primary_setup.trigger as
    the string "260.00" (with a real numeric stop already set), and save_thesis
    rejected it outright -- correct per rule 27 (a live trigger with a numeric
    stop must be re-gradeable), but it meant the report could never be saved,
    ever, on retry, since the bad string never changed. Fix: a quoted string
    that IS a clean number gets silently coerced to float instead of rejected;
    only a genuine free-text trigger (no number to extract) still raises."""

    def test_numeric_string_trigger_is_coerced_not_rejected(self, temp_db):
        persistence.save_thesis(
            ticker="CRM", status="pending", source="screener",
            primary_setup={"type": "Reclaim", "trigger": "260.00", "stop": 250.0,
                           "atr_at_build": 5.0, "targets": []},
        )
        saved = persistence.get_thesis("CRM")
        assert saved["primary_setup"]["trigger"] == 260.0

    def test_numeric_string_target_price_is_coerced_not_rejected(self, temp_db):
        persistence.save_thesis(
            ticker="CRM", status="pending", source="screener",
            primary_setup={"type": "Reclaim", "trigger": 260.0, "stop": 250.0,
                           "atr_at_build": 5.0, "targets": [{"price": "270.00"}]},
        )
        saved = persistence.get_thesis("CRM")
        assert saved["primary_setup"]["targets"][0]["price"] == 270.0

    def test_genuine_free_text_trigger_with_stop_set_still_raises(self, temp_db):
        with pytest.raises(ValueError, match="trigger must be numeric"):
            persistence.save_thesis(
                ticker="CRM", status="pending", source="screener",
                primary_setup={"type": "Reclaim", "trigger": "close above 260", "stop": 250.0,
                               "atr_at_build": 5.0, "targets": []},
            )

    def test_free_text_trigger_still_allowed_with_no_stop_yet(self, temp_db):
        # Still-watching setup, no stop yet -- a descriptive trigger is legitimate
        # here (e.g. "reclaim of 126-128 zone"), same as ASTS's real case.
        persistence.save_thesis(
            ticker="CRM", status="pending", source="screener",
            primary_setup={"type": "Reclaim", "trigger": "reclaim of 126-128 zone", "stop": None,
                           "atr_at_build": 5.0, "targets": []},
        )
        saved = persistence.get_thesis("CRM")
        assert saved["primary_setup"]["trigger"] == "reclaim of 126-128 zone"


class TestCorruptedJsonRowIsolation:
    """Found in review: get_open_positions()/get_pending_report_rows()/
    get_journal_rows()/get_shadow_candidates() used to call json.loads() on
    stored blobs with no try/except -- one ticker with a malformed blob (a
    crash mid-write, a future caller bug) would raise and take down the WHOLE
    report, hiding every other ticker's valid data too. _safe_json_loads()
    isolates this to just the bad row's field."""

    def test_a_corrupted_row_does_not_break_reading_other_valid_rows(self, temp_db):
        _open_nvda(temp_db)  # a real, valid row
        with persistence._db() as conn:
            conn.execute(
                "INSERT INTO thesis (ticker, status, sleeve, updated_at) VALUES (?, 'open_position', 'swing', ?)",
                ("BADCO", persistence._now()),
            )
            conn.execute(
                "INSERT INTO positions (ticker, entry_date, entry_price, qty, entry_type, "
                "initial_stop, current_stop, entry_setup, status, updated_at) "
                "VALUES ('BADCO', '2026-07-11', 100.0, 10, 'full', 90.0, 90.0, "
                "'{not valid json', 'open', ?)",
                (persistence._now(),),
            )
        rows = persistence.get_open_positions()
        tickers = {r["ticker"] for r in rows}
        assert tickers == {"NVDA", "BADCO"}
        badco = next(r for r in rows if r["ticker"] == "BADCO")
        assert badco["entry_setup"] is None  # corrupted blob -> None, not a crash


class TestRemainingQty:
    """Found in review (2026-07-16): positions.qty is the original, fixed fill
    size (correctly never touched by a partial exit -- it's the R-multiple/
    full-close-threshold denominator). But nothing computed the REAL live
    share count after a partial exit, so every consumer (fetch_analysis_data.py
    -> /playbook's screenshot comparison, /open's own display) read the stale
    original qty forever. Real incident this reproduces: XLF, 350 original,
    140 exited via a genuine /exit two days earlier, 210 truly remaining --
    /playbook saw qty=350, correctly-already-recorded 210 in the screenshot,
    and wrongly concluded it was an unrecorded discrepancy."""

    def test_no_exits_yet_remaining_equals_original(self, temp_db):
        _open_nvda(temp_db, qty=75)
        pos = persistence.get_open_position("NVDA")
        assert pos["qty"] == 75
        assert pos["remaining_qty"] == 75

    def test_one_partial_exit_reduces_remaining_but_not_qty(self, temp_db):
        _open_nvda(temp_db, qty=350, entry_price=54.765, initial_stop=53.9, current_stop=56.01)
        persistence.record_exit("NVDA", exit_price=56.52, exit_qty=140, exit_date="2026-07-14",
                                 source="exit_command")
        pos = persistence.get_open_position("NVDA")
        assert pos["qty"] == 350        # original fill size, never changes
        assert pos["remaining_qty"] == 210  # the real XLF numbers, reproduced

    def test_get_open_positions_also_reports_remaining_qty(self, temp_db):
        _open_nvda(temp_db, qty=350)
        persistence.record_exit("NVDA", exit_price=56.52, exit_qty=140, exit_date="2026-07-14",
                                 source="exit_command")
        rows = persistence.get_open_positions()
        nvda = next(r for r in rows if r["ticker"] == "NVDA")
        assert nvda["qty"] == 350
        assert nvda["remaining_qty"] == 210

    def test_fully_closed_position_is_not_returned_at_all(self, temp_db):
        _open_nvda(temp_db, qty=100)
        persistence.record_exit("NVDA", exit_price=210.0, exit_qty=100, exit_date="2026-07-16",
                                 source="exit_command")
        assert persistence.get_open_position("NVDA") is None
        assert persistence.get_open_positions() == []


def _open_core(ticker, entry_price, qty):
    with persistence._db() as conn:
        conn.execute(
            "INSERT INTO thesis (ticker, status, sleeve, updated_at) VALUES (?, 'open_position', 'core', ?)",
            (ticker, persistence._now()),
        )
    persistence.create_position(
        ticker=ticker, entry_date="2026-06-01", entry_price=entry_price, qty=qty,
        entry_type="full", entry_setup={"type": "Core", "stop": entry_price * 0.9, "atr_at_build": 1.0},
        initial_stop=entry_price * 0.9, current_stop=entry_price * 0.9,
    )


class TestAccountSettings:
    """Found in review (2026-07-16/17): DEFAULT_RISK_USD was never set and
    /setrisk was never wired, so real position sizing has never actually been
    risk-based in practice -- every real report so far said "cannot compute
    final quantity." account_settings replaces the .env-based approach with a
    single settings row covering risk %, portfolio heat cap, and allocation
    targets, all editable live via Telegram."""

    def test_defaults_match_the_agreed_values(self, temp_db):
        settings = persistence.get_account_settings()
        assert settings["equity_usd"] is None  # no invented default
        assert settings["risk_pct"] == pytest.approx(0.01)
        assert settings["portfolio_heat_cap_pct"] == pytest.approx(0.06)
        assert settings["sector_cap_pct"] == pytest.approx(0.40)
        assert settings["core_pct_target"] == pytest.approx(0.60)
        assert settings["spy_within_core_pct_target"] == pytest.approx(0.60)
        assert settings["qqq_within_core_pct_target"] == pytest.approx(0.40)

    def test_set_equity_updates_the_stored_value(self, temp_db):
        persistence.set_equity(150000)
        assert persistence.get_account_settings()["equity_usd"] == 150000

    def test_set_equity_rejects_non_positive(self, temp_db):
        with pytest.raises(ValueError):
            persistence.set_equity(0)
        with pytest.raises(ValueError):
            persistence.set_equity(-100)

    def test_set_risk_pct_updates_the_stored_value(self, temp_db):
        persistence.set_risk_pct(0.02)
        assert persistence.get_account_settings()["risk_pct"] == pytest.approx(0.02)

    def test_set_risk_pct_rejects_out_of_range(self, temp_db):
        with pytest.raises(ValueError):
            persistence.set_risk_pct(0)
        with pytest.raises(ValueError):
            persistence.set_risk_pct(1.0)
        with pytest.raises(ValueError):
            persistence.set_risk_pct(-0.01)


class TestPortfolioHeat:
    def test_no_equity_set_returns_none_pct_not_zero(self, temp_db):
        _open_nvda(temp_db, entry_price=208.62, current_stop=206.04, qty=75)
        heat = persistence.get_portfolio_heat()
        assert heat["heat_pct"] is None
        assert heat["heat_usd"] > 0
        assert heat["breached"] is False  # can't be breached if unknown

    def test_heat_usd_and_pct_computed_correctly(self, temp_db):
        _open_nvda(temp_db, entry_price=208.62, current_stop=206.04, qty=75)
        persistence.set_equity(100000)
        heat = persistence.get_portfolio_heat()
        expected_usd = 75 * (208.62 - 206.04)
        assert heat["heat_usd"] == pytest.approx(expected_usd)
        assert heat["heat_pct"] == pytest.approx(expected_usd / 100000)
        assert heat["cap_pct"] == pytest.approx(0.06)

    def test_breach_detected_when_heat_exceeds_cap(self, temp_db):
        _open_nvda(temp_db, entry_price=208.62, current_stop=189.80, qty=75)  # large risk/share
        persistence.set_equity(1000)  # tiny equity forces a breach
        assert persistence.get_portfolio_heat()["breached"] is True

    def test_partial_exit_reduces_heat_via_remaining_qty(self, temp_db):
        # Real XLF numbers reproduced on the NVDA fixture ticker for simplicity.
        _open_nvda(temp_db, entry_price=54.765, current_stop=53.9, initial_stop=53.9, qty=350)
        persistence.record_exit("NVDA", exit_price=56.52, exit_qty=140, exit_date="2026-07-14",
                                 source="exit_command")
        persistence.set_equity(100000)
        heat = persistence.get_portfolio_heat()
        assert heat["heat_usd"] == pytest.approx(210 * (54.765 - 53.9))

    def test_position_with_no_current_stop_contributes_zero_not_an_error(self, temp_db):
        with persistence._db() as conn:
            conn.execute(
                "INSERT INTO thesis (ticker, status, sleeve, updated_at) VALUES ('GOOGL', 'open_position', 'swing', ?)",
                (persistence._now(),),
            )
            conn.execute(
                "INSERT INTO positions (ticker, entry_date, entry_price, qty, entry_type, "
                "initial_stop, current_stop, status, updated_at) "
                "VALUES ('GOOGL', '2026-01-01', 150.0, 10, 'full', NULL, NULL, 'open', ?)",
                (persistence._now(),),
            )
        persistence.set_equity(100000)
        heat = persistence.get_portfolio_heat()
        assert heat["heat_usd"] == 0.0
        assert heat["heat_pct"] == 0.0

    def test_core_sleeve_positions_are_included_in_heat(self, temp_db):
        # Rule 8's exemption is from swing rules (target/2H-alert/tranche math),
        # not from real risk -- a wide structural stop is still real risk.
        _open_core("SPY", entry_price=700.0, qty=100)
        with persistence._db() as conn:
            conn.execute("UPDATE positions SET current_stop=650.0 WHERE ticker='SPY'")
        persistence.set_equity(1000000)
        heat = persistence.get_portfolio_heat()
        assert heat["heat_usd"] == pytest.approx(100 * (700.0 - 650.0))


class TestAllocationDrift:
    def test_no_equity_set_returns_none_everywhere(self, temp_db):
        _open_nvda(temp_db)
        drift = persistence.get_allocation_drift()
        assert drift["core_pct_actual"] is None
        assert drift["swing_pct_actual"] is None
        assert drift["spy_within_core_pct_actual"] is None
        assert drift["qqq_within_core_pct_actual"] is None
        # Targets are always available, even without equity set.
        assert drift["core_pct_target"] == pytest.approx(0.60)
        assert drift["swing_pct_target"] == pytest.approx(0.40)

    def test_core_swing_and_within_core_split_computed_correctly(self, temp_db):
        _open_core("SPY", entry_price=700.0, qty=100)   # $70,000
        _open_core("QQQ", entry_price=600.0, qty=50)     # $30,000 -- core total $100,000
        _open_nvda(temp_db, entry_price=200.0, qty=100)  # $20,000 swing
        persistence.set_equity(200000)
        drift = persistence.get_allocation_drift()
        assert drift["core_pct_actual"] == pytest.approx(100000 / 200000)
        assert drift["swing_pct_actual"] == pytest.approx(20000 / 200000)
        assert drift["spy_within_core_pct_actual"] == pytest.approx(70000 / 100000)
        assert drift["qqq_within_core_pct_actual"] == pytest.approx(30000 / 100000)

    def test_targets_reflect_account_settings_not_hardcoded(self, temp_db):
        persistence.set_equity(100000)
        drift = persistence.get_allocation_drift()
        assert drift["core_pct_target"] == pytest.approx(0.60)
        assert drift["spy_within_core_pct_target"] == pytest.approx(0.60)
        assert drift["qqq_within_core_pct_target"] == pytest.approx(0.40)

    def test_partial_exit_uses_remaining_qty_not_original(self, temp_db):
        _open_core("SPY", entry_price=700.0, qty=100)  # $70,000 original
        persistence.record_exit("SPY", exit_price=750.0, exit_qty=30, exit_date="2026-07-01",
                                 source="exit_command")  # 70 remain -> $49,000
        persistence.set_equity(100000)
        drift = persistence.get_allocation_drift()
        assert drift["core_pct_actual"] == pytest.approx(49000 / 100000)


def _open_swing(ticker, entry_price, current_stop, qty):
    with persistence._db() as conn:
        conn.execute(
            "INSERT INTO thesis (ticker, status, sleeve, updated_at) VALUES (?, 'open_position', 'swing', ?)",
            (ticker, persistence._now()),
        )
    persistence.create_position(
        ticker=ticker, entry_date="2026-07-01", entry_price=entry_price, qty=qty,
        entry_type="full", entry_setup={"type": "Breakout", "stop": current_stop, "atr_at_build": 1.0},
        initial_stop=current_stop, current_stop=current_stop,
    )


class TestSectorExposure:
    """Found in the strategy review: ~84% of the real book turned out to be
    one correlated bet (SPY+QQQ+NVDA+AMZN) with nothing anywhere surfacing
    it. get_sector_exposure() sums swing-sleeve-only $ risk by
    sector_map.py's correlation group, excluding Core entirely (rule 8
    positions aren't swing decisions being gated here)."""

    def test_no_open_swing_positions_returns_empty_dict(self, temp_db):
        assert persistence.get_sector_exposure() == {}

    def test_core_only_book_returns_empty_dict(self, temp_db):
        _open_core("SPY", entry_price=700.0, qty=100)
        assert persistence.get_sector_exposure() == {}

    def test_single_group_sums_correctly(self, temp_db):
        _open_swing("NVDA", entry_price=208.62, current_stop=206.04, qty=75)  # risk 193.5
        _open_swing("MSFT", entry_price=240.58, current_stop=233.80, qty=50)  # risk 339.0
        # 2026-08-04: the cap now gates on the ONE standard sector, so both of
        # these are "Information Technology" rather than the old hand-made
        # "mega_cap_growth_tech" grouping. AMZN was swapped for MSFT here
        # precisely because AMZN's real sector is Consumer Discretionary --
        # which is the whole reason the separate correlation warning exists.
        exposure = persistence.get_sector_exposure()
        assert set(exposure.keys()) == {"Information Technology"}
        assert exposure["Information Technology"]["risk_usd"] == pytest.approx(193.5 + 339.0)
        assert exposure["Information Technology"]["pct_of_swing_book"] == pytest.approx(1.0)

    def test_two_groups_split_correctly(self, temp_db):
        _open_swing("NVDA", entry_price=208.62, current_stop=206.04, qty=75)  # risk 193.5, tech
        # JPM, not XLF: an ETF has no SIC code and therefore no sector at all,
        # which is correct but makes it the wrong fixture for a two-sector split.
        _open_swing("JPM", entry_price=54.765, current_stop=53.9, qty=350)     # risk 302.75
        exposure = persistence.get_sector_exposure()
        total = 193.5 + 302.75
        assert exposure["Information Technology"]["pct_of_swing_book"] == pytest.approx(193.5 / total)
        assert exposure["Financials"]["pct_of_swing_book"] == pytest.approx(302.75 / total)

    def test_unmapped_ticker_falls_into_unclassified_not_dropped(self, temp_db):
        _open_swing("ZZZZ", entry_price=100.0, current_stop=95.0, qty=10)  # risk 50, unmapped
        exposure = persistence.get_sector_exposure()
        assert "unclassified" in exposure
        assert exposure["unclassified"]["risk_usd"] == pytest.approx(50.0)

    def test_position_with_no_real_risk_is_excluded_not_zero_divided(self, temp_db):
        # A stop already trailed above entry (locked-in profit) has no real
        # downside risk left -- must not contribute to or corrupt the split.
        _open_swing("NVDA", entry_price=208.62, current_stop=220.0, qty=75)  # stop above entry
        _open_swing("JPM", entry_price=54.765, current_stop=53.9, qty=350)
        exposure = persistence.get_sector_exposure()
        assert "Information Technology" not in exposure
        assert exposure["Financials"]["pct_of_swing_book"] == pytest.approx(1.0)


class TestPendingWithdrawal:
    """Found in review (2026-07-18): a real $17,500 withdrawal was decided
    but not yet settled -- the broker's own account total still showed the
    full pre-withdrawal figure. get_effective_equity() is the real number
    every risk/heat/allocation calc should use, not equity_usd alone."""

    def test_defaults_to_zero(self, temp_db):
        assert persistence.get_account_settings()["pending_withdrawal_usd"] == 0

    def test_set_pending_withdrawal_updates_the_stored_value(self, temp_db):
        persistence.set_pending_withdrawal(17500)
        assert persistence.get_account_settings()["pending_withdrawal_usd"] == 17500

    def test_set_pending_withdrawal_rejects_negative(self, temp_db):
        with pytest.raises(ValueError):
            persistence.set_pending_withdrawal(-1)

    def test_zero_is_a_valid_clearing_value(self, temp_db):
        persistence.set_pending_withdrawal(17500)
        persistence.set_pending_withdrawal(0)
        assert persistence.get_account_settings()["pending_withdrawal_usd"] == 0

    def test_effective_equity_none_when_equity_unset(self, temp_db):
        assert persistence.get_effective_equity() is None

    def test_effective_equity_subtracts_pending_withdrawal(self, temp_db):
        persistence.set_equity(100000)
        persistence.set_pending_withdrawal(17500)
        assert persistence.get_effective_equity() == pytest.approx(82500)

    def test_effective_equity_equals_raw_equity_when_nothing_pending(self, temp_db):
        persistence.set_equity(100000)
        assert persistence.get_effective_equity() == pytest.approx(100000)

    def test_portfolio_heat_uses_effective_equity_not_raw(self, temp_db):
        _open_nvda(temp_db, entry_price=208.62, current_stop=206.04, qty=75)  # risk 193.5
        persistence.set_equity(100000)
        persistence.set_pending_withdrawal(17500)
        heat = persistence.get_portfolio_heat()
        assert heat["heat_pct"] == pytest.approx(193.5 / 82500)

    def test_allocation_drift_uses_effective_equity_not_raw(self, temp_db):
        _open_core("SPY", entry_price=700.0, qty=100)  # $70,000
        persistence.set_equity(100000)
        persistence.set_pending_withdrawal(17500)
        drift = persistence.get_allocation_drift()
        assert drift["core_pct_actual"] == pytest.approx(70000 / 82500)


class TestCashAvailable:
    """Found real, 2026-07-19: closing a modest risk-dollar gap on a
    tight-stop position required ~17-20x that gap in actual cash -- more
    than the whole account's remaining cash -- with nothing surfacing that a
    single trade could quietly consume most of the money on hand even while
    portfolio heat stayed well under its own cap. get_cash_available() is
    the real dollar figure that bounds a NEW trade's cost, distinct from
    portfolio heat (which bounds risk, not dollars)."""

    def test_none_when_equity_unset(self, temp_db):
        assert persistence.get_cash_available() is None

    def test_no_positions_returns_full_equity(self, temp_db):
        persistence.set_equity(100000)
        assert persistence.get_cash_available() == pytest.approx(100000)

    def test_subtracts_position_value_at_entry_price(self, temp_db):
        _open_nvda(temp_db, entry_price=208.62, qty=75)  # $15,646.50
        persistence.set_equity(100000)
        assert persistence.get_cash_available() == pytest.approx(100000 - 208.62 * 75)

    def test_core_and_swing_both_count_against_cash(self, temp_db):
        _open_core("SPY", entry_price=700.0, qty=100)  # $70,000
        _open_nvda(temp_db, entry_price=208.62, qty=75)  # $15,646.50
        persistence.set_equity(200000)
        expected = 200000 - 70000 - (208.62 * 75)
        assert persistence.get_cash_available() == pytest.approx(expected)

    def test_partial_exit_reduces_invested_value_via_remaining_qty(self, temp_db):
        _open_nvda(temp_db, entry_price=54.765, initial_stop=53.9, current_stop=53.9, qty=350)
        persistence.record_exit("NVDA", exit_price=56.52, exit_qty=140, exit_date="2026-07-14",
                                 source="exit_command")
        persistence.set_equity(100000)
        # 210 remaining, not 350 -- same remaining_qty fix as everywhere else.
        assert persistence.get_cash_available() == pytest.approx(100000 - 54.765 * 210)

    def test_pending_withdrawal_reduces_cash_available_too(self, temp_db):
        persistence.set_equity(100000)
        persistence.set_pending_withdrawal(17500)
        assert persistence.get_cash_available() == pytest.approx(82500)


class TestXPosts:
    """bot/fetch_x_feed.py's storage layer (2026-07-22): dedup by post_id,
    ticker-cashtag lookup, and the fixed macro-account lookup.

    Found in the 2026-07-30 full-system checkup: the default `posted_at` used
    to be a hardcoded absolute timestamp ("2026-07-22T10:00:00+00:00"). Every
    "fresh" test here checks it against a 24h/6h window measured from the REAL
    clock -- once enough real time passed, that fixed date fell outside the
    window and these tests started failing even though the underlying code
    was never actually broken. Defaulting to "right now" makes these tests
    describe "a post from just now," which is what they actually mean, and
    keeps that true forever, not just on the day this was written."""

    def _record(self, post_id="1", account="charliebilello", posted_at=None,
                text="text", url="https://x.com/x/status/1", tickers=None):
        persistence.record_x_post(post_id=post_id, account=account,
                                   posted_at=posted_at or _utcnow_iso(),
                                   text=text, url=url, tickers=tickers or [])

    def test_record_and_dedupe_by_post_id(self, temp_db):
        self._record(post_id="123", tickers=["NVDA"])
        self._record(post_id="123", tickers=["NVDA"])  # same tweet seen again -- must not duplicate
        with persistence._db() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM x_posts").fetchone()["c"]
        assert count == 1

    def test_get_recent_posts_for_ticker_matches_cashtag(self, temp_db):
        self._record(post_id="1", tickers=["NVDA", "SPY"])
        self._record(post_id="2", tickers=["TSLA"])
        result = persistence.get_recent_posts_for_ticker("NVDA", hours=24)
        assert len(result) == 1
        assert result[0]["tickers"] == ["NVDA", "SPY"]

    def test_get_recent_posts_for_ticker_is_case_insensitive(self, temp_db):
        self._record(post_id="1", tickers=["NVDA"])
        assert len(persistence.get_recent_posts_for_ticker("nvda", hours=24)) == 1

    def test_get_recent_posts_for_ticker_excludes_stale_posts(self, temp_db):
        self._record(post_id="1", posted_at="2020-01-01T00:00:00+00:00", tickers=["NVDA"])
        assert persistence.get_recent_posts_for_ticker("NVDA", hours=24) == []

    def test_get_recent_posts_for_ticker_no_false_positive_substring_match(self, temp_db):
        # A post tagged just $NVDA must not match a lookup for a ticker that happens
        # to share a substring -- exact list-membership check, never a LIKE-style scan.
        self._record(post_id="1", tickers=["NVDA"])
        assert persistence.get_recent_posts_for_ticker("VDA", hours=24) == []

    def test_get_recent_macro_posts_filters_to_macro_accounts_only(self, temp_db):
        self._record(post_id="1", account="elonmusk", tickers=[])
        self._record(post_id="2", account="some_random_account", tickers=[])
        result = persistence.get_recent_macro_posts(hours=6)
        assert len(result) == 1
        assert result[0]["account"] == "elonmusk"

    def test_get_recent_macro_posts_excludes_stale_posts(self, temp_db):
        self._record(post_id="1", account="jimcramer", posted_at="2020-01-01T00:00:00+00:00")
        assert persistence.get_recent_macro_posts(hours=6) == []


class TestXCandidateAlerts:
    """fetch_x_feed.py's idea-sourcing step (2026-07-22): surface a ticker's
    name once, ever, the first time it's mentioned and isn't already tracked.
    See TestXPosts's own docstring for why `posted_at` defaults to "now"
    rather than a hardcoded date."""

    def _record(self, post_id="1", account="charliebilello", posted_at=None,
                text="text", url="https://x.com/x/status/1", tickers=None):
        persistence.record_x_post(post_id=post_id, account=account,
                                   posted_at=posted_at or _utcnow_iso(),
                                   text=text, url=url, tickers=tickers or [])

    def test_get_known_tickers_returns_every_thesis_ticker_regardless_of_status(self, temp_db):
        persistence.save_thesis(ticker="NVDA", status="pending", source="screener",
                                 primary_setup={"type": "Reclaim", "trigger": 100.0, "stop": 95.0,
                                                "atr_at_build": 2.0, "targets": []})
        persistence.set_sleeve("SPY", "core")  # sets sleeve via a plain thesis row, no primary_setup
        assert persistence.get_known_tickers() == {"NVDA", "SPY"}

    def test_new_cashtag_not_in_thesis_is_a_candidate(self, temp_db):
        self._record(post_id="1", tickers=["GEV"])
        result = persistence.get_new_candidate_tickers(hours=24)
        assert len(result) == 1
        assert result[0]["ticker"] == "GEV"
        assert result[0]["account"] == "charliebilello"

    def test_already_tracked_ticker_is_not_a_candidate(self, temp_db):
        persistence.set_sleeve("NVDA", "swing")
        self._record(post_id="1", tickers=["NVDA"])
        assert persistence.get_new_candidate_tickers(hours=24) == []

    def test_already_alerted_ticker_is_not_a_candidate_again(self, temp_db):
        posted_at = _utcnow_iso()
        self._record(post_id="1", tickers=["GEV"], posted_at=posted_at)
        persistence.record_candidate_alerted(ticker="GEV", account="charliebilello",
                                              posted_at=posted_at,
                                              url="https://x.com/x/status/1", text="text")
        assert persistence.get_new_candidate_tickers(hours=24) == []

    def test_same_ticker_mentioned_twice_only_yields_one_candidate_row(self, temp_db):
        self._record(post_id="1", tickers=["GEV"])
        self._record(post_id="2", tickers=["GEV"])
        result = persistence.get_new_candidate_tickers(hours=24)
        assert len(result) == 1

    def test_stale_mention_outside_the_window_is_not_a_candidate(self, temp_db):
        self._record(post_id="1", posted_at="2020-01-01T00:00:00+00:00", tickers=["GEV"])
        assert persistence.get_new_candidate_tickers(hours=24) == []

    def test_record_candidate_alerted_is_idempotent(self, temp_db):
        persistence.record_candidate_alerted(ticker="GEV", account="a", posted_at="2026-07-22T10:00:00+00:00",
                                              url="https://x.com/x/status/1", text="t")
        persistence.record_candidate_alerted(ticker="GEV", account="a", posted_at="2026-07-22T10:00:00+00:00",
                                              url="https://x.com/x/status/1", text="t")
        with persistence._db() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM x_candidate_alerts").fetchone()["c"]
        assert count == 1


def _close_nvda_fully(qty=75, exit_price=220.0, initial_stop=189.80):
    """Opens and fully closes an NVDA position so _generate_closing_summary()
    fires, giving tests a real closing_summaries row (thesis_validated NULL)
    to reflect on."""
    _open_nvda(None, current_stop=initial_stop, initial_stop=initial_stop, qty=qty)
    persistence.record_exit("NVDA", exit_price, qty, "2026-07-20", source="exit_command")


class TestReflection:
    """/reflect (2026-07-25): closing_summaries.thesis_validated existed since
    the original schema but nothing ever wrote it (MASTER_SYSTEM_SPEC section 9
    flagged this as an open item). These lock in the read/write pair plus the
    same-ticker/cross-ticker lesson lookups used to inject past context into a
    new /screener or /monitor run -- mirrors TradingAgents' reflection log."""

    def test_freshly_closed_position_is_unreflected(self, temp_db):
        _close_nvda_fully()
        pending = persistence.get_unreflected_closes()
        assert len(pending) == 1
        assert pending[0]["ticker"] == "NVDA"
        assert pending[0]["thesis_validated"] is None

    def test_record_reflection_clears_it_from_pending(self, temp_db):
        _close_nvda_fully()
        closing_id = persistence.get_unreflected_closes()[0]["id"]
        persistence.record_reflection(closing_id, thesis_validated=True, lesson="Reclaim held, alpha was real.")
        assert persistence.get_unreflected_closes() == []

    def test_record_reflection_on_unknown_id_raises(self, temp_db):
        with pytest.raises(ValueError, match="no closing_summaries row"):
            persistence.record_reflection(9999, thesis_validated=True, lesson="x")

    def test_ticker_lessons_only_returns_reflected_rows(self, temp_db):
        _close_nvda_fully()
        assert persistence.get_ticker_lessons("NVDA") == []
        closing_id = persistence.get_unreflected_closes()[0]["id"]
        persistence.record_reflection(closing_id, thesis_validated=False, lesson="Stopped out on noise, buffer was too tight.")
        lessons = persistence.get_ticker_lessons("NVDA")
        assert len(lessons) == 1
        assert lessons[0]["lesson"] == "Stopped out on noise, buffer was too tight."

    def test_cross_lessons_excludes_the_ticker_itself(self, temp_db):
        _close_nvda_fully()
        closing_id = persistence.get_unreflected_closes()[0]["id"]
        persistence.record_reflection(closing_id, thesis_validated=True, lesson="Worked as planned.")
        assert persistence.get_recent_cross_lessons(exclude_ticker="NVDA") == []
        assert len(persistence.get_recent_cross_lessons(exclude_ticker="AAPL")) == 1

    def test_cross_lessons_filters_by_setup_type(self, temp_db):
        _close_nvda_fully()  # entry_setup type="Reclaim", see _open_nvda's default
        closing_id = persistence.get_unreflected_closes()[0]["id"]
        persistence.record_reflection(closing_id, thesis_validated=True, lesson="Reclaim worked.")
        assert len(persistence.get_recent_cross_lessons(exclude_ticker="AAPL", setup_type="Reclaim")) == 1
        assert persistence.get_recent_cross_lessons(exclude_ticker="AAPL", setup_type="Breakout") == []


class TestOverrideTagging:
    """Automatic override tagging (2026-08-02) -- a fill taken against the
    system's own non-buy verdict gets marked at write time, with nothing extra
    for the user to type. Found real: GOOGL was bought 2026-07-28 on a thesis
    whose own decision was 'No Trade' (grade F) and ASTS on 2026-07-31 against
    'Watchlist' (grade D); both then sat in the journal indistinguishable from
    system-approved trades, making 'are the user's overrides good?' unanswerable.
    Disclosure only -- never blocks or resizes a fill."""

    def _save_thesis(self, decision, grade):
        persistence.save_thesis(
            ticker="ABC", status="pending", source="screener",
            primary_setup={"type": "Reclaim", "trigger": 100.0, "stop": 95.0,
                           "atr_at_build": 2.0, "targets": []},
            decision=decision, rubric_grade=grade,
        )

    def _fill(self):
        return persistence.create_position(
            "ABC", entry_date="2026-08-02", entry_price=101.0, qty=10,
            entry_type="full", entry_setup={"type": "Reclaim", "stop": 95.0},
            initial_stop=95.0,
        )

    def _stored(self, position_id):
        with persistence._db() as conn:
            return dict(conn.execute(
                "SELECT override_of_decision, override_of_grade FROM positions WHERE id=?",
                (position_id,),
            ).fetchone())

    def test_no_trade_decision_is_tagged(self, temp_db):
        self._save_thesis("No Trade", "F")
        stored = self._stored(self._fill())
        assert stored["override_of_decision"] == "No Trade"
        assert stored["override_of_grade"] == "F"

    def test_watchlist_decision_is_tagged(self, temp_db):
        self._save_thesis("Watchlist", "D")
        stored = self._stored(self._fill())
        assert stored["override_of_decision"] == "Watchlist"
        assert stored["override_of_grade"] == "D"

    def test_buy_decision_is_not_tagged(self, temp_db):
        self._save_thesis("Buy Now", "B")
        stored = self._stored(self._fill())
        assert stored["override_of_decision"] is None
        assert stored["override_of_grade"] is None

    def test_weak_grade_alone_is_tagged_even_on_a_buy_decision(self, temp_db):
        # A "Buy Only If Confirmed" that has since degraded to D is still a fill
        # the system would not offer today -- the grade half of the check exists
        # precisely so a stale-but-nominally-buy decision doesn't slip through.
        self._save_thesis("Buy Only If Confirmed", "D")
        stored = self._stored(self._fill())
        assert stored["override_of_decision"] is None
        assert stored["override_of_grade"] == "D"

    def test_missing_decision_and_grade_is_not_tagged(self, temp_db):
        # A legacy/backfilled thesis with neither field on file is unknowable,
        # not an override -- never guess one, same posture as any other
        # missing-input case in this system.
        self._save_thesis(None, None)
        stored = self._stored(self._fill())
        assert stored["override_of_decision"] is None
        assert stored["override_of_grade"] is None

    def test_classify_override_is_case_and_space_insensitive(self, temp_db):
        assert persistence.classify_override("  no trade  ", None) is not None
        assert persistence.classify_override(None, "d") is not None
        assert persistence.classify_override("BUY NOW", "a") is None

    # --- a fired trigger is not an override (2026-08-09) -------------------
    #
    # The stored decision is written once, at build time, and nothing rewrites
    # it -- so an idea whose trigger has since confirmed still carries the word
    # it was born with. By decision_policy's own definitions that idea IS "Buy
    # Now" once the trigger confirms on a settled daily close. All four open
    # positions taken from a screened idea were tagged overrides of "Watchlist"
    # on exactly this mistake; the owner's own account was "i bought the stocks
    # after the trigger was on and not before". The column was measuring
    # obedience and reporting rebellion.

    def _log_green(self):
        persistence.log_monitor_check("ABC", status="green", price=101.0)

    def test_watchlist_is_not_an_override_once_the_trigger_confirmed(self, temp_db):
        self._save_thesis("Watchlist", "B")
        self._log_green()
        stored = self._stored(self._fill())
        assert stored["override_of_decision"] is None

    def test_no_trade_is_not_an_override_once_the_trigger_confirmed(self, temp_db):
        self._save_thesis("No Trade", "B")
        self._log_green()
        stored = self._stored(self._fill())
        assert stored["override_of_decision"] is None

    def test_a_weak_grade_survives_the_trigger_confirming(self, temp_db):
        # Price reaching the entry says nothing about whether the idea was any
        # good, which is the whole thing the grade claims to know. Only the
        # decision half goes stale.
        self._save_thesis("Watchlist", "D")
        self._log_green()
        stored = self._stored(self._fill())
        assert stored["override_of_decision"] is None
        assert stored["override_of_grade"] == "D"

    def test_without_a_green_the_tag_still_applies(self, temp_db):
        # The guard must not swallow the real case it was built to catch.
        self._save_thesis("Watchlist", "B")
        stored = self._stored(self._fill())
        assert stored["override_of_decision"] == "Watchlist"


class TestSetupTypeAndDecisionAreLocked:
    """2026-08-09. `setup_type` is the field the shadow book groups by and
    `decision` is the field it splits on, and neither was constrained. Live
    result: four rows held a paragraph where a label belongs, and the same
    decision arrived as both "Buy" (8 rows) and "Buy Now" (6), so the strongest
    call this system makes was split across two labels and each half looked too
    small to read."""

    def _save(self, setup_type, decision="Watchlist"):
        persistence.save_thesis(
            ticker="ABC", status="pending", source="screener",
            primary_setup={"type": setup_type, "trigger": 100.0, "stop": 95.0,
                           "atr_at_build": 2.0, "targets": []},
            decision=decision, rubric_grade="B",
        )
        return persistence.get_thesis("ABC")

    def test_a_near_miss_setup_type_is_normalised(self, temp_db):
        assert self._save("breakout")["primary_setup"]["type"] == "Breakout"
        assert self._save("Breakout/Continuation")["primary_setup"]["type"] == "Breakout"

    def test_prose_in_the_setup_type_is_refused(self, temp_db):
        with pytest.raises(ValueError, match="prose"):
            self._save(
                "V-reversal / capitulation reclaim (swing, backfilled 2026-07-15 from "
                "historical data as of 2026-06-29). Sharp decline from 278.56."
            )

    def test_the_alternate_setup_is_locked_too(self, temp_db):
        with pytest.raises(ValueError, match="alternate_setup"):
            persistence.save_thesis(
                ticker="ABC", status="pending", source="screener",
                primary_setup={"type": "Breakout", "trigger": 100.0, "stop": 95.0,
                               "atr_at_build": 2.0, "targets": []},
                alternate_setup={"type": "Momentum Squeeze", "trigger": 90.0},
            )

    def test_buy_and_buy_now_stop_being_two_groups(self, temp_db):
        assert self._save("Breakout", decision="Buy")["decision"] == "Buy Now"
        assert self._save("Breakout", decision="buy now")["decision"] == "Buy Now"
        assert self._save("Breakout", decision="No Trade")["decision"] == "No Trade"

    def test_an_unknown_decision_is_stored_as_given_not_refused(self, temp_db):
        # Deliberately lenient where setup type is strict: an unrecognised word
        # can still be read and mapped afterwards, so refusing a completed
        # 20-minute analysis over it would cost more than it saves.
        assert self._save("Breakout", decision="Maybe Later")["decision"] == "Maybe Later"

    def test_the_flattened_idea_row_gets_the_normalised_type(self, temp_db):
        # thesis.primary_setup and the ideas row are written from the SAME dict;
        # a fix applied to only one is a new way for the field to disagree with
        # itself.
        self._save("breakout")
        assert persistence.get_live_idea("ABC")["setup_type"] == "Breakout"


class TestTriggerFiredAge:
    """2026-08-02: /monitorall used to re-report a long-fired trigger as freshly
    actionable on every run, because a thesis stays 'pending' until a real
    /filled. This is the arithmetic half of the fix -- how many real trading
    days ago the trigger first confirmed. The block itself lives in
    MONITOR_v2.md; nothing here judges."""

    def _save(self, ticker="ABC"):
        persistence.save_thesis(
            ticker=ticker, status="pending", source="screener",
            primary_setup={"type": "Breakout", "trigger": 100.0, "stop": 95.0,
                           "atr_at_build": 2.0, "targets": []},
        )

    def test_no_green_yet_returns_none(self, temp_db):
        self._save()
        persistence.log_monitor_check("ABC", status="yellow", price=99.0)
        assert persistence.get_trigger_fired_age("ABC") is None

    def test_unknown_ticker_returns_none(self, temp_db):
        assert persistence.get_trigger_fired_age("NOPE") is None

    def test_green_today_is_not_stale(self, temp_db):
        self._save()
        persistence.log_monitor_check("ABC", status="green", price=101.0)
        age = persistence.get_trigger_fired_age("ABC")
        assert age["stale"] is False
        assert age["first_green_date"] == datetime.now(timezone.utc).date().isoformat()

    def test_uses_the_first_green_not_the_latest(self, temp_db):
        # The whole point: repeated greens on later runs must not keep resetting
        # the clock, or a trigger could never go stale at all. This is the real
        # MSFT shape -- one confirmation, then a green re-logged every night.
        self._save()
        built = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        old_green = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        with persistence._db() as conn:
            conn.execute("UPDATE thesis SET date_built=? WHERE ticker='ABC'", (built,))
            conn.execute(
                "INSERT INTO monitor_log (ticker, checked_at, status, price) VALUES (?, ?, 'green', ?)",
                ("ABC", old_green, 101.0),
            )
        persistence.log_monitor_check("ABC", status="green", price=115.0)
        age = persistence.get_trigger_fired_age("ABC")
        assert age["first_green_date"] == old_green[:10]
        assert age["stale"] is True

    def test_greens_logged_before_the_thesis_was_built_are_ignored(self, temp_db):
        # A ticker screened again after an old idea was dropped starts a fresh
        # clock -- the previous idea's greens belong to a trigger that no longer
        # exists, and counting them would make a brand-new thesis born stale.
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with persistence._db() as conn:
            conn.execute(
                "INSERT INTO monitor_log (ticker, checked_at, status, price) VALUES (?, ?, 'green', ?)",
                ("ABC", old, 80.0),
            )
        self._save()  # date_built = now, after that stale green
        assert persistence.get_trigger_fired_age("ABC") is None


class TestQueuedScreenerTickers:
    """2026-08-02: refresh_pending.py enqueues a real ~25min screener run per
    stale thesis. Without this check, a long batch still draining when the next
    nightly run fires would queue the same ticker twice -- two runs, two
    contradictory reports for one idea."""

    def _enqueue(self, update_id, text, status=None):
        persistence.enqueue_message(
            update_id=update_id, from_id="t", chat_id="t",
            message_type="text", message_text=text, raw_update={},
        )
        if status:
            with persistence._db() as conn:
                conn.execute("UPDATE messages SET status=? WHERE update_id=?", (status, update_id))

    def test_empty_queue_returns_empty_set(self, temp_db):
        assert persistence.tickers_already_queued_for_screener() == set()

    def test_received_and_processing_both_count(self, temp_db):
        self._enqueue(-1, "/screener AAPL")
        self._enqueue(-2, "/screener MSFT", status="processing")
        assert persistence.tickers_already_queued_for_screener() == {"AAPL", "MSFT"}

    def test_finished_messages_do_not_count(self, temp_db):
        # A ticker analyzed and delivered an hour ago is exactly the case that
        # SHOULD be eligible for a fresh rebuild.
        self._enqueue(-3, "/screener AAPL", status="sent")
        assert persistence.tickers_already_queued_for_screener() == set()

    def test_other_commands_are_ignored(self, temp_db):
        self._enqueue(-4, "/monitorall")
        self._enqueue(-5, "/positions")
        assert persistence.tickers_already_queued_for_screener() == set()

    def test_ticker_case_is_normalized(self, temp_db):
        self._enqueue(-6, "/screener aapl")
        assert persistence.tickers_already_queued_for_screener() == {"AAPL"}


class TestColdList:
    """Rule 29's cold list (2026-08-03). A 'No Trade' idea leaves the active
    waiting list without being forgotten: "nowhere to sell" is usually a
    statement about where price is standing today, not about the company, so it
    is re-screened every few trading days. Rule 7 already documents two real
    cases (ANET, MU) where the identical chart produced a qualifying target
    purely from a different entry price."""

    def _save(self, ticker="ABC", decision="No Trade"):
        persistence.save_thesis(
            ticker=ticker, status="pending", source="screener",
            primary_setup={"type": "Breakout", "trigger": 100.0, "stop": 95.0,
                           "atr_at_build": 2.0, "targets": []},
            decision=decision,
        )

    def _age_thesis(self, ticker, days):
        """Ages both dates. The re-check clock runs off `cold_since` (the day
        the row was shelved) and falls back to `date_built` for rows written
        before that column existed, so a test that means "this has been on the
        shelf a while" has to move both."""
        old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with persistence._db() as conn:
            conn.execute("UPDATE thesis SET date_built=?, cold_since=? WHERE ticker=?",
                          (old, old, ticker))

    def test_no_trade_goes_cold_automatically(self, temp_db):
        self._save(decision="No Trade")
        assert persistence.get_thesis("ABC")["status"] == "cold"

    def test_a_cold_idea_leaves_the_active_waiting_list(self, temp_db):
        self._save(decision="No Trade")
        assert persistence.get_pending_report_rows() == []

    def test_a_real_decision_stays_pending(self, temp_db):
        self._save(decision="Watchlist")
        assert persistence.get_thesis("ABC")["status"] == "pending"

    def test_a_fresh_cold_idea_is_not_due_yet(self, temp_db):
        self._save(decision="No Trade")
        assert persistence.get_cold_recheck_candidates() == []

    def test_an_old_cold_idea_is_due(self, temp_db):
        self._save(decision="No Trade")
        self._age_thesis("ABC", 10)
        due = persistence.get_cold_recheck_candidates()
        assert [r["ticker"] for r in due] == ["ABC"]

    def test_retries_are_capped(self, temp_db):
        self._save(decision="No Trade")
        self._age_thesis("ABC", 10)
        for _ in range(persistence.COLD_MAX_RECHECKS):
            persistence.bump_cold_recheck("ABC")
        assert persistence.get_cold_recheck_candidates() == []
        assert [r["ticker"] for r in persistence.get_exhausted_cold()] == ["ABC"]

    def test_the_recheck_clock_starts_when_the_row_is_shelved(self, temp_db):
        # An idea built months ago and only shelved today must NOT be due
        # tonight. Before `cold_since` the clock ran off `date_built`, which
        # made every late-shelved thesis instantly due -- exactly the burnt
        # hour on a broken chart the shelf exists to avoid.
        self._save(decision="Watchlist")
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        with persistence._db() as conn:
            conn.execute("UPDATE thesis SET date_built=? WHERE ticker=?", (old, "ABC"))
        persistence.set_cold("ABC")
        assert persistence.get_cold_recheck_candidates() == []

    def test_a_row_shelved_before_cold_since_existed_still_works(self, temp_db):
        # Old rows have cold_since NULL and must fall back to date_built,
        # behaving exactly as they did before the column was added.
        self._save(decision="No Trade")
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        with persistence._db() as conn:
            conn.execute("UPDATE thesis SET date_built=?, cold_since=NULL WHERE ticker=?",
                          (old, "ABC"))
        assert [r["ticker"] for r in persistence.get_cold_recheck_candidates()] == ["ABC"]

    def test_an_exhausted_idea_is_never_deleted(self, temp_db):
        # It is reported with a /drop line for the user to send -- the same
        # posture refresh_pending.py takes with dead ideas. Never silent.
        self._save(decision="No Trade")
        for _ in range(persistence.COLD_MAX_RECHECKS):
            persistence.bump_cold_recheck("ABC")
        assert persistence.get_thesis("ABC") is not None

    def test_coming_back_to_life_clears_the_retry_count(self, temp_db):
        self._save(decision="No Trade")
        persistence.bump_cold_recheck("ABC")
        self._save(decision="Buy Only If Confirmed")   # a re-screen found a target
        row = persistence.get_thesis("ABC")
        assert row["status"] == "pending"
        assert row["cold_rechecks"] == 0


class TestThesisHistory:
    """2026-08-03. save_thesis is an upsert keyed on ticker, so every rebuild
    overwrote the previous trigger/stop/grade with no copy kept anywhere -- the
    old plan was simply gone. The user's own words: a nightly rewrite he cannot
    see, cannot judge, and cannot undo. These lock in that nothing is destroyed
    and that a rewrite is reportable afterwards."""

    def _save(self, ticker="ABC", trigger=100.0, stop=95.0, grade="B",
              decision="Buy Only If Confirmed", status="pending"):
        persistence.save_thesis(
            ticker=ticker, status=status, source="screener",
            primary_setup={"type": "Breakout", "trigger": trigger, "stop": stop,
                           "atr_at_build": 2.0, "targets": []},
            rubric_grade=grade, decision=decision,
        )

    def _history(self, ticker="ABC"):
        with persistence._db() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM thesis_history WHERE ticker=? ORDER BY id", (ticker,))]

    def test_a_first_save_archives_nothing(self, temp_db):
        # There is no prior plan to preserve -- archiving an empty one would
        # just be noise in the before/after message.
        self._save()
        assert self._history() == []

    def test_an_overwrite_archives_the_previous_version(self, temp_db):
        self._save(trigger=100.0, stop=95.0, grade="B")
        self._save(trigger=112.0, stop=104.0, grade="C")
        history = self._history()
        assert len(history) == 1
        assert history[0]["prior_trigger"] == 100.0
        assert history[0]["prior_stop"] == 95.0
        assert history[0]["prior_grade"] == "B"
        # and the live row is the NEW one -- archiving must not block the write
        assert persistence.get_thesis("ABC")["rubric_grade"] == "C"

    def test_the_full_prior_row_is_recoverable(self, temp_db):
        # The point of the snapshot: putting a bad rewrite back by hand needs
        # the whole row, not only the four fields the message prints.
        self._save(trigger=100.0, grade="A")
        self._save(trigger=112.0, grade="D")
        import json
        snapshot = json.loads(self._history()[0]["snapshot"])
        assert snapshot["rubric_grade"] == "A"
        assert json.loads(snapshot["primary_setup"])["trigger"] == 100.0

    def test_every_overwrite_is_kept_not_just_the_last(self, temp_db):
        self._save(grade="A")
        self._save(grade="B")
        self._save(grade="C")
        assert [h["prior_grade"] for h in self._history()] == ["A", "B"]

    def test_a_prose_trigger_archives_as_null_never_a_guess(self, temp_db):
        # 26 of 55 real rows stored a sentence where the price belongs. The
        # archive must not invent a number out of it.
        persistence.save_thesis(
            ticker="ABC", status="pending", source="screener",
            primary_setup={"type": "Breakout", "trigger": "סגירה מעל האזור",
                           "atr_at_build": 2.0, "targets": []},
            decision="Watchlist",
        )
        self._save(trigger=112.0)
        assert self._history()[0]["prior_trigger"] is None


class TestChangesSince:
    """Backs refresh_pending.py's nightly before/after message."""

    def _save(self, ticker="ABC", trigger=100.0, grade="B", decision="Watchlist"):
        persistence.save_thesis(
            ticker=ticker, status="pending", source="screener",
            primary_setup={"type": "Breakout", "trigger": trigger, "stop": 95.0,
                           "atr_at_build": 2.0, "targets": []},
            rubric_grade=grade, decision=decision,
        )

    def test_reports_before_and_after(self, temp_db):
        self._save(trigger=100.0, grade="B")
        since = _utcnow_iso()
        self._save(trigger=112.0, grade="C")
        changes = persistence.get_thesis_changes_since(since)
        assert len(changes) == 1
        assert changes[0]["before"] == {"decision": "Watchlist", "grade": "B",
                                         "trigger": 100.0, "stop": 95.0}
        assert changes[0]["after"]["grade"] == "C"
        assert changes[0]["after"]["trigger"] == 112.0

    def test_a_rewrite_before_the_window_is_not_reported(self, temp_db):
        self._save(grade="A")
        self._save(grade="B")          # archived BEFORE the window opens
        since = _utcnow_iso()
        assert persistence.get_thesis_changes_since(since) == []

    def test_two_rewrites_in_one_night_report_as_one_net_change(self, temp_db):
        self._save(grade="A")
        since = _utcnow_iso()
        self._save(grade="B")
        self._save(grade="C")
        changes = persistence.get_thesis_changes_since(since)
        assert len(changes) == 1
        # newest archive wins: the state right before the LAST overwrite
        assert changes[0]["before"]["grade"] == "B"
        assert changes[0]["after"]["grade"] == "C"

    def test_can_be_filtered_to_named_tickers(self, temp_db):
        self._save("ABC")
        self._save("XYZ")
        since = _utcnow_iso()
        self._save("ABC", grade="D")
        self._save("XYZ", grade="D")
        assert [c["ticker"] for c in
                persistence.get_thesis_changes_since(since, ["ABC"])] == ["ABC"]


class TestRebuildNeverRemovesFromTheList:
    """2026-08-03, the user's Change 2. A rebuild of an idea he is actively
    watching must never move it off the waiting list by itself."""

    def _save(self, ticker="ABC", decision="Buy Only If Confirmed"):
        persistence.save_thesis(
            ticker=ticker, status="pending", source="screener",
            primary_setup={"type": "Breakout", "trigger": 100.0, "stop": 95.0,
                           "atr_at_build": 2.0, "targets": []},
            decision=decision,
        )

    def test_a_rebuild_that_turns_no_trade_stays_on_the_list(self, temp_db):
        self._save(decision="Buy Only If Confirmed")   # a live waiting idea
        self._save(decision="No Trade")                # tonight's rebuild
        assert persistence.get_thesis("ABC")["status"] == "pending"

    def test_a_brand_new_no_trade_still_goes_cold(self, temp_db):
        # Rule 29 unchanged for genuinely new ideas -- it was never the problem.
        self._save(decision="No Trade")
        assert persistence.get_thesis("ABC")["status"] == "cold"

    def test_a_cold_idea_that_is_still_no_trade_stays_cold(self, temp_db):
        # Must not be "protected" back onto the active list: it was never on it.
        self._save(decision="No Trade")
        assert persistence.get_thesis("ABC")["status"] == "cold"
        self._save(decision="No Trade")               # automatic cold re-check
        assert persistence.get_thesis("ABC")["status"] == "cold"

    def test_a_cold_idea_that_finds_a_target_comes_back(self, temp_db):
        self._save(decision="No Trade")
        self._save(decision="Buy Only If Confirmed")
        assert persistence.get_thesis("ABC")["status"] == "pending"


class TestDecisionTransitions:
    """2026-08-08, the user's request: "document that a stock was on watchlist /
    buy if confirmed, and now the trigger is on".

    thesis.decision is one mutable column that the next /screener rebuild
    overwrites in place, so "PLTR was a Watchlist when it fired" is knowable
    exactly once, as it happens. The backfill over the real DB found 24 real
    confirmations: 14 called Watchlist, 6 Buy Only If Confirmed, 4 No Trade, and
    not one called Buy Now.
    """

    def _thesis(self, decision="Watchlist", grade="B"):
        persistence.save_thesis(
            ticker="ABC", status="pending", source="test",
            primary_setup={"type": "Breakout", "trigger": 100.0, "stop": 95.0,
                            "atr_at_build": 2.0, "targets": [{"price": 110.0}]},
            rubric_grade=grade, decision=decision,
        )

    def _green(self, at=None):
        persistence.log_monitor_check("ABC", "green", price=102.0)
        if at:
            with persistence._db() as conn:
                conn.execute("UPDATE monitor_log SET checked_at=? WHERE ticker='ABC'", (at,))

    def test_the_word_it_carried_when_it_fired_is_kept(self, temp_db):
        self._thesis(decision="Watchlist")
        self._green()
        assert persistence.record_decision_transition("ABC", status="green") is True
        row = persistence.get_decision_transitions("ABC")[0]
        assert row["decision_stored"] == "Watchlist"
        assert row["rubric_grade_stored"] == "B"

    def test_a_later_rebuild_cannot_rewrite_it(self, temp_db):
        # The whole point. Without the record, the rebuild below erases the only
        # evidence that this idea was a "don't buy yet" when it confirmed.
        self._thesis(decision="Watchlist")
        self._green()
        persistence.record_decision_transition("ABC", status="green")
        self._thesis(decision="Buy Only If Confirmed", grade="A")
        assert persistence.get_decision_transitions("ABC")[0]["decision_stored"] == "Watchlist"

    def test_the_same_green_is_only_recorded_once(self, temp_db):
        # /monitor and the nightly /monitorall both keep seeing one real event
        # for days on end. The FIRST sighting is the fact worth keeping.
        self._thesis()
        self._green()
        assert persistence.record_decision_transition("ABC", status="green") is True
        assert persistence.record_decision_transition("ABC", status="green") is False
        assert len(persistence.get_decision_transitions("ABC")) == 1

    def test_it_records_when_it_fired_not_when_it_was_written_down(self, temp_db):
        # Days apart in the real system: the same green is re-reported on every
        # run until the idea is filled or dropped, so a Monday scan must not
        # stamp itself onto a Friday confirmation.
        self._thesis()
        with persistence._db() as conn:
            conn.execute("UPDATE thesis SET date_built='2026-08-01T00:00:00+00:00' WHERE ticker='ABC'")
        self._green(at="2026-08-03T20:10:00+00:00")
        persistence.record_decision_transition("ABC", status="green")
        assert persistence.get_decision_transitions("ABC")[0]["occurred_at"].startswith("2026-08-03")

    def test_no_green_on_record_means_nothing_to_document(self, temp_db):
        # An earlier version stamped the current time here, which invented a
        # "fired today" for 33 of the 58 stored theses when the backfill ran.
        self._thesis()
        assert persistence.record_decision_transition("ABC", status="green") is False
        assert persistence.get_decision_transitions("ABC") == []

    def test_a_green_from_before_this_build_does_not_count(self, temp_db):
        # Caught on the first real row: PLTR's 20:35 green was picked up for a
        # thesis rebuilt at 22:19 the same night, which would have paired a
        # confirmation with a decision written after it happened.
        self._green(at="2020-01-01T00:00:00+00:00")
        self._thesis()
        assert persistence.record_decision_transition("ABC", status="green") is False

    def test_an_unknown_ticker_is_not_recorded(self, temp_db):
        assert persistence.record_decision_transition("NOPE", status="green") is False


class TestPositionExistsForIdea:
    """2026-08-09. The other half of the shadow book: it already holds the ideas
    the system said no to, which no real trade record can contain. This says
    which of them the owner took anyway -- the column that turns "does my
    system's opinion beat my own?" into a countable question."""

    def _build(self, ticker="ABC", trigger=100.0):
        return persistence.save_thesis(
            ticker=ticker, status="pending", source="screener",
            primary_setup={"type": "Breakout", "trigger": trigger, "stop": 95.0,
                           "atr_at_build": 2.0, "targets": []},
            decision="Watchlist", rubric_grade="B",
        )

    def _fill(self, ticker="ABC"):
        return persistence.create_position(
            ticker, entry_date="2026-08-02", entry_price=101.0, qty=10,
            entry_type="full",
            entry_setup={"type": "Breakout", "stop": 95.0, "atr_at_build": 2.0},
            initial_stop=95.0,
        )

    def test_untaken_idea_is_false(self, temp_db):
        idea_id = self._build()
        assert persistence.position_exists_for_idea(idea_id) is False

    def test_taken_idea_is_true(self, temp_db):
        idea_id = self._build()
        self._fill()
        assert persistence.position_exists_for_idea(idea_id) is True

    def test_only_the_build_that_was_bought_counts(self, temp_db):
        # A symbol screened twice in a week is two builds and only one may have
        # been acted on. Matching on ticker would mark both as taken and make
        # the comparison come out a tie every time.
        first = self._build(trigger=100.0)
        second = self._build(trigger=110.0)
        self._fill()
        assert persistence.position_exists_for_idea(first) is False
        assert persistence.position_exists_for_idea(second) is True

    def test_a_closed_trade_still_counts_as_taken(self, temp_db):
        # The question is "did the owner act on this idea", and a trade since
        # closed was still acted on.
        idea_id = self._build()
        self._fill()
        persistence.record_exit("ABC", exit_price=94.0, exit_qty=10,
                                exit_date="2026-08-05", source="exit_command")
        assert persistence.position_exists_for_idea(idea_id) is True

    def test_no_idea_id_is_false_never_an_error(self, temp_db):
        assert persistence.position_exists_for_idea(None) is False


class TestDeadNightStreak:
    """Broken-idea shelving (2026-08-11). A thesis whose price has fallen under
    its own stop is counted, night after night, and moved to the cold shelf once
    the streak reaches DEAD_NIGHTS_BEFORE_COLD -- so the waiting list the owner
    actually reads stays short, without any idea being deleted behind their
    back. The streak resets the moment price is back above the stop, and on any
    fresh build, because a new build carries a new stop."""

    def _save(self, decision="Watchlist"):
        return persistence.save_thesis(
            ticker="ABC", status="pending", source="screener",
            primary_setup={"trigger": 100.0, "stop": 95.0, "entry_zone": "99-100",
                           "atr_at_build": 2.0, "targets": []},
            decision=decision,
        )

    def _nights(self):
        return persistence.get_thesis("ABC")["dead_nights"]

    def test_a_fresh_thesis_has_no_streak(self, temp_db):
        self._save()
        assert self._nights() == 0

    def test_each_night_adds_one_and_the_total_comes_back(self, temp_db):
        self._save()
        assert persistence.bump_dead_night("ABC") == 1
        assert persistence.bump_dead_night("ABC") == 2
        assert self._nights() == 2

    def test_price_back_above_the_stop_starts_the_count_over(self, temp_db):
        # Without this, a thesis that dipped under its stop on three separate
        # weeks would be shelved on the third, having been fine in between.
        self._save()
        persistence.bump_dead_night("ABC")
        persistence.bump_dead_night("ABC")
        persistence.clear_dead_nights("ABC")
        assert self._nights() == 0

    def test_clearing_a_streak_that_is_already_zero_is_harmless(self, temp_db):
        self._save()
        persistence.clear_dead_nights("ABC")
        assert self._nights() == 0

    def test_a_rebuild_clears_the_streak(self, temp_db):
        # The rebuild writes a NEW stop, so a streak measured against the old
        # one says nothing about this thesis.
        self._save()
        persistence.bump_dead_night("ABC")
        self._save()
        assert self._nights() == 0

    def test_the_shelving_threshold_is_reachable_by_counting(self, temp_db):
        self._save()
        nights = 0
        for _ in range(persistence.DEAD_NIGHTS_BEFORE_COLD):
            nights = persistence.bump_dead_night("ABC")
        assert nights == persistence.DEAD_NIGHTS_BEFORE_COLD
