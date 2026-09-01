"""Tests for run_files.py -- where a run's leftovers live and when they die.

The counts in these docstrings are the real ones from 2026-08-09: 259 files in
the project root, 231 of them per-run payloads, about 50 MB, plus a 38 MB
ack_listener.log.
"""

import time

import pytest

import run_files


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """run_files talks to module-level paths, so both have to move together --
    a test that swept the REAL project root would be a very bad test."""
    root = tmp_path / "project"
    root.mkdir()
    runs = root / "_runs"
    monkeypatch.setattr(run_files, "PROJECT_ROOT", root)
    monkeypatch.setattr(run_files, "RUNS_DIR", runs)
    return root, runs


def _age(path, days):
    old = time.time() - days * 86400
    import os
    os.utime(path, (old, old))


class TestTidyRoot:
    def test_run_payloads_move_out_of_the_root(self, sandbox):
        root, runs = sandbox
        for name in ("_decision_NVDA.json", "_monitorall_20260808.json",
                     "scratch_report.md", "_playbook_123.jpg"):
            (root / name).write_text("x")
        run_files.tidy_root()
        assert sorted(p.name for p in runs.iterdir()) == [
            "_decision_NVDA.json", "_monitorall_20260808.json",
            "_playbook_123.jpg", "scratch_report.md",
        ]
        assert not list(root.glob("_decision_*"))

    def test_source_files_are_never_touched(self, sandbox):
        root, _ = sandbox
        for name in ("persistence.py", "CLAUDE.md", "trading_new.db", "start.bat"):
            (root / name).write_text("x")
        run_files.tidy_root()
        for name in ("persistence.py", "CLAUDE.md", "trading_new.db", "start.bat"):
            assert (root / name).exists()

    def test_dry_run_moves_nothing(self, sandbox):
        root, runs = sandbox
        (root / "_decision_NVDA.json").write_text("x")
        moved = run_files.tidy_root(dry_run=True)
        assert len(moved) == 1
        assert (root / "_decision_NVDA.json").exists()


class TestSweep:
    def test_only_old_payloads_are_deleted(self, sandbox):
        _, runs = sandbox
        runs.mkdir()
        fresh = runs / "_decision_FRESH.json"
        stale = runs / "_decision_STALE.json"
        fresh.write_text("x")
        stale.write_text("x")
        _age(stale, run_files.KEEP_DAYS + 1)
        removed = run_files.sweep()
        assert [p.name for p in removed] == ["_decision_STALE.json"]
        assert fresh.exists()
        assert not stale.exists()

    def test_a_payload_keeps_its_first_week(self, sandbox):
        # Its most valuable hour is the one right after a failure, when it is
        # the only evidence of what the run was thinking.
        _, runs = sandbox
        runs.mkdir()
        p = runs / "_decision_NVDA.json"
        p.write_text("x")
        _age(p, run_files.KEEP_DAYS - 1)
        assert run_files.sweep() == []
        assert p.exists()

    def test_a_non_payload_inside_runs_is_never_deleted(self, sandbox):
        # The sweep runs unattended with nobody watching; it only ever touches
        # names it recognises.
        _, runs = sandbox
        runs.mkdir()
        keeper = runs / "notes.md"
        keeper.write_text("x")
        _age(keeper, 999)
        assert run_files.sweep() == []
        assert keeper.exists()

    def test_sweep_never_reaches_the_project_root(self, sandbox):
        root, runs = sandbox
        runs.mkdir()
        loose = root / "_decision_NVDA.json"
        loose.write_text("x")
        _age(loose, 999)
        assert run_files.sweep() == []
        assert loose.exists()

    def test_missing_runs_dir_is_not_an_error(self, sandbox):
        assert run_files.sweep() == []


class TestRotateLog:
    def test_a_small_log_is_left_alone(self, sandbox):
        root, _ = sandbox
        log = root / "small.log"
        log.write_text("x" * 100)
        assert run_files.rotate_log(log) is False
        assert not (root / "small.log.1").exists()

    def test_an_oversized_log_rolls_to_generation_one(self, sandbox):
        root, _ = sandbox
        log = root / "big.log"
        log.write_text("x" * (run_files.LOG_MAX_BYTES + 1))
        assert run_files.rotate_log(log) is True
        assert (root / "big.log.1").exists()
        assert not log.exists()

    def test_older_generations_shift_and_the_oldest_is_dropped(self, sandbox):
        root, _ = sandbox
        log = root / "big.log"
        (root / "big.log.1").write_text("gen1")
        (root / "big.log.2").write_text("gen2")
        log.write_text("x" * (run_files.LOG_MAX_BYTES + 1))
        run_files.rotate_log(log, keep=2)
        # gen2 was the oldest kept generation and is gone; gen1 became gen2.
        assert (root / "big.log.2").read_text() == "gen1"

    def test_rotation_keeps_the_evidence_rather_than_truncating(self, sandbox):
        # The moment a log gets too big is often the moment something has been
        # going wrong repeatedly -- the worst time to throw it away.
        root, _ = sandbox
        log = root / "big.log"
        body = "important" + "x" * run_files.LOG_MAX_BYTES
        log.write_text(body)
        run_files.rotate_log(log)
        assert (root / "big.log.1").read_text().startswith("important")


def test_housekeeping_never_raises(sandbox, monkeypatch):
    # A failed sweep must not cost a scheduled job its actual work.
    monkeypatch.setattr(run_files, "tidy_root", lambda **kw: (_ for _ in ()).throw(OSError("boom")))
    assert run_files.housekeeping() == {"moved": [], "swept": [], "rotated": []}
