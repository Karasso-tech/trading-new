"""Unit tests for persistence.py's circuit breaker (Hardening Pass item 8).

Uses an isolated temp SQLite DB (never the real trading_new.db) via monkeypatching
persistence.DB_PATH -- see temp_db fixture below.
"""

from datetime import datetime, timedelta, timezone

import pytest

import persistence


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(persistence, "DB_PATH", db_path)
    persistence.init_db()
    return db_path


def _insert_closed_position(ticker: str, exit_reasons: list, when: datetime) -> None:
    """One closed position with N sequential exits rows; the LAST reason in the
    list is what the circuit breaker actually looks at (the final exit)."""
    ts = when.isoformat()
    with persistence._db() as conn:
        conn.execute(
            "INSERT INTO thesis (ticker, status, sleeve, updated_at) VALUES (?, 'closed', 'swing', ?)",
            (ticker, ts),
        )
        cur = conn.execute(
            "INSERT INTO positions (ticker, entry_date, entry_price, qty, entry_type, "
            "initial_stop, current_stop, status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'closed', ?)",
            (ticker, "2026-01-01", 100.0, 100, "full", 90.0, 90.0, ts),
        )
        position_id = cur.lastrowid
        for reason in exit_reasons:
            conn.execute(
                "INSERT INTO exits (position_id, ticker, exit_date, exit_price, exit_qty, "
                "exit_reason, source, created_at) VALUES (?, ?, ?, ?, ?, ?, 'exit_command', ?)",
                (position_id, ticker, "2026-01-05", 95.0, 50, reason, ts),
            )


def test_no_closed_positions_streak_is_zero(temp_db):
    assert persistence.get_consecutive_stopout_streak() == 0


def test_all_stop_outs_streak_counts_all(temp_db):
    base = datetime.now(timezone.utc)
    _insert_closed_position("AAA", ["stop"], base + timedelta(minutes=1))
    _insert_closed_position("BBB", ["stop"], base + timedelta(minutes=2))
    _insert_closed_position("CCC", ["stop"], base + timedelta(minutes=3))
    assert persistence.get_consecutive_stopout_streak() == 3


def test_non_stop_final_exit_breaks_streak_walking_backward(temp_db):
    base = datetime.now(timezone.utc)
    # Oldest first: a winner, then two consecutive stop-outs (most recent).
    _insert_closed_position("OLD", ["target_1"], base + timedelta(minutes=1))
    _insert_closed_position("MID", ["stop"], base + timedelta(minutes=2))
    _insert_closed_position("NEW", ["stop"], base + timedelta(minutes=3))
    assert persistence.get_consecutive_stopout_streak() == 2


def test_multi_tranche_position_uses_final_exit_reason(temp_db):
    base = datetime.now(timezone.utc)
    # Position closed via target_1 partial, then a stop on the runner -- the LAST
    # exit (stop) is what counts, not the first.
    _insert_closed_position("MULTI", ["target_1", "stop"], base + timedelta(minutes=1))
    assert persistence.get_consecutive_stopout_streak() == 1


def test_threshold_unset_returns_none_and_breaker_inactive(temp_db, monkeypatch):
    monkeypatch.setattr(persistence, "DB_PATH", temp_db)  # no .env alongside it
    assert persistence.get_circuit_breaker_threshold() is None
    base = datetime.now(timezone.utc)
    _insert_closed_position("AAA", ["stop"], base + timedelta(minutes=1))
    _insert_closed_position("BBB", ["stop"], base + timedelta(minutes=2))
    _insert_closed_position("CCC", ["stop"], base + timedelta(minutes=3))
    status = persistence.circuit_breaker_status()
    assert status["threshold"] is None
    assert status["streak"] == 3
    assert status["tripped"] is False  # never trips on an invented default


def test_threshold_set_and_streak_meets_it_trips(temp_db):
    env_path = temp_db.parent / ".env"
    env_path.write_text("CIRCUIT_BREAKER_STOPOUTS=3\n", encoding="utf-8")
    base = datetime.now(timezone.utc)
    _insert_closed_position("AAA", ["stop"], base + timedelta(minutes=1))
    _insert_closed_position("BBB", ["stop"], base + timedelta(minutes=2))
    _insert_closed_position("CCC", ["stop"], base + timedelta(minutes=3))
    status = persistence.circuit_breaker_status()
    assert status["threshold"] == 3
    assert status["streak"] == 3
    assert status["tripped"] is True


def test_threshold_set_but_streak_below_it_does_not_trip(temp_db):
    env_path = temp_db.parent / ".env"
    env_path.write_text("CIRCUIT_BREAKER_STOPOUTS=3\n", encoding="utf-8")
    base = datetime.now(timezone.utc)
    _insert_closed_position("AAA", ["stop"], base + timedelta(minutes=1))
    status = persistence.circuit_breaker_status()
    assert status["streak"] == 1
    assert status["tripped"] is False


def test_invalid_threshold_value_treated_as_unset(temp_db):
    env_path = temp_db.parent / ".env"
    env_path.write_text("CIRCUIT_BREAKER_STOPOUTS=not_a_number\n", encoding="utf-8")
    assert persistence.get_circuit_breaker_threshold() is None
